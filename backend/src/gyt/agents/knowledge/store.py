"""知识库的 embedding 与向量存储 —— ingest 与 retriever 的**单一入口**。

===========================================================================
为什么单独一层
---------------------------------------------------------------------------
    建库(ingest)和查询(retriever)必须用**同一个 embedding 模型 + 同一个 collection**,
    否则查询向量和入库向量不在一个空间里,检索永远对不上。把两者收到这里一处定义,
    杜绝「入库用 A 模型、查询用 B 模型」这类静默失灵。

    · embedding:本地 BGE-M3(config.embedding_model)。**首次实例化会加载约 2.2GB 权重**,
      慢;所以用 lru_cache 一个进程只加载一次。CPU 跑,归一化便于余弦相似。
      BGE-M3 是 instruction-free 的稠密检索模型,查询侧不用加特殊前缀。
    · 向量库:Chroma,持久化到 config.chroma_dir(data/chroma/)。langchain-chroma 1.x
      自动落盘,不用手动 persist。collection 名字固定,别改(改了等于换库)。
===========================================================================
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from gyt.config import get_settings

if TYPE_CHECKING:  # 仅类型检查用,运行期不触发重导入
    from langchain_chroma import Chroma
    from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# ⚠️ langchain_huggingface / langchain_chroma 会在 import 时把 torch/transformers/chromadb
# 整条重依赖拉进来(实测冷启动 import 就要 2 分钟)。**绝不在模块顶层 import**,否则
# `import gyt.graph`(建图/起服务/跑测试都会走)会被平白拖慢、甚至超时。
# 一律在函数内部惰性 import —— 只有真的要做 embedding/建库时才付这个代价。

COLLECTION_NAME = "gyt_specs"
"""规范库的 collection 名。入库与查询必须一致,定了别改。"""


# ---------------------------------------------------------------------------
# 两把锁 —— 2026-08-23 线上事故的修复。**刻意分开,合并成一把会静默出事。**
#
# 病状(生产,thread 01a02c51-afc7-7110-81a8-55ade6b37826,01:52):
#     同一个回合里 `search_regulation` 被调了两次,LangGraph **并行**执行一个回合的
#     多个 tool call(与 cad 那次 `4cb2809` 同一个 bug 类),两条都走
#     `asyncio.to_thread` 落在不同线程,同时第一次建 chromadb 客户端。结果:
#         线程 A  AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
#         线程 B  KeyError: '/app/data/chroma'
#     两条各 12.0 秒后被 tool_guard 兜成 fail(INTERNAL),界面上两个红叉,
#     那一轮总耗时 43.2 秒。**随后的 4 次调用全绿** —— 这是判据的关键。
#
# 根因(chromadb 1.5.9,`chromadb/api/shared_system_client.py:29-49`):
#     _create_system_if_not_exists 是一段**无锁的 check-then-act**,操作的是类级
#     全局字典 _identifier_to_system。它旁边那把 _refcount_lock **只护 refcount、
#     不护建系统这条路**。两个线程都判「还没建」→ 都建一个 System → 后者覆盖前者,
#     前者成了没人认领的孤儿(而它的 Rust bindings 已经被顶掉)。接下来:
#         失败的线程走 Client.__init__ 的 except → _release_system
#         → refcount 一路减到 0 → **把整个 System pop 掉并 stop()**
#         → 还卡在 _create_system_if_not_exists 里的线程,那句
#           `return cls._identifier_to_system[identifier]`(第 49 行)当场 KeyError。
#     ⚠️ 这段机制**订正过一次**(2026-08-23 对抗性复审抓的):原先写的是「失败那条
#        此时还没 _increment_refcount,所以 _decrement_refcount 查不到 key 返回 0」——
#        错。`SharedSystemClient.__init__` 在建完系统的**下一句**就加了 refcount
#        (shared_system_client.py:26),而 `Client.__init__` 的 `super().__init__()`
#        在 `try:` **之前**(api/client.py:69/70),所以走到 except 时一定已经加过。
#        给 inc/dec 挂仪表实测:每一次 dec 都找得到 key,pop 是**正常减到 0** 触发的。
#        结论(pop → 另一条线程第 49 行 KeyError)不变,只有中间那一步的解释变了。
#     所以竞态**只在进程首次创建时存在**:字典里一旦有了那个 key,后面全走 else 分支。
#     (热身之后 40 次并发零失败,实测印证。)
#
# 为什么是加锁而不是缓存客户端:本机 4 线程 × 20 轮 = 80 次调用实测
#     现状(每次新建、无锁)  失败约 30~50 次(多轮:28/33/37/44/48/49,抖动很大)
#     只加锁(每次新建)      失败  0 次
#     锁 + 缓存客户端        失败  0 次
#     —— 后两者同效,而缓存会新引入「chromadb 把那个 System pop 掉之后,缓存住的
#     客户端永久 KeyError、不重启不自愈」的风险。生产热修取风险最低的那个。
#
# 🔴 **为什么是两把而不是一把**:合并的话 `/library`(count_chunks_by_doc)会去抢
#     同一把锁,而那把锁正被 get_embeddings 攥着做 BGE-M3 的首次加载(约 12 秒)。
#     **阻塞不是异常**,count_chunks_by_doc 那句 `except Exception: return {}` 兜不住 ——
#     表现是资料库页面转十几秒,而日志、控制台、测试三处都不说话。而 count_chunks_by_doc
#     的头注明写着「只读 metadata、**绝不加载 embedding 模型**」,合并等于把这条作废。
# ---------------------------------------------------------------------------

_CLIENT_LOCK: Final = threading.Lock()
"""护 chromadb 客户端的**构造**(不护检索)。见上面那段推演。"""

_EMBEDDINGS_LOCK: Final = threading.Lock()
"""护 BGE-M3 的**首次加载**。

