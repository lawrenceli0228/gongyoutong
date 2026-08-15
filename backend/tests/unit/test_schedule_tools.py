"""Schedule 工具层(agents/schedule/tools.py)的单元测试 —— 不联网、不调模型。

这层是「模型的嘴」与「台账的账本」之间唯一的桥,测试重点不是覆盖率,
而是把几类**会让台账悄悄记错**的失败钉死:
  · 日期解析必须发生在代码里(dates 层),工具只透传人话错误 —— 模型不许碰日历
  · due_display 必须由代码生成,模型照抄 —— 换算星期的活一旦交给模型就会错
  · 改/销必须按 T 号,坏号/丢号要报人话,而不是静默落在错误的任务上
  · 「今天到期」不算逾期 —— 差一天,师傅就会被冤枉拖工期

约定:今天一律打桩成 2026-08-08(周六),与 docs/W3_Schedule_Agent_计划.md 的
示例同一天,人肉核对方便。数据库走 conftest 的 tmp 数据目录隔离,用真 SQLite。
"""

from __future__ import annotations

from datetime import date

import pytest

from gyt.agents.schedule import tools as schedule_tools
from gyt.agents.schedule.tools import add_task, finish_task, list_tasks, reschedule_task
from gyt.db import tasks as db

TODAY = date(2026, 8, 8)  # 周六
ENVELOPE_KEYS = {"ok", "data", "user_msg", "error_code"}

HAZARD_NO = "GYT-H-20260816-101500-a1b2"
"""隐患号样例,形状同 db/hazards.py 的 GYT-H-YYYYMMDD-HHMMSS-4hex。
带这个号的任务 = 隐患整改的活,销项与改期都只有 supervision 一个出口(W9 D13)。"""

