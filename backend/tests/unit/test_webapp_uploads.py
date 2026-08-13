"""webapp.py(自定义 HTTP 端点:项目管理 + 图纸上传)的单元测试 —— 不联网,全落 tmp_path。

用 Starlette TestClient 直打 ASGI app。``import webapp`` 成立同 test_auth.py:
backend/ 是 pytest 的 rootdir、已在 sys.path 上。

⚠️ 这里**不测鉴权**:X-Api-Key 校验由 langgraph 的 auth_middleware 在服务层套上
(靠 langgraph.json 的 enable_custom_route_auth,已在 .personal 方案 §3.4 真起服务 curl 验过);
TestClient 直打的是裸 app,本就没有那层中间件,在这里测鉴权是测错了对象。

环境隔离复用 conftest 的 ``_isolated_settings``(autouse):projects_dir / sqlite / artifacts
都落在 tmp_path 下,每个用例一套全新状态。
"""

from __future__ import annotations

import pytest
import webapp
from starlette.testclient import TestClient

from gyt.config import get_settings
from gyt.core import artifacts
from gyt.db import projects as db

_DXF = b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n"


@pytest.fixture
def client() -> TestClient:
    return TestClient(webapp.app)


def _dxf_files(name: str = "首层平面图.dxf", content: bytes = _DXF):
    return {"file": (name, content, "application/octet-stream")}


# ---------------------------------------------------------------------------
# 建项目
# ---------------------------------------------------------------------------


def test_建项目_json_返回短码并建好目录骨架(client: TestClient) -> None:
    resp = client.post("/projects", json={"name": "幸福小区A3栋", "code": "A3"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["id"] == "a3"  # code 压成短码
    # 库里有了
    assert db.get_project("a3") is not None
    # 目录骨架建好了
    root = get_settings().projects_dir / "a3"
    assert (root / "drawings" / "plan").is_dir()
    assert (root / "docs" / "task_book").is_dir()


def test_建项目_form_也行_全中文名回退随机短码(client: TestClient) -> None:
    resp = client.post("/projects", data={"name": "幸福小区"})

    assert resp.status_code == 201
    # 全中文名(无 ASCII、无 code)压不出短码 → 回退随机 p-xxxxxxxx
    assert resp.json()["data"]["id"].startswith("p-")


def test_建项目_缺名字_400(client: TestClient) -> None:
    resp = client.post("/projects", json={"code": "A3"})

    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error_code"] == "INVALID_INPUT"


def test_建项目_短码撞车加序号(client: TestClient) -> None:
    first = client.post("/projects", json={"name": "甲", "code": "A3"}).json()["data"]["id"]
    second = client.post("/projects", json={"name": "乙", "code": "A3"}).json()["data"]["id"]

    assert first == "a3"
    assert second == "a3-2"


def test_列项目(client: TestClient) -> None:
    client.post("/projects", json={"name": "甲", "code": "A3"})
    client.post("/projects", json={"name": "乙", "code": "B1"})

    resp = client.get("/projects")

    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()["data"]["projects"]]
    assert ids == ["a3", "b1"]


# ---------------------------------------------------------------------------
# 传图
# ---------------------------------------------------------------------------


def _make_project(client: TestClient, code: str = "A3") -> str:
    return client.post("/projects", json={"name": "测试项目", "code": code}).json()["data"]["id"]


def test_传图_一条龙落地注册入库(client: TestClient) -> None:
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/drawings",
        files=_dxf_files("南立面图.dxf"),
        data={"view_type": "elevation", "title": "南立面图", "floor": "1F"},
    )

    assert resp.status_code == 201
    data = resp.json()["data"]
    # 物理文件落在项目目录
    landed = get_settings().projects_dir / pid / "drawings" / "elevation" / "南立面图.dxf"
    assert landed.read_bytes() == _DXF
    assert data["rel_path"] == "drawings/elevation/南立面图.dxf"
    # 产物注册表能取回
    assert artifacts.resolve(data["artifact_id"]).read_bytes() == _DXF
    # drawings 行写好了
    row = db.get_drawing_by_id(data["drawing_id"])
    assert row is not None
    assert row.project_id == pid
    assert row.view_type == "elevation"
    assert row.title == "南立面图"
    assert row.floor == "1F"


