"""整链评测(``eval.scorers`` 的 orchestration 一套)单元测试。

前三套的判分测试在 ``test_eval_runner.py``,这一套单独开一个文件:它测的是
**跨 Agent 的调度**,判据(前缀 + 上限 + 状态)和前三套一条都不共用,
混进那个文件只会让「哪条规则由谁钉着」更难查。

这里钉的不是覆盖率,而是几类**不报错、但会让整套分数变成一个精确的假数字**的失效:

  · **前缀 + 上限必须一起生效。** 只有前缀没有上限的话,一个「把七个 Agent 全派一遍」
    的 supervisor 每行都能过 —— 任何期望路径都是那条长路径的前缀。
    这是判分最危险的方向:偏高的假分数。用例把这条正反两面都钉住。
  · **被测对象崩了不许拿分。** 拿到异常文本时如果兜底成「空路径」,
    ``expected_path`` 为空的那几行(clarify)会因为「空 == 空」判**对**。
    所以专门有一条「崩了 + clarify 行」的用例守着。
  · **少报 handoffs 不许成为逃生口。** 路径三跳而 handoffs 报 2,一样要拦下。
  · **报告措辞要能区分四种坏法**(顺序错 / 多派了活 / 状态不符 / 路径不匹配)。
    只说「错了」的话,没人知道该去调派活顺序还是收工条件 —— 这是 RowScore 的
    docstring 明写的要求,所以每种坏法各有一条用例断言它的关键词。
  · **`expected_path` 不许填 none。** 填了会过白名单却永远判不对
    (被测对象报上来的路径里不可能有一个叫 none 的成员),而报告写的是「路径不匹配」,
    矛头指向模型 —— 2026-08-11 ``report`` 踩过的那个坑。

全程零网络、零模型:被测的全是纯函数,输入就是一行 dict 加一个假的调度记录。
"""

from __future__ import annotations

from typing import Any

import pytest
from eval.scorers import (
    AGENT_NONE,
    ORCHESTRATION_AGENTS,
    ORCHESTRATION_STATUSES,
    ROUTING_AGENTS,
    DatasetError,
    parse_path,
    score_orchestration,
    validate_orchestration_row,
)

# --- 造数:一行整链集 + 一份调度记录 -----------------------------------------


def _orch_row(
    *,
    expected_path: str = "safety>knowledge",
    expected_status: str = "success",
    max_handoffs: str = "2",
    row_id: str = "O01",
    note: str = "",
) -> dict[str, str]:
    """造一行整链集。列名与 ``datasets/orchestration.csv`` 的表头逐字对应。"""
    return {
        "id": row_id,
        "type": "orchestration",
        "user_input": "拍了张照,看看有没有隐患顺便查下规范怎么规定的",
        "expected_path": expected_path,
        "requires_project": "false",
        "expected_status": expected_status,
        "max_handoffs": max_handoffs,
        "note": note,
    }


def _trace(
    path: list[str] | str | None = None,
    status: str = "success",
    handoffs: int | None = None,
) -> dict[str, Any]:
    """造一份被测对象交回来的调度记录。

    ``handoffs`` 不传时按路径长度填 —— 那是最常见的正常情况;
    「少报 handoffs」这种坏情况由专门的用例显式传一个偏小的数。
    """
    steps = [] if path is None else path
    return {
        "path": steps,
        "status": status,
        "handoffs": len(steps) if handoffs is None else handoffs,
    }


# ===========================================================================
# 一、parse_path —— 拆路径
# ===========================================================================


def test_路径按大于号拆成有序元组() -> None:
    # Arrange & Act
    path = parse_path("safety>knowledge")

    # Assert:是元组不是集合 —— 顺序就是这套评测的判据本身
    assert path == ("safety", "knowledge")


def test_路径拆分顺带去空白并转小写() -> None:
    # Arrange & Act:标注的人手抖打了空格 / 大写,不该因此判错
    path = parse_path("  Safety > KNOWLEDGE  ")

    # Assert
    assert path == ("safety", "knowledge")


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_空的expected_path是空元组不是报错(raw: Any) -> None:
    # Arrange & Act:空 = 不该派给任何 Agent,是一种**合法标注**;
    #               None 是「CSV 少了这一列」,也不该在这儿炸
    path = parse_path(raw)

    # Assert
    assert path == ()


@pytest.mark.parametrize("raw", ["safety>>knowledge", "safety>knowledge>", ">safety>knowledge"])
def test_空片段直接丢掉不占一跳(raw: str) -> None:
    # Arrange & Act:多打一个 > 不该让路径长度凭空多一格、平白撞上 max_handoffs
    path = parse_path(raw)

    # Assert
    assert path == ("safety", "knowledge")


