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

import codecs
import logging
import math
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox

logger = logging.getLogger(__name__)

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
DXF_INDEX_SCHEMA_VERSION = 3


def _contains_cjk(text: str, minimum: int = 2) -> bool:
    """至少含 ``minimum`` 个中日韩统一表意文字,用于旧 DXF 编码探测。"""
    count = 0
    for char in text:
        if "\u3400" <= char <= "\u9fff":
            count += 1
            if count >= minimum:
                return True
    return False


def _decodes_to_cjk(path: Path, encoding: str) -> bool:
    """流式严格解码;整份文件编码合法且出现中文才算候选。"""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    found = False
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(64 * 1024):
                decoded = decoder.decode(chunk)
                if not found:
                    found = _contains_cjk(decoded)
            tail = decoder.decode(b"", final=True)
            if not found:
                found = _contains_cjk(tail)
    except UnicodeDecodeError:
        return False
    return found


def _legacy_encoding_override(path: Path) -> str | None:
    """识别没有 ``$DWGCODEPAGE`` 的旧 ASCII DXF 中文编码。

    R12 真图常直接以 GBK 写中文,却不声明 codepage。ezdxf 会按 cp1252 打开,
    文件不报错但所有中文房间名都会变成乱码。已声明 codepage 的图完全交给 ezdxf;
    只有未声明且能被某候选编码严格解出中文时才覆盖,避免误伤西文图。
    """
    header = bytearray()
    with path.open("rb") as fh:
        for line in fh:
            header.extend(line)
            if line.strip() == b"ENDSEC":
                break
    if b"$DWGCODEPAGE" in header:
        return None
    for encoding in ("utf-8", "gbk", "gb18030"):
        if _decodes_to_cjk(path, encoding):
            return encoding
    return None


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


def _expanded_entities(
    entities: Iterable[Any], block_chain: frozenset[str] = frozenset()
) -> Iterator[Any]:
    """递归展开 INSERT,同时保留 DIMENSION 本体及其变换后坐标。

    ``msp.query`` 只看顶层,国内施工图却常把整层平面做成块引用。使用
    ``recursive_decompose`` 会把 DIMENSION 炸成线和文字,丢掉实测值;这里仅展开
    INSERT,所以房间文字和尺寸对象两边都保得住。块名链用于挡住非法循环引用。
    """
    for entity in entities:
        if entity.dxftype() != "INSERT":
            yield entity
            continue
        name = str(entity.dxf.get("name", ""))
        if name in block_chain:
            logger.warning("CAD 块出现循环引用,已跳过:%s", name)
            continue
        refs = entity.multi_insert() if getattr(entity, "mcount", 1) > 1 else (entity,)
        for ref in refs:
            # ATTRIB 不属于块定义,virtual_entities() 不会自动带出来;它的位置已是 WCS。
            yield from getattr(ref, "attribs", ())
            try:
                children = ref.virtual_entities()
                yield from _expanded_entities(children, block_chain | {name})
            except (AttributeError, ezdxf.DXFError) as exc:
                logger.warning("CAD 块 %s 无法展开,已跳过:%s", name or "(未命名)", exc)


def _point(entity: Any, name: str) -> list[float] | None:
    value = entity.dxf.get(name)
    if value is None:
        return None
    try:
        return [round(float(value.x), 3), round(float(value.y), 3)]
    except (AttributeError, TypeError, ValueError):
        return None


def _dimension(entity: Any) -> dict[str, Any]:
    kind = _DIM_KIND.get(int(getattr(entity, "dimtype", 0)) & 7, "other")
    start = _point(entity, "defpoint2")
    end = _point(entity, "defpoint3")
    try:
        measurement: float | None = float(entity.get_measurement())
    except (TypeError, ValueError):
        measurement = None
    measurement_source = "entity"
    # ezdxf 对嵌套 INSERT 展开后的 ALIGNED DIMENSION 偶尔返回 0,但该实体自己的
    # 两个定义点仍完整。对齐标注按 DXF 定义就是两点直线距离,可安全恢复读数;
    # 这仍是在读已有 DIMENSION,不是拿普通轴线坐标估一个未标尺寸。
    if kind == "aligned" and (measurement is None or math.isclose(measurement, 0.0)):
        if start is not None and end is not None:
            defined_length = math.hypot(end[0] - start[0], end[1] - start[1])
            if defined_length > 0:
                measurement = defined_length
                measurement_source = "definition_points"
    raw_text = entity.dxf.get("text", "<>")
    if raw_text in _AUTO_DIM_TEXT:
        text = _fmt_measurement(measurement) if measurement is not None else ""
    else:
        text = str(raw_text)
    if start is not None and end is not None:
        dx, dy = end[0] - start[0], end[1] - start[1]
        orientation = "horizontal" if abs(dx) >= abs(dy) else "vertical"
        position = [round((start[0] + end[0]) / 2, 3), round((start[1] + end[1]) / 2, 3)]
    else:
        orientation = "unknown"
        position = _point(entity, "text_midpoint")
    return {
        "text": text,
        "measurement": round(measurement, 4) if measurement is not None else None,
        "layer": _layer_of(entity),
        "kind": kind,
        "position": position,
        "text_position": _point(entity, "text_midpoint"),
        "start": start,
        "end": end,
        "orientation": orientation,
        "measurement_source": measurement_source,
    }


