"""评测框架(``eval.scorers`` + ``eval.runner``)单元测试。

**重点在判分逻辑本身**。理由:判分错了比不判分更糟 —— 它会给出一个看起来
很精确的错误数字,然后全组照着这个假数字调半天提示词。所以三套判分函数
每一条规则(尤其是「只对一半不给分」这类)都单独钉一个用例。

全程零网络:被测对象一律是本文件里的假 async 函数,一次真实 API 调用都不发。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from eval import runner as runner_mod
from eval import scorers
from eval.runner import (
    EXIT_BELOW_THRESHOLD,
    EXIT_DATASET_ERROR,
    EXIT_OK,
    SUITES,
    RunnerSpecError,
    Status,
    SuiteReport,
    exit_code_of,
    format_diagnostics,
    format_report,
    format_summary,
    is_placeholder,
    load_rows,
    load_runners,
    main,
    run_suite,
    run_suites,
)
from eval.scorers import (
    VIOLATION_VOCAB,
    DatasetError,
    RowScore,
    parse_pages,
    score_rag,
    score_routing,
    score_safety,
    split_items,
    validate_rag_row,
    validate_routing_row,
    validate_safety_row,
)

from gyt.config import get_settings

# --- 测试用常量:不在用例里散落字面量 ---------------------------------------

ROUTING_HEADER = ("id", "type", "user_input", "expected_agent", "note")
SAFETY_HEADER = ("id", "type", "image", "label", "violations", "note")
RAG_HEADER = (
    "id",
    "type",
    "question",
    "expected_answer_points",
    "expected_source",
    "expected_page",
    "note",
)

THIS_MODULE = "tests.unit.test_eval_runner"


@pytest.fixture(autouse=True)
def _tiny_min_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「最小样本量」下限降到 1 条。

    本文件绝大多数用例只拿 1~2 行数据去验证某一条判分规则,真实下限(20/30/20)
    会挡在判分之前,把每条用例都变成在测样本量。下限本身由
    test_run_suite_fails_when_sample_size_below_minimum 单独钉死,不会漏测。
    """
    for suite_name in ("ROUTING", "SAFETY", "RAG"):
        monkeypatch.setenv(f"GYT_EVAL_MIN_ROWS_{suite_name}", "1")
    get_settings.cache_clear()


