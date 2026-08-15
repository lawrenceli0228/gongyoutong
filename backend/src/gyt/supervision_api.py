"""监理处置直连接口。**本模块头注是 ``POST /supervision/*`` 请求与响应契约的唯一真相。**

隐患台账与状态机的唯一真相在 ``gyt/db/hazards.py``,不在这里 —— 那边是存储与状态合法性,
这边是协议与业务判断(该走哪条路、期限算不算得出来、话怎么说给工地上的人听)。

===========================================================================
🔴 挂载:不 import 进 ``backend/webapp.py`` 就是全部 404,而且没有任何报错
===========================================================================
``langgraph.json`` 的 ``http.app`` **只能有一个**,现在指的是 ``./webapp.py:app``。
本模块导出 ``SUPERVISION_ROUTES``,由 webapp.py 铺进它的 ``routes=[...]``
(与 ``checkin_api.CHECKIN_ROUTES`` 同一手法)。删掉那一铺 = 监理端点整个消失,
而现象是 404、**不是启动报错**(langgraph 不知道有谁本该在)。

公网那一侧还要 ``Caddyfile`` 里 ``/api/supervision*`` 那条 route,
且**必须排在 ``handle_path /api/*`` 之前** —— handle 系列按书写顺序择一匹配,
排后面 = 永不生效且无报错。

===========================================================================
为什么写入动作全部走 HTTP,一个 LLM 都不经过(方案 §5.1)
===========================================================================
签发《监理通知单》《工程暂停令》是**法律行为**,不能由概率性系统单方面触发。
supervision Agent 只能查、只能建议(``list_hazards`` / ``get_hazard`` /
``suggest_disposal``),**任何产出文书或改状态的动作都不是 Agent 工具**。
与 W7「打卡写入不走对话链」同构 —— 那次理由是防编造和幂等,这次更硬。

===========================================================================
端点一览(路径不含 ``/api``:那一段由 Caddy 剥掉,与打卡链同规矩)
===========================================================================
全部 ``POST``、``Content-Type: application/json``、请求体是一个 JSON 对象::

    POST /supervision/confirm            {"hazard_nos": ["GYT-H-…", …]}   ← 也收单条 hazard_no
        pending → open(D17 的人工确认闸)。**支持批量,每条一个事务、互不牵连。**

    POST /supervision/grade              {"hazard_no": …, "grade": "一般"|"严重"}
        人工定级,清 needs_grading。**只允许在还没签过任何文书时改**
        (状态 pending / open;理由见 ``_GRADABLE_STATUSES``)。

    POST /supervision/notice             {"hazard_no": …, "due_phrase": "下周三"}
        《监理通知单》一份。grade=严重 **拒**(硬拦①)。

    POST /supervision/suspend            {"hazard_no": …, "due_phrase": "明天"}
        《通知单》+《工程暂停令》+《致建设单位报告》**三份原子产出**。
        grade≠严重 **拒**(硬拦②)。

    POST /supervision/reinspect-result   {"hazard_no": …, "result": "pass"|"fail",
                                          "after_photo_id": "<32位hex>"}
        复查结论。**结论由人下**(D11),``after_photo_id`` 必填且必须是真实产物。
        它**不出文书**:只往 hazard_docs 挂一条 ``reinspect`` 记录
        (那一行的 ``doc_no`` 长相刻意不像文书编号,见 ``_REINSPECT_NO_MARK``)。

    POST /supervision/resume             {"hazard_no": …}
        《工程复工令》。**只发给 status=resuming**(停过工 + 复查已合格)。

    POST /supervision/escalate           {"hazard_no": …}
        《监理报告》报主管部门,正文附 hazard_docs 完整证据链。只从 reinspect_failed 进。

响应码:

    200  ok=True
    400  INVALID_INPUT   缺字段 / 级别或结论不在词表 / **期限解析不出** / 照片编号不对
    401  UNAUTHORIZED    handler 自查令牌不过(纵深防御,同 checkin_api)
    404  NOT_FOUND       隐患编号查不到 / 复查照片查不到
    409  CONFLICT        三条硬拦、状态机不允许、并发把状态改掉了
    500  INTERNAL        兜底;编号摇不出来也落这里。user_msg 是人话,细节只进日志

===========================================================================
🔴 三条硬拦(全在服务端代码里,不靠提示词。Codex#3 / Codex#11)
===========================================================================
1. ``notice`` 对 ``grade='严重'`` **拒绝**,提示去走 ``suspend``;
2. ``suspend`` 对 ``grade!='严重'`` 拒绝;
3. **任何签发**(notice / suspend / resume / escalate)对 ``needs_grading=1`` 拒绝。

第 1 条与第 2 条是**两个方向相反的事故**,两向都要拦:
「严重隐患只发了通知单」= 该停工的没停,「一般隐患签了暂停令」= 平白停一片人的工。
第 3 条是 Codex#11:只标注不拦截的话,未知风险能按一般隐患走完整闭环并被销项。
解锁的唯一通道是 ``POST /supervision/grade``(人定级 + 清旗子)。

===========================================================================
🔴 ``due_phrase`` 收用户原话,交 ``agents/schedule/dates.py`` 算,解析不出就 fail
===========================================================================
**不许留空**(Codex#12):``due_date`` 为空的隐患永远进不了超期清单,
也就**永远不会被升级** —— 一条没人催的隐患躺在库里,报表上还显示「在办」。

``dates.py`` 用 314 行证明了模型换算中文日期不可靠,红线是「模型只传原话,代码来算」。
本模块**一行日期换算都不许自己写**,今天是哪天取自香港时间权威
(``attendance/receipt.py``,D7),不看宿主时区。

===========================================================================
🔴 Envelope 的 ``data`` 形状是冻结的(Codex#16),前端按 ``documents`` 渲染下载卡
===========================================================================
::

    {"ok": true, "data": {
        "hazard_no": "GYT-H-…",
        "status": "suspended",                 ← 动作完成后库里的真实状态
        "documents": [                         ← 顺序固定,一份一张卡
          {"doc_type": "notice",       "doc_no": "GYT-TZ-…", "artifact_id": "…",
           "filename": "监理通知单_GYT-TZ-….docx"},
          {"doc_type": "suspension",   "doc_no": "GYT-ZT-…", "artifact_id": "…", "filename": "…"},
          {"doc_type": "owner_report", "doc_no": "GYT-JS-…", "artifact_id": "…", "filename": "…"}]},
     "user_msg": "…", "error_code": null}

定死数组形状是为了**避免静默丢件**:三份文书里少出一份,若形状是三个独立的键,
前端少渲一张卡没有任何异常;是数组的话「N 张卡」与「N 份文书」天然对齐。

两处例外,都在下面写明:
  · ``confirm`` 是批量动作,``data`` = ``{"confirmed": [...], "failed": [{…}]}``;
  · ``grade`` 与 ``reinspect-result`` 不出文书,``documents`` 恒为 ``[]``。
    **复查记录不进 ``documents``** —— 它没有 artifact_id,进去就是一张点不开的卡。

===========================================================================
🔴 文件先落盘、库后写(方案 §6.4,Codex#7)
===========================================================================
::

    ① 三份 docx 渲染到内存(docgen 是纯函数,不落盘)
    ② artifacts.register ×3 → 三个 artifact_id   ← 文件写失败:整个操作 fail,库一行没动
    ③ 一个事务:写 hazard_docs ×3 + 改 status     ← 库写失败:留三个孤儿文件,可接受

取舍:**孤儿文件(有文件无记录)比孤儿记录(有记录无文件)安全得多** ——
后者会让证据链里出现一份点不开的「文书」,而那是要拿去追责的材料。
孤儿文件的清理器本批不做(方案 §8 明记)。

事务盖不住 ``artifacts.register`` 的文件与 sidecar 写入,所以顺序是唯一的保证;
写库真的失败时**状态一定没变**(db 层一个事务里改状态 + 挂文书,见 ``_run_transition``)。

===========================================================================
鉴权、限流、并发
===========================================================================
- **鉴权两道**:第一道是 langgraph 的 ``enable_custom_route_auth``(``langgraph.json``
  里那个键漏了就整条裸奔且无报错);第二道是本模块 handler 自查,判定与 ``auth.py``
  完全同源(都用 ``core/access.py``:空 / 占位符 / 过短 = 未配置 = 放行)。
- **不设限流桶。** 与打卡不同:这些端点在登录闸 + ``X-Api-Key`` 之后,而且状态机本身
  就是闸(同一条隐患签过一次,第二次会被迁移守卫拒掉),刷不出量来。
  哪天要加,照抄 ``checkin_api.get_global_limiter`` 那只桶的写法,连测试夹具一起抄。
- **并发**:「先读一次做判断」与「写」之间有窗口,真正说了算的是 db 层
  ``UPDATE … WHERE status IN (…)`` 的 rowcount —— 本模块拿到 False 一律回 409
  「状态刚被改过」,**绝不信先读的那份快照**。
- 阻塞活(sqlite、docx 渲染、落盘)一律 ``run_in_threadpool``:这些路由和图跑在同一个
  事件循环里,langgraph 的 blockbuster 会把同步 IO 判成违规并抛 BlockingError
  (schedule / cad 都踩过同款坑)。

===========================================================================
这个文件有点长 —— 下一刀该切在哪
===========================================================================
本文件比 ``checkin_api.py`` 还大一圈,主要胖在「文书正文写什么」那一段
(``_hazard_meta`` / ``_evidence_section`` / 五个 ``_sections_*`` / ``_render_doc``,
约两百行,其中大半是中文正文本身,不是逻辑)。

**那一段就是天然的接缝**:它只依赖 ``docgen`` 与一个 ``_DocContext``,与鉴权、
状态机、原子性一点关系都没有。哪天再加两种文书(F4 的定期汇总、专项报告),
就把它整段搬去 ``agents/supervision/documents.py``,本文件只留「协议 + 判断 + 编排」。
现在不搬的理由只有一条:S4 这条泳道说好只碰四个文件,而 ``agents/supervision/``
另有人在动 —— 为了行数去抢一个并行泳道的目录,不划算。
"""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Any, Final, NamedTuple

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.agents.schedule.dates import DueParseError, format_display, parse_due
from gyt.agents.supervision.docgen import (
    SUPERVISION_DISCLAIMER,
    SUPERVISION_SIGNATURE_LINES,
    Section,
    TableSection,
    TextSection,
    render_document,
)
from gyt.attendance.receipt import TimeSnapshot, make_snapshot
from gyt.core import artifacts
from gyt.core.access import _api_key_from_headers, effective_access_token
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.core.doc_no import DOC_TITLE_ZH, DocKind, DocNoExhaustedError, generate_unique
from gyt.core.errors import Envelope, ErrorCode, fail, ok
from gyt.db import hazards
from gyt.db import projects as projects_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 受控词表与常量(禁止在函数体里散落字面量)
# ---------------------------------------------------------------------------

