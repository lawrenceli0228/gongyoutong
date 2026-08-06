"""TODO-5 收口的断言测试 —— 「真实 Agent 调用路径确实经过磁盘缓存」。

这个文件存在的唯一理由:**这条链路会静默失效**。

    缓存没生效时,系统一切正常 —— 答案照出、日志照打、测试照绿,
    只是每次都真的花钱问了一遍模型。等到演示当天断网启用兜底,
    才会发现 cache_dir 里空空如也。所以必须有人**数模型被调了几次**。

红线:本文件不允许发生任何真实网络调用。
  · llm.ChatOpenAI 被换成 CountingFakeChatModel —— 注意是换构造器而不是换
    get_chat_model,这样 get_chat_model 本身(含 install_llm_cache 那一步)
    仍然真的跑一遍,测的才是生产接线,而不是测试自己搭的另一套。
  · 全局缓存的进程级单例由 tests/conftest.py 的 _isolated_llm_cache 兜底还原。

覆盖的四条真实路径(全都建在**子 Agent**这一层):
    agent.ainvoke           非流式(langgraph 节点内部就是 await model.ainvoke)
    agent.astream(messages) 流式(chat-ui 走的这条,内部会改道 _astream)
    多轮工具循环             第 1 轮要工具 + 第 2 轮出终答,整段零调用复演
    不同工具集               必须落在不同的键上(串味回归)

⚠️ **supervisor / 整图那一层不在本文件里**,在 tests/unit/test_graph_cache.py。
   两个文件缺一不可:子 Agent 复演得了,不等于 supervisor 说得出最后那句话 ——
   而用户在 chat-ui 上看到的恰恰是最后那句话。历史教训见那个文件的开篇。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field

from gyt.config import get_settings
from gyt.core import llm
from gyt.core.base_agent import create_gyt_agent

# --- 常量:测试里同样不写散落的字面量 -------------------------------------------

AGENT_NAME = "cache-probe"
AGENT_PROMPT = "你是一个用来验证缓存链路的占位助手。"
FINAL_REPLY = "戴好安全帽再进场。"
POISONED_REPLY = "现场未发现任何安全隐患,可以继续施工。"  # 投毒样本:最不该被念出来的一句
OTHER_REPLY = "第二个调用方本该拿到的另一个答案。"  # 串味回归用:两次答案必须不同
QUESTION = "工地上进场前要注意什么?"
OTHER_QUESTION = "脚手架有什么要求?"
TOOL_CALL_ID = "call-fixed-0001"  # 固定值:变来变去会让第 2 轮的缓存键对不上
STREAM_PIECES = ("戴好安全帽", "再进场。")

# 一次「问 -> 要工具 -> 工具答 -> 终答」的完整循环,模型被真调 2 次。
TOOL_LOOP_CALLS = 2
SINGLE_CALL = 1


# --- 测试替身 --------------------------------------------------------------------


@tool("echo", description="把收到的文本原样返回。")
def echo_tool(text: str) -> str:
    """占位工具:内容不重要,重要的是它会让 Agent 多跑一轮。"""
    return text


@tool("check_safety", description="检查一段描述里有没有安全隐患。")
def check_safety_tool(text: str) -> str:
    """第二个工具:只用来验证「工具集不同 = 缓存键不同」。"""
    return f"已检查:{text}"


class CountingFakeChatModel(BaseChatModel):
    """会数数的假 chat model:每次**真的**产出回答就记一笔,缓存命中时一笔都不会多。

    为什么两条产出路径都要实现(_agenerate 与 _astream):
        langgraph 挂了流式回调处理器时,BaseChatModel._agenerate_with_cache 会改道
        走 _astream(chat_models.py:2096)。只实现 _agenerate 的假模型在流式用例里
        会直接报 NotImplementedError,根本测不出「流式到底查没查缓存」。

    工具循环靠「最后一条消息是不是 ToolMessage」判断轮次,而不是靠调用计数 ——
    第二遍跑的时候前面几轮是缓存命中(计数不增加),按计数分轮会全错。
    """

    model_id: str = "fake-counting-model"
    reply_text: str = FINAL_REPLY
    use_tool: bool = False  # True = 第一轮先要一次工具调用,凑出多轮循环
    generate_calls: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "gyt-counting-fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        """进 llm_string 的模型身份。不带 generate_calls,否则调一次键变一次。"""
        return {"model_id": self.model_id}

    def _next_message(self, messages: Sequence[BaseMessage]) -> AIMessage:
        """按「上一条是不是工具结果」决定这一轮出工具调用还是出终答。"""
        already_used_tool = any(isinstance(m, ToolMessage) for m in messages)
        if self.use_tool and not already_used_tool:
            return AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"text": QUESTION}, "id": TOOL_CALL_ID}],
            )
        return AIMessage(content=self.reply_text)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步路径本项目不用,但 BaseChatModel 要求实现,顺手也记一笔。"""
        self.generate_calls.append("generate")
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.generate_calls.append("agenerate")
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """逐片吐出终答,模拟真实模型的 token 流(工具调用轮不走这条,见下方断言)。"""
        self.generate_calls.append("astream")
        message = self._next_message(messages)
        for piece in STREAM_PIECES if message.content else ("",):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """按真实 ChatOpenAI 的行为把工具转成 OpenAI schema 再绑。

        必须转 —— langgraph-prebuilt 会去读绑定结果里每个工具的 .get("name"),
        直接塞 BaseTool 对象会炸。顺带这也让 llm_string 里的工具维度是稳定字符串。
        """
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)