def _dimensions(entities: Iterable[Any]) -> list[dict[str, Any]]:
    """图纸上已有的 DIMENSION 标注读数(query_dimension 的唯一数据源)。

    text 是标注上写的字、measurement 是几何实测值 —— 两者可能不同(标注可手改),
    所以两个都留下。text 是 "<>"/"" 表示没手改,此时用格式化后的实测值当展示文字。
    """
    out: list[dict[str, Any]] = []
    for entity in entities:
        if entity.dxftype() == "DIMENSION":
            out.append(_dimension(entity))
    return out


def _texts(entities: Iterable[Any]) -> list[dict[str, Any]]:
    """图上的 TEXT / MTEXT 文字(read_view_params 的数据源之一)。

    标高「±0.000」「3.600」、层高说明、房间名这些**常是文字而非 DIMENSION**,单靠 _dimensions
    读不到。这里只如实抽图上写着的字,不解释、不换算 —— 与标注一个性质(read 侧照抄,不自算)。
    索引保留全部文字供按房间名定位;工具输出仍会截短展示,不能在解析层先把后面的房间名丢掉。
    """
    out: list[dict[str, Any]] = []
    for entity in entities:
        if entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB"}:
            continue
        if entity.dxftype() == "MTEXT":
            text = entity.plain_text().strip()  # 去掉 MTEXT 的排版控制码,留纯文字
        else:
            text = str(entity.dxf.get("text", "")).strip()
        if text:
            out.append(
                {"text": text, "layer": _layer_of(entity), "position": _point(entity, "insert")}
            )
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


# 离群/坐标异常两个阈值,超了就当「整图渲出来是一个点浮在巨大空白里」处理。
# ① offset:内容外框离原点的最大偏移 > 内容自身尺寸的 K 倍 = 大地坐标系(测绘绝对坐标)。
#    渲染画布从原点铺到内容处,内容缩成一个点。正常图内容在原点附近、偏移≈尺寸(比值 1~数倍);
#    大地坐标图比值动辄上万(遂川总图实测 ~4万,而正常的 test-3 仅 2.3)。K=100 留足余量。
_EXTENTS_OFFSET_RATIO = 100.0
# ② spread:全跨度 > 核心跨度(2–98 百分位)的 K 倍 = 个别野点把内容范围本身撑大,
#    多数图元其实挤在一小簇里(offset 判据抓不到这种 —— 野点同时放大尺寸和偏移,比值≈1)。
_EXTENTS_SPREAD_RATIO = 20.0
_EXTENTS_MIN_ENTITIES = 20  # 图元太少,离群统计没意义,不判


def _dim_blown(sorted_vals: list[float]) -> bool:
    """某一维:核心跨度(2–98 百分位)撑不满全跨度的 1/K 就算被野点撑大。

    核心跨度用百分位算,天然躲开野点本身;核心退化成 0(多数图元中心挤在一条线上)
    不判 —— 那更像「一行文字 + 远处标题栏」的正常图,别误伤。
    """
    n = len(sorted_vals)
    full = sorted_vals[-1] - sorted_vals[0]
    if full <= 0:
        return False
    core = sorted_vals[int(0.98 * (n - 1))] - sorted_vals[int(0.02 * (n - 1))]
    if core <= 0:
        return False
    return full / core >= _EXTENTS_SPREAD_RATIO


def _spread_blown(msp: Any) -> bool:
    """野点判据:逐图元中心的全跨度远大于 2–98 百分位核心跨度(见 _dim_blown)。"""
    xs: list[float] = []
    ys: list[float] = []
    for box in bbox.multi_flat(msp, fast=True):
        center = box.center
        xs.append(center.x)
        ys.append(center.y)
    if len(xs) < _EXTENTS_MIN_ENTITIES:
        return False
    xs.sort()
    ys.sort()
    return _dim_blown(xs) or _dim_blown(ys)


def _extents_outlier(msp: Any) -> bool:
    """整图外框是否会让渲染画布爆成一张几乎空白的巨图(大地坐标系 / 个别野点)。

    这类图整张渲出来内容会缩成一个点、几乎空白(config.drawing_render_max_entities 注释
    与 render.py 都提过),与其给用户一张没用的巨图,不如在渲染前如实拦下、改走
    「查图层/构件/尺寸」。两种成因分别用 offset / spread 两个判据,任一命中即判。
    阻塞:一次整图 bbox +(必要时)逐图元 bbox,与 _bounds 同量级。
    """
    ext = bbox.extents(msp, fast=True)
    if not ext.has_data:
        return False
    lo, hi = ext.extmin, ext.extmax
    size = max(hi.x - lo.x, hi.y - lo.y)
    if size <= 0:
        return False
    # ① 大地坐标系:内容离原点极远(短路,命中就不用再扫逐图元)
    offset = max(abs(lo.x), abs(hi.x), abs(lo.y), abs(hi.y))
    if offset > size * _EXTENTS_OFFSET_RATIO:
        return True
    # ② 野点:内容范围本身被个别图元撑大
    return _spread_blown(msp)