def _write_csv(path: Path, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """按 utf-8-sig(带 BOM)写一份数据集 —— 刻意模拟 Excel 存出来的文件。"""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _routing_row(expected_agent: str, row_id: str = "R01") -> dict[str, str]:
    return {
        "id": row_id,
        "type": "positive",
        "user_input": "这张照片有没有安全隐患",
        "expected_agent": expected_agent,
        "note": "",
    }


def _safety_row(label: str, violations: str = "", row_id: str = "S01") -> dict[str, str]:
    return {
        "id": row_id,
        "type": "positive",
        "image": "photo_01.jpg",
        "label": label,
        "violations": violations,
        "note": "",
    }


def _rag_row(
    *,
    row_type: str = "positive",
    points: str = "立杆间距不得大于 1.8m",
    source: str = "施工规范A.pdf",
    page: str = "23",
    row_id: str = "K01",
    note: str = "",
) -> dict[str, str]:
    return {
        "id": row_id,
        "type": row_type,
        "question": "脚手架立杆间距有什么要求",
        "expected_answer_points": points,
        "expected_source": source,
        "expected_page": page,
        "note": note,
    }


def _runner_returning(value: Any) -> Any:
    """造一个恒定返回 value 的假被测对象。"""

    async def _run(row: Mapping[str, str]) -> Any:
        return value

    return _run


def _runner_by_id(table: Mapping[str, Any]) -> Any:
    """造一个「按行 id 查表返回」的假被测对象,用来精确控制某几条对某几条错。"""

    async def _run(row: Mapping[str, str]) -> Any:
        return table[row["id"]]

    return _run


async def _exploding_runner(row: Mapping[str, str]) -> Any:
    raise RuntimeError("模型服务连不上")


FAKE_RUNNERS: dict[str, Any] = {"routing": _runner_returning("safety")}
"""给 ``--runners tests.unit.test_eval_runner:FAKE_RUNNERS`` 这条用例用的注入表。"""


# ===========================================================================
# 一、routing 判分
# ===========================================================================


def test_routing_hit_when_agent_matches() -> None:
    # Arrange & Act
    result = score_routing(_routing_row("safety"), "safety")

    # Assert
    assert result.passed is True
    assert result.row_id == "R01"


def test_routing_is_case_and_space_insensitive() -> None:
    # Arrange & Act:模型输出带空格/大写不该被判错,那是格式问题不是路由问题
    result = score_routing(_routing_row("safety"), "  Safety ")

    # Assert
    assert result.passed is True


def test_routing_accepts_structured_output() -> None:
    # Arrange & Act:被测对象返回结构化 dict 也要接得住
    result = score_routing(_routing_row("knowledge"), {"agent": "knowledge"})

    # Assert
    assert result.passed is True


def test_routing_empty_output_is_treated_as_none() -> None:
    # Arrange & Act:expected=none 时「谁都没派」正是标准答案
    result = score_routing(_routing_row("none"), "")

    # Assert
    assert result.passed is True


def test_routing_none_row_fails_when_dispatched() -> None:
    # Arrange & Act:闲聊却把活派出去了 —— 这正是 none 行存在的意义
    result = score_routing(_routing_row("none"), "safety")

    # Assert
    assert result.passed is False
    assert "本不该派给任何子 Agent" in result.reason


def test_routing_missing_dispatch_reason_is_specific() -> None:
    # Arrange & Act
    result = score_routing(_routing_row("schedule"), None)

    # Assert:明细要说清是「没派」而不是「派错」,两者的修法完全不同
    assert result.passed is False
    assert "谁都没派" in result.reason


def test_routing_wrong_agent_reason_names_both_sides() -> None:
    # Arrange & Act
    result = score_routing(_routing_row("knowledge"), "safety")

    # Assert:调提示词的人要能从明细里直接看出错在哪
    assert result.passed is False
    assert "knowledge" in result.reason and "safety" in result.reason


# ===========================================================================
# 二、safety 判分
# ===========================================================================


def test_safety_hit_on_single_violation() -> None:
    # Arrange & Act
    result = score_safety(
        _safety_row("violation", "未戴安全帽"),
        {"label": "violation", "violations": ["未戴安全帽"]},
    )

    # Assert
    assert result.passed is True


def test_safety_violation_order_does_not_matter() -> None:
    # Arrange & Act:集合比较,输出顺序不该影响得分
    result = score_safety(
        _safety_row("violation", "未戴安全帽;未穿反光衣"),
        {"label": "violation", "violations": "未穿反光衣;未戴安全帽"},
    )

    # Assert
    assert result.passed is True


def test_safety_partial_hit_gets_no_credit() -> None:
    # Arrange & Act:两项只报对一项 —— 漏报一项等于漏一个隐患,不给部分分
    result = score_safety(
        _safety_row("violation", "未戴安全帽;未穿反光衣"),
        {"label": "violation", "violations": "未戴安全帽"},
    )

    # Assert
    assert result.passed is False
    assert "漏报 未穿反光衣" in result.reason


def test_safety_over_reporting_fails() -> None:
    # Arrange & Act:多喊一项也算错,否则「八个词全喊一遍」的模型能拿高分
    result = score_safety(
        _safety_row("violation", "未戴安全帽"),
        {"label": "violation", "violations": "未戴安全帽;用电隐患"},
    )

    # Assert
    assert result.passed is False
    assert "误报 用电隐患" in result.reason


def test_safety_out_of_vocabulary_word_is_called_out() -> None:
    # Arrange & Act:模型自由发挥了一个词表外的词
    result = score_safety(
        _safety_row("violation", "未戴安全帽"),
        {"label": "violation", "violations": "没戴帽子"},
    )

    # Assert:判错之外还要点破「提示词的输出约束没生效」,否则很难查
    assert result.passed is False
    assert "不在受控词表里" in result.reason


def test_safety_label_mismatch_short_circuits() -> None:
    # Arrange & Act:结论都不对,违规项对不对已经无所谓
    result = score_safety(
        _safety_row("compliant"),
        {"label": "violation", "violations": "未戴安全帽"},
    )

    # Assert
    assert result.passed is False
    assert "判断结论就不对" in result.reason


def test_safety_compliant_row_passes_only_with_empty_violations() -> None:
    # Arrange & Act
    hit = score_safety(_safety_row("compliant"), {"label": "compliant", "violations": ""})
    miss = score_safety(_safety_row("compliant"), {"label": "compliant", "violations": "用电隐患"})

    # Assert:合规照片被喊出违规 = 误报,比漏报更劝退用户
    assert hit.passed is True
    assert miss.passed is False


def test_safety_not_site_row_must_report_nothing() -> None:
    # Arrange & Act
    result = score_safety(
        _safety_row("not_site", row_id="S09"), {"label": "not_site", "violations": []}
    )

    # Assert
    assert result.passed is True


def test_safety_vocabulary_matches_readme_word_list() -> None:
    # Assert:词表是提示词与标注的共同约束,改动必须是有意识的三处同步
    assert VIOLATION_VOCAB == frozenset(
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


# ===========================================================================
# 三、rag 判分
# ===========================================================================


def test_rag_hit_requires_points_source_and_page() -> None:
    # Arrange & Act
    result = score_rag(
        _rag_row(),
        {"answer": "规范要求立杆间距不得大于 1.8m。", "source": "施工规范A.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is True


def test_rag_correct_answer_with_wrong_page_gets_no_credit() -> None:
    # Arrange & Act:答案对但引用错 = 它是猜的不是查的
    result = score_rag(
        _rag_row(),
        {"answer": "立杆间距不得大于 1.8m。", "source": "施工规范A.pdf", "page": "9"},
    )

    # Assert
    assert result.passed is False
    assert "页码不对" in result.reason


def test_rag_correct_answer_with_wrong_source_gets_no_credit() -> None:
    # Arrange & Act
    result = score_rag(
        _rag_row(),
        {"answer": "立杆间距不得大于 1.8m。", "source": "别的规范B.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is False
    assert "出处不对" in result.reason


def test_rag_right_citation_with_missing_point_gets_no_credit() -> None:
    # Arrange & Act:出处对但答案没答到点上,同样不给分
    result = score_rag(
        _rag_row(),
        {"answer": "请参见规范相关章节。", "source": "施工规范A.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is False
    assert "漏了要点" in result.reason


def test_rag_multi_point_requires_every_point() -> None:
    # Arrange
    row = _rag_row(points="设置防护栏杆;佩戴安全带;铺设安全网", row_id="K05")

    # Act:只答到两点
    result = score_rag(
        row,
        {"answer": "要设置防护栏杆,并佩戴安全带。", "source": "施工规范A.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is False
    assert "铺设安全网" in result.reason


def test_rag_page_range_hits_any_page_inside() -> None:
    # Arrange & Act:标注写 12-14,模型引 13 页算命中
    result = score_rag(
        _rag_row(page="12-14"),
        {"answer": "立杆间距不得大于 1.8m。", "source": "施工规范A.pdf", "page": "13"},
    )

    # Assert
    assert result.passed is True


def test_rag_source_hit_allows_filename_inside_sentence() -> None:
    # Arrange & Act:模型常把出处写成一句话,不该因为格式判错
    result = score_rag(
        _rag_row(),
        {
            "answer": "立杆间距不得大于 1.8m。",
            "source": "见《施工规范A.pdf》",
            "page": "第 23 页",
        },
    )

    # Assert
    assert result.passed is True


def test_rag_answer_whitespace_is_ignored_when_matching_points() -> None:
    # Arrange & Act:换行/空格位置每次都不一样,不该影响要点命中
    result = score_rag(
        _rag_row(points="立杆间距不得大于1.8m"),
        {"answer": "立杆间距\n不得大于 1.8m", "source": "施工规范A.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is True


def test_rag_no_answer_row_passes_when_model_admits() -> None:
    # Arrange & Act
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K06"),
        {"answer": "知识库无依据,这个问题我查不到。", "source": "", "page": ""},
    )

    # Assert
    assert result.passed is True


def test_rag_no_answer_row_fails_when_model_fabricates() -> None:
    # Arrange & Act:知识库里没有,却答得头头是道 —— 这正是最该抓的失败模式
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K06"),
        {"answer": "项目经理月薪约两万元。", "source": "", "page": ""},
    )

    # Assert
    assert result.passed is False
    assert "规定措辞" in result.reason


@pytest.mark.parametrize(
    "fabricated",
    [
        # 正文里自然出现「找不到」,但整句是彻头彻尾的编造 —— 早先会被判成"老实承认了"
        "现场找不到合格钢管时,应按《钢结构规范》第 8.2 条更换为同规格材料。",
        # 末尾捎一句托词,主体仍是编的
        "项目经理月薪一般在 1.5 万到 2 万之间,具体无法回答需咨询公司。",
    ],
)
def test_rag_no_answer_rejects_fabrication_that_happens_to_contain_negative_words(
    fabricated: str,
) -> None:
    # Arrange & Act:防编造是 no_answer 行的唯一价值,放行方向的错误最致命
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K06"),
        {"answer": fabricated, "source": ""},
    )

    # Assert
    assert result.passed is False


def test_rag_no_answer_accepts_honest_refusal_in_other_wording() -> None:
    # Arrange & Act:诚实拒答不该被扣「编造」的帽子(会让人往完全错误的方向调提示词)
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K06"),
        {"answer": "资料中未提及这方面的内容。", "source": ""},
    )

    # Assert
    assert result.passed is True


def test_rag_no_answer_rejects_admission_that_still_cites_a_clause() -> None:
    """嘴上认了却又给出条文号/页码/书名号 —— 知识库里没有的东西不可能有这些。"""
    # Arrange & Act
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K06"),
        {"answer": "知识库无依据,不过一般参照《钢结构规范》第 8.2 条执行。", "source": ""},
    )

    # Assert
    assert result.passed is False
    assert "编造" in result.reason


def test_rag_no_answer_bare_string_answer_is_not_treated_as_a_citation() -> None:
    # Arrange & Act:被测对象只回一个裸字符串时,答案正文不能同时被当成 source
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K06"),
        "知识库里没有这方面的依据",
    )

    # Assert:早先这里恒判 False,理由是"编了个出处「知识库里没有这方面的依据」"
    assert result.passed is True


def test_rag_no_answer_row_fails_when_citation_is_invented() -> None:
    # Arrange & Act:嘴上说查不到,却又编了个出处
    result = score_rag(
        _rag_row(row_type="no_answer", points="", source="", page="", row_id="K07"),
        {"answer": "知识库无依据。", "source": "薪酬制度.pdf", "page": "3"},
    )

    # Assert
    assert result.passed is False
    assert "编了个出处" in result.reason


def test_rag_missing_expected_source_never_counts_as_hit() -> None:
    # Arrange & Act:标注漏填出处时,判分绝不能因为"空串包含在任何字符串里"而蒙对
    result = score_rag(
        _rag_row(source=""),
        {"answer": "立杆间距不得大于 1.8m。", "source": "随便什么.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is False
    assert "出处不对" in result.reason


def test_rag_handles_missing_fields_without_crashing() -> None:
    # Arrange & Act:被测对象只给了答案、没给出处页码(常见的半成品输出)
    result = score_rag(_rag_row(), {"answer": None})

    # Assert:判错但不能抛异常 —— 一条烂输出不该炸掉整套评测
    assert result.passed is False


def test_rag_no_answer_marker_list_excludes_hedging_words() -> None:
    # Assert:"可能""建议咨询"这类托词不算承认,收进白名单等于给编造放行
    assert not any("可能" in marker for marker in scorers.NO_ANSWER_MARKERS)
    assert not any("建议" in marker for marker in scorers.NO_ANSWER_MARKERS)


# ===========================================================================
# 四、归一化小工具
# ===========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("23", {23}),
        ("12-14", {12, 13, 14}),
        ("14-12", {12, 13, 14}),
        ("第 7 页", {7}),
        ("7;9", {7, 9}),
        ([7, 9], {7, 9}),
        ("", set()),
        (None, set()),
        ("没有页码", set()),
    ],
)
def test_parse_pages_variants(raw: Any, expected: set[int]) -> None:
    # Act & Assert
    assert parse_pages(raw) == frozenset(expected)


def test_parse_pages_clamps_absurd_range() -> None:
    # Act:1-99999 明显是笔误,只取端点,绝不展开成十万个整数
    pages = parse_pages("1-99999")

    # Assert
    assert pages == frozenset({1, 99999})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7,90", {7, 90}),  # 逗号列举,**不是**区间
        ("第23页,共100页", {23, 100}),
        ("依据第3页与第30页", {3, 30}),
        ("12~14", {12, 13, 14}),  # 明确的区间写法照常展开
        ("12至14", {12, 13, 14}),
    ],
)
def test_parse_pages_only_expands_explicit_ranges(raw: str, expected: set[int]) -> None:
    """只有明确的区间写法才展开。

    早先的做法是把一个片段里所有数字的最小/最大值当区间端点整段展开:
    「7,90」会变成 84 页,而 page_ok 只要交集非空 —— 引了完全不相干页码的回答
    会被判成「页码全对」。那是判分最危险的一类错误:精确、错误、且偏高。
    """
    # Act & Assert
    assert parse_pages(raw) == frozenset(expected)


def test_rag_wrong_pages_are_not_inflated_into_a_hit() -> None:
    # Arrange & Act:标注第 23 页,模型引的是 7 和 90,两者毫不相干
    result = score_rag(
        _rag_row(page="23"),
        {"answer": "立杆间距不得大于 1.8m。", "source": "施工规范A.pdf", "page": "7,90"},
    )

    # Assert
    assert result.passed is False
    assert "页码不对" in result.reason


def test_rag_source_prefix_of_another_document_is_not_a_hit() -> None:
    """工地文档常有汇编版 / 年份版,文件名互为前缀 —— 子串判据会把引错判成引对。"""
    # Arrange & Act
    result = score_rag(
        _rag_row(source="施工规范.pdf"),
        {
            "answer": "立杆间距不得大于 1.8m。",
            "source": "施工规范汇编2020.pdf",
            "page": "23",
        },
    )

    # Assert
    assert result.passed is False
    assert "出处不对" in result.reason


def test_rag_numeric_point_is_not_matched_inside_a_longer_number() -> None:
    """要点「1.5m」不能被答案里的「11.5m」蒙对 —— 方向偏高,会把 RAG 分抬上去。"""
    # Arrange & Act
    result = score_rag(
        _rag_row(points="1.5m"),
        {"answer": "防护栏杆高度不应低于11.5m", "source": "施工规范A.pdf", "page": "23"},
    )

    # Assert
    assert result.passed is False
    assert "漏了要点" in result.reason


def test_safety_bare_label_string_is_not_read_as_a_violation_item() -> None:
    """被测对象只回一个裸 label 时,那个字符串不能同时被当成违规项清单。"""
    # Act & Assert
    assert score_safety(_safety_row("compliant"), "compliant").passed is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("甲;乙", ("甲", "乙")),
        ("甲;乙", ("甲", "乙")),
        (" 甲 ; ; 乙 ", ("甲", "乙")),
        (["甲", " 乙 "], ("甲", "乙")),
        ("", ()),
        (None, ()),
        (12, ("12",)),
    ],
)
def test_split_items_variants(raw: Any, expected: tuple[str, ...]) -> None:
    # Act & Assert
    assert split_items(raw) == expected


def test_split_items_does_not_split_on_comma() -> None:
    # Act:中文答案要点里逗号太常见,按逗号切会把一个要点撕成两半
    items = split_items("立杆间距不得大于 1.8m,且应设置扫地杆")

    # Assert
    assert len(items) == 1


# ===========================================================================
# 五、数据集校验(写错了要当场炸,而不是给出一个错误的分数)
# ===========================================================================


def test_validate_routing_row_requires_expected_agent() -> None:
    # Act & Assert
    with pytest.raises(DatasetError, match="expected_agent"):
        validate_routing_row(_routing_row(""))


def test_validate_routing_row_rejects_agent_outside_the_roster() -> None:
    """拼错一个字母的那一行永远判不对,报的原因还是「派错人了」—— 矛头指向模型。"""
    # Act & Assert
    with pytest.raises(DatasetError, match="knowlege"):
        validate_routing_row(_routing_row("knowlege"))

    # 名单里的每个名字都必须能过
    for agent in sorted(scorers.ROUTING_AGENTS):
        validate_routing_row(_routing_row(agent))


def test_validate_rag_row_rejects_unknown_type() -> None:
    # Act & Assert
    with pytest.raises(DatasetError, match="type"):
        validate_rag_row(_rag_row(row_type="positve"))


def test_validate_rag_row_rejects_bare_number_points() -> None:
    """只填一个数字的要点判不准也说明不了问题,当场退回让人抄成完整短语。"""
    # Act & Assert
    with pytest.raises(DatasetError, match="太短"):
        validate_rag_row(_rag_row(points="20"))


def test_validate_safety_row_rejects_unknown_label() -> None:
    # Act & Assert
    with pytest.raises(DatasetError, match="label"):
        validate_safety_row(_safety_row("有违规"))


def test_validate_safety_row_rejects_word_outside_vocabulary() -> None:
    # Act & Assert:标注用词跑出词表,判分就永远是错的,必须当场拦
    with pytest.raises(DatasetError, match="受控词表"):
        validate_safety_row(_safety_row("violation", "没戴帽子"))


def test_validate_safety_row_rejects_self_contradiction() -> None:
    # Act & Assert
    with pytest.raises(DatasetError, match="自相矛盾"):
        validate_safety_row(_safety_row("compliant", "未戴安全帽"))

    with pytest.raises(DatasetError, match="没填违规项"):
        validate_safety_row(_safety_row("violation", ""))


def test_validate_rag_row_requires_three_elements() -> None:
    # Act & Assert
    with pytest.raises(DatasetError, match="expected_page"):
        validate_rag_row(_rag_row(page=""))


def test_validate_rag_no_answer_row_must_leave_answer_and_source_empty() -> None:
    """no_answer 行不许带答案要点和出处;**note 照样可以写标注理由**。

    判据只有这两列 —— 用例名和报错文案以前都说「后四列全部留空」,
    而 `expected_page` 和 `note` 填了本来就放行(仓库里 K17~K20 的 note
    写的正是「负向-非规范」)。文案说 4 列、代码查 2 列、数据填 1 列,
    三方各说各的,照文案去清空 note 是白改。
    """
    # Act & Assert:no_answer 行填了出处,说明标注的人理解错了这类样本的用途
    with pytest.raises(DatasetError, match="必须留空"):
        validate_rag_row(_rag_row(row_type="no_answer", points="", source="某规范.pdf"))

    # 正确写法不该报错
    validate_rag_row(_rag_row(row_type="no_answer", points="", source="", page=""))

    # note 填了标注理由**不该**被拦下来 —— 仓库里现有的四行 no_answer 就是这么填的
    validate_rag_row(
        _rag_row(row_type="no_answer", points="", source="", page="", note="负向-非规范")
    )


# ===========================================================================
# 六、读盘
# ===========================================================================


def test_load_rows_reads_utf8_with_bom(tmp_path: Path) -> None:
    # Arrange:Excel 存出来的 CSV 带 BOM,用 utf-8 读会让第一列列名变成 '﻿id'
    _write_csv(
        tmp_path / "routing.csv", ROUTING_HEADER, [("R01", "positive", "看照片", "safety", "")]
    )

    # Act
    rows = load_rows(SUITES["routing"], tmp_path)

    # Assert
    assert rows[0]["id"] == "R01"
    assert rows[0]["expected_agent"] == "safety"


def test_load_rows_raises_on_missing_file(tmp_path: Path) -> None:
    # Act & Assert
    with pytest.raises(DatasetError, match="找不到数据集文件"):
        load_rows(SUITES["safety"], tmp_path)


def test_load_rows_raises_on_missing_column(tmp_path: Path) -> None:
    # Arrange:表头写漏一列 —— 不拦的话那一列全读成空,全套判错,分数低得莫名其妙
    _write_csv(tmp_path / "safety.csv", ("id", "type", "image", "label"), [])

    # Act & Assert
    with pytest.raises(DatasetError, match="violations"):
        load_rows(SUITES["safety"], tmp_path)


def test_load_rows_raises_chinese_error_on_gbk_encoding(tmp_path: Path) -> None:
    """数据集是给标注同学用 Excel 填的,中文 Windows 上默认存 GBK。

    不转成中文 DatasetError 的话,他们看到的是一坨
    「UnicodeDecodeError: codec can't decode byte 0xb9」的 traceback ——
    既不知道是哪个文件,也不知道该怎么救。
    """
    # Arrange:一份合法内容,用 GBK 存盘
    text = "id,type,user_input,expected_agent,note\nR01,positive,看照片,safety,备注\n"
    (tmp_path / "routing.csv").write_bytes(text.encode("gbk"))

    # Act & Assert
    with pytest.raises(DatasetError, match="UTF-8"):
        load_rows(SUITES["routing"], tmp_path)


def test_load_rows_raises_when_a_row_has_more_fields_than_the_header(tmp_path: Path) -> None:
    """字段里有逗号没加双引号 —— 最常见的 CSV 损坏,而且后果是标准答案被悄悄错位。

    早先这里是静默丢弃溢出部分:violations 只剩第一项、note 拿到第二项,
    validate 完全通过,模型把两项都报全了反而被判「误报」。
    """
    # Arrange
    (tmp_path / "safety.csv").write_text(
        "id,type,image,label,violations,note\n"
        "S01,positive,p1.jpg,violation,未戴安全帽,未穿反光衣,一图两违规\n",
        encoding="utf-8",
    )

    # Act & Assert
    with pytest.raises(DatasetError, match="字段数与表头不符"):
        load_rows(SUITES["safety"], tmp_path)


def test_load_rows_turns_io_errors_into_chinese_dataset_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """权限不足 / 文件被占用之类的 IO 错误,也要变成一句能照着办的中文,而不是 traceback。"""
    # Arrange
    _write_csv(
        tmp_path / "routing.csv", ROUTING_HEADER, [("R01", "positive", "看照片", "safety", "")]
    )

    def _deny(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "open", _deny)

    # Act & Assert
    with pytest.raises(DatasetError, match="读不了数据集文件"):
        load_rows(SUITES["routing"], tmp_path)


def test_is_placeholder_detects_template_rows() -> None:
    # Act & Assert:仓库自带示例行的数字全是编的,必须被剔出去
    assert is_placeholder({"id": "S01", "note": "示例待替换:换成真实照片"}) is True
    assert is_placeholder({"id": "S01", "note": "示例-可保留:合规对照"}) is False


# ===========================================================================
# 七、跑一套(注入被测对象,零网络)
# ===========================================================================


async def test_run_suite_skips_when_agent_not_wired(tmp_path: Path) -> None:
    # Act:W2 第一天 Safety Agent 还不存在,这里必须是 SKIP 而不是崩
    report = await run_suite(SUITES["safety"], None, datasets_dir=tmp_path)

    # Assert
    assert report.status is Status.SKIPPED
    assert "还没接进来" in report.message


async def test_run_suite_skips_when_dataset_is_only_placeholders(tmp_path: Path) -> None:
    # Arrange
    _write_csv(
        tmp_path / "routing.csv",
        ROUTING_HEADER,
        [("R01", "positive", "看照片", "safety", "示例待替换")],
    )

    # Act
    report = await run_suite(SUITES["routing"], _runner_returning("safety"), datasets_dir=tmp_path)

    # Assert:不判失败,只提示「数据集还没填」
    assert report.status is Status.SKIPPED
    assert "数据集还没填" in report.message
    assert report.skipped_ids == ("R01",)


async def test_run_suite_scores_only_filled_rows(tmp_path: Path) -> None:
    # Arrange:一条真数据 + 一条占位行 —— 数据填多少跑多少
    _write_csv(
        tmp_path / "routing.csv",
        ROUTING_HEADER,
        [
            ("R01", "positive", "看照片", "safety", ""),
            ("R02", "positive", "示例待替换", "knowledge", "示例待替换"),
        ],
    )

    # Act
    report = await run_suite(SUITES["routing"], _runner_returning("safety"), datasets_dir=tmp_path)

    # Assert
    assert report.total == 1
    assert report.score == 1.0
    assert report.skipped_ids == ("R02",)
    assert report.status is Status.PASSED


async def test_run_suite_fails_when_sample_size_below_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """样本量不足时,「100%(1/1)」这种分数一律不作数,必须非 0 退出。

    没有这道闸的话:30 行 safety 里 29 行是占位、只剩 1 行真数据,
    一个恒答「violation;未戴安全帽」的模型就能让报告写出
    「safety [PASS] 得分 100.0%(1/1) 门槛 80%」并 exit 0 —— W2 末的验收就是看这个绿灯。
    """
    # Arrange:1 条真数据,但下限要求 2 条
    monkeypatch.setenv("GYT_EVAL_MIN_ROWS_ROUTING", "2")
    get_settings.cache_clear()
    _write_csv(
        tmp_path / "routing.csv", ROUTING_HEADER, [("R01", "positive", "看照片", "safety", "")]
    )
    called: list[str] = []

    async def _spy(row: Mapping[str, str]) -> Any:
        called.append(row["id"])
        return "safety"

    # Act
    report = await run_suite(SUITES["routing"], _spy, datasets_dir=tmp_path)

    # Assert:判 FAIL(不是 SKIP —— SKIP 是绿的),而且没白烧一次模型额度
    assert report.status is Status.FAILED
    assert exit_code_of((report,)) == EXIT_BELOW_THRESHOLD
    assert "不作数" in report.message
    assert called == []
    # 报告里不许出现 0.0%(0/0) 这种会被误读成「模型全错」的假数字
    assert "0.0%" not in format_report(report)


async def test_run_suite_threshold_comes_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange:两条数据只对一条 = 50%
    _write_csv(
        tmp_path / "routing.csv",
        ROUTING_HEADER,
        [
            ("R01", "positive", "看照片", "safety", ""),
            ("R02", "positive", "查规范", "knowledge", ""),
        ],
    )
    agent = _runner_returning("safety")

    # Act & Assert:门槛 0.9 → 不达标
    monkeypatch.setenv("GYT_EVAL_THRESHOLD_ROUTING", "0.9")
    get_settings.cache_clear()
    strict = await run_suite(SUITES["routing"], agent, datasets_dir=tmp_path)
    assert strict.score == pytest.approx(0.5)
    assert strict.status is Status.FAILED
    assert strict.threshold == pytest.approx(0.9)

    # Act & Assert:改门槛到 0.5 → 达标。证明数字确实来自 config,没在脚本里写死
    monkeypatch.setenv("GYT_EVAL_THRESHOLD_ROUTING", "0.5")
    get_settings.cache_clear()
    lenient = await run_suite(SUITES["routing"], agent, datasets_dir=tmp_path)
    assert lenient.status is Status.PASSED


async def test_run_suite_validates_dataset_before_calling_agent(tmp_path: Path) -> None:
    # Arrange:label 写错的一行
    _write_csv(
        tmp_path / "safety.csv",
        SAFETY_HEADER,
        [("S01", "positive", "photo_01.jpg", "有违规", "未戴安全帽", "")],
    )
    called: list[str] = []

    async def _spy(row: Mapping[str, str]) -> Any:
        called.append(row["id"])
        return {"label": "violation", "violations": "未戴安全帽"}

    # Act & Assert:数据集写错要在烧额度之前炸
    with pytest.raises(DatasetError):
        await run_suite(SUITES["safety"], _spy, datasets_dir=tmp_path)
    assert called == [], "数据集没过校验就不该调用被测 Agent"


async def test_run_suite_one_exploding_row_does_not_kill_the_run(tmp_path: Path) -> None:
    # Arrange
    _write_csv(
        tmp_path / "routing.csv",
        ROUTING_HEADER,
        [
            ("R01", "positive", "看照片", "safety", ""),
            ("R02", "positive", "查规范", "knowledge", ""),
        ],
    )

    # Act
    report = await run_suite(SUITES["routing"], _exploding_runner, datasets_dir=tmp_path)

    # Assert:两条都按错计,但整套仍然出了报告
    assert report.total == 2
    assert report.passed_count == 0
    assert "抛异常" in report.results[0].reason
    assert "RuntimeError" in report.results[0].actual


async def test_run_suites_runs_each_registered_suite(tmp_path: Path) -> None:
    # Arrange:只给 routing 接被测对象,另外两套应各自 SKIP
    _write_csv(
        tmp_path / "routing.csv", ROUTING_HEADER, [("R01", "positive", "看照片", "safety", "")]
    )

    # Act
    reports = await run_suites(
        tuple(SUITES), {"routing": _runner_returning("safety")}, datasets_dir=tmp_path
    )

    # Assert
    assert [r.suite for r in reports] == ["routing", "safety", "rag", "orchestration"]
    assert [r.status for r in reports] == [
        Status.PASSED,
        Status.SKIPPED,
        Status.SKIPPED,
        # 第四条是 orchestration(2026-08-22 加)。这条用例刻意把
        # 「跑了哪几套」和「各是什么状态」分成两句断言 —— 加套时两句都要跟,
        # 只改一句的表现是另一句报「Left contains one more item」,而人会先去查 runner。
        Status.SKIPPED,
    ]


async def test_safety_suite_runs_end_to_end_with_fake_vision(tmp_path: Path) -> None:
    # Arrange:两张照片,模型第二张漏报了反光衣
    _write_csv(
        tmp_path / "safety.csv",
        SAFETY_HEADER,
        [
            ("S01", "positive", "photo_01.jpg", "violation", "未戴安全帽", ""),
            ("S02", "positive", "photo_02.jpg", "violation", "未戴安全帽;未穿反光衣", ""),
        ],
    )
    fake_vision = _runner_by_id(
        {
            "S01": {"label": "violation", "violations": "未戴安全帽"},
            "S02": {"label": "violation", "violations": "未戴安全帽"},
        }
    )

    # Act
    report = await run_suite(SUITES["safety"], fake_vision, datasets_dir=tmp_path)

    # Assert
    assert report.score == pytest.approx(0.5)
    assert [r.row_id for r in report.results if not r.passed] == ["S02"]


# ===========================================================================
# 八、报告与退出码
# ===========================================================================


def _report(status: Status, results: tuple[RowScore, ...] = ()) -> SuiteReport:
    return SuiteReport(suite="routing", status=status, threshold=0.9, results=results)


def test_format_report_lists_failed_rows_with_expected_and_actual() -> None:
    # Arrange:只给总分是没用的,调提示词需要知道错在哪
    results = (
        RowScore("R01", True, "safety", "safety", "派给了期望的 Agent。"),
        RowScore("R02", False, "knowledge", "safety", "派错人了"),
    )

    # Act
    text = format_report(_report(Status.FAILED, results))

    # Assert
    assert "50.0%" in text and "(1/2)" in text
    assert "R02" in text and "期望:knowledge" in text and "实际:safety" in text
    assert "R01" not in text, "默认只展开挂掉的条目,通过的不刷屏"


def test_format_report_reports_all_pass_and_placeholder_notice() -> None:
    # Arrange:全对 + 有占位行被跳过
    report = SuiteReport(
        suite="routing",
        status=Status.PASSED,
        threshold=0.9,
        results=(RowScore("R01", True, "safety", "safety", "对了"),),
        skipped_ids=("R09", "R10"),
    )

    # Act
    text = format_report(report)

    # Assert:跳过了几条必须写在报告里,否则「10 条全对」会被误读成数据填满了
    assert "全部通过。" in text
    assert "跳过的占位行:R09、R10" in text


def test_format_report_verbose_shows_passing_rows() -> None:
    # Arrange
    results = (RowScore("R01", True, "safety", "safety", "对了"),)

    # Act
    text = format_report(_report(Status.PASSED, results), verbose=True)

    # Assert
    assert "R01" in text


def test_format_report_of_skipped_suite_explains_why() -> None:
    # Arrange
    report = SuiteReport(
        suite="rag", status=Status.SKIPPED, threshold=0.8, message="数据集还没填。"
    )

    # Act
    text = format_report(report)

    # Assert
    assert "SKIP" in text and "数据集还没填" in text


def test_format_summary_covers_every_suite() -> None:
    # Arrange
    reports = (
        _report(Status.PASSED, (RowScore("R01", True, "a", "a", ""),)),
        SuiteReport(suite="rag", status=Status.SKIPPED, threshold=0.8, message="没接"),
    )

    # Act
    text = format_summary(reports)

    # Assert
    assert "routing" in text and "rag" in text and "SKIP" in text


def test_empty_report_score_is_zero_not_division_error() -> None:
    # Act & Assert
    assert _report(Status.SKIPPED).score == 0.0


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((Status.PASSED, Status.SKIPPED), EXIT_OK),
        ((Status.SKIPPED, Status.SKIPPED), EXIT_OK),
        ((Status.PASSED, Status.FAILED), EXIT_BELOW_THRESHOLD),
    ],
)
def test_exit_code_of(statuses: tuple[Status, ...], expected: int) -> None:
    # Arrange
    reports = tuple(_report(status) for status in statuses)

    # Act & Assert:只有「低于门槛」才红,SKIP 不参与红绿判定
    assert exit_code_of(reports) == expected


