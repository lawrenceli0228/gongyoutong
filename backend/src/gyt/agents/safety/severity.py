"""隐患定级 —— 从违规项到严重级别的**确定性**映射。

===========================================================================
为什么定级放在代码里,而不是让视觉模型顺嘴给
---------------------------------------------------------------------------
产品定位是「初筛与记录」:模型负责回答**看见了什么**(事实问题),
级别负责回答**这事有多急**(管理口径)。后者不是视觉问题 ——
同一条「未戴安全帽」在任何照片里都是同一个级别,让模型每张现想一遍,
只会引入随机性,还没法向安全员解释"为什么昨天判一般、今天判较大"。

写死成表的三个好处:
  · 可解释:安全员问起来,答案是「表里定的」,不是「模型觉得」;
  · 可校准:安全员不认可就改这张表,一处生效 —— 不用动提示词
    (动提示词会让整份视觉缓存作废,演示预热全废,见 TODO-11);
  · 可测试:纯函数,单测直接锁死,还有一条测试盯着它与受控词表完全同步。

===========================================================================
⚠️ 这张表是初版,上线前应由持证安全员过目校准
---------------------------------------------------------------------------
排法按事故类型的普遍致害程度:

  重大 —— 直接威胁生命的两类:高处坠落是建筑业头号致死事故类型;
          动火失控直指火灾爆炸。看到就该停工处理。
  较大 —— 系统性缺陷,影响的是一片人而不是一个人:临边洞口、用电、消防通道。
  一般 —— 个人防护单项缺失,单次危害有限。
          (同一问题反复出现该不该升级,是安全员的判断,不写进代码。)

「该不该要求」由安全员定的原则同样适用于级别:这里给的是**默认值**。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

SEVERITY_MAJOR: Final[str] = "重大"
SEVERITY_MEDIUM: Final[str] = "较大"
SEVERITY_MINOR: Final[str] = "一般"
SEVERITY_PENDING: Final[str] = "待定级"
"""模型吐出受控词表以外的词时的级别。

不猜、不隐藏:词表外的违规项本来就**刻意原样透传**(见 tools.py 顶部①,
那是提示词失守的诊断信号),给它随便扣一个级别等于把信号抹掉一半。
"""

SEVERITY_ORDER: Final[tuple[str, ...]] = (
    SEVERITY_MAJOR,
    SEVERITY_MEDIUM,
    SEVERITY_MINOR,
    SEVERITY_PENDING,
)
"""从重到轻。worst() 按这个顺序取最前面命中的一档。

待定级排在最末:它表达的是「不知道」,不是「很严重」——
但 worst() 对「只有待定级」的场合仍会返回它,不会静默说成一般。
"""

SEVERITY_BY_VIOLATION: Final[dict[str, str]] = {
    "高空作业未系安全带": SEVERITY_MAJOR,
    "动火作业无监护": SEVERITY_MAJOR,
    "临边无防护": SEVERITY_MEDIUM,
    "用电隐患": SEVERITY_MEDIUM,
    "消防通道堵塞": SEVERITY_MEDIUM,
    "未戴安全帽": SEVERITY_MINOR,
    "未穿反光衣": SEVERITY_MINOR,
    "材料堆放混乱": SEVERITY_MINOR,
}
"""受控词表 → 默认级别。

**键集合必须与 eval/scorers.py 的 VIOLATION_VOCAB 完全相等**,
测试 test_定级表与受控词表完全同步 盯着 —— 词表加一个词、这里漏配,
表现是新词全部「待定级」,报表上看着像 bug 实际是漏配,提前在单测里炸掉。
"""


def grade(violations: Sequence[str]) -> dict[str, str]:
    """逐项定级。词表外的词 → 待定级(不猜)。返回全新 dict,不改入参。"""
    return {item: SEVERITY_BY_VIOLATION.get(item, SEVERITY_PENDING) for item in violations}


def worst(violations: Sequence[str]) -> str | None:
    """整张照片的最高级别;没有违规项返回 None。

    规则:已知级别里取最重;**全部**是待定级时返回「待定级」——
    不知道级别不等于不严重,不能静默降成一般。
    """
    if not violations:
        return None
    levels = set(grade(violations).values())
    for level in SEVERITY_ORDER:
        if level in levels:
            return level
    return None  # pragma: no cover —— SEVERITY_ORDER 覆盖了 grade 的全部取值,到不了这里


__all__ = [
    "SEVERITY_BY_VIOLATION",
    "SEVERITY_MAJOR",
    "SEVERITY_MEDIUM",
    "SEVERITY_MINOR",
    "SEVERITY_ORDER",
    "SEVERITY_PENDING",
    "grade",
    "worst",
]
