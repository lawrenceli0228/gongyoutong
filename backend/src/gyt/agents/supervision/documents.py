"""五种监理文书的**正文内容** —— 每种文书上印着什么字,全仓只有这一处。

===========================================================================
与 ``docgen.py`` 的分工:一个管长相,一个管内容
---------------------------------------------------------------------------
    docgen.py     **骨架长什么样** —— 标题、元信息表、节、签字栏、免责句怎么
                  排,表格用什么样式,免责句小几号字。六种监理文书 + 巡检记录
                  **共用同一套**,所以将来加一行页眉只改那一处。
    documents.py  **每种文书写什么** —— 《监理通知单》三节各说什么话、
                  《工程复工令》要引用哪一份原暂停令、证据链表摆哪几列。
                  五种文书**各写各的**,因为它们的差别本来就全在内容上。

一句话:``docgen`` 决定「这份纸长什么样」,本模块决定「这份纸上印着什么字」。
两件事分开的理由与 docgen 头注的 D8 是同一条,只是切在了另一个方向上:
版式合并是因为六份长得一样,正文分开是因为六份说的话本来就不一样。

===========================================================================
本模块的边界:只依赖 ``docgen`` 和一个 ``DocContext``
---------------------------------------------------------------------------
鉴权、状态机、期限解析、编号重试、原子性顺序 —— 一概不在这儿,全在
``gyt/supervision_api.py``。素材由那边查好了塞进 ``DocContext``
(``supervision_api._context()`` 干这活:项目名一次查询、证据链一次查询),
**本模块一次库都不打** —— 它 import ``gyt.db.hazards`` 只为两件事:
给 ``HazardRow`` / ``HazardDocRow`` 做类型标注,以及读 ``DOC_TYPES`` 这张
受控词表做导入时校验。

🔴 **纯函数:返回 bytes,不落盘、不查库、不改入参。** 理由与 ``docgen`` 头注
同一条(方案 §6.4):渲染一旦自己落盘,「① 渲染 → ② 落盘 → ③ 写库」的第一步
就有了半成品 —— 第二份渲染炸掉时第一份已经躺在磁盘上,而它既没进注册表也没
进库,连清理器都不认识它。

===========================================================================
为什么从 ``supervision_api.py`` 切出来(2026-08-16)
---------------------------------------------------------------------------
S4 把七个端点写在一个文件里,1357 行,超了 CLAUDE.md 的 800 行上限,而胖的
那两百行正是这里的内容 —— 且**大半是中文正文本身,不是逻辑**。那个文件的头注
当时就点了名:这一段是天然的接缝(只依赖 docgen 与一个上下文,与鉴权、状态机、
原子性一点关系都没有),不切只因为 ``agents/supervision/`` 那会儿有并行泳道在动。
泳道让开了,就切在这儿。

⚠️ **搬家那一次是纯搬家,一个字节都没改。** 验收判据不是「测试绿」,是**同一份输入
渲染出来的 docx 逐字节相同** —— 文书正文改一个标点,新出的留档材料就跟之前
签发的那些对不上,而那不会有任何报错,要到有人把两份纸并排看时才发现。
将来改这里的正文也是同一条:改之前先想清楚已经发出去的那些怎么办。

===========================================================================
2026-08-16 第二次改动:正文**确实改了字**(F4 / F5)
---------------------------------------------------------------------------
所以上面那条"逐字节相同"的判据从这一次起不再适用于这两处,登记在案:

  **F5 —— 《致建设单位报告》正文与自己的附件互相打脸。**
  正文写死「项目监理机构已就此签发《监理通知单》与《工程暂停令》」,而同一份纸
  附的证据链一份都列不出来:``supervision_api`` 的 ``ctx = _context(...)`` 在
  ``_sign(...)`` **之前**执行,本批三份那会儿还没入库(顺序是 §6.4 冻结的,不能动)。
  改法**没有动顺序**,动的是措辞与标题:证据链标题写明截止到「本文书签发前」,
  正文把这三份说成「同批出具」而不是「此前已签发」。两句话从此互补而不是打架。
  正文里任何"此前已签发 X"的说法一律由 ``_prior_issued_titles(ctx)`` 从证据链派生,
  不许手写文书名 —— F5 就是手写栽的。

  **F4 —— 复查照片写进了库,却没有任何地方读得出来。**
  ``hazard_docs.photo_id`` 存着每次复查那张照片,而全仓只有 ``documents.py`` 用
  ``ctx.row.photo_id``(那是**首次发现**那张)。于是「复查必须挂一张照片」这条红线
  退化成一道提交时的门槛,追责时反倒取不出证据。改法:证据链表加一栏
  ``REINSPECT_PHOTO_LABEL``,逐行印它自己的复查照片编号;元信息表那一格同时改叫
  ``FOUND_PHOTO_LABEL``,让"发现时"和"第 N 次复查时"在纸上分得开。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, NamedTuple

from gyt.agents.supervision.docgen import (
    SUPERVISION_DISCLAIMER,
    SUPERVISION_SIGNATURE_LINES,
    Section,
    TableSection,
    TextSection,
    render_document,
)
from gyt.core.doc_no import DOC_TITLE_ZH, DocKind
from gyt.db import hazards

# ---------------------------------------------------------------------------
# 受控词表与常量(禁止在函数体里散落字面量)
# ---------------------------------------------------------------------------

ISSUING_KINDS: Final[tuple[DocKind, ...]] = (
    DocKind.NOTICE,
    DocKind.SUSPENSION,
    DocKind.RESUMPTION,
    DocKind.OWNER_REPORT,
    DocKind.AUTHORITY_REPORT,
)
"""会被签发出来的五种文书。``DocKind.HAZARD`` 不在里面 —— 那是隐患自己的身份号,不是文书。

