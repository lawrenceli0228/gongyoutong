"""PDF 图纸线单测:parse_pdf 抽文字 + render.pdf_to_png 出预览(矢量 PDF + 扫描件两种)。

矢量 PDF 造样要内嵌 CJK 字体;找不到字体的环境整体 skip(不让缺字体把 CI 拖红)。
"""

from __future__ import annotations

import pytest

from gyt.agents.cad import parse_pdf, render
from tests.unit._dxf_fixtures import (
    PDF_TEXT_LINES,
    _find_cjk_font,
    make_scanned_pdf,
    make_vector_pdf,
)

# 没有可内嵌的 CJK 字体就整文件跳过 —— 造不了带中文的矢量 PDF,断言无从谈起。
pytestmark = pytest.mark.skipif(
    _find_cjk_font() is None, reason="环境缺 CJK 字体,造不了带中文的矢量 PDF 样例"
)


def test_矢量PDF抽出文字且标为可读(tmp_path):
    make_vector_pdf(tmp_path / "vec.pdf")
    result = parse_pdf.parse_pdf(tmp_path / "vec.pdf")

    assert result["format"] == "pdf"
    assert result["page_count"] == 1
    assert result["has_text"] is True
    texts = " ".join(a["text"] for a in result["annotations"])
    # 图上写的标高/房间名都被如实抽出。
    for line in PDF_TEXT_LINES:
        assert line in texts
    # 每条都带页码(PDF 用 page 定位,和 DXF 用 layer 对称)。
    assert all("page" in a for a in result["annotations"])


def test_扫描PDF无文字层则has_text为假(tmp_path):
    make_scanned_pdf(tmp_path / "scan.pdf")
    result = parse_pdf.parse_pdf(tmp_path / "scan.pdf")

    assert result["format"] == "pdf"
    assert result["page_count"] == 1
    assert result["has_text"] is False
    assert result["annotations"] == []


def test_pdf_to_png出真PNG(tmp_path):
    make_vector_pdf(tmp_path / "vec.pdf")
    png = render.pdf_to_png(tmp_path / "vec.pdf")
    # 真栅格化:PNG 魔数开头、非空。
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 100


def test_pdf_to_png支持指定倍率与页码且默认不变(tmp_path):
    # 读字兜底要用更高倍率(小字更清)、可指定页;默认参数必须保持预览行为不变。
    import io

    from PIL import Image

    p = tmp_path / "scan.pdf"
    make_scanned_pdf(p)

    default_png = render.pdf_to_png(p)  # 老调用(位置参数):默认首页、预览倍率
    hi_png = render.pdf_to_png(p, scale=4.0)  # 更高倍率
    first_png = render.pdf_to_png(p, page_index=0)  # 显式首页

    for png in (default_png, hi_png, first_png):
        assert png[:8] == b"\x89PNG\r\n\x1a\n"  # 都是真 PNG

    # 倍率更高 → 像素尺寸更大(按图像宽度判,别按压缩后字节数,纯色图压得太狠不可靠)。
    assert Image.open(io.BytesIO(hi_png)).size[0] > Image.open(io.BytesIO(default_png)).size[0]
    # page_index=0 与默认同尺寸(都是首页)。
    assert Image.open(io.BytesIO(first_png)).size == Image.open(io.BytesIO(default_png)).size


def test_损坏PDF解析时抛pypdf异常(tmp_path):
    # parse 层故意不吞:异常冒到工具层才翻成 FILE_CORRUPT(与 DXF 那条线同姿势)。
    from pypdf.errors import PyPdfError

    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not a real pdf body")
    with pytest.raises(PyPdfError):
        parse_pdf.parse_pdf(bad)
