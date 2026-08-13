"""core/project_fs.py(项目/全局文件的物理落地位)的单元测试 —— 纯文件系统,不联网。

环境隔离复用 conftest 的 ``_isolated_settings``(autouse):projects_dir / global_dir
都落在 tmp_path/data 下,用例之间互不干扰,结束由 pytest 清理。

这层要钉死的静默错误:
  · 文件名穿越 —— 上传「../../../etc/passwd.dxf」若能写出目标目录,就是任意文件写漏洞;
  · 全局作用域收了任务书 —— 任务书必属某项目,混进全局会污染「全局规范」的语义;
  · 同名覆盖 —— 后传的图把先传的物理文件盖掉,而先前那条 drawings 行还指着它 → 悬空;
  · rel_path 算错 —— 存进库的相对路径拼错,面板浏览/下载就找不到文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gyt.config import get_settings
from gyt.core import project_fs

_DXF = b"0\nSECTION\n"  # 随便几个字节,这层不解析内容


# ---------------------------------------------------------------------------
# 骨架
# ---------------------------------------------------------------------------


def test_ensure_project_tree建齐平立剖与文档子目录且幂等() -> None:
    root = project_fs.ensure_project_tree("gyt-a3")

    assert root == get_settings().projects_dir / "gyt-a3"
    for vt in ("plan", "elevation", "section"):
        assert (root / "drawings" / vt).is_dir()
    for dt in ("regulation", "task_book"):
        assert (root / "docs" / dt).is_dir()

    # 幂等:再调一次不报错、不清空
    (root / "drawings" / "plan" / "占位.txt").write_text("x", encoding="utf-8")
    project_fs.ensure_project_tree("gyt-a3")
    assert (root / "drawings" / "plan" / "占位.txt").exists()


# ---------------------------------------------------------------------------
# 落图纸
# ---------------------------------------------------------------------------


def test_land_drawing落到对应视图子目录且内容保真() -> None:
    dest = project_fs.land_drawing("gyt-a3", "elevation", "南立面图.dxf", _DXF)

    root = get_settings().projects_dir / "gyt-a3"
    assert dest == root / "drawings" / "elevation" / "南立面图.dxf"
    assert dest.read_bytes() == _DXF


def test_land_drawing支持从Path读源(tmp_path: Path) -> None:
    src = tmp_path / "src.dxf"
    src.write_bytes(_DXF)

    dest = project_fs.land_drawing("gyt-a3", "plan", "首层平面图.dxf", src)

    assert dest.read_bytes() == _DXF


def test_land_drawing非法view_type抛ValueError() -> None:
    with pytest.raises(ValueError):
        project_fs.land_drawing("gyt-a3", "3d", "x.dxf", _DXF)


def test_land_drawing同名不覆盖而是加序号() -> None:
    first = project_fs.land_drawing("gyt-a3", "plan", "平面图.dxf", b"AAA")
    second = project_fs.land_drawing("gyt-a3", "plan", "平面图.dxf", b"BBB")
    third = project_fs.land_drawing("gyt-a3", "plan", "平面图.dxf", b"CCC")

    assert first.name == "平面图.dxf"
    assert second.name == "平面图 (2).dxf"
    assert third.name == "平面图 (3).dxf"  # 连撞两次也要继续加序号
    assert first.read_bytes() == b"AAA"  # 先传的没被盖
    assert second.read_bytes() == b"BBB"
    assert third.read_bytes() == b"CCC"


# ---------------------------------------------------------------------------
# 落文档(全局 vs 项目)
# ---------------------------------------------------------------------------


def test_land_doc全局规范落到global目录() -> None:
    dest = project_fs.land_doc("global", "regulation", "GB50016.pdf", _DXF)

    assert dest == get_settings().global_dir / "docs" / "regulation" / "GB50016.pdf"
    assert dest.read_bytes() == _DXF


def test_land_doc全局不收任务书() -> None:
    with pytest.raises(ValueError):
        project_fs.land_doc("global", "task_book", "任务书.pdf", _DXF)


def test_land_doc项目规范与任务书落到项目docs() -> None:
    reg = project_fs.land_doc("project", "regulation", "专项规范.pdf", _DXF, project_id="gyt-a3")
    tb = project_fs.land_doc("project", "task_book", "施工任务书.pdf", _DXF, project_id="gyt-a3")

    root = get_settings().projects_dir / "gyt-a3"
    assert reg == root / "docs" / "regulation" / "专项规范.pdf"
    assert tb == root / "docs" / "task_book" / "施工任务书.pdf"


def test_land_doc项目作用域缺project_id抛ValueError() -> None:
    with pytest.raises(ValueError):
        project_fs.land_doc("project", "regulation", "x.pdf", _DXF)


def test_land_doc非法scope或doc_type抛ValueError() -> None:
    with pytest.raises(ValueError):
        project_fs.land_doc("nowhere", "regulation", "x.pdf", _DXF, project_id="gyt-a3")
    with pytest.raises(ValueError):
        project_fs.land_doc("project", "manual", "x.pdf", _DXF, project_id="gyt-a3")


# ---------------------------------------------------------------------------
# 安全:文件名穿越 / 非法 project_id
# ---------------------------------------------------------------------------


def test_文件名穿越被剥成basename落在目标目录内() -> None:
    dest = project_fs.land_drawing("gyt-a3", "plan", "../../../etc/passwd.dxf", _DXF)

    target = get_settings().projects_dir / "gyt-a3" / "drawings" / "plan"
    assert dest.parent == target  # 没跑出去
    assert dest.name == "passwd.dxf"  # 目录成分被剥光
    # 没有在 data 根之外写出任何东西
    assert dest.resolve().is_relative_to(get_settings().data_dir.resolve())


@pytest.mark.parametrize("bad", ["", ".", "..", "../"])
def test_非法文件名抛ValueError(bad: str) -> None:
    with pytest.raises(ValueError):
        project_fs.land_drawing("gyt-a3", "plan", bad, _DXF)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "..", "空格 名"])
def test_非法project_id抛ValueError(bad: str) -> None:
    with pytest.raises(ValueError):
        project_fs.ensure_project_tree(bad)


# ---------------------------------------------------------------------------
# rel_path
# ---------------------------------------------------------------------------


def test_project_rel_path算出相对项目根的posix路径() -> None:
    dest = project_fs.land_drawing("gyt-a3", "plan", "首层平面图.dxf", _DXF)

    rel = project_fs.project_rel_path("gyt-a3", dest)

    assert rel == "drawings/plan/首层平面图.dxf"  # 正斜杠,相对项目根


def test_project_rel_path不在该项目下抛ValueError() -> None:
    outside = get_settings().global_dir / "docs" / "regulation" / "x.pdf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(_DXF)

    with pytest.raises(ValueError):
        project_fs.project_rel_path("gyt-a3", outside)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def test_作用域与文档类型常量锁死() -> None:
    assert project_fs.SCOPES == ("global", "project")
    assert project_fs.DOC_TYPES == ("regulation", "task_book")