# ===========================================================================
# 九、被测对象注入 与 命令行入口
# ===========================================================================


def test_load_runners_without_spec_returns_empty_table() -> None:
    # Act & Assert:不传就是一个都没接,三套全 SKIP
    assert load_runners(None) == {}
    assert load_runners("") == {}


def test_load_runners_resolves_module_attribute() -> None:
    # Act
    table = load_runners(f"{THIS_MODULE}:FAKE_RUNNERS")

    # Assert
    assert set(table) == {"routing"}


@pytest.mark.parametrize(
    "spec",
    [
        "没有冒号",
        "gyt.config:根本不存在的属性",
        "根本.不存在的.模块:X",
        f"{THIS_MODULE}:ROUTING_HEADER",  # 不是字典
    ],
)
def test_load_runners_rejects_bad_spec(spec: str) -> None:
    # Act & Assert
    with pytest.raises(RunnerSpecError):
        load_runners(spec)


def test_load_runners_rejects_unknown_suite_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange:套名打错会静默地什么都不跑,必须拦下来
    monkeypatch.setattr(runner_mod, "_BAD_TABLE", {"saftey": _runner_returning("x")}, raising=False)

    # Act & Assert
    with pytest.raises(RunnerSpecError, match="不认识的套名"):
        load_runners("eval.runner:_BAD_TABLE")


