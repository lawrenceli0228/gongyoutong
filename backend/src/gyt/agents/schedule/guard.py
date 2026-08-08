"""RequireLedgerTool —— 台账动作必须先过工具,拦「凭记忆做假账」。

===========================================================================
这层解决的是什么(2026-08-08 真机抓获,库为证)
---------------------------------------------------------------------------
多轮对话攒了几条自己的回执之后,schedule 开始**不调工具直接补全回执**:

    用户: T1 干完了
    [schedule] 销了:复检三层钢筋(T1)已完成。   ← 没调 finish_task!
    …(库里 T1 仍是 open;后续查询表格里的「已完成」也是编的)

temperature=0 拦不住 —— 这是 few-shot 自我模仿(上文里全是自己的回执范例,
补一条比调工具"顺手"),不是采样噪声。提示词红线也只是概率性生效。
嘴上销了库里没销,台账就废了,所以这里用结构件兜底。

做法:台账 Agent 的**每个回合首答**(上一条消息是用户发言或交接回执)
必须带工具调用;没带就把回答打回去、附一句系统校验重试一次。
回合中段(上一条是自家工具的结果)当然允许纯文本 —— 那是在念真回执。
重试仍不带工具就放行并记 warning:上层 recursion_limit 熔断兜底,
这里绝不能自己造第二个循环。
===========================================================================
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

_NUDGE = (
    "(系统校验)你刚才没有调用任何工具就想直接回答。台账问题必须先调工具拿真实数据:"
    "记任务用 add_task、查清单用 list_tasks、改期用 reschedule_task、销项用 finish_task。"
    "凭记忆或上文猜测回答等于做假账。现在重新处理:先调对应的工具。"
)

_HANDOFF_TOOL_PREFIX = "transfer_to_"


def _is_turn_start(messages: list[BaseMessage]) -> bool:
    """回合首答判定:上一条是用户发言,或是「Successfully transferred …」交接回执。

    这两种情形下,台账的正确动作永远是先调工具(记/查/改/销总有一款对应);
    上一条若是自家工具的 ToolMessage,则模型正在把工具结果念成人话,纯文本合法。
    """
    if not messages:
        return False
    last = messages[-1]
    if isinstance(last, HumanMessage):
        return True
    return isinstance(last, ToolMessage) and str(getattr(last, "name", "") or "").startswith(
        _HANDOFF_TOOL_PREFIX
    )


def _lacks_tool_call(response: Any) -> bool:
    result = getattr(response, "result", None) or []
    return not any(isinstance(m, AIMessage) and m.tool_calls for m in result)


class RequireLedgerTool(AgentMiddleware):
    """回合首答必须带工具调用,否则附系统校验打回重试一次。

    与 FocusOnOwnWork 的组合顺序:Focus 在外层先裁上下文,本件在内层
    看到的已是裁后的消息列表(交接回执被 Focus 保留,首答判定不受影响)。
    """

    def _retry_request(self, request: Any) -> Any:
        request.messages = [*(request.messages or []), HumanMessage(content=_NUDGE)]
        return request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        response = handler(request)
        if _is_turn_start(list(request.messages or [])) and _lacks_tool_call(response):
            logger.warning("schedule 首答未调工具,已打回重试(防假账)")
            response = handler(self._retry_request(request))
            if _lacks_tool_call(response):
                logger.warning("schedule 重试后仍未调工具,放行交由熔断兜底")
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        response = await handler(request)
        if _is_turn_start(list(request.messages or [])) and _lacks_tool_call(response):
            logger.warning("schedule 首答未调工具,已打回重试(防假账)")
            response = await handler(self._retry_request(request))
            if _lacks_tool_call(response):
                logger.warning("schedule 重试后仍未调工具,放行交由熔断兜底")
        return response


__all__ = ["RequireLedgerTool"]
