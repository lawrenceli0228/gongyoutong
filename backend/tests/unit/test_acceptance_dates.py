"""``scripts/acceptance_dates.py`` 的验收 —— 五年全量不变量 + 十四日模拟 + 两道锚点。

===========================================================================
它证明什么、不证明什么(边界别吹)
---------------------------------------------------------------------------
**证明**:真机验收脚本**自己这一侧**,换成任何一天都不会 ①抛异常 ②选错 A10 的分支
③算错期望值 ④展示串与 ISO 错位 ⑤跨月/跨年/闰日出洋相 ⑥**四条日期断言里有两条
悄悄撞成同一天,当天退化成废话**。

**不证明**:模型会不会照抄 ``due_display``、supervisor 会不会派对 Agent、
库里落没落对、容器时区对不对。一句话:它验的是「脚本不会因为换了一天就算错、
也不会因为换了一天就少验一样东西」,不是「系统是对的」。

===========================================================================
三样东西,分工别搞混
---------------------------------------------------------------------------
    锚点一 ``test_基准日期望值与重构前的硬编码字面量逐字相同``
        证明「没有偷偷改变行为」。只覆盖一天(2026-08-08)。
        七项里五项逐字不动;C3 那两项是**有意**改的,旧值 → 新值和理由
        登记在 ``_INTENTIONAL`` 和它下面那条测试里,不许悄悄改。
    锚点二 ``test_十四日模拟_独立预言机逐日复算``
        完全不碰 dates.py,用 stdlib **逐日扫描**重算。证明「日历逻辑本身是对的」。
    不变量 ``test_五年全量_四个日期任何一天都两两不等``
        证明「判据强度没有被日历偷走」。五年 1826 天逐日核。

三样缺一不可:
  · 只有锚点一:dates.py 哪天算错了,期望值跟着一起错,验收照样全绿。
  · 只有锚点二:证明不了和 2026-08-08 那天人工核过日历的口径一致。
  · 只有前两样:日期可以算得一个不错,判据却在某几个星期几集体失效 —— 旧口径
    每逢周四 A3 == A1、周六 C3 == A4,当天那几条断言在**数学上**就不可能红。
    ``test_旧口径的撞车矩阵`` 五年全量把那张矩阵原样钉住,当作「修过什么」的常驻凭证;
    2026-08-10 的对抗复核就是顺着这四天挖出假绿灯的。

===========================================================================
预言机为什么改成逐日扫描(2026-08-10 二轮复核的 LOW 项)
---------------------------------------------------------------------------
上一版 ``_oracle`` 的 docstring 写着「不碰 dates.py,独立见证」,代码却是
``today + timedelta(days=(4 - today.weekday()) % 7)`` —— 和 ``dates.py:201``
的 ``(target - today.weekday()) % 7`` **同一个公式**。「不 import」是真的,
「独立」不是:有人把 ``% 7`` 改成 ``% 7 or 7``,两边同时变,全部用例照绿。
docstring 声称的性质代码不具备,撞本仓最硬的红线。
现在换成 ``isoweekday()`` + ``for step in range(7)`` 逐日扫 —— 换了取数口径、
换了算法形态,上面那种同源改动会被立刻抓住。

===========================================================================
为什么起始日选了这五个 / 为什么是十四天不是十天
---------------------------------------------------------------------------
连续 14 天,**不管起始日是哪天,7 个星期几各出现恰好 2 次**(下面 M3 那条就在锁它)。
十天也能碰到 7 个星期几,但覆盖次数随起始日变,只能写「至少覆盖到了」这种弱断言;
十四天则与起始日无关地恒为「每个星期几 2 次 / 报错支 8 次 / 正常支 6 次」,
断言从「覆盖到了」升级成**计数精确相等** —— 分支写歪一格,换个起始日立刻翻红。

星期维度既然被 14 包死,起始日的自由度就全留给日历边界:
    2026-08-08 周六  文档与单测的基准日,留作锚点
    2026-08-24 周一  跨 31 天的月末(8/31 → 9/1)
    2026-12-24 周四  **跨年**(format_display 不带年,跨年错只能靠 ISO 抓)
    2028-02-20 周日  **闰日 2/29** + 跨月
    2027-02-22 周一  平年 2/28 → 3/1,抓「硬编码闰日」类错误
五年全量那几条则不挑日子:2026-01-01 起连续 1826 天(含 2028 闰年),一天不落。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

# scripts/ 不是包(没有 __init__.py),而且 live_acceptance.py 一 import 就会真跑验收 ——
# 所以这里按文件路径单独加载被测模块,不走 `from scripts.x import y`。
# acceptance_dates.py 自己也是这么加载 dates.py 的,理由写在它的模块 docstring 里。
_BACKEND: Final[Path] = Path(__file__).resolve().parents[2]
_MODULE_PATH: Final[Path] = _BACKEND / "scripts" / "acceptance_dates.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("acceptance_dates_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None, f"加载不了 {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ad: Final[ModuleType] = _load()

WINDOW_DAYS: Final[int] = 14
START_DAYS: Final[tuple[date, ...]] = (
    date(2026, 8, 8),
    date(2026, 8, 24),
    date(2026, 12, 24),
    date(2028, 2, 20),
    date(2027, 2, 22),
)

BASELINE: Final[date] = date(2026, 8, 8)
"""基准日(周六)。锚点一那几条都钉在这一天。"""

FIVE_YEARS_START: Final[date] = date(2026, 1, 1)
FIVE_YEARS_DAYS: Final[int] = 1826
"""2026-01-01 起 1826 天 = 到 2030-12-31,含 2028 这个闰年。
挑五年不是为了好听:一整年只能证明每个星期几各碰过 52 次,
碰不到「闰年 2/29 恰好是某个星期几」这类组合。"""

DISPLAY_RE: Final[re.Pattern[str]] = re.compile(r"\d{1,2}月\d{1,2}日\(周[一二三四五六日]\)")
"""**半角**括号、月日不补零。有人把 format_display 改成全角括号或者补零,
验收脚本里那批 `in` 判据会集体失配却没人知道 —— 这条正则就是那件事的报警器。
(2026-08-10 实测 format_display(date(2026,8,14)) 的括号是 U+0028/U+0029。)"""

WEEKDAY_CHARS: Final[str] = "一二三四五六日"

_ISO_WEEKDAY: Final[dict[str, int]] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
}
"""汉字 → ``isoweekday()``(周一=1 … 周日=7)。

