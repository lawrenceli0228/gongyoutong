"""考勤日历层(agents/attendance/ranges.py)的表驱动单测 —— 纯函数,不联网、不碰库。

中文时间短语错一天,考勤就对到别人的工钱上,所以这里锁的不是覆盖率,
而是几类最容易「静默算错」的口径:
  · 方向:「周三」必须是**过去**最近的周三(schedule 那边是未来)——
    两个文件词形几乎一样,方向弄反了没有任何报错;
  · 一周起点(周一):周日那天「本周」必须是整整一周,不是只有周日一天;
  · 「周X」= 过去最近**含今天**:周三当天说「周三」不许滚到上周三;
  · 未来日期一律报错:考勤查未来必然是空,静默返回空会让人以为「那天真没人来」;
  · 跨年:1 月的「上月」要落进去年 12 月,不能在今年打转;
  · 空输入是缺省口径(范围=本月、单日=今天),不是错误 —— 工具的默认参数靠它。

基准日 today=2026-08-12(周三,周中)+ 2026-08-16(周日,一周边界),
与 test_schedule_dates 同一周,人肉对照方便(那边的锚是 2026-08-08 周六)。
today 全部显式注入:parse_* 若偷偷看系统时钟,这批用例过了基准日就会翻红。
"""

from __future__ import annotations

import re
from datetime import date
from types import ModuleType

import pytest

from gyt.agents.attendance import ranges
from gyt.agents.attendance.ranges import (
    RangeParseError,
    format_day,
    format_range,
    parse_day,
    parse_range,
)

BASE = date(2026, 8, 12)
"""2026-08-12,周三 —— 周中基准日,模块 docstring 里所有示例的推算基准。"""

SUNDAY = date(2026, 8, 16)
"""2026-08-16,周日 —— 「周一为一周起点」约定的关键证据日。"""

MONDAY = date(2026, 8, 10)
"""2026-08-10,周一 —— 「本周」缩成单日、「上周」整段让位的边界日。"""


def _span(y1: int, m1: int, d1: int, y2: int, m2: int, d2: int) -> tuple[date, date]:
    return date(y1, m1, d1), date(y2, m2, d2)


