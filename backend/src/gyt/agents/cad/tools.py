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
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pypdf.errors import PyPdfError

from gyt.agents.cad import index, render
from gyt.agents.cad.demo_registry import get_demo_drawings
from gyt.config import ALLOWED_DRAWING_EXT, get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.core.run_context import project_from_config
from gyt.db import projects as db

logger = logging.getLogger(__name__)

_BYTES_PER_MB: Final[int] = 1024 * 1024
_PREVIEW_NAME: Final[str] = "preview.png"

# 视图类型的人话标签(drawings.view_type ∈ plan/elevation/section)。
_VIEW_CN: Final[dict[str, str]] = {"plan": "平面图", "elevation": "立面图", "section": "剖面图"}

# 天正构件标签 → 人话构件名。认不出的落回原标签,不假装认得。
# 两类键都在这里(对应 parse._detect_tianzheng 的两种加载形态):
#   · ``TCH_*`` —— 形态①,实体直载为私有类型,标签即 TCH_ 类型名;
#   · 英文图层名 —— 形态②,天正构件被存成通用 ACAD_PROXY_ENTITY、拿不到逐个类型,
#     parse 退一步按图层给它们计数,标签就是天正标准英文图层名(COLUMN/WALL/…)。
_TCH_CN: Final[dict[str, str]] = {
    # 形态①:TCH_* 私有类型
    "TCH_WALL": "墙",
    "TCH_COLUMN": "柱",
    "TCH_WINDOW": "窗",
    "TCH_DOOR": "门",
    "TCH_OPENING": "洞口",
    "TCH_STAIR": "楼梯",
    "TCH_AXIS": "轴线",
    "TCH_DIM": "标注",
    "TCH_TEXT": "文字",
    "TCH_BALCONY": "阳台",
    "TCH_RAILING": "栏杆",
    "TCH_ROOF": "屋顶",
    # 形态②:天正标准英文图层名(proxy 构件按图层归类后的标签)
    "WALL": "墙",
    "COLUMN": "柱",
    "WINDOW": "窗",
    "DOOR": "门",
    "CURTWALL": "幕墙",
    "OPENING": "洞口",
    "STAIR": "楼梯",
    "AXIS": "轴线",
    "SPACE": "房间",
    "BALCONY": "阳台",
    "RAILING": "栏杆",
    "ROOF": "屋顶",
}

# 交给用户的「把天正图变成能读的图」的正确步骤 —— 三条路,从推荐到应急。
# 这段是给工地/设计院的人照着做的:天正把墙/柱/门窗锁在私有构件里,连正版 AutoCAD 不装
# 天正插件也看不到几何;唯一的路是在天正里把它们导成普通 AutoCAD 图元。
_TIANZHENG_STEPS: Final[str] = (
    "要我读得了几何,得先在天正里把私有构件导成普通 AutoCAD 图元(下面三条路,优先用第①条):"
    "① 天正菜单「文件布图 → 图形导出」(命令 TEXP),弹出的保存类型选「低版本 AutoCAD(2004/2007)」"
    "或直接选 *.dxf —— 墙/柱/门窗会被转成双线、块、圆弧等基本图元,几何保留得最好;"
    "② 或用「另存为」选「天正3(T3)格式」,效果接近、操作更快;"
    "③ 应急:全选后输 X(EXPLODE)回车炸开(天正对象是嵌套的,可能要连炸两次)再另存 DXF —— "
    "这条常丢标注数值、把文字打散,能不用就别用。"
    "导出后把新的 DXF 重新上传,图层、构件、标注就都能正常查了。"
)


def _is_pdf(idx: dict[str, Any]) -> bool:
    """索引是不是 PDF 图纸(vs DXF)。老索引没有 format 字段时按 DXF 处理(兼容)。"""
    return idx.get("format") == "pdf"


# PDF 图纸做不了结构化查询(图层/构件/标注读数)时的统一说法 —— 指路到「能做的两样」。
_PDF_STRUCTURED_UNAVAILABLE: Final[str] = (
    "这是 PDF 图纸,里面没有图层/构件/标注这些结构化对象(那是 DXF 才有的),"
    "所以这项查不了。PDF 上能做两样:出预览图看整张图,或读图上写的文字(标高、房间名、标注数字等)。"
)