刻意用 iso 口径而不是 ``weekday()``:dates.py 全程用 ``weekday()``,
换一套下标能让「下标算错一格」这类错误在两边表现不同,预言机才拦得住。
"""


def _window(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(WINDOW_DAYS)]


def _five_years() -> list[date]:
    return [FIVE_YEARS_START + timedelta(days=i) for i in range(FIVE_YEARS_DAYS)]


# --- 独立预言机:逐日扫描,不碰 dates.py,也不抄它的公式 -------------------------


def _scan_forward(start: date, iso_wd: int) -> date:
    """从 start 起(**含 start**)一天一天往后走,第一个撞上 iso_wd 的那天。

    这就是 dates.py「周X = 未来最近的那个 X,含今天」那条约定的独立复述。
    谁把它改成「今天不算,滚到下周」,这里立刻不一致。
    """
    for step in range(7):
        day = start + timedelta(days=step)
        if day.isoweekday() == iso_wd:
            return day
    raise AssertionError(f"七天里必然碰得到 isoweekday={iso_wd},走到这儿说明扫描写坏了")


def _scan_back_to_monday(day: date) -> date:
    """一天一天往回走,直到踩上周一 —— dates.py「一周从周一起算」那条约定的独立复述。"""
    cursor = day
    for _ in range(7):
        if cursor.isoweekday() == 1:
            return cursor
        cursor -= timedelta(days=1)
    raise AssertionError("七天里必然碰得到一个周一,走到这儿说明扫描写坏了")


def _oracle(phrase: str, today: date) -> date:
    """把脚本真正发出去的那句短语,用 stdlib 从头解析一遍。

    只认候选表里那几类词法。**认不出来就 AssertionError** —— 将来有人往
    A3_CANDIDATES / C3_CANDIDATES 里加了新词法却忘了同步这里,会当场炸,
    而不是安安静静地跳过不验(那正是「独立预言机」最容易烂掉的方式)。

    ⚠️ 别往这里加复杂短语。一旦预言机也得抄 dates.py 的规则,它就不独立了。
    """
    if phrase == ad.PHRASE_A1:
        return today + timedelta(days=1)
    if phrase == ad.PHRASE_A4:
        return today + timedelta(days=2)
    if phrase.startswith("下周") and phrase.endswith("之前"):
        next_week_monday = _scan_back_to_monday(today) + timedelta(days=7)
        return _scan_forward(next_week_monday, _ISO_WEEKDAY[phrase[2]])
    if phrase.startswith("周"):
        return _scan_forward(today, _ISO_WEEKDAY[phrase[1]])
    raise AssertionError(f"预言机不认识「{phrase}」—— 候选表加了新词法就得同步这里")


def _legacy_dates(today: date) -> dict[str, date]:
    """**旧口径**(A3 固定问「周五」、C3 固定问「下周一」)的四个日期,同样用预言机算。

    只服务 ``test_旧口径的撞车矩阵`` 那一条:把「修之前是什么样」变成可执行的凭证,
    而不是注释里一句「据说周四会撞」。
    """
    return {
        "A1": today + timedelta(days=1),
        "A4": today + timedelta(days=2),
        "A3": _scan_forward(today, 5),
        "C3": _scan_forward(_scan_back_to_monday(today) + timedelta(days=7), 1),
    }


def _display(day: date) -> str:
    """format_display 的独立复算 —— 同样不碰 dates.py。"""
    return f"{day.month}月{day.day}日(周{WEEKDAY_CHARS[day.weekday()]})"


def _dates_parse(phrase: str, today: date) -> date | None:
    """借派生层已经加载好的那份 dates.py 解析,测试不再单独加载一遍。

    走私有属性 ``ad._dates`` 是刻意的:再按文件路径加载一遍 dates.py,会多出一个
    模块对象,coverage 那个坑(见被测模块的 docstring)就又有了发作的余地。
    """
    return ad._dates.parse_due(phrase, today=today)


def _dates_parse_error(phrase: str, today: date) -> str:
    """拿到 dates.py 对这个短语抛出的**真异常原文**;不抛就是测试前提坏了,当场炸。"""
    try:
        _dates_parse(phrase, today=today)
    except ad.DueParseError as exc:
        return str(exc)
    raise AssertionError(f"{today} 这天「{phrase}」居然解析成功了,测试前提不成立")


def _four_dates(got: ad.Expected) -> dict[str, date]:
    """从一份 Expected 里取出参与「四日不撞车」的那四个日期。

    A1 没有独立的 ISO 字段(D 组不查它),所以按 ``今天 + 1`` 复算 —— 这不是抄公式:
    A1 的原话「明天上午」固定不动,它是不变量的锚,``today + 1`` 就是它的定义。
    """
    return {
        "A1": date.fromisoformat(got.today_iso) + timedelta(days=1),
        "A3": date.fromisoformat(got.d1_iso),
        "A4": date.fromisoformat(got.d2_iso),
        "C3": date.fromisoformat(got.d3_iso),
    }


# --- 锚点一:没有偷偷改变行为 ---------------------------------------------------

_PRE_T4: Final[dict[str, str]] = {
    "a1_display": "8月9日(周日)",
    "a3_display": "8月14日(周五)",
    "a4_display": "8月10日(周一)",
    "c3_display": "8月10日(周一)",
    "d1_iso": "2026-08-14",
    "d2_iso": "2026-08-10",
    "d3_iso": "2026-08-10",
}
"""T4 之前写死在 live_acceptance.py 里的七个字面量,基准日 2026-08-08(周六)。
出处:改动前的第 179/187/191/254/275/279/281 行。这份表是「行为有没有漂」的原点,
**不许照着 expected() 反推回填**。"""

_INTENTIONAL: Final[dict[str, str]] = {
    "c3_display": "8月11日(周二)",
    "d3_iso": "2026-08-11",
}
"""七项里**有意**改掉的两项,以及改成什么。

