"""CAD 测试专用的 DXF 造样工具(下划线开头 → pytest 不当测试收集)。

单测**不依赖** data/demo/drawings 下脚本生成的资产,自己就地在 tmp 里造最小样例,
这样测试是自洽的、可复现的,不会因为有人重跑生成脚本而漂移。造法与 scripts/make_demo_dxf.py
同源,但只留断言要用到的最小图元。GBK 图必须 R2000 + doc.encoding='gbk'(见落地文档第 5 节)。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf.entities import DXFTagStorage
from ezdxf.lldxf.extendedtags import ExtendedTags
from ezdxf.lldxf.types import dxftag

SPAN_MM = 6000


def make_plain_dxf(path: Path) -> None:
    """普通图:R2010(UTF-8),WALL/AXIS/DIM 三层,一线一圆一标注,单位 mm。"""
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("WALL")
    doc.layers.add("AXIS")
    doc.layers.add("DIM")
    msp = doc.modelspace()
    msp.add_line((0, 0), (SPAN_MM, 0), dxfattribs={"layer": "WALL"})
    msp.add_circle((SPAN_MM / 2, 1500), radius=300, dxfattribs={"layer": "AXIS"})
    dim = msp.add_linear_dim(
        base=(0, -500), p1=(0, 0), p2=(SPAN_MM, 0), dxfattribs={"layer": "DIM"}
    )
    dim.render()
    doc.saveas(path)


def make_gbk_dxf(path: Path) -> None:
    """头号图:R2000 + GBK,中文图层名 轴线/柱/标注,柱-KZ1 块×2,一道 6000 标注,单位 mm。"""
    doc = ezdxf.new("R2000")
    doc.encoding = "gbk"  # 存盘按 GBK 写中文,$DWGCODEPAGE 随之 ANSI_936(见落地文档第 5 节)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("轴线")
    doc.layers.add("柱")
    doc.layers.add("标注")
    blk = doc.blocks.new("柱-KZ1")
    blk.add_lwpolyline([(-150, -150), (150, -150), (150, 150), (-150, 150)], close=True)
    msp = doc.modelspace()
    for x in (0, SPAN_MM):
        msp.add_blockref("柱-KZ1", (x, 0), dxfattribs={"layer": "柱"})
    text = msp.add_text("首层平面图", dxfattribs={"layer": "轴线"})
    text.set_placement((100, 200))
    dim = msp.add_linear_dim(
        base=(0, -500), p1=(0, 0), p2=(SPAN_MM, 0), dxfattribs={"layer": "标注"}
    )
    dim.render()
    doc.saveas(path)


def _add_tch_proxy(doc, msp, dxftype: str, layer: str, handle: str) -> None:
    """往图里塞一个天正私有构件(``TCH_*``),模拟 ezdxf 认不得、当代理实体加载的情形。

    真天正图里,墙/柱/门窗是天正 ARX 注册的私有类。ezdxf 没有它们的定义,读进来就是
    ``DXFTagStorage`` —— 不暴露标准 ``.dxf.layer``。这里手搓一个:图层(组码 8)藏在
    ``AcDbEntity`` 子类里,正是 ``parse._layer_of`` 要退回去捞的地方。存盘 → 重读后,
    ezdxf 仍把它当未知类型加载,dxftype() 回 ``TCH_*``,和真图一致。
    """
    tags = [
        (0, dxftype),
        (5, handle),
        (330, "0"),
        (100, "AcDbEntity"),
        (8, layer),
        (100, "Acad" + dxftype),
        (1, "proprietary"),  # 私有数据占位,ezdxf 原样保管、不解释
    ]
    entity = DXFTagStorage.load(ExtendedTags([dxftag(*t) for t in tags]), doc)
    doc.entitydb.add(entity)
    msp.add_entity(entity)


def make_tianzheng_dxf(path: Path) -> None:
    """天正图:一条普通 AXIS 线 + 3 面 TCH_WALL、2 根 TCH_COLUMN 私有构件(单位 mm)。

    断言点:parse 不崩(旧代码在此 DXFAttributeError → 被误判成 FILE_CORRUPT),
    图层名从代理实体的组码 8 里读得出,tianzheng.detected 为真。
    """
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("AXIS")
    doc.layers.add("WALL-TCH")
    doc.layers.add("COL-TCH")
    msp = doc.modelspace()
    msp.add_line((0, 0), (SPAN_MM, 0), dxfattribs={"layer": "AXIS"})
    for i in range(3):
        _add_tch_proxy(doc, msp, "TCH_WALL", "WALL-TCH", f"A{i}")
    for i in range(2):
        _add_tch_proxy(doc, msp, "TCH_COLUMN", "COL-TCH", f"B{i}")
    doc.saveas(path)


def make_broken_dxf(path: Path) -> None:
    """损坏图:先造一张好图,再从中间截断,ezdxf.readfile 会抛 DXFStructureError。"""
    good = path.with_name("_tmp_good.dxf")
    make_plain_dxf(good)
    raw = good.read_bytes()
    good.unlink()
    path.write_bytes(raw[: len(raw) // 2])


# 造带中文文字的矢量 PDF 用的字体:优先系统 CJK 字体(Windows 自带雅黑),缺了就退回思源。
# 核心字体(Helvetica 等)渲染不了中文,必须内嵌一个含 CJK 字形的 TTF/TTC。
_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",  # 微软雅黑(原生 Windows 自带)
    r"C:\Windows\Fonts\simhei.ttf",  # 黑体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # WSL2/Linux 装 fonts-noto-cjk 后
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


def _find_cjk_font() -> str | None:
    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


# 矢量 PDF 上写的示例文字(标高/房间名/标注数字都是文字,正是 PDF 能读的那部分)。
PDF_TEXT_LINES = ("首层平面图", "标高 ±0.000", "客厅 3600", "卧室 3000")


def make_vector_pdf(path: Path) -> None:
    """造一张**矢量** PDF 图纸:一页,写几行中文文字(标高/房间名/标注),字体内嵌 CJK。

    模拟国内最常见的「AutoCAD/天正打印成 PDF」的矢量件 —— 有文字层,pypdf 抽得出。
    没找到 CJK 字体的环境会跳过(见 test 里的 skip)。
    """
    from fpdf import FPDF  # 仅测试期依赖,放函数内,别让 import 期就要求它在

    font_path = _find_cjk_font()
    if font_path is None:  # pragma: no cover —— CI/开发机基本都有 CJK 字体
        raise RuntimeError("环境里找不到可内嵌的 CJK 字体,make_vector_pdf 造不了带中文的 PDF")

    pdf = FPDF()
    pdf.add_font("cjk", "", font_path)
    pdf.set_font("cjk", size=14)
    pdf.add_page()
    for line in PDF_TEXT_LINES:
        pdf.cell(0, 10, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def make_scanned_pdf(path: Path) -> None:
    """造一张**无文字层**的 PDF(模拟扫描件):一页,只有一张纯色图片,抽不到任何文字。"""
    from fpdf import FPDF

    img = path.with_name("_tmp_scan.png")
    try:
        from PIL import Image

        Image.new("RGB", (200, 120), (200, 200, 200)).save(img)
        pdf = FPDF()
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=100)
        pdf.output(str(path))
    finally:
        img.unlink(missing_ok=True)
