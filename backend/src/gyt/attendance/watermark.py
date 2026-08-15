"""凭证图水印渲染(W7 T4,B 泳道)。**接口冻结,实现已落地。**

接口为什么先于实现存在(冻结理由,保留):checkin_api(A 泳道)在模块顶层
import 本模块并在 handler 里调用;它的测试用
``mock.patch("gyt.attendance.watermark.render_attendance_photo")`` 替换真渲染 ——
patch 的前提是这条导入路径**真实存在**。A / B 两条泳道并行开工,靠的就是这份
先冻结的签名。要动签名或异常类型,先回 checkin_api 那头对齐。

实现四件事(验收面在 docs/W7_打卡Agent_技术方案.md §3.4,关键实测 2026-08-15 复核):

  ① **解码前闸**(解压炸弹):``Image.open`` 只解析文件头,拿到声称的尺寸就
    过 ``attendance_decode_max_pixels``,超了抛类型化的 WatermarkError ——
    像素永远不会被解码。与 Pillow 自带的 ``Image.MAX_IMAGE_PIXELS`` 的关系
    见 render 内的注释:我们的闸在**解码**之前、判定确定、异常有类型;
    PIL 那道在超标 1 倍内只发 warning(没人看),2 倍才抛它自己的错。

  ② **先缩后画**:JPEG 先 ``draft()`` 在 DCT 域降采样(解码内存直接砍到几分之一,
    这是 1.9GB VPS 不 OOM 的关键),再 ``exif_transpose``(EXIF 方向),
    再 ``thumbnail(LANCZOS)`` 收口 —— 与 safety/tools.py:315 同一收口写法。
    ⚠️ 顺序与 draft 目标的两条实测结论写在 render 内,别凭直觉改。

  ③ **字体候选探测**:显式覆盖走 ``settings.attendance_watermark_font``
    (非空则只认它);否则按 ``_FONT_CANDIDATES`` 探测。全部落空 →
    FontUnavailableError。**绝不 fallback 到 ImageFont.load_default()** ——
    那是 ASCII 位图字体,汉字全画成方块;凭证图上一排方块比「打卡失败」
    更糟,因为工友不会为「图有点怪」报障,假凭证就这么流出去了。

  ④ **缺字检测只查 cmap**:``getmask().getbbox()`` 对 .notdef 方块也返回非空
    像素框,有字和缺字零区分度(方案 §1.7 的实测)。唯一可靠判据是 fontTools
    查 cmap;.ttc 按 **与 Pillow 同一个 face index** 取面(STHeiti 两面 cmap
    差一个码点 37707 vs 37708,查错面就在那几个字上错)—— 两处消费同一个
    ``FontChoice``,第二个 index 在结构上不存在。

本模块只抛类型化异常,不产用户文案 —— 「字体缺字」翻译成给工友看的人话
归 A 泳道的 messages.py(D12:文案集中,散在渲染层的字符串换简繁时必漏)。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Final, NamedTuple

from fontTools.ttLib import TTCollection, TTFont
from PIL import Image, ImageDraw, ImageFont, ImageOps

from gyt.config import get_settings

logger = logging.getLogger(__name__)

# --- 模块级常量(禁止在函数里散落魔法值)-------------------------------------

_FONT_CANDIDATES: Final[tuple[tuple[str, int], ...]] = (
    # Debian(线上镜像与 CI):fonts-wqy-microhei 包,face0 = 文泉驿微米黑。
    # 装在 Dockerfile 的 **app 阶段**(动 base 会让 models 层缓存失效、重下 2.2GB)。
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    # macOS(本机开发)。路径与 face 均为本机实测(2026-08-15,ls + fontTools 逐个探):
    #   PingFang.ttc        36 面,face0 = PingFang HK Regular,cmap 33257 码点(含「𠮶」——
    #                       所以钉死 WQY 的缺字用例在 macOS 上必须 skip,见 §3.4);
    #   STHeiti Light.ttc   2 面,face0 = Heiti TC Light,cmap 37707(方案说的那一面);
    #   Hiragino Sans GB    4 面,face0 = W3,cmap 29026(无「𠮶」)—— 覆盖最小,排最后。
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
)
"""候选字体表:``(路径, face index)``,顺序即优先级。

