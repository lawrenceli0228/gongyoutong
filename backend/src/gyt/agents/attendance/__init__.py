"""attendance —— 班组考勤查询 Agent(**只读**:数出勤天数、看某天打卡明细)。

D15 定死:打卡**写入**走直连 HTTP 接口(checkin_api.py),一个 LLM 都不经过;
本 Agent 只管「查」这一半(为什么查询要独立成 Agent,见 W7 方案 §0 的 D16 推演)。
所以这个包里没有任何写库的工具 —— 这是设计,不是没写完;有人想打卡,
prompt.md 会让它引导去点界面上的打卡按钮,严禁口头答应「帮你打上了」。

挂进图的那一行在 graph.AGENT_REGISTRY(汇合阶段由负责人加),本包**不 import graph**:
编排层依赖 Agent 包,不许反向(与 core/focus.py 那条 SUPERVISOR_NAME 同款约定)。

本体走 **text 档**(DeepSeek):考勤查询是纯文字活,没有任何视觉调用。
中文时间短语(「这个月」「上周三」)**不由模型换算** —— 词表与方向约定的唯一
真相源在 ``ranges.py`` 的模块 docstring(与 schedule/dates.py 同口径、反方向,
两边头注互相指认),行为规则在 ``prompt.md`` 的日期红线;工具返回的
``range_display`` / ``day_display`` 是模型唯一许可的日期说法来源。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.attendance.guard import RequireAttendanceTool
from gyt.agents.attendance.tools import ATTENDANCE_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt

# 节点名 = 交接工具名(transfer_to_attendance)= chat-ui 轨迹上显示的名字。
# 它同时是 eval/datasets/routing.csv 里 expected_agent 的取值之一,
# 也要进 scorers.ROUTING_AGENTS(汇合阶段)—— 改这个字符串等于改对外 API,谨慎。
ATTENDANCE_AGENT_NAME = "attendance"

ATTENDANCE_DIR = Path(__file__).parent

__all__ = ["ATTENDANCE_AGENT_NAME", "ATTENDANCE_DIR", "build_attendance_agent"]


def build_attendance_agent() -> CompiledStateGraph:
    """组装 attendance Agent(已编译,可直接挂给 Supervisor)。

    返回:
        已 compile 的 CompiledStateGraph,.name 为 "attendance"。

    抛出:
        FileNotFoundError: prompt.md 不见了。
        MissingAPIKeyError: DeepSeek 的 Key 没配(考勤查询只用 text 档)。
    """
    return create_gyt_agent(
        name=ATTENDANCE_AGENT_NAME,
        prompt=load_prompt(ATTENDANCE_DIR),
        tools=list(ATTENDANCE_TOOLS),
        # 纯文字查询,走便宜的 DeepSeek;这个包里不存在视觉调用。
        purpose="text",
        # 防编数结构件:回合首答必须带工具调用,否则打回重试(重试仍不调则放行)。
        # 判据选择与 give_up 取舍的完整理由在 guard.py 顶部,别只看这一行就照抄给别的泳道。
        extra_middleware=(RequireAttendanceTool(),),
    )
