"""GYT 自定义 HTTP 端点 —— 项目管理与图纸上传,挂在 LangGraph 服务上。

由 langgraph.json 的 ``http.app`` 挂载,放 backend/ 根、与 langgraph.json 平级
(langgraph-cli 按文件路径 import,不是 gyt 包的一部分,同 auth.py)。

鉴权(已探明,见 .personal/W7…方案 §3.4):langgraph.json 里
``"enable_custom_route_auth": true`` 会把现有 auth.py 的 X-Api-Key 校验**自动套到这些路由上**,
所以端点内不再自己写鉴权。本机联调无令牌时放行,与整个服务一致。

阻塞 IO 一律 ``run_in_threadpool``:这些路由和图跑在同一事件循环里,同步 sqlite/落盘/注册
属阻塞 IO,langgraph 的 blockbuster 会抛 BlockingError(schedule/cad 都踩过同款坑)。

返回体统一四键信封 ``{ok, data, user_msg, error_code}``(与 core/errors.Envelope 同形,
前端好复用;user_msg 一律工地师傅看得懂的中文)。

范围(W7 方案 §3):本文件目前是**项目 + 图纸**两条线。规范/任务书上传(/docs)随 §4 的
knowledge ingest_document 一起落地 —— 那样「上传即可检索」是一个完整动作,不留「传了但搜不到」的洞。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import ezdxf
from pypdf.errors import PyPdfError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from gyt.agents.cad import index as cad_index
from gyt.agents.cad import pdf_export
from gyt.agents.cad.demo_registry import get_demo_drawings
from gyt.agents.knowledge import ingest, store
from gyt.checkin_api import CHECKIN_ROUTES
from gyt.config import ALLOWED_DRAWING_EXT, get_settings
from gyt.core import artifacts, project_fs
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.db import hazards
from gyt.db import projects as db
from gyt.reports_api import REPORTS_ROUTES
from gyt.supervision_api import SUPERVISION_ROUTES
from gyt.timing_api import TIMING_ROUTES
from gyt.upload_api import UPLOAD_ROUTES

logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1024 * 1024
_PDF_EXT = ".pdf"
_DXF_EXT = ".dxf"

# 文件内容端点用的媒体类型与展示方式常量(禁止散落魔法值)。
_MEDIA_PDF = "application/pdf"
_MEDIA_PNG = "image/png"
# DXF 不是浏览器能预览的格式,原文下载一律当二进制推给用户(触发「另存为」)。
_MEDIA_DXF = "application/octet-stream"
_DISPOSITIONS = frozenset({"inline", "attachment"})
_FORMATS = frozenset({"original", "pdf"})


# --- 信封响应 ----------------------------------------------------------------


def _envelope(ok: bool, data: Any, user_msg: str, error_code: str | None) -> dict[str, Any]:
    return {"ok": ok, "data": data, "user_msg": user_msg, "error_code": error_code}


def _ok(data: Any, user_msg: str, *, status: int = 200) -> JSONResponse:
    return JSONResponse(_envelope(True, data, user_msg, None), status_code=status)


def _fail(status: int, user_msg: str, error_code: str) -> JSONResponse:
    return JSONResponse(_envelope(False, None, user_msg, error_code), status_code=status)


# --- 文件内容响应(预览 / 下载)---------------------------------------------
# 前端拿不了裸 URL 直接 window.open(API Key 在请求头里),只能带鉴权头 fetch 成 Blob。
# 这些端点复用 langgraph 的 X-Api-Key 鉴权(enable_custom_route_auth,同其它自定义路由),
# 端点内不再自己写鉴权。返回体是**文件字节**而不是信封,所以出错走 _fail(信封)、
# 成功走 FileResponse(流式,不把整份读进内存)。


def _content_disposition(disposition: str, filename: str) -> str:
    """拼一个既兼容老浏览器又能带中文名的 Content-Disposition(RFC 5987)。

    中文文件名(工地图纸「首层平面图.dxf」)不能直接进 filename= —— header 只允许 ASCII,
    塞中文会乱码甚至截断。做法:filename= 给一个 ASCII 兜底名,filename*=UTF-8'' 给百分号
    编码的真名,现代浏览器优先用后者。disposition 是 inline(浏览器内预览)或 attachment(下载)。
    """
    # ASCII 兜底名:非 ASCII 字符丢掉,再抹掉会破坏 header 的引号/换行;空了给个中性名。
    ascii_fallback = re.sub(r'[\r\n"]', "_", filename.encode("ascii", "ignore").decode("ascii"))
    ascii_fallback = ascii_fallback.strip() or "download"
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _disposition_of(request: Request, default: str = "inline") -> str:
    """从 query 取展示方式(inline/attachment),非法值回退默认(不报错,给个安全默认)。"""
    value = request.query_params.get("disposition", default)
    return value if value in _DISPOSITIONS else default


def _serve_file(path: Path, *, media_type: str, disposition: str, filename: str) -> FileResponse:
    """把一份磁盘文件按指定媒体类型 + 展示方式流式返回(不经内存整读)。

    不传 FileResponse 的 filename 参数(那会让它自己拼一个 attachment 头),改用我们
    显式算好的 Content-Disposition —— inline 预览必须由我们控制,库默认只会给 attachment。
    """
    return FileResponse(
        path,
        media_type=media_type,
        headers={"content-disposition": _content_disposition(disposition, filename)},
    )


# --- project_id 生成 ---------------------------------------------------------


def _slug(text: str | None) -> str:
    """把 code/name 压成安全短码:小写、非字母数字并成短横、掐掉首尾短横。

    中文名会只剩其中的 ASCII 字母数字(如「幸福小区A3栋」→「a3」);全中文则为空,由上层兜底。
    """
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _new_project_id(name: str, code: str | None) -> str:
    """生成不与现有项目撞车的短码:优先 code,其次 name,都压不出就用随机;撞了加序号。"""
    base = _slug(code) or _slug(name) or f"p-{uuid4().hex[:8]}"
    pid, i = base, 2
    while db.get_project(pid) is not None:
        pid, i = f"{base}-{i}", i + 1
    return pid


# --- 端点 --------------------------------------------------------------------


async def list_projects(request: Request) -> JSONResponse:
    """GET /projects —— 列出所有项目(供上传面板下拉)。"""
    rows = await run_in_threadpool(db.list_projects)
    data = {
        "projects": [
            {"id": r.id, "name": r.name, "code": r.code, "created_at": r.created_at} for r in rows
        ]
    }
    return _ok(data, f"共 {len(rows)} 个项目。")


async def library(request: Request) -> JSONResponse:
    """GET /library —— 所有项目的图纸 + 规范/任务书总览(供前端「资料库」浏览入口)。

    一次把三样打平返回:项目清单、跨项目图纸(db.list_drawings)、跨项目 + 全局文档
    (project_fs.list_docs)。前端按 project_id 分组渲染,全局规范单列一组。只读,不改任何状态。
    """

    def _work() -> tuple[
        list[Any], list[Any], list[Any], dict[tuple[str, str, str], int], set[tuple[str, str, str]]
    ]:
        docs = project_fs.list_docs()
        # 无文字层(扫描件/转曲)标记 —— 让状态显示成「不可检索」而不是永远「入库中」。
        # 文件系统 stat 属阻塞 IO,和其它三样一起在这一个线程池调用里做完。
        unsearchable = {
            (e.scope, e.project_id or "", e.filename)
            for e in docs
            if project_fs.doc_is_unsearchable(e.scope, e.doc_type, e.filename, e.project_id)
        }
        return (
            db.list_projects(),
            db.list_drawings(),
            docs,
            store.count_chunks_by_doc(),  # 每份文档的向量块数,标「入库中 / 已入库 N 段」
            unsearchable,
        )

    projects, drawings, docs, chunk_counts, unsearchable = await run_in_threadpool(_work)
    name_by_id = {p.id: p.name for p in projects}

    def _doc_status(scope: str, pid: str | None, filename: str, chunks: int) -> str:
        """给前端展示用的四态:已可检索 / 不可检索 / 处理中(空库时也归这类,下次刷新再数)。

        · unsearchable(无文字层标记)→ "unsearchable"(可预览,暂不可检索,OCR 待支持);
        · chunks > 0                 → "indexed"(已入库、可检索);
        · 其余(有文字层但块数还是 0)→ "ingesting"(embedding 还没跑完 / 刚数不出)。
        「解析失败」这一态目前不会落到资料库:打不开的 PDF 在上传口就被 422 拦下、不归档。
        """
        if (scope, pid or "", filename) in unsearchable:
            return "unsearchable"
        return "indexed" if chunks > 0 else "ingesting"

    def _doc_json(e: Any) -> dict[str, Any]:
        # 向量块数:0 = 还在入库(embedding 未完)/ 无文字层,>0 = 已入库、可检索。
        chunks = chunk_counts.get((e.scope, e.project_id or "", e.filename), 0)
        return {
            "scope": e.scope,
            "project_id": e.project_id,
            "project_name": name_by_id.get(e.project_id) if e.project_id else None,
            "doc_type": e.doc_type,
            "filename": e.filename,
            "rel_path": e.rel_path,
            "size_bytes": e.size_bytes,
            "modified_at": e.modified_at,
            "chunks": chunks,
            # 展示状态:indexed / ingesting / unsearchable(见 _doc_status)。
            "status": _doc_status(e.scope, e.project_id, e.filename, chunks),
        }

    data = {
        "projects": [{"id": p.id, "name": p.name, "code": p.code} for p in projects],
        "drawings": [
            {
                "drawing_id": d.id,  # 前端「删这张图」要用它打 DELETE .../drawings/{id}
                "project_id": d.project_id,
                "project_name": name_by_id.get(d.project_id),
                "title": d.title,
                "view_type": d.view_type,
                "floor": d.floor,
                "rel_path": d.rel_path,
                "artifact_id": d.artifact_id,
                # 后缀:前端据它决定预览走「PDF 原样」还是「DXF 转 PDF」(.dxf / .pdf)。
                "ext": PurePosixPath(d.rel_path or "").suffix.lower(),
                "created_at": d.created_at,
            }
            for d in drawings
        ],
        "docs": [_doc_json(e) for e in docs],
    }
    return _ok(data, f"共 {len(drawings)} 张图纸、{len(docs)} 份资料。")


async def create_project(request: Request) -> JSONResponse:
    """POST /projects —— 建项目(name 必填、code 可选),同时建好项目目录骨架。

    收 JSON 或 form 都行(面板发 FormData,curl 发 JSON 都方便)。
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = await request.json()
        name = str(body.get("name") or "").strip()
        code = str(body.get("code") or "").strip() or None
    else:
        form = await request.form()
        name = str(form.get("name") or "").strip()
        code = str(form.get("code") or "").strip() or None

    if not name:
        return _fail(400, "项目名不能为空,给它起个名字(比如「幸福小区A3栋」)。", "INVALID_INPUT")

    def _work() -> str:
        pid = _new_project_id(name, code)
        db.create_project(pid, name, code)
        project_fs.ensure_project_tree(pid)
        return pid

    pid = await run_in_threadpool(_work)
    logger.info("已建项目 %s(name=%s)", pid, name)
    return _ok(
        {"id": pid, "name": name, "code": code},
        f"项目「{name}」建好了(编号 {pid})。",
        status=201,
    )