候选表是**代码携带的平台知识**,故意不进配置(config.py 那边的注释同此口径):
放 .env 里没人会改,反而多一处会漂的拷贝。要显式指定就设
``GYT_ATTENDANCE_WATERMARK_FONT``,那时本表整个失效。
"""

_JPEG_QUALITY: Final[int] = 85
"""重编码质量。85 是「肉眼无感、体积砍半」的常用平衡点;凭证图的用途是
「证明这个人这个时间在这里」,不是取证放大(§3.3 明说别当证据库),不值更高。"""

_FONT_WIDTH_DIV: Final[int] = 24
_MIN_FONT_PX: Final[int] = 14
"""字号 = 缩后宽度 // 24,下限 14px。

敢用固定除数的前提是**先缩图**:thumbnail 之后长边 ≤ attendance_max_edge_px,
宽度落在可预测区间,除出来的字号才稳定(默认 1600 宽 → 66px)。
不缩的话同一除数在 4000px 原图上是蚂蚁、在 800px 截图上糊半张 —— §3.3 理由②。
下限兜的是极小图(配置允许到 320px):320//24=13,再小字就糊成一团。"""

_LINE_SPACING: Final[float] = 1.4
_BAR_ALPHA: Final[int] = 150
_TEXT_FILL: Final[tuple[int, int, int, int]] = (255, 255, 255, 255)
"""横条 alpha 150/255 ≈ 六成不透明:白字压得住任何底色,又不至于整条糊死照片。"""


class WatermarkError(Exception):
    """水印渲染失败的基类。给 handler 一个能整类捕获的锚。"""


class FontUnavailableError(WatermarkError):
    """候选字体一个都找不到。这是部署错误,不是用户错误 —— 该硬失败,不该带病出图。"""


class MissingGlyphsError(WatermarkError):
    """字体画不出某些字(典型:HKSCS 生僻姓名字撞上 WQY)。

    缺哪些字必须带出来 —— 「水印失败」和「你名字里的『𠮶』字打不出来」
    是完全不同的两句话,后者工友自己就能换个写法解决。
    """

    def __init__(self, chars: Sequence[str]) -> None:
        self.chars: tuple[str, ...] = tuple(chars)
        super().__init__(f"字体缺字:{''.join(self.chars)}")


class FontChoice(NamedTuple):
    """一次字体选择的结果:路径 + face 序号。

    **整条链只允许出现一个 index。** Pillow 的 ``truetype(path, index=N)`` 与
    fontTools 的 ``TTCollection(path).fonts[N]`` 必须消费同一个 N ——
    STHeiti.ttc 两面 cmap 差一个码点(37707 vs 37708),查错面就只在那几个字上错,
    平时全绿、上线才炸(W7 上线闸⑤)。让加载与缺字检测共用同一个 FontChoice,
    「第二个 index」这种东西在类型上就不存在。NamedTuple 可哈希,顺带当缓存键。
    """

    path: str
    index: int


def resolve_font() -> FontChoice:
    """选出水印用的字体面。

    优先级:
      1. ``settings.attendance_watermark_font`` 非空 → **只认它**(face 固定 0),
         不存在直接 FontUnavailableError —— 显式配置错了就要立刻炸,
         静默退回候选表会把配置错误藏到没人记得改过配置的那天;
      2. 按 ``_FONT_CANDIDATES`` 依次探测,取第一个存在的。

    全部落空 → FontUnavailableError。**绝不退回 ImageFont.load_default()**,
    理由见模块头注③。本函数不缓存:几次 ``is_file()`` 便宜,而测试要靠
    monkeypatch settings / 候选表来演「全灭」,缓存会把补丁焊死。
    """
    explicit = get_settings().attendance_watermark_font.strip()
    if explicit:
        if Path(explicit).is_file():
            return FontChoice(path=explicit, index=0)
        raise FontUnavailableError(f"配置指定的水印字体不存在:{explicit}")
    for path, index in _FONT_CANDIDATES:
        if Path(path).is_file():
            return FontChoice(path=path, index=index)
    raise FontUnavailableError(
        "候选中文字体一个都不存在。线上镜像该在 Dockerfile 的 app 阶段装 fonts-wqy-microhei;"
        "本机开发可用 GYT_ATTENDANCE_WATERMARK_FONT 指一份中文字体"
    )


