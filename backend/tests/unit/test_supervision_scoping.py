"""``agents/supervision/scoping.py`` 的单元测试 —— 筛子、超期判定、两张中文词表。

这个模块是 2026-08-16(S1)从 ``agents/supervision/tools.py`` 与 ``documents.py`` 里
抽出来的**共享判据**:对话链的 ``list_hazards`` 与 W10 那三个查询端点从此读同一份。
所以这里钉的不是"覆盖率",而是几类**不报错但会出法律事故**的失效:

  · **超期边界是严格小于。** 今天到期不算超期 —— 差一天就把还在期限内的人写成
    「拒不整改」,而那句话是要进上报材料的。这条有专门一个用例,变异验证过。
  · **超期的三处排除**:``pending``(D17)、``needs_grading=1``(Codex#11)、
    ``resuming``(等复工令)。多算一条超期 = 拿一份我们自己记乱账的材料去指控施工方。
  · **两张中文词表的覆盖**:``STATUS_ZH`` 八档、``RESULT_ZH`` 两档。漏一档的表现是
    回给工友的话里 / 留档文书上冒出一个英文单词,而那不会有任何报错。
  · **``hazard_item()`` 的键集合**是对外契约(对话链念人话、端点出 JSON、前端渲染
    都照它),悄悄加减字段会让某一侧静默少一格。
  · **「今天」是香港日历日,不是宿主时区那天。** 两条用例一起守:一条篡改宿主 TZ
    (照抄 ``test_checkin_api.py`` 的写法,**本机没有 ``time.tzset`` 会跳过**),
    一条打假时钟(到处都跑得了,补上前一条被跳过时的窟窿)。
  · **本模块的 import 约束**:自己不许碰 langchain / langgraph / python-docx,
    也不许 import 本包任何模块(否则与 ``tools.py`` 成环)。用 AST 扫源码钉死。

为什么这里直接手搓 ``HazardRow`` 而不像 ``test_supervision_tools.py`` 那样走 db 造数:
被测的全是**纯谓词**,输入就是 (status, due_date, needs_grading) 三个字段,过一遍
状态机只会把用例拖慢、把失败原因搅浑。状态组合在生产上到底可不可达,是
``test_db_hazards.py`` 的活;端到端那条链由 ``test_supervision_tools.py`` 盯着。
代价是手搓的行可能跟不上表结构 —— 所以下面第一条用例就把
``HazardRow._fields`` 与本文件的构造器逐字段对上。
"""

from __future__ import annotations

import ast
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from gyt.agents.supervision import scoping
from gyt.attendance.receipt import HK
from gyt.db import hazards as db

TODAY = date(2026, 8, 20)  # 周四
TODAY_ISO = TODAY.isoformat()
"""今天到期。**它不算超期**(边界严格小于)。"""

YESTERDAY_ISO = "2026-08-19"
FUTURE_ISO = "2026-08-25"


def _行(
    *,
    status: str = db.STATUS_OPEN,
    due_date: str | None = None,
    needs_grading: bool = False,
    grade: str = db.GRADE_NORMAL,
    hazard_no: str = "GYT-H-20260816-090000-0001",
    item: str = "未戴安全帽",
) -> db.HazardRow:
    """造一行隐患。**只有前三个参数会影响被测判据**,其余给的是不碍事的占位值。

    ⚠️ 字段一个不能少:``HazardRow`` 是 NamedTuple,漏一个是 TypeError 不是静默错 ——
    但**多出来的新字段不会报错**(它有默认值的话),所以下面另有一条用例把
    ``HazardRow._fields`` 与这里的关键字逐个对上。
    """
    return db.HazardRow(
        id=1,
        hazard_no=hazard_no,
        project_id="gyt-a3",
        photo_sha256="sha-1",
        photo_id="a" * 32,
        item=item,
        severity="一般",
        grade=grade,
        grading_version="1",
        needs_grading=int(needs_grading),
        was_suspended=0,
        status=status,
        due_date=due_date,
        found_at="2026-08-16 09:00:00",
        confirmed_at=None,
        closed_at=None,
        created_at="2026-08-16 09:00:00",
        updated_at="2026-08-16 09:00:00",
        # 2026-08-21 加的两列(dismiss():不出文书关掉时的理由 + 谁关的)。
        # 这里给 None = 「这一行不是那么关掉的」,与绝大多数真实行一致。
        closed_reason=None,
        closed_by=None,
    )


