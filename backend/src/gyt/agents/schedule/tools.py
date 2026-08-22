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
W9 / S7:带 hazard_no 的任务,这里销不了、也改不了期
---------------------------------------------------------------------------
台账里有一类活是**隐患整改**(tasks.hazard_no 非空,由 supervision 在签发
《监理通知单》时同步创建)。对这类活,finish_task / reschedule_task 一律拒绝
并把人引到 supervision 那条路 —— W9 方案 §5.3 的 D13「销项只有一个出口」。

为什么是**拒绝**,不是「顺手把隐患那边也同步改掉」(这一步想岔了后果很重):

 1. 销项那一侧根本不该由这里说了算。隐患要转 closed,前提是**挂上复查照片**、
    并且**由人点确认**(方案 D11/D14)。schedule 这层手里既没有照片也没有人的
    确认动作 —— 自动同步只能造出一个「没有证据的合格」,而它会被写进留档文书:
    「隐患已消除」白纸黑字,现场原样没动。这正是方案用红字禁掉的那一条。
 2. 改期那一侧同理。隐患的 due_date 是签发通知单时定下的,要进超期升级判定;
    从聊天里一句「改到周五」单方面改掉它,等于用一句话改了一份已签发文书的整改期限。
 3. 两边各改各的会让期限**分叉**,而超期判定读的是隐患那一份。分叉的下场:
    schedule 这边看着已经销了/已经宽限了,hazards 那边照样超期,系统于是建议
    签发《监理报告》指控施工方拒不整改 —— 一份建立在我们自己记乱账上的对外指控。
 4. 用户走的路没错(TODO-21 认可的「巡检 → 一句话记整改任务」照旧保留),
    错的是**有两个销项实现**。所以堵掉这一个、把人领到对的那个,
    而不是让两个出口互相同步 —— 同步只是把分叉从数据挪到了代码里。

普通任务(hazard_no 为空)的行为**一个字都没变**,这是回归面。
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


def _reject_hazard_task(row: db.TaskRow, *, guidance: str) -> Envelope:
    """带 hazard_no 的任务在这层一律拒绝(W9 D13),理由见文件头「销项只有一个出口」。

    错误码挑 INVALID_INPUT 而**不新增一个**:新增错误码要动 core/errors.py 那份
    跨泳道共享契约(前端、日志、attendance 直连接口都读它),为一条业务规则去动它不划算。
    现有码里 INVALID_INPUT 最贴 —— 本文件已有同款先例:「已完成的任务不许改期」
    也是业务规则拒绝、也用的它。CONFLICT 看着像但不能用:那是 W7 打卡「同幂等键内容
    对不上」的 409 语义,套过来会让前端和日志误判成重复提交。

    ``hazard_no`` 必须进 user_msg:师傅要拿着这个号去跟监理对,报不出号就对不上账。
    prompt.md 红线 1 要求模型对 ok=false 原样转述 user_msg,所以这句就是他最终听到的话。
    """
    return fail(
        ErrorCode.INVALID_INPUT,
        user_msg=(
            f"{_display_id(row.id)}「{row.title}」是隐患整改的活,{guidance}"
            f"这条对应的隐患号是 {row.hazard_no},跟监理说的时候报这个号。"
        ),
    )


def _task_item(row: db.TaskRow, *, today_iso: str) -> dict[str, Any]:
    """一条任务的对外形态。due_display/overdue 都在这儿算好,模型照抄。

    ⚠️ 刻意**不**把 hazard_no 放进来:prompt.md 的清单是固定四列,多一个字段模型就会
    想办法把它念出来,而隐患号一旦进了模型的嘴,下一步就是凭记忆复述(report 守卫
    抓到过同款)。查清单不需要它,真要看隐患用 supervision 的列表 —— 那边的编号
    是从工具结果直读的。顺带:这样 list_tasks 对普通任务的返回形状一个键都没变。
    """
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
    # 隐患整改的活拦在最前面,**先于**已销项判断:期限的权威在隐患台账那边,
    # 这条任务眼下是什么状态都不影响结论 —— 这里不是改它期限的地方。
    # 摆在前面还顺带保证「不管走哪个分支都拦得住」,不会留下某种状态下反而放行的窗口。
    if row.hazard_no:
        # 🔴 2026-08-22:这句话原来的后半截是「得让监理去改这条隐患的整改期限」——
        #    话本身没错(期限的权威确实在隐患台账那边),但**监理那边当时没有这个动作**。
        #    全仓改隐患期限的路径一条都不存在,于是工友照着做、监理找不到按钮,
        #    两边都以为是自己没找到。这是「用户被指向一条不存在的路」的典型一例。
        #    现在那条路是真的:POST /supervision/extend + 监理面板上那颗
        #    「改整改期限(不出文書,要寫原因)」。措辞跟着改成**指得到的那个位置**。
        #    ⚠️ 界面上那颗按钮的字是繁體(界面恒繁體,W12);这句话是对话链的人话,
        #       按本仓「答话跟着用户打的字走」的规矩留简体,前端会按需转 ——
        #       所以这里写「隐患处置」而不是照抄按钮上那几个繁體字。
        return _reject_hazard_task(
            row,
            guidance="期限在这儿改不了。要宽限几天,请监理在「隐患」那个面板上打开这条隐患,"
            "用「改整改期限」改 —— 期限的权威在隐患台账那边,"
            "从那儿改,台账和隐患两边才对得上。",
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
    # 隐患整改的活拦在最前面,**先于**幂等分支:这类活的销项要挂复查照片、要人确认,
    # 出口只有 supervision 那条(W9 D13)。摆在幂等分支前面是为了让拒绝**无条件** ——
    # 万一库里有一条已经被销掉的带号任务(旧数据,或绕过本层写进去的),
    # 再销一次也照样拒;否则就会出现「第二次调用反而成功」这种最难查的窗口。
    if row.hazard_no:
        return _reject_hazard_task(
            row,
            guidance="在这儿销不了。整改完拍一张改好的照片传上来,监理看过点了确认才算销。",
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
