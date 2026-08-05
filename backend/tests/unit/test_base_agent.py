"""gyt.core.base_agent 的单元测试 —— Agent 薄工厂。

红线：本文件不允许发生任何真实网络调用。所有模型一律换成下面的 FakeChatModel，
凭据由 tests/conftest.py 的 _isolated_settings（autouse）注入的假 Key 顶着。

关于 FakeChatModel 为什么放在测试文件里而不是 conftest.py：
    conftest.py 属于 L6 泳道，T1 阶段并行开发期间不跨泳道改文件（改了必冲突）。
    等 T1 合流之后，如果第三个泳道也需要这个假模型，再由 L6 统一挪进 conftest，
    届时把这里和 test_graph.py 的 import 一起删掉即可。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.state import CompiledStateGraph

from gyt.core import llm
from gyt.core.base_agent import (
    PROMPT_FILENAME,
    RESERVED_AGENT_NAME,
    create_gyt_agent,
    load_prompt,
)

# —— 常量：测试里也不写散落的字面量 ——

FAKE_REPLY_TEXT = "这是假模型的固定回复，没有联网。"
SAMPLE_AGENT_NAME = "sample"
SAMPLE_PROMPT = "你是一个用于测试的占位助手。"
# GBK 编码的「安全帽」三个字，用来构造「不是 UTF-8」的提示词文件。
GBK_PROMPT_BYTES = "安全帽".encode("gbk")


class FakeChatModel(BaseChatModel):
    """假的 chat model：不联网，回一句固定的话。

    必须实现 bind_tools 的原因：
        langgraph_supervisor.create_supervisor 在建图阶段就会调用
        `model.bind_tools(all_tools)`（supervisor.py:420-426），而
        langchain_core 的 BaseChatModel.bind_tools 默认是 raise NotImplementedError，
        不重写的话 test_graph.py 建图直接就炸了。
        另外 langgraph-prebuilt 的 _should_bind_tools 会去读绑定结果里每个工具的
        `.get("type")` / `.get("name")`，也就是说**绑进去的必须是 OpenAI 风格的 dict**，
        不能直接把 BaseTool 对象塞进去，所以这里要过一道 convert_to_openai_tool。
    """

    reply_text: str = FAKE_REPLY_TEXT

    @property
    def _llm_type(self) -> str:
        return "gyt-fake-chat-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """永远返回同一条 AIMessage。测试只关心「图能不能建起来」，不关心内容。"""
        message = AIMessage(content=self.reply_text)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """按真实 ChatOpenAI 的行为把工具转成 OpenAI schema 后再绑定。"""
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)


class ModelStub(NamedTuple):
    """打桩结果：既能拿到假模型本身，也能回看 get_chat_model 被怎么调的。"""

    model: FakeChatModel
    calls: list[tuple[str, dict[str, Any]]]


@pytest.fixture
def stub_chat_model(monkeypatch: pytest.MonkeyPatch) -> ModelStub:
    """把 gyt.core.llm.get_chat_model 换成假的，并记录每次调用的 purpose。

    打在 llm 模块对象上（而不是 base_agent 里）是有意的：base_agent 用的是
    `from gyt.core import llm` + `llm.get_chat_model(...)`，属性查找发生在调用时，
    所以在这里 setattr 才能真正生效。
    """
    model = FakeChatModel()
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_get_chat_model(purpose: str = "text", **overrides: Any) -> FakeChatModel:
        calls.append((purpose, overrides))
        return model

    monkeypatch.setattr(llm, "get_chat_model", _fake_get_chat_model)
    return ModelStub(model=model, calls=calls)


@tool("dummy", description="测试用占位工具，把收到的文本原样返回。")
def dummy_tool(text: str) -> str:
    """占位工具。base_agent 只负责把工具组装进去，不关心工具干什么。"""
    return text


def _write_prompt(directory: Path, content: str) -> Path:
    """在指定目录写一份 prompt.md，返回文件路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / PROMPT_FILENAME
    path.write_text(content, encoding="utf-8")
    return path


# ===========================================================================
# load_prompt
# ===========================================================================


def test_load_prompt_剥掉单个跨行的_html_注释块(tmp_path: Path) -> None:
    # Arrange：注释块跨多行，正文在注释后面
    _write_prompt(
        tmp_path,
        "<!--\n维护说明：这段不该进模型上下文。\n改动记录：2026-08-05 新建。\n-->\n"
        "# 角色\n你是测试助手。",
    )

    # Act
    body = load_prompt(tmp_path)

    # Assert
    assert "维护说明" not in body
    assert "改动记录" not in body
    assert "<!--" not in body and "-->" not in body
    assert body == "# 角色\n你是测试助手。"


def test_load_prompt_剥掉多个注释块且不吃掉中间的正文(tmp_path: Path) -> None:
    # Arrange：两段注释夹着一段正文，考验非贪婪匹配
    _write_prompt(
        tmp_path,
        "<!-- 第一段注释 -->\n正文甲\n<!-- 第二段注释 -->\n正文乙\n",
    )

    # Act
    body = load_prompt(tmp_path)

    # Assert：正文甲、正文乙都要在（贪婪匹配会把「正文甲」一起吃掉）
    assert "正文甲" in body
    assert "正文乙" in body
    assert "第一段注释" not in body
    assert "第二段注释" not in body


def test_load_prompt_去掉首尾空白(tmp_path: Path) -> None:
    # Arrange
    _write_prompt(tmp_path, "\n\n   正文有空白包着   \n\n")

    # Act / Assert
    assert load_prompt(tmp_path) == "正文有空白包着"


