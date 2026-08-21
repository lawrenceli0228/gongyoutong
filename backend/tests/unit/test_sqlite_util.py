"""core/sqlite_util.py(四个 db 模块共用的连接/事务/外键/时间戳样板)的单元测试。

不联网,库落在用例独占的 tmp_path:环境隔离整套复用 conftest 的 ``_isolated_settings``
(autouse),它把 GYT_DATA_DIR 指到 tmp_path/data 并在用例前后各做一次
``get_settings.cache_clear()``,所以本文件不需要任何自建 fixture。

本文件的被测物只用**自己的**玩具 DDL(``_TOY_DDL``):公共件的契约是「给我一段 DDL,
我保证什么时候跑它、连接长什么样」,拿真业务表来测会把这件事和隐患/考勤的业务约束缠在
一起,而那些约束各自在 test_db_*.py 里有。唯一 import db 模块的是最后那条守边界的用例
(确认 ``db/hazards.py`` 没把香港时区那份时间源改成 ``now_iso``)。

这层要钉死的静默错误(全是"不报错但结果错"那一类):
  · **外键没开却以为开了** —— ``foreign_keys=True`` 漏传 / 参数失灵,表现是脏行静默入库,
    要到"证据链引不出隐患""这个项目有哪些图掺进幽灵行"那天才发现;
  · **不该开的地方开了** —— 默认必须是关(tasks / attendance 两个域原本就不开,
    重构不许顺手改行为);
  · **事务没兜住** —— 块内抛异常却已经落库,调用方以为回滚了;
  · **库路径被缓存** —— 换 GYT_DATA_DIR 之后还写老库,测试之间互相看得见对方的数据。

---------------------------------------------------------------------------
🔴 测不出来的那一条:PRAGMA 的**位置**
---------------------------------------------------------------------------
``open_db`` 要求 ``PRAGMA foreign_keys = ON`` 写在 ``with conn:`` **之外**。
把它挪进事务里 —— 也就是重构最容易犯的那个错 —— **这里测不出来**:Python 的
sqlite3 只在 DML 时才隐式 BEGIN,``with conn:`` 本身不开事务,所以挪进去之后外键
照样生效、下面每一条用例照样绿。**这条位置约束只能靠注释和 review 守。**

下面两条用例把"为什么测不出来"和"那颗地雷是真的"分别钉死:
``test_PRAGMA挪进with_conn里也照样生效_所以位置约束测不出来`` 是前者的物证,
``test_PRAGMA在真的开着的事务里是no_op`` 是后者的物证。删掉它们,下一个人会以为
"位置随便放,反正测试会兜住"。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime

import pytest

from gyt.config import get_settings
from gyt.core.sqlite_util import (
    _enable_wal,
    in_clause,
    insert_sql,
    now_iso,
    open_db,
    placeholders,
)

# 玩具表:一张父表 + 一张外键指向它的子表,足够验"外键开没开";多条语句还顺带
# 验了公共件走的是 executescript(单条 execute 会抛 "one statement at a time")。
_TOY_DDL = """
CREATE TABLE IF NOT EXISTS toy_parent (
  id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS toy_child (
  id  INTEGER PRIMARY KEY AUTOINCREMENT,
  pid TEXT NOT NULL REFERENCES toy_parent(id)
);
CREATE INDEX IF NOT EXISTS idx_toy_child_pid ON toy_child(pid);
"""


def _raw_connect() -> sqlite3.Connection:
    """绕过公共件直连库文件:查 PRAGMA、验落库结果,都需要"上帝视角"。默认不开外键。"""
    return sqlite3.connect(get_settings().sqlite_path)


def _table_names() -> set[str]:
    with closing(_raw_connect()) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# open_db:建表 / 事务 / 库路径
# ---------------------------------------------------------------------------


def test_进场就把DDL跑掉_且多条语句都执行() -> None:
    """幂等建表是"连接进场"的一部分:``with`` 块里第一件事就该能直接用表。

    ``_TOY_DDL`` 有三条语句(两表 + 一索引)—— 公共件必须走 executescript,
    走 execute 的话会当场抛 "You can only execute one statement at a time"。
    """
    with open_db(_TOY_DDL) as conn:
        conn.execute("INSERT INTO toy_parent (id) VALUES ('p1')")

    assert {"toy_parent", "toy_child"} <= _table_names()


def test_建表幂等_同一段DDL跑很多次都不炸() -> None:
    """每个公开函数每次操作都跑一遍 DDL(四个 db 模块共同的姿势),它必须廉价且幂等。"""
    for _ in range(3):
        with open_db(_TOY_DDL) as conn:
            conn.execute("INSERT INTO toy_parent (id) VALUES (?)", (f"p{_}",))

    with closing(_raw_connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM toy_parent").fetchone()[0] == 3


def test_正常离场提交() -> None:
    with open_db(_TOY_DDL) as conn:
        conn.execute("INSERT INTO toy_parent (id) VALUES ('committed')")

    with closing(_raw_connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM toy_parent").fetchone()[0] == 1


def test_块内抛异常则整块回滚_而不是留下半截数据() -> None:
    """事务边界是调用方唯一的保证:hazards 的「迁移 + 挂文书」就靠它一起成一起败。

    半截数据的危害在证据链上最重:状态改了而文书没挂上,或者反过来。
    """
    with pytest.raises(RuntimeError, match="故意炸"), open_db(_TOY_DDL) as conn:
        conn.execute("INSERT INTO toy_parent (id) VALUES ('rolled-back')")
        raise RuntimeError("故意炸")

    with closing(_raw_connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM toy_parent").fetchone()[0] == 0


def test_离场关闭连接() -> None:
    """一次操作一条连接、用完就关(定案 #6):不关的话连接会随线程池越攒越多。"""
    with open_db(_TOY_DDL) as conn:
        pass

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_库路径每次现取_换GYT_DATA_DIR就换库(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """路径不许在模块里缓存 —— 缓存了的话测试换库时这层还写老库,
    而"老库里恰好也有同名表"会让断言全绿、验的却是上一轮的数据(W7 踩过)。"""
    with open_db(_TOY_DDL) as conn:
        conn.execute("INSERT INTO toy_parent (id) VALUES ('in-first-db')")
    first_db = get_settings().sqlite_path

    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "another"))
    get_settings.cache_clear()
    second_db = get_settings().sqlite_path
    assert second_db != first_db

    with open_db(_TOY_DDL) as conn:
        assert conn.execute("SELECT COUNT(*) FROM toy_parent").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 外键:开 / 不开 / 那条测不出来的位置约束
# ---------------------------------------------------------------------------


def test_foreign_keys_True时外键真的拦() -> None:
    """projects / hazards 两个域靠它:图不许挂在不存在的项目上,文书不许挂在不存在的隐患上。"""
    with pytest.raises(sqlite3.IntegrityError), open_db(_TOY_DDL, foreign_keys=True) as conn:
        conn.execute("INSERT INTO toy_child (pid) VALUES ('根本没这个爹')")


def test_默认不开外键_与重构前的tasks和attendance一致() -> None:
    """默认值是**照搬现状**,不是"忘了开":tasks / attendance 原本就没开这一项。

    哪天要给它们打开,那是另一件事(得先想清楚旧库里有没有已经存在的孤儿行),
    别借重构顺手改 —— 这条用例就是拦这种"顺手"的。
    """
    with open_db(_TOY_DDL) as conn:
        conn.execute("INSERT INTO toy_child (pid) VALUES ('孤儿行')")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0


def test_PRAGMA挪进with_conn里也照样生效_所以位置约束测不出来() -> None:
    """🔴 **这条用例是"测不出来"这句话的物证,不是在推荐这种写法。**

    重构最容易犯的错是把 ``PRAGMA foreign_keys = ON`` 从 ``with conn:`` 之前挪到之后。
    这里手工复现那种写法:外键**照样生效**——因为 Python 的 sqlite3 只在 DML 时才隐式
    BEGIN,``with conn:`` 自己不发 BEGIN,PRAGMA 执行时其实还在 autocommit 状态。

    也就是说:没有任何行为断言能区分"挪进去"和"没挪进去"。位置只能靠
    ``core/sqlite_util.open_db`` 的注释与 code review 守住。
    """
    with closing(_raw_connect()) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")  # ← 故意放在 `with conn:` 里面
        conn.executescript(_TOY_DDL)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO toy_child (pid) VALUES ('孤儿行')")


def test_PRAGMA在真的开着的事务里是no_op() -> None:
    """🔴 那颗地雷是真的 —— 只是本仓的 ``with conn:`` 恰好还没把事务开起来。

    真有事务在开着的时候(这里用一条 DML 把它开起来)设 ``PRAGMA foreign_keys = ON``:
    **回读就是 0,而且一声不吭**,外键从此静默不校验。哪天有人给 ``open_db`` 加一句
    显式 BEGIN、或者改用 ``autocommit=False``,PRAGMA 的位置就会立刻变成生死线 ——
    这条用例把那个前提钉在这儿,免得后人以为"位置无所谓"。
    """
    with closing(_raw_connect()) as conn:
        conn.executescript(_TOY_DDL)
        conn.execute("INSERT INTO toy_parent (id) VALUES ('开个事务')")  # 隐式 BEGIN
        assert conn.in_transaction

        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0  # ← no-op,无报错

        conn.execute("INSERT INTO toy_child (pid) VALUES ('孤儿行')")  # 没有 IntegrityError
        conn.rollback()


# ---------------------------------------------------------------------------
# 并发:WAL 与 busy timeout(2026-08-21 补,在此之前三样全缺)
# ---------------------------------------------------------------------------


def test_WAL真的开了_而且是库级持久属性() -> None:
    """开完一次之后,**另开一条连接**读到的也是 wal。

    分两半断言是有意的:``open_db`` 内部回读只能证明"那一条连接上是 wal",
    而 WAL 的价值在于**跨连接**(读不阻塞写)。第二半用上帝视角的裸连接
    再读一次,才是真正要的那个性质。
    """
    with open_db(_TOY_DDL) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    with closing(_raw_connect()) as raw:  # 另一条连接,没经过 open_db
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_有人在读的时候写得进去_这才是WAL要买的东西() -> None:
    """🔴 **行为证据**,不是配置回读 —— 上一条测的是"设上了",这条测的是"有用"。

    ⚠️ 方向很容易搞反,我第一版就写反了,而且变异测试当场抓到:
    写「写者开着事务不提交,读者能不能读」是**测不出区别**的 —— 默认模式下
    ``BEGIN IMMEDIATE`` 只拿 RESERVED 锁,读者照样读得到,要到 COMMIT 才升
    EXCLUSIVE。那一版把 WAL 那行注掉照样绿,是一条只是看起来像证据的用例。

    真正能分开两种模式的是**反方向**:
      读者 B 开着事务读(拿 SHARED 锁),写者 A 这时候要 COMMIT ——
      · 默认模式 → A 升不到 EXCLUSIVE,等到超时抛 ``database is locked``;
      · WAL 下   → A 直接提交成功,B 继续读它的旧快照。

    而这正是线上会撞的那一幕:一个人开着隐患清单,另一个人提交打卡。

    A 故意用 **0.3 秒**超时:WAL 那行被删掉时这条在 0.3 秒内红掉,
    而不是拿着生产配的 30 秒在 CI 里干挂半分钟。
    """
    with open_db(_TOY_DDL):  # 先建表 + 把库切成 WAL
        pass

    with (
        closing(sqlite3.connect(get_settings().sqlite_path, timeout=0.3)) as writer,
        closing(_raw_connect()) as reader,
    ):
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM toy_parent").fetchone()  # 拿住 SHARED

        writer.execute("INSERT INTO toy_parent (id) VALUES ('有人在读的时候写的')")
        writer.commit()  # ← 默认模式下这句会 database is locked

        reader.rollback()

    with closing(_raw_connect()) as check:
        assert check.execute("SELECT COUNT(*) FROM toy_parent").fetchone()[0] == 1


def test_撞锁等多久来自config_不许在代码里写死() -> None:
    """守的是「超时这类常量只从 ``get_settings()`` 取」那条红线。

    判据是 ``sqlite3.connect`` 实际收到的 ``timeout`` 关键字 —— 把参数删掉
    (退回 python 默认 5 秒)或者写死一个数字,这条都会红。
    """
    captured: dict[str, object] = {}
    real_connect = sqlite3.connect

    def spy(*args: object, **kwargs: object) -> sqlite3.Connection:
        captured.update(kwargs)
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sqlite3, "connect", spy)
        with open_db(_TOY_DDL):
            pass

    assert captured.get("timeout") == get_settings().sqlite_busy_timeout_s