``core/doc_no.py`` 头注写明:这五档 ``kind.name.lower()`` 恰好落在
``hazards.DOC_TYPES`` 里,**但别写一个"六档一一对应"的循环** —— 两头各有一个孤儿
(``HAZARD`` 不进 doc_type,``reinspect`` 没有类型段)。下面那条导入时校验只查这五档。

⚠️ 这张表管着两件事,少一档两处一起哑:``_SECTION_BUILDERS`` 的完整性校验
(漏了就是签发时 KeyError)、``_doc_type_zh`` 的中文名反查(漏了就是证据链表里
冒出一个英文 doc_type)。真签发那一步在 ``supervision_api._issue_documents``,
它写进 ``hazard_docs.doc_type`` 的正是 ``kind.name.lower()``。
"""

_UNKNOWN_DOC_TYPES: Final[tuple[str, ...]] = tuple(
    k.name.lower() for k in ISSUING_KINDS if k.name.lower() not in hazards.DOC_TYPES
)
if _UNKNOWN_DOC_TYPES:  # pragma: no cover —— 两张词表漂了才触发
    raise RuntimeError(
        f"文书类型 {_UNKNOWN_DOC_TYPES} 不在 hazards.DOC_TYPES 里 —— "
        "写库时会撞 CHECK,而那要到真签发那一刻才炸;这里在导入时就拦下"
    )

UNASSIGNED_PROJECT_ZH: Final[str] = "(未归属项目)"
"""``project_id`` 为空串时文书上的写法(D6:未归属是空串,不是 NULL)。
留白会让人以为这一格漏填了,写明白它就是一条待归属的隐患。

放在这儿而不是放在查项目名的那一侧:它是**印在纸上的字**,跟正文其它措辞同一档。
"""

OWNER_REPORT_BATCH: Final[tuple[DocKind, ...]] = (
    DocKind.NOTICE,
    DocKind.SUSPENSION,
    DocKind.OWNER_REPORT,
)
"""《致建设单位报告》是跟哪几份文书**同一次动作出具**的。