# --- 夹具 ------------------------------------------------------------------------


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> CountingFakeChatModel:
    """把 llm.ChatOpenAI 换成会数数的假模型,但**保留** get_chat_model 的真实逻辑。

    这一点是本文件的关键:install_llm_cache() 就写在 get_chat_model 里,
    如果像别处那样直接把 get_chat_model 整个换掉,缓存根本不会被装上 ——
    测试会绿,而生产接线有没有断开完全测不出来。
    """
    model = CountingFakeChatModel()
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **_kwargs: model)
    return model


def _build_agent(tools: list[BaseTool] | None = None) -> CompiledStateGraph:
    """走**真实**工厂建一个子 Agent(等价于 5 个业务 Agent 的建法)。"""
    return create_gyt_agent(name=AGENT_NAME, prompt=AGENT_PROMPT, tools=list(tools or []))


async def _ask(agent: CompiledStateGraph, question: str = QUESTION) -> str:
    """问一句,取最后一条消息的文本。"""
    result = await agent.ainvoke({"messages": [("user", question)]})
    return str(result["messages"][-1].content)


async def _ask_streaming(agent: CompiledStateGraph, question: str = QUESTION) -> list[Any]:
    """按 chat-ui 的方式问一句:stream_mode="messages",收下所有事件。"""
    events: list[Any] = []
    async for event in agent.astream({"messages": [("user", question)]}, stream_mode="messages"):
        events.append(event)
    return events


# ===========================================================================
# 核心:真实 Agent 路径确实经过缓存
# ===========================================================================


async def test_agent_第二次同输入完全不再调用模型(fake_model: CountingFakeChatModel) -> None:
    """TODO-5 的验收断言:同一个问题问第二遍,内层模型一次都不许被调用。

    这是「演示断网靠彩排缓存复演」(D9 三层兜底第二层)成立与否的唯一硬证据。
    """
    # Arrange:第一遍是冷启动,允许真调
    agent = _build_agent()
    first = await _ask(agent)
    assert first == FINAL_REPLY
    assert len(fake_model.generate_calls) == SINGLE_CALL
    assert list(get_settings().cache_dir.glob("*.json"))  # 确实落盘了

    # Act:同样的问题再来一遍(整个 Agent 重建一次,证明缓存不依赖实例状态)
    second = await _ask(_build_agent())

    # Assert:答案一致,且模型调用次数**没有增加**
    assert second == FINAL_REPLY
    assert len(fake_model.generate_calls) == SINGLE_CALL


