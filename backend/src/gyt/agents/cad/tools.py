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
from pypdfium2 import PdfiumError

from gyt.agents.cad import index, pdf_export, render, vision
from gyt.agents.cad.demo_registry import get_demo_drawings
from gyt.config import ALLOWED_DRAWING_EXT, get_settings
from gyt.core import artifacts, project_fs
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.core.llm import LLMCallError, MissingAPIKeyError
from gyt.core.run_context import project_from_config
from gyt.db import projects as db

logger = logging.getLogger(__name__)

_BYTES_PER_MB: Final[int] = 1024 * 1024
_PREVIEW_NAME: Final[str] = "preview.png"
_PDF_EXT: Final[str] = ".pdf"

# 视图类型的人话标签(drawings.view_type ∈ plan/elevation/section)。
_VIEW_CN: Final[dict[str, str]] = {"plan": "平面图", "elevation": "立面图", "section": "剖面图"}

# 资料(文档)类型的人话标签(project_fs.DOC_TYPES ∈ regulation/task_book)。
_DOC_CN: Final[dict[str, str]] = {
    project_fs.DOC_REGULATION: "规范",
    project_fs.DOC_TASK_BOOK: "任务书",
}

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


# --- 打开 / 预览 / 下载 / 导出:图纸与资料的定位(带歧义消解)---------------------
# 这一组是「自然语言操作文件」的地基:把「打开 1 号楼二层平面图」这种话收敛成一份可预览/下载的
# 具体文件。契约 2 照旧 —— 返回的是 artifact_id + 元数据,**绝不把文件内容塞进 state**。


def _name_for_artifact_sync(artifact_id: str) -> str:
    """从 id 反查展示名:先演示图映射,再上传图库行,都查不到给中性词。阻塞(调用方 to_thread)。"""
    for name, mapped_id in get_demo_drawings().items():
        if mapped_id == artifact_id:
            return name
    row = db.find_drawing_by_artifact(artifact_id)
    return row.title if row is not None else "这张图纸"


def _locate_drawing_sync(
    drawing: str, project_id: str
) -> tuple[dict[str, str] | None, Envelope | None]:
    """把「图名或编号」定位到一张具体图纸。阻塞函数(sqlite + 读元数据),调用方 to_thread 包。

    返回 (定位结果, 待转述信封):
      · 定位成功 → ({artifact_id, name, ext}, None);
      · 找不到 / 参数空 → (None, fail(...));
      · **名字不唯一** → (None, ok(needs_disambiguation + candidates)) —— 让用户挑,别猜错图纸。

    顺序:① 已是合法 id 直接用;② 演示名精确命中;③ 上传项目图按名字找**全部**同名。
    选了工地(project_id)时③只在该项目内找,和 list_drawings / _load_index 的作用域一致。
    """
    text = (drawing or "").strip()
    if not text:
        return None, fail(ErrorCode.INVALID_INPUT, "要看哪张图?说个图名或编号。")

    if artifacts.ARTIFACT_ID_RE.fullmatch(text):
        try:
            meta = artifacts.read_meta(text)
        except ArtifactNotFound:
            return None, fail(ErrorCode.NOT_FOUND, f"没找到编号 {text} 的图纸(可能已过期)。")
        return {
            "artifact_id": text,
            "name": _name_for_artifact_sync(text),
            "ext": str(meta.get("ext", "")).lower(),
        }, None

    demo = get_demo_drawings()
    if text in demo:
        aid = demo[text]
        try:
            meta = artifacts.read_meta(aid)
        except ArtifactNotFound:
            return None, fail(ErrorCode.NOT_FOUND, f"演示图纸「{text}」不见了,重启后端预注册一下。")
        return {"artifact_id": aid, "name": text, "ext": str(meta.get("ext", "")).lower()}, None

    rows = db.find_drawings_by_title(text, project_id or None)
    if len(rows) == 1:
        row = rows[0]
        try:
            meta = artifacts.read_meta(row.artifact_id)
        except ArtifactNotFound:
            return None, fail(ErrorCode.NOT_FOUND, f"图纸「{text}」的文件不见了。")
        return {
            "artifact_id": row.artifact_id,
            "name": row.title,
            "ext": str(meta.get("ext", "")).lower(),
        }, None
    if len(rows) > 1:
        candidates = [
            {
                "artifact_id": r.artifact_id,
                "project_id": r.project_id,
                "title": r.title,
                "view_type": r.view_type,
                "floor": r.floor or "",
            }
            for r in rows
        ]
        listed = "、".join(
            f"{r.title}({_VIEW_CN.get(r.view_type, r.view_type)}"
            f"{'·' + r.floor if r.floor else ''}·项目 {r.project_id})"
            for r in rows
        )
        return None, ok(
            data={"needs_disambiguation": True, "candidates": candidates},
            user_msg=(
                f"有 {len(rows)} 张都叫「{text}」:{listed}。你要看哪一个?说清是哪个项目或楼层。"
            ),
        )
    return None, fail(
        ErrorCode.NOT_FOUND,
        user_msg=f"没找到「{text}」这张图纸。现在能看的有:{_drawings_hint()}。",
    )


