"""Schedule Agent 的工具集 —— 台账四件套:记 / 查 / 改 / 销。

===========================================================================
分层责任(谁的错在谁那层炸,别串)
---------------------------------------------------------------------------
    模型(prompt.md)      只负责:把用户的日期**原话**塞进参数、照抄 due_display
        │
        ▼ 本文件(工具层)   校验在边界:标题非空、T 号格式、日期短语交给 dates 层
        │                  信封契约 {ok, data, user_msg},失败全是中文人话
        ▼ dates.py         中文日历唯一真相源(词表+歧义约定都在那边,别抄过来)
        ▼ db/tasks.py      SQLite 存取保真;同步实现,这里用 asyncio.to_thread 包

为什么日期与展示串都在代码里生成:
  「下周三」是几月几号、2026-08-14 是周几 —— 模型换算这两件事都不可靠,
  错一次师傅就白跑一天。所以 parse_due 负责算,format_display 负责说,
  模型只许照抄 data 里的 due_display(prompt.md 的日期红线与此对应)。

为什么 db 调用要 asyncio.to_thread:
  工具跑在 langgraph 的事件循环里,同步 sqlite3 属阻塞 IO,`langgraph dev`
  的 blockbuster 会直接抛 BlockingError(config.cache_dir 踩过同款坑)。
  丢线程池就地根治,不赖 `--allow-blocking` 这根拐棍。
===========================================================================
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Final

from langchain_core.tools import tool

from gyt.agents.schedule.dates import DueParseError, format_display, parse_due
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.db import tasks as db

TASK_ID_PREFIX: Final[str] = "T"
"""展示用任务号前缀:台账里的 3 号任务对外一律叫 T3 —— 电话里报得清,
和巡检报告编号「人念得出来」的取向一致。工具收参时大小写、带不带 T 都认。"""

_ADD_DESCRIPTION = (
    "往工地任务台账里记一条新任务。title 填「干什么」(别把「明天」这类日期词留在标题里);"
    "due 填用户说的期限**原话**(如「明天上午」「下周三」「9月1号」),留空表示没定期限。"
    "返回任务号(T 开头)和解析后的日期,回复用户时照抄返回里的 due_display。"
)

_LIST_DESCRIPTION = (
    "查任务台账。within 填用户说的截止期限**原话**(如「下周三之前」),留空查全部;"
    "include_done 为 true 时连已完成的也列出来。返回按期限升序的任务清单"
    "(逾期的排最前且带 overdue 标志),没定期限的在 undated 里单列。"
)

_RESCHEDULE_DESCRIPTION = (
    "给台账里的某条任务改期限。task_id 填任务号(如 T3,来自查询结果或上文,不许猜);"
    "due 填新期限的**原话**(如「周五」)。改完返回新旧日期,回复用户时照抄 due_display。"
)

_FINISH_DESCRIPTION = (
    "把台账里的某条任务标记为完成(销项)。task_id 填任务号(如 T3,"
    "来自查询结果或上文,不许猜)。对已完成的任务重复调用不算错。"
)


def _today() -> date:
    """今天的日期(演示机本地时区 = 北京时间)。独立成函数是给测试打桩用的。"""
    return date.today()


def _display_id(row_id: int) -> str:
    return f"{TASK_ID_PREFIX}{row_id}"


def _parse_task_id(raw: str) -> int | None:
    """把模型传来的任务号收敛成整数 rowid。认 "T3" / "t3" / "3",其余返回 None。"""
    text = (raw or "").strip()
    if text[:1] in (TASK_ID_PREFIX, TASK_ID_PREFIX.lower()):
        text = text[1:]
    return int(text) if text.isdigit() else None


def _parse_due_or_fail(phrase: str) -> tuple[date | None, Envelope | None]:
    """日期短语 → (日期, None) 或 (None, 失败信封)。

    dates 层的报错已经是带示例的人话,这里**原样透传**成 user_msg ——
    再包一层「输入无效」官腔只会把「该怎么说」的信息盖掉。
    """
    try:
        return parse_due(phrase, today=_today()), None
    except DueParseError as exc:
        return None, fail(ErrorCode.INVALID_INPUT, user_msg=str(exc))


def _task_item(row: db.TaskRow, *, today_iso: str) -> dict[str, Any]:
    """一条任务的对外形态。due_display/overdue 都在这儿算好,模型照抄。"""
    return {
        "id": _display_id(row.id),
        "title": row.title,
        "due_date": row.due_date,
        "due_display": format_display(date.fromisoformat(row.due_date)) if row.due_date else None,
        "status": row.status,
        # 「今天到期」不算逾期 —— 差一天就会冤枉师傅拖工期,边界必须是严格小于。
        "overdue": bool(row.due_date and row.due_date < today_iso and row.status == db.STATUS_OPEN),
    }


@tool("add_task", description=_ADD_DESCRIPTION)
@tool_guard
async def add_task(title: str, due: str = "") -> Envelope:
    """记一条任务。due 收中文原话,解析失败原样透传 dates 层的人话。"""
    cleaned = (title or "").strip()
    if not cleaned:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="任务得有个标题,比如「复检三层钢筋」。要记什么活,说一下内容。",
        )

    due_day, error = _parse_due_or_fail(due)
    if error is not None:
        return error

    due_iso = due_day.isoformat() if due_day else None
    task_id = await asyncio.to_thread(db.create, cleaned, due_iso)
    display = format_display(due_day) if due_day else None

    if display:
        summary = f"记上了:{_display_id(task_id)} {cleaned},{display}。"
    else:
        summary = f"记上了:{_display_id(task_id)} {cleaned},没定期限(要定随时说)。"
    return ok(
        data={
            "task_id": _display_id(task_id),
            "title": cleaned,
            "due_date": due_iso,
            "due_display": display,
        },
        user_msg=summary,
    )


@tool("list_tasks", description=_LIST_DESCRIPTION)
@tool_guard
async def list_tasks(within: str = "", include_done: bool = False) -> Envelope:
    """查台账。一次取全后在内存里分桶/截止过滤(方案红线:禁循环逐条查)。"""
    cutoff_day, error = _parse_due_or_fail(within)
    if error is not None:
        return error

    rows = await asyncio.to_thread(db.list_rows, include_done=include_done)
    today_iso = _today().isoformat()
    cutoff_iso = cutoff_day.isoformat() if cutoff_day else None

    dated = [row for row in rows if row.due_date]
    if cutoff_iso:
        # ISO 串按字典序比较即按日期比较,截止日当天算「之前」(含当天)。
        dated = [row for row in dated if row.due_date <= cutoff_iso]  # type: ignore[operator]
    undated = [row for row in rows if not row.due_date]

    tasks = [_task_item(row, today_iso=today_iso) for row in dated]
    open_count = sum(1 for t in tasks if t["status"] == db.STATUS_OPEN)
    if tasks or undated:
        summary = f"查到了:{open_count} 条有期限的没完成,另有 {len(undated)} 条没定期限。"
    else:
        summary = "台账里现在没有任务。"
    return ok(
        data={
            "tasks": tasks,
            "undated": [_task_item(row, today_iso=today_iso) for row in undated],
            "today": today_iso,
            "cutoff": cutoff_iso,
            "cutoff_display": format_display(cutoff_day) if cutoff_day else None,
        },
        user_msg=summary,
    )


@tool("reschedule_task", description=_RESCHEDULE_DESCRIPTION)
@tool_guard
async def reschedule_task(task_id: str, due: str) -> Envelope:
    """改期。校验顺序:号的格式 → 新期限非空 → 日期解析 → 任务存在 → 未销项。"""
    row_id = _parse_task_id(task_id)
    if row_id is None:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="任务号得是 T 开头的编号(比如 T3)。先「查一下任务」拿到号再改。",
        )
    if not (due or "").strip():
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="改到哪天得说一下,比如「改到周五」「改到9月1号」。",
        )
    due_day, error = _parse_due_or_fail(due)
    if error is not None or due_day is None:
        return error or fail(ErrorCode.INVALID_INPUT)

    row = await asyncio.to_thread(db.fetch, row_id)
    if row is None:
        return fail(
            ErrorCode.NOT_FOUND,
            user_msg=f"台账里没有 {_display_id(row_id)} 这条任务。先「查一下任务」核对号码。",
        )
    if row.status == db.STATUS_DONE:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg=(
                f"{_display_id(row_id)}「{row.title}」已经销项完成了,不用改期。"
                "要重新安排这件事,就新记一条任务。"
            ),
        )

    updated = await asyncio.to_thread(db.set_due, row_id, due_day.isoformat())
    if not updated:
        # fetch 与写入之间的窗口里状态变了(比如刚被销项):如实报,不许假装改成功。
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg=f"{_display_id(row_id)} 的状态刚刚变过(可能已被销项),先「查一下任务」再改。",
        )
    display = format_display(due_day)
    old_display = format_display(date.fromisoformat(row.due_date)) if row.due_date else None
    from_part = f"从 {old_display} " if old_display else ""
    return ok(
        data={
            "task_id": _display_id(row_id),
            "title": row.title,
            "old_due": row.due_date,
            "old_due_display": old_display,
            "new_due": due_day.isoformat(),
            "due_display": display,
        },
        user_msg=f"改好了:{row.title}({_display_id(row_id)}){from_part}改到 {display}。",
    )


@tool("finish_task", description=_FINISH_DESCRIPTION)
@tool_guard
async def finish_task(task_id: str) -> Envelope:
    """销项。幂等:已完成的再销一次不算错,如实说「本来就完成了」。"""
    row_id = _parse_task_id(task_id)
    if row_id is None:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="任务号得是 T 开头的编号(比如 T3)。先「查一下任务」拿到号再销。",
        )

    row = await asyncio.to_thread(db.fetch, row_id)
    if row is None:
        return fail(
            ErrorCode.NOT_FOUND,
            user_msg=f"台账里没有 {_display_id(row_id)} 这条任务。先「查一下任务」核对号码。",
        )

    was_done = row.status == db.STATUS_DONE
    if not was_done:
        await asyncio.to_thread(db.set_done, row_id)
        summary = f"销了:{row.title}({_display_id(row_id)})完成。"
    else:
        summary = f"{_display_id(row_id)}「{row.title}」本来就销过了,台账里没漏。"
    return ok(
        data={"task_id": _display_id(row_id), "title": row.title, "was_done": was_done},
        user_msg=summary,
    )


SCHEDULE_TOOLS: list = [add_task, list_tasks, reschedule_task, finish_task]

__all__ = [
    "SCHEDULE_TOOLS",
    "add_task",
    "finish_task",
    "list_tasks",
    "reschedule_task",
]
