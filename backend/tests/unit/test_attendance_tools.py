"""Attendance 工具层(agents/attendance/tools.py)的单元测试 —— 不联网、不调模型。

这层是「模型的嘴」与「考勤台账」之间唯一的桥,测试重点不是覆盖率,
而是把几类**会让考勤悄悄报错数**的失败钉死:
  · 时间解析必须发生在代码里(ranges 层),工具只透传人话错误 —— 模型不许碰日历
  · range_display / day_display 必须由代码生成,模型照抄 —— 换算星期的活交给模型就会错
  · **返回值绝不含经纬度、绝不含 artifact_id**(W7 §3.10 的最小化约定):
    造数时故意喂真坐标和凭证图 id,再整包序列化搜字样 —— 存储层哪天多吐字段,
    这里第一时间翻红
  · 人数超上限要如实说「只列前 N 人」,不许装作列全了
  · 空结果是 ok + 人话(口径对齐 schedule 的「台账里现在没有任务」),不是错误

约定:今天一律打桩成 2026-08-12(周三),与 test_attendance_ranges 的 BASE 同一天。
数据走 conftest 的 tmp 数据目录隔离,用真 SQLite(db/attendance.py 全链真跑),
造数用 insert_checkin + CheckinDraft —— 与直连写入口同一条入库路径。
"""

from __future__ import annotations

import itertools
import json
from datetime import date, datetime

import pytest

from gyt.agents.attendance import tools as attendance_tools
from gyt.agents.attendance.tools import list_attendance_days, list_attendance_detail
from gyt.attendance.receipt import HK
from gyt.config import get_settings
from gyt.db import attendance as db

TODAY = date(2026, 8, 12)  # 周三
ENVELOPE_KEYS = {"ok", "data", "user_msg", "error_code"}

_REAL_TODAY = attendance_tools._today
"""_today 的真身引用,在任何 fixture 打桩之前(import 期)抓住,供真身测试用。"""