def test_main_returns_zero_when_everything_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Act:今天的真实状态 —— 数据集没填、Agent 没接,make eval 必须是绿的
    code = main(["--datasets-dir", str(tmp_path)])

    # Assert
    assert code == EXIT_OK
    assert "SKIP" in capsys.readouterr().out


def test_main_returns_nonzero_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange:一条数据,模型全答错
    _write_csv(
        tmp_path / "routing.csv",
        ROUTING_HEADER,
        [("R01", "positive", "查规范", "knowledge", "")],
    )
    monkeypatch.setattr(
        runner_mod, "_WRONG", {"routing": _runner_returning("safety")}, raising=False
    )

    # Act
    code = main(
        [
            "--suite",
            "routing",
            "--datasets-dir",
            str(tmp_path),
            "--runners",
            "eval.runner:_WRONG",
        ]
    )

    # Assert:低于门槛必须非 0,CI 才拦得住
    assert code == EXIT_BELOW_THRESHOLD
    assert "FAIL" in capsys.readouterr().out


def test_main_returns_dataset_error_code_on_broken_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange:表头缺列
    _write_csv(tmp_path / "routing.csv", ("id", "type"), [])
    monkeypatch.setattr(runner_mod, "_ANY", {"routing": _runner_returning("safety")}, raising=False)

    # Act
    code = main(
        ["--suite", "routing", "--datasets-dir", str(tmp_path), "--runners", "eval.runner:_ANY"]
    )

    # Assert
    assert code == EXIT_DATASET_ERROR
    assert "评测跑不起来" in capsys.readouterr().out