@lru_cache(maxsize=8)
def _font_cmap(choice: FontChoice) -> frozenset[int]:
    """这一面字体的 cmap 码点集合(getBestCmap),按 FontChoice 记忆化。

    每次打卡重解析 TTC 是纯浪费:PingFang.ttc 78MB、36 面,lazy 解析一次约
    0.14s(本机实测)—— 缓存后为零。键就是 FontChoice;字体文件是系统只读
    资源,进程存活期内不会变,缓存不存在读旧的问题。maxsize=8:一台机器
    一辈子也用不到第二种字体,8 纯粹是「别无限长」。

    字体文件读坏 / index 越界都归 FontUnavailableError —— 和「文件不存在」
    同一性质:部署错误,该硬失败。
    """
    try:
        if choice.path.lower().endswith(".ttc"):
            with TTCollection(choice.path, lazy=True) as collection:
                faces = collection.fonts
                if choice.index >= len(faces):
                    raise FontUnavailableError(
                        f"字体 {choice.path} 只有 {len(faces)} 面,取不到第 {choice.index} 面"
                    )
                return frozenset(faces[choice.index].getBestCmap())
        with TTFont(choice.path, lazy=True) as font:
            return frozenset(font.getBestCmap())
    except FontUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 —— fontTools 的异常类型很杂,一律当部署坏字体
        raise FontUnavailableError(f"字体解析失败:{choice.path}(face {choice.index})") from exc


def _missing_chars(choice: FontChoice, lines: Sequence[str]) -> tuple[str, ...]:
    """找出这批文字里字体画不出的字。去重、保序(dict 自 3.7 起保插入序)。

    判据只有 cmap 一个 —— ``getmask().getbbox()`` 对 .notdef 方块本身的像素
    也返回非空,零区分度(§1.7 实测,别改回去)。
    """
    cmap = _font_cmap(choice)
    return tuple(dict.fromkeys(c for line in lines for c in line if ord(c) not in cmap))


def _load_scaled(photo: bytes, max_edge: int, decode_max_pixels: int) -> Image.Image:
    """解码并缩到长边 ≤ max_edge。顺序与每一步的理由:

    open(只读头) → 像素闸 → draft(DCT 域降采样) → exif_transpose → thumbnail

    **像素闸在解码前**:``Image.open`` 只解析文件头,``img.size`` 是文件声称的
    尺寸,此刻一个像素都没解。超过 decode_max_pixels 就抛类型化 WatermarkError。
    与 Pillow 自带 ``Image.MAX_IMAGE_PIXELS``(默认约 1.79 亿)的关系:PIL 那道
    也在 open 时查,但超标 1 倍以内只发 DecompressionBombWarning(日志里没人看),
    2 倍(约 3.58 亿)才抛错 —— 我们的闸阈值来自配置、判定确定、异常带类型,
    真正的「几 KB 声称 30 亿像素」会先撞 PIL 的 2 倍线,被下面的 except 统一
    包成 WatermarkError,同样到不了解码。

    **draft 必须最早,且目标必须按原图等比给**(两条都是 2026-08-15 实测):
      · exif_transpose 会触发全量解码,而 draft 在 load 之后是空操作(实测返回
        None、尺寸不变)—— 顺序反了,DCT 降采样整个失效,内存优势归零;
      · draft 的档位(1/2、1/4、1/8)取 ``min(宽//目标宽, 高//目标高)``,
        方形目标 ``(1600,1600)`` 对 4000×3000 算出 min(2,1)=1,**完全不降**;
        按等比目标 ``(1600,1200)`` 才降到 2000×1500。降采样发生在存储方向上,
        transpose 旋转的是已缩小的位图,方向不受影响;最终边界由随后的
        **正方形** thumbnail 收口,与旋转先后无关(实测 Orientation=6 的图
        输出宽高正确对调)。
      · draft 只对 JPEG 生效,其它格式是空操作 —— 无条件调用是安全的。
    """
    try:
        img = Image.open(io.BytesIO(photo))
        claimed_w, claimed_h = img.size
    except Exception as exc:  # noqa: BLE001 —— Pillow 的异常类型很杂,一律当「不是图/坏图」
        raise WatermarkError("图片解不开:不是图片或文件已损坏") from exc

    if claimed_w * claimed_h > decode_max_pixels:
        raise WatermarkError(
            f"图片声称的像素量超过上限:{claimed_w}x{claimed_h} > {decode_max_pixels}"
        )

    long_edge = max(claimed_w, claimed_h)
    if long_edge > max_edge:
        ratio = max_edge / long_edge
        img.draft(
            "RGB",
            (max(1, int(claimed_w * ratio)), max(1, int(claimed_h * ratio))),
        )
    try:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    except Exception as exc:  # noqa: BLE001 —— 真正解码在这一步,截断文件在此暴露
        raise WatermarkError("图片解码失败:文件可能不完整") from exc
    return img