def test_传图_不给title默认用文件名主干(client: TestClient) -> None:
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/drawings",
        files=_dxf_files("KZ1柱平面.dxf"),
        data={"view_type": "plan"},
    )

    assert resp.status_code == 201
    assert resp.json()["data"]["title"] == "KZ1柱平面"


def test_传图_非法view_type_400(client: TestClient) -> None:
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/drawings", files=_dxf_files(), data={"view_type": "3d"}
    )

    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_INPUT"


def test_传图_非dxf_415(client: TestClient) -> None:
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/drawings",
        files={"file": ("图纸.dwg", b"whatever", "application/octet-stream")},
        data={"view_type": "plan"},
    )

    assert resp.status_code == 415
    assert resp.json()["error_code"] == "FILE_UNSUPPORTED"


def test_传图_缺文件_400(client: TestClient) -> None:
    pid = _make_project(client)

    resp = client.post(f"/projects/{pid}/drawings", data={"view_type": "plan"})

    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_INPUT"


def test_传图_超大_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GYT_DRAWING_MAX_MB", "0.00005")  # ~52 字节上限
    get_settings.cache_clear()
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/drawings",
        files=_dxf_files(content=b"x" * 200),
        data={"view_type": "plan"},
    )

    assert resp.status_code == 413
    assert resp.json()["error_code"] == "FILE_TOO_LARGE"


def test_传图_项目不存在_404(client: TestClient) -> None:
    resp = client.post(
        "/projects/no-such/drawings", files=_dxf_files(), data={"view_type": "plan"}
    )

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 传规范 / 任务书(/docs、/projects/{id}/docs)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ingest(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """挡掉真入库(会加载 2.2GB BGE-M3),只记录调用参数、返回假 chunk 数。"""
    calls: list[dict] = []

    def _fake(path, *, scope: str, doc_type: str, project_id: str = "") -> int:
        calls.append({"scope": scope, "doc_type": doc_type, "project_id": project_id})
        return 7

    monkeypatch.setattr("gyt.agents.knowledge.ingest.ingest_document", _fake)
    return calls


def _pdf_files(name: str = "GB50016.pdf", content: bytes = b"%PDF-1.4 fake"):
    return {"file": (name, content, "application/pdf")}


def test_传全局规范_落地注册入库(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post("/docs", files=_pdf_files(), data={"doc_type": "regulation"})

    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["scope"] == "global"
    assert data["chunks"] == 7
    assert (get_settings().global_dir / "docs" / "regulation" / "GB50016.pdf").exists()
    assert fake_ingest[0] == {"scope": "global", "doc_type": "regulation", "project_id": ""}


def test_全局不收任务书_400(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post("/docs", files=_pdf_files("任务书.pdf"), data={"doc_type": "task_book"})

    assert resp.status_code == 400
    assert fake_ingest == []  # 没走到入库


def test_传项目任务书_落到项目docs并入库(client: TestClient, fake_ingest: list[dict]) -> None:
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/docs",
        files=_pdf_files("施工任务书.pdf"),
        data={"doc_type": "task_book"},
    )

    assert resp.status_code == 201
    assert fake_ingest[0] == {"scope": "project", "doc_type": "task_book", "project_id": pid}
    assert (get_settings().projects_dir / pid / "docs" / "task_book" / "施工任务书.pdf").exists()


def test_传文档_非pdf_415(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post(
        "/docs",
        files={"file": ("规范.docx", b"x", "application/octet-stream")},
        data={"doc_type": "regulation"},
    )

    assert resp.status_code == 415
    assert fake_ingest == []


def test_传文档_非法doctype_400(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post("/docs", files=_pdf_files(), data={"doc_type": "manual"})

    assert resp.status_code == 400


def test_传项目文档_项目不存在_404(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post(
        "/projects/no-such/docs", files=_pdf_files(), data={"doc_type": "regulation"}
    )

    assert resp.status_code == 404
    assert fake_ingest == []


def test_传文档_缺文件_400(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post("/docs", data={"doc_type": "regulation"})

    assert resp.status_code == 400
    assert fake_ingest == []


def test_传文档_超大_413(
    client: TestClient, fake_ingest: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GYT_DOCUMENT_MAX_MB", "0.00005")  # ~52 字节上限
    get_settings.cache_clear()

    resp = client.post(
        "/docs", files=_pdf_files(content=b"x" * 200), data={"doc_type": "regulation"}
    )

    assert resp.status_code == 413
    assert fake_ingest == []
