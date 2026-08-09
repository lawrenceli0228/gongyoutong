"""DXF → PNG 预览(落地文档第 7 节:中文字体 + 线程安全两个坑)。

===========================================================================
坑一:pyplot 非线程安全 —— 用 OO API,不碰全局 pyplot
---------------------------------------------------------------------------
    render 跑在工具层 asyncio.to_thread 的线程池里,而 pyplot 维护一份**全局**
    figure 状态,多线程同时画会互相踩。所以这里显式 Figure() + FigureCanvasAgg,
    把 Axes 交给 ezdxf 的 MatplotlibBackend,全程不 import pyplot、不用 qsave。

坑二:matplotlib 默认无中文字体 —— 图层名/标注会渲染成方框
---------------------------------------------------------------------------
    原生 Windows 直接指系统自带的 Microsoft YaHei / SimHei(不用装);
    WSL2/Linux 上装 fonts-noto-cjk 后 "Noto Sans CJK SC" 兜底。
    rcParams 是进程级全局设置,在 import 时设一次即可。
===========================================================================
"""

from __future__ import annotations

import io
from pathlib import Path

import ezdxf
import matplotlib
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

# 无头后端:服务器/线程池里没有显示设备,必须 Agg。用 OO API 时它不引 pyplot 全局态。
matplotlib.use("Agg")
# 中文字体:Windows 自带 YaHei/SimHei;Linux 兜 Noto。设一次(进程级),不进函数体。
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC"]
matplotlib.rcParams["axes.unicode_minus"] = False

_DPI = 150

# 白底 + 黑线:DXF 里图元多是 ACI 7(随背景取黑/白的自适应色)。默认背景是黑、7 号
# 画成白线;我们要出的是白底 PNG,不改配色的话就是白线画在白底上 —— 一片空白。
# 显式钉死「白底 + 前景全黑」,细线在浅色看图器里也看得清。
_LIGHT_CONFIG = Configuration(
    background_policy=BackgroundPolicy.WHITE,
    color_policy=ColorPolicy.BLACK,
)


def to_png(path: Path) -> bytes:
    """把一张 DXF 渲染成整图 PNG,返回字节。阻塞函数:调用方负责 to_thread 包。

    只渲染模型空间整图(MVP 砍掉局部裁剪 region,落地文档 6.5)。
    文件损坏/打不开时不吞异常,让 ezdxf 抛给工具层翻成 FILE_CORRUPT。
    """
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    fig = Figure(facecolor="white")
    FigureCanvasAgg(fig)  # 显式挂 Agg 画布(OO 路径,不经 pyplot)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    # finalize=True 让 addon 自己把纵横比与坐标范围摆正,不用手算 xlim/ylim。
    frontend = Frontend(RenderContext(doc), MatplotlibBackend(ax), config=_LIGHT_CONFIG)
    frontend.draw_layout(msp, finalize=True)

    buf = io.BytesIO()
    # bbox_inches="tight" 贴着图元裁掉四周空白,细长/小图也能填满画面;
    # 显式白底,免得默认透明背景在看图器里叠出诡异颜色。
    fig.savefig(
        buf, format="png", dpi=_DPI, facecolor="white", bbox_inches="tight", pad_inches=0.2
    )
    return buf.getvalue()


__all__ = ["to_png"]