def test_main_verbose_flag_prints_passing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    _write_csv(
        tmp_path / "routing.csv", ROUTING_HEADER, [("R01", "positive", "看照片", "safety", "")]
    )
    monkeypatch.setattr(
        runner_mod, "_RIGHT", {"routing": _runner_returning("safety")}, raising=False
    )

    # Act
    code = main(
        [
            "--suite",
            "routing",
            "--datasets-dir",
            str(tmp_path),
            "--runners",
            "eval.runner:_RIGHT",
            "--verbose",
        ]
    )

    # Assert
    assert code == EXIT_OK
    assert "R01" in capsys.readouterr().out


FILLED_DATASETS: Final[dict[str, int]] = {
    "safety": 30,
    "routing": 33,
    "rag": 20,
    "orchestration": 26,
}
"""已经填完真数据的套 → 应有的可判分行数。

三套**都已填完**:safety 于 2026-08-07(27 张人工标注 + 3 张自备干扰项)、
routing 与 rag 于 2026-08-09(随 cad / knowledge 落地)。所以下面那条
「没登记的套必须还看得出是占位状态」的分支现在一条都走不到 —— 留着是为了
将来加第四套时仍有提示,不是说还有谁没填。

数字必须 ≥ config 里的 eval_min_rows_*(20 / 30 / 20),否则跑分脚本会直接判不通过。
⚠️ **safety 与 rag 卡死在下限上,一条不多**:safety 30=30、rag 20=20;
orchestration 刻意不这样 —— 26 行 / 下限 15,富余 11 行,理由写在 config 里那个字段上;
routing 33(下限 20:W7 加了 R23-R26 的 attendance 四行,W9 又加了 R27-R33 的
supervision 五行 + 两行对抗行,现在富余 13 条)。
删数据行、或让某行的备注里蹦出「待替换/请替换」被剔出分母,都会当场把整套打到硬闸以下。
"""


