# 工友通 AI Agent（MVP）核心 Agent 设计

为了保证项目能够在比赛周期内完成，同时充分展示多智能体（Multi-Agent）协作能力，建议不要一次开发全部子
Agent，而是优先实现以下 **6 个核心 Agent**：

## 1. Supervisor Agent（任务调度）

负责理解用户请求，并协调各子 Agent 完成任务，是整个系统的调度中心。

## 2. Safety Agent（安全巡检）

负责施工现场安全检查，支持图片、多模态分析，识别安全帽、反光衣、高空作业、消防隐患等风险。

## 3. Knowledge Agent（RAG 问答）

基于企业知识库和施工规范，回答施工、安全、制度等相关问题，并提供可追溯的依据。

## 4. Schedule Agent（任务与进度）

负责施工任务管理、进度查询、施工计划安排及任务提醒。

## 5. Report Agent（日报/巡检报告）

根据现场信息、巡检结果及施工进度，自动生成施工日报、巡检报告和整改建议，可导出
PDF 或 Word。

## 6. CAD Agent（图纸查询）

支持 CAD
图纸解析与查询，实现图纸信息检索、尺寸查询、构件定位等功能，作为项目的重要亮点。

------------------------------------------------------------------------

# MVP 目标

该版本能够完整体现以下 AI Agent 核心能力：

-   Multi-Agent 协同调度（Supervisor + Sub Agents）
-   多模态能力（图片、文档、CAD）
-   RAG 知识库问答
-   Tool Calling（调用 CAD 解析、报告生成等工具）
-   自动化工作流（Workflow）
-   完整的 Web Demo 与可演示的 MVP

## 后续可扩展 Agent

-   Material Agent（材料管理）
-   Quality Agent（质量检测）
-   Risk Agent（风险预警）
-   Attendance Agent（考勤管理）
-   Procurement Agent（采购管理）
-   Weather Agent（天气预警）
-   Voice Agent（语音助手）

## 开发策略

采用"**先完成可运行
MVP，再逐步扩展能力**"的开发策略，确保项目稳定交付，同时保留较强的后续扩展性。