def test_造数构造器覆盖了HazardRow的全部字段() -> None:
    """手搓行的代价在这儿兜住:db 加一列而这里没跟上,用例会拿着旧形状测新代码。

    比的是字段名集合而不是条数 —— 加一列、删一列、改个名,三种都得当场红。
    """
    assert set(_行()._asdict()) == set(db.HazardRow._fields)


# ===========================================================================
# 筛子四个词
# ===========================================================================


def test_全部这个筛子一条都不筛() -> None:
    """「全部」= 连已销项、已上报都列出来。它是唯一一个不做任何排除的档。"""
    for status in db.STATUSES:
        行 = _行(status=status)
        assert scoping.in_scope(行, scope=scoping.SCOPE_ALL, today_iso=TODAY_ISO), status


def test_在办排除已销项与已上报_其余六档都在() -> None:
    """「在办」排的是状态机里那两个终点(出口是空集),别多排也别少排。

    少排一档(比如把 closed 留下)= 监理看到一堆早就销了的隐患,以为活没干完;
    多排一档(比如顺手把 pending 也排掉)= 待确认那批在对话里彻底消失,
    而它们同样是照片里认出来的真隐患(D17)。
    """
    在办的 = {
        s
        for s in db.STATUSES
        if scoping.in_scope(_行(status=s), scope=scoping.SCOPE_ACTIVE, today_iso=TODAY_ISO)
    }
    assert 在办的 == set(db.STATUSES) - {db.STATUS_CLOSED, db.STATUS_ESCALATED}


def test_待确认这个筛子只出pending() -> None:
    只出的 = {
        s
        for s in db.STATUSES
        if scoping.in_scope(_行(status=s), scope=scoping.SCOPE_PENDING, today_iso=TODAY_ISO)
    }
    assert 只出的 == {db.STATUS_PENDING}


def test_超期这个筛子等价于超期判定本身() -> None:
    """筛子与 ``is_overdue`` 不许各写一套判据 —— 漂了的表现是清单里列出来 5 条,
    而同一次返回里的 ``overdue`` 计数说 3 条,两个数就在同一段话里打架。"""
    行们 = [
        _行(status=db.STATUS_NOTIFIED, due_date=YESTERDAY_ISO),
        _行(status=db.STATUS_NOTIFIED, due_date=TODAY_ISO),
        _行(status=db.STATUS_PENDING, due_date=YESTERDAY_ISO),
        _行(status=db.STATUS_RESUMING, due_date=YESTERDAY_ISO),
        _行(status=db.STATUS_SUSPENDED, due_date=YESTERDAY_ISO, needs_grading=True),
    ]
    for 行 in 行们:
        assert scoping.in_scope(
            行, scope=scoping.SCOPE_OVERDUE, today_iso=TODAY_ISO
        ) is scoping.is_overdue(行, TODAY_ISO)


def test_野筛子落在在办这一档_所以调用方必须自己先拦() -> None:
    """``in_scope`` 认不出的词**不报错**,落在「在办」。

    这不是漏写,是分工:受控词表校验归调用方(对话链回一句人话让模型换个词,
    端点返 400)。把这条行为钉下来是因为它反直觉 —— 谁要是指望这里替他拦野词,
    结果就是静默按「在办」筛,而少给的清单在界面上和对话里都看不出来少了。
    """
    assert "严重的" not in scoping.SCOPES
    assert scoping.in_scope(_行(status=db.STATUS_OPEN), scope="严重的", today_iso=TODAY_ISO)
    assert not scoping.in_scope(_行(status=db.STATUS_CLOSED), scope="严重的", today_iso=TODAY_ISO)


def test_筛子就这四个词() -> None:
    """加一个筛子要连 supervision_api 的查询端点和前端一起改,别在这儿单方面加。"""
    assert scoping.SCOPES == ("在办", "待确认", "超期", "全部")


# ===========================================================================
# 超期判定 —— 这一节每条都对应一次真实的"会冤枉人"
# ===========================================================================


def test_今天到期不算超期() -> None:
    """🔴 边界必须是**严格小于**。

    改成 ``<=`` 的后果不是差一个数:期限当天现场还在干活的人,当天就被系统写成
    「已超期」,而超期是升级的起点、是《监理报告》里指控施工方拒不整改的依据。
    差一天就是冤枉人。
    """
    assert (
        scoping.is_overdue(_行(status=db.STATUS_NOTIFIED, due_date=TODAY_ISO), TODAY_ISO) is False
    )


