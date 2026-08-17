"""Safety Agent 的工具集 —— 看工地照片,判断有没有安全违规。

===========================================================================
为什么视觉调用在**工具内部**,而不是让 Agent 本体走 vision 档
---------------------------------------------------------------------------
Agent 本体用 create_gyt_agent(purpose="text") 走 DeepSeek,只有这一个工具
在内部直调 Kimi 视觉模型。三条理由,每条都实打实:

1. **图片不进 Agent 的消息历史。** 一张手机原图转成 base64 是几 MB。若让 Agent
   本体持有图片,则每一轮对话都要把它重发一遍;而 supervisor 的
   output_mode="last_message" 回灌时还会再带一次。上下文会被撑爆,钱按 token 烧。
2. **中文错误文案。** 工具内部走 llm.ainvoke(路径乙),它有 _classify/_user_msg
   那套「工人看得懂的人话」;Agent 路径的异常目前还是 openai SDK 的英文
   traceback(TODO-9 未做)。把视觉调用放进工具,顺带就拿到了中文错误。
3. **贵的模型只在真的要看图时被调一次。** 文本档 $0.14/M vs 视觉档 $3/M。

    Agent 本体(DeepSeek,便宜)
        │ 决定"该看图了",调工具
        ▼
    analyze_site_photo(artifact_id)
        │ artifacts.resolve → 读字节 → 校验 → base64
        ▼
    llm.ainvoke(get_chat_model("vision", max_retries=0), [...])   ← 路径乙
        │ 先查磁盘缓存(键含 prompt_version,改提示词自动失效)
        ▼
    Kimi 视觉模型 → JSON → 解析 → Envelope 信封

===========================================================================
「识别」与「登记」拆成两半(W9 方案 §6.2 的 D14 / D17)
---------------------------------------------------------------------------
    _recognize(artifact_id) ──► 五键判断        ← **纯函数,零副作用**
         ▲                          ▲
         │                          │
  analyze_site_photo          report 的保真复调 / eval/hooks.py
  (工具:识别 + 登记 pending)     (只要那份判断,**不许再登记一遍**)

为什么非拆不可 —— 不拆的两个具体后果,都不报错:

  ① `report/tools.py` 为了数据保真会**拿同一个 artifact_id 重调一次识别**。若登记
     长在 `analyze_site_photo` 里,而复调**不带 config** → `project_id` 取到空串,
     于是 safety 那次登记进「工地A」、report 那次登记进空串:**同一张照片同一个隐患,
     台账里两行,一行还无主。** 所以 report 改调 `_recognize`。
  ② `eval/hooks.py` 的 safety 套每行都重新 `artifacts.register` 一份同图副本,
     幂等键(照片内容 sha256)也拦不住不同内容的 30 张图 —— `make eval SUITE=safety`
     每跑一次就往**生产台账**灌 30 条幽灵隐患。所以它也只调 `_recognize`。

登记侧的三条硬约定(方案 §6.2,与 `db/hazards.py` 的建表注释同源):

  · **幂等键 = (project_id, photo_sha256, item)**,`photo_sha256` 是**照片内容**的哈希,
    **不是 artifact_id** —— `core/artifacts.py` 用 `uuid4().hex` 发号,同一张照片重传
    就是新号,彩排三轮会得三批隐患。哈希算在 `artifacts.resolve()` 拿回的**原始字节**上,
    不是 `_prepare_image` 压缩后的字节:压缩结果随 `photo_compress_*` 配置变,
    调一次参数全库的幂等键就漂了。
  · **落 `pending`**(D17):自动登记的东西还没有人确认过,不算整改率、不进超期清单、
    不能被升级。监理在界面上确认才转 `open`。
  · **写库失败不堵死主路径**(D10):识别结果照常 `ok` 返回,失败项进 `failed_items`,
    并往 `hazard_ingest_failures` 记一行。连那张表都写不进(整个 sqlite 挂了)时,
    退化为**只有日志** —— 方案 §6.2 认了这个退化,别以为还有别的兜底。

===========================================================================
两处**刻意不做**的事,改之前先读完这段
---------------------------------------------------------------------------
① 不过滤模型输出的违规项。
   模型可能吐出受控词表以外的词(「未佩戴安全帽」多一个"佩"字)。这里**原样透传**,
   不做纠正、不做映射。因为 scorers.score_safety 会算 `unknown = got_items -
   VIOLATION_VOCAB` 并在报告里写「不在受控词表里,提示词的输出约束没生效」——
   那正是我们需要看到的诊断信号。在这里悄悄修好,评测就永远发现不了提示词坏了,
   而线上换个模型/改个提示词随时会复发。**让错误可见,比让它消失更重要。**

② 不因 label 取值非法就 fail。
   模型答了个 "有违规" 这种不在三态里的值,照样透传进信封。理由同上:
   fail 会把它归成"工具挂了",而实际是"模型没守格式"。两者的修法完全不同 ——
   前者查代码,后者改提示词。只有**解析不出 JSON** 才算工具失败。

===========================================================================
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
from collections.abc import Iterator, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from PIL import Image

from gyt.agents.safety.severity import grade, worst

# 定级第二跳(severity → 监理口径的 grade)。import 方向 safety → supervision 不成环:
# supervision/grading.py 只 import safety/severity.py(纯表)与 db/hazards.py,碰不到本模块。
from gyt.agents.supervision.grading import grade_of
from gyt.attendance.receipt import make_snapshot
from gyt.config import ALLOWED_IMAGE_EXT, get_settings
from gyt.core import artifacts, llm
from gyt.core.base_agent import load_prompt
from gyt.core.doc_no import DocKind, generate_unique
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.core.llm import LLMCallError, MissingAPIKeyError
from gyt.core.run_context import project_from_config
from gyt.db import hazards

logger = logging.getLogger(__name__)

SAFETY_DIR: Final[Path] = Path(__file__).parent
"""本包目录。vision_prompt.md 就在旁边,用 __file__ 推导保证任何工作目录都找得到。"""

VISION_PROMPT_FILENAME: Final[str] = "vision_prompt.md"
"""工具内部那次视觉调用的提示词。与 Agent 本体的 prompt.md 是两份,职责见文件顶部。"""

CACHE_EXTRA: Final[str] = "safety.analyze_site_photo"
"""缓存键里的防串味维度。

