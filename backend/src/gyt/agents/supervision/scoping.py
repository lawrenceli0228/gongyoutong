"""监理隐患的**筛子、超期判定与中文名** —— 对话链与 HTTP 端点共用的那块地面。

===========================================================================
它为什么存在:一份判据,两个调用方
---------------------------------------------------------------------------
``supervision_api.py`` 是被 langgraph 按**文件路径**加载的 HTTP 层。W10 给它加的
三个查询端点(``GET /supervision/hazards`` 等)必须和 ``agents/supervision/tools.py``
的 ``list_hazards`` 用**同一套**筛子与超期判据 —— 判据再抄一份,后端筛出来的清单
与对话里报的条数就会悄悄对不上,而两边测试都绿(TODO-45 A 组记的就是这类漂移:
13 条评审发现里没有一条是行覆盖能抓的)。所以要有一块两边都够得着的地面。

===========================================================================
🔴 本模块的 import 约束:自己一行 langchain / langgraph / python-docx 都不许有
---------------------------------------------------------------------------
**只许** import 这几样:

    gyt.db.hazards               受控词表与 HazardRow(状态机的真相在那边)
    gyt.agents.schedule.dates    期限的展示形态(format_display)
    gyt.attendance.receipt       香港时区 HK(全仓唯一时间权威)
    标准库

**不许** import 本包的任何模块(``tools.py`` / ``documents.py`` / ``docgen.py`` /
``__init__.py``)。``tests/unit/test_supervision_scoping.py`` 用 AST 扫本文件的
import 语句钉死了这两条。

这条约束买到的两样东西,一样已经兑现、一样还没有,别搞混:

  · **不成环 —— 已经兑现,今天就在起作用。** 现有的边全是向下的:
    ``supervision_api → documents → scoping``、``tools → scoping``。
    本模块反过来 import ``tools`` 的那一刻(``tools`` import 本模块)立刻成环,
    而循环导入炸出来的报错指向的往往是第三个文件,查起来极费劲。
  · **让 HTTP 层真的轻量 —— 还没兑现,是留着的一条路。**
    ⚠️ 2026-08-16 实测:``import gyt.agents.supervision.scoping`` **照样会把
    langchain / langgraph 拉起来**。原因不在本文件 —— Python 会先执行父包的
    ``__init__.py``,而 ``agents/supervision/__init__.py`` 顶部 import 了
    ``tools.SUPERVISION_TOOLS``(它要在那儿把 Agent 建出来给 ``graph.AGENT_REGISTRY`` 用)。
    这笔账是既成事实,不是本次搬迁带来的:``supervision_api.py`` 早就 import 了
    本包的 ``documents``,同样触发那个 ``__init__.py``。真要摘干净得把包
    ``__init__.py`` 里建 Agent 那段延迟到函数里 —— **那是另一件事,别在这儿顺手改**
    (``graph.py:147`` 直接从包名 import ``build_supervision_agent``)。
    在那之前,别拿"反正已经拉了 langchain"当理由往本文件加 import:
    加了就把那条路堵死了,而堵死这件事同样不会有任何报错。

===========================================================================
这次(2026-08-16,S1)收敛掉了什么 —— 新事实,别照着旧注释理解
---------------------------------------------------------------------------
搬进来之前,下面这几样是**散在两三个文件里的拷贝**:

  · ``STATUS_ZH``(八档状态中文名)—— TODO-45 A 组记的「三份拷贝」之二。
    第一份在 ``supervision_api.py`` 的 ``_STATUS_ZH``,第二份原在
    ``agents/supervision/tools.py``。2026-08-16 用 AST 逐条比对过:
    **两份的键、值、顺序、内嵌注释逐字相同**(唯一差别是模块别名
    ``hazards.`` / ``db.``)。所以这一份是它们的合并结果,不是第三份。
    ``supervision_api.py`` 那份归另一条泳道删,删了改成 import 本模块即可。
    第三份是前端的 ``scripts/frontend-overrides/supervision-lib.ts``,
    跨语言镜像,收敛不掉(它得跟着这里手工对齐,CLAUDE.md 同源清单有登记)。
    tools.py 里那段「为什么暂时收敛不掉」的老注释已随搬迁删除 —— 它说的前提
    (「让 supervision_api 再 import tools 就成了环」)本来就只对 tools.py 成立,
    对一个零依赖的叶子模块不成立。
  · ``RESULT_ZH`` / ``result_zh()``(pass/fail → 合格/不合格)—— 原在
    ``documents.py``。搬过来是因为 W10 的详情端点也要念这两个词,留在那儿
    它就成了第三份。顺带把 ``NO_VALUE``(那个「—」)一起搬了:``result_zh``
    离了它就得再抄一遍「—」,而抄一遍正是这次要消灭的东西。
  · 筛子五词(2026-09-18 加「待复查」)、超期判定、``hazard_item()`` 的对外形状 —— 原在 ``tools.py``,
    改成公开名搬过来。tools.py 的 ``SCOPE_*`` / ``SCOPES`` 仍然转出去
    (它的 ``__all__`` 里有,老调用点不必改),真相在这里。

⚠️ **搬迁本身没改任何判据。** 边界(严格小于)、三处排除(pending / needs_grading /
resuming)、``hazard_item`` 的键集合一个字节都没动 —— 改判据要连同
``supervision_api.py`` 的四道闸一起想,那是另一件事。

===========================================================================
「今天」为什么不是 date.today()(原文照搬自 tools.py,一个字没改)
---------------------------------------------------------------------------
``due_date`` 是**香港日历日**(D7:全仓唯一时间权威 = ``attendance/receipt.py``,
``db/hazards.py`` 盖 ``found_at`` 用的也是它)。超期判定要和期限用同一本日历,
否则跨午夜的窗口里"今天到期"会被算成"已超期" —— 而超期是升级、是对外指控
施工方拒不整改的起点,差一天就是冤枉人。**不许改回 ``date.today()``**
(那是宿主时区,容器 Shanghai 与本机数值一致纯属巧合)。

⚠️ 与 ``agents/schedule/tools.py`` 的 ``_today()`` 刻意**不同源**:那边管任务台账,
今天用的是宿主本地日期;这边管的是要写进法律文书的期限。别为了"统一"合并。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Final

# 期限的展示形态与 schedule 同源:「8月20日(周四)」这种带星期字的说法**只有**
# dates.format_display 产得出来,模型照抄即可。自己换算星期是 schedule 那边
# 用 314 行证明过不可靠的事,监理这边的期限还要进文书,更不能让模型算。
# (supervision_api.py 解析 due_phrase 时 import 的也是这个模块,同一本日历。)
from gyt.agents.schedule.dates import format_display
from gyt.attendance.receipt import HK
from gyt.db import hazards as db

# ---------------------------------------------------------------------------
# 受控词表(禁止在函数体里散落字面量)
# ---------------------------------------------------------------------------

STATUS_ZH: Final[dict[str, str]] = {
    db.STATUS_PENDING: "待确认",
    db.STATUS_OPEN: "已确认待处置",
    db.STATUS_NOTIFIED: "已签发通知单",
    # 🔴 「已出具暂停令」不是「已责令停工」(db/hazards.py 的 STATUSES 头注):
    # suspended 只证明文书出了稿,不证明工地真停了工。对外措辞不许升级。
    db.STATUS_SUSPENDED: "已出具暂停令",
    db.STATUS_REINSPECT_FAILED: "复查不合格",
    db.STATUS_RESUMING: "待签发复工令",
    db.STATUS_CLOSED: "已销项",
    db.STATUS_ESCALATED: "已上报主管部门",
}
"""状态 → 给工地上的人看的中文。**八个状态每个都要有名字。**