_STATUS_ZH: Final[dict[str, str]] = {
    hazards.STATUS_PENDING: "待确认",
    hazards.STATUS_OPEN: "已确认待处置",
    hazards.STATUS_NOTIFIED: "已签发通知单",
    # 🔴 「已出具暂停令」不是「已责令停工」(db/hazards.py 的 STATUSES 头注):
    # suspended 只证明文书出了稿,不证明工地真停了工。对外措辞不许升级。
    hazards.STATUS_SUSPENDED: "已出具暂停令",
    hazards.STATUS_REINSPECT_FAILED: "复查不合格",
    hazards.STATUS_RESUMING: "待签发复工令",
    hazards.STATUS_CLOSED: "已销项",
    hazards.STATUS_ESCALATED: "已上报主管部门",
}
"""状态 → 给工地上的人看的中文。八个状态每个都要有名字。"""

_UNNAMED_STATUSES: Final[tuple[str, ...]] = tuple(
    s for s in hazards.STATUSES if s not in _STATUS_ZH
)
if _UNNAMED_STATUSES:  # pragma: no cover —— 只在 db 层加了状态而这里漏配时触发
    raise RuntimeError(
        f"状态 {_UNNAMED_STATUSES} 没有中文名 —— db/hazards.py 的 STATUSES 加了档,"
        "本模块的 _STATUS_ZH 要跟上。做成导入时硬失败是刻意的:漏配的表现是"
        "回给工友的话里冒出一个英文状态词,而那不会有任何报错"
    )

_ISSUING_KINDS: Final[tuple[DocKind, ...]] = (
    DocKind.NOTICE,
    DocKind.SUSPENSION,
    DocKind.RESUMPTION,
    DocKind.OWNER_REPORT,
    DocKind.AUTHORITY_REPORT,
)
"""本模块会签发的五种文书。``DocKind.HAZARD`` 不在里面 —— 那是隐患自己的身份号,不是文书。

``core/doc_no.py`` 头注写明:这五档 ``kind.name.lower()`` 恰好落在
``hazards.DOC_TYPES`` 里,**但别写一个"六档一一对应"的循环** —— 两头各有一个孤儿
(``HAZARD`` 不进 doc_type,``reinspect`` 没有类型段)。下面那条导入时校验只查这五档。
"""

_UNKNOWN_DOC_TYPES: Final[tuple[str, ...]] = tuple(
    k.name.lower() for k in _ISSUING_KINDS if k.name.lower() not in hazards.DOC_TYPES
)
if _UNKNOWN_DOC_TYPES:  # pragma: no cover —— 两张词表漂了才触发
    raise RuntimeError(
        f"文书类型 {_UNKNOWN_DOC_TYPES} 不在 hazards.DOC_TYPES 里 —— "
        "写库时会撞 CHECK,而那要到真签发那一刻才炸;这里在导入时就拦下"
    )

_GRADABLE_STATUSES: Final[frozenset[str]] = frozenset({hazards.STATUS_PENDING, hazards.STATUS_OPEN})
"""允许人工改级的状态:**还没签过任何文书的那两档**。

db 层刻意不管这件事(``set_grade`` 的头注:「文书都签发了还能不能改级」是业务判断)。
这里定成"签发前才可改",理由是本批**不做重签**:通知单已经按「一般」出了稿,
库里却改成「严重」,证据链当场自相矛盾,而那份纸还在工地上贴着。
真定错了,走复查/升级流程,或按新证据另立一条隐患(新照片、新编号)。
"""

_MAX_CONFIRM_BATCH: Final[int] = 200
"""一次批量确认的条数上限。防的是一个巨大的 JSON 数组把一次请求拖成几百个事务。
200 远大于一次巡检能出的隐患数,够用且有边界。"""

_MAX_REASON_ECHO: Final[int] = 2
"""全批都没确认成时,user_msg 里最多复述几条原因。多了就成一堵墙,工友读不完。"""

_REINSPECT_TAIL_BYTES: Final[int] = 4
"""复查行 ``doc_no`` 的随机尾字节数(8 位十六进制)。"""

_REINSPECT_NO_MARK: Final[str] = "#FC-"
"""复查记录行的编号分隔标记 —— 长相刻意**不像文书编号**(``GYT-XX-…``)。

``hazard_docs.doc_no`` 是 NOT NULL UNIQUE,复查行也得占一格;而 ``core/doc_no.py``
的 ``DocKind`` **没有 reinspect 这一档**(方案 §6.3 的编号表里复查行没有类型段),
它的头注同时警告「别在调用点就地拼一个文书编号的样子:就地拼的格式没人给它写正则,
将来挂守卫时认不出来」。

所以这里给的是 ``<隐患编号>#FC-<8位hex>``:全局唯一(隐患编号本身唯一 + 随机尾),
而且**一眼看得出不是文书编号** —— 没人会拿它去对 ``PATTERNS`` 里任何一条正则。
⚠️ 哪天复查要发正式编号,回 ``core/doc_no.py`` 加一档 ``DocKind`` 并同步
``DOC_TITLE_ZH``,**别把这个格式改成 ``GYT-XX-`` 的样子**。
"""

