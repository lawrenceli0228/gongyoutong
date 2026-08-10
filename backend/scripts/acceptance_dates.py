"""真机验收的**日期派生层** —— 注入「今天」,算出 live_acceptance.py 全部断言要用的期望值。

===========================================================================
为什么单独拆一个模块(而不是写在 live_acceptance.py 里)
---------------------------------------------------------------------------
``live_acceptance.py`` 顶层就是可执行代码,没有 ``if __name__ == "__main__"`` ——
``import`` 它等于真连 :2024、真跑 25 轮、真花钱。所以写在它里面的东西
**永远没法被 pytest 覆盖**。拆出来之后,十四日模拟才跑得起来
(``backend/tests/unit/test_acceptance_dates.py``)。

本模块的纪律,和 ``dates.py`` 同一套:
  · 纯函数、无副作用、不联网、不建图、不碰库;
  · ``today`` 由调用方注入,**严禁 date.today()** —— 谁调用谁负责说清今天是哪天。

===========================================================================
dates.py 怎么进来:先正常 import,进不来才按文件路径加载(顺序不许反)
---------------------------------------------------------------------------
两条路都要留,因为两个环境各缺一半:
  · 容器里的 pytest / backend/.venv —— ``import gyt.agents.schedule.dates`` 好使;
  · 宿主那个裸 3.11 解释器(``~/.local/share/uv/python/cpython-3.11-macos-x86_64-none``)
    —— 既没装 gyt 也没装 langgraph,包式 import 直接 ModuleNotFoundError。
    而离线跑十四日模拟恰恰只有它可用(Intel Mac 装不上 torch,``uv run pytest`` 起不来)。

**顺序必须是「先包式、后文件路径」,这是踩出来的,不是洁癖:**
    2026-08-10 实测,先按文件路径加载会把 ``dates.py`` 的**测试覆盖率打成 0%**。
    容器里跑 ``make test-docker`` 时,``test_acceptance_dates.py`` 按字母序排在
    ``test_schedule_dates.py`` 前面,先执行了一遍 dates.py,而那次执行挂的模块名是
    ``gyt_schedule_dates_for_acceptance`` —— 不在 ``--cov=gyt`` 的源码包名下面。
    coverage 据此判定「这个文件不用追踪」,**并且把这个判断按文件名缓存住**;
    后面 ``test_schedule_dates.py`` 那 67 个用例再怎么跑,一行都不计。
    现象:总覆盖率 88% → 84%,dates.py 那一行赫然写着 ``143 143 0%``,
    而 67 个用例全绿 —— 典型的「测试没坏,尺子坏了」。
    先走包式 import 就没这回事:模块名是 ``gyt.agents.schedule.dates``,coverage 认。
    (单独跑 ``pytest tests/unit/test_schedule_dates.py --cov=gyt`` 是 100%,
     两相对照即可复现。)

副作用要知道:走文件路径那条时,加载出来的是一个**独立的模块对象**,和
``import gyt.agents.schedule.dates`` 拿到的不是同一个。dates.py 是纯函数、零模块状态,
所以无害 —— 但哪天它长出模块级可变状态,这条注释就该重新评估。

===========================================================================
核心不变量:A1 / A3 / A4 / C3 这四个日期,**任何一天都必须两两不等**
---------------------------------------------------------------------------
这不是洁癖,是**判据强度**:两条断言撞到同一天,当天就在数学上不可能报红。

  A3 == A1(旧口径每逢周四)——**最贵的一条**。A3 是「复检钢筋那条改到 X」,
      两者同一天时 ``reschedule_task`` 把周五改成周五:rowcount 仍是 1、
      回执照样念得出那个日期、D1 断的 ``due_date`` 也照样对。于是
      「A3 这一步压根没执行」当天**没有任何断言区分得出来** —— 整条改期链路
      (含 ``WHERE status='open'`` 那道闸)零覆盖,A3 和 D1 验的都退化成 A1 的结果。
  C3 == A4(旧口径每逢周六)—— D3 退化成 D2 的复读,整改任务落没落库验不出来。
  A3 == A4(周三)、A1 == C3(周日)—— 各吃掉一条判据,同一个道理。

旧口径(A1 明天 / A3 固定问「周五」/ A4 后天 / C3 固定问「下周一」)的撞车矩阵,
2026-08-10 从 2026-01-01 起连续 1826 天逐日扫出来:

      A1 == A3 → 周四        A3 == A4 → 周三
      A1 == C3 → 周日        A4 == C3 → 周六

七天里有四天在偷工减料。这段扫描不是一次性的:测试里
``test_五年全量_四个日期任何一天都两两不等`` 与 ``test_旧口径的撞车矩阵``
是它的常驻版,旧口径那条把上面这张矩阵原样钉住,新口径那条要求 0 天违反。

怎么修:**不许按星期几写死映射表**(那是把日历知识抄第二遍,抄错了没人拦)。
A1 / A4 固定不动当锚,A3 / C3 各给一张**候选表** —— 从演示原话打头、按星期几依次顺延,
取第一个不与已占日期撞车的那个,这就是 ``_pick()`` 的全部职责。
A3 先挑、C3 后挑(A3 那条撞车最贵,给它优先权),顺序定死不许反。

===========================================================================
两道锚点 + 一条不变量,分工别搞混(都在 tests/unit/test_acceptance_dates.py 里)
---------------------------------------------------------------------------
  锚点一:``expected(date(2026, 8, 8))`` 对上重构前那批硬编码字面量。
          证明「没有偷偷改变行为」。七项里**五项逐字不动**;C3 那两项
          (``c3_display`` / ``d3_iso``)是这次为了拆撞车**有意**改的,
          测试里单独登记了旧值 → 新值和为什么改,不是顺手改掉。
  锚点二:独立预言机 —— 完全不碰 dates.py,用 stdlib **逐日扫描**重算一遍。
          证明「dates.py 的日历逻辑本身是对的」。只覆盖本模块用到的这几个短语。
  不变量:四日两两不等,五年全量逐日核。证明「判据强度没有被日历偷走」。
三样缺一不可:只有锚点一,dates.py 哪天算错了期望值会跟着一起错,验收照样全绿;
只有锚点二,证明不了「和 2026-08-08 那天人工核对过的口径一致」;
只有前两样,四条断言可以在某几个星期几集体退化成废话,而两道锚点都察觉不到。
"""