def _doc_meta(entry: project_fs.DocEntry) -> dict[str, Any]:
    """一份资料的可预览/下载元数据(供前端拼 /files/doc 的 query)。"""
    return {
        "scope": entry.scope,
        "doc_type": entry.doc_type,
        "filename": entry.filename,
        "project_id": entry.project_id,
    }


def _scoped_docs_sync(project_id: str) -> list[project_fs.DocEntry]:
    """当前作用域内可见的资料:全局规范 + 当前工地的资料;没选工地则只有全局。阻塞(读目录)。

    与 knowledge 的作用域纪律一致 —— 不把**别的**项目的资料端出来(防串味)。
    """
    all_docs = project_fs.list_docs(None)  # 全局 + 所有项目
    if project_id:
        return [
            e for e in all_docs if e.scope == project_fs.SCOPE_GLOBAL or e.project_id == project_id
        ]
    return [e for e in all_docs if e.scope == project_fs.SCOPE_GLOBAL]


def _locate_document_sync(
    name: str, project_id: str
) -> tuple[dict[str, Any] | None, Envelope | None]:
    """把「资料名」定位到一份具体文档(按文件名包含匹配,带歧义消解)。阻塞函数,调用方 to_thread。

    返回同 _locate_drawing_sync:成功给元数据,找不到给 fail,重名给 ok(needs_disambiguation)。
    「《安全生产规范》」这种带书名号/多余空白的输入会先洗掉再匹配。
    """
    query = (name or "").strip().strip("《》").strip()
    if not query:
        return None, fail(ErrorCode.INVALID_INPUT, "要看哪份资料?说个名字。")
    docs = _scoped_docs_sync(project_id)
    matches = [e for e in docs if query.lower() in e.filename.lower()]
    if len(matches) == 1:
        return _doc_meta(matches[0]), None
    if len(matches) > 1:
        listed = "、".join(f"《{e.filename}》" for e in matches[:10])
        return None, ok(
            data={"needs_disambiguation": True, "candidates": [_doc_meta(e) for e in matches]},
            user_msg=f"有 {len(matches)} 份名字里带「{query}」:{listed}。要看哪一份?",
        )
    avail = "、".join(f"《{e.filename}》" for e in docs[:10]) or "(暂无)"
    return None, fail(ErrorCode.EMPTY_RESULT, f"没找到叫「{query}」的资料。现在有:{avail}。")


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
            # 文字选不中(文字转图形/扫描件):矢量文字抽不到,但可渲染成图让视觉模型认 ——
            # 认出来的可能有误,别把话说死成「读不了」。
            tail = (
                "文字选不中(是图形或扫描的),不过我可以照着图**认**出来(可能有误、可能漏),"
                "你说「读图上文字」就行;图层/构件/标注读数仍是 DXF 专有,PDF 给不了。"
            )
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
        # PDF 走 pypdfium2 栅格化首页,没有「图元太多」这道 DXF 渲染专属的拦阻。
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

    # 渲染前先按图元数拦一道:图元数越大 SVG→PDF 越慢(见 config.drawing_render_max_entities
    # 的伸缩实测),而且大地坐标系的真图渲染出来常是空白。超阈值就**不渲染、如实说**,别硬撑到超时。
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