为什么改:基准日是周六,旧口径下「下周一」== 「后天」== 8-10,C3 和 A4 落在同一天,
D3 就此退化成 D2 的复读 —— 整改任务到底落没落库,那一天根本验不出来。
候选表把 C3 顺延到「下周二之前」,这两项随之变化。**其余五项必须逐字不动。**
下面 ``test_基准日那两项变更是撞车逼出来的`` 会验「旧值确实等于 A4」,
所以这不是一句自说自话的「我改了但是有理由」。
"""


def test_基准日期望值与重构前的硬编码字面量逐字相同() -> None:
    """逐项对 ``_PRE_T4``,只有 ``_INTENTIONAL`` 登记过的那两项走新值。

    这是「行为没漂」的**唯一**证据。加字段可以,改这七项的值不行 ——
    真要改,就往 ``_INTENTIONAL`` 里登记一条并写清为什么,别直接改 ``_PRE_T4``。
    """
    got = ad.expected(BASELINE)
    for field, pre_t4 in _PRE_T4.items():
        want = _INTENTIONAL.get(field, pre_t4)
        assert getattr(got, field) == want, field

    assert got.today_iso == "2026-08-08"
    assert got.weekday_char == "六"


def test_基准日那两项变更是撞车逼出来的() -> None:
    """证明 ``_INTENTIONAL`` 不是随手改的:旧值恰好就是 A4 的值,新值和谁都不撞。

    旧口径 D3 == D2 时,「整改任务落库了」和「整改任务的期限抄了 T2 的」这两件事
    在库里长得一模一样 —— D3 那条断言当天等于白写。
    """
    got = ad.expected(BASELINE)
    assert _PRE_T4["d3_iso"] == _PRE_T4["d2_iso"] == got.d2_iso, "旧 C3 就是撞在 A4 头上"
    assert _PRE_T4["c3_display"] == got.a4_display

    four = _four_dates(got)
    assert len(set(four.values())) == 4, four
    assert got.d3_iso == _INTENTIONAL["d3_iso"]


def test_基准日发出去的四句原话() -> None:
    """把基准日**真正问出口**的四句钉住 —— 期望值对不对,前提是问的还是这几句。

    周六那天 A3 用的仍是演示原话「周五」(它在周六不撞车),只有 C3 顺延了一格。
    """
    got = ad.expected(BASELINE)
    assert got.a1_phrase == "明天上午"
    assert got.a3_phrase == "周五"
    assert got.a4_phrase == "后天"
    assert got.c3_phrase == "下周二之前"


def test_基准日走A10报错支且不发探针() -> None:
    """2026-08-08 是周六,本周三早过了 —— 与改动前那条写死的「本周三之前」判据一致。

    报错支不发探针(主问自己就踩在报错分支上,再补一问纯属白花一轮模型调用)。
    """
    got = ad.expected(BASELINE)
    assert got.a10_phrase == ad.PHRASE_A10_PAST
    assert got.a10_branch == ad.BRANCH_PAST
    assert got.a10_hint == ad.HINT_A10_PAST
    assert got.a10_probe_phrase is None
    assert got.a10_probe_hint is None


# --- 核心不变量:四日不撞车(五年全量) ----------------------------------------


def test_五年全量_四个日期任何一天都两两不等() -> None:
    """**本轮修复的核心断言。** A1/A3/A4/C3 撞成同一天 = 当天那几条判据在数学上不可能红。

    1826 天逐日核,一天都不许违反。这条要是红了,别去改它 —— 去看候选表和 ``_pick``。
    """
    violations: list[tuple[date, dict[str, date]]] = []
    for today in _five_years():
        four = _four_dates(ad.expected(today))
        if len(set(four.values())) != 4:
            violations.append((today, four))
    assert violations == [], violations[:5]


def test_旧口径的撞车矩阵() -> None:
    """把「修之前撞在哪几天」变成可执行的凭证,而不是注释里一句「据说」。

    用预言机算旧口径(A3 固定问周五、C3 固定问下周一),五年全量统计撞车的星期几。
    结果就是 2026-08-10 对抗复核报的那张矩阵:

        A1 == A3 → 周四        A3 == A4 → 周三
        A1 == C3 → 周日        A4 == C3 → 周六

    七天里四天在偷工减料。这条测试**不测被测模块**,它测的是「我们到底修掉了什么」——
    哪天有人把候选表退回固定短语,上一条(五年全量不撞车)会红,而这条仍然绿,
    两条一起看就能立刻定位:退回旧口径了。
    """
    collisions: dict[tuple[str, str], set[int]] = {}
    for today in _five_years():
        legacy = _legacy_dates(today)
        slots = sorted(legacy)
        for i, left in enumerate(slots):
            for right in slots[i + 1 :]:
                if legacy[left] == legacy[right]:
                    collisions.setdefault((left, right), set()).add(today.weekday())
    # 键按槽位名字典序;值是 date.weekday()(周一=0)。
    # A1 与 A4 这一对不在表里 —— +1 与 +2 永远差一天,它俩本来就撞不上。
    assert collisions == {
        ("A1", "A3"): {3},  # 周四:「周五」就是明天
        ("A1", "C3"): {6},  # 周日:「下周一」就是明天
        ("A3", "A4"): {2},  # 周三:「周五」就是后天
        ("A4", "C3"): {5},  # 周六:「下周一」就是后天
    }, collisions


def test_五年全量_四个短语都出自候选表且两个锚从不动() -> None:
    """A1/A4 恒等于常量;A3/C3 只能从各自的候选表里取,不许凭空冒出第三种词法。"""
    for today in _five_years():
        got = ad.expected(today)
        assert got.a1_phrase == ad.PHRASE_A1, today
        assert got.a4_phrase == ad.PHRASE_A4, today
        assert got.a3_phrase in ad.A3_CANDIDATES, (today, got.a3_phrase)
        assert got.c3_phrase in ad.C3_CANDIDATES, (today, got.c3_phrase)


def test_五年全量_顺延只发生在撞车那几天() -> None:
    """候选表的实际产出,按星期几钉成一张表 —— 「按日历选短语」到底选出了什么。

    这是**特征化测试**(记录行为),不是实现:表是从 expected() 跑出来的,
    改了候选表顺序它就会红,提醒你重新确认一遍每天问的是什么。
    读法:演示原话在五/七天里还是原话,只有撞车那两天各让一格。
    """
    seen: dict[int, set[tuple[str, str]]] = {}
    for today in _five_years():
        got = ad.expected(today)
        seen.setdefault(today.weekday(), set()).add((got.a3_phrase, got.c3_phrase))
    assert seen == {
        0: {("周五", "下周一之前")},
        1: {("周五", "下周一之前")},
        2: {("周六", "下周一之前")},  # 周三:「周五」= 后天,让给 A4
        3: {("周日", "下周一之前")},  # 周四:「周五」= 明天,让给 A1;「周六」= 后天,再让
        4: {("周五", "下周一之前")},  # 周五:「周五」= 今天,不撞
        5: {("周五", "下周二之前")},  # 周六:「下周一」= 后天,让给 A4
        6: {("周五", "下周三之前")},  # 周日:「下周一」= 明天、「下周二」= 后天,连让两格
    }, seen


def test_五年全量_A3改期永远真的改动了T1的期限() -> None:
    """A3 的整条价值就在这一条:它必须和 A1 落在不同的日子。

    旧口径每逢周四 A3 == A1,``reschedule_task`` 把同一天改成同一天,rowcount 照样是 1,
    A3 与 D1 两条断言验的都退化成 A1 的结果 —— 改期链路(含 ``WHERE status='open'``
    那道闸)当天零覆盖。这条断言把那种退化钉死在门外。
    """
    for today in _five_years():
        got = ad.expected(today)
        assert date.fromisoformat(got.d1_iso) != today + timedelta(days=1), today


# --- A10:两支 + 探针 ----------------------------------------------------------


def test_五年全量_探针只在日历上真有已过的本周X那天发() -> None:
    """探针 ⟺ 「正常支」且「本周一已经过了」⟺ 周二、周三。

    周一是唯一发不出探针的那天,原因不是漏了,是日历上根本不存在已过的本周 X
    (本周一就是今天)—— 这条测试顺手把那个事实也验了。
    """
    probe_weekdays: set[int] = set()
    for today in _five_years():
        got = ad.expected(today)
        if got.a10_probe_phrase is None:
            assert got.a10_probe_hint is None, today
            continue
        assert got.a10_branch == ad.BRANCH_AHEAD, today
        assert got.a10_probe_phrase == ad.PHRASE_A10_PROBE, today
        assert got.a10_probe_hint == ad.HINT_A10_PROBE, today
        probe_weekdays.add(today.weekday())
    assert probe_weekdays == {1, 2}, probe_weekdays

    monday = _scan_forward(FIVE_YEARS_START, 1)
    assert ad.expected(monday).a10_probe_phrase is None
    assert _dates_parse(ad.PHRASE_A10_PROBE, monday) == monday, "周一的「本周一」就是今天"


def test_五年全量_报错不猜这条词法七天里覆盖六天() -> None:
    """「本周X 已过 → 报错不猜」这条契约,每周有几天真的被验到。

    修之前是 4/7(只有主问走报错支的周四~周日);补上探针之后是 6/7。
    差的那天是周一,理由见上一条 —— 不是欠账,是日历上不存在。
    """
    covered = {
        today.weekday()
        for today in _five_years()
        if ad.expected(today).a10_branch == ad.BRANCH_PAST
        or ad.expected(today).a10_probe_phrase is not None
    }
    assert covered == {1, 2, 3, 4, 5, 6}, covered
    assert 0 not in covered


def test_报错支与探针的改法提示都与dates原文一致() -> None:
    """``HINT_A10_*`` 不许凭记忆写 —— 拿 dates.py 抛出来的真异常核。

    这两个串是 live_acceptance.py 判「报了错有没有给改法」的判据。
    dates.py 哪天把「要记下周的就说“下周三”」这句话改了措辞,这条立刻红,
    而不是等到真机上 A10 莫名其妙报红再回头查。
    """
    thursday = _scan_forward(date(2026, 8, 1), 4)
    got = ad.expected(thursday)
    assert got.a10_branch == ad.BRANCH_PAST
    message = _dates_parse_error(got.a10_phrase, thursday)
    assert got.a10_phrase in message
    assert got.a10_hint in message
    assert "已经过" in message

    tuesday = _scan_forward(date(2026, 8, 1), 2)
    probe = ad.expected(tuesday)
    assert probe.a10_probe_phrase == ad.PHRASE_A10_PROBE
    probe_message = _dates_parse_error(probe.a10_probe_phrase, tuesday)
    assert probe.a10_probe_phrase in probe_message
    assert probe.a10_probe_hint in probe_message
    assert "已经过" in probe_message


# --- 结构件:候选表用尽当场炸,不悄悄退化 --------------------------------------


def test_候选表用尽会当场报错而不是返回撞车的日期() -> None:
    """``_pick`` 的兜底分支:宁可炸,也不许返回一个已经被占的日期。

    正常路径永远走不到这里(候选表七个,鸽笼原理有富余),所以只能直接喂一张
    注定用尽的表来验。真让它悄悄返回撞车日期的话,屏幕上照样 25/25,
    而四条断言集体变成废话 —— 这正是本轮在修的那类病。
    """
    today = BASELINE
    taken = {today + timedelta(days=1): "A1"}
    with pytest.raises(RuntimeError) as exc:
        ad._pick("A3", ("明天",), today, taken)
    assert "全撞车" in str(exc.value)
    assert "A3" in str(exc.value)


def test_两个固定锚被改成同一句也会当场炸(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1/A4 走单元素候选表,看着多余 —— 它挡的就是「有人把两句原话改成一样」。"""
    monkeypatch.setattr(ad, "PHRASE_A4", ad.PHRASE_A1)
    with pytest.raises(RuntimeError) as exc:
        ad.expected(BASELINE)
    assert "A4" in str(exc.value)