def test_分号不是分隔符所以手误会被validate拦下() -> None:
    # Arrange:本仓其它多值列都用分号,在这一列写分号是可预料的手误
    path = parse_path("safety;knowledge")

    # Assert:它变成一个野 Agent 名,而不是被静默拆成两跳
    assert path == ("safety;knowledge",)
    with pytest.raises(DatasetError):
        validate_orchestration_row(_orch_row(expected_path="safety;knowledge"))


# ===========================================================================
# 二、validate_orchestration_row —— 拦标注错误
# ===========================================================================


def test_合法行不抛() -> None:
    # Arrange & Act & Assert:正常行必须安安静静过去
    validate_orchestration_row(_orch_row())


@pytest.mark.parametrize("status", sorted(ORCHESTRATION_STATUSES))
def test_三个状态都合法(status: str) -> None:
    # Arrange & Act & Assert
    validate_orchestration_row(_orch_row(expected_status=status))


def test_空路径的clarify行合法() -> None:
    # Arrange & Act & Assert:「意图不清该追问」正是这套要测的正向行为之一
    validate_orchestration_row(
        _orch_row(expected_path="", expected_status="clarify", max_handoffs="0")
    )


def test_拼错的agent名当场炸() -> None:
    # Arrange & Act & Assert:knowlege 漏了 d —— 放行的话这行永远判不对,
    #                        而报告写的是「路径不匹配」,矛头指向模型
    with pytest.raises(DatasetError, match="knowlege"):
        validate_orchestration_row(_orch_row(expected_path="safety>knowlege"))


def test_路径里不许填none而要留空() -> None:
    # Arrange & Act & Assert:被测对象报上来的路径里永远不会有一个叫 none 的成员,
    #                        填了就是一行永远判不对的死行(report 踩过的那个坑)
    with pytest.raises(DatasetError, match="none"):
        validate_orchestration_row(_orch_row(expected_path="none", max_handoffs="1"))


def test_报错文案要教人怎么改() -> None:
    # Arrange & Act
    with pytest.raises(DatasetError) as excinfo:
        validate_orchestration_row(_orch_row(expected_path="safety>knowlege"))

    # Assert:报错要说清合法值和分隔符,不然人只知道错了不知道怎么改
    message = str(excinfo.value)
    assert "safety" in message and ">" in message and "留空" in message


@pytest.mark.parametrize("status", ["", "ok", "SUCCESSS", "追问"])
def test_状态词表外一律炸(status: str) -> None:
    # Arrange & Act & Assert
    with pytest.raises(DatasetError, match="expected_status"):
        validate_orchestration_row(_orch_row(expected_status=status))


@pytest.mark.parametrize("cap", ["", "两跳", "1.5", "-1"])
def test_上限不是非负整数就炸(cap: str) -> None:
    # Arrange & Act & Assert:负数和小数都得拦 —— 一个 -1 的上限会让那行永远判不对
    with pytest.raises(DatasetError, match="max_handoffs"):
        validate_orchestration_row(_orch_row(max_handoffs=cap))


def test_上限装不下期望路径就炸() -> None:
    # Arrange & Act & Assert:标准答案自己就超了上限,这是标注自相矛盾,
    #                        不拦的话会以「多派了活」的名义记到模型头上
    with pytest.raises(DatasetError, match="永远判不对"):
        validate_orchestration_row(_orch_row(expected_path="safety>knowledge", max_handoffs="1"))


def test_上限比期望路径长是合法的() -> None:
    # Arrange & Act & Assert:那正是「前缀」这条余量的来源 —— 允许多绕一步
    validate_orchestration_row(_orch_row(expected_path="safety>knowledge", max_handoffs="3"))


def test_合法agent名单派生自路由白名单() -> None:
    # Arrange & Act & Assert:名单只许有一份真相。手抄一份的下场是下次加 Agent
    #                        只有一处跟着改,而另一处把新 Agent 判成拼写错误
    assert ORCHESTRATION_AGENTS == ROUTING_AGENTS - {AGENT_NONE}
    assert AGENT_NONE not in ORCHESTRATION_AGENTS


# ===========================================================================
# 三、score_orchestration —— 判过的路径
# ===========================================================================


def test_路径状态交接数全对就判过() -> None:
    # Arrange & Act
    result = score_orchestration(_orch_row(), _trace(["safety", "knowledge"]))

    # Assert
    assert result.passed is True
    assert result.row_id == "O01"


