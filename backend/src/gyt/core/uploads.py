"""把聊天界面里**直接上传的图片**接进产物注册表。

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

from gyt.config import ALLOWED_IMAGE_EXT
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind

logger = logging.getLogger(__name__)

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


def _decode_image_part(part: dict[str, Any]) -> tuple[bytes, str] | None:
    """从一个 image_url content 块里取出 (字节, 扩展名)。取不出来返回 None。

    只认 base64 data URI —— 月之暗面本来也只收这个(公网 http/https 图片链接不支持)。
    """
    # image content 块在野外有三种写法,三种都要接住 —— 少认一种的表现是
    # 「图片被静默丢掉,用户等一个永远不来的答复」:
    #   {"image_url": {"url": "data:..."}}   OpenAI 兼容格式,agent-chat-ui 用的就是它
    #   {"image_url": "data:..."}            LangChain 也接受的简写
    #   {"url": "data:..."}                  少数客户端把 url 提到了顶层
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
    ids: list[str] = []
    rejected = 0
    for part in message.content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind in ("image_url", "image"):
            decoded = _decode_image_part(part)
            if decoded is None:
                rejected += 1
                continue
            payload, ext = decoded
            ids.append(
                artifacts.register(payload, kind=ArtifactKind.PHOTO, original_name=f"upload{ext}")
            )

    if not ids and not rejected:
        return None  # 没有图片,原样放行

    body = " ".join(t.strip() for t in texts if t.strip())
    if ids:
        listed = "、".join(ids)
        body = f"{body}\n(照片编号:{listed})" if body else f"看看这张照片。(照片编号:{listed})"
        logger.info("已把 %d 张上传图片登记为产物:%s", len(ids), listed)
    if rejected:
        body = f"{body} {_UNSUPPORTED_HINT}"

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
