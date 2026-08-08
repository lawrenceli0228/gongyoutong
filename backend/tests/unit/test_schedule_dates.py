"""日历层(agents/schedule/dates.py)的表驱动单测 —— 纯函数,不联网、不碰库。

中文相对日期错一天,台账就误导一整个工地,所以这里锁的不是覆盖率,
而是几类最容易「静默算错」的口径:
  · 一周起点(周一):周日那天说「下周三」必须是 3 天后,不是 10 天后;
  · 「周X」= 未来最近**含今天**:周六当天说「周六」不许滚到下周六;
  · 「本周X」已过要报错不猜,不许静默滚成下周;
  · 跨年:12 月说「下月底」「明天」要落进明年,不能在今年打转;
  · 历史日期(昨天、已过的月日)一律报错 —— 静默收下一个过去的日子,
    等于记了一条生下来就逾期的任务。

基准日统一 today=2026-08-08(周六),与计划文档 §6 的示例同源。
today 全部显式注入:parse_due 若偷偷调 date.today(),这批用例过了今天就会翻红。
"""

from __future__ import annotations

from datetime import date

import pytest

from gyt.agents.schedule.dates import DueParseError, format_display, parse_due

BASE = date(2026, 8, 8)
"""2026-08-08,周六 —— 计划文档 §6 所有示例的推算基准。"""

SUNDAY = date(2026, 8, 9)
"""2026-08-09,周日 —— 「周一为一周起点」约定的关键证据日。"""


# ---------------------------------------------------------------------------
# 主表:基准日 2026-08-08(周六)逐条锁定词表 v1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 固定偏移词(今日/明日与今天/明天同义)
        ("今天", date(2026, 8, 8)),
        ("今日", date(2026, 8, 8)),
        ("明天", date(2026, 8, 9)),
        ("明日", date(2026, 8, 9)),
        ("后天", date(2026, 8, 10)),
        ("大后天", date(2026, 8, 11)),
        # N天后 / N天内 同义;中文数字只收单字(一~十、两)
        ("3天后", date(2026, 8, 11)),
        ("三天后", date(2026, 8, 11)),
        ("两天后", date(2026, 8, 10)),
        ("10天内", date(2026, 8, 18)),
        # 周X = 未来最近的那个 X,含今天(基准日是周六)
        ("周三", date(2026, 8, 12)),
        ("周六", date(2026, 8, 8)),
        ("星期天", date(2026, 8, 9)),
        ("礼拜日", date(2026, 8, 9)),
        ("周3", date(2026, 8, 12)),
        ("周7", date(2026, 8, 9)),
        # 本周/这周:周一起点,等于今天合法(已过的见报错组)
        ("本周日", date(2026, 8, 9)),
        ("这周六", date(2026, 8, 8)),
        # 下周 = 本周一+7;下下周 = 本周一+14
        ("下周三", date(2026, 8, 12)),
        ("下周日", date(2026, 8, 16)),
        ("下下周三", date(2026, 8, 19)),
        # 月底一族
        ("月底", date(2026, 8, 31)),
        ("本月底", date(2026, 8, 31)),
        ("下月底", date(2026, 9, 30)),
        # X月Y号 / X月Y日:一律今年,等于今天合法
        ("9月1号", date(2026, 9, 1)),
        ("8月20日", date(2026, 8, 20)),
        ("8月8号", date(2026, 8, 8)),
        # YYYY-MM-DD:等于今天合法
        ("2026-08-15", date(2026, 8, 15)),
        ("2026-08-08", date(2026, 8, 8)),
        # 时段词(前缀/后缀)与期限后缀的剥离,含需要多轮剥的叠加写法
        ("明天上午", date(2026, 8, 9)),
        ("上午明天", date(2026, 8, 9)),
        ("下周三之前", date(2026, 8, 12)),
        ("月底前", date(2026, 8, 31)),
        ("明天上午之前", date(2026, 8, 9)),
    ],
)
def test_基准日词表逐条锁定(text: str, expected: date) -> None:
    """词表 v1 主表。任何一行翻红 = 词表口径被动过,先查 dates.py 再动本表。"""
    assert parse_due(text, today=BASE) == expected


# ---------------------------------------------------------------------------
# 空输入:无期限,不是错误
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
def test_空输入是无期限不是报错(text: str | None) -> None:
    """空 = 用户没提期限,必须返回 None。

    这里若报错,add_task 的「无期限任务」路径(due 默认空串)就整个断了;
    全角空格单列 —— 手机输入法最爱塞它,str.strip() 漏掉就是静默炸。
    """
    assert parse_due(text, today=BASE) is None


