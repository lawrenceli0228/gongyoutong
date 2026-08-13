"""产物注册表的单元测试。

除了往返正确性,重点是攻击性用例:路径穿越的原始文件名、非法 artifact_id、
超长 / 大小写 / 不在白名单的扩展名,以及被手工篡改的元数据 sidecar。
所有测试都跑在 conftest 的 _isolated_settings 隔离目录里,不碰真实 data/。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from gyt.config import get_settings
from gyt.core.artifacts import (
    ARTIFACT_ID_RE,
    ArtifactKind,
    ArtifactNotFound,
    delete,
    read_meta,
    register,
    resolve,
)

# 假的二进制载荷:PNG 魔数 + 一段 UTF-8 中文,确保二进制与非 ASCII 路径都被覆盖。
PAYLOAD = b"\x89PNG\r\n\x1a\n" + "这是一份假的照片字节流".encode()


def _sidecar_of(artifact_id: str) -> Path:
    """测试辅助:定位某个产物的元数据 sidecar。"""
    matches = list(get_settings().artifacts_dir.glob(f"*/{artifact_id}.json"))
    assert len(matches) == 1, f"应当有且只有一份 sidecar,实际 {matches}"
    return matches[0]


# --- 往返 --------------------------------------------------------------------


def test_往返_注册后能取回一模一样的内容():
    # Arrange / Act
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="工地照片.jpg")

    # Assert
    assert ARTIFACT_ID_RE.fullmatch(artifact_id), "artifact_id 必须是 32 位小写 hex"
    assert resolve(artifact_id).read_bytes() == PAYLOAD


def test_可以直接登记一个磁盘上的文件(tmp_path: Path):
    # Arrange
    source = tmp_path / "原始图纸.dxf"
    source.write_bytes(PAYLOAD)

    # Act
    artifact_id = register(source, kind=ArtifactKind.DRAWING, original_name=source.name)

    # Assert
    assert resolve(artifact_id).read_bytes() == PAYLOAD
    assert read_meta(artifact_id)["ext"] == ".dxf"


def test_两次登记得到不同的编号():
    first = register(PAYLOAD, kind=ArtifactKind.OTHER, original_name="a.txt")
    second = register(PAYLOAD, kind=ArtifactKind.OTHER, original_name="a.txt")

    assert first != second


def test_元数据字段齐全且正确():
    # Act
    artifact_id = register(PAYLOAD, kind=ArtifactKind.REPORT, original_name="日报.docx")
    meta = read_meta(artifact_id)

    # Assert
    assert meta["id"] == artifact_id
    assert meta["kind"] == "REPORT"
    assert meta["original_name"] == "日报.docx"
    assert meta["ext"] == ".docx"
    assert meta["size_bytes"] == len(PAYLOAD)
    assert meta["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert datetime.fromisoformat(meta["created_at"])  # ISO8601 可解析


def test_按日期分目录落盘且不留临时文件():
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="a.jpg")

    day_dir = resolve(artifact_id).parent
    assert day_dir.parent == get_settings().artifacts_dir
    assert len(day_dir.name) == 8 and day_dir.name.isdigit()
    assert list(day_dir.glob("*.tmp")) == [], "原子写之后不该残留 .tmp"


def test_read_meta_返回副本改不到磁盘():
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="a.jpg")

    meta = read_meta(artifact_id)
    meta["kind"] = "被篡改"

    assert read_meta(artifact_id)["kind"] == "PHOTO"


# --- 攻击面 1:原始文件名带路径穿越 -------------------------------------------


def test_原名带路径穿越时依然安全落盘():
    # Arrange:典型的路径穿越 payload
    evil_name = "../../../etc/passwd.jpg"

    # Act
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name=evil_name)
    path = resolve(artifact_id)

    # Assert:文件只可能落在 artifacts_dir 里,且文件名只由 artifact_id 决定
    artifacts_dir = get_settings().artifacts_dir.resolve()
    assert path.resolve().is_relative_to(artifacts_dir)
    assert ".." not in path.parts
    assert path.name == f"{artifact_id}.jpg"
    assert "passwd" not in path.name
    # 原始名只留在元数据里做展示,不参与任何路径拼接
    assert read_meta(artifact_id)["original_name"] == evil_name


def test_原名是纯路径片段时扩展名置空():
    artifact_id = register(PAYLOAD, kind=ArtifactKind.OTHER, original_name="../..")

    assert resolve(artifact_id).name == artifact_id


# --- 攻击面 2:扩展名净化 -----------------------------------------------------


@pytest.mark.parametrize(
    ("original_name", "expected_ext"),
    [
        ("照片.jpg", ".jpg"),
        ("PHOTO.JPG", ".jpg"),  # 大小写:统一小写化
        ("扫描件.JPEG", ".jpeg"),
        ("图纸.DXF", ".dxf"),
        ("报告.docx", ".docx"),
        (r"C:\Users\gongyou\..\现场.PNG", ".png"),  # Windows 反斜杠也要归一
        ("../../../etc/passwd.jpg", ".jpg"),
        ("没有扩展名", ""),  # 无扩展名
        ("", ""),  # 空文件名
        (".jpg", ""),  # 隐藏文件,不算扩展名
        ("木马.superlongext", ""),  # 超长扩展名(>8)
        ("木马.exe", ""),  # 不在白名单
        ("图纸.dwg", ""),  # DWG 故意不在白名单,必须离线预转 DXF
        ("压缩包.tar.gz", ""),  # 只看最后一段,且不在白名单
        ("怪名.j-p-g", ""),  # 字符集越界(含 "-")
        ("怪名.j pg", ""),  # 字符集越界(含空格)
    ],
)
def test_扩展名净化链路(original_name: str, expected_ext: str):
    # Act
    artifact_id = register(PAYLOAD, kind=ArtifactKind.OTHER, original_name=original_name)

    # Assert:元数据与真实落盘名都必须是净化后的扩展名
    assert read_meta(artifact_id)["ext"] == expected_ext
    assert resolve(artifact_id).name == f"{artifact_id}{expected_ext}"
    assert resolve(artifact_id).read_bytes() == PAYLOAD


# --- 攻击面 3:非法 artifact_id ------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../evil",  # 路径穿越
        "",  # 空串
        "/etc/passwd",  # 绝对路径
        "..",
        "0" * 31 + "/",  # 带斜杠
        "0" * 31,  # 太短
        "0" * 33,  # 太长
        "A" * 32,  # 大写 hex 不接受
        "g" * 32,  # 非 hex 字符
        "0" * 32 + "\n",  # 尾部换行(fullmatch 必须拦住)
        "*" * 32,  # glob 通配符
        None,  # 非字符串
        12345,  # 非字符串
    ],
)
def test_非法编号一律拒绝(bad_id):
    with pytest.raises(ArtifactNotFound):
        resolve(bad_id)
    with pytest.raises(ArtifactNotFound):
        read_meta(bad_id)


def test_合法但不存在的编号也拒绝():
    with pytest.raises(ArtifactNotFound):
        resolve("0" * 32)


def test_文件被删掉后_resolve_报找不到():
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="a.jpg")
    resolve(artifact_id).unlink()

    with pytest.raises(ArtifactNotFound):
        resolve(artifact_id)


# --- 攻击面 4:元数据被篡改 ---------------------------------------------------


def test_元数据里的扩展名被篡改成路径穿越时拒绝():
    # Arrange
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="a.jpg")
    sidecar = _sidecar_of(artifact_id)
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    meta["ext"] = "/../../etc/passwd"
    sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # Act / Assert:二次校验必须拦住
    with pytest.raises(ArtifactNotFound):
        resolve(artifact_id)


def test_元数据损坏时报错而不是返回半截数据():
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="a.jpg")
    _sidecar_of(artifact_id).write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(ArtifactNotFound):
        read_meta(artifact_id)


def test_元数据不是对象时报错():
    artifact_id = register(PAYLOAD, kind=ArtifactKind.PHOTO, original_name="a.jpg")
    _sidecar_of(artifact_id).write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ArtifactNotFound):
        read_meta(artifact_id)


# --- 入参校验 ----------------------------------------------------------------


def test_未知产物类型显式报错():
    with pytest.raises(ValueError):
        register(PAYLOAD, kind="不存在的类型", original_name="a.jpg")


def test_源文件不存在时显式报错(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        register(tmp_path / "根本没有这个文件.jpg", kind=ArtifactKind.PHOTO, original_name="a.jpg")


def test_不支持的入参类型报错():
    with pytest.raises(TypeError):
        register("我是字符串不是字节", kind=ArtifactKind.OTHER, original_name="a.txt")


def test_产物类型也接受字符串写法():
    artifact_id = register(PAYLOAD, kind="PHOTO", original_name="a.jpg")

    assert read_meta(artifact_id)["kind"] == "PHOTO"


# --- 删除 --------------------------------------------------------------------


def test_delete删掉正文与sidecar且幂等() -> None:
    artifact_id = register(PAYLOAD, kind=ArtifactKind.DRAWING, original_name="a.dxf")
    assert resolve(artifact_id).exists()

    assert delete(artifact_id) is True
    with pytest.raises(ArtifactNotFound):
        resolve(artifact_id)
    # 幂等:再删返回 False(已经不在),不抛
    assert delete(artifact_id) is False


def test_delete非法id返回False不抛() -> None:
    assert delete("不是32位hex") is False
    assert delete("a" * 32) is False  # 形状对但查无此物