_SIGN_RETRY_MAX: Final[int] = 2
"""文书编号撞库后,整轮(重摇号 + 重渲染 + 重落盘)最多再来几次。

为什么必须重渲染而不是只换库里那一列:**编号印在文书正文里**(元信息表第一行)。
只换库里的号,纸上的号和台账就对不上 —— 那份文书自己证伪自己
(与 ``checkin_api._persist_checkin`` 的「换编号从水印起重来」同一条理由)。
上一轮已落盘的文件成孤儿,交清理器,§6.4 认了这笔账。

为什么是 2 而不是「转到成功」:4 位随机尾 + 秒级时刻,连撞多次几乎一定不是运气问题,
一直转只会把一次可诊断的失败拖成一个挂死的请求(同 ``doc_no.DEFAULT_ATTEMPTS`` 的账)。
"""

_UNASSIGNED_PROJECT_ZH: Final[str] = "(未归属项目)"
"""``project_id`` 为空串时文书上的写法(D6:未归属是空串,不是 NULL)。
留白会让人以为这一格漏填了,写明白它就是一条待归属的隐患。"""

_MSG_DOC_NO_EXHAUSTED: Final[str] = (
    "这次没能给文书排上编号,请过几秒再点一次。文书没有出,隐患状态也没变。"
)
"""编号摇不出来时的人话。

后半句是关键:**明确告诉工友"什么都没发生"**,否则他会以为文书已经出了一半,
去翻列表找一份并不存在的文书。``DocNoExhaustedError`` 的原文只进日志
(它的头注:异常文本别原样透给用户)。
"""

_MSG_RACED: Final[str] = "这条隐患的状态刚被改过(可能有人同时在处理),刷新一下再看看。"
"""db 层 rowcount=0 时的人话 —— 先读的那份快照说可以、写的时候不行,就是并发。"""


# ---------------------------------------------------------------------------
# 端点允许的起始状态 —— 声明在这里,导入时对着 db 的迁移表校验
# ---------------------------------------------------------------------------


def _sources(target: str, *sources: str) -> frozenset[str]:
    """声明"某个端点允许的起始状态",并当场校验它是 ``ALLOWED_TRANSITIONS`` 的子集。

    手法照搬 ``db/hazards.py`` 的 ``_sources_for``(那个是私有的,不跨模块 import)。
    为什么端点这一侧还要自己声明一遍、而不是直接问 ``can_transition``:
    端点往往**比迁移表更窄**。``notified → closed`` 是合法边(复查合格直接销项),
    但《复工令》端点只该接 ``resuming`` —— 拿 ``can_transition`` 当判据的话,
    一条 ``notified`` 的隐患会先在这里被放行,再被 db 的 WHERE 拦下,
    工友拿到的是「状态刚被改过」这种驴唇不对马嘴的提示。

    **但永远不许更宽**:更宽的那一条就是绕过状态机的后门。这里在**导入时**炸掉,
    进程起不来 —— 与 db 层同一手法:少了同步的东西不会有任何运行期报错,那就让它连起都起不来。
    """
    illegal = tuple(
        s for s in sources if target not in hazards.ALLOWED_TRANSITIONS.get(s, frozenset())
    )
    if illegal:  # pragma: no cover —— 只在有人改错状态机表时触发
        raise RuntimeError(
            f"端点声明的起始状态 {illegal} 在 db 的迁移表里到不了 {target};"
            "supervision_api 与 db/hazards.py 的状态机漂了,先对齐那张表"
        )
    return frozenset(sources)


_NOTICE_FROM: Final = _sources(hazards.STATUS_NOTIFIED, hazards.STATUS_OPEN)
_SUSPEND_FROM: Final = _sources(hazards.STATUS_SUSPENDED, hazards.STATUS_OPEN)
# 复工令只接 resuming:它是给**停过工**的隐患收尾的。
_RESUME_FROM: Final = _sources(hazards.STATUS_CLOSED, hazards.STATUS_RESUMING)
# 升级只从 reinspect_failed 进:举证链是「我通知过 + 期限到了 + 复查过 + 他没改」,
# 没复查过就升级 = 拿一份建立在自己记乱账上的材料去指控施工方。
_ESCALATE_FROM: Final = _sources(hazards.STATUS_ESCALATED, hazards.STATUS_REINSPECT_FAILED)
_REINSPECT_FAIL_FROM: Final = _sources(
    hazards.STATUS_REINSPECT_FAILED,
    hazards.STATUS_NOTIFIED,
    hazards.STATUS_SUSPENDED,
    hazards.STATUS_REINSPECT_FAILED,
)
# 复查合格有两条出口(closed / resuming),由 db 按 was_suspended 挑边 —— 这里取并集:
# 端点只负责判断「现在能不能复查」,**绝不自己挑边**(Codex#6:挑错的两种后果分别是
# 漏发复工令和滥发复工令)。
_REINSPECT_PASS_FROM: Final = _sources(
    hazards.STATUS_CLOSED, hazards.STATUS_NOTIFIED, hazards.STATUS_REINSPECT_FAILED
) | _sources(hazards.STATUS_RESUMING, hazards.STATUS_SUSPENDED, hazards.STATUS_REINSPECT_FAILED)


# ---------------------------------------------------------------------------
# 响应外壳与"被拒"这件事的表达
# ---------------------------------------------------------------------------


class _Result(NamedTuple):
    """一次处置动作的结果:HTTP 状态码 + 四键信封。线程池里的同步活返回它。"""

    status: int
    envelope: Envelope


class _Refused(Exception):
    """这次动作被拦下了,带着要回给前端的状态码与信封。

    为什么用异常而不是 union 返回值:签发是「查 → 四道闸 → 算期限 → 渲染 → 落盘 → 写库」
    一条直线,每道闸都可能拦。用返回值表达的话每步后面都要跟一个 isinstance 分支,
    真正的业务顺序会被淹掉。而这个异常**不出本模块** —— 每个动作都在 ``_guarded``
    里被接住转成 ``_Result``,不存在"拿异常当控制流还漏到外面"的风险。
    """

    def __init__(self, status: int, envelope: Envelope) -> None:
        super().__init__(envelope["user_msg"])
        self.status = status
        self.envelope = envelope


def _refuse(status: int, code: ErrorCode, user_msg: str, *, detail: str = "") -> _Refused:
    """拼一个 ``_Refused``。``detail`` 只进日志(``fail()`` 保证它不进返回值)。"""
    return _Refused(status, fail(code, user_msg, detail=detail))


def _respond(env: Envelope, status: int) -> JSONResponse:
    """Envelope → JSON 响应。进了 handler 之后所有出口都走这里,一个都不许裸拼 dict。"""
    return JSONResponse(env, status_code=status)


def _zh(status: str) -> str:
    """状态 → 中文。词表在导入时已校验齐全,这里的兜底只为不让展示层炸掉。"""
    return _STATUS_ZH.get(status, status)


# ---------------------------------------------------------------------------
# 鉴权:纵深防御的第二道(与 checkin_api._deny_if_token_bad 同源同判据)
# ---------------------------------------------------------------------------


