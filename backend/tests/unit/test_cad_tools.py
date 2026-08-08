"""tools.py 单测:六个工具的成功路径 + 各类失败信封(落地文档第 11 节)。

覆盖清单里点名的硬断言:
  · 图名/id 不存在 → NOT_FOUND;非 DXF → FILE_UNSUPPORTED;超大 → FILE_TOO_LARGE;
  · query_dimension 对「没标注的尺寸」如实 EMPTY_RESULT(不硬编);
  · 损坏 DXF → FILE_CORRUPT;render_preview 出 PNG 且落盘成产物。
"""

from __future__ import annotations

import ezdxf
import pytest

from gyt.agents.cad import tools
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from tests.unit._dxf_fixtures import make_broken_dxf, make_gbk_dxf


@pytest.fixture
def cad_env(tmp_path, monkeypatch):
    """造 GBK 图并预注册成「首层平面图」,把工具的 get_demo_drawings 指向本次映射。"""
    make_gbk_dxf(tmp_path / "gbk.dxf")
    drawing_id = artifacts.register(
        tmp_path / "gbk.dxf", kind=ArtifactKind.DRAWING, original_name="plan_gbk.dxf"
    )
    mapping = {"首层平面图": drawing_id}
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: mapping)
    return {"id": drawing_id, "name": "首层平面图", "tmp": tmp_path, "monkeypatch": monkeypatch}


# --- list_drawings -----------------------------------------------------------


async def test_list_drawings列出名字(cad_env):
    result = await tools.list_drawings.ainvoke({})
    assert result["ok"] is True
    assert result["data"]["drawings"] == ["首层平面图"]


async def test_list_drawings空表时EMPTY_RESULT(monkeypatch):
    monkeypatch.setattr(tools, "get_demo_drawings", dict)
    result = await tools.list_drawings.ainvoke({})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"


# --- parse_drawing -----------------------------------------------------------


async def test_parse_drawing按名字给概览(cad_env):
    result = await tools.parse_drawing.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is True
    data = result["data"]
    assert data["encoding"] == "gbk"
    assert data["units_label"] == "mm"
    assert data["layers_count"] >= 3
    assert data["entities_total"] >= 1


async def test_parse_drawing按id也认(cad_env):
    result = await tools.parse_drawing.ainvoke({"drawing": cad_env["id"]})
    assert result["ok"] is True


async def test_图名不存在返回NOT_FOUND(cad_env):
    result = await tools.parse_drawing.ainvoke({"drawing": "地下室平面图"})
    assert result["ok"] is False
    assert result["error_code"] == "NOT_FOUND"
    # 失败文案要把现有图纸名报给用户,方便他改口。
    assert "首层平面图" in result["user_msg"]


async def test_非DXF文件返回FILE_UNSUPPORTED(tmp_path, monkeypatch):
    txt_id = artifacts.register(b"not a dxf", kind=ArtifactKind.DOCUMENT, original_name="a.txt")
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"随便": txt_id})
    result = await tools.parse_drawing.ainvoke({"drawing": "随便"})
    assert result["ok"] is False
    assert result["error_code"] == "FILE_UNSUPPORTED"


async def test_超大图纸返回FILE_TOO_LARGE(cad_env, monkeypatch):
    # 把上限压到极小(不真造大文件),重建 Settings 让新值生效 —— 只改这一项,
    # data_dir 仍由 conftest 的 env 固定,artifacts_dir 不变,已注册的图照样 resolve 得到。
    from gyt.config import get_settings

    monkeypatch.setenv("GYT_DRAWING_MAX_MB", "0.000001")
    get_settings.cache_clear()
    result = await tools.parse_drawing.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is False
    assert result["error_code"] == "FILE_TOO_LARGE"


async def test_损坏DXF返回FILE_CORRUPT(tmp_path, monkeypatch):
    make_broken_dxf(tmp_path / "broken.dxf")
    bad_id = artifacts.register(
        tmp_path / "broken.dxf", kind=ArtifactKind.DRAWING, original_name="broken.dxf"
    )
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"坏图": bad_id})
    result = await tools.parse_drawing.ainvoke({"drawing": "坏图"})
    assert result["ok"] is False
    assert result["error_code"] == "FILE_CORRUPT"


# --- query_dimension ---------------------------------------------------------


async def test_query_dimension读出标注读数(cad_env):
    result = await tools.query_dimension.ainvoke({"drawing": "首层平面图", "target": "标注"})
    assert result["ok"] is True
    dims = result["data"]["dimensions"]
    assert any(d["text"] == "6000" and d["units_label"] == "mm" for d in dims)


async def test_query_dimension没标注的尺寸如实说做不了(cad_env):
    # 关键断言:图上没有和「柱距」对得上的标注 → EMPTY_RESULT,不硬编一个数。
    result = await tools.query_dimension.ainvoke({"drawing": "首层平面图", "target": "柱距"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"
    assert "已经标注" in result["user_msg"]


async def test_query_dimension整图无标注时EMPTY_RESULT(tmp_path, monkeypatch):
    # 造一张没有任何 DIMENSION 的图。
    doc = ezdxf.new("R2010")
    doc.layers.add("WALL")
    doc.modelspace().add_line((0, 0), (1, 0), dxfattribs={"layer": "WALL"})
    doc.saveas(tmp_path / "nodim.dxf")
    nodim_id = artifacts.register(
        tmp_path / "nodim.dxf", kind=ArtifactKind.DRAWING, original_name="nodim.dxf"
    )
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"白图": nodim_id})
    result = await tools.query_dimension.ainvoke({"drawing": "白图"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"


# --- list_components ---------------------------------------------------------


async def test_list_components按块名数出柱(cad_env):
    result = await tools.list_components.ainvoke({"drawing": "首层平面图", "kind": "柱-KZ1"})
    assert result["ok"] is True
    blocks = result["data"]["blocks"]
    assert blocks == [{"name": "柱-KZ1", "insert_count": 2}]


async def test_list_components按图层给分布(cad_env):
    result = await tools.list_components.ainvoke({"drawing": "首层平面图", "layer": "柱"})
    assert result["ok"] is True
    assert result["data"]["layer"] == "柱"
    assert result["data"]["kinds"].get("INSERT") == 2


async def test_list_components概览(cad_env):
    result = await tools.list_components.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is True
    assert "entities_by_kind" in result["data"]


async def test_list_components找不到的构件EMPTY_RESULT(cad_env):
    result = await tools.list_components.ainvoke({"drawing": "首层平面图", "kind": "电梯井"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"


# --- layer_stats -------------------------------------------------------------


async def test_layer_stats列出图层(cad_env):
    result = await tools.layer_stats.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is True
    names = {ly["name"] for ly in result["data"]["layers"]}
    assert {"轴线", "柱", "标注"} <= names


# --- render_preview ----------------------------------------------------------


async def test_render_preview出PNG并落盘(cad_env):
    result = await tools.render_preview.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is True
    png_id = result["data"]["png_id"]
    # 真落盘成产物:能 resolve 出文件,且内容是 PNG 魔数开头。
    path = artifacts.resolve(png_id)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
