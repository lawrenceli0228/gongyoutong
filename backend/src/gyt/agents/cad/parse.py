"""DXF 解析:ezdxf 文档 → 一个纯 JSON 可序列化的中间对象(落地文档第 4 节的 schema)。

===========================================================================
职责边界(为什么这一层只解析、不落盘、不异步)
---------------------------------------------------------------------------
    本文件全部是**同步阻塞**函数:ezdxf.readfile / bbox 计算都是阻塞 IO/CPU。
    调用方(index.ensure_index)负责用 ``asyncio.to_thread`` 把它丢进线程池 ——
    这里绝不 import asyncio,也绝不落盘,保证「解析」这件事可单独测、可打桩。

    产物里**不含** source_artifact_id:那是「这份解析对应哪张图」的索引级信息,
    由 index 层在写盘前补上(parse 只认得一个 Path,不认得 artifact_id)。

编码探测(落地文档第 5 节):用 ``doc.output_encoding`` 而不是 ``doc.encoding``。
    · R2007(AC1021)及以后:内容一律 UTF-8 → output_encoding == "utf-8";
    · R2004(AC1018)及更老 + $DWGCODEPAGE=ANSI_936:GBK → output_encoding == "gbk"。
    ``doc.encoding`` 对新版文件会误报成 cp1252(它只看 $DWGCODEPAGE 头,不看版本),
    而 output_encoding 已经把版本规则算进去了,正是我们要「认出 GBK」的那个值。

损坏文件(FILE_CORRUPT):本层**故意让 ezdxf 的异常向上抛**,不在这里吞。
    截断/结构损坏的 DXF 会让 ezdxf.readfile 抛 DXFStructureError/IOError,
    工具层(tools.py)接住它翻成 FILE_CORRUPT 的中文信封。
    ⚠️ 这里不挂 ezdxf.recover 兜底 —— recover 会把半截文件「救」回来一部分,
    那样损坏文件就检不出来了。recover 是「真图声明编码与实际不符」时的升级手段
    (落地文档第 5 节第 2 步),留到真踩到再加,记 TODO。
===========================================================================
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox

# $INSUNITS 码 → 人话单位标签(落地文档风险 #6:尺寸没单位 = 没意义)。
# 只列常见的;认不出的落到 f"单位码{code}",不假装知道。
_UNITS_LABEL: dict[int, str] = {
    0: "无单位",
    1: "in",
    2: "ft",
    3: "mi",
    4: "mm",
    5: "cm",
    6: "m",
    7: "km",
    8: "µin",
    9: "mil",
    10: "yd",
    11: "Å",
    12: "nm",
    13: "µm",
    14: "dm",
    15: "dam",
    16: "hm",
    17: "Gm",
    18: "au",
    19: "ly",
    20: "pc",
}

# DIMENSION 的 dimtype 低 3 位 → 人话类别。够 MVP 用,认不出落 "other"。
_DIM_KIND: dict[int, str] = {
    0: "linear",
    1: "aligned",
    2: "angular",
    3: "diameter",
    4: "radius",
    5: "angular3p",
    6: "ordinate",
}

# dim.dxf.text 取这些值表示「用实测值,没手改」——此时展示文字 = 格式化后的实测。
_AUTO_DIM_TEXT = frozenset({"<>", "", None})

# 认不出所在图层时的默认层名(AutoCAD 的 0 层),不硬编成别的。
_DEFAULT_LAYER = "0"


def _layer_of(entity: Any) -> str:
    """读图元所在图层,对天正私有构件(``TCH_*``)这类代理实体做防御式兜底。

    普通图元有标准 ``layer`` 属性,直接取。天正的墙/柱/门窗是私有构件,ezdxf 认不得,
    当**未知代理实体**(``DXFTagStorage``)加载 —— 它不暴露 ``.dxf.layer``,而且连
    ``.dxf.get("layer", "0")`` 都会抛 ``DXFAttributeError``(``dxf`` 命名空间会校验属性名,
    未知属性直接抛、不给默认值)。这类实体的图层(组码 8)藏在原始标签的 ``AcDbEntity``
    子类里,退回去扫一遍捞出来;实在没有(有的代理连图层都不带)就落到默认层 ``0``。

    这是「三处扫实体都假设每个实体都有 ``.dxf.layer``」那个真 bug 的收口:天正图不再让
    ``_scan_modelspace`` 崩掉,进而不再被工具层误判成「文件传坏了」(FILE_CORRUPT)。
    """
    dxf = entity.dxf
    if dxf.hasattr("layer"):
        return str(dxf.layer)
    xtags = getattr(entity, "xtags", None)  # 只有 DXFTagStorage(代理实体)才有
    if xtags is not None:
        for subclass in xtags.subclasses:
            for tag in subclass:
                if tag.code == 8:  # 组码 8 在图元里恒为图层名
                    return str(tag.value)
    return _DEFAULT_LAYER


def _units_label(insunits: int) -> str:
    return _UNITS_LABEL.get(insunits, f"单位码{insunits}")


def _fmt_measurement(value: float) -> str:
    """把实测值格式化成标注上会显示的样子:整数就不带小数,否则留两位。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def _scan_modelspace(msp: Any) -> tuple[Counter, dict[str, Counter], Counter]:
    """扫一遍模型空间,一次算出三样东西,别扫三遍:

    返回 (entities_by_kind, per_layer_kinds, insert_by_block)
      · entities_by_kind:{"LINE": 320, ...} 全图按图元类型计数
      · per_layer_kinds:{图层名: Counter(该层各类型计数)}
      · insert_by_block:{被引用的块名: INSERT 次数}
    """
    by_kind: Counter = Counter()
    per_layer: dict[str, Counter] = {}
    insert_by_block: Counter = Counter()
    for entity in msp:
        kind = entity.dxftype()
        layer = _layer_of(entity)  # 天正代理实体也兜得住,不再 KeyError/DXFAttributeError
        by_kind[kind] += 1
        per_layer.setdefault(layer, Counter())[kind] += 1
        if kind == "INSERT":
            insert_by_block[str(entity.dxf.name)] += 1
    return by_kind, per_layer, insert_by_block


