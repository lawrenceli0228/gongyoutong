"""中文日期短语 → date 的**确定性**解析 —— Schedule Agent 的日历层。

===========================================================================
为什么日期在代码里算,而不是让模型算
---------------------------------------------------------------------------
LLM 的星期数学不可靠:「下周三」是哪天,取决于今天是哪天、一周从哪天起算,
让模型每次现推一遍只会引入随机性,错了还没法向工头解释。
写成纯函数词表的三个好处:
  · 可测试:表驱动单测逐条锁死;today 由调用方注入,本模块严禁 date.today();
  · 可解释:结果用 format_display 复述给用户(「8月14日(周五)」),
    解析歪了师傅当场能纠 —— 这是日期歧义的最后防线;
  · 可冻结:词表即契约,提示词只需要说「把用户原话传给工具」。

⚠️ 考勤查询有一个「同口径、反方向」的双胞胎:agents/attendance/ranges.py ——
「周三」在那边是**过去**最近的周三,未来日期在那边是错误。两个文件**刻意不共用**
(W7 方案 §4.6:共用一份词表必然有一边方向错),想「消除重复」之前先读那边头注。
改本文件的公共口径(周一起点、含今天、时段词、单字中文数字)要对照那边一起看。

⚠️ 繁體折叠表(_HANT_VARIANTS)两边**也各有一份,而且内容不一样**:各自只收
自己词表里真出现的字 —— 本文件比那边多「後」「內」(後天 / 大後天 / N天後 /
N天內),考勤那边压根没有这几个词形(它不看未来)。所以这张表也不许合并:
合了就等于往考勤那边塞两个它永远用不上的字,「封闭」这条判据当场失效。

===========================================================================
词表 v1(冻结;改词表要同步改 prompt.md 与演示脚本)
---------------------------------------------------------------------------
示例按 today=2026-08-08(周六)推算:

  (空)/ None            → None(= 无期限任务)
  今天/今日 · 明天/明日   → 8-08 · 8-09;后天 → 8-10;大后天 → 8-11
  N天后 / N天内(同义)   → 3天后 → 8-11;N 收阿拉伯数字和单字中文(一~十、两)
  周X / 星期X / 礼拜X     → 未来最近的那个 X,含今天;X 收 一~六/日/天/1~7
  本周X / 这周X           → 本日历周(周一起点)的 X;已过 → 报错
  下周X / 下下周X         → 本周一 +7 / +14 再落到 X
  月底/本月底/这个月底    → 当月最后一天;下月底/下个月底 → 下月最后一天
  X月Y号 / X月Y日         → 一律今年;假日期或已过 → 报错(提示写完整日期)
  YYYY-MM-DD              → 校验真日期;已过 → 报错
  时段词                  → 前后缀剥掉(早上/上午/中午/下午/晚上/傍晚/一早)
  之前/以前/前(尾部)    → 剥掉:「下周三之前」=「下周三」
  其余一切                → DueParseError,消息紧凑列出支持写法

两条歧义约定(与 prompt.md 措辞同源,改要一起改):
  · 「周X」= 未来最近的那个 X,**含今天** —— 周六当天说「周六」就是今天;
  · 「本周X」已过则**报错不猜** —— 宁可让用户换个说法(「下周三」),
    不做静默猜测;期限猜错一次,台账的信用就归零。

===========================================================================
简繁两种写法都认(2026-08-18 补 —— 港方监理打的就是繁體)
---------------------------------------------------------------------------
中建国际在港施工,监理自然会打「下週三」「後天」「禮拜三」「10天內」
「兩天後」「9月1號」。上面那张词表全是简体,所以**解析入口先把繁體异形字
折成简体**(_fold_hant),之后一切照旧 —— 词表、正则、报错文案一个字没动。
补这一下之前,这些写法一律被 DueParseError 打回,而报错还举例叫人改用简体写法。

三条边界,越过任何一条都会静默坏:
  · **只折输入,不折输出。** format_display 产出的「8月14日(周五)」仍是简体 ——
    屏幕上的繁體由前端渲染层转(docs/W12_三语切换_方案.md §5.3 定的分层:
    「后端一律不转,只有前端渲染层转」)。这里顺手把输出也转了就是两处各转一半,
    前端那道还会把已经是繁體的字再转一遍。
  · **不引转换库。** opencc / hanziconv 一进来就动 uv.lock = 重建镜像,而
    TODOS.md 的 TODO-40 记着线上 30GB 的盘装不下一次后端全量重建。同款取舍
    写在 scripts/frontend-overrides/lang-lib.ts 头注的「为什么不引库」。
  · **字表封闭。** 只收上面这张词表里真会出现的字,不做通用简繁转换 ——
    多收一个字等于悄悄扩了词表,而且是没有任何测试盯着的那种扩法。
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Final


class DueParseError(ValueError):
    """日期短语解析失败。str(exc) 是给工地师傅看的中文人话,并附正确说法示例。"""


_WEEKDAY_CHARS: Final[str] = "一二三四五六日"
"""下标 = date.weekday()(周一=0 … 周日=6)。展示与报错共用;周日写「周日」不写「周天」。"""

_RELATIVE_DAYS: Final[dict[str, int]] = {
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
}

_CN_DIGITS: Final[dict[str, int]] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
"""单字中文数字。「十一」这类复合写法**刻意**不收:组合规则一写开就没边
(十一/廿三/一百零五…),而口语里超过十天的期限多半直接说数字 ——
碰到就报错引导用数字,比默默算错强。"""

_WEEKDAY_TARGETS: Final[dict[str, int]] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}
"""「周三」的三 → weekday 下标。收阿拉伯数字 1~7 是因为手机打字常见「周3」。"""

_TIME_WORDS: Final[tuple[str, ...]] = ("早上", "上午", "中午", "下午", "晚上", "傍晚", "一早")
"""时段词,前缀后缀都剥 —— 台账只到天粒度,「明天上午」=「明天」。"""

_DEADLINE_SUFFIXES: Final[tuple[str, ...]] = ("之前", "以前", "前")
"""期限后缀,只剥尾部。长的在前:先试「之前」,免得被单字「前」剥成「下周三之」。"""

_THIS_MONTH_END: Final[frozenset[str]] = frozenset({"月底", "本月底", "这个月底"})
_NEXT_MONTH_END: Final[frozenset[str]] = frozenset({"下月底", "下个月底"})

_DAYS_RE: Final[re.Pattern[str]] = re.compile(r"(.+)天[后内]")
"""「N天后」与「N天内」同义 —— 工地口语里都指 today+N,不做区间语义。"""

_BARE_WEEKDAY_RE: Final[re.Pattern[str]] = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天1-7])")
_THIS_WEEK_RE: Final[re.Pattern[str]] = re.compile(r"(?:本周|这周)([一二三四五六日天1-7])")
_NEXT_WEEKS_RE: Final[re.Pattern[str]] = re.compile(r"(下下周|下周)([一二三四五六日天1-7])")
_MONTH_DAY_RE: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})月(\d{1,2})[号日]")
_ISO_RE: Final[re.Pattern[str]] = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

_EXAMPLES: Final[str] = "明天 / 后天 / 3天后 / 周三 / 下周三 / 月底 / 9月1号 / 2026-08-15"
"""兜底报错里的举例:每类词法各挑一个代表,一行念完 —— 不是文档。"""

_HANT_VARIANTS: Final[dict[str, str]] = {
    "週": "周",  # 週三 / 下週三 / 下下週三
    "後": "后",  # 後天 / 大後天 / 3天後
    "禮": "礼",  # 禮拜三
    "這": "这",  # 這週六 / 這個月底
    "個": "个",  # 這個月底 / 下個月底
    "號": "号",  # 9月1號
    "內": "内",  # 10天內
    "兩": "两",  # 兩天後
}
"""繁體异形字 → 简体。**逐条对着上面那张词表数出来的,没有第九个字。**

