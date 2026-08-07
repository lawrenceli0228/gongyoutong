"""report —— 巡检记录生成 Agent(英雄链 safety → report 的第二环)。

W3 当前形态:只做「单张照片 → 巡检记录 docx」。日报/周报需要隐患台账
(多条记录的存储与聚合),是赛后项 —— prompt.md 里已明确不许诺。

数据保真设计(为什么它"只搬编号不搬内容")见 tools.py 顶部。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.report.tools import REPORT_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt

# 节点名 = 交接工具名 = 路由评测里 expected_agent 的取值之一(scorers.ROUTING_AGENTS)。
REPORT_AGENT_NAME = "report"

REPORT_DIR = Path(__file__).parent

__all__ = ["REPORT_AGENT_NAME", "REPORT_DIR", "build_report_agent"]


def build_report_agent() -> CompiledStateGraph:
    """组装 report Agent(已编译)。文档渲染是本地纯计算,本体走便宜的 text 档。"""
    return create_gyt_agent(
        name=REPORT_AGENT_NAME,
        prompt=load_prompt(REPORT_DIR),
        tools=list(REPORT_TOOLS),
        purpose="text",
    )
