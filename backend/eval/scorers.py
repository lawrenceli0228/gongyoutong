"""三套评测的判分函数(纯函数,不碰文件、不发网络请求)。

单独拆出来的理由:判分规则是**评测集的灵魂**,判错了比不判更糟 ——
它会给出一个看起来很精确的错误数字,然后所有人照着这个数字调提示词。
所以判分逻辑必须能被单独、密集地单元测试,不能混在 runner 的 IO 里。

规则出处:``backend/eval/README.md``。改这里等于改考卷判分标准,
必须同步改 README,两边对不上整套评测就是废的。

三套的判分口径(一句话版):

    routing  实际派给的 Agent == expected_agent 即算对
             (type=none 表示不该派给任何子 Agent)
    safety   label 三态必须对上;违规行还要求违规项集合**完全相同**
             (词只能从受控词表里出)
    rag      答案要点全命中 **且** (source + page) 命中,两个都对才算对
             (type=no_answer 期望它明说查不到,编造即判错)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# 受控词表与三态标签 —— 与 eval/README.md、agents/safety/vision_prompt.md 同源
#
# 这三份东西必须**始终一致**:
#   README 词表  ==  本文件 VIOLATION_VOCAB  ==  vision_prompt.md 那张「只能用这 8 个词」的表格
# 标注用的词和模型输出的词对不上,判分就永远是错的。要改就三处一起改。
#
# ⚠️ 第三处**不是** agents/safety/prompt.md(这行注释以前就是这么写的,是错的)。
#    判断在视觉档做,词表只在 vision_prompt.md 里;prompt.md 是 Safety 本体的文本档提示词,
#    它自己头注明令「别在这儿再抄一份词表」。照旧指路牌去改的人会改一个不生效的文件,
#    而真正喂给 kimi 的那份一个字没动 —— 判分从此系统性错位。
#    唯一的自动化守卫 tests/unit/test_safety.py 读的也是 VISION_PROMPT_FILENAME。
# ---------------------------------------------------------------------------

VIOLATION_VOCAB: Final[frozenset[str]] = frozenset(
    {
        "未戴安全帽",
        "未穿反光衣",
        "高空作业未系安全带",
        "临边无防护",
        "消防通道堵塞",
        "材料堆放混乱",
        "用电隐患",
        "动火作业无监护",
    }
)

LABEL_VIOLATION: Final[str] = "violation"
LABEL_COMPLIANT: Final[str] = "compliant"
LABEL_NOT_SITE: Final[str] = "not_site"
SAFETY_LABELS: Final[frozenset[str]] = frozenset({LABEL_VIOLATION, LABEL_COMPLIANT, LABEL_NOT_SITE})

AGENT_NONE: Final[str] = "none"
"""路由集里「不该派给任何子 Agent」的取值。模型没派活时也归一到它。"""

ROUTING_AGENTS: Final[frozenset[str]] = frozenset(
    {
        "safety",
        "inspection",
        "knowledge",
        "schedule",
        "cad",
        "attendance",
        "supervision",
        AGENT_NONE,
    }
)
"""路由集 expected_agent 的合法取值,**与 eval/README.md 的 routing 小节同源,要改一起改**。

不校验的话,标注侧一个拼写错误(knowlege 漏了 d)会让那一行永远不可能被判对,
而报出的失败原因是「派错人了」—— 把矛头指向模型。routing 门槛是三套里最高的 90%,
20 条里错标 2 条就直接把上限压到 90%,团队会以为是 Supervisor 不行而去反复改提示词。

