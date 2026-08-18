"""中文时间短语 → **过去方向**的日期/日期段 —— 考勤查询的日历层。

===========================================================================
⚠️ 与 schedule/dates.py 是「同口径、反方向」的双胞胎,不许合并
---------------------------------------------------------------------------
「周三」这两个字,在任务台账里是**未来**最近的周三(期限朝前看),
在考勤查询里是**过去**最近的周三(记录朝后看)—— 共用一份词表必然有一边错
(W7 方案 §4.6 的结论)。所以本文件与 schedule/dates.py 各自独立、头注互相指认;
下一个人想来「消除重复」之前,先想清楚哪一边的方向要被牺牲掉。
为什么日期在代码里算而不是让模型算,理由与 dates.py 顶部完全相同,不再抄一遍。

两边**必须一致**的口径(改任何一边都要对照另一边):
  · 一周从周一起算(_monday_of 只写一处);
  · 「周X」含今天:周三当天说「周三」就是今天;
  · 时段词(上午/下午…)前后缀都剥 —— 查询只到天粒度;
  · 中文数字只收单字(一~十、两),复合写法报错引导用数字;
  · 展示格式:半角括号、月日不补零、周日写「周日」不写「周天」。

⚠️ 繁體折叠表(_HANT_VARIANTS)两边**也各有一份,而且内容不一样**:各自只收
自己词表里真出现的字 —— schedule 那边比这边多「後」「內」(後天 / N天後 /
N天內),本文件压根没有这几个词形(考勤不看未来)。所以这张表同样不许合并:
合了就是往这边塞两个永远用不上的字,「封闭」这条判据当场失效。

两边**刻意不同**的地方(方向差异,不是漏抄):
  · 未来日期一律报错:考勤查未来必然是空结果,报错比默默返回空诚实;
  · 不剥「之前/以前/前」尾巴:「周三之前」在期限里就等于「周三」,在查询里是
    另一种(开区间)语义,剥掉等于静默把问题改窄 —— 报错让用户换说法;
  · 空输入不是错误也不是 None,而是**缺省口径**(范围=本月、单日=今天):
    查询是只读的,缺省的起止会随 range_display / day_display 一字不差回给用户,
    师傅看到起止不对当场能纠 —— 与「写台账宁可报错不猜」不矛盾:那边猜错会落库,
    这边猜错只是多问一句。

===========================================================================
词表 v1(冻结;改词表要同步改 prompt.md 的示范问法与红线措辞)
---------------------------------------------------------------------------
示例按 today=2026-08-12(周三)推算:

  (空)/ None            → parse_range = 本月(8-01 ~ 8-12);parse_day = 今天
  今天/今日 · 昨天/昨日   → 8-12 · 8-11;前天 → 8-10;大前天 → 8-09
  周X / 星期X / 礼拜X     → 过去最近的那个 X,含今天:「周五」→ 8-07;X 收 一~六/日/天/1~7
  本周X / 这周X           → 本日历周(周一起点)的 X;还没到 → 报错
  上周X                   → 上一个日历周的 X:「上周三」→ 8-05
  本周 / 这周             → 周一 ~ 今天(8-10 ~ 8-12)
  上周                    → 上周一 ~ 上周日(8-03 ~ 8-09)
  本月 / 这个月           → 月初 ~ 今天(8-01 ~ 8-12)
  上月 / 上个月           → 上月整月(7-01 ~ 7-31)
  最近N天 / 近N天         → 今天往回数 N 天,含今天:「最近7天」→ 8-06 ~ 8-12
  X月Y号 / X月Y日         → 一律今年;假日期或还没到 → 报错(提示写完整日期)
  YYYY-MM-DD              → 校验真日期;还没到 → 报错
  时段词                  → 前后缀剥掉(早上/上午/中午/下午/晚上/傍晚/一早)
  其余一切                → RangeParseError,消息紧凑列出支持写法

「明天」「下周三」「月底」这些**朝未来**的词形刻意不收:考勤没有未来,
碰到就落进兜底报错 —— 这是方向约定的一部分,不是词表漏了。
today 由调用方注入(工具层传考勤口径的今天,测试传固定日),本模块不看系统时钟。

===========================================================================
简繁两种写法都认(2026-08-18 补 —— 港方监理打的就是繁體)
---------------------------------------------------------------------------
中建国际在港施工,现场会打「上週三」「這個月」「上個禮拜」「最近兩天」「8月5號」。
上面那张词表全是简体,所以**解析入口先把繁體异形字折成简体**(_fold_hant),
之后一切照旧 —— 词表、正则、报错文案一个字没动。

三条边界,越过任何一条都会静默坏(与 schedule/dates.py 逐字同款,那边写得更全):
  · **只折输入,不折输出。** format_day / format_range 产出的
    「8月12日(周三)」仍是简体 —— 屏幕上的繁體由前端渲染层转
    (docs/W12_三语切换_方案.md §5.3:「后端一律不转,只有前端渲染层转」);
  · **不引转换库**(opencc 之流会动 uv.lock = 重建镜像,见 TODO-40);
  · **字表封闭**:只收本文件词表里真会出现的字,不做通用简繁转换。

⚠️ 折叠**不会**把朝未来的词形放进来:「後天」折不折都不在词表里,照样报错 ——
   本文件的表里根本没有「後」这个键(见 _HANT_VARIANTS 的说明)。
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Final


class RangeParseError(ValueError):
    """时间短语解析失败。str(exc) 是给工地师傅看的中文人话,并附正确说法示例。"""


Span = tuple[date, date]
"""一段日子:(起, 止),两端都含。单日就是 (d, d)。"""

_WEEKDAY_CHARS: Final[str] = "一二三四五六日"
"""下标 = date.weekday()(周一=0 … 周日=6)。展示与报错共用;周日写「周日」不写「周天」。"""

_FIXED_DAYS: Final[dict[str, int]] = {
    "今天": 0,
    "今日": 0,
    "昨天": 1,
    "昨日": 1,
    "前天": 2,
    "大前天": 3,
}
"""固定偏移词(往回数)。与 dates.py 的 今天/明天/后天/大后天 一族镜像对称。"""

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
"""单字中文数字。「十五」这类复合写法**刻意**不收,报错引导用数字 ——
口径与 schedule/dates.py 相同;这份小表是**有意重复**的(§4.6:两文件不共用),
别为省这十行去 import 那边。"""

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
"""时段词,前缀后缀都剥 —— 查询只到天粒度,「昨天下午」=「昨天」。
⚠️ 这里没有 dates.py 那份「之前/以前/前」尾巴表,是刻意的(见模块头注)。"""

_THIS_MONTH_WORDS: Final[frozenset[str]] = frozenset({"本月", "这个月", "这月"})
_LAST_MONTH_WORDS: Final[frozenset[str]] = frozenset({"上月", "上个月"})

_RECENT_RE: Final[re.Pattern[str]] = re.compile(r"(?:最近|近)(.+)天")
"""「最近N天」与「近N天」同义:今天往回 N 天,含今天。"""

_BARE_WEEKDAY_RE: Final[re.Pattern[str]] = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天1-7])")
_THIS_WEEKDAY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:本|这)(?:个)?(?:周|星期|礼拜)([一二三四五六日天1-7])"
)
_LAST_WEEKDAY_RE: Final[re.Pattern[str]] = re.compile(
    r"上(?:个)?(?:周|星期|礼拜)([一二三四五六日天1-7])"
)
_THIS_WEEK_RE: Final[re.Pattern[str]] = re.compile(r"(?:本|这)(?:个)?(?:周|星期|礼拜)")
_LAST_WEEK_RE: Final[re.Pattern[str]] = re.compile(r"上(?:个)?(?:周|星期|礼拜)")
_MONTH_DAY_RE: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})月(\d{1,2})[号日]")
_ISO_RE: Final[re.Pattern[str]] = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

_MAX_RECENT_DAYS: Final[int] = 366
"""「最近N天」的上界(一年,含闰)。没有这道闸,超大数字会让 today-timedelta 抛
OverflowError,穿透 RangeParseError 边界被 tool_guard 兜成「系统开小差」——
把用户输入问题说成系统故障(dates.py 的 _MAX_DAYS_AHEAD 同一课)。"""

_EXAMPLES: Final[str] = (
    "今天 / 昨天 / 周三 / 上周三 / 本周 / 上周 / 这个月 / 上月 / 最近7天 / 8月5日"
)
"""兜底报错里的举例:每类词法各挑一个代表,一行念完 —— 不是文档。"""

_DAY_EXAMPLES: Final[str] = "今天 / 昨天 / 前天 / 周三 / 上周五 / 8月5日"
"""单日解析(parse_day)的举例:只列单日词形,别把「这个月」递给用户又拒收。"""

_HANT_VARIANTS: Final[dict[str, str]] = {
    "週": "周",  # 週三 / 上週 / 上週三 / 本週
    "禮": "礼",  # 禮拜三 / 上個禮拜
    "這": "这",  # 這週三 / 這個月
    "個": "个",  # 這個月 / 上個月 / 上個禮拜
    "號": "号",  # 8月5號
    "兩": "两",  # 最近兩天
}
"""繁體异形字 → 简体。**逐条对着上面那张词表数出来的,没有第七个字。**