def _deny_if_token_bad(request: Request) -> JSONResponse | None:
    """handler 自查令牌。放行返回 None,不过返回 401。

    第一道在 langgraph 的鉴权中间件(``langgraph.json`` 的
    ``enable_custom_route_auth: true``);这道防的是那个键被漏配 —— 漏配时第一道
    **整条消失且没有任何报错**,只有这道自查兜得住,也只有它能被单元测试钉死。

    判定与 ``auth.py`` / ``checkin_api.py`` 完全同源(同一份 ``core/access.py``):
    空 / 占位符 / 过短 = 未配置 = 放行 —— ``make dev`` 与真机验收不发令牌头,
    「未配置也拦」等于当场打死本机联调。

    ⚠️ 每次请求现取(``effective_access_token`` 内部走 ``get_settings()``),
    **不许在 import 时读成常量** —— 那样测试换环境变量测不到,生产换口令要重启才生效。

    拒绝文案取 ``DEFAULT_USER_MSG[UNAUTHORIZED]``,与 ``auth.DENY_MESSAGE`` /
    ``attendance/messages.DENY`` 一字不差(同源清单里那条:任何差异都是送给爆破脚本的信号)。
    """
    expected = effective_access_token()
    if not expected:
        return None
    presented = _api_key_from_headers(request.headers)
    # compare_digest:恒定时间比对,不给逐位试探留统计量;encode 成 bytes 防非 ASCII
    # 口令把 401 变成 500(理由原样见 auth.authenticate)。
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return None
    logger.warning(
        "监理接口鉴权失败:path=%s 原因=%s",
        request.url.path,
        "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
    )
    return _respond(fail(ErrorCode.UNAUTHORIZED), 401)


# ---------------------------------------------------------------------------
# 请求体解析
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> dict[str, Any]:
    """收一个 JSON 对象。坏 JSON / 不是对象一律 400,不猜。"""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 —— 坏 JSON 的异常类型随实现变,一律收敛成 400
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "这次提交的内容后台没读懂,刷新一下页面再试一次。",
            detail=f"请求体不是合法 JSON:{exc}",
        ) from exc
    if not isinstance(body, dict):
        raise _refuse(400, ErrorCode.INVALID_INPUT, "这次提交的内容格式不对,刷新一下再试。")
    return body


def _text(body: dict[str, Any], key: str) -> str:
    """取一个字符串字段并去掉首尾空白;缺失 / 非字符串都归一成空串,由各处自己说人话。"""
    value = body.get(key)
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# 四道闸:隐患存在 → 已定级 → 级别方向 → 状态机
# ---------------------------------------------------------------------------


def _require_hazard(hazard_no: str) -> hazards.HazardRow:
    """取隐患行;编号为空或查不到都在这里拦下。"""
    if not hazard_no:
        raise _refuse(400, ErrorCode.INVALID_INPUT, "没说是哪条隐患(缺隐患编号)。")
    row = hazards.fetch(hazard_no)
    if row is None:
        raise _refuse(404, ErrorCode.NOT_FOUND, f"没找到隐患「{hazard_no}」,核对一下编号。")
    return row


def _require_graded(row: hazards.HazardRow) -> None:
    """硬拦③:``needs_grading=1`` 的隐患,**任何签发都拒**(Codex#11)。

    必须排在级别方向那两道闸**之前**:没定级的行 ``grade`` 是映射表给的默认值(一般),
    先报「这条是一般隐患,不能签暂停令」会把人带去改级别以外的地方,而真正要做的是定级。
    """
    if row.needs_grading:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」还没定级(现场判的是「{row.severity}」),"
            "先在界面上定成一般隐患或严重隐患,再签文书。",
        )


def _refuse_severe_notice(row: hazards.HazardRow) -> None:
    """硬拦①:严重隐患**不许只签通知单**,得走三文书那条路。"""
    if row.grade == hazards.GRADE_SEVERE:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」是严重隐患,只发一份通知单不够,"
            "要走「签发暂停令」(通知单 + 暂停令 + 致建设单位报告,一次三份)。",
        )


def _refuse_normal_suspend(row: hazards.HazardRow) -> None:
    """硬拦②:一般隐患**不许签暂停令** —— 平白停一片人的工。"""
    if row.grade != hazards.GRADE_SEVERE:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」是{row.grade}隐患,不能签暂停令,"
            "走「签发监理通知单」就够了。确实要停工,先把它改定为严重隐患。",
        )


def _require_status(row: hazards.HazardRow, allowed: frozenset[str], action: str) -> None:
    """状态机闸:当前状态不在这个动作的起始状态集里就拦下,并把当前状态说成人话。

    ⚠️ 这只是**给人话用的前置判断**。真正说了算的是 db 层 UPDATE 的 rowcount ——
    先读与写之间有并发窗口,拿到 False 一律回 409(见 ``_MSG_RACED``)。
    """
    if row.status not in allowed:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」现在是「{_zh(row.status)}」,这一步({action})做不了。",
        )


def _resolve_due(due_phrase: str, today: date) -> tuple[str, str]:
    """用户原话 → ``(ISO 日期, 「8月20日(周四)」)``。**解析不出就拒,绝不留空。**

    换算全部交给 ``agents/schedule/dates.py``(Codex#12 点名复用),本模块一行日期
    数学都不写。``DueParseError`` 的文本本来就是给工地师傅看的人话(还附了正确写法),
    原样透出去比重新包装一层有用得多。
    """
    if not due_phrase:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "得写明整改期限(比如「明天」「3天后」「下周三」「月底」)。"
            "没有期限的隐患不会进超期清单,也就永远不会有人来催。",
        )
    try:
        resolved = parse_due(due_phrase, today=today)
    except DueParseError as exc:
        raise _refuse(400, ErrorCode.INVALID_INPUT, str(exc)) from exc
    if resolved is None:  # pragma: no cover —— 空串上面已拦,这里纯防御
        raise _refuse(400, ErrorCode.INVALID_INPUT, "这个期限没看懂,换个说法,比如「下周三」。")
    return resolved.isoformat(), format_display(resolved)


def _require_photo(after_photo_id: str) -> str:
    """复查照片必填,而且必须是**真实存在的产物**(方案 §5.2 红线)。

    只校验"非空"是不够的:编号打错一位照样非空,而复查合格是能把隐患销项的 ——
    「隐患已消除」被写进留档文书,现场却原样没动。真去 ``read_meta`` 问一次,
    编号错就在这儿拦下。
    """
    if not after_photo_id:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "复查要挂一张整改后的现场照片:先把照片传上来,再拿它的编号来下结论。",
        )
    try:
        artifacts.read_meta(after_photo_id)
    except ArtifactNotFound as exc:
        raise _refuse(
            404,
            ErrorCode.NOT_FOUND,
            "没找到这张复查照片,核对一下照片编号,或者重新传一张。",
            detail=f"复查照片 {after_photo_id!r} 取不到:{exc}",
        ) from exc
    return after_photo_id


# ---------------------------------------------------------------------------
# 文书正文 —— 版式归 docgen(纯函数、不落盘),这里只管填什么
# ---------------------------------------------------------------------------


class _DocContext(NamedTuple):
    """渲染一次动作的全部素材。一次动作一份,三份文书共用它(时刻、期限必须一致)。"""

    row: hazards.HazardRow
    project_name: str
    signed_display: str  # 「2026-08-16 09:30:00」—— 签发时刻,与编号里的时刻同一快照
    due_display: str  # 「8月20日(周四)」;复工令/监理报告没有新期限,为空串
    evidence: tuple[hazards.HazardDocRow, ...]  # 已挂在这条隐患上的文书与复查记录


class _IssuedDoc(NamedTuple):
    """一份已经落盘、等着写库的文书。``documents`` 数组里的一项就是它。"""

    doc_type: str
    doc_no: str
    artifact_id: str
    filename: str

    def as_payload(self) -> dict[str, str]:
        """冻结契约里那四个键,顺序即字段顺序。"""
        return {
            "doc_type": self.doc_type,
            "doc_no": self.doc_no,
            "artifact_id": self.artifact_id,
            "filename": self.filename,
        }