_SEQ = itertools.count(1)
"""event_id / receipt_no 的唯一序号。跨用例递增无妨 —— 库本身每个用例都是新的。"""


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「今天」钉死:日期断言不许依赖跑测试的真实日子。"""
    monkeypatch.setattr(attendance_tools, "_today", lambda: TODAY)


def _checkin(worker: str, work_date: str, hm: str) -> None:
    """造一条打卡。故意喂真坐标与凭证图 id ——「返回值不含它们」那条测试靠这份数据才有意义。"""
    seq = next(_SEQ)
    iso = f"{work_date}T{hm}:00+08:00"  # 与 receipt.snapshot_at 的定宽格式一致
    db.insert_checkin(
        db.CheckinDraft(
            event_id=f"evt-{seq:04d}",
            req_digest="d" * 64,
            worker_name=worker,
            site_name="A栋",
            checked_at=iso,
            work_date=work_date,
            lat=22.302711,
            lon=114.177216,
            accuracy_m=12.5,
            geo_status="ok",
            source="camera",
            receipt_no=f"GYT-A-{work_date.replace('-', '')}-{hm.replace(':', '')}00-{seq:04d}",
            artifact_id="f" * 32,
            created_at=iso,
        )
    )


def _use_max_workers(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """收紧查询人数上限。conftest 已清过 GYT_*,这里设完必须重置 Settings 单例。"""
    monkeypatch.setenv("GYT_ATTENDANCE_QUERY_MAX_WORKERS", str(value))
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# list_attendance_days —— 数天数
# ---------------------------------------------------------------------------


async def test_数天数_全组_每人一行按天数降序() -> None:
    _checkin("张三", "2026-08-10", "08:00")
    _checkin("张三", "2026-08-10", "17:30")
    _checkin("张三", "2026-08-11", "08:05")
    _checkin("张三", "2026-08-11", "17:40")
    _checkin("李四", "2026-08-11", "07:50")

    result = await list_attendance_days.ainvoke({"within": "这个月"})

    assert set(result) == ENVELOPE_KEYS
    assert result["ok"] is True
    data = result["data"]
    assert data["date_from"] == "2026-08-01"
    assert data["date_to"] == "2026-08-12"
    assert data["range_display"] == "8月1日到8月12日"
    assert data["truncated"] is False
    assert data["workers"] == [
        {"worker_name": "张三", "days": 2, "checkins": 4},
        {"worker_name": "李四", "days": 1, "checkins": 1},
    ]
    # 复述闭环的原材料必须出现在 user_msg 里,模型照抄即可
    msg = result["user_msg"]
    assert "8月1日到8月12日" in msg
    assert "张三:出勤 2 天(打卡 4 次)" in msg
    assert "李四:出勤 1 天(打卡 1 次)" in msg
    assert msg.index("张三") < msg.index("李四")  # 天数降序,张三在前


async def test_数天数_指定工人只查他() -> None:
    _checkin("张三", "2026-08-10", "08:00")
    _checkin("李四", "2026-08-11", "07:50")

    result = await list_attendance_days.ainvoke({"within": "这个月", "worker_name": " 张三 "})

    assert result["ok"] is True
    assert [w["worker_name"] for w in result["data"]["workers"]] == ["张三"]
    assert "李四" not in result["user_msg"]


async def test_数天数_范围真的在过滤() -> None:
    """上月的记录不许混进「这个月」;「上月」要能单独把它捞出来。"""
    _checkin("王五", "2026-07-15", "08:00")
    _checkin("张三", "2026-08-10", "08:00")

    this_month = await list_attendance_days.ainvoke({"within": "这个月"})
    last_month = await list_attendance_days.ainvoke({"within": "上月"})

    assert [w["worker_name"] for w in this_month["data"]["workers"]] == ["张三"]
    assert [w["worker_name"] for w in last_month["data"]["workers"]] == ["王五"]
    assert last_month["data"]["range_display"] == "7月1日到7月31日"


async def test_数天数_没说时段按本月算并回显起止() -> None:
    """within 留空 = 本月(缺省口径)。缺省必须随 range_display 回显 —— 回显就不算猜。"""
    _checkin("张三", "2026-08-10", "08:00")

    result = await list_attendance_days.ainvoke({"within": ""})

    assert result["ok"] is True
    assert result["data"]["date_from"] == "2026-08-01"
    assert result["data"]["date_to"] == "2026-08-12"
    assert "8月1日到8月12日" in result["user_msg"]


async def test_数天数_空台账如实说没有() -> None:
    result = await list_attendance_days.ainvoke({"within": "这个月"})

    assert result["ok"] is True  # 查空不是错误 —— 口径对齐 schedule 的空台账
    assert result["error_code"] is None
    assert result["data"]["workers"] == []
    assert "没有打卡记录" in result["user_msg"]


async def test_数天数_指定工人查无_提醒核对名字() -> None:
    """指名查空多半是名字和打卡时填的不一致,提示语必须把这层说破。"""
    _checkin("张三", "2026-08-10", "08:00")

    result = await list_attendance_days.ainvoke({"within": "这个月", "worker_name": "张四"})

    assert result["ok"] is True
    assert result["data"]["workers"] == []
    assert "张四" in result["user_msg"]
    assert "打卡时填的一致" in result["user_msg"]


async def test_数天数_时间看不懂时透传ranges层人话() -> None:
    """ranges 层的报错已经是带示例的人话,工具不许再包一层官腔盖掉它。"""
    result = await list_attendance_days.ainvoke({"within": "猴年马月"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "猴年马月" in result["user_msg"]
    assert "今天" in result["user_msg"]  # 兜底消息里的举例


async def test_数天数_未来时段拒绝() -> None:
    result = await list_attendance_days.ainvoke({"within": "8月20日"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "还没到" in result["user_msg"]


async def test_数天数_超上限如实说只列前N人(monkeypatch: pytest.MonkeyPatch) -> None:
    """LIMIT 截断是静默的:不如实说,班组长会以为全组就这几个人来过。"""
    _use_max_workers(monkeypatch, 2)
    _checkin("张三", "2026-08-10", "08:00")
    _checkin("张三", "2026-08-11", "08:00")
    _checkin("张三", "2026-08-12", "08:00")
    _checkin("李四", "2026-08-10", "08:10")
    _checkin("李四", "2026-08-11", "08:10")
    _checkin("王五", "2026-08-10", "08:20")

    result = await list_attendance_days.ainvoke({"within": "这个月"})

    assert result["ok"] is True
    data = result["data"]
    assert data["truncated"] is True
    # 按天数降序截断:留下张三(3 天)、李四(2 天),王五(1 天)被截掉且不外泄
    assert [w["worker_name"] for w in data["workers"]] == ["张三", "李四"]
    assert "只列前 2 人" in result["user_msg"]
    assert "王五" not in result["user_msg"]


# ---------------------------------------------------------------------------
# list_attendance_detail —— 某天明细
# ---------------------------------------------------------------------------


async def test_明细_首末次数口径与到得早的在前() -> None:
    _checkin("张三", "2026-08-12", "08:00")
    _checkin("张三", "2026-08-12", "12:30")
    _checkin("张三", "2026-08-12", "17:30")
    _checkin("李四", "2026-08-12", "07:45")

    result = await list_attendance_detail.ainvoke({})  # 默认 on_date="今天"

    assert result["ok"] is True
    data = result["data"]
    assert data["work_date"] == "2026-08-12"
    assert data["day_display"] == "8月12日(周三)"
    assert data["truncated"] is False
    # 按首次打卡升序:李四(07:45)在张三(08:00)前
    assert data["workers"] == [
        {"worker_name": "李四", "first_hm": "07:45", "last_hm": "07:45", "checkins": 1},
        {"worker_name": "张三", "first_hm": "08:00", "last_hm": "17:30", "checkins": 3},
    ]
    msg = result["user_msg"]
    assert "8月12日(周三)" in msg
    assert "李四:07:45 打了 1 次" in msg  # 只打 1 次不复读首末
    assert "首次 08:00" in msg
    assert "末次 17:30" in msg
    assert "打了 3 次" in msg  # 次数 >2:让人知道首末之间还有流水


async def test_明细_指定日子与指定工人_别的日子不混进来() -> None:
    _checkin("张三", "2026-08-11", "08:10")
    _checkin("张三", "2026-08-12", "09:00")
    _checkin("李四", "2026-08-11", "07:50")

    result = await list_attendance_detail.ainvoke({"on_date": "昨天", "worker_name": "张三"})

    assert result["ok"] is True
    assert result["data"]["work_date"] == "2026-08-11"
    assert result["data"]["workers"] == [
        {"worker_name": "张三", "first_hm": "08:10", "last_hm": "08:10", "checkins": 1}
    ]


async def test_明细_时段词不挡路() -> None:
    _checkin("张三", "2026-08-11", "15:00")

    result = await list_attendance_detail.ainvoke({"on_date": "昨天下午"})

    assert result["ok"] is True
    assert result["data"]["work_date"] == "2026-08-11"


async def test_明细_一段时间拒绝并教挑一天() -> None:
    result = await list_attendance_detail.ainvoke({"on_date": "这个月"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "挑一天" in result["user_msg"]


async def test_明细_未来日子拒绝() -> None:
    result = await list_attendance_detail.ainvoke({"on_date": "8月20日"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "还没到" in result["user_msg"]


async def test_明细_空结果如实说那天没有() -> None:
    result = await list_attendance_detail.ainvoke({"on_date": "昨天"})

    assert result["ok"] is True
    assert result["data"]["workers"] == []
    assert "8月11日(周二)" in result["user_msg"]
    assert "没有打卡记录" in result["user_msg"]


async def test_明细_指定工人查无_提醒核对名字() -> None:
    _checkin("张三", "2026-08-12", "08:00")

    result = await list_attendance_detail.ainvoke({"worker_name": "张四"})

    assert result["ok"] is True
    assert "张四" in result["user_msg"]
    assert "打卡时填的一致" in result["user_msg"]


async def test_明细_超上限如实说只列前N人(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_max_workers(monkeypatch, 2)
    _checkin("张三", "2026-08-12", "08:00")
    _checkin("李四", "2026-08-12", "07:45")
    _checkin("王五", "2026-08-12", "09:30")

    result = await list_attendance_detail.ainvoke({})

    assert result["ok"] is True
    data = result["data"]
    assert data["truncated"] is True
    # 按首次时间升序截断:留下到得早的李四、张三,王五(09:30)被截掉且不外泄
    assert [w["worker_name"] for w in data["workers"]] == ["李四", "张三"]
    assert "只列前 2 人" in result["user_msg"]
    assert "王五" not in result["user_msg"]


# ---------------------------------------------------------------------------
# 共性契约
# ---------------------------------------------------------------------------


async def test_返回值翻不出经纬度与凭证图id() -> None:
    """W7 §3.10 最小化约定的守门:造数时喂了真坐标(22.30…/114.17…)与凭证图 id
    (32 个 f),两个工具的**整包**返回值序列化后必须一个字样都翻不出来。
    存储层哪天多吐字段、或有人把整行透传进 data,这条第一时间翻红。"""
    _checkin("张三", "2026-08-12", "08:00")

    days = await list_attendance_days.ainvoke({"within": "这个月"})
    detail = await list_attendance_detail.ainvoke({})

    for envelope in (days, detail):
        assert envelope["ok"] is True
        payload = json.dumps(envelope, ensure_ascii=False)
        for banned in ("lat", "lon", "artifact", "22.30", "114.17", "f" * 32):
            assert banned not in payload, f"返回值里翻出了 {banned!r}: {payload}"


async def test_失败信封也是四键齐全() -> None:
    result = await list_attendance_detail.ainvoke({"on_date": "猴年马月"})

    assert set(result) == ENVELOPE_KEYS
    assert result["data"] is None


def test_今天函数真身走香港日历() -> None:
    """全文件都把 _today 打了桩,这条用 import 期抓住的真身引用来验证:
    考勤的「今天」必须与写入侧 work_date 同一本日历(Asia/Hong_Kong,receipt.HK),
    改成 date.today() 的话,宿主时区一换,跨午夜窗口里「今天谁到了」就查错一天。

    前后各取一次真日历再断言 in:万一恰好跨午夜,两次采样必有一次相等,不闪红。
    """
    before = datetime.now(HK).date()
    real = _REAL_TODAY()
    after = datetime.now(HK).date()
    assert real in (before, after)


def test_工具注册表齐全且都带中文描述() -> None:
    """ATTENDANCE_TOOLS 是挂给 create_gyt_agent 的唯一入口,少一个工具=少一样活。"""
    names = {t.name for t in attendance_tools.ATTENDANCE_TOOLS}
    assert names == {"list_attendance_days", "list_attendance_detail"}
    for t in attendance_tools.ATTENDANCE_TOOLS:
        assert t.description.strip(), f"{t.name} 没写描述,模型不知道什么时候该用它"
