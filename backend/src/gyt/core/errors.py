"""统一工具错误信封。

所有 Agent 工具函数的返回值都必须是这里定义的 ``Envelope`` 字典,
目的是让 LLM 每次都拿到结构一致的结果,并且「工具失败就是失败」——
提示词侧要求如实转述 ``user_msg``,禁止编造成功。

三条硬约束:
1. ``user_msg`` 是给工地上的师傅看的中文人话,不出现堆栈、异常类名、内部路径。
2. ``detail`` 只写日志,绝不进返回值(防止内部细节被 LLM 复述给用户)。
3. ``tool_guard`` 永不向上抛异常,保证 Agent 循环不会因为一个工具炸掉而中断。
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from enum import Enum
from typing import Any, TypedDict, TypeVar, cast

from gyt.config import get_settings

logger = logging.getLogger(__name__)

# 类型变量:tool_guard 装饰后要保留原函数签名,方便 IDE 与类型检查。
F = TypeVar("F", bound=Callable[..., Any])


class ErrorCode(str, Enum):
    """工具错误码。取值一律等于名字本身,便于日志与前端直接比对字符串。"""

    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_UNSUPPORTED = "FILE_UNSUPPORTED"
    FILE_CORRUPT = "FILE_CORRUPT"
    NOT_FOUND = "NOT_FOUND"
    EMPTY_RESULT = "EMPTY_RESULT"
    INVALID_INPUT = "INVALID_INPUT"
    # 下面两个是打卡直连接口(W7,checkin_api.py)带进来的 HTTP 语义,
    # Agent 工具链用不到:工具没有「同幂等键但内容对不上」(409)和
    # 「令牌不过」(401)这两种失败形态。但直连 handler 的响应体同样走
    # Envelope(checkin_api.py 模块头注的响应契约),错误码得在这一张表里有名字,
    # 不许在 handler 里另起一套字符串。
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    INTERNAL = "INTERNAL"


class Envelope(TypedDict):
    """工具返回信封。四个键固定存在,缺一不可。

    ok:         这次工具调用是否成功
    data:       成功时的业务数据;失败时恒为 None
    user_msg:   给工人看的中文提示(成功时可为空串)
    error_code: 失败时的 ErrorCode 字符串;成功时为 None
    """

    ok: bool
    data: Any | None
    user_msg: str
    error_code: str | None


# 每个错误码配一句工人能看懂的中文人话。禁止写「HTTP 502」这类术语。
DEFAULT_USER_MSG: dict[ErrorCode, str] = {
    ErrorCode.TIMEOUT: "网络有点慢,这次没等到结果,请稍后再试一次。",
    ErrorCode.RATE_LIMITED: "现在用的人太多,正在排队,请过一会儿再试。",
    ErrorCode.UPSTREAM_ERROR: "后台服务暂时出了点问题,请稍后再试一次。",
    ErrorCode.FILE_TOO_LARGE: "文件太大了,请压缩一下或者拍小一点再传。",
    ErrorCode.FILE_UNSUPPORTED: "这种格式暂时打不开,请换成支持的格式再传。",
    ErrorCode.FILE_CORRUPT: "文件打不开,可能是上传过程中传坏了,请重新传一次。",
    ErrorCode.NOT_FOUND: "没找到这份东西,可能已经被删掉了,请确认后再试。",
    ErrorCode.EMPTY_RESULT: "没查到相关内容,换个说法或者补充点条件再问一次。",
    ErrorCode.INVALID_INPUT: "填的信息不太对,请检查一下再重新提交。",
    ErrorCode.CONFLICT: "这次提交的内容和之前那次对不上,请核对后再试一次。",
    # 与 backend/auth.py 的 DENY_MESSAGE 同一句 —— 那边的头注解释了为什么拒绝文案
    # 必须只有一句且一字不差(任何差异都是送给爆破脚本的信号)。改这句要连
    # auth.DENY_MESSAGE 和 attendance/messages.py 的 DENY 一起改。
    ErrorCode.UNAUTHORIZED: "访问被拒绝,请联系发你链接的人。",
    ErrorCode.INTERNAL: "系统开小差了,已经记录下来,请稍后再试一次。",
}


def _normalize_code(code: ErrorCode | str) -> ErrorCode:
    """把外部传进来的错误码收敛成 ErrorCode 枚举。

    注意:ErrorCode 虽然继承 str,但枚举成员的 hash 走的是成员对象,
    直接拿裸字符串 "TIMEOUT" 去查 DEFAULT_USER_MSG 会 KeyError,
    所以这里必须先做一次显式归一化。认不出来的码一律降级成 INTERNAL,
    保证 fail() 在任何情况下都能返回一句像样的中文提示,而不是自己先炸。
    """
    if isinstance(code, ErrorCode):
        return code
    try:
        return ErrorCode(str(code))
    except ValueError:
        logger.warning("收到未知错误码 %r,已降级为 INTERNAL", code)
        return ErrorCode.INTERNAL


def ok(data: Any = None, user_msg: str = "") -> Envelope:
    """构造成功信封。返回全新 dict,不复用也不修改任何入参。"""
    return {"ok": True, "data": data, "user_msg": user_msg, "error_code": None}


def fail(code: ErrorCode | str, user_msg: str = "", *, detail: str = "") -> Envelope:
    """构造失败信封。

    参数:
        code:     错误码,决定默认中文提示。
        user_msg: 自定义中文提示;留空时取 DEFAULT_USER_MSG[code]。
        detail:   内部细节(异常信息、URL、路径等)。**只写日志**,
                  绝不会出现在返回值里,避免泄露给 LLM 与最终用户。
    """
    normalized = _normalize_code(code)
    if detail:
        # detail 到此为止:进日志、不进返回值。
        logger.warning("工具失败 code=%s detail=%s", normalized.value, detail)
    return {
        "ok": False,
        "data": None,
        "user_msg": user_msg or DEFAULT_USER_MSG[normalized],
        "error_code": normalized.value,
    }


# ---------------------------------------------------------------------------
# tool_guard:工具函数的最后一道防线
#
#   tool_guard(fn)
#         |
#         +--> inspect.iscoroutinefunction(fn) ?
#              |                            |
#             yes                          no
#              |                            |
#              v                            v
#        async 包装器                   同步包装器
#              |                            |
#              +--------------+-------------+
#                             |
#                        try: 执行 fn
#                             |
#              +--------------+--------------+
#              |                             |
#          正常返回                    抛出 Exception
#              |                             |
#              v                             v
#     原样透传(约定 fn 已返回信封)   logger.exception 记录完整堆栈
#                                            |
#                                            v
#                              返回 fail(INTERNAL) 信封,永不向上抛
#
# 说明:只捕 Exception,不捕 BaseException——KeyboardInterrupt / SystemExit
# 必须继续往上走,否则进程没法正常退出。
# ---------------------------------------------------------------------------


def _guard_detail(fn: Callable[..., Any], exc: BaseException) -> str:
    """拼接只进日志的内部细节。"""
    return f"{fn.__module__}.{fn.__qualname__} 抛出 {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 耗时观测:工具那一侧
#
# 为什么要记(2026-08-20,一次花了两小时的日志考古之后):线上一次「拍照 → 出
# 巡检记录」量到 213 秒,日志里只有 httpx 那几行「HTTP Request: POST ... 200 OK」,
# 于是只能靠**相邻两行的时间差**倒推 —— 而倒推不出的恰恰是最要紧的一件事:
# 那 198 秒是**在模型调用里面**,还是**在发出请求之前**(工具后处理、隐患入库、
# 中间件、图调度)?两者修法完全不同。
#
# 模型那一侧由 core/timing.py 的 LlmCallTiming 接上,工具这一侧就是下面这段。
# 两件合起来才把那段黑箱切开(完整推演在 core/timing.py 的模块头注):
#
#     模型调用记了 198 秒  → 在调用里面,查模型侧 / 网络
#     模型调用记了 2 秒    → 在调用之前,查工具与图调度 ← 这条日志说话
#
# 同一份数还会**推一份给前端**(custom 事件,kind="tool"),理由同样在
# core/timing.py 的头注:日志只有我看得见,而在工地上等的是工友。
# ---------------------------------------------------------------------------

_NO_SLOW_THRESHOLD = float("inf")
"""配置读不到时用的"永不告警"阈值。

