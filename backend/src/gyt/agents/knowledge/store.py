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
from collections import Counter
from functools import lru_cache
from typing import TYPE_CHECKING

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
def get_embeddings() -> HuggingFaceEmbeddings:
    """本地 BGE-M3 embedding(进程内只加载一次)。

    ⚠️ 首次调用会加载 ~2.2GB 权重(本地没缓存时还要先下载),慢是正常的。
    Dockerfile 已在构建期把权重烤进镜像(冷启动零下载);本地首跑会真下一次。
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


def get_vectorstore() -> Chroma:
    """规范向量库句柄(持久化在 config.chroma_dir)。

    每次返回一个新句柄但共享同一个持久化目录与同一个(缓存的)embedding ——
    句柄本身很轻,真正贵的是 embedding 模型,那个只加载一次。
    """
    chroma_cls = _lazy_import("langchain_chroma", "Chroma")  # 同上

    settings = get_settings()
    return chroma_cls(  # type: ignore[operator]
        collection_name=COLLECTION_NAME,
        embedding_function=get_embeddings(),
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
        client = chromadb.PersistentClient(path=str(get_settings().chroma_dir))
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