async def upload_drawing(request: Request) -> JSONResponse:
    """POST /projects/{project_id}/drawings —— 上传一张图纸(DXF 或 PDF)并入库。

    multipart:file(.dxf/.pdf)、view_type(plan/elevation/section)、title(可省=文件名主干)、floor(可选)。
    落地 → 注册产物 → 写 drawings 行,一条龙。CAD Agent 后续按后缀分流:DXF 走结构化解析,
    PDF 出预览 + 读图上文字(图层/构件/标注读数是 DXF 专有)。
    """
    pid = request.path_params["project_id"]
    form = await request.form()
    upload = form.get("file")
    view_type = str(form.get("view_type") or "").strip()
    floor = str(form.get("floor") or "").strip() or None
    title = str(form.get("title") or "").strip()

    if upload is None or not hasattr(upload, "filename"):
        return _fail(400, "没收到图纸文件,请选一个 .dxf 或 .pdf 再传。", "INVALID_INPUT")
    filename = upload.filename or ""
    if view_type not in db.VIEW_TYPES:
        return _fail(
            400, "要标明这是平面图、立面图还是剖面图(plan/elevation/section)。", "INVALID_INPUT"
        )
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in ALLOWED_DRAWING_EXT:
        return _fail(
            415,
            "只收 DXF(.dxf)或 PDF(.pdf)图纸;DWG 请先离线转成 DXF 再传。"
            "PDF 图纸能出预览、能读图上文字,但图层/构件/标注读数只有 DXF 给得了。",
            "FILE_UNSUPPORTED",
        )

    payload = await upload.read()
    # PDF 图纸按文档上限、DXF 按图纸上限(两者当前都是 100MB,口径分开、别写死一个)。
    limit_mb = get_settings().document_max_mb if ext == _PDF_EXT else get_settings().drawing_max_mb
    if len(payload) > int(limit_mb * _BYTES_PER_MB):
        return _fail(
            413,
            f"这张图纸有 {len(payload) / _BYTES_PER_MB:.1f}MB,"
            f"超过 {limit_mb:.0f}MB 上限,先精简再传。",
            "FILE_TOO_LARGE",
        )
    if not title:
        title = PurePosixPath(filename).stem

    def _work() -> dict[str, Any] | None:
        if db.get_project(pid) is None:
            return None
        landed = project_fs.land_drawing(pid, view_type, filename, payload)
        rel = project_fs.project_rel_path(pid, landed)
        artifact_id = artifacts.register(landed, kind=ArtifactKind.DRAWING, original_name=filename)
        drawing_id = db.add_drawing(pid, artifact_id, view_type, title, floor=floor, rel_path=rel)
        return {"drawing_id": drawing_id, "artifact_id": artifact_id, "rel_path": rel}

    result = await run_in_threadpool(_work)
    if result is None:
        return _fail(404, "没找到这个项目,先建项目再往里传图。", "NOT_FOUND")
    logger.info("项目 %s 收到图纸 %s(%s)", pid, title, view_type)
    return _ok(
        {"project_id": pid, "view_type": view_type, "title": title, **result},
        f"图纸「{title}」上传成功。",
        status=201,
    )


