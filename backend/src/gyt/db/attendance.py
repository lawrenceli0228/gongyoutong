"""考勤台账的 SQLite 存储层。**本文件是 attendance 表结构的唯一真相**(T1 冻结)。

职责边界照搬 ``db/tasks.py``:只保证**存取保真**(参数化读写)与**表级约束**;
业务校验(姓名长度、坐标范围、枚举合法性)归 ``checkin_api.py``,
那些规则失败时要说中文人话并进 Envelope,搅进这层会把 SQL 与话术绑死。

并发姿势也照搬:每个公开函数自己 connect → 幂等建表 → 参数化 → 提交 → 关闭,
模块级零连接、零可变状态。sqlite3 连接本就禁止跨线程共用,不留共享连接就撞不上线程问题。

**这层没有时钟。** 所有时间戳(checked_at / work_date / created_at / purged_at)
由调用方传入 —— 时间权威只有一个:``attendance/receipt.py``。这层若自己取 now,
「单一快照」的保证就在存储层被悄悄打破,而且测试只能靠冻结时钟。

---------------------------------------------------------------------------
为什么 CHECK 子句是**拼**出来的,不是手写的
---------------------------------------------------------------------------
``geo_status`` / ``source`` 的合法取值有三个地方要一致:

    Python 常量(本文件)   ←→   SQL 的 CHECK 子句   ←→   前端塞进 header 的值

前两者在这里由 ``_in_clause()`` 从**同一个元组**生成,所以结构上不可能漂。
第三者靠 ``checkin_api.py`` 在入库前校验 —— 那道校验必须存在,理由是:
**漂了的表现是整条 INSERT 抛 sqlite3.IntegrityError**,报错在数据库层,
而真正的原因是前端某个分支写了个拼错的字符串。工友看到的是「打卡失败」,
查的人打开的是 SQL。

---------------------------------------------------------------------------
``artifact_id`` 为什么可空 —— 别再改回 NOT NULL
---------------------------------------------------------------------------
留存策略要求「照片过期后删图、行留着」(W7 方案 §3.9),删完 ``artifact_id`` 置 NULL、
``photo_purged_at`` 记时间。写成 NOT NULL 就和清理器直接冲突,而冲突要到
清理器第一次真跑的时候才暴露 —— 那通常是上线几周之后。

界面上 ``artifact_id IS NULL`` 要渲染成「凭证图已过期清理」这句话,
**不能渲染 <img>** —— 裂图标和「图真的没了」在界面上长得一模一样。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from typing import Final, NamedTuple

from gyt.config import get_settings

# ---------------------------------------------------------------------------
# 受控词表 —— 与 CHECK 子句同源(见模块头注)
# ---------------------------------------------------------------------------

GEO_STATUSES: Final[tuple[str, ...]] = (
    "ok",  # 拿到坐标
    "denied",  # 用户拒绝了定位权限
    "timeout",  # 超时(3 秒)
    "unsupported",  # 设备/浏览器没有 geolocation
    "error",  # getCurrentPosition 回了别的错
    "absent",  # 前端压根没发这个字段
)
"""定位结果的六种状态。

**为什么不是「有坐标 / 没坐标」两态:** 光靠 ``lat IS NULL`` 分不清
「用户拒绝 / 超时 / 设备不支持 / 前端字段漂了 / 程序出错 / 前端没发」——
审计的时候这六种意义完全不同:前两种是用户行为,后四种是我们自己的问题。
两态的话,线上出现一批没坐标的记录,没有任何办法判断该去查前端还是去问工友。
"""

SOURCES: Final[tuple[str, ...]] = (
    "camera",  # getUserMedia 现场取景截帧
    "fallback",  # <input capture> 降级路径,可能是相册里的旧图 → 弱凭证
)
"""取像来源。``fallback`` 意味着**这张图不保证是当场拍的**(可能从相册选)。

