"""GYT 核心基础设施层:被所有 Agent 复用的公共能力。

- ``errors``      统一错误信封与 ``tool_guard`` 装饰器
- ``artifacts``   文件引用注册表(register / resolve / read_meta)
- ``llm``         双供应商聊天模型工厂、指数退避重试、响应磁盘缓存
- ``base_agent``  Agent 薄工厂

这一层只依赖 ``gyt.config`` 和第三方库,不反向依赖任何具体 Agent。
"""
