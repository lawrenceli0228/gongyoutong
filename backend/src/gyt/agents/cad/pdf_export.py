"""DXF → 矢量 PDF 的导出与缓存(render.to_pdf 之上的一层「只转一次」)。

===========================================================================
为什么要缓存,以及缓存钉在哪
---------------------------------------------------------------------------
    渲染一张真实施工图仍要几秒到几十秒(SVG→PDF 的 svglib 那步是大头,见 render.py),
    而「预览一张图」用户会点很多次。所以首次导出后把生成的 PDF **注册成产物**,并记一条
    映射:`<源 DXF 的 artifact_id>` → `{pdf_id, dxf_sha256}`。同一张没变过的 DXF 再来预览
    就直接返回上次那份 PDF 的 id,零重算。

    缓存失效判据是**源 DXF 的 sha256**(产物元数据里现成就有,见 core/artifacts.register):
    换了内容(哪怕同一个 artifact_id 被重新注册过)sha 变,缓存自然作废、重转。
    另一道校验是「上次那份 PDF 还在不在」—— 产物被清理过(删图/删项目)就当未命中重转。

    映射落在 config.cad_index_dir 下,文件名 `<dxf_id>.pdfmap.json`。与结构化索引
    (`<dxf_id>.json`,见 index.py)同目录但后缀不同,互不覆盖;drawing_id 先过
    ARTIFACT_ID_RE 才拼文件名,天然不含 "/"/".."/glob 通配符,防路径穿越。

阻塞:readfile + SVG 渲染 + SVG→PDF + 读写盘全是阻塞 IO/CPU。本模块同步实现,
    调用方(工具层 / webapp 端点)负责 asyncio.to_thread 丢线程池。

图元数保护不在这里做:调用方本来就先 ensure_index 拿到了图元总数,在那一层按
    settings.drawing_render_max_entities 拦超大图(如实说「太大先没出」),别转到一半卡死。
===========================================================================
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from gyt.agents.cad import render
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind

logger = logging.getLogger(__name__)

_MAP_SUFFIX = ".pdfmap.json"
_TMP_SUFFIX = ".tmp"


def _map_path(dxf_artifact_id: str) -> Path:
    """定位映射文件。id 非法直接抛,别拿脏字符串去拼路径(同 index._index_path)。"""
    if not artifacts.ARTIFACT_ID_RE.fullmatch(dxf_artifact_id):
        raise ValueError(f"drawing_id 不是合法的 artifact_id:{dxf_artifact_id!r}")
    return get_settings().cad_index_dir / f"{dxf_artifact_id}{_MAP_SUFFIX}"


def _read_map(dxf_artifact_id: str) -> dict[str, Any] | None:
    """读映射;文件不存在或读坏了都返回 None(当未命中重转),不抛。"""
    path = _map_path(dxf_artifact_id)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("PDF 导出映射 %s 读坏了,按未命中重转", dxf_artifact_id)
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_map(dxf_artifact_id: str, pdf_id: str, dxf_sha256: str) -> None:
    """原子落映射(先写唯一临时文件再 rename,防并发首转互踩,同 index.write)。"""
    path = _map_path(dxf_artifact_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=_TMP_SUFFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pdf_id": pdf_id, "dxf_sha256": dxf_sha256}, fh, ensure_ascii=False)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _pdf_name_for(dxf_artifact_id: str) -> str:
    """给导出的 PDF 起个人类可读的原名:源 DXF 原名主干 + .pdf(仅进产物元数据、下载卡片)。"""
    try:
        original = str(artifacts.read_meta(dxf_artifact_id).get("original_name") or "")
    except artifacts.ArtifactNotFound:
        original = ""
    stem = PurePosixPath(original).stem or "drawing"
    return f"{stem}.pdf"


def export_dxf_to_pdf(dxf_artifact_id: str) -> str:
    """把一张 DXF 导出成矢量 PDF 并注册为产物,返回 PDF 的 artifact_id。阻塞函数。

    命中缓存(源 sha 未变、上次那份 PDF 还在)直接返回旧 id;否则渲染 → 注册 → 记映射。
    可能抛 ``artifacts.ArtifactNotFound``(源 id 无效/文件丢失)与 ezdxf 解析异常(文件损坏)——
    都由调用方接住翻成对应中文信封,本函数不吞。
    """
    dxf_path = artifacts.resolve(dxf_artifact_id)  # 可能抛 ArtifactNotFound
    dxf_sha256 = str(artifacts.read_meta(dxf_artifact_id).get("sha256") or "")

    cached = _read_map(dxf_artifact_id)
    if cached and cached.get("dxf_sha256") == dxf_sha256:
        pdf_id = str(cached.get("pdf_id") or "")
        try:
            artifacts.resolve(pdf_id)  # 上次那份还在才算命中
        except artifacts.ArtifactNotFound:
            logger.info("PDF 导出缓存的产物 %s 已不在,重转 %s", pdf_id, dxf_artifact_id)
        else:
            logger.info("PDF 导出命中缓存:%s → %s", dxf_artifact_id, pdf_id)
            return pdf_id

    pdf_bytes = render.to_pdf(dxf_path)  # 可能抛 ezdxf.DXFError(文件损坏)
    pdf_id = artifacts.register(
        pdf_bytes, kind=ArtifactKind.DRAWING, original_name=_pdf_name_for(dxf_artifact_id)
    )
    _write_map(dxf_artifact_id, pdf_id, dxf_sha256)
    logger.info("DXF %s 导出为 PDF %s(%d 字节)", dxf_artifact_id, pdf_id, len(pdf_bytes))
    return pdf_id


__all__ = ["export_dxf_to_pdf"]
