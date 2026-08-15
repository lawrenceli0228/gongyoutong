"""supervision —— 监理业务闭环(W9)。

本包目前只放**纯逻辑件**,Agent 本体与工具随后续泳道落地(见
``docs/W9_监理业务闭环_方案.md`` §11 的 S4/S5):

    grading.py   severity(safety 四档) → grade(监理二档)的确定性映射 + 规则版本号
    docgen.py    八种文书共用的 docx 骨架(标题 + 元信息表 + N 个节 + 统一免责句)

⚠️ **状态机不在这里。** ``ALLOWED_TRANSITIONS`` 与状态迁移函数一律放
``gyt.db.hazards``。理由是**依赖方向**:``agents/*/tools.py`` import ``gyt.db``,
方向严格是 agents → db。把状态真相放这里会让 db 反向依赖 agents,既有循环导入
风险,也让存储层不能独立使用。(2026-08-16 Codex 复审第 22 条,方案 §4.2 有原文。)

这条方向约束**不等于**「db 层什么都不许 import」。截至 2026-08-16,
``db/*.py`` 除 ``gyt.config`` 外还 import ``gyt.attendance.receipt``
(``hazards.py`` 取香港时间快照)—— 那是 CLAUDE.md 点名的**唯一时间权威**,
且 receipt 是纯标准库叶子模块、不构成环。要守的是「db 不依赖 agents」,
不是「db 谁都不依赖」;`core/doc_no.py` 破同一条规矩的理由与它一致,
两处头注互指。
"""
