"""ping —— T1 骨架阶段的连通性占位 Agent。

它的全部价值是当一根「通电指示灯」:
只要 Supervisor 能把请求路由给它、它能调到 echo 工具、工具能把信封原路传回来,
就说明多智能体骨架是活的,W2 可以放心往上挂真业务 Agent。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.ping.tools import PING_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt

# 节点名 = 交接工具名(transfer_to_ping)= chat-ui 轨迹上显示的名字。
# 改这个字符串等于改对外 API,测试和演示脚本都会跟着挂,谨慎。
PING_AGENT_NAME = "ping"

# 本包所在目录,prompt.md 就在旁边。用 __file__ 推导而不是拼相对路径,
# 保证从任何工作目录启动(uv run / docker / pytest)都能找到。
PING_DIR = Path(__file__).parent

__all__ = ["PING_AGENT_NAME", "PING_DIR", "build_ping_agent"]


def build_ping_agent() -> CompiledStateGraph:
    """组装 ping Agent(已编译,可直接挂给 Supervisor)。

    返回:
        已 compile 的 CompiledStateGraph,.name 为 "ping"。
    """
    return create_gyt_agent(
        name=PING_AGENT_NAME,
        prompt=load_prompt(PING_DIR),
        # 传副本,别把模块级常量交出去。
        tools=list(PING_TOOLS),
        # ping 只做文本回显,走便宜的 DeepSeek 档。
        purpose="text",
    )