前端必须如实上报,不许为了「好看」一律写 camera —— 那等于把弱凭证伪装成强凭证。
"""


def _in_clause(values: tuple[str, ...]) -> str:
    """把受控词表渲染成 SQL 的 IN (...) 片段。

    值全是本模块里写死的 ASCII 字面量,**没有任何运行期输入进来**,
    所以这里用 f-string 拼 SQL 不违反「全参数化」那条红线 ——
    那条红线管的是「用户给的值不许进 SQL 文本」。
    """
    return ", ".join(f"'{v}'" for v in values)


# ---------------------------------------------------------------------------
# 建表 —— 幂等,每次操作都执行(与 tasks.py 同一姿势)
# ---------------------------------------------------------------------------

_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS attendance (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id    TEXT    NOT NULL UNIQUE,
  req_digest  TEXT    NOT NULL,
  worker_name TEXT    NOT NULL,
  site_name   TEXT,
  checked_at  TEXT    NOT NULL,
  work_date   TEXT    NOT NULL,
  lat         REAL,
  lon         REAL,
  accuracy_m  REAL,
  geo_status  TEXT    NOT NULL CHECK (geo_status IN ({_in_clause(GEO_STATUSES)})),
  source      TEXT    NOT NULL CHECK (source IN ({_in_clause(SOURCES)})),
  receipt_no  TEXT    NOT NULL UNIQUE,
  artifact_id TEXT,
  photo_purged_at TEXT,
  project_id  TEXT,
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_date   ON attendance(work_date);
CREATE INDEX IF NOT EXISTS idx_att_worker ON attendance(worker_name, work_date);
"""
"""建表语句。**改这里就是改 W7 方案 §3.1,两边要一起改。**

``project_id`` 照搬 tasks 表的处理:建表即预留、写入恒 NULL、不进任何函数签名。

``work_date`` 落库而不是用表达式索引,是因为写侧算一次最省 ——
**不是因为 SQLite 做不到**(它支持表达式索引和生成列)。写明白是为了防止
下一个人以为这是被迫的而不敢动。

两个索引的用途:``idx_att_date`` 服务「今天谁到了」,
``idx_att_worker`` 服务「张三这个月来了几天」。
``event_id`` / ``receipt_no`` 的 UNIQUE 各自带一个隐式索引,不用再建。
"""


class AttendanceRow(NamedTuple):
    """attendance 表的一行。字段顺序即 SELECT 列序(``_COLUMNS`` 由 ``_fields`` 派生)。

    ⚠️ **字段顺序必须与 ``_DDL`` 的列顺序一致。** 顺序错位不会有任何报错,
    只会让 ``lat`` 里装着 ``lon`` —— 而两个都是 REAL,类型检查抓不到,
    表现是地图上的点跑到地球另一边(本方案不上地图,所以连这个提示都没有)。
    ``test_db_attendance.py`` 有一条测试直接比对这两份顺序。
    """

    id: int
    event_id: str  # 客户端幂等键,落 sessionStorage
    req_digest: str  # **服务端自己算的**指纹;绝不是 header 里那个
    worker_name: str  # 原样存,⚠️ 绝不做简繁转换
    site_name: str | None
    checked_at: str  # ISO,含 +08:00 偏移;来自 Asia/Hong_Kong 的单一快照
    work_date: str  # YYYY-MM-DD,与 checked_at 同一快照派生
    lat: float | None
    lon: float | None
    accuracy_m: float | None
    geo_status: str  # GEO_STATUSES 之一
    source: str  # SOURCES 之一
    receipt_no: str
    artifact_id: str | None  # ⚠️ 可空,清理后置 NULL(见模块头注)
    photo_purged_at: str | None
    project_id: str | None  # 预留,恒 None
    created_at: str


