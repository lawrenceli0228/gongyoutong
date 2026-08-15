"""文书正文(``agents/supervision/documents.py``)的单元测试 —— 纯函数级,不起 HTTP、不碰库。

与另外两份的分工,别互相重复造:

    test_supervision_docgen.py  **骨架**:标题→元信息表→各节→签字栏→免责句的顺序、
                                表格列数对不上要炸、``disclaimer`` 必填、**不落盘**。
    test_supervision_api.py     **整条链**:七个端点、三条硬拦、三份文书原子产出、
                                落盘顺序、编号撞库重试。它也真渲染 docx,但只从
                                《工程暂停令》那一份上抽查了「编号印上了 + 免责句没串」。
    本文件                       **每种文书写了什么字**:五种各自的正文里那些
                                「不报错但内容错」的分支 —— 端到端那一层抽查不到,
                                因为它们要么要一个特定形状的证据链,要么根本不产生异常。

这一层钉的是这几类静默错误:

  · **免责句/签字栏串档或漏盖** —— 五种全测。只测暂停令那一份的话,新加的文书
    走岔了没人知道;而一份没写「未签字不算数」的停工文书看起来最正常。
  · **元信息表少印文书编号** —— 编号是这份纸的对外身份(电话里报它、排证据链按它),
    印不上去不会有任何异常,只是这份文书从此对不回台账。
  · **复工令引用不到原《工程暂停令》** —— ``_find_doc_no`` 找不到时给的是
    「(未找到)」这句话。真留空的话,复工令上会出现「原《工程暂停令》编号 。」
    这种断句,而它是要拿去存档的。
  · **空证据链画成一张空表** —— 空表在留档文件里读起来像「这一栏还没填」,
    而「暂无」是个明确结论;追责场合这两者意思完全不同。
  · **``doc_type`` 反查漏档** —— 证据链表里冒出一个英文 ``owner_report``。

⚠️ 这些正文是**留档材料**。改这里的期望值之前先想清楚已经发出去的那些纸怎么办 ——
2026-08-16 从 supervision_api.py 切出来那次是纯搬家,判据就是「渲染出来的 docx
逐字节相同」,不是「测试绿」。
"""

from __future__ import annotations

import io

import pytest
from docx import Document

from gyt.agents.supervision.docgen import SUPERVISION_DISCLAIMER, SUPERVISION_SIGNATURE_LINES
from gyt.agents.supervision.documents import (
    ISSUING_KINDS,
    UNASSIGNED_PROJECT_ZH,
    DocContext,
    render_doc,
)
from gyt.core.doc_no import DOC_TITLE_ZH, DocKind
from gyt.db import hazards

_KIND_IDS = [kind.name for kind in ISSUING_KINDS]
"""parametrize 的用例名。用 ``DocKind`` 的成员名,报错时一眼看出是哪种文书。"""

_HAZARD_NO = "GYT-H-20260816-093000-ab12"
_PHOTO_ID = "f" * 32
_SUSPENSION_NO = "GYT-ZT-20260816-093000-2222"

_带证据链的三种 = (DocKind.OWNER_REPORT, DocKind.RESUMPTION, DocKind.AUTHORITY_REPORT)
"""正文里带「处置经过」表的三种。另外两种(通知单、暂停令)是签发链的**第一步**,
那会儿名下本来就还没有任何文书 —— 给它们摆一张必然为空的表纯属噪音。"""


# --- 造素材(全是 NamedTuple,构造它们一次库都不用打)-------------------------


def _row(**overrides: object) -> hazards.HazardRow:
    """一条已定级、已停工的隐患。字段全给齐,免得哪天加列了这里静默走默认值。"""
    base: dict[str, object] = dict(
        id=7,
        hazard_no=_HAZARD_NO,
        project_id="gyt-a3",
        photo_sha256="0" * 64,
        photo_id=_PHOTO_ID,
        item="临边无防护",
        severity="重大",
        grade=hazards.GRADE_SEVERE,
        grading_version="1",
        needs_grading=0,
        was_suspended=1,
        status=hazards.STATUS_SUSPENDED,
        due_date="2026-08-20",
        found_at="2026-08-16 08:00:00",
        confirmed_at="2026-08-16 08:30:00",
        closed_at=None,
        created_at="2026-08-16 08:00:00",
        updated_at="2026-08-16 09:30:00",
    )
    return hazards.HazardRow(**{**base, **overrides})  # type: ignore[arg-type]


def _doc(doc_type: str, doc_no: str, **overrides: object) -> hazards.HazardDocRow:
    base: dict[str, object] = dict(
        id=1,
        hazard_no=_HAZARD_NO,
        doc_type=doc_type,
        doc_no=doc_no,
        artifact_id="a" * 32,
        photo_id=None,
        result=None,
        created_at="2026-08-16 09:30:00",
    )
    return hazards.HazardDocRow(**{**base, **overrides})  # type: ignore[arg-type]


