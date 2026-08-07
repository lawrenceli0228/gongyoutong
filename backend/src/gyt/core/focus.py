"""让子 Agent 只看见「用户要什么」和「自己做过什么」,看不见别人的闲聊。

===========================================================================
这层解决的是什么(真实故障,复现过多次)
---------------------------------------------------------------------------
langgraph_supervisor 让 Supervisor 与所有子 Agent **共享同一份 messages**。
于是子 Agent 被派活时,能看到 Supervisor 之前说过的每一句话,包括:

    [supervisor] 我这就安排 safety 同事看一下,稍等。

而 safety 读到这句,就跟着演起了调度者的角色:

    [safety] 师傅,照片我这就安排 safety 同事看,稍等。
             (照片编号 ... 已转给负责安全检查的同事,他会用工具查看)
    [safety] → transfer_back_to_supervisor

**它没调工具,把活推回了 Supervisor,而 Supervisor 又在等它的结果** ——
两边互相等,用户永远等不到答复,而且界面上看不出任何错误。

这是 few-shot 污染,不是随机性:
  · temperature=0 之后照样复发(试过);
  · 把提示词写成「你就是那个同事,别推给别人」也照样复发(也试过)——
    历史里那句现成的示范,比提示词里的告诫更有说服力。

**单轮对话不容易碰到,多轮才稳定复现** —— 因为历史越长,可模仿的示范越多。
而演示现场必然是多轮的。

===========================================================================
做法与边界
---------------------------------------------------------------------------
只在**喂给模型之前**把「别的 Agent 说的纯文本」滤掉,不改 state ——
Supervisor 汇总时仍然拿得到子 Agent 的完整输出,chat-ui 上的执行轨迹也照常显示。

⚠️ 只滤**没有 tool_calls 的** AIMessage。带 tool_calls 的必须留下,
因为紧跟其后的 ToolMessage 要靠它的 tool_call_id 配对 —— 把它滤掉会留下
一条孤立的 tool 消息,上游直接 400。这是实现这类裁剪最容易踩的坑。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage

logger = logging.getLogger(__name__)


def keep_focused(messages: list[BaseMessage], own_name: str) -> list[BaseMessage]:
    """滤掉「别的 Agent 说的纯文本」,其余原样保留。

    保留:
      · 用户消息(它才是任务的来源)
      · 工具结果
      · 自己说过的话
      · **任何带 tool_calls 的 AIMessage**(哪怕是别人的)—— 见文件顶部的配对说明

    丢掉:
      · 其它 Agent(含 Supervisor)的纯文本发言 —— 那是给用户看的转述,
        对子 Agent 干活没有信息量,却会被它当成"该怎么说话"的示范。
    """
    kept: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, AIMessage) and not message.tool_calls:
            name = getattr(message, "name", None)
            if name is not None and name != own_name:
                continue
        kept.append(message)
    return kept


class FocusOnOwnWork(AgentMiddleware):
    """把 keep_focused 挂到子 Agent 的模型调用上。

    每个子 Agent 一个实例(要知道自己叫什么才能分辨"哪些是自己说的")。
    """

    def __init__(self, own_name: str) -> None:
        super().__init__()
        self.own_name = own_name

    def _focus(self, request: Any) -> Any:
        original = list(request.messages or [])
        focused = keep_focused(original, self.own_name)
        if len(focused) != len(original):
            logger.debug(
                "%s:从上下文里滤掉了 %d 条其它 Agent 的发言",
                self.own_name,
                len(original) - len(focused),
            )
            request.messages = focused
        return request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._focus(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._focus(request))


__all__ = ["FocusOnOwnWork", "keep_focused"]