这里是 Python 侧的**唯一真相**(2026-08-16 S1 起)。``agents/supervision/tools.py``
原来那份已经删了;``supervision_api.py`` 那份等另一条泳道删,删之前两份逐字相同
(比对结论见模块头注)。前端 ``supervision-lib.ts`` 的那份是跨语言镜像,
改中文名要手工带上它。

⚠️ 下面那道守卫**只数键、不比值** —— 它拦得住「db 加了一档而这里漏配」,
拦不住「两份拷贝的中文名漂开」。后者只能靠拷贝数量本身减少来防,
这也正是把它搬到这儿的理由(TODO-45 A 组)。
"""

_UNNAMED_STATUSES: Final[tuple[str, ...]] = tuple(s for s in db.STATUSES if s not in STATUS_ZH)
if _UNNAMED_STATUSES:  # pragma: no cover —— 只在 db 层加了状态而这里漏配时触发
    raise RuntimeError(
        f"状态 {_UNNAMED_STATUSES} 没有中文名 —— db/hazards.py 的 STATUSES 加了档,"
        "agents/supervision/scoping.py 的 STATUS_ZH 要跟上。做成导入时硬失败是刻意的:"
        "漏配的表现是回给工友的话里冒出一个英文状态词,而那不会有任何报错"
    )

NO_VALUE: Final[str] = "—"
"""「这一格本来就没有内容」的写法。空单元格在留档文件里读起来像漏填。