怎么确定它封闭:把词表与正则里出现的汉字全摊开(今 天 日 昨 前 大 / 一 二 两
三 四 五 六 七 八 九 十 / 早 上 午 中 下 晚 傍 / 最 近 / 本 这 个 月 上 /
周 星 期 礼 拜 / 号),逐字问一句「它的繁體是不是另一个字形」—— 只有这六个是。

🔴 **比 schedule/dates.py 那份少「後」和「內」,这是对的不是漏抄。**
那两个字只出现在「後天 / 大後天 / N天後 / N天內」里,而这几个词形本文件
**刻意不收**(考勤没有未来)。收进来的话:①「後天」折成「后天」照样报错、
行为一个字不变;② 但表里从此躺着两个本模块永远用不上的字,下一个人就会以为
「这表是通用简繁表,随手加字没关系」—— 封闭性是靠「每个字都指得出词表里的
哪个词」撑住的。test_attendance_ranges.py 有一条钉着「表里没有多余的字」。

⚠️ 这是**折叠**不是转换:六个键全是简体里用不到的字形,所以纯简体输入过它是
恒等映射 —— 一个字节都不会变。
⚠️ **加词表就要回来数一遍。** 哪天这边真收了「N天內」,「內」就得补进来 ——
测试里那张参考对照表会替你喊(它盯着「词表里有繁體异形的字必须收全」)。
"""

_HANT_TRANS: Final[dict[int, str]] = str.maketrans(_HANT_VARIANTS)
"""str.translate 用的码点表:一遍扫串搞定,不是逐字 replace 扫六遍。"""


def format_day(d: date, *, today: date) -> str:
    """「8月12日(周三)」—— 单日的展示格式,模型必须照抄。

    口径同 schedule.format_display:半角括号、月日不补零、周日写「周日」。
    差异只有一条:**跨年才带年份**(「2025年12月31日(周三)」)—— 考勤能查去年,
    不带年份师傅会当成今年的。today 只用来判断要不要带年。
    """
    core = f"{d.month}月{d.day}日(周{_WEEKDAY_CHARS[d.weekday()]})"
    return f"{d.year}年{core}" if d.year != today.year else core


def format_range(date_from: date, date_to: date, *, today: date) -> str:
    """「8月1日到8月12日」—— 日期段的展示格式,模型必须照抄。

    范围两端不带星期(两个括号太吵,师傅只关心起止),跨年才带年份;
    起止是同一天就退成 format_day —— 「8月10日到8月10日」是机器话,不说。
    """
    if date_from == date_to:
        return format_day(date_from, today=today)
    return f"{_bare_day(date_from, today)}到{_bare_day(date_to, today)}"


def _bare_day(d: date, today: date) -> str:
    core = f"{d.month}月{d.day}日"
    return f"{d.year}年{core}" if d.year != today.year else core


def _fold_hant(text: str) -> str:
    """繁體异形字折成简体 —— 下面所有词表都是简体,所以要在**进词表之前**折。

    ⚠️ 折完的串会进几条报错文案(港方打「最近兩三天」,报错里引的是「最近两三天」)。
    刻意不还原:那句话上屏前还要过前端渲染层的繁體转换,还原等于同一个字来回转
    两次;而引用用户原话的两条兜底报错(_fallback / _day_fallback)以及 parse_day
    的「是一段日子」那句,拿的都是**没折过**的 text。
    """
    return text.translate(_HANT_TRANS)


def _strip_noise(text: str) -> str:
    """先折繁體异形字,再反复剥空白与时段词(前后缀),直到稳定。

    「昨天下午」要剥一轮,「上午 昨天 」这种粘贴出来的也要能剥干净,
    所以循环到不动点。⚠️ 不剥「之前/前」尾巴,理由见模块头注。

    折叠挂在这里而不是 parse_range / parse_day 各写一遍:两个入口都必须先过这道门,
    挂在唯一的必经之路上,就不存在「将来多一个入口忘了折」这种漏法。
    """
    current = _fold_hant(text)
    while True:
        candidate = current.strip()
        for word in _TIME_WORDS:
            if candidate.startswith(word):
                candidate = candidate[len(word) :]
            if candidate.endswith(word):
                candidate = candidate[: -len(word)]
        if candidate == current:
            return candidate
        current = candidate


def _fallback(raw: str) -> str:
    return f"「{raw.strip()}」没听懂指哪段时间。可以这样说:{_EXAMPLES}。"


def _day_fallback(raw: str) -> str:
    return f"「{raw.strip()}」没听懂指哪天。可以这样说:{_DAY_EXAMPLES}。"


def _monday_of(today: date) -> date:
    """本日历周的周一 —— 本周/上周共用的锚点,一周起点约定只写这一处。"""
    return today - timedelta(days=today.weekday())


def _last_day_of(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


# ---------------------------------------------------------------------------
# 逐个词法的解析器:命中返回 Span,不认识返回 None,认识但不合法直接 raise
# ---------------------------------------------------------------------------


def _parse_fixed_day(phrase: str, today: date) -> Span | None:
    offset = _FIXED_DAYS.get(phrase)
    if offset is None:
        return None
    resolved = today - timedelta(days=offset)
    return resolved, resolved


def _parse_recent_days(phrase: str, today: date) -> Span | None:
    matched = _RECENT_RE.fullmatch(phrase)
    if matched is None:
        return None
    raw_count = matched.group(1)
    if raw_count.isdecimal():
        days = int(raw_count)
    elif raw_count in _CN_DIGITS:
        days = _CN_DIGITS[raw_count]
    else:
        # 「最近十五天」落这里:复合中文数字不猜,引导换成数字写法
        raise RangeParseError(f"「{phrase}」这样的天数我算不准,请用数字写,比如「最近15天」。")
    if days < 1:
        raise RangeParseError("天数得从 1 开始;只看今天就直接说「今天」。")
    if days > _MAX_RECENT_DAYS:
        raise RangeParseError(
            f"「{phrase}」太久了(超过 {_MAX_RECENT_DAYS} 天)。"
            "要查更早的,可以按月说(比如「上月」),或直接写日期,比如「2025-08-05」。"
        )
    # 含今天:「最近1天」= 今天,所以往回退 N-1 天
    return today - timedelta(days=days - 1), today


def _parse_this_weekday(phrase: str, today: date) -> Span | None:
    matched = _THIS_WEEKDAY_RE.fullmatch(phrase)
    if matched is None:
        return None
    target = _WEEKDAY_TARGETS[matched.group(1)]
    resolved = _monday_of(today) + timedelta(days=target)
    if resolved > today:
        # 约定:还没到的日子报错不猜(dates.py 是「已过报错」,方向相反、哲学相同)
        raise RangeParseError(
            f"本周{_WEEKDAY_CHARS[target]}还没到(今天周{_WEEKDAY_CHARS[today.weekday()]})。"
            f"要查上周的就说“上周{_WEEKDAY_CHARS[target]}”。"
        )
    return resolved, resolved


def _parse_last_weekday(phrase: str, today: date) -> Span | None:
    matched = _LAST_WEEKDAY_RE.fullmatch(phrase)
    if matched is None:
        return None
    target = _WEEKDAY_TARGETS[matched.group(1)]
    # 上周最晚是上周日(= 本周一的前一天),永远在过去,不存在「还没到」分支
    resolved = _monday_of(today) - timedelta(days=7) + timedelta(days=target)
    return resolved, resolved


def _parse_bare_weekday(phrase: str, today: date) -> Span | None:
    matched = _BARE_WEEKDAY_RE.fullmatch(phrase)
    if matched is None:
        return None
    target = _WEEKDAY_TARGETS[matched.group(1)]
    # 约定:过去最近的那个 X,含今天 —— 周三当天说「周三」就是今天,不滚上周。
    # (today.weekday() - target) % 7 = 往回退几天;dates.py 是 (target - weekday) % 7 往前。
    resolved = today - timedelta(days=(today.weekday() - target) % 7)
    return resolved, resolved


def _parse_this_week(phrase: str, today: date) -> Span | None:
    if _THIS_WEEK_RE.fullmatch(phrase) is None:
        return None
    # 周一当天「本周」就是一天(8-10 ~ 8-10),format_range 会退成单日展示
    return _monday_of(today), today


def _parse_last_week(phrase: str, today: date) -> Span | None:
    if _LAST_WEEK_RE.fullmatch(phrase) is None:
        return None
    monday = _monday_of(today)
    return monday - timedelta(days=7), monday - timedelta(days=1)


def _parse_month(phrase: str, today: date) -> Span | None:
    if phrase in _THIS_MONTH_WORDS:
        return date(today.year, today.month, 1), today
    if phrase not in _LAST_MONTH_WORDS:
        return None
    if today.month == 1:  # 跨年:1 月的「上月」是去年 12 月
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    return date(year, month, 1), _last_day_of(year, month)


def _parse_month_day(phrase: str, today: date) -> Span | None:
    matched = _MONTH_DAY_RE.fullmatch(phrase)
    if matched is None:
        return None
    month, day = int(matched.group(1)), int(matched.group(2))
    try:
        resolved = date(today.year, month, day)
    except ValueError as exc:
        # 2月30日、13月1号,以及平年的 2月29日,统统在这里拦下
        raise RangeParseError(f"今年({today.year}年)没有{month}月{day}日,请检查一下日期。") from exc
    if resolved > today:
        # 约定:「X月Y号」一律今年,不自作聪明滚回去年 —— 跨年歧义留给用户拍板
        raise RangeParseError(
            f"{month}月{day}日按今年算是 {resolved.isoformat()},还没到,考勤只能查已经过去的日子。"
            "要查去年的,请写带年份的完整日期。"
        )
    return resolved, resolved


def _parse_iso(phrase: str, today: date) -> Span | None:
    matched = _ISO_RE.fullmatch(phrase)
    if matched is None:
        return None
    year, month, day = (int(part) for part in matched.groups())
    try:
        resolved = date(year, month, day)
    except ValueError as exc:
        raise RangeParseError(f"{phrase} 不是真实存在的日期,请检查一下。") from exc
    if resolved > today:
        raise RangeParseError(
            f"{phrase} 还没到(今天是 {today.isoformat()}),考勤只能查已经过去的日子。"
        )
    return resolved, resolved


_PARSERS: Final[tuple[Callable[[str, date], Span | None], ...]] = (
    _parse_fixed_day,
    _parse_recent_days,
    _parse_this_weekday,
    _parse_last_weekday,
    _parse_bare_weekday,
    _parse_this_week,
    _parse_last_week,
    _parse_month,
    _parse_month_day,
    _parse_iso,
)
"""全部整串匹配、词法两两不相交,先后顺序只影响可读性,不影响结果。
(「上周三」整串含尾字,fullmatch 不会被「上周」吃掉;反之亦然。)"""


def _resolve(phrase: str, today: date) -> Span | None:
    for parser in _PARSERS:
        span = parser(phrase, today)
        if span is not None:
            return span
    return None


def parse_range(text: str | None, *, today: date) -> Span:
    """中文时间短语 → (起, 止);空/None → 本月(缺省口径);看不懂 → RangeParseError。

    today 必须由调用方注入(工具层传考勤口径的今天,测试传固定日),本模块不看系统时钟。
    """
    if text is None or not text.strip():
        # 缺省 = 本月。为什么敢缺省不报错,见模块头注「刻意不同」第三条。
        return date(today.year, today.month, 1), today
    phrase = _strip_noise(text)
    if not phrase:
        # 整句只剩时段词(「上午」):说了时段没说日子,报错让用户补日子
        raise RangeParseError(_fallback(text))
    span = _resolve(phrase, today)
    if span is None:
        raise RangeParseError(_fallback(text))
    return span


def parse_day(text: str | None, *, today: date) -> date:
    """中文时间短语 → 单个日子;空/None → 今天(缺省口径);看不懂/是一段 → RangeParseError。

    「这个月」「上周」这类多日词形在这里**报错不截断**:默默取一段的某一天是猜测,
    而「今天谁到了」问的就是具体一天 —— 让用户挑一天,比替他挑诚实。
    """
    if text is None or not text.strip():
        return today
    phrase = _strip_noise(text)
    if not phrase:
        raise RangeParseError(_day_fallback(text))
    span = _resolve(phrase, today)
    if span is None:
        raise RangeParseError(_day_fallback(text))
    date_from, date_to = span
    if date_from != date_to:
        raise RangeParseError(
            f"「{text.strip()}」是一段日子,看某天的明细得挑一天。可以这样说:{_DAY_EXAMPLES}。"
        )
    return date_from


__all__ = ["RangeParseError", "Span", "format_day", "format_range", "parse_day", "parse_range"]