# 本模块**刻意不写** ``from __future__ import annotations``(全仓其余文件都写了)。
# 理由是实测踩出来的:PEP 563 把注解变成字符串后,@dataclass 要回头去
# ``sys.modules[cls.__module__].__dict__`` 里解析它们;而按文件路径加载出来的模块
# **默认不在 sys.modules 里**,于是装饰器当场炸:
#     AttributeError: 'NoneType' object has no attribute '__dict__'
#         (dataclasses.py:712 _is_type)
# 而**本模块自己**就是按文件路径被加载的(live_acceptance.py 与两个测试都这么干,
# 因为 scripts/ 不是包)。这里的注解在 3.11 下本来就全部运行期可用,不需要 PEP 563 ——
# 与其要求每个加载方都记得先往 sys.modules 里塞一手,不如这一行干脆不写。
import importlib.util
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Final

# --- 加载日历真相源 -----------------------------------------------------------

_DATES_PY: Final[Path] = (
    Path(__file__).resolve().parents[1] / "src" / "gyt" / "agents" / "schedule" / "dates.py"
)


def _load_dates() -> ModuleType:
    """拿到日历模块。优先包式 import(覆盖率的缘故,见模块 docstring),退而求其次按路径加载。"""
    try:
        from gyt.agents.schedule import dates
    except ImportError:
        return _load_dates_by_path()
    return dates


def _load_dates_by_path() -> ModuleType:
    """兜底:按文件路径加载 dates.py。找不到就当场说人话,别让调用方对着 AttributeError 猜。"""
    if not _DATES_PY.is_file():
        raise RuntimeError(
            f"找不到日历模块:{_DATES_PY}。"
            "它是 live_acceptance 全部日期期望值的唯一真相源 —— "
            "文件被挪走或改名了,先把这里的路径改对再跑验收。"
        )
    spec = importlib.util.spec_from_file_location("gyt_schedule_dates_for_acceptance", _DATES_PY)
    if spec is None or spec.loader is None:  # pragma: no cover — 路径存在时 importlib 必给 spec
        raise RuntimeError(f"日历模块加载不起来:{_DATES_PY}")
    module = importlib.util.module_from_spec(spec)
    # 先登记再 exec:dates.py 现在不需要这一手(它没有 dataclass),但哪天它长出一个,
    # 缺了这行会炸成一句和日期毫无关系的 AttributeError —— 上面那段注释里那个坑。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_dates: Final[ModuleType] = _load_dates()

