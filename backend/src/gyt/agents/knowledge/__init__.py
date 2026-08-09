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

KNOWLEDGE_AGENT_NAME = "knowledge"

KNOWLEDGE_DIR = Path(__file__).parent

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
    )