async def _handle_doc_upload(request: Request, *, scope: str, project_id: str) -> JSONResponse:
    """规范/任务书上传的共用编排:校验 → 落地 → 注册产物 → 按作用域入库(Chroma)。

    scope=global(/docs)只收规范;scope=project(/projects/{id}/docs)收规范或任务书。
    文档目前只收 PDF(docx 记为后续)。入库那步会加载 BGE-M3,首份上传偏慢,属正常。
    """
    form = await request.form()
    upload = form.get("file")
    doc_type = str(form.get("doc_type") or "").strip()

    if upload is None or not hasattr(upload, "filename"):
        return _fail(400, "没收到文档文件,请选一个 PDF 再传。", "INVALID_INPUT")
    filename = upload.filename or ""
    if doc_type not in project_fs.DOC_TYPES:
        return _fail(400, "要标明这是规范还是任务书(regulation/task_book)。", "INVALID_INPUT")
    if scope == project_fs.SCOPE_GLOBAL and doc_type != project_fs.DOC_REGULATION:
        return _fail(400, "全局资料只收规范;任务书要传到某个具体项目下。", "INVALID_INPUT")
    if PurePosixPath(filename).suffix.lower() != _PDF_EXT:
        return _fail(415, "文档目前只收 PDF;Word 请先导出成 PDF 再传。", "FILE_UNSUPPORTED")

    payload = await upload.read()
    max_bytes = int(get_settings().document_max_mb * _BYTES_PER_MB)
    if len(payload) > max_bytes:
        return _fail(
            413,
            f"这份文档超过 {get_settings().document_max_mb:.0f}MB 上限,先精简再传。",
            "FILE_TOO_LARGE",
        )

    # 入库分两段:轻活(落地 + 文字预检)同步做、秒级返回;重活(embedding,大文件几分钟)丢后台,
    # 让「传了像卡住 / 被容器重建掐断」成为历史。资料库按向量块数显示「入库中 / 已入库 N 段」。
    # 仍是原子的:预检不过、或后台入库失败/抽不出 chunk,都回滚落地文件 + 产物,不留搜不到的幽灵。
    def _prepare() -> dict[str, Any]:
        if scope == project_fs.SCOPE_PROJECT and db.get_project(project_id) is None:
            return {"status": "no_project"}
        landed = project_fs.land_doc(scope, doc_type, filename, payload, project_id=project_id)
        artifact_id = artifacts.register(landed, kind=ArtifactKind.DOCUMENT, original_name=filename)
        try:
            has_text = ingest.pdf_has_text(landed)
        except Exception:  # noqa: BLE001 —— PDF 打不开(加密/损坏)当没文字层处理,回滚
            landed.unlink(missing_ok=True)
            artifacts.delete(artifact_id)
            logger.exception("文档 %s 预检打不开", filename)
            return {"status": "bad_pdf"}
        if not has_text:
            # 资料上传策略(改判):无文字层 PDF(扫描件 / 文字转曲的 CAD 打印件)不再拒。
            # 保留归档、能预览,只是打「不可检索」标记、不进检索、**绝不伪造 OCR 结果**。
            # 文件与产物都留着(不 unlink、不 delete),资料库据标记把状态显示成「不可检索」。
            project_fs.mark_doc_unsearchable(
                scope, doc_type, landed.name, project_id=project_id or None
            )
            return {
                "status": "no_text",
                "landed": str(landed),
                "artifact_id": artifact_id,
                "filename": landed.name,
            }
        return {
            "status": "ok",
            "landed": str(landed),
            "artifact_id": artifact_id,
            "filename": landed.name,
        }

    prep = await run_in_threadpool(_prepare)
    prep_status = prep["status"]
    if prep_status == "no_project":
        return _fail(404, "没找到这个项目,先建项目再往里传资料。", "NOT_FOUND")
    if prep_status == "bad_pdf":
        return _fail(
            422, f"「{filename}」打不开(可能加密或损坏),没存进去,换一份 PDF 再传。", "FILE_CORRUPT"
        )
    if prep_status == "no_text":
        # 无文字层:已归档、能预览,但进不了检索(不做 OCR、不伪造)。回 201「收下了」,不起后台入库。
        kind_cn = "规范" if doc_type == project_fs.DOC_REGULATION else "任务书"
        landed_name = str(prep["filename"])
        logger.info(
            "收到无文字层%s文档 %s(scope=%s project=%s):已归档、可预览、暂不入检索",
            kind_cn,
            landed_name,
            scope,
            project_id or "-",
        )
        return JSONResponse(
            _envelope(
                True,
                {
                    "scope": scope,
                    "project_id": project_id or None,
                    "doc_type": doc_type,
                    "filename": landed_name,
                    "artifact_id": str(prep["artifact_id"]),
                    "status": "unsearchable",
                },
                f"{kind_cn}「{landed_name}」已归档,能预览。但没读到文字层"
                "(多半是扫描件/图片版 PDF),暂时进不了检索(OCR 待支持)。",
                None,
            ),
            status_code=201,
        )

    # 文件已落地、有文字层 —— embedding 丢后台,立刻回 202「正在入库」。
    landed_path = Path(prep["landed"])
    artifact_id = str(prep["artifact_id"])

    def _ingest_bg() -> None:
        try:
            n = ingest.ingest_document(
                landed_path, scope=scope, doc_type=doc_type, project_id=project_id
            )
            if n == 0:  # 预检说有字却抽不出 chunk,防御性回滚
                landed_path.unlink(missing_ok=True)
                artifacts.delete(artifact_id)
                logger.warning("后台入库 %s 抽出 0 段,已回滚", filename)
            else:
                logger.info("后台入库完成 %s:%d 段", filename, n)
        except Exception:  # noqa: BLE001 —— 后台入库炸了也要回滚,不留搜不到的幽灵
            landed_path.unlink(missing_ok=True)
            artifacts.delete(artifact_id)
            logger.exception("后台入库失败,已回滚 %s", filename)

    async def _run_bg() -> None:
        await run_in_threadpool(_ingest_bg)  # 阻塞入库挪进线程池,别卡事件循环

    kind_cn = "规范" if doc_type == project_fs.DOC_REGULATION else "任务书"
    logger.info(
        "收到%s文档 %s(scope=%s project=%s),转后台入库", kind_cn, filename, scope, project_id or "-"
    )
    return JSONResponse(
        _envelope(
            True,
            {
                "scope": scope,
                "project_id": project_id or None,
                "doc_type": doc_type,
                "filename": filename,
                "artifact_id": artifact_id,
                "status": "ingesting",
            },
            f"{kind_cn}「{filename}」已收到,正在入库(大文件要几分钟),完成前还搜不到。"
            "可在「资料库」点刷新看进度。",
            None,
        ),
        status_code=202,
        background=BackgroundTask(_run_bg),
    )


