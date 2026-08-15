"""文书编号生成器 —— 六种监理文书 + 隐患 + 巡检记录,全仓只有这一处拼编号。

===========================================================================
为什么要一个共用生成器,而不是各处 f-string 现拼
---------------------------------------------------------------------------
① **时间必须是香港日历时间。** 编号里的日期时刻是文书的对外身份 —— 监理在
   电话里报它、在台账里对它、上报主管部门时按它排证据链。全仓唯一的时间
   权威是 ``attendance/receipt.py``(``HK = ZoneInfo("Asia/Hong_Kong")``)。
   **不许用 ``datetime.now(UTC).astimezone()``**:那是**宿主时区** ——
   容器里是 ``TZ=Asia/Shanghai``、本机开发可能是任意时区,两地数值碰巧一致
   (都是 UTC+8 无夏令时)**是巧合不是保证**,而 ``make test`` 在同为 UTC+8
   的本机全绿也发现不了(方案 §6.3 原文;``report/tools.py`` 以前就踩着这条,
   本次一并改掉)。

② **必须有随机尾,而且必须有撞库重试。** ``receipt.py:78-79`` 的原注释写得
   很清楚:随机尾不是装饰(同一秒内多人操作是真实场景,只有日期时刻的编号
   **必碰**),兜底靠库里 ``UNIQUE`` + **调用方撞库重试**(重试就是再摇一次
   随机尾)。方案 §6.3 点名:v2 抄了随机后缀却没抄重试 —— 那样并发签发时
   偶发冲突会**让整次法律文书操作直接失败**。重试收在 ``generate_unique``
   一处,别让每个端点各写各的 while 循环。

③ **每类编号各写各的正则。** attendance 的 ``GYT-A-…`` 与 report 的
   ``REPORT_RECEIPT_PATTERN`` **互不匹配是实测过的**(test_checkin_api.py
   有守门断言)。哪天给某类文书挂编号溯源守卫,**必须拿这一类自己的正则** ——
   拿错一条会**一个编号都认不出、静默全放行**,而测试照绿。
   本模块的正则与生成器由同一个 ``kind.value`` 派生,格式改了两边一起变;
   test_doc_no.py 里还有一条「生成的号必须被自己的正则 fullmatch」的闸。

===========================================================================
关于 ``core`` → ``attendance`` 这条 import
---------------------------------------------------------------------------
``core/__init__.py`` 写着「这一层只依赖 gyt.config 和第三方库」,而这里
import 了 ``gyt.attendance.receipt``。这是**有意的例外**,理由两条:

  · ``receipt.py`` 一个 gyt 模块都不 import(只用标准库),``attendance/__init__.py``
    是纯 docstring —— 模块图上不构成环,也不会捎带拉起 PIL 那类重依赖
    (拉 PIL 的是同包的 watermark.py,这里没碰)。
  · 把 ``HK`` 抄进 core 就等于制造**第二个时间真相源**,而 CLAUDE.md 的同源
    清单点名「唯一时间权威 = attendance/receipt.py」。两害相权,一条向下的
    import 比两份时区常量安全得多。

要动这条边之前先想清楚:目标不是让依赖图好看,是让全仓只有一个地方决定
「现在几点」。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from enum import StrEnum
from typing import Final

from gyt.attendance.receipt import TimeSnapshot, make_snapshot

logger = logging.getLogger(__name__)


class DocKind(StrEnum):
    """文书种类 → 编号里的类型段。**枚举值就是类型段本身。**

    ⚠️ 用 ``StrEnum`` 而不是隔壁 ``ErrorCode`` / ``ArtifactKind`` 那种
    ``(str, Enum)``:那两个是**共享接口契约 v1 明文钉死**的写法,pyproject 里
    专门给它们开了 UP042 豁免(豁免注释里写着「给 lint 让路,不给契约让路」)。
    本枚举不在那份契约里,照着抄只会白白多一条豁免。别为了"风格统一"改回去。

    取值来自方案 §6.3 的对照表,不许在别处另起简称。

    ⚠️ **本枚举与 ``hazard_docs.doc_type`` 的受控词表是「部分重叠」,不是「对齐」**
    (2026-08-16 复核时把措辞改精确了。关系有测试钉着:
    ``test_doc_no.py`` 的 ``test_成员名对齐hazard_docs的doc_type词表``,
    它断言的正是下面这两个孤儿):

      · ``kind.name.lower()`` 落在 doc_type 词表里的只有五档 —— notice /
        suspension / resumption / owner_report / authority_report;
      · ``HAZARD`` **根本不是一种文书**,它是隐患自己的身份号(``hazards.hazard_no``),
        doc_type 词表里没有也不该有 ``hazard``;
      · 反过来,doc_type 里的 ``reinspect``(复查记录行)在 §6.3 的编号表里
        **没有分配类型段** —— 复查行不出文书,``hazard_docs.doc_no`` 那一格
        目前由调用方给什么就是什么。

    所以 S4 写端点时:文书那五档用 ``DocKind[…].name.lower()`` 当 doc_type 是安全的,
    但**别写一个"六档一一对应"的循环**去覆盖 doc_type 词表 —— 两头各有一个孤儿。
    真要给 reinspect 编号,回来加一档并同步 ``DOC_TITLE_ZH``,别在调用点就地拼
    字符串:就地拼的格式没人给它写正则,将来挂守卫时认不出来。
    """

    HAZARD = "H"
    NOTICE = "TZ"
    SUSPENSION = "ZT"
    RESUMPTION = "FG"
    OWNER_REPORT = "JS"
    AUTHORITY_REPORT = "JB"


DOC_TITLE_ZH: Final[dict[DocKind, str]] = {
    DocKind.HAZARD: "隐患",
    DocKind.NOTICE: "监理通知单",
    DocKind.SUSPENSION: "工程暂停令",
    DocKind.RESUMPTION: "工程复工令",
    DocKind.OWNER_REPORT: "致建设单位报告",
    DocKind.AUTHORITY_REPORT: "监理报告",
}
"""种类 → 中文名。文书标题、文件名、报错话术都从这里取。

