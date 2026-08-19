"""pdf_export.py + render.to_pdf 单测:DXF → 矢量 PDF 的导出、缓存复用、失败路径。

全落 tmp_path(conftest 的 _isolated_settings),不联网、不调模型。
"""

from __future__ import annotations

import ezdxf
import pytest

from gyt.agents.cad import pdf_export, render
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from tests.unit._dxf_fixtures import make_broken_dxf, make_plain_dxf


def _register_dxf(tmp_path, name: str = "plan.dxf") -> str:
    make_plain_dxf(tmp_path / name)
    return artifacts.register(tmp_path / name, kind=ArtifactKind.DRAWING, original_name=name)


def test_to_pdf_产出矢量PDF字节(tmp_path) -> None:
    make_plain_dxf(tmp_path / "p.dxf")
    data = render.to_pdf(tmp_path / "p.dxf")
    assert data[:5] == b"%PDF-"  # 真是 PDF,不是把 PNG 包进去
    assert len(data) > 100


def test_export_注册为产物且可resolve(tmp_path) -> None:
    did = _register_dxf(tmp_path)
    pid = pdf_export.export_dxf_to_pdf(did)

    assert artifacts.ARTIFACT_ID_RE.fullmatch(pid)
    assert artifacts.resolve(pid).read_bytes()[:5] == b"%PDF-"
    assert artifacts.read_meta(pid)["kind"] == "DRAWING"


def test_export_缓存复用_同一DXF不重转(tmp_path, monkeypatch) -> None:
    did = _register_dxf(tmp_path)
    calls = {"n": 0}
    real = render.to_pdf

    def _counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(pdf_export.render, "to_pdf", _counting)
    first = pdf_export.export_dxf_to_pdf(did)
    second = pdf_export.export_dxf_to_pdf(did)

    assert first == second  # 同一份 PDF
    assert calls["n"] == 1  # 第二次命中缓存,没再渲染


def test_export_缓存产物被删则重转(tmp_path) -> None:
    did = _register_dxf(tmp_path)
    first = pdf_export.export_dxf_to_pdf(did)
    artifacts.delete(first)  # 上次那份 PDF 没了(删图/删项目会这样)

    second = pdf_export.export_dxf_to_pdf(did)
    assert second != first
    assert artifacts.resolve(second).read_bytes()[:5] == b"%PDF-"


def test_export_损坏DXF抛异常交上层翻信封(tmp_path) -> None:
    make_broken_dxf(tmp_path / "broken.dxf")
    bad = artifacts.register(
        tmp_path / "broken.dxf", kind=ArtifactKind.DRAWING, original_name="broken.dxf"
    )
    # 本函数不吞异常:让 ezdxf 的解析异常向上抛,工具层/端点接住翻 FILE_CORRUPT。
    with pytest.raises(ezdxf.DXFError):
        pdf_export.export_dxf_to_pdf(bad)


def test_export_源id无效抛NotFound(tmp_path) -> None:
    with pytest.raises(artifacts.ArtifactNotFound):
        pdf_export.export_dxf_to_pdf("0" * 32)
