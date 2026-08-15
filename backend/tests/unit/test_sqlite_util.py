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
from gyt.core.sqlite_util import in_clause, insert_sql, now_iso, open_db, placeholders

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