class CheckinDraft(NamedTuple):
    """待写入的一条打卡(``id`` 由库分配、``project_id`` 恒 NULL,都不在这里)。

    与 ``AttendanceRow`` 分开是刻意的:写入方不该被迫编造 ``id``,
    也不该有机会给 ``project_id`` 塞值。
    """

    event_id: str
    req_digest: str
    worker_name: str
    site_name: str | None
    checked_at: str
    work_date: str
    lat: float | None
    lon: float | None
    accuracy_m: float | None
    geo_status: str
    source: str
    receipt_no: str
    artifact_id: str | None
    created_at: str


class WorkerDays(NamedTuple):
    """「张三这个月来了几天」的一行:按人聚合的出勤天数与打卡次数。"""

    worker_name: str
    days: int  # COUNT(DISTINCT work_date)
    checkins: int  # 总打卡次数


class WorkerDayDetail(NamedTuple):
    """「今天谁到了」的一行:每人**首次、末次、次数**(W7 §3.10 定死的口径)。

    不返回全部流水:D10 允许无限打卡,一天几百条流水既撑爆上下文也没人看;
    「他今天打了 6 次」这句话由 ``checkins`` 支撑,要细节让人再问。
    """

    worker_name: str
    first_at: str  # 当天最早一条的 checked_at
    last_at: str  # 当天最晚一条的 checked_at
    checkins: int


class PurgeCandidate(NamedTuple):
    """待清理的一行:行号 + 它当前挂着的图。"""

    row_id: int
    artifact_id: str


class DuplicateEventError(Exception):
    """``event_id`` 撞唯一约束 —— 幂等第三层的信号,调用方回查那条并返回它。"""


class DuplicateReceiptError(Exception):
    """``receipt_no`` 撞唯一约束 —— 同秒碰撞,调用方换个随机尾重试。"""


_COLUMNS: Final[str] = ", ".join(AttendanceRow._fields)
"""SELECT 列序从 ``_fields`` 派生:行元组 → NamedTuple 的对位靠它锁死。"""

_INSERT_COLUMNS: Final[str] = ", ".join(CheckinDraft._fields)
_INSERT_MARKS: Final[str] = ", ".join("?" for _ in CheckinDraft._fields)
_INSERT_SQL: Final[str] = f"INSERT INTO attendance ({_INSERT_COLUMNS}) VALUES ({_INSERT_MARKS})"

_FETCH_BY_ID_SQL: Final[str] = f"SELECT {_COLUMNS} FROM attendance WHERE id = ?"
_FETCH_BY_EVENT_SQL: Final[str] = f"SELECT {_COLUMNS} FROM attendance WHERE event_id = ?"

_RECENT_SQL: Final[str] = f"SELECT {_COLUMNS} FROM attendance ORDER BY id DESC LIMIT ?"
"""「最近的凭证」按插入顺序倒排。

**不按 ``checked_at`` 排**:那是服务器时钟的快照,同一秒内多条的相对顺序不确定,
而 ``id`` 是自增主键,顺序即写入顺序,且走主键索引不需要额外排序。
"""

# 查询侧的两条聚合(W7 §3.10:聚合在 SQL 里,且必须有上限)。
# f-string 只拼本文件的固定片段,运行期的值(日期、姓名、上限)全走 ? 占位。
_DAYS_SQL_ALL: Final[str] = (
    "SELECT worker_name, COUNT(DISTINCT work_date) AS days, COUNT(*) AS checkins "
    "FROM attendance WHERE work_date >= ? AND work_date <= ? "
    "GROUP BY worker_name ORDER BY days DESC, worker_name LIMIT ?"
)
_DAYS_SQL_ONE: Final[str] = (
    "SELECT worker_name, COUNT(DISTINCT work_date) AS days, COUNT(*) AS checkins "
    "FROM attendance WHERE work_date >= ? AND work_date <= ? AND worker_name = ? "
    "GROUP BY worker_name LIMIT ?"
)
_DETAIL_SQL_ALL: Final[str] = (
    "SELECT worker_name, MIN(checked_at), MAX(checked_at), COUNT(*) "
    "FROM attendance WHERE work_date = ? "
    "GROUP BY worker_name ORDER BY MIN(checked_at), worker_name LIMIT ?"
)
_DETAIL_SQL_ONE: Final[str] = (
    "SELECT worker_name, MIN(checked_at), MAX(checked_at), COUNT(*) "
    "FROM attendance WHERE work_date = ? AND worker_name = ? "
    "GROUP BY worker_name LIMIT ?"
)
# MIN/MAX 直接比 checked_at 的 ISO 文本:所有行都由 receipt.py 以同一格式、
# 同一 +08:00 偏移写入,等长同构的 ISO 串文本序 = 时间序。混入别的格式才会错,
# 而写入口只有一个。

