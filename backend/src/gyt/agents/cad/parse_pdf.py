"""PDF 图纸解析:pypdf 逐页抽文字 → 一个纯 JSON 可序列化的索引(与 parse.py 平行的另一条线)。

===========================================================================
为什么 PDF 是独立一条线,不塞进 DXF 那套
---------------------------------------------------------------------------
    DXF 是**结构化矢量**:图层、块(构件)、DIMENSION 标注都是带语义的对象,所以能答
    「几个图层/多少柱/这道标注读数是 6000」。PDF 图纸(多为 AutoCAD/天正「打印成 PDF」的
    矢量件)里只有线条路径 + 文字字形 —— **没有图层、没有块、没有 DIMENSION 对象**。
    能如实抽出来的只有**图上的文字**(标高、房间名、标注数字都是文字),其余结构化查询
    在 PDF 上做不了,工具层照实说,不假装。

    本文件全部是**同步阻塞**函数(pypdf 抽文本是阻塞 IO/CPU):调用方(index.ensure_index)
    负责用 ``asyncio.to_thread`` 丢线程池。这里绝不 import asyncio、绝不落盘。

扫描件(has_text=False):有的 PDF 是纸质图扫描/拍照,没有文字层,一个字也抽不到。
    这时 has_text 为 False,工具层会如实说「这是扫描件,读不了文字,只能看预览」——
    不硬编、不假装 OCR(视觉/OCR 是后续可选项,不在本次范围)。

损坏/加密 PDF:本层**故意让 pypdf 的异常向上抛**,不在这里吞。
    工具层(tools.py)接住 ``pypdf.errors.PyPdfError`` 翻成 FILE_CORRUPT 的中文信封 ——
    与 DXF 那条线接 ezdxf.DXFError 同一姿势。
===========================================================================
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf import PdfReader

# 抽到的文字条数上限:真实施工图一页上千条文字,全塞进索引/提示词会撑爆(与 parse._texts
# 的 limit 同一考量)。默认 200 条,够回答标高/房间名/标注数字这类问题。
_ANNOTATION_LIMIT = 200

# 短于这个长度的碎片(单个标点、页码残渣)跳过,别当有效文字(与 ingest.pdf_to_documents 同源)。
_MIN_FRAGMENT_LEN = 1


def _page_lines(page: Any, page_no: int) -> list[dict[str, Any]]:
    """把一页抽出的文本切成行,每行记 {text, page}。空行/纯空白丢掉。"""
    raw = page.extract_text() or ""
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        text = line.strip()
        if len(text) >= _MIN_FRAGMENT_LEN:
            out.append({"text": text, "page": page_no})
    return out


def parse_pdf(path: Path) -> dict[str, Any]:
    """解析一张 PDF 图纸,产出与 DXF 索引平行的中间对象(不含 source_artifact_id)。

    阻塞函数:调用方负责 ``asyncio.to_thread`` 包。
    文件损坏/加密打不开时**不吞异常**,让 pypdf 抛给工具层翻成 FILE_CORRUPT。
    """
    reader = PdfReader(str(path))
    page_count = len(reader.pages)

    annotations: list[dict[str, Any]] = []
    for page_no, page in enumerate(reader.pages, start=1):
        for item in _page_lines(page, page_no):
            annotations.append(item)
            if len(annotations) >= _ANNOTATION_LIMIT:
                break
        if len(annotations) >= _ANNOTATION_LIMIT:
            break

    return {
        "format": "pdf",  # 索引格式判别:工具层按它走 pdf 分支
        "page_count": page_count,
        # 有没有可抽的文字:区分「矢量 PDF(能问文字)」与「扫描件(只能看预览)」。
        "has_text": bool(annotations),
        "annotations": annotations,
    }


__all__ = ["parse_pdf"]
