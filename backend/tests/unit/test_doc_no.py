"""文书编号生成器的单元测试(W9 S2)—— 全程不联网、不碰库。

覆盖率不是重点,这里钉的是四件「坏了不报错」的事:

  · **时间从哪来。** 有人图省事改回 ``datetime.now(UTC).astimezone()``,在同为
    UTC+8 的本机跑测试全绿,换个时区的机器上编号就错日期(方案 §6.3)。
  · **六种正则两两不串,且与 report / attendance 那两套也不串。** 拿错正则挂
    守卫的后果是**一个编号都认不出、静默全放行** —— 这是 CLAUDE.md 同源清单
    里点名的守门断言,test_checkin_api.py 已经为 attendance↔report 那一对
    钉过一次,这里把新增的六种补齐。
  · **撞库重试真的会重试**,而且重试只换随机尾、不让编号里的时刻往后飘。
  · **``exists`` 抛异常不许被吞成「撞了」** —— 吞了以后报的是「编号都被占了」,
    排查的人会去翻编号表,而真正的毛病在库连接上。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from gyt.agents.report import REPORT_RECEIPT_PATTERN
from gyt.attendance import receipt
from gyt.core import doc_no
from gyt.core.doc_no import DocKind, DocNoExhaustedError

# 香港 08:30 那一瞬(与 test_checkin_api.py 用同一个锚点,方便对读)。
UTC_0030 = datetime(2026, 8, 15, 0, 30, 0, tzinfo=UTC)
SNAP = receipt.snapshot_at(UTC_0030)

ALL_KINDS = tuple(DocKind)


class Test编号长相:
    @pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.name)
    def test_每种编号都被自己那条正则fullmatch(self, kind: DocKind) -> None:
        """生成端与正则端的同源闸。

        两边都由 ``kind.value`` 派生,所以正常情况下不可能对不上;这条钉的是
        「将来有人手抄一份正则过去」—— 那一刻起格式与守卫就各走各的,而线上
        表现是守卫静默失效,没有任何报错。
        """
        no = doc_no.new_doc_no(kind, SNAP)
        assert re.fullmatch(doc_no.PATTERNS[kind], no), no

    @pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.name)
    def test_日期时刻段是香港时间(self, kind: DocKind) -> None:
        """UTC 00:30 → 香港 08:30。落成 0030 就说明有人拿 UTC 直接格式化了。"""
        assert doc_no.new_doc_no(kind, SNAP).startswith(f"GYT-{kind.value}-20260815-083000-")

    def test_同一快照多次生成随机尾不同(self) -> None:
        """随机尾是撞库重试的前提:重试 = 拿同一个快照再摇一次,
        若两次必然相同,重试就是原地打转。8 连抽全同的概率是 (1/65536)^7。"""
        assert len({doc_no.new_doc_no(DocKind.NOTICE, SNAP) for _ in range(8)}) > 1

    def test_六种种类各自的类型段互不相同(self) -> None:
        """类型段撞了就等于两种文书共用一套编号,库里的 UNIQUE 也拦不住 ——
        它们本来就该是不同的号段。"""
        assert len({kind.value for kind in ALL_KINDS}) == len(ALL_KINDS)


class Test时间权威:
    def test_不传快照时走的是香港时间权威(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**这条是「时间从哪来」的钉子。**

        把权威的 ``make_snapshot`` 换成固定快照,编号必须跟着变。哪天有人在
        doc_no 里自己调 ``datetime.now(...)``,这条当场红 —— 而如果只断言
        「编号里的日期是今天」,在 UTC+8 的本机上永远绿。
        """
        monkeypatch.setattr(doc_no, "make_snapshot", lambda: SNAP)
        assert doc_no.new_doc_no(DocKind.SUSPENSION).startswith("GYT-ZT-20260815-083000-")
        assert doc_no.new_report_no() == "GYT-20260815-083000"

    def test_巡检记录编号也走同一个权威(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """report 那一档是异形(没类型段没随机尾),但时间来路必须和别人一样。"""
        monkeypatch.setattr(doc_no, "make_snapshot", lambda: receipt.snapshot_at(UTC_0030))
        assert doc_no.new_report_no() == doc_no.new_report_no(SNAP)


class Test正则互不匹配:
    """守门断言组。

    统一用 ``re.search``(不锚定)而不是 ``fullmatch`` —— 因为真实的守卫就是
    这么用的(``RequireReceiptSource`` 在模型的一整段话里 search 编号)。
    用 fullmatch 来测会漏掉「A 类编号里**包含**一段能被 B 类正则搜到的子串」
    这种串法,而那才是线上真会出事的形态。
    """

    @pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.name)
    def test_一种编号只被自己那条认出来(self, kind: DocKind) -> None:
        no = doc_no.new_doc_no(kind, SNAP)
        for other in ALL_KINDS:
            matched = re.search(doc_no.PATTERNS[other], no) is not None
            assert matched is (other is kind), (
                f"{kind.name} 的编号 {no} 被 {other.name} 的正则认走了"
            )

    @pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.name)
    def test_六种都不被巡检记录的守卫正则认走(self, kind: DocKind) -> None:
        """``REPORT_RECEIPT_PATTERN`` 要求 ``GYT-`` 后紧跟 8 位数字,而这六种
        紧跟的是字母类型段。串了的后果:监理文书编号会被 report 的守卫当成
        「巡检记录编号」去找出处,找不到就把 report 的正常回答顶替掉。"""
        assert re.search(REPORT_RECEIPT_PATTERN, doc_no.new_doc_no(kind, SNAP)) is None

    @pytest.mark.parametrize("kind", ALL_KINDS, ids=lambda k: k.name)
    def test_六种都不被考勤凭证正则认走(self, kind: DocKind) -> None:
        """考勤是 ``GYT-A-…``,和这六种同为「字母类型段」形状,是最容易串的一组。"""
        attendance_pattern = r"GYT-A-\d{8}-\d{6}-[0-9a-f]{4}"
        assert re.search(attendance_pattern, doc_no.new_doc_no(kind, SNAP)) is None

    def test_巡检记录编号不被任何一种文书正则认走(self) -> None:
        """反向:``GYT-20260815-083000`` 没有类型段,六条正则都该认不出。"""
        no = doc_no.new_report_no(SNAP)
        for kind in ALL_KINDS:
            assert re.search(doc_no.PATTERNS[kind], no) is None, kind.name

    def test_考勤凭证编号不被任何一种文书正则认走(self) -> None:
        no = receipt.new_receipt_no(SNAP)
        for kind in ALL_KINDS:
            assert re.search(doc_no.PATTERNS[kind], no) is None, kind.name

    def test_巡检记录编号仍然被它自己的守卫正则认得出(self) -> None:
        """W9 S2 把生成端搬进了 doc_no,格式**必须**原封不动 —— 加个类型段或
        随机尾都会让 ``REPORT_RECEIPT_PATTERN`` 认不出编号,守卫从此静默全放行,
        而 test_report.py 只断 ``startswith("GYT-")``,那种改法测试照绿。"""
        assert re.search(REPORT_RECEIPT_PATTERN, doc_no.new_report_no(SNAP)) is not None