def _tianzheng(idx: dict[str, Any]) -> dict[str, Any]:
    """从索引里取天正检出结果;老索引没有这个字段时给个「未检出」的兜底。"""
    return idx.get("tianzheng") or {"detected": False, "component_kinds": {}}


def _tianzheng_summary(component_kinds: dict[str, int]) -> str:
    """把 {TCH_WALL: 66, ...} 说成「66 墙、34 门窗…」,按数量降序,认不出的用原类型名。"""
    ordered = sorted(component_kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    return "、".join(f"{count} {_TCH_CN.get(kind, kind)}" for kind, count in ordered)


# --- 图名解析与展示名 ---------------------------------------------------------


async def _ensure_registered() -> None:
    """把「首次预注册」这步的阻塞 IO 挪进线程池。

    get_demo_drawings 第一次被调用时会扫目录、读 DXF、写 artifacts —— 全是同步阻塞 IO。
    若在 async 工具里直接同步调它,`langgraph dev` 的 blockbuster 会抛 BlockingError,
    工具被 tool_guard 兜成 INTERNAL 失败(现象:cad「没返回结果」)。这里用 to_thread
    先把 lru_cache 焐热;之后 _resolve_drawing / _display_name 里的同步调用都是纯缓存命中
    (只返回 dict,不碰盘),不再阻塞事件循环。每个工具入口调一次即可。
    """
    await asyncio.to_thread(get_demo_drawings)


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


async def _load_index(
    drawing: str, project_id: str = ""
) -> tuple[dict[str, Any] | None, str, Envelope | None]:
    """所有查询工具的第一步:名字→id→(格式/大小闸)→ensure_index。

    返回 (索引, drawing_id, 失败信封)。成功时失败信封为 None;失败时索引为 None。

    作用域(与 knowledge 同一套「当前工地」):demo 图纸视同全局、任何时候都认;按**图名**找
    上传入库的项目图时,给了 project_id 就**只在该项目内**找(选了工地就不串到别的项目),
    没给才跨项目找(旧行为)。用户直接给 id 的一律照认,不受作用域限制。
    """
    await _ensure_registered()  # 首次预注册的阻塞 IO 挪进线程池,防 blockbuster
    resolved = _resolve_drawing(drawing)
    if isinstance(resolved, dict):
        # id / demo 名都没命中 → 再查上传入库的项目图纸(按展示名)。选了工地就限定在该项目内,
        # 否则跨项目。同步 sqlite 属阻塞 IO,必须 to_thread,否则 blockbuster 抛 BlockingError。
        title = (drawing or "").strip()
        if project_id:
            row = await asyncio.to_thread(db.resolve_by_title, project_id, title)
        else:
            row = await asyncio.to_thread(db.find_drawing_by_title, title)
        if row is None:
            return None, "", resolved  # demo/id/项目图都没有 → 原样返回 NOT_FOUND
        drawing_id = row.artifact_id
    else:
        drawing_id = resolved

    try:
        meta = await asyncio.to_thread(artifacts.read_meta, drawing_id)
    except ArtifactNotFound:
        return (
            None,
            drawing_id,
            fail(
                ErrorCode.NOT_FOUND,
                user_msg=f"这张图纸找不到了,可能没预注册。现在能看的有:{_drawings_hint()}。",
            ),
        )

    ext = str(meta.get("ext", "")).lower()
    if ext not in ALLOWED_DRAWING_EXT:
        kinds = "、".join(sorted(ALLOWED_DRAWING_EXT))
        return (
            None,
            drawing_id,
            fail(
                ErrorCode.FILE_UNSUPPORTED,
                user_msg=f"这个文件不是图纸格式,我只看得了 {kinds}。",
            ),
        )

    settings = get_settings()
    size = int(meta.get("size_bytes", 0))
    # PDF 图纸用文档上限、DXF 用图纸上限(两者当前都放到 100MB,但口径分开、别写死一个)。
    limit_mb = settings.document_max_mb if ext == ".pdf" else settings.drawing_max_mb
    if size > limit_mb * _BYTES_PER_MB:
        return (
            None,
            drawing_id,
            fail(
                ErrorCode.FILE_TOO_LARGE,
                user_msg=(
                    f"这张图纸有 {size / _BYTES_PER_MB:.1f}MB,超过了 "
                    f"{limit_mb:.0f}MB 的上限,先精简一下再看。"
                ),
            ),
        )

    try:
        idx = await index.ensure_index(drawing_id)
    except ArtifactNotFound:
        return (
            None,
            drawing_id,
            fail(
                ErrorCode.NOT_FOUND,
                user_msg=f"这张图纸的文件不见了。现在能看的有:{_drawings_hint()}。",
            ),
        )
    except (ezdxf.DXFError, PyPdfError) as exc:  # DXF/PDF 两条线的解析异常都在这里收口
        logger.info("图纸 %s 解析失败:%s", drawing_id, exc)
        return (
            None,
            drawing_id,
            fail(
                ErrorCode.FILE_CORRUPT,
                user_msg="这张图纸打不开,可能文件传坏了或格式不标准(DXF/PDF),换一张再看。",
                detail=f"解析 {drawing_id} 失败:{type(exc).__name__}: {exc}",
            ),
        )
    return idx, drawing_id, None


# --- 工具 --------------------------------------------------------------------

_LIST_DESCRIPTION = (
    "列出现在系统里能看的图纸名字。用户问「有哪些图纸」「都能看什么图」,"
    "或者你不知道该看哪张图时,先调它拿到图纸名单,再按名字去查。"
)


@tool("list_drawings", description=_LIST_DESCRIPTION)
@tool_guard
async def list_drawings(*, config: RunnableConfig) -> Envelope:
    """返回能看的图纸:演示预注册的 + 上传入库的项目图纸(按项目 + 视图分组)。

    ⚠️ 选了当前工地时**只列该项目的图纸**(不掺 demo、更不列别的项目)—— 修「问项目2的图层
    却报出项目1的图」:根源就是这里把所有项目的图都端出来,LLM 顺手挑了别项目的。
    没选工地才回到「demo + 全部项目」的旧行为。用户直接报图名的仍按名字找(_resolve_drawing)。
    """
    await _ensure_registered()  # 首次预注册的阻塞 IO 挪进线程池,防 blockbuster
    project_id = project_from_config(config)
    logger.info("list_drawings 作用域 project_id=%r", project_id)
    # 选了工地:只查该项目的图;没选:查全部。同步 sqlite → to_thread。
    uploaded = await asyncio.to_thread(db.list_drawings, project_id=project_id or None)
    # 选了工地就不掺 demo(那是全局样例,会把 LLM 从「本项目的图」上带偏)。
    demo_names = [] if project_id else _available_names()

    # 选了工地就把工地名报出来 —— 让答复**锚定当前项目**,别再来回串(名字比编号好记)。
    proj_label = ""
    if project_id:
        proj = await asyncio.to_thread(db.get_project, project_id)
        proj_label = f"当前工地「{proj.name}」" if proj else f"当前工地(编号 {project_id})"

    if not demo_names and not uploaded:
        msg = (
            f"{proj_label}现在还没有图纸,先在上传面板把图纸传到这个工地下。"
            if project_id
            else "现在一张图纸都没有 —— 演示图先重启后端预注册,项目图先在上传面板传进来。"
        )
        return fail(ErrorCode.EMPTY_RESULT, user_msg=msg)

    if project_id:
        # 作用域只剩当前工地:直接「当前工地『X』的图纸:…」,一句话锚死是谁的图。
        titles = [f"{r.title}({_VIEW_CN.get(r.view_type, r.view_type)})" for r in uploaded]
        user_msg = f"{proj_label}的图纸:{'、'.join(titles)}。想看哪张就说名字。"
    else:
        parts: list[str] = []
        if demo_names:
            parts.append(f"演示图纸:{'、'.join(demo_names)}")
        by_project: dict[str, list[str]] = {}
        for row in uploaded:
            label = _VIEW_CN.get(row.view_type, row.view_type)
            by_project.setdefault(row.project_id, []).append(f"{row.title}({label})")
        for pid, titles in by_project.items():
            parts.append(f"项目 {pid}:{'、'.join(titles)}")
        user_msg = "；".join(parts) + "。想看哪张就说名字。"

    return ok(
        data={
            "demo": demo_names,
            "uploaded": [
                {"project_id": r.project_id, "title": r.title, "view_type": r.view_type}
                for r in uploaded
            ],
        },
        user_msg=user_msg,
    )


_PARSE_DESCRIPTION = (
    "先把一张图纸整体过一遍,拿到概览(几个图层、多少图元、什么编码/版本/单位、外框范围)。"
    "用户说「看看这张图」「先打开首层平面图」这类先要个整体印象的,调它。"
    "drawing 填图纸名字(如「首层平面图」)或图纸编号。"
)


@tool("parse_drawing", description=_PARSE_DESCRIPTION)
@tool_guard
async def parse_drawing(drawing: str, *, config: RunnableConfig) -> Envelope:
    """整体概览一张图纸。选中工地时按当前项目找图(见 _load_index)。"""
    idx, drawing_id, error = await _load_index(drawing, project_from_config(config))
    if error is not None:
        return error
    assert idx is not None

    name = _display_name(drawing_id)

    if _is_pdf(idx):
        # PDF 图纸没有图层/图元的概念,概览换成「几页、能不能读文字」,并说清能做的两样。
        page_count = int(idx.get("page_count", 0))
        has_text = bool(idx.get("has_text", False))
        if has_text:
            tail = (
                "能出预览图,也能读图上写的文字(标高、房间名、标注数字等)。"
                "图层/构件/标注读数是 DXF 专有,PDF 给不了。"
            )
        else:
            tail = "看着像扫描件,读不到文字;能出预览图看整张,但图上文字/图层/构件都读不了。"
        return ok(
            data={"format": "pdf", "page_count": page_count, "has_text": has_text},
            user_msg=f"{name}是 PDF 图纸,共 {page_count} 页。{tail}",
        )

    layers_count = len(idx["layers"])
    entities_total = sum(idx["entities_by_kind"].values())
    tz = _tianzheng(idx)

    if tz["detected"]:
        # 天正图:如实说清「构件锁在私有格式里、我读不到几何」,并给出导出 T3 的正确步骤。
        # 不再假装这是一张普通图 —— 图层数/图元数照给(那些是真的),但重点是那句「先导出」。
        summary = _tianzheng_summary(tz["component_kinds"])
        user_msg = (
            f"{name}是天正(TArch)格式的图,里面含 {summary} —— 这些是天正私有构件,"
            f"锁在它自家格式里,我现在读不到它们的几何(正版 AutoCAD 不装天正插件也一样看不到)。"
            f"{_TIANZHENG_STEPS}"
        )
    else:
        user_msg = (
            f"{name}看过了:{layers_count} 个图层、{entities_total} 个图元"
            f"(单位 {idx['units_label']})。能问尺寸、构件、图层了。"
        )

    return ok(
        data={
            "layers_count": layers_count,
            "entities_total": entities_total,
            "encoding": idx["encoding"],
            "dxf_version": idx["dxf_version"],
            "units_label": idx["units_label"],
            "bounds": idx["bounds"],
            "tianzheng": tz,
        },
        user_msg=user_msg,
    )


_DIM_DESCRIPTION = (
    "读图纸上**已经标注**的尺寸读数。target 填要找的尺寸关键词(如「柱距」「标注」"
    "或某个图层名),留空则把图上所有标注都列出来。"
    "注意:它只报图纸上画出来的标注,图上没标的尺寸它答不了,会如实说做不了 ——"
    "不要用它去「估」一个没标注的尺寸。drawing 填图纸名字或编号。"
)


@tool("query_dimension", description=_DIM_DESCRIPTION)
@tool_guard
async def query_dimension(drawing: str, target: str = "", *, config: RunnableConfig) -> Envelope:
    """读图纸已有的 DIMENSION 标注(落地文档 6.2:只读标注,不算轴网间距)。"""
    idx, drawing_id, error = await _load_index(drawing, project_from_config(config))
    if error is not None:
        return error
    assert idx is not None

    if _is_pdf(idx):
        # PDF 没有 DIMENSION 对象,读不了标注读数 —— 如实说,并指路到「读参数/看图上文字」。
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=(
                f"{_display_name(drawing_id)}是 PDF 图纸,没有可读的标注对象。"
                "图上标注的数字如果是文字,可以用『读参数/看图上文字』去问;"
                "但结构化的标注读数只有 DXF 图能给。"
            ),
        )

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
        tz = _tianzheng(idx)
        if not dims and tz["detected"]:
            # 天正图读不到标注,不是「图上没标」,是标注锁在 TCH_* 私有构件里 —— 别误导用户。
            return fail(
                ErrorCode.EMPTY_RESULT,
                user_msg=f"{name}是天正图,标注是天正私有构件,我读不到它的读数。{_TIANZHENG_STEPS}",
            )
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
async def list_components(
    drawing: str, kind: str = "", layer: str = "", *, config: RunnableConfig
) -> Envelope:
    """数构件/图元,可按图层或类型/块名筛(落地文档 6.3)。"""
    idx, drawing_id, error = await _load_index(drawing, project_from_config(config))
    if error is not None:
        return error
    assert idx is not None

    if _is_pdf(idx):
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=f"{_display_name(drawing_id)}——{_PDF_STRUCTURED_UNAVAILABLE}",
        )

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
    tz = _tianzheng(idx)
    # 天正图:构件是 TCH_* 私有对象(会出现在图元类型分布里),不是普通块,提示先导出 T3。
    tz_note = (
        f" 其中 {_tianzheng_summary(tz['component_kinds'])} 是天正私有构件,我读不到几何 —— "
        f"{_TIANZHENG_STEPS}"
        if tz["detected"]
        else ""
    )
    return ok(
        data={"blocks": blocks, "entities_by_kind": by_kind, "tianzheng": tz},
        user_msg=f"{name}的构件块:{block_desc};图元类型分布:{kind_desc}。{tz_note}",
    )