DueParseError = _dates.DueParseError
"""转出来给测试用 —— 免得测试再按文件路径加载一遍 dates.py。"""


# --- 脚本发出去的原话(唯一一份) ---------------------------------------------
#
# 期望值和「到底说了哪句话」必须绑在一处:两边各写一份短语,哪天有人改了脚本里的
# 提问却忘了改期望值,断言会以一种非常难查的方式变成「验了个寂寞」——
# 它照样绿,只是绿得毫无意义。
#
# ⚠️ A3 / C3 的短语**不再是常量,而是按日历从候选表里挑出来的**(见上方「核心不变量」)。
#    live_acceptance.py 必须用 ``D.a3_phrase`` / ``D.c3_phrase`` 拼提问,
#    **不许再把「周五」「下周一之前」写死在句子里** —— 写死就等于每周有两天在验错东西,
#    而且是那种「屏幕上全绿、实际零覆盖」的验错。四个槽位都给了 ``aN_phrase`` 字段,
#    统一从那里取,别挑着用。

PHRASE_A1: Final[str] = "明天上午"
"""A1「给我建个任务:{}复检三层钢筋」。固定不动 —— 它是不变量的两个锚之一。

为什么 A1/A4 当锚而不是跟着一起顺延:这两条是**相对日**词法(today+1 / today+2),
它们的日期与星期几无关,天然不会自己跟自己撞;让它们不动,顺延的自由度全留给
A3(周X)和 C3(下周X),读脚本的人也只需要盯两张候选表。
"""

PHRASE_A4: Final[str] = "后天"
"""A4「再记一条:{}清点脚手架扣件」。同样固定,理由见 PHRASE_A1。

它还扛着 A10 正常支的前提:T2 的期限(= today+2)必须落在「本周日」这个 cutoff 里,
否则那一支的表是空的、三条正向铁证一条都拿不到。today+2 <= 本周日 ⟺ weekday <= 4,
而正常支只在周一~周三出现,恒成立(测试 ``test_十四日模拟_语义不变量`` 钉住)。
"""

A3_CANDIDATES: Final[tuple[str, ...]] = ("周五", "周六", "周日", "周一", "周二", "周三", "周四")
"""A3「复检钢筋那条改到{}」的候选表。

顺序 = **演示原话「周五」打头,撞车了就按星期几依次往后顺延**。
2026-01-01 起 1826 天逐日核过,实际用到的只有前三个:五天用原话「周五」,
周三顺延一格到「周六」(「周五」那天恰是后天,让给 A4),
周四顺延两格到「周日」(「周五」是明天让给 A1、「周六」是后天让给 A4)。
七个全列上是余量,让 ``_pick`` 的兜底报错永远轮不到 —— 将来 A1/A4 的口径万一动了,
也不至于当场没得挑。
"""

C3_CANDIDATES: Final[tuple[str, ...]] = (
    "下周一之前",
    "下周二之前",
    "下周三之前",
    "下周四之前",
    "下周五之前",
    "下周六之前",
    "下周日之前",
)
"""C3「把这些隐患记成一条整改任务,{}搞定」的候选表,规则同 A3:原话「下周一之前」打头。

「之前」这个后缀**留在短语里**(不是由调用方拼上去的),这样 ``_pick`` 解析的就是
脚本真正发出去的那一串,顺带把 dates.py 的 ``_strip_noise`` 尾缀剥离也覆盖到了。
同一段 1826 天扫描:周一~周五用原话「下周一之前」,周六顺延一格到「下周二之前」
(那天「下周一」恰是后天),周日顺延两格到「下周三之前」(「下周一」是明天、
「下周二」是后天,连让两格)。
"""

PHRASE_A10_PAST: Final[str] = "本周三"
"""A10 报错支的主问:今天是周四~周日时,「本周三」已经过了 → 工具必须报错不猜。"""

