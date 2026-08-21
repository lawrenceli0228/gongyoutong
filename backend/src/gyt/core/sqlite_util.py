"""SQLite 存储层的公共样板 —— 连接、事务、外键、时间戳,四个 ``db/*.py`` 共用。

(W9 S8 泳道;方案 §6.1 的 D9,D18 把它从主线拆出来并行做,做完四处一起换。)

起因:``db/tasks.py`` / ``db/attendance.py`` / ``db/projects.py`` / ``db/hazards.py``
四处的连接 contextmanager **docstring 逐字相同**,``_now_iso()`` 在 tasks 与 projects
也逐字相同 —— 第四份拷贝(hazards)落地那天就已经说好要抽。

**这一层不知道有哪些表。** DDL 文本、建表顺序、幂等迁移(``db/tasks.py`` 的
``_migrate()``)全部留在各自模块里;公共件只负责「什么时候跑它」—— 进场、在事务里、
每次都跑。各模块仍然保留自己的 ``_xxx_db()``:那个名字是本域「一次操作一条连接」的
入口,测试也拿它当上帝视角的口子。

⚠️ 依赖方向:本模块只 import ``gyt.config`` 与标准库(``core/__init__.py`` 的规矩),
``db/*`` import 它是向下的一条边,不构成环。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from gyt.config import get_settings

_log = logging.getLogger(__name__)


@contextmanager
def open_db(ddl: str, *, foreign_keys: bool = False) -> Iterator[sqlite3.Connection]:
    """本次操作专用连接:(按需)开外键 → 进场幂等建表 → 离场提交并关闭(中途异常回滚后关闭)。

    库路径每次现从 ``get_settings()`` 取、不在模块里缓存 —— 测试用 ``GYT_DATA_DIR`` +
    ``get_settings.cache_clear()`` 换库时这层自动跟着走,不需要任何补丁点。

    ``ddl`` 一律走 ``executescript``:含多条语句的域(attendance 两索引、hazards 三表两索引、
    projects 两张表)直接受益,单条语句的域(tasks)跑起来完全等价。``executescript`` 会
    先隐式提交 —— 它是进场第一件事,前面没有未提交的东西,安全。**幂等建表每次都执行**
    是四个域原本就一致的姿势,顺带免去「谁负责初始化」的时序纠纷。

    ===========================================================================
    🔴 ``foreign_keys`` 为什么是**参数**,而不是让调用方自己执行一句 PRAGMA
    ---------------------------------------------------------------------------
    ``PRAGMA foreign_keys`` **在事务内是 no-op,而且一声不吭**:实测(py3.11 /
    sqlite 3.53)在真的开着的事务里设它,回读就是 ``0``,外键从此静默不校验 ——
    文书能挂在一个根本不存在的隐患号上、图能挂在不存在的项目上,而这类错要到
    上报主管部门(或「这个项目有哪些图」掺进幽灵行)那天才发现。

    而本函数 ``yield`` 出去的连接**已经在 ``with conn:`` 里面**:调用方拿到它之后再执行
    PRAGMA,位置必然是错的。所以「开不开外键」只能做成进场时的一个参数,由这里在
    ``with conn:`` **之前**执行 —— 调用方结构上没有机会放错地方。

    ⚠️ **这条位置约束测不出来。** Python 的 sqlite3 只在 DML 时才隐式 BEGIN,
    ``with conn:`` 本身不开事务;所以把下面那行挪进 ``with conn:`` 里,外键照样生效、
    测试照样全绿(``tests/unit/test_sqlite_util.py`` 有两条用例把这个事实钉死:
    一条证明"挪进去也看不出来",一条证明"真事务里它确实是 no-op")。
    这条只能靠注释和 review 守 —— **别挪。**

    默认 ``False`` 是照搬现状:tasks / attendance 两个域没有外键、原本也不开这一项,
    重构不改行为。要给它们打开是另一件事,单独评估、单独测。

    ===========================================================================
    并发:``timeout`` 与 WAL 是一套,别只上一样
    ---------------------------------------------------------------------------
    2026-08-21 之前这里是裸的 ``sqlite3.connect(path)``:没传 timeout(python 默认
    **5 秒**)、没开 WAL、全仓零处 ``OperationalError`` 处理。而 HTTP 层有 22 处
    ``run_in_threadpool`` 打同一个库文件,starlette 默认线程池 40 并发 ——
    单人演示永远不出事,**两个人同时点界面就是 ``database is locked``**,
    抛出来还是英文异常。

    - ``timeout`` 兜的是**写与写**相撞(值从 config 取,别在这儿写死数字);
    - WAL 让**读不再阻塞写**,这才是界面那种「读多写少」场景的大头。

    ⚠️ WAL 是**库级持久属性**,设一次就一直是。每次进场再设一遍是刻意的:
    幂等、便宜,而且免去「谁负责初始化」的时序纠纷(和上面幂等建表同一个姿势)。

    🔴 **``PRAGMA journal_mode`` 和 ``foreign_keys`` 受同一条位置约束** ——
    事务里设它同样不生效,所以必须写在 ``with conn:`` **之前**。别挪。

    🔴 **WAL 会多出 ``-wal`` / ``-shm`` 两个文件**,这条会咬到库外面:
    备份不能再 ``cp`` 或 rsync 单个 ``gyt.sqlite3``(那样拿到的是不完整状态),
    必须走 ``sqlite3 .backup``。``docker-compose.vps.yml`` 的备份服务就是这么写的,
    ``docs/W7_上线实录与部署踩坑.md`` 里那条手动 rsync 命令同理 —— 它拷的是整个
    ``data/`` 目录,三个文件一起走,所以仍然成立,但**别把它"优化"成只拷库文件**。

    ⚠️ 开不成 WAL 会**记一条 warning 而不是抛异常**:退回 delete 模式只是慢和容易撞锁,
    不是坏掉,为这个让整个后端起不来不划算。但它必须响 —— 静默退回的话,
    「两个人同时用会撞锁」这个已经修过的毛病会悄悄复发。
    """
    settings = get_settings()
    with closing(
        sqlite3.connect(settings.sqlite_path, timeout=settings.sqlite_busy_timeout_s)
    ) as conn:
        # 🔴 这两行必须在 `with conn:` 之外 —— 挪进去就是 no-op,理由见上面两节。
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        _enable_wal(conn)
        with conn:
            conn.executescript(ddl)
            yield conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """开 WAL,并**回读确认**。开不成只记 warning,不抛。

    回读是必须的:``PRAGMA journal_mode`` 设不成时**不报错**,它只是返回当前实际的
    模式。真会设不成的场景是库文件落在不支持共享内存的文件系统上(某些网络挂载),
    不回读的话我们会以为开了。
    """
    try:
        row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.Error as exc:  # pragma: no cover - 只有异常文件系统才走到
        _log.warning("开 WAL 失败,退回默认日志模式(并发写会更容易撞锁):%s", exc)
        return
    mode = (row[0] if row else "") or ""
    if mode.lower() != "wal":
        _log.warning(
            "WAL 没开成,当前日志模式是 %r(并发写会更容易撞锁)。"
            "多半是库文件所在的文件系统不支持共享内存。",
            mode,
        )


def now_iso() -> str:
    """本地时区、秒级、带 UTC 偏移的 ISO 时间戳(tasks / projects 两个域的 ``created_at`` 等)。

    ⚠️ 带偏移量**不是 bug**,别顺手"修"成 HK 固定时区:ISO 串自带 ``+08:00`` 这类偏移,
    时刻本身无歧义,换台机器读也不会读错。真正受宿主时区影响的是**不带偏移的日期**
    (``due_date`` 那种 YYYY-MM-DD),而那一列的值是上层算好传进来的,不在存储层生成。

    ⚠️ **它不是全仓的时间权威。** 需要「香港日历时间」的地方(隐患的 ``found_at`` /
    ``closed_at``、各类编号里的日期时刻)必须走 ``attendance/receipt.py`` 的快照 ——
    ``db/hazards.py`` 与 ``core/doc_no.py`` 走的是那条路,**别把它们改到这里来**。
    """
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def in_clause(values: Sequence[str]) -> str:
    """把**受控词表**渲染成 SQL 的 ``IN (...)`` 片段(建表时拼 CHECK 用)。

    ⚠️ 只能喂模块级常量。值全是代码里写死的字面量时,f-string 拼 SQL 不违反「全参数化」
    那条红线 —— 那条红线管的是「**运行期的值**不许进 SQL 文本」。拿它去拼用户给的东西
    就是注入口子,要拼那种请用 ``placeholders()`` 走占位符。
    """
    return ", ".join(f"'{v}'" for v in values)


def placeholders(count: int) -> str:
    """生成 ``?, ?, ?`` —— **个数**由代码算,值一律走占位符(方案红线 4)。

    ``IN ()`` 是语法错误:``count == 0`` 时会拼出空串,调用方必须在此之前就短路返回,
    别让空列表去打库(四个 db 模块的 ``list_by_hazard`` / ``docs_of`` 都是这么写的)。
    """
    return ", ".join("?" * count)


def insert_sql(table: str, fields: Sequence[str]) -> str:
    """拼一条 ``INSERT INTO 表 (列…) VALUES (?, …)``。**表名与列名只接模块级常量。**

    只服务「列名从 NamedTuple 的 ``_fields`` 派生」那一类 INSERT(``db/attendance.py`` 一处、
    ``db/hazards.py`` 三处):列名与传进去的参数元组来自**同一个** NamedTuple,加一列时
    只要 NamedTuple 与 DDL 一起改,INSERT 不会漏列、也不会与占位符个数对不上。

    手写列名的 INSERT(``db/tasks.py`` / ``db/projects.py``:列名是字面量、且**刻意少于**
    NamedTuple 的字段 —— 比如 ``project_id`` 建表即预留但从不写入)不要往这里塞,
    那会把「哪些列不写入」这条业务决定藏进一个通用函数里。
    """
    return f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders(len(fields))})"


__all__ = [
    "in_clause",
    "insert_sql",
    "now_iso",
    "open_db",
    "placeholders",
]