def _ctx(**overrides: object) -> DocContext:
    base: dict[str, object] = dict(
        row=_row(),
        project_name="幸福里三期",
        signed_display="2026-08-16 09:30:00",
        due_display="8月20日(周四)",
        evidence=(
            _doc("notice", "GYT-TZ-20260816-093000-1111"),
            _doc("suspension", _SUSPENSION_NO, id=2),
        ),
    )
    return DocContext(**{**base, **overrides})  # type: ignore[arg-type]


# --- 读回工具(不落盘那条钉在 test_supervision_docgen.py,这里只管内容)---------


def _paragraphs(payload: bytes) -> list[str]:
    return [p.text for p in Document(io.BytesIO(payload)).paragraphs]


def _tables(payload: bytes) -> list[list[list[str]]]:
    return [
        [[cell.text for cell in row.cells] for row in table.rows]
        for table in Document(io.BytesIO(payload)).tables
    ]


def _meta(payload: bytes) -> dict[str, str]:
    """元信息表(第一张表)读成字典。键唯一是这张表的设计前提。"""
    return {key: value for key, value in _tables(payload)[0]}


def _render(kind: DocKind, ctx: DocContext | None = None) -> bytes:
    return render_doc(kind, f"GYT-{kind.value}-20260816-093000-9999", ctx or _ctx())


# ---------------------------------------------------------------------------
# 两张受控词表:漏一档不会有运行期报错,只会静默走岔
# ---------------------------------------------------------------------------


def test_五种文书每种都有正文构造函数() -> None:
    """模块导入时就会硬失败,这条用例是把那个约束写成人看得见的形式。

    漏一档的真实表现是签发时 KeyError → 兜底 500,工友只看到「系统开小差」。
    """
    from gyt.agents.supervision.documents import _SECTION_BUILDERS

    assert set(_SECTION_BUILDERS) == set(ISSUING_KINDS)


def test_五种文书的doc_type都落在hazards的受控词表里() -> None:
    """不然写库时撞 CHECK,而那要到真签发那一刻才炸。"""
    assert {k.name.lower() for k in ISSUING_KINDS} <= set(hazards.DOC_TYPES)


def test_隐患编号不是文书所以不进签发清单() -> None:
    """``DocKind.HAZARD`` 是隐患自己的身份号(doc_no.py 头注点名的两个孤儿之一),
    混进来就会去找一个并不存在的正文构造函数。"""
    assert DocKind.HAZARD not in ISSUING_KINDS


# ---------------------------------------------------------------------------
# 五种文书都要过的三关
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ISSUING_KINDS, ids=_KIND_IDS)
def test_每种文书都印着自己的编号和隐患编号(kind: DocKind) -> None:
    """编号是这份纸的对外身份 —— 电话里报它、上报主管部门时按它排证据链。
    印不上去不会有任何异常,只是这份文书从此对不回台账。
    (也正因为印在正文里,撞号重试才必须连正文一起重渲染。)
    """
    meta = _meta(_render(kind))

    assert meta[f"{DOC_TITLE_ZH[kind]}编号"] == f"GYT-{kind.value}-20260816-093000-9999"
    assert meta["隐患编号"] == _HAZARD_NO
    assert meta["现场照片编号"] == _PHOTO_ID
    assert meta["隐患级别"] == "严重(现场判定:重大)"


@pytest.mark.parametrize("kind", ISSUING_KINDS, ids=_KIND_IDS)
def test_每种文书都盖监理那句免责且签字栏留空(kind: DocKind) -> None:
    """**五种全测**,不是只抽查一份。

    巡检记录那句讲的是「AI 初筛 vs 持证安全员」,压根没有「总监签字后生效」这一环;
    串了的表现是一份看起来很正式、却没写明"未签字不算数"的停工文书。
    签字栏冒号后面一律留空:系统替人填名字就是伪造(D15)。
    """
    texts = _paragraphs(_render(kind))

    assert SUPERVISION_DISCLAIMER in texts
    for line in SUPERVISION_SIGNATURE_LINES:
        assert line in texts


@pytest.mark.parametrize("kind", ISSUING_KINDS, ids=_KIND_IDS)
def test_每种文书的正文里都说清了是哪条隐患(kind: DocKind) -> None:
    """元信息表里有一格「隐患事项」,但正文自己也得说 —— 只在表格里出现的话,
    这份文书读起来就是一堆不知所云的要求。"""
    正文 = "\n".join(_paragraphs(_render(kind)))

    assert "临边无防护" in 正文


@pytest.mark.parametrize("kind", ISSUING_KINDS, ids=_KIND_IDS)
def test_只有监理报告的标题多一个工程监理前缀(kind: DocKind) -> None:
    """《监理报告》是报给建设主管部门的,标题得自报家门;别的四份是发给工地上的,
    标题就是它本来的名字。写反了不会报错,只是发出去的公文名字不对。"""
    标题 = _paragraphs(_render(kind))[0]
    期望 = DOC_TITLE_ZH[kind]
    if kind is DocKind.AUTHORITY_REPORT:
        期望 = f"工程监理{期望}"

    assert 标题 == 期望


