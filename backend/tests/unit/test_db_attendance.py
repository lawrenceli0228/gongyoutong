"""考勤台账表结构(db/attendance.py)的守门单测。T1 只冻结结构,读写归 T3。

这里锁的三件事都属于「错了不报错、或者报错报在错的地方」:

  · **NamedTuple 的字段顺序必须等于 DDL 的列顺序。** 错位不会有任何异常 ——
    ``lat`` 里装着 ``lon``,两个都是 REAL,类型检查抓不到。
  · **受控词表必须与 CHECK 子句同源。** 漂了的表现是 INSERT 抛
    ``sqlite3.IntegrityError``,报错在数据库层,而真正的原因在前端某个分支。
  · **``artifact_id`` 必须可空。** 写成 NOT NULL 与清理器直接冲突,
    而冲突要到清理器第一次真跑才暴露 —— 通常是上线几周之后。

全部用内存库(``:memory:``),不碰 ``data/gyt.sqlite3``。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

import pytest

from gyt.db import attendance as store
from gyt.db.attendance import (
    _DDL,
    GEO_STATUSES,
    SOURCES,
    AttendanceRow,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """建好表的内存库。每个用例一份,天然隔离。"""
    c = sqlite3.connect(":memory:")
    c.executescript(_DDL)
    yield c
    c.close()


def _ddl_columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(attendance)")]


def _row(**overrides: object) -> dict[str, object]:
    """一行合法的最小数据。用例只覆盖自己关心的那一列。"""
    base: dict[str, object] = {
        "event_id": "evt-1",
        "req_digest": "d" * 64,
        "worker_name": "张三",
        "site_name": "A栋",
        "checked_at": "2026-08-15T08:30:00+08:00",
        "work_date": "2026-08-15",
        "lat": 22.302711,
        "lon": 114.177216,
        "accuracy_m": 12.5,
        "geo_status": "ok",
        "source": "camera",
        "receipt_no": "GYT-A-20260815-083000-ab12",
        "artifact_id": "f" * 32,
        "photo_purged_at": None,
        "project_id": None,
        "created_at": "2026-08-15T08:30:00+08:00",
    }
    return {**base, **overrides}


def _insert(conn: sqlite3.Connection, **overrides: object) -> None:
    data = _row(**overrides)
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    conn.execute(f"INSERT INTO attendance ({cols}) VALUES ({marks})", tuple(data.values()))


class Test结构对位:
    def test_NamedTuple_字段顺序等于_DDL_列顺序(self, conn: sqlite3.Connection) -> None:
        """这是整个存储层的对位锚。

        ``_COLUMNS`` 由 ``AttendanceRow._fields`` 派生,``SELECT {_COLUMNS}`` 的结果
        再喂给 ``AttendanceRow(*row)`` —— 只要两边顺序一致就永远对得上。
        断言写成「顺序相等」而不是「集合相等」,正是因为**错位不抛异常**。
        """
        assert list(AttendanceRow._fields) == _ddl_columns(conn)

    def test_预留了_project_id_且不进任何签名(self, conn: sqlite3.Connection) -> None:
        """照搬 tasks 表:建表即预留、写入恒 NULL(方案定案 #11)。

        建表时不留,将来加多项目隔离就得写迁移 —— 而这个项目没有迁移框架。
        """
        assert "project_id" in _ddl_columns(conn)

    def test_两个索引都建了(self, conn: sqlite3.Connection) -> None:
        """``idx_att_date`` 服务「今天谁到了」,``idx_att_worker`` 服务「张三这个月来了几天」。

        少了索引不会报错,只会在台账攒到几万行之后慢下来 —— 而那时没人会想到是索引。
        """
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_att_date", "idx_att_worker"} <= names


class Test受控词表与_CHECK_同源:
    def test_六个_geo_status_全部能写进去(self, conn: sqlite3.Connection) -> None:
        for i, status in enumerate(GEO_STATUSES):
            _insert(conn, event_id=f"e{i}", receipt_no=f"r{i}", geo_status=status)
        assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == len(GEO_STATUSES)

    def test_两个_source_全部能写进去(self, conn: sqlite3.Connection) -> None:
        for i, src in enumerate(SOURCES):
            _insert(conn, event_id=f"e{i}", receipt_no=f"r{i}", source=src)
        assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == len(SOURCES)

    def test_词表外的_geo_status_被_CHECK_挡住(self, conn: sqlite3.Connection) -> None:
        """这条是「前端字段漂了」的守门测试。

        没有 CHECK 的话,前端拼错的 ``"denied "``(尾随空格)会**成功入库**,
        而查询侧按六个值分组时那批记录哪一组都不进,静默消失。
        """
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, geo_status="denied ")

    def test_词表外的_source_被_CHECK_挡住(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, source="gallery")

    def test_CHECK_子句是从词表拼出来的(self) -> None:
        """结构性保证:Python 常量与 SQL 不可能各改各的。

        断言 DDL 文本里出现的**恰好**是词表里那些值 —— 手写一份的话,
        往 Python 元组里加一个新状态而忘了改 SQL,表现是新状态入库即失败。
        """
        clause = re.search(r"geo_status IN \(([^)]*)\)", _DDL)
        assert clause is not None
        assert [v.strip().strip("'") for v in clause.group(1).split(",")] == list(GEO_STATUSES)


class Test可空性与唯一性:
    def test_artifact_id_可空(self, conn: sqlite3.Connection) -> None:
        """⚠️ 别改回 NOT NULL。

        留存策略要求「照片过期后删图、行留着」:清理器把 ``artifact_id`` 置 NULL、
        ``photo_purged_at`` 记时间。NOT NULL 与它直接冲突,
        而冲突要到清理器第一次真跑才暴露。
        """
        _insert(conn, artifact_id=None, photo_purged_at="2026-09-15T00:00:00+08:00")
        row = conn.execute("SELECT artifact_id, photo_purged_at FROM attendance").fetchone()
        assert row[0] is None
        assert row[1] is not None

    def test_坐标三列可空_定位失败照样能打卡(self, conn: sqlite3.Connection) -> None:
        """工地信号差是常态。为了定位挡住打卡是本末倒置。"""
        _insert(conn, lat=None, lon=None, accuracy_m=None, geo_status="denied")
        assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 1

    def test_event_id_唯一(self, conn: sqlite3.Connection) -> None:
        """幂等第三层就靠它:并发同键时输家 INSERT 失败 → 回查那条返回它。"""
        _insert(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, receipt_no="GYT-A-20260815-083001-cd34")

    def test_receipt_no_唯一(self, conn: sqlite3.Connection) -> None:
        """同秒多人打卡时编号会碰 —— 唯一约束是「撞库重试」那条逻辑的触发器。

        没有它,两条记录共用一个凭证编号,而工友拿编号来问的时候查出两条。
        """
        _insert(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, event_id="evt-2")

    def test_建表幂等(self, conn: sqlite3.Connection) -> None:
        """每个公开函数都会执行一次建表(与 tasks.py 同姿势),必须能反复跑。"""
        _insert(conn)
        conn.executescript(_DDL)
        assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 以下走真实存储层(库落在 conftest 的 _isolated_settings 指的 tmp 数据目录),
# 不再用上面的内存库 —— 存取函数要连同「每次现取 settings.sqlite_path」一起测。
# ---------------------------------------------------------------------------


def _draft(**overrides: object) -> store.CheckinDraft:
    """一条合法的待写入打卡。用例只覆盖自己关心的字段。"""
    base: dict[str, object] = {
        "event_id": "evt-f1",
        "req_digest": "d" * 64,
        "worker_name": "张三",
        "site_name": "A栋",
        "checked_at": "2026-08-15T08:30:00+08:00",
        "work_date": "2026-08-15",
        "lat": 22.302711,
        "lon": 114.177216,
        "accuracy_m": 12.5,
        "geo_status": "ok",
        "source": "camera",
        "receipt_no": "GYT-A-20260815-083000-ab12",
        "artifact_id": "f" * 32,
        "created_at": "2026-08-15T08:30:00+08:00",
    }
    base.update(overrides)
    return store.CheckinDraft(**base)  # type: ignore[arg-type]


class Test存取层往返:
    def test_写读往返全字段保真(self) -> None:
        """insert→find 的字段级往返:SELECT 列序与 AttendanceRow 错位时在这里现形。"""
        row = store.insert_checkin(_draft())
        assert row.id > 0
        assert row.project_id is None  # 预留列,写入恒 NULL
        fetched = store.find_by_event_id("evt-f1")
        assert fetched == row
        assert fetched is not None and fetched.worker_name == "张三"
        assert fetched.lat == pytest.approx(22.302711)

    def test_查无此键返回_None(self) -> None:
        assert store.find_by_event_id("不存在的键") is None

    def test_撞_event_id_翻译成类型化异常(self) -> None:
        """幂等层③的信号。调用方拿到它就去回查同键那条并返回 —— 不该看见裸的
        IntegrityError,否则 handler 得自己 parse sqlite 的报错文本。"""
        store.insert_checkin(_draft())
        with pytest.raises(store.DuplicateEventError):
            store.insert_checkin(_draft(receipt_no="GYT-A-20260815-083001-cd34"))

    def test_撞_receipt_no_翻译成类型化异常(self) -> None:
        """同秒碰撞的信号。正确动作是换个随机尾重试 —— 与上一条的动作完全相反,
        所以必须是两个不同的异常类型,不能共用一个。"""
        store.insert_checkin(_draft())
        with pytest.raises(store.DuplicateReceiptError):
            store.insert_checkin(_draft(event_id="evt-f2"))

    def test_recent_按插入序倒排(self) -> None:
        for i in range(3):
            store.insert_checkin(_draft(event_id=f"e{i}", receipt_no=f"r-{i}"))
        rows = store.list_recent(2)
        assert [r.event_id for r in rows] == ["e2", "e1"]


class Test聚合查询:
    """W7 §3.10:聚合在 SQL 里,每人首末两次 + 次数,LIMIT 封顶。"""

    def _seed(self) -> None:
        specs = [
            # 张三:15 号打三次(早/午/晚)、16 号一次 → 2 天 4 次
            ("张三", "2026-08-15", "08:00:00"),
            ("张三", "2026-08-15", "12:00:00"),
            ("张三", "2026-08-15", "17:30:00"),
            ("张三", "2026-08-16", "08:05:00"),
            # 李四:只有 15 号一次
            ("李四", "2026-08-15", "09:00:00"),
        ]
        for i, (name, day, hms) in enumerate(specs):
            store.insert_checkin(
                _draft(
                    event_id=f"agg-{i}",
                    receipt_no=f"agg-r-{i}",
                    worker_name=name,
                    work_date=day,
                    checked_at=f"{day}T{hms}+08:00",
                )
            )

    def test_按人算天数_同天多次只算一天(self) -> None:
        self._seed()
        result = store.count_worker_days(
            date_from="2026-08-01", date_to="2026-08-31", max_workers=10
        )
        assert result == [
            store.WorkerDays("张三", days=2, checkins=4),
            store.WorkerDays("李四", days=1, checkins=1),
        ]

    def test_按人算天数_指定单人(self) -> None:
        self._seed()
        result = store.count_worker_days(
            date_from="2026-08-01", date_to="2026-08-31", worker_name="李四", max_workers=10
        )
        assert result == [store.WorkerDays("李四", days=1, checkins=1)]

    def test_日期窗口边界含首尾(self) -> None:
        self._seed()
        only_16 = store.count_worker_days(
            date_from="2026-08-16", date_to="2026-08-16", max_workers=10
        )
        assert only_16 == [store.WorkerDays("张三", days=1, checkins=1)]

    def test_某天明细_首末与次数(self) -> None:
        self._seed()
        detail = store.list_day_detail("2026-08-15", max_workers=10)
        assert detail == [
            store.WorkerDayDetail(
                "张三", "2026-08-15T08:00:00+08:00", "2026-08-15T17:30:00+08:00", 3
            ),
            store.WorkerDayDetail(
                "李四", "2026-08-15T09:00:00+08:00", "2026-08-15T09:00:00+08:00", 1
            ),
        ]

    def test_明细人数上限生效(self) -> None:
        """D10 允许无限打卡,「某天明细」没有上限就可能几百行 —— LIMIT 是硬要求。"""
        self._seed()
        assert len(store.list_day_detail("2026-08-15", max_workers=1)) == 1


class Test清理支撑:
    def test_在用图集合一次取回(self) -> None:
        store.insert_checkin(_draft(event_id="c1", receipt_no="cr1", artifact_id="a" * 32))
        store.insert_checkin(_draft(event_id="c2", receipt_no="cr2", artifact_id="b" * 32))
        store.insert_checkin(_draft(event_id="c3", receipt_no="cr3", artifact_id=None))
        assert store.list_active_artifact_ids() == {"a" * 32, "b" * 32}

    def test_到期行按work_date早于cutoff筛_不含当天(self) -> None:
        store.insert_checkin(_draft(event_id="old", receipt_no="r-old", work_date="2026-05-01"))
        store.insert_checkin(_draft(event_id="edge", receipt_no="r-edge", work_date="2026-05-10"))
        expired = store.list_expired_photos("2026-05-10")
        assert [c.artifact_id for c in expired] == ["f" * 32]
        assert store.find_by_event_id("old").id == expired[0].row_id  # type: ignore[union-attr]

    def test_置空幂等且不覆盖首次清理时间(self) -> None:
        """重复跑清理器是常态(定时任务),第二次不许把第一次的清理时间刷掉 ——
        「什么时候删的」是留存审计要回答的问题。"""
        row = store.insert_checkin(_draft())
        assert store.mark_photos_purged([row.id], "2026-11-15T03:00:00+08:00") == 1
        assert store.mark_photos_purged([row.id], "2026-12-01T03:00:00+08:00") == 0
        after = store.find_by_event_id(row.event_id)
        assert after is not None
        assert after.artifact_id is None
        assert after.photo_purged_at == "2026-11-15T03:00:00+08:00"

    def test_空列表直接返回零(self) -> None:
        assert store.mark_photos_purged([], "2026-11-15T03:00:00+08:00") == 0
