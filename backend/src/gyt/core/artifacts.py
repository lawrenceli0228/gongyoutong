"""文件引用注册表:传引用不传值。

消息状态(LangGraph state)里只允许出现 32 位十六进制的 ``artifact_id`` 字符串,
图片 / 图纸 / docx 这类大对象一律落盘,靠 ``resolve()`` 现取现用。
这样既省 token,也避免把二进制塞进 LLM 上下文。

=============================================================================
原始文件名净化链路(防路径穿越 / 覆盖攻击)
=============================================================================

    original_name = "../../../etc/passwd.JPG"   <- 完全不可信的外部输入
             |
             |  (1) 只取 basename:反斜杠先归一成斜杠,再丢掉所有目录成分
             v
        "passwd.JPG"
             |
             |  (2) 只取扩展名并小写化
             v
           ".jpg"
             |
             |  (3) 字符集 [a-z0-9.] + 长度 <= 8;任一不满足 -> ext = ""
             v
           ".jpg"
             |
             |  (4) 白名单(图片 / 文档 / CAD);不在白名单 -> ext = ""
             v
           ".jpg"
             |
             |  (5) 落盘名 = artifact_id + ext
             |      原始文件名只进元数据 sidecar,永不参与拼路径
             v
    <artifacts_dir>/20260805/9f2c...e1.jpg      <- 只可能落在 artifacts_dir 内

反向查找(resolve / read_meta)同样不接受外部路径:
artifact_id 先过 ARTIFACT_ID_RE(纯 32 位 hex,天然不含 "/" "." ".." 与 glob 通配符),
再用 glob 在日期分目录里找 sidecar,拿到 ext 后才拼出真实文件路径。

删除(delete)有两拨调用方,2026-08-15 合流后并存 —— 别再照旧版注释以为只有一个:
  · webapp.py 的项目/图纸/资料删除编排(6 处);
  · attendance/cleanup.py 的凭证图留存清理。
两边语义要求不同却能共用一个函数,靠的是它**任何情况都不抛**:
编排那侧「一步找不到不该拖垮整体」,清理那侧「文件本来就没了 = 目标已达成」。
⚠️ 代价写明白:**格式非法的 id 也只是返回 False** —— 把文件名当 id 传进来这类
调用方 bug 会被静默吞掉。真要查这种,去看调用方日志,别指望这里报。
它同样只收 artifact_id、所有路径从 id 派生,不收任何外部路径。
=============================================================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from gyt.config import (
    ALLOWED_CAD_EXT,
    ALLOWED_DOC_EXT,
    ALLOWED_IMAGE_EXT,
    get_settings,
)

logger = logging.getLogger(__name__)

# --- 模块级常量:禁止在函数里散落魔法值 ---------------------------------------
ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 扩展名只允许小写字母、数字和点;长度含点不超过 8(".jpeg"=5, ".docx"=5 都够用)
_EXT_SAFE_RE = re.compile(r"^[a-z0-9.]+$")
_MAX_EXT_LEN = 8

# 落盘扩展名白名单。两点注意:
# 1. .dwg 故意不在其中——MVP 要求 DWG 离线预转成 DXF;
# 2. .json 也不在其中——保证正文文件永远不可能覆盖同名的元数据 sidecar。
_ALLOWED_EXT = (
    frozenset(ALLOWED_IMAGE_EXT) | frozenset(ALLOWED_DOC_EXT) | frozenset(ALLOWED_CAD_EXT)
)

_META_SUFFIX = ".json"
_TMP_SUFFIX = ".tmp"
_DATE_DIR_FMT = "%Y%m%d"
_ID_ECHO_LIMIT = 64  # 报错时最多回显多少字符的 id,防日志被超长输入刷屏


class ArtifactKind(str, Enum):
    """产物类型。取值等于名字的小写形式无关紧要,这里保持与名字一致的大写串。"""

    PHOTO = "PHOTO"
    DRAWING = "DRAWING"
    DOCUMENT = "DOCUMENT"
    REPORT = "REPORT"
    # 打卡凭证图(W7)。单列一类而不并进 PHOTO,是清理器的前提:
    # 留存策略只对考勤图生效(到期删图、行置 NULL),PHOTO 是巡检链的工地照片,
    # 混在一起清理器就分不清"谁能删"—— 它宁可不删,于是磁盘只进不出。
    ATTENDANCE = "ATTENDANCE"
    OTHER = "OTHER"


class ArtifactNotFound(Exception):
    """产物编号不合法,或者对应的文件 / 元数据不存在。"""


# --- 内部工具 ----------------------------------------------------------------


def _echo(value: object) -> str:
    """安全回显外部输入(截断),只用于异常与日志。"""
    return repr(value)[:_ID_ECHO_LIMIT]


def _safe_ext(original_name: str) -> str:
    """从不可信的原始文件名里提取一个可以安全落盘的扩展名。

    见文件顶部净化链路图。任何一步不满足都返回空串(即最终落盘文件无扩展名),
    而不是抛异常——注册本身仍应成功,类型信息由 ``kind`` 字段承载。
    """
    raw = str(original_name or "").replace("\\", "/").replace("\x00", "")
    base = PurePosixPath(raw).name  # (1) 丢掉所有目录成分,".." 会变成空串
    ext = PurePosixPath(base).suffix.lower()  # (2) 取扩展名并小写化
    if not ext or len(ext) > _MAX_EXT_LEN:  # (3a) 长度闸门
        return ""
    if not _EXT_SAFE_RE.fullmatch(ext):  # (3b) 字符集闸门
        return ""
    if ext not in _ALLOWED_EXT:  # (4) 白名单闸门
        logger.info("扩展名 %s 不在白名单,落盘时按无扩展名处理", ext)
        return ""
    return ext


def _normalize_kind(kind: ArtifactKind | str) -> str:
    """把 kind 收敛成合法字符串;认不出来就显式报错,不静默降级。"""
    if isinstance(kind, ArtifactKind):
        return kind.value
    try:
        return ArtifactKind(str(kind)).value
    except ValueError as exc:
        raise ValueError(f"未知的产物类型:{_echo(kind)}") from exc


def _load_bytes(data: bytes | Path) -> bytes:
    """把入参统一成字节串。入参本身不做任何修改。"""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, Path):
        try:
            return data.read_bytes()
        except OSError as exc:
            raise FileNotFoundError(f"源文件读不出来:{_echo(str(data))}") from exc
    raise TypeError(f"register 只接受 bytes 或 Path,收到 {type(data).__name__}")


def _write_atomic(path: Path, payload: bytes) -> None:
    """原子写:先写 .tmp 再 rename,避免读到写了一半的文件。"""
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    tmp.write_bytes(payload)
    tmp.replace(path)


def _validate_stored_ext(ext: str) -> str:
    """对 sidecar 里读出来的 ext 做二次校验(防元数据被手工篡改后拼出越权路径)。"""
    if not ext:
        return ""
    if len(ext) > _MAX_EXT_LEN or not _EXT_SAFE_RE.fullmatch(ext):
        raise ArtifactNotFound(f"产物元数据里的扩展名不合法:{_echo(ext)}")
    return ext


def _sidecar_path(artifact_id: str) -> Path:
    """校验 id 并在日期分目录里定位元数据 sidecar。"""
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise ArtifactNotFound(f"产物编号不合法:{_echo(artifact_id)}")
    # id 已确认是纯 32 位 hex:不含 "/"、".."、"*"、"?"、"[",拼 glob 模式是安全的。
    artifacts_dir = get_settings().artifacts_dir
    for candidate in sorted(artifacts_dir.glob(f"*/{artifact_id}{_META_SUFFIX}")):
        return candidate
    raise ArtifactNotFound(f"找不到这份产物:{_echo(artifact_id)}")


def _read_sidecar(sidecar: Path) -> dict[str, Any]:
    """读元数据并返回副本;读坏了显式报错,绝不返回半截数据。"""
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError 覆盖 JSONDecodeError
        raise ArtifactNotFound(f"产物元数据读不出来:{_echo(sidecar.name)}") from exc
    if not isinstance(meta, dict):
        raise ArtifactNotFound(f"产物元数据格式不对:{_echo(sidecar.name)}")
    return dict(meta)  # 返回新对象,调用方改不到磁盘内容


# --- 对外 API ----------------------------------------------------------------


def register(data: bytes | Path, *, kind: ArtifactKind, original_name: str) -> str:
    """登记一份产物,返回 32 位十六进制的 artifact_id。

    落盘位置: ``<artifacts_dir>/<YYYYMMDD>/<artifact_id><ext>``
    同目录另写 ``<artifact_id>.json`` 元数据 sidecar。
    文件名一律用 artifact_id,原始文件名只进元数据,绝不参与拼路径。
    """
    payload = _load_bytes(data)
    kind_value = _normalize_kind(kind)
    ext = _safe_ext(original_name)
    artifact_id = uuid4().hex  # uuid4().hex 天然是 32 位小写 hex,满足 ARTIFACT_ID_RE

    day_dir = get_settings().artifacts_dir / datetime.now(UTC).strftime(_DATE_DIR_FMT)
    day_dir.mkdir(parents=True, exist_ok=True)

    _write_atomic(day_dir / f"{artifact_id}{ext}", payload)
    meta: dict[str, Any] = {
        "id": artifact_id,
        "kind": kind_value,
        # 原始名只作展示用途(下载卡片、报告附件名),永不参与路径拼接。
        "original_name": str(original_name),
        "ext": ext,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_atomic(
        day_dir / f"{artifact_id}{_META_SUFFIX}",
        json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    logger.info("已登记产物 %s kind=%s size=%d", artifact_id, kind_value, len(payload))
    return artifact_id


def resolve(artifact_id: str) -> Path:
    """把 artifact_id 换成磁盘上的真实路径。id 不合法或文件缺失一律抛 ArtifactNotFound。"""
    sidecar = _sidecar_path(artifact_id)
    ext = _validate_stored_ext(str(_read_sidecar(sidecar).get("ext", "")))
    blob = sidecar.parent / f"{artifact_id}{ext}"
    if not blob.is_file():
        raise ArtifactNotFound(f"产物文件已丢失:{_echo(artifact_id)}")
    return blob


def read_meta(artifact_id: str) -> dict[str, Any]:
    """读取产物元数据(返回副本)。"""
    return _read_sidecar(_sidecar_path(artifact_id))


def delete(artifact_id: str) -> bool:
    """删除一份产物(正文 blob + sidecar)。删除是幂等的:id 不合法 / 找不到都返回 False,不抛。

    删图纸 / 删项目时清理注册表里那份字节副本用。故意不抛异常 —— 删除编排里一步找不到
    不该拖垮整体(库行已删、文件已删,产物副本残留只是占点盘,不是错误)。
    """
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.fullmatch(artifact_id):
        return False
    try:
        sidecar = _sidecar_path(artifact_id)
    except ArtifactNotFound:
        return False
    try:
        ext = _validate_stored_ext(str(_read_sidecar(sidecar).get("ext", "")))
    except ArtifactNotFound:
        ext = ""
    (sidecar.parent / f"{artifact_id}{ext}").unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    logger.info("已删除产物 %s", artifact_id)
    return True


__all__ = [
    "ARTIFACT_ID_RE",
    "ArtifactKind",
    "ArtifactNotFound",
    "delete",
    "read_meta",
    "register",
    "resolve",
]
