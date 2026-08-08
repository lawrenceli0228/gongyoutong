"""CAD 测试专用的 DXF 造样工具(下划线开头 → pytest 不当测试收集)。

单测**不依赖** data/demo/drawings 下脚本生成的资产,自己就地在 tmp 里造最小样例,
这样测试是自洽的、可复现的,不会因为有人重跑生成脚本而漂移。造法与 scripts/make_demo_dxf.py
同源,但只留断言要用到的最小图元。GBK 图必须 R2000 + doc.encoding='gbk'(见落地文档第 5 节)。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf

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


def make_broken_dxf(path: Path) -> None:
    """损坏图:先造一张好图,再从中间截断,ezdxf.readfile 会抛 DXFStructureError。"""
    good = path.with_name("_tmp_good.dxf")
    make_plain_dxf(good)
    raw = good.read_bytes()
    good.unlink()
    path.write_bytes(raw[: len(raw) // 2])
