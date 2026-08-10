"""RequireToolCall(公共防编造中间件)的单元测试 —— 不联网、不调模型。

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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from gyt.agents.schedule.guard import RequireLedgerTool
from gyt.core.require_tool import RequireToolCall

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