def test_期望是实际的前缀且没超上限就判过() -> None:
    # Arrange:期望只要求走到 safety,实际多走了一步 knowledge,上限给了 2
    row = _orch_row(expected_path="safety", max_handoffs="2")

    # Act
    result = score_orchestration(row, _trace(["safety", "knowledge"]))

    # Assert:这就是「前缀」那条余量 —— 多绕一步但该走的走对了,不一票否决
    assert result.passed is True


def test_空路径的clarify行不派活就判过() -> None:
    # Arrange
    row = _orch_row(expected_path="", expected_status="clarify", max_handoffs="0")

    # Act
    result = score_orchestration(row, _trace([], status="clarify"))

    # Assert:意图不清时反问一句是**正确行为**,不是失败
    assert result.passed is True


def test_模型输出的大小写和空格不影响判分() -> None:
    # Arrange & Act:格式问题不是调度问题
    result = score_orchestration(_orch_row(), _trace([" Safety ", "KNOWLEDGE"]))

    # Assert
    assert result.passed is True


def test_path也收字符串形式() -> None:
    # Arrange & Act:别逼每个 runner 的作者各写一套适配
    result = score_orchestration(_orch_row(), {"path": "safety>knowledge", "status": "success"})

    # Assert
    assert result.passed is True


# ===========================================================================
# 四、score_orchestration —— 四种坏法各说各的话
# ===========================================================================


def test_顺序颠倒判不过并且说清是顺序问题() -> None:
    # Arrange:同一批 Agent,先查规范后识图 —— 英雄链的顺序反了
    # Act
    result = score_orchestration(_orch_row(), _trace(["knowledge", "safety"]))

    # Assert
    assert result.passed is False
    assert "顺序" in result.reason


def test_顺序错时报告里的期望与实际长得不一样() -> None:
    # Arrange & Act
    result = score_orchestration(_orch_row(), _trace(["knowledge", "safety"]))

    # Assert:钉死「渲染路径不许排序」。拿 _describe 凑合的话两边排完序一模一样,
    #        看报告的人会以为判分函数疯了
    assert "safety>knowledge" in result.expected
    assert "knowledge>safety" in result.actual


def test_多派一跳超上限判不过() -> None:
    # Arrange:期望 safety>knowledge(是实际的前缀!),但实际多派了 schedule
    # Act
    result = score_orchestration(_orch_row(), _trace(["safety", "knowledge", "schedule"]))

    # Assert:**这条是整套的支点** —— 只有前缀没有上限的话,
    #        一个把所有人都派一遍的 supervisor 每行都能过
    assert result.passed is False
    assert "多派了活" in result.reason
    assert "上限 2 次" in result.reason


def test_少报交接数也拦得住() -> None:
    # Arrange:路径实打实走了三跳,却只报了 2 次交接
    # Act
    result = score_orchestration(
        _orch_row(), _trace(["safety", "knowledge", "schedule"], handoffs=2)
    )

    # Assert:取「路径长度」与「报上来的次数」里大的那个 ——
    #        取小的等于给多派活开一个逃生口
    assert result.passed is False
    assert "多派了活" in result.reason


def test_不该派活却派了要说清是不该派() -> None:
    # Arrange
    row = _orch_row(expected_path="", expected_status="clarify", max_handoffs="0")

    # Act
    result = score_orchestration(row, _trace(["safety"], status="success"))

    # Assert:空 expected_path 天生是任何路径的前缀,不单独说一句的话
    #        报告里只剩「超了上限」,读不出「压根不该派」
    assert result.passed is False
    assert "本不该派给任何 Agent" in result.reason


def test_路径完全不搭判不过并且说清是不匹配() -> None:
    # Arrange & Act
    result = score_orchestration(_orch_row(), _trace(["schedule", "attendance"]))

    # Assert
    assert result.passed is False
    assert "路径不匹配" in result.reason


def test_状态不符判不过() -> None:
    # Arrange:路径一步不差,但整条链没跑成
    # Act
    result = score_orchestration(_orch_row(), _trace(["safety", "knowledge"], status="fail"))

    # Assert
    assert result.passed is False
    assert "状态不符" in result.reason
    assert "success" in result.reason and "fail" in result.reason


def test_状态漏报也算不符() -> None:
    # Arrange & Act:被测对象没给 status —— 不能默默当成 success
    result = score_orchestration(_orch_row(), {"path": ["safety", "knowledge"]})

    # Assert
    assert result.passed is False
    assert "状态不符" in result.reason