**刻意不在这里抄一份 20.0 的默认值** —— 抄了就是第二份真相(唯一入口是
config.get_settings),而且配置真读不出来的时候猜一个数只会误报。inf 的效果是
"照样记耗时,只是不往 warning 抬":那个数字本身仍然原样在日志里,人一眼看得出。
"""


def _tool_slow_threshold() -> float:
    """取工具慢调用阈值(秒)。取不到就退回 _NO_SLOW_THRESHOLD。

    ⚠️ 观测件读配置也可能炸(.env 写坏、pydantic 校验不过),所以这里必须兜住 ——
       绝不许出现"因为阈值读不出来,所以工具执行失败"。
    """
    try:
        return get_settings().tool_slow_warn_s
    except Exception:  # noqa: BLE001 —— 观测绝不许把主路径打断
        return _NO_SLOW_THRESHOLD


def _emit_tool_timing(name: str, elapsed: float, *, succeeded: bool, slow: bool) -> None:
    """把这条耗时也记进后端缓冲(``kind="tool"``),前端自己来取。

    ⚠️ **整只函数兜住异常** —— 同 ``_log_tool_elapsed``:界面上少一行耗时是小事,
       把工友的提问弄失败是大事。连兜底那句 debug 也再兜一层(会抛的 logger
       在 test_timing.py 里是真实存在的替身)。

    🔴 **这里不传 ``thread_id``,是刻意的。** ``emit_timing`` 取不到就会自己去问
       LangGraph 运行时(``timing._current_thread_id``),而那句 ``import langgraph``
       在**它那边**的函数体里 —— 于是 ``core/errors.py`` 至今一行 langgraph 都不用碰,
       下面那条"模块级不许 import langchain"的守卫继续成立。
       顺手在这儿 ``from langgraph.config import get_config`` 会当场破掉它。
       ``agent`` 同理不传:工具跑在哪个节点这件事,值不值得为它破守卫是另一码事,
       今天的答案是不值得 —— 界面上工具那一类本来就按工具名显示。

    🔴 **`from gyt.core.timing import ...` 必须留在函数体里,不许提到文件顶部。**
       ``core/errors.py`` 至今是 langchain-free 的,而 ``core/timing.py`` 要拉
       langchain(``LlmCallTiming`` 的基类 ``BaseCallbackHandler``)+ 间接拉
       langgraph。attendance/cleanup.py、core/doc_no.py、attendance/messages.py
       三个轻量入口都还靠着这条,``test_timing.py`` 里有一条守卫钉死了
       **模块级** import 一行都不许有。
       放函数里是安全的:真会走到这儿的进程一定在跑 Agent,那时 timing 早
       import 好了,只剩一次 sys.modules 字典查找。

    ⚠️ 为什么不在这儿自己写一份、而要跨模块去取:记录契约(``timing.RECORD_KEYS``
       那十个键的名字)只能有一份真相。抄一份的下场是哪天改字段名只改了一边,
       而两边都不报错 —— 前端按 ``kind`` 分流时静默少掉工具那一整类。
    """
    try:
        from gyt.core.timing import KIND_TOOL, emit_timing

        emit_timing(kind=KIND_TOOL, name=name, seconds=elapsed, ok=succeeded, slow=slow)
    except Exception:  # noqa: BLE001 —— 观测件绝不许把工具打断
        with suppress(Exception):
            logger.debug("工具耗时推事件失败,已忽略", exc_info=True)


def _log_tool_elapsed(name: str, elapsed: float, failed: str | None = None) -> None:
    """给一次工具执行记一条耗时。``failed`` 非空表示走的是异常路径。

    ⚠️ **整只函数兜住异常**:观测件坏掉最多是少一条日志,绝不许变成"工具失败"。
       连兜底那句 debug 也再兜一层 —— test_timing.py 里会把 logger 整个换成
       "每个方法都抛"的替身,验的就是业务结果一点不受影响。

    ⚠️ 分级判据(``>=`` 阈值抬 warning)与 core/timing.py 的 ``LlmCallTiming``
       是同一条,但**刻意不共用代码**:timing.py 要 import langchain
       (``LlmCallTiming`` 的基类 ``BaseCallbackHandler``),而 core/errors.py
       至今是 langchain-free 的 —— 2026-08-20 实测导入耗时 24.8ms vs 249.6ms,
       且 attendance/cleanup.py、core/doc_no.py、attendance/messages.py 三个轻量
       入口都还靠着这条(它们现在 import 完 sys.modules 里没有 langchain_core)。
       为省这十几行去换一条重依赖不划算。**改这里的分级判据,记得回 timing.py
       看一眼那一条**,别让两边漂了。

       ⚠️ 上面这条**在记耗时那件事上让了一步、而且只让这一步**:``_emit_tool_timing``
          会去 timing.py 取 ``emit_timing``,但那是**函数体内**的 import,模块级
          依然一行 langchain 都没有(守卫仍在 test_timing.py)。让步的理由是
          "记录契约的十个键只能有一份真相",和这里的分级判据无关 ——
          **别拿它当借口把分级代码也合过去**。
    """
    # 阈值取不出来时 `_tool_slow_threshold` 已经退回 inf,不会抛;这里再给 slow 一个
    # 初值,是为了让下面那句推事件在 logger 半路炸掉时也有个数可用(推的是"不算慢",
    # 保守方向:宁可漏标一次慢,也不许凭空标一个)。
    slow = False
    try:
        threshold = _tool_slow_threshold()
        # 判据是 `>=` 不是 `>`,边界那一下归"慢" —— 与 core/timing.py 同一条。
        slow = elapsed >= threshold
        if failed is not None:
            # 失败路径也要记 —— 「慢且最终失败」那一类不记就永远看不见。
            # 堆栈由调用方的 logger.exception 负责,这里只补"耗时"这一个数。
            #
            # ⚠️ 失败一律 warning,**不看阈值** —— 失败本身就值得看见。但 `slow`
            #    那个字段照样按阈值如实算:那是"超没超时限"这件事,和日志分级
            #    不是同一回事,别因为这里恒 warning 就把 slow 也写死成 True。
            logger.warning("工具执行失败 name=%s 耗时=%.1fs 类型=%s", name, elapsed, failed)
        else:
            # 超阈值抬到 warning:线上日志量很大,慢调用要能一眼捞出来。
            level = logging.WARNING if slow else logging.INFO
            logger.log(
                level,
                "工具执行完成 name=%s 耗时=%.1fs%s",
                name,
                elapsed,
                f"(超过 {threshold:.0f}s 阈值)" if level == logging.WARNING else "",
            )
    except Exception:  # noqa: BLE001 —— 同上,观测件绝不许把工具打断
        with suppress(Exception):
            logger.debug("工具耗时观测失败,已忽略", exc_info=True)
    # 日志记完**再**记缓冲,顺序同 core/timing.py:日志是最后的兜底,前端那条
    # 路断了还能翻日志,反过来不成立。放在 try 外面,是为了让"日志器坏了"和
    # "记缓冲坏了"这两件事互不牵连 —— 各自兜各自的。
    _emit_tool_timing(name, elapsed, succeeded=failed is None, slow=slow)


def tool_guard(fn: F) -> F:
    """兜底装饰器:同步 / async 工具函数都能包。

    被包住的函数一旦抛异常,调用方拿到的是 fail(INTERNAL) 信封而不是异常。
    正常返回值原样透传(约定工具自己已经用 ok()/fail() 包好了)。

    顺带记一条耗时(见上面 `_log_tool_elapsed` 那段)。挂在这里而不是让每个工具
    各自记,理由和信封本身一样:**这里是所有工具的唯一必经处**,谁都不会忘。

    ⚠️ **两个包装器都要记**。漏一个的表现是"有的工具有耗时日志、有的没有",
       而人会以为那个工具没被调用过 —— 极难注意到,所以下面两段是刻意对称的。
    """
    # 提前取一次:functools.wraps 会把 __qualname__ 抄到包装器上,写 fn.__qualname__
    # 和写包装器的是同一个值,但取一次更明确"记的是被包的那个函数"。
    name = fn.__qualname__

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 兜底就是要吃掉一切业务异常
                logger.exception("工具执行失败(async): %s", name)
                _log_tool_elapsed(name, time.monotonic() - started, type(exc).__name__)
                return fail(ErrorCode.INTERNAL, detail=_guard_detail(fn, exc))
            # 刻意用 else 段而不是 finally:BaseException(KeyboardInterrupt /
            # SystemExit / CancelledError)照旧原样上抛,且**不**记成"执行完成"——
            # 那是句假话。代价是被取消的慢调用不留耗时,可以接受。
            _log_tool_elapsed(name, time.monotonic() - started)
            return result

        return cast(F, _async_wrapper)

    @functools.wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 同上,永不向上抛
            logger.exception("工具执行失败: %s", name)
            _log_tool_elapsed(name, time.monotonic() - started, type(exc).__name__)
            return fail(ErrorCode.INTERNAL, detail=_guard_detail(fn, exc))
        _log_tool_elapsed(name, time.monotonic() - started)
        return result

    return cast(F, _sync_wrapper)


__all__ = [
    "DEFAULT_USER_MSG",
    "Envelope",
    "ErrorCode",
    "fail",
    "ok",
    "tool_guard",
]
