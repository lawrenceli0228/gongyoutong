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
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple

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
# 「可预览、暂不可检索」标记 sidecar 的后缀(无文字层 PDF:扫描件 / 文字转曲的 CAD 打印件)。
# 落在文档旁边、名为 ``<原名>.pdf.gytnosearch``:上传时若抽不到文字层就打这个标记,
# 资料库据它把状态显示成「不可检索(OCR 待支持)」而不是永远「入库中」。
# **不做 OCR、不伪造检索结果**,只是把「归档了但搜不到」这个事实显式记下来。
_NOSEARCH_SUFFIX: Final[str] = ".gytnosearch"

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


# --- 文档浏览(docs 不进 SQLite,靠扫描目录镜像列出)---------------------------


class DocEntry(NamedTuple):
    """一份落地文档的浏览信息(规范 / 任务书)。

    图纸有 drawings 表可查,文档没有 —— 入的是 Chroma 向量库(供检索),字节镜像落在
    docs/ 目录下。要"看到所有资料"就直接读这层目录镜像,不再为浏览另立一张索引表。
    """

    scope: str  # SCOPE_GLOBAL / SCOPE_PROJECT
    project_id: str | None
    doc_type: str  # DOC_TYPES 之一
    filename: str
    rel_path: str  # 相对 data_dir 的 POSIX 路径,给人浏览
    size_bytes: int
    modified_at: str  # 文件 mtime 的本地 ISO 时间戳


def _iter_doc_files(dir_path: Path) -> Iterator[Path]:
    """列一个 doc 目录下的正式文件(跳过落地中途的 .tmp 与状态 sidecar;目录不存在即空)。"""
    if not dir_path.is_dir():
        return
    for f in sorted(dir_path.iterdir()):
        if f.is_file() and f.suffix not in (_TMP_SUFFIX, _NOSEARCH_SUFFIX):
            yield f


def _doc_entry(f: Path, scope: str, project_id: str | None, doc_type: str) -> DocEntry:
    stat = f.stat()
    rel = f.resolve().relative_to(get_settings().data_dir.resolve()).as_posix()
    modified = datetime.fromtimestamp(stat.st_mtime, UTC).astimezone().isoformat(timespec="seconds")
    return DocEntry(
        scope=scope,
        project_id=project_id,
        doc_type=doc_type,
        filename=f.name,
        rel_path=rel,
        size_bytes=stat.st_size,
        modified_at=modified,
    )


def list_docs(project_id: str | None = None) -> list[DocEntry]:
    """扫描落地的文档镜像,列出规范 / 任务书。

    project_id=None:含全局规范 + 所有项目的文档;给定 project_id:只列该项目的文档
    (不含全局)。纯读目录,和 db.list_drawings 一起喂给 webapp 的 /library,拼出「所有
    项目的图纸 + 规范」总览。
    """
    settings = get_settings()
    entries: list[DocEntry] = []

    # 全局规范(只在不限定项目时纳入)
    if project_id is None:
        global_docs = settings.global_dir / _DOCS_SUBDIR
        for doc_type in DOC_TYPES:
            for f in _iter_doc_files(global_docs / doc_type):
                entries.append(_doc_entry(f, SCOPE_GLOBAL, None, doc_type))

    # 项目文档
    if project_id is not None:
        pids = [project_id] if _project_root(project_id).is_dir() else []
    else:
        projects_root = settings.projects_dir
        pids = (
            sorted(p.name for p in projects_root.iterdir() if p.is_dir())
            if projects_root.is_dir()
            else []
        )
    for pid in pids:
        docs_root = _project_root(pid) / _DOCS_SUBDIR
        for doc_type in DOC_TYPES:
            for f in _iter_doc_files(docs_root / doc_type):
                entries.append(_doc_entry(f, SCOPE_PROJECT, pid, doc_type))

    return entries


# --- 文档定位(供文件内容端点按「作用域 + 类型 + 安全 basename」取回镜像文件)----------
# 与落地 / 删除同一条路径重建逻辑:绝不接受外部传来的绝对路径,一律由校验过的
# scope + doc_type + safe basename(+ 校验过的 project_id)拼出目标,防路径穿越。


def _doc_file_path(scope: str, doc_type: str, filename: str, project_id: str | None) -> Path:
    """按落地位重建一份文档镜像的绝对路径(不检查是否存在)。参数非法直接抛。"""
    if scope not in SCOPES:
        raise ValueError(f"scope 不合法(只认 {SCOPES}):{scope!r}")
    if doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type 不合法(只认 {DOC_TYPES}):{doc_type!r}")
    name = _safe_name(filename)  # 剥掉所有目录成分,".." 归空即拒
    if scope == SCOPE_GLOBAL:
        return get_settings().global_dir / _DOCS_SUBDIR / doc_type / name
    if not project_id:
        raise ValueError("项目作用域必须给 project_id")
    return _project_root(project_id) / _DOCS_SUBDIR / doc_type / name