# ---------------------------------------------------------------------------
# 主表:基准日 2026-08-12(周三)逐条锁定词表 v1(范围解析)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 固定偏移词(今日/昨日与今天/昨天同义),全是单日
        ("今天", _span(2026, 8, 12, 2026, 8, 12)),
        ("今日", _span(2026, 8, 12, 2026, 8, 12)),
        ("昨天", _span(2026, 8, 11, 2026, 8, 11)),
        ("昨日", _span(2026, 8, 11, 2026, 8, 11)),
        ("前天", _span(2026, 8, 10, 2026, 8, 10)),
        ("大前天", _span(2026, 8, 9, 2026, 8, 9)),
        # 周X = 过去最近的那个 X,含今天(基准日是周三)—— 方向锁,schedule 是未来
        ("周三", _span(2026, 8, 12, 2026, 8, 12)),
        ("周一", _span(2026, 8, 10, 2026, 8, 10)),
        ("周五", _span(2026, 8, 7, 2026, 8, 7)),
        ("星期天", _span(2026, 8, 9, 2026, 8, 9)),
        ("礼拜日", _span(2026, 8, 9, 2026, 8, 9)),
        ("周3", _span(2026, 8, 12, 2026, 8, 12)),
        ("周7", _span(2026, 8, 9, 2026, 8, 9)),
        ("周2", _span(2026, 8, 11, 2026, 8, 11)),
        # 本周X / 这周X:周一起点,等于今天合法(还没到的见报错组)
        ("本周一", _span(2026, 8, 10, 2026, 8, 10)),
        ("这周三", _span(2026, 8, 12, 2026, 8, 12)),
        ("这个星期二", _span(2026, 8, 11, 2026, 8, 11)),
        # 上周X:上个日历周的 X,永远在过去
        ("上周三", _span(2026, 8, 5, 2026, 8, 5)),
        ("上周日", _span(2026, 8, 9, 2026, 8, 9)),
        ("上星期五", _span(2026, 8, 7, 2026, 8, 7)),
        ("上个礼拜一", _span(2026, 8, 3, 2026, 8, 3)),
        # 本周 = 周一~今天;上周 = 上周一~上周日
        ("本周", _span(2026, 8, 10, 2026, 8, 12)),
        ("这周", _span(2026, 8, 10, 2026, 8, 12)),
        ("本星期", _span(2026, 8, 10, 2026, 8, 12)),
        ("这个礼拜", _span(2026, 8, 10, 2026, 8, 12)),
        ("上周", _span(2026, 8, 3, 2026, 8, 9)),
        ("上星期", _span(2026, 8, 3, 2026, 8, 9)),
        ("上个礼拜", _span(2026, 8, 3, 2026, 8, 9)),
        # 本月 = 月初~今天;上月 = 上月整月
        ("本月", _span(2026, 8, 1, 2026, 8, 12)),
        ("这个月", _span(2026, 8, 1, 2026, 8, 12)),
        ("这月", _span(2026, 8, 1, 2026, 8, 12)),
        ("上月", _span(2026, 7, 1, 2026, 7, 31)),
        ("上个月", _span(2026, 7, 1, 2026, 7, 31)),
        # 最近N天 / 近N天:今天往回 N 天,含今天;中文数字只收单字
        ("最近7天", _span(2026, 8, 6, 2026, 8, 12)),
        ("最近1天", _span(2026, 8, 12, 2026, 8, 12)),
        ("近3天", _span(2026, 8, 10, 2026, 8, 12)),
        ("最近三天", _span(2026, 8, 10, 2026, 8, 12)),
        ("最近两天", _span(2026, 8, 11, 2026, 8, 12)),
        # X月Y号 / X月Y日:一律今年,等于今天合法,过去合法
        ("8月5日", _span(2026, 8, 5, 2026, 8, 5)),
        ("8月12号", _span(2026, 8, 12, 2026, 8, 12)),
        ("7月31日", _span(2026, 7, 31, 2026, 7, 31)),
        # YYYY-MM-DD:等于今天合法,去年也合法(考勤能查旧账)
        ("2026-08-05", _span(2026, 8, 5, 2026, 8, 5)),
        ("2026-08-12", _span(2026, 8, 12, 2026, 8, 12)),
        ("2025-12-31", _span(2025, 12, 31, 2025, 12, 31)),
        # 时段词(前缀/后缀)剥离 —— 查询只到天粒度
        ("昨天下午", _span(2026, 8, 11, 2026, 8, 11)),
        ("今天早上", _span(2026, 8, 12, 2026, 8, 12)),
        ("上午昨天", _span(2026, 8, 11, 2026, 8, 11)),
    ],
)
def test_基准日词表逐条锁定(text: str, expected: tuple[date, date]) -> None:
    """词表 v1 主表。任何一行翻红 = 词表口径被动过,先查 ranges.py 再动本表。"""
    assert parse_range(text, today=BASE) == expected


# ---------------------------------------------------------------------------
# 空输入:缺省口径(范围=本月、单日=今天),不是错误
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="None"),
        pytest.param("", id="空串"),
        pytest.param("   ", id="半角空白"),
        pytest.param("　　", id="全角空格"),
    ],
)
def test_空输入范围解析按本月算(text: str | None) -> None:
    """空 = 用户没提时间段,按本月算(缺省会随 range_display 回显给用户,不算猜)。

    这里若报错,list_attendance_days 的「没说时段就查本月」路径整个断了;
    全角空格单列 —— 手机输入法最爱塞它,str.strip() 漏掉就是静默炸。
    """
    assert parse_range(text, today=BASE) == _span(2026, 8, 1, 2026, 8, 12)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="None"),
        pytest.param("", id="空串"),
        pytest.param("   ", id="半角空白"),
    ],
)
def test_空输入单日解析按今天算(text: str | None) -> None:
    """空 = 今天:list_attendance_detail 的默认参数「今天」和空串必须同一个答案。"""
    assert parse_day(text, today=BASE) == BASE