def _layers(doc: Any, per_layer: dict[str, Counter]) -> list[dict[str, Any]]:
    """图层清单:图层表里每一层都列出来(即使没图元),配上该层的图元数与类型分布。"""
    out: list[dict[str, Any]] = []
    for layer in doc.layers:
        name = str(layer.dxf.name)
        kinds = per_layer.get(name, Counter())
        out.append(
            {
                "name": name,
                "entity_count": int(sum(kinds.values())),
                "color": int(layer.dxf.color),
                "kinds": {k: int(v) for k, v in sorted(kinds.items())},
            }
        )
    return out


def _blocks(doc: Any, insert_by_block: Counter) -> list[dict[str, Any]]:
    """用户定义的块清单 + 各自被 INSERT 了几次。

    过滤两类系统块,它们都不是用户画的构件,列出来只会污染构件清单:
      · '*' 开头 —— 模型/图纸空间(*Model_Space)、标注生成的匿名块(*D0…);
      · '_' 开头 —— AutoCAD 内部块,典型是标注箭头(_CLOSEDFILLED / _OPEN…)。
    这是 AutoCAD 的命名惯例:施工图里的构件块名(柱-KZ1、KZ1、Q-1…)不会以下划线打头,
    所以按前缀过滤对本域是安全的(真踩到下划线开头的用户块再说,记 TODO)。
    """
    out: list[dict[str, Any]] = []
    for block in doc.blocks:
        name = str(block.name)
        if name.startswith(("*", "_")):
            continue
        out.append({"name": name, "insert_count": int(insert_by_block.get(name, 0))})
    return out