# --- 十四日模拟 ---------------------------------------------------------------


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_每一天都算得出期望值(start: date) -> None:
    """M1:14 天里 expected() 全部正常返回。

    这条不是「跑通就行」的凑数断言:周四~周日 ``parse_due("本周三")`` **会抛
    DueParseError**,派生层必须自己接住并切到报错支。漏接的话不是某条断言红,
    是整个验收脚本在 A10 那一行当场崩掉,后面 15 条一条都跑不到。
    """
    for today in _window(start):
        got = ad.expected(today)
        assert got.today == today
        assert got.today_iso == today.isoformat()


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_两支分别恰好出现八次和六次(start: date) -> None:
    """M2:与起始日**无关**的强不变量 —— 报错支 8 次、正常支 6 次。

    8 = 周四~周日 4 个星期几 × 2 次;6 = 周一~周三 3 个 × 2 次。
    这条断言之所以能写成精确相等而不是「至少」,全靠 14 天这个长度(见模块 docstring)。
    """
    branches = Counter(ad.expected(today).a10_branch for today in _window(start))
    assert branches == {ad.BRANCH_PAST: 8, ad.BRANCH_AHEAD: 6}


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_探针恰好出现四次(start: date) -> None:
    """M2b:探针只在周二/周三发 → 14 天里恰好 2 个星期几 × 2 次 = 4 次。

    和上面 M2 同一个道理:14 天让「几次」这种计数与起始日解耦,写得成精确相等。
    """
    probes = [t for t in _window(start) if ad.expected(t).a10_probe_phrase is not None]
    assert len(probes) == 4, probes
    assert {t.weekday() for t in probes} == {1, 2}


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_七个星期几各出现两次(start: date) -> None:
    """M3:把「14 天 = 星期几全覆盖」这个前提本身钉死。上面 M2 的 8/6 建立在它之上。"""
    weekdays = Counter(today.weekday() for today in _window(start))
    assert weekdays == dict.fromkeys(range(7), 2)


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_分支边界卡在周三与周四之间(start: date) -> None:
    """M4:``报错支 ⟺ weekday >= 3``。分支条件写歪一格(>= 2 或 >= 4)立刻红。

    为什么边界在这儿:本周三 = 本周一+2,``本周三 < 今天 ⟺ weekday > 2``。
    """
    for today in _window(start):
        got = ad.expected(today)
        expect_past = today.weekday() >= 3
        assert (got.a10_branch == ad.BRANCH_PAST) is expect_past, today


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_所有展示串都是半角括号且月日不补零(start: date) -> None:
    """M5:见 DISPLAY_RE 的注释 —— 这是「全角/补零改动」的报警器。"""
    for today in _window(start):
        got = ad.expected(today)
        for field in (got.a1_display, got.a3_display, got.a4_display, got.c3_display):
            assert DISPLAY_RE.fullmatch(field), (today, field)


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_展示串与ISO指向同一天(start: date) -> None:
    """M6:专抓**展示串与 ISO 错位** —— A 组断展示串、D 组断 ISO,错位了两边会各自「自洽」。

    复算方式刻意独立:从 ISO 反解出 date,自己查表算周几,不走 format_display。
    """
    for today in _window(start):
        got = ad.expected(today)
        pairs = (
            (got.a3_display, got.d1_iso),
            (got.a4_display, got.d2_iso),
            (got.c3_display, got.d3_iso),
        )
        for display, iso in pairs:
            assert display == _display(date.fromisoformat(iso)), (today, display, iso)


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_独立预言机逐日复算(start: date) -> None:
    """锚点二的主体:完全不碰 dates.py,用逐日扫描把**当天真正问出口的那四句**重算一遍。

    这道锚点堵的是「用被测系统算期望值」的循环风险:``dates.py`` 的
    「周X 含今天」「下周以本周一为基准」这两条歧义约定一旦被人「优化」掉,
    这个测试立刻红,而只有锚点一的话它会安安静静地跟着一起错。

    预言机吃的是 ``got.a3_phrase`` 这种**运行期挑出来的短语**,不是写死的「周五」——
    候选表顺延到哪一句,它就跟着算哪一句,顺延本身不会让这道锚点失效。
    """
    for today in _window(start):
        got = ad.expected(today)
        assert got.a1_display == _display(_oracle(got.a1_phrase, today)), today
        assert got.a3_display == _display(_oracle(got.a3_phrase, today)), today
        assert got.a4_display == _display(_oracle(got.a4_phrase, today)), today
        assert got.c3_display == _display(_oracle(got.c3_phrase, today)), today
        assert got.d1_iso == _oracle(got.a3_phrase, today).isoformat(), today
        assert got.d2_iso == _oracle(got.a4_phrase, today).isoformat(), today
        assert got.d3_iso == _oracle(got.c3_phrase, today).isoformat(), today


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_语义不变量(start: date) -> None:
    """M7 的另一半:手写的语义不变量,不依赖 dates.py 也不依赖预言机的解析。

    最要紧的是 ``后天 <= 本周日`` —— 它就是**「A10 正常支的表格里一定有 T2」的前提**。
    A10 跑在 A5(T1 销项)之后,台账里唯一一条未完成的是 T2(期限 = 后天);
    T2 落不进 cutoff 的话表是空的,那一支三条正向铁证一条都拿不到。
    """
    for today in _window(start):
        got = ad.expected(today)
        a3_day = date.fromisoformat(got.d1_iso)
        a4_day = date.fromisoformat(got.d2_iso)
        c3_day = date.fromisoformat(got.d3_iso)

        assert a4_day == today + timedelta(days=2), today

        # A3 是「周X」:落在今天起七天内,且星期几对得上问出口的那个字
        assert 0 <= (a3_day - today).days <= 6, today
        assert a3_day.isoweekday() == _ISO_WEEKDAY[got.a3_phrase[1]], today

        # C3 是「下周X」:整个落在下一个日历周里(本周一+7 ~ 本周一+13)
        next_monday = _scan_back_to_monday(today) + timedelta(days=7)
        assert next_monday <= c3_day <= next_monday + timedelta(days=6), today
        assert c3_day.isoweekday() == _ISO_WEEKDAY[got.c3_phrase[2]], today

        if got.a10_branch == ad.BRANCH_PAST:
            this_wed = _scan_back_to_monday(today) + timedelta(days=2)
            assert this_wed < today, today
        else:
            this_sunday = _scan_back_to_monday(today) + timedelta(days=6)
            assert a4_day <= this_sunday, (today, a4_day, this_sunday)


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_A10只有两种主问法且与分支一一对应(start: date) -> None:
    """M10:防将来有人加了第三支却忘了把问法对齐 —— 那会让 A10 沉默地测错东西。"""
    got = [ad.expected(today) for today in _window(start)]
    seen = {(e.a10_phrase, e.a10_branch) for e in got}
    assert seen == {
        (ad.PHRASE_A10_PAST, ad.BRANCH_PAST),
        (ad.PHRASE_A10_AHEAD, ad.BRANCH_AHEAD),
    }


