"""模型调用与工具执行的耗时观测 —— 让「界面卡了三分钟」这种事下次自己说话。

===========================================================================
为什么要这件(2026-08-20,一次花了两小时的日志考古之后)
---------------------------------------------------------------------------
线上一次「拍照 → 出巡检记录」量到 213 秒。日志里能看到的只有 httpx 那几行
「HTTP Request: POST ... 200 OK」,于是只能靠**相邻两行的时间差**倒推:

    02:04:02  moonshot 200 OK        ← 识图完成
         ↓
         ↓  198 秒,**中间一条日志都没有**
         ↓
    02:07:20  deepseek 200 OK        ← safety 组织回答

倒推不出的恰恰是最要紧的一件事:这 198 秒是**在那次 deepseek 调用里面**,
还是**在发出请求之前**(工具后处理、隐患入库、中间件、图调度)?
两者的修法完全不同,而 httpx 的日志只在请求发出后才打印,分不开。

当时排除了一圈(图片 base64 没混进请求:state 只有 5.6KB;CPU 空闲;
DeepSeek 本身 5.8~7.2 秒;流式最慢也才 16.4 秒;隐患入库 1.0 秒),
仍然定不了位 —— **因为没有任何东西在量这一段**。

所以这个模块只干一件事:**把耗时这个数本身变成判据**。

    模型调用记了 198 秒  → 那 198 秒在调用里面,查模型侧 / 网络
    模型调用记了 2 秒    → 那 198 秒在调用之前,查工具与图调度
                            (而工具那一侧由 tool_guard 的计时接上)

两件合起来把那段黑箱**完全切开**,不用再猜。

===========================================================================
为什么用 callback 而不是包一层模型代理
---------------------------------------------------------------------------
🔴 CLAUDE.md 有明令:**不要在 `base_agent.py` 里再包一层模型代理** ——
   那会把工具集这一维从缓存键里弄丢。

callback 不碰模型的构造参数,所以不动缓存键。**这一条是实测过的,不是推理**
(2026-08-20,同一台机上):

    ChatOpenAI(..., callbacks=[H()])._get_llm_string()
      == ChatOpenAI(...)._get_llm_string()        → True

⚠️ 量的时候要走**真实的 get_chat_model 路径**,别拿一个简化参数的 ChatOpenAI 量:
   键里含 timeout / max_retries / extra_body / 工具集,参数少一项键就短一截。
   实测键长(2026-08-20,真实路径):text 档 491、vision 档 410,带不带 callbacks
   **逐字节相同**,且 "callback" 这个词根本不出现在键里。
   —— 这段本来写的是「键长都是 355」,那是拿简化参数量出来的数,**已作废**。
   结论(相等)当时就是对的,但留档的数错了;本仓对过期数字的容忍度是零,别再照抄。

必须实测的理由:缓存键一旦变了,**整份视觉缓存作废** —— 那是演示前要焐
22 分钟的东西(TODO-11 的演示铁律)。这种代价不能靠"我记得应该不影响"。

===========================================================================
挂在哪
---------------------------------------------------------------------------
    core/llm.py  get_chat_model()   ← 路径甲(Agent / Supervisor)唯一必经处
    core/llm.py  ainvoke()          ← 路径乙(识图、评测打分等直调)
    core/errors.py  tool_guard()    ← 所有工具的唯一必经处

三处都是"谁都绕不过"的收口,不用让每个 Agent 各自记得加。

===========================================================================
为什么同一份数还要往前端推一遍(2026-08-20,同一天)
---------------------------------------------------------------------------
上面记的这些**只有能翻日志的人看得见**。工友举着手机在工地上等三分钟,界面上
从头到尾只有一句「正在忙」—— 他分不出是在等识图、在等台账,还是已经卡死了。
于是「慢」这件事对他永远是黑箱,而对我只是一次 grep。

所以同一份数再走一条 **custom 事件**通道推给界面(界面上已有的「隱藏中間步驟」
开关就是这类信息的天然位置)。

🔴 **为什么是 custom 事件、而不是挂在工具返回上**:supervisor 的
   ``output_mode="last_message"`` 会把子 Agent 的工具返回整个丢掉 —— 那正是
   巡检记录卡至今一张都没渲染出来的原因(CLAUDE.md 的「监理常驻操作台」一节 /
   TODO-47)。custom 事件走的是 ``stream_mode="custom"`` 那条**独立通道**,
   不经过 supervisor 的消息裁剪,所以不受它影响。前端也已经在订阅了 ——
   线上日志实测 ``stream_mode=['values', 'messages-tuple', 'custom']``。

事件契约(**前端已按这个写,字段名不许改**,同 CLAUDE.md 对 Agent name 的那条):

    {"gyt_timing": {kind, name, seconds,
                    input_tokens, output_tokens, reasoning_tokens,
                    slow, ok}}

逐字段的含义、以及「什么时候是 None」写在 ``emit_timing`` 的 docstring 里。

🔴 **推送绝不许把主路径打断**,这条比上面任何一条都硬:``get_stream_writer()``
   在 LangGraph 运行时**之外**会直接抛(实测 RuntimeError「Called get_config
   outside of a runnable context」),而评测、单测、CLI、清理脚本全都不在图内 ——
   那是**正常情况,不是故障**。所以拿不到 writer 一律安静走开,连日志都不记。
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

_MAX_TRACKED = 256
"""同时在飞的调用数上限,防字典泄漏。

