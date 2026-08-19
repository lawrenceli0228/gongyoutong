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

import re
from datetime import date
from types import ModuleType

import pytest

from gyt.agents.schedule import dates
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


# ---------------------------------------------------------------------------
# 繁體写法(2026-08-18 补)—— 中建国际在港施工,监理打的就是「下週三」「後天」
#
# 判据只有一条:**繁體那句的结果必须与对应简体那句完全一致**。
# 不写死日期是刻意的 —— 写死的话,哪天词表口径变了会红两行(简体主表 + 这里),
# 人会以为是繁體这套坏了;跑同一个断言的话,繁體这一组只对「两种写法是否等价」
# 负责,口径变动只红主表一处,方向指得明明白白。
# ---------------------------------------------------------------------------

_HANT_REFERENCE: tuple[tuple[str, str], ...] = (
    # 已经在 _HANT_VARIANTS 里的八对
    ("周", "週"),
    ("后", "後"),
    ("礼", "禮"),
    ("这", "這"),
    ("个", "個"),
    ("号", "號"),
    ("内", "內"),
    ("两", "兩"),
    # 下面这些**今天不在词表里**,放着是给将来加词用的:哪天词表冒出「几天」
    # 「几点」「过去N天」「刚过的周三」这类写法,完备性守卫会立刻要求把对应繁體字
    # 补进 _HANT_VARIANTS —— 不补的话,港方那种写法会被静默打回,不报错不掉测试。
    ("几", "幾"),
    ("时", "時"),
    ("点", "點"),
    ("过", "過"),
    ("刚", "剛"),
)
"""繁简对照的**第二份、独立于生产代码**的来源,只服务下面两条守卫。

生产代码那张 `_HANT_VARIANTS` 是「实际折哪几个字」,这张是「哪些字有繁體异形」;
两张表由两个人在两个文件里各写各的,守卫才有意义 —— 从生产表反推判据等于自证。
"""