def _hazard_meta(kind: DocKind, doc_no: str, ctx: _DocContext) -> tuple[tuple[str, str], ...]:
    """五种文书共用的元信息表。

    ⚠️ 「文书编号」必须印在文书上:它是这份纸的对外身份,监理在电话里报它、
    上报主管部门时按它排证据链。也正因为印在正文里,撞号重试必须连正文一起重渲染
    (见 ``_SIGN_RETRY_MAX``)。
    """
    meta: list[tuple[str, str]] = [
        (f"{DOC_TITLE_ZH[kind]}编号", doc_no),
        ("隐患编号", ctx.row.hazard_no),
        ("工程项目", ctx.project_name),
        ("隐患事项", ctx.row.item),
        ("隐患级别", f"{ctx.row.grade}(现场判定:{ctx.row.severity})"),
        ("签发时间", ctx.signed_display),
    ]
    if ctx.due_display:
        meta.append(("整改期限", ctx.due_display))
    meta.append(("现场照片编号", ctx.row.photo_id))
    return tuple(meta)


def _evidence_section(ctx: _DocContext) -> TableSection:
    """证据链表:这条隐患名下已有的文书与复查记录,按发生先后(db 已排好序)。

    《监理报告》靠它举证「我通知过 + 期限到了 + 复查过 + 他没改」。空的时候出一句话
    而不是一张空表 —— 空表在留档文件里读起来像「这一栏还没填」。
    """
    return TableSection(
        heading="处置经过(已出具的文书与复查记录)",
        header=("序号", "类型", "编号", "结论", "时间"),
        rows=tuple(
            (
                str(index),
                _doc_type_zh(doc.doc_type),
                doc.doc_no,
                doc.result or "—",
                doc.created_at,
            )
            for index, doc in enumerate(ctx.evidence, start=1)
        ),
        empty_note="本条隐患名下暂无已出具的文书或复查记录。",
    )


def _doc_type_zh(doc_type: str) -> str:
    """``hazard_docs.doc_type`` → 中文。文书五档取自 ``DOC_TITLE_ZH``(唯一那张表),
    复查行不是文书、``DocKind`` 里没有它,单独给一个名字。"""
    if doc_type == "reinspect":
        return "复查记录"
    for kind in _ISSUING_KINDS:
        if kind.name.lower() == doc_type:
            return DOC_TITLE_ZH[kind]
    return doc_type  # pragma: no cover —— 词表外的类型原样透出,不猜


def _find_doc_no(ctx: _DocContext, doc_type: str) -> str:
    """从证据链里找某类文书的编号(复工令要引用原《工程暂停令》)。找不到给一句话,不留空白。"""
    for doc in ctx.evidence:
        if doc.doc_type == doc_type:
            return doc.doc_no
    return "(未找到)"


def _sections_notice(ctx: _DocContext) -> tuple[Section, ...]:
    return (
        TextSection(
            heading="一、隐患情况",
            body=f"经现场巡查,发现「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患。"
            f"该隐患首次发现时间为 {ctx.row.found_at},现场照片编号 {ctx.row.photo_id}。",
        ),
        TextSection(
            heading="二、整改要求",
            body=f"请施工单位于 {ctx.due_display} 前完成整改,并留存整改后的现场照片备查。"
            "整改期间应采取临时防护措施,防止事态扩大。",
        ),
        TextSection(
            heading="三、复查安排",
            body="整改完成后请及时报项目监理机构复查。复查以整改后的现场照片为凭,"
            "由监理人员判定;复查合格方可销项。逾期未改或复查不合格的,按规定升级处理。",
        ),
    )


def _sections_suspension(ctx: _DocContext) -> tuple[Section, ...]:
    return (
        TextSection(
            heading="一、暂停理由",
            body=f"现场存在「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患,"
            "继续施工可能造成事故,依据监理职责签发本暂停令。",
        ),
        TextSection(
            heading="二、暂停范围",
            body="与本条隐患相关的作业面暂停施工。暂停期间应保持现场安全状态,不得擅自恢复作业。",
        ),
        TextSection(
            heading="三、复工条件",
            body=f"请于 {ctx.due_display} 前完成整改。隐患整改完毕并经项目监理机构复查合格后,"
            "由项目监理机构另行签发《工程复工令》,方可复工。",
        ),
    )


def _sections_owner_report(ctx: _DocContext) -> tuple[Section, ...]:
    return (
        TextSection(
            heading="一、报告事项",
            body=f"现场发现「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患。"
            "项目监理机构已就此签发《监理通知单》与《工程暂停令》,现向建设单位报告。",
        ),
        TextSection(
            heading="二、整改期限",
            body=f"要求施工单位于 {ctx.due_display} 前完成整改,整改后经复查合格方可复工。",
        ),
        TextSection(
            heading="三、请建设单位配合事项",
            body="请建设单位督促施工单位落实整改,并协调整改所需的人力与资源;"
            "施工单位拒不整改的,项目监理机构将按规定报工程所在地建设主管部门。",
        ),
        _evidence_section(ctx),
    )


def _sections_resumption(ctx: _DocContext) -> tuple[Section, ...]:
    return (
        TextSection(
            heading="一、复查结论",
            body=f"「{ctx.row.item}」经复查已整改完毕,现场条件具备复工要求。"
            f"原《工程暂停令》编号 {_find_doc_no(ctx, DocKind.SUSPENSION.name.lower())}。",
        ),
        TextSection(
            heading="二、复工范围",
            body="原暂停施工的相关作业面即日起可恢复施工。复工后应加强自检,防止同类隐患再次发生。",
        ),
        _evidence_section(ctx),
    )


def _sections_authority_report(ctx: _DocContext) -> tuple[Section, ...]:
    return (
        TextSection(
            heading="一、事由",
            body=f"现场存在「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患。"
            "项目监理机构已按规定签发文书要求整改,施工单位逾期未改或复查不合格,"
            "现依据监理职责报工程所在地建设主管部门。",
        ),
        _evidence_section(ctx),
        TextSection(
            heading="三、请予处理事项",
            body="请建设主管部门予以核查处理。项目监理机构将继续跟踪该隐患的整改情况,"
            "并保存全部影像与文书资料备查。",
        ),
    )


_SECTION_BUILDERS: Final[dict[DocKind, Callable[[_DocContext], tuple[Section, ...]]]] = {
    DocKind.NOTICE: _sections_notice,
    DocKind.SUSPENSION: _sections_suspension,
    DocKind.OWNER_REPORT: _sections_owner_report,
    DocKind.RESUMPTION: _sections_resumption,
    DocKind.AUTHORITY_REPORT: _sections_authority_report,
}
"""种类 → 正文各节。**五种文书每种都要有**(下面导入时校验)。

漏一档的表现是签发时 KeyError → 兜底 500,工友只看到「系统开小差」,
而真正的毛病是这张表少了一行 —— 所以在导入时就拦。
"""

_MISSING_BUILDERS: Final[tuple[str, ...]] = tuple(
    k.name for k in _ISSUING_KINDS if k not in _SECTION_BUILDERS
)
if _MISSING_BUILDERS:  # pragma: no cover
    raise RuntimeError(f"文书 {_MISSING_BUILDERS} 没有正文构造函数,_SECTION_BUILDERS 漏了")


def _render_doc(kind: DocKind, doc_no: str, ctx: _DocContext) -> bytes:
    """渲染一份文书,返回字节。**纯函数:不落盘、不改入参**(§6.4 ① 的前提)。

    免责句与签字栏一律用监理那两件 —— ``docgen.render_document`` 的 ``disclaimer``
    必填无默认值,结构上不可能"漏传一个参数于是悄悄套上了巡检记录那句"。
    """
    return render_document(
        title=f"工程监理{DOC_TITLE_ZH[kind]}"
        if kind is DocKind.AUTHORITY_REPORT
        else DOC_TITLE_ZH[kind],
        meta=_hazard_meta(kind, doc_no, ctx),
        sections=_SECTION_BUILDERS[kind](ctx),
        disclaimer=SUPERVISION_DISCLAIMER,
        signature_lines=SUPERVISION_SIGNATURE_LINES,
    )


