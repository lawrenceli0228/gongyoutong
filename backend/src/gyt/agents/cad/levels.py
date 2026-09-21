"""从图上**已经写着的标高**推算层高 —— 纯函数,不碰盘、不联网、不调模型。

===========================================================================
为什么这一步必须在代码里做,而不是让模型自己减
---------------------------------------------------------------------------
    `prompt.md` 的尺寸红线是「**一个数都不许自己算、自己估**」,而它是对的:
    工地照着错尺寸干活是要出事的。可 2026-09-21 的线上体验测试撞出了这条红线的
    副作用 —— 用户问「幼儿园立面的层高」,图上明明写着

        ±0.000、3.300、6.600、9.900、12.700、14.200、20.300

    而工具只把这些标高原样报回去,模型照红线拒答:「层高这个数图上没写,
    我不能拿标高自己减 —— 算错了是要出事的。」对着原图核过:层高就是 3.300,
    而且左侧尺寸链上**白纸黑字写着 3300**。工友要的答案就在数据里,他没拿到。

    出路不是松红线,是**把这步减法挪进代码**:红线原话是「数值一律照抄工具 data」,
    所以让 data 里本来就有这个数。代码做的减法是确定性的、可测的、可复核的;
    模型做的减法是不可测的。两者的差别不在算术,在**能不能写一条用例钉住它**。

层高 ≠ 相邻标高差,这是本模块最容易写错的地方
---------------------------------------------------------------------------
    把标高排序后逐个相减,拿到的是一串**高度差**,里面混着完全不同的东西。
    上面那张幼儿园立面,排完是

        -0.450 -0.300 ±0.000 3.300 6.600 9.900 12.700 14.200 20.300
    差      0.150  0.300  3.300 3.300 3.300  2.800  1.500  6.100

    0.150 / 0.300 是室内外高差与台阶,2.800 / 1.500 / 6.100 是屋面、女儿墙、塔楼尖顶。
    **只有那三个连着出现的 3.300 是层高。** 判据是「落在合理层高区间内、
    且在相邻档位上**连续**出现够次数的那个差值」——「连续」是关键,而且它比
    「重复几次」严得多:一张图上常有两套标高(主楼一套、裙房一套),混排之后
    跨系统能凑出物理上并不存在的差值,那些凑出来的天然是孤立的(详见 _contiguous_runs)。

    🔴 **凑不出就返回 None,绝不挑一个最像的报上去。** 只有一层的房子、每层都不等高的
    房子、标高没认全的图,都会走到这里 —— 那时候如实说「图上没有直接标层高」才是对的,
    这正是红线要守的东西。本模块存在的意义是「数据够的时候别再拒答」,
    不是「无论如何都给个数」。

⚠️ 这里推出来的数**永远带着出处**(`steps` 里每一档的 `from` / `to`),工具层必须把它一起报给
   用户,并说明是**由标高相减得出、不是图上直接标的层高**。PDF 走视觉认字那条路时,
   标高本身就可能认错,那句「以原图为准」更不能省(见 tools._read_pdf_text_by_vision)。
===========================================================================
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

from gyt.config import get_settings

# 标高在中文图纸上的写法:可带正负号或 `±`,小数点后**三位**(米制标高的行业惯例,
# `3.300` / `±0.000` / `-0.450`)。三位这个约束是刻意的 —— 它把标高和尺寸链上的
# 整数毫米(`3300`、`1200`)、比例(`1:100`)、轴号分得干干净净,不用靠上下文猜。
_ELEVATION_RE: Final = re.compile(r"[±+\-]?\d+\.\d{3}(?![\d.])")

# ⚠️ 层高的合理区间与「至少重复几次」这三个阈值在 `config.py`
#    (`cad_floor_height_min_m` / `_max_m` / `_min_repeats`)—— 本仓常量唯一入口在那儿,
#    这里只取不定义。落在区间外的差值照样会进 `steps` 给人看,只是不参与「标准层高」的评选。

# 标高相减后按几位小数归整。图上标高本来就是三位小数,浮点减法会留下
# 3.3000000000000007 这种尾巴,不归整的话 Counter 一个都合不到一起。
_ROUND_DIGITS: Final = 3


def parse_elevations(texts: Iterable[str]) -> list[float]:
    """从一堆图上文字里把标高捞出来,去重后从低到高排序。

    `±0.000` 当 0 处理。同一个标高在图上常出现多次(左右两侧各标一遍),去重。
    认不出任何标高就返回空列表 —— 调用方据此走「图上没写」那条路。
    """
    found: set[float] = set()
    for text in texts:
        for raw in _ELEVATION_RE.findall(str(text)):
            cleaned = raw.replace("±", "").replace("+", "")
            try:
                found.add(round(float(cleaned), _ROUND_DIGITS))
            except ValueError:  # pragma: no cover —— 正则已经保证形状,这里只是兜底
                continue
    return sorted(found)


def derive_floor_heights(texts: Iterable[str]) -> dict[str, Any] | None:
    """由图上标高推算层高。推不出(或数据不够)返回 None —— **绝不挑一个最像的**。

    返回:
        {
          "elevations": [-0.45, 0.0, 3.3, ...],        # 认出来的标高,低→高
          "steps": [{"from": 0.0, "to": 3.3, "height": 3.3}, ...],   # 相邻两档的高差
          "typical": 3.3,                               # 标准层高(重复最多的那个)
          "typical_count": 3,                           # 这段楼梯连着几层
          "ladder": [2, 3, 4],                          # 楼梯那几档在 steps 里的下标
          "source": "derived_from_elevations",          # 出处标记,工具层据此措辞
        }

    判据见模块头与 config 那三个阈值:落在合理层高区间内、且重复够次数的差值才算标准层高;
    并列时取**矮的那个**(层高在安全计算里往小取更保守,而且并列本身就该让人自己看
    `steps` 复核)。
    """
    settings = get_settings()
    elevations = parse_elevations(texts)
    if len(elevations) < 2:
        return None

    steps = [
        {"from": low, "to": high, "height": round(high - low, _ROUND_DIGITS)}
        for low, high in zip(elevations, elevations[1:], strict=False)
    ]

    runs = _contiguous_runs(
        steps,
        low=settings.cad_floor_height_min_m,
        high=settings.cad_floor_height_max_m,
    )
    if not runs:
        return None

    最长 = max(len(r) for r in runs)
    if 最长 < settings.cad_floor_height_min_repeats:
        return None

    并列 = {steps[r[0]]["height"] for r in runs if len(r) == 最长}
    if len(并列) > 1:
        # 两段一样长、层高却不同(常见于一栋楼两个体量各自成系统)。这时「标准层高」
        # 这个说法本身就不成立,报哪个都是替人做判断 —— 如实回 None,让他自己看标高。
        return None

    那一段 = next(r for r in runs if len(r) == 最长)
    return {
        "elevations": elevations,
        "steps": steps,
        "typical": steps[那一段[0]]["height"],
        "typical_count": 最长,
        "ladder": 那一段,  # 楼梯那几档在 steps 里的下标,describe 与调用方复核都用它
        "source": "derived_from_elevations",
    }


def _contiguous_runs(steps: list[dict[str, float]], *, low: float, high: float) -> list[list[int]]:
    """把「高度相同、且在 steps 里**位置相连**」的档位切成一段段,返回每段的下标。

    🔴 「相连」是这个模块的命根子,别弱化成「出现过几次」(2026-09-21 实测教训)。
    那张幼儿园立面上其实有**两套标高**:左侧主楼 ±0.000/3.300/6.600/9.900…,
    右侧裙房 ±0.000/3.300/7.200/10.000。混排成一串之后,

        …6.600 7.200 9.900 10.000 12.700…
    差        0.600 2.700  0.100  2.700

    `7.200→9.900` 和 `10.000→12.700` 这两个 2.700 是**跨着两栋楼凑出来的**,
    物理上并不存在这么一层。可它们各出现一次、加起来「重复了两次」,
    按次数判定就会把真正的 3.300 挤掉,答出 2.700 —— 一个错的层高,
    而且看着像模像样。楼层是一层层摞上去的,标准层高必然**连着**出现;
    跨系统凑出来的差值天然是孤立的。这就是用连续性而不是用次数的理由。
    """
    runs: list[list[int]] = []
    for i, step in enumerate(steps):
        合格 = low <= step["height"] <= high
        接得上 = bool(runs) and runs[-1] and runs[-1][-1] == i - 1
        同高 = 接得上 and steps[runs[-1][0]]["height"] == step["height"]
        if not 合格:
            continue
        if 同高:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def describe_floor_heights(levels: dict[str, Any]) -> str:
    """把推算结果说成工地上听得懂的一句话(工具层拼进 user_msg)。

    🔴 这句话里**必须**同时有三样:数、出处(从哪档标高数到哪档)、以及「这不是图上直接
    标的层高」。少任何一样,工友都没法判断该不该信它 —— 而这正是当初宁可拒答的理由。

    ⚠️ 阶梯只描述**连续的那一段**。同一个层高可能出现在楼里不相邻的两处(主楼 3.300、
    另一头裙房也 3.300),把它们用「→」串成一条会画出一条并不存在的楼梯 ——
    读的人会以为中间那几档也是这个层高。所以取最长的一段连续的报,其余进「其余几档」。
    """
    typical = levels["typical"]
    steps: list[dict[str, float]] = levels["steps"]
    楼梯: list[int] = levels["ladder"]

    阶梯 = "→".join([_fmt(steps[i]["from"]) for i in 楼梯] + [_fmt(steps[楼梯[-1]]["to"])])
    其余 = [
        f"{_fmt(s['from'])}→{_fmt(s['to'])} 差 {_fmt(s['height'])}"
        for i, s in enumerate(steps)
        if i not in set(楼梯)
    ]
    尾巴 = (
        f";其余几档不等高({'、'.join(其余)}),多半是室内外高差 / 屋面 / 塔楼 / 另一个体量。"
        if 其余
        else "。"
    )

    return (
        f"按图上标高推算,标准层高 {_fmt(typical)} 米 —— {阶梯} 连着 {levels['typical_count']} 层"
        f"都是这个数{尾巴}"
        "⚠️ 这个数是**由图上标高相减得出的,不是图纸上直接标的「层高」**,关键数字以原图为准。"
    )


def _fmt(value: float) -> str:
    """标高/高差一律三位小数(图纸惯例),0 写成 `±0.000`。

    不做「去掉末尾零」那种美化:图上写的就是 `3.300`,报成 `3.3` 等于把工友看到的字改了,
    他对图时会多愣一下。而浮点减法的尾巴(3.3000000000000007)在 :.3f 这里天然就截掉了。
    """
    return "±0.000" if value == 0 else f"{value:.3f}"


__all__ = ["derive_floor_heights", "describe_floor_heights", "parse_elevations"]
