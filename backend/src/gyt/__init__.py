"""工友通(GYT)—— 建筑工地多智能体助手后端包。

模块地图:
- ``gyt.config``            全局配置与常量的唯一入口(``get_settings()``)
- ``gyt.core.errors``       统一错误信封(``ok`` / ``fail`` / ``tool_guard``)
- ``gyt.core.artifacts``    文件引用注册表(状态里只传 artifact_id,不传大对象)
- ``gyt.core.llm``          DeepSeek / Moonshot 双供应商客户端 + 重试 + 缓存
- ``gyt.core.base_agent``   Agent 薄工厂(读提示词 + 选模型 + 组装)
- ``gyt.graph``             Supervisor 图,导出已编译的 ``graph``

这里刻意不做任何再导出,保持包导入零副作用,也避免并行开发时出现循环导入。
用配置请写 ``from gyt.config import get_settings``。
"""

__version__ = "0.1.0"