def test_WAL开不成时只记warning不抛_但必须响() -> None:
    """退回默认日志模式只是"更容易撞锁",不是坏掉 —— 为它让整个后端起不来不划算。

    但它**必须响**:静默退回的话,「两个人同时用会撞锁」这个已经修过的毛病
    会悄悄复发,而现场没有任何线索。
    """

    class _拒绝换模式的连接:
        """模仿不支持共享内存的文件系统:PRAGMA 不报错,只是返回原来的模式。"""

        def execute(self, _sql: str) -> _拒绝换模式的连接:
            return self

        def fetchone(self) -> tuple[str]:
            return ("delete",)

    with pytest.MonkeyPatch.context():
        import logging as _logging

        records: list[_logging.LogRecord] = []

        class _收集(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                records.append(record)

        logger = _logging.getLogger("gyt.core.sqlite_util")
        handler = _收集()
        logger.addHandler(handler)
        try:
            _enable_wal(_拒绝换模式的连接())  # type: ignore[arg-type]
        finally:
            logger.removeHandler(handler)

    assert [r for r in records if r.levelno == _logging.WARNING], "WAL 没开成却一声不吭"


# ---------------------------------------------------------------------------
# now_iso / in_clause / placeholders / insert_sql
# ---------------------------------------------------------------------------


def test_now_iso是秒级且带偏移量() -> None:
    """带偏移量**不是 bug**(tasks / projects 两个域的原注释):ISO 串自带 ``+08:00``
    这类偏移,时刻本身无歧义。秒级 = 不带微秒,断言里比对时不会被尾巴噪声搅乱。"""
    stamp = now_iso()
    parsed = datetime.fromisoformat(stamp)

    assert parsed.utcoffset() is not None, "没有偏移量 = 换台机器读就读错了"
    assert parsed.microsecond == 0
    assert "." not in stamp


def test_now_iso不是香港时区权威() -> None:
    """守一条边界:需要香港日历时间的地方(隐患的 found_at、各类文书编号)走
    ``attendance/receipt.py`` 的快照,**不许改成调这里**。

    这条只钉"两者是不同的东西"这一点:本机与容器碰巧都是 UTC+8,数值一致是巧合,
    所以不比数值 —— 比的是 ``db/hazards.py`` 有没有把自己的时间源换成 now_iso。
    """
    import gyt.db.hazards as hazards

    assert hazards._now_iso.__module__ == "gyt.db.hazards"
    assert "make_snapshot" in hazards._now_iso.__code__.co_names


def test_in_clause拼出CHECK能用的片段() -> None:
    assert in_clause(("a", "b")) == "'a', 'b'"
    assert in_clause(("只有一个",)) == "'只有一个'"


def test_placeholders个数对得上() -> None:
    assert placeholders(3) == "?, ?, ?"
    assert placeholders(1) == "?"


def test_placeholders零个拼出空串_调用方必须在此之前短路() -> None:
    """``IN ()`` 是语法错误。这层不替调用方兜底(兜了会把"空列表也去打库"变成合法写法),
    四个 db 模块都在调用前 ``if not xxx: return []``。"""
    assert placeholders(0) == ""


def test_insert_sql的列名与占位符个数同源() -> None:
    """列名与占位符来自**同一个** fields 元组 —— 结构上不可能一边加了列另一边没加。"""
    assert insert_sql("t", ("a", "b", "c")) == "INSERT INTO t (a, b, c) VALUES (?, ?, ?)"


def test_insert_sql拼出来的语句真能执行() -> None:
    """光比字符串不够:拼错空格 / 逗号也可能"看起来对"。让 sqlite 自己认一遍。"""
    with open_db(_TOY_DDL) as conn:
        conn.execute(insert_sql("toy_parent", ("id",)), ("p1",))
        assert conn.execute("SELECT id FROM toy_parent").fetchone()[0] == "p1"


# ---------------------------------------------------------------------------
# 建表语句里不许有 SQL 注释(2026-08-21,CI 上抓到的)
# ---------------------------------------------------------------------------


def test_四个域的建表语句里一行SQL注释都没有() -> None:
    """🔴 一条**只是为了讲清楚**的注释,会让那张表在某些机器上再也 DROP 不了列。

    经过:2026-08-21 给 ``hazard_docs`` 加列时,顺手在 ``CREATE TABLE`` 里写了三行
    ``--`` 注释解释「新列为什么必须在末尾」。本机(sqlite 3.53)全绿,
    **CI 的 python 3.12 那个 job 红、3.11 绿**:

        sqlite3.OperationalError: error in table hazard_docs after drop column:
        incomplete input

    根因:sqlite 把建表语句**原样存进 ``sqlite_master``,注释一起存**。而
    ``ALTER TABLE … DROP COLUMN`` 的实现是「把那段列定义从存下来的 SQL 文本里剪掉、
    再重新解析一遍」—— 被删的列前面正好有一行 ``--`` 时,剪完剩下的文本里注释
    把后半句吞掉。sqlite 版本不同,吞与不吞的边界也不同。

    所以规矩是:**说明写在 SQL 外面**(Python 注释),DDL 里只留纯 SQL。
    四个域一起钉:今天只有 hazards 用得上 DROP COLUMN,但哪天别的域要做同样的
    做旧/迁移,同一颗雷会原样再炸一次 —— 而它只在**某些机器上**炸。
    """
    import re

    from gyt.db import attendance, hazards, projects, tasks

    for module in (attendance, hazards, projects, tasks):
        for name, value in vars(module).items():
            if not name.endswith("_DDL") or not isinstance(value, str):
                continue
            offenders = [line for line in value.splitlines() if re.search(r"--", line)]
            assert not offenders, (
                f"{module.__name__}.{name} 的建表语句里有 SQL 注释:{offenders}\n"
                "把说明搬到 SQL 外面 —— 注释会被存进 sqlite_master,"
                "让 ALTER TABLE DROP COLUMN 在某些 sqlite 版本上报 incomplete input。"
            )