怎么确定它封闭:把词表与正则里出现的汉字全摊开(今 天 日 明 后 大 / 一 二 两
三 四 五 六 七 八 九 十 / 早 上 午 中 下 晚 傍 / 之 以 前 / 月 底 本 这 个 内 /
周 星 期 礼 拜 / 号),逐字问一句「它的繁體是不是另一个字形」—— 只有这八个是。
其余(今/天/日/明/大/星/期/拜/月/底/本/早/午/中/晚/傍/之/以/前 与一~十)
两边同形,收进来纯属噪音;而噪音进了表就等于给「封闭」这条判据开了口子。

⚠️ 这是**折叠**不是转换:八个键全是简体里用不到的字形,所以纯简体输入过它是
恒等映射 —— 一个字节都不会变。test_schedule_dates.py 有三条钉着这件事
(恒等 / 表里的字都在词表中出现过 / 词表里有繁體异形的字都收全了)。
⚠️ **加词表就要回来数一遍。** 漏一个字的表现是「港方那种写法被打回」,
不报错、不掉测试、没人会发现 —— 这次要修的就是这个形状的缺陷。
"""

_HANT_TRANS: Final[dict[int, str]] = str.maketrans(_HANT_VARIANTS)
"""str.translate 用的码点表:一遍扫串搞定,不是逐字 replace 扫八遍。"""


def format_display(d: date) -> str:
    """「8月14日(周五)」—— 建/改任务后模型必须照抄的复述格式。

    半角括号、月日不补零、周日写「周日」。改这里 = 改演示口径,慎动。
    """
    return f"{d.month}月{d.day}日(周{_WEEKDAY_CHARS[d.weekday()]})"


def _fold_hant(text: str) -> str:
    """繁體异形字折成简体 —— 下面所有词表都是简体,所以要在**进词表之前**折。

    ⚠️ 折完的串会进几条报错文案(港方打「十一天後」,报错里引的是「十一天后」)。
    刻意不还原:那句话上屏前还要过前端渲染层的繁體转换,还原等于同一个字来回转
    两次;而真正引用用户原话的兜底报错(_fallback)拿的是**没折过**的 text。
    """
    return text.translate(_HANT_TRANS)


def _strip_noise(text: str) -> str:
    """先折繁體异形字,再反复剥空白、时段词(前后缀)、尾部期限后缀,直到稳定。

    「明天上午之前」要剥两轮(之前 → 上午),所以循环到不动点,不是各剥一遍。

    折叠挂在这里而不是 parse_due 里:本模块所有解析入口都必须先过这道门,
    挂在唯一的必经之路上,就不存在「将来多一个入口忘了折」这种漏法
    (考勤那边有 parse_range / parse_day 两个入口,同一个理由)。
    """
    current = _fold_hant(text)
    while True:
        candidate = current.strip()
        for word in _TIME_WORDS:
            if candidate.startswith(word):
                candidate = candidate[len(word) :]
            if candidate.endswith(word):
                candidate = candidate[: -len(word)]
        for suffix in _DEADLINE_SUFFIXES:
            if candidate.endswith(suffix):
                candidate = candidate[: -len(suffix)]
                break
        if candidate == current:
            return candidate
        current = candidate


def _fallback(raw: str) -> str:
    return f"「{raw.strip()}」这个期限我没看懂。可以这样说:{_EXAMPLES}。"


def _monday_of(today: date) -> date:
    """本日历周的周一 —— 本周/下周/下下周共用的锚点,一周起点约定只写这一处。"""
    return today - timedelta(days=today.weekday())


def _parse_relative_day(phrase: str, today: date) -> date | None:
    offset = _RELATIVE_DAYS.get(phrase)
    if offset is None:
        return None
    return today + timedelta(days=offset)


_MAX_DAYS_AHEAD: Final[int] = 365
"""「N天后」的上界。一年开外的远期安排不该用相对天数记 —— 引导写完整日期。
没有这道闸,超大数字会让 today+timedelta 抛 OverflowError,穿透 DueParseError
边界被 tool_guard 兜成「系统开小差」—— 把用户输入问题说成系统故障(审查 MEDIUM 项)。"""


def _parse_days_offset(phrase: str, today: date) -> date | None:
    matched = _DAYS_RE.fullmatch(phrase)
    if matched is None:
        return None
    raw_count = matched.group(1)
    if raw_count.isdecimal():
        days = int(raw_count)
    elif raw_count in _CN_DIGITS:
        days = _CN_DIGITS[raw_count]
    else:
        # 「十一天后」落这里:复合中文数字不猜,引导换成数字写法
        raise DueParseError(f"「{phrase}」这样的天数我算不准,请用数字写,比如「11天后」。")
    if days < 1:
        raise DueParseError("天数得从 1 开始;今天到期就直接说「今天」。")
    if days > _MAX_DAYS_AHEAD:
        raise DueParseError(
            f"「{phrase}」太远了(超过 {_MAX_DAYS_AHEAD} 天)。"
            "一年开外的安排,请直接写完整日期,比如「2027-08-15」。"
        )
    return today + timedelta(days=days)


def _parse_bare_weekday(phrase: str, today: date) -> date | None:
    matched = _BARE_WEEKDAY_RE.fullmatch(phrase)
    if matched is None:
        return None
    target = _WEEKDAY_TARGETS[matched.group(1)]
    # 约定:未来最近的那个 X,含今天 —— 周六当天说「周六」就是今天,不滚下周
    return today + timedelta(days=(target - today.weekday()) % 7)


def _parse_this_week(phrase: str, today: date) -> date | None:
    matched = _THIS_WEEK_RE.fullmatch(phrase)
    if matched is None:
        return None
    target = _WEEKDAY_TARGETS[matched.group(1)]
    resolved = _monday_of(today) + timedelta(days=target)
    if resolved < today:
        # 约定:已过报错不猜。消息直接给出改法,师傅换一个字就能过
        raise DueParseError(
            f"本周{_WEEKDAY_CHARS[target]}已经过了(今天周{_WEEKDAY_CHARS[today.weekday()]})。"
            f"要记下周的就说“下周{_WEEKDAY_CHARS[target]}”。"
        )
    return resolved


def _parse_next_weeks(phrase: str, today: date) -> date | None:
    matched = _NEXT_WEEKS_RE.fullmatch(phrase)
    if matched is None:
        return None
    weeks = 2 if matched.group(1) == "下下周" else 1
    target = _WEEKDAY_TARGETS[matched.group(2)]
    # 基准是本周一:周日说「下周三」= 3 天后(周日属于本周)。
    # 下周最早也是明天(周一+7 ≥ 今天+1),永远在未来,不存在「已过」分支
    return _monday_of(today) + timedelta(days=7 * weeks + target)


def _last_day_of(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _parse_month_end(phrase: str, today: date) -> date | None:
    if phrase in _THIS_MONTH_END:
        return _last_day_of(today.year, today.month)
    if phrase not in _NEXT_MONTH_END:
        return None
    if today.month == 12:  # 跨年:12 月的「下月」是明年 1 月
        return _last_day_of(today.year + 1, 1)
    return _last_day_of(today.year, today.month + 1)


def _parse_month_day(phrase: str, today: date) -> date | None:
    matched = _MONTH_DAY_RE.fullmatch(phrase)
    if matched is None:
        return None
    month, day = int(matched.group(1)), int(matched.group(2))
    try:
        resolved = date(today.year, month, day)
    except ValueError as exc:
        # 2月30日、13月1号,以及平年的 2月29日,统统在这里拦下
        raise DueParseError(f"今年({today.year}年)没有{month}月{day}日,请检查一下日期。") from exc
    if resolved < today:
        # 约定:「X月Y号」一律今年,不自作聪明滚到明年 —— 跨年歧义留给用户拍板
        raise DueParseError(
            f"{month}月{day}日按今年算是 {resolved.isoformat()},已经过了。"
            "要记明年的请直接写完整日期,如 2027-01-05。"
        )
    return resolved


def _parse_iso(phrase: str, today: date) -> date | None:
    matched = _ISO_RE.fullmatch(phrase)
    if matched is None:
        return None
    year, month, day = (int(part) for part in matched.groups())
    try:
        resolved = date(year, month, day)
    except ValueError as exc:
        raise DueParseError(f"{phrase} 不是真实存在的日期,请检查一下。") from exc
    if resolved < today:
        raise DueParseError(f"{phrase} 已经过了(今天是 {today.isoformat()})。")
    return resolved


_PARSERS: Final[tuple[Callable[[str, date], date | None], ...]] = (
    _parse_relative_day,
    _parse_days_offset,
    _parse_bare_weekday,
    _parse_this_week,
    _parse_next_weeks,
    _parse_month_end,
    _parse_month_day,
    _parse_iso,
)
"""全部整串匹配、词法两两不相交,先后顺序只影响可读性,不影响结果。"""


def parse_due(text: str | None, *, today: date) -> date | None:
    """中文日期短语 → date;None/空白 → None(无期限);看不懂 → DueParseError。

    today 必须由调用方注入(工具层传真日历,测试传固定日),本模块不看系统时钟。
    """
    if text is None or not text.strip():
        return None
    phrase = _strip_noise(text)
    if not phrase:
        # 整句只剩时段词(「上午」):说了时段没说日子。静默当成无期限是猜测,
        # 报错让用户补日子 —— 与「本周X已过报错不猜」同一条哲学
        raise DueParseError(_fallback(text))
    for parser in _PARSERS:
        resolved = parser(phrase, today)
        if resolved is not None:
            return resolved
    raise DueParseError(_fallback(text))


__all__ = ["DueParseError", "format_display", "parse_due"]
