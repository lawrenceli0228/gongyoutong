"""GYT 核心基础设施层:被所有 Agent 复用的公共能力。

- ``errors``      统一错误信封与 ``tool_guard`` 装饰器
- ``artifacts``   文件引用注册表(register / resolve / read_meta)
- ``llm``         双供应商聊天模型工厂、指数退避重试、响应磁盘缓存
- ``base_agent``  Agent 薄工厂
- ``doc_no``      文书编号生成器(六种监理文书 + 巡检记录,全仓唯一拼编号处)
- ``sqlite_util`` db 层的连接/事务/外键样板与本地时间戳(四个 ``db/*.py`` 共用,W9 S8)

这一层只依赖 ``gyt.config`` 和第三方库,不反向依赖任何具体 Agent。

⚠️ **唯一的例外是 ``doc_no`` → ``gyt.attendance.receipt``**(2026-08-16 W9 加)。
破这条规矩是有意的:``receipt.py`` 是 CLAUDE.md 同源清单点名的**唯一时间权威**
(``HK = ZoneInfo("Asia/Hong_Kong")``),把 ``HK`` 抄进 core 就等于制造第二个
时间真相源。而这条 import 在模块图上不构成环 —— ``receipt.py`` 一个 gyt 模块
都不 import(只用标准库),``attendance/__init__.py`` 是纯 docstring,
也不会捎带拉起同包 ``watermark.py`` 的 PIL。
详见 ``doc_no.py`` 顶部「关于 core → attendance 这条 import」。
"""