# ---------------------------------------------------------------------------
# 报错组:词表外、朝未来的词形、病态写法,一律 RangeParseError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "明天",  # 朝未来的词形刻意不收 —— 考勤没有未来
        "后天",
        "大后天",
        "下周三",
        "月底",  # schedule 词形,在考勤里是未来语义,不收
        "3天后",
        "周三之前",  # 「之前」尾巴刻意不剥:开区间语义不支持,剥掉=静默改问题
        "周末",  # 周六还是周日?不猜
        "上上周三",  # 只收上周,再往前请写日期
        "大大前天",
        "最近0天",  # 天数必须是正整数
        "最近十五天",  # 复合中文数字不收,报错引导用数字
        "上午",  # 只有时段没有日子,报错引导补日子
        "13月1号",  # 月份越界
        "2月30日",  # 日历上不存在
        "2026-02-30",
        "猴年马月",
    ],
)
def test_词表外与病态写法一律报错(text: str) -> None:
    """报错不猜:这里任何一行「碰巧解析成功」,都说明解析器越权扩了词表。"""
    with pytest.raises(RangeParseError):
        parse_range(text, today=BASE)


def test_看不懂时兜底消息列出支持写法() -> None:
    """兜底消息会被工具层原样透传成 user_msg,必须自带「怎么说才对」的示例。"""
    with pytest.raises(RangeParseError) as caught:
        parse_range("等发工资那天", today=BASE)
    message = str(caught.value)
    for example in ("今天", "昨天", "上周", "这个月", "最近7天", "8月5日"):
        assert example in message, f"兜底消息缺示例 {example}: {message}"


@pytest.mark.parametrize(
    "text",
    [
        "8月20日",  # 按今年算还没到
        "12月1号",
        "2026-09-01",
    ],
)
def test_未来日期报错_考勤只查过去(text: str) -> None:
    """未来日期必须报错而不是返回空结果 —— 静默的空会让人以为「那天真没人来」。"""
    with pytest.raises(RangeParseError, match="还没到"):
        parse_range(text, today=BASE)


def test_本周还没到的日子报错并教用户换说法() -> None:
    """周三说「本周五」= 还没到。报错消息直接给出正确说法(「上周五」),改两个字就能过。"""
    with pytest.raises(RangeParseError) as caught:
        parse_range("本周五", today=BASE)
    message = str(caught.value)
    assert "还没到" in message
    assert "上周五" in message


def test_天数上界_超一年报错引导换说法() -> None:
    """没有上界时,超大数字会让 today-timedelta 抛 OverflowError 穿透解析边界,
    被 tool_guard 兜成「系统开小差」—— 把输入问题说成系统故障(dates.py 同款教训)。
    这条锁两件事:仍是 RangeParseError(不是别的异常),且消息给出替代说法。"""
    for phrase in ("最近400天", "最近99999999999999天"):
        with pytest.raises(RangeParseError, match="太久"):
            parse_range(phrase, today=BASE)


# ---------------------------------------------------------------------------
# parse_day:单日专用入口,多日词形要拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天", date(2026, 8, 12)),
        ("昨天", date(2026, 8, 11)),
        ("周日", date(2026, 8, 9)),
        ("上周三", date(2026, 8, 5)),
        ("8月5日", date(2026, 8, 5)),
        ("昨天下午", date(2026, 8, 11)),
        ("2025-12-31", date(2025, 12, 31)),
    ],
)
def test_单日解析词表(text: str, expected: date) -> None:
    assert parse_day(text, today=BASE) == expected


@pytest.mark.parametrize("text", ["这个月", "上周", "本周", "最近3天", "上月"])
def test_单日解析拒绝一段日子并教挑一天(text: str) -> None:
    """「今天谁到了」问的是一天;默默从一段里挑一天是猜测,必须报错让用户挑。"""
    with pytest.raises(RangeParseError) as caught:
        parse_day(text, today=BASE)
    assert "挑一天" in str(caught.value)


def test_单日解析未来日期照样报错() -> None:
    with pytest.raises(RangeParseError, match="还没到"):
        parse_day("8月20日", today=BASE)


