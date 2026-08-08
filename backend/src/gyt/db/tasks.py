"""任务台账的 SQLite 存储层:同步 sqlite3,每个操作一条独立连接(定案 #6)。

职责边界(这层"薄"是刻意的):
- 只保证两件事:**存取保真**(参数化读写,内容原样进出)与**表级约束**
  (NOT NULL / CHECK)。业务校验 —— 空标题、日期格式、「已完成不许改期」——
  全部归工具层(agents/schedule/tools.py):那些规则失败时要说中文人话并进信封契约,
  搅进这层会把 SQL 与话术绑死,两头都难测。
- 截止日过滤 / 逾期标注同样不在这层:list_rows 一次取全(方案红线 4),
  「≤ 某天」的筛选留给工具层的纯 Python date 比较 —— 放这层就得往 SQL 里塞
  "今天",测试还得冻结库时钟,得不偿失。

并发姿势(定案 #6):上层工具用 asyncio.to_thread 把这里的同步函数摔进工作线程。
每个公开函数内部自己 connect → 幂等建表 → 参数化语句 → 提交 → 关闭,
模块级零连接、零可变状态 —— sqlite3 连接对象本就禁止跨线程共用,
不留共享连接就永远撞不上线程问题;建表语句幂等且廉价,每次都执行,
顺带免去「谁负责初始化」的时序纠纷。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from typing import Final, NamedTuple

from gyt.config import get_settings

STATUS_OPEN: Final[str] = "open"
STATUS_DONE: Final[str] = "done"

# 建表语句 —— 与《W3_Schedule_Agent_计划》§4 一字不差,幂等,每次操作都执行。
# project_id 只在这里出现:建表即预留、写入恒 NULL、不进任何函数签名(定案 #11)。
_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  due_date   TEXT,
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
  project_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class TaskRow(NamedTuple):
    """tasks 表的一行。字段顺序即 SELECT 列序(_COLUMNS 由 _fields 派生,改名不会错位)。"""

    id: int
    title: str
    due_date: str | None  # ISO YYYY-MM-DD;None = 无期限
    status: str  # STATUS_OPEN | STATUS_DONE
    project_id: str | None  # 方案预留,MVP 恒 None(定案 #11)
    created_at: str
    updated_at: str


# SELECT 列序从 TaskRow._fields 派生:行元组 → NamedTuple 的对位靠它锁死。
_COLUMNS: Final[str] = ", ".join(TaskRow._fields)

# 排序:有期限在前按期限升序(ISO 文本序 = 日期序),无期限垫底不丢(定案 #7),
# 同期限按 id 先来先排 —— 保证两次列表里「T3」指向同一条活。
_ORDER_BY: Final[str] = "ORDER BY (due_date IS NULL), due_date, id"

# 以下语句的值一律走 ? 占位(方案红线 4):f-string 只拼上面两个模块级常量片段,
# 任何运行期的值(标题、日期、id)都绝不进入 SQL 文本。
_INSERT_SQL: Final[str] = (
    "INSERT INTO tasks (title, due_date, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
)
_FETCH_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks WHERE id = ?"
_LIST_OPEN_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks WHERE status = ? {_ORDER_BY}"
_LIST_ALL_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks {_ORDER_BY}"
_SET_DUE_SQL: Final[str] = (
    "UPDATE tasks SET due_date = ?, updated_at = ? WHERE id = ? AND status = ?"
)
_SET_DONE_SQL: Final[str] = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?"


def _now_iso() -> str:
    """本地时区、秒级、带 UTC 偏移的 ISO 时间戳(与 report/tools.py 的时间写法同源)。"""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@contextmanager
def _task_db() -> Iterator[sqlite3.Connection]:
    """本次操作专用连接:进场幂等建表,离场提交并关闭(中途异常则回滚后关闭)。

    库路径每次现从 get_settings() 取、不在模块里缓存 —— 测试用 GYT_DATA_DIR +
    cache_clear() 换库时这层自动跟着走,不需要任何补丁点。
    """
    with closing(sqlite3.connect(get_settings().sqlite_path)) as conn, conn:
        conn.execute(_DDL)
        yield conn


def create(title: str, due_date: str | None) -> int:
    """记一条新任务,返回自增 id。

    不做业务校验(空标题、日期格式由工具层拦下并出人话提示);
    created_at 与 updated_at 取同一枚时间戳 —— 「生下来就没改过」要可断言。
    """
    now = _now_iso()
    with _task_db() as conn:
        new_id = conn.execute(_INSERT_SQL, (title, due_date, STATUS_OPEN, now, now)).lastrowid
    if new_id is None:  # pragma: no cover — sqlite 对 INSERT 必给 rowid,纯防御
        raise RuntimeError("SQLite 没有返回新任务的 rowid")
    return new_id


def fetch(task_id: int) -> TaskRow | None:
    """按 id 取一条;查无此行返回 None(「T 号不存在」的人话归工具层拼)。"""
    with _task_db() as conn:
        raw = conn.execute(_FETCH_SQL, (task_id,)).fetchone()
    return TaskRow(*raw) if raw is not None else None


def list_rows(*, include_done: bool = False) -> list[TaskRow]:
    """一次取全(方案红线 4,禁循环内逐条查):默认只看未完成,include_done=True 全量。

    截止日过滤 / 逾期判断刻意不在这层做,理由见模块 docstring 的职责边界。
    """
    query = _LIST_ALL_SQL if include_done else _LIST_OPEN_SQL
    params: tuple[str, ...] = () if include_done else (STATUS_OPEN,)
    with _task_db() as conn:
        raw_rows = conn.execute(query, params).fetchall()
    return [TaskRow(*raw) for raw in raw_rows]


def set_due(task_id: int, due_date: str) -> bool:
    """改期并刷新 updated_at;返回 False = 任务不存在**或已销项**。

    WHERE 里带 status='open' 是写前状态守卫:工具层「先 fetch 再改」的两步之间
    存在理论窗口(比如并发销项),不加守卫的话已完成的任务会被静默改期、
    绕过业务规则(审查 LOW 项)。调用方要靠**返回值**分辨成败,不许信先读的快照。
    """
    with _task_db() as conn:
        touched = conn.execute(_SET_DUE_SQL, (due_date, _now_iso(), task_id, STATUS_OPEN)).rowcount
    return touched > 0


def set_done(task_id: int) -> bool:
    """销项并刷新 updated_at;返回 False = 任务不存在。

    幂等(定案 #9):已 done 的行照样 UPDATE 并返回 True ——
    「这条本来就完成了」的判断与话术归工具层,它会先 fetch 再措辞。
    """
    with _task_db() as conn:
        touched = conn.execute(_SET_DONE_SQL, (STATUS_DONE, _now_iso(), task_id)).rowcount
    return touched > 0


__all__ = [
    "STATUS_DONE",
    "STATUS_OPEN",
    "TaskRow",
    "create",
    "fetch",
    "list_rows",
    "set_done",
    "set_due",
]