# ---------------------------------------------------------------------------
# 签发编排:① 渲染 → ② 落盘 → ③ 一个事务写库(§6.4),撞号则整轮重来
# ---------------------------------------------------------------------------


def _draw_doc_no(kind: DocKind, snap: TimeSnapshot, taken: set[str]) -> str:
    """摇一个文书编号,并保证**本次动作内**不自撞。

    ⚠️ ``exists`` 只看本次已摇出来的号,**不查全表** —— ``db/hazards.py`` 没有
    「这个 doc_no 占了没」的查询入口,而绕过存储层直连库是更坏的选择(S4 不动 db/)。
    真正的兜底本来就是建表时那条 ``doc_no TEXT NOT NULL UNIQUE`` + 下面
    ``_sign`` 对 ``sqlite3.IntegrityError`` 的整轮重试 —— ``generate_unique`` 的头注
    第 3 条写得很清楚:它只是把绝大多数碰撞挡在写库之前,不是替代唯一索引。

    这一层挡的是**三份文书自己撞上自己**:三份共用一个时间快照(编号里的时刻必须一致,
    否则同一次签发在纸面上变成三个时间点),差别只剩 4 位随机尾。
    """
    doc_no = generate_unique(kind, taken.__contains__, snap=snap)
    taken.add(doc_no)
    return doc_no


def _issue_documents(
    kinds: Sequence[DocKind], ctx: _DocContext, snap: TimeSnapshot
) -> list[_IssuedDoc]:
    """① 全部渲染到内存,② 再逐份落盘。**两步严格分开。**

    合成一步(渲一份存一份)的后果:第二份渲染炸掉时第一份已经在盘上,而它既没进库
    也没人认识它 —— 而分开之后,渲染阶段任何异常都发生在一个字节都还没落盘之前。
    """
    taken: set[str] = set()
    drafted = [(kind, _draw_doc_no(kind, snap, taken)) for kind in kinds]
    rendered = [(kind, doc_no, _render_doc(kind, doc_no, ctx)) for kind, doc_no in drafted]

    issued: list[_IssuedDoc] = []
    for kind, doc_no, payload in rendered:
        filename = f"{DOC_TITLE_ZH[kind]}_{doc_no}.docx"
        # kind=REPORT:与巡检记录同类(系统生成的 docx 产物)。**不用 DOCUMENT** ——
        # 那一档在 webapp.py 里是"用户上传的规范/任务书",混进去资料库会把监理文书
        # 列成规范。也不用 ATTENDANCE:清理器只删那一类,文书要长期留档。
        artifact_id = artifacts.register(payload, kind=ArtifactKind.REPORT, original_name=filename)
        issued.append(
            _IssuedDoc(
                doc_type=kind.name.lower(),
                doc_no=doc_no,
                artifact_id=artifact_id,
                filename=filename,
            )
        )
    return issued


def _sign(
    kinds: Sequence[DocKind],
    ctx: _DocContext,
    snap: TimeSnapshot,
    apply: Callable[[list[hazards.DocDraft]], bool],
) -> list[_IssuedDoc]:
    """签发 N 份文书并落库。返回 ``documents`` 数组的素材(顺序 = ``kinds`` 的顺序)。

    ``apply`` 是 db 层那一个事务(``mark_notified`` / ``mark_suspended`` / …):
    状态迁移 + 挂文书一起写,迁移没命中就一行都不写。

    三种结局:
      · 正常     → 返回落盘 + 落库都成了的那几份;
      · 撞号     → ``sqlite3.IntegrityError``(``doc_no UNIQUE``),整轮重来
                   (重摇号 + **重渲染**,因为编号印在正文里),旧文件成孤儿;
      · 状态被抢 → ``apply`` 回 False,409。**库一行没动**,盘上留孤儿文件(§6.4 认了)。
    """
    for attempt in range(1 + _SIGN_RETRY_MAX):
        issued = _issue_documents(kinds, ctx, snap)
        drafts = [
            hazards.DocDraft(doc_type=d.doc_type, doc_no=d.doc_no, artifact_id=d.artifact_id)
            for d in issued
        ]
        try:
            moved = apply(drafts)
        except sqlite3.IntegrityError as exc:
            # doc_no 撞了库里已有的号。db 层刻意不吞这个异常(它的头注:吞了就等于把
            # "该重试"变成静默失败),重试就在这里。
            logger.warning("文书编号撞库(第 %d 次),换随机尾重来:%s", attempt + 1, exc)
            continue
        if not moved:
            raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)
        return issued
    raise _refuse(
        500,
        ErrorCode.INTERNAL,
        _MSG_DOC_NO_EXHAUSTED,
        detail=f"文书编号连续 {1 + _SIGN_RETRY_MAX} 轮撞库,疑似随机源或台账异常",
    )


def _context(row: hazards.HazardRow, snap: TimeSnapshot, due_display: str) -> _DocContext:
    """凑齐渲染素材:项目名 + 证据链。两次查询,不在循环里逐条查。"""
    project = projects_db.get_project(row.project_id) if row.project_id else None
    return _DocContext(
        row=row,
        project_name=project.name if project is not None else _UNASSIGNED_PROJECT_ZH,
        signed_display=snap.display,
        due_display=due_display,
        evidence=tuple(hazards.docs_of([row.hazard_no])),
    )


def _issued_payload(
    hazard_no: str, status: str, issued: Iterable[_IssuedDoc], user_msg: str
) -> _Result:
    """冻结契约那三个键 + 四键信封。**所有出文书的动作都从这一个出口走。**

    散在各处拼 dict 的下场是某一处漏掉 ``documents`` 或换了键名,而前端只是少渲一张卡、
    不报错(Codex#16 要冻结形状的原因)。
    """
    return _Result(
        200,
        ok(
            {
                "hazard_no": hazard_no,
                "status": status,
                "documents": [d.as_payload() for d in issued],
            },
            user_msg,
        ),
    )


# ---------------------------------------------------------------------------
# 七个动作的同步实现(全部在线程池里跑)
# ---------------------------------------------------------------------------


def _work_confirm(body: dict[str, Any]) -> _Result:
    """pending → open 的人工确认闸(D17)。**支持批量,每条一个事务、互不牵连。**

    没有这一步,safety 看一眼照片就能开启法律流程 —— 自动登记的东西必须有人认过。
    """
    raw = body.get("hazard_nos")
    if raw is None:
        single = _text(body, "hazard_no")
        raw = [single] if single else []
    if not isinstance(raw, list):
        raise _refuse(400, ErrorCode.INVALID_INPUT, "隐患编号要放在一个列表里。")
    # 去重保序:界面上双击会把同一条发两遍,不去重的话第二条会被报成「不用再确认」,
    # 看起来像出了错,其实什么问题都没有。
    nos = list(dict.fromkeys(str(x).strip() for x in raw if str(x).strip()))
    if not nos:
        raise _refuse(400, ErrorCode.INVALID_INPUT, "没说要确认哪几条隐患。")
    if len(nos) > _MAX_CONFIRM_BATCH:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            f"一次最多确认 {_MAX_CONFIRM_BATCH} 条,分几次来。",
        )

    confirmed: list[str] = []
    failed: list[dict[str, str]] = []
    for no in nos:
        # 先试着确认(db 的 WHERE status='pending' 是硬守卫),**失败了才回查**去解释原因 ——
        # 反过来先查再改就是每条两次库,而 db 层本来就要求"每条一个事务"。
        if hazards.confirm(no):
            confirmed.append(no)
            continue
        row = hazards.fetch(no)
        reason = (
            "查不到这条隐患,核对一下编号。"
            if row is None
            else f"这条现在是「{_zh(row.status)}」,不用再确认。"
        )
        failed.append({"hazard_no": no, "reason": reason})

    if not confirmed:
        # 全军覆没:``fail()`` 的 data 恒为 None(信封契约),原因只能进 user_msg,
        # 所以最多复述两条 —— 再多就成一堵墙,工友读不完。
        echo = "".join(
            f"{item['hazard_no']}:{item['reason']}" for item in failed[:_MAX_REASON_ECHO]
        )
        more = (
            f"(还有 {len(failed) - _MAX_REASON_ECHO} 条)" if len(failed) > _MAX_REASON_ECHO else ""
        )
        raise _refuse(409, ErrorCode.CONFLICT, f"这 {len(failed)} 条都没能确认。{echo}{more}")

    tail = f",另有 {len(failed)} 条没确认成(可能已经确认过了)" if failed else ""
    return _Result(
        200,
        ok(
            {"confirmed": confirmed, "failed": failed},
            f"已确认 {len(confirmed)} 条隐患{tail}。确认后就能签发文书了。",
        ),
    )