def test_单日解析胡话报错且举例只列单日词形() -> None:
    """parse_day 的兜底举例不许递「这个月」这种范围词 —— 用户照着说又会被拒,来回打脸。"""
    with pytest.raises(RangeParseError) as caught:
        parse_day("猴年马月", today=BASE)
    message = str(caught.value)
    assert "今天" in message
    assert "这个月" not in message


# ---------------------------------------------------------------------------
# 边界日之一:周日(2026-08-16)—— 周一起点约定的证据
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 周日属于本周(周一起点的最后一天),所以「本周」是整整七天 8-10~8-16 ——
        # 这一行就是一周起点约定的锁;起点若错成周日,答案会缩成 (8-16, 8-16)
        ("本周", _span(2026, 8, 10, 2026, 8, 16)),
        ("上周", _span(2026, 8, 3, 2026, 8, 9)),
        ("周日", _span(2026, 8, 16, 2026, 8, 16)),  # 过去最近含今天,不滚上周日
        ("本周日", _span(2026, 8, 16, 2026, 8, 16)),  # 等于今天,合法不报错
        ("周一", _span(2026, 8, 10, 2026, 8, 10)),  # 本周的周一,已过,单日
        ("最近7天", _span(2026, 8, 10, 2026, 8, 16)),
    ],
)
def test_周日当天的一周边界(text: str, expected: tuple[date, date]) -> None:
    assert parse_range(text, today=SUNDAY) == expected


# ---------------------------------------------------------------------------
# 边界日之二:周一(2026-08-10)—— 「本周」缩成单日
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("本周", _span(2026, 8, 10, 2026, 8, 10)),  # 周一当天「本周」只有一天
        ("上周", _span(2026, 8, 3, 2026, 8, 9)),
        ("周一", _span(2026, 8, 10, 2026, 8, 10)),  # 含今天
        ("本周一", _span(2026, 8, 10, 2026, 8, 10)),
        ("昨天", _span(2026, 8, 9, 2026, 8, 9)),
    ],
)
def test_周一当天的一周边界(text: str, expected: tuple[date, date]) -> None:
    assert parse_range(text, today=MONDAY) == expected


def test_周一当天本周二还没到报错() -> None:
    with pytest.raises(RangeParseError, match="还没到"):
        parse_range("本周二", today=MONDAY)


# ---------------------------------------------------------------------------
# 边界日之三:跨年与月初
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("上月", _span(2026, 12, 1, 2026, 12, 31)),  # 1 月的「上月」是去年 12 月
        ("上个月", _span(2026, 12, 1, 2026, 12, 31)),
        ("本月", _span(2027, 1, 1, 2027, 1, 5)),
    ],
)
def test_一月上旬的跨年口径(text: str, expected: tuple[date, date]) -> None:
    assert parse_range(text, today=date(2027, 1, 5)) == expected


def test_一月说十二月按今年算还没到() -> None:
    """跨年歧义最大的一句:1 月说「12月28日」多半想说去年,但词表冻结为一律今年
    → 报错并引导写带年份的完整日期。改成静默滚去年 = 破坏冻结约定。"""
    with pytest.raises(RangeParseError) as caught:
        parse_range("12月28日", today=date(2027, 1, 5))
    message = str(caught.value)
    assert "还没到" in message
    assert "完整日期" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("本月", _span(2026, 8, 1, 2026, 8, 1)),  # 月初第一天:「本月」只有一天
        ("上月", _span(2026, 7, 1, 2026, 7, 31)),
    ],
)
def test_月初第一天的月边界(text: str, expected: tuple[date, date]) -> None:
    assert parse_range(text, today=date(2026, 8, 1)) == expected


# ---------------------------------------------------------------------------
# format_day / format_range:照抄闭环的展示格式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 8, 14), "8月14日(周五)"),
        (date(2026, 8, 9), "8月9日(周日)"),  # 不补零:8月9日,不是 08月09日
        (date(2025, 12, 31), "2025年12月31日(周三)"),  # 跨年带年份 —— 考勤能查旧账
    ],
)
def test_单日展示格式(day: date, expected: str) -> None:
    """这串字会被模型**照抄**给用户(day_display),格式本身就是契约:
    半角括号、月日不补零、周日写「周日」不写「周天」、跨年才带年份。"""
    assert format_day(day, today=BASE) == expected