**这里必须恰好等于「可被路由到的集合」= `graph.AGENT_REGISTRY` 里**每一个** name + none。**
(刻意不写死个数:这句话原来写着「五个 name」,attendance(W7)与 supervision(W9)
先后落地之后它就过期了 —— 而过期的数字比没有数字更误导人,会让人以为多出来的那个是错的。)
2026-08-11 从这张表里删掉了 `report`,因为它给白名单开了个口子:
`report` 不在 `AGENT_REGISTRY` 里,它只是英雄链 `inspection` 子图**内部**的第二跳
(`graph.py` 的 `add_edge(safety, "report")` 硬边),`transfer_to_report` 这条路根本不存在;
而 `run_routing_row` 取的正是 supervisor 第一跳的 `transfer_to_X` —— 实际值**永远不可能是
report**。于是标成 `expected_agent=report` 的行能过白名单、却永远判不对,
报出来的原因还是「派错人了」——**正是上面这段话说要防的那件事,唯独对 report 失效**。
而 report 恰恰是最容易标错的那个值:要加一条「拍照出报告」的行,直觉会写 report 而不是
inspection。当时 routing.csv 里 0 行用它,所以没造成损失 —— 那是运气,不是设计。
"""

RAG_TYPE_NO_ANSWER: Final[str] = "no_answer"

RAG_TYPES: Final[frozenset[str]] = frozenset({"positive", "multi_point", RAG_TYPE_NO_ANSWER})
"""知识集 type 的合法取值,同样与 README 同源(理由同 ROUTING_AGENTS)。"""

NO_ANSWER_CARRIERS: Final[tuple[str, ...]] = ("知识库", "资料", "文档", "规范")
"""「查不到」这个表态的**主语**。必须是知识来源本身,不能是别的东西。

这一条是整道防线的支点:正因为要求主语是知识来源,
「现场找不到合格钢管时……」这类编造才进不来 —— 它的主语是「现场」。
"""

NO_ANSWER_LOCATIVES: Final[tuple[str, ...]] = ("", "里", "中")
"""主语与否定式之间可有可无的方位词。「知识库没有」「知识库里没有」「知识库中没有」都算数。"""

NO_ANSWER_NEGATIONS: Final[tuple[str, ...]] = (
    "无依据",
    "没有",
    "没收录",
    "未收录",
    "未提及",
    "查不到",
    "找不到",
    "未找到",
    "无相关",
    "没有相关",
)
"""紧跟在主语(+方位词)之后的否定表述。

注意这里可以收「找不到」这种普通词组 —— 因为它只有紧跟在主语后面
(「知识库找不到」)才会被认,单独出现在正文里(「现场找不到」)匹配不上。
"""

NO_ANSWER_MARKERS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(  # 去重且保序,便于报错文案里稳定地举例
        f"{carrier}{locative}{negation}"
        for carrier in NO_ANSWER_CARRIERS
        for locative in NO_ANSWER_LOCATIVES
        for negation in NO_ANSWER_NEGATIONS
    )
)
"""承认「查不到」的规定措辞白名单,由上面三张表做**笛卡尔积**生成。

### 为什么是生成而不是手写列表

原先这里是一份手写的短语清单。它同时收了「知识库未收录」和「资料中未收录」,
却偏偏漏了「知识库中未收录」—— 于是模型老老实实回答「知识库中未收录该内容」
会被判成**编造**。这个方向的误判和放行编造是镜像的、同样有害:
团队看到一个偏低的假分,会以为模型爱编,然后往错误方向去调提示词。

中文的这个表态天然是「主语 × 方位词 × 否定式」的组合,手工枚举必然漏组合,
而每漏一个就补一条的做法只会一直漏下去。所以改成按三张表相乘生成
(当前 4 × 3 × 10 = 120 种写法),要扩展就往对应的那张表里加一个词。

### 两条不许违反的硬要求(少一条这道防线就等于没有)

1. **必须带主语。** 早先白名单里收过裸的「找不到」「无法回答」——
   那是编造答案正文里极易自然出现的普通词组,不是承认查不到的信号。
   实测:「现场**找不到**合格钢管时,应按《钢结构规范》第 8.2 条更换」
   这种彻头彻尾的编造会被直接判对,而失效方向正是最不该放行的那一侧。
   现在「找不到」只在 NO_ANSWER_NEGATIONS 里、必须紧跟主语,上面那句依然判错。
2. **别往里加「可能」「建议咨询」这类模糊词** —— 那正是编造时最爱用的托词。