_LAYERS_DESCRIPTION = (
    "列出图纸的所有图层,每层有多少图元、什么颜色。用户问「这张图有哪些图层」"
    "「图层情况」时调它。drawing 填图纸名字或编号。"
)


@tool("layer_stats", description=_LAYERS_DESCRIPTION)
@tool_guard
async def layer_stats(drawing: str, *, config: RunnableConfig) -> Envelope:
    """图层清单与各层图元数(落地文档 6.4)。"""
    idx, drawing_id, error = await _load_index(drawing, project_from_config(config))
    if error is not None:
        return error
    assert idx is not None

    if _is_pdf(idx):
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=f"{_display_name(drawing_id)}——{_PDF_STRUCTURED_UNAVAILABLE}",
        )

    layers: list[dict[str, Any]] = idx["layers"]
    name = _display_name(drawing_id)
    # 非空图层排前面、按图元数降序,让「主力图层」一眼可见。
    ordered = sorted(layers, key=lambda ly: ly["entity_count"], reverse=True)
    nonempty = [ly for ly in ordered if ly["entity_count"] > 0]
    head = "、".join(f"{ly['name']}({ly['entity_count']})" for ly in nonempty[:5]) or "各层都是空的"
    # 天正图的图层名/计数能读(组码 8),但墙/柱几何还锁着 —— 顺带提示一句,别让用户以为能查几何。
    tz_note = f" 另外这是天正图,{_TIANZHENG_STEPS}" if _tianzheng(idx)["detected"] else ""
    return ok(
        data={"layers": ordered, "layers_count": len(layers)},
        user_msg=f"{name}共 {len(layers)} 个图层,主要有:{head}。{tz_note}",
    )