def test_load_prompt_文件不存在时抛出中文_FileNotFoundError(tmp_path: Path) -> None:
    # Arrange：目录存在但里面没有 prompt.md
    empty_dir = tmp_path / "no_prompt"
    empty_dir.mkdir()

    # Act / Assert
    with pytest.raises(FileNotFoundError) as excinfo:
        load_prompt(empty_dir)
    assert PROMPT_FILENAME in str(excinfo.value)
    assert "找不到提示词文件" in str(excinfo.value)


@pytest.mark.parametrize(
    ("content", "case_name"),
    [
        ("", "整个文件是空的"),
        ("   \n\n  \t ", "只有空白字符"),
        ("<!-- 只有维护说明，一句正文都没写 -->", "剥完注释就空了"),
    ],
)
def test_load_prompt_内容为空时抛出_ValueError(
    tmp_path: Path, content: str, case_name: str
) -> None:
    # Arrange
    _write_prompt(tmp_path, content)

    # Act / Assert：空提示词是配置事故，必须显式失败，不许静默放过
    with pytest.raises(ValueError) as excinfo:
        load_prompt(tmp_path)
    assert "空" in str(excinfo.value), case_name


def test_load_prompt_非_utf8_文件抛出中文_ValueError(tmp_path: Path) -> None:
    # Arrange：模拟队友在 Windows 上误存成 GBK
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / PROMPT_FILENAME).write_bytes(GBK_PROMPT_BYTES)

    # Act / Assert
    with pytest.raises(ValueError) as excinfo:
        load_prompt(tmp_path)
    assert "UTF-8" in str(excinfo.value)


def test_load_prompt_能读出真实的_ping_提示词且注释已剥净() -> None:
    # Arrange：直接读仓库里那份真提示词，防止「测试用例过了但真文件写坏了」
    from gyt.agents.ping import PING_DIR

    # Act
    body = load_prompt(PING_DIR)

    # Assert：注释已剥掉，且提示词里的两条硬约束还在（prompt.md 里明确写了禁改）
    assert "<!--" not in body
    assert "维护说明" not in body
    assert "必须调用 echo 工具" in body
    assert "禁止" in body


# ===========================================================================
# create_gyt_agent
# ===========================================================================


def test_create_gyt_agent_返回已编译的图且名字正确(stub_chat_model: ModelStub) -> None:
    # Act
    agent = create_gyt_agent(name=SAMPLE_AGENT_NAME, prompt=SAMPLE_PROMPT, tools=[dummy_tool])

    # Assert：langchain.agents.create_agent 返回值已经 compile 过，不用也不该再 compile
    assert isinstance(agent, CompiledStateGraph)
    assert agent.name == SAMPLE_AGENT_NAME


def test_create_gyt_agent_默认走_text_档模型(stub_chat_model: ModelStub) -> None:
    # Act
    create_gyt_agent(name=SAMPLE_AGENT_NAME, prompt=SAMPLE_PROMPT, tools=[dummy_tool])

    # Assert
    assert stub_chat_model.calls == [("text", {})]


def test_create_gyt_agent_把_purpose_原样透传给_get_chat_model(
    stub_chat_model: ModelStub,
) -> None:
    # Act：safety 识图要走 vision 档（Kimi），这条链路不能断
    create_gyt_agent(
        name=SAMPLE_AGENT_NAME,
        prompt=SAMPLE_PROMPT,
        tools=[dummy_tool],
        purpose="vision",
    )

    # Assert
    assert [purpose for purpose, _ in stub_chat_model.calls] == ["vision"]


def test_create_gyt_agent_不修改调用方传进来的工具列表(
    stub_chat_model: ModelStub,
) -> None:
    # Arrange：不可变原则 —— 调用方常常直接把模块级常量（如 PING_TOOLS）传进来
    original: list[BaseTool] = [dummy_tool]
    snapshot = list(original)

    # Act
    create_gyt_agent(name=SAMPLE_AGENT_NAME, prompt=SAMPLE_PROMPT, tools=original)

    # Assert
    assert original == snapshot
    assert original is not snapshot


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", "空字符串"),
        ("   ", "只有空白"),
        (RESERVED_AGENT_NAME, "LangGraph 是保留名，langgraph_supervisor 会拒绝"),
    ],
)
def test_create_gyt_agent_拒绝非法的_agent_名字(
    stub_chat_model: ModelStub, name: str, reason: str
) -> None:
    # Act / Assert
    with pytest.raises(ValueError) as excinfo:
        create_gyt_agent(name=name, prompt=SAMPLE_PROMPT, tools=[dummy_tool])
    assert "名字" in str(excinfo.value), reason


@pytest.mark.parametrize("blank_prompt", ["", "   \n\t "])
def test_create_gyt_agent_拒绝空提示词(stub_chat_model: ModelStub, blank_prompt: str) -> None:
    # Act / Assert
    with pytest.raises(ValueError) as excinfo:
        create_gyt_agent(name=SAMPLE_AGENT_NAME, prompt=blank_prompt, tools=[dummy_tool])
    assert "提示词" in str(excinfo.value)


def test_create_gyt_agent_参数不合法时根本不去造模型(
    stub_chat_model: ModelStub,
) -> None:
    # Act：参数校验必须发生在造模型之前，否则缺 Key 的环境下会先抛
    #      MissingAPIKeyError，把真正的「名字写错了」这个原因盖住
    with pytest.raises(ValueError):
        create_gyt_agent(name="", prompt=SAMPLE_PROMPT, tools=[dummy_tool])

    # Assert
    assert stub_chat_model.calls == []


__all__ = ["FakeChatModel", "ModelStub"]
