"""隐患台账的 SQLite 存储层 —— **本文件是隐患状态机的唯一真相**(W9 方案 D3 / Codex#22)。

职责边界(这层"薄"是刻意的,与 db/tasks.py、db/projects.py 同源):
- 只保证三件事:**存取保真**(参数化读写,内容原样进出)、**表级约束**
  (NOT NULL / CHECK / UNIQUE / 外键)与**状态机合法性**(见 ALLOWED_TRANSITIONS)。
- 业务校验 —— ``due_phrase`` 中文日期解析、「严重隐患不许只签通知单」、
  「needs_grading=1 不许签发」—— 全部归上层(``supervision_api.py`` 的 HTTP 端点):
  那些规则失败时要说中文人话并进 Envelope 契约,搅进这层会把 SQL 与话术绑死,两头都难测。
- **超期筛选也不在这层**(方案 §5.2):``list_overdue`` 是纯 Python 日期比较,照 schedule
  的做法 —— 往 SQL 里塞「今天」得冻结库时钟才能测。这层 ``list_rows`` 一次取全,上层去算。

为什么状态机在 db 层而不在 ``agents/supervision/``(Codex#22):全仓 ``db/*.py`` 被
``agents/*/tools.py`` import,方向严格是 agents → db。反过来放会有循环导入风险,
且存储层就不能独立使用了。

⚠️ **本模块 import 了 ``gyt.attendance.receipt``,是 db 层第一次 import ``gyt.config`` 之外的东西。**
理由是 D7:业务时区只有一个权威,``found_at`` / ``closed_at`` 必须与文书编号里的时刻同源。
``receipt.py`` 是叶子模块(只 import 标准库),没有循环风险;而"db 只 import config"那条约定的
用意是**防止 db 反向依赖编排层与 Agent 层**,receipt 不在那一侧。
**绝不许改回 ``datetime.now(UTC).astimezone()``** —— 那是"靠宿主时区猜":容器是 Shanghai、
本机可能是任意时区,数值一致纯属巧合(``report/tools.py`` 现在就踩着,已记 TODO-41)。

并发姿势(沿用 tasks 定案 #6):上层用 asyncio.to_thread 把这里的同步函数摔进工作线程。
每个公开函数内部自己 connect → 幂等建表 → 参数化语句 → 提交 → 关闭,模块级零连接、零可变状态
—— sqlite3 连接对象本就禁止跨线程共用,不留共享连接就撞不上线程问题。
与 db/tasks.py、db/projects.py、db/attendance.py 写**同一个 gyt.sqlite3**(单库多表)。

D18:``_hazard_db`` 与 tasks/attendance/projects 三处高度雷同,是**故意**的第四份拷贝。
抽到 core(D9)是对的,但那件事与监理闭环无关,却要动三个已上线模块(其中 attendance 有线上
真实打卡数据)—— 风险不该担在核心链的关键路径上。定案:先照抄,重构单开一条泳道,做完四处一起换。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from typing import Final, NamedTuple

from gyt.attendance.receipt import make_snapshot
from gyt.config import get_settings

# ---------------------------------------------------------------------------
# 受控词表 —— 与 CHECK 子句同源(拼 CHECK 的手法照搬 db/attendance.py)
# ---------------------------------------------------------------------------

STATUS_PENDING: Final[str] = "pending"  # 自动登记的落点(D17),还没人确认
STATUS_OPEN: Final[str] = "open"  # 监理确认之后的正式起点
STATUS_NOTIFIED: Final[str] = "notified"  # 已签《监理通知单》(grade=一般 这条路)
STATUS_SUSPENDED: Final[str] = "suspended"  # 已签暂停令三文书(grade=严重 这条路)
STATUS_REINSPECT_FAILED: Final[str] = "reinspect_failed"  # 复查不合格 / 超期未改
STATUS_RESUMING: Final[str] = "resuming"  # 曾停工、复查已合格,**在等《复工令》**
STATUS_CLOSED: Final[str] = "closed"
STATUS_ESCALATED: Final[str] = "escalated"

STATUSES: Final[tuple[str, ...]] = (
    STATUS_PENDING,
    STATUS_OPEN,
    STATUS_NOTIFIED,
    STATUS_SUSPENDED,
    STATUS_REINSPECT_FAILED,
    STATUS_RESUMING,
    STATUS_CLOSED,
    STATUS_ESCALATED,
)
"""八个状态。顺序 = 状态机自然流向,只用于生成 CHECK 子句与遍历,不表达优先级。

三条不显然的语义:
- ``pending`` **不算进整改率、不进超期清单、不能被升级**(D17)—— safety 看图看出来的东西
  还没有人确认过,直接进正式流程等于让模型单方面开启法律流程。
- 🔴 ``suspended`` 只证明**暂停令已出稿**,不证明工地真停了工(方案 §4.1)。对外措辞必须是
  「已出具暂停令」而不是「已责令停工」—— 送达与执行确认不在本批范围。