`lru_cache` 在调用期间**不持锁** —— 两个线程同时 miss 就会各真加载一份。
线上实测那一轮就是这样(01:52:29.327 / .374 两条 `Loading SentenceTransformer`),
而 BGE-M3 fp32 约 2.27GB、那台机器一共 1966MB,当时 swap 已用 1306MB。
"""


class VectorStackUnavailableError(RuntimeError):
    """向量库那条依赖链在这台机器上装不上 —— **这不是配置错误,是环境限制。**

    为什么要有这个异常,而不是让原始的 ``ModuleNotFoundError`` 冒出去:
    那条链是 ``langchain_chroma`` → ``langchain_huggingface`` → ``transformers``
    → ``sentence-transformers`` → **torch**,而 torch 2.13 没有 macosx_x86_64 轮子
    (CLAUDE.md 第一句就写着 `make setup` 在 Intel Mac 上必失败)。

    🔴 裸报错会让人**照着包名一个一个去装**:装完 chroma 撞 huggingface、
    装完 huggingface 撞 transformers,四轮之后才发现尽头是 torch 那堵墙。
    2026-08-22 跑整链评测时就这么撞了一次 —— 日志里只有
    ``ModuleNotFoundError: No module named 'langchain_chroma'`` 这一句,
    完全看不出它背后是一整条装不上的链。

    所以这里一次把话说完,并且**直接给出那条走得通的路**(容器)。
    """


_VECTOR_STACK_HINT = (
    "这台机器上装不了向量库那条依赖链(langchain_chroma → langchain_huggingface "
    "→ transformers → torch,而 torch 在 Intel Mac 上没有轮子)。\n"
    "知识库相关的活儿要在容器里跑:\n"
    "  建索引:docker compose run --rm --no-deps backend python -m gyt.agents.knowledge.ingest\n"
    "  跑测试:make test-docker\n"
    "  起服务:make dev-docker\n"
    "⚠️ 别照着缺的包名一个一个装 —— 尽头是 torch,装不上。"
)


def _lazy_import(module: str, attr: str) -> object:
    """惰性 import 一个向量库相关的名字;链断了就抛一句说得清的话。

    ⚠️ 只接 ``ModuleNotFoundError``,不接 ``ImportError`` 全家:
    后者会把「装是装上了但版本不兼容」也吞成这句提示,而那两件事的修法完全不同
    (一个是换机器/进容器,一个是对版本)。
    """
    try:
        return getattr(__import__(module, fromlist=[attr]), attr)
    except ModuleNotFoundError as exc:
        raise VectorStackUnavailableError(f"{_VECTOR_STACK_HINT}\n(缺的是 {exc.name})") from exc


@lru_cache(maxsize=1)
def _load_embeddings() -> HuggingFaceEmbeddings:
    """真加载 BGE-M3。**只许 `get_embeddings` 在 `_EMBEDDINGS_LOCK` 里调它。**

    直接调它等于绕过锁,并发首调会各加载一份 2.27GB —— 那正是要修的病。
    单独拆出来是因为锁必须在 `lru_cache` **外面**:套在里面的话两个线程照样
    都 miss、都进函数体、都真加载一遍,`lru_cache` 不会在调用返回后再复查一次。
    """
    # 惰性 + 链断了说人话,见 _lazy_import / VectorStackUnavailableError
    embeddings_cls = _lazy_import("langchain_huggingface", "HuggingFaceEmbeddings")

    settings = get_settings()
    logger.info("加载 embedding 模型 %s(首次约 2.2GB,慢属正常)", settings.embedding_model)
    return embeddings_cls(  # type: ignore[operator]
        model_name=settings.embedding_model,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def get_embeddings() -> HuggingFaceEmbeddings:
    """本地 BGE-M3 embedding(进程内只加载一次;**并发首调也只加载一次**)。

    ⚠️ 首次调用会加载 ~2.2GB 权重(本地没缓存时还要先下载),慢是正常的。
    Dockerfile 已在构建期把权重烤进镜像(冷启动零下载);本地首跑会真下一次。
    """
    # ① 惰性 import 放在**锁外**。锁里跑 `__import__` 会和 CPython 自己的模块导入锁
    #    形成锁序反转,而 importlib 的死锁检测只看模块锁之间的环、看不见外来的锁 ——
    #    表现是**静默挂起**,不是报错。这一句同时保住了「链断了一次把话说完」那条
    #    契约(test_knowledge_store.py 里走 get_embeddings 的那条守门用例
    #    `test_embedding_那一半也走同一条路`;另两条从 get_vectorstore 进,撞的是
    #    langchain_chroma 那句):异常在进锁之前就抛出来,手上不持任何锁。
    _lazy_import("langchain_huggingface", "HuggingFaceEmbeddings")
    # ② 进锁做真加载。命中缓存时锁内只是一次字典查找(约 100ns),
    #    对照一次检索 2.1~2.8 秒,可以忽略。
    with _EMBEDDINGS_LOCK:
        return _load_embeddings()


# 把 `lru_cache` 的两个查询接口转出来 —— 上面把真加载拆成了 `_load_embeddings`,
# 不转的话 `store.get_embeddings.cache_clear()` 当场 AttributeError,而
# `test_knowledge_store.py` 里那条「embedding 那一半也走同一条路」正是这么调的。
# `cache_info` 一起转:排查「BGE-M3 到底加载了几次」时第一个会伸手去拿的就是它,
# 而那正是这次事故的核心问题(线上那一轮真加载了两份,机器一共 1966MB)。
get_embeddings.cache_clear = _load_embeddings.cache_clear  # type: ignore[attr-defined]
get_embeddings.cache_info = _load_embeddings.cache_info  # type: ignore[attr-defined]


def get_vectorstore() -> Chroma:
    """规范向量库句柄(持久化在 config.chroma_dir)。

    每次返回一个新句柄。**句柄不轻**(这里以前写的是「句柄本身很轻」,那句是错的,
    而且正是代码写成每次新建的理由):`Chroma(...)` 内部会构造一个
    `chromadb.PersistentClient`,它要过 chromadb 的**进程级**注册表
    `SharedSystemClient._identifier_to_system`,给 refcount **+2**(`Client` 自己一次,
    它内部的 `AdminClient.from_system` 再一次,api/client.py:104;而我们从不 close),
    并且那条创建路径**没有锁** —— 2026-08-23 线上两条红就是这么来的,
    完整推演在文件上方 `_CLIENT_LOCK` 那段。

    ⚠️ 真正贵的仍然是 embedding 模型,那个只加载一次(`get_embeddings`)。
    ⚠️ **`get_embeddings()` 必须在 `_CLIENT_LOCK` 外面调** —— 它自己那把锁会在首次
       加载时攥住约 12 秒,拿进来就等于让 `/library` 陪着一起等(见上方那段的 🔴)。
    """
    # 惰性 import 与取 embedding 都在**锁外**:前者防锁序反转,后者防 /library 被拖住。
    chroma_cls = _lazy_import("langchain_chroma", "Chroma")  # 同上
    settings = get_settings()
    embeddings = get_embeddings()

    # 锁只包住构造这一下,不包检索 —— 并发查询的速度一点不受影响。
    with _CLIENT_LOCK:
        return chroma_cls(  # type: ignore[operator]
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings,
            persist_directory=str(settings.chroma_dir),
        )


def count_chunks_by_doc() -> dict[tuple[str, str, str], int]:
    """数每份文档在向量库里的 chunk 数,键 (scope, project_id, source)。供 /library 标入库进度。

    只读 metadata、**绝不加载 embedding 模型** —— 走 chromadb 原生 client(不经 get_vectorstore,
    否则一开资料库就把 2.2GB BGE-M3 拉进内存)。库还没建 / 并发锁 / 版本差异都回空 dict,绝不抛:
    浏览端点不该被向量库的临时状态拖垮(数不出就当「暂无 / 入库中」,下次刷新再数)。
    """
    import chromadb  # 惰性:见文件顶部说明(chromadb 也不轻,别进 import gyt.graph 的热路径)

    try:
        # 与 get_vectorstore 抢**同一把** _CLIENT_LOCK。全仓建 chromadb 客户端的地方
        # **只有两处**:`get_vectorstore`(检索与入库共用它)和这里。identifier 都是
        # 同一个 chroma_dir,不一起护住的话「一边问规范一边开资料库」照样能撞出那个
        # 竞态 —— 而这条路整个包在下面那句 `except: return {}` 里,撞了的表现是
        # 资料库页面显示「暂无」,零报错。`test_数向量块与建客户端并发_不许因竞态回空`
        # 是**唯一**能抓到「只给 get_vectorstore 加锁、漏了这半边」的用例。
        # `get_settings()` 与 get_vectorstore 一样提到锁外:config 的 `_ensure_dir`
        # 是「访问即 mkdir」,锁里少一次文件系统调用,两条路的写法也一致。
        chroma_dir = str(get_settings().chroma_dir)
        with _CLIENT_LOCK:
            client = chromadb.PersistentClient(path=chroma_dir)
        col = client.get_collection(COLLECTION_NAME)
        metas = col.get(include=["metadatas"]).get("metadatas") or []
    except Exception:  # noqa: BLE001 —— 库缺失 / 并发锁 / 版本差异一律按「暂时数不出」处理
        logger.debug("数向量块失败,当作暂无", exc_info=True)
        return {}
    return dict(
        Counter(
            (
                str(m.get("scope") or ""),
                str(m.get("project_id") or ""),
                str(m.get("source") or ""),
            )
            for m in metas
        )
    )


__all__ = ["COLLECTION_NAME", "count_chunks_by_doc", "get_embeddings", "get_vectorstore"]