配套两条(缺一不可,见 _score_no_answer):只在答案开头找表态;答案里出现引用形态
(书名号 / 第 X 条 / 第 X 页)一律判编造。
另外 knowledge Agent 的提示词必须要求模型用「知识库里没有依据」这类措辞 ——
README + 判分 + 提示词三处同源,与 safety 受控词表同样的处理方式。
"""

NO_ANSWER_LOOKAHEAD_CHARS: Final[int] = 40
"""只在答案开头这么多个字(压掉空白后)里找「承认查不到」的表态。

「承认查不到」是个**表态**,表态就该放在开头。全文任意位置都认的话,
编造的答案末尾捎一句「具体资料中未提及」就能蒙混过关。
"""

CITATION_SHAPES: Final[tuple[str, ...]] = (r"《[^》]+》", r"第\s*\d+\s*条", r"第\s*\d+\s*页")
"""引用形态。no_answer 行的答案里只要出现这些,不管嘴上认不认一律判编造 ——
知识库里根本没有的东西,不可能有书名号出处、条文号和页码。"""

ITEM_SEPARATORS: Final[str] = r"[;；]"
"""多值字段的分隔符。只认分号(半角/全角),不认逗号 —— 中文答案要点里逗号太常见。"""

PAGE_SEPARATORS: Final[str] = r"[,，、\s]+"
"""页码字段在 ITEM_SEPARATORS 之外**再切一层**的分隔符。

ITEM_SEPARATORS 刻意不认逗号,但页码恰恰常写成「7,90」「第23页,共100页」。
不切的话整段会被当成一个片段,而早先 parse_pages 又把片段里的最小/最大值
当成一个区间的两个端点整段展开 —— 「7,90」会膨胀成 84 页,随便引哪一页都能"命中"。
"""

PAGE_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\D*(\d+)\s*[-–—~～至到]\s*(\d+)\D*")
"""**明确的**区间写法(12-14 / 12~14 / 12至14)。只有它才展开,别的一律按独立页码处理。"""

SOURCE_TOKEN_SEPARATORS: Final[str] = r"[《》\"'“”‘’()（）\[\]【】,，;；:：、/\\\s]+"
"""从模型给的出处串里切出候选文件名的分隔符(见 _source_hit)。"""

NUMERIC_POINT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?\D*")
"""纯数值型要点(1.5m / 20 / 3.2%)。这类要点必须查左边界,见 _point_hit。"""

MIN_ANSWER_POINT_LEN: Final[int] = 4
"""单条答案要点的最短长度(字符数)。

只填一个数字的要点既判不准也极易误命中(「1.5m」会被「11.5m」包住),
而且它压根说明不了"这条答案对不对"。当场退回让标注的人抄成完整短语。
"""

MAX_PAGE_SPAN: Final[int] = 200
"""页码范围一次最多展开多少页。防「12-99999」这种笔误把内存撑爆。"""


class DatasetError(ValueError):
    """数据集本身有问题(缺列、标签写错、用了词表外的违规项)。

    与「模型答错」是两回事:模型答错只是这一条不得分,数据集写错则整套分数
    都没有意义,必须让跑分立刻停下来并报出中文原因。
    """


@dataclass(frozen=True, slots=True)
class RowScore:
    """单条样本的判分结果。frozen:判完就是事实,不许有人事后改分。

    字段:
        row_id:   数据集里的 id,报告里靠它定位是哪条挂了
        passed:   这条是否算对
        expected: 期望是什么(人可读的一行)
        actual:   模型实际给了什么(人可读的一行)
        reason:   为什么判成这样。**挂了的时候必须写清楚**,
                  只说「错了」对调提示词没有任何帮助
        expected_items / actual_items:
                  **诊断专用,不参与判分。** 只有 safety 这种「答案是一个集合」的套会填。

                  为什么需要:判分是集合完全相等、不给部分分(理由见 score_safety),
                  于是「一项都没答对」和「三项答对两项」在分数上完全一样,都是 0。
                  一个 30% 的报告里,你分不清模型是压根不会,还是每张只差一项 ——
                  而这两种情况的修法完全相反(换模型 vs 调提示词措辞)。
                  这两个字段让 runner 能额外算出重合度与逐类召回,把这件事分开。
    """

    row_id: str
    passed: bool
    expected: str
    actual: str
    reason: str
    expected_items: frozenset[str] | None = None
    actual_items: frozenset[str] | None = None


# ---------------------------------------------------------------------------
# 归一化小工具(私有)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """把任意输入收敛成去掉首尾空白的字符串;None 变空串。"""
    if value is None:
        return ""
    return str(value).strip()


def _compact(value: Any) -> str:
    """去掉**全部**空白并转小写,用于做「包含」比较。

    模型输出的空格/换行位置每次都不一样,不压掉的话字面比较会无谓地判错。
    """
    return "".join(str(value).split()).lower() if value is not None else ""


def split_items(raw: Any) -> tuple[str, ...]:
    """把「分号分隔的字符串」或「已经是列表」统一成去重前的字符串元组。

    "未戴安全帽;未穿反光衣"      -> ("未戴安全帽", "未穿反光衣")
    ["未戴安全帽", " 用电隐患 "]  -> ("未戴安全帽", "用电隐患")
    "" / None / []               -> ()
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts: Iterable[Any] = re.split(ITEM_SEPARATORS, raw)
    elif isinstance(raw, Iterable):
        parts = raw
    else:
        parts = (raw,)
    return tuple(item for item in (_text(part) for part in parts) if item)