- ``resuming`` 是"等复工令"这一站。少了它,停过工的隐患复查合格就直接销项 = 漏发复工令(Codex#6)。
"""

GRADE_NORMAL: Final[str] = "一般"
GRADE_SEVERE: Final[str] = "严重"
GRADES: Final[tuple[str, ...]] = (GRADE_NORMAL, GRADE_SEVERE)
"""监理口径的二分。**这两个字符串的真相在这里**,``agents/supervision/grading.py`` import 它们。

为什么归 db:hazards 表的 CHECK 用它拼,而 agents → db 是合法方向、反过来不是。
grading 那边只要 import,就**结构上不可能**产出一个过不了 CHECK 的 grade。"""

DOC_TYPES: Final[tuple[str, ...]] = (
    "notice",  # 监理通知单          GYT-TZ-…
    "suspension",  # 工程暂停令          GYT-ZT-…
    "resumption",  # 工程复工令          GYT-FG-…
    "owner_report",  # 致建设单位报告      GYT-JS-…
    "authority_report",  # 报主管部门的监理报告 GYT-JB-…
    "reinspect",  # 复查记录(不是文书:没有 artifact_id,挂 photo_id + result)
)
"""六种挂在隐患上的东西。前五种是文书,``reinspect`` 是复查留痕。

**与 ``core/doc_no.py`` 的 ``DocKind`` 成员名同源**:那边 ``kind.name.lower()`` 就是这里的
doc_type(它的 ``test_成员名对齐hazard_docs的doc_type词表`` 钉着这层关系;为了不跟本模块的
落地进度绑在一起,那条测试刻意抄了字面量)。两个例外:``DocKind.HAZARD`` 是隐患本身、不进这张表;
``reinspect`` 在方案 §6.3 的编号表里没有类型段,所以 ``DocKind`` 里没有它。

**每类文书若要挂编号守卫必须各写各的正则**(``doc_no.PATTERNS`` 按种类给好了)——
attendance 的与 report 的互不匹配是实测过的,拿错正则会一个都认不出、静默全放行。"""

DOC_RESULTS: Final[tuple[str, ...]] = ("pass", "fail")
"""复查结论。**由人下**(D11):复查照片角度光线取景都变了,「没拍到那个部位」和
「问题已消除」在模型眼里一样,而那是往「误判合格」方向错 —— 这一侧会死人。"""


def _in_clause(values: Sequence[str]) -> str:
    """把受控词表拼成 SQL 的 IN 列表。**只拼模块级常量,永远不碰运行期的值。**"""
    return ", ".join(f"'{v}'" for v in values)


def _placeholders(count: int) -> str:
    """生成 ``?, ?, ?`` —— 个数由代码算,值一律走占位符(方案红线 4)。"""
    return ", ".join("?" * count)


# ---------------------------------------------------------------------------
# 状态机 —— 唯一真相
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATUS_PENDING: frozenset({STATUS_OPEN}),
    STATUS_OPEN: frozenset({STATUS_NOTIFIED, STATUS_SUSPENDED}),
    # notified 的行 was_suspended 恒为 0(没有任何路径能把停过工的行送回 notified),
    # 所以复查合格直接 closed;它到不了 resuming,写进来反而等于允许滥发复工令。
    STATUS_NOTIFIED: frozenset({STATUS_CLOSED, STATUS_REINSPECT_FAILED}),
    # 🔴 suspended **没有** closed:停过工的必须先走 resuming 出复工令(Codex#6)。
    # 这条缺失就是「漏发复工令」那个 bug 的结构性修法,别顺手补上。
    STATUS_SUSPENDED: frozenset({STATUS_RESUMING, STATUS_REINSPECT_FAILED}),
    # 自环 = 再复查又不合格;closed / resuming 两条出口由 was_suspended 决定(见 pass_reinspection)。
    STATUS_REINSPECT_FAILED: frozenset(
        {STATUS_CLOSED, STATUS_RESUMING, STATUS_REINSPECT_FAILED, STATUS_ESCALATED}
    ),
    STATUS_RESUMING: frozenset({STATUS_CLOSED}),
    STATUS_CLOSED: frozenset(),
    STATUS_ESCALATED: frozenset(),
}
"""合法迁移表 = 下面这张图的可执行版本。**改一处必须改两处**(图是人读的那份,表是执行的那份;
漂了的表现是「照图施工的人写出一个永远 rowcount=0 的调用」,而且不报错)。

                    safety 判出违规项
                          │
                 登记 hazards(<pending>)      ← D17:自动写入,但不算整改率、
                          │                      不进超期清单、不能被升级
                 监理确认 ──(否决)──► delete_pending()
                          │ 确认
                       <open>
                          │
              ┌───────────┴───────────┐   ← 分岔由 grade 决定,代码判,不是 LLM 判
          grade=一般               grade=严重
              │                       │
       《监理通知单》         《通知单》+《暂停令》+《致建设单位报告》
              │                       │      ↑ 三份一次原子产出;was_suspended=1
         <notified>              <suspended>
              │                       │
              └──────────┬────────────┘
                         │  复查(必须挂复查照片;模型给建议,人下结论 — D11)
              ┌──────────┴──────────┐
          人确认合格            人确认不合格 / 超期未改
              │                       │
   ┌──────────┴──────────┐    <reinspect_failed>
was_suspended=0   was_suspended=1     │  再复查 ┄┄┄┄┄┄┘(自环,回到复查分支)
   │                     │            │  或升级
<closed>          <resuming> ──《复工令》──► <closed>
                                      └──► <escalated> ──《监理报告》报主管部门

八个状态**每个都要有键**(单测盯着键集 == STATUSES)。``closed`` / ``escalated`` 的空集合
不是"忘了填",是"到此为止":隐患销项之后又冒出来是**新的一条隐患**(新照片、新编号),
不是把旧行改回去 —— 留档的证据链不许被回退改写。
"""


def can_transition(src: str, dst: str) -> bool:
    """src → dst 是不是合法迁移。纯函数,不碰库。

    不认识的状态一律 False(不认识就不放行)—— 上层拿到 False 该说人话,别当成"可能可以"。
    """
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def _sources_for(target: str, *sources: str) -> tuple[str, ...]:
    """声明"某个迁移函数允许的起始状态",并当场校验它是 ALLOWED_TRANSITIONS 的子集。

    为什么不直接把反查结果拿来用:反查(「谁能走到 target」)只给出**上限**,具体函数往往
    还要更窄 —— ``resuming → closed`` 走的是复工令那条路,和"复查合格直接销项"是两件事,
    不能共用一个 WHERE。但**上限必须由表说了算**:哪天有人在表里删掉一条边、而某个迁移函数
    还留着对应的 WHERE,那个函数就成了绕过状态机的后门。

    这里在**导入时**就炸掉,进程起不来 —— 与 ``scripts/serve_login.py`` 的启动硬失败同一手法:
    少了同步的东西不会有任何运行期报错,那就让它连起都起不来。
    """
    legal = {src for src, targets in ALLOWED_TRANSITIONS.items() if target in targets}
    unknown = tuple(s for s in sources if s not in legal)
    if unknown:  # pragma: no cover —— 只在有人改错 ALLOWED_TRANSITIONS 时触发
        raise RuntimeError(
            f"迁移函数声明的起始状态 {unknown} 不在 ALLOWED_TRANSITIONS 里(目标 {target});"
            "状态机表与迁移函数漂了,先对齐 db/hazards.py 里那张图与那张表"
        )
    return sources


# ---------------------------------------------------------------------------
# 建表 —— 与方案 §4.1 逐列一致,幂等,每次操作都执行
# ---------------------------------------------------------------------------

_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS hazards (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  hazard_no       TEXT NOT NULL UNIQUE,
  project_id      TEXT NOT NULL DEFAULT '',
  photo_sha256    TEXT NOT NULL,
  photo_id        TEXT NOT NULL,
  item            TEXT NOT NULL,
  severity        TEXT NOT NULL,
  grade           TEXT NOT NULL CHECK (grade IN ({_in_clause(GRADES)})),
  grading_version TEXT NOT NULL,
  needs_grading   INTEGER NOT NULL DEFAULT 0,
  was_suspended   INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL CHECK (status IN ({_in_clause(STATUSES)})),
  due_date        TEXT,
  found_at        TEXT NOT NULL,
  confirmed_at    TEXT,
  closed_at       TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (project_id, photo_sha256, item)
);
CREATE INDEX IF NOT EXISTS idx_hazards_proj_status ON hazards(project_id, status);

CREATE TABLE IF NOT EXISTS hazard_docs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  hazard_no   TEXT NOT NULL REFERENCES hazards(hazard_no),
  doc_type    TEXT NOT NULL CHECK (doc_type IN ({_in_clause(DOC_TYPES)})),
  doc_no      TEXT NOT NULL UNIQUE,
  artifact_id TEXT,
  photo_id    TEXT,
  result      TEXT CHECK (result IS NULL OR result IN ({_in_clause(DOC_RESULTS)})),
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hazard_docs_no ON hazard_docs(hazard_no);

CREATE TABLE IF NOT EXISTS hazard_ingest_failures (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id  TEXT NOT NULL DEFAULT '',
  photo_id    TEXT NOT NULL,
  item        TEXT NOT NULL,
  reason      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
"""
"""建表语句。**改这里就是改方案 §4.1,两边要一起改。**

几处不显然的:
- ``project_id`` 是 ``NOT NULL DEFAULT ''`` 而不是可空(D6):取不到项目就是**空串**。
  用 NULL 的话唯一键会失效 —— SQL 里 ``NULL != NULL``,同一张照片重传能无限新增行。
- ``UNIQUE (project_id, photo_sha256, item)`` 是幂等键(D14 + Codex#9):**不是 artifact_id**
  (``core/artifacts.py`` 用 uuid4,同张照片重传就是新号,彩排三轮得三批),
  **必须带 project_id**(不带的话同一张照片用于第二个项目时会复用第一个项目的隐患 = 跨项目串账)。
- ``severity`` 没有 CHECK:它是 safety 直出、原样透传的,词表外的词本来就要能存进来
  (那是提示词失守的诊断信号,见 severity.py)。真正卡人的是 ``grade``,它有 CHECK。
- ``hazard_docs.result`` 写成 ``IS NULL OR IN (...)``:文书行没有结论,只有 reinspect 行有(Codex#19)。
- ``hazard_ingest_failures``(Codex#10):写库失败要能**事后统计**,只打日志的话进程一重启就失忆。
"""


class HazardRow(NamedTuple):
    """hazards 表的一行。字段顺序即 SELECT 列序(``_COLUMNS`` 由 ``_fields`` 派生)。

    ⚠️ **字段顺序必须与 ``_DDL`` 的列顺序一致。** 错位不会有任何报错 —— ``severity`` 与
    ``grade`` 都是 TEXT,换了位置类型检查抓不到,表现是「重大隐患按一般隐患签了通知单」。
    ``test_db_hazards.py`` 有一条直接比对这两份顺序。
    """

    id: int
    hazard_no: str  # GYT-H-YYYYMMDD-HHMMSS-4hex,由上层生成后传进来(生成器归 core/doc_no.py)
    project_id: str  # 空串 = 未归属(D6),不是 None
    photo_sha256: str  # 幂等键的一节:内容哈希,不是 artifact_id
    photo_id: str  # 首次发现那张照片的 artifact_id(展示用)
    item: str  # 受控词表 8 选 1;词表外原样透传(同 safety)
    severity: str  # 重大/较大/一般/待定级 —— safety 直出,不改写
    grade: str  # GRADES 之一,由 supervision/grading.py 映射
    grading_version: str  # 定级规则版本快照(D4):事后能答「当时按哪版表定的」
    needs_grading: int  # 0/1。1 = 待人工定级,**所有签发动作硬拒**(Codex#11)
    was_suspended: int  # 0/1。1 = 曾进过 suspended,复查合格后必须先出复工令(Codex#6)
    status: str  # STATUSES 之一
    due_date: str | None  # 整改期限(香港日历日);进 notified/suspended 时必须非空
    found_at: str
    confirmed_at: str | None  # D17:人把 pending 确认成 open 的时刻
    closed_at: str | None
    created_at: str
    updated_at: str


class HazardDocRow(NamedTuple):
    """hazard_docs 表的一行 —— 一条隐患的证据链就是它按 hazard_no 取回的全部行。"""

    id: int
    hazard_no: str
    doc_type: str  # DOC_TYPES 之一
    doc_no: str  # 全局唯一;撞号靠 UNIQUE 兜底 + 调用方换个随机尾重试(§6.3)
    artifact_id: str | None  # 文书行必须有(应用层校验);reinspect 行为空
    photo_id: str | None  # 仅 reinspect 行:复查照片
    result: str | None  # 仅 reinspect 行:DOC_RESULTS 之一
    created_at: str


class IngestFailureRow(NamedTuple):
    """hazard_ingest_failures 表的一行。``reason`` 是给排查的人看的内部细节,**不进人话**。"""

    id: int
    project_id: str
    photo_id: str
    item: str
    reason: str
    created_at: str


class DocDraft(NamedTuple):
    """要挂到隐患上的一份文书 / 一条复查记录。``created_at`` 由本层统一盖(单一快照)。

    文书**先落盘再写库**(§6.4):调用方拿到 ``artifact_id`` 之后才来调这里 —— 顺序反了会在
    证据链里留下一份点不开的「文书」,那比孤儿文件危险得多。
    """

    doc_type: str
    doc_no: str
    artifact_id: str | None = None
    photo_id: str | None = None
    result: str | None = None


class Registration(NamedTuple):
    """``create()`` 的结果:库里现在的那一行 + 这次是不是**真的**新增。

    ``created=False`` 时 ``row.hazard_no`` 是**首次登记时的号**,不是这次传进来的那个 ——
    同一张照片重传、彩排三轮,拿到的都是同一条隐患(D14)。
    """

    row: HazardRow
    created: bool


# SELECT 列序从 _fields 派生:行元组 → NamedTuple 的对位靠它锁死。
_COLUMNS: Final[str] = ", ".join(HazardRow._fields)
_DOC_COLUMNS: Final[str] = ", ".join(HazardDocRow._fields)
_FAILURE_COLUMNS: Final[str] = ", ".join(IngestFailureRow._fields)

# INSERT 的列表同样由 _fields 派生(去掉自增 id),参数元组也从 NamedTuple 切片来:
# 两边同源,加一列时只要 NamedTuple 与 DDL 一起改,INSERT 不会漏。
_INSERT_FIELDS: Final[tuple[str, ...]] = HazardRow._fields[1:]
_INSERT_SQL: Final[str] = (
    f"INSERT INTO hazards ({', '.join(_INSERT_FIELDS)}) "
    f"VALUES ({_placeholders(len(_INSERT_FIELDS))}) "
    # 🔴 冲突目标必须写明是那个三元组。写成裸 ``ON CONFLICT DO NOTHING`` 的话,``hazard_no``
    #    撞号也会被静默吞掉 —— 而那正是 §6.3 要求"调用方撞库重试"的信号,吞掉之后 create()
    #    会回一条**别的**隐患行,而调用方以为自己登记成功了。
    "ON CONFLICT (project_id, photo_sha256, item) DO NOTHING"
)

_DOC_INSERT_FIELDS: Final[tuple[str, ...]] = HazardDocRow._fields[1:]
_INSERT_DOC_SQL: Final[str] = (
    f"INSERT INTO hazard_docs ({', '.join(_DOC_INSERT_FIELDS)}) "
    f"VALUES ({_placeholders(len(_DOC_INSERT_FIELDS))})"
)

_FAILURE_INSERT_FIELDS: Final[tuple[str, ...]] = IngestFailureRow._fields[1:]
_INSERT_FAILURE_SQL: Final[str] = (
    f"INSERT INTO hazard_ingest_failures ({', '.join(_FAILURE_INSERT_FIELDS)}) "
    f"VALUES ({_placeholders(len(_FAILURE_INSERT_FIELDS))})"
)

# 排序:有期限在前按期限升序(ISO 文本序 = 日期序),无期限垫底不丢,同期限按 id 先来先排 ——
# 与 db/tasks.py 同一套理由:两次列表里「第 3 条」要指向同一条隐患。
_ORDER_BY: Final[str] = "ORDER BY (due_date IS NULL), due_date, id"

_FETCH_SQL: Final[str] = f"SELECT {_COLUMNS} FROM hazards WHERE hazard_no = ?"
_FETCH_BY_KEY_SQL: Final[str] = (
    f"SELECT {_COLUMNS} FROM hazards WHERE project_id = ? AND photo_sha256 = ? AND item = ?"
)
_LIST_BASE_SQL: Final[str] = f"SELECT {_COLUMNS} FROM hazards"
_DOCS_BASE_SQL: Final[str] = f"SELECT {_DOC_COLUMNS} FROM hazard_docs"
_DOCS_ORDER_BY: Final[str] = "ORDER BY hazard_no, id"
_FAILURES_BASE_SQL: Final[str] = f"SELECT {_FAILURE_COLUMNS} FROM hazard_ingest_failures"
_FAILURES_ORDER_BY: Final[str] = "ORDER BY id"

_SET_GRADE_SQL: Final[str] = (
    "UPDATE hazards SET grade = ?, needs_grading = 0, updated_at = ? WHERE hazard_no = ?"
)
# 否决只针对 pending:确认过的隐患不许被删,证据链不能凭一次点击消失。
_DELETE_PENDING_SQL: Final[str] = "DELETE FROM hazards WHERE hazard_no = ? AND status = ?"


def _transition_sql(
    target: str,
    sources: tuple[str, ...],
    *,
    extra_set: str = "",
    extra_where: str = "",
) -> str:
    """拼一条状态迁移 UPDATE。

    ``WHERE status IN (...)`` 是**写前状态守卫**:上层「先查再改」的两步之间存在理论窗口
    (并发签发、并发复查),不加守卫的话非法迁移会被静默放行。调用方一律靠 ``rowcount``
    分辨成败,**不许信先读的快照**(同 ``db/tasks.py:136-138`` 的守卫,那条真机抓到过)。

    ``extra_set`` / ``extra_where`` 只接**模块内的字面量片段**,运行期的值走 ? 占位。
    """
    return (
        f"UPDATE hazards SET status = ?, updated_at = ?{extra_set} "
        f"WHERE hazard_no = ? AND status IN ({_placeholders(len(sources))}){extra_where}"
    )


# --- 每个迁移函数的起始状态:声明在这里,导入时对着 ALLOWED_TRANSITIONS 校验 -----------

_SOURCES_CONFIRM: Final = _sources_for(STATUS_OPEN, STATUS_PENDING)
_SOURCES_NOTIFY: Final = _sources_for(STATUS_NOTIFIED, STATUS_OPEN)
_SOURCES_SUSPEND: Final = _sources_for(STATUS_SUSPENDED, STATUS_OPEN)
_SOURCES_FAIL: Final = _sources_for(
    STATUS_REINSPECT_FAILED, STATUS_NOTIFIED, STATUS_SUSPENDED, STATUS_REINSPECT_FAILED
)
_SOURCES_RESUMING: Final = _sources_for(STATUS_RESUMING, STATUS_SUSPENDED, STATUS_REINSPECT_FAILED)
_SOURCES_CLOSE_ON_PASS: Final = _sources_for(
    STATUS_CLOSED, STATUS_NOTIFIED, STATUS_REINSPECT_FAILED
)
_SOURCES_RESUMED: Final = _sources_for(STATUS_CLOSED, STATUS_RESUMING)
_SOURCES_ESCALATE: Final = _sources_for(STATUS_ESCALATED, STATUS_REINSPECT_FAILED)

_CONFIRM_SQL: Final[str] = _transition_sql(
    STATUS_OPEN, _SOURCES_CONFIRM, extra_set=", confirmed_at = ?"
)
_NOTIFY_SQL: Final[str] = _transition_sql(
    STATUS_NOTIFIED, _SOURCES_NOTIFY, extra_set=", due_date = ?"
)
# ``was_suspended = 1`` 是字面量、不是运行期的值,所以可以进 SQL 文本。这一列是 Codex#6 的修法:
# 复查失败后 notified 与 suspended 都坍缩成 reinspect_failed,没有它,再次合格时状态机分不清
# 该直接关闭还是必须先出复工令 —— 漏发或滥发复工令。
_SUSPEND_SQL: Final[str] = _transition_sql(
    STATUS_SUSPENDED, _SOURCES_SUSPEND, extra_set=", due_date = ?, was_suspended = 1"
)
_FAIL_SQL: Final[str] = _transition_sql(STATUS_REINSPECT_FAILED, _SOURCES_FAIL)
_START_RESUMPTION_SQL: Final[str] = _transition_sql(
    STATUS_RESUMING, _SOURCES_RESUMING, extra_where=" AND was_suspended = 1"
)
_CLOSE_ON_PASS_SQL: Final[str] = _transition_sql(
    STATUS_CLOSED,
    _SOURCES_CLOSE_ON_PASS,
    extra_set=", closed_at = ?",
    extra_where=" AND was_suspended = 0",
)
_RESUMED_SQL: Final[str] = _transition_sql(
    STATUS_CLOSED, _SOURCES_RESUMED, extra_set=", closed_at = ?"
)
_ESCALATE_SQL: Final[str] = _transition_sql(STATUS_ESCALATED, _SOURCES_ESCALATE)


def _now_iso() -> str:
    """当前时刻,ISO 秒级、带 +08:00 偏移。**业务时区权威只有一个**(D7,理由见模块头注)。

    每次操作取一枚,同一次操作内的多个时间字段共用它(created_at / updated_at / found_at
    生下来同值,「没改过」要可断言)。
    """
    return make_snapshot().checked_at


@contextmanager
def _hazard_db() -> Iterator[sqlite3.Connection]:
    """本次操作专用连接:开外键 → 进场幂等建表 → 离场提交并关闭(中途异常回滚后关闭)。

    ⚠️ **``PRAGMA foreign_keys`` 必须在 ``with conn:`` 之外执行 —— 事务内是 no-op。**
    (``db/projects.py:137`` 有同一条原注释。)漏了或放错位置的表现是:外键**静默不校验**,
    ``hazard_docs`` 可以挂在一个根本不存在的 ``hazard_no`` 上,而证据链要到上报主管部门那天
    才发现引不出隐患。设一次对整条连接的后续操作都生效。

    建表用 ``executescript`` 而不是 ``execute``:``_DDL`` 含五条语句(三表 + 两索引)。
    executescript 会先隐式提交 —— 它是进场第一件事,前面没有未提交的东西,安全。
    建表顺序 hazards 在前:hazard_docs 的外键引用它。

    库路径每次现从 get_settings() 取、不在模块里缓存 —— 测试用 GYT_DATA_DIR + cache_clear()
    换库时这层自动跟着走,不需要任何补丁点。
    """
    with closing(sqlite3.connect(get_settings().sqlite_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.executescript(_DDL)
            yield conn


# --- hazards:登记与读取 ------------------------------------------------------


def create(
    *,
    hazard_no: str,
    project_id: str,
    photo_sha256: str,
    photo_id: str,
    item: str,
    severity: str,
    grade: str,
    grading_version: str,
    needs_grading: bool,
) -> Registration:
    """登记一条隐患,落 ``pending``(D17)。三元组已存在则**不新增**,返回库里已有那行。

    ``hazard_no`` **由调用方生成后传进来**(生成器在 ``core/doc_no.py``):这层不发号,也就
    不需要在这层写重试循环 —— 撞号时 ``hazard_no UNIQUE`` 会抛 ``sqlite3.IntegrityError``,
    调用方换个随机尾**再调一次本函数**即可(§6.3)。本层**不吞**这个异常:吞了就等于把
    "该重试"这件事变成静默失败。

    不做业务校验(空 item、severity 是否在词表内、grade 与 severity 是否自洽,都归上层出人话)。
    ``status`` 恒 ``pending``、``was_suspended`` 恒 0、``due_date`` 恒 NULL:这三样只能由后续的
    迁移函数改,不许在登记时抄近路。
    """
    now = _now_iso()
    draft = HazardRow(
        id=0,  # 占位:自增列不进 INSERT,下面切掉
        hazard_no=hazard_no,
        project_id=project_id,
        photo_sha256=photo_sha256,
        photo_id=photo_id,
        item=item,
        severity=severity,
        grade=grade,
        grading_version=grading_version,
        needs_grading=int(needs_grading),
        was_suspended=0,
        status=STATUS_PENDING,
        due_date=None,
        found_at=now,
        confirmed_at=None,
        closed_at=None,
        created_at=now,
        updated_at=now,
    )
    with _hazard_db() as conn:
        inserted = conn.execute(_INSERT_SQL, tuple(draft)[1:]).rowcount > 0
        # 回查用的是**三元组**而不是 hazard_no:冲突被吞掉时,库里那行的号是首次登记的号,
        # 不是这次传进来的这个。一次 SELECT,不是循环查。
        raw = conn.execute(_FETCH_BY_KEY_SQL, (project_id, photo_sha256, item)).fetchone()
    if raw is None:  # pragma: no cover —— 刚插入(或已存在)的行必在,纯防御
        raise RuntimeError("刚登记的隐患取不回来")
    return Registration(row=HazardRow(*raw), created=inserted)


def fetch(hazard_no: str) -> HazardRow | None:
    """按编号取一条;查无返回 None(「这个编号查不到」的人话归上层拼)。"""
    with _hazard_db() as conn:
        raw = conn.execute(_FETCH_SQL, (hazard_no,)).fetchone()
    return HazardRow(*raw) if raw is not None else None


def list_rows(
    *, project_id: str | None = None, statuses: Sequence[str] | None = None
) -> list[HazardRow]:
    """一次取全(方案红线 4,禁循环内逐条查)。两个筛子都留空 = 全量。

    ``project_id=None`` 是"不筛项目";``project_id=""`` 是"只看**未归属**的"—— 两者语义完全
    不同(D6:未归属是空串不是 NULL),别把 None 和空串混用。

    超期判断、整改率统计不在这层(``pending`` 与 ``needs_grading=1`` 不进超期清单是业务口径,
    D17 + Codex#11)。WHERE 片段是模块内拼出来的**结构**,个数由 len 算,值一律走占位符。
    """
    conditions: list[str] = []
    params: list[str] = []
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    if statuses is not None:
        conditions.append(f"status IN ({_placeholders(len(statuses))})")
        params.extend(statuses)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"{_LIST_BASE_SQL}{where} {_ORDER_BY}"
    with _hazard_db() as conn:
        raw_rows = conn.execute(query, tuple(params)).fetchall()
    return [HazardRow(*raw) for raw in raw_rows]


def set_grade(hazard_no: str, grade: str) -> bool:
    """人工定级:改 ``grade`` 并把 ``needs_grading`` 清零。返回 False = 编号不存在。

    这是 ``needs_grading=1`` 的隐患唯一的解锁通道(Codex#11:没定级的隐患所有签发工具硬拒,
    否则未知风险能按一般隐患走完整闭环并被销项)。

    这层**不限制**在什么状态下改级:「文书都签发了还能不能改级」是业务判断,归端点层 ——
    它同时还要管「改级后要不要重签文书」,那不是一条 UPDATE 的事。
    ``grade`` 不在 GRADES 里会撞 CHECK 抛 IntegrityError,本层不吞。
    """
    with _hazard_db() as conn:
        touched = conn.execute(_SET_GRADE_SQL, (grade, _now_iso(), hazard_no)).rowcount
    return touched > 0


def delete_pending(hazard_no: str) -> bool:
    """否决一条**待确认**的隐患(状态机图里 pending 那条否决支)。返回是否真的删掉。

    ``WHERE status = 'pending'`` 是硬守卫:确认过的隐患不许被删,留档与证据链不能因为一次
    误点消失。已挂文书的行还会被外键拦住。
    """
    with _hazard_db() as conn:
        touched = conn.execute(_DELETE_PENDING_SQL, (hazard_no, STATUS_PENDING)).rowcount
    return touched > 0


# --- 状态迁移 ----------------------------------------------------------------
#
# 统一姿势:一条 UPDATE + rowcount 判成败;要挂的文书写在**同一个事务**里(§6.4 ③),
# 且只有迁移真的命中才写 —— 迁移被拒还写文书 = 证据链里出现一份没有状态支撑的文书。


def _insert_docs(
    conn: sqlite3.Connection, hazard_no: str, docs: Sequence[DocDraft], now: str
) -> None:
    """把 N 份文书挂到隐患上。``executemany`` 一次写完,不在循环里逐条 execute。"""
    if not docs:
        return
    conn.executemany(
        _INSERT_DOC_SQL,
        [(hazard_no, d.doc_type, d.doc_no, d.artifact_id, d.photo_id, d.result, now) for d in docs],
    )


def _run_transition(
    sql: str, hazard_no: str, docs: Sequence[DocDraft], now: str, params: tuple[object, ...]
) -> bool:
    """跑一条迁移 + 附带的文书,一个事务。返回 False = 迁移被拒(状态不对或编号不存在)。"""
    with _hazard_db() as conn:
        if conn.execute(sql, params).rowcount == 0:
            return False
        _insert_docs(conn, hazard_no, docs, now)
        return True


def confirm(hazard_no: str) -> bool:
    """监理确认:``pending`` → ``open``,盖 ``confirmed_at``(D17)。

    这一步是自动登记与正式流程之间的闸。**没有它,safety 看一眼照片就能开启法律流程。**
    批量确认由端点循环调用 —— 每条一个事务,一条失败不牵连其它条。
    """
    now = _now_iso()
    params = (STATUS_OPEN, now, now, hazard_no, *_SOURCES_CONFIRM)
    return _run_transition(_CONFIRM_SQL, hazard_no, (), now, params)


def mark_notified(hazard_no: str, due_date: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """签发《监理通知单》:``open`` → ``notified``,写整改期限。

    ⚠️ ``due_date`` **必须是解析成功的日期**(Codex#12):端点收用户原话、用
    ``agents/schedule/dates.py`` 换算,解析不出就 fail、**不许留空** —— ``due_date`` 为空的
    隐患永远进不了超期清单,也就永远不会被升级。格式校验归端点,但别拿空串当"没期限"传进来。

    ``grade='严重'`` 不许只签通知单这件事由端点硬拦(Codex#3):那是"该走哪条路"的业务判断,
    这层只回答"这条路的状态迁移合不合法"。
    """
    now = _now_iso()
    params = (STATUS_NOTIFIED, now, due_date, hazard_no, *_SOURCES_NOTIFY)
    return _run_transition(_NOTIFY_SQL, hazard_no, docs, now, params)


def mark_suspended(hazard_no: str, due_date: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """签发《通知单》+《工程暂停令》+《致建设单位报告》:``open`` → ``suspended``。

    三份文书随 ``docs`` 一起进来,与状态改动在**同一个事务**(§6.4 ③);文件必须先落盘拿到
    ``artifact_id`` 再调这里。同时把 ``was_suspended`` 置 1:此后即使复查合格也**必须先出
    复工令**(Codex#6)。
    """
    now = _now_iso()
    params = (STATUS_SUSPENDED, now, due_date, hazard_no, *_SOURCES_SUSPEND)
    return _run_transition(_SUSPEND_SQL, hazard_no, docs, now, params)


def mark_reinspect_failed(hazard_no: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """复查不合格 / 超期未改 → ``reinspect_failed``。可反复进入(再复查又不合格)。

    ``docs`` 一般挂一条 ``reinspect`` 记录(``photo_id`` + ``result='fail'``)—— 复查结论必须
    有照片当证据(D11),但"必填"是端点的事,这层不拦。
    """
    now = _now_iso()
    params = (STATUS_REINSPECT_FAILED, now, hazard_no, *_SOURCES_FAIL)
    return _run_transition(_FAIL_SQL, hazard_no, docs, now, params)


def start_resumption(hazard_no: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """复查合格、**曾经停过工** → ``resuming``(等《复工令》)。``pass_reinspection`` 的分支之一。

    ``AND was_suspended = 1`` 是这条路的门票:没停过工的隐患走到这里等于滥发复工令。
    调用方一般不该直接调它 —— 调 ``pass_reinspection``,让代码去挑边。
    """
    now = _now_iso()
    params = (STATUS_RESUMING, now, hazard_no, *_SOURCES_RESUMING)
    return _run_transition(_START_RESUMPTION_SQL, hazard_no, docs, now, params)


def close_after_pass(hazard_no: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """复查合格、**没停过工** → ``closed``,盖 ``closed_at``。``pass_reinspection`` 的另一条分支。

    ``AND was_suspended = 0`` 是这条路的门票:停过工的隐患从这里销项 = **漏发复工令**(Codex#6
    点名的失效模式)。``resuming`` 的行天然带 was_suspended=1,所以它绝不会从这条路溜走。
    """
    now = _now_iso()
    params = (STATUS_CLOSED, now, now, hazard_no, *_SOURCES_CLOSE_ON_PASS)
    return _run_transition(_CLOSE_ON_PASS_SQL, hazard_no, docs, now, params)


def pass_reinspection(hazard_no: str, *, docs: Sequence[DocDraft] = ()) -> str | None:
    """复查合格。返回实际落到的状态(``resuming`` / ``closed``);没命中返回 None。

    **分支由 ``was_suspended`` 决定,不由调用方决定**(Codex#6)—— 让端点自己挑边,早晚会挑错,
    而挑错的两种后果分别是漏发复工令和滥发复工令。两条分支的守卫互斥(``was_suspended`` 非 0
    即 1),先试哪条都一样;``docs``(通常是一条 ``reinspect`` 记录)跟着中标的那条进同一个事务。
    """
    if start_resumption(hazard_no, docs=docs):
        return STATUS_RESUMING
    if close_after_pass(hazard_no, docs=docs):
        return STATUS_CLOSED
    return None


def mark_resumed(hazard_no: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """签发《工程复工令》:``resuming`` → ``closed``,盖 ``closed_at``。

    起始状态只有 ``resuming`` 一个 —— 复工令是给停过工的隐患收尾的,``notified`` 那条路
    根本不该出现复工令。
    """
    now = _now_iso()
    params = (STATUS_CLOSED, now, now, hazard_no, *_SOURCES_RESUMED)
    return _run_transition(_RESUMED_SQL, hazard_no, docs, now, params)


def mark_escalated(hazard_no: str, *, docs: Sequence[DocDraft] = ()) -> bool:
    """拒不整改 → ``escalated``,签《监理报告》报主管部门。

    只能从 ``reinspect_failed`` 进:升级的举证链是「我通知过 + 期限到了 + 复查过 + 他没改」,
    没复查过就升级 = 拿一份建立在自己记乱账上的材料去指控施工方。
    """
    now = _now_iso()
    params = (STATUS_ESCALATED, now, hazard_no, *_SOURCES_ESCALATE)
    return _run_transition(_ESCALATE_SQL, hazard_no, docs, now, params)


# --- 文书 / 证据链 ------------------------------------------------------------


def docs_of(hazard_nos: Sequence[str]) -> list[HazardDocRow]:
    """一次取回若干条隐患的全部文书与复查记录(证据链)。

    **禁止在循环里逐条查**(CLAUDE.md 红线):上报主管部门要一次举证「通知过 + 期限到了 +
    复查过 + 他没改」,汇总页也是一屏一堆隐患,逐条查就是 N+1。空入参直接返回 ``[]`` ——
    ``IN ()`` 是语法错误,别让它去打库(一次别塞太多编号,SQLITE_MAX_VARIABLE_NUMBER,
    分批由调用方切)。按 ``hazard_no, id`` 排:同一条隐患的证据按发生先后排好,直接可读。
    """
    if not hazard_nos:
        return []
    query = (
        f"{_DOCS_BASE_SQL} WHERE hazard_no IN ({_placeholders(len(hazard_nos))}) {_DOCS_ORDER_BY}"
    )
    with _hazard_db() as conn:
        raw_rows = conn.execute(query, tuple(hazard_nos)).fetchall()
    return [HazardDocRow(*raw) for raw in raw_rows]


# --- 登记失败(Codex#10) -------------------------------------------------------


def record_ingest_failure(*, project_id: str, photo_id: str, item: str, reason: str) -> None:
    """记一条"这个违规项没能写进台账"。

    为什么落表而不是只打日志:进程一重启日志就统计不出来了,而这类失败恰恰是「隐患漏记」的
    唯一线索。识别结果本身照常返回(不堵死"看照片"这条已上线的演示主路径),失败项另外进
    Envelope 的 ``failed_items``(D10)。``reason`` 是内部细节(异常类型、约束名),给排查的人看,
    **不许出现在人话里**。若连这张表都写不进(整个 sqlite 挂了),就只剩日志了 —— 方案 §6.2 认了。
    """
    with _hazard_db() as conn:
        conn.execute(_INSERT_FAILURE_SQL, (project_id, photo_id, item, reason, _now_iso()))


def list_ingest_failures(*, project_id: str | None = None) -> list[IngestFailureRow]:
    """一次取回登记失败记录(可按项目筛)。给"事后统计漏了多少条"用。"""
    where = " WHERE project_id = ?" if project_id is not None else ""
    params = (project_id,) if project_id is not None else ()
    query = f"{_FAILURES_BASE_SQL}{where} {_FAILURES_ORDER_BY}"
    with _hazard_db() as conn:
        raw_rows = conn.execute(query, params).fetchall()
    return [IngestFailureRow(*raw) for raw in raw_rows]


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DOC_RESULTS",
    "DOC_TYPES",
    "GRADES",
    "GRADE_NORMAL",
    "GRADE_SEVERE",
    "STATUSES",
    "STATUS_CLOSED",
    "STATUS_ESCALATED",
    "STATUS_NOTIFIED",
    "STATUS_OPEN",
    "STATUS_PENDING",
    "STATUS_REINSPECT_FAILED",
    "STATUS_RESUMING",
    "STATUS_SUSPENDED",
    "DocDraft",
    "HazardDocRow",
    "HazardRow",
    "IngestFailureRow",
    "Registration",
    "can_transition",
    "close_after_pass",
    "confirm",
    "create",
    "delete_pending",
    "docs_of",
    "fetch",
    "list_ingest_failures",
    "list_rows",
    "mark_escalated",
    "mark_notified",
    "mark_reinspect_failed",
    "mark_resumed",
    "mark_suspended",
    "pass_reinspection",
    "record_ingest_failure",
    "set_grade",
    "start_resumption",
]
