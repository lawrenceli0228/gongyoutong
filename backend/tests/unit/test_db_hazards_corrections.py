"""db/hazards.py 里两处**不改状态的订正**的单元测试 —— 改归属 与 改整改期限(2026-08-22)。

单开一个文件而不是往 ``test_db_hazards.py`` 里塞:那份钉的是**状态机**(八态两两组合的
补集循环、幂等键、外键),而这两个函数恰恰**不是**状态迁移(status 一个字节都不动)。
混在一起最直接的坏处是那个补集循环 —— 它按 ``ALLOWED_TRANSITIONS`` 穷举,
而这两条边在表里根本没有,读的人会先去表里找它们,找不到再回来。

环境隔离同 ``test_db_hazards.py``:复用 conftest 的 ``_isolated_settings``(autouse),
每个用例天然拿到一张全新的库。

这份要钉死的静默错误:
  · **状态守卫失守** —— 已经签发过文书的隐患被改了工地。纸上写着 A 工地、台账写着 B,
    两份都拿得出来而谁也说不清哪份算数,且**没有任何报错**。
  · **幂等键撞车被吞成 False** —— 「目标工地下已经有这条」和「状态不对」是两件事,
    混成一个信号的话监理会去查状态,方向全错。
  · **改期被拒却留了痕** —— 留痕里写着「期限改到了 X」而 ``due_date`` 还是原来那个,
    事后看的人会以为是后来又被改回去了。
  · **留痕只记新期限** —— 那样回答不了「这次挪了几天」,而那正是要看的东西。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from itertools import count

import pytest

from gyt.config import get_settings
from gyt.db import hazards

DUE = "2026-08-20"
"""这层不解析中文日期(那归端点 + schedule/dates.py),合法 ISO 日期就够。"""

LATER = "2026-08-27"
"""改期后的日期。刻意比 DUE 晚一周 —— 「宽限几天」是这条端点的主用途。"""

_SEQ = count(1)


def _raw_connect() -> sqlite3.Connection:
    """绕过存储层直连库文件:摆脏状态、查列序,都要"上帝视角"。"""
    return sqlite3.connect(get_settings().sqlite_path)


def _table_columns(table: str) -> list[str]:
    """库里某张表的真实列序。表名是本文件里的字面量,不是运行期的值。"""
    with closing(_raw_connect()) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _register(**overrides: object) -> hazards.HazardRow:
    """登记一条隐患并返回那一行。默认值都能过 CHECK。"""
    seq = next(_SEQ)
    params: dict[str, object] = {
        "hazard_no": f"GYT-H-20260822-090000-{seq:04x}",
        "project_id": "gyt-a3",
        "photo_sha256": f"sha256-{seq}",
        "photo_id": f"photo-{seq}",
        "item": "未戴安全帽",
        "severity": "一般",
        "grade": hazards.GRADE_NORMAL,
        "grading_version": "1",
        "needs_grading": False,
    }
    params.update(overrides)
    return hazards.create(**params).row  # type: ignore[arg-type]


def _force_status(hazard_no: str, status: str) -> None:
    """绕过状态机直接把一行摆到某个状态 —— 要从八个状态各出发一次,正常流程摆太慢,
    而且会把"被测的那一步"混进一长串前置动作里。"""
    with closing(_raw_connect()) as conn, conn:
        conn.execute("UPDATE hazards SET status = ? WHERE hazard_no = ?", (status, hazard_no))


# ---------------------------------------------------------------------------
# 改归属(reassign_project)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", hazards.REASSIGNABLE_STATUSES)
def test_没签过文书的两档可以改归属(status: str) -> None:
    """pending 与 open —— 纸还没发出去,改台账不会和任何东西对不上。"""
    row = _register()
    _force_status(row.hazard_no, status)

    assert hazards.reassign_project(row.hazard_no, "gyt-b7") is True
    after = hazards.fetch(row.hazard_no)
    assert after is not None
    assert after.project_id == "gyt-b7"
    assert after.status == status, "改归属**不是状态迁移**,status 一个字节都不许动"


@pytest.mark.parametrize(
    "status", [s for s in hazards.STATUSES if s not in hazards.REASSIGNABLE_STATUSES]
)
def test_签过文书之后一律不许改归属(status: str) -> None:
    """补集穷举:其余六档全拒。

    ⚠️ 这条**必须写成补集而不是逐个列举**。列举法的失效方式很具体:哪天状态机加了
    第九档,列举的清单不会有任何东西提醒它漏了 —— 而那一档默认是"能改",
    也就是往**放行**的方向漏。
    """
    row = _register()
    _force_status(row.hazard_no, status)

    assert hazards.reassign_project(row.hazard_no, "gyt-b7") is False
    after = hazards.fetch(row.hazard_no)
    assert after is not None
    assert after.project_id == "gyt-a3", "拒了就一列都不许动"


def test_改归属可以挪回未归属() -> None:
    """空串是 D6 定下的那一档(未归属),不是"清空"这种错误态 —— 挪得回去。"""
    row = _register()
    assert hazards.reassign_project(row.hazard_no, "") is True
    after = hazards.fetch(row.hazard_no)
    assert after is not None and after.project_id == ""


def test_编号不存在时改归属返回False不抛() -> None:
    assert hazards.reassign_project("GYT-H-不存在", "gyt-b7") is False


def test_目标工地已有同一条时抛IntegrityError而不是返回False() -> None:
    """幂等键 ``(project_id, photo_sha256, item)`` 撞车 —— **不吞**。

    为什么不能吞成 False:False 的含义是「状态不对」。两件事混成一个信号的话,
    监理拿到「这条状态刚被改过」的提示,回去查状态,而状态好好的 —— 方向全错。
    真相是「那条隐患在目标工地已经登记过了」,该由端点说这句人话。
    """
    same_photo, same_item = "sha256-撞车", "未戴安全帽"
    _register(project_id="gyt-b7", photo_sha256=same_photo, item=same_item)
    mine = _register(project_id="gyt-a3", photo_sha256=same_photo, item=same_item)

    with pytest.raises(sqlite3.IntegrityError):
        hazards.reassign_project(mine.hazard_no, "gyt-b7")

    after = hazards.fetch(mine.hazard_no)
    assert after is not None and after.project_id == "gyt-a3", "撞车之后事务回滚,一列都没动"


def test_可改归属的那两档确实挂不上任何文书() -> None:
    """``REASSIGNABLE_STATUSES`` 是 status 这一维的守卫,而它想表达的是"还没签过文书"。

    两者今天等价是**状态机的结果**:``hazard_docs`` 只在 notified / suspended /
    resuming / escalated 那几步写入,{pending, open} 一步都走不到。这条用例钉的就是
    这条等价 —— 哪天状态机让 open 也能挂文书,它会红,而不是 ``reassign_project``
    静默放行一条纸已经发出去的隐患。
    """
    doc_writing_targets = {
        hazards.STATUS_NOTIFIED,
        hazards.STATUS_SUSPENDED,
        hazards.STATUS_RESUMING,
        hazards.STATUS_ESCALATED,
    }
    reachable_from_reassignable = {
        target
        for src in hazards.REASSIGNABLE_STATUSES
        for target in hazards.ALLOWED_TRANSITIONS[src]
    }
    assert reachable_from_reassignable & doc_writing_targets <= doc_writing_targets
    for src in hazards.REASSIGNABLE_STATUSES:
        row = _register()
        _force_status(row.hazard_no, src)
        assert hazards.docs_of([row.hazard_no]) == [], f"{src} 这一档不该有任何文书"


# ---------------------------------------------------------------------------
# 改整改期限(extend_due_date)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", hazards.DUE_CHANGEABLE_STATUSES)
def test_有期限的两档可以改期并留全痕(status: str) -> None:
    """notified 与 suspended —— 这两档才有一个在跑的期限。"""
    row = _register()
    hazards.confirm(row.hazard_no)
    if status == hazards.STATUS_NOTIFIED:
        hazards.mark_notified(row.hazard_no, DUE, expected_grade=hazards.GRADE_NORMAL)
    else:
        hazards.mark_suspended(row.hazard_no, DUE, expected_grade=hazards.GRADE_NORMAL)

    assert (
        hazards.extend_due_date(row.hazard_no, LATER, reason="下雨停工三天", changed_by="陈工")
        is True
    )

    after = hazards.fetch(row.hazard_no)
    assert after is not None
    assert after.due_date == LATER
    assert after.status == status, "改期**不是状态迁移**,status 一个字节都不许动"

    (trace,) = hazards.due_changes_of([row.hazard_no])
    assert (trace.old_due, trace.new_due) == (DUE, LATER), (
        "只记新期限的话,留痕回答不了「这次挪了几天」—— 而那正是要看的东西"
    )
    assert (trace.reason, trace.changed_by) == ("下雨停工三天", "陈工")


@pytest.mark.parametrize(
    "status", [s for s in hazards.STATUSES if s not in hazards.DUE_CHANGEABLE_STATUSES]
)
def test_没有期限的六档改期一律被拒且不留痕(status: str) -> None:
    """补集穷举。**「不留痕」这半条同样重要**:改被拒还记一行的话,台账里会出现一次
    「期限改到了 X」而 ``due_date`` 还是原来那个,事后看留痕的人会以为是后来又改回去了。
    """
    row = _register()
    _force_status(row.hazard_no, status)

    assert hazards.extend_due_date(row.hazard_no, LATER, reason="试试") is False
    assert hazards.due_changes_of([row.hazard_no]) == []
    after = hazards.fetch(row.hazard_no)
    assert after is not None and after.due_date is None


def test_编号不存在时改期返回False不抛() -> None:
    assert hazards.extend_due_date("GYT-H-不存在", LATER, reason="试试") is False


def test_连续改期按先后留下多行() -> None:
    """「这条被展了几次期」是这张表存在的全部理由 —— 一条隐患一路展到下个月,
    每次都有个说得通的理由,只有把每一次并排看才看得出来。"""
    row = _register()
    hazards.confirm(row.hazard_no)
    hazards.mark_notified(row.hazard_no, DUE, expected_grade=hazards.GRADE_NORMAL)

    hazards.extend_due_date(row.hazard_no, "2026-08-25", reason="材料没到", changed_by="陈工")
    hazards.extend_due_date(row.hazard_no, "2026-09-01", reason="又下雨", changed_by="陈工")
    hazards.extend_due_date(row.hazard_no, "2026-09-08", reason="人手不够", changed_by="李工")

    traces = hazards.due_changes_of([row.hazard_no])
    assert [(t.old_due, t.new_due) for t in traces] == [
        (DUE, "2026-08-25"),
        ("2026-08-25", "2026-09-01"),
        ("2026-09-01", "2026-09-08"),
    ], "首尾要接得上 —— 接不上说明 old_due 读的不是同一个事务里的那份"


def test_留痕批量取回_空入参不打库() -> None:
    """姿势与 ``docs_of`` 逐字相同:``IN ()`` 是语法错误,空入参直接返回 []。"""
    assert hazards.due_changes_of([]) == []

    nos = []
    for _ in range(2):
        row = _register()
        hazards.confirm(row.hazard_no)
        hazards.mark_notified(row.hazard_no, DUE, expected_grade=hazards.GRADE_NORMAL)
        hazards.extend_due_date(row.hazard_no, LATER, reason="批量取回用")
        nos.append(row.hazard_no)

    traces = hazards.due_changes_of(nos)
    assert len(traces) == 2
    assert [t.hazard_no for t in traces] == sorted(nos), "按 hazard_no, id 排"


def test_留痕表字段顺序与建表列顺序一致() -> None:
    """错位不会有任何报错(old_due / new_due / reason 全是 TEXT),
    但会让「理由」那一列装着日期。"""
    _register()  # 触发幂等建表
    assert _table_columns("hazard_due_changes") == list(hazards.DueChangeRow._fields)


def test_留痕挂在不存在的隐患上会被外键拦住() -> None:
    """``hazard_due_changes.hazard_no`` 有外键。漏开 ``PRAGMA foreign_keys`` 的表现是
    留痕能挂在一个根本不存在的隐患上,而那要到查「这条展了几次期」那天才发现引不出来。
    """
    _register()  # 触发建表
    with closing(_raw_connect()) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hazard_due_changes "
                "(hazard_no, old_due, new_due, reason, changed_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("GYT-H-不存在", None, LATER, "试试", None, "2026-08-22T09:00:00+08:00"),
            )


# ---------------------------------------------------------------------------
# 两个受控集合本身
# ---------------------------------------------------------------------------


def test_三个受控集合都是STATUSES的子集() -> None:
    """打错一个字母的表现是那一档静默失效(``status IN (…)`` 永不命中),而不是报错。"""
    known = set(hazards.STATUSES)
    assert set(hazards.REASSIGNABLE_STATUSES) <= known
    assert set(hazards.DUE_CHANGEABLE_STATUSES) <= known
    assert set(hazards.GRADABLE_STATUSES) <= known


def test_改期与改归属的两个集合不相交() -> None:
    """不是巧合,是这两件事的定义:一个是「纸还没发出去」,一个是「纸已经发出去、
    上面写着期限」。哪天它们有了交集,说明状态机变了,两处的推演都要回去重读。
    """
    assert not (set(hazards.REASSIGNABLE_STATUSES) & set(hazards.DUE_CHANGEABLE_STATUSES))