def test_昨天到期算超期() -> None:
    """边界的另一半:真过了期限就得认,不认等于把该升级的隐患一直挂着。"""
    assert (
        scoping.is_overdue(_行(status=db.STATUS_NOTIFIED, due_date=YESTERDAY_ISO), TODAY_ISO)
        is True
    )


def test_期限还没到不算超期() -> None:
    assert (
        scoping.is_overdue(_行(status=db.STATUS_NOTIFIED, due_date=FUTURE_ISO), TODAY_ISO) is False
    )


def test_没定期限的不算超期() -> None:
    """``due_date`` 为空 = 还没签发文书、还没下期限。没下过期限就谈不上逾期未改。"""
    assert scoping.is_overdue(_行(status=db.STATUS_OPEN, due_date=None), TODAY_ISO) is False


def test_没定级的不算超期_Codex11() -> None:
    """``needs_grading=1`` 的隐患**任何签发都被端点硬拒**,催它没有意义。

    判据必须排在状态判断之前:排在后面的话,一条"已签发通知单 + 待定级"的行会被
    算成超期,而它连下一步都走不了。
    """
    行 = _行(status=db.STATUS_NOTIFIED, due_date=YESTERDAY_ISO, needs_grading=True)
    assert scoping.is_overdue(行, TODAY_ISO) is False


def test_待确认的不进超期清单_D17() -> None:
    """自动登记的还没人确认,不算进整改率、不催办 —— 也就谈不上超期。"""
    行 = _行(status=db.STATUS_PENDING, due_date=YESTERDAY_ISO)
    assert scoping.is_overdue(行, TODAY_ISO) is False


def test_待签复工令的不进超期清单() -> None:
    """``resuming`` = 复查已经合格了,只差我们自己签一份《复工令》。

    把它算成超期,等于拿"我们自己还没签复工令"去指控施工方拒不整改。
    """
    行 = _行(status=db.STATUS_RESUMING, due_date=YESTERDAY_ISO)
    assert scoping.is_overdue(行, TODAY_ISO) is False


def test_只有下过期限还没改好的那三档才可能超期() -> None:
    """八档过一遍,把 ``OVERDUE_STATUSES`` 这张表和判定行为对死。"""
    超期的 = {
        s
        for s in db.STATUSES
        if scoping.is_overdue(_行(status=s, due_date=YESTERDAY_ISO), TODAY_ISO)
    }
    assert 超期的 == {db.STATUS_NOTIFIED, db.STATUS_SUSPENDED, db.STATUS_REINSPECT_FAILED}
    assert 超期的 == set(scoping.OVERDUE_STATUSES)


# ===========================================================================
# 中文词表 —— 漏一档就是把英文枚举值送到工地师傅眼前
# ===========================================================================


def test_STATUS_ZH覆盖八档状态且没有多余的键() -> None:
    """🔴 八个状态每个都要有中文名。

    漏一档的表现是回给工友的话里冒出一个 ``reinspect_failed`` 这样的英文词,
    **不会有任何报错**;多一个键则说明 db 那边删了状态而这里没跟上,同样是漂移。
    (模块导入时那道守卫只查"漏",查不了"多",所以这条两头都比。)
    """
    assert set(scoping.STATUS_ZH) == set(db.STATUSES)


def test_八档中文名互不相同() -> None:
    """两档撞名 = 界面上两种完全不同的处境显示成同一句话,而谁也发现不了。"""
    assert len(set(scoping.STATUS_ZH.values())) == len(db.STATUSES)


def test_中文名里一个英文字母都不许有() -> None:
    """兜底:哪天有人图省事把某一档写成 ``pending(待确认)``,这条当场红。"""
    for 状态, 中文 in scoping.STATUS_ZH.items():
        assert not any(c.isascii() and c.isalpha() for c in 中文), 状态


def test_已出具暂停令不许写成已责令停工() -> None:
    """🔴 ``suspended`` 只证明**文书出了稿**,不证明工地真停了工(db/hazards.py 的
    STATUSES 头注)。对外措辞不许升级 —— 说成"已责令停工"是在声称一件没发生的事。"""
    assert scoping.STATUS_ZH[db.STATUS_SUSPENDED] == "已出具暂停令"