llm.ainvoke 的键 = (模型, prompt_version, 消息, extra)。填一个本工具独有的值,
保证「同一张图被别的调用方问了别的问题」不会撞进同一条缓存 —— 那会让工具
原样取走别人的答案,零次网络调用,日志里只有一行 debug,几乎查不出来。
"""

MIME_BY_EXT: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
"""扩展名 → MIME。键集合必须是 config.ALLOWED_IMAGE_EXT 的超集,
下面有个模块级断言在 import 时就把两者对不上的情况炸出来(见文件末尾)。"""

KEY_LABEL: Final[str] = "label"
KEY_VIOLATIONS: Final[str] = "violations"
KEY_NOTE: Final[str] = "note"
KEY_HAZARDS: Final[str] = "hazards"
KEY_FAILED_ITEMS: Final[str] = "failed_items"
LABEL_VIOLATION: Final[str] = "violation"
"""三态里唯一会产生隐患的那一档。

`compliant` / `not_site` **一条都不登记** —— 前者是"看过了没问题",后者压根不是现场,
给它们建台账行等于往整改率里掺噪声。词表外的野 label(模型没守格式,见文件顶部②)
也一律不登记:不认识就不写库,和 severity 对词表外违规项的处理是同一个原则。
"""

OUTPUT_KEYS: Final[tuple[str, ...]] = (KEY_LABEL, KEY_VIOLATIONS, KEY_NOTE)
"""模型输出契约的三个字段名。**vision_prompt.md 里必须逐字出现这三个名字。**

为什么要提成常量并加测试守着:提示词是给非工程师队友改的(见 base_agent.load_prompt)。
把 `violations` 顺手改成 `items` 之后 —— 模型照新契约输出、这里 get("violations")
取到空、于是 label=violation 的照片返回 violations=[] 且 ok=True,
Agent 按 prompt.md 的规定对工人说「这张照片里没看到明显的安全问题」。
**一张明确判了违规的照片被讲成没问题,全程零报错、零测试转红。**
这是安全类产品最坏的一种失败:静默漏报。
守卫在 tests/unit/test_safety.py::test_视觉提示词必须写明输出契约的三个字段名。
"""

VISION_MAX_ATTEMPTS: Final[int] = 2
"""视觉调用最多尝试几次(1 次首发 + 1 次重试),而不是默认的 1 + llm_max_retries = 4。

**重试次数是乘在超时上的。** 视觉调用本身就慢(实测 4K 工地照片 59.7 秒),
超时又被调到 150 秒,按默认 4 次算:4 × 150 + 退避 7 秒 = **607 秒(10 分钟)**
用户对着一个没有任何反馈的界面干等 —— 演示日这等同于死机。压到 2 次是 301 秒,
仍然长,但至少在"人愿意等"的量级里。