def _draw_lines(img: Image.Image, lines: Sequence[str], choice: FontChoice) -> Image.Image:
    """底部半透明深色横条 + 白字逐行画。返回 RGB 新图,不动入参。

    尺寸全部相对**缩后宽度**成比例(字号、行距、边距)—— 前提与因果见
    ``_FONT_WIDTH_DIV`` 的说明:先缩图,尺寸可预测,比例常数才敢固定。
    """
    base = img.convert("RGBA")
    width, height = base.size
    font_size = max(width // _FONT_WIDTH_DIV, _MIN_FONT_PX)
    # 加载与缺字检测消费同一个 FontChoice(path 与 index 都不许分叉),见类注释。
    font = ImageFont.truetype(choice.path, font_size, index=choice.index)
    line_h = round(font_size * _LINE_SPACING)
    margin = max(font_size // 2, 8)
    bar_top = max(height - (margin * 2 + line_h * len(lines)), 0)

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle(((0, bar_top), (width, height)), fill=(0, 0, 0, _BAR_ALPHA))
    y = bar_top + margin
    for line in lines:
        draw.text((margin, y), line, font=font, fill=_TEXT_FILL)
        y += line_h
    return Image.alpha_composite(base, overlay).convert("RGB")


def render_attendance_photo(photo: bytes, lines: Sequence[str]) -> bytes:
    """把水印画上凭证图,返回重编码后的 JPEG 字节。

    契约(冻结):
      · ``photo``:原始 JPEG 字节(手机原图,大小上限由 handler 在流式阶段把住);
      · ``lines``:要画的文字行(姓名 / 地盤 / 时间 / 凭证编号 / 定位说明)——
        **内容由调用方拼**,本函数只管画。文案不在这里组装,是 D12(文案集中)
        的要求:散在渲染层的字符串换简繁时必漏;
      · 返回:缩图 + 水印 + 重编码后的 JPEG;**原图不落盘、不进产物注册表**;
      · 抛:FontUnavailableError / MissingGlyphsError / WatermarkError(图解不开等)。

    步骤顺序:字体解析与缺字检测放在解码**之前** —— 两者都不碰像素
    (cmap 有缓存,首次约 0.14s),画不出来的字早一毫秒知道就少解一张图;
    部署级错误(FontUnavailableError)也因此不会被一张恰好损坏的照片遮住。
    """
    choice = resolve_font()
    missing = _missing_chars(choice, lines)
    if missing:
        raise MissingGlyphsError(missing)

    settings = get_settings()
    img = _load_scaled(
        photo,
        max_edge=settings.attendance_max_edge_px,
        decode_max_pixels=settings.attendance_decode_max_pixels,
    )
    stamped = _draw_lines(img, lines, choice) if lines else img.convert("RGB")

    buffer = io.BytesIO()
    stamped.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    logger.info(
        "水印已渲染:%dx%d %.1fKB,%d 行,字体 %s[%d]",
        stamped.width,
        stamped.height,
        buffer.tell() / 1024,
        len(lines),
        choice.path,
        choice.index,
    )
    return buffer.getvalue()


__all__ = [
    "FontChoice",
    "FontUnavailableError",
    "MissingGlyphsError",
    "WatermarkError",
    "render_attendance_photo",
    "resolve_font",
]