_PREVIEW_DESCRIPTION = (
    "把整张图纸渲染成一张 PNG 预览图,方便用户直接看图。"
    "用户说「看看这张图长啥样」「出个预览」时调它。drawing 填图纸名字或编号。"
)


@tool("render_preview", description=_PREVIEW_DESCRIPTION)
@tool_guard
async def render_preview(drawing: str, *, config: RunnableConfig) -> Envelope:
    """渲染整图 PNG,落盘为产物,返回 png_id(落地文档 6.5;MVP 只出图不接视觉问答)。"""
    idx, drawing_id, error = await _load_index(drawing, project_from_config(config))
    if error is not None:
        return error
    assert idx is not None

    name = _display_name(drawing_id)

    if _is_pdf(idx):
        # PDF 走 pypdfium2 栅格化首页,没有「图元太多」这道 matplotlib 专属的拦阻。
        path = await asyncio.to_thread(artifacts.resolve, drawing_id)
        try:
            png_bytes = await asyncio.to_thread(render.pdf_to_png, path)
        except PyPdfError as exc:
            logger.info("PDF 图纸 %s 渲染失败:%s", drawing_id, exc)
            return fail(
                ErrorCode.FILE_CORRUPT,
                user_msg="这张 PDF 图纸渲染不出来,可能文件有问题,换一张再试。",
                detail=f"pdf render {drawing_id} 失败:{type(exc).__name__}: {exc}",
            )
        png_id = await asyncio.to_thread(
            artifacts.register, png_bytes, kind=ArtifactKind.OTHER, original_name=_PREVIEW_NAME
        )
        return ok(
            data={"png_id": png_id, "format": "pdf", "page_count": int(idx.get("page_count", 0))},
            user_msg=(
                f"{name}的预览图渲染好了(首页,编号 {png_id})。"
                "注:预览图暂时不在聊天里直接显示,这个编号先留着备用。"
            ),
        )

    # 渲染前先按图元数拦一道:真实工程图上千图元,matplotlib 逐个画会卡几分钟(实测 268s),
    # 而且大地坐标系的真图渲染出来常是空白。超阈值就**不渲染、如实说**,别硬撑到超时。
    entities_total = sum(idx["entities_by_kind"].values())
    max_entities = get_settings().drawing_render_max_entities
    if entities_total > max_entities:
        return fail(
            ErrorCode.FILE_TOO_LARGE,
            user_msg=(
                f"{name}图元太多({entities_total} 个),生成预览会很慢,这次先没出。"
                "不过图层、构件、标注尺寸都能正常查——你想看哪样直接说。"
            ),
            detail=f"render 跳过:entities={entities_total} > {max_entities}",
        )

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
    return ok(
        data={"png_id": png_id, "layers_count": len(idx["layers"])},
        user_msg=(
            f"{name}的预览图渲染好了(编号 {png_id})。"
            "注:预览图暂时不在聊天里直接显示,这个编号先留着备用。"
        ),
    )