def test_RESULT_ZH覆盖全部复查结论() -> None:
    """留档文书上、回给工友的话里都不许出现 ``pass`` / ``fail``。"""
    assert set(scoping.RESULT_ZH) == set(db.DOC_RESULTS)
    assert scoping.result_zh("pass") == "合格"
    assert scoping.result_zh("fail") == "不合格"


def test_result_zh没有结论时给短横而不是猜一个() -> None:
    """文书行本来就没有结论(``result`` 是 NULL)。猜成「合格」比留个「—」危险得多。"""
    assert scoping.result_zh(None) == scoping.NO_VALUE


def test_result_zh对词表外的取值原样透出_不猜() -> None:
    """真出现说明 CHECK 被绕过了。纸上留着那个怪值,比悄悄译成「合格」安全。"""
    assert scoping.result_zh("怪值") == "怪值"


# ===========================================================================
# 期限的说法与 hazard_item 的形状
# ===========================================================================


def test_期限的说法由代码生成_带星期字() -> None:
    """「8月20日(周四)」这种说法只有 ``dates.format_display`` 产得出来 ——
    让模型自己换算星期,schedule 那边用 314 行证明过不可靠,而这个期限还要进文书。"""
    assert scoping.due_display(TODAY_ISO) == "8月20日(周四)"


def test_没定期限时是None不是空串() -> None:
    """空串会让上层写出「期限 」这种半句话,还多一种"看着像有"的形态要处理。"""
    assert scoping.due_display(None) is None


def test_hazard_item的键集合是对外契约() -> None:
    """🔴 对话链照它念人话,W10 的查询端点照它出 JSON,前端照它渲染。

    悄悄加一个字段 = 前端不认识、白占上下文;悄悄减一个 = 某一侧静默少一格。
    刻意**不在**里面的三个也一并说明白,免得有人"顺手补全":
      · ``photo_sha256``   幂等键的内节,对人没用;
      · ``project_id``     归属由调用方在汇总层统一说,逐行重复只会挤上下文;
      · ``grading_version`` 事后追溯用,不是对话内容。
    """
    项 = scoping.hazard_item(
        _行(status=db.STATUS_NOTIFIED, due_date=FUTURE_ISO), today_iso=TODAY_ISO
    )
    assert set(项) == {
        "hazard_no",
        "item",
        "grade",
        "status",
        "status_display",
        "due_date",
        "due_display",
        "overdue",
        "needs_grading",
    }
    for 不该有的 in ("photo_sha256", "project_id", "grading_version"):
        assert 不该有的 not in 项


def test_hazard_item把状态与期限都换成中文说法() -> None:
    项 = scoping.hazard_item(
        _行(status=db.STATUS_SUSPENDED, due_date=TODAY_ISO), today_iso=TODAY_ISO
    )
    assert 项["status_display"] == "已出具暂停令"
    assert 项["due_display"] == "8月20日(周四)"
    assert 项["status"] == db.STATUS_SUSPENDED  # 原始值照旧留着,前端要拿它对按钮
    assert 项["overdue"] is False  # 今天到期,不算超期


def test_hazard_item的needs_grading是真布尔() -> None:
    """库里存的是 0/1。原样透出去的话,前端 ``if (h.needs_grading)`` 看着能用,
    而 JSON 里是数字 —— 严格比较(``=== true``)的那一处会静默失效。"""
    项 = scoping.hazard_item(_行(needs_grading=True), today_iso=TODAY_ISO)
    assert 项["needs_grading"] is True


# ===========================================================================
# 「今天」= 香港日历日 —— 两条用例守同一件事
# ===========================================================================


class _假时钟:
    """只认带时区的 ``now(tz)``。

    照 ``attendance/receipt.py`` 的 ``snapshot_at`` 那条哲学做:naive 调用 =
    有人在靠宿主时区,当场炸而不是悄悄返回一个值。
    """

    定死的时刻 = datetime(2026, 8, 16, 20, 30, tzinfo=UTC)
    """UTC 8月16日 20:30 = 香港 **8月17日** 04:30 —— 跨午夜的关键证据时刻。"""

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        if tz is None:
            raise AssertionError("today_hk 调了不带时区的 now() —— 那就是在靠宿主时区")
        return cls.定死的时刻.astimezone(tz)  # type: ignore[arg-type]