def test_shipped_datasets_are_readable() -> None:
    """仓库里现有的四份 CSV 必须能读、表头齐全。

    这条用例同时是给「填数据的人」用的进度指示器:
      · 还没填的套 —— 必须仍能看出是占位状态(有「待替换」字样);
      · 填完的套 —— 登记进 FILLED_DATASETS,改为断言**真实可判分条数**。
    四套现在都在 FILLED_DATASETS 里,所以走的全是第二条分支;
    第一条分支留给将来的第五套 —— 2026-08-22 加 orchestration 时它**一次都没走到**
    (数据集是一次填满的,没经过占位状态),这说明那条分支从写下来到现在没被验证过。
    """
    for name, spec in SUITES.items():
        rows = load_rows(spec)
        assert rows, f"{spec.dataset} 一行数据都没有"

        if name in FILLED_DATASETS:
            scorable = [row for row in rows if not is_placeholder(row)]
            assert len(scorable) == FILLED_DATASETS[name], (
                f"{spec.dataset} 可判分行数是 {len(scorable)},"
                f"登记的却是 {FILLED_DATASETS[name]} —— 改了数据集就同步改这里。"
            )
            continue

        assert any(is_placeholder(row) for row in rows), (
            f"{spec.dataset} 已经没有占位行了 —— 数据看来填好了,"
            f"请往本文件的 FILLED_DATASETS 里加一行 {name!r}: 真实条数。"
        )


