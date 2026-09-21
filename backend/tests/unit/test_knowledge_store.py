"""``agents/knowledge/store.py`` 的守门断言 —— 两组,判据完全不同,别混着读。

【第一组:链断了怎么说话】(下面前三条,2026-08-22 起)
真正的向量库行为(建库、查询、scope 过滤)在本机测不了:它要 torch →
sentence-transformers → 2.2GB 权重,而 **torch 2.13 没有 macosx_x86_64 轮子**,
Intel Mac 上装不上(CLAUDE.md 第一句写的就是这个)。那部分靠 `make test-docker`。
这一组测的是**那条链断掉时说的话**,而它恰恰是最容易在容器里"静默通过"的东西 ——
所以一律用 monkeypatch **人造**断链,不依赖跑测试的这台机器真的缺包。
依赖真环境的话,这几条在容器里会因为「包都在、异常压根不抛」而变成空转,
而空转的用例和通过的用例在报告里长得一模一样。

【第二组:两把锁】(文件下半「第二组」那道横线以后,2026-08-23 线上竞态的回归网)
病状与完整推演在 `store.py` 上方 `_CLIENT_LOCK` 那段注释里,这里只记怎么测:

  · **chromadb 是真跑的**(本机装得上、用得了),`_真建客户端的假Chroma` 里面
    真建一个 `chromadb.PersistentClient` —— 竞态就在那一句里
    (chromadb 1.5.9 `shared_system_client.py:29-48` 无锁的 check-then-act)。
    整个假掉的话 `_CLIENT_LOCK` 护的是一段空气,把锁删了测试照绿。
  · **torch / langchain_chroma / langchain_huggingface / sentence_transformers
    一个都不许碰**(本机没有,容器里碰了要吃 2.2GB),所以走
    `_装假的向量栈` 打掉 `store._lazy_import`。
  · 🔴 **竞态只存在于「这个 path 第一次建 System」那一瞬**,而
    `SharedSystemClient._identifier_to_system` 是**类变量**、跨用例活着。
    不清干净的话并发用例全走 else 分支变成空转 —— 见 `_每条用例都从冷启动起步`。

这一节做过变异测试(2026-08-23,本机):把 store.py 里两处 `with _CLIENT_LOCK:`
都拿掉之后 ——
    整节连跑 8 遍   下面四条并发用例 **8/8 全红**
    第 1 条单跑 12 遍  **12/12 见红**
    裸调 `PersistentClient` 对照组:4 线程 × 20 轮 = 80 次,炸 44 次
        (AttributeError 23 / KeyError 18 / ValueError 3 —— 前两种与线上那两条同款)
⚠️ **复现率对「这个库文件是不是头一回被建」极敏感**:改用「预热时顺手建一次客户端」
   的对照组,同样的两线程单栅栏只剩 13/30 轮见红。所以 `_预热()` 刻意只热
   embedding 与 mkdir、**一下都不碰 chromadb** —— 顺手加一句 `get_vectorstore()`
   看着无害,实际是把这一节的灵敏度砍掉一多半,而且不会有任何东西说话。
"""

from __future__ import annotations

import builtins
import threading
import time
from collections.abc import Callable, Iterator, Sequence

import chromadb
import pytest
from chromadb.api.shared_system_client import SharedSystemClient

from gyt.agents.knowledge import store
from gyt.config import get_settings