def _vocab_chars(module: ModuleType) -> frozenset[str]:
    """把模块里所有大写常量(词表 / 正则 / 举例串)用到的汉字收成一个集合。

    自动扫 ``vars()`` 而不是手工列表名:新加一张词表就自动进这个集合。
    手工名单的失败方式是静默的 —— 漏登记一张表,下面两条守卫少查一片而测试照绿,
    本仓在「同源清单」上已经吃过好几次这个亏。

    ⚠️ 异形字表自身必须跳过:它的键是繁體字,收进来会把「简体词表里不含任何一个键」
    这条判据当场毒死(那正是恒等性的证明)。模块 docstring 不用管 —— ``__doc__``
    这个名字 ``isupper()`` 为假,天然进不来(而它现在满篇繁體例子)。
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
        # 固定偏移词:後
        ("後天", "后天"),
        ("大後天", "大后天"),
        # N天後 / N天內:後 內 兩
        ("3天後", "3天后"),
        ("三天後", "三天后"),
        ("兩天後", "两天后"),
        ("10天內", "10天内"),
        # 周X / 禮拜X:週 禮
        ("週三", "周三"),
        ("週六", "周六"),
        ("禮拜三", "礼拜三"),
        ("禮拜日", "礼拜日"),
        # 本週X / 這週X:週 這
        ("本週日", "本周日"),
        ("這週六", "这周六"),
        # 下週X / 下下週X:週 —— 头一条就是今天实测被打回的那句
        ("下週三", "下周三"),
        ("下週日", "下周日"),
        ("下下週三", "下下周三"),
        # 月底一族:這 個
        ("這個月底", "这个月底"),
        ("下個月底", "下个月底"),
        # X月Y號:號
        ("9月1號", "9月1号"),
        ("8月20號", "8月20号"),
        # 剥噪声那一路也要在折叠之后照常工作
        ("下週三之前", "下周三之前"),
        ("週三上午", "周三上午"),
        ("後天上午之前", "后天上午之前"),
    ],
)
def test_繁體写法与简体写法结果逐条一致(hant: str, hans: str) -> None:
    """港方监理打繁體,必须和简体一个答案。任何一行红 = 那个异形字没折进去。

    先断非空:两边都解析不出来时 ``None == None`` 也会过,那是假绿灯 ——
    而「繁體全线报错」恰恰是这次要修的缺陷本身,不能让断言把它盖住。
    """
    resolved = parse_due(hant, today=BASE)
    assert resolved is not None, f"「{hant}」压根没解析出日期,两边都空的断言不算数"
    assert resolved == parse_due(hans, today=BASE)


@pytest.mark.parametrize(
    ("mixed", "hans"),
    [
        ("下週3", "下周3"),  # 繁週 + 手机常打的阿拉伯数字
        ("3天後", "3天后"),  # 数字 + 繁後
        ("兩天后", "两天后"),  # 繁兩 + 简后
        ("这個月底", "这个月底"),  # 简这 + 繁個
        ("後天上午", "后天上午"),  # 繁後 + 简时段词
        ("下週三之前", "下周三之前"),  # 繁週 + 简期限后缀
    ],
)
def test_简繁混打也认(mixed: str, hans: str) -> None:
    """工地上混打是常态:输入法记着繁體、人手快打了简体,一句话里两种都有。

    折叠是**逐字**的(str.translate),所以混打天然成立 —— 这组用例守的是
    「哪天有人把逐字折叠改成整词匹配」:那一改混打立刻全瞎,而纯繁體用例照绿。
    """
    resolved = parse_due(mixed, today=BASE)
    assert resolved is not None, f"「{mixed}」压根没解析出日期"
    assert resolved == parse_due(hans, today=BASE)


def test_简体输入过折叠一个字节都不变() -> None:
    """本次改动对简体的承诺是「一个字节都不许变」,这条就是那句承诺的锁。

    折叠是逐字表驱动的,所以「词表里每个字都折不动」= 任何由词表拼出来的简体串
    都折不动,这比抽查几句强。外加几句真实短语兜住数字 / 半角符号 / 全角括号。
    """
    for char in sorted(_vocab_chars(dates)):
        assert dates._fold_hant(char) == char, f"简体词表里的「{char}」被折叠动了"
    for phrase in ("下周三之前", "10天内", "9月1号", "2026-08-15", "明天上午", "这个月底"):
        assert dates._fold_hant(phrase) == phrase


def test_异形字表封闭_键不在简体词表里且值全指得出词表里的词() -> None:
    """两头一起卡死,防它长成一张通用简繁转换表。

    · **键 ∩ 简体词表 = 空**:这是「简体输入是恒等映射」的结构性证明。
      哪天有人把「干→幹」这类**两边都在用**的字收进来,这条当场红 ——
      那种字一折就会把简体的「干活」也改掉(lang-lib.ts 头注踩过同一个坑)。
    · **值 ⊆ 简体词表**:表里每个字都要指得出词表里的哪个词。多收一个字
      = 悄悄扩了词表,而且是没有任何用例盯着的那种扩法。
    """
    vocab = _vocab_chars(dates)
    both_scripts = sorted(set(dates._HANT_VARIANTS) & vocab)
    assert not both_scripts, f"这些字简繁两边都在用,折了会误伤简体输入:{both_scripts}"
    strays = sorted({hans for hans in dates._HANT_VARIANTS.values() if hans not in vocab})
    assert not strays, f"这些字本模块词表里根本没有,表在往通用转换器长:{strays}"


def test_异形字表完备_词表里凡有繁體异形的字都收全了() -> None:
    """加词表漏收异形字的守卫 —— 这次要修的缺陷,就是「词表里有、折叠表里没有」。

    漏一个字的现场表现是:港方那一种写法被 DueParseError 打回,而报错还举例
    叫他改用简体写法。零报错、零测试红、没人会发现,只有港方觉得这系统不认人话。
    """
    vocab = _vocab_chars(dates)
    missing = {
        hans: hant
        for hans, hant in _HANT_REFERENCE
        if hans in vocab and hant not in dates._HANT_VARIANTS
    }
    assert not missing, f"词表里用到了这些字,但它们的繁體写法没收进折叠表:{missing}"


def test_繁體输入的展示格式仍是简体() -> None:
    """**只折输入,不折输出。**

    屏幕上的繁體由前端渲染层转(docs/W12_三语切换_方案.md §5.3:后端一律不转)。
    这里要是顺手把 format_display 也转了,就成了两处各转一半 —— 前端那道会把
    已经是繁體的字再转一遍,而且台账里存的、docx 上印的字形会跟着漂。
    """
    resolved = parse_due("下週三", today=BASE)
    assert resolved is not None
    assert format_display(resolved) == "8月12日(周三)"


def test_看不懂的繁體写法报错引用的是用户原话() -> None:
    """兜底报错引的是**没折过**的原话:师傅打的什么就念什么,不许把他的字改了念。

    (折过的串确实会进另外几条报错文案 —— 那是刻意的,见 dates._fold_hant 的注释:
    那些话上屏前还要过前端的繁體渲染层,在这儿还原等于同一个字来回转两次。)
    """
    with pytest.raises(DueParseError) as caught:
        parse_due("後年開工那天", today=BASE)
    assert "後年開工那天" in str(caught.value)
