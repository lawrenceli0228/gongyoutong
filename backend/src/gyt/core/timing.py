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


def _fmt_tokens(response: LLMResult) -> str:
    """把 token 用量整成一小段人话。

    命中缓存时 langchain 照样会触发 callback,但 ``llm_output`` 里没有用量 ——
    所以"没有用量"基本等于"这次没真的问模型"。写成「无用量」而不是直接断言
    "命中缓存":那是推断不是事实,别把推断写成事实。
    """
    out = response.llm_output or {}
    usage = out.get("token_usage") or out.get("usage") or {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return "无用量(多半是命中缓存)"
    return f"输入 {prompt} / 输出 {completion} tok"


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
            level = logging.WARNING if elapsed >= self._slow_seconds else logging.INFO
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


__all__ = ["LlmCallTiming"]