def _page_fragments(raw: Any) -> tuple[str, ...]:
    """页码字段的切片:先按分号切,再按逗号/顿号/空白切一层(理由见 PAGE_SEPARATORS)。"""
    fragments: list[str] = []
    for item in split_items(raw):
        fragments.extend(part for part in re.split(PAGE_SEPARATORS, item) if part)
    return tuple(fragments)


def parse_pages(raw: Any) -> frozenset[int]:
    """把页码字段展开成页码集合。支持 ``23`` / ``12-14`` / ``7;9`` / ``7,90`` / ``[7, 9]``。

        23              -> {23}
        12-14           -> {12, 13, 14}
        14-12           -> {12, 13, 14}   (写反了也认,不为难标注的人)
        7,90            -> {7, 90}        (**不是** 7..90)
        第23页,共100页  -> {23, 100}

    **只有明确的区间写法才展开**(PAGE_RANGE_PATTERN)。早先的做法是把一个片段里
    出现的所有数字当成一个区间的两个端点整段展开,后果是:模型引了完全不相干的
    「7,90」会被展开成 84 页,而 page_ok 只要求交集非空 —— 于是判成「页码全对」。
    那是判分最危险的一类错误:一个精确的、错误的、偏高的 RAG 分数。

    认不出来的片段直接忽略(不抛异常):页码列由人手填,
    出现「第23页」这种写法很正常,能抠出数字就够用了。
    """
    pages: set[int] = set()
    for item in _page_fragments(raw):
        numbers = [int(n) for n in re.findall(r"\d+", item)]
        if not numbers:
            continue
        match = PAGE_RANGE_PATTERN.fullmatch(item)
        if match is None:  # 不是区间写法,里面每个数字各算一个独立页码
            pages.update(numbers)
            continue
        start, end = sorted((int(match.group(1)), int(match.group(2))))
        if end - start > MAX_PAGE_SPAN:  # 明显是笔误,只取端点,不展开
            pages.update({start, end})
            continue
        pages.update(range(start, end + 1))
    return frozenset(pages)


def _describe(items: Iterable[str]) -> str:
    """把集合渲染成报告里好读的一行;空集合显示「无」。"""
    ordered = sorted(items)
    return "、".join(ordered) if ordered else "无"


