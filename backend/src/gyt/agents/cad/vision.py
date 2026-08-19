"""CAD 图纸「文字选不中」时的读字兜底:把渲染好的 PNG 交给视觉模型(kimi-k3)认字。

===========================================================================
为什么有这一层,以及它和「抽出来的字」的本质区别
---------------------------------------------------------------------------
国内 CAD 打印成 PDF 时,只要勾了「文字作为图形」或用了 SHX 字体,文字就被转成矢量线条;
光栅打印/扫描件则整页是位图。两种情况 pypdf 的 extract_text 都抽不到一个字
(parse_pdf 把它们标成 has_text=False)。这一层就是那时的兜底:渲染成图 → 交给视觉模型认。

⚠️ 认出来的字 ≠ 抽出来的字。矢量文字层抽出来的是**精确**的;视觉模型**认**出来的**可能错、可能漏**。
   施工场景里一个被认错的标高/尺寸是要出事的,所以工具层报这些字时必须带「可能有误、以原图为准」
   的措辞(见 tools.read_view_params),和「读到图上文字」那套精确措辞泾渭分明。

为什么复用 llm.ainvoke(路径乙)而不自己搓 openai client:它白送三样 ——
   · 磁盘缓存(键含图片字节 + prompt_version + cache_extra):同一张图第二次问 0 网络调用、0 花费;
   · 中文错误信封:失败抛 LLMCallError(.user_msg 是人话);
   · 重试/超时封装。
用一个 CAD 专属的 cache_extra 与安全线隔开,防两边缓存串味。

本模块**不接异常**:MissingAPIKeyError / LLMCallError 向上抛,由工具层接住翻成中文 fail
(与 safety/tools.py 同姿势 —— 错误文案的归口在工具层,不在这里)。
===========================================================================
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from gyt.core import llm
from gyt.core.base_agent import load_prompt

_DIR = Path(__file__).parent
_VISION_PROMPT_FILENAME = "vision_prompt.md"

# 缓存键里的防串味维度:和 safety 的 "safety.analyze_site_photo" 分开,免得「同一张图被两处问了
# 不同问题」撞进同一条缓存、取走对方的答案(见 core/llm.py 里 cache_extra 的说明)。
_CACHE_EXTRA = "cad.read_drawing_text"

# 视觉调用最多几次(1 首发 + 1 重试),与 safety 同口径:重试是乘在超时上的,别叠 SDK 自带退避
# 把一次逻辑调用放大成 4×timeout。
_MAX_ATTEMPTS = 2

_USER_TEXT = "把这张施工图上的文字如实认出来,按系统提示词的要求一行一条输出。"

# 视觉模型认不出任何文字时,提示词约定回的固定句(见 vision_prompt.md 末尾)。工具层据此当「空」。
EMPTY_MARKER = "图上没认出文字"


@lru_cache(maxsize=1)
def _prompt() -> str:
    """读读字提示词(进程内只读一次)。缺文件时在这里抛,而不是 import 期。"""
    return load_prompt(_DIR, _VISION_PROMPT_FILENAME)


def _message_text(message: Any) -> str:
    """取模型回复正文。``.text`` 在 langchain-core 1.x 是 property;不是 str 就退回 ``.content``。"""
    raw = getattr(message, "text", None)
    return raw if isinstance(raw, str) else str(getattr(message, "content", ""))


async def read_drawing_text(png_bytes: bytes) -> str:
    """把一张已渲染的图纸 PNG 交给视觉模型认字,返回纯文本(已去首尾空白)。

    走 llm.ainvoke(路径乙),自带缓存/中文错误/重试。**不接异常**:
    MissingAPIKeyError / LLMCallError 向上抛给工具层翻成中文 fail。
    认不出任何字时按提示词约定返回 EMPTY_MARKER,调用方据此当「空」。
    """
    data_uri = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
    messages = [
        SystemMessage(content=_prompt()),
        HumanMessage(
            content=[
                {"type": "text", "text": _USER_TEXT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        ),
    ]
    # max_retries=0 必须在**造模型时**传(model_copy 事后改无效,见 llm._for_direct_call);
    # 真正的重试由 llm.ainvoke 的 max_attempts 那层做。视觉档不传采样参数(get_chat_model 已处理)。
    model = llm.get_chat_model("vision", max_retries=0)
    answer = await llm.ainvoke(
        model, messages, cache_extra=_CACHE_EXTRA, max_attempts=_MAX_ATTEMPTS
    )
    return _message_text(answer).strip()


__all__ = ["EMPTY_MARKER", "read_drawing_text"]