def test_范围展示格式_不带星期() -> None:
    assert format_range(date(2026, 8, 1), date(2026, 8, 12), today=BASE) == "8月1日到8月12日"


def test_范围展示格式_同一天退成单日() -> None:
    """周一当天的「本周」是 (8-10, 8-10):「8月10日到8月10日」是机器话,必须退成单日。"""
    assert format_range(date(2026, 8, 10), date(2026, 8, 10), today=BASE) == "8月10日(周一)"


def test_范围展示格式_跨年带年份() -> None:
    display = format_range(date(2026, 12, 1), date(2026, 12, 31), today=date(2027, 1, 5))
    assert display == "2026年12月1日到2026年12月31日"


# ---------------------------------------------------------------------------
# 繁體写法(2026-08-18 补)—— 中建国际在港施工,现场打的就是「上週三」「這個月」
#
# 判据只有一条:**繁體那句的结果必须与对应简体那句完全一致**。
# 不写死日期是刻意的 —— 写死的话,哪天词表口径变了会红两行(简体主表 + 这里),
# 人会以为是繁體这套坏了;跑同一个断言的话,口径变动只红主表一处。
#
# ⚠️ 这一整节与 test_schedule_dates.py 那节是**同款、不同表**:那边的折叠表多
#    「後」「內」两个字,因为那边收「後天 / N天後 / N天內」而这边刻意不收
#    (考勤没有未来)。别把两边的表或用例合并,理由写在两份 ranges.py / dates.py 头注。
# ---------------------------------------------------------------------------

_HANT_REFERENCE: tuple[tuple[str, str], ...] = (
    # 已经在本模块 _HANT_VARIANTS 里的六对
    ("周", "週"),
    ("礼", "禮"),
    ("这", "這"),
    ("个", "個"),
    ("号", "號"),
    ("两", "兩"),
    # 🔴 下面两对**故意留在这张参考表里而没进 _HANT_VARIANTS**:
    #    「後」「內」只出现在朝未来的词形里(後天 / N天後 / N天內),本模块不收那些词,
    #    所以折叠表里现在没有它们(完备性守卫因此不会响)。哪天这边真收了「N天內」,
    #    这条守卫会立刻红,提醒把「內」补进折叠表 —— 这正是它存在的理由。
    ("后", "後"),
    ("内", "內"),
    # 给将来加词用的余量,同上口径
    ("几", "幾"),
    ("时", "時"),
    ("点", "點"),
    ("过", "過"),
    ("刚", "剛"),
)
"""繁简对照的**第二份、独立于生产代码**的来源,只服务下面两条守卫。

生产代码那张 `_HANT_VARIANTS` 是「实际折哪几个字」,这张是「哪些字有繁體异形」;
两张表各写各的,守卫才有意义 —— 从生产表反推判据等于自证。
"""


def _vocab_chars(module: ModuleType) -> frozenset[str]:
    """把模块里所有大写常量(词表 / 正则 / 举例串)用到的汉字收成一个集合。

    与 test_schedule_dates.py 那份**逐字相同,且刻意各写一份** —— 两个被测模块
    刻意不共用词表(ranges.py 头注),测试跟着各自模块走,不搭一座跨模块的桥;
    这十来行的重复,比一个「共用测试基建」诱着人把两边词表也合并要便宜得多。

    自动扫 ``vars()`` 而不是手工列表名:新加一张词表就自动进这个集合。手工名单的
    失败方式是静默的 —— 漏登记一张表,下面两条守卫少查一片而测试照绿。

    ⚠️ 异形字表自身必须跳过:它的键是繁體字,收进来会把「简体词表里不含任何一个键」
    这条判据当场毒死。模块 docstring 不用管 —— ``__doc__`` 这名字 ``isupper()``
    为假,天然进不来(而它现在满篇繁體例子)。
    """
    pieces: list[str] = []
    for name, value in vars(module).items():
        if not name.isupper() or name in {"_HANT_VARIANTS", "_HANT_TRANS"}:
            continue
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, re.Pattern):
            pieces.append(value.pattern)
        elif isinstance(value, dict | frozenset | set | tuple | list):
            pieces.extend(item for item in value if isinstance(item, str))
    return frozenset(ch for piece in pieces for ch in piece if "一" <= ch <= "鿿")