HINT_A10_PAST: Final[str] = "下周三"
"""报错支的回执里**必须**出现的改法。

来自 ``dates.py`` 的 ``_parse_this_week`` 报错原文「要记下周的就说“下周三”」
(prompt.md 红线 1 要求原样转述)。断它 = 断一条产品要求:报了错就得给改法,
不能给师傅一条死胡同。测试 ``test_报错支与探针的改法提示都与dates原文一致``
拿 dates.py 抛出来的真异常核过,不是凭记忆写的(那条测试的存在就是因为
「凭记忆写措辞」这件事在本仓已经栽过)。
"""

PHRASE_A10_AHEAD: Final[str] = "本周日"
"""A10 正常支的主问:今天是周一~周三时,「本周三」还没过,报错支根本不成立(见 _a10_branch)。"""

PHRASE_A10_PROBE: Final[str] = "本周一"
"""A10 正常支的**补问探针**:把「本周X已过 → 报错不猜」这条词法拉进正常支。

为什么非补不可(2026-08-10 逐日核过 dates.py):正常支主问的是「本周日」,而
「本周日」和「周日」在周一~周三永远解析到同一天 —— 那一支**碰都碰不到**
``_parse_this_week`` 的报错分支。后果是:有人把那个 raise 删掉、让本周三静默滚成
下周三,周一~周三跑验收照样 25/25 全绿。补上「本周一」之后,周二/周三这两天
正常支也会真的踩到报错分支,「报错不猜」的词法覆盖从 4/7 天提到 6/7 天。
周一是唯一盖不到的那天 —— 那天日历上根本不存在「已经过的本周 X」(本周一就是今天),
不是漏了,是不存在。
"""

HINT_A10_PROBE: Final[str] = "下周一"
"""探针回执里必须出现的改法,来源同 HINT_A10_PAST(真异常原文核过)。"""

BRANCH_PAST: Final[str] = "报错不猜"
BRANCH_AHEAD: Final[str] = "正常解析"

_WEEKDAY_CHARS: Final[str] = "一二三四五六日"
"""下标 = date.weekday()。与 dates.py 的同名常量同源 —— 这里只用于拼 A10 的标签
(「今天周三」),不参与任何日期计算,所以抄一份比按文件路径再挖一遍私有常量划算。"""


@dataclass(frozen=True)
class Expected:
    """一次验收要用到的全部原话与日期期望值。字段按**断言编号**命名,不按语义。

    为什么按编号:改脚本的人是照着屏幕上那行 ``FAIL  A3 ...`` 回来找的,
    ``a3_display`` 一眼对得上;``friday_display`` 还得先在脑子里推一层「A3 说的是周五」。

    同源关系(一个短语派生出展示串和 ISO 两种形态,分别给 A 组和 D 组用):

        A1 a1_phrase → a1_display
        A3 a3_phrase → a3_display ──→ d1_iso   (D1 断库里 T1 的 due_date)
        A4 a4_phrase → a4_display ──→ d2_iso   (D2 断库里 T2 的 due_date)
        C3 c3_phrase → c3_display ──→ d3_iso   (D3 断库里整改任务的 due_date)

    展示串和 ISO 必须成对派生自**同一个 date**:A 组断展示串、D 组断 ISO,
    两边一旦各算各的,错位了还会各自「自洽」—— 这正是十四日模拟 M6 那条在防的事。

    四个 ``*_phrase`` 是**发出去的原话**,不是给人看的标签:live_acceptance.py 必须
    拿它们拼提问。A1/A4 恒等于 PHRASE_A1/PHRASE_A4,A3/C3 按日历从候选表里挑
    (见模块 docstring 的「核心不变量」)。
    """

    today: date
    today_iso: str
    weekday_char: str
    """今天周几(「一」~「日」),只用于把分支信息打进 A10 的标签。"""

    a1_phrase: str
    a3_phrase: str
    a4_phrase: str
    c3_phrase: str

    a1_display: str
    a3_display: str
    a4_display: str
    c3_display: str

    d1_iso: str
    d2_iso: str
    d3_iso: str

    a10_phrase: str
    """A10 这一轮主问要发出去的期限短语(两支不同,见 a10_branch)。"""
    a10_branch: str
    """BRANCH_PAST(报错不猜)或 BRANCH_AHEAD(正常解析)。"""
    a10_hint: str | None
    """报错支的回执里必须出现的改法(= HINT_A10_PAST);正常支为 None。"""
    a10_due_display: str
    """A10 正常支里,表格中 T2 那一行的期限列应该长什么样。

    == a4_display:A10 跑在 A5(T1 销项)之后,台账里唯一一条未完成的就是
    A4 记下的 T2(期限 = 后天)。断它 = 断「模型照抄了工具算好的 due_display」,
    是事实级判据而不是措辞级。报错支用不上这个字段,但仍然填上:
    让 dataclass 保持定长,测试好写。
    """
    a10_probe_phrase: str | None
    """A10 正常支的补问探针短语;**None = 这一天不发探针**(报错支,以及周一)。

    用法见模块外的接线说明:探针的判据要 ``and`` 进 A10 那一条 check,
    **不许新开一条断言** —— 断言总数 25 是对外口径(README / CLAUDE.md 都写着)。
    """
    a10_probe_hint: str | None
    """探针回执里必须出现的改法(= HINT_A10_PROBE);不发探针时为 None。"""