_REAL_TODAY = schedule_tools._today
"""_today 的真身引用,在任何 fixture 打桩之前(import 期)抓住,供真身测试用。"""


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「今天」钉死:日期断言不许依赖跑测试的真实日子。"""
    monkeypatch.setattr(schedule_tools, "_today", lambda: TODAY)


# ---------------------------------------------------------------------------
# add_task —— 记任务
# ---------------------------------------------------------------------------


async def test_记任务_带期限_返回T号与due_display() -> None:
    result = await add_task.ainvoke({"title": "复检三层钢筋", "due": "明天"})

    assert set(result) == ENVELOPE_KEYS
    assert result["ok"] is True
    data = result["data"]
    assert data["task_id"] == "T1"
    assert data["title"] == "复检三层钢筋"
    assert data["due_date"] == "2026-08-09"
    assert data["due_display"] == "8月9日(周日)"
    # 复述闭环的原材料必须出现在 user_msg 里,模型照抄即可
    assert "8月9日(周日)" in result["user_msg"]

    row = db.fetch(1)
    assert row is not None
    assert (row.title, row.due_date, row.status) == ("复检三层钢筋", "2026-08-09", "open")


async def test_记任务_无期限() -> None:
    result = await add_task.ainvoke({"title": "清理西侧通道"})

    assert result["ok"] is True
    assert result["data"]["due_date"] is None
    assert result["data"]["due_display"] is None
    assert "没定期限" in result["user_msg"]


async def test_记任务_标题剥空白() -> None:
    result = await add_task.ainvoke({"title": "  复检  ", "due": ""})

    assert result["data"]["title"] == "复检"
    assert db.fetch(1).title == "复检"  # type: ignore[union-attr]


async def test_记任务_空标题拒绝() -> None:
    result = await add_task.ainvoke({"title": "   "})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "标题" in result["user_msg"]
    assert db.list_rows() == []  # 没写进台账


async def test_记任务_日期看不懂时把dates层的人话透传() -> None:
    """dates 层的报错已经是带示例的人话,工具不许再包一层官腔盖掉它。"""
    result = await add_task.ainvoke({"title": "复检", "due": "乱七八糟"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "明天" in result["user_msg"]  # 兜底消息里的举例
    assert db.list_rows() == []


# ---------------------------------------------------------------------------
# list_tasks —— 查任务
# ---------------------------------------------------------------------------


def _seed_four() -> None:
    """一条无期限 + 三条有期限(其中一条已逾期)。id 依插入序 1..4。"""
    db.create("清理西侧通道", None)  # T1 无期限
    db.create("模板验收", "2026-08-10")  # T2
    db.create("补临边护栏", "2026-08-01")  # T3 已逾期
    db.create("配电箱复查", "2026-08-20")  # T4


async def test_查任务_按期限升序且逾期在最前() -> None:
    _seed_four()

    result = await list_tasks.ainvoke({})

    assert result["ok"] is True
    data = result["data"]
    assert data["today"] == "2026-08-08"
    assert [t["id"] for t in data["tasks"]] == ["T3", "T2", "T4"]
    assert [t["overdue"] for t in data["tasks"]] == [True, False, False]
    assert data["tasks"][0]["due_display"] == "8月1日(周六)"
    assert [t["id"] for t in data["undated"]] == ["T1"]
    assert data["undated"][0]["due_display"] is None


async def test_查任务_within截止过滤_无期限单列不吞掉() -> None:
    """「下周三之前」不该把没定期限的活吞掉 —— 它们单独一桶,由提示词带一句。"""
    _seed_four()

    result = await list_tasks.ainvoke({"within": "下周三之前"})

    data = result["data"]
    assert data["cutoff"] == "2026-08-12"
    assert [t["id"] for t in data["tasks"]] == ["T3", "T2"]  # 8-20 被截掉
    assert [t["id"] for t in data["undated"]] == ["T1"]


async def test_查任务_今天到期不算逾期() -> None:
    db.create("今日复查", "2026-08-08")

    result = await list_tasks.ainvoke({})

    assert result["data"]["tasks"][0]["overdue"] is False


async def test_查任务_include_done两态() -> None:
    db.create("模板验收", "2026-08-10")
    db.set_done(1)

    hidden = await list_tasks.ainvoke({})
    shown = await list_tasks.ainvoke({"include_done": True})

    assert hidden["data"]["tasks"] == []
    assert [t["status"] for t in shown["data"]["tasks"]] == ["done"]


async def test_查任务_空台账如实说空() -> None:
    result = await list_tasks.ainvoke({})

    assert result["ok"] is True
    assert result["data"]["tasks"] == []
    assert result["data"]["undated"] == []
    assert "没有" in result["user_msg"]


async def test_查任务_within看不懂时拒绝() -> None:
    result = await list_tasks.ainvoke({"within": "猴年马月"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# reschedule_task —— 改期
# ---------------------------------------------------------------------------


async def test_改期_正常_新旧期限都回给模型照抄() -> None:
    db.create("模板验收", "2026-08-10")

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})

    assert result["ok"] is True
    data = result["data"]
    assert data["task_id"] == "T1"
    assert data["old_due"] == "2026-08-10"
    assert data["new_due"] == "2026-08-14"
    assert data["due_display"] == "8月14日(周五)"
    assert data["old_due_display"] == "8月10日(周一)"
    assert db.fetch(1).due_date == "2026-08-14"  # type: ignore[union-attr]


async def test_改期_T号大小写与空白容错() -> None:
    db.create("模板验收", "2026-08-10")

    result = await reschedule_task.ainvoke({"task_id": "  t1 ", "due": "周五"})

    assert result["ok"] is True


async def test_改期_纯数字号也认() -> None:
    db.create("模板验收", "2026-08-10")

    result = await reschedule_task.ainvoke({"task_id": "1", "due": "周五"})

    assert result["ok"] is True


async def test_改期_不存在的号报人话() -> None:
    result = await reschedule_task.ainvoke({"task_id": "T99", "due": "周五"})

    assert result["ok"] is False
    assert result["error_code"] == "NOT_FOUND"
    assert "T99" in result["user_msg"]


async def test_改期_已完成的任务拒绝改() -> None:
    db.create("模板验收", "2026-08-10")
    db.set_done(1)

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "销" in result["user_msg"] or "完成" in result["user_msg"]
    assert db.fetch(1).due_date == "2026-08-10"  # type: ignore[union-attr]


async def test_改期_写入时状态守卫兜底不许假装成功(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch 看到 open、真正写入前状态被改(并发销项)的窗口:
    db.set_due 的状态守卫会返回 False,工具必须如实报「状态变过」——
    嘴上说「改好了」库里却没动,台账就废了。"""
    db.create("模板验收", "2026-08-10")
    monkeypatch.setattr(schedule_tools.db, "set_due", lambda *_a, **_k: False)

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "状态" in result["user_msg"]