⚠️ **这是一份镜像,唯一真相在 ``supervision_api._work_suspend`` 传给 ``_sign`` 的
那个 ``kinds`` 元组**(顺序同时也是前端下载卡的顺序)。为什么这儿非得再写一遍:
``render_doc(kind, doc_no, ctx)`` 每次只拿到**自己那一份**的编号,同批另外两份的
编号它根本看不见 —— 而《致建设单位报告》的正文必须点名说清「同时还出了通知单
和暂停令」,否则建设单位不知道工地已经被要求停工。

两边漂了的表现:报告正文说同批出了三份、实际只出了两份(或名字不对)。不会有
任何报错。真要收敛成一份,得让 ``_issue_documents`` 把本批清单塞进 ``DocContext``
(那是另一条泳道的改动,见本次 F5 的说明)。
"""

_BATCH_TITLES: Final[str] = "".join(f"《{DOC_TITLE_ZH[k]}》" for k in OWNER_REPORT_BATCH)
"""同批三份文书的书名号串。相邻书名号之间**不加顿号**(GB/T 15834),别顺手补上。"""

_REINSPECT_DOC_TYPE: Final[str] = "reinspect"
"""``hazard_docs`` 里那条**不是文书**的行:复查留痕。没有 ``artifact_id``,
但**有它自己的 ``photo_id``** —— 那张复查照片是这条记录在追责场合唯一的现场凭据。"""

FOUND_PHOTO_LABEL: Final[str] = "发现时现场照片编号"
REINSPECT_PHOTO_LABEL: Final[str] = "复查照片编号"
"""两个照片编号在纸上的叫法。🔴 **一个字都不许写成一样。**

它们指的是两张完全不同的照片:
  · ``hazards.photo_id``     —— 隐患**首次发现**时那张(一条隐患一张,印在元信息表里);
  · ``hazard_docs.photo_id`` —— **每次复查各一张**(挂在证据链的 reinspect 行上)。

混成同一个词的后果不是排版难看,是**拿发现时的照片当"整改后"的证据** ——
而复查必须挂照片这条红线(方案 §5.2)的全部意义就是事后追责时分得清这两者。
"""

_NO_VALUE: Final[str] = "—"
"""表格里"这一格本来就没有内容"的写法。空单元格在留档文件里读起来像漏填。"""

_MISSING_REINSPECT_PHOTO: Final[str] = "(缺复查照片)"
"""复查记录没挂照片时的写法。**不许写成 ``—`` 混进去**:文书行没有照片是正常的,
复查记录没有照片是这条证据链断了 —— 两者在追责场合意思完全不同,纸上要看得出来。"""


# ---------------------------------------------------------------------------
# 渲染素材 —— 调用方查好了塞进来,本模块只读
# ---------------------------------------------------------------------------


class DocContext(NamedTuple):
    """渲染一次动作的全部素材。一次动作一份,三份文书共用它(时刻、期限必须一致)。"""

    row: hazards.HazardRow
    project_name: str
    signed_display: str  # 「2026-08-16 09:30:00」—— 签发时刻,与编号里的时刻同一快照
    due_display: str  # 「8月20日(周四)」;复工令/监理报告没有新期限,为空串
    evidence: tuple[hazards.HazardDocRow, ...]  # 已挂在这条隐患上的文书与复查记录


# ---------------------------------------------------------------------------
# 共用零件:元信息表、证据链表、两张反查
# ---------------------------------------------------------------------------


def _hazard_meta(kind: DocKind, doc_no: str, ctx: DocContext) -> tuple[tuple[str, str], ...]:
    """五种文书共用的元信息表。

    ⚠️ 「文书编号」必须印在文书上:它是这份纸的对外身份,监理在电话里报它、
    上报主管部门时按它排证据链。也正因为印在正文里,撞号重试必须连正文一起重渲染
    (见 ``supervision_api._SIGN_RETRY_MAX``)。
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
    # 🔴 印的是**首次发现**那张照片的编号,叫法必须与证据链里那一栏区分得开
    #    (理由见 FOUND_PHOTO_LABEL / REINSPECT_PHOTO_LABEL 的说明)。
    meta.append((FOUND_PHOTO_LABEL, ctx.row.photo_id))
    return tuple(meta)


