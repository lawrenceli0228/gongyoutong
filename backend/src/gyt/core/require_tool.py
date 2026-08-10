"""防「嘴上办了、实际没办」的两件结构件。

  RequireToolCall      —— 回合首答必须真调工具(schedule 用)
  RequireReceiptSource —— 报出的凭证编号必须有出处(report 用)

**给哪个 Agent 用哪件,不是口味问题**,选错了两头都不对 —— 见下面「两种判据,别混用」。

===========================================================================
这层解决的是什么(两条泳道各自踩的坑,都有物证)
---------------------------------------------------------------------------
① 台账做假账(schedule,2026-08-08 真机抓获,库为证)

   多轮对话攒了几条自己的回执之后,schedule 开始**不调工具直接补全回执**:

       用户: T1 干完了
       [schedule] 销了:复检三层钢筋(T1)已完成。   ← 没调 finish_task!
       …(库里 T1 仍是 open;后续查询表格里的「已完成」也是编的)

② 巡检记录编号是编的(report,2026-08-10 真机抓获,磁盘为证)

   英雄链 safety──硬边──►report 的硬边只保证 report **被叫起来**,
   不保证它**真调工具**:

       [report] 巡检记录出好了,编号 GYT-20260809-153739。
                ↑ 没调 render_inspection_report,磁盘上没有这份 docx。
       工友拿着这个编号去找管理员,谁也找不到。

两次是同一种病:temperature=0 拦不住 —— 这是 few-shot 自我模仿(上文里全是
现成的回执范例,照着补一条比调工具「顺手」),不是采样噪声;提示词红线也只是
概率性生效(本仓三次栽在「用提示词管住模型行为」上)。所以两条泳道都改用结构件
兜底,这个文件就是那件公共的。

===========================================================================
做法与边界
---------------------------------------------------------------------------
**每个回合首答**(上一条消息是用户发言,或是交接回执)必须带工具调用;
没带就把这次回答打回去、附一句系统校验(nudge)重试一次。
回合中段(上一条是自家工具的结果)当然允许纯文本 —— 那是在念真回执。

重试一次仍然不调工具时,两条泳道要的东西不一样,靠 on_give_up 选:

  · "pass" —— 放行,只记 warning(schedule 用这档)。
    台账动作下一轮查询就会露馅,还有得救,不值得为它牺牲一次回答。
  · "fail" —— 用 give_up_message 顶替模型编的那段话(report 用这档)。
    「文件没出成」工友看得见、能重来;「以为出成了、拿着假编号去找人」
    没有任何补救机会。后者严重得多,所以宁可当场如实说没出成。

⚠️ 最多重试一次,**绝不能自己造第二个循环** —— 熔断归上层 recursion_limit 管。

===========================================================================
两种判据,别混用(2026-08-10 复核抓出来的教训)
---------------------------------------------------------------------------
本模块有两件结构件,守的是同一条红线的两个侧面:

  RequireToolCall      —— 「回合首答必须调工具」。适合 schedule 这种
                          **每个首答都必然对应一个台账动作**的泳道。
  RequireReceiptSource —— 「输出里报出的凭证编号,必须在本回合工具结果里出现过」。
                          适合 report 这种**有合法纯文本回合**的泳道。

给 report 用前者是错的,两头都不对,这两条都是实测:

  ① 一次都不触发。英雄链是写死的图边 START→safety→report(graph.py:183-187),
     report 首答前最后一条是 safety 的纯文本 AIMessage —— 既不是用户发言,
     也不是 transfer_to_* 回执,``_is_turn_start`` 恒为 False。
     而 AGENT_REGISTRY 里根本没有独立的 report,transfer_to_report 这条路不存在。
  ② 一旦触发反而打断合法流程。report/prompt.md 明文要求两种**该纯文本回答**的回合:
     第 23 行「拿不准就问一句『师傅,给哪张出记录?』」、
     第 45 行「有人问日报/周报:如实说日报功能还没上线」。
     按首答判据这两种都会被顶替 —— 反问被吃掉就是对话死锁。

编号溯源之所以精准:``GYT-日期-时刻`` 是**工具**生成的
(agents/report/tools.py:151 的 ``strftime("GYT-%Y%m%d-%H%M%S")``),
模型没有任何合法理由自己造一个。所以「报了编号却没有出处」= 一定在编,
而反问和拒答压根不含编号,零误伤。
===========================================================================
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Final, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

_HANDOFF_TOOL_PREFIX: Final[str] = "transfer_to_"

GiveUpAction = Literal["pass", "fail"]
"""重试之后仍然没调工具时的处置:放行(只记账)还是顶替(不让编的内容出去)。"""

_GIVE_UP_ACTIONS: Final[tuple[str, ...]] = ("pass", "fail")


def _is_turn_start(messages: list[BaseMessage]) -> bool:
    """回合首答判定:上一条是用户发言,或是「Successfully transferred …」交接回执。

    这两种情形下,正确动作永远是先调工具(台账的记/查/改/销、report 的出文件);
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


