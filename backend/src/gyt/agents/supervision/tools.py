"""Supervision Agent 的工具集 —— 监理隐患**只读**三件套:列清单 / 看一条 / 给处置建议。

===========================================================================
🔴 三个工具全是只读的,这是设计不是没写完(W9 方案 §5.1 / §5.3)
---------------------------------------------------------------------------
签发《监理通知单》《工程暂停令》是**法律行为**,不能由概率性系统单方面触发。
所以写入那一半(确认、定级、签发、登记复查结论、复工、上报)**全部**走
``supervision_api.py`` 的 HTTP 直连端点,一个 LLM 都不经过 —— 与 W7「打卡写入
不走对话链」同构,只是这次的理由更硬。

本文件因此:
  · 一行写库代码都没有(只 import ``db.hazards`` 的读函数与状态词表);
  · ``suggest_disposal`` 叫"建议"就只出话 —— 它讲清"该怎么处置、要签哪几份、
    请到界面上点",**它自己一份文书都不出**。

有人说「帮我把这条隐患的暂停令签了」时,正确回答是"请到界面上点签发",
这句话由 ``prompt.md`` 负责(与 attendance 的"请点界面上的打卡按钮"同款)。

===========================================================================
分层责任(谁的错在谁那层炸,别串)
---------------------------------------------------------------------------
    模型(prompt.md)      只负责:把隐患编号原样填进参数、照抄工具结果里的编号与日期
        │
        ▼ 本文件(工具层)   受控筛子词表校验、处置建议的**确定性**推导、人话组装;
        │                  信封契约 {ok, data, user_msg}
        ▼ scoping.py       筛子五词、超期判定、状态与结论的中文名、hazard_item 的形状
        │                  (**零 langchain**,supervision_api 的查询端点共用同一份)
        ▼ db/hazards.py    只读取数(``list_rows`` / ``fetch`` / ``docs_of``),
          │                状态机与表级约束的唯一真相也在那边
          ▼ 写入侧          supervision_api.py 七个 HTTP 端点(见上)

===========================================================================
筛子与超期判定为什么不在本文件里(2026-08-16 S1)
---------------------------------------------------------------------------
它们搬去了同包的 ``scoping.py``。理由不是"文件太长",是 **W10 要给
``supervision_api.py`` 加三个查询端点,而它们必须和 ``list_hazards`` 用同一套判据** ——
判据再抄一份,后端筛出来的清单和对话里报的条数就会悄悄对不上,而两边测试都绿
(TODO-45 A 组记的正是这类漂移)。

🔴 **边是单向的:本文件 import ``scoping``,scoping 一个包内模块都不许 import。**
哪天有人为了图方便让 scoping 回头 import 本文件,当场成环 —— 而循环导入炸出来的
报错往往指着第三个文件。完整的依赖约束写在 ``scoping.py`` 头注。

「今天」为什么是香港日历日而不是 ``date.today()``,一并搬去了那边的头注 ——
判据一个字节没改,只是换了个住处。
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from gyt.agents.safety.severity import SEVERITY_PENDING

# 筛子五词、超期判定、状态与结论的中文名、hazard_item 的形状 —— 全部在 scoping.py。
# ⚠️ **按模块名 import,调用点写 ``scoping.xxx()``,不许 ``from ... import today_hk``**:
#    测试把「今天」钉死靠的是 monkeypatch ``scoping.today_hk`` 这一个点,
#    按名 import 会在导入那一刻把函数对象绑死,桩打不进去(理由写在那个函数的 docstring)。
from gyt.agents.supervision import scoping
from gyt.config import get_settings
from gyt.core.doc_no import DOC_TITLE_ZH, DocKind
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.core.run_context import project_from_config
from gyt.db import hazards as db

# ---------------------------------------------------------------------------
# 受控词表(禁止在函数体里散落字面量)
# ---------------------------------------------------------------------------

# 筛子五个词**转出去**给老调用点(``__all__`` 里有,测试与别处直接 import 它们)。
# 🔴 这四行只是转出,**真相在 ``scoping.py``** —— 不许在这儿改字面量,
#    改了就又变成两份拷贝,而两份筛子词表漂开的表现是端点认得的词对话链不认。
SCOPE_ACTIVE: Final[str] = scoping.SCOPE_ACTIVE
SCOPE_PENDING: Final[str] = scoping.SCOPE_PENDING
SCOPE_REINSPECT: Final[str] = scoping.SCOPE_REINSPECT
SCOPE_OVERDUE: Final[str] = scoping.SCOPE_OVERDUE
SCOPE_ALL: Final[str] = scoping.SCOPE_ALL
SCOPES: Final[tuple[str, ...]] = scoping.SCOPES

_REINSPECT_DOC_TYPE: Final[str] = "reinspect"
"""``hazard_docs`` 里那条**不是文书**的行:复查留痕,没有 artifact_id,也没有编号类型段。"""

_DOC_TYPE_ZH: Final[dict[str, str]] = {
    **{kind.name.lower(): DOC_TITLE_ZH[kind] for kind in DocKind if kind is not DocKind.HAZARD},
    _REINSPECT_DOC_TYPE: "复查记录",
}
"""``hazard_docs.doc_type`` → 中文名。文书那五档**派生**自 ``core/doc_no.DOC_TITLE_ZH``,
不手抄第二份(那边改了名字这边自动跟上)。