async def test_改期_没说改到哪天拒绝() -> None:
    db.create("模板验收", "2026-08-10")

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "  "})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"


async def test_改期_日期看不懂时透传dates层人话() -> None:
    db.create("模板验收", "2026-08-10")

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "猴年马月"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "明天" in result["user_msg"]  # dates 层兜底消息里的举例
    assert db.fetch(1).due_date == "2026-08-10"  # type: ignore[union-attr]


async def test_改期_坏号拒绝并教正确写法() -> None:
    result = await reschedule_task.ainvoke({"task_id": "abc", "due": "周五"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "T" in result["user_msg"]


# ---------------------------------------------------------------------------
# finish_task —— 销项
# ---------------------------------------------------------------------------


async def test_销项_正常() -> None:
    db.create("模板验收", "2026-08-10")

    result = await finish_task.ainvoke({"task_id": "T1"})

    assert result["ok"] is True
    assert result["data"] == {"task_id": "T1", "title": "模板验收", "was_done": False}
    assert "销" in result["user_msg"]
    assert db.fetch(1).status == "done"  # type: ignore[union-attr]


async def test_销项_重复销是幂等不是报错() -> None:
    """多轮对话里用户重复说「T1 干完了」不该被吓一句错误。"""
    db.create("模板验收", "2026-08-10")
    await finish_task.ainvoke({"task_id": "T1"})

    result = await finish_task.ainvoke({"task_id": "T1"})

    assert result["ok"] is True
    assert result["data"]["was_done"] is True
    assert "本来就" in result["user_msg"]


async def test_销项_坏号拒绝并教正确写法() -> None:
    result = await finish_task.ainvoke({"task_id": "随便写的"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "T" in result["user_msg"]


async def test_销项_不存在的号报人话() -> None:
    result = await finish_task.ainvoke({"task_id": "T5"})

    assert result["ok"] is False
    assert result["error_code"] == "NOT_FOUND"
    assert "T5" in result["user_msg"]


# ---------------------------------------------------------------------------
# W9 / S7:带隐患号的活 —— 销项与改期都只有 supervision 一个出口(D13)
#
# 为什么是拒绝而不是"顺手把隐患那边也同步改掉":隐患要转 closed,前提是挂上复查照片
# 并由人点确认(方案 D11/D14),schedule 这层两样都没有,自动同步只能造出一个
# 「没有证据的合格」,而它会被写进留档文书。两边各改各的则会让期限分叉,
# 而超期判定读的是隐患那一份 —— 最后系统拿着我们自己记乱的账,建议签发
# 《监理报告》指控施工方拒不整改。详见 agents/schedule/tools.py 的文件头。
# ---------------------------------------------------------------------------


async def test_销项_隐患整改的活拒绝并报出隐患号() -> None:
    """拒绝要给出口:告诉他下一步拍照片找监理,并报出隐患号 —— 他要拿着号去对账。"""
    db.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_NO)

    result = await finish_task.ainvoke({"task_id": "T1"})

    assert set(result) == ENVELOPE_KEYS
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert HAZARD_NO in result["user_msg"]
    assert "照片" in result["user_msg"]  # 下一步该干什么,得说清
    assert db.fetch(1).status == "open"  # type: ignore[union-attr]  库里一个字节没动


async def test_改期_隐患整改的活拒绝并报出隐患号() -> None:
    """期限的权威在隐患台账那边:从这儿单方面改,等于用一句聊天改掉一份已签发文书的期限。"""
    db.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_NO)

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert HAZARD_NO in result["user_msg"]
    assert "监理" in result["user_msg"]
    assert db.fetch(1).due_date == "2026-08-10"  # type: ignore[union-attr]  期限没动


async def test_销项_隐患整改的活已经销过了也照样拒() -> None:
    """拒绝是无条件的,摆在幂等分支**前面**:库里若有一条已被销掉的带号任务
    (旧数据,或绕过工具层写进去的),再销一次也得拒 ——
    否则就留下「第二次调用反而成功」这种最难查的窗口。"""
    db.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_NO)
    db.set_done(1)

    result = await finish_task.ainvoke({"task_id": "T1"})

    assert result["ok"] is False
    assert HAZARD_NO in result["user_msg"]
    assert "本来就" not in result["user_msg"]  # 没落到幂等分支


async def test_改期_隐患整改的活已销项时报的是隐患不是已完成() -> None:
    """守卫排在「已销项不许改期」之前:这条任务眼下什么状态都不影响结论 ——
    这里就不是改它期限的地方。回错话会把人指到「新记一条任务」那条岔路上。"""
    db.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_NO)
    db.set_done(1)

    result = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})

    assert result["ok"] is False
    assert HAZARD_NO in result["user_msg"]
    assert "不用改期" not in result["user_msg"]