为什么不干脆压到 1 次(不重试):偶发的网络抖动重试一次确实救得回来,
而那正是演示现场最常见的故障。真正该被砍掉的是"超时后还重试"——
同一张图、同一个模型,150 秒都没返回,再试一次大概率还是超时。
但区分"超时不重试、限流才重试"要改 _classify 的分类逻辑,影响面比这里大,
留给 TODO-12 的降采样一起做:图小了延迟自然下来,这个洞就不存在了。
"""

_USER_TEXT: Final[str] = "请看这张工地照片,按系统提示词要求的 JSON 格式给出判断。"

_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE
)
"""剥 ```json ... ``` 代码块。模型很爱加这层壳,提示词里已要求不加,但不能只靠它守规矩。"""

_BYTES_PER_MB: Final[int] = 1024 * 1024

_JPEG_QUALITY_STEPS: Final[tuple[int, ...]] = (85, 75, 65, 55)
"""压体积时逐档下调的 JPEG 质量。

从 85 起步而不是更低:判断「有没有戴安全帽」「腰上有没有挂钩」靠的是细节,
过度压缩会把远处的小目标糊掉 —— 那是在用画质换钱,而漏报一个隐患的代价远高于几分钱。
最低只到 55;真到了这一档还超限,就让它超 —— 上游真正的硬限是整个请求体 100MB,
离得远得很,没必要为了一个软目标把图压烂。"""


@lru_cache(maxsize=1)
def _vision_prompt() -> str:
    """读视觉提示词(进程内只读一次)。

    用 lru_cache 而不是模块级常量:模块级会在 import 时就读盘,
    而本模块会被 gyt.graph 在建图时 import —— 提示词文件缺失应该在
    build_safety_agent() 时报错,而不是在 import 阶段炸得莫名其妙。
    """
    return load_prompt(SAFETY_DIR, VISION_PROMPT_FILENAME)


def _iter_balanced_objects(text: str) -> Iterator[str]:
    """按花括号配平,依次吐出文本里每一段独立的 ``{...}``。

    为什么不用正则:原先写的是贪婪的 ``\\{.*\\}``,它会把
    ``{答案}\\n参考格式:{示例}`` 整段吃成一个候选,于是**一个本来完全可用的答案**
    被判成解析失败。而模型在答案后面补一句带花括号的说明是很常见的。
    改成非贪婪也不对 —— 那会在第一个嵌套的 ``}`` 处截断。

    扫描时跳过字符串字面量内部的花括号与转义符,所以
    ``{"note": "见 {GB50720} 第3条"}`` 会被正确地当作**一个**对象。
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                yield text[start : index + 1]


def _message_text(message: Any) -> str:
    """取出一条模型回复的正文。

    ``.text`` 在 langchain-core 1.x 是 property(旧版是方法,调用它会刷弃用告警)。
    取到的不是 str 就退回 ``.content`` —— 覆盖旧版本与测试替身两种情况。
    """
    raw = getattr(message, "text", None)
    return raw if isinstance(raw, str) else str(getattr(message, "content", ""))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。抠不出来返回 None(由调用方转成失败信封)。

        原始输出
          │ ① 整段就是 ```json ... ``` → 剥掉外壳
          │ ② 直接 json.loads 试一次
          │ ③ 还不行 → 按花括号配平扫出每一段 {...},**逐段**试,取第一个能解析成 dict 的
          ▼
        dict / None

    ③ 必须逐段试而不是只试第一段:模型爱在正文前先写一句引子
    (「根据《规范》判断:」),引子里若带花括号,第一段就不是 JSON。
    只试第一段的话这类输出仍然解析失败。

    只接受 dict:模型偶尔会返回一个顶层数组(把 violations 直接吐出来),
    那种结构下游取不到 label,当解析失败处理比硬适配更安全。

    ⚠️ 这个函数的返回值决定了**要不要把这次回答写进缓存**
    (见 analyze_site_photo 里传给 llm.ainvoke 的 validate)。
    它每放宽一点,就少一次「答案其实是好的、却被永久缓存成失败」。
    """
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group("body").strip()

    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    for candidate in _iter_balanced_objects(body):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _as_violation_list(raw: Any) -> list[str]:
    """把模型给的 violations 归一成字符串列表。**不做词表过滤**(理由见文件顶部②)。

    容忍三种形态:已经是列表、用分隔符连起来的一个字符串、None/缺失。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[;;,、,]", raw)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [str(raw).strip()]


class ImageRejected(Exception):
    """图片过不了预处理这一关。code 决定给工人的中文文案。"""

    def __init__(self, code: ErrorCode, user_msg: str) -> None:
        self.code = code
        self.user_msg = user_msg
        super().__init__(user_msg)


