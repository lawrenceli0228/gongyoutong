"""评测接线板(eval/hooks.py)的单元测试 —— 全程不联网、不真调模型。

这一层很薄,但它是「评测分数」与「被测 Agent」之间唯一的桥。桥错了的表现是
**分数看起来正常、却测的不是你以为的东西**,所以这里的测试重点不是覆盖率,
而是把几种「静默测错」钉死:
  · 照片找不到时必须炸,不能默默给一个空结果(那会被判成模型答错)
  · CSV 里的文件名不许当路径用(评测集是人手填的,按不可信输入处理)
  · 工具失败时报告里要看得见原因,而不是一个「空 / (无)」
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from eval import hooks
from eval.hooks import PHOTOS_DIR, RUNNERS, EvalRunnerError, run_safety_row
from eval.runner import SUITES

GOOD_DATA = {"label": "not_site", "violations": [], "note": "办公室"}


class _FakeTool:
    """替身:模拟 analyze_site_photo 这个 BaseTool 的 .ainvoke 入口。"""

    def __init__(self, envelope: Any) -> None:
        self.envelope = envelope
        self.seen: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any]) -> Any:
        self.seen.append(payload)
        return self.envelope


def _patch_tool(monkeypatch: pytest.MonkeyPatch, envelope: Any) -> _FakeTool:
    fake = _FakeTool(envelope)
    monkeypatch.setattr(hooks, "analyze_site_photo", fake)
    return fake


def _patch_photos_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把照片目录指到临时目录,避免测试依赖仓库里那 30 张真图。"""
    monkeypatch.setattr(hooks, "PHOTOS_DIR", tmp_path)
    return tmp_path


