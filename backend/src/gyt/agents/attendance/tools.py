"""Attendance Agent 的工具集 —— 考勤查询两件套:数天数 / 看某天明细。

===========================================================================
分层责任(谁的错在谁那层炸,别串)
---------------------------------------------------------------------------
    模型(prompt.md)      只负责:把用户的时间**原话**塞进参数、照抄 *_display
        │
        ▼ 本文件(工具层)   校验在边界:时间短语交给 ranges 层,姓名剥空白
        │                  信封契约 {ok, data, user_msg},失败全是中文人话
        ▼ ranges.py        中文日历唯一真相源(词表+方向约定都在那边,别抄过来)
        ▼ db/attendance.py 只读聚合(SQL 里带 GROUP BY 与 LIMIT);同步实现,
          │                这里用 asyncio.to_thread 包(blockbuster 那课同 schedule)
          ▼ 写入侧          checkin_api.py 直连 HTTP,一个 LLM 都不经过(D15)——
                           本文件**没有任何写库工具**,这是设计,不是没写完。

===========================================================================
最小化约定(W7 方案 §3.10,只约束 LLM 侧)
---------------------------------------------------------------------------
两个工具的返回值(data 与 user_msg)**绝不含经纬度、绝不含 artifact_id**:
坐标与凭证图对「查考勤」没有用,进了返回值就是进模型上下文,再被复述就出圈了。
所以 data 一律**逐字段挑着装**(哪怕存储层哪天多返回字段,这里也不跟着漏)。
⚠️ 直连接口 GET /checkin/recent 返回 artifact_id 是**刻意的**(§3.7,前端要渲染
凭证卡片),与这里不冲突 —— 两条路径受众不同,别来「统一」。

===========================================================================
「今天」为什么不是 date.today()
---------------------------------------------------------------------------
写入侧的 work_date 是**香港日历日**(attendance/receipt.py 的单一快照,时区
写死 Asia/Hong_Kong,不信宿主 TZ)。查询侧的「今天」必须用同一本日历,否则
跨午夜的窗口里「今天谁到了」会去查昨天的记录。依赖方向 agents/attendance →
attendance → core 是 W7 §3.12 定下的正向,可以放心 import。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Final

from langchain_core.tools import tool

from gyt.agents.attendance.ranges import (
    RangeParseError,
    format_day,
    format_range,
    parse_day,
    parse_range,
)
from gyt.attendance.receipt import HK
from gyt.config import get_settings
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.db import attendance as db

_DAYS_DESCRIPTION = (
    "查考勤台账:某段时间里每个人出勤了几天、打了几次卡(「张三这个月来了几天」"
    "「上周都谁来过」)。within 填用户说的时间段**原话**(如「这个月」「上周」「最近7天」),"
    "留空按本月算;worker_name 填要查的工人姓名,留空查全组。"
    "返回按出勤天数从多到少的清单,回复用户念时间段时照抄返回里的 range_display。"
)

_DETAIL_DESCRIPTION = (
    "查某一天的打卡明细:那天谁到了、几点打的卡(「今天谁到了」「张三昨天几点打的」)。"
    "on_date 填用户说的日子**原话**(如「今天」「昨天」「8月5日」),只能是一天,"
    "一段时间(「这个月」)会被拒绝;worker_name 填工人姓名,留空查当天所有人。"
    "每人返回首次、末次打卡时间(HH:MM)和总次数,没有逐笔流水。"
    "回复用户念日子时照抄返回里的 day_display。"
)

_HM_SLICE: Final[slice] = slice(11, 16)
"""从 checked_at 里切出 HH:MM 的位置。