@pytest.mark.parametrize(
    ("hant", "hans"),
    [
        # 周X / 禮拜X:週 禮
        ("週三", "周三"),
        ("週五", "周五"),
        ("禮拜日", "礼拜日"),
        # 本週X / 這週X:週 這 個
        ("本週一", "本周一"),
        ("這週三", "这周三"),
        ("這個星期二", "这个星期二"),
        # 上週X:週 禮 個
        ("上週三", "上周三"),
        ("上週日", "上周日"),
        ("上個禮拜一", "上个礼拜一"),
        # 整周:週 這 個 禮
        ("本週", "本周"),
        ("這週", "这周"),
        ("這個禮拜", "这个礼拜"),
        ("上週", "上周"),
        ("上個禮拜", "上个礼拜"),
        # 月:這 個
        ("這個月", "这个月"),
        ("這月", "这月"),
        ("上個月", "上个月"),
        # 最近N天:兩
        ("最近兩天", "最近两天"),
        ("近兩天", "近两天"),
        # X月Y號:號
        ("8月5號", "8月5号"),
        ("8月12號", "8月12号"),
        # 剥时段词那一路也要在折叠之后照常工作
        ("上週三下午", "上周三下午"),
    ],
)
def test_繁體写法与简体写法结果逐条一致(hant: str, hans: str) -> None:
    """港方打繁體,必须和简体一个答案。任何一行红 = 那个异形字没折进去。"""
    assert parse_range(hant, today=BASE) == parse_range(hans, today=BASE)


@pytest.mark.parametrize(
    ("hant", "hans"),
    [
        ("上週三", "上周三"),
        ("禮拜日", "礼拜日"),
        ("這週三", "这周三"),
        ("8月5號", "8月5号"),
    ],
)
def test_单日解析的繁體写法与简体一致(hant: str, hans: str) -> None:
    """parse_day 是第二个入口。折叠挂在两个入口的必经之路(_strip_noise)上,
    这组用例守的就是「别哪天有人把折叠挪进 parse_range 里」—— 挪了这里当场红。"""
    assert parse_day(hant, today=BASE) == parse_day(hans, today=BASE)


@pytest.mark.parametrize(
    ("mixed", "hans"),
    [
        ("上週3", "上周3"),  # 繁週 + 手机常打的阿拉伯数字
        ("这個月", "这个月"),  # 简这 + 繁個
        ("上个禮拜", "上个礼拜"),  # 简个 + 繁禮
        ("最近兩天", "最近两天"),  # 简最近 + 繁兩
        ("上週三下午", "上周三下午"),  # 繁週 + 简时段词
    ],
)
def test_简繁混打也认(mixed: str, hans: str) -> None:
    """工地上混打是常态:输入法记着繁體、人手快打了简体,一句话里两种都有。

    折叠是**逐字**的(str.translate),所以混打天然成立 —— 这组守的是
    「哪天有人把逐字折叠改成整词匹配」:那一改混打立刻全瞎,而纯繁體用例照绿。
    """
    assert parse_range(mixed, today=BASE) == parse_range(hans, today=BASE)


@pytest.mark.parametrize(
    ("hant", "hans"),
    [
        ("後天", "后天"),
        ("大後天", "大后天"),
        ("下週三", "下周三"),
        ("3天後", "3天后"),
        ("10天內", "10天内"),
        ("週三之前", "周三之前"),
    ],
)
def test_朝未来的繁體词形照样报错_方向约定不因简繁而松(hant: str, hans: str) -> None:
    """**这条比「繁體能解析」更要紧。**

    折叠只换字形,不换方向:考勤没有未来,「後天」和「后天」必须都报错。
    哪天有人图省事把 schedule 那张八字表整个抄过来(多「後」「內」),这里的行为
    其实**也不会变**(词表里本来就没有「后天」)—— 所以这组用例真正盯的是
    另一头:别有人顺手把「後天 / N天後」这些词形也一并「补齐」到考勤词表里。
    那一补,查考勤问「後天」会返回一段未来的空记录,而工友会以为那天真没人来。
    """
    with pytest.raises(RangeParseError):
        parse_range(hans, today=BASE)
    with pytest.raises(RangeParseError):
        parse_range(hant, today=BASE)


