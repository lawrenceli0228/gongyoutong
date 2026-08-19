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
from gyt.db import projects as db
from tests.unit._dxf_fixtures import (
    _find_cjk_font,
    make_broken_dxf,
    make_gbk_dxf,
    make_scanned_pdf,
    make_tianzheng_dxf,
    make_vector_pdf,
)

# 缺 CJK 字体的环境跳过 PDF 相关用例(造不了带中文的矢量 PDF)。
_needs_cjk = pytest.mark.skipif(
    _find_cjk_font() is None, reason="环境缺 CJK 字体,造不了带中文的矢量 PDF 样例"
)


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
    assert result["data"]["demo"] == ["首层平面图"]
    assert result["data"]["uploaded"] == []  # 没上传项目图时,这块为空


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


# --- PDF 图纸(预览 + 读图上文字;结构化查询如实拒)---------------------------


@pytest.fixture
def pdf_env(tmp_path, monkeypatch):
    """把一张矢量 PDF 预注册成「首层平面图PDF」,供工具层断言 PDF 分流。"""
    make_vector_pdf(tmp_path / "vec.pdf")
    drawing_id = artifacts.register(
        tmp_path / "vec.pdf", kind=ArtifactKind.DRAWING, original_name="plan.pdf"
    )
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"首层平面图PDF": drawing_id})
    return {"id": drawing_id, "name": "首层平面图PDF"}


@_needs_cjk
async def test_parse_drawing认PDF给页数与能力说明(pdf_env):
    result = await tools.parse_drawing.ainvoke({"drawing": "首层平面图PDF"})
    assert result["ok"] is True
    assert result["data"]["format"] == "pdf"
    assert result["data"]["page_count"] == 1
    assert result["data"]["has_text"] is True
    assert "PDF" in result["user_msg"]
    assert "预览" in result["user_msg"]  # 说清能做的两样之一


@_needs_cjk
async def test_read_view_params读出PDF图上文字(pdf_env):
    # PDF 的看家能力:把图上文字如实抽出来答问。
    result = await tools.read_view_params.ainvoke({"drawing": "首层平面图PDF"})
    assert result["ok"] is True
    assert result["data"]["format"] == "pdf"
    texts = " ".join(a["text"] for a in result["data"]["annotations"])
    assert "标高 ±0.000" in texts
    assert "客厅 3600" in texts