_ACTIVE_ARTIFACTS_SQL: Final[str] = (
    "SELECT artifact_id FROM attendance WHERE artifact_id IS NOT NULL"
)
_EXPIRED_SQL: Final[str] = (
    "SELECT id, artifact_id FROM attendance WHERE artifact_id IS NOT NULL AND work_date < ?"
)


@contextmanager
def _att_db() -> Iterator[sqlite3.Connection]:
    """本次操作专用连接:进场幂等建表,离场提交并关闭(中途异常则回滚后关闭)。

    库路径每次现从 get_settings() 取、不在模块里缓存 —— 测试用 GYT_DATA_DIR +
    cache_clear() 换库时这层自动跟着走,不需要任何补丁点。

    建表用 ``executescript`` 而不是 ``execute``:``_DDL`` 含三条语句(表 + 两索引)。
    executescript 会先隐式提交 —— 它是进场第一件事,前面没有未提交的东西,安全。
    """
    with closing(sqlite3.connect(get_settings().sqlite_path)) as conn, conn:
        conn.executescript(_DDL)
        yield conn


def _fetch_by_rowid(conn: sqlite3.Connection, row_id: int) -> AttendanceRow:
    raw = conn.execute(_FETCH_BY_ID_SQL, (row_id,)).fetchone()
    if raw is None:  # pragma: no cover — 同一事务里刚插入的行必在,纯防御
        raise RuntimeError("刚写入的考勤行取不回来")
    return AttendanceRow(*raw)


def insert_checkin(draft: CheckinDraft) -> AttendanceRow:
    """写一条打卡,返回完整的行(含库分配的 id)。

    两种唯一约束冲突翻译成**不同的**类型化异常 —— 调用方的正确动作完全相反:
    ``DuplicateEventError`` → 回查同 event_id 那条返回它(幂等层③);
    ``DuplicateReceiptError`` → 换个随机尾重试(同秒碰撞)。
    靠字符串匹配区分是 sqlite3 的常规做法(它不给结构化的冲突信息),
    匹配的是我们自己 DDL 里的列名,格式由 sqlite 保证稳定。
    其它 IntegrityError(如 CHECK 失败)原样上抛 —— 那是上游校验漏了,该炸在测试里。
    """
    try:
        with _att_db() as conn:
            row_id = conn.execute(_INSERT_SQL, tuple(draft)).lastrowid
            if row_id is None:  # pragma: no cover — sqlite 对 INSERT 必给 rowid,纯防御
                raise RuntimeError("SQLite 没有返回新考勤行的 rowid")
            return _fetch_by_rowid(conn, row_id)
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "attendance.event_id" in message:
            raise DuplicateEventError(draft.event_id) from exc
        if "attendance.receipt_no" in message:
            raise DuplicateReceiptError(draft.receipt_no) from exc
        raise


def find_by_event_id(event_id: str) -> AttendanceRow | None:
    """按幂等键取一条;查无返回 None。幂等层①与层③都靠它。"""
    with _att_db() as conn:
        raw = conn.execute(_FETCH_BY_EVENT_SQL, (event_id,)).fetchone()
    return AttendanceRow(*raw) if raw is not None else None


