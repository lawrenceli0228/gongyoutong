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
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.agents.knowledge import ingest
from gyt.config import ALLOWED_CAD_EXT, get_settings
from gyt.core import artifacts, project_fs
from gyt.core.artifacts import ArtifactKind
from gyt.db import projects as db

logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1024 * 1024
_PDF_EXT = ".pdf"


# --- 信封响应 ----------------------------------------------------------------


def _envelope(ok: bool, data: Any, user_msg: str, error_code: str | None) -> dict[str, Any]:
    return {"ok": ok, "data": data, "user_msg": user_msg, "error_code": error_code}


def _ok(data: Any, user_msg: str, *, status: int = 200) -> JSONResponse:
    return JSONResponse(_envelope(True, data, user_msg, None), status_code=status)


def _fail(status: int, user_msg: str, error_code: str) -> JSONResponse:
    return JSONResponse(_envelope(False, None, user_msg, error_code), status_code=status)


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

    def _work() -> tuple[list[Any], list[Any], list[Any]]:
        return (
            db.list_projects(),
            db.list_drawings(),
            project_fs.list_docs(),
        )

    projects, drawings, docs = await run_in_threadpool(_work)
    name_by_id = {p.id: p.name for p in projects}
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
                "created_at": d.created_at,
            }
            for d in drawings
        ],
        "docs": [
            {
                "scope": e.scope,
                "project_id": e.project_id,
                "project_name": name_by_id.get(e.project_id) if e.project_id else None,
                "doc_type": e.doc_type,
                "filename": e.filename,
                "rel_path": e.rel_path,
                "size_bytes": e.size_bytes,
                "modified_at": e.modified_at,
            }
            for e in docs
        ],
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
    """POST /projects/{project_id}/drawings —— 上传一张 DXF 图纸并入库。

    multipart:file(.dxf)、view_type(plan/elevation/section)、title(可省=文件名主干)、floor(可选)。
    落地 → 注册产物 → 写 drawings 行,一条龙。
    """
    pid = request.path_params["project_id"]
    form = await request.form()
    upload = form.get("file")
    view_type = str(form.get("view_type") or "").strip()
    floor = str(form.get("floor") or "").strip() or None
    title = str(form.get("title") or "").strip()

    if upload is None or not hasattr(upload, "filename"):
        return _fail(400, "没收到图纸文件,请选一个 .dxf 再传。", "INVALID_INPUT")
    filename = upload.filename or ""
    if view_type not in db.VIEW_TYPES:
        return _fail(
            400, "要标明这是平面图、立面图还是剖面图(plan/elevation/section)。", "INVALID_INPUT"
        )
    if PurePosixPath(filename).suffix.lower() not in ALLOWED_CAD_EXT:
        return _fail(415, "只收 DXF 图纸(.dxf);DWG 请先离线转成 DXF 再传。", "FILE_UNSUPPORTED")

    payload = await upload.read()
    max_bytes = int(get_settings().drawing_max_mb * _BYTES_PER_MB)
    if len(payload) > max_bytes:
        return _fail(
            413,
            f"这张图纸有 {len(payload) / _BYTES_PER_MB:.1f}MB,超过 "
            f"{get_settings().drawing_max_mb:.0f}MB 上限,先精简再传。",
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

    # 上传做成**原子**的:要么「落盘 + 入库成功、可检索」,要么彻底不留 —— 别再出现
    # 「文件躺在资料库里、问答却搜不到」的幽灵(GB-50016 那次:入库跑了几分钟被容器重建掐断,
    # 文件已落地、chroma 却是空的)。入库任何环节失败或一段都没抽到,都回滚落地文件 + 产物。
    def _work() -> dict[str, Any]:
        if scope == project_fs.SCOPE_PROJECT and db.get_project(project_id) is None:
            return {"status": "no_project"}
        landed = project_fs.land_doc(scope, doc_type, filename, payload, project_id=project_id)
        artifact_id = artifacts.register(
            landed, kind=ArtifactKind.DOCUMENT, original_name=filename
        )
        try:
            chunks = ingest.ingest_document(
                landed, scope=scope, doc_type=doc_type, project_id=project_id
            )
        except Exception:  # noqa: BLE001 —— 入库任何环节炸了都要回滚,不留搜不到的幽灵文件
            landed.unlink(missing_ok=True)
            artifacts.delete(artifact_id)
            logger.exception("文档 %s 入库失败,已回滚落地文件与产物", filename)
            return {"status": "ingest_error"}
        if chunks == 0:
            # 一段文字都抽不到(多半是扫描件/图片版 PDF):同样回滚,并如实告诉用户要先 OCR。
            landed.unlink(missing_ok=True)
            artifacts.delete(artifact_id)
            return {"status": "empty"}
        return {"status": "ok", "artifact_id": artifact_id, "chunks": chunks}

    result = await run_in_threadpool(_work)
    status = result["status"]
    if status == "no_project":
        return _fail(404, "没找到这个项目,先建项目再往里传资料。", "NOT_FOUND")
    if status == "ingest_error":
        return _fail(
            500,
            f"「{filename}」入库失败,已自动清掉没占地方,请稍后重试或换一份文件。",
            "INTERNAL",
        )
    if status == "empty":
        return _fail(
            422,
            f"「{filename}」没读到可检索的文字(多半是扫描件/图片版 PDF),"
            "需要先做 OCR 转成文字版再传。已自动清掉,没留在项目里。",
            "EMPTY_RESULT",
        )
    kind_cn = "规范" if doc_type == project_fs.DOC_REGULATION else "任务书"
    logger.info("收到%s文档 %s(scope=%s project=%s)", kind_cn, filename, scope, project_id or "-")
    return _ok(
        {
            "scope": scope,
            "project_id": project_id or None,
            "doc_type": doc_type,
            "filename": filename,
            "artifact_id": result["artifact_id"],
            "chunks": result["chunks"],
        },
        f"{kind_cn}「{filename}」上传成功,已入库 {result['chunks']} 段。",
        status=201,
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
    全局规范(project_id="")不受影响。顺序:先图纸(外键)→ 项目向量 → 目录 → 项目行。
    """
    pid = request.path_params["project_id"]

    def _work() -> dict[str, Any] | None:
        if db.get_project(pid) is None:
            return None
        rows = db.delete_project_drawings(pid)
        for r in rows:
            project_fs.delete_drawing_file(pid, r.rel_path)
            artifacts.delete(r.artifact_id)
            (get_settings().cad_index_dir / f"{r.id}.json").unlink(missing_ok=True)
        chunks = ingest.delete_project_documents(pid)
        project_fs.delete_project_tree(pid)
        db.delete_project(pid)
        return {"drawings": len(rows), "chunks": chunks}

    result = await run_in_threadpool(_work)
    if result is None:
        return _fail(404, "没找到这个项目(可能已删)。", "NOT_FOUND")
    logger.info("删除项目 %s(图纸 %d 张,向量 %d 段)", pid, result["drawings"], result["chunks"])
    return _ok(
        {"project_id": pid, **result},
        f"项目已删除(含 {result['drawings']} 张图纸、{result['chunks']} 段规范索引)。",
    )


app = Starlette(
    routes=[
        Route("/projects", list_projects, methods=["GET"]),
        Route("/projects", create_project, methods=["POST"]),
        Route("/library", library, methods=["GET"]),
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
    ]
)

__all__ = ["app"]
