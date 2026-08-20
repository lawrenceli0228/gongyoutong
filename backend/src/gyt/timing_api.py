"""耗时观测的直连接口 —— 界面上那几行「每一步花了多久」的数据源。

本模块导出 ``TIMING_ROUTES``,由 webapp.py 铺进它的 ``routes=[...]``
(与 ``checkin_api.CHECKIN_ROUTES`` / ``supervision_api.SUPERVISION_ROUTES`` 同一条路)。

===========================================================================
为什么是直连接口,而不是挂在聊天流上
---------------------------------------------------------------------------
完整推演在 ``core/timing.py`` 的模块头注,这里只留结论与那条会咬人的判据:

    值得看的模型调用**全在子图里**(safety 识图、schedule 记台账,都是
    create_supervisor 挂进去的独立 Pregel)。子图里发的 custom 事件必须请求方
    开 ``subgraphs=True`` 才出得来;而一开它,子图的 ``values`` 事件也跟着出来,
    SDK 对每个 values 事件是**整份替换**主状态 —— 于是子 Agent 说的话
    先出现、再消失,工友看见的是「话被收回去了」。

    两件是同一个开关的两头,走聊天流只能二选一。

所以耗时走自己的入口,照 W7 打卡、W10 监理操作台的先例 ——
**「操作台不是聊天产物,它有自己的入口和自己的数据源。」** 观测数据是第三个同类。

===========================================================================
请求 / 响应契约(**唯一真相在这儿**)
---------------------------------------------------------------------------
    GET /timing?thread_id=<会话号>&since=<游标>

        thread_id  必填。就是 chat-ui 里那个 threadId
        since      选填,默认 0。只要 ``seq`` **大于**它的记录 ——
                   增量拉取,前端每轮把上次拿到的 ``next_since`` 原样带回来

    200 {"ok": true,
         "data": {"records": [...], "next_since": 12},
         "user_msg": "", "error_code": null}

        records    每条十个键,形状的唯一真相是 ``core/timing.RECORD_KEYS``。
                   **按 seq 升序**,前端直接 append 就行,不用自己排
        next_since 下一轮该带的游标。**没有新记录时原样退回传入的 since**,
                   不是 0 —— 退成 0 会让前端下一轮把整段重拉,界面上表现为
                   耗时行成倍重复,而两边都不报错

    400  thread_id 没给,或 since 不是非负整数
    401  令牌不过(判据与 auth.py / checkin_api / supervision_api 完全同源)
    500  兜底

⚠️ **响应体一律走 Envelope**(四键 ok/data/user_msg/error_code),和另外两个直连
   接口同一个形状 —— 前端解信封那段代码是共用的,这里裸拼一个 dict 就等于
   给它加一个特例。

⚠️ **这是只读接口,没有任何写入口。** 耗时记录只由 ``core/timing.emit_timing``
   在进程内产生 —— 外面不许往里塞,否则界面上那几行就不再是"实际发生了什么"。
"""

from __future__ import annotations

import hmac
import logging
from typing import Final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.core.access import _api_key_from_headers, effective_access_token
from gyt.core.errors import Envelope, ErrorCode, fail, ok
from gyt.core.timing import recent_timings

logger = logging.getLogger(__name__)

_MISSING_THREAD_MSG: Final[str] = "这一步的耗时暂时取不到,不影响你继续用。"
"""400 的人话。

⚠️ 缺 thread_id / since 写错**都是前端的编程错误,工友一辈子碰不到**。
   但这句话仍然按"工地师傅看得懂"来写(本仓对 user_msg 的硬规矩没有例外),
   而且刻意**不说"参数错误"** —— 真漏到界面上时,那句话对工友毫无意义,
   反而会让他以为自己按错了。真正的原因走 ``detail=``,只进日志。
"""


def _respond(env: Envelope, status: int) -> JSONResponse:
    """Envelope → JSON 响应。进了 handler 之后所有出口都走这里,一个都不许裸拼 dict。"""
    return JSONResponse(env, status_code=status)


