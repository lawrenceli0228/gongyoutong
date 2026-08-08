"""schedule —— 工地任务台账 Agent(记任务、查任务、改期限、销项)。

本体走 **text 档**(DeepSeek):台账是纯文字活,没有任何视觉调用。
中文相对日期(「下周三」「月底」)**不由模型换算** —— 词表与歧义约定的唯一
真相源在 ``dates.py`` 的模块 docstring,行为规则在 ``prompt.md`` 的日期红线,
两边措辞同源;工具返回的 ``due_display`` 是模型唯一许可的日期说法来源。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.schedule.tools import SCHEDULE_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt

# 节点名 = 交接工具名(transfer_to_schedule)= chat-ui 轨迹上显示的名字。
# 它同时是 eval/datasets/routing.csv 里 expected_agent 的取值之一,
# 也在 scorers.ROUTING_AGENTS 里 —— 改这个字符串等于同时改路由评测集,谨慎。
SCHEDULE_AGENT_NAME = "schedule"

SCHEDULE_DIR = Path(__file__).parent

__all__ = ["SCHEDULE_AGENT_NAME", "SCHEDULE_DIR", "build_schedule_agent"]


def build_schedule_agent() -> CompiledStateGraph:
    """组装 schedule Agent(已编译,可直接挂给 Supervisor)。

    返回:
        已 compile 的 CompiledStateGraph,.name 为 "schedule"。

    抛出:
        FileNotFoundError: prompt.md 不见了。
        MissingAPIKeyError: DeepSeek 的 Key 没配(台账只用 text 档)。
    """
    return create_gyt_agent(
        name=SCHEDULE_AGENT_NAME,
        prompt=load_prompt(SCHEDULE_DIR),
        tools=list(SCHEDULE_TOOLS),
        # 纯文字台账,走便宜的 DeepSeek;这个包里不存在视觉调用。
        purpose="text",
    )