def _resolve(phrase: str, today: date) -> date:
    """短语 → date。解析成「无期限」当场喊停,别让 None 一路飘到断言里变成假绿灯。"""
    day = _dates.parse_due(phrase, today=today)
    if day is None:  # pragma: no cover — 候选表里没有空串,parse_due 不会返回 None
        raise RuntimeError(f"「{phrase}」被解析成了「无期限」,验收期望值没法算。")
    return day


def _pick(
    slot: str,
    candidates: tuple[str, ...],
    today: date,
    taken: dict[date, str],
) -> tuple[str, date]:
    """从候选表里挑第一个**不与已占日期撞车**的短语,返回 (原话, 日期) 并就地登记。

    这就是「四日不撞车」这条不变量的**唯一**实现点(为什么必须有它,见模块 docstring)。
    调用方按 A1 → A4 → A3 → C3 的顺序依次调用,``taken`` 一路传下去。

    A1/A4 也走这个函数,只是候选表只有一个元素 —— 看着多余,其实是让
    「有人把 PHRASE_A1 和 PHRASE_A4 改成同一个词」这种事也当场炸,
    而不是安安静静地让 D1/D2 从此验同一行。

    候选表用尽 = 当场 RuntimeError,**不返回一个撞车的日期**。
    这条路正常情况下走不到(候选表都是七个,鸽笼原理保证有富余),
    留着是因为「悄悄退化」比「当场报错」贵得多:前者会让四条断言集体变成废话,
    而屏幕上依旧 25/25。
    """
    for phrase in candidates:
        day = _resolve(phrase, today)
        if day not in taken:
            taken[day] = slot
            return phrase, day
    occupied = "、".join(f"{d.isoformat()}={s}" for d, s in sorted(taken.items()))
    raise RuntimeError(
        f"{today.isoformat()} 这天,{slot} 的候选表 {candidates} 全撞车了(已占:{occupied})。"
        "候选表要么加词,要么这四条断言的日期口径得重新设计 —— "
        "别改成「撞就撞吧」,那正是这套机制要挡的事。"
    )


def _a10_branch(today: date) -> tuple[str, str]:
    """A10 主问该问哪句话、走哪一支 —— 判据是「本周三过了没」,由日历说了算。

    为什么必须分支(2026-08-10 实测 dates.py 连续 7 天):
        周四~周日:``parse_due("本周三")`` 抛 DueParseError「本周三已经过了…」
        周一~周三:正常解析成本周三,**一点错都没有**
    原来的脚本写死问「本周三之前」并断「已经过」,等于要求日历每天都是周四到周日。
    周一到周三跑验收必红,而红的是脚本不是系统 —— 这种红比不测还坏,
    它会训练人「这条本来就红,跳过」,下次真出问题也没人看。

    周一那天日历上**根本不存在**「已经过的本周 X」(本周一就是今天),
    所以正常支不是妥协,是这三天唯一能问的主问。

    正常支为什么问「本周日」而不是「本周三」:
        它得保证 T2(期限 = today+2)落在 cutoff 里,不然表是空的、什么都断不了。
        本周日 = 本周一+6,要求 today+2 ≤ 本周一+6,即 weekday ≤ 4 —— 周一~周三恒成立。
        (改问「本周三」的话:周二 cutoff=明天、周三 cutoff=今天,T2 都进不来,
         而且 list_tasks 空结果时的 user_msg 是「台账里现在没有任务。」,
         事实上还错 —— 台账里明明有 T2,只是没到期。那条文案问题另记 TODO。)

    正常支的**词法窟窿**由 ``_a10_probe`` 的补问堵上,别再重复那段说明。
    """
    try:
        _dates.parse_due(PHRASE_A10_PAST, today=today)
    except _dates.DueParseError:
        return PHRASE_A10_PAST, BRANCH_PAST
    return PHRASE_A10_AHEAD, BRANCH_AHEAD


