"""隐患定级的第二跳 —— 从 safety 的四档 severity 到监理口径的二档 grade(W9 定案 D4)。

===========================================================================
为什么要二次映射,而不是让 safety 直接给「一般/严重」
---------------------------------------------------------------------------
两套级别回答的是两个问题,不该合成一个:

    severity(重大/较大/一般/待定级)   「这事有多危险」—— 安全管理口径,
                                        ``agents/safety/severity.py`` 已经定死。
    grade(一般/严重)                  「按法条该走哪条路」—— 监理法定职责口径。
                                        它**决定分岔**:一般 → 只签《监理通知单》;
                                        严重 → 《通知单》+《工程暂停令》+《致建设单位报告》。

合成一个的后果是:哪天安全员想把「材料堆放混乱」调成较大,就会顺带把它推上停工那条路 ——
两个口径必须能各自调整。分两跳之后,调 severity 只动危险度,调这张表才动法律路径。

写死成表(而不是让 LLM 判)的理由与 severity.py 逐条相同:可解释(「表里定的」)、
可校准(改一处生效,不用动提示词、不作废视觉缓存)、可测试(纯函数 + 单测锁死)。
**更硬的一条:签发暂停令是法律行为,不能由概率性系统单方面决定走哪条路。**

===========================================================================
⚠️ 这张表是初版,上线前应由**总监理工程师**过目校准
---------------------------------------------------------------------------
排法(方案 §4.3):

  重大 → 严重   高处坠落、动火失控 —— 直接威胁生命,应停工处理。
  较大 → 严重   临边洞口、用电、消防通道 —— 系统性缺陷,影响的是一片人。
  一般 → 一般   个人防护单项缺失,单次危害有限。
  待定级 → 一般 + ``needs_grading=1``
                **不知道 ≠ 不严重。** 词表外的违规项本来就刻意原样透传(那是提示词
                失守的诊断信号),这里既不敢当成严重(会平白停工),也不能当成
                普通的一般隐患放它走完闭环 —— 所以配一面旗子,签发前必须由人定级。

🔴 ``needs_grading=1`` 时所有签发端点硬拒(Codex#11)。只标注不拦截的话,
未知风险可以按一般隐患走完整闭环并被销项 —— 那正是这套系统最不该发生的事。
解锁的唯一通道是 ``db.hazards.set_grade()``(人在界面上定级,同时清零这面旗子)。
"""

from __future__ import annotations

from typing import Final, NamedTuple

from gyt.agents.safety.severity import (
    SEVERITY_MAJOR,
    SEVERITY_MEDIUM,
    SEVERITY_MINOR,
    SEVERITY_PENDING,
)

# grade 的两个字符串取自 db 层 —— 那里是 hazards 表 CHECK 子句的来源。
# import 方向 agents → db 是合法的(反过来不是),这么写的收益是:
# **本模块结构上不可能产出一个过不了 CHECK 的 grade**。别在这儿另抄两个字面量。
from gyt.db.hazards import GRADE_NORMAL, GRADE_SEVERE

GRADING_VERSION: Final[str] = "1"
"""定级规则版本。**改了下面那张表就必须 +1**(单测 ``test_定级表与版本号一起钉死`` 盯着)。

每条隐患入库时快照这个值(``hazards.grading_version``),这样事后能回答
「这条当时是按哪一版规则定的严重」—— 校准过一次表之后,老隐患的定级依据不会被
新表悄悄改写,而留档文书上写的正是当时那个级别。
"""

GRADE_BY_SEVERITY: Final[dict[str, str]] = {
    SEVERITY_MAJOR: GRADE_SEVERE,
    SEVERITY_MEDIUM: GRADE_SEVERE,
    SEVERITY_MINOR: GRADE_NORMAL,
    SEVERITY_PENDING: GRADE_NORMAL,  # 配 needs_grading=1,见 needs_grading_for()
}
"""severity → grade。

**键集必须与 ``safety/severity.py`` 的 SEVERITY_ORDER 完全相等**,
测试 ``test_定级表键集与safety的四档完全同步`` 盯着 —— safety 那边加一档而这里漏配,
表现是新档静默落到 ``.get(..., 一般)`` 的默认值上,也就是**重大隐患按一般隐患签了通知单**,
报表上一切正常,没有任何报错。
"""

_NEEDS_HUMAN_GRADING: Final[frozenset[str]] = frozenset({SEVERITY_PENDING})
"""哪些 severity 必须由人再定一次级。

只有「待定级」一个 —— 但**未知的 severity 也按这一档处理**(见 grade_of):
不认识的东西一律当成"不知道",不猜。
"""


class Grading(NamedTuple):
    """一次定级的全部结果 —— 三个值一起进库,不许只取其一。

    ``needs_grading`` 单独丢掉的表现是:未定级的隐患畅通无阻地走完闭环;
    ``version`` 单独丢掉的表现是:事后说不清当时按哪版表定的级。
    """

    grade: str  # GRADES 之一(db.hazards 的 CHECK 认得)
    needs_grading: bool  # True = 签发前必须由人定级(Codex#11)
    version: str  # GRADING_VERSION 的快照,直接写进 hazards.grading_version


def grade_of(severity: str) -> Grading:
    """把 safety 的 severity 映射成监理口径。纯函数,不碰库、不碰模型。

    未知的 severity(既不是四档之一,也不是空)按「待定级」处理:
    走 ``一般`` 那条路但举旗要求人工定级 —— **不猜、也不静默放行**。
    这与 severity.py 对词表外违规项的处理是同一个原则。
    """
    return Grading(
        grade=GRADE_BY_SEVERITY.get(severity, GRADE_NORMAL),
        needs_grading=severity not in GRADE_BY_SEVERITY or severity in _NEEDS_HUMAN_GRADING,
        version=GRADING_VERSION,
    )


__all__ = ["GRADE_BY_SEVERITY", "GRADING_VERSION", "Grading", "grade_of"]
