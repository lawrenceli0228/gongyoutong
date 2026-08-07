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

import base64
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from gyt.config import ALLOWED_IMAGE_EXT, get_settings
from gyt.core import artifacts, llm
from gyt.core.base_agent import load_prompt
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.core.llm import LLMCallError, MissingAPIKeyError

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

_USER_TEXT: Final[str] = "请看这张工地照片,按系统提示词要求的 JSON 格式给出判断。"

_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE
)
"""剥 ```json ... ``` 代码块。模型很爱加这层壳,提示词里已要求不加,但不能只靠它守规矩。"""

_OBJECT_RE: Final[re.Pattern[str]] = re.compile(r"\{.*\}", re.DOTALL)
"""兜底:从一段夹杂说明文字的输出里抠出最外层的 {...}。贪婪匹配,取最长的一段。"""

_BYTES_PER_MB: Final[int] = 1024 * 1024


@lru_cache(maxsize=1)
def _vision_prompt() -> str:
    """读视觉提示词(进程内只读一次)。

    用 lru_cache 而不是模块级常量:模块级会在 import 时就读盘,
    而本模块会被 gyt.graph 在建图时 import —— 提示词文件缺失应该在
    build_safety_agent() 时报错,而不是在 import 阶段炸得莫名其妙。
    """
    return load_prompt(SAFETY_DIR, VISION_PROMPT_FILENAME)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。抠不出来返回 None(由调用方转成失败信封)。

        原始输出
          │ ① 整段就是 ```json ... ``` → 剥掉外壳
          │ ② 直接 json.loads 试一次
          │ ③ 还不行 → 正则抠出最外层 {...} 再 loads
          ▼
        dict / None

    只接受 dict:模型偶尔会返回一个顶层数组(把 violations 直接吐出来),
    那种结构下游取不到 label,当解析失败处理比硬适配更安全。
    """
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group("body").strip()

    for candidate in (body, None):
        if candidate is None:
            match = _OBJECT_RE.search(body)
            if not match:
                return None
            candidate = match.group(0)
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


_ANALYZE_DESCRIPTION = (
    "看一张工地现场照片,判断是不是工地、有没有安全违规。"
    "参数 artifact_id 是照片的产物编号(32 位十六进制),由用户上传照片后系统给出。"
    "返回 label(violation 有违规 / compliant 合规 / not_site 不是工地)、"
    "violations(违规项清单)、note(判断依据与看不清的地方)。"
    "只有这个工具能看到照片,你自己看不到 —— 判断照片必须调它。"
)


@tool("analyze_site_photo", description=_ANALYZE_DESCRIPTION)
@tool_guard
async def analyze_site_photo(artifact_id: str) -> Envelope:
    """看一张工地照片,返回结构化的安全判断。

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

    data_uri = f"data:{MIME_BY_EXT[ext]};base64,{base64.b64encode(payload).decode('ascii')}"
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

    try:
        answer = await llm.ainvoke(model, messages, cache_extra=CACHE_EXTRA)
    except LLMCallError as exc:
        # 属性名是 error_code(不是 code),user_msg 已经是中文人话,原样透传给工人。
        logger.warning("视觉模型调用失败:%s", exc)
        return fail(exc.error_code, user_msg=str(exc.user_msg))

    # .text 在 langchain-core 1.x 是 property(旧版是方法,调用它会刷弃用告警)。
    # 取到的不是 str 就退回 content —— 覆盖旧版本与测试替身两种情况。
    raw_text = getattr(answer, "text", None)
    text = raw_text if isinstance(raw_text, str) else str(answer.content)
    parsed = _extract_json_object(text)
    if parsed is None:
        logger.warning("视觉模型输出不是 JSON,原文前 200 字:%s", text[:200])
        return fail(
            ErrorCode.UPSTREAM_ERROR,
            user_msg="这次没看明白这张照片,请再试一次。",
            detail=f"模型输出无法解析为 JSON:{text[:500]}",
        )

    label = str(parsed.get("label") or "").strip()
    violations = _as_violation_list(parsed.get("violations"))
    note = str(parsed.get("note") or "").strip()

    return ok(
        data={"label": label, "violations": violations, "note": note},
        user_msg=_summarize(label, violations),
    )


def _summarize(label: str, violations: list[str]) -> str:
    """给工人看的一句话结论。Agent 本体会在此基础上展开,这里只保证信封自己也读得懂。"""
    if label == "not_site":
        return "这张照片看着不像工地。"
    if violations:
        return f"发现 {len(violations)} 处问题:{'、'.join(violations)}。"
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

__all__ = ["CACHE_EXTRA", "MIME_BY_EXT", "SAFETY_TOOLS", "analyze_site_photo"]