EVIDENCE_HEADING: Final[str] = "处置经过(本文书签发前已出具的文书与复查记录)"
"""证据链那一节的小标题。**括号里那句「本文书签发前」是 F5 的修法之一,别当成啰嗦删掉。**

``supervision_api._context()`` 是在 ``_sign()` **之前**建的(方案 §6.4 冻结的顺序:
文件先全部落盘、库后写,为的是不留孤儿记录),所以证据链天然截止到**本次签发之前** ——
本批正在出的这几份文书,它一份都列不出来。标题不写清截止点的话,同一份纸上就会出现
「正文说已签发通知单与暂停令 / 附表说名下什么都没有」这种自相矛盾,而那是要拿去
追责的法律文书。写清之后,「此前无记录」与「本批同时出具三份」不再打架,是互补的两句。
"""

_EVIDENCE_EMPTY: Final[str] = "截至本文书签发前,本条隐患名下没有已出具的文书或复查记录。"
"""证据链为空时那句话(不是画一张空表 —— 空表读起来像「这一栏还没填」,
而「此前没有」是一个明确结论;追责场合两者意思完全不同)。"""

_EVIDENCE_EMPTY_OWNER_REPORT: Final[str] = (
    f"截至本报告签发前,本条隐患名下没有已出具的文书或复查记录;"
    f"本次与本报告同批出具的{_BATCH_TITLES}见「一、报告事项」。"
)
"""《致建设单位报告》专用的那句。

它的证据链**在生产上必然为空**:这份报告只从 ``open`` 那条路出(``_work_suspend``),
而 ``open`` 的隐患名下一份文书都还没有。用通用那句的话,建设单位读到的是
「名下什么都没有」,而正文刚说完同批出了三份 —— 得当场把这两句接上,别让人自己去推。
"""


def _evidence_section(ctx: DocContext, *, empty_note: str = _EVIDENCE_EMPTY) -> TableSection:
    """证据链表:这条隐患名下已有的文书与复查记录,按发生先后(db 已排好序)。

    《监理报告》靠它举证「我通知过 + 期限到了 + 复查过 + 他没改」。空的时候出一句话
    而不是一张空表 —— 空表在留档文件里读起来像「这一栏还没填」。

    ``empty_note`` 留了个口子只为《致建设单位报告》一家(理由见
    ``_EVIDENCE_EMPTY_OWNER_REPORT``);**表头与行的形状三种文书共用**,别在这儿再开分叉。

    「{REINSPECT_PHOTO_LABEL}」那一栏是 F4 的修法:复查照片一直存在
    ``hazard_docs.photo_id`` 里,却**没有任何地方读得出来** —— 于是「复查必须挂照片」
    退化成一道提交时的门槛,而它本来的用处是事后追责时把结论和现场影像对上。
    """
    return TableSection(
        heading=EVIDENCE_HEADING,
        header=("序号", "类型", "编号", "结论", REINSPECT_PHOTO_LABEL, "时间"),
        rows=tuple(
            (
                str(index),
                _doc_type_zh(doc.doc_type),
                doc.doc_no,
                doc.result or _NO_VALUE,
                _evidence_photo(doc),
                doc.created_at,
            )
            for index, doc in enumerate(ctx.evidence, start=1)
        ),
        empty_note=empty_note,
    )


def _evidence_photo(doc: hazards.HazardDocRow) -> str:
    """证据链某一行的照片编号那一格。

    三种写法各有各的意思,别合并:
      · 有编号        —— 原样印出来,拿它就能调回那张原图(前端按 artifact_id 取件);
      · 文书行没有    —— ``—``,文书本来就不挂照片,正常;
      · 复查行没有    —— ``(缺复查照片)``,这是证据链断了一环,必须在纸上看得见。

    先看有没有编号再看是不是复查行:哪天文书行真挂上了照片(比如将来把送达回执
    附进去),这里也不会把它藏掉 —— 留档材料宁可多印一格,不能悄悄少印一格。
    """
    if doc.photo_id:
        return doc.photo_id
    return _MISSING_REINSPECT_PHOTO if doc.doc_type == _REINSPECT_DOC_TYPE else _NO_VALUE


