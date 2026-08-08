"""CAD Agent 的工具集 —— 看 DXF 图纸:列图纸 / 概览 / 尺寸 / 构件 / 图层 / PNG 预览。

===========================================================================
每个工具的三段式开头(照抄 schedule/safety 的契约,别自创)
---------------------------------------------------------------------------
    @tool(name, description=中文) 在外、@tool_guard 在内(贴着函数),async def,返回 Envelope。
    入参用 ``drawing``(中文名或 32 位 id 都认,内部 _resolve_drawing 收敛成 id)。
    第一步一律 _load_index —— 内部 ensure_index:命中读盘、未命中就地解析并落盘。
    解析/渲染/读写盘全是阻塞 IO,一律 await asyncio.to_thread(...),否则 langgraph dev
    的 blockbuster 抛 BlockingError(schedule 的 sqlite、config 的 mkdir 都踩过同款坑)。

失败出口(全中文人话信封,内部细节只进 detail=):
    图名/ id 不认  → NOT_FOUND(并把现有图纸名列给用户)
    不是 .dxf     → FILE_UNSUPPORTED
    超 drawing_max_mb → FILE_TOO_LARGE
    ezdxf 打不开   → FILE_CORRUPT
    查不到内容     → EMPTY_RESULT(query_dimension 对「没标注的尺寸」如实说做不了,不硬编)
===========================================================================
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import ezdxf
from langchain_core.tools import tool

from gyt.agents.cad import index, render
from gyt.agents.cad.demo_registry import get_demo_drawings
from gyt.config import ALLOWED_CAD_EXT, get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard

logger = logging.getLogger(__name__)

_BYTES_PER_MB: Final[int] = 1024 * 1024
_PREVIEW_NAME: Final[str] = "preview.png"


# --- 图名解析与展示名 ---------------------------------------------------------


def _available_names() -> list[str]:
    return list(get_demo_drawings().keys())


def _drawings_hint() -> str:
    names = _available_names()
    return "、".join(names) if names else "(暂时一张都没有,演示前先重启后端预注册)"


def _resolve_drawing(drawing: str) -> str | Envelope:
    """把 ``drawing``(中文名或 id)收敛成 artifact_id;认不出返回 NOT_FOUND 信封。

    顺序(落地文档 1.5.2):① 已是合法 id 直接用;② 命中演示映射表的中文名换成 id;
    ③ 都不是 → fail(NOT_FOUND) 并把现有图纸名报给用户。
    """
    text = (drawing or "").strip()
    if artifacts.ARTIFACT_ID_RE.fullmatch(text):
        return text
    mapping = get_demo_drawings()
    if text in mapping:
        return mapping[text]
    return fail(
        ErrorCode.NOT_FOUND,
        user_msg=f"没找到「{text or '(空)'}」这张图纸。现在能看的有:{_drawings_hint()}。",
    )


def _display_name(drawing_id: str) -> str:
    """从 id 反查演示展示名,查不到就回一个中性词(不暴露 id 给用户)。"""
    for name, mapped_id in get_demo_drawings().items():
        if mapped_id == drawing_id:
            return name
    return "这张图纸"


async def _load_index(drawing: str) -> tuple[dict[str, Any] | None, str, Envelope | None]:
    """所有查询工具的第一步:名字→id→(格式/大小闸)→ensure_index。

    返回 (索引, drawing_id, 失败信封)。成功时失败信封为 None;失败时索引为 None。
    """
    resolved = _resolve_drawing(drawing)
    if isinstance(resolved, dict):  # 已经是 Envelope(NOT_FOUND)
        return None, "", resolved
    drawing_id = resolved

    try:
        meta = await asyncio.to_thread(artifacts.read_meta, drawing_id)
    except ArtifactNotFound:
        return None, drawing_id, fail(
            ErrorCode.NOT_FOUND,
            user_msg=f"这张图纸找不到了,可能没预注册。现在能看的有:{_drawings_hint()}。",
        )

    ext = str(meta.get("ext", "")).lower()
    if ext not in ALLOWED_CAD_EXT:
        kinds = "、".join(sorted(ALLOWED_CAD_EXT))
        return None, drawing_id, fail(
            ErrorCode.FILE_UNSUPPORTED,
            user_msg=f"这个文件不是图纸格式,我只看得了 {kinds}。",
        )

    settings = get_settings()
    size = int(meta.get("size_bytes", 0))
    if size > settings.drawing_max_mb * _BYTES_PER_MB:
        return None, drawing_id, fail(
            ErrorCode.FILE_TOO_LARGE,
            user_msg=(
                f"这张图纸有 {size / _BYTES_PER_MB:.1f}MB,超过了 "
                f"{settings.drawing_max_mb:.0f}MB 的上限,先精简一下再看。"
            ),
        )

    try:
        idx = await index.ensure_index(drawing_id)
    except ArtifactNotFound:
        return None, drawing_id, fail(
            ErrorCode.NOT_FOUND,
            user_msg=f"这张图纸的文件不见了。现在能看的有:{_drawings_hint()}。",
        )
    except ezdxf.DXFError as exc:  # DXFStructureError 等都是它的子类
        logger.info("图纸 %s 解析失败:%s", drawing_id, exc)
        return None, drawing_id, fail(
            ErrorCode.FILE_CORRUPT,
            user_msg="这张图纸打不开,可能文件传坏了或不是标准 DXF,换一张再看。",
            detail=f"ezdxf 解析 {drawing_id} 失败:{type(exc).__name__}: {exc}",
        )
    return idx, drawing_id, None


# --- 工具 --------------------------------------------------------------------

_LIST_DESCRIPTION = (
    "列出现在系统里能看的图纸名字。用户问「有哪些图纸」「都能看什么图」,"
    "或者你不知道该看哪张图时,先调它拿到图纸名单,再按名字去查。"
)


@tool("list_drawings", description=_LIST_DESCRIPTION)
@tool_guard
async def list_drawings() -> Envelope:
    """返回当前预注册的演示图纸名字清单。"""
    names = _available_names()
    if not names:
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg="现在系统里一张图纸都没有,演示前先重启后端把图纸预注册进来。",
        )
    return ok(
        data={"drawings": names},
        user_msg=f"现在能看的图纸有:{'、'.join(names)}。想看哪张就说名字。",
    )


_PARSE_DESCRIPTION = (
    "先把一张图纸整体过一遍,拿到概览(几个图层、多少图元、什么编码/版本/单位、外框范围)。"
    "用户说「看看这张图」「先打开首层平面图」这类先要个整体印象的,调它。"
    "drawing 填图纸名字(如「首层平面图」)或图纸编号。"
)


@tool("parse_drawing", description=_PARSE_DESCRIPTION)
@tool_guard
async def parse_drawing(drawing: str) -> Envelope:
    """整体概览一张图纸。"""
    idx, drawing_id, error = await _load_index(drawing)
    if error is not None:
        return error
    assert idx is not None

    layers_count = len(idx["layers"])
    entities_total = sum(idx["entities_by_kind"].values())
    name = _display_name(drawing_id)
    return ok(
        data={
            "layers_count": layers_count,
            "entities_total": entities_total,
            "encoding": idx["encoding"],
            "dxf_version": idx["dxf_version"],
            "units_label": idx["units_label"],
            "bounds": idx["bounds"],
        },
        user_msg=(
            f"{name}看过了:{layers_count} 个图层、{entities_total} 个图元"
            f"(单位 {idx['units_label']})。能问尺寸、构件、图层了。"
        ),
    )


_DIM_DESCRIPTION = (
    "读图纸上**已经标注**的尺寸读数。target 填要找的尺寸关键词(如「柱距」「标注」"
    "或某个图层名),留空则把图上所有标注都列出来。"
    "注意:它只报图纸上画出来的标注,图上没标的尺寸它答不了,会如实说做不了 ——"
    "不要用它去「估」一个没标注的尺寸。drawing 填图纸名字或编号。"
)


@tool("query_dimension", description=_DIM_DESCRIPTION)
@tool_guard
async def query_dimension(drawing: str, target: str = "") -> Envelope:
    """读图纸已有的 DIMENSION 标注(落地文档 6.2:只读标注,不算轴网间距)。"""
    idx, drawing_id, error = await _load_index(drawing)
    if error is not None:
        return error
    assert idx is not None

    dims: list[dict[str, Any]] = idx["dimensions"]
    key = (target or "").strip()
    if key:
        matched = [
            d for d in dims if key in str(d.get("layer", "")) or key in str(d.get("text", ""))
        ]
    else:
        matched = list(dims)

    name = _display_name(drawing_id)
    if not matched:
        # 如实说做不了,别硬编(落地文档 6.2 的核心):图上没标就是没标。
        if not dims:
            hint = f"{name}上没有任何标注尺寸,"
        else:
            hint = f"{name}上没找到和「{key}」对得上的标注,"
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=f"{hint}我只能读图纸上**已经标注**的尺寸,图上没标的量不了。",
        )

    units = idx["units_label"]
    readings = [
        {
            "text": d["text"],
            "measurement": d["measurement"],
            "layer": d["layer"],
            "kind": d["kind"],
            "units_label": units,
        }
        for d in matched
    ]
    shown = "、".join(f"{r['text']}{units}" for r in readings)
    return ok(
        data={"dimensions": readings, "units_label": units},
        user_msg=f"{name}上标注的尺寸(读数以标注为准):{shown}。",
    )


_COMPONENTS_DESCRIPTION = (
    "数图纸上的构件:某种块/图元有几个、集中在哪个图层。kind 填构件或图元类型"
    "(如块名「柱-KZ1」、或 LINE/CIRCLE/INSERT 这类图元类型);layer 填只看某个图层。"
    "两个都留空则给出全图的块清单和图元类型分布。drawing 填图纸名字或编号。"
)


@tool("list_components", description=_COMPONENTS_DESCRIPTION)
@tool_guard
async def list_components(drawing: str, kind: str = "", layer: str = "") -> Envelope:
    """数构件/图元,可按图层或类型/块名筛(落地文档 6.3)。"""
    idx, drawing_id, error = await _load_index(drawing)
    if error is not None:
        return error
    assert idx is not None

    layers: list[dict[str, Any]] = idx["layers"]
    blocks: list[dict[str, Any]] = idx["blocks"]
    by_kind: dict[str, int] = idx["entities_by_kind"]
    name = _display_name(drawing_id)
    layer_key = (layer or "").strip()
    kind_key = (kind or "").strip()

    if layer_key:
        hit = next((ly for ly in layers if ly["name"] == layer_key), None)
        if hit is None or hit["entity_count"] == 0:
            return fail(
                ErrorCode.EMPTY_RESULT,
                user_msg=f"{name}上没有「{layer_key}」这个图层,或者它是空的。",
            )
        kinds_desc = "、".join(f"{k} {v}个" for k, v in hit["kinds"].items())
        return ok(
            data={"layer": layer_key, "entity_count": hit["entity_count"], "kinds": hit["kinds"]},
            user_msg=f"{name}「{layer_key}」层有 {hit['entity_count']} 个图元:{kinds_desc}。",
        )

    if kind_key:
        # 先当块名匹配(含模糊包含),再当图元类型匹配。
        block_hits = [b for b in blocks if kind_key == b["name"] or kind_key in b["name"]]
        if block_hits:
            layers_with = [ly["name"] for ly in layers if "INSERT" in ly["kinds"]]
            parts = "、".join(f"{b['name']} {b['insert_count']}处" for b in block_hits)
            where = f",都在「{'、'.join(layers_with)}」层" if layers_with else ""
            return ok(
                data={"blocks": block_hits, "insert_layers": layers_with},
                user_msg=f"{name}上{parts}{where}。",
            )
        ktype = kind_key.upper()
        if ktype in by_kind:
            where = [
                {"layer": ly["name"], "count": ly["kinds"][ktype]}
                for ly in layers
                if ktype in ly["kinds"]
            ]
            where_desc = "、".join(f"{w['layer']}({w['count']})" for w in where) or "(未分层)"
            return ok(
                data={"kind": ktype, "count": by_kind[ktype], "layers": where},
                user_msg=f"{name}上 {ktype} 共 {by_kind[ktype]} 个,分布在:{where_desc}。",
            )
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=f"{name}上没找到「{kind_key}」这类构件。可以先问问这张图有哪些图层/构件。",
        )

    # 都没填:给全图概览。
    block_desc = "、".join(f"{b['name']}({b['insert_count']})" for b in blocks) or "没有自定义块"
    kind_desc = "、".join(f"{k} {v}" for k, v in by_kind.items())
    return ok(
        data={"blocks": blocks, "entities_by_kind": by_kind},
        user_msg=f"{name}的构件块:{block_desc};图元类型分布:{kind_desc}。",
    )


_LAYERS_DESCRIPTION = (
    "列出图纸的所有图层,每层有多少图元、什么颜色。用户问「这张图有哪些图层」"
    "「图层情况」时调它。drawing 填图纸名字或编号。"
)


@tool("layer_stats", description=_LAYERS_DESCRIPTION)
@tool_guard
async def layer_stats(drawing: str) -> Envelope:
    """图层清单与各层图元数(落地文档 6.4)。"""
    idx, drawing_id, error = await _load_index(drawing)
    if error is not None:
        return error
    assert idx is not None

    layers: list[dict[str, Any]] = idx["layers"]
    name = _display_name(drawing_id)
    # 非空图层排前面、按图元数降序,让「主力图层」一眼可见。
    ordered = sorted(layers, key=lambda ly: ly["entity_count"], reverse=True)
    nonempty = [ly for ly in ordered if ly["entity_count"] > 0]
    head = "、".join(f"{ly['name']}({ly['entity_count']})" for ly in nonempty[:5]) or "各层都是空的"
    return ok(
        data={"layers": ordered, "layers_count": len(layers)},
        user_msg=f"{name}共 {len(layers)} 个图层,主要有:{head}。",
    )


_PREVIEW_DESCRIPTION = (
    "把整张图纸渲染成一张 PNG 预览图,方便用户直接看图。"
    "用户说「看看这张图长啥样」「出个预览」时调它。drawing 填图纸名字或编号。"
)


@tool("render_preview", description=_PREVIEW_DESCRIPTION)
@tool_guard
async def render_preview(drawing: str) -> Envelope:
    """渲染整图 PNG,落盘为产物,返回 png_id(落地文档 6.5;MVP 只出图不接视觉问答)。"""
    idx, drawing_id, error = await _load_index(drawing)
    if error is not None:
        return error
    assert idx is not None

    path = await asyncio.to_thread(artifacts.resolve, drawing_id)
    try:
        png_bytes = await asyncio.to_thread(render.to_png, path)
    except ezdxf.DXFError as exc:
        logger.info("图纸 %s 渲染失败:%s", drawing_id, exc)
        return fail(
            ErrorCode.FILE_CORRUPT,
            user_msg="这张图纸渲染不出来,可能文件有问题,换一张再试。",
            detail=f"render {drawing_id} 失败:{type(exc).__name__}: {exc}",
        )

    png_id = await asyncio.to_thread(
        artifacts.register, png_bytes, kind=ArtifactKind.OTHER, original_name=_PREVIEW_NAME
    )
    name = _display_name(drawing_id)
    return ok(
        data={"png_id": png_id, "layers_count": len(idx["layers"])},
        user_msg=f"{name}的预览图出好了(编号 {png_id}),{len(idx['layers'])} 个图层都画上了。",
    )


CAD_TOOLS: list = [
    list_drawings,
    parse_drawing,
    query_dimension,
    list_components,
    layer_stats,
    render_preview,
]
"""供 gyt.agents.cad 组装时使用。拿去用之前先 list(...) 复制一份,别原地 append。"""

__all__ = [
    "CAD_TOOLS",
    "layer_stats",
    "list_components",
    "list_drawings",
    "parse_drawing",
    "query_dimension",
    "render_preview",
]
