"""RequireLedgerTool(防假账中间件)的单元测试 —— 不联网、不调模型。

2026-08-08 真机抓获的病:多轮之后 schedule 不调工具、照着上文自己的回执
「补」一条(嘴上销了库里没销)。这里钉死中间件的三条行为:
  · 回合首答没带工具调用 → 打回重试一次,且重试请求里带系统校验话术
  · 回合中段(刚拿到自家工具结果)纯文本合法 → 不打回
  · 重试仍不带工具 → 放行(绝不自己造循环,熔断归 recursion_limit 管)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from gyt.agents.schedule.guard import RequireLedgerTool


@dataclass
class _FakeRequest:
    messages: list[BaseMessage] = field(default_factory=list)


@dataclass
class _FakeResponse:
    result: list[BaseMessage] = field(default_factory=list)


class _ScriptedHandler:
    """按剧本吐响应,并记录每次收到的请求消息数。"""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.seen: list[list[BaseMessage]] = []

    async def __call__(self, request: Any) -> _FakeResponse:
        self.seen.append(list(request.messages))
        return self.responses.pop(0)


def _plain(text: str) -> _FakeResponse:
    return _FakeResponse(result=[AIMessage(content=text)])


def _with_tool() -> _FakeResponse:
    return _FakeResponse(
        result=[AIMessage(content="", tool_calls=[{"name": "finish_task", "args": {}, "id": "t1"}])]
    )


async def test_首答无工具调用_打回重试并附系统校验() -> None:
    handler = _ScriptedHandler([_plain("销了:T1 已完成。"), _with_tool()])
    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])

    response = await RequireLedgerTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "假账回答必须打回重试"
    assert any("做假账" in str(m.content) for m in handler.seen[1]), "重试请求要带系统校验话术"
    assert response.result[0].tool_calls, "最终采纳的是带工具调用的那次"


async def test_交接回执之后的首答同样受管() -> None:
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

    await RequireLedgerTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 2


async def test_回合中段念回执是合法的_不打回() -> None:
    """刚拿到自家工具的结果,纯文本正是在念真回执 —— 打回它反而制造循环。"""
    handler = _ScriptedHandler([_plain("销了:复检三层钢筋(T1)完成。")])
    request = _FakeRequest(
        messages=[
            HumanMessage(content="T1 干完了"),
            AIMessage(content="", tool_calls=[{"name": "finish_task", "args": {}, "id": "t1"}]),
            ToolMessage(content='{"ok": true}', tool_call_id="t1", name="finish_task"),
        ]
    )

    await RequireLedgerTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 1, "念真回执不许打回"


async def test_首答带工具调用_直接放行() -> None:
    handler = _ScriptedHandler([_with_tool()])
    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])

    await RequireLedgerTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 1


async def test_重试仍不带工具_放行不造循环() -> None:
    handler = _ScriptedHandler([_plain("销了。"), _plain("真销了,信我。")])
    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])

    response = await RequireLedgerTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "只重试一次"
    assert "信我" in str(response.result[0].content), "第二次的结果原样放行,熔断归上层"


def test_同步路径同一套行为() -> None:
    calls: list[int] = []

    def handler(request: Any) -> _FakeResponse:
        calls.append(len(request.messages))
        return _plain("销了。") if len(calls) == 1 else _with_tool()

    request = _FakeRequest(messages=[HumanMessage(content="T1 干完了")])
    response = RequireLedgerTool().wrap_model_call(request, handler)

    assert len(calls) == 2
    assert response.result[0].tool_calls
