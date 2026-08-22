"""巡检记录的直连接口 —— 界面上那个「巡檢記錄」抽屉的数据源(2026-08-22)。

本模块导出 ``REPORTS_ROUTES``,由 webapp.py 铺进它的 ``routes=[...]``
(与 ``checkin_api.CHECKIN_ROUTES`` / ``supervision_api.SUPERVISION_ROUTES`` /
``timing_api.TIMING_ROUTES`` 同一条路)。

===========================================================================
🔴 为什么非有这条不可:巡检记录出了,而界面上**没有任何下载出口**
---------------------------------------------------------------------------
「拍照 → 自动出 Word」是本产品的头号卖点,而这条链的**终点是断的**:

  ① 文档真的生成了、真的登记成产物、真的落在磁盘上(``agents/report/tools.py``);
  ② 那份 Envelope 里有 ``report_no`` / ``filename`` / ``artifact_id``,
     而 supervisor 的 ``output_mode="last_message"`` **把子 Agent 的工具返回整个丢掉**
     —— 这是 W10 已经查清并记录在案的老根因(``docs/W10_界面取不到工具返回_方案.md``);
  ③ 于是 ``tool-calls.tsx`` 里那张巡检记录卡(W3 就写好了)**一次都没渲染出来过**;
  ④ 而 ``agents/report/prompt.md`` 教模型说「要打印或转发跟管理员说编号就行」——
     **那个管理员不存在**。真机验收里工友原话就是被这么打发走的(TODO-34)。

合起来:系统答「巡检记录出好了,编号 GYT-…」,人照着去找管理员,而没有管理员;
文件就在服务器上,谁都拿不到。

形状照 W7 打卡面板、W10 监理操作台的先例 ——
**「操作台不是聊天产物,它有自己的入口和自己的数据源。」** 这是第四个同类。

===========================================================================
为什么不建表,而是扫产物 sidecar
---------------------------------------------------------------------------
巡检记录**从来没进过任何库表**:``render_inspection_report`` 生成 docx 之后直接
``artifacts.register(kind=REPORT)``,元数据落在 ``<artifacts_dir>/<日期>/<id>.json``
这份 sidecar 里(``id`` / ``kind`` / ``original_name`` / ``created_at`` / ``size_bytes``
一应俱全)。建表要么只覆盖今后、要么得写一次回填 —— 而 sidecar 里本来就有全部信息,
``attendance/cleanup.py`` 早就在用同一套扫法(那边扫 ATTENDANCE,这里扫 REPORT)。

⚠️ **扫是有边界的**,见 ``_MAX_SIDECARS_SCANNED``:线上产物目录会长到上万份
(每张照片一份),每次请求读一万个小 JSON 是不能接受的。做法是**按日期目录从新往旧扫**
(目录名是 ``YYYYMMDD``,字典序即时间序),够数就停。扫到上限还没够数时如实回
``scan_truncated: true`` —— 前端据此说「只列了最近这些」,而不是让人以为"就这么多"。

===========================================================================
请求 / 响应契约(**唯一真相在这儿**)
---------------------------------------------------------------------------
    GET /reports?limit=<条数>

        limit  选填,默认 ``_DEFAULT_LIMIT``,上限 ``_MAX_LIMIT``。
               超上限**截到上限,不报错** —— 这是个只读列表,为一个过大的数字
               挡住整页内容不划算(与 ``_issued_by`` 超长截断同一取舍)。

    200 {"ok": true,
         "data": {"reports": [...], "total": 3, "scan_truncated": false},
         "user_msg": "…", "error_code": null}

        reports  按**生成时间倒序**(最近的在最前)。每条五个键::

            artifact_id  32 位十六进制。前端拿它拼下载地址:
                         ``<ARTIFACT_BASE>/by-id/<artifact_id>``
                         (``scripts/serve_artifacts.py`` 的 ``/by-id/`` 端点,
                         它会在日期分目录里 glob —— 前端猜不出日期段,
                         也不能按本地当天日期去猜:晚上 UTC 已经是"明天")
            report_no    ``GYT-八位日期-六位时刻``,从 ``original_name`` 里解出来。
                         🔴 **它同时是过滤判据**:解不出编号的产物根本不会出现在这个
                         清单里 —— ``ArtifactKind.REPORT`` 那一档**监理文书也在用**,
                         只按 kind 过滤会把《工程暂停令》一类混进来(见 ``_REPORT_NO_RE``)
            filename     原始文件名(``巡检记录_GYT-….docx``),下载时显示的名字
            size_bytes   文件大小
            created_at   登记时刻(UTC ISO,产物 sidecar 里那个)

        total          本次**列出来**几条(不是磁盘上一共几份)
        scan_truncated 扫到上限还没扫完 → true

    400  limit 不是正整数
    401  令牌不过(判据与 auth.py / checkin_api / supervision_api / timing_api 同源)
    500  兜底

⚠️ **这是只读接口,没有任何写入口。** 巡检记录只由 ``render_inspection_report``
   产生 —— 这里只负责把已经存在的那些列出来。

⚠️ **它不回文件正文。** 正文由 ``serve_artifacts.py``(本机 :8788)或公网那侧的
   ``Caddyfile`` 的 ``handle_path /artifacts/*`` 递出去 —— 与照片、监理文书同一条出口。
   在这里再实现一遍下载 = 第二条产物出口,而那条链的每一环
   (``NEXT_PUBLIC_ARTIFACT_BASE`` → ARG → compose → Caddy)都已经在 CLAUDE.md
   的同源清单里登记过,不该有第二套。
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from pathlib import Path
from typing import Any, Final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.config import get_settings
from gyt.core.access import _api_key_from_headers, effective_access_token
from gyt.core.artifacts import ArtifactKind
from gyt.core.errors import Envelope, ErrorCode, fail, ok

logger = logging.getLogger(__name__)

_SIDECAR_GLOB: Final[str] = "*.json"
"""日期目录里 sidecar 的样子。形状与下面读的字段(``kind`` / ``original_name`` /
``created_at`` / ``size_bytes``)都以 ``core/artifacts.register()`` 写入的为准 ——
那边改存法,这里跟着改(与 ``attendance/cleanup.py`` 同一条约定,两边注释互指)。"""

_DEFAULT_LIMIT: Final[int] = 20
"""默认列几条。抽屉里一屏能看完的量;要更多的人自己传 limit。"""

_MAX_LIMIT: Final[int] = 200
"""上限。挡的是 ``?limit=999999`` 把整个产物目录扫穿。"""

_MAX_SIDECARS_SCANNED: Final[int] = 2000
"""一次请求最多读多少份 sidecar。