async def upload_global_doc(request: Request) -> JSONResponse:
    """POST /docs —— 上传全局规范(对所有项目通用),只收规范。"""
    return await _handle_doc_upload(request, scope=project_fs.SCOPE_GLOBAL, project_id="")


async def upload_project_doc(request: Request) -> JSONResponse:
    """POST /projects/{project_id}/docs —— 上传项目规范或任务书。"""
    pid = request.path_params["project_id"]
    return await _handle_doc_upload(request, scope=project_fs.SCOPE_PROJECT, project_id=pid)


# --- 删除端点 ----------------------------------------------------------------
# 删除是不可逆的。四处一致性:库行 / 物理镜像 / 产物注册表 / Chroma 向量。少清一处的后果:
#   · 漏清 Chroma  → 删了的规范问答里还答得出来(最隐蔽,专门的删向量步在此堵);
#   · 漏清库行     → 资料库里还列着一条指向已删文件的幽灵;
#   · 漏清镜像/产物 → 盘上留死文件(不影响正确性,占点空间)。


async def _body_dict(request: Request) -> dict[str, Any]:
    """DELETE 也带一点参数(doc_type/filename)。收 JSON 或 form 都行,统一成 dict。"""
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 —— 空体/坏 JSON 都按空 dict 处理,交由上层校验缺字段
            return {}
        return body if isinstance(body, dict) else {}
    form = await request.form()
    return dict(form)