两个孤儿在 ``doc_no.DocKind`` 的头注里写明,这里正好对上:
  · ``DocKind.HAZARD`` 是隐患自己的身份号、不是文书,所以从派生里剔掉;
  · ``reinspect`` 在方案 §6.3 的编号表里没有类型段,所以 DocKind 里没有它,单独补一行。
**别写成"六档一一对应"的循环** —— 两头各有一个孤儿,循环写出来必错一头。
"""

_UNNAMED_DOC_TYPES: Final[tuple[str, ...]] = tuple(t for t in db.DOC_TYPES if t not in _DOC_TYPE_ZH)
if _UNNAMED_DOC_TYPES:  # pragma: no cover —— 只在两张词表漂了时触发
    raise RuntimeError(
        f"文书类型 {_UNNAMED_DOC_TYPES} 没有中文名 —— db/hazards.py 的 DOC_TYPES 加了档,"
        "agents/supervision/tools.py 的 _DOC_TYPE_ZH 要跟上"
    )

_GRADE_UNKNOWN: Final[str] = SEVERITY_PENDING
"""``needs_grading=1`` 时对人念的级别。**不是** ``hazards.grade`` 里的值(那是映射表给的
默认档,不是判过的结论)。

直接**取自** ``agents/safety/severity.py`` 的「待定级」而不是另抄一个字符串:两边说的
是同一件事(不知道),那边改词这边跟着改。同包的 ``grading.py`` 走的是同一条 import ——
severity.py 只 import 标准库,是张纯表,拉它不构成环、也不捎带任何重依赖。"""

_LIST_DESCRIPTION = (
    "查监理隐患台账:还有几条没销、待确认的有几条、超期的有哪些。"
    f"scope 只能填这几个词之一:{'/'.join(SCOPES)}(留空按「{SCOPE_ACTIVE}」算,"
    "「在办」= 没销项也没上报的全部;「待复查」= 文书已签出去、等去现场复查的)。"
    "返回每条隐患的编号(GYT-H-开头)、违规项、级别、状态、整改期限和超不超期,"
    "并单独给出待确认条数与未归属条数。回复用户时编号和日期照抄返回里的原文。"
)

_GET_DESCRIPTION = (
    "看某一条隐患的详情和证据链:现在什么状态、期限几号、已经签了哪些文书、"
    "复查过没有、复查结论是合格还是不合格。"
    "hazard_no 填隐患编号(GYT-H-开头,来自查询结果或用户原话,不许自己编)。"
    "「这条复查了吗」「通知单签了没」这类问题用它。"
)

_SUGGEST_DESCRIPTION = (
    "给某一条隐患出处置建议:按它现在的状态和级别,讲清下一步该做什么、要签哪几份文书。"
    "hazard_no 填隐患编号(GYT-H-开头,不许自己编)。"
    "它**只给建议**,不会真的签发文书、也不会改任何状态 —— 签发要人在界面上点。"
    "用户问「这条该怎么处置」「要不要停工」「下一步干什么」时用它。"
)

_NEXT_CONFIRM: Final[str] = "确认"
_NEXT_GRADE: Final[str] = "定级"
_NEXT_NOTICE: Final[str] = "签发监理通知单"
_NEXT_SUSPEND: Final[str] = "签发工程暂停令"
_NEXT_REINSPECT: Final[str] = "登记复查结论"
_NEXT_RESUME: Final[str] = "签发工程复工令"
_NEXT_ESCALATE: Final[str] = "再复查或上报主管部门"
_NEXT_DONE: Final[str] = "不用再处置"
"""建议里的「下一步」受控词。**与 ``supervision_api.py`` 报状态机拒绝时用的动作名同款**
(那边是 ``_require_status(..., "签发监理通知单")`` 之类),这样界面上的按钮、模型说的话、
端点拒绝时的措辞是同一套词 —— 师傅不用在三种说法之间自己做翻译。"""


def _hazard_line(item: dict[str, Any]) -> str:
    """清单里的一行人话。编号在最前 —— 师傅要拿着它跟监理对账、在界面上找那一条。

    🔴 ``needs_grading`` 的行,级别那一格念的是「待定级」而**不是** ``grade`` 里那个值:
    那个值是映射表给的默认档(一般),不是有人判过的结论。照着念出来就是
    「这条是一般隐患」—— 方案 §5.3 与端点 ``_require_graded`` 拦的正是这句话
    (它会把人带去改级别以外的地方,而真正要做的是先定级)。
    ``data`` 里两个字段都原样留着,前端与模型要分辨得出来。
    """
    level = _GRADE_UNKNOWN if item["needs_grading"] else item["grade"]
    tail = f",期限{item['due_display']}" if item["due_display"] else ",还没定期限"
    if item["overdue"]:
        tail += ",已超期"
    return f"{item['hazard_no']} {item['item']}({level},{item['status_display']}{tail})"


@tool("list_hazards", description=_LIST_DESCRIPTION)
@tool_guard
async def list_hazards(scope: str = SCOPE_ACTIVE, *, config: RunnableConfig) -> Envelope:
    """列隐患清单。一次取全后在内存里筛(方案红线:禁在循环里逐条查)。

    ⚠️ ``config`` 的注解必须**恰好**是 ``RunnableConfig``:langchain 按
    ``param.annotation is RunnableConfig`` 判定要不要注入,写成 ``RunnableConfig | None``
    会漏掉 —— 表现是 config 永远拿不到、当前工地恒为空串,于是**别的工地的隐患
    全被当成本工地的列出来**,而且不会有任何报错(``core/run_context.py`` 头注有原话)。

    归属怎么处理(D6:``project_id`` 允许是空串 = 未归属,不是 NULL):
      · 前端没选工地 → 当前工地是空串 → 列的**就是**未归属那一堆,如实说明;
      · 选了工地     → 只列这个工地的,另外**报一句未归属还有几条** ——
        不报的话那批隐患在界面上永远没人看见,而它们同样是照片里认出来的真隐患。
      两次查询、不是 N+1(第二次只数条数),两条路径的语义差别在 user_msg 里说清楚。
    """
    picked = (scope or "").strip() or SCOPE_ACTIVE
    if picked not in SCOPES:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg=f"查隐患只能按这几种来:{'、'.join(SCOPES)}。你说的「{picked}」我这儿没有。",
        )

    project_id = project_from_config(config)  # 没选工地 = 空串(D6),不是 None
    rows = await asyncio.to_thread(db.list_rows, project_id=project_id)
    if project_id:
        # 只数条数,不把别的工地/未归属的明细混进本工地的清单里。
        unassigned = len(await asyncio.to_thread(db.list_rows, project_id=""))
    else:
        unassigned = len(rows)  # 这一堆本身就是未归属的

    today_iso = scoping.today_hk().isoformat()
    limit = get_settings().supervision_list_max_rows
    matched = [r for r in rows if scoping.in_scope(r, scope=picked, today_iso=today_iso)]
    shown = [scoping.hazard_item(r, today_iso=today_iso) for r in matched[:limit]]
    truncated = len(matched) > limit

    # 三个计数都在**本次筛出来的全部行**上算(不是截断后的那批):师傅要的是
    # 「一共还有几条」,截断只影响列出来几行。
    pending_count = sum(1 for r in matched if r.status == db.STATUS_PENDING)
    overdue_count = sum(1 for r in matched if scoping.is_overdue(r, today_iso))

    data: dict[str, Any] = {
        "scope": picked,
        "project_id": project_id,
        "today": today_iso,
        "hazards": shown,
        "total": len(matched),
        "pending": pending_count,
        "overdue": overdue_count,
        # ⚠️ unassigned 是**不过筛子**的总数(未归属一共几条),与上面三个计数口径不同:
        # 它回答的是「有没有一批隐患没人看得见」,拿筛子筛过反而会把它藏起来。
        "unassigned": unassigned,
        "truncated": truncated,
    }
    return ok(
        data=data,
        user_msg=_list_summary(
            picked,
            shown,
            total=len(matched),
            pending=pending_count,
            overdue=overdue_count,
            unassigned=unassigned,
            project_id=project_id,
            truncated=truncated,
            limit=limit,
        ),
    )


def _list_summary(
    scope: str,
    shown: list[dict[str, Any]],
    *,
    total: int,
    pending: int,
    overdue: int,
    unassigned: int,
    project_id: str,
    truncated: bool,
    limit: int,
) -> str:
    """清单的人话。空结果是 ok + 如实说(口径对齐 schedule 的「台账里现在没有任务」)。"""
    if not total:
        head = f"{scope}的隐患一条都没有。"
        return head + _unassigned_note(unassigned, project_id, empty=True)

    lines = [_hazard_line(item) for item in shown]
    parts = [f"{scope}的隐患 {total} 条"]
    # 两个计数**只在非零时**出现:恒定带上「超期 0 条」会让人读成一句套话,
    # 真有超期那天反而看不见。待确认(D17)与超期是监理最要盯的两个数。
    extras = []
    if scope != SCOPE_OVERDUE and overdue:
        extras.append(f"超期 {overdue} 条")
    if scope != SCOPE_PENDING and pending:
        extras.append(f"待确认 {pending} 条")
    if extras:
        parts.append("(其中" + "、".join(extras) + ")")
    head = "".join(parts) + ":\n" + "\n".join(lines)
    if truncated:
        head += f"\n条数太多,只列了前 {limit} 条,剩下的到界面上翻。"
    return head + _unassigned_note(unassigned, project_id, empty=False)


def _unassigned_note(unassigned: int, project_id: str, *, empty: bool) -> str:
    """未归属那批的说明(D6)。**这句是"显式报"的落点,别为了简洁删掉。**

    未归属 = 照片进来时前端没选工地,隐患照样登记(不登记等于丢隐患),
    但它不属于任何一个工地的清单,不专门说一句就永远没人看见。
    """
    if not unassigned:
        return ""
    if not project_id:
        # 当前就在看未归属那一堆,说清楚"这些还没归工地",不要再报一遍条数。
        # 一条都没列出来时这句是噪音(「这些」指不着任何东西),直接不说。
        return "" if empty else "\n(这些隐患还没归到具体工地,界面上没选工地时拍的照片都落在这儿。)"
    tail = "别漏了。" if not empty else "别漏了 —— 它们不在本工地的清单里。"
    return f"\n另外还有 {unassigned} 条隐患没归到任何工地,{tail}"


@tool("get_hazard", description=_GET_DESCRIPTION)
@tool_guard
async def get_hazard(hazard_no: str) -> Envelope:
    """看一条隐患的详情 + 证据链(已签的文书、复查记录)。

    证据链一次取回(``docs_of`` 收的是编号列表),不在循环里逐条查 —— 这条与
    "上报主管部门时要一次举证"是同一个 SQL,别在这层退化成 N+1。
    """
    wanted = (hazard_no or "").strip()
    if not wanted:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="得说是哪一条隐患,把隐患编号给我(GYT-H- 开头的那串)。",
        )

    row = await asyncio.to_thread(db.fetch, wanted)
    if row is None:
        return fail(
            ErrorCode.NOT_FOUND,
            user_msg=f"台账里没有「{wanted}」这条隐患,核对一下编号。",
        )

    docs = await asyncio.to_thread(db.docs_of, [row.hazard_no])
    today_iso = scoping.today_hk().isoformat()
    item = scoping.hazard_item(row, today_iso=today_iso)
    documents = [
        {
            "doc_type": d.doc_type,
            "doc_type_display": _DOC_TYPE_ZH[d.doc_type],  # 键必存在:导入期已校验
            "doc_no": d.doc_no,
            # artifact_id 给前端渲下载卡用;复查记录那行没有文件,是 None。
            "artifact_id": d.artifact_id,
            # 🔴 **复查照片编号 —— 这一行是「复查必须挂照片」那条红线的取件口**(方案 §5.2)。
            #    它是 ``hazard_docs.photo_id``:**这一次复查**拍的那张,一次复查一张。
            #    **不是** ``hazards.photo_id``(隐患首次发现那张,一条隐患只有一张)——
            #    两者混起来的后果是拿发现时的照片当"整改后"的证据,而红线的全部意义
            #    就是事后追责时分得清这两张。文书行没有复查照片,是 None。
            #    2026-08-16 之前这个字段一处都没往外给过:照片存进了库,却谁也取不出来,
            #    红线被抽成了一道提交时的门槛。前端按 artifact_id 取件的那条路直接能用它。
            "photo_id": d.photo_id,
            "result": d.result,
            "created_at": d.created_at,
        }
        for d in docs
    ]
    reinspections = [d for d in documents if d["doc_type"] == _REINSPECT_DOC_TYPE]
    data: dict[str, Any] = {
        **item,
        "documents": documents,
        "reinspected": bool(reinspections),
        "found_at": row.found_at,
        "closed_at": row.closed_at,
    }
    return ok(data=data, user_msg=_detail_summary(item, documents, reinspections))


def _detail_summary(
    item: dict[str, Any],
    documents: list[dict[str, Any]],
    reinspections: list[dict[str, Any]],
) -> str:
    """详情的人话。**「复查了吗」要能被一眼答上**,所以复查单独起一行说。"""
    lines = [_hazard_line(item)]
    issued = [d for d in documents if d["doc_type"] != _REINSPECT_DOC_TYPE]
    if issued:
        lines.append(
            "已签的文书:" + "、".join(f"{d['doc_type_display']} {d['doc_no']}" for d in issued)
        )
    else:
        lines.append("还没签过任何文书。")
    if not reinspections:
        lines.append("还没复查过。")
    else:
        last = reinspections[-1]  # docs_of 按 id 升序,最后一条就是最近一次
        # 结论的中文名走 ``scoping.result_zh``(全仓唯一那张表)。这里原来写的是
        # ``"合格" if last["result"] == "pass" else "不合格"`` —— 那正是那张表的
        # 另一份拷贝,S1 一并收敛掉。
        # 🔴 顺带修掉那句 else 的静默错话:``hazard_docs.result`` 的 CHECK 是
        #    ``IS NULL OR IN (...)``,历史行/补录行**真的可能没有结论**,而旧写法
        #    会把"没记结论"念成「不合格」—— 在没有结论的情况下对外声称施工方复查没过。
        #    没结论就如实说没结论。
        verdict = scoping.result_zh(last["result"]) if last["result"] else "没记结论"
        lines.append(f"复查过 {len(reinspections)} 次,最近一次结论:{verdict}。")
        lines.append(_reinspect_photo_line(last))
    return "\n".join(lines)


def _reinspect_photo_line(last: dict[str, Any]) -> str:
    """最近一次复查的照片编号那一句。

    为什么非得在**人话里**也念一遍(``data.documents`` 里每一条都带着,前端能全渲):
    在对话里问「这条复查了吗」的人,下一步多半就是要调那张图去核对或者去存档 ——
    编号只进 data 不进正文的话,他还得再去界面上翻一遍。念的是最近一次那张,
    历次的在 ``data`` 里,要全的走界面。

    🔴 念的是 ``hazard_docs.photo_id``(**这一次复查**那张),不是 ``hazards.photo_id``
    (首次发现那张)—— 混起来就是拿发现时的照片当"整改后"的证据。

    缺编号时**不许沉默地少说一句**:那意味着这条复查记录在追责场合举不出现场凭据,
    而它恰恰是「复查必须挂照片」这条红线漏掉的那一条,得让人看见。
    """
    photo_id = last.get("photo_id")
    if photo_id:
        return f"最近一次的复查照片编号 {photo_id},要调原图报这个号。"
    return "最近一次复查没留下照片编号 —— 这条复查在事后追责时举不出现场凭据。"


def _advise(row: db.HazardRow) -> tuple[str, tuple[str, ...], str]:
    """按状态与级别推出处置建议:(下一步, 要签的文书, 人话)。**纯函数,不经过模型。**

    判据顺序照抄 ``supervision_api.py`` 的四道闸,一步都不许换位:

        已销项 / 已上报 ─▶ 到此为止
        待确认(pending)─▶ 先确认(D17:没人确认过就进正式流程 = 让模型单方面开启法律流程)
        needs_grading  ─▶ 先定级(Codex#11:排在级别分岔**之前**。反过来的话
                           会先说"这条是一般隐患、签通知单就行",而它真实级别是未知的)
        open + 严重    ─▶ 三文书(通知单 + 暂停令 + 致建设单位报告)
        open + 一般    ─▶ 通知单一份
        已发文书       ─▶ 到期复查(要复查照片,结论由人下 —— D11)
        复查不合格     ─▶ 再复查 或 上报主管部门
        待签复工令     ─▶ 复工令(停过工的必须走这一步,漏了就是漏发复工令)

    ⚠️ 这里给的是**建议**,真正说了算的是端点里那三条硬拦 + 状态机 rowcount。
    两边判据写得一样是刻意的(说了"该签暂停令"结果点下去被拒,比不给建议还糟),
    但**别把这段当成授权** —— 它一份文书都签不出来。

    函数偏长,但**胖的是中文正文不是逻辑**:每个分支只有一个 return,判据各一行。
    真要拆就拆成「判据表 + 文案表」两张 —— 别按状态拆成八个小函数,那样顺序这条最要紧的
    约束就散没了(顺序错位的后果见上面 needs_grading 那一行)。
    """
    if row.status == db.STATUS_CLOSED:
        return _NEXT_DONE, (), "这条已经销项了,不用再处置。"
    if row.status == db.STATUS_ESCALATED:
        return _NEXT_DONE, (), "这条已经报到主管部门了,后面按主管部门的意见来。"
    if row.status == db.STATUS_PENDING:
        # 待确认 + 待定级会同时成立(safety 认出一个词表外的违规项就是这样)。
        # 两件事都得说,但顺序是**先确认再定级** —— 那是状态机的顺序。
        tail = (
            f"现场判的是「{row.severity}」,确认完还得在界面上定成一般隐患或严重隐患,才能签文书。"
            if row.needs_grading
            else ""
        )
        return (
            _NEXT_CONFIRM,
            (),
            "这条是看照片自动认出来的,还没人确认,先在界面上点确认 —— "
            "确认之前它不算进整改率,也不会催办;确实不是隐患就在界面上否掉。" + tail,
        )
    if row.needs_grading:
        return (
            _NEXT_GRADE,
            (),
            f"这条现场判的是「{row.severity}」,还没定级,先在界面上定成一般隐患或严重隐患,"
            "再签文书 —— 没定级的隐患签发会被拒。",
        )
    if row.status == db.STATUS_OPEN and row.grade == db.GRADE_SEVERE:
        docs = (
            DOC_TITLE_ZH[DocKind.NOTICE],
            DOC_TITLE_ZH[DocKind.SUSPENSION],
            DOC_TITLE_ZH[DocKind.OWNER_REPORT],
        )
        return (
            _NEXT_SUSPEND,
            docs,
            "这条是严重隐患,只发一份通知单不够:要在界面上点「签发暂停令」,"
            f"一次出三份({'、'.join(docs)}),同时定下整改期限。",
        )
    if row.status == db.STATUS_OPEN:
        docs = (DOC_TITLE_ZH[DocKind.NOTICE],)
        return (
            _NEXT_NOTICE,
            docs,
            f"这条是{row.grade}隐患,在界面上点「签发监理通知单」,出一份{docs[0]}并定下整改期限。"
            "确实严重到要停工的,先把它改定为严重隐患再签。",
        )
    if row.status in (db.STATUS_NOTIFIED, db.STATUS_SUSPENDED):
        return (
            _NEXT_REINSPECT,
            (),
            "文书已经签了,下一步是到期去现场复查:拍一张复查照片,在界面上登记复查结论。"
            "合格还是不合格**由人来定**,照片是证据 —— 系统不替你下这个结论。",
        )
    if row.status == db.STATUS_REINSPECT_FAILED:
        docs = (DOC_TITLE_ZH[DocKind.AUTHORITY_REPORT],)
        return (
            _NEXT_ESCALATE,
            docs,
            "上次复查不合格。改好了就再复查一次;要是一直不改,"
            f"在界面上点「上报主管部门」,出一份{docs[0]}把整条证据链带上。",
        )
    # 只剩 resuming 一档(八态已穷举:上面覆盖了另外七档)。
    docs = (DOC_TITLE_ZH[DocKind.RESUMPTION],)
    return (
        _NEXT_RESUME,
        docs,
        f"这条停过工、复查也合格了,还差一份{docs[0]}:在界面上点「签发复工令」,签完自动销项。",
    )


@tool("suggest_disposal", description=_SUGGEST_DESCRIPTION)
@tool_guard
async def suggest_disposal(hazard_no: str) -> Envelope:
    """给一条隐患出处置建议。**只出话:不签文书、不改状态、不写库。**

    建议由 ``_advise`` 按库里的状态与级别**确定性**推出,不是模型现编 —— 与
    ``severity``/``grade`` 两张映射表同一个理由:签发暂停令是法律行为,
    走哪条路不能由概率性系统决定(方案 §5.1)。
    """
    wanted = (hazard_no or "").strip()
    if not wanted:
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg="得说是哪一条隐患,把隐患编号给我(GYT-H- 开头的那串)。",
        )

    row = await asyncio.to_thread(db.fetch, wanted)
    if row is None:
        return fail(
            ErrorCode.NOT_FOUND,
            user_msg=f"台账里没有「{wanted}」这条隐患,核对一下编号。",
        )

    next_action, documents, advice = _advise(row)
    today_iso = scoping.today_hk().isoformat()
    item = scoping.hazard_item(row, today_iso=today_iso)
    data: dict[str, Any] = {
        **item,
        "next_action": next_action,
        "documents": list(documents),
    }
    lines = [_hazard_line(item), advice]
    if item["overdue"]:
        # 超期单独顶一句:它是升级的起点,埋在建议正文里容易被读漏。
        lines.insert(1, "这条已经超过整改期限了,尽快处理。")
    return ok(data=data, user_msg="\n".join(lines))


SUPERVISION_TOOLS: list = [list_hazards, get_hazard, suggest_disposal]
"""挂给 Agent 的三件套。**新增工具前先问一句:它会写库或出文书吗?** 会的话它不属于这里,
属于 ``supervision_api.py`` 的端点(方案 §5.1,理由见本文件头注)。"""

__all__ = [
    "SCOPES",
    "SCOPE_ACTIVE",
    "SCOPE_ALL",
    "SCOPE_OVERDUE",
    "SCOPE_PENDING",
    "SCOPE_REINSPECT",
    "SUPERVISION_TOOLS",
    "get_hazard",
    "list_hazards",
    "suggest_disposal",
]