_PROJECTS_DESCRIPTION = (
    "列出系统里有哪些工地/项目。用户问「有哪些项目」「哪些工地」,或要按项目找图纸时,先调它。"
)


@tool("list_projects", description=_PROJECTS_DESCRIPTION)
@tool_guard
async def list_projects() -> Envelope:
    """列出已建项目(供用户按项目找图)。"""
    rows = await asyncio.to_thread(db.list_projects)  # 同步 sqlite → to_thread
    if not rows:
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg="现在还没有建任何项目。在上传面板新建项目、把图纸传进来就能看了。",
        )
    listed = "、".join(f"{r.name}({r.id})" for r in rows)
    return ok(
        data={"projects": [{"id": r.id, "name": r.name} for r in rows]},
        user_msg=f"现在有这些项目:{listed}。",
    )


_VIEW_PARAMS_DESCRIPTION = (
    "读一张图的**平立剖参数**:图上已标注的尺寸 + 图上写的文字(标高、层高、房间名等常是文字)。"
    "用户问「这张立面图的标高」「层高多少」「这图上标了哪些尺寸和文字」时调它。"
    "drawing 填图纸名字或编号。注意:它只报图上**已经标/写**的,图上没有的它答不了,会如实说 ——"
    "不要拿它去估一个没标的参数。"
)


