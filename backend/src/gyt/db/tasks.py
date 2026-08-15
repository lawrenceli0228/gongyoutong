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
每个公开函数内部自己 connect → 幂等建表 → **幂等迁移** → 参数化语句 → 提交 → 关闭,
模块级零连接、零可变状态 —— sqlite3 连接对象本就禁止跨线程共用,
不留共享连接就永远撞不上线程问题;建表语句幂等且廉价,每次都执行,
顺带免去「谁负责初始化」的时序纠纷。

---------------------------------------------------------------------------
W9 / S7:hazard_no 这一列,以及为什么必须写迁移
---------------------------------------------------------------------------
``hazard_no`` 可空,非空表示「这条任务是某条隐患的整改活」,由 supervision
在签发《监理通知单》时同步创建,``due_date`` 与隐患一致(W9 方案 §5.3 的 D13)。
带号的任务在 ``agents/schedule/tools.py`` 里**销不了、也改不了期** —— 出口只有
supervision 那条(要复查照片、要人确认)。两个出口会让 tasks 与 hazards 的
状态和期限分叉,而超期升级判定读的是 hazards 那一份:最后系统会拿着我们自己
记乱的账,建议签发《监理报告》指控施工方拒不整改。

🔴 **``CREATE TABLE IF NOT EXISTS`` 不会给已经存在的旧表加列**(W9 §6.6)。
线上 ``data/gyt.sqlite3`` 里那张 tasks 表是 W3 建的,没有这一列;只改 DDL 的话,
新机器一切正常、线上却是「代码认得这列、库里没有」,第一条 SELECT 就
``no such column: hazard_no`` —— 而 ``make test`` 全绿(测试库每次都是新建的)。
所以 ``_migrate()`` 与 ``_DDL`` 一起放在连接的进场处,每次执行,幂等。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from typing import Final, NamedTuple

from gyt.config import get_settings

STATUS_OPEN: Final[str] = "open"
STATUS_DONE: Final[str] = "done"