🔴 **这个数是这条端点唯一的性能保险。** 线上产物目录里绝大多数是照片(每次上传一份),
巡检记录只占极小比例 —— 也就是说"找 20 份记录"可能要翻过几百上千份照片的 sidecar。
不设上限的话,一个跑了半年的站点上点开这个抽屉会读上万个小文件。

取 2000 的依据:按线上一天几十次上传估,2000 份大约覆盖最近一两个月,
足够翻出最近 20 份巡检记录;而 2000 次 ``read_text`` 在 SSD 上是几十毫秒量级。
扫到这儿还没凑够就如实回 ``scan_truncated``,**不假装"就这么多"**。
"""

_REPORT_NO_RE: Final[re.Pattern[str]] = re.compile(r"(GYT-\d{8}-\d{6})")
"""从文件名里抠巡检记录编号。**它同时是这条端点的过滤判据**,见下面那段红字。

⚠️ **与 ``agents/report/tools.py`` 的 ``strftime("GYT-%Y%m%d-%H%M%S")`` 同源** ——
那边改了编号长相,这里就一份都列不出来(而不是「编号那格空着」)。
CLAUDE.md 的同源清单里「巡检记录编号长相」那一行已经登记了它的另外两个同源点
(生成器 + ``REPORT_RECEIPT_PATTERN``),这是第三处。

