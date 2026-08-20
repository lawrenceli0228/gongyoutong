"""DXF → 矢量 PDF / PNG 预览(落地文档第 7 节:中文字体 + 线程安全两个坑)。

===========================================================================
渲染后端:ezdxf 原生 SVG 后端(2026-08-20 从 matplotlib 换过来)
---------------------------------------------------------------------------
    旧实现用 ezdxf 的 MatplotlibBackend 逐图元建 matplotlib artist,慢到离谱
    (实测 2280 图元 19s、真实施工图注释里记着 268s),阈值被迫压到 1000,几乎没有
    真图能出图。改用 ezdxf 自带的 **SVGBackend** —— 轻量 recorder 直接吐 SVG XML,
    同图快约 7x。矢量 PDF 由 svglib 把 SVG 解析成 reportlab 图元、再 renderPDF 写出。
    PNG 预览不再单独走一套渲染,而是复用「出 PDF」再用 pypdfium2 栅格化首页 ——
    一条快链,阈值(config.drawing_render_max_entities)对 PDF/PNG 两条路一致。

坑一:线程安全 —— 每次调用各建各的对象,无全局画布状态
---------------------------------------------------------------------------
    render 跑在工具层 asyncio.to_thread 的线程池里。旧实现要绕开 pyplot 的全局
    figure 状态;新实现的 SVGBackend / svg2rlg / reportlab canvas 都是**每次调用新建**,
    没有可被并发踩的进程级可变状态。ezdxf 的字体管理器是只读共享,不在此处建缓存。

坑二:中文字体 —— ezdxf 的 SVG 后端不认 matplotlib 的 rcParams
---------------------------------------------------------------------------
    SVGBackend 用 ezdxf 自己的字体系统(fontTools)解析文字为矢量路径。DXF 的文字样式
    默认字体多半没有中文字形,直接渲成 .notdef 豆腐块。所以渲染前把每个文字样式的字体
    指到一个含中文的字体(_CJK_FONT_CANDIDATES,平台知识留在代码里,同 watermark.py)。
    因为文字最终是 <path> 而非 <text>,产物 PDF/PNG 不再依赖阅读器/系统字体。
===========================================================================
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import ezdxf
import pypdfium2 as pdfium
from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
from ezdxf.fonts import fonts
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

logger = logging.getLogger(__name__)

# PDF 栅格化的缩放倍率:PDF 页以 72pt/英寸为基准,×2 ≈ 144 DPI,施工图上的细线/小字
# 也看得清,又不至于让预览图太大。read_drawing_text 的读字兜底会传更高的 scale。
_PDF_RENDER_SCALE = 2.0

# 整图四周留白(mm)。SVGBackend 的 Page(0,0,...) 会按内容外接框自动定尺寸,margins 只加边距。
_PAGE_MARGIN_MM = 5.0

# 白底 + 黑线:DXF 里图元多是 ACI 7(随背景取黑/白的自适应色)。显式钉死「白底 + 前景全黑」,
# 细线在浅色看图器里也看得清;不设的话 7 号会画成白线、白底上一片空白。
_LIGHT_CONFIG = Configuration(
    background_policy=BackgroundPolicy.WHITE,
    color_policy=ColorPolicy.BLACK,
)

# 含中文字形的字体候选(按平台优先级)。这是代码携带的平台知识 —— 同 attendance/watermark.py
# 的候选表,不进 config.py(放 .env 里没人会改,反而多一处会漂的拷贝)。
# Windows 自带 YaHei/SimHei;Linux 装 fonts-noto-cjk 后有 Noto;SimSun 兜底。
_CJK_FONT_CANDIDATES = (
    "msyh.ttc",  # Microsoft YaHei —— Windows 自带
    "NotoSansCJKsc-Regular.otf",  # fonts-noto-cjk(装了更好看)
    "NotoSansCJK-Regular.ttc",
    "wqy-microhei.ttc",  # 文泉驿微米黑 —— 线上镜像装的就是它(Dockerfile app 阶段,水印也用)
    "simhei.ttf",  # SimHei —— Windows 自带
    "simsun.ttc",  # 宋体
)

# 解析结果只算一次(进程级)。None 表示本机没探到中文字体 —— 那就不改样式,
# 让 ezdxf 自行兜底(拉丁字符照常,中文可能仍是豆腐块,但不比原来更差)。
_cjk_font_resolved = False
_cjk_font_name: str | None = None


def _resolve_cjk_font() -> str | None:
    """挑一个 ezdxf 字体管理器认得的中文字体文件名;探不到返回 None(只算一次)。"""
    global _cjk_font_resolved, _cjk_font_name
    if _cjk_font_resolved:
        return _cjk_font_name
    _cjk_font_resolved = True
    for cand in _CJK_FONT_CANDIDATES:
        try:
            face = fonts.font_manager.get_font_face(cand)
        except Exception:  # noqa: BLE001 —— 字体管理器内部异常一律当「这个候选不可用」
            continue
        # get_font_face 找不到会回退到某个默认字体(filename 与请求的不同);
        # 只认「原样命中」的,才是真装了这个中文字体。
        if face and face.filename and face.filename.lower() == cand.lower():
            _cjk_font_name = cand
            # 同时把 ezdxf 的 fallback 也钉成这个中文字体。关键:裸 Debian 镜像里 ezdxf 的
            # 默认 fallback 是 ArialUni.ttf(镜像里没装),图里任何解析不到的字体(天正 SHX、
            # MTEXT 内联 \f 等)一回退就撞 FontNotFoundError「no fonts available」直接崩 ——
            # 这正是移除 matplotlib(它自带 DejaVu 兜底)后暴露出来的坑。私有属性,ezdxf 版本
            # 已锁 >=1.4,<2;设不了也不致命(下面 _apply_cjk_font 已把样式字体指过去)。
            try:
                fonts.font_manager._fallback_font_name = cand  # noqa: SLF001
            except Exception:  # noqa: BLE001
                logger.debug("设置 ezdxf fallback 字体失败,忽略", exc_info=True)
            logger.debug("CAD 渲染选用中文字体:%s", cand)
            return cand
    logger.warning(
        "未探到可用的中文字体(候选:%s),中文可能渲成方块", ", ".join(_CJK_FONT_CANDIDATES)
    )
    _cjk_font_name = None
    return None


def _apply_cjk_font(doc: ezdxf.document.Drawing) -> None:
    """把文档里每个文字样式的字体指到中文字体,让 SVG 后端能渲出中文(见坑二)。"""
    cjk = _resolve_cjk_font()
    if not cjk:
        return
    for style in doc.styles:
        try:
            style.dxf.font = cjk
        except Exception:  # noqa: BLE001 —— 个别特殊样式(形文件等)设不了就跳过,不影响其余
            continue


def _render_pdf_bytes(doc: ezdxf.document.Drawing) -> bytes:
    """DXF 文档 → 矢量 PDF 字节。ezdxf SVGBackend 出 SVG,再 svglib+reportlab 转 PDF。"""
    _apply_cjk_font(doc)
    backend = svg.SVGBackend()
    Frontend(RenderContext(doc), backend, config=_LIGHT_CONFIG).draw_layout(
        doc.modelspace(), finalize=True
    )
    page = layout.Page(0, 0, layout.Units.mm, margins=layout.Margins.all(_PAGE_MARGIN_MM))
    svg_str = backend.get_string(page)

    # svg2rlg 吃带 XML 声明的 unicode 会报错,喂 BytesIO(utf-8) 即可,免落临时文件。
    drawing = svg2rlg(io.BytesIO(svg_str.encode("utf-8")))
    if drawing is None:
        # 自产 SVG 正常不会解析失败;真失败了当损坏处理,让上层翻成 FILE_CORRUPT。
        raise ezdxf.DXFError("SVG→PDF 转换失败:svglib 未能解析渲染出的 SVG")
    return renderPDF.drawToString(drawing)


def to_pdf(path: Path) -> bytes:
    """把一张 DXF 渲染成**矢量 PDF**,返回字节。阻塞函数:调用方负责 to_thread 包。

    只渲染模型空间整图(finalize 自动摆正纵横比)。图元以矢量路径写进 PDF,放大不糊。
    文件损坏/打不开时不吞异常,让 ezdxf 抛给上层翻成 FILE_CORRUPT。
    """
    doc = ezdxf.readfile(str(path))
    return _render_pdf_bytes(doc)


def to_png(path: Path) -> bytes:
    """把一张 DXF 渲染成整图 PNG,返回字节。阻塞函数:调用方负责 to_thread 包。

    复用 ``to_pdf`` 的快链再用 pypdfium2 栅格化首页 —— 不另起一套渲染,PDF/PNG 行为一致。
    文件损坏/打不开时不吞异常,让 ezdxf 抛给工具层翻成 FILE_CORRUPT。
    """
    pdf_bytes = to_pdf(path)
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        bitmap = doc[0].render(scale=_PDF_RENDER_SCALE)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="png")
        return buf.getvalue()
    finally:
        doc.close()


def pdf_to_png(path: Path, *, page_index: int = 0, scale: float = _PDF_RENDER_SCALE) -> bytes:
    """把一张 PDF 图纸的某一页栅格化成 PNG,返回字节。阻塞函数:调用方负责 to_thread 包。

    默认出首页、预览倍率(``_PDF_RENDER_SCALE``):render_preview 按默认调,行为不变。
    「文字选不中」的读字兜底(vision.read_drawing_text)会传更高的 ``scale``
    (settings.cad_ocr_render_scale)把小字放清;``page_index`` 预留给多页(当前只用首页)。
    文件损坏/加密/页码越界时都不吞异常,让 pypdfium2 抛给工具层翻成 FILE_CORRUPT。
    """
    doc = pdfium.PdfDocument(str(path))
    try:
        page = doc[page_index]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="png")
        return buf.getvalue()
    finally:
        doc.close()


__all__ = ["pdf_to_png", "to_pdf", "to_png"]
