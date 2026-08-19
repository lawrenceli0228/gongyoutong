"""文件内容端点单测:/files/drawing/{id} 与 /files/doc 的预览/下载响应头 + 归属校验。

用 Starlette TestClient 直打 ASGI app(同 test_webapp_uploads);**不测鉴权**(那层由
langgraph 的 auth_middleware 在服务层套上,裸 app 上测是测错对象)。全落 tmp_path。
"""

from __future__ import annotations

import pytest
import webapp
from starlette.testclient import TestClient

from gyt.config import get_settings
from gyt.core import artifacts, project_fs
from gyt.core.artifacts import ArtifactKind
from tests.unit._dxf_fixtures import make_plain_dxf

_PDF_BYTES = b"%PDF-1.4\n%mock pdf drawing\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def client() -> TestClient:
    return TestClient(webapp.app)


def _make_project(client: TestClient, code: str = "A3") -> str:
    return client.post("/projects", json={"name": "测试项目", "code": code}).json()["data"]["id"]


def _upload_dxf(client: TestClient, pid: str, tmp_path, name: str = "平面图.dxf") -> dict:
    make_plain_dxf(tmp_path / "src.dxf")
    payload = (tmp_path / "src.dxf").read_bytes()
    resp = client.post(
        f"/projects/{pid}/drawings",
        files={"file": (name, payload, "application/octet-stream")},
        data={"view_type": "plan", "title": name.rsplit(".", 1)[0]},
    )
    assert resp.status_code == 201
    return resp.json()["data"]


def _upload_pdf_drawing(client: TestClient, pid: str, name: str = "结构图.pdf") -> dict:
    resp = client.post(
        f"/projects/{pid}/drawings",
        files={"file": (name, _PDF_BYTES, "application/pdf")},
        data={"view_type": "plan", "title": name.rsplit(".", 1)[0]},
    )
    assert resp.status_code == 201
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 图纸文件:原文件下载 / 预览 / DXF 转 PDF
# ---------------------------------------------------------------------------


def test_取DXF原文件_默认inline_二进制类型(client: TestClient, tmp_path) -> None:
    pid = _make_project(client)
    d = _upload_dxf(client, pid, tmp_path)

    resp = client.get(f"/files/drawing/{d['artifact_id']}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    cd = resp.headers["content-disposition"]
    assert cd.startswith("inline")
    assert "filename*=UTF-8''" in cd  # 中文名走 RFC 5987


def test_取DXF原文件_attachment下载(client: TestClient, tmp_path) -> None:
    pid = _make_project(client)
    d = _upload_dxf(client, pid, tmp_path)

    resp = client.get(f"/files/drawing/{d['artifact_id']}?disposition=attachment")

    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")


def test_DXF转PDF预览_返回PDF字节(client: TestClient, tmp_path) -> None:
    pid = _make_project(client)
    d = _upload_dxf(client, pid, tmp_path)

    resp = client.get(f"/files/drawing/{d['artifact_id']}?format=pdf&disposition=inline")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:5] == b"%PDF-"
    assert resp.headers["content-disposition"].startswith("inline")


