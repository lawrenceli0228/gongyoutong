"""聊天附件的直传接口 —— 让照片/图纸**不再以 base64 进对话状态**。

本模块导出 ``UPLOAD_ROUTES``,由 webapp.py 铺进它的 ``routes=[...]``
(与 checkin / supervision / timing 同一条路,``langgraph.json`` 的 ``http.app`` 只能有一个)。

===========================================================================
🔴 为什么要这条路(2026-08-21 线上量出来的)
---------------------------------------------------------------------------
老路是「base64 随消息发上来 → ``core/uploads.py`` 在 pre_model_hook 里改写成编号」。
改写本身是对的,但它有个**追不回来的时间差**:改写在第 9 步,而那条带 base64 的
``HumanMessage`` **第 8 步就已经提交进检查点**了。``RemoveMessage`` 改得了以后的状态,
改不掉已经落盘的那一份 —— 于是每张照片都在某个检查点里留一份**永久**拷贝。

线上实测(2 核 / 1966 MB 的机器):

    11 条会话      → langgraph 内存库 **1.1 GB**(磁盘 pickle 234 MB)
    可用内存 91 MB,swap 已吞 1.3 GB
    而 langgraph 每 10 秒**无条件全量** pickle 一遍(没有脏标记)

后果不是「历史下得慢」,是**整个后端被它自己的内存库拖住**:

    langgraph 的 /state,一条**不存在**的会话(零载荷 404)   12.7 s
    我们自己的 /timing(同一个进程、同一个 app)              0.25 s

===========================================================================
请求 / 响应契约(**唯一真相在这儿**)
---------------------------------------------------------------------------
    POST /attachments?name=<URL 编码的原文件名>
    Content-Type: <浏览器给的 MIME>
    <请求体 = 原始字节,不是 multipart、不是 base64>

    200 {"ok": true,
         "data": {"outcome": "photo", "artifact_id": "<32位hex>"},
         "user_msg": "", "error_code": null}

        outcome    六选一,**唯一真相是 core/uploads.ATTACHMENT_OUTCOMES**
        artifact_id 只有 photo / drawing 才非空;其余四种是 null

    401 令牌不过        413 请求体超过硬上限        500 兜底

⚠️ **「这张附件不能用」不是 HTTP 错误**(格式不支持 / 是 PDF / 太大),一律 200 +
   一个 outcome。理由:那几句给用户看的话**只有一份真相,在 ``core/uploads.py``**
   (`_PDF_HINT` 那几条),前端不该也不必抄一遍。前端拿到 outcome 原样塞进消息块,
   由 ``uploads._rewrite`` 拼成人话 —— 与老路殊途同归到同一批文案。
   另一个理由更硬:`lang-lib.ts` 的 `userTypedText()` 靠数那几句里的简体字判语种
   (`_PDF_HINT` 一句就投 28 张票),把拼装挪到前端会让那条判据**静默失效**。

⚠️ **为什么用 raw body 而不是 multipart**:multipart 要把整个请求解析完才知道多大,
   而 ``Request.stream()`` 是真流式,读到哪算到哪 —— 超限当场断,不往内存里攒。
   判据整套抄自 ``checkin_api._read_photo`` / ``supervision_api._raw_body``。
   文件名走**查询串**(URL 编码)而不是自定义头:中文名在查询串里是标准行为,
   而自定义头得再约一层 Base64URL(打卡那边就是那么干的,这里不必再欠一份复杂度)。
"""

from __future__ import annotations

import hmac
import logging
from typing import Final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.access import _api_key_from_headers, effective_access_token
from gyt.core.artifacts import ArtifactKind
from gyt.core.errors import Envelope, ErrorCode, fail, ok
from gyt.core.uploads import (
    OUTCOME_DRAWING,
    OUTCOME_PHOTO,
    classify_attachment,
)

logger = logging.getLogger(__name__)

_BYTES_PER_MB: Final[int] = 1024 * 1024