正常情况下 start / end 成对出现,字典是空的。但 callback 不保证一定成对
(进程被打断、异常路径没走到 on_llm_error),所以留一道硬闸:超了就丢最早的。
丢掉的后果只是"那一条没记上耗时",不影响业务 —— 观测件绝不许把主路径拖垮。
"""


def _usage(response: LLMResult) -> Any:
    """把 ``llm_output`` 里 token 用量那一段挖出来,取不到给空字典。

    单拎出来是因为**两个调用方要用同一种取法**(日志那句 ``_fmt_tokens``、
    推事件那份 ``_token_counts``)。抄成两份的下场是哪天上游把键从
    ``token_usage`` 改成 ``usage``,日志里有数而界面上是 None —— 两边对不上,
    而人会先怀疑前端。
    """
    out = response.llm_output or {}
    return out.get("token_usage") or out.get("usage") or {}


def _fmt_tokens(response: LLMResult) -> str:
    """把 token 用量整成一小段人话。

    命中缓存时 langchain 照样会触发 callback,但 ``llm_output`` 里没有用量 ——
    所以"没有用量"基本等于"这次没真的问模型"。写成「无用量」而不是直接断言
    "命中缓存":那是推断不是事实,别把推断写成事实。
    """
    usage = _usage(response)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return "无用量(多半是命中缓存)"
    return f"输入 {prompt} / 输出 {completion} tok"


def _as_int(value: Any) -> int | None:
    """只认整数,别的一律 None。

    🔴 **取不到就是 None,不许退成 0。** 界面上 0 会被读成"这次没花 token",
       而真相是"不知道"—— 把不知道写成 0 就是编数据。``bool`` 也挡掉:它是
       ``int`` 的子类,``True`` 会悄悄变成 1 个 token。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _token_counts(response: LLMResult) -> tuple[int | None, int | None, int | None]:
    """取(输入, 输出, 其中思考)三个数。任何一项取不到都给 None。

    ``reasoning_tokens`` 藏在 ``token_usage.completion_tokens_details.reasoning_tokens``
    里。**2026-08-20 实测 moonshot 确实报这个字段**:一次识图 输出 779 / 其中思考 596——
    76% 的算力花在工友永远看不见的地方,那也是当天把视觉思考关掉的直接依据
    (数据留在 ``config.disable_thinking_for_vision`` 上方)。所以这个数值得单独推:
    它一出现,「为什么这么慢」当场就有了答案。

    不是每家供应商都报,**取不到是常态,不是故障** —— 所以这里安静给 None,
    一条日志都不记(记了就是每次调用刷一行噪声)。

    ⚠️ 整只函数兜住异常:上游哪天把 ``details`` 换成对象、换成 None、换成字符串,
       都不该让一次模型调用失败。观测件绝不许把主路径打断。
    """
    try:
        usage = _usage(response)
        details = usage.get("completion_tokens_details")
        # 实测 langchain_openai 递进来的是 ``response.model_dump()`` 的产物 ——
        # 一层套一层的纯 dict。但别把这当成保证:换供应商 / 换版本完全可能给一个
        # pydantic 对象,所以 dict 取法和属性取法两条都留着。
        if details is None:
            reasoning = None
        elif hasattr(details, "get"):
            reasoning = details.get("reasoning_tokens")
        else:
            reasoning = getattr(details, "reasoning_tokens", None)
        return (
            _as_int(usage.get("prompt_tokens")),
            _as_int(usage.get("completion_tokens")),
            _as_int(reasoning),
        )
    except Exception:  # noqa: BLE001 —— 观测件绝不许把主路径打断
        return (None, None, None)