async def remove_drawing(request: Request) -> JSONResponse:
    """DELETE /projects/{project_id}/drawings/{drawing_id} —— 删一张图纸。

    级联清:drawings 库行 + 物理文件 + 产物注册表副本 + CAD 解析索引。
    """
    pid = request.path_params["project_id"]
    try:
        did = int(request.path_params["drawing_id"])
    except (TypeError, ValueError):
        return _fail(400, "图纸编号不对,应是数字。", "INVALID_INPUT")

    def _work() -> dict[str, Any] | None:
        row = db.get_drawing_by_id(did)
        if row is None or row.project_id != pid:
            return None
        db.delete_drawing(did)
        project_fs.delete_drawing_file(pid, row.rel_path)
        artifacts.delete(row.artifact_id)
        # CAD 解析索引一张图一份 <drawing_id>.json(config.cad_index_dir),有就顺手清掉。
        (get_settings().cad_index_dir / f"{row.id}.json").unlink(missing_ok=True)
        return {"drawing_id": did, "title": row.title}

    result = await run_in_threadpool(_work)
    if result is None:
        return _fail(404, "没找到这张图纸(可能已删)。", "NOT_FOUND")
    logger.info("项目 %s 删除图纸 %s(%s)", pid, result["title"], did)
    return _ok(result, f"图纸「{result['title']}」已删除。")


async def _handle_doc_delete(request: Request, *, scope: str, project_id: str) -> JSONResponse:
    """删规范/任务书的共用编排:删物理镜像 + 清 Chroma 向量块(按 source+scope+project 精确)。"""
    body = await _body_dict(request)
    doc_type = str(body.get("doc_type") or "").strip()
    filename = str(body.get("filename") or "").strip()

    if not filename:
        return _fail(400, "没说要删哪份资料(缺 filename)。", "INVALID_INPUT")
    if doc_type not in project_fs.DOC_TYPES:
        return _fail(400, "要标明这是规范还是任务书(regulation/task_book)。", "INVALID_INPUT")
    if scope == project_fs.SCOPE_GLOBAL and doc_type != project_fs.DOC_REGULATION:
        return _fail(400, "全局资料只有规范。", "INVALID_INPUT")

    def _work() -> dict[str, Any] | None:
        if scope == project_fs.SCOPE_PROJECT and db.get_project(project_id) is None:
            return None
        removed = project_fs.delete_doc(scope, doc_type, filename, project_id=project_id)
        chunks = ingest.delete_document(filename, scope=scope, project_id=project_id)
        return {"file_removed": removed, "chunks": chunks}

    result = await run_in_threadpool(_work)
    if result is None:
        return _fail(404, "没找到这个项目(可能已删)。", "NOT_FOUND")
    if not result["file_removed"] and result["chunks"] == 0:
        return _fail(404, f"没找到资料「{filename}」(可能已删)。", "NOT_FOUND")
    kind_cn = "规范" if doc_type == project_fs.DOC_REGULATION else "任务书"
    logger.info("删除%s文档 %s(scope=%s project=%s)", kind_cn, filename, scope, project_id or "-")
    return _ok(
        {"scope": scope, "project_id": project_id or None, "filename": filename, **result},
        f"{kind_cn}「{filename}」已删除(清了 {result['chunks']} 段索引)。",
    )


async def remove_global_doc(request: Request) -> JSONResponse:
    """DELETE /docs —— 删一份全局规范(body: doc_type=regulation, filename)。"""
    return await _handle_doc_delete(request, scope=project_fs.SCOPE_GLOBAL, project_id="")


async def remove_project_doc(request: Request) -> JSONResponse:
    """DELETE /projects/{project_id}/docs —— 删一份项目规范/任务书(body: doc_type, filename)。"""
    pid = request.path_params["project_id"]
    return await _handle_doc_delete(request, scope=project_fs.SCOPE_PROJECT, project_id=pid)


async def remove_project(request: Request) -> JSONResponse:
    """DELETE /projects/{project_id} —— 删整个项目(级联)。

    清:名下图纸行/文件/产物/CAD索引 + 项目作用域向量块 + 项目目录 + 项目行。
    全局规范(project_id="")不受影响。
    顺序:**先摘隐患** → 图纸(外键)→ 项目向量 → 目录 → 项目行。

    🔴 隐患**只摘不删**,而且必须排在最前面。两件事:

    · **不删** —— ``hazards`` 挂着已经签发的法律文书与整条证据链
      (``db/hazards.py`` 的 ``delete_pending`` 注释:确认过的隐患不许被删,
      证据链不能凭一次点击消失,而删项目正是一次点击)。摘成「未归属」(空串,D6)之后,
      supervision 侧会显式报「未归属 N 条」,监理照样看得见、照样处置得了。
      不摘的话它们的 ``project_id`` 指向一个不存在的项目(这张表**没有外键**),
      既不在未归属桶里也不在任何项目里 —— 彻底找不到,而库里它们还是「在办」。
    · **排最前** —— 摘不动时(极小概率撞幂等键)整条 UPDATE 回滚、异常抛上来,
      这时图纸文件、向量、目录都还一个没动,拒绝掉就是干净的"什么都没发生";
      放在后面的话,失败时项目已经被拆了一半。
    """
    pid = request.path_params["project_id"]

    def _work() -> dict[str, Any] | None:
        if db.get_project(pid) is None:
            return None
        try:
            detached = hazards.detach_project(pid)
        except sqlite3.IntegrityError:
            # db 层刻意不吞这个异常(那层的头注:吞了就等于把"该重试/该拒绝"变成静默失败)。
            # 这里接住,整个删除动作作废 —— 此刻还一个文件都没动。
            logger.warning("项目 %s 名下的隐患摘不到未归属(撞幂等键),拒绝删除", pid, exc_info=True)
            return {"hazards_blocked": True}
        rows = db.delete_project_drawings(pid)
        for r in rows:
            project_fs.delete_drawing_file(pid, r.rel_path)
            artifacts.delete(r.artifact_id)
            (get_settings().cad_index_dir / f"{r.id}.json").unlink(missing_ok=True)
        chunks = ingest.delete_project_documents(pid)
        project_fs.delete_project_tree(pid)
        db.delete_project(pid)
        return {"drawings": len(rows), "chunks": chunks, "hazards": detached}

    result = await run_in_threadpool(_work)
    if result is None:
        return _fail(404, "没找到这个项目(可能已删)。", "NOT_FOUND")
    if result.get("hazards_blocked"):
        # 后半句「这次没删」是关键:不说的话人会以为删了一半,回头去找一个其实还在的工地。
        return _fail(
            409,
            "这个工地名下有隐患跟「未归属」清单里的重复了(同一张照片、同一个问题),"
            "没法整批转过去,所以这次没删。先在隐患清单里把重复的那条处置掉,再删这个工地。",
            "CONFLICT",
        )
    logger.info(
        "删除项目 %s(图纸 %d 张,向量 %d 段,隐患 %d 条转未归属)",
        pid,
        result["drawings"],
        result["chunks"],
        result["hazards"],
    )
    # 隐患那句只在真有隐患时说:说了才知道那批法律文书没跟着项目一起消失。
    hazard_tail = (
        f"名下 {result['hazards']} 条隐患没有删除,已转到「未归属」清单,可以继续处置。"
        if result["hazards"]
        else ""
    )
    return _ok(
        {"project_id": pid, **result},
        f"项目已删除(含 {result['drawings']} 张图纸、{result['chunks']} 段规范索引)。{hazard_tail}",
    )