def _prior_issued_titles(ctx: DocContext) -> tuple[str, ...]:
    """证据链里**确实已经出具过**的文书中文名,按出现先后去重(复查记录不是文书,不算)。

    🔴 **正文里任何「已签发 / 已出具 X」的说法都必须从这里取,不许手写文书名。**
    F5 就是手写栽的:《致建设单位报告》正文写死了「已就此签发《监理通知单》与
    《工程暂停令》」,而它自己附的证据链一份都列不出来 —— 一份正文与附件互相打脸的
    法律文书。从证据链派生之后,「正文说签了什么」与「附件列了什么」**结构上**同源。

    同批正在出的那几份文书不在这里(证据链截止到本次签发前,见 ``EVIDENCE_HEADING``),
    正文要提它们得用「同批出具」这种说法,不能说成「已签发」。
    """
    titles: list[str] = []
    for doc in ctx.evidence:
        if doc.doc_type == _REINSPECT_DOC_TYPE:
            continue
        title = _doc_type_zh(doc.doc_type)
        if title not in titles:
            titles.append(title)
    return tuple(titles)


def _doc_type_zh(doc_type: str) -> str:
    """``hazard_docs.doc_type`` → 中文。文书五档取自 ``DOC_TITLE_ZH``(唯一那张表),
    复查行不是文书、``DocKind`` 里没有它,单独给一个名字。

    ⚠️ 前端有一份镜像(``scripts/frontend-overrides/supervision-lib.ts`` 的
    ``DOC_TYPE_ZH``),它的注释指着本函数(写的是旧位置 ``supervision_api._doc_type_zh``,
    这次搬家没动那个文件)。两边都是"认不出就原样透出",改中文名要一起改。
    """
    if doc_type == _REINSPECT_DOC_TYPE:
        return "复查记录"
    for kind in ISSUING_KINDS:
        if kind.name.lower() == doc_type:
            return DOC_TITLE_ZH[kind]
    return doc_type  # pragma: no cover —— 词表外的类型原样透出,不猜


def _find_doc_no(ctx: DocContext, doc_type: str) -> str:
    """从证据链里找某类文书的编号(复工令要引用原《工程暂停令》)。找不到给一句话,不留空白。"""
    for doc in ctx.evidence:
        if doc.doc_type == doc_type:
            return doc.doc_no
    return "(未找到)"


# ---------------------------------------------------------------------------
# 五种文书各自的正文 —— 这一段大半是中文本身,逻辑几乎为零
# ---------------------------------------------------------------------------


