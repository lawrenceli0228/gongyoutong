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
from uuid import uuid4

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.agents.knowledge import ingest, store
from gyt.checkin_api import CHECKIN_ROUTES
from gyt.config import ALLOWED_CAD_EXT, get_settings
from gyt.core import artifacts, project_fs
from gyt.core.artifacts import ArtifactKind
from gyt.db import hazards
from gyt.db import projects as db
from gyt.supervision_api import SUPERVISION_ROUTES

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

    def _work() -> tuple[list[Any], list[Any], list[Any], dict[tuple[str, str, str], int]]:
        return (
            db.list_projects(),
            db.list_drawings(),
            project_fs.list_docs(),
            store.count_chunks_by_doc(),  # 每份文档的向量块数,标「入库中 / 已入库 N 段」
        )

    projects, drawings, docs, chunk_counts = await run_in_threadpool(_work)
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
                # 向量块数:0 = 还在入库(embedding 未完),>0 = 已入库、可检索。
                "chunks": chunk_counts.get((e.scope, e.project_id or "", e.filename), 0),
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
            landed.unlink(missing_ok=True)
            artifacts.delete(artifact_id)
            return {"status": "empty"}
        return {"status": "ok", "landed": str(landed), "artifact_id": artifact_id}

    prep = await run_in_threadpool(_prepare)
    prep_status = prep["status"]
    if prep_status == "no_project":
        return _fail(404, "没找到这个项目,先建项目再往里传资料。", "NOT_FOUND")
    if prep_status == "bad_pdf":
        return _fail(
            422, f"「{filename}」打不开(可能加密或损坏),没存进去,换一份 PDF 再传。", "FILE_CORRUPT"
        )
    if prep_status == "empty":
        return _fail(
            422,
            f"「{filename}」没读到可检索的文字(多半是扫描件/图片版 PDF),"
            "需要先做 OCR 转成文字版再传。已自动清掉,没留在项目里。",
            "EMPTY_RESULT",
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
        # 监理这一摊(gyt/supervision_api.py 的 SUPERVISION_ROUTES)—— 合计十条。
        # W9 七条**写入**:确认 / 定级 / 通知单 / 暂停令三文书 / 复查结论 / 复工令 /
        # 上报主管部门;W10 又加三条,因为界面上那块处置面板改成了**常驻操作台**、
        # 数据不再从聊天流里取(根因见 docs/W10_界面取不到工具返回_方案.md):
        #   GET  /supervision/hazards              隐患清单(scope 四选一、project_id 三态)
        #   GET  /supervision/hazards/{hazard_no}  单条详情 + 证据链
        #   POST /supervision/reject               否决一条待确认的隐患
        # **别在这儿数条数**,同上:以那个列表为准 —— 这行数字已经跟着改过一次。
        #
        # 挂在这里的理由与打卡那一铺一字不差:``langgraph.json`` 的 ``http.app``
        # 只能有一个,这里是三拨自定义路由唯一的汇合点。实现同样住在 gyt 包里
        # (它要 from gyt.db / gyt.core 取东西,且要能被 docker-compose.dev.yml
        # 的 src 挂载热重载覆盖到)。
        # ⚠️ 删掉这一行 = 监理端点整个消失,现象还是 404、**不是启动报错**。
        *SUPERVISION_ROUTES,
    ]
)

__all__ = ["app"]