# ---------------------------------------------------------------------------
# 推给前端的 custom 事件(完整理由见模块头注「为什么同一份数还要往前端推一遍」)
# ---------------------------------------------------------------------------

EVENT_KEY = "gyt_timing"
"""事件的顶层键。**前端按这个名字认,改名等于改对外 API。**

只包一层、且带自己的前缀,是因为 custom 这条通道是**共用**的:谁都能往里写,
前端要能一眼把我们这条和别人那条分开。
"""

KIND_LLM = "llm"
KIND_TOOL = "tool"
"""事件的两种来源。**唯一真相在这两行。**

``core/errors.py`` 的 ``tool_guard`` 也要推,它从这里取 ``KIND_TOOL`` ——
**不许自己手抄一个字符串**。手抄的下场是前端按 ``kind`` 分流时静默少一整类
(工具那半边的耗时全不显示),而后端前端都不报错。
"""


def emit_timing(
    *,
    kind: str,
    name: str,
    seconds: float,
    ok: bool,
    slow: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> None:
    """把一条耗时推给前端。**不在图内就安静走开,任何情况下都不抛。**

    参数即契约(前端已按这个写,字段名不许改):

        kind:             ``KIND_LLM`` / ``KIND_TOOL``,前端按它分流
        name:             模型名(kimi-k3)或工具名(analyze_site_photo)
        seconds:          耗时,**保留一位小数**(和日志里的 ``%.1f`` 是同一个数)
        ok:               这次调用成没成。**失败的也推** —— 界面上「这一步失败了、
                          花了 213 秒」和「这一步很慢」是两回事
        slow:             是否 ``>=`` 阈值。判据与日志抬 warning 那条同源
        input_tokens 等:  只有 ``kind=llm`` 才可能有;命中缓存、供应商不报用量、
                          或者压根是工具,都给 None。**None 不许写成 0**(见 ``_as_int``)

    ⚠️ **八个键恒定存在**,``kind=tool`` 时三个 token 字段是 None 而不是缺席 ——
       形状固定的对象前端才能安心解构,少一个键就得每处都判一次 undefined。
    """
    try:
        # 🔴 **函数内 import,不许"顺手"提到文件顶部。** 两个理由,任一条都足够:
        #   ① `import langgraph.config` 实测 555.8ms(2026-08-20,还是在
        #      langchain_core 已加载的前提下量的)。本模块被 core/llm.py 模块级
        #      import,提上去等于给每一个 import 到 llm 的入口(含评测)白加半秒。
        #   ② 提上去就把"langgraph 装没装好"变成**import 期硬失败** ——
        #      观测件把主路径打断,正是本模块头注明令禁止的那件事。
        # 代价只有一次 sys.modules 字典查找:真在图里跑的时候 langgraph 早加载好了。
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 —— 这里是**正常路径**,见下
        # 🔴 不在 LangGraph 运行时里就会抛(实测 RuntimeError「Called get_config
        #    outside of a runnable context」)。评测、单测、CLI、清理脚本全都不在
        #    图内 —— **那是正常情况,不是故障**。所以连 debug 都不记:记了就是
        #    每跑一轮评测刷一屏噪声,而噪声会把真正的告警埋掉。
        #
        # ⚠️ 变异测试实证(2026-08-20):把这个 try/except 拿掉,test_timing.py
        #    当场红 7 条 —— 除了专门盯这件事的那条,还连累 5 条压根不管推事件的
        #    老用例。那 5 条就是"观测件把主路径打断"在真实世界里的样子。
        return
    if writer is None:
        return
    try:
        writer(
            {
                EVENT_KEY: {
                    "kind": kind,
                    "name": name,
                    # 保留一位小数:界面上「7.5 秒」够用,推 7.483210 只是噪声,
                    # 而且和日志里那个 %.1f 对得上 —— 两边说的必须是同一个数,
                    # 不然对账的人会以为看见了两次不同的调用。
                    "seconds": round(seconds, 1),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "slow": slow,
                    "ok": ok,
                }
            }
        )
    except Exception:  # noqa: BLE001 —— 推送坏了最多是界面上少一行,不能连累调用
        logger.debug("耗时事件推送失败,已忽略", exc_info=True)


class LlmCallTiming(BaseCallbackHandler):
    """给每次模型调用记一条「耗时 + 用量」。

    ⚠️ 这是**观测件**:任何情况下都不许抛异常把业务打断。所以每个回调里都兜了
       try/except —— 观测坏了最多是少一条日志,不能变成"用户提问失败"。
    """

    def __init__(self, slow_seconds: float) -> None:
        self._slow_seconds = slow_seconds
        self._started: dict[UUID, tuple[float, str]] = {}

    # -- 起点 ---------------------------------------------------------------
    # langchain 对 chat model 走 on_chat_model_start,对老式 LLM 走 on_llm_start。
    # 两个都实现:只实现前者的话,哪天有人加一个非 chat 的模型就静默不记了。

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._mark(run_id, kwargs)

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._mark(run_id, kwargs)

    def _mark(self, run_id: UUID, kwargs: dict[str, Any]) -> None:
        try:
            if len(self._started) >= _MAX_TRACKED:
                self._started.pop(next(iter(self._started)), None)
            params = kwargs.get("invocation_params") or {}
            model = str(params.get("model") or params.get("model_name") or "?")
            self._started[run_id] = (time.monotonic(), model)
        except Exception:  # noqa: BLE001 —— 观测件绝不许把主路径打断
            logger.debug("模型调用计时:记起点失败", exc_info=True)

    # -- 终点 ---------------------------------------------------------------

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            started = self._started.pop(run_id, None)
            if started is None:
                return
            elapsed, model = time.monotonic() - started[0], started[1]
            # 超过阈值抬到 warning:线上日志量很大,慢调用要能一眼捞出来。
            # 判据本身就是这个数 —— 见模块头注「为什么要这件」。
            slow = elapsed >= self._slow_seconds
            level = logging.WARNING if slow else logging.INFO
            logger.log(
                level,
                "模型调用完成 model=%s 耗时=%.1fs %s%s",
                model,
                elapsed,
                _fmt_tokens(response),
                f"(超过 {self._slow_seconds:.0f}s 阈值)" if level == logging.WARNING else "",
            )
        except Exception:  # noqa: BLE001
            logger.debug("模型调用计时:记终点失败", exc_info=True)
            return
        # 日志记完**再**推事件,顺序是刻意的:日志是最后的兜底 —— 前端那条通道断了
        # 我还能翻日志,反过来不成立。所以绝不许让推送的任何环节挡在日志前面。
        input_tokens, output_tokens, reasoning_tokens = _token_counts(response)
        emit_timing(
            kind=KIND_LLM,
            name=model,
            seconds=elapsed,
            ok=True,
            slow=slow,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """失败的调用也要记 —— 不记的话「慢且最终失败」那一类永远看不见,
        而且字典会泄漏(start 进去了没人 pop)。"""
        try:
            started = self._started.pop(run_id, None)
            if started is None:
                return
            elapsed, model = time.monotonic() - started[0], started[1]
            logger.warning(
                "模型调用失败 model=%s 耗时=%.1fs 类型=%s",
                model,
                elapsed,
                type(error).__name__,
            )
        except Exception:  # noqa: BLE001
            logger.debug("模型调用计时:记失败态失败", exc_info=True)
            return
        # 失败的也推。界面上「这一步失败了、还花了 213 秒」和「这一步很慢」是两回事,
        # 前端按 ok 分流 —— 只推成功的话,最值得看见的那一类恰好看不见。
        #
        # ⚠️ `slow` 照样按阈值如实算:上面那句日志"失败一律 warning"是**日志侧**的
        #    分级(失败本身就值得看见),和"这次是不是超阈值"不是同一件事,别混。
        # 三个 token 字段留 None:失败时压根没有 LLMResult,编不出数来。
        emit_timing(
            kind=KIND_LLM,
            name=model,
            seconds=elapsed,
            ok=False,
            slow=elapsed >= self._slow_seconds,
        )


__all__ = ["EVENT_KEY", "KIND_LLM", "KIND_TOOL", "LlmCallTiming", "emit_timing"]