# --- 文件内容端点(预览 / 下载)---------------------------------------------


async def serve_drawing_file(request: Request) -> Any:
    """GET /files/drawing/{artifact_id} —— 取一张图纸的文件内容(预览或下载)。

    query:
      · disposition = inline(浏览器内预览,默认)/ attachment(触发下载);
      · format = original(原文件,默认)/ pdf(DXF 转成矢量 PDF;PDF 图纸原样返回)。

    归属校验:artifact_id 必须是**登记在册**的图纸 —— 项目图(drawings 表)或演示图
    (预注册映射)。拿任意产物 id(照片、报告)来掏文件一律 404,不暴露磁盘路径。
    """
    artifact_id = request.path_params["artifact_id"]
    if not artifacts.ARTIFACT_ID_RE.fullmatch(artifact_id):
        return _fail(404, "没找到这张图纸(编号不对)。", "NOT_FOUND")
    fmt = request.query_params.get("format", "original")
    if fmt not in _FORMATS:
        fmt = "original"

    def _lookup() -> dict[str, str] | None:
        # 只认在册图纸:项目图(库)或演示图(预注册映射)。都不是 → None(404)。
        row = db.find_drawing_by_artifact(artifact_id)
        demo_name = next((n for n, aid in get_demo_drawings().items() if aid == artifact_id), None)
        if row is None and demo_name is None:
            return None
        try:
            meta = artifacts.read_meta(artifact_id)
        except ArtifactNotFound:
            return None
        return {
            "ext": str(meta.get("ext", "")).lower(),
            "title": row.title if row is not None else (demo_name or "图纸"),
            "original_name": str(meta.get("original_name") or ""),
        }

    info = await run_in_threadpool(_lookup)
    if info is None:
        return _fail(404, "没找到这张图纸,可能已被删除。", "NOT_FOUND")
    ext = info["ext"]
    if ext not in ALLOWED_DRAWING_EXT:
        return _fail(415, "这个文件不是图纸格式,只支持 DXF / PDF。", "FILE_UNSUPPORTED")

    disposition = _disposition_of(request)
    if fmt == "pdf" and ext == _DXF_EXT:
        return await _serve_dxf_as_pdf(artifact_id, info["title"], disposition)

    # 其余情况直接给原文件:PDF 图纸(含 format=pdf 时原样)、或 DXF 原文件下载。
    def _build() -> Any:
        try:
            path = artifacts.resolve(artifact_id)
        except ArtifactNotFound:
            return _fail(404, "这张图纸的文件不见了。", "NOT_FOUND")
        if ext == _PDF_EXT:
            media = _MEDIA_PDF
            out_name = info["original_name"] or f"{info['title']}.pdf"
        else:
            media = _MEDIA_DXF
            out_name = info["original_name"] or f"{info['title']}{ext}"
        return _serve_file(path, media_type=media, disposition=disposition, filename=out_name)

    return await run_in_threadpool(_build)


async def serve_cad_preview(request: Request) -> Any:
    """GET /files/cad-preview/{artifact_id} —— 取 CAD 工具生成的 PNG 预览。

    本地聊天界面已经连着 2024，预览继续复用这条 HTTP 链即可；不再要求用户为了
    CAD 单独启动 8788。编号先过 32 位 hex 校验，再由产物注册表解析，外部路径永远
    不参与拼接。只认 ``OTHER + .png + preview.png``，避免拿这个端点读取照片或文书。

    公网仍走隔离的只读 artifacts 容器；本端点会被 LangGraph 的自定义路由鉴权保护。
    前端因此带 X-Api-Key fetch 成 Blob，不能直接把裸 URL 塞进 ``img.src``。
    """
    artifact_id = request.path_params["artifact_id"]
    if not artifacts.ARTIFACT_ID_RE.fullmatch(artifact_id):
        return _fail(404, "没找到这张预览图(编号不对)。", "NOT_FOUND")

    def _build() -> Any:
        try:
            meta = artifacts.read_meta(artifact_id)
            path = artifacts.resolve(artifact_id)
        except ArtifactNotFound:
            return _fail(404, "这张预览图已经不存在,请重新生成。", "NOT_FOUND")
        if (
            meta.get("kind") != ArtifactKind.OTHER.value
            or meta.get("ext") != ".png"
            or meta.get("original_name") != "preview.png"
        ):
            return _fail(404, "这个编号不是 CAD 预览图。", "NOT_FOUND")
        return _serve_file(
            path,
            media_type=_MEDIA_PNG,
            disposition="inline",
            filename="preview.png",
        )

    return await run_in_threadpool(_build)