散在各端点里写死中文名的下场,W7 已经吃过一次(D12 换简繁时到处找漏网的
字符串)—— 集中在这一张表里,换词只改一处。有测试钉「每个枚举成员都有名字」。
"""

_TIME_FORMAT: Final[str] = "%Y%m%d-%H%M%S"
"""编号里日期时刻那一段的写法。生成端与正则端同源于它 + ``kind.value``。"""

_TAIL_HEX_BYTES: Final[int] = 2
"""随机尾字节数。2 字节 = 4 位十六进制 = 65536 个坑位,与 ``receipt.py`` 同口径:
同秒 10 次操作的碰撞概率约 0.07%,剩下的交给 ``generate_unique`` 撞库重试。"""

DEFAULT_ATTEMPTS: Final[int] = 5
"""撞库重试的默认次数。

为什么是 5 而不是「一直重试到成功」:``exists`` 每次都要真打一次库,而连撞
5 次意味着**不是运气问题**(65536 个坑位连中 5 次的概率是 10 的负 20 次方
量级)—— 那时候几乎一定是 ``exists`` 的实现或查询条件写错了,一直转只会把
一次可诊断的失败拖成一个挂死的请求。
"""


def _pattern_for(kind: DocKind) -> str:
    """由类型段派生这一类的正则。与 ``new_doc_no`` 同源,不许手抄第二份。"""
    return rf"GYT-{kind.value}-\d{{8}}-\d{{6}}-[0-9a-f]{{4}}"


PATTERNS: Final[dict[DocKind, str]] = {kind: _pattern_for(kind) for kind in DocKind}
"""种类 → 这一类编号的正则。挂编号溯源守卫时取自己那一条,别图省事共用一条宽的。"""

HAZARD_NO_PATTERN: Final[str] = PATTERNS[DocKind.HAZARD]
NOTICE_NO_PATTERN: Final[str] = PATTERNS[DocKind.NOTICE]
SUSPENSION_NO_PATTERN: Final[str] = PATTERNS[DocKind.SUSPENSION]
RESUMPTION_NO_PATTERN: Final[str] = PATTERNS[DocKind.RESUMPTION]
OWNER_REPORT_NO_PATTERN: Final[str] = PATTERNS[DocKind.OWNER_REPORT]
AUTHORITY_REPORT_NO_PATTERN: Final[str] = PATTERNS[DocKind.AUTHORITY_REPORT]


class DocNoExhaustedError(RuntimeError):
    """连着几次摇出来的编号都已被占用 —— 本次签发必须放弃,不许硬写一个上去。

    调用方接住它之后要做的是:回一个 ``fail()`` 信封让人重试,**不要**降级成
    「用最后那个撞了的号继续」。文书编号是对外身份,两份不同的文书顶着同一个
    编号,比这次没出成严重得多。异常文本只进日志(``tool_guard`` 会拦下来,
    见 core/errors.py 的说明),别原样透给用户。
    """


def new_doc_no(kind: DocKind, snap: TimeSnapshot | None = None) -> str:
    """摇一个文书编号:``GYT-<类型>-YYYYMMDD-HHMMSS-xxxx``。

    参数:
        kind: 文书种类,决定类型段。
        snap: 时间快照。不传就现取一个。

    **什么时候要显式传 snap:**

      · 撞库重试(``generate_unique`` 内部)—— 重试只该换随机尾,不该让编号
        里的时刻跟着往后飘。飘了以后没人能凭编号对回真正的签发时刻。
      · 一次动作产出多份文书(§5.1 的「三文书原子产出」)—— 三份共用一个
        快照,时刻才对得齐;各取各的 now,跨秒时三份文书会显示成三个时间点,
        而它们本来是同一次签发。
    """
    shot = snap if snap is not None else make_snapshot()
    return f"GYT-{kind.value}-{shot.stamp:{_TIME_FORMAT}}-{secrets.token_hex(_TAIL_HEX_BYTES)}"


def generate_unique(
    kind: DocKind,
    exists: Callable[[str], bool],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    snap: TimeSnapshot | None = None,
) -> str:
    """摇一个**库里还没有**的文书编号。撞库重试收在这里,调用方别自己写循环。

    参数:
        exists:   拿编号问库「这个号占了没」。占了返回 True。
        attempts: 最多摇几次,摇满还撞就抛 ``DocNoExhaustedError``。
        snap:     时间快照;不传就现取一个,并在整轮重试里复用(见下)。

    契约里有三件容易写错的事,写在这儿省得每个调用方各想一遍:

      1. **整轮重试共用一个快照。** 变的只有随机尾 —— 这正是 ``receipt.py``
         的原话「重试就是再调一次本函数,每次随机尾都是新的」。若每次重新
         取 now,编号里的时刻会随重试往后爬,而那个时刻是要写进文书正文的。
      2. **``exists`` 抛异常一律原样冒泡,不当成「撞了」。** 库连不上是库连
         不上,把它吞成一次撞库会白白烧掉重试次数,最后报一个「编号都被占了」
         —— 排查的人会去翻编号表,而真正的毛病在连接上。
      3. **返回的号还没入库。** 「查得到不存在」和「写进去」之间是有窗口的,
         真正的兜底是库里那条 ``UNIQUE`` 约束(方案 §4.1 的
         ``doc_no TEXT NOT NULL UNIQUE``)。这个助手只是把绝大多数碰撞挡在
         写库之前,**不是**替代唯一索引。
    """
    if attempts < 1:
        raise ValueError("attempts 至少是 1 —— 传 0 等于一个号都不摇,调用方永远拿不到结果")

    shot = snap if snap is not None else make_snapshot()
    for _ in range(attempts):
        candidate = new_doc_no(kind, shot)
        if not exists(candidate):
            return candidate
        # 撞库本身是设计内的正常事件,但连撞多次几乎一定是 exists 写错了,
        # 所以每次都留一条 warning,方便事后从日志看出「撞了几次」。
        logger.warning("文书编号 %s 已被占用,换一个随机尾重摇", candidate)

    raise DocNoExhaustedError(
        f"连着摇了 {attempts} 次{DOC_TITLE_ZH[kind]}编号都已经被占用,这次没能出号,请稍后再试一次。"
    )


def new_report_no(snap: TimeSnapshot | None = None) -> str:
    """巡检记录编号:``GYT-YYYYMMDD-HHMMSS``。**它是这里唯一没有类型段的一档。**

    为什么留着这个异形(别顺手给它加前缀或随机尾):

      · ``agents/report/__init__.py`` 的 ``REPORT_RECEIPT_PATTERN =
        r"GYT-\\d{8}-\\d{6}"`` 是「编号溯源」守卫认编号用的 —— 要求 ``GYT-``
        后面**紧跟 8 位数字**。加个类型段就再也匹配不上,守卫从此一个编号
        都认不出、**静默全放行**,而 test_report.py 只断
        ``report_no.startswith("GYT-")``,这种改法测试照绿(CLAUDE.md 同源
        清单里点名的那条陷阱)。
      · ``agents/report/tools.py`` 的原注释:时间戳号「人念得出来、电话里
        报得清」。巡检记录是工友当场要报给工长的号,4 位十六进制尾巴念起来
        比它本身还长。

    代价是**同一秒内出两份巡检记录会拿到同一个号** —— 这是沿用现状,不是本次
    的设计选择:巡检记录唯一定位靠的是随返回值一起给出的 32 位产物编号,
    ``report_no`` 只是给人念的。六种监理文书不同,它们要进 ``hazard_docs``
    的 ``UNIQUE`` 列,所以一律走 ``new_doc_no`` / ``generate_unique``。

    ⚠️ 改这里的格式 = 改 ``REPORT_RECEIPT_PATTERN``,两处一起改,少一处守卫就废。
    """
    shot = snap if snap is not None else make_snapshot()
    return f"GYT-{shot.stamp:{_TIME_FORMAT}}"


__all__ = [
    "AUTHORITY_REPORT_NO_PATTERN",
    "DEFAULT_ATTEMPTS",
    "DOC_TITLE_ZH",
    "HAZARD_NO_PATTERN",
    "NOTICE_NO_PATTERN",
    "OWNER_REPORT_NO_PATTERN",
    "PATTERNS",
    "RESUMPTION_NO_PATTERN",
    "SUSPENSION_NO_PATTERN",
    "DocKind",
    "DocNoExhaustedError",
    "generate_unique",
    "new_doc_no",
    "new_report_no",
]