def _deny_if_token_bad(request: Request) -> JSONResponse | None:
    """handler 自查令牌。放行返回 None,不过返回 401。

    与 ``checkin_api._deny_if_token_bad`` / ``supervision_api._deny_if_token_bad``
    **同源同判据**(同一份 ``core/access.py``),连日志措辞都对齐。

    第一道在 langgraph 的鉴权中间件(``langgraph.json`` 的
    ``enable_custom_route_auth: true``);这道防的是那个键被漏配 —— 漏配时第一道
    **整条消失且没有任何报错**,只有这道自查兜得住,也只有它能被单元测试钉死。

    空 / 占位符 / 过短 = 未配置 = 放行:``make dev`` 与真机验收不发令牌头,
    「未配置也拦」等于当场打死本机联调。

    ⚠️ 每次请求现取,**不许在 import 时读成常量** —— 那样测试换环境变量测不到,
       生产换口令要重启才生效。
    """
    expected = effective_access_token()
    if not expected:
        return None
    presented = _api_key_from_headers(request.headers)
    # compare_digest:恒定时间比对,不给逐位试探留统计量;encode 成 bytes 防非 ASCII
    # 口令把 401 变成 500(理由原样见 auth.authenticate)。
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return None
    logger.warning(
        "耗时接口鉴权失败:path=%s 原因=%s",
        request.url.path,
        "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
    )
    return _respond(fail(ErrorCode.UNAUTHORIZED), 401)


def _parse_since(raw: str | None) -> int | None:
    """游标解析。合法给 int,不合法给 None(调用方据此回 400)。

    ⚠️ **不给就是 0,不是错误** —— 前端第一次拉取本来就没有游标。
       但"给了一个解析不出来的东西"是另一回事:那时候静默当 0 会把整段重拉,
       界面上耗时行凭空翻倍,而没有任何一侧报错。所以宁可 400。

    负数也拒:``since=-1`` 能跑通,但它表达的是"比不存在的记录还早",
    是调用方算错了游标的信号,放过去只会把错误藏到下一层。
    """
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


async def get_timing(request: Request) -> JSONResponse:
    """取某个会话的耗时记录(增量)。

    🔴 **令牌自查排在取参数之前**,与另外两个直连接口同一条规矩。这条端点没有
       请求体,所以这里的代价很小 —— 但顺序本身是规矩,别因为"这条无所谓"
       就开个先例。

    ⚠️ 读缓冲挪进线程池,虽然它只是一次加锁 + 列表拷贝。理由是 blockbuster
       **确实拦 ``threading.Lock.acquire``**,判据是
       ``not blocking or timeout == 0 or not lock.locked()`` —— 也就是说
       **只有"锁正被别人占着"那一下才会被判违规**。写那一侧跑在 langchain 的
       线程执行器里(实测:callback 与图不在同一个线程),所以撞上的概率很小
       **但不是零**。今天线上跑的是 ``--allow-blocking``(Dockerfile 的 CMD 里
       就有),真出事也不会抛 —— 正因为如此才更该躲开:一个只在拿掉那个开关时
       才偶发、且偶发得毫无规律的 500,比一个稳定的错误难查十倍。
    """
    denied = _deny_if_token_bad(request)
    if denied is not None:
        return denied

    thread_id = (request.query_params.get("thread_id") or "").strip()
    if not thread_id:
        return _respond(
            fail(ErrorCode.INVALID_INPUT, _MISSING_THREAD_MSG, detail="缺 thread_id 查询参数"),
            400,
        )

    since = _parse_since(request.query_params.get("since"))
    if since is None:
        return _respond(
            fail(
                ErrorCode.INVALID_INPUT,
                _MISSING_THREAD_MSG,
                detail=f"since 不是非负整数:{request.query_params.get('since')!r}",
            ),
            400,
        )

    try:
        records, next_since = await run_in_threadpool(recent_timings, thread_id, since)
    except Exception:
        # 兜底:观测接口炸了绝不能连累别的。堆栈只进日志;user_msg 走
        # DEFAULT_USER_MSG[INTERNAL](已是人话),不在这里另造第二句。
        logger.exception("取耗时记录失败:thread_id=%s", thread_id)
        return _respond(fail(ErrorCode.INTERNAL), 500)

    return _respond(ok(data={"records": records, "next_since": next_since}), 200)


TIMING_ROUTES: Final[list[Route]] = [
    Route("/timing", get_timing, methods=["GET"]),
]
"""本模块对外的全部路由 —— **一条,只读**。

由 ``webapp.py`` 铺进 Starlette 的 ``routes=[...]``。为什么不像 checkin_api 那样
自己也建一个 ``app``:那份是 2026-08-15 合流时的历史(它当时是 langgraph.json
的 ``http.app`` 直接指向物),现在唯一挂载点是 webapp.py,新模块不必再留那条尾巴。
"""

__all__ = ["TIMING_ROUTES", "get_timing"]