async def _serve_dxf_as_pdf(artifact_id: str, title: str, disposition: str) -> Any:
    """把一张 DXF 转成矢量 PDF 后返回(带图元数保护 + 缓存复用)。异步:分步下线程池。"""
    try:
        idx = await cad_index.ensure_index(artifact_id)
    except ArtifactNotFound:
        return _fail(404, "这张图纸的文件不见了。", "NOT_FOUND")
    except (ezdxf.DXFError, PyPdfError) as exc:
        logger.info("导出 PDF 解析失败 %s:%s", artifact_id, exc)
        return _fail(422, "这张图纸打不开,可能文件传坏了或格式不标准。", "FILE_CORRUPT")

    entities_total = sum(idx.get("entities_by_kind", {}).values())
    max_entities = get_settings().drawing_render_max_entities
    if entities_total > max_entities:
        return _fail(
            413,
            f"这张图纸图元太多({entities_total} 个),生成 PDF 会很慢,这次先没出。"
            "图层、构件、标注尺寸都能正常查——想看哪样直接说。",
            "FILE_TOO_LARGE",
        )
    if idx.get("extents_outlier"):
        # 离群图元/大地坐标把外框撑爆:整图渲出来是「一个点浮在巨大空白里」,如实拦下(见
        # parse._extents_outlier)。不然就是这条线最初暴露的问题——出一张没用的巨图 PDF。
        return _fail(
            413,
            "这张图纸坐标异常(有离群图元把范围撑得极大),整张渲出来内容会缩成一个点、"
            "几乎空白,这次先没出。图层、构件、标注尺寸都能正常查——想看哪样直接说。",
            "FILE_TOO_LARGE",
        )

    def _build() -> Any:
        try:
            pdf_id = pdf_export.export_dxf_to_pdf(artifact_id)
            path = artifacts.resolve(pdf_id)
        except ArtifactNotFound:
            return _fail(404, "这张图纸的文件不见了。", "NOT_FOUND")
        except ezdxf.DXFError as exc:
            logger.info("导出 PDF 渲染失败 %s:%s", artifact_id, exc)
            return _fail(422, "这张图纸转不成 PDF,可能文件有问题,换一张再试。", "FILE_CORRUPT")
        return _serve_file(
            path, media_type=_MEDIA_PDF, disposition=disposition, filename=f"{title}.pdf"
        )

    return await run_in_threadpool(_build)