def test_简体输入过折叠一个字节都不变() -> None:
    """本次改动对简体的承诺是「一个字节都不许变」,这条就是那句承诺的锁。

    折叠是逐字表驱动的,所以「词表里每个字都折不动」= 任何由词表拼出来的简体串
    都折不动,这比抽查几句强。外加几句真实短语兜住数字 / 半角符号。
    """
    for char in sorted(_vocab_chars(ranges)):
        assert ranges._fold_hant(char) == char, f"简体词表里的「{char}」被折叠动了"
    for phrase in ("上周三", "这个月", "最近7天", "8月5号", "2025-12-31", "昨天下午"):
        assert ranges._fold_hant(phrase) == phrase


def test_异形字表封闭_键不在简体词表里且值全指得出词表里的词() -> None:
    """两头一起卡死,防它长成一张通用简繁转换表。

    · **键 ∩ 简体词表 = 空**:这是「简体输入是恒等映射」的结构性证明。
      哪天有人把「干→幹」这类**两边都在用**的字收进来,这条当场红。
    · **值 ⊆ 简体词表**:表里每个字都要指得出词表里的哪个词 —— 这条同时钉死了
      「别把 schedule 那份八字表整个抄过来」:「后」「内」在本模块词表里没有,
      抄过来这条立刻红,而不是安安静静多躺两个用不上的字。
    """
    vocab = _vocab_chars(ranges)
    both_scripts = sorted(set(ranges._HANT_VARIANTS) & vocab)
    assert not both_scripts, f"这些字简繁两边都在用,折了会误伤简体输入:{both_scripts}"
    strays = sorted({hans for hans in ranges._HANT_VARIANTS.values() if hans not in vocab})
    assert not strays, f"这些字本模块词表里根本没有,表在往通用转换器长:{strays}"


def test_异形字表完备_词表里凡有繁體异形的字都收全了() -> None:
    """加词表漏收异形字的守卫。

    漏一个字的现场表现是:港方那一种写法被 RangeParseError 打回,而报错还举例
    叫他改用简体写法。零报错、零测试红、没人会发现,只有港方觉得这系统不认人话。
    ⚠️ 参考表里的「後」「內」今天是**空转**的(本模块词表没有那两个字)——
    那是刻意的,它们等着「哪天这边收了 N天內」的那一刻响。
    """
    vocab = _vocab_chars(ranges)
    missing = {
        hans: hant
        for hans, hant in _HANT_REFERENCE
        if hans in vocab and hant not in ranges._HANT_VARIANTS
    }
    assert not missing, f"词表里用到了这些字,但它们的繁體写法没收进折叠表:{missing}"


def test_繁體输入的展示格式仍是简体() -> None:
    """**只折输入,不折输出。**

    屏幕上的繁體由前端渲染层转(docs/W12_三语切换_方案.md §5.3:后端一律不转)。
    这里要是顺手把 format_day / format_range 也转了,就成了两处各转一半 ——
    前端那道会把已经是繁體的字再转一遍,而凭证水印、台账里存的字形还得另说。
    """
    assert format_range(*parse_range("上週", today=BASE), today=BASE) == "8月3日到8月9日"
    assert format_day(parse_day("上週三", today=BASE), today=BASE) == "8月5日(周三)"


def test_看不懂的繁體写法报错引用的是用户原话() -> None:
    """兜底报错引的是**没折过**的原话:师傅打的什么就念什么,不许把他的字改了念。"""
    with pytest.raises(RangeParseError) as caught:
        parse_range("開工那個月", today=BASE)
    assert "開工那個月" in str(caught.value)
