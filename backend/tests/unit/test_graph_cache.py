"""**整图**级的缓存断言 —— 「演示断网靠彩排缓存复演」到底成不成立。

为什么单独一个文件、而不是并进 test_llm_cache.py:
    test_llm_cache.py 的四条路径**全部**建在 create_gyt_agent 上,也就是全部只走
    子 Agent 那一层。supervisor 那一层一个断言都没有 —— 而演示真正要跑的是整图。
    历史教训:supervisor 汇总那一次调用永远命不中缓存(库每轮新造 uuid4 当
    tool_call_id,见 core/llm.py 图 0.5 第二段),却带着 182 条绿测试和 98% 覆盖率
    合了进来,原因就是回归锁装在了不会被撞的那扇门上。

    子 Agent 复演得了、supervisor 说不出最后那句话 —— 而用户在 chat-ui 上看到的
    恰恰就是最后那句话。所以必须有人在**整图**这一层数模型被调了几次。

红线:本文件不允许发生任何真实网络调用(llm.ChatOpenAI 被换成假模型)。
注意:gyt.graph 一被 import 就会真的建图(见 graph.py 结论三),
所以**必须在打完桩之后**再 import,本文件靠 graph_module 夹具保证这个顺序。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from types import ModuleType
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from gyt.core import llm

# --- 常量 ------------------------------------------------------------------------

QUESTION = "帮我 ping 一下看看通不通"
SUB_AGENT_REPLY = "pong"
SUPERVISOR_REPLY = "已经确认系统是通的。"
HANDOFF_TOOL_PREFIX = "transfer_to_"

# 一次完整的整图问答:supervisor 派活 + 子 Agent 干活 + supervisor 汇总 = 3 次真调。
FULL_GRAPH_CALLS = 3


class HandoffAwareFakeModel(BaseChatModel):
    """会数数、且**真的走一次交接**的假模型。

    supervisor 与子 Agent 共用同一个实例(get_chat_model 被换成返回它),
    靠「自己这次被绑了哪些工具」区分身份:

        绑了 transfer_to_* 的 = supervisor
            └ 还没有工具结果 --> 出一次交接工具调用(派活)
            └ 已经有工具结果 --> 出终答(汇总)
        没绑交接工具的       = 子 Agent --> 直接出一句话
    """

    model_id: str = "fake-handoff-model"
    bound_tools: list[Any] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "gyt-handoff-fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        """进 llm_string 的模型身份。不带 calls,否则调一次键变一次。"""
        return {"model_id": self.model_id}

    def _handoff_tool_name(self) -> str | None:
        for spec in self.bound_tools:
            name = spec.get("function", {}).get("name", "") if isinstance(spec, dict) else ""
            if name.startswith(HANDOFF_TOOL_PREFIX):
                return name
        return None

    def _next_message(self, messages: Sequence[BaseMessage]) -> AIMessage:
        handoff = self._handoff_tool_name()
        if handoff is None:
            return AIMessage(content=SUB_AGENT_REPLY)
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content=SUPERVISOR_REPLY)
        return AIMessage(
            content="",
            tool_calls=[{"name": handoff, "args": {}, "id": "handoff-fixed-0001"}],
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append("generate")
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append("agenerate")
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.calls.append("astream")
        message = self._next_message(messages)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_calls=message.tool_calls)
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """记下自己被绑了什么工具 —— 这是本假模型区分 supervisor / 子 Agent 的唯一依据。"""
        converted = [convert_to_openai_tool(t) for t in tools]
        return self.model_copy(update={"bound_tools": converted}).bind(tools=converted, **kwargs)


class _Offline(ConnectionError):
    """演示日断网:模型层直接抛这个,断言整图仍然靠缓存把话说完。"""


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> HandoffAwareFakeModel:
    """换掉 llm.ChatOpenAI 而不是 get_chat_model —— 后者里面装着 install_llm_cache,
    整个换掉就等于绕过了生产接线,测了个寂寞。"""
    model = HandoffAwareFakeModel()
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **_kwargs: model)
    return model


@pytest.fixture
def graph_module(fake_model: HandoffAwareFakeModel) -> Iterator[ModuleType]:
    """打完桩之后再 import gyt.graph(模块级 build_graph() 会真的建图)。"""
    import gyt.graph as graph_module

    yield graph_module


async def _ask_graph(graph_module: ModuleType) -> str:
    """建一张全新的图问一句话,返回最后一条消息的文本。"""
    graph = graph_module.build_graph()
    result = await graph.ainvoke({"messages": [("user", QUESTION)]})
    return str(result["messages"][-1].content)


# ===========================================================================
# 核心:整图第二遍必须零新增调用
# ===========================================================================


async def test_整图第二次同输入完全不再调用模型(
    graph_module: ModuleType, fake_model: HandoffAwareFakeModel
) -> None:
    """D9 第二层兜底(断网靠彩排缓存复演)在**整图**层面成立与否的唯一硬证据。

    这条会红的典型原因:supervisor 交接回来时库新造了 uuid4 当 tool_call_id,
    它进了 supervisor 汇总那一次的 prompt,缓存键每轮都不一样 ——
    见 core/llm.py 的 _renumber_tool_call_ids。
    """
    # Arrange:第一遍冷启动,派活 + 子 Agent + 汇总 = 3 次真调
    first = await _ask_graph(graph_module)
    assert first == SUPERVISOR_REPLY
    assert len(fake_model.calls) == FULL_GRAPH_CALLS

    # Act:同样一句话再问一遍(整张图重建,证明缓存不依赖实例状态)
    second = await _ask_graph(graph_module)

    # Assert:答案一致,且**一次都没有新增调用**
    assert second == SUPERVISOR_REPLY
    assert len(fake_model.calls) == FULL_GRAPH_CALLS


async def test_断网时整图仍能靠彩排缓存说完最后一句(
    graph_module: ModuleType, fake_model: HandoffAwareFakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """演示日场景:彩排联网跑过一遍,当天断网,用户发彩排过的原话。

    要求不是「吐出一半」,而是**最终答复一个字不少**地出来 ——
    屏幕上停在英文交接提示、然后抛裸异常,比完全不答更难圆场。
    """
    # Arrange:彩排(联网)
    assert await _ask_graph(graph_module) == SUPERVISOR_REPLY

    # 断网:模型层任何一次真调都直接炸
    async def _offline(*_args: Any, **_kwargs: Any) -> ChatResult:
        raise _Offline("演示日断网,不许发出任何请求")

    monkeypatch.setattr(HandoffAwareFakeModel, "_agenerate", _offline)
    monkeypatch.setattr(HandoffAwareFakeModel, "_astream", _offline)

    # Act & Assert:整图照样把话说完
    assert await _ask_graph(graph_module) == SUPERVISOR_REPLY


async def test_整图换个问题仍会真调模型(
    graph_module: ModuleType, fake_model: HandoffAwareFakeModel
) -> None:
    """反向确认:上面那条不是「一律命中」这种假阳性。"""
    # Arrange
    await _ask_graph(graph_module)
    baseline = len(fake_model.calls)

    # Act:换一句没彩排过的话
    graph = graph_module.build_graph()
    await graph.ainvoke({"messages": [("user", "帮我看看这张照片")]})

    # Assert
    assert len(fake_model.calls) > baseline