async def test_agent_流式路径同样命中缓存(fake_model: CountingFakeChatModel) -> None:
    """chat-ui 是流式的。缓存要是只在非流式路径生效,等于演示当天没有兜底。

    顺带钉死一条实测结论:流式请求会改道 _astream(而不是 _agenerate),
    但缓存查询发生在改道**之前**(chat_models.py:2030-2059),所以照样命中。
    """
    # Arrange:冷启动一遍流式请求
    agent = _build_agent()
    first_events = await _ask_streaming(agent)
    assert fake_model.generate_calls == ["astream"]  # 确实走了流式分支
    assert first_events  # 冷启动有逐片 token

    # Act
    second_events = await _ask_streaming(_build_agent())

    # Assert:模型没被再调,而且 UI 仍然收得到内容(只是不再逐字)
    assert fake_model.generate_calls == ["astream"]
    assert second_events
    streamed = "".join(str(getattr(event[0], "content", "")) for event in second_events)
    assert streamed == FINAL_REPLY


async def test_多轮工具循环能整段零调用复演(fake_model: CountingFakeChatModel) -> None:
    """最容易被忽略、也最要命的一条:多轮循环必须**整段**复演,不能只复演第一轮。

    langchain 命中缓存时会往 AIMessage 上盖一个 usage_metadata.total_cost=0
    (chat_models.py:_convert_cached_generations),这条被改过的消息会进入第 2 轮的
    prompt。缓存键若不先做归一化(llm._strip_prompt_noise),第 2 轮就永远对不上,
    彩排得跑三遍才收敛 —— 演示当天赌不起。这条测试就是那个归一化的回归锁。
    """
    # Arrange:让假模型先要一次工具调用,凑出「要工具 -> 工具答 -> 终答」两轮
    fake_model.use_tool = True
    agent = _build_agent([echo_tool])
    first = await _ask(agent)
    assert first == FINAL_REPLY
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS

    # Act
    second = await _ask(_build_agent([echo_tool]))

    # Assert:两轮全部命中,一次模型调用都没有新增
    assert second == FINAL_REPLY
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS


async def test_不同工具集不共享缓存(fake_model: CountingFakeChatModel) -> None:
    """串味回归:同一句话、不同工具集,该给的答案完全不同,绝不能互相取走。

    产品上的后果:safety(带识图工具)和 report(带落盘工具)问同一句话时,
    一方会把另一方的答案原样念出来 —— 零次模型调用、日志里只有一行"命中缓存"。
    比"没有缓存"严重得多,所以这条必须钉死。
    """
    # Arrange:工具集 A
    await _ask(_build_agent([echo_tool]))
    assert len(fake_model.generate_calls) == SINGLE_CALL

    # Act:同一句话,换成工具集 B
    await _ask(_build_agent([check_safety_tool]))

    # Assert:必须重新问模型(键里含工具集),且磁盘上是两份不同的缓存
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS
    assert len(list(get_settings().cache_dir.glob("*.json"))) == TOOL_LOOP_CALLS

    # 再回到工具集 A:命中它自己的那份,不再新增调用
    await _ask(_build_agent([echo_tool]))
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS


async def test_不同问题各自成键(fake_model: CountingFakeChatModel) -> None:
    """换个问题当然要重新问模型 —— 反向确认缓存不是"一律命中"这种假阳性。"""
    await _ask(_build_agent(), QUESTION)
    await _ask(_build_agent(), OTHER_QUESTION)
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS


