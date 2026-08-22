"""``agents/knowledge/store.py`` 的守门断言 —— 只测「链断了怎么说话」这一件事。

真正的向量库行为(建库、查询、scope 过滤)测不了:它要 torch → sentence-transformers
→ 2.2GB 权重,而 **torch 2.13 没有 macosx_x86_64 轮子**,Intel Mac 上装不上
(CLAUDE.md 第一句写的就是这个)。那部分靠 `make test-docker` 在容器里跑。

本文件测的是**那条链断掉时说的话**,而它恰恰是最容易在容器里"静默通过"的东西 ——
所以下面一律用 monkeypatch **人造**断链,不依赖跑测试的这台机器真的缺包。
依赖真环境的话,这几条在容器里会因为「包都在、异常压根不抛」而变成空转,
而空转的用例和通过的用例在报告里长得一模一样。
"""

from __future__ import annotations

import builtins

import pytest

from gyt.agents.knowledge import store


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
