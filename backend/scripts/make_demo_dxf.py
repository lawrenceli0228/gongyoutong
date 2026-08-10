"""生成 CAD Agent 的三张演示/测试 DXF,并当场自检。

三张的角色(详见 .personal/CAD_Agent_落地文档.md 第 1.2 / 5 节):

    plan_utf8.dxf   普通图:现代版本(R2010=AC1024,UTF-8)。对照组,无编码戏。
    plan_gbk.dxf    头号图:老版本(R2000=AC1015)+ $DWGCODEPAGE=ANSI_936(GBK),
                    含中文图层名。GBK 只在 ≤R2004 才生效 —— 新版是 UTF-8,
                    用新版存这张,GBK 解码路径根本不走,测了个寂寞。
    broken.dxf      损坏图:把 plan_utf8 从中间截断。ezdxf.readfile 应抛异常,
                    工具层要 catch 成 FILE_CORRUPT。

跑法(在 backend/ 下):
    uv run python scripts/make_demo_dxf.py

产物落在仓库根的 data/demo/drawings/。脚本末尾会自检:
GBK 图必须能解回中文图层名「轴线」,且 doc.encoding == 'gbk';否则退出码 1。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import recover

# 仓库根:本文件在 backend/scripts/ 下,parents[2] = 仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data" / "demo" / "drawings"

# 演示要问的柱距,标注出来给 query_dimension 用(没标注它答不了,见文档 6.2)。
SPAN_MM = 6000


def _add_span_dim(msp, *, layer: str) -> None:
    """加一道水平线性标注,读数 = SPAN_MM。render() 必须调,否则标注不出图元。"""
    msp.add_line((0, 0), (SPAN_MM, 0), dxfattribs={"layer": layer})
    dim = msp.add_linear_dim(
        base=(0, -500),  # 尺寸线的位置(基线)
        p1=(0, 0),
        p2=(SPAN_MM, 0),
        dxfattribs={"layer": layer},
    )
    dim.render()


def make_plan_utf8(path: Path) -> None:
    """普通图:现代版本、UTF-8。对照组。"""
    doc = ezdxf.new("R2010")  # AC1024,现代版本一律 UTF-8
    doc.header["$INSUNITS"] = 4  # 毫米。尺寸没单位=没意义(风险 #6);这个头不会被覆写
    doc.layers.add("WALL")
    doc.layers.add("AXIS")
    doc.layers.add("DIM")
    msp = doc.modelspace()
    msp.add_line((0, 0), (SPAN_MM, 0), dxfattribs={"layer": "WALL"})
    msp.add_line((0, 0), (0, 3000), dxfattribs={"layer": "WALL"})
    msp.add_circle((SPAN_MM / 2, 1500), radius=300, dxfattribs={"layer": "AXIS"})
    _add_span_dim(msp, layer="DIM")
    doc.saveas(path)


def make_plan_gbk(path: Path) -> None:
    """头号图:老版本 R2000 + ANSI_936(GBK),中文图层名/文字。"""
    doc = ezdxf.new("R2000")  # AC1015,只有 ≤R2004 才走 $DWGCODEPAGE
    # 必须直接设 doc.encoding —— ezdxf 存盘时会用 tocodepage(doc.encoding) 覆写
    # $DWGCODEPAGE(document.py 第 712 行),手动设 header 会被丢弃。设成 'gbk' 后:
    # output_encoding=gbk(R2000<R2007 走 self.encoding)、存盘 $DWGCODEPAGE=ANSI_936。
    doc.encoding = "gbk"  # 存盘按 GBK 写中文字节,$DWGCODEPAGE 随之写成 ANSI_936
    doc.header["$INSUNITS"] = 4  # 毫米,同 utf8 图
    doc.layers.add("墙")
    doc.layers.add("轴线")
    doc.layers.add("柱")
    doc.layers.add("标注")
    # 中文文字样式:ezdxf 渲染 DXF 里的 TEXT 用的是**文字样式的字体**,不认 matplotlib
    # 的 rcParams。想让 PNG 里的中文标题不是方框,就得让样式指向一个 CJK 字体文件
    # (SimHei 是单一 TTF,Windows 自带,ezdxf 字体管理器找得到)。
    doc.styles.add("HZ", font="simhei.ttf")

    # 一个像点样的小平面:12m×6m 外墙,3×2 柱网(6 根 KZ1),纵横轴线,两道标注。
    # 这样 render_preview 出来是张认得出的平面图,而不是一条线;
    # 也让 list_components / layer_stats / query_dimension 各有真东西可查。
    width, height = 2 * SPAN_MM, SPAN_MM  # 12000 × 6000
    xs, ys = (0, SPAN_MM, 2 * SPAN_MM), (0, SPAN_MM)

    blk = doc.blocks.new("柱-KZ1")  # 400×400 的方,代表一根框架柱
    blk.add_lwpolyline([(-200, -200), (200, -200), (200, 200), (-200, 200)], close=True)

    msp = doc.modelspace()
    # 外墙轮廓
    msp.add_lwpolyline(
        [(0, 0), (width, 0), (width, height), (0, height)], close=True, dxfattribs={"layer": "墙"}
    )
    # 轴线(纵 3 横 2,两端各出头 800)
    for x in xs:
        msp.add_line((x, -800), (x, height + 800), dxfattribs={"layer": "轴线"})
    for y in ys:
        msp.add_line((-800, y), (width + 800, y), dxfattribs={"layer": "轴线"})
    # 柱:每个网格交点插一根
    for x in xs:
        for y in ys:
            msp.add_blockref("柱-KZ1", (x, y), dxfattribs={"layer": "柱"})
    # 标注:底部一跨柱距 6000 + 整体 12000(query_dimension 演示读数)
    for base_y, p2x in ((-1600, SPAN_MM), (-3000, width)):
        dim = msp.add_linear_dim(
            base=(0, base_y), p1=(0, 0), p2=(p2x, 0), dxfattribs={"layer": "标注"}
        )
        dim.render()
    # 标题
    title = msp.add_text("首层平面图", dxfattribs={"layer": "轴线", "height": 400, "style": "HZ"})
    title.set_placement((0, height + 1500))
    doc.saveas(path)  # 存盘即用 GBK 字节写中文


def make_broken(src: Path, path: Path) -> None:
    """损坏图:把一张好 DXF 从中间截断。"""
    raw = src.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])


def _verify() -> list[str]:
    """自检。返回失败信息列表(空 = 全过)。"""
    problems: list[str] = []

    # 1) 普通图:能读、有 WALL 图层。
    try:
        doc = ezdxf.readfile(OUT_DIR / "plan_utf8.dxf")
        layers = {ly.dxf.name for ly in doc.layers}
        if "WALL" not in layers:
            problems.append(f"plan_utf8:缺 WALL 图层,实得 {sorted(layers)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"plan_utf8 读失败:{type(exc).__name__}: {exc}")

    # 2) GBK 图(头号):必须解回中文「轴线」,且 encoding 是 gbk。
    try:
        doc = ezdxf.readfile(OUT_DIR / "plan_gbk.dxf")
        layers = {ly.dxf.name for ly in doc.layers}
        if "轴线" not in layers:
            problems.append(f"plan_gbk:中文图层名没解回来,实得 {sorted(layers)}")
        if doc.encoding != "gbk":
            problems.append(f"plan_gbk:encoding 期望 gbk,实得 {doc.encoding!r}")
        ver = doc.dxfversion
        if ver > "AC1018":  # 字符串比较即版本比较;>AC1018 说明不是老版本,GBK 路径没走
            problems.append(f"plan_gbk:版本 {ver} 太新(>AC1018),GBK 路径不会被走到")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"plan_gbk 读失败:{type(exc).__name__}: {exc}")

    # 3) 损坏图:ezdxf.readfile 应当失败(这里期望它抛)。
    try:
        ezdxf.readfile(OUT_DIR / "broken.dxf")
        # recover 也救不动才算真损坏;能读回来说明截得不够狠。
        try:
            recover.readfile(OUT_DIR / "broken.dxf")
        except Exception:  # noqa: BLE001
            pass
        problems.append("broken:ezdxf.readfile 居然读成功了,截断不够,FILE_CORRUPT 测不到")
    except Exception:  # noqa: BLE001
        pass  # 期望的失败

    return problems


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    make_plan_utf8(OUT_DIR / "plan_utf8.dxf")
    make_plan_gbk(OUT_DIR / "plan_gbk.dxf")
    make_broken(OUT_DIR / "plan_utf8.dxf", OUT_DIR / "broken.dxf")
    print(f"已生成到 {OUT_DIR}:")
    for name in ("plan_utf8.dxf", "plan_gbk.dxf", "broken.dxf"):
        size = (OUT_DIR / name).stat().st_size
        print(f"  {name}  ({size} bytes)")

    problems = _verify()
    print()
    if problems:
        print("自检未通过:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print(
        "自检通过:普通图可读 / GBK 图解回中文「轴线」且 encoding=gbk 版本≤AC1018 / 损坏图如期读失败"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