def test_DXF转PDF_图元超限413(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GYT_DRAWING_RENDER_MAX_ENTITIES", "1")  # 最小合法上限,plain 图必超
    get_settings.cache_clear()
    pid = _make_project(client)
    d = _upload_dxf(client, pid, tmp_path)

    resp = client.get(f"/files/drawing/{d['artifact_id']}?format=pdf")

    assert resp.status_code == 413
    assert resp.json()["error_code"] == "FILE_TOO_LARGE"


def test_PDF图纸_原样预览与导出都给PDF(client: TestClient) -> None:
    pid = _make_project(client)
    d = _upload_pdf_drawing(client, pid)

    orig = client.get(f"/files/drawing/{d['artifact_id']}")
    assert orig.status_code == 200
    assert orig.headers["content-type"] == "application/pdf"
    assert orig.content == _PDF_BYTES

    # PDF 图纸「导出 PDF」= 原样给它(不再转)。
    as_pdf = client.get(f"/files/drawing/{d['artifact_id']}?format=pdf")
    assert as_pdf.status_code == 200
    assert as_pdf.content == _PDF_BYTES


def test_图纸编号非法_404(client: TestClient) -> None:
    resp = client.get("/files/drawing/not-a-hex-id")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


def test_合法id但不是在册图纸_404(client: TestClient) -> None:
    # 归属校验:随便一个照片产物,不是登记在册的图纸 → 不许掏它的文件。
    photo_id = artifacts.register(b"jpgbytes", kind=ArtifactKind.PHOTO, original_name="x.jpg")
    resp = client.get(f"/files/drawing/{photo_id}")
    assert resp.status_code == 404


def test_删图后再取_404(client: TestClient, tmp_path) -> None:
    pid = _make_project(client)
    d = _upload_dxf(client, pid, tmp_path)
    client.delete(f"/projects/{pid}/drawings/{d['drawing_id']}")

    resp = client.get(f"/files/drawing/{d['artifact_id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 资料文件:预览 / 下载 / 归属校验 / 路径穿越
# ---------------------------------------------------------------------------


def test_取全局规范_inline预览(client: TestClient) -> None:
    project_fs.land_doc("global", "regulation", "安全生产规范.pdf", _PDF_BYTES)

    resp = client.get("/files/doc?scope=global&doc_type=regulation&filename=安全生产规范.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.content == _PDF_BYTES


def test_取全局规范_attachment下载(client: TestClient) -> None:
    project_fs.land_doc("global", "regulation", "安全生产规范.pdf", _PDF_BYTES)

    resp = client.get(
        "/files/doc?scope=global&doc_type=regulation&filename=安全生产规范.pdf&disposition=attachment"
    )
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")


def test_取项目任务书(client: TestClient) -> None:
    pid = _make_project(client)
    project_fs.land_doc("project", "task_book", "施工任务书.pdf", _PDF_BYTES, project_id=pid)

    resp = client.get(
        f"/files/doc?scope=project&doc_type=task_book&filename=施工任务书.pdf&project_id={pid}"
    )
    assert resp.status_code == 200
    assert resp.content == _PDF_BYTES


def test_资料不存在_404(client: TestClient) -> None:
    resp = client.get("/files/doc?scope=global&doc_type=regulation&filename=没有.pdf")
    assert resp.status_code == 404


def test_资料_跨项目取不到_404(client: TestClient) -> None:
    pid = _make_project(client)
    project_fs.land_doc("project", "regulation", "本项目规范.pdf", _PDF_BYTES, project_id=pid)
    other = client.post("/projects", json={"name": "别家", "code": "B1"}).json()["data"]["id"]

    # 用别的项目 id 去取这份规范 → 那个项目下没有 → 404,不串到别人的资料。
    resp = client.get(
        f"/files/doc?scope=project&doc_type=regulation&filename=本项目规范.pdf&project_id={other}"
    )
    assert resp.status_code == 404


def test_资料_项目不存在_404(client: TestClient) -> None:
    resp = client.get(
        "/files/doc?scope=project&doc_type=regulation&filename=x.pdf&project_id=no-such"
    )
    assert resp.status_code == 404


def test_资料_路径穿越_404不越界(client: TestClient) -> None:
    resp = client.get("/files/doc?scope=global&doc_type=regulation&filename=../../../../etc/passwd")
    assert resp.status_code == 404


def test_资料_缺参数_400(client: TestClient) -> None:
    assert client.get("/files/doc?scope=global&doc_type=regulation").status_code == 400
    assert client.get("/files/doc?scope=bad&doc_type=regulation&filename=x.pdf").status_code == 400
    # 项目作用域没给 project_id
    assert (
        client.get("/files/doc?scope=project&doc_type=regulation&filename=x.pdf").status_code == 400
    )