def _work_grade(body: dict[str, Any]) -> _Result:
    """人工定级:改 grade 并清掉 ``needs_grading`` —— 未定级隐患唯一的解锁通道。"""
    hazard_no = _text(body, "hazard_no")
    grade = _text(body, "grade")
    if grade not in hazards.GRADES:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            f"级别只能填「{hazards.GRADE_NORMAL}」或「{hazards.GRADE_SEVERE}」。",
        )
    row = _require_hazard(hazard_no)
    if row.status not in _GRADABLE_STATUSES:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{hazard_no}」已经签过文书(现在是「{_zh(row.status)}」),不能改级别 —— "
            "改了的话已发出去的那份文书就和台账对不上了。定错了请走复查或升级流程。",
        )
    if not hazards.set_grade(hazard_no, grade):  # pragma: no cover —— 与否决并发才走到
        raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)

    hint = (
        "可以签发《工程暂停令》了(一次出三份文书)。"
        if grade == hazards.GRADE_SEVERE
        else "可以签发《监理通知单》了。"
    )
    return _Result(
        200,
        ok(
            {
                "hazard_no": hazard_no,
                "status": row.status,
                "grade": grade,
                "needs_grading": False,
                "documents": [],  # 定级不出文书,但形状保持一致,前端不用分叉
            },
            f"隐患「{hazard_no}」已定为{grade}隐患。{hint}",
        ),
    )


def _work_notice(body: dict[str, Any]) -> _Result:
    """《监理通知单》一份(grade=一般 那条路)。"""
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③ —— 必须排在级别方向之前
    _refuse_severe_notice(row)  # 硬拦①
    _require_status(row, _NOTICE_FROM, "签发监理通知单")
    due_date, due_display = _resolve_due(_text(body, "due_phrase"), snap.stamp.date())

    ctx = _context(row, snap, due_display)
    issued = _sign(
        (DocKind.NOTICE,),
        ctx,
        snap,
        lambda drafts: hazards.mark_notified(row.hazard_no, due_date, docs=drafts),
    )
    logger.info("隐患 %s 已签发通知单 %s(期限 %s)", row.hazard_no, issued[0].doc_no, due_date)
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_NOTIFIED,
        issued,
        f"《监理通知单》已出稿(编号 {issued[0].doc_no}),整改期限 {due_display}。"
        "这份文书要总监理工程师签字盖章后才是正式文件。",
    )


def _work_suspend(body: dict[str, Any]) -> _Result:
    """三份文书原子产出(grade=严重 那条路):通知单 → 暂停令 → 致建设单位报告。

    顺序是冻结契约的一部分(前端按数组渲染下载卡),也是文书之间的引用顺序。
    """
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③
    _refuse_normal_suspend(row)  # 硬拦②
    _require_status(row, _SUSPEND_FROM, "签发工程暂停令")
    due_date, due_display = _resolve_due(_text(body, "due_phrase"), snap.stamp.date())

    ctx = _context(row, snap, due_display)
    issued = _sign(
        (DocKind.NOTICE, DocKind.SUSPENSION, DocKind.OWNER_REPORT),
        ctx,
        snap,
        lambda drafts: hazards.mark_suspended(row.hazard_no, due_date, docs=drafts),
    )
    logger.info(
        "隐患 %s 已出具暂停令三文书:%s(期限 %s)",
        row.hazard_no,
        "、".join(d.doc_no for d in issued),
        due_date,
    )
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_SUSPENDED,
        issued,
        "三份文书已出稿:《监理通知单》《工程暂停令》《致建设单位报告》,"
        f"整改期限 {due_display}。"
        # 🔴 措辞红线:出稿 ≠ 停工。签字盖章之前不得据以停工(D15 的免责句同一条边界)。
        "⚠️ 暂停令要总监理工程师签字盖章后才能据以停工,现在只是出了稿。",
    )


def _work_reinspect_result(body: dict[str, Any]) -> _Result:
    """复查结论(D11:模型给建议,**人下结论**,照片是证据)。不出文书,只留一条复查记录。

    合格之后落到哪个状态**由 db 按 ``was_suspended`` 挑边**(closed / resuming),
    本模块绝不自己挑 —— 挑错的两种后果分别是漏发复工令和滥发复工令(Codex#6)。
    """
    hazard_no = _text(body, "hazard_no")
    result = _text(body, "result")
    if result not in hazards.DOC_RESULTS:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "复查结论只能是合格(pass)或不合格(fail),得由人来下。",
        )
    photo_id = _require_photo(_text(body, "after_photo_id"))
    row = _require_hazard(hazard_no)
    passed = result == "pass"
    _require_status(
        row,
        _REINSPECT_PASS_FROM if passed else _REINSPECT_FAIL_FROM,
        "登记复查结论",
    )

    for attempt in range(1 + _SIGN_RETRY_MAX):
        draft = hazards.DocDraft(
            doc_type="reinspect",
            doc_no=f"{row.hazard_no}{_REINSPECT_NO_MARK}{secrets.token_hex(_REINSPECT_TAIL_BYTES)}",
            photo_id=photo_id,
            result=result,
        )
        try:
            if passed:
                landed = hazards.pass_reinspection(row.hazard_no, docs=[draft])
            else:
                landed = (
                    hazards.STATUS_REINSPECT_FAILED
                    if hazards.mark_reinspect_failed(row.hazard_no, docs=[draft])
                    else None
                )
        except sqlite3.IntegrityError as exc:  # pragma: no cover —— 8 位随机尾撞库,天文数字
            logger.warning("复查记录编号撞库(第 %d 次),换随机尾重来:%s", attempt + 1, exc)
            continue
        if landed is None:
            raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)
        logger.info("隐患 %s 复查结论 %s → %s", row.hazard_no, result, landed)
        return _Result(
            200,
            ok(
                {
                    "hazard_no": row.hazard_no,
                    "status": landed,
                    # 复查记录**不进 documents**:它没有 artifact_id,进去就是一张点不开的卡。
                    "documents": [],
                    "result": result,
                    "after_photo_id": photo_id,
                },
                _reinspect_msg(landed, row.item),
            ),
        )
    raise _refuse(  # pragma: no cover
        500,
        ErrorCode.INTERNAL,
        _MSG_DOC_NO_EXHAUSTED,
        detail="复查记录编号连续撞库,疑似随机源异常",
    )


def _reinspect_msg(landed: str, item: str) -> str:
    """复查落到哪一站,就说哪一句 —— 三条路各有下一步,含糊一句话工友就不知道还要不要干活。"""
    if landed == hazards.STATUS_CLOSED:
        return f"复查合格,隐患「{item}」已销项。"
    if landed == hazards.STATUS_RESUMING:
        return f"复查合格。这条停过工,还要签发《工程复工令》才算完,隐患「{item}」暂不销项。"
    return f"已记为复查不合格,隐患「{item}」继续跟踪;逾期不改可以上报主管部门。"