def _override_ai_text(response: Any, text: str) -> Any:
    """把响应里那条没带工具调用的 AIMessage 换成 text,别的消息原样不动。

    为什么这么改能真的挡住模型编的内容(2026-08-10 读 langchain 1.3.14 源码核对,
    路径 backend/.venv/lib/python3.11/site-packages/langchain/agents/factory.py):

      · 从 handler 手里拿到的一定是 ModelResponse 数据类:多件中间件串起来时,
        内层结果会被 inner_handler 统一 `_normalize_to_model_response`(factory.py:298-309,
        函数定义在 177-190);只有一件时直接透传最内层的 `_execute_model_sync`,
        它本身就 `return ModelResponse(...)`(factory.py:1428)。
        ModelResponse 是 `@dataclass`,字段 result / structured_response
        (middleware/types.py:270-285)—— 所以这里能用 dataclasses.replace 换出新对象。
      · 模型节点最终写进 state 的**就是这个 .result**:
        `state = {"messages": model_response.result}`(factory.py:211)。
        换掉里面的 AIMessage,工友看到的就是我们的话,不是模型编的编号。
      · 没有工具调用时 result 里就一条 AIMessage(factory.py:1270 的
        `return {"messages": [output]}`),所以这里不会出现「同一段话顶替好几遍」。
        仍然写成遍历,是为了不假设列表长度。
      · 用 model_copy 而不是新建 AIMessage:实测它只换 content,
        id / name / usage_metadata / response_metadata 原样留着。留着都有用 ——
        name 是 create_agent 给消息盖的 Agent 名(factory.py:1420-1421 / 1468-1469),
        core/focus.py 的滤网就是按它分辨发言人的;usage_metadata 丢了 token 账就不准;
        id 保持不变,这条才还是「同一条消息被改了内容」,而不是凭空多出来一条。

    顺带一提(2026-08-10 核对 frontend/ 的 useStream):现在的 chat-ui 只订 "values"
    这一路流,不订 token 级的 "messages"。所以模型编的那段话根本没机会先闪到界面上
    再被我们改掉 —— 顶替是干净的,工友只会看见 give_up_message。
    """
    result = list(getattr(response, "result", None) or [])
    patched: list[BaseMessage] = [
        m.model_copy(update={"content": text}) if isinstance(m, AIMessage) else m for m in result
    ]
    return dataclasses.replace(response, result=patched)


def _with_nudge(request: Any, nudge: str) -> Any:
    """追加一条系统校验消息,返回**新的** request。

    ``request.messages = [...]`` 那种直接赋值在 langchain 1.3.14 里已经带废弃告警
    (``ModelRequest.__setattr__`` 的 docstring:middleware/types.py:167-170),
    官方给的路子是 ``override()``(types.py:201,原话「follows an immutable pattern,
    leaving the original request unchanged」)。和本仓「永不就地改」的规矩也对得上。
    退化分支是给替身对象留的:测试里的假 request 没有 override。
    """
    messages = [*(request.messages or []), HumanMessage(content=nudge)]
    override = getattr(request, "override", None)
    if callable(override):
        return override(messages=messages)
    request.messages = messages  # 替身对象:没有 override 就只能就地改
    return request