class Test撞库重试:
    def test_没撞上时只问一次库(self) -> None:
        asked: list[str] = []

        def exists(no: str) -> bool:
            asked.append(no)
            return False

        got = doc_no.generate_unique(DocKind.NOTICE, exists, snap=SNAP)
        assert asked == [got]

    def test_前两次撞上时换随机尾接着摇(self) -> None:
        asked: list[str] = []

        def exists(no: str) -> bool:
            asked.append(no)
            return len(asked) <= 2

        got = doc_no.generate_unique(DocKind.NOTICE, exists, snap=SNAP)
        assert len(asked) == 3 and asked[-1] == got
        assert len(set(asked)) == 3, "重试摇出了一模一样的号 —— 那是原地打转不是重试"

    def test_重试只换随机尾而不让时刻往后飘(self) -> None:
        """**整轮重试共用一个快照**(receipt.py:78-79 的做法)。

        若每次重新取 now,编号里的时刻会随重试往后爬 —— 而那个时刻要写进文书
        正文,爬过之后就再也对不回真正的签发瞬间了。
        """
        asked: list[str] = []

        def exists(no: str) -> bool:
            asked.append(no)
            return len(asked) < 4

        doc_no.generate_unique(DocKind.SUSPENSION, exists, snap=SNAP, attempts=4)
        heads = {no.rsplit("-", 1)[0] for no in asked}
        assert heads == {"GYT-ZT-20260815-083000"}, heads

    def test_不传快照时整轮重试也共用一个快照(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """快照在循环外取一次,不是每次重试都取 —— 数一下权威被调了几次。"""
        calls: list[int] = []

        def fake_snapshot() -> receipt.TimeSnapshot:
            calls.append(1)
            return SNAP

        monkeypatch.setattr(doc_no, "make_snapshot", fake_snapshot)
        asked: list[str] = []

        def exists(no: str) -> bool:
            asked.append(no)
            return len(asked) < 3

        doc_no.generate_unique(DocKind.RESUMPTION, exists)
        assert len(calls) == 1, "每次重试都重新取时间了 —— 编号里的时刻会跟着往后飘"

    def test_摇满了还撞就抛异常而不是硬返回(self) -> None:
        """**不许降级成「用最后那个撞了的号继续」。** 两份不同的文书顶着同一个
        编号,比这次没出成严重得多 —— 后者工友看得见、重试得了。"""
        with pytest.raises(DocNoExhaustedError) as caught:
            doc_no.generate_unique(DocKind.OWNER_REPORT, lambda _no: True, attempts=3, snap=SNAP)
        assert "3" in str(caught.value)
        assert "致建设单位报告" in str(caught.value), "得说清是哪种文书没出成"

    def test_摇满的报错是中文人话(self) -> None:
        """这句话有可能被端点原样当成 user_msg 递出去(哪怕现在还没有),
        所以先钉住:没有类名、没有英文术语,只有工地上听得懂的中文。"""
        with pytest.raises(DocNoExhaustedError) as caught:
            doc_no.generate_unique(
                DocKind.AUTHORITY_REPORT, lambda _no: True, attempts=2, snap=SNAP
            )
        assert re.search(r"[A-Za-z]", str(caught.value)) is None, str(caught.value)

    def test_查库自己炸了要原样冒泡不许当成撞库(self) -> None:
        """吞成「撞了」会白白烧掉重试次数,最后报一个「编号都被占了」——
        排查的人去翻编号表,而真正的毛病在库连接上。"""
        calls: list[str] = []

        def exists(no: str) -> bool:
            calls.append(no)
            raise RuntimeError("数据库连接断了")

        with pytest.raises(RuntimeError, match="数据库连接断了"):
            doc_no.generate_unique(DocKind.HAZARD, exists, snap=SNAP)
        assert len(calls) == 1, "库炸了还接着重试,等于把一次可诊断的失败拖成 N 次"

    def test_attempts小于一直接拒绝(self) -> None:
        with pytest.raises(ValueError, match="attempts"):
            doc_no.generate_unique(DocKind.NOTICE, lambda _no: False, attempts=0)

    def test_返回的号是没被占用的那个(self) -> None:
        taken = {doc_no.new_doc_no(DocKind.NOTICE, SNAP) for _ in range(3)}
        got = doc_no.generate_unique(DocKind.NOTICE, lambda no: no in taken, snap=SNAP)
        assert got not in taken


class Test受控词表同源:
    def test_每种种类都有中文名(self) -> None:
        """漏一个的症状:报错话术里出现 ``DocKind.XXX`` 这种内部词,或者直接
        KeyError —— 而它只在「编号摇满了」这条冷路径上触发。"""
        assert set(doc_no.DOC_TITLE_ZH) == set(ALL_KINDS)
        assert all(name.strip() for name in doc_no.DOC_TITLE_ZH.values())

    def test_每种种类都有正则(self) -> None:
        assert set(doc_no.PATTERNS) == set(ALL_KINDS)

    def test_成员名对齐hazard_docs的doc_type词表(self) -> None:
        """方案 §4.1 的 CHECK 约束写死了 doc_type 的取值。枚举成员名小写之后
        必须落在那张表里(``hazard``、``reinspect`` 两个例外见下),这样 S4 写
        端点时 ``kind.name.lower()`` 就是 doc_type,不用再发明第二套名字。

        这里刻意抄一份字面量而不是 import ``db.hazards`` —— 那个模块是并行泳道
        S1 在写的,测试不该跟着它的进度红绿摇摆;字面量的真相源是方案 §4.1。
        """
        doc_types = {
            "notice",
            "suspension",
            "resumption",
            "owner_report",
            "authority_report",
            "reinspect",
        }
        names = {kind.name.lower() for kind in ALL_KINDS}
        # hazard 是隐患本身(进 hazards 表),不是挂在 hazard_docs 下的一份文书。
        assert names - {"hazard"} <= doc_types, names - {"hazard"} - doc_types
        # reinspect(复查行)在 §6.3 的编号表里没有分配类型段,所以这边没有它 ——
        # 真要给复查行编号,回 DocKind 加一档,别在调用点就地拼字符串。
        assert doc_types - names == {"reinspect"}
