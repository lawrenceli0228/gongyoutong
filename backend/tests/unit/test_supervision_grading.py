"""agents/supervision/grading.py(severity → grade 二次映射)的单元测试 —— 纯函数,不碰库、不联网。

这层要钉死的静默错误:
  · **漏配一档** —— safety 那边加/改一档而这张表没跟,新档静默落到默认值「一般」上,
    表现是**重大隐患按一般隐患签了通知单**:法条要求的停工那条路根本不会被触发,
    而报表上一切正常、没有任何报错;
  · **``needs_grading`` 举旗举错** —— 该举不举,未知风险就能按一般隐患走完整闭环并被销项
    (Codex#11 点名的失效);不该举乱举,则每条隐患都要人先定级一遍,监理会绕过这套系统;
  · **改了表却没 +1 版本号** —— 老隐患的定级依据被新表悄悄改写,事后对不上留档文书。
"""

from __future__ import annotations

from gyt.agents.safety.severity import (
    SEVERITY_BY_VIOLATION,
    SEVERITY_MAJOR,
    SEVERITY_MEDIUM,
    SEVERITY_MINOR,
    SEVERITY_ORDER,
    SEVERITY_PENDING,
)
from gyt.agents.supervision.grading import (
    GRADE_BY_SEVERITY,
    GRADING_VERSION,
    grade_of,
)
from gyt.db.hazards import GRADE_NORMAL, GRADE_SEVERE, GRADES


def test_定级表与版本号一起钉死() -> None:
    """整张表逐档写死在这里。

    **改表的人必须同时改这条断言和 GRADING_VERSION**(D4:改表必须 +1)——
    版本号是入库快照,没跟着改的话,新老隐患在库里都标着同一个版本,
    事后"这条当时按哪版规则定的严重"就再也答不出来了。
    """
    assert GRADE_BY_SEVERITY == {
        SEVERITY_MAJOR: GRADE_SEVERE,  # 高处坠落、动火失控 —— 停工那条路
        SEVERITY_MEDIUM: GRADE_SEVERE,  # 临边/用电/消防通道 —— 系统性缺陷,也停工
        SEVERITY_MINOR: GRADE_NORMAL,  # 个人防护单项缺失 —— 只签通知单
        SEVERITY_PENDING: GRADE_NORMAL,  # 不知道 ≠ 不严重 → 配 needs_grading
    }
    assert GRADING_VERSION == "1"


def test_定级表键集与safety的四档完全同步() -> None:
    """safety 加一档而这里漏配 = 新档静默走「一般」通道,提前在单测里炸掉。

    比的是 ``SEVERITY_ORDER``(severity 的**完整值域**,四档全在),
    不是 ``SEVERITY_BY_VIOLATION.values()`` —— 后者只有三档,「待定级」是词表外的默认值、
    不作为字典的 value 出现,拿它当基准会让「待定级」这一档漏配时测试照绿。
    两个都断:值域相等 + 受控词表能产出的每一档都在表里。
    """
    assert set(GRADE_BY_SEVERITY) == set(SEVERITY_ORDER)
    assert set(SEVERITY_BY_VIOLATION.values()) <= set(GRADE_BY_SEVERITY)


def test_产出的grade一定过得了hazards表的CHECK() -> None:
    """两档都用到、且不多不少 —— ``GRADES`` 就是 hazards 表 grade CHECK 子句的来源。

    本模块从 ``db.hazards`` import 这两个字符串(而不是另抄字面量),所以这条测试
    真正防的是"表里出现第三个 grade":那种行会在入库时撞 CHECK,而错在这张表。
    """
    assert set(GRADE_BY_SEVERITY.values()) == set(GRADES)


def test_四档各自映射成什么() -> None:
    assert grade_of(SEVERITY_MAJOR).grade == GRADE_SEVERE
    assert grade_of(SEVERITY_MEDIUM).grade == GRADE_SEVERE
    assert grade_of(SEVERITY_MINOR).grade == GRADE_NORMAL
    assert grade_of(SEVERITY_PENDING).grade == GRADE_NORMAL


def test_只有待定级要举人工定级的旗() -> None:
    """举旗的判据必须精确:该举不举,未知风险就能按一般隐患走完闭环并被销项(Codex#11);
    不该举乱举,监理每条都得先手工定级一遍,然后就绕过这套系统了。"""
    assert grade_of(SEVERITY_PENDING).needs_grading is True

    for severity in (SEVERITY_MAJOR, SEVERITY_MEDIUM, SEVERITY_MINOR):
        assert grade_of(severity).needs_grading is False, severity


def test_未知severity按待定级处理而不是瞎猜() -> None:
    """severity 只该来自 safety 的四档,但真出现别的值时(改名漏改、别处塞进来),
    不许静默当成一般隐患放行 —— 走一般那条路但举旗,等人来定。"""
    unknown = grade_of("特别重大")

    assert unknown.grade == GRADE_NORMAL
    assert unknown.needs_grading is True


def test_每次定级都带上版本号快照() -> None:
    """三个值要一起进库:版本号丢了,事后说不清当时按哪版表定的级。"""
    assert grade_of(SEVERITY_MAJOR).version == GRADING_VERSION
    assert grade_of("词表外的东西").version == GRADING_VERSION
