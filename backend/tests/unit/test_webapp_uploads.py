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
from gyt.core import artifacts, project_fs
from gyt.core.doc_no import DocKind, new_doc_no
from gyt.db import hazards
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

    resp = client.post(f"/projects/{pid}/drawings", files=_dxf_files(), data={"view_type": "3d"})

    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_INPUT"


def test_传图_非dxf非pdf_415(client: TestClient) -> None:
    pid = _make_project(client)

    resp = client.post(
        f"/projects/{pid}/drawings",
        files={"file": ("图纸.dwg", b"whatever", "application/octet-stream")},
        data={"view_type": "plan"},
    )

    assert resp.status_code == 415
    assert resp.json()["error_code"] == "FILE_UNSUPPORTED"


def test_传图_PDF图纸也收并入库(client: TestClient) -> None:
    # 上传口只按后缀+大小放行,不解析;PDF 图纸走和 DXF 同一条落地→注册→入库的路。
    pid = _make_project(client)
    pdf_bytes = b"%PDF-1.4\n%mock pdf drawing\n"

    resp = client.post(
        f"/projects/{pid}/drawings",
        files={"file": ("首层平面图.pdf", pdf_bytes, "application/pdf")},
        data={"view_type": "plan", "title": "首层平面图"},
    )

    assert resp.status_code == 201
    data = resp.json()["data"]
    landed = get_settings().projects_dir / pid / "drawings" / "plan" / "首层平面图.pdf"
    assert landed.read_bytes() == pdf_bytes
    assert data["rel_path"] == "drawings/plan/首层平面图.pdf"
    row = db.get_drawing_by_id(data["drawing_id"])
    assert row is not None and row.view_type == "plan" and row.title == "首层平面图"


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
    resp = client.post("/projects/no-such/drawings", files=_dxf_files(), data={"view_type": "plan"})

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 传规范 / 任务书(/docs、/projects/{id}/docs)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ingest(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """挡掉真入库(会加载 2.2GB BGE-M3),只记录调用参数、返回假 chunk 数。

    上传改成异步后:同步预检 pdf_has_text(假 PDF 会让 pypdf 报错)也要一并打桩成「有文字」,
    否则走不到后台入库那步。ingest_document 由后台任务调用(TestClient 会跑完 background)。
    """
    calls: list[dict] = []

    def _fake(path, *, scope: str, doc_type: str, project_id: str = "") -> int:
        calls.append({"scope": scope, "doc_type": doc_type, "project_id": project_id})
        return 7

    monkeypatch.setattr("gyt.agents.knowledge.ingest.pdf_has_text", lambda _p: True)
    monkeypatch.setattr("gyt.agents.knowledge.ingest.ingest_document", _fake)
    return calls


def _pdf_files(name: str = "GB50016.pdf", content: bytes = b"%PDF-1.4 fake"):
    return {"file": (name, content, "application/pdf")}


def test_传全局规范_落地并转后台入库(client: TestClient, fake_ingest: list[dict]) -> None:
    resp = client.post("/docs", files=_pdf_files(), data={"doc_type": "regulation"})

    # 异步:立刻回 202「正在入库」,文件已落地;embedding 在后台跑(TestClient 会跑完 background)。
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["scope"] == "global"
    assert data["status"] == "ingesting"
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

    assert resp.status_code == 202
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


# ---------------------------------------------------------------------------
# 资料库总览 GET /library
# ---------------------------------------------------------------------------


def test_library总览_打平项目图纸与全局项目文档(client: TestClient) -> None:
    pid = _make_project(client)  # a3
    client.post(
        f"/projects/{pid}/drawings",
        files=_dxf_files("平面.dxf"),
        data={"view_type": "plan", "title": "平面"},
    )
    # 文档直接落地(绕过 Chroma 入库,这里只验总览拼装),模拟已归档的规范/任务书。
    project_fs.land_doc("global", "regulation", "GB50016.pdf", b"%PDF-1")
    project_fs.land_doc("project", "regulation", "本项目规范.pdf", b"%PDF-22", project_id=pid)

    resp = client.get("/library")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [p["id"] for p in data["projects"]] == [pid]

    assert len(data["drawings"]) == 1
    d = data["drawings"][0]
    assert d["project_id"] == pid
    assert d["project_name"] == "测试项目"  # 已按 project_id 补上人类可读名
    assert d["view_type"] == "plan"

    docs = {x["filename"]: x for x in data["docs"]}
    assert docs["GB50016.pdf"]["scope"] == "global"
    assert docs["GB50016.pdf"]["project_name"] is None
    assert docs["本项目规范.pdf"]["scope"] == "project"
    assert docs["本项目规范.pdf"]["project_name"] == "测试项目"
    assert docs["本项目规范.pdf"]["size_bytes"] == len(b"%PDF-22")


def test_library总览_空库三样皆空(client: TestClient) -> None:
    resp = client.get("/library")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["projects"] == []
    assert data["drawings"] == []
    assert data["docs"] == []


# ---------------------------------------------------------------------------
# 删除:图纸 / 资料 / 项目(不可逆,四处一致性)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ingest_delete(monkeypatch: pytest.MonkeyPatch) -> list:
    """挡掉真删向量(会加载 BGE-M3),只记录调用、返回固定块数。"""
    calls: list = []

    def _fake_doc(source, *, scope, project_id="", vectorstore=None) -> int:
        calls.append(("doc", source, scope, project_id))
        return 3

    def _fake_proj(project_id, *, vectorstore=None) -> int:
        calls.append(("proj", project_id))
        return 5

    monkeypatch.setattr("gyt.agents.knowledge.ingest.delete_document", _fake_doc)
    monkeypatch.setattr("gyt.agents.knowledge.ingest.delete_project_documents", _fake_proj)
    return calls


def _upload_drawing(client: TestClient, pid: str, name: str = "平面.dxf") -> dict:
    resp = client.post(
        f"/projects/{pid}/drawings",
        files=_dxf_files(name),
        data={"view_type": "plan", "title": "平面"},
    )
    assert resp.status_code == 201
    return resp.json()["data"]


def test_删图纸_库文件产物一起清(client: TestClient) -> None:
    pid = _make_project(client)
    d = _upload_drawing(client, pid)
    landed = get_settings().projects_dir / pid / "drawings" / "plan" / "平面.dxf"
    assert landed.exists()

    resp = client.delete(f"/projects/{pid}/drawings/{d['drawing_id']}")

    assert resp.status_code == 200
    assert db.get_drawing_by_id(d["drawing_id"]) is None  # 库行没了
    assert not landed.exists()  # 物理文件没了
    with pytest.raises(artifacts.ArtifactNotFound):  # 产物没了
        artifacts.resolve(d["artifact_id"])


def test_删图纸_别的项目id_404且不误删(client: TestClient) -> None:
    pid = _make_project(client)
    d = _upload_drawing(client, pid)

    resp = client.delete(f"/projects/no-such/drawings/{d['drawing_id']}")

    assert resp.status_code == 404
    assert db.get_drawing_by_id(d["drawing_id"]) is not None  # 没被误删


def test_删图纸_编号非数字_400(client: TestClient) -> None:
    pid = _make_project(client)
    resp = client.delete(f"/projects/{pid}/drawings/abc")
    assert resp.status_code == 400


def test_删全局规范_清文件与向量(client: TestClient, fake_ingest_delete: list) -> None:
    project_fs.land_doc("global", "regulation", "GB50016.pdf", b"%PDF")

    resp = client.request(
        "DELETE", "/docs", json={"doc_type": "regulation", "filename": "GB50016.pdf"}
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["chunks"] == 3
    assert ("doc", "GB50016.pdf", "global", "") in fake_ingest_delete
    assert not (get_settings().global_dir / "docs" / "regulation" / "GB50016.pdf").exists()


def test_删项目规范(client: TestClient, fake_ingest_delete: list) -> None:
    pid = _make_project(client)
    project_fs.land_doc("project", "regulation", "本项目规范.pdf", b"%PDF", project_id=pid)

    resp = client.request(
        "DELETE",
        f"/projects/{pid}/docs",
        json={"doc_type": "regulation", "filename": "本项目规范.pdf"},
    )

    assert resp.status_code == 200
    assert ("doc", "本项目规范.pdf", "project", pid) in fake_ingest_delete


def test_删资料_文件与向量都不在_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gyt.agents.knowledge.ingest.delete_document", lambda *a, **k: 0)
    resp = client.request(
        "DELETE", "/docs", json={"doc_type": "regulation", "filename": "根本没有.pdf"}
    )
    assert resp.status_code == 404


def test_删资料_缺filename_400(client: TestClient, fake_ingest_delete: list) -> None:
    resp = client.request("DELETE", "/docs", json={"doc_type": "regulation"})
    assert resp.status_code == 400


def test_删项目_级联清图纸文档目录(client: TestClient, fake_ingest_delete: list) -> None:
    pid = _make_project(client)
    d = _upload_drawing(client, pid)
    root = get_settings().projects_dir / pid
    assert root.is_dir()

    resp = client.delete(f"/projects/{pid}")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["drawings"] == 1
    assert body["chunks"] == 5  # fake_proj 返回 5
    assert db.get_project(pid) is None
    assert not root.exists()  # 整个项目目录没了
    with pytest.raises(artifacts.ArtifactNotFound):
        artifacts.resolve(d["artifact_id"])
    assert ("proj", pid) in fake_ingest_delete


def test_删项目_不存在_404(client: TestClient, fake_ingest_delete: list) -> None:
    resp = client.delete("/projects/no-such")
    assert resp.status_code == 404


def _hazard_of(project_id: str, *, sha: str = "sha-1", item: str = "未戴安全帽") -> str:
    """在某个项目下登记一条隐患,返回编号(编号由 core/doc_no.py 现摇,不手拼)。"""
    return hazards.create(
        hazard_no=new_doc_no(DocKind.HAZARD),
        project_id=project_id,
        photo_sha256=sha,
        photo_id="photo-x",
        item=item,
        severity="一般",
        grade=hazards.GRADE_NORMAL,
        grading_version="1",
        needs_grading=False,
    ).row.hazard_no


def test_删项目_名下隐患转未归属而不是跟着项目消失(
    client: TestClient, fake_ingest_delete: list
) -> None:
    """🔴 隐患挂着已签发的法律文书与证据链,**删项目不许把它们一起带走**
    (``db/hazards.py`` 的 ``delete_pending`` 注释:确认过的隐患不许被删,证据链不能凭一次
    点击消失 —— 而删项目正是一次点击)。

    也不许原样留着:``hazards.project_id`` 是 ``NOT NULL DEFAULT ''`` 且**没有外键**,
    项目一删它就指向一个不存在的项目 —— 既不在「未归属」桶里(它非空)、也不在项目列表里
    (项目没了),**彻底找不到**,而库里它还是「在办」。所以要摘成空串(D6 那个可见的桶)。
    """
    pid = _make_project(client)
    别家 = _hazard_of("other-site", sha="sha-别家")
    在办 = _hazard_of(pid, sha="sha-在办")
    hazards.confirm(在办)
    hazards.mark_notified(
        在办,
        "2026-12-31",
        expected_grade=hazards.GRADE_NORMAL,
        docs=[hazards.DocDraft("notice", "GYT-TZ-9001", artifact_id="a" * 32)],
    )

    resp = client.delete(f"/projects/{pid}")

    assert resp.status_code == 200
    assert db.get_project(pid) is None
    assert resp.json()["data"]["hazards"] == 1
    assert "未归属" in resp.json()["user_msg"]  # 得让人知道那批文书没跟着没了
    row = hazards.fetch(在办)
    assert row is not None
    assert row.project_id == ""  # 摘到未归属那一桶,监理还看得见、还能接着处置
    assert row.status == hazards.STATUS_NOTIFIED  # 状态一点没动
    assert [d.doc_no for d in hazards.docs_of([在办])] == ["GYT-TZ-9001"]  # 文书还挂着
    assert [r.hazard_no for r in hazards.list_rows(project_id="")] == [在办]
    assert [r.hazard_no for r in hazards.list_rows(project_id="other-site")] == [别家]


def test_删项目_隐患摘不动时整个删除作废且什么都没动(
    client: TestClient, fake_ingest_delete: list
) -> None:
    """极小概率:同一张照片、同一个违规项既在这个工地下登记过、又在未归属那堆里躺着一条,
    摘过去就撞幂等键 ``UNIQUE (project_id, photo_sha256, item)``。

    这时宁可**整个删除作废** —— 摘一半剩一半就是留下一批找不到的隐患,而界面上显示
    「项目已删除」。所以摘隐患必须是删除编排的**第一步**:这条用例连图纸都要断言还在,
    那正是"排在最前面"这件事的判据(挪到后面的话,这里的图纸文件已经没了)。
    """
    pid = _make_project(client)
    d = _upload_drawing(client, pid)
    带项目的 = _hazard_of(pid, sha="同一张照片")
    _hazard_of("", sha="同一张照片")  # 未归属那堆里已经有同款

    resp = client.delete(f"/projects/{pid}")

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "CONFLICT"
    assert "没删" in resp.json()["user_msg"]  # 明确告诉人:这次什么都没发生
    # 一样都没动:项目还在、图纸行与文件还在、产物还在、隐患还挂在原项目上
    assert db.get_project(pid) is not None
    assert db.get_drawing_by_id(d["drawing_id"]) is not None
    assert artifacts.resolve(d["artifact_id"]).is_file()
    assert (get_settings().projects_dir / pid).is_dir()
    row = hazards.fetch(带项目的)
    assert row is not None
    assert row.project_id == pid


# ---------------------------------------------------------------------------
# 入库健壮性:失败 / 抽不出文字都要回滚,不留搜不到的幽灵文件
# ---------------------------------------------------------------------------


def test_传文档_后台入库失败_回滚(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 异步:预检过(有文字),但后台 embedding 炸了 → 客户端先拿 202,后台回滚落地文件。
    # TestClient 会跑完 background,所以回滚在断言时已完成。
    monkeypatch.setattr("gyt.agents.knowledge.ingest.pdf_has_text", lambda _p: True)

    def _boom(*a, **k):
        raise RuntimeError("embedding 炸了")

    monkeypatch.setattr("gyt.agents.knowledge.ingest.ingest_document", _boom)

    resp = client.post("/docs", files=_pdf_files("坏了.pdf"), data={"doc_type": "regulation"})

    assert resp.status_code == 202  # 先收下
    # 后台入库失败 → 回滚:落地文件没留下(不留搜不到的幽灵)
    assert not (get_settings().global_dir / "docs" / "regulation" / "坏了.pdf").exists()


def test_传文档_抽不出文字_归档但不入检索(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 资料上传策略(改判):扫描件/无文字层 PDF **不再拒** —— 保留归档、能预览,
    # 只是打「不可检索」标记、不进检索、不做 OCR、不伪造结果。
    monkeypatch.setattr("gyt.agents.knowledge.ingest.pdf_has_text", lambda _p: False)

    def _boom(*a, **k):  # 不该走后台入库(无文字层直接归档)
        raise AssertionError("无文字层不应触发 embedding 入库")

    monkeypatch.setattr("gyt.agents.knowledge.ingest.ingest_document", _boom)

    resp = client.post("/docs", files=_pdf_files("扫描件.pdf"), data={"doc_type": "regulation"})

    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["status"] == "unsearchable"
    assert "OCR" in resp.json()["user_msg"]
    # 文件留着(能预览),并打了「不可检索」标记
    assert (get_settings().global_dir / "docs" / "regulation" / "扫描件.pdf").exists()
    assert project_fs.doc_is_unsearchable("global", "regulation", "扫描件.pdf")


def test_library_无文字层文档状态为unsearchable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 无文字层上传后,资料库该把状态标成 unsearchable(而不是永远「入库中」)。
    monkeypatch.setattr("gyt.agents.knowledge.ingest.pdf_has_text", lambda _p: False)
    monkeypatch.setattr("gyt.agents.knowledge.ingest.ingest_document", lambda *a, **k: 0)
    client.post("/docs", files=_pdf_files("扫描件.pdf"), data={"doc_type": "regulation"})

    resp = client.get("/library")

    docs = {d["filename"]: d for d in resp.json()["data"]["docs"]}
    assert docs["扫描件.pdf"]["status"] == "unsearchable"
    assert docs["扫描件.pdf"]["chunks"] == 0
