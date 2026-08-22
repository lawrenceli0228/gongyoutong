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

from gyt.agents.safety.guard import RequireVisionTool
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
        # 🔴 防假账守卫(2026-08-22 补)。在它之前,safety 是四个动作型 Agent 里
        # **唯一裸奔**的那个 —— 而它是头号动作。
        # 线上实测抓到的病状:连传四张照片,后两张 analyze_site_photo **一次都没被调**,
        # 而模型把 prompt.md 里那段示范句原样背了出来(两张不同的照片、逐字相同的措辞),
        # 还补了一句「这张我登记成待确认隐患了」—— 而台账里没有、失败清单里也没有。
        # 判据与两处「跟别家不一样」的选择(为什么用首答判据、为什么 on_give_up=pass)
        # 全在 guard.py 的模块 docstring 里,改之前先读那一段。
        extra_middleware=(RequireVisionTool(),),
    )
