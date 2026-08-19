"""site_switch 单测:自然语言解析到项目 + switch_project 工具的信封契约。

resolve_target 是纯函数(给定 rows),直接构造 ProjectRow 断言;工具用例走 conftest 的
独立 tmp DB(autouse fixture),用 db.create_project 造数据,不联网、不产生账单。
"""

from __future__ import annotations

from gyt.db import projects as db
from gyt.db.projects import ProjectRow
from gyt.site_switch import resolve_target, switch_project


def _rows(*specs: tuple[str, str, str | None]) -> list[ProjectRow]:
    """按 (id, name, code) 造 ProjectRow 列表(created_at 填个占位值)。"""
    return [
        ProjectRow(id=i, name=n, code=c, created_at="2026-01-01T00:00:00+08:00")
        for i, n, c in specs
    ]


# --- resolve_target(纯函数)---------------------------------------------------


def test_全部类关键词一律切到全局():
    rows = _rows(("1", "测试项目1", None))
    for t in ["全部", "全部工地", "不限项目", "所有工地", "全局", "看全部"]:
        assert resolve_target(t, rows)[0] == "global", t


def test_按id精确命中且大小写不敏感():
    rows = _rows(("gyt-a3", "幸福小区A3栋", "A3"))
    st, pl = resolve_target("GYT-A3", rows)
    assert st == "one"
    assert isinstance(pl, ProjectRow) and pl.id == "gyt-a3"


def test_按名字精确命中():
    rows = _rows(("1", "阳光花园", None), ("2", "滨江一号", None))
    st, pl = resolve_target("阳光花园", rows)
    assert st == "one"
    assert isinstance(pl, ProjectRow) and pl.name == "阳光花园"


def test_带后缀的口语也能命中():
    # 「阳光花园工地」里包含项目名「阳光花园」—— 子串双向匹配接得住。
    rows = _rows(("1", "阳光花园", None))
    st, pl = resolve_target("阳光花园工地", rows)
    assert st == "one"
    assert isinstance(pl, ProjectRow) and pl.name == "阳光花园"


def test_对上多个候选返回many让用户说清():
    rows = _rows(("1", "测试项目1", None), ("2", "测试项目2", None))
    st, pl = resolve_target("测试项目", rows)  # 子串同时命中两个
    assert st == "many"
    assert isinstance(pl, list) and len(pl) == 2


def test_一个都没对上返回none():
    rows = _rows(("1", "阳光花园", None))
    assert resolve_target("火星基地", rows)[0] == "none"
    assert resolve_target("", rows)[0] == "none"


# --- switch_project 工具(信封契约,走 tmp DB)---------------------------------


async def test_switch_project成功切到某项目且data带id():
    # data.project_id 是前端 ProjectSwitchSync 落地用的关键字段,必须准。
    db.create_project("gyt-a3", "幸福小区A3栋", "A3")
    result = await switch_project.ainvoke({"target": "幸福小区A3栋"})
    assert result["ok"] is True
    assert result["data"]["project_id"] == "gyt-a3"
    assert result["data"]["name"] == "幸福小区A3栋"
    assert "幸福小区A3栋" in result["user_msg"]


async def test_switch_project切到全部项目id为空串():
    # 空串 = 不限项目(全局)。前端据此 setProjectId("") 清空选择。
    db.create_project("gyt-a3", "幸福小区A3栋", "A3")
    result = await switch_project.ainvoke({"target": "全部"})
    assert result["ok"] is True
    assert result["data"]["project_id"] == ""
    assert result["data"]["name"] == "全部工地"


async def test_switch_project找不到时如实说并列出现有():
    db.create_project("gyt-a3", "幸福小区A3栋", "A3")
    result = await switch_project.ainvoke({"target": "火星工地"})
    assert result["ok"] is False
    assert result["error_code"] == "NOT_FOUND"
    assert "幸福小区A3栋" in result["user_msg"]  # 把现有工地列给用户


async def test_switch_project一个项目都没建时给建项目指引():
    result = await switch_project.ainvoke({"target": "随便哪个"})
    assert result["ok"] is False
    assert "还没建任何工地" in result["user_msg"]


async def test_switch_project多个候选时要求说清():
    db.create_project("1", "测试项目1", None)
    db.create_project("2", "测试项目2", None)
    result = await switch_project.ainvoke({"target": "测试项目"})
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
