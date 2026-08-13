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

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from gyt.agents.knowledge.store import get_vectorstore
from gyt.config import get_settings
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.core.project_fs import SCOPE_GLOBAL, SCOPE_PROJECT

logger = logging.getLogger(__name__)

# 前端把「当前工地」(用户在界面上选中的项目)放进 run 的 config.configurable[这个键]。
# LLM 没在问句里点名项目时,检索按它兜底作用域 —— 修的正是「选了当前项目、问答还只查全局」。
PROJECT_CONFIG_KEY = "gyt_project_id"


def _project_from_config(config: RunnableConfig | None) -> str:
    """从运行配置里取前端选中的「当前工地」项目编号;没有就空串。

    config 由 LangGraph 一路透传到工具(含子图),前端 submit 时经 config.configurable 注入。
    """
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get(PROJECT_CONFIG_KEY) or "").strip()

_SEARCH_DESCRIPTION = (
    "在规范知识库里检索,回答施工/安全/消防/防火等**规范条文**问题。"
    "query 填要查的问题或关键词(如「消防车道要多宽」「疏散距离要求」)。"
    "project_id 选填:用户在问**某个具体项目**的规范/任务书时填该项目编号,会连该项目的资料一起查;"
    "不填就只查对所有项目通用的全局规范(查国标的常见场景)。"
    "返回若干条命中原文,每条带 source(文件名)和 page(页码)——回答时必须照抄这两个字段当出处。"
    "库里查不到会明确返回失败,这时如实告诉用户查不到,别自己编。"
)


def _scope_filter(project_id: str) -> dict[str, Any]:
    """检索作用域:无项目上下文只查全局规范;有项目则「全局 + 该项目」,永不串到别的项目。"""
    if project_id:
        return {
            "$or": [
                {"scope": SCOPE_GLOBAL},
                {"$and": [{"scope": SCOPE_PROJECT}, {"project_id": project_id}]},
            ]
        }
    return {"scope": SCOPE_GLOBAL}


def _scope_label(passage: dict[str, Any]) -> str:
    """把命中的作用域说成人话,进出处标注(全局规范 / 项目任务书 / 项目规范)。"""
    if passage.get("scope") == SCOPE_GLOBAL:
        return "全局规范"
    kind = "任务书" if passage.get("doc_type") == "task_book" else "项目规范"
    return f"{kind}·{passage.get('project_id') or '?'}"


def _search(query: str, k: int, where: dict[str, Any]) -> list[tuple[Any, float]]:
    """阻塞:按作用域过滤的向量检索,返回 [(Document, 距离)]。首次调用会把 BGE-M3 载入内存。"""
    return get_vectorstore().similarity_search_with_score(query, k=k, filter=where)


@tool("search_regulation", description=_SEARCH_DESCRIPTION)
@tool_guard
async def search_regulation(
    query: str, project_id: str = "", *, config: RunnableConfig
) -> Envelope:
    """检索规范,返回命中原文 + 出处(文件名 + 页码 + 作用域)。查不到 → EMPTY_RESULT(不硬答)。

    作用域优先级:工具入参 project_id(用户在问句里点名了项目)> 前端选中的「当前工地」
    (config.configurable[PROJECT_CONFIG_KEY])> 两者都空则只查全局规范。给了项目就查
    「全局 + 该项目」,永不串别的项目(内容级作用域)。config 是 LLM 看不到的注入参数。
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="你要查什么规范?说具体点,比如「消防车道要多宽」「疏散距离怎么要求」。",
        )

    settings = get_settings()
    # 入参优先(用户明确点名的项目),否则回退到前端选中的当前工地。
    effective_pid = project_id.strip() or _project_from_config(config)
    where = _scope_filter(effective_pid)
    hits = await asyncio.to_thread(_search, cleaned, settings.knowledge_top_k, where)

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
            detail=f"最近距离 {best} > 阈值 {max_dist};query={cleaned!r};项目={effective_pid!r}",
        )

    passages = [
        {
            "text": doc.page_content,
            "source": doc.metadata.get("source"),
            "page": doc.metadata.get("page"),
            "scope": doc.metadata.get("scope"),
            "doc_type": doc.metadata.get("doc_type"),
            "project_id": doc.metadata.get("project_id"),
            "score": round(float(score), 3),
        }
        for doc, score in kept
    ]
    top = passages[0]
    return ok(
        data={"passages": passages, "query": cleaned},
        user_msg=(
            f"查到 {len(passages)} 条相关规范,最相关的在《{top['source']}》"
            f"第 {top['page']} 页({_scope_label(top)})。"
        ),
    )


KNOWLEDGE_TOOLS: list = [search_regulation]
"""供 gyt.agents.knowledge 组装时使用。拿去用之前先 list(...) 复制一份,别原地 append。"""

__all__ = ["KNOWLEDGE_TOOLS", "PROJECT_CONFIG_KEY", "search_regulation"]
