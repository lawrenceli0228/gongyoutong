"""supervision —— 监理业务闭环(W9)。

本包目前只放**纯逻辑件**,Agent 本体与工具随后续泳道落地(见
``docs/W9_监理业务闭环_方案.md`` §11 的 S4/S5):

    grading.py   severity(safety 四档) → grade(监理二档)的确定性映射 + 规则版本号
    docgen.py    八种文书共用的 docx 骨架(标题 + 元信息表 + N 个节 + 统一免责句)

⚠️ **状态机不在这里。** ``ALLOWED_TRANSITIONS`` 与状态迁移函数一律放
``gyt.db.hazards``。理由是依赖方向:全仓 ``db/*.py`` 只 import ``gyt.config``,
而 ``agents/*/tools.py`` import ``gyt.db`` —— 方向严格是 agents → db。
把状态真相放这里会让 db 反向依赖 agents,既有循环导入风险,也让存储层不能独立使用。
(2026-08-16 Codex 复审第 22 条,方案 §4.2 有原文。)
"""