def _detect_tianzheng(doc: Any, by_kind: Counter, per_layer: dict[str, Counter]) -> dict[str, Any]:
    """检出天正(TArch)私有构件,产出 ``{detected, component_kinds}``。

    工具层据此对用户说「这是天正图、先导出 T3」,而不是把读不到几何当成「文件坏了」,
    也不至于把一张满是天正墙/柱/标注的图当成「普通图、什么标注都没有」。
    ``component_kinds`` 是 ``{构件标签: 数量}``,交给 tools._tianzheng_summary 说成
    「66 柱、44 墙…」。

    天正私有构件在 ezdxf 里有**两种加载形态**,两种都要认(旧代码只认①,把②整片漏掉):

    ① 直载为 ``TCH_*``:ezdxf 没有私有类定义,当未知类型读进来,``dxftype()`` 就是
       ``TCH_WALL`` 之类 —— 直接按类型计数,标签即 TCH_ 类型名。

    ② 载为通用 ``ACAD_PROXY_ENTITY``:天正带 proxy graphics 存盘时,实体被包成标准代理
       实体,``dxftype()`` 一律是 ``ACAD_PROXY_ENTITY``,**真实类型只在 CLASSES 段登记为
       ``TCH_*``**(APPID 里还留一个 ``_TCH``)。这是国内实际图纸最常见的形态。代理实体
       拿不到逐个的私有类型,退一步**按图层给它们计数**(天正标准英文图层名
       COLUMN/WALL/WINDOW/CURTWALL/AXIS/SPACE…;认不出的图层名原样留着,不假装认得)。

    形态②要求「CLASSES 段有 TCH_ 类」**且**「图里确有代理实体」双条件:只剩残留类登记、
    没有任何天正实体的空图不算天正,免得乱指(泛化的 ``ACAD_PROXY_ENTITY`` 也可能来自别家插件,
    但配上 CLASSES 段的 ``TCH_*`` 登记就足以锁定是天正)。
    """
    tch_by_type = {k: int(v) for k, v in sorted(by_kind.items()) if k.startswith("TCH_")}
    if tch_by_type:  # 形态①
        return {"detected": True, "component_kinds": tch_by_type}

    # 形态②:CLASSES 段登记了 TCH_* 类 + 图里确有代理实体
    has_tch_class = any(str(getattr(cls.dxf, "name", "")).startswith("TCH_") for cls in doc.classes)
    if has_tch_class and by_kind.get("ACAD_PROXY_ENTITY"):
        proxy_by_layer = {
            layer: int(kinds["ACAD_PROXY_ENTITY"])
            for layer, kinds in sorted(per_layer.items())
            if kinds.get("ACAD_PROXY_ENTITY")
        }
        if proxy_by_layer:
            return {"detected": True, "component_kinds": proxy_by_layer}

    return {"detected": False, "component_kinds": {}}


def parse_dxf(path: Path) -> dict[str, Any]:
    """解析一张 DXF,产出落地文档第 4 节的中间对象(不含 source_artifact_id)。

    阻塞函数:调用方负责 ``asyncio.to_thread`` 包。
    文件损坏/打不开时**不吞异常**,让 ezdxf 的异常向上抛给工具层翻成 FILE_CORRUPT。
    """
    encoding = _legacy_encoding_override(path)
    doc = ezdxf.readfile(str(path), encoding=encoding)
    msp = doc.modelspace()

    by_kind, per_layer, insert_by_block = _scan_modelspace(msp)
    # 一次递归展开后同时抽尺寸与文字。两个函数各自拿 list 迭代,不重复展开大块。
    expanded = list(_expanded_entities(msp))
    insunits = int(doc.header.get("$INSUNITS", 0))

    return {
        "format": "dxf",  # 索引格式判别:工具层按它在 dxf/pdf 两条线间分流
        "index_schema_version": DXF_INDEX_SCHEMA_VERSION,
        "encoding": str(doc.output_encoding),
        "dxf_version": str(doc.dxfversion),
        "insunits": insunits,
        "units_label": _units_label(insunits),
        "layers": _layers(doc, per_layer),
        "entities_by_kind": {k: int(v) for k, v in sorted(by_kind.items())},
        "blocks": _blocks(doc, insert_by_block),
        "dimensions": _dimensions(expanded),
        "annotations": _texts(expanded),
        "bounds": _bounds(msp),
        "extents_outlier": _extents_outlier(msp),
        "tianzheng": _detect_tianzheng(doc, by_kind, per_layer),
    }


__all__ = ["parse_dxf"]