@tool("read_view_params", description=_VIEW_PARAMS_DESCRIPTION)
@tool_guard
async def read_view_params(drawing: str, *, config: RunnableConfig) -> Envelope:
    """读平立剖参数:复用索引的 dimensions(标注)+ annotations(图上文字),带上视图类型。"""
    idx, drawing_id, error = await _load_index(drawing, project_from_config(config))
    if error is not None:
        return error
    assert idx is not None

    # 若是上传入库的项目图,回头取它的 view_type 与展示名(demo 图没有 view_type)。
    row = await asyncio.to_thread(db.find_drawing_by_artifact, drawing_id)
    view_type = row.view_type if row is not None else None
    name = row.title if row is not None else _display_name(drawing_id)
    view_label = _VIEW_CN.get(view_type or "", "图纸")

    if _is_pdf(idx):
        # PDF 的看家能力:把图上抽到的文字(标高、房间名、标注数字)如实报出来。没抽到就说扫描件。
        pdf_annotations: list[dict[str, Any]] = idx.get("annotations", [])
        if not pdf_annotations:
            return fail(
                ErrorCode.EMPTY_RESULT,
                user_msg=(
                    f"{name}是 PDF 图纸,但读不到任何文字(像扫描件)。"
                    "能出预览图看整张,图上的文字读不了。"
                ),
            )
        ann_shown = "、".join(a["text"] for a in pdf_annotations[:8])
        return ok(
            data={"format": "pdf", "view_type": view_type, "annotations": pdf_annotations},
            user_msg=(
                f"{name}({view_label},PDF)读到图上文字 {len(pdf_annotations)} 条:{ann_shown}。"
                "这些是图上写着的字(含标高/房间名等),没写的读不了;图层/构件/标注读数 PDF 给不了。"
            ),
        )

    dims: list[dict[str, Any]] = idx["dimensions"]
    annotations: list[dict[str, Any]] = idx.get("annotations", [])
    units = idx["units_label"]

    if not dims and not annotations:
        tz = _tianzheng(idx)
        if tz["detected"]:
            # 天正图:标注/文字锁在 TCH_* 私有构件里,不是「没写」——给导出步骤,别误判。
            return fail(
                ErrorCode.EMPTY_RESULT,
                user_msg=f"{name}是天正图,标注和文字都是天正私有构件,我读不到。{_TIANZHENG_STEPS}",
            )
        # 图上既没标注也没文字 → 如实说做不了,别硬编(同 query_dimension 的红线)。
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=f"{name}上没读到任何标注尺寸或文字 —— 图上没标/没写的参数我量不了。",
        )

    dim_shown = "、".join(f"{d['text']}{units}" for d in dims[:6]) or "(没有标注尺寸)"
    ann_shown = "、".join(a["text"] for a in annotations[:8]) or "(没有图上文字)"
    return ok(
        data={
            "view_type": view_type,
            "dimensions": dims,
            "annotations": annotations,
            "units_label": units,
        },
        user_msg=(
            f"{name}({view_label})读到:标注尺寸 {len(dims)} 处、图上文字 {len(annotations)} 条。"
            f"标注:{dim_shown}。文字(含标高/层高等):{ann_shown}。"
            "这些都是图上已经标/写的,没标的量不了。"
        ),
    )


CAD_TOOLS: list = [
    list_drawings,
    list_projects,
    parse_drawing,
    query_dimension,
    list_components,
    layer_stats,
    read_view_params,
    render_preview,
]
"""供 gyt.agents.cad 组装时使用。拿去用之前先 list(...) 复制一份,别原地 append。"""

__all__ = [
    "CAD_TOOLS",
    "layer_stats",
    "list_components",
    "list_drawings",
    "list_projects",
    "parse_drawing",
    "query_dimension",
    "read_view_params",
    "render_preview",
]
