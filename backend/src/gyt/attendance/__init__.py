"""打卡(考勤)链的非 Agent 部分:时间权威、水印、清理、文案。

这个包与 ``gyt/agents/attendance``(查询 Agent)是**两层**,依赖方向单向:

    agents/attendance(LLM 查询) → attendance(本包,无 LLM) → core

打卡的写入路径(checkin_api → 本包 → db/attendance)一个 LLM 都不经过,
所以这里的模块**不许 import agents 下的任何东西** —— 反着依赖,三个与 Agent
无关的模块就得为了一句文案背上整个 Agent 栈。同型约定见 core/focus.py 的
SUPERVISOR_NAME(core 层不许反向 import 编排层,靠注释约束)。
"""
