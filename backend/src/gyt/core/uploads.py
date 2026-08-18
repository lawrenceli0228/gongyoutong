"""把聊天界面里**直接上传的图片 / DXF 图纸**接进产物注册表。

（图纸是方案 A:前端 override 后会把 .dxf 作为 file 块发上来,这里认出来登记成 DRAWING 产物,
把消息改写成带「图纸编号」的纯文本,cad Agent 按编号查图。DXF 一律按 .dxf 后缀认,不信 MIME。
下面的说明以图片为主线,图纸走的是同一条「登记产物→消息里只留编号」的路子。）

===========================================================================
为什么需要这一层
---------------------------------------------------------------------------
agent-chat-ui 的输入框上有一个「Upload PDF or Image」按钮,用户点它传图之后,
图片是以**多模态 content 块**的形式进消息的:

    HumanMessage(content=[
        {"type": "text", "text": "查安全隐患"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
    ])

而 Supervisor 与所有子 Agent 走的都是 DeepSeek 文本档(便宜),**它不支持视觉**——
这样一条消息发过去,上游直接 400:

    {"__error__":{"error":"BadRequestError","message":"An internal error occurred"}}

前端不会把这个错渲染出来,用户看到的是"点了发送但没反应",极难自查。

之前的设计假设用户会自己把照片注册成产物、再把 32 位编号粘进对话 ——
那对开发者尚可,对演示是灾难:评委一定会去点那个上传按钮,不会去复制十六进制串。

===========================================================================
这层做什么
---------------------------------------------------------------------------
在 Supervisor 调模型**之前**拦一道(create_supervisor 的 pre_model_hook):

    上传的图片 → artifacts.register(...) → 把 content 块换成一句带编号的文本

    HumanMessage([text, image_url])  →  HumanMessage("查安全隐患\\n(照片编号:<32位>)")

于是:
  · 文本档模型再也见不到 image content,不会 400;
  · Safety Agent 拿到的正是它要的 artifact_id,链路原样跑通;
  · 图片只存一次进磁盘,**不会**在每轮对话里被 base64 重发(这正是把视觉调用
    放进工具的初衷,见 agents/safety/tools.py 顶部)。

**替换是永久的**(用 RemoveMessage 换掉原消息),不是只改这一次模型输入 ——
因为子 Agent 与 Supervisor 共享同一份 messages,只改模型输入的话
子 Agent 那边照样会拿到 image 块再炸一次。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any, Final

from langchain_core.messages import HumanMessage, RemoveMessage

from gyt.config import ALLOWED_IMAGE_EXT, get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind

logger = logging.getLogger(__name__)

_BYTES_PER_MB: Final[int] = 1024 * 1024

_DATA_URI_RE: Final[re.Pattern[str]] = re.compile(
    r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<payload>.+)$", re.IGNORECASE | re.DOTALL
)

EXT_BY_MIME: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
"""MIME → 落盘扩展名。**取值必须落在 config.ALLOWED_IMAGE_EXT 里**,
否则 artifacts._safe_ext 会把扩展名剥成空串、文件落盘时没有后缀,
而 Safety 工具是按后缀判格式的 —— 那会变成一条「上传成功但永远看不了」的静默死路。
文件末尾有 import 期断言守着这件事。"""

_UNSUPPORTED_HINT: Final[str] = "(这张图的格式暂时打不开,请转成 JPG 或 PNG 再传一次)"

_PDF_HINT: Final[str] = (
    "(你传的是 PDF。聊天窗口当场看图暂时只认 DXF。"
    "如果这是**图纸**,请用右上角「📂 资料归档」面板上传 —— 那里图纸支持 PDF 和 DXF,"
    "归档进去后就能查图层/构件、出预览、读图上文字。"
    "如果 PDF 里其实是现场照片,麻烦先截个图再传)"
)

_DRAWING_TOO_LARGE_HINT: Final[str] = "(你传的图纸太大了,先精简一下再传一次)"

_PHOTO_TOO_LARGE_HINT: Final[str] = "(你传的照片太大了,压缩一下或者截个图再传一次)"


def _decode_dxf_part(part: dict[str, Any]) -> tuple[bytes, str] | None:
    """从一个上传附件块里认出 DXF 图纸,取出 (字节, 落盘用文件名)。不是 DXF 返回 None。

    **一律按文件名 `.dxf` 结尾判,不信 MIME** —— 浏览器给 .dxf 的类型极不稳定
    (常是空串或 application/octet-stream,偶尔才 image/vnd.dxf)。前端(override 后)发的是
    LangChain 的 file 块,与 PDF 同形:

        {"type": "file", "mimeType": "image/vnd.dxf", "data": "<裸base64>",
         "metadata": {"filename": "首层平面图.dxf"}}

    data 是**不带** "data:;base64," 前缀的裸 base64(前端 fileToBase64 已剥掉前缀)。
    落盘文件名保留原名(带 .dxf),让 artifacts._safe_ext 能取到 .dxf(白名单已含 ALLOWED_CAD_EXT);
    万一没拿到文件名就兜底成 upload.dxf,保证扩展名在。
    """
    meta = part.get("metadata") or {}
    filename = str(meta.get("filename") or meta.get("name") or "")
    mime = str(part.get("mimeType") or part.get("mime_type") or "").lower()
    if not (filename.lower().endswith(".dxf") or "dxf" in mime):
        return None

    data = part.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        payload = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("上传图纸的 base64 解不开,已忽略")
        return None
    name = filename if filename.lower().endswith(".dxf") else "upload.dxf"
    return payload, name


def _decode_image_part(part: dict[str, Any]) -> tuple[bytes, str] | None:
    """从一个图片 content 块里取出 (字节, 扩展名)。取不出来返回 None。

    **图片块在野外有两大类写法,都必须接住** —— 少认一种的表现是
    「图片被静默丢掉,用户看到『请转成 JPG 或 PNG』然后一脸茫然」,
    而他传的本来就是 JPG。(这个坑真踩过:我照 OpenAI 的形状写,
    而 agent-chat-ui 发的是 LangChain 的形状,两者一个字段都对不上。)

    甲、LangChain content block(**agent-chat-ui 用的就是这个**,
        见 frontend/src/lib/multimodal-utils.ts):

            {"type": "image", "mimeType": "image/jpeg", "data": "<纯 base64>"}

        注意 ① 字段叫 data 而不是 url;② base64 **不带** "data:...;base64," 前缀;
        ③ MIME 的键是驼峰 mimeType(LangChain 自己序列化时又会写成 mime_type,
        所以两种都认)。

    乙、OpenAI 兼容格式:

            {"image_url": {"url": "data:image/jpeg;base64,..."}}
            {"image_url": "data:..."}   /   {"url": "data:..."}
    """
    # 甲:裸 base64 + 单独的 MIME 字段
    data = part.get("data")
    if isinstance(data, str) and data:
        mime = part.get("mimeType") or part.get("mime_type") or ""
        ext = EXT_BY_MIME.get(str(mime).lower())
        if ext is None:
            return None
        try:
            return base64.b64decode(data, validate=True), ext
        except (binascii.Error, ValueError):
            logger.warning("上传图片的 base64 解不开(mime=%s),已忽略", mime)
            return None

    # 乙:data URI
    raw = part.get("image_url")
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str):
        url = part.get("url")
    if not isinstance(url, str):
        return None
    matched = _DATA_URI_RE.match(url.strip())
    if not matched:
        return None
    ext = EXT_BY_MIME.get(matched.group("mime").lower())
    if ext is None:
        return None
    try:
        return base64.b64decode(matched.group("payload"), validate=True), ext
    except (binascii.Error, ValueError):
        logger.warning("上传的图片 base64 解不开,已忽略")
        return None


def _rewrite(message: HumanMessage) -> HumanMessage | None:
    """把一条含图片的用户消息改写成纯文本。没有图片就返回 None(表示不用动)。"""
    if not isinstance(message.content, list):
        return None

    texts: list[str] = []
    photo_ids: list[str] = []
    drawing_ids: list[str] = []
    rejected = 0
    pdf_rejected = 0
    oversized = 0
    photo_oversized = 0
    settings = get_settings()
    drawing_limit = int(settings.drawing_max_mb * _BYTES_PER_MB)
    photo_limit = int(settings.photo_max_mb * _BYTES_PER_MB)
    for part in message.content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "file":
            # file 块可能是 DXF 图纸(方案 A),也可能是 PDF。先按文件名认 DXF ——
            # DXF 的 MIME 不可靠,只信 .dxf 后缀(见 _decode_dxf_part)。
            dxf = _decode_dxf_part(part)
            if dxf is not None:
                payload, filename = dxf
                if len(payload) > drawing_limit:
                    # 在入口就挡超限,别等到 cad 查询才 FILE_TOO_LARGE(体验差)。
                    oversized += 1
                else:
                    drawing_ids.append(
                        artifacts.register(
                            payload, kind=ArtifactKind.DRAWING, original_name=filename
                        )
                    )
            elif str(part.get("mimeType") or "").lower() == "application/pdf":
                # 上传按钮允许 PDF,那是 knowledge Agent 的活。
                # ⚠️ 这行以前写「队友泳道,还没接」—— 2026-08-11 已推翻:knowledge
                # 2026-08-09(9b120be)就挂进 AGENT_REGISTRY 了。**真正没接的是这一段线**:
                # ingest.py 只从 `demo_assets_dir` 下预置的规范建库,没有「用户上传的 PDF
                # → 增量入库」这条路。所以这里仍然只能拒,但理由不是「Agent 没写」。
                # 照旧说法改代码的人会以为删掉这个分支就行 —— 那会让上传的 PDF 静默丢掉。
                # 给一句**准确**的话 —— 说"请转成 JPG"是错的,传 PDF 本就是按钮允许的。
                pdf_rejected += 1
        elif kind in ("image_url", "image"):
            decoded = _decode_image_part(part)
            if decoded is None:
                rejected += 1
                continue
            payload, ext = decoded
            if len(payload) > photo_limit:
                # 与图纸同一姿势在入口就挡(2026-08-15 补的现存 bug,W7 §1.10:
                # 此前只有 drawing 查了上限,照片分支裸奔 —— 超大图会被原样登记落盘,
                # 直到 Safety 工具解码才在链路深处翻车)。
                photo_oversized += 1
                continue
            photo_ids.append(
                artifacts.register(payload, kind=ArtifactKind.PHOTO, original_name=f"upload{ext}")
            )

    if (
        not photo_ids
        and not drawing_ids
        and not rejected
        and not pdf_rejected
        and not oversized
        and not photo_oversized
    ):
        return None  # 没有附件,原样放行

    body = " ".join(t.strip() for t in texts if t.strip())
    if photo_ids:
        listed = "、".join(photo_ids)
        body = f"{body}\n(照片编号:{listed})" if body else f"看看这张照片。(照片编号:{listed})"
        logger.info("已把 %d 张上传图片登记为产物:%s", len(photo_ids), listed)
    if drawing_ids:
        # 图纸编号与照片编号分开 —— cad/prompt.md 按「图纸编号」这个词把 id 传给工具,
        # 别和照片编号串(safety 认照片编号)。
        listed = "、".join(drawing_ids)
        body = f"{body}\n(图纸编号:{listed})" if body else f"看看这张图纸。(图纸编号:{listed})"
        logger.info("已把 %d 张上传图纸登记为产物:%s", len(drawing_ids), listed)
    if rejected:
        body = f"{body} {_UNSUPPORTED_HINT}"
    if pdf_rejected:
        body = f"{body} {_PDF_HINT}"
    if oversized:
        body = f"{body} {_DRAWING_TOO_LARGE_HINT}"
    if photo_oversized:
        body = f"{body} {_PHOTO_TOO_LARGE_HINT}"

    return HumanMessage(content=body, id=message.id)


def ingest_uploads(state: dict[str, Any]) -> dict[str, Any]:
    """Supervisor 的 pre_model_hook:把上传的图片换成产物编号。

    返回空 dict 表示什么都不用改 —— 绝大多数轮次都会走这条路(用户只是打字)。

    只处理**最后一条**用户消息:更早的那些在它们自己那一轮已经被改写过了,
    重复处理会把同一张图反复登记,artifacts 目录白白膨胀。
    """
    messages = state.get("messages") or []
    if not messages:
        return {}
    last = messages[-1]
    if not isinstance(last, HumanMessage):
        return {}

    rewritten = _rewrite(last)
    if rewritten is None:
        return {}

    # 必须**永久**换掉,不能只改这一次的模型输入:子 Agent 与 Supervisor 共享
    # 同一份 messages,只改模型输入的话,子 Agent 那边照样会拿到 image 块再炸一次。
    return {"messages": [RemoveMessage(id=last.id), rewritten]}


_MISSING = set(EXT_BY_MIME.values()) - ALLOWED_IMAGE_EXT
if _MISSING:  # pragma: no cover —— 配置写错才会走到
    raise RuntimeError(
        f"EXT_BY_MIME 里的 {sorted(_MISSING)} 不在 config.ALLOWED_IMAGE_EXT 中。"
        "上传的图片会落盘成无扩展名的文件,Safety 工具按后缀判格式,将永远看不了它。"
    )

__all__ = ["EXT_BY_MIME", "ingest_uploads"]