# ===========================================================================
# 九、诊断指标(format_diagnostics)—— 不参与判定红绿,只让失败可诊断
# ===========================================================================


def _diag_row(
    row_id: str, expected: set[str], actual: set[str], *, label_ok: bool = True
) -> RowScore:
    return RowScore(
        row_id=row_id,
        passed=expected == actual and label_ok,
        expected="violation / ...",
        actual="violation / ...",
        reason="结论与违规项全部对上。"
        if label_ok
        else "判断结论就不对:该是 violation,模型说是 compliant。",
        expected_items=frozenset(expected),
        actual_items=frozenset(actual),
    )


def test_diagnostics_are_empty_without_set_fields() -> None:
    """routing / rag 的 RowScore 不填集合字段,诊断段应当整段不出现。"""
    plain = RowScore(row_id="R01", passed=True, expected="safety", actual="safety", reason="对")
    assert format_diagnostics([plain]) == []


def test_diagnostics_overlap_counts_partial_credit() -> None:
    """判分不给部分分,但诊断要看得见「差多远」。

    这正是加这个指标的理由:三项答对两项与一项没答对,在分数上都是 0,
    修法却完全相反(改提示词措辞 vs 换模型)。
    """
    rows = [
        _diag_row("S01", {"未戴安全帽", "未穿反光衣", "用电隐患"}, {"未戴安全帽", "未穿反光衣"}),
        _diag_row("S02", {"未戴安全帽"}, {"未戴安全帽"}),
    ]
    text = "\n".join(format_diagnostics(rows))
    # S01 Jaccard = 2/3,S02 = 1 → 平均 (0.667+1)/2 ≈ 83.3%
    assert "83.3%" in text
    assert "2 行" in text