@_needs_cjk
async def test_query_dimension对PDF如实说读不了(pdf_env):
    result = await tools.query_dimension.ainvoke({"drawing": "首层平面图PDF"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"
    assert "PDF" in result["user_msg"]


@_needs_cjk
async def test_layer_stats对PDF如实说没有图层对象(pdf_env):
    result = await tools.layer_stats.ainvoke({"drawing": "首层平面图PDF"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"
    assert "PDF" in result["user_msg"]


@_needs_cjk
async def test_list_components对PDF如实说没有构件对象(pdf_env):
    result = await tools.list_components.ainvoke({"drawing": "首层平面图PDF"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"


@_needs_cjk
async def test_render_preview对PDF出PNG(pdf_env):
    result = await tools.render_preview.ainvoke({"drawing": "首层平面图PDF"})
    assert result["ok"] is True
    assert result["data"]["format"] == "pdf"
    path = artifacts.resolve(result["data"]["png_id"])
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def scanned_pdf_env(tmp_path, monkeypatch):
    """一张无文字层的 PDF(has_text=False),用来测「文字选不中 → 视觉兜底」这条路。

    视觉调用一律打桩(下面每个用例各自 setattr tools.vision.read_drawing_text),
    绝不发真网络请求(CLAUDE.md:测试不联网、不产生账单)。make_scanned_pdf 不需要 CJK 字体。
    """
    make_scanned_pdf(tmp_path / "scan.pdf")
    sid = artifacts.register(
        tmp_path / "scan.pdf", kind=ArtifactKind.DRAWING, original_name="scan.pdf"
    )
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"扫描图": sid})
    return {"id": sid, "name": "扫描图"}


async def test_文字选不中的PDF走视觉兜底认出字(scanned_pdf_env, monkeypatch):
    # 核心:文字层为空时不再直接放弃,渲染成图交给视觉模型认字。
    async def _fake_read(_png: bytes) -> str:
        return "标高 ±0.000\n消防车道 2000\n配电室"

    monkeypatch.setattr(tools.vision, "read_drawing_text", _fake_read)
    result = await tools.read_view_params.ainvoke({"drawing": "扫描图"})

    assert result["ok"] is True
    assert result["data"]["source"] == "vision"
    assert result["data"]["annotations_recognized"] == ["标高 ±0.000", "消防车道 2000", "配电室"]
    # 红线:认出来的字必须带「可能有误 / 以原图为准」,和「读到图上文字」那套精确措辞分开。
    assert "可能有误" in result["user_msg"]
    assert "以原图为准" in result["user_msg"]


async def test_视觉也认不出字时如实说没认出(scanned_pdf_env, monkeypatch):
    async def _fake_read(_png: bytes) -> str:
        return tools.vision.EMPTY_MARKER

    monkeypatch.setattr(tools.vision, "read_drawing_text", _fake_read)
    result = await tools.read_view_params.ainvoke({"drawing": "扫描图"})

    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"
    assert "没认出" in result["user_msg"]


async def test_视觉调用失败时透传中文错误(scanned_pdf_env, monkeypatch):
    from gyt.core.llm import LLMCallError

    async def _fake_read(_png: bytes) -> str:
        raise LLMCallError("AI 助手暂时连不上,请稍后再试。", "UPSTREAM_ERROR")

    monkeypatch.setattr(tools.vision, "read_drawing_text", _fake_read)
    result = await tools.read_view_params.ainvoke({"drawing": "扫描图"})

    assert result["ok"] is False
    assert result["error_code"] == "UPSTREAM_ERROR"
    assert "连不上" in result["user_msg"]


async def test_视觉密钥没配时指到env而非系统开小差(scanned_pdf_env, monkeypatch):
    from gyt.core.llm import MissingAPIKeyError

    async def _fake_read(_png: bytes) -> str:
        raise MissingAPIKeyError("Kimi(月之暗面)", "GYT_MOONSHOT_API_KEY")

    monkeypatch.setattr(tools.vision, "read_drawing_text", _fake_read)
    result = await tools.read_view_params.ainvoke({"drawing": "扫描图"})

    assert result["ok"] is False
    # 指到 .env 哪一行,不被 tool_guard 吞成 INTERNAL。
    assert "GYT_MOONSHOT_API_KEY" in result["user_msg"]


@_needs_cjk
async def test_有矢量文字的PDF不触发视觉省钱路径不误触(pdf_env, monkeypatch):
    called = False

    async def _fake_read(_png: bytes) -> str:
        nonlocal called
        called = True
        return "不该被调到"

    monkeypatch.setattr(tools.vision, "read_drawing_text", _fake_read)
    result = await tools.read_view_params.ainvoke({"drawing": "首层平面图PDF"})

    assert result["ok"] is True
    assert called is False  # 有矢量文字直接读,绝不触发视觉(那是要花钱的)


async def test_parse_drawing对文字选不中的PDF改口指路读图上文字(scanned_pdf_env):
    # §4 联动:has_text=False 的概览文案不再说死「都读不了」,改成指路到「读图上文字」兜底。
    # parse_drawing 本身不调视觉,无需打桩。
    result = await tools.parse_drawing.ainvoke({"drawing": "扫描图"})
    assert result["ok"] is True
    assert result["data"]["has_text"] is False
    assert "读图上文字" in result["user_msg"]
    assert "都读不了" not in result["user_msg"]


# --- 天正图(TCH_* 私有构件)------------------------------------------------


@pytest.fixture
def tianzheng_env(tmp_path, monkeypatch):
    """把一张天正图预注册成「天正楼层图」,供工具层断言不再误判、并给出导出步骤。"""
    make_tianzheng_dxf(tmp_path / "tz.dxf")
    drawing_id = artifacts.register(
        tmp_path / "tz.dxf", kind=ArtifactKind.DRAWING, original_name="tz.dxf"
    )
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"天正楼层图": drawing_id})
    return {"id": drawing_id, "name": "天正楼层图"}


async def test_天正图不再误判为FILE_CORRUPT且给导出步骤(tianzheng_env):
    # 回归核心:旧代码把天正图当「文件传坏了」。现在应成功、如实说是天正图、并给 T3 导出步骤。
    result = await tools.parse_drawing.ainvoke({"drawing": "天正楼层图"})
    assert result["ok"] is True
    assert result["data"]["tianzheng"]["detected"] is True
    msg = result["user_msg"]
    assert "天正" in msg
    assert "图形导出" in msg and "T3" in msg  # 正确步骤交给了用户
    assert "传坏" not in msg  # 不再是误诊那句


async def test_天正图查标注给的是导出指引而非图上没标(tianzheng_env):
    # 天正的标注锁在私有构件里,不能对用户说「图上没标」—— 要说清是天正、给导出路。
    result = await tools.query_dimension.ainvoke({"drawing": "天正楼层图"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"
    assert "天正" in result["user_msg"]
    assert "图上没标" not in result["user_msg"]


async def test_天正图概览标出私有构件计数(tianzheng_env):
    result = await tools.list_components.ainvoke({"drawing": "天正楼层图"})
    assert result["ok"] is True
    assert result["data"]["tianzheng"]["detected"] is True
    # 私有构件按人话计数报出来:3 墙、2 柱。
    assert "墙" in result["user_msg"] and "柱" in result["user_msg"]


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


async def test_render_preview图元过多时如实拒绝(cad_env, monkeypatch):
    # 把阈值压到 1,让任何真图都算「太多」—— 不渲染、如实说,不硬撑到卡死。
    from gyt.config import get_settings

    monkeypatch.setenv("GYT_DRAWING_RENDER_MAX_ENTITIES", "1")
    get_settings.cache_clear()
    result = await tools.render_preview.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is False
    assert result["error_code"] == "FILE_TOO_LARGE"
    assert "图元太多" in result["user_msg"]
    # 关键:失败信封里没有 png_id,模型无从编造「预览出好了」。
    assert result["data"] is None


# --- list_projects / 上传的项目图 / read_view_params(W7 §5)--------------------


@pytest.fixture
def uploaded_env(tmp_path, monkeypatch):
    """把 GBK 图当作**上传入库的项目图**:注册产物 + 建 drawings 行(view=立面),demo 置空。"""
    monkeypatch.setattr(tools, "get_demo_drawings", dict)  # demo 空,只留项目图
    make_gbk_dxf(tmp_path / "gbk.dxf")
    aid = artifacts.register(
        tmp_path / "gbk.dxf", kind=ArtifactKind.DRAWING, original_name="ele.dxf"
    )
    db.create_project("gyt-a3", "A3栋", "A3")
    db.add_drawing("gyt-a3", aid, "elevation", "南立面图", floor="1F")
    return {"aid": aid}


async def test_list_projects列出项目(monkeypatch):
    monkeypatch.setattr(tools, "get_demo_drawings", dict)
    db.create_project("gyt-a3", "幸福小区A3栋", "A3")
    result = await tools.list_projects.ainvoke({})
    assert result["ok"] is True
    assert result["data"]["projects"] == [{"id": "gyt-a3", "name": "幸福小区A3栋"}]


async def test_list_projects没项目时EMPTY(monkeypatch):
    monkeypatch.setattr(tools, "get_demo_drawings", dict)
    result = await tools.list_projects.ainvoke({})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"


async def test_按展示名解析到上传的项目图(uploaded_env):
    # 不是 demo、不是 id,而是 drawings 表里的展示名 → 也能查到
    result = await tools.parse_drawing.ainvoke({"drawing": "南立面图"})
    assert result["ok"] is True
    assert result["data"]["encoding"] == "gbk"


async def test_list_drawings含上传的项目图(uploaded_env):
    result = await tools.list_drawings.ainvoke({})
    assert result["ok"] is True
    assert {
        "project_id": "gyt-a3",
        "title": "南立面图",
        "view_type": "elevation",
    } in result["data"]["uploaded"]
    assert "南立面图" in result["user_msg"]
    assert "立面" in result["user_msg"]


async def test_read_view_params读出视图类型标注与文字(uploaded_env):
    result = await tools.read_view_params.ainvoke({"drawing": "南立面图"})
    assert result["ok"] is True
    data = result["data"]
    assert data["view_type"] == "elevation"  # 从 drawings 行取
    ann = [a["text"] for a in data["annotations"]]
    assert "首层平面图" in ann  # 图上 TEXT 文字被抽出(标高/层高走这条)
    assert any(d["text"] == "6000" for d in data["dimensions"])  # 标注也在
    assert "立面图" in result["user_msg"]


async def test_read_view_params对demo图无view_type也能读(cad_env):
    # demo 图不在 drawings 表 → view_type=None,但标注/文字照读
    result = await tools.read_view_params.ainvoke({"drawing": "首层平面图"})
    assert result["ok"] is True
    assert result["data"]["view_type"] is None


async def test_read_view_params图上没标没写时EMPTY(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_demo_drawings", dict)
    doc = ezdxf.new("R2010")
    doc.layers.add("WALL")
    doc.modelspace().add_line((0, 0), (1, 0), dxfattribs={"layer": "WALL"})
    doc.saveas(tmp_path / "blank.dxf")
    aid = artifacts.register(
        tmp_path / "blank.dxf", kind=ArtifactKind.DRAWING, original_name="blank.dxf"
    )
    db.create_project("gyt-a3", "A3栋")
    db.add_drawing("gyt-a3", aid, "plan", "白图")

    result = await tools.read_view_params.ainvoke({"drawing": "白图"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"


# --- 当前工地作用域(选了项目就只在该项目内按图名找图)-------------------------


async def test_图纸解析按当前工地作用域(tmp_path, monkeypatch):
    """选了工地:按图名找图只在该项目内;别的项目的同名请求不串过来;不选则跨项目(旧行为)。"""
    make_gbk_dxf(tmp_path / "a.dxf")
    aid = artifacts.register(tmp_path / "a.dxf", kind=ArtifactKind.DRAWING, original_name="a.dxf")
    monkeypatch.setattr(tools, "get_demo_drawings", dict)  # 没有 demo,强制走 db 标题解析
    db.create_project("pa", "项目A", "A")
    db.create_project("pb", "项目B", "B")
    db.add_drawing("pa", aid, "plan", "现场图")  # 只有 pa 有「现场图」

    # 选中 pa → 在本项目内找到,解析成功
    r = await tools.parse_drawing.ainvoke(
        {"drawing": "现场图"}, config={"configurable": {"gyt_project_id": "pa"}}
    )
    assert r["ok"] is True

    # 选中 pb → pb 没有「现场图」,不串到 pa → NOT_FOUND
    r2 = await tools.parse_drawing.ainvoke(
        {"drawing": "现场图"}, config={"configurable": {"gyt_project_id": "pb"}}
    )
    assert r2["ok"] is False
    assert r2["error_code"] == "NOT_FOUND"

    # 不选工地 → 跨项目仍找得到(旧行为不变)
    r3 = await tools.parse_drawing.ainvoke({"drawing": "现场图"})
    assert r3["ok"] is True


async def test_list_drawings选了工地只列本项目图(tmp_path, monkeypatch):
    """选了工地:list_drawings 只列该项目的图,不掺 demo、不列别的项目。断「问项目2报项目1的图」。"""
    make_gbk_dxf(tmp_path / "a.dxf")
    a = artifacts.register(tmp_path / "a.dxf", kind=ArtifactKind.DRAWING, original_name="a.dxf")
    make_gbk_dxf(tmp_path / "b.dxf")
    b = artifacts.register(tmp_path / "b.dxf", kind=ArtifactKind.DRAWING, original_name="b.dxf")
    monkeypatch.setattr(tools, "get_demo_drawings", lambda: {"演示图": "0" * 32})
    db.create_project("pa", "A", "A")
    db.create_project("pb", "B", "B")
    db.add_drawing("pa", a, "plan", "甲图")
    db.add_drawing("pb", b, "plan", "乙图")

    # 选 pa → 只有甲图,没有 demo、没有乙图
    r = await tools.list_drawings.ainvoke({}, config={"configurable": {"gyt_project_id": "pa"}})
    assert r["ok"] is True
    assert [u["title"] for u in r["data"]["uploaded"]] == ["甲图"]
    assert r["data"]["demo"] == []

    # 不选工地 → demo + 全部项目(旧行为)
    r2 = await tools.list_drawings.ainvoke({})
    assert {u["title"] for u in r2["data"]["uploaded"]} == {"甲图", "乙图"}
    assert r2["data"]["demo"] == ["演示图"]
