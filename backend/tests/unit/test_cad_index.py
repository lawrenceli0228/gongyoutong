"""index.py 单测:落盘复读一致 + 键=id + ensure_index 命中不重复解析(落地文档第 11 节)。"""

from __future__ import annotations

import pytest

from gyt.agents.cad import index, parse
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from tests.unit._dxf_fixtures import make_gbk_dxf


def _register(tmp_path) -> str:
    make_gbk_dxf(tmp_path / "gbk.dxf")
    return artifacts.register(
        tmp_path / "gbk.dxf", kind=ArtifactKind.DRAWING, original_name="gbk.dxf"
    )


def test_落盘后可复读且一致(tmp_path):
    drawing_id = _register(tmp_path)
    parsed = parse.parse_dxf(artifacts.resolve(drawing_id))
    index.write(drawing_id, parsed)

    back = index.read(drawing_id)
    assert back == parsed


def test_索引键就是drawing_id(tmp_path):
    from gyt.config import get_settings

    drawing_id = _register(tmp_path)
    index.write(drawing_id, {"layers": []})
    expected = get_settings().cad_index_dir / f"{drawing_id}.json"
    assert expected.is_file()


def test_读不存在的索引返回None(tmp_path):
    assert index.read("a" * 32) is None


def test_读坏掉的索引返回None(tmp_path):
    from gyt.config import get_settings

    bad = get_settings().cad_index_dir / f"{'b' * 32}.json"
    bad.write_text("{ 这不是合法 json", encoding="utf-8")
    assert index.read("b" * 32) is None


def test_非法id拒绝拼路径(tmp_path):
    with pytest.raises(ValueError):
        index.read("../etc/passwd")


async def test_ensure_index命中读盘不重复解析(tmp_path, monkeypatch):
    # 打桩数 parse 调用次数,和 safety 数内层模型调用同理(落地文档第 11 节)。
    drawing_id = _register(tmp_path)

    calls = {"n": 0}
    real = parse.parse_dxf

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(parse, "parse_dxf", counting)

    first = await index.ensure_index(drawing_id)  # 未命中 → 解析一次并落盘
    assert calls["n"] == 1
    assert first["source_artifact_id"] == drawing_id

    second = await index.ensure_index(drawing_id)  # 命中读盘 → 不再解析
    assert calls["n"] == 1
    assert second == first


async def test_ensure_index图纸不存在时抛ArtifactNotFound(tmp_path):
    with pytest.raises(artifacts.ArtifactNotFound):
        await index.ensure_index("f" * 32)