def test_跨午夜取的是香港那天不是UTC那天(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC 还在 16 号晚上,香港已经是 17 号凌晨 —— 期限要跟香港这本日历走。

    这条**到处都跑得了**,补的正是下面那条 TZ 篡改用例的窟窿:本机这个 Python
    没有 ``time.tzset``(和 Windows 一样),那条会被跳过,只剩这条守着。
    """
    monkeypatch.setattr(scoping, "datetime", _假时钟)
    assert scoping.today_hk() == date(2026, 8, 17)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="平台没有 tzset(Windows / 本机这个 Python)")
def test_篡改宿主TZ后今天不变() -> None:
    """W7 上线闸⑥那类静默错误的单测形态,写法照抄 ``test_checkin_api.py``。

    挑的两个时区是刻意的:``Pacific/Kiritimati`` 是 UTC+14,``Etc/GMT+12`` 是 UTC-12
    (IANA 的 Etc 系列符号是反的,别按字面读),两者相差 **26 小时** ——
    超过一整天,所以它们的**日历日在任何时刻都不相同**。也就是说,只要实现里出现
    ``date.today()`` 这种靠宿主时区的写法,这条用例**不管什么时候跑都会红**,
    不像"挑两个普通时区"那样只有一天里的某几个小时才抓得到。

    monkeypatch 在这儿不够用:它恢复环境变量但**不会替你再调一次 tzset()**,
    进程会带着最后一个时区跑完剩下的测试 —— 所以手工 try/finally。
    """
    原值 = os.environ.get("TZ")
    try:
        取到的 = []
        for tz in ("Pacific/Kiritimati", "Etc/GMT+12"):
            os.environ["TZ"] = tz
            time.tzset()
            取到的.append(scoping.today_hk())
        assert 取到的[0] == 取到的[1], "宿主时区一改「今天」就跟着变 —— 那是在靠宿主猜"
        assert 取到的[0] == datetime.now(HK).date()
    finally:
        if 原值 is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = 原值
        time.tzset()


# ===========================================================================
# import 约束 —— 这个模块是共享地面,它的依赖面就是它的全部价值
# ===========================================================================

_允许的_IMPORT = frozenset(
    {
        "__future__",
        "datetime",
        "typing",
        "gyt.agents.schedule.dates",
        "gyt.attendance.receipt",
        "gyt.db",
    }
)
"""``scoping.py`` 允许出现的 import 目标(``from X import ...`` 里的那个 X)。

要往这张表里加一行,先回去读 ``scoping.py`` 头注那一节:加进来的东西会不会
把 langchain / langgraph / python-docx 捎带进来?捎带了就等于把"将来让 HTTP 层
真的轻量"那条路堵死,而堵死不会有任何报错。
"""


def _本文件的import目标() -> list[str]:
    """用 AST 扫 ``scoping.py`` 的源码,取出它 import 的模块名。

    为什么不在运行时查 ``sys.modules``:测到这儿的时候 langchain 早被别的用例导进来了,
    查什么都是脏的。而且父包 ``agents/supervision/__init__.py`` 本来就会拉 langchain
    (2026-08-16 实测,原因与修法写在 scoping.py 头注)—— **能钉住的、也是真正
    要钉住的,是这个文件自己写了哪些 import。**
    """
    树 = ast.parse(Path(scoping.__file__).read_text(encoding="utf-8"))
    目标: list[str] = []
    for 节点 in ast.walk(树):
        if isinstance(节点, ast.Import):
            目标 += [别名.name for 别名 in 节点.names]
        elif isinstance(节点, ast.ImportFrom):
            # level>0 是相对导入(from . import x)—— 一律记成本包,下面那条用例会拦
            目标.append("." * 节点.level + (节点.module or ""))
    return 目标


def test_scoping只许import白名单里那几样() -> None:
    """越界的 import 一律当红。名单短是刻意的:它就是这个模块存在的理由。"""
    越界 = sorted(set(_本文件的import目标()) - _允许的_IMPORT)
    assert not 越界, f"scoping.py 多了这些 import:{越界} —— 先读它头注那一节"


def test_scoping不许import本包任何模块_否则成环() -> None:
    """``tools.py`` 与 ``documents.py`` 都 import 本模块。它回头 import 它们中的任何一个,
    当场循环导入 —— 而循环导入炸出来的报错往往指着第三个文件,查起来极费劲。"""
    回头的 = [名 for 名 in _本文件的import目标() if 名.startswith((".", "gyt.agents.supervision"))]
    assert not 回头的, f"scoping.py 回头 import 了本包的 {回头的},与 tools.py 成环"