def _field(payload: Any, key: str, *, bare_ok: bool = True) -> Any:
    """从模型输出里取一个字段。

    被测对象可能返回结构化 dict,也可能只返回一个裸字符串(比如路由只给 Agent 名),
    两种都要能接住,免得每个 Agent 的作者各写一套适配。

    ⚠️ bare_ok=False 是给**引用类字段**(source / page / violations)用的,必须传。
    裸字符串没有字段结构,兜底会让同一段文本同时充当 answer、source、page 三个字段:
      · no_answer 行的 cited_source 恒等于答案正文、恒为非空 —— 一个完全正确的
        「知识库无依据」回答会被判成「编了个出处」,RAG 套所有 no_answer 行
        对裸字符串被测对象永远是 0 分,分数被系统性压低;
      · positive 行的 page 拿到整段答案文本,配合页码解析几乎必然蒙对。
    两个方向都是错的,所以引用类字段在拿不到 Mapping 时一律返回 None。
    """
    if isinstance(payload, Mapping):
        return payload.get(key)
    return payload if bare_ok else None


# ---------------------------------------------------------------------------
# 一、routing —— 派活准不准
# ---------------------------------------------------------------------------


def validate_routing_row(row: Mapping[str, str]) -> None:
    """校验路由集的一行:expected_agent 必须填,且必须是合法的 Agent 名。

    为什么名字也要校验(safety 套对受控词表严到「不在词表就炸」,这里不能松):
    拼错一个字母的那一行永远不可能被判对,失败原因还写着「派错人了」,
    把矛头指向模型 —— 见 ROUTING_AGENTS 的说明。
    """
    row_id = _text(row.get("id")) or "?"
    if not _text(row.get("expected_agent")):
        raise DatasetError(f"路由集 {row_id} 缺 expected_agent,不填就没法判分。")
    agent = _normalize_agent(row.get("expected_agent"))
    if agent not in ROUTING_AGENTS:
        raise DatasetError(
            f"路由集 {row_id} 的 expected_agent 是「{_text(row.get('expected_agent'))}」,"
            f"只能填 {'/'.join(sorted(ROUTING_AGENTS))}。"
            "名字拼错的那一行永远判不对,还会让人以为是模型路由不准。"
        )


def _normalize_agent(value: Any) -> str:
    """归一化 Agent 名:去空白转小写;空值一律当作「没派给任何人」。"""
    name = _compact(_field(value, "agent"))
    return name or AGENT_NONE