def _prepare_image(payload: bytes, ext: str) -> tuple[bytes, str]:
    """把原始字节整成「能安全喂给视觉模型」的样子,返回 (字节, MIME)。

        原始字节
          │ ① Pillow 打不开 ──────────────▶ ImageRejected(FILE_CORRUPT)
          │    在此之前只看扩展名 —— 一个改名成 .jpg 的文本文件照样会被 base64 发出去
          ▼
          │ ② 帧数 > 1(动图)────────────▶ ImageRejected(FILE_UNSUPPORTED)
          │    animated gif/webp 的 MIME 与静态图**完全相同**,只看扩展名必然放行;
          │    而月之暗面可能把它当视频解码计费 —— 账单是静态图的几十倍,且完全静默
          ▼
          │ ③ 长边 > photo_compress_max_edge_px → 等比缩小
          │    视觉 token 按**像素面积**计。实测 4000×2430 的工地照片端到端 59.7 秒,
          │    而 1600×1067 的同类只要 10.1 秒 —— 手机原图正好是前者那个量级
          ▼
          │ ④ 仍大于 photo_compress_target_mb → 逐档降 JPEG 质量
          ▼
        (处理后的字节, MIME)

    **没超限的图原样返回**,连重编码都不做 —— 重编码只会白白损失画质,
    而判断「有没有戴安全帽」经不起反复有损压缩。
    """
    settings = get_settings()
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.load()
            width, height = probe.size
            frames = getattr(probe, "n_frames", 1)
            fmt = (probe.format or "").upper()
    except Exception as exc:  # noqa: BLE001 —— Pillow 的异常类型很杂,一律当损坏处理
        raise ImageRejected(
            ErrorCode.FILE_CORRUPT,
            "这张照片打不开,可能传的时候坏了,或者根本不是图片。请重新传一次。",
        ) from exc

    if frames > 1:
        raise ImageRejected(
            ErrorCode.FILE_UNSUPPORTED,
            f"这是一张动图({frames} 帧),看不了。请传静态照片(截一帧再发也行)。",
        )

    max_edge = settings.photo_compress_max_edge_px
    limit_bytes = int(settings.photo_compress_target_mb * _BYTES_PER_MB)
    if max(width, height) <= max_edge and len(payload) <= limit_bytes:
        return payload, MIME_BY_EXT[ext]

    with Image.open(io.BytesIO(payload)) as image:
        image = image.convert("RGB")  # 统一丢掉 alpha/调色板,JPEG 存不了它们
        if max(width, height) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        for quality in _JPEG_QUALITY_STEPS:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= limit_bytes:
                break
    logger.info(
        "照片已预处理:%dx%d %.1fMB → %dx%d %.1fMB(原格式 %s)",
        width,
        height,
        len(payload) / _BYTES_PER_MB,
        image.width,
        image.height,
        len(data) / _BYTES_PER_MB,
        fmt or "?",
    )
    return data, "image/jpeg"


_ANALYZE_DESCRIPTION = (
    "看一张工地现场照片,判断是不是工地、有没有安全违规。"
    "参数 artifact_id 是照片的产物编号(32 位十六进制),由用户上传照片后系统给出。"
    "返回 label(violation 有违规 / compliant 合规 / not_site 不是工地)、"
    "violations(违规项清单)、note(判断依据与看不清的地方);"
    "发现的违规项还会被登记成「待确认」隐患,编号在返回的 hazards 里,等监理确认后才进入正式流程。"
    "只有这个工具能看到照片,你自己看不到 —— 判断照片必须调它。"
)


