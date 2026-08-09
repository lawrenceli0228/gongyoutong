"""Knowledge Agent 的工具:规范检索(带页码出处)。

===========================================================================
契约(照抄,别自创)
---------------------------------------------------------------------------
    @tool 在外、@tool_guard 在内,async,返回 Envelope。向量检索是阻塞 IO/CPU
    (首次还会把 BGE-M3 加载进内存),一律 await asyncio.to_thread(...)。

命脉:每条命中都带 source(**完整文件名**)+ page(PDF 物理页)——评测判分要
    source + page 都对(见 eval/scorers.py::score_rag)。这两个字段照抄向量库 metadata,
    agent 再照抄给用户,谁都不许改、不许自己补页码。

「无依据」怎么判(no_answer 的可靠性靠它):
    Chroma 返回**距离**(越小越近)。最近的一条都比 knowledge_max_distance 还远,
    就判「知识库里没有」,返回 fail(EMPTY_RESULT)。agent 据此如实说查不到,不硬答。
    (阈值按语料标定,见 config。双保险:prompt 里还要求 agent 若命中的原文其实答不上
     问题,也照样说无依据。)
===========================================================================
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import tool

from gyt.agents.knowledge.store import get_vectorstore
from gyt.config import get_settings
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard

logger = logging.getLogger(__name__)

_SEARCH_DESCRIPTION = (
    "在规范知识库里检索,回答施工/安全/消防/防火等**规范条文**问题。"
    "query 填要查的问题或关键词(如「消防车道要多宽」「疏散距离要求」)。"
    "返回若干条命中的规范原文,每条带 source(规范文件名)和 page(页码)——"
    "回答时必须照抄这两个字段当出处。库里查不到会明确返回失败,这时如实告诉用户查不到,别自己编。"
)


def _search(query: str, k: int) -> list[tuple[Any, float]]:
    """阻塞:向量检索,返回 [(Document, 距离)]。首次调用会把 BGE-M3 载入内存。"""
    return get_vectorstore().similarity_search_with_score(query, k=k)


@tool("search_regulation", description=_SEARCH_DESCRIPTION)
@tool_guard
async def search_regulation(query: str) -> Envelope:
    """检索规范,返回命中原文 + 出处(文件名 + 页码)。查不到 → EMPTY_RESULT(不硬答)。"""
    cleaned = (query or "").strip()
    if not cleaned:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="你要查什么规范?说具体点,比如「消防车道要多宽」「疏散距离怎么要求」。",
        )

    settings = get_settings()
    hits = await asyncio.to_thread(_search, cleaned, settings.knowledge_top_k)

    max_dist = settings.knowledge_max_distance
    kept = [(doc, score) for doc, score in hits if score <= max_dist]
    if not kept:
        # 一条够近的都没有 → 如实说没有,别硬答(no_answer 路径)。
        best = f"{min(s for _, s in hits):.3f}" if hits else "无命中"
        return fail(
            ErrorCode.EMPTY_RESULT,
            # 开头必须用「知识库里查不到」这种规定措辞:no_answer 判定只看答案开头的表态词
            # (知识库+里+查不到/没有/无依据…),放后面不算(见 scorers 的 _admits_no_answer)。
            user_msg=(
                f"知识库里查不到和「{cleaned}」对得上的规范条文。"
                "我只答规范里写了的,查不到就不编——建议问安全员或直接查原规范。"
            ),
            detail=f"最近距离 {best} > 阈值 {max_dist};query={cleaned!r}",
        )

    passages = [
        {
            "text": doc.page_content,
            "source": doc.metadata.get("source"),
            "page": doc.metadata.get("page"),
            "score": round(float(score), 3),
        }
        for doc, score in kept
    ]
    top = passages[0]
    return ok(
        data={"passages": passages, "query": cleaned},
        user_msg=(
            f"查到 {len(passages)} 条相关规范,最相关的在《{top['source']}》第 {top['page']} 页。"
        ),
    )


KNOWLEDGE_TOOLS: list = [search_regulation]
"""供 gyt.agents.knowledge 组装时使用。拿去用之前先 list(...) 复制一份,别原地 append。"""

__all__ = ["KNOWLEDGE_TOOLS", "search_regulation"]