原在 ``documents.py``(名字是 ``_NO_VALUE``),随 ``result_zh`` 一起搬过来 ——
留在那边的话本模块就得再抄一个「—」,而"别抄第二份"正是这次搬迁的全部目的。
``documents.py`` 现在从这儿 import 它,证据链表里那两处用法一个字没改。
"""

RESULT_ZH: Final[dict[str, str]] = {
    "pass": "合格",
    "fail": "不合格",
}
"""``hazard_docs.result`` → 中文。**留档文书上、回给工友的话里都不许出现英文枚举值。**

这张表 2026-08-16 由代码评审补在 ``documents.py``:证据链那一列原来是
``doc.result or _NO_VALUE``,把库里的 ``pass`` / ``fail`` **原样印到纸上** ——
而那份纸是要报建设主管部门的。同一次搬到这儿,因为 W10 的隐患详情端点
也要念这两个词,留在 documents.py 它就成了第三份拷贝。

⚠️ 键集必须等于 ``db.hazards.DOC_RESULTS`` —— 下面有导入期硬失败守着。
判据只验**覆盖**(每个取值都有中文名),验不了「两处中文名一致」。
"""

_MISSING_RESULT_ZH: Final[tuple[str, ...]] = tuple(r for r in db.DOC_RESULTS if r not in RESULT_ZH)
if _MISSING_RESULT_ZH:  # pragma: no cover —— 配齐了就到不了这里
    raise RuntimeError(
        f"hazards.DOC_RESULTS 里这些取值没有中文名:{_MISSING_RESULT_ZH};"
        "agents/supervision/scoping.py 的 RESULT_ZH 要跟上。做成导入时硬失败是刻意的 ——"
        "漏配的表现是留档文书上印出一个英文单词,而那是要送到建设主管部门手里的纸,"
        "没有任何测试会因为「纸上有个英文词」而变红。"
    )

SCOPE_ACTIVE: Final[str] = "在办"
SCOPE_PENDING: Final[str] = "待确认"
SCOPE_REINSPECT: Final[str] = "待复查"
SCOPE_OVERDUE: Final[str] = "超期"
SCOPE_ALL: Final[str] = "全部"

SCOPES: Final[tuple[str, ...]] = (
    SCOPE_ACTIVE,
    SCOPE_PENDING,
    SCOPE_REINSPECT,
    SCOPE_OVERDUE,
    SCOPE_ALL,
)
"""隐患清单的筛子,**只认这五个词**(默认「在办」)。顺序 = 一条隐患走过的先后
(待确认 → 待复查 → 超期),前端 ``HAZARD_SCOPES`` 逐字镜像、按同一顺序摆按钮。

「待复查」是 2026-09-18 按用户反馈加的第五档:「整改完后的过程应该另有一个待复查清单,
不和待确认混在一起」。两张清单上的人要做的事完全不同 —— 待确认是「看照片判是不是隐患」,
待复查是「拿整改后的照片下合格/不合格结论」;混在「在办」里,监理翻一屏才找得到
今天该去复查哪几条。

刻意做成受控词表而不是自由文本:模型自己发明筛子(「严重的」「这周的」)时,
静默按"全部"处理会让监理以为清单就这么多。词表外一律回一句人话让它换个词。