===========================================================================
🔴 为什么过滤判据不能只是 ``kind == REPORT``(2026-08-22 真机照出来的)
---------------------------------------------------------------------------
``ArtifactKind.REPORT`` 这一档**不只装巡检记录** —— ``supervision_api`` 签发
五种监理文书时用的是**同一个 kind**(那一行在 ``_sign`` 里)。于是只按 kind 过滤的话,
《工程暂停令》《监理通知单》《致建设单位报告》《工程复工令》会全部出现在
工友的「巡檢記錄」抽屉里。

本机实测(2026-08-22,真跑起来的容器 + 真历史数据):
    kind=REPORT 共 39 份
      · 巡检记录        11 份  ← 该列的
      · 监理通知单       11 份  ┐
      · 致建设单位报告     6 份  ├ 全是监理文书,**共 28 份,占 72%**
      · 工程暂停令       6 份  │
      · 工程复工令       5 份  ┘
而它们在界面上还都显示「(无编号)」—— 因为解不出巡检记录号。

**判据换成「文件名里有没有一个巡检记录号」**,而这个判别信号本仓早就有并且守着:
六种监理编号**都带类型段**(``GYT-ZT-`` / ``GYT-TZ-`` / ``GYT-JS-`` …),
巡检记录号**不带** —— ``REPORT_RECEIPT_PATTERN`` 与 ``SUPERVISION_RECEIPT_PATTERN``
故意互不匹配就是这条(CLAUDE.md 同源清单里有专门一行)。
上面那 39 份实测:11 份解得出、28 份解不出,**100% 干净分离**,历史数据也一样。