def test_同时犯两条错就报两句() -> None:
    # Arrange & Act:顺序反了 + 状态也不对
    result = score_orchestration(_orch_row(), _trace(["knowledge", "safety"], status="fail"))

    # Assert:两条规则互不抑制。抑制逻辑本身会变成下一个 bug 的藏身处
    assert result.passed is False
    assert "顺序" in result.reason and "状态不符" in result.reason


# ===========================================================================
# 五、score_orchestration —— 拿到异常文本
# ===========================================================================


@pytest.mark.parametrize("actual", ["RuntimeError: 模型服务连不上", "", None, 3])
def test_拿不到调度记录一律判不过(actual: Any) -> None:
    # Arrange & Act:runner 的契约是「拿不到就 raise」,异常文本会原样落到这里
    result = score_orchestration(_orch_row(), actual)

    # Assert:理由要说清是「没拿到东西」而不是「路径走错了」——
    #        两者的修法完全不同(修被测对象 vs 调提示词)
    assert result.passed is False
    assert "没拿到可判分的调度记录" in result.reason


def test_崩了的clarify行也不许拿分() -> None:
    # Arrange:这条是**回归钉子**。拿到异常文本时若兜底成一条空路径,
    #         空 expected_path + 空实际路径 = 「空 == 空」,这行会判**对** ——
    #         模型崩了反倒拿分,整套分数当场作废
    row = _orch_row(expected_path="", expected_status="clarify", max_handoffs="0")

    # Act
    result = score_orchestration(row, "RuntimeError: 模型服务连不上")

    # Assert
    assert result.passed is False
    assert "没拿到可判分的调度记录" in result.reason


def test_异常文本原样进报告的实际列() -> None:
    # Arrange & Act:「第 7 条为什么挂了」要能一眼看见
    result = score_orchestration(_orch_row(), "RuntimeError: 模型服务连不上")

    # Assert
    assert result.actual == "RuntimeError: 模型服务连不上"


class Test没派活时两档状态不作区分:
    """`clarify` 与 `fail` 在「两边路径都空」时视为同一档(2026-08-22 定)。

    🔴 **这是明知的精度损失,不是漏了。** 下次看见它别当 bug 修掉。

    区分这两档靠的是 `hooks.classify_outcome` 里那条「末尾有没有问号」——
    很粗。supervisor 说「这事我做不了,你是要我记一条任务吗?」既是拒绝也带问号;
    换个说法不带问号,同一个意思判成 fail。实测 O20 就在两轮之间翻过面 ——
    它测的其实不是模型对不对,是这条判据的边界在哪。

    真要分开得上外部 LLM 裁判读那句话的意图(像 safety 套那样),那是另一件事。
    在那之前这套能可靠回答的只有**「派没派活」**。
    """

    def _row(self, expected_status: str) -> dict[str, str]:
        return {
            "id": "OX",
            "expected_path": "",
            "expected_status": expected_status,
            "max_handoffs": "0",
        }

    def test_期望fail实际clarify_算过(self) -> None:
        got = {"path": [], "status": "clarify", "handoffs": 0}
        assert score_orchestration(self._row("fail"), got).passed

    def test_期望clarify实际fail_也算过(self) -> None:
        got = {"path": [], "status": "fail", "handoffs": 0}
        assert score_orchestration(self._row("clarify"), got).passed

    def test_但是派了活就不能再互换(self) -> None:
        """🔴 互换**只在两边都没派活时**成立。

        派了活还判 fail(或反之)是真的不一致 —— 那时候路径本身就是硬证据,
        不该被这条宽容规则盖掉。

        ⚠️ 这条用例的取值是被变异测试逼出来的。第一版写的是
        `expected=success` vs `actual=clarify` —— 而 success 本来就不在互换集合里,
        所以把「两边都没派活」这个限定**整个拿掉,那一版照样绿**:
        它测的是「success 不参与互换」(另一条已经在测),不是「限定还在不在」。

        要钉住限定,两个状态**都得在互换集合里**({clarify, fail}),而路径**非空**
        —— 只有这个组合能让那个限定成为唯一的判据。
        """
        row = {
            "id": "OY",
            "expected_path": "safety",
            "expected_status": "clarify",
            "max_handoffs": "1",
        }
        got = {"path": ["safety"], "status": "fail", "handoffs": 1}
        sc = score_orchestration(row, got)
        assert not sc.passed
        assert "状态不符" in sc.reason

    def test_success不参与互换(self) -> None:
        """success 表示「派了活并跑完」,它和「没派活」是两件事,任何时候都不许互换。"""
        got = {"path": [], "status": "clarify", "handoffs": 0}
        assert not score_orchestration(self._row("success"), got).passed
