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

🔴 **每轮扫的是全部用户消息,不是只扫最后一条**(2026-08-19 改)。
原来只扫最后一条,假设「更早的在它们自己那轮已经改写过了」——
**那条假设在 run 失败时不成立**(失败的 run 不提交检查点,改写就丢了),
后果是那条线程被永久毒化、此后每次提问都 400。完整实录见 `ingest_uploads` 头注。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any, Final, NamedTuple

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


# ---------------------------------------------------------------------------
# 直传:附件先换成编号,再进消息(2026-08-21)
# ---------------------------------------------------------------------------
#
# 🔴 **为什么要有这条路** —— 因为上面那条「base64 进消息、这一层再改写」有个**追不回来
#    的时间差**:改写发生在 `pre_model_hook`(第 9 步),而那条带 base64 的 HumanMessage
#    **第 8 步就已经提交进检查点了**。`RemoveMessage` 改得了以后的状态,改不掉已经落盘
#    的那一份 —— 于是每张照片都在某个检查点里留一份**永久**的 base64 拷贝。
#
#    2026-08-21 线上量到的代价(2 核 / 1966 MB 的机器):
#
#        11 条会话        →  langgraph 内存库 1.1 GB(磁盘 pickle 234 MB)
#        可用内存 91 MB,swap 已吞 1.3 GB
#        而 langgraph 每 10 秒**无条件全量** pickle 一遍(没有脏标记,
#        `langgraph_runtime_inmem/_persistence.py:17` + `:51-63`)
#
#    表现:一条**零载荷的 404** 也要 12.7 秒,而同进程里我们自己的 `/timing` 只要 0.25 秒。
#    也就是说这不是「历史太大下得慢」,是**整个后端被它自己的内存库拖住**。
#
# 修法:附件不再以字节进消息。前端先把它 POST 到 `timing`/`checkin` 同款的直连接口
# (`upload_api.py`),换回一个 32 位编号,消息里只带下面这种**编号块**:
#
#     {"type": "gyt_attachment", "outcome": "photo", "artifact_id": "<32位hex>"}
#
# ⚠️ **这一层仍然是拼人话的地方**,没有搬走:编号块进来之后,下面 `_rewrite` 照旧
#    拼「(照片编号:…)」和那几句提示。这么分是刻意的 ——
#    ① 提示文案只有一份真相(还在这儿),前端不用抄一遍;
#    ② `lang-lib.ts` 的 `userTypedText()` 靠「在第一个编号块处截断」判语种,
#       而它数的正是这儿拼出来的那几句(`_PDF_HINT` 一句就投 28 张简体票)——
#       把拼装搬到前端,那条判据会**静默失效**。
#
# ⚠️ 旧的 image / file 块**照旧接住,一行都没删**:老客户端、别的调用方、
#    以及直传接口挂掉时的兜底,都还走那条路。两条路殊途同归到同一批 `photo_ids`。

ATTACHMENT_BLOCK_TYPE: Final[str] = "gyt_attachment"
"""编号块的 `type`。**前端按这个名字发,改名等于改对外 API。**

用一个自造的 type(而不是复用 `text`)是为了让它**不可能**和用户自己打的字混淆:
用户可以打出「(照片编号:xxx)」这行字,但打不出一个 content 块。
"""

OUTCOME_PHOTO: Final[str] = "photo"
OUTCOME_DRAWING: Final[str] = "drawing"
OUTCOME_UNSUPPORTED: Final[str] = "unsupported"
OUTCOME_PDF: Final[str] = "pdf"
OUTCOME_PHOTO_TOO_LARGE: Final[str] = "photo_too_large"
OUTCOME_DRAWING_TOO_LARGE: Final[str] = "drawing_too_large"

ATTACHMENT_OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_PHOTO,
    OUTCOME_DRAWING,
    OUTCOME_UNSUPPORTED,
    OUTCOME_PDF,
    OUTCOME_PHOTO_TOO_LARGE,
    OUTCOME_DRAWING_TOO_LARGE,
)
"""一次直传的**全部**可能结局。**唯一真相在这儿**,`upload_api.py` 与前端都镜像它。

🔴 六个值与 `_rewrite` 里那六个计数桶**一一对应**。加一个值而不管那边,表现是
   那种附件被静默归进「格式不支持」—— 用户传了张好照片,却被告知请转成 JPG。
"""