⚠️ **根因没修,只是绕开了**:``kind=REPORT`` 被两条链共用这件事还在。
哪天有人写第三个「列 REPORT 产物」的地方,会原样再踩一次。已记 TODO-56。
"""

_EMPTY_MSG: Final[str] = "还没有巡检记录。工地上拍张照发给我,我看完就给你出一份。"
_DENIED_LOG: Final[str] = "巡检记录接口鉴权失败:path=%s 原因=%s"


def _respond(env: Envelope, status: int) -> JSONResponse:
    """Envelope → JSON 响应。进了 handler 之后所有出口都走这里,一个都不许裸拼 dict。"""
    return JSONResponse(env, status_code=status)


def _deny_if_token_bad(request: Request) -> JSONResponse | None:
    """handler 自查令牌。放行返回 None,不过返回 401。

    与另外三个直连接口**同源同判据**(同一份 ``core/access.py``),连日志措辞都对齐。
    第一道在 langgraph 的鉴权中间件(``langgraph.json`` 的 ``enable_custom_route_auth``);
    这道防的是那个键被漏配 —— 漏配时第一道**整条消失且没有任何报错**。

    🔴 **这条端点尤其不能漏**:回执里是工地巡检记录的文件名与编号,
    而编号能直接换出那份 docx(里面有隐患明细、有现场照片)。

    空 / 占位符 / 过短 = 未配置 = 放行:``make dev`` 与真机验收不发令牌头。
    ⚠️ 每次请求现取,不许在 import 时读成常量。
    """
    expected = effective_access_token()
    if not expected:
        return None
    presented = _api_key_from_headers(request.headers)
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return None
    logger.warning(
        _DENIED_LOG,
        request.url.path,
        "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
    )
    return _respond(fail(ErrorCode.UNAUTHORIZED), 401)


def _parse_limit(raw: str | None) -> int | None:
    """条数解析。合法给 int,不合法给 None(调用方据此回 400)。

    ⚠️ **超上限是截断而不是报错**(与 ``supervision_api._issued_by`` 超长截断同一取舍):
    这是个只读列表,为一个过大的数字挡住整页内容不划算。而"传了个解析不出来的东西"
    是另一回事 —— 那时候静默当默认值会让人以为自己传的数生效了。
    """
    if raw is None or raw == "":
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, _MAX_LIMIT)


def _report_no_of(original_name: str) -> str | None:
    """从 ``巡检记录_GYT-20260822-153012.docx`` 里抠出编号。抠不出返回 **None**。

    **返回 None = 这份产物不是巡检记录**,调用方据此把它整个跳过(见 ``_scan_reports``)。
    在 2026-08-22 之前它只是个显示字段(抠不出就让那一格空着),而那正是
    28 份监理文书混进「巡檢記錄」抽屉的原因 —— 完整实测在 ``_REPORT_NO_RE`` 上面。

    🔴 **不许在这儿编一个编号**(比如拿 artifact_id 前八位凑一个)。
    编号是要被人报给别人、写进留档的东西 —— 一个长得像编号但对不上任何文档的串,
    比一格空白坏得多;而**比空白更坏的是它会让一份根本不是巡检记录的东西看起来像**。
    """
    found = _REPORT_NO_RE.search(original_name or "")
    return found.group(1) if found else None


def _scan_reports(limit: int) -> tuple[list[dict[str, Any]], bool]:
    """按日期目录从新往旧扫产物 sidecar,收够 ``limit`` 份巡检记录就停。

    返回 ``(记录列表, 是否扫到上限还没扫完)``。

    ⚠️ **目录名是 ``YYYYMMDD``,字典序即时间序**,所以 ``sorted(..., reverse=True)``
       就是"从新往旧"。这一点靠的是 ``core/artifacts.register`` 里那句
       ``datetime.now(UTC).strftime("%Y%m%d")`` —— 它哪天换成带分隔符的格式,
       排序会**静默错**(列表顺序乱掉,而不会报错)。

    ⚠️ 读不出 / 不是 dict / kind 不对的 sidecar 一律**跳过而不是抛** ——
       一份坏掉的元数据不该让整个抽屉打不开(同 ``cleanup.py``:宁可漏,不可炸)。
       但它照样计入扫描预算:坏文件也是要读的 IO。
    """
    artifacts_dir = get_settings().artifacts_dir
    if not artifacts_dir.is_dir():
        # 一次巡检记录都还没出过时,这个目录根本不存在 —— 那不是错误。
        return [], False

    report_kind = ArtifactKind.REPORT.value
    collected: list[dict[str, Any]] = []
    scanned = 0

    for day_dir in sorted(
        (p for p in artifacts_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
    ):
        for sidecar in sorted(day_dir.glob(_SIDECAR_GLOB), reverse=True):
            if scanned >= _MAX_SIDECARS_SCANNED:
                return collected, True
            scanned += 1
            meta = _read_meta(sidecar)
            if meta is None or meta.get("kind") != report_kind:
                continue
            # 🔴 第二道判据:必须解得出**巡检记录号**。只看 kind 的话,监理那五种文书
            #    会全部混进来(它们用的是同一个 kind)—— 整段推演在 _REPORT_NO_RE 上面。
            report_no = _report_no_of(str(meta.get("original_name", "")))
            if report_no is None:
                continue
            collected.append(_report_payload(sidecar.stem, meta, report_no))
            if len(collected) >= limit:
                return collected, False

    return collected, False


def _read_meta(sidecar: Path) -> dict[str, Any] | None:
    """读一份 sidecar。读不出 / 不是 dict 都返回 None,**不抛**(理由见 ``_scan_reports``)。"""
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _report_payload(artifact_id: str, meta: dict[str, Any], report_no: str) -> dict[str, Any]:
    """一份巡检记录的对外形状(五个键,契约见模块头注)。

    ``report_no`` **由调用方传进来**,不在这儿重算一遍:它是 ``_scan_reports`` 的
    过滤判据,算两遍就有两份判据 —— 而那种漂移的表现是「过滤时认作巡检记录、
    显示时又说没有编号」。

    ⚠️ ``artifact_id`` 取的是 **sidecar 的文件名**,不是 ``meta["id"]``:
       文件名是路径上真实存在的那个(``resolve`` 与 ``/by-id/`` 都按它找正文),
       而 ``meta["id"]`` 是写进去的一份拷贝。两者理论上恒等,但真不等的那天,
       按文件名走的那个才拿得到文件。
    """
    return {
        "artifact_id": artifact_id,
        "report_no": report_no,
        "filename": str(meta.get("original_name", "")),
        "size_bytes": meta.get("size_bytes"),
        "created_at": meta.get("created_at"),
    }


def _user_msg(count: int, truncated: bool) -> str:
    """列表上方那句话。它要答的是「有几份、能不能点开」,不是「接口调用成功」。

    🔴 **``truncated`` 必须排在 ``count == 0`` 前面。** 这两个条件同时成立
    (扫描预算在照片上耗光、一份记录都没翻到)是最坏的一格:那时候说
    「还没有巡检记录」是**一句谎**,而人会据此以为自己那份记录丢了、
    或者干脆以为这个功能坏了。真相是"没扫到",两件事差得很远。
    写这个函数时第一版就把空态判在了前面,是这一组用例把它逼出来的。
    """
    if truncated and not count:
        return (
            "最近这一批产物里没翻到巡检记录(只列了最近这些,更早的没往下找)。"
            "要找更早的记录,把编号报给我。"
        )
    if not count:
        return _EMPTY_MSG
    tail = "(只列了最近这些)" if truncated else ""
    return f"这里有 {count} 份巡检记录{tail},点一下就能下载存档或转发。"


async def get_reports(request: Request) -> JSONResponse:
    """列出最近的巡检记录。

    🔴 **令牌自查排在取参数之前**,与另外三个直连接口同一条规矩。
    ⚠️ 目录遍历与文件读取整个挪进线程池:这是货真价实的阻塞 IO
       (最多 ``_MAX_SIDECARS_SCANNED`` 次 ``read_text``),留在事件循环里
       会把整个进程卡住,而现象是"别的请求也一起慢",最难查的那一类。
    """
    denied = _deny_if_token_bad(request)
    if denied is not None:
        return denied

    limit = _parse_limit(request.query_params.get("limit"))
    if limit is None:
        return _respond(
            fail(
                ErrorCode.INVALID_INPUT,
                "这一下没读懂,刷新一下再点一次。",
                detail=f"limit 不是正整数:{request.query_params.get('limit')!r}",
            ),
            400,
        )

    try:
        reports, truncated = await run_in_threadpool(_scan_reports, limit)
    except Exception:
        # 兜底:抽屉打不开绝不能连累别的。堆栈只进日志。
        logger.exception("扫描巡检记录失败")
        return _respond(fail(ErrorCode.INTERNAL), 500)

    return _respond(
        ok(
            data={"reports": reports, "total": len(reports), "scan_truncated": truncated},
            user_msg=_user_msg(len(reports), truncated),
        ),
        200,
    )


REPORTS_ROUTES: Final[list[Route]] = [
    Route("/reports", get_reports, methods=["GET"]),
]
"""本模块对外的全部路由 —— **一条,只读**。

由 ``webapp.py`` 铺进 Starlette 的 ``routes=[...]``。删掉那一铺 = 抽屉里永远是空的,
而现象是 **404,不是启动报错**(前端对非 2xx 是安静走开的,一行都不出)。

公网那一侧不用动 ``Caddyfile``:它落在 ``handle_path /api/*`` 那条通用规则下面
(与 ``/timing`` 同一处),而且是 GET + 小响应,不碰任何请求体大小闸。
"""

__all__ = ["REPORTS_ROUTES", "get_reports"]