@pytest.mark.parametrize("start", START_DAYS, ids=lambda d: d.isoformat())
def test_十四日模拟_A10正常支的期限展示串等于后天(start: date) -> None:
    """``a10_due_display`` 是 A10 正常支唯一的事实级铁证,它必须恒等于 A4 的展示串。

    (A10 时台账里唯一一条未完成的就是 A4 记下的 T2,期限 = 后天。)
    """
    for today in _window(start):
        got = ad.expected(today)
        assert got.a10_due_display == got.a4_display, today


# --- 日历边界(每个起始日各治一种) --------------------------------------------


def test_跨年只能靠ISO抓得住() -> None:
    """M8:``format_display`` 不带年份,跨年错在展示串上**看不出来**。

    2026-12-28(周一)的「下周一之前」是 2027-01-04:展示串「1月4日(周一)」
    看着毫无破绽,只有 ISO 的年份能证明它没被算回 2026 年 1 月。
    """
    isos = {ad.expected(today).d3_iso for today in _window(date(2026, 12, 24))}
    assert any(iso.startswith("2027-") for iso in isos), isos
    got = ad.expected(date(2026, 12, 28))
    assert got.c3_phrase == "下周一之前"
    assert got.d3_iso == "2027-01-04"
    assert got.c3_display == "1月4日(周一)"


