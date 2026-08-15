"""对外访问闸门的公共件:「有效令牌」判定、``X-Api-Key`` 取值、令牌桶。

===========================================================================
为什么要把这几样从 backend/auth.py 抽出来(2026-08-15,W7 打卡)
---------------------------------------------------------------------------
``backend/auth.py`` 与 ``src/gyt/checkin_api.py`` 都是被 langgraph-api 用
``importlib.util.spec_from_file_location`` **按文件路径**加载的(前者走
``langgraph.json`` 的 ``auth.path``,后者走 ``http.app``)。按文件路径加载的
模块**互相 import 不可靠**:auth.py 躺在 backend 根、不在安装包里,
checkin_api 在那个加载环境下 ``import auth`` 能不能成,取决于 sys.path 的
偶然状态 —— 而失败的形态是**部署后才炸**,单测抓不到。两个文件倒是都能
``from gyt.xxx import``(auth.py 一直在这么干),所以 **gyt 包是它们唯一的
共同地面**,公共件只能放这里。

依赖方向单向:auth.py / checkin_api.py → 本模块 → gyt.config。
本模块**不许**反向 import 那两个文件(auth.py 根本不在包里,import 不到)。

搬过来的东西**语义一字不差**(``tests/unit/test_auth.py`` 是行为契约,
搬完必须原样全绿):

  · 「有效令牌」判定 —— 空 / 占位符开头 / 过短 = 未配置 = 鉴权关闭;
  · ``X-Api-Key`` 的大小写不敏感取值(含畸形字节不许炸成 500);
  · 按身份计的令牌桶。

名字保持原样(含下划线前缀)——它们仍是闸门的内部件,只是作用域从
「单文件私有」变成「包内共享」;auth.py 原地 re-export,旧引用
(``auth.TokenBucket`` 等)一个都不断。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from gyt.config import get_settings

API_KEY_HEADER: Final[str] = "x-api-key"
"""令牌请求头名(**小写**存放,比对时两边都转小写)。

前端已经在发这个头了:``frontend/src/providers/Stream.tsx`` 里
``if (apiKey) headers.set("X-Api-Key", apiKey);`` —— 大小写是 ``X-Api-Key``。
HTTP 头名本来就大小写不敏感,ASGI 规范还要求传到应用层时已经小写,
但**下面的取值逻辑不依赖这条规范**,理由见 ``_api_key_from_headers``。
"""


# ---------------------------------------------------------------------------
# 「有效令牌」判定(唯一入口是 gyt.config.get_settings,不许在这里写死任何数字)
# ---------------------------------------------------------------------------


def _access_token() -> str:
    """当前配置的访问令牌;空串 = 没配 = 鉴权关闭。

    每次调用都重新走 ``get_settings()`` —— 它自带 lru_cache,进程内只解析一次,
    但测试里 ``get_settings.cache_clear()`` 之后能立刻拿到新值。
    不在模块级把它读成常量,就是为了这个可测性。
    """
    return get_settings().access_token.strip()


_PLACEHOLDER_PREFIXES: Final[tuple[str, ...]] = ("替换成", "change", "your", "todo", "xxx")
"""模板占位符的开头。命中就当成「没设」处理。

为什么需要这张表:``docker-compose.vps.yml`` 用 ``${GYT_ACCESS_TOKEN:?…}`` 做 fail-closed,
但那道闸只认**空**,不认「非空但是假的」。2026-08-11 安全复核实测:
``.env.vps.example`` 里那行占位符是非空中文字符串,**顺利通过 compose 检查**,
后端于是「正常开启鉴权」,而令牌是一串**公开写在 git 仓库里的字**——
第二道锁当场归零,且全流程零信号(横幅只在令牌为空时打,日志里写的是「已开启」)。