⚠️ HTTP 端点收到词表外的 scope 时也得**明确拒绝**,不许静默回落到「全部」——
理由与对话链完全一样:少给的清单在界面上看不出来少了。
"""

CLOSED_STATUSES: Final[tuple[str, ...]] = (db.STATUS_CLOSED, db.STATUS_ESCALATED)
"""「在办」要排除的两档 —— 状态机里的两个终点(``ALLOWED_TRANSITIONS`` 里出口是空集)。"""

REINSPECT_STATUSES: Final[tuple[str, ...]] = (
    db.STATUS_NOTIFIED,
    db.STATUS_SUSPENDED,
    db.STATUS_REINSPECT_FAILED,
)
"""「待复查」= **已经下过整改期限、等监理去现场复查**的三档(含复查过一次不合格的)。

三处不显然的排除,每一处都对应一条定案:
  · ``pending`` —— D17:自动登记的还没人确认,不算进整改率、不进超期清单;
  · ``open`` —— 还没签文书、没下期限,施工方还不知道要改,谈不上复查;
  · ``resuming`` —— 复查**已经合格**了,只是在等《复工令》。再列进待复查会让人
    再去复查一次;算成超期更糟 = 拿"我们自己还没签复工令"去指控施工方拒不整改。
"""

OVERDUE_STATUSES: Final[tuple[str, ...]] = REINSPECT_STATUSES
"""哪些状态才可能"超期"= 待复查那三档,**一个字不差**。

这不是本仓反复警告的那种「今天碰巧相等、别合并」(``REASSIGNABLE_STATUSES`` vs
``GRADABLE_STATUSES``)—— 它是**定义**:超期 = 待复查 ∧ 期限已过。一条隐患只有在
等复查的时候才谈得上「过了期限还没改好」,所以超期清单永远是待复查清单的子集
(``test_supervision_scoping.py`` 钉着这层包含关系)。哪天两张表真要分开,先回答
「哪一档能超期却不用复查」—— 答不上来就别分。