敢用切片不用 datetime 解析,是因为**写入口唯一**:所有 checked_at 都出自
attendance/receipt.py 的 ``snapshot_at``(``isoformat(timespec="seconds")``,
定宽「2026-08-15T08:30:00+08:00」,第 11~15 位恒为时分)。db/attendance.py 的
MIN/MAX 按文本序比较靠的也是同一条前提 —— 哪天多出第二个写入口,先坏的是那边的
时间序,这里只是跟着显示错。真到那天,两处一起换 datetime 解析。
"""


def _today() -> date:
    """考勤口径的「今天」= 香港日历日(与写入侧 work_date 同源)。独立成函数供测试打桩。"""
    return datetime.now(HK).date()


def _hm(checked_at: str) -> str:
    """「2026-08-15T08:30:00+08:00」→「08:30」。前提见 _HM_SLICE 的说明。"""
    return checked_at[_HM_SLICE]


def _clean_name(worker_name: str) -> str | None:
    """姓名剥空白;空 = 查全组(存储层收 None)。不做任何简繁转换 —— 姓名是例外中的例外。"""
    cleaned = (worker_name or "").strip()
    return cleaned or None


def _empty_msg(scope_display: str, name: str | None) -> str:
    """空结果的人话。与 schedule 的口径一致:查空是 ok + 如实说,不是错误。

    指名查空多半是名字写得跟打卡时不一样(打卡页存什么名就得查什么名),
    提示一句,省得师傅以为人真没来。
    """
    if name is None:
        return f"{scope_display}没有打卡记录。"
    return f"{scope_display}没有{name}的打卡记录(名字要和打卡时填的一致,再核对一下)。"


@tool("list_attendance_days", description=_DAYS_DESCRIPTION)
@tool_guard
async def list_attendance_days(within: str, worker_name: str = "") -> Envelope:
    """按人数出勤天数。within 收中文原话,解析失败原样透传 ranges 层的人话。"""
    today = _today()  # 只取一次快照:解析与展示用同一个「今天」,跨午夜也不劈叉
    try:
        date_from, date_to = parse_range(within, today=today)
    except RangeParseError as exc:
        # ranges 层的报错已经是带示例的人话,原样透传 —— 再包官腔只会盖掉「该怎么说」
        return fail(ErrorCode.INVALID_INPUT, user_msg=str(exc))

    name = _clean_name(worker_name)
    limit = get_settings().attendance_query_max_workers
    # 多取一行只为判断「后面还有没有人」,第 limit+1 行绝不外泄(下面立刻切掉)
    rows = await asyncio.to_thread(
        db.count_worker_days,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        worker_name=name,
        max_workers=limit + 1,
    )
    truncated = len(rows) > limit
    rows = rows[:limit]

    range_display = format_range(date_from, date_to, today=today)
    # data 逐字段挑着装:绝不整行透传存储层返回(最小化约定,见模块头注)
    data: dict[str, Any] = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "range_display": range_display,
        "workers": [
            {"worker_name": row.worker_name, "days": row.days, "checkins": row.checkins}
            for row in rows
        ],
        "truncated": truncated,
    }
    if not rows:
        return ok(data=data, user_msg=_empty_msg(range_display, name))

    lines = [f"{row.worker_name}:出勤 {row.days} 天(打卡 {row.checkins} 次)" for row in rows]
    summary = f"查到了,{range_display}:\n" + "\n".join(lines)
    if truncated:
        # SQL 按天数降序 + LIMIT,截掉的是出勤最少的那批 —— 如实说,不装全乎
        summary += f"\n人太多,只列前 {limit} 人(按出勤天数从多到少),后面的没显示。"
    return ok(data=data, user_msg=summary)


@tool("list_attendance_detail", description=_DETAIL_DESCRIPTION)
@tool_guard
async def list_attendance_detail(on_date: str = "今天", worker_name: str = "") -> Envelope:
    """某天按人明细(首次/末次/次数)。on_date 只收单日词形,一段时间会被 ranges 层拒绝。"""
    today = _today()
    try:
        day = parse_day(on_date, today=today)
    except RangeParseError as exc:
        return fail(ErrorCode.INVALID_INPUT, user_msg=str(exc))

    name = _clean_name(worker_name)
    limit = get_settings().attendance_query_max_workers
    rows = await asyncio.to_thread(
        db.list_day_detail,
        day.isoformat(),
        worker_name=name,
        max_workers=limit + 1,
    )
    truncated = len(rows) > limit
    rows = rows[:limit]

    day_display = format_day(day, today=today)
    data: dict[str, Any] = {
        "work_date": day.isoformat(),
        "day_display": day_display,
        # 只给 HH:MM 与次数,不给完整时间戳 —— 模型用不上秒和时区偏移,少给少错
        "workers": [
            {
                "worker_name": row.worker_name,
                "first_hm": _hm(row.first_at),
                "last_hm": _hm(row.last_at),
                "checkins": row.checkins,
            }
            for row in rows
        ],
        "truncated": truncated,
    }
    if not rows:
        return ok(data=data, user_msg=_empty_msg(day_display, name))

    lines = [_detail_line(row) for row in rows]
    if truncated:
        # SQL 按首次打卡时间升序 + LIMIT,截掉的是到得晚的那批
        header = f"{day_display}打卡的不止 {limit} 人,只列前 {limit} 人(到得早的在前):"
    else:
        header = f"{day_display}有 {len(rows)} 个人打过卡:"
    return ok(data=data, user_msg=header + "\n" + "\n".join(lines))


def _detail_line(row: db.WorkerDayDetail) -> str:
    """一个人一行。只打过 1 次时首末是同一时刻,复读两遍是机器话,单列一种说法。

    次数 ≥2 必带「共打了 N 次」:首末之间可能还有中间流水(D10 不限次数),
    明细口径只到首末+次数(§3.10 定死),让师傅知道有 N 次、要逐笔就再问管理员。
    """
    first, last = _hm(row.first_at), _hm(row.last_at)
    if row.checkins == 1:
        return f"{row.worker_name}:{first} 打了 1 次"
    return f"{row.worker_name}:首次 {first},末次 {last},共打了 {row.checkins} 次"


ATTENDANCE_TOOLS: list = [list_attendance_days, list_attendance_detail]

__all__ = [
    "ATTENDANCE_TOOLS",
    "list_attendance_days",
    "list_attendance_detail",
]