@tool_guard
async def _recognize(artifact_id: str) -> Envelope:
    """看一张工地照片,返回结构化的安全判断。**纯函数:不写库、不落盘、零副作用。**

    三个调用方:``analyze_site_photo``(识别完接着登记)、``report/tools.py`` 的保真复调、
    ``eval/hooks.py`` 的 safety 套。后两个**只要这份判断,不许带上登记**(理由见文件顶部)。

    它不是工具、不进 ``SAFETY_TOOLS``,但仍然挂 ``@tool_guard``:三个调用方历史上拿到的
    都是「永不抛异常的信封」,把这条保证挪走会让 eval 的报告从一句中文原因变成一段
    Python traceback,而 report 那边则从「透传 safety 的中文失败」变成笼统的「系统开小差了」。

    流程与各步的失败出口:

        artifact_id
          │ 空 / 不是 32 位 hex ─────────────▶ fail(INVALID_INPUT)
          ▼ artifacts.resolve()
        磁盘路径
          │ 找不到 / 文件丢了 ───────────────▶ fail(NOT_FOUND)
          │ 扩展名不在图片白名单 ─────────────▶ fail(FILE_UNSUPPORTED)
          ▼ 读字节
        bytes
          │ 空文件 ────────────────────────▶ fail(FILE_CORRUPT)
          │ 超过 photo_max_mb ──────────────▶ fail(FILE_TOO_LARGE)
          ▼ base64 + 构造消息
        llm.ainvoke(vision 档, max_retries=0)
          │ 超时/限流/上游挂 ────────────────▶ fail(对应码,文案来自 LLMCallError)
          ▼ 模型输出
        抠 JSON
          │ 抠不出来 ──────────────────────▶ fail(UPSTREAM_ERROR)
          ▼
        ok(data={"label":..., "violations":[...], "note":...})

    参数:
        artifact_id: 照片的产物编号,32 位小写十六进制。

    返回:
        Envelope 信封。成功时 data 形如
        ``{"label": "violation", "violations": ["未戴安全帽"], "note": "..."}``。
    """
    settings = get_settings()

    cleaned = (artifact_id or "").strip()
    if not artifacts.ARTIFACT_ID_RE.match(cleaned):
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="照片编号不对。编号是 32 位的字母数字组合,请把上传照片后系统给的那串发我。",
        )

    try:
        path = artifacts.resolve(cleaned)
    except artifacts.ArtifactNotFound:
        logger.info("产物不存在或已丢失:%s", cleaned)
        return fail(
            ErrorCode.NOT_FOUND,
            user_msg="没找到这张照片,可能已经被清理了,麻烦重新传一次。",
        )

    ext = path.suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        logger.info("产物 %s 的扩展名 %s 不是支持的图片格式", cleaned, ext or "(无)")
        kinds = "、".join(sorted(ALLOWED_IMAGE_EXT))
        return fail(
            ErrorCode.FILE_UNSUPPORTED,
            user_msg=f"这个文件不是能看的图片格式,请传 {kinds} 之一。",
        )

    try:
        payload = path.read_bytes()
    except OSError as exc:
        logger.warning("产物 %s 读不出来:%s", cleaned, exc)
        return fail(ErrorCode.FILE_CORRUPT)

    if not payload:
        return fail(ErrorCode.FILE_CORRUPT, user_msg="这张照片是空文件,请重新传一次。")

    limit = int(settings.photo_max_mb * _BYTES_PER_MB)
    if len(payload) > limit:
        logger.info("产物 %s 超限:%d 字节 > %d", cleaned, len(payload), limit)
        return fail(
            ErrorCode.FILE_TOO_LARGE,
            user_msg=(
                f"这张照片有 {len(payload) / _BYTES_PER_MB:.1f}MB,超过了 "
                f"{settings.photo_max_mb:.0f}MB 的上限。用手机相册里的「压缩后发送」再传一次就行。"
            ),
        )

    try:
        prepared, mime = _prepare_image(payload, ext)
    except ImageRejected as exc:
        logger.info("产物 %s 预处理未通过:%s", cleaned, exc.user_msg)
        return fail(exc.code, user_msg=exc.user_msg)

    data_uri = f"data:{mime};base64,{base64.b64encode(prepared).decode('ascii')}"
    messages = [
        SystemMessage(content=_vision_prompt()),
        HumanMessage(
            content=[
                {"type": "text", "text": _USER_TEXT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        ),
    ]

    # max_retries=0 是 TODO-10 的落地点:路径乙外面已经有 _invoke_with_retry
    # (1 + llm_max_retries 次),若再叠 SDK 自带的退避,一次逻辑调用最坏发 4×4=16 个
    # HTTP 请求 —— 30 张视觉评测最坏烧掉约 29 元,而调提示词要跑很多轮。
    # 必须在**造模型时**传,model_copy 事后改是无效的(原因见 llm._for_direct_call)。
    #
    # MissingAPIKeyError 要显式接住:它的消息本身就是一句可操作的中文
    # (「请在 .env 里加上 GYT_MOONSHOT_API_KEY」),而 tool_guard 会把漏网异常一律
    # 转成 INTERNAL 的「系统开小差了」—— 那句话对排查毫无帮助。
    # 这是**演示日最可能发生的一类故障**(Key 没填 / 填错 / 额度耗尽 / 账号没充值
    # 导致 kimi-k3 未解锁)。而且 Agent 本体走 text 档,建图时只校验 DeepSeek 的 Key,
    # Kimi 的 Key 要到这一刻才检查 —— 也就是说**图起得来不代表识图能用**,
    # 这条路径必须自己把话说清楚,指到 .env 的哪一行。
    # (这个洞是真实冒烟时踩出来的,不是推演出来的。)
    try:
        model = llm.get_chat_model("vision", max_retries=0)
    except MissingAPIKeyError as exc:
        logger.error("视觉模型的接口密钥没配置:%s", exc)
        return fail(ErrorCode.INTERNAL, user_msg=str(exc))

    # is_usable 是**写缓存前的最后一道闸**:抠不出 JSON 的回答照常返回给这里处理,
    # 但不落盘。不加这道闸的话,那句「请再试一次」永远不可能成功 ——
    # 坏回答会被缓存,第二次直接命中、0 次网络调用、一字不差的同一句失败,
    # 这张照片对该 prompt_version 就被永久钉死了(2026-08-07 实测确认)。
    # 而且缓存键算在图片**内容**上,让用户重传同一个文件也逃不掉。
    try:
        answer = await llm.ainvoke(
            model,
            messages,
            cache_extra=CACHE_EXTRA,
            is_usable=lambda msg: _extract_json_object(_message_text(msg)) is not None,
            max_attempts=VISION_MAX_ATTEMPTS,
        )
    except LLMCallError as exc:
        # 属性名是 error_code(不是 code),user_msg 已经是中文人话,原样透传给工人。
        logger.warning("视觉模型调用失败:%s", exc)
        return fail(exc.error_code, user_msg=str(exc.user_msg))

    text = _message_text(answer)
    parsed = _extract_json_object(text)
    if parsed is None:
        logger.warning("视觉模型输出不是 JSON,原文前 200 字:%s", text[:200])
        return fail(
            ErrorCode.UPSTREAM_ERROR,
            user_msg="这次没看明白这张照片,请再试一次。",
            detail=f"模型输出无法解析为 JSON:{text[:500]}",
        )

    label = str(parsed.get(KEY_LABEL) or "").strip()
    violations = _as_violation_list(parsed.get(KEY_VIOLATIONS))
    note = str(parsed.get(KEY_NOTE) or "").strip()

    # severity / max_severity 是给下游(Agent 话术、W3 的 Report/Schedule)用的
    # **确定性字段**:由 severity.py 的映射表算出,不经过模型,同一违规项永远同一级。
    # 键恒存在(空 dict / None),下游不用做"有没有这个键"的分支。
    # ⚠️ 这五个键是 safety → report 的交接契约:label / violations / severity /
    #    max_severity / note。改任何一个键名都是改契约,要连着 report 一起动。
    #    W9 起 analyze_site_photo 会在这份 data 上**再补两个键**(hazards / failed_items),
    #    但那两个是登记侧的产物,不属于识别 —— 所以它们不在这里,而在下面那层。
    return ok(
        data={
            KEY_LABEL: label,
            KEY_VIOLATIONS: violations,
            "severity": grade(violations),
            "max_severity": worst(violations),
            KEY_NOTE: note,
        },
        user_msg=_summarize(label, violations),
    )


# ---------------------------------------------------------------------------
# 登记那一半 —— 把识别出的违规项写成 pending 隐患(W9 D14 / D17)
#
# 全是**同步阻塞**的 sqlite / 磁盘操作,调用方一律用 asyncio.to_thread 包一层:
# 直接在事件循环里跑 sqlite,blockbuster 会抛 BlockingError,被 tool_guard 兜成
# INTERNAL 失败信封 —— 现象是「识图工具没返回结果」,和识别本身一点关系都没有。
# ---------------------------------------------------------------------------


def _hazard_no_taken(candidate: str) -> bool:
    """撞库判据:这个隐患编号库里有没有。交给 ``doc_no.generate_unique`` 当 ``exists`` 用。

    查不到就返回 False;库连不上时**原样把异常抛出去**(不吞成"撞了")——
    ``generate_unique`` 的契约第 2 条点名了这件事:吞掉会白白烧掉重试次数,
    最后报一个「编号都被占了」,而排查的人会去翻编号表,真正的毛病在连接上。
    """
    return hazards.fetch(candidate) is not None


def _record_failure(*, project_id: str, photo_id: str, item: str, reason: str) -> None:
    """记一行「这个违规项没能写进台账」。**本函数自己绝不抛**。

    连 ``hazard_ingest_failures`` 都写不进(整个 sqlite 挂了)时就只剩这条日志 ——
    方案 §6.2 明确认了这个退化,别以为后面还有别的兜底。
    ``reason`` 是给排查的人看的内部细节(异常类型 + 消息),**不进任何面向工友的文案**。
    """
    try:
        hazards.record_ingest_failure(
            project_id=project_id, photo_id=photo_id, item=item, reason=reason
        )
    except Exception:  # noqa: BLE001 —— 失败表也写不进就只能记日志,不能反过来炸掉主路径
        logger.exception(
            "隐患登记失败、连失败表也写不进:project=%r photo=%s item=%s", project_id, photo_id, item
        )


def _ingest_hazards(
    *, project_id: str, photo_id: str, violations: Sequence[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """把这次识别出的违规项逐条登记成 ``pending`` 隐患,返回 (已登记, 失败项)。

    **同步阻塞**(sqlite + 读文件),调用方负责 to_thread。

        violations
          │ 去重保序 —— 模型偶尔把同一项说两遍,而幂等键决定了台账里本来就只会有一行;
          │             回执里报两条一模一样的待确认项,只会让监理以为真有两处
          ▼ artifacts.resolve(photo_id).read_bytes() → sha256
        幂等键的第二节
          │ 照片读不回来(被清理了 / 磁盘坏)──▶ 整批算失败,但**识别结果照常返回**
          ▼ 逐项:severity → grade_of → 摇编号 → hazards.create
        Registration(row, created)
          │ 任一项写库失败 ──▶ 该项进 failed_items + 记一行 hazard_ingest_failures,
          │                    **其余项继续**(一条坏行不该带走整张照片的登记)
          ▼
        ([{hazard_no, item, grade, status, needs_grading}, …], [失败的 item, …])

    ⚠️ 报出去的编号一律取 ``create(...).row.hazard_no`` 而**不是**刚摇出来的那个:三元组
    已存在时 ``create`` 什么都不插,回的是**首次登记时的号**(D14)。用刚摇的那个 = 给工友
    一个库里根本不存在的编号,他拿去查会扑空,而这里不会有任何报错。
    """
    # dict.fromkeys 去重且保序(Python 3.7+ 的 dict 有序);violations 本身仍然原样透传,
    # 那份是诊断信号,不在这里动(见文件顶部①)。
    items = list(dict.fromkeys(violations))
    if not items:
        return [], []

    try:
        digest = hashlib.sha256(artifacts.resolve(photo_id).read_bytes()).hexdigest()
    except Exception as exc:  # noqa: BLE001 —— 读不回照片就登记不了,但不该连识别结果一起毁掉
        logger.warning("隐患登记取不到照片字节:photo=%s —— %s", photo_id, exc)
        reason = f"读取照片字节失败:{type(exc).__name__}: {exc}"
        for item in items:
            _record_failure(project_id=project_id, photo_id=photo_id, item=item, reason=reason)
        return [], items

    levels = grade(items)  # 逐项 severity(词表外 → 待定级),确定性映射,不经过模型
    # 整批共用一个时间快照:同一张照片的 N 条隐患是**一次动作**,编号里的时刻该对得齐。
    # (跨秒时各取各的 now,同一次识别会显示成两个时间点 —— doc_no.new_doc_no 的原话。)
    snap = make_snapshot()

    registered: list[dict[str, Any]] = []
    failed: list[str] = []
    for item in items:
        level = levels[item]
        grading = grade_of(level)  # severity → 监理口径 grade + needs_grading + 版本快照
        try:
            # generate_unique 已经把绝大多数撞号挡在写库之前;它与 create 之间仍有一个
            # 理论窗口,真撞上时 hazard_no 的 UNIQUE 会抛 IntegrityError,落到下面的
            # except 里变成一条 failed_items —— 不静默、不硬写一个重号上去。
            hazard_no = generate_unique(DocKind.HAZARD, _hazard_no_taken, snap=snap)
            row = hazards.create(
                hazard_no=hazard_no,
                project_id=project_id,
                photo_sha256=digest,
                photo_id=photo_id,
                item=item,
                severity=level,
                grade=grading.grade,
                grading_version=grading.version,
                needs_grading=grading.needs_grading,
            ).row
        except Exception as exc:  # noqa: BLE001 —— D10:一项写不进不该堵死整条识别路径
            logger.warning(
                "隐患登记失败:project=%r photo=%s item=%s —— %s", project_id, photo_id, item, exc
            )
            failed.append(item)
            _record_failure(
                project_id=project_id,
                photo_id=photo_id,
                item=item,
                reason=f"{type(exc).__name__}: {exc}",
            )
            continue
        registered.append(
            {
                "hazard_no": row.hazard_no,
                "item": row.item,
                "grade": row.grade,
                "status": row.status,
                "needs_grading": bool(row.needs_grading),
            }
        )
    return registered, failed


@tool("analyze_site_photo", description=_ANALYZE_DESCRIPTION)
@tool_guard
async def analyze_site_photo(artifact_id: str, *, config: RunnableConfig) -> Envelope:
    """识别一张工地照片,并把发现的违规项登记成**待确认**隐患。

        _recognize(artifact_id)
          │ 任一步失败 ──────────▶ 原样透传失败信封(登记这一半一步都不跑)
          ▼ 五键 data
        label == "violation" ?
          │ 否(compliant / not_site / 野 label)──▶ hazards=[] failed_items=[]
          ▼ 是
        asyncio.to_thread(_ingest_hazards)   ← sqlite + 读文件都是阻塞 IO
          ▼
        ok(data={…五键…, "hazards": [...], "failed_items": [...]})

    ⚠️ ``config`` 的注解必须**恰好**是 ``RunnableConfig``:langchain 按
    ``param.annotation is RunnableConfig`` 判定要不要注入,写成 ``RunnableConfig | None``
    会漏掉 —— 表现是 config 永远拿不到、``project_id`` 恒为空串,于是**所有工地的隐患
    全挤进"未归属"那一堆**,而且不会有任何报错(``core/run_context.py`` 头注有原话)。

    ⚠️ 这里返回的 data 比 ``_recognize`` **多两个键**,那是 safety → report 的交接契约
    的扩展(方案 §6.2:「data 从五键变多键 = 改了交接契约」)。report 侧按 ``.get()``
    取值,多出来的键它不看 —— 但改键名之前仍要回去看一眼 ``report/tools.py``。
    """
    envelope = await _recognize(artifact_id)
    data = envelope.get("data")
    if not envelope.get("ok") or not isinstance(data, dict):
        return envelope

    enriched: dict[str, Any] = {**data, KEY_HAZARDS: [], KEY_FAILED_ITEMS: []}
    violations = list(data.get(KEY_VIOLATIONS) or [])
    # 两个键**恒存在**(没隐患就是空列表),下游不用做"有没有这个键"的分支 ——
    # 与上面 severity/max_severity 恒存在是同一条约定。
    if data.get(KEY_LABEL) == LABEL_VIOLATION and violations:
        project_id = project_from_config(config)  # 没选工地 = 空串(D6),不是 None
        registered, failed = await asyncio.to_thread(
            _ingest_hazards,
            project_id=project_id,
            photo_id=(artifact_id or "").strip(),
            violations=violations,
        )
        enriched[KEY_HAZARDS] = registered
        enriched[KEY_FAILED_ITEMS] = failed
        logger.info(
            "照片 %s 登记隐患:project=%r 成功 %d 条、失败 %d 条",
            (artifact_id or "").strip(),
            project_id,
            len(registered),
            len(failed),
        )

    # user_msg 原样沿用识别那句 —— 「登记了几条待确认」属于监理台账的话术,归 S4 的
    # supervision 界面说,别在工友的识图回执里塞一句他这会儿用不上的流程话。
    return ok(data=enriched, user_msg=str(envelope.get("user_msg") or ""))


def _severity_rank(level: str) -> int:
    """级别 → 排序权重(重的小)。找不到就排最后,别因为一个新级别名把整句话炸了。"""
    from gyt.agents.safety.severity import SEVERITY_ORDER

    try:
        return SEVERITY_ORDER.index(level)
    except ValueError:
        return len(SEVERITY_ORDER)


def _summarize(label: str, violations: list[str]) -> str:
    """给工人看的一句话结论。Agent 本体会在此基础上展开,这里只保证信封自己也读得懂。"""
    if label == "not_site":
        return "这张照片看着不像工地。"
    if violations:
        level = worst(violations)
        graded = grade(violations)
        # 重的排前面:工人扫一眼就先看到最要命的那条
        ordered = sorted(violations, key=lambda v: _severity_rank(graded[v]))
        head = f"发现 {len(ordered)} 处问题"
        if level:
            head += f"(最高级别:{level})"
        return f"{head}:{'、'.join(ordered)}。"
    if label == "compliant":
        return "这张照片里没看到明显的安全问题。"
    return "已看过这张照片,但模型没给出标准结论,请人工再确认一下。"


SAFETY_TOOLS: list = [analyze_site_photo]
"""供 gyt.agents.safety 组装时使用。拿去用之前先 list(...) 复制一份,别原地 append。"""

# import 期自检:MIME 表必须覆盖图片白名单里的每一个扩展名。
# 不加这条的话,以后有人往 ALLOWED_IMAGE_EXT 里加了 .gif 却忘了加 MIME,
# 会在**演示当天**才炸出 KeyError —— 前面的校验放行了,构造 data URI 时才取不到值。
_MISSING_MIME = ALLOWED_IMAGE_EXT - MIME_BY_EXT.keys()
if _MISSING_MIME:  # pragma: no cover —— 配置写错才会走到,正常永远为空
    raise RuntimeError(
        f"图片白名单里的 {sorted(_MISSING_MIME)} 在 MIME_BY_EXT 里没有对应项。"
        "两处必须同时改:gyt.config.ALLOWED_IMAGE_EXT 与本文件的 MIME_BY_EXT。"
    )

__all__ = [
    "CACHE_EXTRA",
    "KEY_FAILED_ITEMS",
    "KEY_HAZARDS",
    "LABEL_VIOLATION",
    "MIME_BY_EXT",
    "OUTPUT_KEYS",
    "SAFETY_TOOLS",
    "ImageRejected",
    "analyze_site_photo",
]
# ``_recognize`` 刻意不进 __all__:它是「只识别、不登记」这条内部通道,给 report 的保真
# 复调与 eval/hooks 用,不是对外能力。两处都写明式 import(from ... import _recognize),
# 谁在用一 grep 就见底 —— 放进 __all__ 反而像是在邀请更多人跳过登记那一半。