async def test_伪造的缓存文件在_Agent_路径上同样被拒(
    fake_model: CountingFakeChatModel,
) -> None:
    """图 3 的身份核对必须在 Agent 路径上也生效 —— 这条路径的投毒面比直调大得多。

    收口之后**所有** Agent 调用都读 cache_dir,而 cache_dir 是宿主机绑定挂载、
    彩排缓存还要在队员之间拷来拷去。读侧不核对的话,谁能往这个目录写文件,
    谁就能决定判断工地危险的 Agent 说什么。

    ⚠️ 投毒样本必须**保留原 meta、只换正文**。真实的投毒者不会去删 meta ——
    meta 里的四项(key / model / prompt_version / messages_digest)全是关于「问题」的,
    一项都不校验答案,所以把 message.content 换掉、meta 一个字节不动是最省事的打法。
    早先这条用例构造的是「meta 整个删掉」,那当然过不了核对 —— 它给的是与事实
    相反的安全感,真正该测的是下面这种。
    """
    # Arrange:先正常跑一遍拿到真实的缓存文件,再**只**把正文换成伪造答案
    await _ask(_build_agent())
    cached_file = next(iter(get_settings().cache_dir.glob("*.json")))
    payload = json.loads(cached_file.read_text(encoding="utf-8"))
    poisoned = {
        "meta": payload["meta"],  # 原封不动
        "message": {**payload["message"], "content": POISONED_REPLY},
    }
    cached_file.write_text(json.dumps(poisoned, ensure_ascii=False), encoding="utf-8")

    # Act
    answer = await _ask(_build_agent())

    # Assert:伪造内容一个字都不许流出去,而且必须真的去问了模型
    assert POISONED_REPLY not in answer
    assert answer == FINAL_REPLY
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS


async def test_整份伪造的缓存文件同样被拒(fake_model: CountingFakeChatModel) -> None:
    """另一半威胁模型:连 meta 都没有的整份伪造文件(旧格式遗留文件也走这条)。"""
    # Arrange
    await _ask(_build_agent())
    cached_file = next(iter(get_settings().cache_dir.glob("*.json")))
    cached_file.write_text(
        json.dumps({"message": {"content": POISONED_REPLY, "type": "ai"}}),
        encoding="utf-8",
    )

    # Act & Assert
    assert await _ask(_build_agent()) == FINAL_REPLY
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS


async def test_提示词版本变了旧缓存全部失效(
    monkeypatch: pytest.MonkeyPatch, fake_model: CountingFakeChatModel
) -> None:
    """D18:改了提示词就把 prompt_version +1,旧答案绝不能再被拿出来糊弄人。"""
    await _ask(_build_agent())
    assert len(fake_model.generate_calls) == SINGLE_CALL

    monkeypatch.setenv("GYT_PROMPT_VERSION", "v2")
    get_settings.cache_clear()
    await _ask(_build_agent())

    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS


async def test_关掉缓存后每次都真的调模型(
    monkeypatch: pytest.MonkeyPatch, fake_model: CountingFakeChatModel
) -> None:
    """GYT_LLM_CACHE_ENABLED=false 时不许装缓存,也不许往磁盘上写。"""
    monkeypatch.setenv("GYT_LLM_CACHE_ENABLED", "false")
    get_settings.cache_clear()

    await _ask(_build_agent())
    await _ask(_build_agent())

    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS
    assert not list(get_settings().cache_dir.glob("*.json"))


# ===========================================================================
# install_llm_cache:全局单例的装/卸
# ===========================================================================


def test_install_llm_cache_幂等(monkeypatch: pytest.MonkeyPatch) -> None:
    """建 6 个 Agent 就是 6 次 get_chat_model,不能每次都换一个新缓存对象。"""
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **_kwargs: CountingFakeChatModel())
    first = llm.get_chat_model("text") and llm.install_llm_cache()
    second = llm.install_llm_cache()
    assert isinstance(first, llm.GytDiskCache)
    assert first is second


def test_install_llm_cache_关掉时只卸自己装的那个() -> None:
    """配置关掉缓存时:自己装的要卸掉,别人装的一根手指都不许碰。"""
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import get_llm_cache, set_llm_cache

    # 自己装的 -> 关掉配置后应被卸掉
    llm.install_llm_cache()
    assert isinstance(get_llm_cache(), llm.GytDiskCache)
    disabled = get_settings().model_copy(update={"llm_cache_enabled": False})
    assert llm.install_llm_cache(disabled) is None
    assert get_llm_cache() is None

    # 别人装的 -> 原样留着
    foreign = InMemoryCache()
    set_llm_cache(foreign)
    assert llm.install_llm_cache(disabled) is None
    assert get_llm_cache() is foreign