def _a10_probe(today: date) -> tuple[str | None, str | None]:
    """正常支要不要补一问「本周一」—— 同样由日历说了算,不按星期几写死。

    判据和主问是同一条:``parse_due("本周一")`` 抛不抛 DueParseError。
    抛 = 这天日历上确实有「已经过的本周 X」,补问才有东西可验(周二、周三);
    不抛 = 本周一就是今天,补问会变成一次正常解析,验不到报错分支,那就别发
    —— 白搭一轮模型调用,还会让 A10 那条 check 的判据变得似是而非。

    这里刻意**不写** ``if today.weekday() in (1, 2)``:那是把日历知识抄第二遍,
    dates.py 哪天调了「本周X」的口径,写死的那份不会跟着变,而这份会。
    """
    try:
        _dates.parse_due(PHRASE_A10_PROBE, today=today)
    except _dates.DueParseError:
        return PHRASE_A10_PROBE, HINT_A10_PROBE
    return None, None


def expected(today: date) -> Expected:
    """算出这一天跑验收时,全部原话与期望值。纯函数,同一个 today 恒等输出。

    四个槽位的挑选顺序是**契约**:A1 → A4 → A3 → C3。
    前两个是固定锚,后两个按候选表顺延;A3 排在 C3 前面,因为 A3 撞车最贵
    (整条改期链路零覆盖),优先权给它。改这个顺序 = 改期望值,别顺手动。
    """
    taken: dict[date, str] = {}
    a1_phrase, a1_day = _pick("A1", (PHRASE_A1,), today, taken)
    a4_phrase, a4_day = _pick("A4", (PHRASE_A4,), today, taken)
    a3_phrase, a3_day = _pick("A3", A3_CANDIDATES, today, taken)
    c3_phrase, c3_day = _pick("C3", C3_CANDIDATES, today, taken)

    a10_phrase, a10_branch = _a10_branch(today)
    if a10_branch == BRANCH_AHEAD:
        probe_phrase, probe_hint = _a10_probe(today)
        a10_hint: str | None = None
    else:
        probe_phrase, probe_hint = None, None
        a10_hint = HINT_A10_PAST

    return Expected(
        today=today,
        today_iso=today.isoformat(),
        weekday_char=_WEEKDAY_CHARS[today.weekday()],
        a1_phrase=a1_phrase,
        a3_phrase=a3_phrase,
        a4_phrase=a4_phrase,
        c3_phrase=c3_phrase,
        a1_display=_dates.format_display(a1_day),
        a3_display=_dates.format_display(a3_day),
        a4_display=_dates.format_display(a4_day),
        c3_display=_dates.format_display(c3_day),
        d1_iso=a3_day.isoformat(),
        d2_iso=a4_day.isoformat(),
        d3_iso=c3_day.isoformat(),
        a10_phrase=a10_phrase,
        a10_branch=a10_branch,
        a10_hint=a10_hint,
        a10_due_display=_dates.format_display(a4_day),
        a10_probe_phrase=probe_phrase,
        a10_probe_hint=probe_hint,
    )


__all__ = [
    "A3_CANDIDATES",
    "BRANCH_AHEAD",
    "BRANCH_PAST",
    "C3_CANDIDATES",
    "HINT_A10_PAST",
    "HINT_A10_PROBE",
    "PHRASE_A1",
    "PHRASE_A10_AHEAD",
    "PHRASE_A10_PAST",
    "PHRASE_A10_PROBE",
    "PHRASE_A4",
    "DueParseError",
    "Expected",
    "expected",
]