def _sections_notice(ctx: DocContext) -> tuple[Section, ...]:
    return (
        TextSection(
            heading="一、隐患情况",
            # 「发现时」三个字不许省:复查照片也叫"现场照片",省掉之后整改后拍的那张
            # 和发现时那张在纸面上就分不出来了(见 FOUND_PHOTO_LABEL)。
            body=f"经现场巡查,发现「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患。"
            f"该隐患首次发现时间为 {ctx.row.found_at},"
            f"{FOUND_PHOTO_LABEL} {ctx.row.photo_id}。",
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


def _sections_suspension(ctx: DocContext) -> tuple[Section, ...]:
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


def _sections_owner_report(ctx: DocContext) -> tuple[Section, ...]:
    """《致建设单位报告》—— F5 修在这一份上。

    原来「一、报告事项」写死了一句「项目监理机构**已就此签发**《监理通知单》与
    《工程暂停令》」,而同一份纸后面附的证据链一份都列不出来(``_context()`` 在
    ``_sign()`` 之前建,本批三份还没入库)。正文说签了、附件说没有 —— 拿去追责时,
    对方律师第一眼就看这个。

    改法是把时态说准:这三份是**同一次动作出具**的,不是"此前已签发"。
    「同批出具」与证据链的「本文书签发前无记录」不矛盾,是互补的两句话。
    签发顺序(文件先全部落盘、库后写)一个字节都没动 —— 那是 §6.4 的原子性前提。
    """
    return (
        TextSection(
            heading="一、报告事项",
            body=f"现场发现「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患。"
            f"项目监理机构就此同批出具{_BATCH_TITLES}(本报告即其中之一),"
            "现向建设单位报告。",
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
        _evidence_section(ctx, empty_note=_EVIDENCE_EMPTY_OWNER_REPORT),
    )


def _sections_resumption(ctx: DocContext) -> tuple[Section, ...]:
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


def _sections_authority_report(ctx: DocContext) -> tuple[Section, ...]:
    """《监理报告》(报工程所在地建设主管部门)—— 这份纸的全部作用就是举证
    「我通知过 + 期限到了 + 复查过 + 他没改」,所以正文说的每一句都要在下面那张表里指得着。

    两处修法都落在这一份上:
      · **F5** —— 「已按规定签发」后面点的名从 ``_prior_issued_titles(ctx)`` 派生,
        不再手写。正文说签了哪几份 = 附表列了哪几份,结构上同源。
      · **F4** —— 「三、请予处理事项」里点明复查照片编号在表的哪一栏、
        以及它与「发现时」那张不是同一张,让主管部门知道原始影像调得出来。
    """
    prior = _prior_issued_titles(ctx)
    # 有前序文书就点名(举证要具体);一份都没有时**不许硬说"已签发文书"** ——
    # 那又是一句附表兜不住的话。生产上走不到这条分支(上报只从 reinspect_failed 进,
    # 名下必有通知单/暂停令),留着是因为"正文与附件一致"这条不能靠调用方自觉。
    issued_clause = (
        "已按规定签发" + "".join(f"《{title}》" for title in prior) + "要求整改"
        if prior
        else "已就此隐患督促整改(处置经过见下表)"
    )
    return (
        TextSection(
            heading="一、事由",
            body=f"现场存在「{ctx.row.item}」,监理定级为{ctx.row.grade}隐患。"
            f"项目监理机构{issued_clause},施工单位逾期未改或复查不合格,"
            "现依据监理职责报工程所在地建设主管部门。",
        ),
        _evidence_section(ctx),
        TextSection(
            heading="三、请予处理事项",
            body=f"上表所列复查记录均以现场复查照片为凭,照片编号见表中「{REINSPECT_PHOTO_LABEL}」一栏;"
            f"隐患发现时的现场照片另有编号,见本文书首部「{FOUND_PHOTO_LABEL}」一栏。"
            "上述影像原件与全部文书由项目监理机构留存备查,可按编号调取。"
            "请建设主管部门予以核查处理,项目监理机构将继续跟踪该隐患的整改情况。",
        ),
    )


_SECTION_BUILDERS: Final[dict[DocKind, Callable[[DocContext], tuple[Section, ...]]]] = {
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
    k.name for k in ISSUING_KINDS if k not in _SECTION_BUILDERS
)
if _MISSING_BUILDERS:  # pragma: no cover
    raise RuntimeError(f"文书 {_MISSING_BUILDERS} 没有正文构造函数,_SECTION_BUILDERS 漏了")


# ---------------------------------------------------------------------------
# 出口:一份文书的完整字节
# ---------------------------------------------------------------------------


def render_doc(kind: DocKind, doc_no: str, ctx: DocContext) -> bytes:
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


__all__ = [
    "EVIDENCE_HEADING",
    "FOUND_PHOTO_LABEL",
    "ISSUING_KINDS",
    "OWNER_REPORT_BATCH",
    "REINSPECT_PHOTO_LABEL",
    "UNASSIGNED_PROJECT_ZH",
    "DocContext",
    "render_doc",
]