# ===========================================================================
# 适配层的边界行为(坏输入不许把主流程带崩)
# ===========================================================================


def test_prompt_解析不了就原样参与算键() -> None:
    """prompt 不是合法 JSON 时宁可少命中,也绝不报错、更不能误命中别人的答案。"""
    assert llm._normalize_prompt("这不是 JSON") == "这不是 JSON"


def test_归一化剥掉运行时噪声但保住类型标识() -> None:
    """只筛 kwargs 里的字段;对象自身那个 id(类路径)是类型标识,剥了就会串味。"""
    serialized = [
        {
            "lc": 1,
            "type": "constructor",
            "id": ["langchain", "schema", "messages", "AIMessage"],
            "kwargs": {
                "content": "你好",
                "type": "ai",
                "id": "run-会变的-0001",
                "usage_metadata": {"total_cost": 0},
                "response_metadata": {"finish_reason": "stop"},
            },
        }
    ]
    stripped = llm._strip_prompt_noise(serialized)

    assert stripped[0]["id"] == ["langchain", "schema", "messages", "AIMessage"]
    assert stripped[0]["kwargs"] == {"content": "你好", "type": "ai"}
    # 噪声不同、正文相同的两条消息必须落在同一个键上
    noisy = json.loads(json.dumps(serialized))
    noisy[0]["kwargs"]["id"] = "run-另一个-9999"
    assert llm._strip_prompt_noise(noisy) == stripped


def test_不带_lc_标志的普通结构原样保留() -> None:
    """非 langchain 序列化对象(比如裸 dict)不该被筛字段,否则会误伤真内容。"""
    plain = {"role": "user", "content": "你好", "id": "keep-me"}
    assert llm._strip_prompt_noise(plain) == plain


@pytest.mark.parametrize(
    ("llm_string", "expected"),
    [
        ('{"kwargs": {"model_name": "deepseek-v4-flash"}}---[]', "deepseek-v4-flash"),
        ('{"kwargs": {"model": "kimi-k3"}}---[]', "kimi-k3"),
        ('{"kwargs": {}}---[]', "unknown-model"),
        ("[1, 2, 3]---[]", "unknown-model"),
        ("根本不是 JSON---[]", "unknown-model"),
    ],
)
def test_从_llm_string_里抠模型名(llm_string: str, expected: str) -> None:
    """抠不出来就用占位名 —— 只影响缓存文件 meta 的可读性,不影响正确性。"""
    assert llm._model_name_from_llm_string(llm_string) == expected


def test_多候选结果不落盘() -> None:
    """n>1 的多候选采样本项目不用;真遇上宁可不缓存,也不落一份读回来缺斤少两的答案。"""
    two = [ChatGeneration(message=AIMessage(content="甲")), ChatGeneration(message=AIMessage("乙"))]
    assert llm._single_ai_message(two) is None
    assert llm._single_ai_message([]) is None


def test_流式攒出的_chunk_先合并回普通消息再落盘() -> None:
    """AIMessageChunk 直接 model_dump 会带上 chunk 独有字段,读侧一读就炸=永远不命中。"""
    merged = llm._single_ai_message([ChatGeneration(message=AIMessageChunk(content="你好"))])
    assert isinstance(merged, AIMessage)
    assert not isinstance(merged, AIMessageChunk)
    assert merged.content == "你好"


def test_缓存条目坏掉时_Agent_照常拿到答案(fake_model: CountingFakeChatModel) -> None:
    """缓存只是加速手段,坏了就当没有,绝不能让它把正事搞崩。"""
    cache = llm.install_llm_cache()
    assert cache is not None
    key = llm.cache_key("any", ["x"], "y")
    (get_settings().cache_dir / f"{key}.json").write_text("坏文件", encoding="utf-8")
    assert cache.lookup("[]", "任意-llm-string") is None


