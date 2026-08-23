"""Knowledge Agent —— 规范检索(RAG):带页码出处的条文问答。

本体走 **text 档**(DeepSeek):检索由本地 BGE-M3 向量完成(不联网、不烧钱),
本体只负责调 search_regulation、把命中原文讲成人话并照抄出处(《文件》第 N 页)。

节点名 = 交接工具名(transfer_to_knowledge)= chat-ui 显示名 = routing.csv 的
expected_agent = scorers.ROUTING_AGENTS 里的 "knowledge" —— 五处一字不差,别改。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.knowledge.tools import KNOWLEDGE_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt
from gyt.core.require_tool import RequireEvidenceCitation, RequireToolCall

KNOWLEDGE_AGENT_NAME = "knowledge"

KNOWLEDGE_DIR = Path(__file__).parent

_REQUIRE_SEARCH = RequireToolCall(
    agent_name=KNOWLEDGE_AGENT_NAME,
    nudge=(
        "(系统校验)你刚才没有调用工具就直接回答了。规范问题必须先调 "
        "search_regulation 检索真实条文;不能凭记忆判断知识库有没有、也不能自己补出处。"
        "现在重新处理:先调用 search_regulation。"
    ),
    on_give_up="fail",
    give_up_message="这次规范检索没有实际执行成功,我不能凭记忆回答。请重试一次。",
)

_REQUIRE_CITATION = RequireEvidenceCitation(
    agent_name=KNOWLEDGE_AGENT_NAME,
    tool_name="search_regulation",
    nudge=(
        "(系统校验)你已经检索过，但刚才的回答没有可核对的真实出处。"
        "凡是声称规范有要求，必须逐字采用工具结果中的 source 和 page，写成"
        "《source》第 page 页；如果命中文本答不上问题，就在开头明确说"
        "『知识库里查不到明确依据』。不要复述其他 Agent 的结论代替规范证据。"
    ),
    give_up_message=(
        "这次检索结果没有形成可核对的规范出处，我不能据此判断是否符合。"
        "请把审查点拆开后重试，或直接核对原规范。"
    ),
)

__all__ = ["KNOWLEDGE_AGENT_NAME", "KNOWLEDGE_DIR", "build_knowledge_agent"]


def build_knowledge_agent() -> CompiledStateGraph:
    """组装 knowledge Agent(已编译,可直接挂给 Supervisor)。

    返回已编译的 CompiledStateGraph,.name 为 "knowledge"。
    走 text 档(DeepSeek);向量检索在工具内部,建图阶段不加载 BGE-M3(那 2.2GB
    只在真的调 search_regulation 时才载入内存)。

    抛出:
        FileNotFoundError: prompt.md 不见了。
        MissingAPIKeyError: DeepSeek 的 Key 没配。
    """
    return create_gyt_agent(
        name=KNOWLEDGE_AGENT_NAME,
        prompt=load_prompt(KNOWLEDGE_DIR),
        tools=list(KNOWLEDGE_TOOLS),
        purpose="text",
        # 提示词的「必须检索」是概率约束;结构件保证首答不调工具就重试,
        # 重试仍不调时直接如实失败,不让模型凭记忆声称「库里没有」。
        extra_middleware=(_REQUIRE_SEARCH, _REQUIRE_CITATION),
    )