# ---------------------------------------------------------------------------
# 整改期限:有就印,没有就整行不出现(不是印一个空格)
# ---------------------------------------------------------------------------


def test_有期限时元信息表印出期限那一行() -> None:
    assert _meta(_render(DocKind.NOTICE))["整改期限"] == "8月20日(周四)"


def test_没有期限时整行不出现而不是留一格空白() -> None:
    """复工令与监理报告不设新期限(``supervision_api`` 那两处传 ``due_display=""``)。
    留一格空的「整改期限:」会被读成「期限还没填」,而它本来就不该有这一行。
    """
    meta = _meta(_render(DocKind.RESUMPTION, _ctx(due_display="")))

    assert "整改期限" not in meta
    assert meta["签发时间"] == "2026-08-16 09:30:00"  # 别的行没被一起弄丢


# ---------------------------------------------------------------------------
# 证据链表:三种带表的文书靠它举证
# ---------------------------------------------------------------------------


def test_复工令正文引用原暂停令的编号() -> None:
    """复工是「原来那份暂停令作废」这件事的对应动作 —— 不写清作废的是哪一份,
    这纸复工令在证据链上就挂不住。
    """
    正文 = "\n".join(_paragraphs(_render(DocKind.RESUMPTION)))

    assert _SUSPENSION_NO in 正文


def test_证据链里没有暂停令时写未找到而不是留一段空白() -> None:
    """留空的话正文会变成「原《工程暂停令》编号 。」这种断句 —— 而它要存档。"""
    正文 = "\n".join(_paragraphs(_render(DocKind.RESUMPTION, _ctx(evidence=()))))

    assert "原《工程暂停令》编号 (未找到)。" in 正文


@pytest.mark.parametrize("kind", _带证据链的三种, ids=[k.name for k in _带证据链的三种])
def test_空证据链出一句话而不是一张空表(kind: DocKind) -> None:
    """空表在留档文件里读起来像「这一栏还没填」,而「暂无」是个明确结论 ——
    追责场合这两者意思完全不同。
    """
    payload = _render(kind, _ctx(evidence=()))

    assert len(_tables(payload)) == 1  # 只剩元信息表
    assert "本条隐患名下暂无已出具的文书或复查记录。" in _paragraphs(payload)


def test_证据链按顺序列出且复查记录叫复查记录() -> None:
    """``hazard_docs.doc_type`` 是英文,印在纸上必须换成中文。

    ``reinspect`` 是唯一一个**不是文书**的档(``DocKind`` 里没有它,见 doc_no.py
    头注那两个孤儿),名字只能单独给 —— 漏了就是留档文书上印着一个 ``reinspect``。
    """
    ctx = _ctx(
        evidence=(
            _doc("notice", "GYT-TZ-20260816-093000-1111"),
            _doc("suspension", _SUSPENSION_NO, id=2),
            _doc(
                "reinspect",
                f"{_HAZARD_NO}#FC-deadbeef",
                id=3,
                artifact_id=None,
                photo_id="c" * 32,
                result="fail",
                created_at="2026-08-16 10:00:00",
            ),
        )
    )

    表 = _tables(_render(DocKind.AUTHORITY_REPORT, ctx))[1]

    assert 表[0] == ["序号", "类型", "编号", "结论", "时间"]
    assert [行[:2] for 行 in 表[1:]] == [
        ["1", "监理通知单"],
        ["2", "工程暂停令"],
        ["3", "复查记录"],
    ]
    # 没有结论的行画一个破折号,不留空格 —— 空单元格读起来像漏填
    assert [行[3] for 行 in 表[1:]] == ["—", "—", "fail"]


def test_词表外的文书类型原样透出不猜() -> None:
    """将来加了一档文书却忘了同步词表:印出原字符串(看得见、查得到),
    比猜一个中文名或者当场炸掉都好 —— 这份文书本身还是要出的。
    """
    ctx = _ctx(evidence=(_doc("专项报告", "GYT-XX-20260816-093000-3333"),))

    表 = _tables(_render(DocKind.OWNER_REPORT, ctx))[1]

    assert 表[1][1] == "专项报告"


# ---------------------------------------------------------------------------
# 未归属项目(D6:空串,不是 NULL)
# ---------------------------------------------------------------------------


def test_未归属项目在纸上有明确写法而不是一格空白() -> None:
    """``supervision_api._context()`` 查不到项目时填的就是它。留白会让人以为这一格
    漏填了,写明白它就是一条待归属的隐患。"""
    assert UNASSIGNED_PROJECT_ZH.strip()

    meta = _meta(_render(DocKind.NOTICE, _ctx(project_name=UNASSIGNED_PROJECT_ZH)))

    assert meta["工程项目"] == UNASSIGNED_PROJECT_ZH