``needs_grading=1`` 的排除(Codex#11:没定级的隐患任何签发都被硬拒,催它没有意义)
写在 ``is_overdue`` 里,不在这张表。
"""


# ---------------------------------------------------------------------------
# 时间与展示
# ---------------------------------------------------------------------------


def today_hk() -> date:
    """监理口径的「今天」= 香港日历日(与 due_date 同一本日历)。

    ⚠️ **调用方一律写 ``scoping.today_hk()`` 这种模块属性调用,别
    ``from ... import today_hk``。** 测试把「今天」钉死靠的就是 monkeypatch
    ``scoping.today_hk`` 这一个点;按名 import 的调用点在导入那一刻就把函数对象
    绑死了,桩打不进去 —— 表现是那条用例跟着跑测试的真实日子飘,今天绿明天红。
    """
    return datetime.now(HK).date()


def due_display(due_date: str | None) -> str | None:
    """ISO 期限 → 「8月20日(周四)」。没定期限就是 None(不是空串,少一种"看着像有"的形态)。"""
    return format_display(date.fromisoformat(due_date)) if due_date else None


def due_defaults(today: date, *, normal_days: int, severe_days: int) -> dict[str, str]:
    """签发文书时**按级别预填**的整改期限:``{级别: ISO 日期}``,两档都给。

    2026-09-18 用户反馈的两句话合成这一件:「期限最好是选日期的形式而不是自己填,
    不然格式不一致后面很难统计」+「思考下如何减少人工填报」。做法是清单端点把
    这两个日期给前端,日期框里预填上,监理不改就直接签。

    · 天数是 ``config.py`` 的两个旋钮,**由端点传进来** —— 本模块的 import 白名单里
      没有 config,而且纯函数一眼能测;
    · 用 ``today`` 参数而不是在里面调 ``today_hk()``:与清单里的 ``today`` 同一个快照,
      跨过午夜那一秒两个数才不会各说各话;
    · 返回 ISO 串而不是 ``date``:前端把它**原样**塞进 ``due_phrase`` 送回来,
      ``dates.py`` 的 ISO 分支认的就是这种写法(它只拒过去的日期,所以 0 天合法)。
      前端因此**一行日期换算都不用写**,天数也不用镜像。
    """
    return {
        db.GRADE_NORMAL: (today + timedelta(days=normal_days)).isoformat(),
        db.GRADE_SEVERE: (today + timedelta(days=severe_days)).isoformat(),
    }


def result_zh(result: str | None) -> str:
    """复查结论 → 中文。``None`` 是文书行(它本来就没有结论),给「—」而不是空格。

    词表外的取值**原样透出**,与 ``documents._doc_type_zh`` 同一个哲学:不猜。
    真出现了说明 CHECK 约束被绕过,留着那个怪值比悄悄改成「合格」安全得多。
    """
    if result is None:
        return NO_VALUE
    return RESULT_ZH.get(result, result)


# ---------------------------------------------------------------------------
# 筛子与超期
# ---------------------------------------------------------------------------


def is_overdue(row: db.HazardRow, today_iso: str) -> bool:
    """这条隐患超期没有。纯 Python 比较,**不往 SQL 里塞「今天」**(方案 §5.2)。

    ISO 文本序 = 日期序,所以直接比字符串。边界是**严格小于**:今天到期不算超期 ——
    差一天就会把还在期限内的人写成"拒不整改",而那句话是要进上报材料的。
    """
    if row.needs_grading:  # Codex#11:没定级的签发都被硬拒,催它没有意义
        return False
    return bool(row.due_date) and row.status in OVERDUE_STATUSES and str(row.due_date) < today_iso


def in_scope(row: db.HazardRow, *, scope: str, today_iso: str) -> bool:
    """受控筛子。五个词各自的判据都在这一处,别散到调用点去。

    ⚠️ 不认识的 scope **落在「在办」这一档**(函数末尾那条 return)。所以调用方
    **必须先拿 ``SCOPES`` 把野词拦掉**,别指望这里替你报错 —— 静默按「在办」筛
    与静默按「全部」筛一样糟,少给的清单在界面上和对话里都看不出来少了。
    """
    if scope == SCOPE_ALL:
        return True
    if scope == SCOPE_PENDING:
        return row.status == db.STATUS_PENDING
    if scope == SCOPE_REINSPECT:
        # 按状态筛、**不看日期**:期限没到监理提前去看是常事。按日期筛的是「超期」。
        return row.status in REINSPECT_STATUSES
    if scope == SCOPE_OVERDUE:
        return is_overdue(row, today_iso)
    return row.status not in CLOSED_STATUSES  # SCOPE_ACTIVE


def hazard_item(row: db.HazardRow, *, today_iso: str) -> dict[str, Any]:
    """一条隐患的对外形态。超期与中文状态都在这儿算好,模型和前端只管照抄。

    刻意**不**放进来的字段:``photo_sha256``(幂等键的内节,对人没用)、
    ``project_id``(归属由调用方在汇总层统一说,逐行重复只会挤上下文)、
    ``grading_version``(事后追溯用,不是对话内容)。少给少错,同 attendance 的最小化约定。

    ⚠️ **键集合是对外契约**:对话链照它念人话,W10 的查询端点照它出 JSON,
    前端按它渲染。加字段要连前端一起改,减字段会让某一侧静默少一格 ——
    ``test_supervision_scoping.py`` 有一条把键集合整个钉死,悄悄加减会当场红。
    """
    return {
        "hazard_no": row.hazard_no,
        "item": row.item,
        "grade": row.grade,
        "status": row.status,
        "status_display": STATUS_ZH[row.status],  # 键必存在:导入期已校验覆盖 STATUSES
        "due_date": row.due_date,
        "due_display": due_display(row.due_date),
        "overdue": is_overdue(row, today_iso),
        "needs_grading": bool(row.needs_grading),
    }


__all__ = [
    "CLOSED_STATUSES",
    "NO_VALUE",
    "OVERDUE_STATUSES",
    "RESULT_ZH",
    "REINSPECT_STATUSES",
    "SCOPES",
    "SCOPE_ACTIVE",
    "SCOPE_ALL",
    "SCOPE_OVERDUE",
    "SCOPE_REINSPECT",
    "SCOPE_PENDING",
    "STATUS_ZH",
    "due_defaults",
    "due_display",
    "hazard_item",
    "in_scope",
    "is_overdue",
    "result_zh",
    "today_hk",
]