_KIND_BY_OUTCOME: Final[dict[str, ArtifactKind]] = {
    OUTCOME_PHOTO: ArtifactKind.PHOTO,
    OUTCOME_DRAWING: ArtifactKind.DRAWING,
}
"""哪两种结局要真落盘。其余四种是「这张不能用」,压根没有产物。"""

_HARD_LIMIT_HEADROOM: Final[float] = 1.25
"""硬上限相对业务上限留的余量。

🔴 **不留余量的话,`drawing_too_large` 这个结局在这条端点上永远产不出来**
(2026-08-21 测试阶段抓到的):硬上限 = ``max(照片, 图纸)`` = 图纸那条,
于是一张超过图纸上限的 DXF **必然先撞 413**,而 413 只能给一句通用的话 ——
它说不出「这是图纸,先精简一下」,那句对图纸才是有用的指路。

留 25% 之后,「超了业务上限但还没离谱」的那一档能走完整流程:200 + 一个结局 →
``uploads._rewrite`` 拼出**对症**的那句提示。真正离谱的(125 MB 以上)才吃 413,
而那时候「说不清是什么格式」也无所谓了。

⚠️ 这个数只是**内存保护**的边界,不是业务判据 —— 业务上限在 ``config.py``,
   由 ``classify_attachment`` 判。别把两者合并:合并的下场就是上面那句话。
"""

_TOO_LARGE_MSG: Final[str] = "你传的文件太大了,先精简一下再传一次。"
"""413 的人话。

⚠️ **刻意不写「截个图」** —— 这条 413 是格式无关的(照片、图纸、PDF 都可能撞到),
   而「截个图」对一张 DXF 图纸是句错的指路。分格式的那几句在
   ``core/uploads.py``(``_PHOTO_TOO_LARGE_HINT`` / ``_DRAWING_TOO_LARGE_HINT``),
   走的是 200 + outcome 那条路 —— 见上面 ``_HARD_LIMIT_HEADROOM``。
"""

_UPLOAD_FAILED_MSG: Final[str] = "这张图没传上去,再试一次;还不行就换张图。"


def _respond(env: Envelope, status: int) -> JSONResponse:
    """Envelope → JSON 响应。进了 handler 之后所有出口都走这里,一个都不许裸拼 dict。"""
    return JSONResponse(env, status_code=status)


def _deny_if_token_bad(request: Request) -> JSONResponse | None:
    """handler 自查令牌。放行返回 None,不过返回 401。

    与 ``checkin_api`` / ``supervision_api`` / ``timing_api`` 那三处**同源同判据**
    (同一份 ``core/access.py``)。第一道在 langgraph 的鉴权中间件
    (``langgraph.json`` 的 ``enable_custom_route_auth``);这道防的是那个键被漏配 ——
    漏配时第一道**整条消失且没有任何报错**。

    🔴 这条端点比另外三条更需要它:**它会往磁盘写文件**。没钥匙的人能往这台机器上
    写 100 MB 一份的东西,那不是「少一道防线」,那是一个免费的填盘器。
    """
    expected = effective_access_token()
    if not expected:
        return None
    presented = _api_key_from_headers(request.headers)
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return None
    logger.warning(
        "附件直传鉴权失败:path=%s 原因=%s",
        request.url.path,
        "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
    )
    return _respond(fail(ErrorCode.UNAUTHORIZED), 401)


def _hard_limit_bytes() -> int:
    """请求体的**硬上限** = 两个业务上限里较大的那个,**再留 25% 余量**。

    🔴 这道闸只管「别把内存撑爆」,**不管「这张能不能用」** —— 后者由
    ``classify_attachment`` 按类型各自判(照片 10 MB / 图纸 100 MB),而且它给的是
    一个 outcome、不是 HTTP 错误。两道分开是刻意的:一张 30 MB 的照片该收下来、
    然后如实告诉工友「照片太大了」,而不是被这道闸 413 掉 —— 413 到了前端只剩
    一个数字,那句对症的人话就没了。

    🔴 **那 25% 余量不是随手加的**,理由整段写在 ``_HARD_LIMIT_HEADROOM`` 上:
       不留的话硬上限恰好等于图纸上限,``drawing_too_large`` 这一档
       **永远轮不到**,超限的图纸一律吃一句格式无关的 413。

    ⚠️ **每次请求现取**,不许读成模块常量:测试换环境变量测不到,生产改上限要重启才生效。
    """
    settings = get_settings()
    largest = max(settings.photo_max_mb, settings.drawing_max_mb)
    return int(largest * _HARD_LIMIT_HEADROOM * _BYTES_PER_MB)


