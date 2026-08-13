"""db/projects.py(项目与图纸索引 SQLite 存储层)的单元测试 —— 不联网,库落在用例独占的 tmp_path。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):它把 GYT_DATA_DIR
指到 tmp_path/data,并在用例前后各做一次 ``get_settings.cache_clear()``,
所以本文件不需要任何自建 fixture,每个用例天然拿到一张全新的库。

这层要钉死的静默错误:
  · 外键没真开 —— 属于不存在项目的图静默入库,「A3 项目有哪些图」会掺进幽灵行;
  · CHECK 没真建上 —— view_type 写成 '3d' 也能进库,平立剖分类当场失真;
  · SQL 拼值(而非参数化)—— 项目名里一个单引号就够炸表,注入用例直接验"表还活着";
  · list_drawings 的筛选分支拼错 —— 「A3 的立面图」漏图或串项目,演示当场看不出病灶在存储层。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from gyt.config import get_settings
from gyt.db import projects

# 32 位十六进制的假 artifact_id(满足 ARTIFACT_ID_RE 的形状,但不必真有文件)。
_AID_A = "a" * 32
_AID_B = "b" * 32
_AID_C = "c" * 32


def _raw_connect() -> sqlite3.Connection:
    """绕过存储层直连库文件:硬塞脏状态需要"上帝视角"。默认连接不开外键,正合用。"""
    return sqlite3.connect(get_settings().sqlite_path)


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


def test_建取项目往返全字段保真() -> None:
    """create→get 的字段级往返:SELECT 列序与 ProjectRow 对不上位时在这里现形。"""
    projects.create_project("gyt-a3", "幸福小区A3栋", "A3")

    row = projects.get_project("gyt-a3")

    assert row is not None
    assert row.id == "gyt-a3"
    assert row.name == "幸福小区A3栋"
    assert row.code == "A3"
    assert row.created_at != ""


def test_项目code可空存None取None() -> None:
    """code 可空是合法状态,不许被悄悄存成空串。"""
    projects.create_project("gyt-b1", "幸福小区B1栋")

    row = projects.get_project("gyt-b1")

    assert row is not None
    assert row.code is None


def test_get_project查无返回None() -> None:
    """查无此项目用 None 说话,不抛异常 —— 「项目不存在」的人话归工具层拼。"""
    assert projects.get_project("nope") is None


def test_list_projects空库返回空列表() -> None:
    """空库就是空列表,不是 None、不抛异常。"""
    assert projects.list_projects() == []


def test_list_projects按建库先后稳定排序() -> None:
    """列项目按 created_at 升序 + id 兜底,保证两次列表里同一项目位置不抖。"""
    projects.create_project("gyt-a3", "A3")
    projects.create_project("gyt-b1", "B1")
    projects.create_project("gyt-c2", "C2")

    assert [r.id for r in projects.list_projects()] == ["gyt-a3", "gyt-b1", "gyt-c2"]


def test_项目id重复撞主键抛IntegrityError() -> None:
    """id 是短码主键:重复建同一项目必须当场被拒,交由上层翻成「这个项目已经建过了」。"""
    projects.create_project("gyt-a3", "第一次", "A3")

    with pytest.raises(sqlite3.IntegrityError):
        projects.create_project("gyt-a3", "又建一次", None)


# ---------------------------------------------------------------------------
# drawings
# ---------------------------------------------------------------------------


def _seed_project() -> None:
    projects.create_project("gyt-a3", "幸福小区A3栋", "A3")


def test_加图往返全字段保真() -> None:
    """add_drawing→get_drawing_by_id 的字段级往返,含可空的 floor/rel_path。"""
    _seed_project()

    drawing_id = projects.add_drawing(
        "gyt-a3", _AID_A, "plan", "首层平面图", floor="1F",
        rel_path="drawings/plan/首层平面图.dxf",
    )

    row = projects.get_drawing_by_id(drawing_id)
    assert row is not None
    assert row.id == drawing_id
    assert row.project_id == "gyt-a3"
    assert row.artifact_id == _AID_A
    assert row.view_type == "plan"
    assert row.floor == "1F"
    assert row.title == "首层平面图"
    assert row.rel_path == "drawings/plan/首层平面图.dxf"
    assert row.created_at != ""


def test_加图可省floor与rel_path存None() -> None:
    """floor/rel_path 可省,省了就是 None,不许悄悄存空串。"""
    _seed_project()

    drawing_id = projects.add_drawing("gyt-a3", _AID_B, "section", "1-1剖面图")

    row = projects.get_drawing_by_id(drawing_id)
    assert row is not None
    assert row.floor is None
    assert row.rel_path is None


def test_get_drawing查无返回None() -> None:
    """查无此图用 None 说话。"""
    assert projects.get_drawing_by_id(999) is None


def test_resolve_by_title同项目内按名字取最新() -> None:
    """按项目 + 展示名找图;同名多张(跨楼层)取最新登记的一张。"""
    _seed_project()
    projects.add_drawing("gyt-a3", _AID_A, "plan", "标准层平面图", floor="3F")
    latest = projects.add_drawing("gyt-a3", _AID_B, "plan", "标准层平面图", floor="5F")

    row = projects.resolve_by_title("gyt-a3", "标准层平面图")
    assert row is not None
    assert row.id == latest
    assert row.floor == "5F"


def test_resolve_by_title不串项目_查无返回None() -> None:
    """名字对但项目不对,不许命中 —— 这是「A3 的图别混进 B1」的地基。"""
    _seed_project()
    projects.create_project("gyt-b1", "B1栋")
    projects.add_drawing("gyt-a3", _AID_A, "plan", "首层平面图")

    assert projects.resolve_by_title("gyt-b1", "首层平面图") is None
    assert projects.resolve_by_title("gyt-a3", "不存在的图") is None


def test_find_drawing_by_title跨项目取最新() -> None:
    """cad 解析上传图时用户只报图名不报项目 → 跨项目找,同名取最新一张。"""
    _seed_project()
    projects.create_project("gyt-b1", "B1栋")
    projects.add_drawing("gyt-a3", _AID_A, "plan", "平面图")
    latest = projects.add_drawing("gyt-b1", _AID_B, "elevation", "平面图")  # 跨项目同名

    row = projects.find_drawing_by_title("平面图")
    assert row is not None
    assert row.id == latest  # 不限项目,取最新登记的一张
    assert projects.find_drawing_by_title("查无此图") is None


def test_find_drawing_by_artifact反查视图类型() -> None:
    """cad 拿到 artifact_id 后回头取 view_type / 展示名。"""
    _seed_project()
    projects.add_drawing("gyt-a3", _AID_A, "section", "1-1剖面图", floor="1F")

    row = projects.find_drawing_by_artifact(_AID_A)
    assert row is not None
    assert row.view_type == "section"
    assert row.title == "1-1剖面图"
    assert projects.find_drawing_by_artifact("f" * 32) is None


def test_list_drawings按项目与视图筛() -> None:
    """四种筛法各走一条 WHERE 分支:全量 / 限项目 / 限视图 / 两者都限。"""
    _seed_project()
    projects.create_project("gyt-b1", "B1栋")
    projects.add_drawing("gyt-a3", _AID_A, "plan", "A3首层平面图")
    projects.add_drawing("gyt-a3", _AID_B, "elevation", "A3南立面图")
    projects.add_drawing("gyt-b1", _AID_C, "plan", "B1平面图")

    assert len(projects.list_drawings()) == 3
    assert len(projects.list_drawings(project_id="gyt-a3")) == 2
    assert len(projects.list_drawings(view_type="plan")) == 2
    both = projects.list_drawings(project_id="gyt-a3", view_type="plan")
    assert [r.title for r in both] == ["A3首层平面图"]


def test_加图给不存在的项目撞外键抛IntegrityError() -> None:
    """外键必须真开(PRAGMA foreign_keys=ON):给不存在的项目登记图,当场被拒 ——
    否则孤儿图静默入库,「这个项目有哪些图」会掺进无主行。"""
    with pytest.raises(sqlite3.IntegrityError):
        projects.add_drawing("no-such-project", _AID_A, "plan", "野图")


def test_加图view_type非法撞CHECK抛IntegrityError() -> None:
    """CHECK 只有 DDL 真写进库才有效:view_type='3d' 不在平立剖白名单,必须当场被拒。"""
    _seed_project()

    with pytest.raises(sqlite3.IntegrityError):
        projects.add_drawing("gyt-a3", _AID_A, "3d", "越界视图")


def test_直连硬塞非法view_type也被CHECK拦() -> None:
    """绕过存储层从"上帝视角"硬塞脏 view_type 同样必须被拒 —— 证明约束在库里,不在 Python 里。"""
    _seed_project()

    with closing(_raw_connect()) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO drawings (project_id, artifact_id, view_type, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("gyt-a3", _AID_A, "half", "脏图", "2000-01-01T00:00:00+00:00"),
        )


# ---------------------------------------------------------------------------
# 注入与隔离
# ---------------------------------------------------------------------------


def test_注入串当项目名与图名存取原样表安然无恙() -> None:
    """方案红线 4:值一律走 ? 占位。若有人改成拼接 SQL,这些名字会当场把表炸掉。"""
    evil = "A3'; DROP TABLE drawings;--"
    projects.create_project("gyt-evil", evil)
    projects.add_drawing("gyt-evil", _AID_A, "plan", evil)

    proj = projects.get_project("gyt-evil")
    assert proj is not None
    assert proj.name == evil  # 原样进出,不许带转义痕迹

    # 表还活着:还能继续写读
    projects.create_project("gyt-survivor", "表还活着的证据")
    assert projects.get_project("gyt-survivor") is not None
    assert len(projects.list_drawings(project_id="gyt-evil")) == 1


def test_库文件落在测试独占的临时目录(tmp_path: Path) -> None:
    """conftest 把 GYT_DATA_DIR 指到 tmp_path/data,库文件必须落在那里 ——
    而不是悄悄写进仓库那份真台账(默认 <仓库根>/data/gyt.sqlite3)。与 db/tasks 同库同文件。"""
    _seed_project()

    expected = tmp_path / "data" / "gyt.sqlite3"
    assert expected.exists()
    assert get_settings().sqlite_path == expected


def test_与tasks同库共存不互相建坏() -> None:
    """projects 与 tasks 写同一个 gyt.sqlite3(单库多表):两边各自幂等建表,互不破坏。"""
    from gyt.db import tasks

    task_id = tasks.create("三层钢筋复检", "2026-08-10")
    projects.create_project("gyt-a3", "A3栋", "A3")

    assert tasks.fetch(task_id) is not None
    assert projects.get_project("gyt-a3") is not None


def test_VIEW_TYPES就是平立剖三视图() -> None:
    """对外语义锁死:平立剖 = plan/elevation/section,上层与 DDL 的 CHECK 同取这一份。"""
    assert projects.VIEW_TYPES == ("plan", "elevation", "section")
