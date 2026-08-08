"""CAD Agent —— 看 DXF 图纸(解析 / 索引 / 查询 / PNG 预览)。

本体走 **text 档**(DeepSeek):看图纸的活是读解析出来的结构化索引 + 出 PNG,
不需要视觉模型。GBK 中文、索引落盘、方案 B 预注册,细节见各子模块 docstring
与 .personal/CAD_Agent_落地文档.md。

节点名 = 交接工具名(transfer_to_cad)= chat-ui 轨迹显示名 = routing.csv 的
expected_agent 取值 = scorers.ROUTING_AGENTS 里的 "cad" —— 五处一字不差,别改。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.cad.tools import CAD_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt

CAD_AGENT_NAME = "cad"

CAD_DIR = Path(__file__).parent

__all__ = ["CAD_AGENT_NAME", "CAD_DIR", "build_cad_agent"]


def build_cad_agent() -> CompiledStateGraph:
    """组装 cad Agent(已编译,可直接挂给 Supervisor)。

    返回:
        已 compile 的 CompiledStateGraph,.name 为 "cad"。

    抛出:
        FileNotFoundError: prompt.md 不见了。
        MissingAPIKeyError: DeepSeek 的 Key 没配(本体走 text 档)。
    """
    return create_gyt_agent(
        name=CAD_AGENT_NAME,
        prompt=load_prompt(CAD_DIR),
        tools=list(CAD_TOOLS),
        purpose="text",
    )