判据与 ``scripts/frontend-overrides/api-key.tsx`` 的 ``startsWith("替换成")`` 同源。
"""

_MIN_TOKEN_LEN: Final[int] = 24
"""令牌的最短长度。``openssl rand -hex 32`` 出来是 64 位,离这个下限很远;
而人手打的「test」「gyt123」这种一定过不去。取 24 不是密码学结论,
是「挡住随手填的,不挡住真随机的」这条工程判据。"""


def _token_problem(raw: str) -> str | None:
    """令牌看着像不像真的。像就返回 None,不像就返回一句中文说明(只进日志)。"""
    if not raw:
        return None  # 空 = 明确的「没配」,由调用方走关闭分支,不算问题
    low = raw.lower()
    if any(low.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return "它看着是模板里的占位符,不是真令牌"
    if len(raw) < _MIN_TOKEN_LEN:
        return f"它只有 {len(raw)} 个字符,短于最低要求的 {_MIN_TOKEN_LEN} 位"
    return None


def effective_access_token() -> str:
    """真正作数的访问令牌;空串 = 未配置(空 / 占位符 / 过短一律视同没配)。

    这是给 ``checkin_api.py`` 的自查用的**组合件**(它的模块头注要求判定
    「必须与 auth.py 完全同源」),auth.py 自己的 ``is_enforcing`` 出于
    「抽取重构行为零变化」没有改写成调这里 —— 但两边的判据就是上面同一对
    ``_access_token`` / ``_token_problem``,结构上漂不了。
    """
    raw = _access_token()
    if not raw or _token_problem(raw) is not None:
        return ""
    return raw


# ---------------------------------------------------------------------------
# 请求头取值
# ---------------------------------------------------------------------------


def _decode_header_part(raw: Any) -> str:
    """把 ASGI 原始头里的一段(bytes 或 str)转成 str。

    ``errors="replace"``:头是外部输入,收到非法 UTF-8 时**不许抛异常** ——
    抛出去就变成 500,等于给了攻击者一个「用畸形字节撬服务」的把手。
    转成替换字符后照常走比对,结果必然不匹配,走正常的 401 路径。
    """
    if isinstance(raw, bytes | bytearray):
        return bytes(raw).decode("utf-8", errors="replace")
    return str(raw)


def _api_key_from_headers(headers: Mapping[Any, Any] | None) -> str:
    """从请求头里取 ``X-Api-Key``,**大小写不敏感**;取不到返回空串。

    为什么要自己归一化,不直接 ``headers[b"x-api-key"]``:

    1. HTTP 头名本来就大小写不敏感(RFC 9110),客户端写成 ``X-API-KEY`` 完全合法;
    2. 键的**类型**在上游两处文档里就对不上 —— langgraph_sdk 的 authenticate
       文档字符串写的是 ``headers: dict[str, bytes]``
       (``langgraph_sdk/auth/types.py:307``),而真正注入这个参数的
       ``langgraph_api/auth/custom.py`` 里 ``SUPPORTED_PARAMETERS`` 标的是
       ``dict[bytes, bytes] | None``,实现是 ``dict(scope.get("headers", {}))``
       (ASGI 原始头,bytes→bytes)。两边说法不一致时,**按最宽的处理**,
       别赌哪一份是对的;
    3. 单测里直接传 str 键的字典也能跑,不用为了测试去凑 bytes。
       (Starlette 的 ``Headers`` 也是个 str→str 的 Mapping,checkin_api
       直接把它递进来,同一条逻辑两处共用。)

    ``headers=None`` 也要接住:上游把这个参数标成了 ``| None``。
    """
    if not headers:
        return ""
    for raw_name, raw_value in headers.items():
        if _decode_header_part(raw_name).strip().lower() == API_KEY_HEADER:
            return _decode_header_part(raw_value).strip()
    return ""


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------


class TokenBucket:
    """按身份计的令牌桶。放行返回 ``None``,拒绝返回「建议等待秒数」。

    形态选的是令牌桶而不是固定窗口计数,因为它天然同时表达两件事:

        capacity(桶容量)      = 允许的**突发**量(连着点几下不该被拦)
        refill(每分钟补多少)  = 允许的**持续**速率(挡住脚本长跑)

    固定窗口做不到这一点 —— 要么突发被误杀,要么窗口边界上能打进双倍。

    ``clock`` 可注入是为了测试:限流的行为**全靠时间推进**,真 ``sleep`` 写出来的
    测试又慢又飘(CI 上负载一高就红)。注入一个假时钟,时间就成了普通输入。

    不可变性红线的说明:限流器就是一件有状态的东西,这里没法「返回新对象」。
    折中做法是每次结算都往 dict 里放一个**新的 tuple**,不原地改可变对象,
    并且状态全部关在这个类里,外面拿不到引用。
    """

    def __init__(
        self,
        capacity: int,
        refill_per_minute: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = float(capacity)
        self.refill_per_minute = float(refill_per_minute)
        self._refill_per_second = float(refill_per_minute) / 60.0
        self._clock = clock
        # identity -> (剩余令牌, 上次结算时刻)
        self._state: dict[str, tuple[float, float]] = {}
        # langgraph dev 大部分时候是单事件循环,但框架里有 run_in_threadpool 这类
        # 线程逃逸路径。锁很便宜(dict 读改写,微秒级),不赌「不会并发」。
        self._lock = threading.Lock()

    def take(self, identity: str) -> float | None:
        """取一枚令牌。放行返回 ``None``;拒绝返回建议等待的秒数(> 0)。

        补令牌用的是「按经过时间连续补」而不是「定时器」:没有后台任务,
        也就没有「进程闲着也在跑东西」的成本,冷启动即正确。
        """
        now = self._clock()
        with self._lock:
            tokens, last_seen = self._state.get(identity, (self.capacity, now))
            # max(0.0, ...):time.monotonic 不会倒流,但注入的假时钟可能被写错,
            # 倒流时按「没过时间」处理,绝不凭空补令牌。
            elapsed = max(0.0, now - last_seen)
            tokens = min(self.capacity, tokens + elapsed * self._refill_per_second)
            if tokens >= 1.0:
                self._state[identity] = (tokens - 1.0, now)
                return None
            self._state[identity] = (tokens, now)
            return (1.0 - tokens) / self._refill_per_second


# 下划线开头的那些(_access_token / _token_problem / _api_key_from_headers …)
# 刻意不进 __all__:它们是闸门内部件,auth.py 与 checkin_api.py 按名字显式 import,
# 不开放给 star-import。
__all__ = [
    "API_KEY_HEADER",
    "TokenBucket",
    "effective_access_token",
]