# ---------------------------------------------------------------------------
# 报错组:词表外与病态写法,一律 DueParseError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "昨天",  # 历史日期,台账不记过去
        "前天",
        "3天前",  # 剥掉尾部「前」剩「3天」,不在词表 —— 历史语义,报错正确
        "周末",  # 周六还是周日?不猜
        "十一天后",  # 复合中文数字不收,报错引导用数字
        "大大后天",
        "下个月5号",
        "0天后",  # 天数必须是正整数
        "上午",  # 只有时段没有日子 ≠ 无期限,报错引导补日子
        "13月1号",  # 月份越界
    ],
)
def test_词表外与病态写法一律报错(text: str) -> None:
    """报错不猜:这里任何一行「碰巧解析成功」,都说明解析器越权扩了词表。"""
    with pytest.raises(DueParseError):
        parse_due(text, today=BASE)


def test_看不懂时兜底消息列出支持写法() -> None:
    """兜底消息会被工具层原样透传成 user_msg,必须自带「怎么说才对」的示例。"""
    with pytest.raises(DueParseError) as caught:
        parse_due("等老板回来那天", today=BASE)
    message = str(caught.value)
    for example in ("3天后", "下周三", "月底", "2026-08-15"):
        assert example in message, f"兜底消息缺示例 {example}: {message}"


def test_本周已过报错并教用户换说法() -> None:
    """周六说「本周三」= 已过。静默滚到下周是最危险的猜测;
    报错消息还要直接给出正确说法(「下周三」),师傅改一个字就能过。"""
    with pytest.raises(DueParseError) as caught:
        parse_due("本周三", today=BASE)
    message = str(caught.value)
    assert "已经过" in message
    assert "下周三" in message


def test_月日已过报错并提示写完整日期() -> None:
    """「X月Y号」冻结为一律今年,已过不许自作聪明滚到明年 —— 歧义留给用户拍板。"""
    with pytest.raises(DueParseError) as caught:
        parse_due("8月1号", today=BASE)
    assert "要记明年的请直接写完整日期" in str(caught.value)


@pytest.mark.parametrize("text", ["2月30日", "2026-02-30"])
def test_假日期报错(text: str) -> None:
    """字符串形状合法、日历上不存在的日期必须拦住,不能让它带病进台账。"""
    with pytest.raises(DueParseError):
        parse_due(text, today=BASE)


def test_ISO已过报错() -> None:
    with pytest.raises(DueParseError, match="已经过"):
        parse_due("2026-08-01", today=BASE)


# ---------------------------------------------------------------------------
# 边界日之一:周日(2026-08-09)—— 周一起点约定的证据
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 周日属于本周(周一起点的最后一天),所以「下周三」是 3 天后的 8-12,
        # 不是 10 天后的 8-19 —— 这一行就是一周起点约定的锁
        ("下周三", date(2026, 8, 12)),
        ("周日", date(2026, 8, 9)),  # 未来最近含今天
        ("本周日", date(2026, 8, 9)),  # 等于今天,合法不报错
    ],
)
def test_周日当天的一周边界(text: str, expected: date) -> None:
    assert parse_due(text, today=SUNDAY) == expected


# ---------------------------------------------------------------------------
# 边界日之二:十二月 —— 月底与跨年
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("月底", date(2026, 12, 31)),
        ("下月底", date(2027, 1, 31)),  # 12 月的「下月」是明年 1 月,不能在今年打转
    ],
)
def test_十二月中旬的月底口径(text: str, expected: date) -> None:
    assert parse_due(text, today=date(2026, 12, 15)) == expected


def test_十二月说一月五号按今年算已过() -> None:
    """跨年歧义最大的一句:12 月说「1月5号」多半想说明年,但词表冻结为
    一律今年 → 报错并引导写完整日期。改成静默滚明年 = 破坏冻结约定。"""
    with pytest.raises(DueParseError) as caught:
        parse_due("1月5号", today=date(2026, 12, 15))
    assert "要记明年的请直接写完整日期" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("月底", date(2026, 12, 31)),  # 今天就是月底:等于今天,合法
        ("明天", date(2027, 1, 1)),  # 跨年进位
    ],
)
def test_年末最后一天边界(text: str, expected: date) -> None:
    assert parse_due(text, today=date(2026, 12, 31)) == expected


# ---------------------------------------------------------------------------
# format_display:复述闭环的展示格式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 8, 14), "8月14日(周五)"),
        (date(2026, 8, 16), "8月16日(周日)"),
        (date(2026, 8, 9), "8月9日(周日)"),  # 不补零:8月9日,不是 08月09日
    ],
)
def test_展示格式不补零且周日写作周日(day: date, expected: str) -> None:
    """这串字会被模型**照抄**给用户(复述闭环),格式本身就是契约:
    半角括号、月日不补零、周日写「周日」不写「周天」。"""
    assert format_display(day) == expected


@pytest.mark.parametrize("phrase", ["400天后", "99999999999999天内"])
def test_天数上界_超一年报错引导写完整日期(phrase: str) -> None:
    """没有上界时,超大数字会让 today+timedelta 抛 OverflowError 穿透解析边界,
    被 tool_guard 兜成「系统开小差」—— 把输入问题说成系统故障(审查 MEDIUM 项)。
    这条锁两件事:仍是 DueParseError(不是别的异常),且消息引导写完整日期。"""
    with pytest.raises(DueParseError, match="完整日期"):
        parse_due(phrase, today=BASE)
