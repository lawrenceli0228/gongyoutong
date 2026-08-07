"""safety —— 工地照片安全识图 Agent(演示主线「巡检英雄链」的第一环)。

本体走 **text 档**(DeepSeek),不是 vision 档 —— 看图那一步在工具
``analyze_site_photo`` 内部直调 Kimi,理由(省 token / 中文错误 / 贵模型只调一次)
写在 ``tools.py`` 顶部,改架构之前先读那一段。

两份提示词:
    prompt.md         本体用。管对话、管什么时候调工具、管把结果讲成人话。
    vision_prompt.md  工具内部那次视觉调用用。管受控词表与输出格式。
受控词表只在 vision_prompt.md 里出现一次,别在别处再抄一份。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.safety.tools import SAFETY_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt

# 节点名 = 交接工具名(transfer_to_safety)= chat-ui 轨迹上显示的名字。
# 它同时是 eval/datasets/routing.csv 里 expected_agent 的取值之一,
# 也在 scorers.ROUTING_AGENTS 里 —— 改这个字符串等于同时改路由评测集,谨慎。
SAFETY_AGENT_NAME = "safety"

SAFETY_DIR = Path(__file__).parent

__all__ = ["SAFETY_AGENT_NAME", "SAFETY_DIR", "build_safety_agent"]


def build_safety_agent() -> CompiledStateGraph:
    """组装 safety Agent(已编译,可直接挂给 Supervisor)。

    返回:
        已 compile 的 CompiledStateGraph,.name 为 "safety"。

    抛出:
        FileNotFoundError: prompt.md 不见了。
        MissingAPIKeyError: DeepSeek 的 Key 没配(本体用 text 档)。
                            注意 Kimi 的 Key 要到**真的调工具**时才检查 ——
                            建图阶段不碰 vision 档,所以只配了 DeepSeek 也能起图。
    """
    return create_gyt_agent(
        name=SAFETY_AGENT_NAME,
        prompt=load_prompt(SAFETY_DIR),
        tools=list(SAFETY_TOOLS),
        # 本体只做对话与派工具,走便宜的 DeepSeek。视觉在工具里。
        purpose="text",
    )
