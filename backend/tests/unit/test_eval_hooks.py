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
    """PHOTOS_DIR 必须按 __file__ 推导,**不能**用 Settings.data_dir ——
    后者是按进程工作目录解析的运行期目录(backend/data),演示素材不在那儿。
    prefilter.py 踩过同一个坑:相对路径会让照片被静默写到 backend/data/。
    """
    assert PHOTOS_DIR.name == "photos"
    assert PHOTOS_DIR.parent.name == "demo"
    assert PHOTOS_DIR.is_absolute()