async def _read_raw_body(request: Request, limit: int) -> bytes | None:
    """边收边数的原始请求体。超上限返回 None(调用方回 413)。

    ``Request.stream()`` 是真流式(逐 ASGI 事件 yield,不预缓冲),所以超了就**当场**停,
    不会先把 500 MB 攒进内存再判断。
    """
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            logger.warning("附件直传超硬上限:已收 %d 字节,上限 %d", received, limit)
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def post_attachment(request: Request) -> JSONResponse:
    """收一个附件,登记成产物,回一个编号(或一个「不能用」的结局)。

    🔴 **令牌自查排在读请求体之前,不许调换** —— 读体就是把最多 100 MB 收进内存,
       放到鉴权之后才是对的。这条与 ``supervision_api._serve`` 那条同一个理由,
       但在这儿更要紧(见 ``_deny_if_token_bad``)。
    """
    denied = _deny_if_token_bad(request)
    if denied is not None:
        return denied

    limit = _hard_limit_bytes()
    try:
        payload = await _read_raw_body(request, limit)
    except Exception:
        logger.exception("附件直传读请求体失败")
        return _respond(fail(ErrorCode.INTERNAL, _UPLOAD_FAILED_MSG), 500)
    if payload is None:
        return _respond(
            fail(ErrorCode.FILE_TOO_LARGE, _TOO_LARGE_MSG, detail=f"请求体超过硬上限 {limit}"),
            413,
        )

    filename = (request.query_params.get("name") or "").strip()
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].strip()
    verdict = classify_attachment(len(payload), filename=filename, mime=mime)

    artifact_kind = _KIND_BY_OUTCOME.get(verdict.outcome)
    if artifact_kind is None or verdict.register_name is None:
        # 「这张不能用」——**200**,带着 outcome 回去,人话归 uploads._rewrite 拼
        # (理由见模块头注那条 ⚠️)。
        logger.info("附件直传:不收下(outcome=%s mime=%r name=%r)", verdict.outcome, mime, filename)
        return _respond(ok(data={"outcome": verdict.outcome, "artifact_id": None}), 200)

    try:
        # 落盘是阻塞 IO,一步都不许留在事件循环里 —— 这些路由和图跑在同一个循环上。
        artifact_id = await run_in_threadpool(
            artifacts.register, payload, kind=artifact_kind, original_name=verdict.register_name
        )
    except Exception:
        logger.exception("附件直传落盘失败:outcome=%s name=%r", verdict.outcome, filename)
        return _respond(fail(ErrorCode.INTERNAL, _UPLOAD_FAILED_MSG), 500)

    logger.info(
        "附件已直传登记:outcome=%s id=%s 字节=%d name=%r",
        verdict.outcome,
        artifact_id,
        len(payload),
        filename,
    )
    return _respond(ok(data={"outcome": verdict.outcome, "artifact_id": artifact_id}), 200)


UPLOAD_ROUTES: Final[list[Route]] = [
    Route("/attachments", post_attachment, methods=["POST"]),
]
"""本模块对外的全部路由 —— **一条**。

⚠️ 路径叫 ``/attachments`` 而不是 ``/uploads``:webapp.py 里已经有一批「资料归档」的
上传端点(``/docs``、``/projects/{id}/drawings``),那是**归档**;这条是**聊天附件**,
两者的去向、生命周期、谁能看都不一样。名字混了,下一个人会往错的那条加功能。
"""

__all__ = ["UPLOAD_ROUTES", "post_attachment"]