def test_闰日二月二十九号会真的出现在期望值里() -> None:
    """M9:2028 是闰年。2028-02-27(周日)的「后天」就是 2月29日(周二)。"""
    displays = {ad.expected(today).a4_display for today in _window(date(2028, 2, 20))}
    assert "2月29日(周二)" in displays, sorted(displays)
    assert ad.expected(date(2028, 2, 27)).d2_iso == "2028-02-29"


def test_平年二月不会凭空长出二十九号() -> None:
    """2027 是平年:2/27 的「后天」必须是 3月1日,不是 2月29日(硬编码闰日的经典错法)。"""
    got = ad.expected(date(2027, 2, 27))
    assert got.d2_iso == "2027-03-01"
    assert got.a4_display == "3月1日(周一)"


def test_跨三十一天月末不串月() -> None:
    """2026-08-30(周日)的「后天」是 9月1日 —— 8 月有 31 天,+2 正好跨过去。"""
    got = ad.expected(date(2026, 8, 30))
    assert got.d2_iso == "2026-09-01"
    assert got.a4_display == "9月1日(周二)"


def test_周六周日的C3顺延掉了旧口径的撞车() -> None:
    """旧口径里「下周一」撞 A4(周六)、撞 A1(周日);新口径顺延后各自让开。

    这条是上一版 ``test_后天与下周一只在周六撞车`` 的**反转**:那一条把撞车
    固化成了期望(「只有周六撞」被写成断言),等于给假绿灯发了张许可证。
    现在要求的是「一天都不许撞」,同时把「旧口径确实会撞」留成注释外的可执行凭证。
    """
    saturday = date(2026, 8, 8)
    assert saturday.weekday() == 5
    legacy = _legacy_dates(saturday)
    assert legacy["C3"] == legacy["A4"], "旧口径:周六「下周一」就是「后天」"
    got = ad.expected(saturday)
    assert got.c3_phrase == "下周二之前"
    assert got.d3_iso != got.d2_iso

    sunday = saturday + timedelta(days=1)
    legacy_sun = _legacy_dates(sunday)
    assert legacy_sun["C3"] == legacy_sun["A1"], "旧口径:周日「下周一」就是「明天」"
    got_sun = ad.expected(sunday)
    assert got_sun.c3_phrase == "下周三之前"
    assert len(set(_four_dates(got_sun).values())) == 4