async def test_正常路径把信封里的data交给判分(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """scorers.score_safety 读的是 {"label","violations"},而工具返回的是**信封**。

    忘了解包的话,scorers 从信封里取 label 会取到 None —— 每一行都判错,
    而报告显示「空 / (无)」,看起来像模型完全不工作。这条钉住解包。
    """
    photos = _patch_photos_dir(monkeypatch, tmp_path)
    (photos / "photo_29.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    fake = _patch_tool(
        monkeypatch, {"ok": True, "data": GOOD_DATA, "user_msg": "", "error_code": None}
    )

    result = await run_safety_row({"id": "S29", "image": "photo_29.jpg"})

    assert result == GOOD_DATA
    # 传给工具的必须是 32 位 hex 的产物编号,不是文件名
    assert len(fake.seen) == 1
    assert len(fake.seen[0]["artifact_id"]) == 32


@pytest.mark.parametrize("value", ["", "   ", None])
async def test_没填image列时炸而不是静默判错(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: Any
) -> None:
    """漏填一列是**数据集的错**。静默返回空结果的话,报告会写成"模型答错了",
    修的人会去调提示词 —— 方向完全错。"""
    _patch_photos_dir(monkeypatch, tmp_path)
    with pytest.raises(EvalRunnerError, match="image"):
        await run_safety_row({"id": "S01", "image": value})


async def test_照片不存在时报出完整路径(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """错误信息要能直接照着改:告诉人具体去哪个目录放哪个名字。"""
    _patch_photos_dir(monkeypatch, tmp_path)
    with pytest.raises(EvalRunnerError, match="照片不存在"):
        await run_safety_row({"id": "S01", "image": "不存在.jpg"})


async def test_CSV里的文件名不许当路径用(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """评测集是人手填的,按不可信输入处理。

    `../` 只取末段之后会变成一个不存在的文件名,于是走"照片不存在"那条出口 ——
    重点是它**没有**跑到 PHOTOS_DIR 外面去读文件。
    """
    photos = _patch_photos_dir(monkeypatch, tmp_path)
    outside = tmp_path.parent / "secret.jpg"
    outside.write_bytes(b"\xff\xd8\xff\xe0not-a-photo-you-should-read")
    assert not (photos / "secret.jpg").exists()

    with pytest.raises(EvalRunnerError, match="照片不存在"):
        await run_safety_row({"id": "S01", "image": "../secret.jpg"})


async def test_工具失败时把原因带进报告(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """runner._score_row 会把异常文本原样写进报告的 actual 字段。

    所以这里抛异常(而不是返回空 dict)是有意的:「第 7 条为什么挂了」
    要在报告里一眼可见,不用回头翻日志。
    """
    photos = _patch_photos_dir(monkeypatch, tmp_path)
    (photos / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    _patch_tool(
        monkeypatch,
        {
            "ok": False,
            "data": None,
            "user_msg": "网络有点慢,这次没等到结果。",
            "error_code": "TIMEOUT",
        },
    )

    with pytest.raises(EvalRunnerError) as caught:
        await run_safety_row({"id": "S01", "image": "a.jpg"})

    message = str(caught.value)
    assert "TIMEOUT" in message
    assert "网络有点慢" in message


async def test_工具没返回信封时也炸(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    photos = _patch_photos_dir(monkeypatch, tmp_path)
    (photos / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    _patch_tool(monkeypatch, "我是一个裸字符串")

    with pytest.raises(EvalRunnerError, match="没返回信封"):
        await run_safety_row({"id": "S01", "image": "a.jpg"})


def test_RUNNERS表的套名都是runner认识的() -> None:
    """套名写错(比如 "safty")时,load_runners 会拒绝 —— 但那是运行时才发现。
    这条让它在单测阶段就红。"""
    assert set(RUNNERS) <= set(SUITES)
    assert "safety" in RUNNERS


def test_RUNNERS里的都是协程函数() -> None:
    """runner 会 `await runner(row)`。填个同步函数进去会在跑评测时才炸,
    而那时候可能已经烧了几十次模型调用。"""
    for name, func in RUNNERS.items():
        assert inspect.iscoroutinefunction(func), f"{name} 不是 async 函数"


def test_RUNNERS是只读的() -> None:
    """防止有人在运行期往表里塞东西。"""
    with pytest.raises(TypeError):
        RUNNERS["safety"] = None  # type: ignore[index]


def test_照片目录指向仓库里的演示素材() -> None:
    """PHOTOS_DIR 必须是按 __file__ 推出来的**绝对**路径,不能是跟着 cwd 跑的相对路径。

    历史教训:prefilter.py 当年用相对路径写默认输出,`python -m eval.prefilter` 从
    backend/ 下跑,27 张照片就被静默写进了 backend/data/(目录自动新建,一声不吭)。

    ⚠️ 旧注释里"data_dir 是按进程工作目录解析的运行期目录(backend/data)"这句**已经作废** ——
    ``Settings.data_dir`` 的默认值现在按 config.py 的 __file__ 推导仓库根,默认布局下
    ``demo_assets_dir`` 与这里的 PHOTOS_DIR 指的是同一个目录。eval 这边仍旧锚 __file__
    的理由(以及与 knowledge/cad 之间那条已知偏差)写在 hooks.REPO_ROOT 的说明里。

    这条用例只钉形状,不钉具体前缀:tmp 目录下也能满足,换机器/换 checkout 都不会假红。
    """
    assert PHOTOS_DIR.name == "photos"
    assert PHOTOS_DIR.parent.name == "demo"
    assert PHOTOS_DIR.is_absolute()


# ---------------------------------------------------------------------------
# routing runner —— 只取 supervisor 第一跳,派活瞬间掐断流式
# ---------------------------------------------------------------------------


class _FakeGraph:
    """替身:按剧本吐 astream 更新,并记录流式是否被提前关掉。"""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.yielded = 0
        self.closed_early = False

    async def astream(self, _input: Any, stream_mode: str = "updates") -> Any:
        try:
            for update in self.updates:
                self.yielded += 1
                yield update
        except GeneratorExit:
            self.closed_early = True
            raise


def _supervisor_update(*, tool: str | None = None, text: str = "") -> dict[str, Any]:
    from langchain_core.messages import AIMessage

    calls = [{"name": tool, "args": {}, "id": "t1"}] if tool else []
    return {"supervisor": {"messages": [AIMessage(content=text, tool_calls=calls)]}}


def _patch_graph(monkeypatch: pytest.MonkeyPatch, fake: _FakeGraph) -> None:
    import gyt.graph as graph_module

    monkeypatch.setattr(graph_module, "graph", fake)


async def test_路由取supervisor第一跳并当场停流(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿到派活对象后子 Agent 一步都不该跑 —— inspection 一旦启动就是整条英雄链。"""
    from eval.hooks import run_routing_row

    fake = _FakeGraph(
        [
            _supervisor_update(tool="transfer_to_inspection", text="我这就安排巡检。"),
            {"inspection": {"messages": []}},  # 这条不该被消费到
        ]
    )
    _patch_graph(monkeypatch, fake)

    got = await run_routing_row({"id": "R05", "user_input": "查一下这张照片,顺便出份巡检记录"})

    assert got == "inspection"
    assert fake.yielded == 1, "拿到第一跳就该停,不该把子 Agent 的更新也消费掉"


async def test_同一条消息里铺垫话加工具调用时工具优先(monkeypatch: pytest.MonkeyPatch) -> None:
    """supervisor 常在一条消息里既写「我来安排」又发交接调用 ——
    顺序反了会把铺垫话误判成 none,整套路由分数系统性偏低。"""
    from eval.hooks import run_routing_row

    _patch_graph(
        monkeypatch,
        _FakeGraph(
            [
                _supervisor_update(tool="transfer_to_safety", text="好的,这就安排同事看照片。"),
            ]
        ),
    )
    assert await run_routing_row({"id": "R01", "user_input": "这张有没有隐患"}) == "safety"


async def test_supervisor纯文本回答判为none(monkeypatch: pytest.MonkeyPatch) -> None:
    """自答、追问、婉拒都算「没派」—— none 行(闲聊/超范围/信息不足)的标准答案。"""
    from eval.hooks import run_routing_row

    _patch_graph(
        monkeypatch,
        _FakeGraph(
            [
                _supervisor_update(text="师傅,你要看哪张照片?把编号发我一下。"),
            ]
        ),
    )
    assert await run_routing_row({"id": "R21", "user_input": "看一下这个"}) == "none"


async def test_忽略非supervisor节点的更新(monkeypatch: pytest.MonkeyPatch) -> None:
    """pre_model_hook(上传改写)等节点也会出更新,不能把它们当成路由决策。"""
    from eval.hooks import run_routing_row

    _patch_graph(
        monkeypatch,
        _FakeGraph(
            [
                {"pre_model_hook": {"messages": []}},
                _supervisor_update(tool="transfer_to_ping"),
            ]
        ),
    )
    assert await run_routing_row({"id": "RX", "user_input": "测试一下"}) == "ping"


async def test_没填user_input时炸而不是静默判none(monkeypatch: pytest.MonkeyPatch) -> None:
    from eval.hooks import run_routing_row

    with pytest.raises(EvalRunnerError, match="user_input"):
        await run_routing_row({"id": "RX", "user_input": "  "})


# --- rag runner(直接打桩检索工具,不加载 BGE-M3、不碰真库)---------------------


class _FakeSearchTool:
    """假的 search_regulation:.ainvoke 直接返回预设信封。"""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope

    async def ainvoke(self, _args: Any) -> Any:
        return self._envelope


async def test_rag命中把passages拼成answer并带出处(monkeypatch: pytest.MonkeyPatch) -> None:
    from eval.hooks import run_rag_row

    import gyt.agents.knowledge.tools as ktools

    envelope = {
        "ok": True,
        "error_code": None,
        "user_msg": "查到 2 条",
        "data": {
            "passages": [
                {"text": "净宽度不应小于4.0", "source": "GB.pdf", "page": 124, "score": 0.5},
                {"text": "另一条相关", "source": "GB.pdf", "page": 125, "score": 0.6},
            ]
        },
    }
    monkeypatch.setattr(ktools, "search_regulation", _FakeSearchTool(envelope))
    out = await run_rag_row({"id": "K01", "question": "消防车道多宽"})
    assert "4.0" in out["answer"]  # 要点在拼接的原文里
    assert out["source"] == "GB.pdf"  # 完整文件名
    assert out["page"] == "124,125"  # 命中的页码都给上


async def test_rag查不到交出承认查不到无出处的形状(monkeypatch: pytest.MonkeyPatch) -> None:
    from eval.hooks import run_rag_row

    import gyt.agents.knowledge.tools as ktools

    envelope = {
        "ok": False,
        "error_code": "EMPTY_RESULT",
        "user_msg": "知识库里查不到和「食堂吃什么」对得上的规范条文。",
        "data": None,
    }
    monkeypatch.setattr(ktools, "search_regulation", _FakeSearchTool(envelope))
    out = await run_rag_row({"id": "K17", "question": "食堂吃什么"})
    assert out["source"] == "" and out["page"] == ""  # no_answer:不许有出处
    assert "查不到" in out["answer"]


async def test_rag没填question时炸() -> None:
    from eval.hooks import run_rag_row

    with pytest.raises(EvalRunnerError, match="question"):
        await run_rag_row({"id": "K01", "question": "  "})
