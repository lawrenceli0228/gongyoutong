"""Supervision 工具层(agents/supervision/tools.py)与守卫接线的单元测试 —— 不联网、不调模型。

这层是「模型的嘴」与「隐患台账」之间唯一的桥。测试重点不是覆盖率,而是把几类
**不报错但会出人命 / 出法律事故**的失效钉死:

  · **三个工具必须一行库都不写。** 写入是 supervision_api.py 的七个 HTTP 端点(方案 §5.1),
    签发暂停令是法律行为,不能由一段对话触发。这条用「跑完工具后整张表逐行比对」来验,
    而不是靠读代码 —— 哪天有人顺手加一句 UPDATE,这条当场红。
  · **超期判定的三处排除**:pending(D17)、needs_grading=1(Codex#11)、resuming(等复工令)。
    多算一条超期 = 拿一份我们自己记乱账的材料去指控施工方拒不整改。
  · **「今天到期」不算超期**:边界是严格小于,差一天就是冤枉人。
  · **未归属(D6)与待确认(D17)必须显式报**:不报的话那两批隐患在对话里永远不出现,
    而它们同样是照片里认出来的真隐患。
  · **处置建议的判据顺序**:needs_grading 必须排在级别分岔**之前**,否则会先说
    「这条是一般隐患、签通知单就行」,而它真实级别是未知的。
  · **编号守卫的正则**:认得出六种监理编号、且**认不出**巡检记录号(两边各认各的,
    doc_no.py 头注点名的硬要求);nudge 里**一个完整编号都不许有**——
    ``_sourced`` 现在也扫用户发言,而 nudge 是以 HumanMessage 追加的。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):库落在用例独占的 tmp_path,
所以每个用例天然拿到一张全新的隐患台账,不需要自建 fixture。
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from gyt.agents.supervision import (
    SUPERVISION_AGENT_NAME,
    SUPERVISION_GIVE_UP_MESSAGE,
    SUPERVISION_RECEIPT_PATTERN,
    SUPERVISION_RETRY_NUDGE,
)
from gyt.agents.supervision import tools as sup_tools
from gyt.agents.supervision.tools import (
    SCOPE_ALL,
    SCOPE_OVERDUE,
    SCOPE_PENDING,
    SUPERVISION_TOOLS,
    get_hazard,
    list_hazards,
    suggest_disposal,
)
from gyt.config import get_settings
from gyt.core.doc_no import DocKind, new_doc_no, new_report_no
from gyt.core.run_context import PROJECT_CONFIG_KEY
from gyt.db import hazards as db

TODAY = date(2026, 8, 20)  # 周四
"""把「今天」钉死:超期判定不许依赖跑测试的真实日子。"""

PAST = "2026-08-18"
"""已经过去的期限 —— 超期那批用它。"""

TODAY_ISO = TODAY.isoformat()
"""今天到期。**它不算超期**(边界严格小于),单独有一条用例盯着。"""

FUTURE = "2026-08-25"

PROJECT = "gyt-a3"
ENVELOPE_KEYS = {"ok", "data", "user_msg", "error_code"}

_SEQ = iter(range(1, 10_000))


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sup_tools, "_today", lambda: TODAY)


def _config(project_id: str = "") -> dict:
    """真实的注入通道(config.configurable),与前端选中工地时走的是同一条。"""
    return {"configurable": {PROJECT_CONFIG_KEY: project_id}}


def _make(
    *,
    status: str = db.STATUS_PENDING,
    grade: str = db.GRADE_NORMAL,
    needs_grading: bool = False,
    due: str = FUTURE,
    project_id: str = PROJECT,
    item: str = "未戴安全帽",
    severity: str = "一般",
) -> str:
    """造一条落在指定状态上的隐患,返回它的编号。

    **只走 db 层的迁移函数**(不直接 UPDATE):状态机怎么规定的,造数就得怎么走 ——
    绕过去造出来的状态组合在生产上根本不存在,拿它测出来的结论也不作数。
    """
    seq = next(_SEQ)
    hazard_no = f"GYT-H-20260816-090000-{seq:04x}"
    db.create(
        hazard_no=hazard_no,
        project_id=project_id,
        photo_sha256=f"sha-{seq}",
        photo_id=f"{seq:032x}",
        item=item,
        severity=severity,
        grade=grade,
        grading_version="1",
        needs_grading=needs_grading,
    )
    if status == db.STATUS_PENDING:
        return hazard_no

    assert db.confirm(hazard_no)  # pending → open
    if status == db.STATUS_OPEN:
        return hazard_no

    if status in (db.STATUS_SUSPENDED, db.STATUS_RESUMING):
        assert db.mark_suspended(hazard_no, due)
        if status == db.STATUS_RESUMING:
            # 停过工的复查合格 → resuming(等复工令),分支由 was_suspended 决定
            assert db.pass_reinspection(hazard_no) == db.STATUS_RESUMING
        return hazard_no

    assert db.mark_notified(hazard_no, due)
    if status == db.STATUS_NOTIFIED:
        return hazard_no
    if status == db.STATUS_CLOSED:
        assert db.pass_reinspection(hazard_no) == db.STATUS_CLOSED
        return hazard_no

    assert db.mark_reinspect_failed(hazard_no)
    if status == db.STATUS_REINSPECT_FAILED:
        return hazard_no
    assert status == db.STATUS_ESCALATED, f"造数没覆盖 {status}"
    assert db.mark_escalated(hazard_no)
    return hazard_no


def _snapshot() -> list[db.HazardRow]:
    """整张隐患表的当前快照 —— 「工具没写库」那条断言的比对基准。"""
    return db.list_rows()


# ===========================================================================
# 🔴 只读:三个工具一行库都不许写
# ===========================================================================


async def test_三个工具跑完库里一个字节都没变() -> None:
    """**这条是这条泳道的验收断言。**

    写入(确认/定级/签发/复查结论)全部走 supervision_api.py 的 HTTP 端点,
    理由不是洁癖:签发《工程暂停令》后面跟着停工、跟着索赔,不能由一段概率性对话触发。
    读代码保证不了这件事,所以这里跑完三个工具、把整张表逐行比对。
    """
    hazard_no = _make(status=db.STATUS_OPEN, grade=db.GRADE_SEVERE)
    before = _snapshot()

    await list_hazards.ainvoke({"scope": SCOPE_ALL}, config=_config(PROJECT))
    await get_hazard.ainvoke({"hazard_no": hazard_no})
    await suggest_disposal.ainvoke({"hazard_no": hazard_no})

    assert _snapshot() == before, "工具改了库 —— 只查不办这条红线破了"
    # 文书更不许凭空多出来:建议说「该签暂停令」,但它一份都不该真签。
    assert db.docs_of([hazard_no]) == []


def test_工具清单就这三件且全是只读的名字() -> None:
    """加工具前先问一句:它会写库或出文书吗?会的话它属于端点,不属于这里。"""
    names = {t.name for t in SUPERVISION_TOOLS}
    assert names == {"list_hazards", "get_hazard", "suggest_disposal"}


# ===========================================================================
# list_hazards —— 清单、筛子、两个必须显式报的数
# ===========================================================================


async def test_默认筛子是在办_已销项与已上报不进清单() -> None:
    open_no = _make(status=db.STATUS_OPEN)
    closed_no = _make(status=db.STATUS_CLOSED)
    escalated_no = _make(status=db.STATUS_ESCALATED)

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert set(result) == ENVELOPE_KEYS
    assert result["ok"] is True
    listed = {h["hazard_no"] for h in result["data"]["hazards"]}
    assert listed == {open_no}
    assert closed_no not in result["user_msg"] and escalated_no not in result["user_msg"]


async def test_筛子只认四个词_野词回人话不静默当全部() -> None:
    """模型自己发明筛子(「严重的」)时静默按全部处理,监理会以为清单就这么多。"""
    _make(status=db.STATUS_OPEN)

    result = await list_hazards.ainvoke({"scope": "严重的"}, config=_config(PROJECT))

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert "在办" in result["user_msg"] and "待确认" in result["user_msg"]
    assert re.search(r"[A-Za-z]", result["user_msg"]) is None, "报错也得是人话,不许冒英文"


async def test_待确认条数必须显式报出来_D17() -> None:
    """pending 是 safety 看照片自动登记的,没人确认过。

    混在总数里报 = 让监理以为这些已经进正式流程了,而它们不算整改率、也不催办。
    """
    _make(status=db.STATUS_PENDING)
    _make(status=db.STATUS_PENDING)
    _make(status=db.STATUS_OPEN)

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert result["data"]["pending"] == 2
    assert "待确认 2 条" in result["user_msg"]


async def test_未归属条数必须显式报出来_D6() -> None:
    """未归属 = 拍照时前端没选工地。不专门说一句,那批隐患在对话里永远不出现。"""
    _make(status=db.STATUS_OPEN, project_id=PROJECT)
    _make(status=db.STATUS_OPEN, project_id="")
    _make(status=db.STATUS_PENDING, project_id="")

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert result["data"]["unassigned"] == 2
    assert "2 条隐患没归到任何工地" in result["user_msg"]
    # 只报条数,不把别的归属的明细混进本工地清单
    assert len(result["data"]["hazards"]) == 1


async def test_没选工地时列的就是未归属那一堆并说清楚() -> None:
    """前端没选工地 → project_id 是空串(D6:不是 None)。这时清单本身就是未归属那批。"""
    _make(status=db.STATUS_OPEN, project_id="")
    _make(status=db.STATUS_OPEN, project_id=PROJECT)

    result = await list_hazards.ainvoke({}, config=_config(""))

    assert result["data"]["project_id"] == ""
    assert result["data"]["unassigned"] == 1
    assert len(result["data"]["hazards"]) == 1
    assert "还没归到具体工地" in result["user_msg"]


async def test_超期只算已下期限还没改好的那三档() -> None:
    """三处排除各对应一条定案,少排一处就会多报超期 —— 而超期是升级与对外指控的起点。"""
    overdue_no = _make(status=db.STATUS_NOTIFIED, due=PAST)
    _make(status=db.STATUS_PENDING)  # D17:待确认的不进超期清单
    _make(status=db.STATUS_RESUMING, due=PAST)  # 复查已合格,只差复工令
    _make(status=db.STATUS_OPEN)  # 还没签发,没有期限

    result = await list_hazards.ainvoke({"scope": SCOPE_OVERDUE}, config=_config(PROJECT))

    assert [h["hazard_no"] for h in result["data"]["hazards"]] == [overdue_no]
    assert result["data"]["overdue"] == 1


async def test_没定级的隐患不算超期_Codex11() -> None:
    """needs_grading=1 的隐患任何签发都被硬拒,催它没有意义 —— 也不该出现在超期清单里。"""
    _make(status=db.STATUS_NOTIFIED, due=PAST, needs_grading=True, severity="待定级")

    result = await list_hazards.ainvoke({"scope": SCOPE_OVERDUE}, config=_config(PROJECT))

    assert result["data"]["hazards"] == []
    assert result["data"]["overdue"] == 0


async def test_没定级的隐患在清单里念待定级_不许念成一般() -> None:
    """``grade`` 那一格在 needs_grading=1 时是**映射表给的默认档**,不是判过的结论。

    照着念出来就是「这条是一般隐患」—— 而它的真实级别是未知的。端点的
    ``_require_graded`` 拦的正是这句话:它会把人带去改级别以外的地方。
    """
    _make(status=db.STATUS_OPEN, grade=db.GRADE_NORMAL, needs_grading=True, severity="待定级")

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert "待定级" in result["user_msg"]
    assert "一般" not in result["user_msg"]
    # data 里两个字段照旧原样留着,前端与模型分辨得出来
    assert result["data"]["hazards"][0]["needs_grading"] is True
    assert result["data"]["hazards"][0]["grade"] == db.GRADE_NORMAL


async def test_今天到期不算超期() -> None:
    """边界必须是严格小于:差一天就把还在期限内的人写成「拒不整改」。"""
    _make(status=db.STATUS_NOTIFIED, due=TODAY_ISO)

    result = await list_hazards.ainvoke({"scope": SCOPE_ALL}, config=_config(PROJECT))

    assert result["data"]["overdue"] == 0
    assert result["data"]["hazards"][0]["overdue"] is False


async def test_期限的说法由代码生成_模型只管照抄() -> None:
    """「8月25日(周二)」这种带星期字的说法只有 dates.format_display 产得出来。

    让模型自己换算星期,schedule 那边用 314 行证明过不可靠;监理这边的期限还要进文书。
    """
    _make(status=db.STATUS_NOTIFIED, due=FUTURE)

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert result["data"]["hazards"][0]["due_display"] == "8月25日(周二)"
    assert "8月25日(周二)" in result["user_msg"]


_LIMIT_ENV = "GYT_SUPERVISION_LIST_MAX_ROWS"


def _use_limit(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """改清单条数上限(与 test_attendance_tools 的 _use_max_workers 同款写法)。

    conftest 的 autouse fixture 会在用例前后各 cache_clear 一次,所以这里设完
    再清一次缓存就够,不需要自己收拾环境。
    """
    monkeypatch.setenv(_LIMIT_ENV, str(value))
    get_settings.cache_clear()


async def test_条数超上限如实说只列前N条(monkeypatch: pytest.MonkeyPatch) -> None:
    """截断是静默的:不如实说,监理会以为台账里就这么几条。"""
    limit = 2
    _use_limit(monkeypatch, limit)
    for _ in range(3):
        _make(status=db.STATUS_OPEN)

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert result["data"]["truncated"] is True
    assert len(result["data"]["hazards"]) == limit
    # 计数报的是**筛出来的全部**,不是截断后的:师傅要的是「一共还有几条」
    assert result["data"]["total"] == 3
    assert f"只列了前 {limit} 条" in result["user_msg"]


async def test_空台账是ok加人话_不是错误() -> None:
    """口径对齐 schedule 的「台账里现在没有任务」:查空不是失败。"""
    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert result["ok"] is True
    assert result["data"]["total"] == 0
    assert "一条都没有" in result["user_msg"]


async def test_清单里的状态是中文_不许漏英文出去() -> None:
    _make(status=db.STATUS_SUSPENDED, grade=db.GRADE_SEVERE, due=FUTURE)

    result = await list_hazards.ainvoke({}, config=_config(PROJECT))

    assert result["data"]["hazards"][0]["status_display"] == "已出具暂停令"
    # 🔴 「已出具暂停令」不是「已责令停工」:文书出了稿 ≠ 工地真停了工
    assert "已责令停工" not in result["user_msg"]
    assert "suspended" not in result["user_msg"]


async def test_待确认筛子只出pending() -> None:
    pending_no = _make(status=db.STATUS_PENDING)
    _make(status=db.STATUS_OPEN)

    result = await list_hazards.ainvoke({"scope": SCOPE_PENDING}, config=_config(PROJECT))

    assert [h["hazard_no"] for h in result["data"]["hazards"]] == [pending_no]


# ===========================================================================
# get_hazard —— 详情与证据链(「这条复查了吗」)
# ===========================================================================


async def test_查无此号如实说_不许含糊() -> None:
    result = await get_hazard.ainvoke({"hazard_no": "GYT-H-20260101-000000-ffff"})

    assert result["ok"] is False
    assert result["error_code"] == "NOT_FOUND"
    assert "没有" in result["user_msg"] and "核对" in result["user_msg"]


async def test_编号空着要人补_不许自己猜一条() -> None:
    result = await get_hazard.ainvoke({"hazard_no": "  "})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"


async def test_没复查过就说没复查过() -> None:
    """把「已签发通知单」念成「已经复查过了」是这条工具最要防的误答。"""
    hazard_no = _make(status=db.STATUS_NOTIFIED, due=FUTURE)

    result = await get_hazard.ainvoke({"hazard_no": hazard_no})

    assert result["data"]["reinspected"] is False
    assert "还没复查过" in result["user_msg"]


async def test_证据链带中文名与结论() -> None:
    """文书名派生自 core/doc_no.DOC_TITLE_ZH,复查记录是那条没有类型段的孤儿。"""
    hazard_no = _make(status=db.STATUS_OPEN)
    notice_no = new_doc_no(DocKind.NOTICE)
    assert db.mark_notified(
        hazard_no,
        FUTURE,
        docs=[db.DocDraft(doc_type="notice", doc_no=notice_no, artifact_id="a" * 32)],
    )
    assert db.mark_reinspect_failed(
        hazard_no,
        docs=[
            db.DocDraft(
                doc_type="reinspect",
                doc_no=f"{hazard_no}-R-1",
                photo_id="b" * 32,
                result="fail",
            )
        ],
    )

    result = await get_hazard.ainvoke({"hazard_no": hazard_no})

    kinds = [(d["doc_type"], d["doc_type_display"]) for d in result["data"]["documents"]]
    assert ("notice", "监理通知单") in kinds
    assert ("reinspect", "复查记录") in kinds
    assert result["data"]["reinspected"] is True
    assert notice_no in result["user_msg"], "文书编号要念得出来,监理拿它对账"
    assert "不合格" in result["user_msg"]


# ===========================================================================
# suggest_disposal —— 建议是确定性推出来的,不是模型现编
# ===========================================================================


@pytest.mark.parametrize(
    ("status", "grade", "needs_grading", "expected_action"),
    [
        (db.STATUS_PENDING, db.GRADE_NORMAL, False, "确认"),
        (db.STATUS_OPEN, db.GRADE_NORMAL, False, "签发监理通知单"),
        (db.STATUS_OPEN, db.GRADE_SEVERE, False, "签发工程暂停令"),
        (db.STATUS_NOTIFIED, db.GRADE_NORMAL, False, "登记复查结论"),
        (db.STATUS_SUSPENDED, db.GRADE_SEVERE, False, "登记复查结论"),
        (db.STATUS_REINSPECT_FAILED, db.GRADE_NORMAL, False, "再复查或上报主管部门"),
        (db.STATUS_RESUMING, db.GRADE_SEVERE, False, "签发工程复工令"),
        (db.STATUS_CLOSED, db.GRADE_NORMAL, False, "不用再处置"),
        (db.STATUS_ESCALATED, db.GRADE_NORMAL, False, "不用再处置"),
    ],
)
async def test_每个状态都给得出下一步(
    status: str, grade: str, needs_grading: bool, expected_action: str
) -> None:
    """八个状态一个都不许落空 —— 落空的表现是模型自己现编一个处置意见。"""
    hazard_no = _make(status=status, grade=grade, needs_grading=needs_grading, due=FUTURE)

    result = await suggest_disposal.ainvoke({"hazard_no": hazard_no})

    assert result["ok"] is True
    assert result["data"]["next_action"] == expected_action


async def test_严重隐患的建议是三份文书_不是一份() -> None:
    """「严重隐患只发了通知单」= 该停工的没停。建议这一侧也不许说成一份。"""
    hazard_no = _make(status=db.STATUS_OPEN, grade=db.GRADE_SEVERE)

    result = await suggest_disposal.ainvoke({"hazard_no": hazard_no})

    assert result["data"]["documents"] == ["监理通知单", "工程暂停令", "致建设单位报告"]
    assert "三份" in result["user_msg"]


async def test_没定级时先说定级_不许先按一般隐患出建议_Codex11() -> None:
    """判据顺序必须和端点的四道闸一致。

    反过来的话会先说「这条是一般隐患,签通知单就行」—— 而它的真实级别是**未知**,
    ``grade`` 那一格是映射表给的默认值。人照着这句去签,签的是一份定错级的文书。
    """
    hazard_no = _make(
        status=db.STATUS_OPEN, grade=db.GRADE_NORMAL, needs_grading=True, severity="待定级"
    )

    result = await suggest_disposal.ainvoke({"hazard_no": hazard_no})

    assert result["data"]["next_action"] == "定级"
    assert "待定级" in result["user_msg"]
    assert "签发监理通知单" not in result["user_msg"]


async def test_建议一律指向界面_不许答应代办() -> None:
    """这个 Agent 一份文书都签不出来,建议里必须说清是去界面上点。"""
    hazard_no = _make(status=db.STATUS_OPEN, grade=db.GRADE_SEVERE)

    result = await suggest_disposal.ainvoke({"hazard_no": hazard_no})

    assert "界面上" in result["user_msg"]
    for forbidden in ("已经帮你", "已签发", "已经签"):
        assert forbidden not in result["user_msg"]


async def test_超期时单独顶一句() -> None:
    """超期是升级的起点,埋在建议正文里容易被读漏。"""
    hazard_no = _make(status=db.STATUS_NOTIFIED, due=PAST)

    result = await suggest_disposal.ainvoke({"hazard_no": hazard_no})

    assert result["data"]["overdue"] is True
    assert "超过整改期限" in result["user_msg"]


# ===========================================================================
# 编号守卫的接线(正则、话术)—— 挂载本身在 __init__.py
# ===========================================================================


def test_守卫正则认得六种监理编号() -> None:
    """隐患号与五种文书号都要认:模型编一个通知单号,比编隐患号更像「文书已经出了」。"""
    pattern = re.compile(SUPERVISION_RECEIPT_PATTERN)
    for kind in DocKind:
        number = new_doc_no(kind)
        assert pattern.fullmatch(number), f"{kind.name} 的编号认不出来,守卫对它静默全放行"


def test_守卫正则认不出巡检记录号_两边各认各的() -> None:
    """doc_no.py 头注点名的硬要求:拿错正则会一个都认不出、静默全放行。

    巡检记录号没有类型段(``GYT-20260816-090000``),六种监理编号都有 ——
    这条钉住「互不匹配」这个性质,别哪天为了统一给巡检记录也加上类型段。
    """
    assert re.compile(SUPERVISION_RECEIPT_PATTERN).search(new_report_no()) is None


def test_打回话术里一个完整编号都没有() -> None:
    """两条理由,后一条是 W9 新增的:

    ① few-shot 自我模仿是本仓记录过的失败机理,递现成范例等于帮倒忙;
    ② ``_sourced`` 现在**也扫用户发言**,而 nudge 是以 HumanMessage 追加进请求的 ——
       写了完整编号样例就等于给模型递一个「合法出处」。
    """
    assert re.compile(SUPERVISION_RECEIPT_PATTERN).search(SUPERVISION_RETRY_NUDGE) is None
    for name in ("list_hazards", "get_hazard", "suggest_disposal"):
        assert name in SUPERVISION_RETRY_NUDGE, "打回话术要点名该调哪些工具"


def test_给工友的失败话术不许回退成技术腔() -> None:
    """这句是本泳道**唯一**会出现在工地师傅眼前的硬编码字符串,单独钉住。"""
    assert re.compile(SUPERVISION_RECEIPT_PATTERN).search(SUPERVISION_GIVE_UP_MESSAGE) is None
    assert "没查着" in SUPERVISION_GIVE_UP_MESSAGE, "得先把「这次没查着」说明白"
    for forbidden in ("已确认", "已签发", "已经查到"):
        assert forbidden not in SUPERVISION_GIVE_UP_MESSAGE


def test_挂进了登记表且路由白名单认得它() -> None:
    """接线锁:摘掉任何一头,routing.csv 的 R27-R31 会静默判不对,而报的原因是「派错人了」。"""
    from eval.scorers import ROUTING_AGENTS

    from gyt.graph import AGENT_REGISTRY

    spec = next((s for s in AGENT_REGISTRY if s.name == SUPERVISION_AGENT_NAME), None)
    assert spec is not None, "supervision 没挂进登记表,Supervisor 永远派不到它"
    assert SUPERVISION_AGENT_NAME in ROUTING_AGENTS
    # 免责句是路由摩擦的唯一防线:少了它,「记一下…整改…」会被钓走(真机 A/C 组当场红)
    assert "schedule" in spec.summary