def test_周三周四的A3顺延掉了旧口径的撞车() -> None:
    """旧口径里「周五」撞 A4(周三)、撞 A1(周四)—— 后者就是那条 HIGH 假绿灯的根。"""
    wednesday = _scan_forward(date(2026, 8, 1), 3)
    legacy = _legacy_dates(wednesday)
    assert legacy["A3"] == legacy["A4"], "旧口径:周三「周五」就是「后天」"
    assert ad.expected(wednesday).a3_phrase == "周六"

    thursday = wednesday + timedelta(days=1)
    legacy_thu = _legacy_dates(thursday)
    assert legacy_thu["A3"] == legacy_thu["A1"], "旧口径:周四「周五」就是「明天」"
    assert ad.expected(thursday).a3_phrase == "周日"


def test_A2的下周三截止恒能盖住T1() -> None:
    """A2「下周三之前还有哪些任务没完成」断的是 T1 在表里,而 A2 跑在 A3 改期**之前**,
    所以那时 T1 的期限还是「明天」。

    这条断言里没有日期字面量,所以派生层不用给它字段 —— 但它**依赖一个不变量**:
    ``今天+1 <= 本周一+9`` 必须恒成立,否则某些星期几 T1 会被 cutoff 滤掉,
    A2 会红,而红的是脚本不是系统。这里把那个不变量锁住(``weekday+1 <= 9`` 恒真)。

    ⚠️ 顺序前提是硬的:哪天有人把 A2 挪到 A3 后面,T1 的期限就变成 A3 那个日子,
    这条不变量得重算(A3 最远 today+6,而周日的 cutoff 只到 today+3)。
    """
    for today in _five_years():
        tomorrow = today + timedelta(days=1)
        next_wed = _scan_back_to_monday(today) + timedelta(days=9)
        assert tomorrow <= next_wed, today
