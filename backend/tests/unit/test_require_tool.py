"""RequireToolCall / RequireReceiptSource(公共防编造中间件)的单元测试 —— 不联网、不调模型。

两次真机事故催生了这件结构件(案情见 core/require_tool.py 的 docstring):
schedule 不调工具就说「销了」(库里没销)、report 不调工具就报巡检记录编号(磁盘上没有)。

这里钉死的行为:
  · 回合首答没带工具调用 → 打回重试一次,且重试请求里带那条系统校验话术
  · 回合中段(刚拿到自家工具结果)纯文本合法 → 不打回
  · 重试仍不带工具:
      - on_give_up="pass"(台账档)→ 原样放行,只记 warning,绝不自己造循环
      - on_give_up="fail"(出文件档)→ 模型编的内容被顶替成如实说明,并记 error
  · 缺省是 "pass":将来接新 Agent 时漏配也只会变宽松,不会悄悄变严把好回答毙掉
  · 同步、异步两条路径行为完全一致

「共用行为」的用例都对两档配置各跑一遍(parametrize),
免得将来只顾着改一档、把另一档改瘸了。

文件末尾另有一节测 **RequireReceiptSource(编号溯源)** 的判据,包括 2026-08-16
W9 加的「第三种判据」:出处除了工具结果,也认**用户自己说的话** ——
理由与 report 侧的零影响证明都写在那一节的分节注释里。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from gyt.agents.schedule.guard import RequireLedgerTool
from gyt.core.require_tool import (
    RequireEvidenceCitation,
    RequireReceiptSource,
    RequireToolCall,
)

# 两条泳道的配置。report 那档在这里手写死,**不 import agents.report** ——
# 这是 core 层的测试,不该反向依赖某个 Agent 包;话术改不改跟本文件无关。
_LEDGER_KWARGS: dict[str, Any] = {
    "agent_name": "schedule",
    "nudge": "(系统校验)先调工具再回答,凭记忆回答等于做假账。",
    "on_give_up": "pass",
}
_DOC_GIVE_UP = "巡检记录没能生成,请再说一次「出巡检记录」让我重试一遍。"
_DOC_KWARGS: dict[str, Any] = {
    "agent_name": "report",
    "nudge": "(系统校验)先调出文件的工具,编号只能来自工具返回。",
    "on_give_up": "fail",
    "give_up_message": _DOC_GIVE_UP,
}

_BOTH_LANES = [
    pytest.param(_LEDGER_KWARGS, id="pass档-台账"),
    pytest.param(_DOC_KWARGS, id="fail档-出文件"),
]


@dataclass
class _FakeRequest:
    messages: list[BaseMessage] = field(default_factory=list)


@dataclass
class _FakeResponse:
    """模型响应的替身。**必须是 dataclass**,别改成普通类。

    中间件顶替文案时走 dataclasses.replace,真身 langchain 的 ModelResponse
    也正是 dataclass(langchain/agents/middleware/types.py:270)。
    """

    result: list[BaseMessage] = field(default_factory=list)
    structured_response: Any = None


class _ScriptedHandler:
    """按剧本吐响应,并记录每次收到的请求消息数。"""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.seen: list[list[BaseMessage]] = []

    async def __call__(self, request: Any) -> _FakeResponse:
        self.seen.append(list(request.messages))
        return self.responses.pop(0)


def _plain(text: str) -> _FakeResponse:
    # 带 id / name:真身里 name 由 create_agent 盖章(langchain/agents/factory.py:1420-1421),
    # 顶替文案时这两样必须留住,所以用例也得先带上才验得出来。
    return _FakeResponse(result=[AIMessage(content=text, id="msg-1", name="报告")])


def _with_tool() -> _FakeResponse:
    return _FakeResponse(
        result=[AIMessage(content="", tool_calls=[{"name": "finish_task", "args": {}, "id": "t1"}])]
    )


@pytest.mark.parametrize("kwargs", _BOTH_LANES)
async def test_首答无工具调用_打回重试并附系统校验(kwargs: dict[str, Any]) -> None:
    handler = _ScriptedHandler([_plain("销了:T1 已完成。"), _with_tool()])
    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])

    response = await RequireToolCall(**kwargs).awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "编出来的回答必须打回重试"
    assert any(kwargs["nudge"] in str(m.content) for m in handler.seen[1]), (
        "重试请求要带系统校验话术"
    )
    assert response.result[0].tool_calls, "最终采纳的是带工具调用的那次"


@pytest.mark.parametrize("kwargs", _BOTH_LANES)
async def test_交接回执之后的首答同样受管(kwargs: dict[str, Any]) -> None:
    handler = _ScriptedHandler([_plain("记上了:T9 云任务。"), _with_tool()])
    request = _FakeRequest(
        messages=[
            HumanMessage(content="记个任务"),
            ToolMessage(
                content="Successfully transferred to schedule",
                tool_call_id="t0",
                name="transfer_to_schedule",
            ),
        ]
    )

    await RequireToolCall(**kwargs).awrap_model_call(request, handler)

    assert len(handler.seen) == 2


@pytest.mark.parametrize("kwargs", _BOTH_LANES)
async def test_回合中段念回执是合法的_不打回(kwargs: dict[str, Any]) -> None:
    """刚拿到自家工具的结果,纯文本正是在念真回执 —— 打回它反而制造循环。"""
    handler = _ScriptedHandler([_plain("销了:复检三层钢筋(T1)完成。")])
    request = _FakeRequest(
        messages=[
            HumanMessage(content="T1 干完了"),
            AIMessage(content="", tool_calls=[{"name": "finish_task", "args": {}, "id": "t1"}]),
            ToolMessage(content='{"ok": true}', tool_call_id="t1", name="finish_task"),
        ]
    )

    response = await RequireToolCall(**kwargs).awrap_model_call(request, handler)

    assert len(handler.seen) == 1, "念真回执不许打回"
    assert "复检三层钢筋" in str(response.result[0].content), "更不许把真回执顶替掉"


@pytest.mark.parametrize("kwargs", _BOTH_LANES)
async def test_首答带工具调用_直接放行(kwargs: dict[str, Any]) -> None:
    handler = _ScriptedHandler([_with_tool()])
    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])

    await RequireToolCall(**kwargs).awrap_model_call(request, handler)

    assert len(handler.seen) == 1


@pytest.mark.parametrize("kwargs", _BOTH_LANES)
def test_同步路径同一套行为(kwargs: dict[str, Any]) -> None:
    calls: list[int] = []

    def handler(request: Any) -> _FakeResponse:
        calls.append(len(request.messages))
        return _plain("销了。") if len(calls) == 1 else _with_tool()

    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])
    response = RequireToolCall(**kwargs).wrap_model_call(request, handler)

    assert len(calls) == 2
    assert response.result[0].tool_calls


async def test_pass档_重试仍不带工具_放行不造循环(caplog: pytest.LogCaptureFixture) -> None:
    handler = _ScriptedHandler([_plain("销了。"), _plain("真销了,信我。")])
    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])

    with caplog.at_level(logging.WARNING, logger="gyt.core.require_tool"):
        response = await RequireToolCall(**_LEDGER_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "只重试一次"
    assert "信我" in str(response.result[0].content), "第二次的结果原样放行,熔断归上层"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "放行是可容忍的降级,只该记 warning"
    )


async def test_fail档_重试仍不带工具_编的内容被顶替(caplog: pytest.LogCaptureFixture) -> None:
    """fail 档的重点:不能让「编号 GYT-…」这种查无此物的话走到工友眼前。"""
    fabricated = "巡检记录出好了,编号 GYT-20260809-153739。"
    handler = _ScriptedHandler([_plain(fabricated), _plain(fabricated)])
    request = _FakeRequest(messages=[HumanMessage(content="出巡检记录")])

    with caplog.at_level(logging.WARNING, logger="gyt.core.require_tool"):
        response = await RequireToolCall(**_DOC_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "只重试一次"
    final = response.result[0]
    assert str(final.content) == _DOC_GIVE_UP, "最终出去的必须是如实说明"
    assert "GYT-20260809-153739" not in str(final.content), "编的编号一个字都不许漏出去"
    assert (final.id, final.name) == ("msg-1", "报告"), (
        "顶替的是内容不是整条消息:name 丢了 focus 滤网就认不出发言人,id 换了就成了新的一条"
    )
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "顶替是一次真实事故,必须记 error 而不是 warning"
    )


def test_fail档_同步路径也顶替() -> None:
    def handler(request: Any) -> _FakeResponse:
        return _plain("巡检记录出好了,编号 GYT-20260809-153739。")

    request = _FakeRequest(messages=[HumanMessage(content="出巡检记录")])
    response = RequireToolCall(**_DOC_KWARGS).wrap_model_call(request, handler)

    assert str(response.result[0].content) == _DOC_GIVE_UP


def test_fail档没配顶替文案_建图期就炸() -> None:
    """空文案不能等到线上真触发那一刻才发现 —— 那时候恰好最不能出错。"""
    with pytest.raises(ValueError, match="give_up_message"):
        RequireToolCall(agent_name="report", nudge="先调工具", on_give_up="fail")

    with pytest.raises(ValueError, match="give_up_message"):
        RequireToolCall(
            agent_name="report", nudge="先调工具", on_give_up="fail", give_up_message="   "
        )


def test_on_give_up_缺省是放行() -> None:
    """将来接新 Agent 漏配时只会变宽松;要变严必须显式写 fail + 文案。"""
    guard = RequireToolCall(agent_name="whoever", nudge="先调工具")

    assert guard.on_give_up == "pass"


def test_on_give_up_写错值直接拒绝() -> None:
    """拼错成 "Fail" 这种,不许静默当成放行 —— 那是「以为很严其实没管」。"""
    with pytest.raises(ValueError, match="on_give_up"):
        RequireToolCall(agent_name="report", nudge="先调工具", on_give_up="Fail")  # type: ignore[arg-type]


def test_台账挂的还是原来那套参数() -> None:
    """迁移到公共件之后,schedule 的行为必须一字不变:话术照旧、仍然放行。"""
    guard = RequireLedgerTool()

    assert isinstance(guard, RequireToolCall)
    assert guard.agent_name == "schedule"
    assert guard.on_give_up == "pass"
    assert guard.give_up_message == ""
    assert "做假账" in guard.nudge, "台账口径的系统校验话术不能丢"
    assert "finish_task" in guard.nudge, "话术要点名具体工具,不然模型不知道调哪个"


# ===========================================================================
# RequireReceiptSource(编号溯源)的**第三种判据** —— 出处也认「用户自己说的话」
#
# W9 方案 §5.3:supervision 泳道的用户会自己把隐患编号打进来
# (「GYT-H-… 那条复查了吗」)。原来的 _sourced 只扫 ToolMessage,模型原样带上
# 这个编号会被判成编造,于是一句完全正确的回答被 give_up 文案顶替掉。
#
# 这一节钉三件事:① 用户报的号算出处;② 模型自己编的号仍然拦得住;
# ③ **report 侧行为不变** —— 那是这次放宽最需要证明的东西。
# report 的正路测试在 test_report.py(跑的是真 Agent),这里补的是判据本身。
# ===========================================================================

_HAZARD_NO = "GYT-H-20260816-090000-1a2b"
"""一个长得像真号的隐患编号。**只出现在测试里** —— 提示词与 nudge 里一个都不许写。"""

_HAZARD_PATTERN = r"GYT-H-\d{8}-\d{6}-[0-9a-f]{4}"
"""supervision 那条正则的形状(派生真相在 core/doc_no.PATTERNS,这里只抄形状测判据)。"""

_SOURCE_KWARGS: dict[str, Any] = {
    "agent_name": "supervision",
    "pattern": _HAZARD_PATTERN,
    "nudge": "(系统校验)你报的编号找不到出处,先调工具查真实台账。",
    "give_up_message": "这条隐患我没查着,刚才报的号做不得数。",
}


def _tool_message(text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id="call-1", name="get_hazard")


async def test_用户自己报的编号算出处_模型复述它不算编造() -> None:
    """**这条是第三种判据的验收断言。**

    用户把编号打进来、模型原样带上 —— 这不是编造,顶替它就是把正确回答吃掉。
    真查不到的话,工具会如实回「台账里没有这条」,那是另一条路上的事。
    """
    reply = f"{_HAZARD_NO} 这条已经签过通知单了,下一步是到期复查。"
    handler = _ScriptedHandler([_plain(reply)])
    request = _FakeRequest(messages=[HumanMessage(content=f"{_HAZARD_NO} 这条隐患复查了吗")])

    response = await RequireReceiptSource(**_SOURCE_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 1, "有出处就该一次过,不许白付一轮重试"
    assert str(response.result[0].content) == reply


async def test_用户没提过的编号照样算编造() -> None:
    """放宽的是「出处的来源」,不是「可以不要出处」。

    用户问的是 A 号,模型报了个 B 号 —— 那仍然是编的,而监理会拿着 B 号去界面上找。
    """
    other = "GYT-H-20260816-101010-ffff"
    handler = _ScriptedHandler([_plain(f"查到了,{other} 已销项。")] * 2)
    request = _FakeRequest(messages=[HumanMessage(content=f"{_HAZARD_NO} 这条复查了吗")])

    response = await RequireReceiptSource(**_SOURCE_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "只重试一次"
    assert str(response.result[0].content) == _SOURCE_KWARGS["give_up_message"]
    assert other not in str(response.result[0].content)


async def test_工具结果仍然是合法出处() -> None:
    """老判据不许被改瘸:念真回执照旧放行。"""
    handler = _ScriptedHandler([_plain(f"{_HAZARD_NO} 现在是已签发通知单。")])
    request = _FakeRequest(
        messages=[
            HumanMessage(content="还有几条隐患没销"),
            _tool_message(f'{{"hazard_no": "{_HAZARD_NO}", "status": "notified"}}'),
        ]
    )

    response = await RequireReceiptSource(**_SOURCE_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 1
    assert _HAZARD_NO in str(response.result[0].content)


async def test_模型自己上一轮编的号不算出处() -> None:
    """出处只认工具结果与用户发言,**AIMessage 永远不算**。

    认了的话,模型上一轮编的号就成了这一轮继续编的依据 —— 那正是 few-shot
    自我模仿那条失效路径,守卫会从第二轮起对同一个假号全面放行。
    """
    handler = _ScriptedHandler([_plain(f"{_HAZARD_NO} 已销项。")] * 2)
    request = _FakeRequest(
        messages=[
            HumanMessage(content="还有几条隐患没销"),
            AIMessage(content=f"查到了,{_HAZARD_NO} 已销项。", name="supervision"),
            HumanMessage(content="那条现在什么状态"),
        ]
    )

    response = await RequireReceiptSource(**_SOURCE_KWARGS).awrap_model_call(request, handler)

    assert str(response.result[0].content) == _SOURCE_KWARGS["give_up_message"]


async def test_report侧不受影响_用户不会打巡检记录号() -> None:
    """**放宽 _sourced 之后,report 的行为必须一个字不变。**

    前提是事实层面的:巡检记录号是 report 调完工具才生成的,工友手上没有 ——
    他要么发照片、要么说「出巡检记录」,不可能把编号打进聊天框。所以对 report 而言,
    HumanMessage 这条新出处**恒为空**,判据等价于改动之前。

    (真 Agent 那一侧的回归在 test_report.py:那条用例里用户说的正是
    「给照片 <32位编号> 出份巡检记录」—— 一个 GYT- 编号都不带。)
    """
    report_kwargs: dict[str, Any] = {
        "agent_name": "report",
        "pattern": r"GYT-\d{8}-\d{6}",
        "nudge": "(系统校验)编号只能来自工具返回。",
        "give_up_message": "这回巡检记录没出成,请再说一次「出巡检记录」。",
    }
    fabricated = "巡检记录出好了,编号 GYT-20260809-153739。"
    handler = _ScriptedHandler([_plain(fabricated)] * 2)
    # 工友真实会说的那句话:有 32 位照片编号,没有任何 GYT- 编号
    request = _FakeRequest(messages=[HumanMessage(content=f"给照片 {'d' * 32} 出份巡检记录")])

    response = await RequireReceiptSource(**report_kwargs).awrap_model_call(request, handler)

    assert str(response.result[0].content) == report_kwargs["give_up_message"]
    assert "GYT-20260809-153739" not in str(response.result[0].content)


def test_守卫没配顶替文案_建图期就炸() -> None:
    """与 RequireToolCall 的 fail 档同款:真抓到编造那一刻,最不能再发现文案没配。"""
    with pytest.raises(ValueError, match="give_up_message"):
        RequireReceiptSource(
            agent_name="supervision", pattern=_HAZARD_PATTERN, nudge="先调工具", give_up_message=" "
        )


# ===========================================================================
# RequireEvidenceCitation（规范答案必须能追到检索结果）
# ===========================================================================

_CITATION_KWARGS: dict[str, Any] = {
    "agent_name": "knowledge",
    "tool_name": "search_regulation",
    "nudge": "请引用检索结果里的真实出处。",
    "give_up_message": "这次没有形成可核对的规范出处。",
}


def _regulation_result() -> ToolMessage:
    return ToolMessage(
        content=(
            '{"ok":true,"data":{"passages":['
            '{"text":"儿童活动用房要求","source":"幼儿园规范.pdf","page":9}'
            ']},"user_msg":"查到了","error_code":null}'
        ),
        tool_call_id="search-1",
        name="search_regulation",
    )


async def test_规范回答引用工具中的真实出处_直接放行() -> None:
    handler = _ScriptedHandler([_plain("要求如下。—— 依据《幼儿园规范.pdf》第 9 页")])
    request = _FakeRequest(messages=[HumanMessage(content="查规范"), _regulation_result()])

    response = await RequireEvidenceCitation(**_CITATION_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 1
    assert "《幼儿园规范.pdf》第 9 页" in str(response.result[0].content)


async def test_规范回答只说有明确要求_打回后采用带出处回答() -> None:
    handler = _ScriptedHandler(
        [
            _plain("规范对此有明确要求。"),
            _plain("要求如下。—— 依据《幼儿园规范.pdf》第 9 页"),
        ]
    )
    request = _FakeRequest(messages=[HumanMessage(content="查规范"), _regulation_result()])

    response = await RequireEvidenceCitation(**_CITATION_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 2
    assert any(_CITATION_KWARGS["nudge"] in str(m.content) for m in handler.seen[1])
    assert "第 9 页" in str(response.result[0].content)


async def test_规范回答重试仍无出处_用安全说明顶替() -> None:
    handler = _ScriptedHandler([_plain("规范有要求。"), _plain("确实有明确要求。")])
    request = _FakeRequest(messages=[HumanMessage(content="查规范"), _regulation_result()])

    response = await RequireEvidenceCitation(**_CITATION_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 2
    assert str(response.result[0].content) == _CITATION_KWARGS["give_up_message"]


async def test_命中内容答不上问题时明确拒答_不强迫引用无关条文() -> None:
    handler = _ScriptedHandler([_plain("知识库里查不到明确依据，不能据此判断。")])
    request = _FakeRequest(messages=[HumanMessage(content="查规范"), _regulation_result()])

    response = await RequireEvidenceCitation(**_CITATION_KWARGS).awrap_model_call(request, handler)

    assert len(handler.seen) == 1
    assert "查不到明确依据" in str(response.result[0].content)