def _message_text(message: BaseMessage) -> str:
    """把一条消息的正文取成纯字符串。

    content 可能是字符串,也可能是内容块列表(多模态消息就是后者)。
    只取块里的 text 字段 —— 图片块之类的没有可比对的编号。
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return str(content)


class RequireReceiptSource(AgentMiddleware):
    """输出里报出的凭证编号,必须在本回合的工具结果里真的出现过,否则就是编的。

    为什么是这个判据而不是「首答必须调工具」,见模块顶部「两种判据,别混用」。
    一句话:编号由工具生成,模型没有合法理由自己造;而反问、如实拒答这些
    **该纯文本回答**的回合压根不含编号,不会被误伤。

    判定与处置:

        模型输出里没有编号            ──► 放行(反问 / 拒答 / 正在调工具都走这里)
        编号全都能在工具结果里找到      ──► 放行(这是在念真回执)
        报了编号但找不到出处          ──► 附 nudge 重试一次
                                        └─► 还是编 ──► 用 give_up_message 顶替

    ⚠️ 同 RequireToolCall:最多重试一次,绝不自己造第二个循环。

    参数:
        agent_name: 只用于日志前缀。
        pattern: 凭证编号的正则。要能唯一识别「工具生成的凭证」,
                 别用宽到会误伤正常话术的模式。
        nudge: 抓到编造时追加给模型的中文系统校验。
               **别在 nudge 里复述一个假编号样例** —— 本仓记录过的失败机理正是
               few-shot 自我模仿,递一个现成范例等于帮倒忙。
        give_up_message: 重试后仍在编时,顶替模型发言的那句人话。必填。
    """

    def __init__(
        self,
        *,
        agent_name: str,
        pattern: str,
        nudge: str,
        give_up_message: str,
    ) -> None:
        super().__init__()
        if not give_up_message.strip():
            # 构造期就炸,别等到线上真抓到编造那一刻才发现文案没配 ——
            # 那一刻恰好是最不能出错的时刻。
            raise ValueError("give_up_message 不能为空:抓到编造时得有一句人话顶上去")
        self.agent_name = agent_name
        self.pattern = re.compile(pattern)
        self.nudge = nudge
        self.give_up_message = give_up_message

    def _claimed(self, response: Any) -> set[str]:
        """模型这次输出里报出来的编号。"""
        result = getattr(response, "result", None) or []
        found: set[str] = set()
        for m in result:
            if isinstance(m, AIMessage):
                found.update(self.pattern.findall(_message_text(m)))
        return found

    def _sourced(self, messages: list[BaseMessage]) -> set[str]:
        """本回合工具结果里真实出现过的编号 —— 唯一合法的出处。"""
        found: set[str] = set()
        for m in messages:
            if isinstance(m, ToolMessage):
                found.update(self.pattern.findall(_message_text(m)))
        return found

    def _fabricated(self, response: Any, messages: list[BaseMessage]) -> set[str]:
        claimed = self._claimed(response)
        if not claimed:
            return set()
        return claimed - self._sourced(messages)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        response = handler(request)
        messages = list(request.messages or [])
        fake = self._fabricated(response, messages)
        if not fake:
            return response
        logger.warning("%s 报了没出处的编号 %s,已打回重试", self.agent_name, sorted(fake))
        retried = handler(self._retry_request(request))
        if not self._fabricated(retried, messages):
            return retried
        logger.error("%s 重试后仍在编编号,已把它编的内容换成如实说明", self.agent_name)
        return _override_ai_text(retried, self.give_up_message)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        response = await handler(request)
        messages = list(request.messages or [])
        fake = self._fabricated(response, messages)
        if not fake:
            return response
        logger.warning("%s 报了没出处的编号 %s,已打回重试", self.agent_name, sorted(fake))
        retried = await handler(self._retry_request(request))
        if not self._fabricated(retried, messages):
            return retried
        logger.error("%s 重试后仍在编编号,已把它编的内容换成如实说明", self.agent_name)
        return _override_ai_text(retried, self.give_up_message)

    def _retry_request(self, request: Any) -> Any:
        return _with_nudge(request, self.nudge)


class RequireToolCall(AgentMiddleware):
    """回合首答必须带工具调用,否则附系统校验打回重试一次。

    与 FocusOnOwnWork 的组合顺序:Focus 在外层先裁上下文,本件在内层
    看到的已是裁后的消息列表(交接回执被 Focus 保留,首答判定不受影响)。

    参数:
        agent_name: 只用于日志前缀,一眼看出是哪个 Agent 被打回了。
        nudge: 首答没调工具时追加给模型的那条中文「系统校验」。措辞留给各泳道自己写 ——
               台账要点名 add_task/finish_task,出文件的要点名出文件的工具。
        on_give_up: 重试一次之后**仍然**没调工具时怎么办,见模块 docstring 的取舍。
        give_up_message: on_give_up="fail" 时顶替模型原话的文案,必须是工地师傅
                         看得懂的人话(它会原样出现在聊天里)。
    """

    def __init__(
        self,
        *,
        agent_name: str,
        nudge: str,
        on_give_up: GiveUpAction = "pass",
        give_up_message: str = "",
    ) -> None:
        super().__init__()
        # 两条校验都放在构造期(= 建图期,进程一起来就炸):
        # fail 档真触发的那一刻,恰恰是最不能再出岔子的时刻,不能等到那时候
        # 才发现文案没配、或者 on_give_up 拼错导致「以为很严其实在放行」。
        if on_give_up not in _GIVE_UP_ACTIONS:
            raise ValueError(f"on_give_up 只能是 {_GIVE_UP_ACTIONS} 之一,收到的是 {on_give_up!r}。")
        if on_give_up == "fail" and not give_up_message.strip():
            raise ValueError(
                f"Agent「{agent_name}」把 on_give_up 设成了 fail,却没给 give_up_message。"
                "顶替文案是要直接说给工友听的,不能是空的。"
            )
        self.agent_name = agent_name
        self.nudge = nudge
        self.on_give_up: GiveUpAction = on_give_up
        self.give_up_message = give_up_message

    def _retry_request(self, request: Any) -> Any:
        return _with_nudge(request, self.nudge)

    def _give_up(self, response: Any) -> Any:
        """重试后仍不调工具:pass 档只记账放行,fail 档换掉模型编的那段话。"""
        if self.on_give_up == "pass":
            # warning:一次可容忍的降级 —— 后面还有露馅和补救的机会。
            logger.warning("%s 重试后仍未调工具,放行交由熔断兜底", self.agent_name)
            return response
        # error:一次本来会变成假账/假编号的事故,已经被顶替下来了,值得单独捞出来看。
        logger.error("%s 重试后仍未调工具,已把它编的内容换成如实说明", self.agent_name)
        return _override_ai_text(response, self.give_up_message)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        response = handler(request)
        if _is_turn_start(list(request.messages or [])) and _lacks_tool_call(response):
            logger.warning("%s 首答未调工具,已打回重试", self.agent_name)
            response = handler(self._retry_request(request))
            if _lacks_tool_call(response):
                response = self._give_up(response)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        response = await handler(request)
        if _is_turn_start(list(request.messages or [])) and _lacks_tool_call(response):
            logger.warning("%s 首答未调工具,已打回重试", self.agent_name)
            response = await handler(self._retry_request(request))
            if _lacks_tool_call(response):
                response = self._give_up(response)
        return response


__all__ = ["GiveUpAction", "RequireReceiptSource", "RequireToolCall"]