def _work_resume(body: dict[str, Any]) -> _Result:
    """《工程复工令》—— 只发给 ``resuming``(停过工 + 复查已合格)。"""
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③ 同样适用:复工令也是签发
    _require_status(row, _RESUME_FROM, "签发工程复工令")

    ctx = _context(row, snap, due_display="")  # 复工令不设新期限
    issued = _sign(
        (DocKind.RESUMPTION,),
        ctx,
        snap,
        lambda drafts: hazards.mark_resumed(row.hazard_no, docs=drafts),
    )
    logger.info("隐患 %s 已签发复工令 %s", row.hazard_no, issued[0].doc_no)
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_CLOSED,
        issued,
        f"《工程复工令》已出稿(编号 {issued[0].doc_no}),这条隐患已销项。"
        "文书要总监理工程师签字盖章后才是正式文件。",
    )


def _work_escalate(body: dict[str, Any]) -> _Result:
    """《监理报告》报主管部门 —— 只从 ``reinspect_failed`` 进,正文附完整证据链。"""
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③
    _require_status(row, _ESCALATE_FROM, "上报主管部门")

    ctx = _context(row, snap, due_display="")
    issued = _sign(
        (DocKind.AUTHORITY_REPORT,),
        ctx,
        snap,
        lambda drafts: hazards.mark_escalated(row.hazard_no, docs=drafts),
    )
    logger.info("隐患 %s 已升级上报,监理报告 %s", row.hazard_no, issued[0].doc_no)
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_ESCALATED,
        issued,
        f"《监理报告》已出稿(编号 {issued[0].doc_no}),附了这条隐患的完整处置经过。"
        "文书要总监理工程师签字盖章后再报建设主管部门。",
    )


# ---------------------------------------------------------------------------
# handler 外壳 —— 七个端点共用
# ---------------------------------------------------------------------------

_Work = Callable[[dict[str, Any]], _Result]


def _guarded(work: _Work) -> _Work:
    """把同步活里两种"预期之内的失败"收敛成 ``_Result``。

    ``DocNoExhaustedError`` 单独接一档:它的头注写明**异常文本别原样透给用户**
    (「连着摇了 5 次…编号都已经被占用」不是工地上的人该读的东西),
    所以原文进 detail、只进日志,用户拿到 ``_MSG_DOC_NO_EXHAUSTED``。
    ⚠️ **绝不许降级成「用最后那个撞了的号继续」** —— 两份不同的文书顶着同一个编号,
    比这次没出成严重得多。
    """

    def _run(body: dict[str, Any]) -> _Result:
        try:
            return work(body)
        except _Refused as refused:
            return _Result(refused.status, refused.envelope)
        except DocNoExhaustedError as exc:
            logger.warning("文书编号摇不出来:%s", exc)
            return _Result(
                500,
                fail(ErrorCode.INTERNAL, _MSG_DOC_NO_EXHAUSTED, detail=str(exc)),
            )

    return _run


async def _handle(request: Request, work: _Work) -> JSONResponse:
    """七个端点共用的外壳:令牌自查 → 收 JSON → 阻塞活挪进线程池 → 兜底 500。

    ⚠️ 阻塞活(sqlite、docx 渲染、落盘)一步都不许留在事件循环里:这些路由和图跑在
    同一个循环上,langgraph 的 blockbuster 会抛 BlockingError(schedule/cad 都踩过)。
    """
    try:
        denied = _deny_if_token_bad(request)
        if denied is not None:
            return denied
        body = await _json_body(request)
        result = await run_in_threadpool(_guarded(work), body)
        return _respond(result.envelope, result.status)
    except _Refused as refused:
        # 只有 _json_body 会在线程池之外抛它(body 解析在 async 侧)。
        return _respond(refused.envelope, refused.status)
    except Exception:
        # 兜底:任何没料到的炸都收敛成 500 信封。堆栈只进日志;user_msg 走
        # DEFAULT_USER_MSG[INTERNAL](已是人话),不在这里另造第二句。
        logger.exception("监理处置请求处理失败:%s", request.url.path)
        return _respond(fail(ErrorCode.INTERNAL), 500)


# 七个 handler。首行都不用反引号:starlette 生成 /docs 时把 docstring 喂给 yaml,
# ` 开头必炸(无害,但每次启动打两条 traceback,查日志的人会被带偏 —— 同 checkin_api)。


async def post_confirm(request: Request) -> JSONResponse:
    """POST /supervision/confirm —— 监理确认(pending → open),支持批量。"""
    return await _handle(request, _work_confirm)


async def post_grade(request: Request) -> JSONResponse:
    """POST /supervision/grade —— 人工定级,清掉 needs_grading。"""
    return await _handle(request, _work_grade)


async def post_notice(request: Request) -> JSONResponse:
    """POST /supervision/notice —— 签发《监理通知单》(严重隐患拒,走 suspend)。"""
    return await _handle(request, _work_notice)


async def post_suspend(request: Request) -> JSONResponse:
    """POST /supervision/suspend —— 三份文书原子产出(一般隐患拒)。"""
    return await _handle(request, _work_suspend)


async def post_reinspect_result(request: Request) -> JSONResponse:
    """POST /supervision/reinspect-result —— 复查结论,照片必填。"""
    return await _handle(request, _work_reinspect_result)


async def post_resume(request: Request) -> JSONResponse:
    """POST /supervision/resume —— 签发《工程复工令》(仅 resuming)。"""
    return await _handle(request, _work_resume)


async def post_escalate(request: Request) -> JSONResponse:
    """POST /supervision/escalate —— 签发《监理报告》报主管部门(仅 reinspect_failed)。"""
    return await _handle(request, _work_escalate)


# ---------------------------------------------------------------------------
# 路由表 —— 真正挂上去的入口是 backend/webapp.py,不是下面那个 app
# ---------------------------------------------------------------------------

SUPERVISION_ROUTES: Final[list[Route]] = [
    Route("/supervision/confirm", post_confirm, methods=["POST"]),
    Route("/supervision/grade", post_grade, methods=["POST"]),
    Route("/supervision/notice", post_notice, methods=["POST"]),
    Route("/supervision/suspend", post_suspend, methods=["POST"]),
    Route("/supervision/reinspect-result", post_reinspect_result, methods=["POST"]),
    Route("/supervision/resume", post_resume, methods=["POST"]),
    Route("/supervision/escalate", post_escalate, methods=["POST"]),
]
"""监理处置这七条路由。**必须被 ``backend/webapp.py`` 铺进它的 routes**。

``langgraph.json`` 的 ``http.app`` 只能有一个(现在指 webapp.py),所以本项目所有
自定义路由都在那里汇合:项目/图纸/资料管理是它自己的,打卡链是 ``CHECKIN_ROUTES``,
监理处置是这一份。

⚠️ webapp.py 那侧删掉这一铺,监理端点就整个消失 —— 而现象是 **404,不是启动报错**
(langgraph 不知道有谁本该在)。改这里的路径同样要去 webapp.py 与 ``Caddyfile``
的 ``/api/supervision*`` 那条 route 一起确认。
"""

app = Starlette(routes=list(SUPERVISION_ROUTES))
"""只给本模块的单元测试用(``TestClient(app)``),**线上不走它**。

留着的理由与 ``checkin_api.app`` 一样:测试要能脱开 webapp.py 单独验鉴权与七个动作,
而 webapp.py 在 backend/ 根、不属于 gyt 包,把它拖进单测会连带整个项目管理栈。
"""


__all__ = [
    "SUPERVISION_ROUTES",
    "app",
    "post_confirm",
    "post_escalate",
    "post_grade",
    "post_notice",
    "post_reinspect_result",
    "post_resume",
    "post_suspend",
]