async def test_拒绝话术除了编号之外没有一个英文字母() -> None:
    """面向用户的字符串必须是工地师傅看得懂的人话:不许漏出堆栈、类名、内部路径、英文术语。
    隐患号和 T 号是他要报出去对账的编号,得留着;把这两个抠掉之后,
    剩下的应该一个英文字母都没有。"""
    db.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_NO)

    finished = await finish_task.ainvoke({"task_id": "T1"})
    rescheduled = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})

    for result in (finished, rescheduled):
        residue = result["user_msg"].replace(HAZARD_NO, "").replace("T1", "")
        assert not any(ch.isascii() and ch.isalpha() for ch in residue), residue


async def test_普通任务不受隐患守卫影响_销项与改期照旧() -> None:
    """回归面:守卫只认**非空**的隐患号,hazard_no 为空的活行为一个字都没变。
    同库里放一条带号的活当对照 —— 它必须原封不动。"""
    db.create("模板验收", "2026-08-10")  # T1 普通任务
    db.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_NO)  # T2 隐患整改

    rescheduled = await reschedule_task.ainvoke({"task_id": "T1", "due": "周五"})
    finished = await finish_task.ainvoke({"task_id": "T1"})

    assert rescheduled["ok"] is True
    assert rescheduled["data"]["new_due"] == "2026-08-14"
    assert finished["ok"] is True
    assert finished["data"] == {"task_id": "T1", "title": "模板验收", "was_done": False}
    assert db.fetch(1).status == "done"  # type: ignore[union-attr]
    assert db.fetch(2).status == "open"  # type: ignore[union-attr]  隔壁那条没被殃及


async def test_查任务_带号的活在清单里与普通活长得完全一样() -> None:
    """list_tasks 的返回是模型照抄的原料,prompt.md 那张表固定四列。
    多一个 hazard_no 键,模型就会想办法把隐患号念出来,而念出来的下一步就是
    凭记忆复述(report 守卫抓到过同款失效)。整个信封里一个字都不许漏出去。"""
    db.create("模板验收", "2026-08-10")
    db.create("补临边护栏", "2026-08-11", hazard_no=HAZARD_NO)

    result = await list_tasks.ainvoke({})

    plain, hazard_task = result["data"]["tasks"]
    assert set(plain) == set(hazard_task)  # 两类任务的键集完全一致
    assert "hazard_no" not in hazard_task
    assert HAZARD_NO not in str(result)


# ---------------------------------------------------------------------------
# 共性契约
# ---------------------------------------------------------------------------


async def test_失败信封也是四键齐全() -> None:
    result = await finish_task.ainvoke({"task_id": "T5"})

    assert set(result) == ENVELOPE_KEYS
    assert result["data"] is None


def test_今天函数真身读的是系统日历() -> None:
    """全文件都把 _today 打了桩,这条用 import 期抓住的真身引用来验证 ——
    它要是悄悄坏了(比如被人改成模块级常量),台账的「今天」会永远停在进程启动那天。

    刻意**不用** monkeypatch.undo():monkeypatch 是函数作用域单例,undo 会把
    conftest 那套环境隔离(假 Key/tmp 数据目录)一起撤掉,静默污染真实目录(审查 LOW 项)。
    """
    assert _REAL_TODAY() == date.today()


def test_工具注册表齐全且都带中文描述() -> None:
    """SCHEDULE_TOOLS 是挂给 create_gyt_agent 的唯一入口,少一个工具=少一样活。"""
    names = {t.name for t in schedule_tools.SCHEDULE_TOOLS}
    assert names == {"add_task", "list_tasks", "reschedule_task", "finish_task"}
    for t in schedule_tools.SCHEDULE_TOOLS:
        assert t.description.strip(), f"{t.name} 没写描述,模型不知道什么时候该用它"