def list_recent(limit: int) -> list[AttendanceRow]:
    """最近 N 条凭证,插入序倒排。上限的 clamp 归 handler(它读 settings)。"""
    with _att_db() as conn:
        raw_rows = conn.execute(_RECENT_SQL, (max(1, limit),)).fetchall()
    return [AttendanceRow(*raw) for raw in raw_rows]


def count_worker_days(
    *,
    date_from: str,
    date_to: str,
    worker_name: str | None = None,
    max_workers: int,
) -> list[WorkerDays]:
    """按人聚合的出勤天数(「张三这个月来了几天」)。一次取回,不逐人查。"""
    with _att_db() as conn:
        if worker_name is None:
            raw = conn.execute(_DAYS_SQL_ALL, (date_from, date_to, max_workers)).fetchall()
        else:
            raw = conn.execute(
                _DAYS_SQL_ONE, (date_from, date_to, worker_name, max_workers)
            ).fetchall()
    return [WorkerDays(*r) for r in raw]


def list_day_detail(
    work_date: str,
    *,
    worker_name: str | None = None,
    max_workers: int,
) -> list[WorkerDayDetail]:
    """某天的按人明细:首次、末次、次数(口径见 ``WorkerDayDetail``)。"""
    with _att_db() as conn:
        if worker_name is None:
            raw = conn.execute(_DETAIL_SQL_ALL, (work_date, max_workers)).fetchall()
        else:
            raw = conn.execute(_DETAIL_SQL_ONE, (work_date, worker_name, max_workers)).fetchall()
    return [WorkerDayDetail(*r) for r in raw]


def list_active_artifact_ids() -> set[str]:
    """全部仍被引用的凭证图 id,一次取回建集合。

    清理器找孤儿靠的就是它 —— **不许**在目录遍历里逐个回库查
    (CLAUDE.md 红线:列表查询一次取回)。一行是 32 字符的 id,
    十万行也就几 MB,这个量级下内存不是问题。
    """
    with _att_db() as conn:
        raw = conn.execute(_ACTIVE_ARTIFACTS_SQL).fetchall()
    return {r[0] for r in raw}


def list_expired_photos(cutoff_work_date: str) -> list[PurgeCandidate]:
    """留存到期、图还挂着的行(``work_date`` 早于 cutoff,不含当天)。"""
    with _att_db() as conn:
        raw = conn.execute(_EXPIRED_SQL, (cutoff_work_date,)).fetchall()
    return [PurgeCandidate(*r) for r in raw]


def mark_photos_purged(row_ids: Sequence[int], purged_at: str) -> int:
    """把这些行的图标记为已清理:``artifact_id`` 置 NULL、记录清理时间。

    返回实际改动的行数。``AND artifact_id IS NOT NULL`` 让重复调用天然幂等,
    也不会覆盖第一次的清理时间。占位符按行数生成 —— id 虽然来自我们自己的
    查询,照样参数化,不给「拼 SQL 也没事」开任何一个先例。
    """
    if not row_ids:
        return 0
    marks = ", ".join("?" for _ in row_ids)
    sql = (
        "UPDATE attendance SET artifact_id = NULL, photo_purged_at = ? "
        f"WHERE id IN ({marks}) AND artifact_id IS NOT NULL"
    )
    with _att_db() as conn:
        touched = conn.execute(sql, (purged_at, *row_ids)).rowcount
    return touched


__all__ = [
    "GEO_STATUSES",
    "SOURCES",
    "AttendanceRow",
    "CheckinDraft",
    "DuplicateEventError",
    "DuplicateReceiptError",
    "PurgeCandidate",
    "WorkerDayDetail",
    "WorkerDays",
    "count_worker_days",
    "find_by_event_id",
    "insert_checkin",
    "list_active_artifact_ids",
    "list_day_detail",
    "list_expired_photos",
    "list_recent",
    "mark_photos_purged",
]
