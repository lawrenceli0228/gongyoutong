"""demo_registry.py 单测:扫目录注册 + 名字→id + list_drawings 名单(落地文档第 11 节)。"""

from __future__ import annotations

import json

from gyt.agents.cad import demo_registry
from gyt.core import artifacts
from tests.unit._dxf_fixtures import make_gbk_dxf, make_plain_dxf


def _make_dir(tmp_path):
    drawings = tmp_path / "drawings"
    drawings.mkdir()
    make_gbk_dxf(drawings / "plan_gbk.dxf")
    make_plain_dxf(drawings / "plan_utf8.dxf")
    (drawings / "names.json").write_text(
        json.dumps({"plan_gbk.dxf": "首层平面图"}, ensure_ascii=False), encoding="utf-8"
    )
    return drawings


def test_扫目录注册出映射表(tmp_path):
    mapping = demo_registry.register_demo_drawings(_make_dir(tmp_path))

    # names.json 配了的用中文名,没配的退回文件名主干。
    assert "首层平面图" in mapping
    assert "plan_utf8" in mapping
    assert len(mapping) == 2


def test_名字映射到合法artifact_id且能resolve(tmp_path):
    mapping = demo_registry.register_demo_drawings(_make_dir(tmp_path))
    drawing_id = mapping["首层平面图"]

    assert artifacts.ARTIFACT_ID_RE.fullmatch(drawing_id)
    # 注册是真落盘了的:resolve 得到真实文件路径。
    assert artifacts.resolve(drawing_id).is_file()


def test_目录不存在返回空表(tmp_path):
    assert demo_registry.register_demo_drawings(tmp_path / "不存在") == {}


async def test_list_drawings工具返回名字清单(tmp_path, monkeypatch):
    mapping = demo_registry.register_demo_drawings(_make_dir(tmp_path))
    # 工具通过 get_demo_drawings 取表;这里把它换成本次注册的结果。
    monkeypatch.setattr("gyt.agents.cad.tools.get_demo_drawings", lambda: mapping)

    from gyt.agents.cad.tools import list_drawings

    envelope = await list_drawings.ainvoke({})
    assert envelope["ok"] is True
    assert set(envelope["data"]["drawings"]) == set(mapping.keys())