def resolve_doc(
    scope: str, doc_type: str, filename: str, project_id: str | None = None
) -> Path | None:
    """把「作用域 + 类型 + 文件名」解析成落地镜像的绝对路径;文件不存在返回 None。

    文件内容端点(webapp)用它取回 PDF 字节做预览/下载。**只吃校验过的标识,不吃路径** ——
    路径由本层用安全 basename 重建,外部即便传 ``../../etc/passwd`` 也只会落在 docs 目录内、
    大概率 None。参数不合法(scope/doc_type 非法、项目作用域缺 project_id)照旧抛 ValueError。
    """
    target = _doc_file_path(scope, doc_type, filename, project_id)
    return target if target.is_file() else None


# --- 「可预览、暂不可检索」标记(无文字层 PDF)---------------------------------
# 只记一个事实:这份 PDF 抽不到文字层(扫描件 / 文字转曲),归了档、能预览,但暂不进检索。
# **不做 OCR、不改文件本身**,仅落一个空 sidecar,供资料库把状态显示对。


def mark_doc_unsearchable(
    scope: str, doc_type: str, filename: str, project_id: str | None = None
) -> None:
    """给一份已落地的无文字层 PDF 打「不可检索」标记(幂等:重复打只是覆盖同一个空文件)。"""
    marker = _doc_file_path(scope, doc_type, filename, project_id).with_name(
        _safe_name(filename) + _NOSEARCH_SUFFIX
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"")


def doc_is_unsearchable(
    scope: str, doc_type: str, filename: str, project_id: str | None = None
) -> bool:
    """这份文档是不是被标了「不可检索」(无文字层)。标记不在即 False。"""
    marker = _doc_file_path(scope, doc_type, filename, project_id).with_name(
        _safe_name(filename) + _NOSEARCH_SUFFIX
    )
    return marker.is_file()


# --- 删除(镜像文件 / 整个项目目录)------------------------------------------
# 一律用「作用域/类型 + 安全 basename」或「校验后的项目相对路径」重建目标,
# 绝不拿外部传来的路径直接删 —— 删除比落地更怕路径穿越。


def delete_doc(scope: str, doc_type: str, filename: str, project_id: str | None = None) -> bool:
    """删除一份落地文档(规范/任务书镜像)。删掉返回 True;文件本就不在返回 False。

    路径由 scope + doc_type + **安全 basename** 重建(同 land_doc 的落地位),不接受外部路径。
    顺带清掉它的「不可检索」标记 sidecar(若有)—— 不然删完再传同名文件会误显示成不可检索。
    """
    target = _doc_file_path(scope, doc_type, filename, project_id)
    marker = target.with_name(_safe_name(filename) + _NOSEARCH_SUFFIX)
    marker.unlink(missing_ok=True)  # 标记是附属物,先清掉(文件不在时也可能残留标记)
    if not target.is_file():
        return False
    target.unlink()
    return True


def delete_drawing_file(project_id: str, rel_path: str | None) -> bool:
    """按 drawings.rel_path 删除图纸物理文件。删掉 True;rel_path 为空或文件不在返回 False。

    落地断言反过来用:解析后的目标必须仍在项目根**之内**,越界(rel_path 含 ../)直接抛。
    """
    if not rel_path:
        return False
    root = _project_root(project_id).resolve()
    target = (root / rel_path).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"图纸路径越界:{rel_path!r}")
    if not target.is_file():
        return False
    target.unlink()
    return True


def delete_project_tree(project_id: str) -> bool:
    """整个删除 data/projects/<id>/(图纸 + 文档镜像一起没)。删掉 True;目录本就不在 False。"""
    root = _project_root(project_id)  # id 非法直接抛,别拿脏字符串去 rmtree
    if not root.is_dir():
        return False
    shutil.rmtree(root)
    return True


__all__ = [
    "DOC_REGULATION",
    "DOC_TASK_BOOK",
    "DOC_TYPES",
    "SCOPE_GLOBAL",
    "SCOPE_PROJECT",
    "SCOPES",
    "DocEntry",
    "delete_doc",
    "delete_drawing_file",
    "delete_project_tree",
    "doc_is_unsearchable",
    "ensure_project_tree",
    "land_doc",
    "land_drawing",
    "list_docs",
    "mark_doc_unsearchable",
    "project_rel_path",
    "resolve_doc",
]
