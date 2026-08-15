"""凭证图水印渲染(attendance/watermark.py)的单元测试。

重点不是「画得好不好看」,而是四个会静默出错的地方(W7 §3.4 / §1.7):
  · 缺字检测 —— 判据必须是 cmap,且与 Pillow 消费**同一个** (path, index);
  · 字体探测 —— 全灭必须硬失败,绝不退回 ASCII 位图字体画方块;
  · 解压炸弹闸 —— 解码前就拒,异常带类型;
  · 先缩后画 —— 输出尺寸、EXIF 方向、重编码体积。

本文件在 macOS(开发机)与 Debian(CI 装了 fonts-wqy-microhei)都要能真跑:
缺字用例不钉死某个字 —— 对 resolve_font() 选中的字体**现场扫**一个 cmap 里
确实没有的码点(钉死 WQY 的那条单独写,macOS 上 skip 是预期,见其注释)。
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path

import pytest
from PIL import Image

from gyt.attendance import watermark
from gyt.attendance.watermark import (
    FontUnavailableError,
    MissingGlyphsError,
    WatermarkError,
    render_attendance_photo,
    resolve_font,
)
from gyt.config import get_settings

# 典型的一组水印行:姓名 / 地盤 / 时间 / 凭证编号 / 定位说明(内容由调用方拼,
# 这里只要覆盖「中文 + ASCII + 全角标点」三类字符)。
LINES = [
    "张三",
    "地盤:测试工地",
    "2026-08-15 08:30:00",
    "GYT-A-20260815-083000-ab12",
    "定位:未提供",
]

_WQY_PATH = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"


def _jpeg(width: int, height: int, *, orientation: int | None = None) -> bytes:
    """造一张纯色 JPEG。orientation 写进 EXIF 的 Orientation(0x0112)。"""
    img = Image.new("RGB", (width, height), (200, 200, 200))
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", **kwargs)
    return buffer.getvalue()


def _absent_chars(count: int) -> list[str]:
    """对本机 resolve_font() 选中的那一面,扫出 count 个 cmap 里确实没有的码点。

    先扫 Plane 15 私用区(U+F0000 起,几乎没有字体会映射),再兜底 BMP 私用区。
    不钉死具体码点:PingFang / WQY / Heiti 的覆盖各不相同,钉死等于只在某台机器上测。
    """
    cmap = watermark._font_cmap(resolve_font())
    found = [
        chr(cp)
        for cp in itertools.chain(range(0xF0000, 0xF0200), range(0xE000, 0xF900))
        if cp not in cmap
    ]
    assert len(found) >= count, "这台机器的字体连两个私用区都覆盖满了?换个区间再扫"
    return found[:count]


# --- 缺字检测(§1.7:唯一可靠判据是 cmap) -----------------------------------


def test_缺字_对本机字体真实缺的码点点名报错():
    missing_char = _absent_chars(1)[0]

    with pytest.raises(MissingGlyphsError) as exc_info:
        render_attendance_photo(_jpeg(640, 480), [f"张三{missing_char}"])

    assert missing_char in exc_info.value.chars
    assert "张" not in exc_info.value.chars, "有字的不许被误报"


def test_缺字_去重且保序():
    first, second = _absent_chars(2)

    with pytest.raises(MissingGlyphsError) as exc_info:
        # 同一个缺字出现两次,报错里只出现一次,且按首次出现的顺序排
        render_attendance_photo(_jpeg(640, 480), [f"{first}{second}", f"再来{first}"])

    assert exc_info.value.chars == (first, second)


@pytest.mark.skipif(
    not Path(_WQY_PATH).is_file(),
    reason="本机没有 WQY —— macOS 上 skip 是预期(§3.4:PingFang 本身有「𠮶」,"
    "在它身上测不出这个缺字);CI 装了 fonts-wqy-microhei 就会真跑",
)
def test_钉死WQY_生僻字必须失败(monkeypatch: pytest.MonkeyPatch):
    """「传 𠮶 必须失败」不可移植,必须钉死字体(§3.4)—— 这条测的是线上真环境。"""
    monkeypatch.setenv("GYT_ATTENDANCE_WATERMARK_FONT", _WQY_PATH)
    get_settings.cache_clear()

    with pytest.raises(MissingGlyphsError) as exc_info:
        render_attendance_photo(_jpeg(640, 480), ["𠮶"])

    assert "𠮶" in exc_info.value.chars


# --- 字体解析 ----------------------------------------------------------------


def test_候选全灭时硬失败(monkeypatch: pytest.MonkeyPatch):
    # conftest 已把 GYT_* 清干净(attendance_watermark_font 为默认空),再掐掉候选表
    monkeypatch.setattr(watermark, "_FONT_CANDIDATES", ())

    with pytest.raises(FontUnavailableError):
        resolve_font()


def test_显式指定的字体不存在时硬失败_不许退回候选表(monkeypatch: pytest.MonkeyPatch):
    """显式配置错了要立刻炸 —— 静默退回候选表会把配置错误藏到没人记得的那天。"""
    monkeypatch.setenv("GYT_ATTENDANCE_WATERMARK_FONT", "/不存在/的字体.ttf")
    get_settings.cache_clear()

    with pytest.raises(FontUnavailableError):
        resolve_font()


def test_显式指定的字体存在时只认它(monkeypatch: pytest.MonkeyPatch):
    # 用候选表里靠后的一个真实字体当显式配置,断言探测顺序被完全跳过
    later_candidate = next(
        (path for path, _ in watermark._FONT_CANDIDATES[1:] if Path(path).is_file()), None
    )
    if later_candidate is None:  # pragma: no cover —— 只有一份字体的机器,测不了这条
        pytest.skip("本机凑不出第二份候选字体")
    monkeypatch.setenv("GYT_ATTENDANCE_WATERMARK_FONT", later_candidate)
    get_settings.cache_clear()

    assert resolve_font() == watermark.FontChoice(path=later_candidate, index=0)


def test_缺字检测与Pillow加载消费同一个字体面(monkeypatch: pytest.MonkeyPatch):
    """FontChoice 单源(§3.4 ④):STHeiti 两面 cmap 差一个码点,查错面就在那几个字上错。

    结构性断言:分别在 Pillow 加载与 cmap 查询两处装探针,渲染一张图,
    两处拿到的 (path, index) 必须一字不差。
    """
    seen: dict[str, tuple[str, int]] = {}
    real_truetype = watermark.ImageFont.truetype
    real_cmap = watermark._font_cmap

    def spy_truetype(font=None, size=10, index=0, **kwargs):
        seen["pil"] = (str(font), index)
        return real_truetype(font, size, index=index, **kwargs)

    def spy_cmap(choice: watermark.FontChoice):
        seen["cmap"] = (choice.path, choice.index)
        return real_cmap(choice)

    monkeypatch.setattr(watermark.ImageFont, "truetype", spy_truetype)
    monkeypatch.setattr(watermark, "_font_cmap", spy_cmap)

    render_attendance_photo(_jpeg(640, 480), ["张三打卡"])

    assert seen.keys() == {"pil", "cmap"}, "两处都必须真的被走到"
    assert seen["pil"] == seen["cmap"]


# --- 正常渲染 ----------------------------------------------------------------


def test_正常渲染_缩到上限内_合法JPEG_横条真画上去了():
    photo = _jpeg(4000, 3000)

    out = render_attendance_photo(photo, LINES)

    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) <= get_settings().attendance_max_edge_px
    # 重编码后的字节数要明显小于解码位图(4000×3000×3 ≈ 36MB)—— 这就是内存闸的意义
    assert len(out) < 4000 * 3000 * 3 // 10
    # 横条与原图的取样点:右上角保持原色(纯色 200 灰,JPEG 有损给几个点容差),
    # 右下角在横条里、又避开左对齐的文字 —— 必须暗得多。
    rgb = img.convert("RGB")
    top = rgb.getpixel((img.width - 4, 2))
    bottom = rgb.getpixel((img.width - 4, img.height - 3))
    assert sum(top) > 540, f"顶部该保持原色,实际 {top}"
    assert sum(bottom) < sum(top) - 200, f"底部该被深色横条压暗,实际 {bottom}"


def test_不到上限的图不放大():
    out = render_attendance_photo(_jpeg(640, 480), LINES)

    img = Image.open(io.BytesIO(out))
    assert img.size == (640, 480), "thumbnail 只缩不放,小图保持原尺寸"


def test_EXIF方向6_输出宽高对调且方向标记清掉():
    """手机竖拍存的是横图 + Orientation=6。输出必须是像素已转正的竖图,
    且不再带 Orientation —— 否则看图软件会二次旋转,凭证横过来。"""
    out = render_attendance_photo(_jpeg(400, 200, orientation=6), ["工地"])

    img = Image.open(io.BytesIO(out))
    assert (img.width, img.height) == (200, 400)
    assert img.getexif().get(0x0112) in (None, 1)


# --- 解压炸弹闸与坏输入 --------------------------------------------------------


def test_解压炸弹_声称的像素量超闸直接拒(monkeypatch: pytest.MonkeyPatch):
    """闸值压到配置下限(1e6),喂一张 120 万像素的正常图触发 —— 断言在**解码前**
    就抛(用不着真造 30000×30000 的怪物)。异常必须是 WatermarkError 本体,
    不是它的某个子类(FontUnavailable / MissingGlyphs 也继承它,别混过)。"""
    monkeypatch.setenv("GYT_ATTENDANCE_DECODE_MAX_PIXELS", "1000000")
    get_settings.cache_clear()

    with pytest.raises(WatermarkError) as exc_info:
        render_attendance_photo(_jpeg(2000, 600), LINES)

    assert exc_info.type is WatermarkError


def test_不是图片一律拒():
    with pytest.raises(WatermarkError) as exc_info:
        render_attendance_photo("这串字节绝对不是JPEG".encode(), LINES)

    assert exc_info.type is WatermarkError


def test_截断的JPEG也拒():
    photo = _jpeg(640, 480)

    with pytest.raises(WatermarkError) as exc_info:
        # 只留头部:Image.open 能认出尺寸,真解码(thumbnail)时才发现数据断了
        render_attendance_photo(photo[: len(photo) // 4], LINES)

    assert exc_info.type is WatermarkError
