"""parse.py 单测:普通/GBK/损坏三张图 + 编码/单位探测(落地文档第 11 节)。"""

from __future__ import annotations

import pytest
from ezdxf.lldxf.const import DXFStructureError

from gyt.agents.cad import parse
from tests.unit._dxf_fixtures import (
    make_broken_dxf,
    make_gbk_dxf,
    make_plain_dxf,
    make_tianzheng_dxf,
)


def test_普通图解析出图层与图元数(tmp_path):
    make_plain_dxf(tmp_path / "plain.dxf")
    result = parse.parse_dxf(tmp_path / "plain.dxf")

    names = {ly["name"] for ly in result["layers"]}
    assert {"WALL", "AXIS", "DIM"} <= names
    assert result["entities_by_kind"]["LINE"] >= 1
    assert result["entities_by_kind"]["CIRCLE"] == 1
    assert result["encoding"] == "utf-8"


def test_GBK图解析出正确中文图层名且版本不高于AC1018(tmp_path):
    # 头号断言:比的是内存字符串,和终端能不能显示中文无关(落地文档 1.3 第 2 条)。
    make_gbk_dxf(tmp_path / "gbk.dxf")
    result = parse.parse_dxf(tmp_path / "gbk.dxf")

    names = {ly["name"] for ly in result["layers"]}
    assert "轴线" in names
    assert "柱" in names
    assert result["encoding"] == "gbk"
    # 没有版本断言,哪天有人换成新版样例,GBK 路径就再没被测过(落地文档第 5 节)。
    assert result["dxf_version"] <= "AC1018"


def test_损坏文件解析时抛DXF异常(tmp_path):
    # parse 层故意不吞:异常冒到工具层才翻成 FILE_CORRUPT(落地文档第 6 节)。
    make_broken_dxf(tmp_path / "broken.dxf")
    with pytest.raises(DXFStructureError):
        parse.parse_dxf(tmp_path / "broken.dxf")


def test_单位与块与标注都被探测出来(tmp_path):
    make_gbk_dxf(tmp_path / "gbk.dxf")
    result = parse.parse_dxf(tmp_path / "gbk.dxf")

    assert result["insunits"] == 4
    assert result["units_label"] == "mm"
    # 柱-KZ1 被插了 2 次;系统块(_CLOSEDFILLED 等 '_' 开头)不该混进来。
    assert result["blocks"] == [{"name": "柱-KZ1", "insert_count": 2}]
    # 标注读数:text 照抄标注、measurement 是实测,单位在索引另有 units_label。
    dims = result["dimensions"]
    assert any(d["text"] == "6000" and d["measurement"] == 6000.0 for d in dims)
    assert result["bounds"] is not None


def test_未知单位码给出兜底标签(tmp_path):
    make_plain_dxf(tmp_path / "plain.dxf")
    import ezdxf

    doc = ezdxf.readfile(tmp_path / "plain.dxf")
    doc.header["$INSUNITS"] = 99  # 没有对应人话标签的码
    doc.saveas(tmp_path / "weird.dxf")
    result = parse.parse_dxf(tmp_path / "weird.dxf")
    assert result["units_label"] == "单位码99"


def test_天正图不再崩且图层从代理实体读得出(tmp_path):
    # 回归:旧代码 str(entity.dxf.layer) 对 TCH_* 代理实体抛 DXFAttributeError,
    # 冒到工具层被误判成 FILE_CORRUPT。现在应正常解析,且从组码 8 兜出图层名。
    make_tianzheng_dxf(tmp_path / "tz.dxf")
    result = parse.parse_dxf(tmp_path / "tz.dxf")

    by_kind = result["entities_by_kind"]
    assert by_kind["TCH_WALL"] == 3
    assert by_kind["TCH_COLUMN"] == 2
    assert by_kind["LINE"] == 1  # 普通 AXIS 线照常读到
    # 代理实体的图层没丢:退回扫 xtags 的组码 8 捞出来的。
    layers_with_entities = {ly["name"] for ly in result["layers"] if ly["entity_count"] > 0}
    assert {"WALL-TCH", "COL-TCH", "AXIS"} <= layers_with_entities


def test_天正图被检出并列出私有构件类型(tmp_path):
    make_tianzheng_dxf(tmp_path / "tz.dxf")
    result = parse.parse_dxf(tmp_path / "tz.dxf")

    tz = result["tianzheng"]
    assert tz["detected"] is True
    assert tz["component_kinds"] == {"TCH_WALL": 3, "TCH_COLUMN": 2}


def test_普通图tianzheng字段为未检出(tmp_path):
    make_plain_dxf(tmp_path / "plain.dxf")
    result = parse.parse_dxf(tmp_path / "plain.dxf")

    assert result["tianzheng"]["detected"] is False
    assert result["tianzheng"]["component_kinds"] == {}


def test_图上文字TEXT被抽进annotations(tmp_path):
    # 标高/层高/房间名这类常是 TEXT/MTEXT 而非 DIMENSION,read_view_params 靠 annotations 读它。
    make_gbk_dxf(tmp_path / "gbk.dxf")
    result = parse.parse_dxf(tmp_path / "gbk.dxf")

    texts = [a["text"] for a in result["annotations"]]
    assert "首层平面图" in texts  # gbk 样例里那条 TEXT 被如实抽出
    assert all("layer" in a for a in result["annotations"])