def score_routing(row: Mapping[str, str], actual: Any) -> RowScore:
    """路由判分:实际派给的 Agent == expected_agent 即算对。

    ``expected_agent=none`` 表示这条不该派给任何子 Agent(闲聊、能力询问、
    信息不足该追问)—— 此时模型「没派活」正是标准答案,所以空输出归一成 none。
    """
    expected = _normalize_agent(row.get("expected_agent"))
    got = _normalize_agent(actual)
    passed = got == expected
    if passed:
        reason = "派给了期望的 Agent。"
    elif expected == AGENT_NONE:
        reason = f"这条本不该派给任何子 Agent,却派给了「{got}」。"
    elif got == AGENT_NONE:
        reason = f"该派给「{expected}」却谁都没派(supervisor 自己答了或没听懂)。"
    else:
        reason = f"派错人了:该给「{expected}」,实际给了「{got}」。"
    return RowScore(
        row_id=_text(row.get("id")),
        passed=passed,
        expected=expected,
        actual=got,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 二、safety —— 识图准不准
# ---------------------------------------------------------------------------


def _expected_violations(row: Mapping[str, str]) -> frozenset[str]:
    return frozenset(split_items(row.get("violations")))


def validate_safety_row(row: Mapping[str, str]) -> None:
    """校验安全集的一行:三态标签合法 + 违规项必须出自受控词表。"""
    row_id = _text(row.get("id")) or "?"
    label = _compact(row.get("label"))
    if label not in SAFETY_LABELS:
        raise DatasetError(
            f"安全集 {row_id} 的 label 是「{_text(row.get('label')) or '空'}」,"
            f"只能填 {'/'.join(sorted(SAFETY_LABELS))}。"
        )
    violations = _expected_violations(row)
    unknown = violations - VIOLATION_VOCAB
    if unknown:
        raise DatasetError(
            f"安全集 {row_id} 用了受控词表以外的违规项:{_describe(unknown)}。"
            "词表定死在 eval/README.md,标注和模型输出必须用同一套词。"
        )
    if label != LABEL_VIOLATION and violations:
        raise DatasetError(f"安全集 {row_id} 标成 {label} 却填了违规项,自相矛盾。")
    if label == LABEL_VIOLATION and not violations:
        raise DatasetError(f"安全集 {row_id} 标成 violation 却没填违规项,判分无从判起。")


def score_safety(row: Mapping[str, str], actual: Any) -> RowScore:
    """安全判分。

    实际 label != 标注 label ──────────────► 判错(先看这个)
              │ 一致
              ▼
    标注是 violation ? ── 否 ──► 实际违规项必须为空,报一条就算错
              │ 是            (合规/非工地却喊违规 = 误报,比漏报更劝退用户)
              ▼
    违规项集合**完全相同**才算对
    (不给部分分:漏一项等于漏一个隐患;多报一项等于狼来了。
     给了部分分,一个把八个词全喊一遍的模型能拿到很高的分数。)
    """
    row_id = _text(row.get("id"))
    expected_label = _compact(row.get("label"))
    expected_items = _expected_violations(row)
    got_label = _compact(_field(actual, "label"))
    # violations 是引用类字段:被测对象只返回一个裸的 label 字符串时,
    # 不能把那个字符串同时当成违规项清单(否则 "violation" 会变成一条违规项)。
    got_items = frozenset(split_items(_field(actual, "violations", bare_ok=False)))

    expected_desc = f"{expected_label} / {_describe(expected_items)}"
    actual_desc = f"{got_label or '空'} / {_describe(got_items)}"
    unknown = got_items - VIOLATION_VOCAB

    if got_label != expected_label:
        reason = f"判断结论就不对:该是 {expected_label},模型说是 {got_label or '空'}。"
        return RowScore(
            row_id, False, expected_desc, actual_desc, reason, expected_items, got_items
        )

    if got_items == expected_items:
        return RowScore(
            row_id,
            True,
            expected_desc,
            actual_desc,
            "结论与违规项全部对上。",
            expected_items,
            got_items,
        )

    missed = _describe(expected_items - got_items)
    extra = _describe(got_items - expected_items)
    reason = f"违规项对不上:漏报 {missed};误报 {extra}。"
    if unknown:
        reason += f" 其中 {_describe(unknown)} 不在受控词表里,提示词的输出约束没生效。"
    return RowScore(row_id, False, expected_desc, actual_desc, reason, expected_items, got_items)


# ---------------------------------------------------------------------------
# 三、rag —— 检索准不准、有没有编造
# ---------------------------------------------------------------------------


def _validate_rag_points(row_id: str, raw_points: Any) -> None:
    """答案要点必须是能判分的完整短语,不许只填一个数字(理由见 MIN_ANSWER_POINT_LEN)。"""
    for point in split_items(raw_points):
        if len(point) < MIN_ANSWER_POINT_LEN:
            raise DatasetError(
                f"知识集 {row_id} 的答案要点「{point}」太短,判不了分。"
                "请抄成带主语谓语的完整短语(如「立杆间距不应大于 1.5m」),不许只填一个数。"
            )


def validate_rag_row(row: Mapping[str, str]) -> None:
    """校验知识集的一行:type 合法 + no_answer 行不许带答案/出处 + 其余行三要素齐且要点可判。

    ⚠️ no_answer 那一支**只查 expected_answer_points 与 expected_source 两列**,
    不查 `expected_page`、不查 `note` —— `note` 本来就该能写标注理由
    (现有 K17~K20 四行都写着「负向-非规范」)。以前这里的报错文案写的是
    「后四列必须全部留空」,和自己的判据对不上,照它去清空 note 是白改。
    """
    row_id = _text(row.get("id")) or "?"
    row_type = _compact(row.get("type"))
    if row_type not in RAG_TYPES:
        raise DatasetError(
            f"知识集 {row_id} 的 type 是「{_text(row.get('type')) or '空'}」,"
            f"只能填 {'/'.join(sorted(RAG_TYPES))}。"
        )
    if row_type == RAG_TYPE_NO_ANSWER:
        if _text(row.get("expected_source")) or _text(row.get("expected_answer_points")):
            raise DatasetError(
                f"知识集 {row_id} 是 no_answer 行,expected_answer_points 与 "
                "expected_source 必须留空(note 可以写标注理由)。"
            )
        return
    missing = [
        name
        for name in ("expected_answer_points", "expected_source", "expected_page")
        if not _text(row.get(name))
    ]
    if missing:
        raise DatasetError(
            f"知识集 {row_id} 缺 {'、'.join(missing)}。"
            "答案要点、出处、页码三者缺一,这条就判不了分。"
        )
    _validate_rag_points(row_id, row.get("expected_answer_points"))


def _source_hit(expected: str, actual: str) -> bool:
    """出处命中:允许模型把文件名裹在句子里(「见《规范A.pdf》第 23 页」)。

    判据是「切出来的某个完整片段 == 期望的文件名(或去扩展名后的名字)」,
    **不是子串包含**。工地文档里同一规范常有汇编版 / 年份版 / 修订版,文件名互为前缀:
    子串判据下「施工规范.pdf」会被「施工规范汇编2020.pdf」判成命中 ——
    模型其实指错了文件,工人照着去翻是翻不到的。

        见《施工规范A.pdf》第 23 页  --切--> 见 | 施工规范A.pdf | 第 | 23 | 页  --> 命中
        施工规范汇编2020.pdf         --切--> 施工规范汇编2020.pdf              --> 不命中
    """
    if not expected:
        return False
    wanted = {_compact(expected), _compact(Path(expected).stem)} - {""}
    for token in re.split(SOURCE_TOKEN_SEPARATORS, _text(actual)):
        if not token:
            continue
        if _compact(token) in wanted or _compact(Path(token).stem) in wanted:
            return True
    return False


def _point_hit(point: str, compact_answer: str) -> bool:
    """答案里有没有命中这条要点。

    纯数值型要点(1.5m / 20)要额外查左边界:裸子串匹配下「1.5m」是「11.5m」的子串,
    「防护栏杆高度不应低于11.5m」会被判成命中了「1.5m」这个要点 —— 方向偏高,
    与页码那条错误叠加时会一起把 RAG 分抬上去。

    极性(「该场景不要求 1.5m 的间距」)不适合在字符串层面解决 ——
    对策写在 eval/README.md 的填写说明里:要点必须抄成带主语谓语的完整短语。
    """
    needle = _compact(point)
    if not needle:
        return True
    if not NUMERIC_POINT_PATTERN.fullmatch(needle):
        return needle in compact_answer
    return re.search(rf"(?<![0-9.]){re.escape(needle)}", compact_answer) is not None


def _admits_no_answer(answer: str) -> bool:
    """答案**开头**有没有按规定措辞承认查不到(理由见 NO_ANSWER_LOOKAHEAD_CHARS)。"""
    head = _compact(answer)[:NO_ANSWER_LOOKAHEAD_CHARS]
    return any(_compact(marker) in head for marker in NO_ANSWER_MARKERS)


def _looks_cited(answer: str) -> bool:
    """答案里有没有引用形态(书名号 / 第 X 条 / 第 X 页)。"""
    return any(re.search(shape, answer) for shape in CITATION_SHAPES)


def _score_no_answer(row_id: str, answer: str, cited_source: str) -> RowScore:
    """no_answer 行:期望它按规定措辞明说查不到,且不许顺手编条文、页码或出处。

    答案开头有规定措辞? --否--> 判错(没按规定措辞承认,也没给可核对的依据)
          │ 是
          ▼
    答案里有《》/第X条/第X页? --是--> 判错(嘴上认了却又编引用)
          │ 否
          ▼
    给了 source ? --是--> 判错(编了个出处)
          │ 否
          ▼
         判对
    """
    actual_desc = f"答案:{answer or '空'};出处:{cited_source or '无'}"
    expected_desc = "按规定措辞说明知识库里没有依据,且不给出处、条文号、页码"
    if not _admits_no_answer(answer):
        # 别一上来就扣「编造」的帽子:诚实拒答但换了个说法的,也会落到这一支,
        # 报告要说清是「措辞不合规定」还是「真编了」,不然看报告的人会往错误方向调提示词。
        reason = (
            "知识库里查不到:模型既没按规定措辞承认查不到,也没给出可核对的依据。"
            "规定措辞见 eval/README.md 的 rag 小节(与 knowledge Agent 提示词同源)。"
        )
        return RowScore(row_id, False, expected_desc, actual_desc, reason)
    if _looks_cited(answer):
        reason = "嘴上承认查不到,答案里却有书名号出处 / 条文号 / 页码 —— 这是编造。"
        return RowScore(row_id, False, expected_desc, actual_desc, reason)
    if _text(cited_source):
        reason = f"嘴上说查不到,却又编了个出处「{cited_source}」,同样算编造。"
        return RowScore(row_id, False, expected_desc, actual_desc, reason)
    return RowScore(row_id, True, expected_desc, actual_desc, "老老实实承认了查不到。")


def score_rag(row: Mapping[str, str], actual: Any) -> RowScore:
    """知识判分:**答案要点全命中 且 (出处 + 页码) 命中,两个都对才算对。**

    只对一半不给分 —— 「答案对但引用错」说明它是猜的不是查的,
    而查得到出处正是 RAG 的全部价值。
    """
    row_id = _text(row.get("id"))
    answer = _text(_field(actual, "answer"))
    # source / page 是引用类字段:被测对象只给裸字符串时它们必须是空,不能拿答案正文兜底。
    cited_source = _text(_field(actual, "source", bare_ok=False))
    cited_page_raw = _field(actual, "page", bare_ok=False)

    if _compact(row.get("type")) == RAG_TYPE_NO_ANSWER:
        return _score_no_answer(row_id, answer, cited_source)

    expected_points = split_items(row.get("expected_answer_points"))
    expected_source = _text(row.get("expected_source"))
    expected_pages = parse_pages(row.get("expected_page"))
    cited_pages = parse_pages(cited_page_raw)

    compact_answer = _compact(answer)
    missed_points = tuple(p for p in expected_points if not _point_hit(p, compact_answer))
    source_ok = _source_hit(expected_source, cited_source)
    page_ok = bool(expected_pages & cited_pages)

    expected_desc = (
        f"要点:{_describe(expected_points)};出处:{expected_source} 第 "
        f"{_text(row.get('expected_page'))} 页"
    )
    actual_desc = (
        f"答案:{answer or '空'};出处:{cited_source or '无'} 第 {_text(cited_page_raw) or '?'} 页"
    )

    problems: list[str] = []
    if missed_points:
        problems.append(f"答案漏了要点:{_describe(missed_points)}")
    if not source_ok:
        problems.append(f"出处不对(应为 {expected_source})")
    if not page_ok:
        wanted = "、".join(str(p) for p in sorted(expected_pages)) or "无"
        problems.append(f"页码不对(应命中第 {wanted} 页)")

    if not problems:
        return RowScore(row_id, True, expected_desc, actual_desc, "要点、出处、页码全对。")
    reason = ";".join(problems) + "。要点与出处页码必须同时对,只对一半不给分。"
    return RowScore(row_id, False, expected_desc, actual_desc, reason)


__all__ = [
    "AGENT_NONE",
    "CITATION_SHAPES",
    "LABEL_COMPLIANT",
    "LABEL_NOT_SITE",
    "LABEL_VIOLATION",
    "MAX_PAGE_SPAN",
    "MIN_ANSWER_POINT_LEN",
    "NO_ANSWER_LOOKAHEAD_CHARS",
    "NO_ANSWER_MARKERS",
    "RAG_TYPES",
    "RAG_TYPE_NO_ANSWER",
    "ROUTING_AGENTS",
    "SAFETY_LABELS",
    "VIOLATION_VOCAB",
    "DatasetError",
    "RowScore",
    "parse_pages",
    "score_rag",
    "score_routing",
    "score_safety",
    "split_items",
    "validate_rag_row",
    "validate_routing_row",
    "validate_safety_row",
]