async def serve_doc_file(request: Request) -> Any:
    """GET /files/doc —— 取一份资料库文档(规范/任务书 PDF)的内容(预览或下载)。

    query:scope(global/project)、doc_type(regulation/task_book)、filename、
    project_id(项目作用域必填)、disposition(inline 默认 / attachment)。

    归属校验:路径由「作用域 + 类型 + 安全 basename(+ 校验过的 project_id)」重建
    (project_fs.resolve_doc),外部传路径穿越串一律落在 docs 目录内、查无即 404。
    """
    scope = (request.query_params.get("scope") or "").strip()
    doc_type = (request.query_params.get("doc_type") or "").strip()
    filename = (request.query_params.get("filename") or "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if scope not in project_fs.SCOPES:
        return _fail(400, "资料范围不对(global/project)。", "INVALID_INPUT")
    if doc_type not in project_fs.DOC_TYPES:
        return _fail(400, "资料类型不对(regulation/task_book)。", "INVALID_INPUT")
    if not filename:
        return _fail(400, "没说要看哪份资料。", "INVALID_INPUT")
    if scope == project_fs.SCOPE_GLOBAL and doc_type != project_fs.DOC_REGULATION:
        return _fail(400, "全局资料只有规范。", "INVALID_INPUT")
    if scope == project_fs.SCOPE_PROJECT and not project_id:
        return _fail(400, "看项目资料要先选项目。", "INVALID_INPUT")

    disposition = _disposition_of(request)

    def _build() -> Any:
        if scope == project_fs.SCOPE_PROJECT and db.get_project(project_id) is None:
            return _fail(404, "没找到这个项目。", "NOT_FOUND")
        path = project_fs.resolve_doc(scope, doc_type, filename, project_id=project_id or None)
        if path is None:
            return _fail(404, f"没找到资料「{filename}」(可能已删)。", "NOT_FOUND")
        return _serve_file(path, media_type=_MEDIA_PDF, disposition=disposition, filename=path.name)

    return await run_in_threadpool(_build)


app = Starlette(
    routes=[
        Route("/projects", list_projects, methods=["GET"]),
        Route("/projects", create_project, methods=["POST"]),
        Route("/library", library, methods=["GET"]),
        Route("/files/drawing/{artifact_id}", serve_drawing_file, methods=["GET"]),
        Route("/files/cad-preview/{artifact_id}", serve_cad_preview, methods=["GET"]),
        Route("/files/doc", serve_doc_file, methods=["GET"]),
        Route("/projects/{project_id}/drawings", upload_drawing, methods=["POST"]),
        Route("/docs", upload_global_doc, methods=["POST"]),
        Route("/projects/{project_id}/docs", upload_project_doc, methods=["POST"]),
        Route("/projects/{project_id}", remove_project, methods=["DELETE"]),
        Route(
            "/projects/{project_id}/drawings/{drawing_id}",
            remove_drawing,
            methods=["DELETE"],
        ),
        Route("/docs", remove_global_doc, methods=["DELETE"]),
        Route("/projects/{project_id}/docs", remove_project_doc, methods=["DELETE"]),
        # 打卡链(gyt/checkin_api.py 的 CHECKIN_ROUTES)—— W7 两条(打卡、最近打卡)
        # + W8 两条(扫码配对的上报与轮询)。**别在这儿数条数**:以那个列表为准,
        # 加路由只改那边,这行注释写死数字就一定会过期(它已经过期过一次)。
        #
        # 为什么铺在这儿而不是它自己挂:``langgraph.json`` 的 ``http.app``
        # **只能有一个**,而本项目现在有两拨自定义路由 —— 本文件的项目/图纸/资料
        # 管理,和打卡。2026-08-15 两条分支合流时撞上,这里是唯一的汇合点。
        #
        # 打卡那侧的实现不在 backend/ 根而在 gyt 包里(它要 from gyt.config 取常量,
        # 且要能被 docker-compose.dev.yml 的 src 挂载热重载覆盖到)。
        # ⚠️ 删掉这一行 = 打卡端点整个消失,而现象是 404、**不是启动报错**。
        *CHECKIN_ROUTES,
        # 监理这一摊(gyt/supervision_api.py 的 SUPERVISION_ROUTES)。
        # ⚠️ **这一行以前写着「合计十一条」,而下面第 16 行就明令「别在这儿数条数」** ——
        #    同一段注释自己打自己,而且那个数在 2026-08-22 加 extend / reassign /
        #    ingest-failures 时已经是错的。数字已删,以那个列表为准。
        # W9 七条**写入**:确认 / 定级 / 通知单 / 暂停令三文书 / 复查结论 / 复工令 /
        # 上报主管部门;W10 又加三条,因为界面上那块处置面板改成了**常驻操作台**、
        # 数据不再从聊天流里取(根因见 docs/W10_界面取不到工具返回_方案.md):
        #   GET  /supervision/hazards              隐患清单(scope 四选一、project_id 三态)
        #   GET  /supervision/hazards/{hazard_no}  单条详情 + 证据链
        #   POST /supervision/reject               否决一条待确认的隐患
        # 再加一条**上传**(它既不是查询也不是动作,不认识任何一条隐患):
        #   POST /supervision/photo                直传复查照片,换一个 photo_id
        #     —— 「登记复查结论」要填 32 位 hex,而后端此前唯一的 PHOTO 产出口在聊天链
        #     (core/uploads.py),于是做复查的人得先把照片发进聊天框、再把编号抄回表单。
        #     ⚠️ 它收的是**原始图片字节**,公网那侧要给它单开一条更大的请求体闸,
        #        判据与那条 route 的写法在 supervision_api.SUPERVISION_ROUTES 的 docstring 里。
        # **别在这儿数条数**,同上:以那个列表为准 —— 这行数字已经跟着改过两次。
        #
        # 挂在这里的理由与打卡那一铺一字不差:``langgraph.json`` 的 ``http.app``
        # 只能有一个,这里是三拨自定义路由唯一的汇合点。实现同样住在 gyt 包里
        # (它要 from gyt.db / gyt.core 取东西,且要能被 docker-compose.dev.yml
        # 的 src 挂载热重载覆盖到)。
        # ⚠️ 删掉这一行 = 监理端点整个消失,现象还是 404、**不是启动报错**。
        *SUPERVISION_ROUTES,
        # 耗时观测(gyt/timing_api.py 的 TIMING_ROUTES)—— 一条,**只读**:
        #   GET /timing?thread_id=…&since=…   某个会话里「每步花了多久」的增量拉取
        #
        # 为什么它也在这儿:同上,``http.app`` 只能有一个,这是第四拨自定义路由。
        # 为什么耗时不走聊天流(它本来是走 custom 事件的,2026-08-21 换掉):
        # 值得看的模型调用全在子图里,子图的 custom 事件要开 ``subgraphs=True``
        # 才出得来,而一开它子图的 ``values`` 就会整份替换前端主状态 ——
        # 子 Agent 说的话先出现再消失。完整推演在 gyt/core/timing.py 模块头注。
        # ⚠️ 删掉这一行 = 界面上耗时行**一行都不出、控制台干净**(前端拿到 404
        #    是安静吞掉的:观测件坏了不许打扰工友)。不会有任何东西说话。
        *TIMING_ROUTES,
        # 聊天附件直传(gyt/upload_api.py 的 UPLOAD_ROUTES)—— 一条:
        #   POST /attachments?name=…   收原始字节 → 登记产物 → 回一个 32 位编号
        #
        # 🔴 为什么要它:老路是 base64 随消息发上来、pre_model_hook 再改写成编号,
        # 而**改写在第 9 步、那条带 base64 的消息第 8 步就已经进检查点了** ——
        # RemoveMessage 追不回来,于是每张照片在某个检查点里留一份永久拷贝。
        # 线上实测 11 条会话 = langgraph 内存库 1.1 GB,而机器一共 1966 MB,
        # 每 10 秒还要全量 pickle 一遍 —— 一条零载荷的 404 都要 12.7 秒。
        # 完整推演在 gyt/upload_api.py 与 gyt/core/uploads.py 的模块头注。
        # ⚠️ 删掉这一行 = 前端传附件时拿到 404,而它**会退回老路**(base64 进消息)——
        #    也就是说功能不坏、只是慢病复发,而且没有任何东西会说话。
        *UPLOAD_ROUTES,
        # 巡检记录抽屉(gyt/reports_api.py 的 REPORTS_ROUTES)—— 一条,**只读**:
        #   GET /reports?limit=…   最近几份巡检记录(编号 / 文件名 / 产物编号)
        #
        # 🔴 为什么要它:「拍照 → 自动出 Word」这条链的**终点一直是断的**。
        # 文档真的生成了、真的落盘了,而那份 Envelope 被 supervisor 的
        # output_mode="last_message" 整个丢掉(W10 的老根因),于是 tool-calls.tsx
        # 里那张巡检记录卡一次都没渲染出来过;而 agents/report/prompt.md 教模型说
        # 「要打印或转发跟管理员说编号就行」—— **那个管理员不存在**(TODO-34 的原话)。
        # 结果是:文件就在服务器上,而谁都拿不到。
        # ⚠️ 删掉这一行 = 抽屉里永远空着,而现象是 404 —— 前端对非 2xx 是安静走开的。
        *REPORTS_ROUTES,
    ]
)

__all__ = ["app"]
