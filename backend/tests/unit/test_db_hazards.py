"""db/hazards.py(隐患台账 + 状态机)的单元测试 —— 不联网,库落在用例独占的 tmp_path。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):它把 GYT_DATA_DIR
指到 tmp_path/data,并在用例前后各做一次 ``get_settings.cache_clear()``,
所以本文件不需要任何自建 fixture,每个用例天然拿到一张全新的库。

这层要钉死的静默错误(全是"不报错但结果错"那一类):
  · **非法状态迁移被放行** —— 尤其 ``suspended → closed``:停过工的隐患直接销项 =
    **漏发《工程复工令》**(Codex#6)。补集循环把八态两两组合全跑一遍。
  · **幂等键漏了 project_id** —— 同一张照片用在第二个项目上时复用第一个项目的隐患行,
    也就是**跨项目串账**:A 工地的整改记录挂到 B 工地头上。
  · **``hazard_no`` 撞号被幂等语句静默吞掉** —— 调用方以为登记成功,拿到的却是别人的编号。
  · **外键没真开** —— ``PRAGMA foreign_keys`` 漏了的话文书能挂在不存在的隐患上,
    要到上报主管部门那天才发现证据链引不出隐患。
  · **NamedTuple 字段顺序与建表列顺序错位** —— ``severity`` 与 ``grade`` 都是 TEXT,
    换了位置类型检查抓不到,表现是"重大隐患按一般隐患签了通知单"。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from itertools import count

import pytest

from gyt.config import get_settings
from gyt.db import hazards

# 手工做旧用的时间戳:一眼假,断言里出现时绝不会与真实当前时间撞车。
OLD_STAMP = "2000-01-01T00:00:00+08:00"

# 整改期限固定一个未来日期:这层不解析中文日期(那归端点 + schedule/dates.py),
# 测试里只要是个合法 ISO 日期就够。
DUE = "2026-08-20"

_SEQ = count(1)
"""登记序号。每个用例一张新库,所以只要**同一个用例内**不撞就够。"""


def _raw_connect() -> sqlite3.Connection:
    """绕过存储层直连库文件:摆脏状态、查 PRAGMA,都需要"上帝视角"。"""
    return sqlite3.connect(get_settings().sqlite_path)


def _table_columns(table: str) -> list[str]:
    """库里某张表的真实列序。表名是本文件里的字面量,不是运行期的值。"""
    with closing(_raw_connect()) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _register(**overrides: object) -> hazards.Registration:
    """登记一条隐患(默认值都是能过 CHECK 的普通值),用 overrides 改想改的那几个。"""
    seq = next(_SEQ)
    params: dict[str, object] = {
        "hazard_no": f"GYT-H-20260816-090000-{seq:04x}",
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
    return hazards.create(**params)  # type: ignore[arg-type]


def _force_status(hazard_no: str, status: str, was_suspended: int) -> None:
    """绕过状态机直接把一行摆到某个状态。

    补集测试要从八个状态各出发一次,而 ``escalated`` / ``closed`` 这些格子靠正常流程
    摆出来既慢又会把"被测的那一步"混进一长串前置动作里。上帝视角是刻意的。
    """
    with closing(_raw_connect()) as conn, conn:
        conn.execute(
            "UPDATE hazards SET status = ?, was_suspended = ? WHERE hazard_no = ?",
            (status, was_suspended, hazard_no),
        )


def _attempt(hazard_no: str, dst: str) -> bool:
    """尝试把某条隐患迁到 dst,返回是否真的迁过去了。

    ⚠️ 只调**以 dst 为目标**的那几个函数。这里**不能**用 ``pass_reinspection`` ——
    它会按 ``was_suspended`` 自己挑边,可能落到另一个状态上,那样测的就不是"dst 这一格"了,
    还会给后面"状态没被改动"的断言留下假失败。
    """
    if dst == hazards.STATUS_OPEN:
        return hazards.confirm(hazard_no)
    if dst == hazards.STATUS_NOTIFIED:
        return hazards.mark_notified(hazard_no, DUE, expected_grade=hazards.GRADE_NORMAL)
    if dst == hazards.STATUS_SUSPENDED:
        return hazards.mark_suspended(hazard_no, DUE, expected_grade=hazards.GRADE_NORMAL)
    if dst == hazards.STATUS_REINSPECT_FAILED:
        return hazards.mark_reinspect_failed(hazard_no)
    if dst == hazards.STATUS_RESUMING:
        return hazards.start_resumption(hazard_no)
    if dst == hazards.STATUS_CLOSED:
        # **三条**路通向 closed,任一条成即算迁到了:
        #   · close_after_pass  复查合格直接销项(没停过工的)
        #   · mark_resumed      出复工令(停过工的)
        #   · dismiss           不出文书关掉(2026-08-21 加,只从 open 出发)
        # 漏掉第三条的表现很具体:补集用例会报「open → closed 该成却没成」——
        # 它不是在说 dismiss 坏了,是在说这个 helper 不知道有这条路。
        return (
            hazards.close_after_pass(hazard_no)
            or hazards.mark_resumed(hazard_no)
            or hazards.dismiss(hazard_no, reason="补集测试:不出文书关掉")
        )
    if dst == hazards.STATUS_ESCALATED:
        return hazards.mark_escalated(hazard_no)
    # pending 没有任何迁移函数以它为目标 —— 只有登记会落 pending,而且回不去。
    assert dst == hazards.STATUS_PENDING
    return False


def _expected_ok(src: str, dst: str, was_suspended: int) -> bool:
    """这一格**应该**成还是不成 = 状态机表说了算 + 两条 was_suspended 门票。

    两条门票不是额外规则,就是 Codex#6 那条分支本身:
      · 进 ``resuming``(等复工令)必须曾停过工;
      · 复查合格直接销项必须**没**停过工 —— 停过的得先走 resuming 出复工令。
    ``resuming → closed`` 是复工令那条路,不受这条门票管。
    """
    if dst not in hazards.ALLOWED_TRANSITIONS[src]:
        return False
    if dst == hazards.STATUS_RESUMING:
        return was_suspended == 1
    if dst == hazards.STATUS_CLOSED and src != hazards.STATUS_RESUMING:
        return was_suspended == 0
    return True


# --- 登记与幂等 ---------------------------------------------------------------


def test_登记往返全字段保真且落待确认态() -> None:
    """create→fetch 的字段级往返:SELECT 列序与 HazardRow 对不上位时在这里现形。

    登记恒落 ``pending``(D17):safety 看一眼照片就开启法律流程是不可接受的,
    必须有人在界面上确认。``was_suspended`` / ``due_date`` / ``confirmed_at`` 三样
    只能由后续迁移函数改,登记时不许抄近路。
    """
    reg = _register(item="临边无防护", severity="较大", grade=hazards.GRADE_SEVERE)

    assert reg.created is True
    row = hazards.fetch(reg.row.hazard_no)
    assert row is not None
    assert row == reg.row
    assert row.item == "临边无防护"
    assert row.severity == "较大"
    assert row.grade == hazards.GRADE_SEVERE
    assert row.status == hazards.STATUS_PENDING
    assert row.needs_grading == 0
    assert row.was_suspended == 0
    assert row.due_date is None
    assert row.confirmed_at is None
    assert row.closed_at is None
    assert row.found_at == row.created_at == row.updated_at  # 生辰三枚时间戳同源同值


def test_行字段顺序与建表列顺序一致() -> None:
    """错位不会有任何报错(全是 TEXT),但会让 severity 里装着 grade。三张表一起钉。"""
    _register()  # 触发幂等建表

    assert _table_columns("hazards") == list(hazards.HazardRow._fields)
    assert _table_columns("hazard_docs") == list(hazards.HazardDocRow._fields)
    assert _table_columns("hazard_ingest_failures") == list(hazards.IngestFailureRow._fields)


def test_时间戳走香港时区权威而不是宿主时区() -> None:
    """D7:业务时区只有一个权威(``attendance/receipt.py``)。

    改回 ``datetime.now(UTC).astimezone()`` 的表现是偏移跟着宿主走 —— 容器是 Shanghai、
    CI 常是 UTC,而本机开发多半是 UTC+8,**在本机跑测试看不出任何问题**。
    这条断言在 UTC 的 CI 上才会真正发力,所以它必须存在。
    """
    row = _register().row

    assert row.found_at.endswith("+08:00")
    assert row.created_at.endswith("+08:00")


def test_幂等_同三元组第二次登记不新增() -> None:
    """幂等键是 ``(project_id, photo_sha256, item)``,**不是 artifact_id**(D14)——
    artifacts 用 uuid4,同一张照片重传就是新号,彩排三轮会得三批隐患。
    第二次要返回**首次登记的那一行**(编号是老号),而不是这次传进来的新号。"""
    first = _register(photo_sha256="同一张照片", item="未戴安全帽")

    second = _register(photo_sha256="同一张照片", item="未戴安全帽")

    assert second.created is False
    assert second.row.hazard_no == first.row.hazard_no
    assert len(hazards.list_rows()) == 1


def test_幂等_同一张照片在不同项目各自独立成行() -> None:
    """跨项目串账的守门断言(Codex#9)。

    唯一键漏了 ``project_id`` 的话,同一张照片用于第二个项目时会命中第一个项目的行,
    于是 A 工地的整改记录挂到 B 工地头上 —— 而两边界面都显示"已登记",没有任何报错。
    """
    a = _register(project_id="gyt-a3", photo_sha256="同一张照片")
    b = _register(project_id="gyt-b7", photo_sha256="同一张照片")

    assert b.created is True
    assert b.row.hazard_no != a.row.hazard_no
    assert len(hazards.list_rows()) == 2
    assert {row.project_id for row in hazards.list_rows()} == {"gyt-a3", "gyt-b7"}


def test_幂等_同一张照片的多个违规项各自独立成行() -> None:
    """一张照片同时看出「未戴安全帽 + 临边无防护」是常态:两条隐患各有编号、各自整改。"""
    first = _register(photo_sha256="同一张照片", item="未戴安全帽")
    second = _register(photo_sha256="同一张照片", item="临边无防护")

    assert second.created is True
    assert second.row.hazard_no != first.row.hazard_no
    assert {row.item for row in hazards.list_rows()} == {"未戴安全帽", "临边无防护"}


def test_编号撞号不被幂等语句吞掉而是抛给调用方重试() -> None:
    """``ON CONFLICT`` 必须只认那个三元组。

    写成裸 ``ON CONFLICT DO NOTHING`` 的话,``hazard_no`` 撞号也会被静默吞掉,
    而 §6.3 明写"兜底靠 UNIQUE + **调用方撞库重试**"—— 吞掉之后调用方以为登记成功了,
    拿到的却是另一条隐患的编号。同秒批量签发时这是真会发生的。
    """
    first = _register()

    with pytest.raises(sqlite3.IntegrityError):
        _register(hazard_no=first.row.hazard_no, photo_sha256="另一张照片")


def test_注入串当违规项存取原样表安然无恙() -> None:
    """方案红线 4:值一律走 ? 占位。若有人改成拼接 SQL,这个词会当场把表炸掉。"""
    evil = "未戴安全帽'); DROP TABLE hazards;--"
    reg = _register(item=evil)

    row = hazards.fetch(reg.row.hazard_no)
    assert row is not None
    assert row.item == evil  # 原样进出,不许带转义痕迹

    assert _register().created is True  # 表还活着的证据


# --- 状态机:正路 -------------------------------------------------------------


def test_确认把待确认转正式并盖确认时刻() -> None:
    """D17 的那道闸:没有它,safety 看一眼照片就能开启法律流程。"""
    no = _register().row.hazard_no

    assert hazards.confirm(no) is True

    row = hazards.fetch(no)
    assert row is not None
    assert row.status == hazards.STATUS_OPEN
    assert row.confirmed_at is not None
    assert row.updated_at == row.confirmed_at  # 同一枚快照,不是各取一次 now


def test_一般隐患_通知单到复查合格直接销项() -> None:
    """grade=一般 这条路:没停过工,复查合格直接 closed,不该出现复工令那一站。"""
    no = _register().row.hazard_no
    hazards.confirm(no)

    assert (
        hazards.mark_notified(
            no,
            DUE,
            expected_grade=hazards.GRADE_NORMAL,
            docs=[hazards.DocDraft("notice", "GYT-TZ-0001", artifact_id="a" * 32)],
        )
        is True
    )
    notified = hazards.fetch(no)
    assert notified is not None
    assert notified.status == hazards.STATUS_NOTIFIED
    assert notified.due_date == DUE
    assert notified.was_suspended == 0

    landed = hazards.pass_reinspection(
        no, docs=[hazards.DocDraft("reinspect", "GYT-FC-0001", photo_id="after", result="pass")]
    )

    assert landed == hazards.STATUS_CLOSED
    closed = hazards.fetch(no)
    assert closed is not None
    assert closed.status == hazards.STATUS_CLOSED
    assert closed.closed_at is not None
    assert [doc.doc_type for doc in hazards.docs_of([no])] == ["notice", "reinspect"]


def test_严重隐患_停过工的复查合格必须先出复工令() -> None:
    """🔴 Codex#6 的守门断言。

    ``suspended`` 的行复查合格时**不能**直接 closed:那是漏发《工程复工令》——
    系统认为隐患销了、工地却还挂着一纸暂停令没撤。分支由 ``was_suspended`` 决定,
    不由调用方决定,所以这里连"直接销项"的那条函数都要断言它拒绝。
    """
    no = _register(grade=hazards.GRADE_SEVERE).row.hazard_no
    hazards.confirm(no)
    three = [
        hazards.DocDraft("notice", "GYT-TZ-0002", artifact_id="a" * 32),
        hazards.DocDraft("suspension", "GYT-ZT-0002", artifact_id="b" * 32),
        hazards.DocDraft("owner_report", "GYT-JS-0002", artifact_id="c" * 32),
    ]

    assert hazards.mark_suspended(no, DUE, docs=three, expected_grade=hazards.GRADE_SEVERE) is True
    suspended = hazards.fetch(no)
    assert suspended is not None
    assert suspended.status == hazards.STATUS_SUSPENDED
    assert suspended.was_suspended == 1
    # 三份文书一次原子产出(§6.4 ③):状态与文书在同一个事务里
    assert [doc.doc_type for doc in hazards.docs_of([no])] == [
        "notice",
        "suspension",
        "owner_report",
    ]

    assert hazards.close_after_pass(no) is False  # ← 直接销项这条路必须堵死

    assert hazards.pass_reinspection(no) == hazards.STATUS_RESUMING
    resuming = hazards.fetch(no)
    assert resuming is not None
    assert resuming.status == hazards.STATUS_RESUMING
    assert resuming.closed_at is None  # 还没销,只是在等复工令

    assert (
        hazards.mark_resumed(
            no, docs=[hazards.DocDraft("resumption", "GYT-FG-0002", artifact_id="d" * 32)]
        )
        is True
    )
    closed = hazards.fetch(no)
    assert closed is not None
    assert closed.status == hazards.STATUS_CLOSED
    assert closed.closed_at is not None


# ---------------------------------------------------------------------------
# 级别的写前守卫(2026-08-21)—— 端点的硬拦判的是快照,这两条守的是写的那一刻
# ---------------------------------------------------------------------------


def test_签通知单那一刻级别被抢改成严重_一行都不写() -> None:
    """🔴 「该停工的没停」那条路的守门断言。

    端点侧 ``_refuse_severe_notice`` 是拿**先读的那份快照**判的,而快照和 UPDATE
    之间隔着渲染三份 docx 的时间。这中间另一个请求完全可以把级别改掉
    —— ``_SET_GRADE_SQL`` 只要求 status 还在可定级档,而这时候它确实还是 open。

    不加守卫的结果:**严重隐患只拿到一份通知单**,而台账、界面、日志都不会说话。
    """
    no = _register(grade=hazards.GRADE_NORMAL).row.hazard_no
    hazards.confirm(no)
    decided = hazards.GRADE_NORMAL  # 端点按「一般」做的决定:只签一份通知单

    # 「另一个请求」抢在 UPDATE 之前把它改成严重
    assert hazards.set_grade(no, hazards.GRADE_SEVERE) is True

    moved = hazards.mark_notified(
        no,
        DUE,
        expected_grade=decided,
        docs=[hazards.DocDraft("notice", "GYT-TZ-抢级别", artifact_id="a" * 32)],
    )

    assert moved is False
    row = hazards.fetch(no)
    assert row is not None
    assert row.status == hazards.STATUS_OPEN  # 状态没动
    assert row.due_date is None  # 期限没写进去
    assert row.grade == hazards.GRADE_SEVERE  # 抢改的那次是生效的
    assert hazards.docs_of([no]) == []  # 文书没挂上 —— 整个事务一行没写


def test_签暂停令那一刻级别被抢改成一般_一行都不写() -> None:
    """反方向:「一般隐患签了暂停令」= 平白停一片人的工。

    ``was_suspended`` 尤其要断言:它一旦被置 1,此后即使复查合格也必须先出复工令
    (Codex#6)。守卫漏了的话,一条本来只该收张通知单的隐患会永远背着那面旗。
    """
    no = _register(grade=hazards.GRADE_SEVERE).row.hazard_no
    hazards.confirm(no)
    decided = hazards.GRADE_SEVERE  # 端点按「严重」做的决定:三份文书

    assert hazards.set_grade(no, hazards.GRADE_NORMAL) is True

    moved = hazards.mark_suspended(
        no,
        DUE,
        expected_grade=decided,
        docs=[
            hazards.DocDraft("notice", "GYT-TZ-抢级别2", artifact_id="a" * 32),
            hazards.DocDraft("suspension", "GYT-ZT-抢级别2", artifact_id="b" * 32),
            hazards.DocDraft("owner_report", "GYT-JS-抢级别2", artifact_id="c" * 32),
        ],
    )

    assert moved is False
    row = hazards.fetch(no)
    assert row is not None
    assert row.status == hazards.STATUS_OPEN
    assert row.was_suspended == 0  # 那面旗没被立起来
    assert hazards.docs_of([no]) == []


def test_级别没被动过时照常签得出去() -> None:
    """守卫不许把正常路径也拦掉 —— 上面两条只证明「抢改会被拦」,这条证明「不抢就通」。

    少了它,把 ``AND grade = ?`` 写成恒假(比如手抖写成 ``AND grade = ''``)
    照样两红一绿看不出来。
    """
    no = _register(grade=hazards.GRADE_NORMAL).row.hazard_no
    hazards.confirm(no)

    assert (
        hazards.mark_notified(
            no,
            DUE,
            expected_grade=hazards.GRADE_NORMAL,
            docs=[hazards.DocDraft("notice", "GYT-TZ-没人抢", artifact_id="a" * 32)],
        )
        is True
    )
    row = hazards.fetch(no)
    assert row is not None
    assert row.status == hazards.STATUS_NOTIFIED
    assert row.due_date == DUE


def test_复查不合格可以反复复查也可以升级() -> None:
    """``reinspect_failed`` 自环 = 再复查又不合格;升级只能从这里进 ——
    举证链是「通知过 + 期限到了 + 复查过 + 他没改」,没复查过就升级等于拿乱账指控施工方。"""
    no = _register().row.hazard_no
    hazards.confirm(no)
    hazards.mark_notified(no, DUE, expected_grade=hazards.GRADE_NORMAL)

    assert hazards.mark_reinspect_failed(no) is True
    assert hazards.mark_reinspect_failed(no) is True  # 再复查、又不合格

    assert (
        hazards.mark_escalated(
            no, docs=[hazards.DocDraft("authority_report", "GYT-JB-0003", artifact_id="e" * 32)]
        )
        is True
    )
    row = hazards.fetch(no)
    assert row is not None
    assert row.status == hazards.STATUS_ESCALATED


def test_不存在的编号所有迁移都返回False() -> None:
    """False 是"这个编号查不到"的唯一信号源:靠 rowcount 说话,不靠异常。"""
    assert hazards.confirm("GYT-H-没这个号") is False
    assert (
        hazards.mark_notified("GYT-H-没这个号", DUE, expected_grade=hazards.GRADE_NORMAL) is False
    )
    assert (
        hazards.mark_suspended("GYT-H-没这个号", DUE, expected_grade=hazards.GRADE_NORMAL) is False
    )
    assert hazards.mark_reinspect_failed("GYT-H-没这个号") is False
    assert hazards.pass_reinspection("GYT-H-没这个号") is None
    assert hazards.mark_resumed("GYT-H-没这个号") is False
    assert hazards.mark_escalated("GYT-H-没这个号") is False


# --- 状态机:补集 -------------------------------------------------------------


@pytest.mark.parametrize("was_suspended", [0, 1])
@pytest.mark.parametrize("src", hazards.STATUSES)
def test_状态机补集_非法迁移一律被拒且一个字节都不动(src: str, was_suspended: int) -> None:
    """八态 × 八态 = 64 格,合法的按表放行,其余全部 ``rowcount=0`` 且状态原样不动。

    (任务书说的 56 是 8×7 去掉自环;这里跑满 64 —— 本表有**一条合法自环**
    ``reinspect_failed → reinspect_failed``(再复查又不合格),去掉自环就漏测它,
    也漏测了另外七条"自己迁到自己"必须被拒。)

    两个 ``was_suspended`` 都跑:这一列是 Codex#6 分支的依据,只跑一个值等于
    只测了一半的状态机 —— 而漏发/滥发复工令正好各藏在一半里。
    """
    for dst in hazards.STATUSES:
        no = _register().row.hazard_no
        _force_status(no, src, was_suspended)

        landed = _attempt(no, dst)

        expected = _expected_ok(src, dst, was_suspended)
        assert landed is expected, f"{src} → {dst}(was_suspended={was_suspended})"
        row = hazards.fetch(no)
        assert row is not None
        assert row.status == (dst if expected else src)


def test_状态机表覆盖八个状态且两个终点没有出口() -> None:
    """键集漏一个状态的表现是 ``ALLOWED_TRANSITIONS[status]`` 直接 KeyError ——
    但只在那条罕见路径上炸,所以在这里先钉住。

    ``closed`` / ``escalated`` 的空集合不是"忘了填":隐患销项之后又冒出来,
    是**新的一条隐患**(新照片、新编号),不是把旧行改回去 —— 留档的证据链不许被回退改写。
    """
    assert set(hazards.ALLOWED_TRANSITIONS) == set(hazards.STATUSES)
    for targets in hazards.ALLOWED_TRANSITIONS.values():
        assert targets <= set(hazards.STATUSES)

    assert hazards.ALLOWED_TRANSITIONS[hazards.STATUS_CLOSED] == frozenset()
    assert hazards.ALLOWED_TRANSITIONS[hazards.STATUS_ESCALATED] == frozenset()
    # 🔴 这一条不许"顺手补上":停过工的必须先走 resuming 出复工令(Codex#6)
    assert hazards.STATUS_CLOSED not in hazards.ALLOWED_TRANSITIONS[hazards.STATUS_SUSPENDED]


def test_can_transition不认识的状态一律拒绝() -> None:
    """不认识就不放行 —— 上层拿到 False 该说人话,别把它当成"可能可以"。"""
    assert hazards.can_transition(hazards.STATUS_PENDING, hazards.STATUS_OPEN) is True
    assert hazards.can_transition(hazards.STATUS_PENDING, hazards.STATUS_CLOSED) is False
    assert hazards.can_transition("待复工", hazards.STATUS_CLOSED) is False
    assert hazards.can_transition(hazards.STATUS_OPEN, "已完成") is False


# --- 文书与证据链 -------------------------------------------------------------


def test_迁移被拒时一份文书都不写() -> None:
    """文书与状态在同一个事务里(§6.4 ③),且**只有迁移真的命中才写**。

    写了的话证据链里会出现一份没有状态支撑的《监理通知单》——
    而上报主管部门时,证据链就是全部举证材料。
    """
    no = _register().row.hazard_no  # 还是 pending,没确认过

    assert (
        hazards.mark_notified(
            no,
            DUE,
            expected_grade=hazards.GRADE_NORMAL,
            docs=[hazards.DocDraft("notice", "GYT-TZ-9999")],
        )
        is False
    )

    assert hazards.docs_of([no]) == []
    row = hazards.fetch(no)
    assert row is not None
    assert row.due_date is None  # 期限也没被顺手写进去


def test_证据链一次取回多条隐患的文书() -> None:
    """CLAUDE.md 红线:列表查询一次取回,禁止在循环里逐条查(汇总页天然是一屏一堆隐患)。"""
    first = _register().row.hazard_no
    second = _register().row.hazard_no
    for no, doc_no in ((first, "GYT-TZ-1001"), (second, "GYT-TZ-1002")):
        hazards.confirm(no)
        hazards.mark_notified(
            no,
            DUE,
            expected_grade=hazards.GRADE_NORMAL,
            docs=[hazards.DocDraft("notice", doc_no, artifact_id="a")],
        )
    hazards.mark_reinspect_failed(
        first, docs=[hazards.DocDraft("reinspect", "GYT-FC-1001", photo_id="p", result="fail")]
    )

    docs = hazards.docs_of([first, second])

    assert [(doc.hazard_no, doc.doc_type) for doc in docs] == [
        (first, "notice"),
        (first, "reinspect"),
        (second, "notice"),
    ]
    assert docs[1].result == "fail"
    assert docs[1].photo_id == "p"
    assert docs[0].artifact_id == "a"


def test_证据链空入参直接返回空表不打库() -> None:
    """``IN ()`` 是语法错误 —— 空列表必须在进 SQL 之前就短路掉。"""
    assert hazards.docs_of([]) == []


def test_外键真开着_文书挂到不存在的隐患上被拒() -> None:
    """``PRAGMA foreign_keys = ON`` 漏了的话 sqlite **默认不校验外键**,而且一声不吭:
    文书能挂在一个根本不存在的 ``hazard_no`` 上,要到上报主管部门那天才发现引不出隐患。

    这条测的是"本模块发出去的连接确实开着外键"(即 ``_hazard_db`` 有没有传
    ``foreign_keys=True``)。⚠️ 它测不出 PRAGMA 的**位置** —— 位置错(挪进事务里)才是
    no-op,而 Python 的 sqlite3 只在 DML 时才隐式开事务。PRAGMA 现在只有一处、在
    ``core/sqlite_util.open_db`` 里,位置靠那儿的注释与 review 守住
    (``test_sqlite_util.py`` 有两条用例把"为什么测不出来"与"那颗地雷是真的"钉死);
    这里守的是"有没有"。
    """
    _register()  # 触发幂等建表

    with pytest.raises(sqlite3.IntegrityError), hazards._hazard_db() as conn:
        conn.execute(
            "INSERT INTO hazard_docs (hazard_no, doc_type, doc_no, artifact_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("GYT-H-根本没这条", "notice", "GYT-TZ-8888", "a" * 32, OLD_STAMP),
        )


def test_复查结论的CHECK拦住第三种说法() -> None:
    """``result`` 只有 pass / fail 两种(D11:人下结论)。多出个 'maybe' 就是有人
    想把"模型看着像合格"塞进证据链 —— 库层直接拒。"""
    no = _register().row.hazard_no

    with pytest.raises(sqlite3.IntegrityError), hazards._hazard_db() as conn:
        conn.execute(
            "INSERT INTO hazard_docs (hazard_no, doc_type, doc_no, result, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (no, "reinspect", "GYT-FC-8888", "maybe", OLD_STAMP),
        )


def test_文书类型的CHECK拦住词表外的类型() -> None:
    """六种之外的 doc_type 一律拒:证据链里出现一份系统自己都不认识的"文书"最难查。"""
    no = _register().row.hazard_no

    with pytest.raises(sqlite3.IntegrityError), hazards._hazard_db() as conn:
        conn.execute(
            "INSERT INTO hazard_docs (hazard_no, doc_type, doc_no, created_at) VALUES (?, ?, ?, ?)",
            (no, "整改回复单", "GYT-XX-8888", OLD_STAMP),
        )


# --- 定级、否决、列表、失败落表 -------------------------------------------------


def test_状态的CHECK真的拦住脏状态() -> None:
    """约束只有 DDL 真写进库才有效:绕过存储层硬塞 status='待处理' 必须当场被拒,
    否则脏状态静默入库,要到 ``ALLOWED_TRANSITIONS[status]`` KeyError 时才爆雷。"""
    no = _register().row.hazard_no

    with closing(_raw_connect()) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE hazards SET status = ? WHERE hazard_no = ?", ("待处理", no))


def test_人工定级改级并清零needs_grading() -> None:
    """``needs_grading=1`` 的隐患唯一的解锁通道(Codex#11)。

    不清零的表现是:签发端点永远拒,监理在界面上点了"定级"却什么也没发生。
    """
    no = _register(severity="待定级", grade=hazards.GRADE_NORMAL, needs_grading=True).row.hazard_no
    before = hazards.fetch(no)
    assert before is not None
    assert before.needs_grading == 1

    assert hazards.set_grade(no, hazards.GRADE_SEVERE) is True

    row = hazards.fetch(no)
    assert row is not None
    assert row.grade == hazards.GRADE_SEVERE
    assert row.needs_grading == 0
    assert hazards.set_grade("GYT-H-没这个号", hazards.GRADE_SEVERE) is False


@pytest.mark.parametrize("status", hazards.STATUSES)
def test_定级只在签发前放行_其余状态一个字节都不动(status: str) -> None:
    """🔴 定级 UPDATE 的**写前状态守卫** —— 与每个迁移函数是同一件事,以前唯独它没有。

    端点那侧是「先读 status 判断能不能改 → 再 UPDATE」两步,中间有并发窗口:
    读到 ``open``(可以改级)的同时另一个请求把暂停令签了、三份文书已落盘、状态成了
    ``suspended``;这条 UPDATE 若不带守卫**照样命中** —— 库里就成了「一般隐患 + 已出暂停令」,
    而且一声不吭:台账与那张贴在工地上的纸从此永久矛盾,追责时谁也说不清哪份算数。

    合法前态按 ``GRADABLE_STATUSES`` 逐格断言,**不在测试里手抄第二份名单** ——
    抄了的话有人改常量时这条测试还会绿。
    """
    no = _register(severity="待定级", grade=hazards.GRADE_NORMAL, needs_grading=True).row.hazard_no
    _force_status(no, status, 0)

    changed = hazards.set_grade(no, hazards.GRADE_SEVERE)

    assert changed is (status in hazards.GRADABLE_STATUSES), status
    row = hazards.fetch(no)
    assert row is not None
    if changed:
        assert row.grade == hazards.GRADE_SEVERE
        assert row.needs_grading == 0
    else:
        # 两个见证:级别没改,旗子也没被顺手清掉 —— 清了的话签发那道闸就白开了
        assert row.grade == hazards.GRADE_NORMAL
        assert row.needs_grading == 1


def test_定级表外的级别撞CHECK且本层不吞异常() -> None:
    """约束由库兜底、异常向上抛(与 db/projects.py 同一职责边界):
    翻成中文人话是端点的事,这层吞了就没人知道写进去的是个野级别。"""
    no = _register().row.hazard_no

    with pytest.raises(sqlite3.IntegrityError):
        hazards.set_grade(no, "中等")


def test_否决只删得掉待确认的() -> None:
    """确认过的隐患不许被删:留档与证据链不能因为一次误点消失。"""
    kept = _register().row.hazard_no
    hazards.confirm(kept)
    dropped = _register().row.hazard_no

    assert hazards.delete_pending(dropped) is True
    assert hazards.delete_pending(kept) is False

    assert [row.hazard_no for row in hazards.list_rows()] == [kept]


def test_摘项目_只摘不删且证据链原样在() -> None:
    """🔴 项目被删时隐患的去处:摘成**未归属**(空串,D6),不是跟着项目消失。

    删掉的话就违了 ``delete_pending`` 那条底线(确认过的隐患不许被删,证据链不能凭一次
    点击消失 —— 而删项目正是一次点击);不摘的话它们的 ``project_id`` 指向一个不存在的
    项目(这张表**没有外键**),既不在未归属桶里也不在任何项目里,**彻底找不到**,
    而库里它们还是「在办」。
    """
    old = _register(project_id="").row.hazard_no  # 本来就在未归属那一堆
    other = _register(project_id="gyt-b7").row.hazard_no  # 别的工地,不许被误伤
    kept = _register(project_id="gyt-a3").row.hazard_no  # 已签文书的,证据链要原样在
    pending = _register(project_id="gyt-a3").row.hazard_no
    hazards.confirm(kept)
    hazards.mark_notified(
        kept,
        DUE,
        expected_grade=hazards.GRADE_NORMAL,
        docs=[hazards.DocDraft("notice", "GYT-TZ-7001", artifact_id="a")],
    )

    moved = hazards.detach_project("gyt-a3")

    assert moved == 2
    for no in (kept, pending):
        row = hazards.fetch(no)
        assert row is not None
        assert row.project_id == ""  # 摘到了未归属那一桶(可见、可继续处置)
    still = hazards.fetch(kept)
    assert still is not None
    assert still.status == hazards.STATUS_NOTIFIED  # 状态一点没动
    assert [doc.doc_no for doc in hazards.docs_of([kept])] == ["GYT-TZ-7001"]  # 文书还挂着
    # 未归属那一桶现在装得下它们(supervision 侧就是靠这条路显式报「未归属 N 条」)
    assert {row.hazard_no for row in hazards.list_rows(project_id="")} == {old, kept, pending}
    assert [row.hazard_no for row in hazards.list_rows(project_id="gyt-b7")] == [other]
    assert hazards.list_rows(project_id="gyt-a3") == []


def test_摘项目_空项目号与查无此项目都是无操作() -> None:
    """空串**不许**当项目号来摘:它本来就是未归属那一堆,真跑一遍等于把全部未归属隐患的
    ``updated_at`` 白刷一遍(改一列历史数据,而且没人会发现)。"""
    no = _register(project_id="").row.hazard_no
    with closing(_raw_connect()) as conn, conn:
        conn.execute("UPDATE hazards SET updated_at = ? WHERE hazard_no = ?", (OLD_STAMP, no))

    assert hazards.detach_project("") == 0
    assert hazards.detach_project("查无此项目") == 0

    row = hazards.fetch(no)
    assert row is not None
    assert row.updated_at == OLD_STAMP  # 一列都没被碰过


def test_摘项目撞幂等键时整批回滚且不吞异常() -> None:
    """同一张照片、同一个违规项,既在这个项目下登记过、又在未归属那堆里躺着一条 ——
    摘过去就撞 ``UNIQUE (project_id, photo_sha256, item)``。

    这时**一条都不许摘**(一个事务,异常回滚),异常照直抛给调用方:
    ``webapp.remove_project`` 接住它、拒绝删除整个项目。吞掉的话会摘掉一半、
    剩下的成了找不到的孤儿,而界面上显示"项目已删除"。
    """
    撞车 = _register(project_id="gyt-a3", photo_sha256="同一张照片", item="未戴安全帽").row
    _register(project_id="", photo_sha256="同一张照片", item="未戴安全帽")
    同伴 = _register(project_id="gyt-a3", photo_sha256="另一张照片").row

    with pytest.raises(sqlite3.IntegrityError):
        hazards.detach_project("gyt-a3")

    # 整批回滚:连没撞车的那条也还挂在原项目上
    assert {row.hazard_no for row in hazards.list_rows(project_id="gyt-a3")} == {
        撞车.hazard_no,
        同伴.hazard_no,
    }


def test_列表一次取全_有期限在前无期限垫底() -> None:
    """排序是"哪条最急"的地基:无期限(还没签发的)垫底但**不丢**,同期限按登记先后稳定。"""
    no_due = _register().row.hazard_no
    late = _register().row.hazard_no
    early = _register().row.hazard_no
    for no in (late, early):
        hazards.confirm(no)
    hazards.mark_notified(late, "2026-08-25", expected_grade=hazards.GRADE_NORMAL)
    hazards.mark_notified(early, "2026-08-18", expected_grade=hazards.GRADE_NORMAL)

    got = [row.hazard_no for row in hazards.list_rows()]

    assert got == [early, late, no_due]


def test_列表按项目与状态筛_未归属是空串不是None() -> None:
    """``project_id=None`` = 不筛项目;``project_id=""`` = 只看**未归属**的(D6)。
    两者语义完全不同 —— 混用的表现是"未归属 N 条"这个数字要么恒等于全量、要么恒为 0。"""
    homeless = _register(project_id="").row.hazard_no
    owned = _register(project_id="gyt-a3").row.hazard_no
    hazards.confirm(owned)

    assert [row.hazard_no for row in hazards.list_rows(project_id="")] == [homeless]
    assert [row.hazard_no for row in hazards.list_rows(project_id="gyt-a3")] == [owned]
    assert len(hazards.list_rows()) == 2

    pending_only = hazards.list_rows(statuses=[hazards.STATUS_PENDING])
    assert [row.hazard_no for row in pending_only] == [homeless]
    two_states = hazards.list_rows(statuses=[hazards.STATUS_PENDING, hazards.STATUS_OPEN])
    assert len(two_states) == 2
    assert hazards.list_rows(statuses=[hazards.STATUS_CLOSED]) == []


def test_登记失败落表可事后统计() -> None:
    """Codex#10:只打日志的话进程一重启就统计不出来了,而这类失败恰恰是"隐患漏记"的唯一线索。"""
    hazards.record_ingest_failure(
        project_id="gyt-a3", photo_id="photo-x", item="临边无防护", reason="IntegrityError: 撞号"
    )
    hazards.record_ingest_failure(
        project_id="", photo_id="photo-y", item="用电隐患", reason="database is locked"
    )

    all_rows = hazards.list_ingest_failures()
    assert [row.item for row in all_rows] == ["临边无防护", "用电隐患"]
    assert all_rows[0].project_id == "gyt-a3"
    assert all_rows[0].created_at.endswith("+08:00")

    assert [row.photo_id for row in hazards.list_ingest_failures(project_id="")] == ["photo-y"]


# ---------------------------------------------------------------------------
# 签发人留痕 issued_by(2026-08-21)—— 在它之前,「这份暂停令是谁签的」无解
# ---------------------------------------------------------------------------


def test_文书能记下签发人_而复查记录也一样() -> None:
    """整条证据链上每一个「人做的动作」都该留下是谁做的。

    复查那条尤其要有:「复查合格」是离销项最近的一步,由人下结论(D11),
    而它不出文书、没有编号 —— 在这一列之前,它是全链唯一一个连时间以外
    什么都不记的动作。
    """
    no = _register().row.hazard_no
    hazards.confirm(no)
    hazards.mark_notified(
        no,
        DUE,
        expected_grade=hazards.GRADE_NORMAL,
        docs=[hazards.DocDraft("notice", "GYT-TZ-记名", artifact_id="a" * 32, issued_by="陳大文")],
    )
    hazards.mark_reinspect_failed(
        no,
        docs=[
            hazards.DocDraft(
                "reinspect", "GYT-FC-记名", photo_id="p", result="fail", issued_by="李四"
            )
        ],
    )

    got = {d.doc_no: d.issued_by for d in hazards.docs_of([no])}
    assert got == {"GYT-TZ-记名": "陳大文", "GYT-FC-记名": "李四"}


def test_不报名字也签得出去_记成空() -> None:
    """🔴 **故意允许为空**,别改成必填。

    必填的后果很难看:界面上没填名字 → 拒绝 → 而这时候文书**已经渲染落盘了**
    (``_sign`` 那条孤儿文件)。为一个补充性的审计字段挡住法律文书的签发,不划算。
    留 NULL 也比编一个名字诚实 —— 查的人一眼看得出「这条没记到人」。
    """
    no = _register().row.hazard_no
    hazards.confirm(no)
    hazards.mark_notified(
        no,
        DUE,
        expected_grade=hazards.GRADE_NORMAL,
        docs=[hazards.DocDraft("notice", "GYT-TZ-无名", artifact_id="a" * 32)],
    )

    assert [d.issued_by for d in hazards.docs_of([no])] == [None]


def test_旧库缺这一列时进场自动补上_且旧行原样保留() -> None:
    """幂等补列的守门断言(姿势照搬 db/tasks.py 的 _migrate)。

    线上那张 ``hazard_docs`` 是 2026-08-21 之前建的,没有这一列。补列必须:
      ① 真的加上;② **不动旧行**(它们的 issued_by 就该是 NULL);
      ③ 加在**末尾** —— ALTER TABLE 只能追加,DDL 也写末尾,
        新建的库与迁移过来的旧库物理列序才一致。

    做旧的手法是 ``ALTER TABLE … DROP COLUMN``(sqlite 3.35+),
    比手搓一张旧表可靠:它保证除了这一列之外**其余部分与真库逐字节同构**。
    """
    no = _register().row.hazard_no
    hazards.confirm(no)
    hazards.mark_notified(
        no,
        DUE,
        expected_grade=hazards.GRADE_NORMAL,
        docs=[
            hazards.DocDraft("notice", "GYT-TZ-旧行", artifact_id="a" * 32, issued_by="会被抹掉")
        ],
    )

    with closing(_raw_connect()) as conn, conn:
        conn.execute("ALTER TABLE hazard_docs DROP COLUMN issued_by")
    assert "issued_by" not in _table_columns("hazard_docs")

    hazards.list_rows()  # 任意一次操作都会在进场处跑 _migrate

    columns = _table_columns("hazard_docs")
    assert columns[-1] == "issued_by", "新列必须在最末 —— ALTER TABLE 只能追加"
    assert columns == list(hazards.HazardDocRow._fields)
    docs = hazards.docs_of([no])
    assert [(d.doc_no, d.issued_by) for d in docs] == [("GYT-TZ-旧行", None)]