def test_diagnostics_skip_rows_whose_label_was_wrong() -> None:
    """label 都答错的行,比违规项没有意义 —— 会把重合度稀释成看不懂的数字。"""
    rows = [
        _diag_row("S01", {"未戴安全帽"}, {"未戴安全帽"}),
        _diag_row("S02", {"用电隐患"}, set(), label_ok=False),
    ]
    text = "\n".join(format_diagnostics(rows))
    assert "100.0%" in text
    assert "仅统计结论判对的 1 行" in text


def test_diagnostics_report_per_class_recall() -> None:
    """新引入的类别靠这个看清是「完全没概念」还是「认得但措辞对不上」。"""
    rows = [
        _diag_row("S01", {"临边无防护"}, set()),
        _diag_row("S02", {"临边无防护"}, {"临边无防护"}),
        _diag_row("S03", {"未戴安全帽"}, {"未戴安全帽"}),
    ]
    text = "\n".join(format_diagnostics(rows))
    assert "临边无防护" in text and "1/2" in text
    assert "未戴安全帽" in text and "1/1" in text


def test_diagnostics_surface_over_reporting() -> None:
    """标注里从没有、模型却报了的词 —— 过触发的信号,必须单独列出来。

    提示词把 8 个词都摆在模型面前,它有动机去凑。不单列的话,
    这种行为会混在「违规项对不上」里看不出来。
    """
    rows = [_diag_row("S01", {"未戴安全帽"}, {"未戴安全帽", "消防通道堵塞"})]
    text = "\n".join(format_diagnostics(rows))
    assert "多报的类" in text
    assert "消防通道堵塞" in text


def test_要选工地的行必须成对写_一行选一行不选() -> None:
    """🔴 `requires_project=true` 的行要成对:一行填 `project_id`、一行留空。

    ===========================================================================
    为什么这条值得做成守卫
    ---------------------------------------------------------------------------
    两行测的是**两个不同的正确行为**:
      · 填了 project_id 的 —— 「选了工地之后这条链走不走得通」;
      · 留空的         —— 「没选工地时 supervisor 会不会先提醒去选」。
        后者是 `AgentSpec.requires_project` 那句提示词的**唯一守卫**。

    2026-08-22 当场撞到过:O09(cad>knowledge)只有「期望直接派活」这一行,
    而干净基线里实际是 supervisor 去要工地了 —— **两件都对,是评测缺一维**
    (数据集写在那句提示词之前)。补了 O26 之后才成对。

    ⚠️ 只写一行不会有任何东西报错:分数照样算得出来,只是有一个行为永远没人守,
    而它恰恰是最近才加的那个。所以这条不能只写在 README 里靠人记。
    """
    rows = load_rows(SUITES["orchestration"])
    要工地 = [r for r in rows if str(r.get("requires_project", "")).strip().lower() == "true"]
    assert 要工地, "一条 requires_project=true 的行都没有,那句提示词就完全没被测到"

    选了 = [r for r in 要工地 if str(r.get("project_id", "")).strip()]
    没选 = [r for r in 要工地 if not str(r.get("project_id", "")).strip()]

    assert 选了, (
        "requires_project=true 的行里没有一条填了 project_id —— "
        "「选了工地之后这条链走不走得通」没人测。"
    )
    assert 没选, (
        "requires_project=true 的行里没有一条留空 project_id —— "
        "「没选工地时会不会先提醒」没人测,而那是 AgentSpec.requires_project 唯一的守卫。"
    )


def test_至少有一行在守_没选工地时会不会先提醒() -> None:
    """`AgentSpec.requires_project` 那句提示词,必须**有人在守**。

    ===========================================================================
    ⚠️ 判据只能这么弱,不能更强 —— 这是 2026-08-22 当场纠正过的一次
    ---------------------------------------------------------------------------
    我先写的是「requires_project=true 且没填 project_id 的行,**一律**不许期望
    success」,理由是「supervisor 按提示词会先要工地」。守卫当场抓出一批行,
    **而抓错了**:同一个干净基线里 ——

        O04(cad,没选工地)   →  真的派给了 cad,success
        O09(cad,没选工地)   →  没派活,回头要工地

    同样的 Agent、同样没选工地,**行为不一样**。再看 supervisor 的原话:
    「查柱子间距这事**我派给看图纸的同事了**。不过有两件事得先办:1. 先选工地……」
    —— 它是**边派边提醒**,不是只提醒不派。

    也就是说那句提示词是**概率性生效**的(CLAUDE.md 反复写的那条:
    「提示词只是概率性生效,结构件才兜得住」)。拿一条断言它确定性生效的守卫去卡,
    只会让数据集被迫写成一个并不成立的样子。

    所以这里只守一件**真的**事:**至少有一行在测「会不会提醒」这个行为**。
    一行都没有的话,那句提示词就完全没人守 —— 而它是这个分支里唯一一处
    改了模型行为的地方。
    """
    rows = load_rows(SUITES["orchestration"])
    候选 = [
        r
        for r in rows
        if str(r.get("requires_project", "")).strip().lower() == "true"
        and not str(r.get("project_id", "")).strip()
        and r["expected_status"] == "clarify"
    ]
    assert 候选, (
        "没有任何一行在测「requires_project=true 且没选工地时会不会先提醒」。"
        "那句提示词是这个分支里唯一改了模型行为的地方,不能一行守卫都没有。"
    )