def _dimensions(msp: Any) -> list[dict[str, Any]]:
    """图纸上已有的 DIMENSION 标注读数(query_dimension 的唯一数据源)。

    text 是标注上写的字、measurement 是几何实测值 —— 两者可能不同(标注可手改),
    所以两个都留下。text 是 "<>"/"" 表示没手改,此时用格式化后的实测值当展示文字。
    """
    out: list[dict[str, Any]] = []
    for dim in msp.query("DIMENSION"):
        try:
            measurement: float | None = float(dim.get_measurement())
        except (TypeError, ValueError):
            # 角度/坐标标注的 get_measurement 可能不是单个 float,认不了就留空。
            measurement = None
        raw_text = dim.dxf.get("text", "<>")
        if raw_text in _AUTO_DIM_TEXT:
            text = _fmt_measurement(measurement) if measurement is not None else ""
        else:
            text = str(raw_text)
        kind = _DIM_KIND.get(int(getattr(dim, "dimtype", 0)) & 7, "other")
        out.append(
            {
                "text": text,
                "measurement": round(measurement, 4) if measurement is not None else None,
                "layer": _layer_of(dim),
                "kind": kind,
            }
        )
    return out


def _texts(msp: Any, limit: int = 200) -> list[dict[str, Any]]:
    """图上的 TEXT / MTEXT 文字(read_view_params 的数据源之一)。

    标高「±0.000」「3.600」、层高说明、房间名这些**常是文字而非 DIMENSION**,单靠 _dimensions
    读不到。这里只如实抽图上写着的字,不解释、不换算 —— 与标注一个性质(read 侧照抄,不自算)。
    限量(默认 200 条)防真实工程图上千条文字把索引与提示词压垮。
    """
    out: list[dict[str, Any]] = []
    for entity in msp.query("TEXT MTEXT"):
        if entity.dxftype() == "MTEXT":
            text = entity.plain_text().strip()  # 去掉 MTEXT 的排版控制码,留纯文字
        else:
            text = str(entity.dxf.text).strip()
        if text:
            out.append({"text": text, "layer": _layer_of(entity)})
        if len(out) >= limit:
            break
    return out


def _bounds(msp: Any) -> dict[str, list[float]] | None:
    """整图外接框。空图(无可测图元)返回 None,别硬编个 [0,0]。"""
    extents = bbox.extents(msp, fast=True)
    if not extents.has_data:
        return None
    lo, hi = extents.extmin, extents.extmax
    return {
        "min": [round(lo.x, 3), round(lo.y, 3)],
        "max": [round(hi.x, 3), round(hi.y, 3)],
    }


def parse_dxf(path: Path) -> dict[str, Any]:
    """解析一张 DXF,产出落地文档第 4 节的中间对象(不含 source_artifact_id)。

    阻塞函数:调用方负责 ``asyncio.to_thread`` 包。
    文件损坏/打不开时**不吞异常**,让 ezdxf 的异常向上抛给工具层翻成 FILE_CORRUPT。
    """
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    by_kind, per_layer, insert_by_block = _scan_modelspace(msp)
    insunits = int(doc.header.get("$INSUNITS", 0))

    # 天正(TArch)私有构件按 TCH_* 类型出现在 by_kind 里。检出它们,让工具层能对用户说
    # 「这是天正图、先导出 T3」,而不是把读不到几何当成「文件坏了」。detect 只认 TCH_ 前缀
    # (天正专有),不把泛化的 ACAD_PROXY_ENTITY 也算进来 —— 那可能来自别家插件,不好乱指。
    tianzheng_kinds = {k: int(v) for k, v in sorted(by_kind.items()) if k.startswith("TCH_")}

    return {
        "format": "dxf",  # 索引格式判别:工具层按它在 dxf/pdf 两条线间分流
        "encoding": str(doc.output_encoding),
        "dxf_version": str(doc.dxfversion),
        "insunits": insunits,
        "units_label": _units_label(insunits),
        "layers": _layers(doc, per_layer),
        "entities_by_kind": {k: int(v) for k, v in sorted(by_kind.items())},
        "blocks": _blocks(doc, insert_by_block),
        "dimensions": _dimensions(msp),
        "annotations": _texts(msp),
        "bounds": _bounds(msp),
        "tianzheng": {
            "detected": bool(tianzheng_kinds),
            "component_kinds": tianzheng_kinds,
        },
    }


__all__ = ["parse_dxf"]
