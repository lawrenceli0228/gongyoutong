"""项目与图纸索引的 SQLite 存储层:同步 sqlite3,每个操作一条独立连接(同 db/tasks.py 定案 #6)。

职责边界(这层"薄"是刻意的,与 db/tasks.py 同源):
- 只保证两件事:**存取保真**(参数化读写,内容原样进出)与**表级约束**
  (NOT NULL / CHECK / 外键)。业务校验 —— 空名、view_type 是否合法、项目是否已存在 ——
  全部归上层(webapp / agents/cad 的工具层):那些规则失败时要说中文人话并进信封契约,
  搅进这层会把 SQL 与话术绑死,两头都难测。
- 约束由库兜底、异常向上抛:插一张属于不存在项目的图 → 外键抛 sqlite3.IntegrityError;
  view_type 不在白名单 → CHECK 抛 sqlite3.IntegrityError;项目 id 重复 → 主键抛。
  本层**不吞**这些异常,交由上层翻成中文信封(同 tasks 层「T 号不存在」的人话归工具层)。

并发姿势(定案 #6):上层工具用 asyncio.to_thread 把这里的同步函数摔进工作线程。
每个公开函数内部自己 connect → 幂等建表 → 参数化语句 → 提交 → 关闭,
模块级零连接、零可变状态 —— sqlite3 连接对象禁止跨线程共用,不留共享连接就永远撞不上线程问题。

与 db/tasks.py 写**同一个 gyt.sqlite3**(单库多表,W7 §03 红线:不每个域一个 db 文件)。
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Final, NamedTuple

from gyt.core.sqlite_util import in_clause, now_iso, open_db

# 平立剖三视图。上层(webapp / cad 工具)拿它做「传前校验 + 中文提示」,
# 库层则靠 drawings 表的 CHECK 兜底。两处同一份真相,改这里等于改对外语义。
VIEW_TYPES: Final[tuple[str, ...]] = ("plan", "elevation", "section")

# ---------------------------------------------------------------------------
# 建表语句 —— 与《W7 业务数据库设计方案》§03 一字沿用,幂等,每次操作都执行。
# ---------------------------------------------------------------------------
# projects 是全库根,attendance 域的 workers/shifts/attendance 也 FK 到它。
# 导出成模块级常量供那边 import 复用,别两处各抄一份 DDL 日后漂移。
PROJECTS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS projects (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  code       TEXT,
  created_at TEXT NOT NULL
);
"""