async def _read_pdf_text_by_vision(
    drawing_id: str, name: str, view_type: str | None, view_label: str
) -> Envelope:
    """PDF 文字层为空(文字转图形/扫描件)时的读字兜底:渲染成图 → 视觉模型认字。

    认出来的字**可能有误、可能漏**(和矢量文字层抽出来的『精确』两回事),所以成功文案里
    必须带「可能有误、以原图为准」的红线措辞 —— 别和「读到图上文字」那套精确措辞混了。
    渲染倍率取 settings.cad_ocr_render_scale(比预览高,小字更清)。视觉调用的异常在这里接住
    翻成中文信封(vision.read_drawing_text 本身不接)。
    """
    path = await asyncio.to_thread(artifacts.resolve, drawing_id)
    scale = get_settings().cad_ocr_render_scale
    try:
        # 渲染用 pypdfium2(抛 PdfiumError),抽文字用 pypdf(抛 PyPdfError)—— 两个库、两种异常,
        # 都当「文件打不开」处理,别让损坏 PDF 冒到 tool_guard 变成「系统开小差」。
        png_bytes = await asyncio.to_thread(render.pdf_to_png, path, scale=scale)
    except (PyPdfError, PdfiumError) as exc:
        logger.info("PDF 读字渲染失败 %s:%s", drawing_id, exc)
        return fail(
            ErrorCode.FILE_CORRUPT,
            user_msg="这张 PDF 图纸打不开,可能文件传坏了,换一张再试。",
            detail=f"pdf ocr render {drawing_id} 失败:{type(exc).__name__}: {exc}",
        )

    try:
        recognized = await vision.read_drawing_text(png_bytes)
    except MissingAPIKeyError as exc:
        # 密钥没配:这句本身是可操作的中文(指到 .env 哪一行),原样透传,别被 tool_guard 吞成 INTERNAL。
        logger.error("视觉模型密钥没配置:%s", exc)
        return fail(ErrorCode.INTERNAL, user_msg=str(exc))
    except LLMCallError as exc:
        logger.warning("PDF 读字视觉调用失败:%s", exc)
        return fail(exc.error_code, user_msg=str(exc.user_msg))

    lines = [ln.strip() for ln in recognized.splitlines() if ln.strip()]
    if not lines or recognized.strip() == vision.EMPTY_MARKER:
        # 认了一遍也没认出字:如实说,不硬编。
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=(
                f"{name}是 PDF 图纸,文字选不中(是图形或扫描的),我照着图认了一遍也没认出字来。"
                "能出预览图看整张,但图上的文字读不了。"
            ),
        )

    shown = ";".join(lines[:8])
    return ok(
        data={
            "format": "pdf",
            "view_type": view_type,
            # 独立字段:和矢量文字层的 annotations(精确)分开,标明来源是视觉识别(可能有误)。
            "annotations_recognized": lines,
            "source": "vision",
        },
        user_msg=(
            f"{name}({view_label},PDF)文字选不中(是图形或扫描的),下面是我**照图认出来**的 "
            f"{len(lines)} 条,**可能有误、可能漏,关键数字(标高/尺寸等)务必以原图为准**:{shown}。"
        ),
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
        # PDF 的看家能力:把图上抽到的文字(标高、房间名、标注数字)如实报出来。
        pdf_annotations: list[dict[str, Any]] = idx.get("annotations", [])
        if not pdf_annotations:
            # 文字层为空(文字转图形/扫描件):不再直接放弃,渲染成图交给视觉模型认字兜底。
            # 认出来的字可能有误,文案会明说(见 _read_pdf_text_by_vision 的红线措辞)。
            return await _read_pdf_text_by_vision(drawing_id, name, view_type, view_label)
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


_OPEN_DRAWING_DESC = (
    "打开/预览一张图纸(在界面里弹出 PDF 预览)。用户说「打开 XX 图」「预览 XX 平面图」"
    "「看看刚上传的施工图」时调它。drawing 填图纸名字或编号。"
    "PDF 图纸直接预览;DXF 图纸会自动转成 PDF 再预览。"
    "若返回里带 needs_disambiguation(同名多张),把候选念给用户让他选,别自己挑。"
)


@tool("open_drawing", description=_OPEN_DRAWING_DESC)
@tool_guard
async def open_drawing(drawing: str, *, config: RunnableConfig) -> Envelope:
    """定位一张图纸,返回可在界面预览的元数据(DXF 走转 PDF,PDF 原样)。"""
    project_id = project_from_config(config)
    located, relay = await asyncio.to_thread(_locate_drawing_sync, drawing, project_id)
    if located is None:
        return relay  # NOT_FOUND / 参数空 / 需要用户消歧
    artifact_id, name = located["artifact_id"], located["name"]

    # 用 _load_index 拿格式(pdf/dxf)并对超大 DXF 做预览保护(同 render_preview 那道闸)。
    idx, _did, error = await _load_index(artifact_id, project_id)
    if error is not None:
        return error
    assert idx is not None

    is_pdf = _is_pdf(idx)
    if not is_pdf:
        entities_total = sum(idx["entities_by_kind"].values())
        max_entities = get_settings().drawing_render_max_entities
        if entities_total > max_entities:
            return fail(
                ErrorCode.FILE_TOO_LARGE,
                user_msg=(
                    f"{name}图元太多({entities_total} 个),转 PDF 预览会很慢,这次先没开。"
                    "图层、构件、标注尺寸都能正常查——想看哪样直接说。"
                ),
            )
    return ok(
        data={
            "action": "preview",
            "target": "drawing",
            "artifact_id": artifact_id,
            "name": name,
            "format": "pdf" if is_pdf else "dxf",
            "preview_format": "pdf",
        },
        user_msg=(
            f"{name}这就打开预览"
            + ("(PDF 图纸,直接看)。" if is_pdf else "(DXF 图纸,给你转成 PDF 看)。")
        ),
    )


_DOWNLOAD_DRAWING_DESC = (
    "下载一张图纸的**原文件**(DXF 就给 DXF,PDF 就给 PDF)。用户说「下载 XX 图」"
    "「把原图给我」时调它。要 DXF 转出来的 PDF 请用 export_drawing_pdf。"
    "drawing 填图纸名字或编号;同名多张会让用户先选。"
)


@tool("download_drawing", description=_DOWNLOAD_DRAWING_DESC)
@tool_guard
async def download_drawing(drawing: str, *, config: RunnableConfig) -> Envelope:
    """定位一张图纸,返回下载原文件的元数据(前端带鉴权头取回后触发下载)。"""
    located, relay = await asyncio.to_thread(
        _locate_drawing_sync, drawing, project_from_config(config)
    )
    if located is None:
        return relay
    fmt = "pdf" if located["ext"] == _PDF_EXT else "dxf"
    return ok(
        data={
            "action": "download",
            "target": "drawing",
            "artifact_id": located["artifact_id"],
            "name": located["name"],
            "format": fmt,
            "download_format": "original",
        },
        user_msg=f"{located['name']}的原文件({'PDF' if fmt == 'pdf' else 'DXF'})给你下载。",
    )


_EXPORT_PDF_DESC = (
    "把一张 DXF 图纸导出成 PDF(矢量,放大不糊),导出后可预览、也能下载这份 PDF。"
    "用户说「把 XX DXF 转成 PDF」「导出这张图的 PDF」「转 PDF 给我看」时调它。"
    "图纸本来就是 PDF 的,会直接告诉用户不用转。drawing 填图纸名字或编号;同名多张会让用户先选。"
)


@tool("export_drawing_pdf", description=_EXPORT_PDF_DESC)
@tool_guard
async def export_drawing_pdf(drawing: str, *, config: RunnableConfig) -> Envelope:
    """DXF → 矢量 PDF(注册为产物、带缓存复用),返回可预览/下载的元数据。"""
    project_id = project_from_config(config)
    located, relay = await asyncio.to_thread(_locate_drawing_sync, drawing, project_id)
    if located is None:
        return relay
    artifact_id, name = located["artifact_id"], located["name"]

    idx, _did, error = await _load_index(artifact_id, project_id)
    if error is not None:
        return error
    assert idx is not None

    if _is_pdf(idx):
        return ok(
            data={
                "action": "preview",
                "target": "drawing",
                "artifact_id": artifact_id,
                "name": name,
                "format": "pdf",
                "preview_format": "pdf",
                "exported": False,
            },
            user_msg=f"{name}本来就是 PDF,不用转,直接给你预览/下载。",
        )

    entities_total = sum(idx["entities_by_kind"].values())
    max_entities = get_settings().drawing_render_max_entities
    if entities_total > max_entities:
        return fail(
            ErrorCode.FILE_TOO_LARGE,
            user_msg=(
                f"{name}图元太多({entities_total} 个),转 PDF 会很慢,这次先没转。"
                "图层、构件、标注尺寸都能正常查——想看哪样直接说。"
            ),
            detail=f"export_pdf 跳过:entities={entities_total} > {max_entities}",
        )

    try:
        pdf_id = await asyncio.to_thread(pdf_export.export_dxf_to_pdf, artifact_id)
    except ArtifactNotFound:
        return fail(ErrorCode.NOT_FOUND, user_msg="这张图纸的文件不见了。")
    except ezdxf.DXFError as exc:
        logger.info("导出 PDF 渲染失败 %s:%s", artifact_id, exc)
        return fail(
            ErrorCode.FILE_CORRUPT,
            user_msg="这张图纸转不成 PDF,可能文件有问题,换一张再试。",
            detail=f"export_pdf {artifact_id} 失败:{type(exc).__name__}: {exc}",
        )
    return ok(
        data={
            "action": "preview",
            "target": "drawing",
            "artifact_id": artifact_id,
            "name": name,
            "format": "dxf",
            "preview_format": "pdf",
            "exported": True,
            "pdf_id": pdf_id,
        },
        user_msg=f"{name}已经转成 PDF 了,可以预览,也能下载这份 PDF。",
    )


_FIND_DOCS_DESC = (
    "在资料库里找规范/任务书文档(不是图纸)。用户问「资料库里有哪些规范」"
    "「有没有 XX 规范」时调它。query 填名字关键词,留空则列出当前能看的全部资料。"
    "只列全局规范 + 当前工地的资料。"
)


@tool("find_documents", description=_FIND_DOCS_DESC)
@tool_guard
async def find_documents(query: str = "", *, config: RunnableConfig) -> Envelope:
    """列/搜资料库文档(规范/任务书),返回可预览/下载的元数据清单。"""
    project_id = project_from_config(config)
    docs = await asyncio.to_thread(_scoped_docs_sync, project_id)
    q = (query or "").strip().strip("《》").strip()
    if q:
        docs = [e for e in docs if q.lower() in e.filename.lower()]
    if not docs:
        return fail(
            ErrorCode.EMPTY_RESULT,
            user_msg=(f"没找到名字里带「{q}」的资料。" if q else "资料库里现在还没有资料。"),
        )
    listed = "、".join(
        f"《{e.filename}》({_DOC_CN.get(e.doc_type, e.doc_type)})" for e in docs[:20]
    )
    return ok(
        data={"documents": [_doc_meta(e) for e in docs]},
        user_msg=f"找到 {len(docs)} 份:{listed}。要预览或下载哪份,说一声。",
    )


_OPEN_DOC_DESC = (
    "打开/预览资料库里的一份规范或任务书(在界面里弹出 PDF 预览)。"
    "用户说「打开资料库里的《安全生产规范》」「预览 XX 规范」时调它。name 填资料名字关键词。"
    "同名多份会让用户先选。"
)


@tool("open_document", description=_OPEN_DOC_DESC)
@tool_guard
async def open_document(name: str, *, config: RunnableConfig) -> Envelope:
    """定位一份资料,返回可在界面预览的元数据。"""
    located, relay = await asyncio.to_thread(
        _locate_document_sync, name, project_from_config(config)
    )
    if located is None:
        return relay
    return ok(
        data={"action": "preview", "target": "doc", **located},
        user_msg=f"《{located['filename']}》这就打开预览。",
    )


_DOWNLOAD_DOC_DESC = (
    "下载资料库里的一份规范或任务书(PDF)。用户说「下载 XX 规范」「把那份任务书给我」时调它。"
    "name 填资料名字关键词;同名多份会让用户先选。"
)


@tool("download_document", description=_DOWNLOAD_DOC_DESC)
@tool_guard
async def download_document(name: str, *, config: RunnableConfig) -> Envelope:
    """定位一份资料,返回下载元数据(前端带鉴权头取回后触发下载)。"""
    located, relay = await asyncio.to_thread(
        _locate_document_sync, name, project_from_config(config)
    )
    if located is None:
        return relay
    return ok(
        data={"action": "download", "target": "doc", **located},
        user_msg=f"《{located['filename']}》给你下载。",
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
    open_drawing,
    download_drawing,
    export_drawing_pdf,
    find_documents,
    open_document,
    download_document,
]
"""供 gyt.agents.cad 组装时使用。拿去用之前先 list(...) 复制一份,别原地 append。"""

__all__ = [
    "CAD_TOOLS",
    "download_document",
    "download_drawing",
    "export_drawing_pdf",
    "find_documents",
    "layer_stats",
    "list_components",
    "list_drawings",
    "list_projects",
    "open_document",
    "open_drawing",
    "parse_drawing",
    "query_dimension",
    "read_view_params",
    "render_preview",
]