def _断链(monkeypatch: pytest.MonkeyPatch, 缺的模块: str) -> None:
    """让某个模块 import 时抛 ModuleNotFoundError,别的照常。"""
    真的 = builtins.__import__

    def 假的(name: str, *args: object, **kwargs: object) -> object:
        if name == 缺的模块:
            raise ModuleNotFoundError(f"No module named {name!r}", name=缺的模块)
        return 真的(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", 假的)


def test_向量库那条链断了时一次把话说完(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 这条守的是「别让人照着包名一个一个去装」。

    2026-08-22 跑整链评测时真撞过:日志里只有
    ``ModuleNotFoundError: No module named 'langchain_chroma'``。
    照它去装的话,装完 chroma 撞 huggingface、装完 huggingface 撞 transformers,
    **四轮之后**才发现尽头是 torch 那堵墙。所以这句话必须一次说清三件事:
    ① 这是环境限制不是配置错;② 尽头是 torch;③ 那条走得通的路(容器)。
    """
    _断链(monkeypatch, "langchain_chroma")

    with pytest.raises(store.VectorStackUnavailableError) as caught:
        store.get_vectorstore()

    话 = str(caught.value)
    assert "torch" in 话, "没点破尽头是 torch,人还会接着装下去"
    assert "make test-docker" in 话 or "dev-docker" in 话, "没给出那条走得通的路"
    assert "别照着缺的包名一个一个装" in 话
    assert "langchain_chroma" in 话, "没说这次具体缺的是哪个,排查时对不上号"


def test_embedding_那一半也走同一条路(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个入口(``get_vectorstore`` / ``get_embeddings``)都要说人话。

    只修一个的表现很隐蔽:先撞哪一个取决于调用顺序,而两条路径的报错完全不同。
    """
    store.get_embeddings.cache_clear()  # lru_cache 会把上一次的结果扣住
    _断链(monkeypatch, "langchain_huggingface")

    with pytest.raises(store.VectorStackUnavailableError) as caught:
        store.get_embeddings()

    assert "torch" in str(caught.value)
    assert "langchain_huggingface" in str(caught.value)


def test_版本不兼容那类错不许被吞成这句话(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 只接 ``ModuleNotFoundError``,不接 ``ImportError`` 全家。

    「包装上了但版本对不上」和「这台机器装不了」是两回事,修法完全不同
    (一个是对版本,一个是换机器/进容器)。把后者的提示贴到前者头上,
    人会跑去开容器,而容器里同样会撞版本问题 —— 白绕一圈。
    """
    真的 = builtins.__import__

    def 版本炸(name: str, *args: object, **kwargs: object) -> object:
        if name == "langchain_chroma":
            raise ImportError("cannot import name 'Chroma' from 'langchain_chroma'")
        return 真的(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", 版本炸)

    with pytest.raises(ImportError) as caught:
        store.get_vectorstore()
    assert not isinstance(caught.value, store.VectorStackUnavailableError)


# ===========================================================================
# 第二组:两把锁(2026-08-23 线上竞态的回归网)
#
# 下面这一整节的存在理由,是生产上那一条 thread(01a02c51,01:52):同一个回合里
# `search_regulation` 被并行调了两次,两条都落在不同线程、同时第一次建 chromadb
# 客户端,各炸 12.0 秒,界面上两个红叉。**随后 4 次调用全绿** —— 这就是判据:
# 竞态只在「进程第一次建这个 path 的 System」那一瞬存在。
# ===========================================================================

_压测线程数 = 4
"""加压用例开几个线程。取 4 是照着复现实验来的(4×20 无锁炸 44 次)。"""

_压测轮数 = 12
"""加压用例跑几轮。每轮都把 chromadb 的类级缓存清掉,才算「又一次首次创建」。

轮数是拿总耗时换命中率买的。按**最保守**的那组对照数算(预热时已建过一次客户端,
单轮见红率 13/20),12 轮漏网概率约 (7/20)^12 ≈ 1e-5;实际这条用例的条件更狠
(见文件头注),变异测试里 8 遍 8 遍红。本机 12 轮约 1.6 秒。
"""

_对撞轮数 = 10
"""count_chunks_by_doc 与 get_vectorstore 对撞几轮。按最保守的对照数(单轮
count 回 {} 的概率 7/20)算,10 轮漏网概率约 (13/20)^10 ≈ 1.3%;
变异测试里(锁真拿掉)8 遍 8 遍红。"""

_假构造耗时秒 = 0.05
"""假类构造里 sleep 多久 —— 用来把「两个线程同时在里面」的窗口撑开。

太小(比如 0)的话,线程 A 可能在线程 B 被调度到之前就构造完了,
「最大并发数 == 1」这条断言就会因为**根本没并发过**而假绿。
"""


class _假Embeddings:
    """站 ``HuggingFaceEmbeddings`` 的位子。构造必须**便宜** —— 真货是 2.2GB 权重。"""

    def __init__(self, **_kwargs: object) -> None:
        pass


class _真建客户端的假Chroma:
    """站 ``langchain_chroma.Chroma`` 的位子,但**里面真的建一个 chromadb 客户端**。

    ⚠️ 别"简化"成什么都不做的空壳:要测的竞态就在 ``chromadb.PersistentClient(...)``
    那一句里,空壳一装,`_CLIENT_LOCK` 护的是一段空气 —— **把锁整个删掉测试照绿**。
    langchain_chroma 本身在 Intel Mac 上装不上(尽头是 torch),所以只能这么替:
    真货那层薄壳我们不关心,底下那个真客户端才是出事的地方。

    ⚠️ 三个参数**只收关键字**,与 `get_vectorstore` 里的调用逐字对应。哪天那边改了
    参数名,这里会 TypeError 当场红 —— 这是**要的**:假货悄悄跟不上真调用,
    等于这一整节从此在测别的东西。
    """

    def __init__(
        self, *, collection_name: str, embedding_function: object, persist_directory: str
    ) -> None:
        self.collection_name = collection_name
        self.embedding_function = embedding_function
        self.client = chromadb.PersistentClient(path=persist_directory)


def _清掉embedding加载缓存() -> None:
    """清 `_load_embeddings` 的 lru_cache,并且容忍它此刻是个被换掉的假货。

    ⚠️ 为什么要容忍:fixture 的 finalizer 是**逆序**跑的,而 `monkeypatch` 比本文件的
    autouse fixture 先 setup(conftest 的 `_isolated_settings` 依赖它),
    所以收尾时 monkeypatch 还没撤 —— `store._load_embeddings` 可能正是某条用例塞进去的
    普通函数,身上没有 `cache_clear`。直接调会 AttributeError,而那声报错会**盖住**
    用例本身真正的失败信息,人会跑去查 fixture。
    """
    清 = getattr(store._load_embeddings, "cache_clear", None)
    if 清 is not None:
        清()


@pytest.fixture(autouse=True)
def _每条用例都从冷启动起步() -> Iterator[None]:
    """把两处**进程级**全局状态在用例前后各清一次。

    🔴 不清就复现不出这个 bug,而且是**静默**复现不出:

      · chromadb 的 `SharedSystemClient._identifier_to_system` 是**类变量**,
        跨用例、跨文件一直活着。竞态只存在于「这个 path 第一次建 System」那一瞬
        (字典里一旦有了 key,`_create_system_if_not_exists` 后面全走 else 分支)。
        上一条用例留下的 key 会让本条用例的并发变成空转 ——
        而空转的用例和通过的用例在报告里长得一模一样。
      · `_load_embeddings` 的 lru_cache 同理:命中缓存的话「并发首调只加载一次」
        这条断言永远成立,测了个寂寞。

    ⚠️ 清的是 `store._load_embeddings.cache_clear`,不是 `store.get_embeddings.cache_clear`
    —— 后者只是前者转出来的同一个函数(`store.py` 末尾那行赋值),这里直接找源头,
    免得哪天转发那行被删掉,fixture 跟着一起哑掉。
    """
    SharedSystemClient.clear_system_cache()
    _清掉embedding加载缓存()
    yield
    SharedSystemClient.clear_system_cache()
    _清掉embedding加载缓存()


def _装假的向量栈(
    monkeypatch: pytest.MonkeyPatch,
    chroma类: type = _真建客户端的假Chroma,
    embeddings类: type = _假Embeddings,
) -> None:
    """把 `store._lazy_import` 换成按模块名分发的假货。

    为什么打 `_lazy_import` 而不是塞 `sys.modules`:
    ① `get_vectorstore` / `get_embeddings` 都只经它拿类,一处同时替掉两个;
    ② **容器里也生效** —— 那边 langchain_chroma / langchain_huggingface 是真装了的,
       靠"机器上缺包"来打桩的话,这一节在 CI 里会去真加载 BGE-M3(2.2GB)。
       与本文件第一组那三条用例同一个理由。
    """

    def 假lazy(module: str, attr: str) -> object:
        return chroma类 if module == "langchain_chroma" else embeddings类

    monkeypatch.setattr(store, "_lazy_import", 假lazy)


def _预热() -> None:
    """把「第一次调用才会做的杂活」提前做掉,让栅栏后面只剩下要测的那一句。

    两件:① embedding 的 lru_cache(不然第一个线程会在 `_EMBEDDINGS_LOCK` 里多待
    一会儿,第二个线程在锁上排队,两条就此错开、撞不上);② `chroma_dir` 的
    `mkdir`(`config._ensure_dir` 首次访问才建目录)。

    ⚠️ 刻意**不**顺手调 `get_vectorstore()` 来预热 —— 那会在 chromadb 的类级注册表
    里留下 identifier,后面的并发就不再是「首次创建」,竞态窗口消失、用例空转。
    """
    store.get_embeddings()
    assert get_settings().chroma_dir.is_dir()


def _同时开跑(*活: Callable[[], object]) -> tuple[list[object], list[Exception]]:
    """开 N 个线程,用 `Barrier` 把它们卡到一起再放,返回 (各自的返回值, 炸出来的异常)。

    ⚠️ 栅栏**不是**为了提高复现率 —— 这一点量过,别照直觉传抄:冷启动单轮、
    锁拿掉,有栅栏 11/12 见红、没栅栏 12/12,差不多。chromadb 首次 init 那段
    足够长(几百毫秒),`Thread.start()` 的错开量根本填不满它。
    栅栏买的是**「同时」这件事不依赖 chromadb 慢不慢**:哪天它 init 快了、
    或者换台机器窗口窄了,没栅栏的版本会**静默**退化成"先后两次调用"
    (照样全绿,而它已经不测竞态了),有栅栏的还在测同一件事。

    返回值按传入顺序放进定长 list(下标赋值,不用锁);异常收进另一个 list
    (CPython 的 `list.append` 是原子的)。**不在线程里 assert** —— 子线程里
    抛出来的 AssertionError 不会让用例失败,只会在 stderr 里飘一段没人看的栈。
    """
    栅栏 = threading.Barrier(len(活))
    结果: list[object] = [None] * len(活)
    炸的: list[Exception] = []

    def 跑(序号: int, 干: Callable[[], object]) -> None:
        栅栏.wait()
        try:
            结果[序号] = 干()
        except Exception as exc:
            炸的.append(exc)

    线程 = [threading.Thread(target=跑, args=(i, f)) for i, f in enumerate(活)]
    for t in 线程:
        t.start()
    for t in 线程:
        t.join()
    return 结果, 炸的


def _造数着构造的假类(耗时秒: float = 0.0) -> tuple[type, list[int]]:
    """造一个「每构造一次就往 list 里追一笔」的假类,连同那个 list 一起给出去。

    刻意**不做成模块级的类 + 类变量计数器**:那样计数会跨用例累加,
    一条用例失败会连累后面几条一起红,而真正坏掉的是哪一条看不出来。
    """
    次数: list[int] = []

    class 假类:
        def __init__(self, **_kwargs: object) -> None:
            次数.append(1)  # 先记「进来过」,再睡 —— 睡在后面才撑得开重入窗口
            if 耗时秒:
                time.sleep(耗时秒)

    return 假类, 次数


def _播种(条目: Sequence[tuple[str, str, str]]) -> None:
    """往**真**向量库里塞几条只有 metadata 有意义的块,三元组是 (scope, project_id, source)。

    embedding 随便给两维常量:本文件一次检索都不做,只数 metadata。

    ⚠️ 播完必须 `clear_system_cache()` —— 播种自己就建了一个 System,留着的话
    后面的并发用例全走 else 分支,竞态窗口消失、用例空转(见 `_每条用例都从冷启动起步`)。
    """
    client = chromadb.PersistentClient(path=str(get_settings().chroma_dir))
    col = client.get_or_create_collection(store.COLLECTION_NAME)
    col.add(
        ids=[str(序号) for 序号 in range(len(条目))],
        embeddings=[[0.1, 0.2] for _ in 条目],
        documents=["块" for _ in 条目],
        metadatas=[
            {"scope": scope, "project_id": project_id, "source": source}
            for scope, project_id, source in 条目
        ],
    )
    del col, client
    SharedSystemClient.clear_system_cache()


# --- 真 chromadb:客户端构造的竞态本身 ---------------------------------------


def test_两个线程同时首次建客户端_一个都不许抛(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **整个 PR 的存在理由。** 生产上就是两条线程同时第一次建客户端,各炸一次。

    线上抓到的两句原话(chromadb 1.5.9):
        线程 A  AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
        线程 B  KeyError: '/app/data/chroma'
    本机无锁复现同款,外加 `ValueError: Could not connect to tenant default_tenant`。

    这一条只有**一轮**,却已经很硬:拿掉锁单跑 12 遍 12 遍见红(本机实测)——
    因为 `_预热()` 刻意不碰 chromadb,两个线程撞的是「连库文件都还没有」那一次,
    窗口最宽。即便如此,统计上说了算的仍是下面那条加压版:这条是最小复现、
    看得懂,那条是真闸门,两条都要。
    """
    _装假的向量栈(monkeypatch)
    _预热()

    _, 炸的 = _同时开跑(store.get_vectorstore, store.get_vectorstore)

    assert not 炸的, f"并发首次建 chromadb 客户端炸了:{[repr(e) for e in 炸的]}"


def test_四线程反复首次建客户端_零失败(monkeypatch: pytest.MonkeyPatch) -> None:
    """加压版 —— 这条才是真闸门(变异测试:锁拿掉之后连跑 8 遍、8 遍红;
    轮数怎么定的见 `_压测轮数`)。

    每轮开头 `clear_system_cache()`,把 chromadb 的类级注册表清回空 ——
    否则从第二轮起 identifier 已在字典里,`_create_system_if_not_exists` 全走
    else 分支,**后面 11 轮全是空转**,而报告上和真跑过一模一样。
    """
    _装假的向量栈(monkeypatch)
    _预热()

    炸的: list[Exception] = []
    for _ in range(_压测轮数):
        SharedSystemClient.clear_system_cache()
        _, 本轮 = _同时开跑(*[store.get_vectorstore] * _压测线程数)
        炸的 += 本轮

    总次数 = _压测线程数 * _压测轮数
    assert not 炸的, f"{总次数} 次并发建客户端炸了 {len(炸的)} 次:{[repr(e) for e in 炸的[:3]]}"


# --- 真 chromadb:count_chunks_by_doc 这条 /library 专用的只读路 ---------------


def test_数向量块_库还没建时回空且不抛() -> None:
    """资料库页面在**建库之前**就会被点开 —— 那时 collection 压根不存在。

    chromadb 这时抛的是 `NotFoundError: Collection [gyt_specs] does not exist`。
    让它冒出去的话,`/library` 直接 500;而正确行为是显示「暂无 / 入库中」。
    """
    assert store.count_chunks_by_doc() == {}


def test_数向量块_按scope项目source三元组分别计数() -> None:
    """键必须是 (scope, project_id, source) 三元组,不是 source 一个。

    漂成两元组的坏法很具体:同名文件(`说明.pdf`)在两个工地各传一份,
    数出来会合成一条,`/library` 上工地 B 的那份**看着像已经入库了**,
    而它可能一个块都没进去。全局规范与工地资料同名时同理。
    """
    _播种(
        [
            ("global", "", "GB50016.pdf"),
            ("global", "", "GB50016.pdf"),
            ("global", "", "JGJ59.pdf"),
            ("project", "P1", "施工组织设计.pdf"),
        ]
    )

    assert store.count_chunks_by_doc() == {
        ("global", "", "GB50016.pdf"): 2,
        ("global", "", "JGJ59.pdf"): 1,
        ("project", "P1", "施工组织设计.pdf"): 1,
    }


def test_数向量块_走的是sqlite快路_chromadb压根不参与(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 /library 数块**不许**再去建 chromadb 客户端 —— 快慢差三个数量级。

    实测(2026-09-21,948 块 / 3 份规范的库):惰性 `import chromadb` 4.5s
    + 建 PersistentClient 与全量 metadata 扫描 1.5s,而直接读它那个 sqlite 是 0.012s。
    也就是后端重启后第一次点「📚 資料庫」要干等 6 秒,买到的只是三个数字。

    判据写成「把 PersistentClient 换成一炸就响的」:回落路一旦被走到就炸,
    所以这条绿 = 快路真的把活干完了。**这也是唯一能抓住「快路被误删/被绕过」的用例** ——
    只看返回值的话,慢路算出来的数字和快路一模一样,删掉快路没有任何东西会红。
    """
    _播种(
        [
            ("global", "", "GB50016.pdf"),
            ("global", "", "GB50016.pdf"),
            ("project", "P1", "任务书.pdf"),
        ]
    )

    def 炸(**_kwargs: object) -> object:
        raise AssertionError("走到 chromadb 了 —— 快路没生效")

    monkeypatch.setattr(chromadb, "PersistentClient", 炸)

    assert store.count_chunks_by_doc() == {
        ("global", "", "GB50016.pdf"): 2,
        ("project", "P1", "任务书.pdf"): 1,
    }


def test_数向量块_快路分不出组但库里有货时_回落到chromadb(monkeypatch: pytest.MonkeyPatch) -> None:
    """schema 漂移的形状是「查得到表、但一行都不返回」,**不抛异常**。

    所以快路不能只接 sqlite3.Error 就算数:chromadb 哪天改了 segments/embeddings 的
    关联方式,那条 JOIN 会安安静静地返回空,于是每份文档都成了 0 块,页面上**永远**
    显示「入库中」,而日志、控制台、测试三处都不说话。哨兵是「embeddings 表非空」。

    这里用「把 SQL 换成一条恒空的查询」来造漂移,断言最终数出来的还是对的
    (= 回落到 chromadb 真的发生了)。
    """
    _播种([("global", "", "GB50016.pdf"), ("global", "", "GB50016.pdf")])
    monkeypatch.setattr(store, "_COUNT_CHUNKS_SQL", "SELECT '', '', '', 0 WHERE 0")

    assert store.count_chunks_by_doc() == {("global", "", "GB50016.pdf"): 2}


def test_数向量块_两条路都断了才回空且不抛(monkeypatch: pytest.MonkeyPatch) -> None:
    """版本差异 / 磁盘异常 / 撞上竞态,一律按「这次数不出来」处理,绝不把异常放出去。

    ⚠️ 先播种再炸,是为了让「回 {}」这件事**有区分度**:空库也回 {},
    不播种的话这条用例即使把 `try/except` 整个删掉、把 PersistentClient 换成
    正常的,照样绿 —— 那就成了 `test_数向量块_库还没建时回空` 的复读。
    """
    _播种([("global", "", "GB50016.pdf")])
    assert store.count_chunks_by_doc(), "播种没生效,下面那半条断言就没意义了"

    def 炸(**_kwargs: object) -> object:
        raise RuntimeError("假装 chromadb 在这儿炸了")

    monkeypatch.setattr(store, "_count_chunks_via_sqlite", lambda: None)
    monkeypatch.setattr(chromadb, "PersistentClient", 炸)

    assert store.count_chunks_by_doc() == {}


def test_数向量块与建客户端并发_不许因竞态回空(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 「一边问规范、一边开资料库」—— 全仓两处建 chromadb 客户端的地方对撞。

    这是两处**必须共用同一把** `_CLIENT_LOCK` 的理由。各用各的锁(或者只给
    `get_vectorstore` 加锁)的话,撞上了会被 `count_chunks_by_doc` 那句
    `except Exception: return {}` 吃掉 —— 表现是资料库页面显示「暂无」,
    而 PDF 明明早就入库了。**日志、控制台、测试三处都不说话。**

    变异测试量过两种拿掉法,**两种都被这一条抓住**:两处都不加锁、以及
    只把 `count_chunks_by_doc` 那半边的锁去掉(而 `get_vectorstore` 照旧加着)——
    后者正是"各用各的锁"那种改法,除了这条用例没有第二个地方会红。
    """
    _装假的向量栈(monkeypatch)
    _播种([("global", "", "GB50016.pdf"), ("global", "", "GB50016.pdf")])
    store.get_embeddings()
    预期 = {("global", "", "GB50016.pdf"): 2}

    for 轮 in range(_对撞轮数):
        SharedSystemClient.clear_system_cache()
        结果, 炸的 = _同时开跑(store.count_chunks_by_doc, store.get_vectorstore)
        assert not 炸的, f"第 {轮 + 1} 轮炸了:{[repr(e) for e in 炸的]}"
        assert 结果[0] == 预期, f"第 {轮 + 1} 轮 count 被竞态吃成了 {结果[0]}"


# --- 打桩:两把锁各自的职责,以及它们之间的边界 -------------------------------


def test_并发首次取embedding_真加载只发生一次(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 锁必须在 `lru_cache` **外面**。套在里面这条当场红。

    `lru_cache` 在**调用期间不持锁**:两个线程同时 miss 就都会进函数体、
    都真加载一遍,返回后谁最后写进缓存算谁的。线上那一轮就是这样 ——
    01:52:29.327 与 .374 两条 `Loading SentenceTransformer`,而 BGE-M3 fp32
    约 2.27GB、那台机器一共 1966MB(当时 swap 已用 1306MB)。

    所以 `store.py` 把真加载拆成 `_load_embeddings`,`get_embeddings` 是
    「先 import、再进锁、锁里调它」的包装。写成 `@lru_cache` 里面 `with 锁:`
    的话,两个线程照样各加载一份 —— 而**加载本身不报错**,只是内存翻倍。
    """
    假类, 次数 = _造数着构造的假类(_假构造耗时秒)
    _装假的向量栈(monkeypatch, embeddings类=假类)

    结果, 炸的 = _同时开跑(store.get_embeddings, store.get_embeddings)

    assert not 炸的, f"并发取 embedding 炸了:{[repr(e) for e in 炸的]}"
    assert len(次数) == 1, f"BGE-M3 被真加载了 {len(次数)} 次 —— 一份就 2.27GB"
    assert 结果[0] is 结果[1], "两个线程拿到了不同的 embedding 实例,缓存等于白设"


def test_get_embeddings_转出了cache_clear_且清完会重新加载(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`store.py` 末尾那行 `get_embeddings.cache_clear = _load_embeddings.cache_clear`。

    真加载拆出去之后,`get_embeddings` 自己**不再是** lru_cache 包的函数,
    身上本来没有 `cache_clear`。不转出来的话:
      · 本文件上面那条 `test_embedding_那一半也走同一条路` 当场 AttributeError;
      · 本文件的 autouse fixture 也清不动缓存,整节并发用例集体空转。

    所以这条不只断"有这个属性",还要断**它清的是同一份缓存** ——
    转错成别的 lru_cache 的 cache_clear,属性检查照样过,而缓存一直没清。
    """
    假类, 次数 = _造数着构造的假类()
    _装假的向量栈(monkeypatch, embeddings类=假类)

    assert callable(getattr(store.get_embeddings, "cache_clear", None)), (
        "get_embeddings 身上没有 cache_clear —— store.py 末尾那行转发被删了"
    )

    第一次 = store.get_embeddings()
    assert store.get_embeddings() is 第一次, "第二次调用没命中缓存"
    assert len(次数) == 1

    store.get_embeddings.cache_clear()
    第二次 = store.get_embeddings()

    assert len(次数) == 2, "cache_clear 之后没有重新加载 —— 转出来的不是同一份缓存的清理器"
    assert 第二次 is not 第一次


def test_建客户端是串行的_观测到的最大并发数为一(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接量「同一时刻有几个线程在 Chroma 构造里」,峰值必须是 1。

    与上面那两条真 chromadb 的用例互补:那两条问「结果对不对」(概率性的,
    要靠轮数堆),这条问「有没有真的串行」(确定性的,一轮就够)。
    锁被删掉、或者 `with` 只包住了构造的一半,这条直接看见峰值 ≥ 2 ——
    不用等 chromadb 那个竞态碰巧命中。
    """
    峰值 = {"当前": 0, "最高": 0}
    计数锁 = threading.Lock()  # 只护上面这个 dict,与被测的两把锁无关

    class _盯并发的假Chroma:
        def __init__(self, **_kwargs: object) -> None:
            with 计数锁:
                峰值["当前"] += 1
                峰值["最高"] = max(峰值["最高"], 峰值["当前"])
            time.sleep(_假构造耗时秒)
            with 计数锁:
                峰值["当前"] -= 1

    _装假的向量栈(monkeypatch, chroma类=_盯并发的假Chroma)
    store.get_embeddings()  # 预热:别让 embedding 那把锁替它挡了并发

    _, 炸的 = _同时开跑(*[store.get_vectorstore] * _压测线程数)

    assert not 炸的
    assert 峰值["最高"] == 1, f"同时有 {峰值['最高']} 个线程在建客户端 —— _CLIENT_LOCK 没护住"


def test_两把锁必须是两把_不许合并() -> None:
    """🔴 合并成一把,`/library` 会被 BGE-M3 的首次加载(约 12 秒)整个卡住。

    **阻塞不是异常** —— `count_chunks_by_doc` 那句 `except Exception: return {}`
    兜不住等待。表现是资料库页面转十几秒,而日志、控制台、测试三处都不说话。
    而它的头注明写着「只读 metadata、**绝不加载 embedding 模型**」,
    合并等于把那条契约作废。

    这条只花一行,却是下面两条(锁边界)的前提:两把锁塌成一把之后,
    "不许攥着客户端锁去加载 embedding" 这句话本身就不成立了。
    """
    assert store._CLIENT_LOCK is not store._EMBEDDINGS_LOCK, (
        "两把锁被合并成同一个对象了 —— /library 会陪着 BGE-M3 一起等 12 秒,且零报错"
    )


def test_取embedding时不许攥着客户端锁(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **这一节最值钱的一条。** 守的是「/library 不会被 12 秒的模型加载卡住」。

    测法:在「真加载 BGE-M3」那一刻回头看 `_CLIENT_LOCK` —— 它必须是空着的。
    `_CLIENT_LOCK` 是普通 `Lock` 不是 `RLock`,所以同一个线程持有时
    `acquire(blocking=False)` 一样返回 False,判据成立。

    两种改法会让它红,而两种在别处都看不出来:
      ① 两把锁合并成一把(那时 `get_embeddings` 里那个 with 攥的就是它);
      ② 有人"顺手"把 `get_embeddings()` 挪进 `get_vectorstore` 的 `with _CLIENT_LOCK:`
         里面 —— 看起来只是少一层缩进,实际是让资料库排在模型加载后面。

    ⚠️ 不在假加载里直接 `assert`:这里虽然跑在主线程、抛得出去,但记录下来
    在外面断言,失败信息里能看见"拿到了什么",比一句光秃秃的 AssertionError 强。
    """
    拿到了: list[bool] = []

    def 假加载() -> object:
        成功 = store._CLIENT_LOCK.acquire(blocking=False)
        拿到了.append(成功)
        if 成功:
            store._CLIENT_LOCK.release()
        return _假Embeddings()

    monkeypatch.setattr(store, "_load_embeddings", 假加载)
    _装假的向量栈(monkeypatch)

    store.get_vectorstore()

    assert 拿到了 == [True], (
        "加载 embedding 时 _CLIENT_LOCK 是被攥着的 —— /library 会陪着一起等 12 秒"
    )


def test_惰性import必须在embedding锁外(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁里跑 `__import__` 会与 CPython 自己的模块导入锁形成锁序反转。

    而 importlib 的死锁检测**只看模块锁之间的环、看不见外来的锁** ——
    表现是**静默挂起**,不是报错:进程还在、CPU 归零、日志停在上一行,
    最后被上游超时兜成一句无关的话。这种病现场没法查,只能在这儿钉死。

    ⚠️ 只断**第一次** `_lazy_import`。`get_embeddings` 会经它两次:
        ① `get_embeddings` 自己那句(锁外)—— 这次才真跑 `__import__`,风险在这儿;
        ② `_load_embeddings` 里那句(锁内)—— 那时模块已在 `sys.modules`,
           `__import__` 只是一次字典命中,不再去抢模块锁。
    把 ① 删掉的话,第一次记录就变成 ② 的 False,这条当场红 —— 那正是要拦的改法。
    顺带:① 还担着「链断了一次把话说完」那条契约(异常必须在进锁之前抛)。
    """
    锁是空的: list[bool] = []

    def 假lazy(module: str, attr: str) -> object:
        成功 = store._EMBEDDINGS_LOCK.acquire(blocking=False)
        锁是空的.append(成功)
        if 成功:
            store._EMBEDDINGS_LOCK.release()
        return _假Embeddings

    monkeypatch.setattr(store, "_lazy_import", 假lazy)

    store.get_embeddings()

    assert 锁是空的, "get_embeddings 一次 _lazy_import 都没做 —— 链断了就说不出人话了"
    assert 锁是空的[0] is True, (
        "第一次惰性 import 是在 _EMBEDDINGS_LOCK 里面跑的 —— 锁序反转,表现是静默挂起"
    )


def test_数向量块绝不加载embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 `/library` 一被点开就吃 2.2GB —— 这是 `count_chunks_by_doc` 走原生 client
    而不走 `get_vectorstore` 的全部理由。

    做法:把真加载换成「一碰就炸」。要是哪天有人图省事把这个函数改写成
    `get_vectorstore().get()`,炸出来的 AssertionError 会被那句
    `except Exception: return {}` **静静吃掉**,于是计数回 {} —— 下面这条相等断言红。
    (吃掉这件事本身没法避免,所以断言写成「计数必须对」,不是「不许抛」。)
    """
    _播种([("global", "", "GB50016.pdf")])

    def 一碰就炸() -> object:
        raise AssertionError("count_chunks_by_doc 碰了 embedding 模型 —— 一开资料库就吃 2.2GB")

    monkeypatch.setattr(store, "_load_embeddings", 一碰就炸)

    assert store.count_chunks_by_doc() == {("global", "", "GB50016.pdf"): 1}
