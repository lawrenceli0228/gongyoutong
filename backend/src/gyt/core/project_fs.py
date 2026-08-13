"""项目 / 全局文件的物理落地位 —— 人类可读的目录镜像(与 artifacts 注册表并存)。

契约 2 的**字节真相仍在 artifacts 注册表**(id 寻址,agent 侧唯一真相);这一层是
给人浏览 / 下载的镜像,目录会说话:

    data/projects/<id>/drawings/{plan,elevation,section}/<原名>.dxf
    data/projects/<id>/docs/{regulation,task_book}/<原名>.pdf
    data/global/docs/regulation/<原名>.pdf

纯文件系统逻辑,可脱离 HTTP 单测。上层(webapp)负责编排:
    land_drawing(...) → artifacts.register(落地路径) → db.projects.add_drawing(rel_path=...)

安全(照 core/artifacts.py 的净化取向):
  · 文件名一律只取 basename(丢掉所有目录成分,".." 归空),再落地;
  · project_id 过安全正则(纯字母数字 + 短横 / 下划线),挡住 "../" 之类;
  · 落地后再断言路径没跑出目标目录(双保险,防路径穿越)。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Final

from gyt.config import get_settings
from gyt.db.projects import VIEW_TYPES

# --- 作用域与文档类型(物理布局的权威取值,knowledge 入库侧也从这里取)--------------
SCOPE_GLOBAL: Final[str] = "global"
SCOPE_PROJECT: Final[str] = "project"
SCOPES: Final[tuple[str, ...]] = (SCOPE_GLOBAL, SCOPE_PROJECT)

DOC_REGULATION: Final[str] = "regulation"
DOC_TASK_BOOK: Final[str] = "task_book"
DOC_TYPES: Final[tuple[str, ...]] = (DOC_REGULATION, DOC_TASK_BOOK)

_DRAWINGS_SUBDIR: Final[str] = "drawings"
_DOCS_SUBDIR: Final[str] = "docs"
_TMP_SUFFIX: Final[str] = ".tmp"

# project_id 只允许字母数字 + 短横 + 下划线 —— 它要当目录名用,不许含分隔符 / ".."。
_PROJECT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def _project_root(project_id: str) -> Path:
    """定位 data/projects/<id>/。id 非法直接抛,别拿脏字符串去拼路径。"""
    if not _PROJECT_ID_RE.fullmatch(project_id or ""):
        raise ValueError(f"项目 id 不合法(只允许字母数字/短横/下划线):{project_id!r}")
    return get_settings().projects_dir / project_id


def _safe_name(filename: str) -> str:
    """从不可信文件名里取一个可安全落盘的 basename。含目录成分的一律剥掉,".." 归空即拒。

    与 artifacts._safe_ext 同源但更进一步:那里只要扩展名、落盘用 uuid;这里要**保留原名**
    (目录给人看),所以整名都得洗干净。中文名照留(PurePosixPath.name 不动非分隔符字符)。
    """
    raw = str(filename or "").replace("\\", "/").replace("\x00", "")
    name = PurePosixPath(raw).name  # 丢掉所有目录成分;"../../x" → "x";".." → ""
    if not name or name in (".", ".."):
        raise ValueError(f"文件名不合法:{filename!r}")
    return name


def _unique_dest(target_dir: Path, name: str) -> Path:
    """同名已存在就在扩展名前加「 (2)」「 (3)」…,不覆盖旧文件(旧 drawings 行还指着它)。"""
    dest = target_dir / name
    if not dest.exists():
        return dest
    pure = PurePosixPath(name)
    stem, suffix = pure.stem, pure.suffix
    i = 2
    while (cand := target_dir / f"{stem} ({i}){suffix}").exists():
        i += 1
    return cand


def _land(target_dir: Path, filename: str, data: bytes | Path) -> Path:
    """把字节原子落到 target_dir 下的安全文件名。返回落地后的绝对路径。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(target_dir, _safe_name(filename))
    # 双保险:落地路径的父目录必须正是目标目录(_safe_name 已剥分隔符,理论到不了,防残留穿越)。
    if dest.resolve().parent != target_dir.resolve():  # pragma: no cover
        raise ValueError(f"落地路径越界:{dest}")
    payload = data.read_bytes() if isinstance(data, Path) else bytes(data)
    tmp = dest.with_name(dest.name + _TMP_SUFFIX)
    try:
        tmp.write_bytes(payload)
        tmp.replace(dest)
    except BaseException:  # pragma: no cover — 写盘失败的清理路径,正常流程到不了
        tmp.unlink(missing_ok=True)  # 别把半截临时文件留在项目目录里
        raise
    return dest


# --- 对外 API ----------------------------------------------------------------


def ensure_project_tree(project_id: str) -> Path:
    """建 data/projects/<id>/ 下的空骨架(drawings/{平立剖} + docs/{规范,任务书}),返回项目根。

    幂等:目录已在就跳过。POST /projects 建项目时调一次,让用户一进目录就看见分类文件夹。
    """
    root = _project_root(project_id)
    for view_type in VIEW_TYPES:
        (root / _DRAWINGS_SUBDIR / view_type).mkdir(parents=True, exist_ok=True)
    for doc_type in DOC_TYPES:
        (root / _DOCS_SUBDIR / doc_type).mkdir(parents=True, exist_ok=True)
    return root


def land_drawing(project_id: str, view_type: str, filename: str, data: bytes | Path) -> Path:
    """把图纸落到 projects/<id>/drawings/<view_type>/<原名>,返回落地后的绝对路径。"""
    if view_type not in VIEW_TYPES:
        raise ValueError(f"view_type 不合法(平立剖只认 {VIEW_TYPES}):{view_type!r}")
    target = _project_root(project_id) / _DRAWINGS_SUBDIR / view_type
    return _land(target, filename, data)


def land_doc(
    scope: str,
    doc_type: str,
    filename: str,
    data: bytes | Path,
    project_id: str | None = None,
) -> Path:
    """把文档落到全局或项目 docs/ 下,返回落地后的绝对路径。

    全局(scope=global)只收规范:任务书必属某项目,给全局传任务书直接拒。
    项目(scope=project)须给 project_id。
    """
    if scope not in SCOPES:
        raise ValueError(f"scope 不合法(只认 {SCOPES}):{scope!r}")
    if doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type 不合法(只认 {DOC_TYPES}):{doc_type!r}")
    if scope == SCOPE_GLOBAL:
        if doc_type != DOC_REGULATION:
            raise ValueError("全局作用域只收规范(regulation);任务书必属某项目")
        target = get_settings().global_dir / _DOCS_SUBDIR / doc_type
    else:  # SCOPE_PROJECT
        if not project_id:
            raise ValueError("项目作用域必须给 project_id")
        target = _project_root(project_id) / _DOCS_SUBDIR / doc_type
    return _land(target, filename, data)


def project_rel_path(project_id: str, path: Path) -> str:
    """把落地绝对路径转成**相对项目根**的 POSIX 字符串,供 drawings.rel_path 存。

    例:.../data/projects/gyt-a3/drawings/plan/首层平面图.dxf → "drawings/plan/首层平面图.dxf"。
    path 不在该项目根下会抛 ValueError(relative_to 的行为)。
    """
    rel = path.resolve().relative_to(_project_root(project_id).resolve())
    return rel.as_posix()


__all__ = [
    "DOC_REGULATION",
    "DOC_TASK_BOOK",
    "DOC_TYPES",
    "SCOPE_GLOBAL",
    "SCOPE_PROJECT",
    "SCOPES",
    "ensure_project_tree",
    "land_doc",
    "land_drawing",
    "project_rel_path",
]