def test_适配层对_langchain_永不上抛(monkeypatch: pytest.MonkeyPatch) -> None:
    """lookup/update 被接在 langchain 的 _agenerate_with_cache 上,一抛就打穿正在进行的对话。

    典型触发源:cache_dir 中途变得不可写(宿主机绑定挂载被改成只读、磁盘写满)——
    那一层会抛 RuntimeError,而缓存本该只是可有可无的加速层。
    """

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("数据目录创建失败")

    monkeypatch.setattr(llm, "_stamp_for_llm_cache", _boom)
    cache = llm.GytDiskCache()

    assert cache.lookup("[]", "任意") is None
    cache.update("[]", "任意", [ChatGeneration(message=AIMessage(content="x"))])  # 不许抛


def test_工具调用_id_按位置归一化() -> None:
    """库每轮新造 uuid4 当 tool_call_id(langgraph_supervisor/handoff.py:132)。

    不按位置归一化的话,supervisor 汇总那一次调用的键每轮都不一样、永远命不中 ——
    整图级后果由 tests/unit/test_graph_cache.py 钉死,这里钉的是归一化本身的语义。
    """

    def _round(first: str, second: str) -> Any:
        return [
            {"lc": 1, "kwargs": {"tool_calls": [{"name": "t", "args": {}, "id": first}]}},
            {"lc": 1, "kwargs": {"tool_call_id": first, "content": "结果一"}},
            {"lc": 1, "kwargs": {"tool_calls": [{"name": "t", "args": {}, "id": second}]}},
            {"lc": 1, "kwargs": {"tool_call_id": second, "content": "结果二"}},
        ]

    # 同一份对话、两轮不同的 uuid,归一化后必须完全一致
    assert llm._renumber_tool_call_ids(_round("uuid-A", "uuid-B"), {}) == (
        llm._renumber_tool_call_ids(_round("uuid-C", "uuid-D"), {})
    )
    # 但两次不同的工具调用仍要落在不同序号上,配对关系不许被抹平
    normalized = llm._renumber_tool_call_ids(_round("uuid-A", "uuid-B"), {})
    assert normalized[0]["kwargs"]["tool_calls"][0]["id"] == "tc0"
    assert normalized[1]["kwargs"]["tool_call_id"] == "tc0"
    assert normalized[2]["kwargs"]["tool_calls"][0]["id"] == "tc1"
    assert normalized[3]["kwargs"]["tool_call_id"] == "tc1"
    # tool_calls 里混进不是 dict 的东西也不许炸(宁可少命中,也不能把主流程带崩)
    assert llm._renumber_tool_call_ids({"tool_calls": ["怪东西", 7]}, {}) == {
        "tool_calls": ["怪东西", 7]
    }


async def test_路径乙不同_cache_extra_必须各调一次模型(
    fake_model: CountingFakeChatModel,
) -> None:
    """cache_extra 的「防串味」承诺必须是真的。

    全局缓存的键是 (prompt, llm_string),里面**没有 cache_extra**。路径乙若不把
    内层全局缓存关掉,两个 Agent 外层各算各的键(看起来隔开了),内层却撞进同一条 ——
    第二个 Agent 原样取走第一个的答案,而且零次网络调用。评测打分器与知识抽取
    正是这么用路径乙的,串了就是「拿摘要文本当判分结果」。
    """
    # Arrange:真实的 get_chat_model(装着全局缓存),同一句话
    model = llm.get_chat_model("text")
    messages = [("user", QUESTION)]

    # Act
    first = await llm.ainvoke(model, messages, cache_extra="safety")
    fake_model.reply_text = OTHER_REPLY
    second = await llm.ainvoke(model, messages, cache_extra="report")

    # Assert:各调一次、各拿各的答案;而且一次问答只落一个文件(没有双写)
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS
    assert (first.content, second.content) == (FINAL_REPLY, OTHER_REPLY)
    assert len(list(get_settings().cache_dir.glob("*.json"))) == TOOL_LOOP_CALLS

    # 再问一次 safety:命中自己那份,不新增调用
    again = await llm.ainvoke(model, messages, cache_extra="safety")
    assert again.content == FINAL_REPLY
    assert len(fake_model.generate_calls) == TOOL_LOOP_CALLS
