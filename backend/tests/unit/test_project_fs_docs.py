"""project_fs 新增能力单测:文档定位(resolve_doc)+「不可检索」标记 + 路径穿越拒。

纯文件系统逻辑,全落 tmp_path(conftest 的 _isolated_settings),不联网。
"""

from __future__ import annotations

from gyt.core import project_fs
from gyt.db import projects as db


def test_resolve_doc_全局规范能取回() -> None:
    project_fs.land_doc("global", "regulation", "GB50016.pdf", b"%PDF-1")
    path = project_fs.resolve_doc("global", "regulation", "GB50016.pdf")
    assert path is not None and path.read_bytes() == b"%PDF-1"


def test_resolve_doc_项目任务书能取回() -> None:
    db.create_project("a3", "测试项目")
    project_fs.ensure_project_tree("a3")
    project_fs.land_doc("project", "task_book", "任务书.pdf", b"%PDF-22", project_id="a3")
    path = project_fs.resolve_doc("project", "task_book", "任务书.pdf", project_id="a3")
    assert path is not None and path.read_bytes() == b"%PDF-22"


def test_resolve_doc_不存在返回None() -> None:
    assert project_fs.resolve_doc("global", "regulation", "根本没有.pdf") is None


def test_resolve_doc_路径穿越只落在docs内() -> None:
    # 传越权文件名:_safe_name 只取 basename,拼出的目标落在 regulation 目录内、查无 → None。
    assert project_fs.resolve_doc("global", "regulation", "../../../../etc/passwd") is None


def test_不可检索标记_打上又查得到_删文档一并清() -> None:
    project_fs.land_doc("global", "regulation", "扫描件.pdf", b"%PDF")
    assert project_fs.doc_is_unsearchable("global", "regulation", "扫描件.pdf") is False

    project_fs.mark_doc_unsearchable("global", "regulation", "扫描件.pdf")
    assert project_fs.doc_is_unsearchable("global", "regulation", "扫描件.pdf") is True

    # 标记是 sidecar,不该被列进 list_docs(否则界面会多出一条幽灵)。
    names = [e.filename for e in project_fs.list_docs()]
    assert names == ["扫描件.pdf"]

    project_fs.delete_doc("global", "regulation", "扫描件.pdf")
    assert project_fs.doc_is_unsearchable("global", "regulation", "扫描件.pdf") is False