# 建表语句 —— W3 原表末尾追加 hazard_no(W9 §6.6),其余一字不差,幂等,每次操作都执行。
# project_id 只在这里出现:建表即预留、写入恒 NULL、不进任何函数签名(定案 #11)。
#
# 🔴 hazard_no 写在**最末**是有意的:``ALTER TABLE ADD COLUMN`` 只能往末尾追加,
#    DDL 也写在末尾,新建的库与迁移过来的旧库物理列序才完全一致。写在中间的话
#    两种库的列序会不一样 —— 本层所有 SELECT 都显式列名(见 _COLUMNS),不会因此出错,
#    但任何一句手写的 ``SELECT *`` 或 ``INSERT`` 不带列名都会在**只有一种库上**出事,
#    而那种 bug 在本机永远复现不出来。
_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  due_date   TEXT,
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
  project_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  hazard_no  TEXT
);
"""

# 幂等迁移表:(列名, 补这一列的语句)。进场处每次比对、缺了才补。
#
# 为什么是「查 PRAGMA 再补」而不是「试着 ALTER,撞了就吞异常」:吞异常会把
# **别的** OperationalError(库文件损坏、只读挂载)一起吞掉,变成静默失败;
# 而这层一旦静默失败,上面看到的是「查询报错说没这列」,病灶隔了一层。
#
# 加新列时往这张表追加一行即可,顺序无所谓(每条各查各的列名)。
# ⚠️ 只用来**加可空列**。改类型、加 NOT NULL、加约束都不能这么干 ——
# 那要建新表 + 搬数据,届时另写,别硬塞进这张表。
_MIGRATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("hazard_no", "ALTER TABLE tasks ADD COLUMN hazard_no TEXT"),
)


class TaskRow(NamedTuple):
    """tasks 表的一行。字段顺序即 SELECT 列序(_COLUMNS 由 _fields 派生,改名不会错位)。

    ⚠️ **加字段一律加在末尾。** 这里的顺序只决定 SELECT 的列序(以及 ``TaskRow(*raw)``
    的对位),与库里的物理列序无关 —— 两者靠 _COLUMNS 自动对齐,插在中间也不会错位。
    但仍然要加在末尾,有两条理由:
      1. 与 _DDL 和 ``ALTER TABLE`` 追加出来的物理列序保持一致(见 _DDL 的注释);
      2. 按位置解包 / 比对前 N 个字段的调用点(测试里有)不会被无声推移。
    """

    id: int
    title: str
    due_date: str | None  # ISO YYYY-MM-DD;None = 无期限
    status: str  # STATUS_OPEN | STATUS_DONE
    project_id: str | None  # 方案预留,MVP 恒 None(定案 #11)
    created_at: str
    updated_at: str
    hazard_no: str | None  # 非空 = 这条是某条隐患的整改活(W9 D13),销项/改期归 supervision


# SELECT 列序从 TaskRow._fields 派生:行元组 → NamedTuple 的对位靠它锁死。
_COLUMNS: Final[str] = ", ".join(TaskRow._fields)

# 排序:有期限在前按期限升序(ISO 文本序 = 日期序),无期限垫底不丢(定案 #7),
# 同期限按 id 先来先排 —— 保证两次列表里「T3」指向同一条活。
_ORDER_BY: Final[str] = "ORDER BY (due_date IS NULL), due_date, id"

# 以下语句的值一律走 ? 占位(方案红线 4):f-string 只拼上面两个模块级常量片段,
# 任何运行期的值(标题、日期、id)都绝不进入 SQL 文本。
_INSERT_SQL: Final[str] = (
    "INSERT INTO tasks (title, due_date, status, created_at, updated_at, hazard_no) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_FETCH_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks WHERE id = ?"
_LIST_OPEN_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks WHERE status = ? {_ORDER_BY}"
_LIST_ALL_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks {_ORDER_BY}"
_LIST_BY_HAZARD_BASE_SQL: Final[str] = f"SELECT {_COLUMNS} FROM tasks WHERE hazard_no IN"
_SET_DUE_SQL: Final[str] = (
    "UPDATE tasks SET due_date = ?, updated_at = ? WHERE id = ? AND status = ?"
)
_SET_DONE_SQL: Final[str] = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?"


def _now_iso() -> str:
    """本地时区、秒级、带 UTC 偏移的 ISO 时间戳(与 report/tools.py 的时间写法同源)。

    ⚠️ 带偏移量**不是 bug**,别顺手"修"成 HK 固定时区:ISO 串自带 ``+08:00`` 这类偏移,
    时刻本身无歧义,换台机器读也不会读错。真正受宿主时区影响的是**不带偏移的日期**
    (``due_date`` 那种 YYYY-MM-DD),而那一列的值是上层算好传进来的,不在这层生成。
    """
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等补列:PRAGMA 问一遍现有列名,_MIGRATIONS 里缺哪列补哪列。

    与 ``_DDL`` 一起在连接进场处每次执行(W9 §6.6)。代价是每次操作多一次
    ``PRAGMA table_info`` —— 读的是已经在内存里的 schema,与 ``CREATE TABLE IF NOT EXISTS``
    同一量级,换来的是「谁负责升级」这件事根本不用有人负责。

    对**新建**的库这里恒为空转(_DDL 已经把列建全);只有线上那种 W3 时代的旧表
    才会真的走一次 ALTER,之后每次都空转。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for column, statement in _MIGRATIONS:
        if column not in existing:
            conn.execute(statement)


@contextmanager
def _task_db() -> Iterator[sqlite3.Connection]:
    """本次操作专用连接:进场幂等建表 + 幂等补列,离场提交并关闭(中途异常则回滚后关闭)。

    库路径每次现从 get_settings() 取、不在模块里缓存 —— 测试用 GYT_DATA_DIR +
    cache_clear() 换库时这层自动跟着走,不需要任何补丁点。
    """
    with closing(sqlite3.connect(get_settings().sqlite_path)) as conn, conn:
        conn.execute(_DDL)
        _migrate(conn)
        yield conn


def create(title: str, due_date: str | None, *, hazard_no: str | None = None) -> int:
    """记一条新任务,返回自增 id。

    不做业务校验(空标题、日期格式由工具层拦下并出人话提示);
    created_at 与 updated_at 取同一枚时间戳 —— 「生下来就没改过」要可断言。

    ``hazard_no`` 做成**关键字、可选、默认 None**:现有调用点(add_task 那条路)
    一个字都不用改,记出来的还是普通任务。带号的整改任务只由 supervision 在
    签发通知单时创建(W9 §5.3),``add_task`` 刻意不把这个参数暴露给模型 ——
    让模型自己填隐患号,等于给它开了一条「凭记忆编号」的路,而那正是
    report 守卫抓到过的失效形态。
    """
    now = _now_iso()
    with _task_db() as conn:
        new_id = conn.execute(
            _INSERT_SQL, (title, due_date, STATUS_OPEN, now, now, hazard_no)
        ).lastrowid
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


def list_by_hazard(hazard_nos: Sequence[str]) -> list[TaskRow]:
    """一次取回若干条隐患各自的整改任务(W9 §5.3,supervision 侧的 S4/S5 会用)。

    **禁止在循环里逐条查**(CLAUDE.md 红线):监理那边一屏就是一堆隐患,
    「每条隐患后面挂着哪条活」逐个查就是 N+1。写法与 ``db/hazards.py`` 的
    ``docs_of`` 同源 —— 编号个数由代码算成 ``?`` 占位,值全走参数化。

    空入参直接返回 ``[]`` —— ``IN ()`` 是语法错误,别让它去打库。
    一次别塞太多编号(SQLITE_MAX_VARIABLE_NUMBER,分批由调用方切)。

    **不带 include_done 开关是有意的**:这是按键取行,给什么号就还什么行,
    全状态一次取回;要不要滤掉已销的归调用方在内存里判 —— 和「截止日过滤不进这层」
    同一条理由(见模块 docstring 的职责边界)。带号的任务被 supervision 关掉之后
    仍然是证据链的一环,默认把它藏掉才是真会出事的那种默认值。
    """
    if not hazard_nos:
        return []
    placeholders = ", ".join("?" * len(hazard_nos))
    query = f"{_LIST_BY_HAZARD_BASE_SQL} ({placeholders}) {_ORDER_BY}"
    with _task_db() as conn:
        raw_rows = conn.execute(query, tuple(hazard_nos)).fetchall()
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
    "list_by_hazard",
    "list_rows",
    "set_done",
    "set_due",
]