_ARTIFACT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
"""编号的长相(`artifacts.register` 出的是 32 位小写 hex)。

🔴 **必须校验,因为这个值是客户端发上来的。** 不校验的话,谁都能往消息里塞一段
   任意文本冒充编号 —— 落地表现不是漏洞(`artifacts.resolve` 自己防路径穿越),
   而是**脏数据进对话**:模型看见一个假编号,拿它去调工具,然后如实报「找不到那张照片」,
   而工友明明刚传过。
"""


class Classified(NamedTuple):
    """一次直传的判定结果。``register_name`` 只有 photo / drawing 才有。"""

    outcome: str
    register_name: str | None


def classify_attachment(size: int, *, filename: str, mime: str) -> Classified:
    """判定一个直传上来的附件该归到哪一档。**纯函数,不碰磁盘。**

    🔴 **判据逐条对着 `_decode_dxf_part` / `_decode_image_part` / `_rewrite` 抄的**,
       两条路必须给出同一个结论 —— 否则同一张图「点上传按钮」和「走旧路」结果不同,
       而没有任何东西会说话。改这里记得回去看那三处。

    顺序也是照抄的,而且**不能换**:
      ① DXF 先判,且**只信文件名后缀 / mime 里带 dxf**(浏览器给 .dxf 的 MIME
         极不稳定,常是空串或 application/octet-stream);
      ② PDF 次之(它是按钮允许的格式,得给一句准确的话,不能说「请转成 JPG」);
      ③ 再按 mime 认图片;
      ④ 都不是 → 格式不支持。

    ``size`` 收的是**字节数**而不是字节本身:判定用不到内容,而图纸上限是 100 MB,
    多传一份进来纯属浪费。
    """
    settings = get_settings()
    name = filename.strip()
    lowered_mime = mime.strip().lower()

    if name.lower().endswith(".dxf") or "dxf" in lowered_mime:
        if size > int(settings.drawing_max_mb * _BYTES_PER_MB):
            return Classified(OUTCOME_DRAWING_TOO_LARGE, None)
        # 落盘名保留原名(带 .dxf),让 artifacts._safe_ext 取得到后缀;拿不到就兜底。
        return Classified(OUTCOME_DRAWING, name if name.lower().endswith(".dxf") else "upload.dxf")

    if lowered_mime == "application/pdf":
        return Classified(OUTCOME_PDF, None)

    ext = EXT_BY_MIME.get(lowered_mime)
    if ext is None:
        return Classified(OUTCOME_UNSUPPORTED, None)
    if size > int(settings.photo_max_mb * _BYTES_PER_MB):
        return Classified(OUTCOME_PHOTO_TOO_LARGE, None)
    return Classified(OUTCOME_PHOTO, f"upload{ext}")


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
        elif kind == ATTACHMENT_BLOCK_TYPE:
            # 直传那条路:附件早就在 upload_api 那儿登记好了,这里只收编号。
            # 六个结局一一落进下面那六个桶,与旧路殊途同归 —— 拼人话的活还在这层。
            outcome = str(part.get("outcome") or "")
            artifact_id = str(part.get("artifact_id") or "").strip().lower()
            if outcome in (OUTCOME_PHOTO, OUTCOME_DRAWING):
                if not _ARTIFACT_ID_RE.match(artifact_id):
                    # 客户端发来的编号长得不对 —— 当成「这张没传上去」处理,
                    # 而不是把一段来路不明的文本拼进对话(理由见 _ARTIFACT_ID_RE)。
                    logger.warning("直传编号块的 artifact_id 不合法,已按格式不支持处理")
                    rejected += 1
                elif outcome == OUTCOME_PHOTO:
                    photo_ids.append(artifact_id)
                else:
                    drawing_ids.append(artifact_id)
            elif outcome == OUTCOME_PDF:
                pdf_rejected += 1
            elif outcome == OUTCOME_DRAWING_TOO_LARGE:
                oversized += 1
            elif outcome == OUTCOME_PHOTO_TOO_LARGE:
                photo_oversized += 1
            else:
                # 认不出的 outcome 也走「格式不支持」——**不许静默丢掉**:
                # 静默丢的表现是用户传了东西、对话里一个字都没提,他会以为传上去了。
                if outcome not in ATTACHMENT_OUTCOMES:
                    logger.warning("直传编号块的 outcome 认不出:%r", outcome)
                rejected += 1
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

    ===========================================================================
    🔴 扫**全部**用户消息,不是只扫最后一条(2026-08-19 真机抓到)
    ---------------------------------------------------------------------------
    原来只处理最后一条,理由写的是「更早的那些在它们自己那一轮已经被改写过了」。
    **那条假设在 run 失败时不成立**,而失败一点都不罕见(断网、限流、模型 5xx)。

    真机实录:网络中断那阵子,

        1. 发照片 → 本 hook 改写了 → 模型调用 APIConnectionError → run 失败
           → 改写**没落盘**(失败的 run 不提交检查点)
        2. 再发一张 → 线程变成 [文本, image原样, image原样]
        3. 本 hook 只看最后一条 → messages[1] 永远是原样

    于是那条线程**被永久毒化**:此后每次提问,文本档模型都会收到一个 image 块,
    DeepSeek 回 400「unknown variant `image`, expected `text`」,而工友看到的
    只是「出错了」。自己好不了 —— 除非删掉整条对话。

    扫全部是安全的,**重复登记那个顾虑不成立**:改写完的消息 content 是纯字符串,
    `_rewrite` 第一行 `isinstance(content, list)` 就返回 None。也就是说
    对已改写的消息本函数天然是空操作,不会把同一张图登记第二次。

    ⚠️ 改这里时我先断言过「`RemoveMessage` + 同 id 会把消息挪到末尾、顺序全乱」,
    **那是错的**,实测(`add_messages` 直接跑一遍)两种写法都是**原地替换**:

        原始            a=第一条 | b=第二条 | c=答话 | d=第三条
        同id替换 b      a=第一条 | b=改写后 | c=答话 | d=第三条
        Remove+同id b   a=第一条 | b=改写后 | c=答话 | d=第三条   ← 位置没动

    所以保留 `RemoveMessage` 这个形状:单条时输出与改动前**逐字节相同**,
    既有测试与既有契约都不用跟着动。别为了「看起来简洁」把它删掉再验一次。
    """
    messages = state.get("messages") or []
    if not messages:
        return {}

    # 必须**永久**换掉,不能只改这一次的模型输入:子 Agent 与 Supervisor 共享
    # 同一份 messages,只改模型输入的话,子 Agent 那边照样会拿到 image 块再炸一次。
    updates: list[Any] = []
    for m in messages:
        if not isinstance(m, HumanMessage):
            continue
        rewritten = _rewrite(m)
        if rewritten is not None:
            updates.extend((RemoveMessage(id=m.id), rewritten))
    if not updates:
        return {}
    return {"messages": updates}


_MISSING = set(EXT_BY_MIME.values()) - ALLOWED_IMAGE_EXT
if _MISSING:  # pragma: no cover —— 配置写错才会走到
    raise RuntimeError(
        f"EXT_BY_MIME 里的 {sorted(_MISSING)} 不在 config.ALLOWED_IMAGE_EXT 中。"
        "上传的图片会落盘成无扩展名的文件,Safety 工具按后缀判格式,将永远看不了它。"
    )

__all__ = [
    "ATTACHMENT_BLOCK_TYPE",
    "ATTACHMENT_OUTCOMES",
    "EXT_BY_MIME",
    "OUTCOME_DRAWING",
    "OUTCOME_DRAWING_TOO_LARGE",
    "OUTCOME_PDF",
    "OUTCOME_PHOTO",
    "OUTCOME_PHOTO_TOO_LARGE",
    "OUTCOME_UNSUPPORTED",
    "Classified",
    "classify_attachment",
    "ingest_uploads",
]
