"""从 LangGraph 运行配置里取「当前工地」—— 前端选中的项目经 config.configurable 注入。

knowledge(规范检索)与 cad(图纸)工具**共用同一个键**:前端一次注入,两条线都按同一个
「当前工地」收敛作用域。键名是前后端契约的一部分,定在这一处,别在各工具里各写各的字符串
(那样迟早漂移成「规范认得项目、图纸认不得」)。

用法(工具里):声明一个注入参 ``*, config: RunnableConfig``(精确注解,LLM 看不到),
再 ``project_from_config(config)`` 取当前工地。config 由 LangGraph 一路透传到子图工具。
⚠️ 注解必须**恰好**是 ``RunnableConfig``,不能写 ``RunnableConfig | None`` —— langchain 按
``param.annotation is RunnableConfig`` 判定要不要注入,写成联合类型会漏掉,config 永远拿不到。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

# 前端 submit 时放进 config.configurable[这个键];knowledge / cad 都读它做作用域兜底。
PROJECT_CONFIG_KEY = "gyt_project_id"


def project_from_config(config: RunnableConfig | None) -> str:
    """取前端选中的「当前工地」项目编号;没有 / 空则返回空串。"""
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get(PROJECT_CONFIG_KEY) or "").strip()


__all__ = ["PROJECT_CONFIG_KEY", "project_from_config"]