# drawings 外键引用 projects,所以建表顺序必须 projects 在前(见 _projects_db)。
# rel_path 是给人浏览用的项目目录内相对路径(§2);agent 侧永远只认 artifact_id。
_VIEW_TYPE_CHECK: Final[str] = in_clause(VIEW_TYPES)
DRAWINGS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS drawings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  artifact_id TEXT NOT NULL,
  view_type   TEXT NOT NULL CHECK (view_type IN ({_VIEW_TYPE_CHECK})),
  floor       TEXT,
  title       TEXT NOT NULL,
  rel_path    TEXT,
  created_at  TEXT NOT NULL
);
"""


class ProjectRow(NamedTuple):
    """projects 表的一行。字段顺序即 SELECT 列序(_PROJECT_COLUMNS 由 _fields 派生,改名不会错位)。"""

    id: str  # 短码主键,如 'gyt-a3'
    name: str
    code: str | None
    created_at: str


class DrawingRow(NamedTuple):
    """drawings 表的一行。字段顺序即 SELECT 列序。"""

    id: int
    project_id: str
    artifact_id: str  # 32 位 id,文件本身落盘(契约 2)
    view_type: str  # VIEW_TYPES 之一
    floor: str | None
    title: str
    rel_path: str | None  # 项目目录内相对路径,仅供人类浏览
    created_at: str


# SELECT 列序从 _fields 派生:行元组 → NamedTuple 的对位靠它锁死。
_PROJECT_COLUMNS: Final[str] = ", ".join(ProjectRow._fields)
_DRAWING_COLUMNS: Final[str] = ", ".join(DrawingRow._fields)

# 以下语句的值一律走 ? 占位(方案红线 4):f-string 只拼模块级常量片段,
# 任何运行期的值(id、名字、日期)都绝不进入 SQL 文本。
_INSERT_PROJECT_SQL: Final[str] = (
    "INSERT INTO projects (id, name, code, created_at) VALUES (?, ?, ?, ?)"
)
_FETCH_PROJECT_SQL: Final[str] = f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?"
# 项目按建库先后列(created_at 是 ISO 文本,序 = 时间序),同刻按 id 兜底稳定。
_LIST_PROJECTS_SQL: Final[str] = f"SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY created_at, id"

_INSERT_DRAWING_SQL: Final[str] = (
    "INSERT INTO drawings (project_id, artifact_id, view_type, floor, title, rel_path, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_FETCH_DRAWING_SQL: Final[str] = f"SELECT {_DRAWING_COLUMNS} FROM drawings WHERE id = ?"
# 同项目内按名字找图,取最新登记的一张(同名跨楼层可能多张,latest 兜底)。
_RESOLVE_DRAWING_SQL: Final[str] = (
    f"SELECT {_DRAWING_COLUMNS} FROM drawings WHERE project_id = ? AND title = ? "
    "ORDER BY id DESC LIMIT 1"
)
_LIST_DRAWINGS_BASE: Final[str] = f"SELECT {_DRAWING_COLUMNS} FROM drawings"
_LIST_DRAWINGS_ORDER: Final[str] = "ORDER BY project_id, view_type, id"
# cad 解析上传图时,用户常只报图名不报项目 → 跨项目按名字找,取最新一张。
_FIND_BY_TITLE_SQL: Final[str] = (
    f"SELECT {_DRAWING_COLUMNS} FROM drawings WHERE title = ? ORDER BY id DESC LIMIT 1"
)
# cad 拿到 artifact_id 后要回头取 view_type / 展示名 → 按 artifact_id 反查。
_FIND_BY_ARTIFACT_SQL: Final[str] = (
    f"SELECT {_DRAWING_COLUMNS} FROM drawings WHERE artifact_id = ? ORDER BY id DESC LIMIT 1"
)
# 「打开/预览某张图」按名字找**全部**同名图纸(可跨项目),用于歧义消解:>1 张就请用户确认。
_FIND_ALL_BY_TITLE_BASE: Final[str] = f"SELECT {_DRAWING_COLUMNS} FROM drawings WHERE title = ?"
# 删除:单张图 / 某项目全部图 / 项目本身。删项目前必须先删它名下的图(外键)。
_DELETE_DRAWING_SQL: Final[str] = "DELETE FROM drawings WHERE id = ?"
_DELETE_PROJECT_DRAWINGS_SQL: Final[str] = "DELETE FROM drawings WHERE project_id = ?"
_DELETE_PROJECT_SQL: Final[str] = "DELETE FROM projects WHERE id = ?"


def _projects_db() -> AbstractContextManager[sqlite3.Connection]:
    """本次操作专用连接:开外键 → 进场幂等建表 → 离场提交并关闭(中途异常回滚后关闭)。

    连接 / 事务 / 库路径 / 幂等建表全在 ``core/sqlite_util.open_db``,四个 db 模块同一份。
    两段 DDL 拼起来交给它一次 ``executescript`` 跑,**顺序 projects 在前** —— drawings 的
    外键引用它。

    ⚠️ 外键靠 ``foreign_keys=True`` 开:``PRAGMA foreign_keys`` **必须在事务外执行**
    (事务内是 no-op、而且一声不吭),所以它只能是公共件的参数 —— 这里拿到的连接已经在
    事务里,自己执行必然放错位置。完整原委见 ``open_db`` 的 docstring,别在这儿再抄一份。
    """
    return open_db(PROJECTS_DDL + DRAWINGS_DDL, foreign_keys=True)


# --- projects ---------------------------------------------------------------


def create_project(project_id: str, name: str, code: str | None = None) -> None:
    """记一个新项目(id 由上层生成的短码)。

    不做业务校验(空名、短码格式由工具层拦下并出人话);id 重复会撞主键抛
    sqlite3.IntegrityError,交由上层翻成「这个项目已经建过了」。
    """
    with _projects_db() as conn:
        conn.execute(_INSERT_PROJECT_SQL, (project_id, name, code, now_iso()))


def get_project(project_id: str) -> ProjectRow | None:
    """按 id 取一个项目;查无此项目返回 None(人话归工具层拼)。"""
    with _projects_db() as conn:
        raw = conn.execute(_FETCH_PROJECT_SQL, (project_id,)).fetchone()
    return ProjectRow(*raw) if raw is not None else None


def list_projects() -> list[ProjectRow]:
    """一次取全部项目(方案红线 4,禁循环内逐条查)。"""
    with _projects_db() as conn:
        rows = conn.execute(_LIST_PROJECTS_SQL).fetchall()
    return [ProjectRow(*r) for r in rows]


# --- drawings ---------------------------------------------------------------


def add_drawing(
    project_id: str,
    artifact_id: str,
    view_type: str,
    title: str,
    floor: str | None = None,
    rel_path: str | None = None,
) -> int:
    """登记一张图纸,返回自增 id。

    不做业务校验:project_id 不存在会撞外键、view_type 不在白名单会撞 CHECK,
    两者都抛 sqlite3.IntegrityError,交由上层翻成中文信封。
    """
    now = now_iso()
    with _projects_db() as conn:
        new_id = conn.execute(
            _INSERT_DRAWING_SQL,
            (project_id, artifact_id, view_type, floor, title, rel_path, now),
        ).lastrowid
    if new_id is None:  # pragma: no cover — sqlite 对 INSERT 必给 rowid,纯防御
        raise RuntimeError("SQLite 没有返回新图纸的 rowid")
    return new_id


def get_drawing_by_id(drawing_id: int) -> DrawingRow | None:
    """按自增 id 取一张图;查无返回 None。"""
    with _projects_db() as conn:
        raw = conn.execute(_FETCH_DRAWING_SQL, (drawing_id,)).fetchone()
    return DrawingRow(*raw) if raw is not None else None


def resolve_by_title(project_id: str, title: str) -> DrawingRow | None:
    """在某项目内按展示名找图,取最新登记的一张;查无返回 None(cad 工具解析图名用)。"""
    with _projects_db() as conn:
        raw = conn.execute(_RESOLVE_DRAWING_SQL, (project_id, title)).fetchone()
    return DrawingRow(*raw) if raw is not None else None


def find_drawing_by_title(title: str) -> DrawingRow | None:
    """跨项目按展示名找图,取最新登记的一张;查无返回 None。

    cad 解析上传图时,用户往往只报图名(「南立面图」)不报项目,这里不限项目地找。
    """
    with _projects_db() as conn:
        raw = conn.execute(_FIND_BY_TITLE_SQL, (title,)).fetchone()
    return DrawingRow(*raw) if raw is not None else None


def find_drawing_by_artifact(artifact_id: str) -> DrawingRow | None:
    """按 artifact_id 反查 drawings 行(cad 拿到 id 后回头取 view_type / 展示名);查无返回 None。"""
    with _projects_db() as conn:
        raw = conn.execute(_FIND_BY_ARTIFACT_SQL, (artifact_id,)).fetchone()
    return DrawingRow(*raw) if raw is not None else None


def find_drawings_by_title(title: str, project_id: str | None = None) -> list[DrawingRow]:
    """按展示名找**全部**同名图纸(给了 project_id 就限定在该项目内),供歧义消解。

    「打开/预览 XX 图」时用:恰好 1 张直接开;>1 张(同名跨项目 / 同名跨楼层)先把候选列给
    用户确认,别猜错图纸。WHERE 片段是模块内字面量,值走 ? 占位(方案红线 4)。
    """
    params: list[str] = [title]
    query = _FIND_ALL_BY_TITLE_BASE
    if project_id is not None:
        query += " AND project_id = ?"
        params.append(project_id)
    query += " ORDER BY project_id, id"
    with _projects_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [DrawingRow(*r) for r in rows]


def list_drawings(
    *, project_id: str | None = None, view_type: str | None = None
) -> list[DrawingRow]:
    """列图纸,可按项目 / 视图类型筛(都留空 = 全量)。一次取全,筛选走 ? 占位。

    WHERE 条件片段(``project_id = ?`` / ``view_type = ?``)是**模块内的字面量**,
    只有值走占位符 —— 符合「SQL 文本只拼常量片段,运行期值绝不进 SQL」的红线。
    """
    conditions: list[str] = []
    params: list[str] = []
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    if view_type is not None:
        conditions.append("view_type = ?")
        params.append(view_type)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"{_LIST_DRAWINGS_BASE}{where} {_LIST_DRAWINGS_ORDER}"
    with _projects_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [DrawingRow(*r) for r in rows]


def delete_drawing(drawing_id: int) -> bool:
    """按自增 id 删一张图纸行。返回是否真的删掉(不存在返回 False)。

    只删库行;物理文件 / 产物 / CAD 解析索引由上层(webapp)编排清理 —— 这层不碰文件系统。
    """
    with _projects_db() as conn:
        cur = conn.execute(_DELETE_DRAWING_SQL, (drawing_id,))
    return cur.rowcount > 0


def delete_project_drawings(project_id: str) -> list[DrawingRow]:
    """删除某项目名下所有图纸行,返回被删的行(供上层按 artifact_id / rel_path 清理文件与产物)。

    项目删除的第一步:drawings 外键指向 projects,不先清空它就删项目会撞外键。
    """
    with _projects_db() as conn:
        rows = conn.execute(
            f"{_LIST_DRAWINGS_BASE} WHERE project_id = ? {_LIST_DRAWINGS_ORDER}",
            (project_id,),
        ).fetchall()
        conn.execute(_DELETE_PROJECT_DRAWINGS_SQL, (project_id,))
    return [DrawingRow(*r) for r in rows]


def delete_project(project_id: str) -> bool:
    """删除项目行。返回是否真的删掉(不存在返回 False)。

    ⚠️ 必须**先** delete_project_drawings 清空名下图纸,否则外键(drawings.project_id)会拦。
    编排顺序归上层;这层只管这一条 DELETE。
    """
    with _projects_db() as conn:
        cur = conn.execute(_DELETE_PROJECT_SQL, (project_id,))
    return cur.rowcount > 0


__all__ = [
    "DRAWINGS_DDL",
    "PROJECTS_DDL",
    "VIEW_TYPES",
    "DrawingRow",
    "ProjectRow",
    "add_drawing",
    "create_project",
    "delete_drawing",
    "delete_project",
    "delete_project_drawings",
    "find_drawing_by_artifact",
    "find_drawing_by_title",
    "find_drawings_by_title",
    "get_drawing_by_id",
    "get_project",
    "list_drawings",
    "list_projects",
    "resolve_by_title",
]
