"""Report Agent 的工具集 —— 把一次照片巡检渲染成可存档的巡检记录(docx)。

===========================================================================
数据保真:隐患内容**不经过任何 LLM 转抄**
---------------------------------------------------------------------------
巡检记录是要留档、可能用于追责的文档,里面的隐患项错一个字就是另一份文件。
所以本工具只收 artifact_id,内部**重新跑一遍** safety 的识别:

    render_inspection_report(artifact_id)
        │ _recognize(artifact_id)   ← safety 的「只识别、不登记」那一半;
        │                             提示词已冻结 + 缓存键算在图片内容上
        │                             → 必然命中磁盘缓存:零成本,
        │                               且与 safety 刚讲给用户的判断逐字节一致
        ▼ envelope.data(五键契约:label/violations/severity/max_severity/note)
    python-docx 渲染 → artifacts.register(REPORT) → 报告编号

英雄链里 safety 先跑(把缓存焐热),硬边到 report,report 的模型只负责把
32 位编号从上文搬进工具参数 —— 搬错一位立刻 NOT_FOUND 报错,
**不可能产出一份内容错误却看起来正常的文档**。这比让模型转抄隐患清单安全一个量级。

⚠️ **为什么复调的是 ``_recognize`` 而不是 ``analyze_site_photo``**(W9 S3 改的):
W9 起 ``analyze_site_photo`` = 识别 + **把违规项登记成 pending 隐患**,幂等键是
``(project_id, photo_sha256, item)``。而这条复调**不带 config** —— 它是工具内部的
一次直调,LangGraph 的 configurable 到不了这里,``project_from_config`` 会取到空串。
于是 safety 那次登记进「工地A」、report 这次登记进 ``''``:**同一张照片的同一个隐患,
台账里两行,一行还是无主的**,而且全程零报错。改调 ``_recognize`` 之后,
**保真性质一个字没变** —— 仍是同一个纯函数、仍命中同一份缓存、仍是那五个键 ——
变的只是不再重复登记一遍。

依赖方向说明:report → safety 的 import 是**有意的**,英雄链本身就是这条耦合
(拍照识违规 → 自动出记录)。别为了"解耦"把识别抽到 core 去 ——
它的提示词、缓存维度、词表守卫全长在 safety 包里,搬家只会制造第二真相源。

另外两条 import(W9 S2 抽公共件时加的,方案 §11 的 S2 泳道):

  · ``agents/supervision/docgen`` —— docx 骨架(标题/元信息表/各节/免责句)
    与六种监理文书共用(D8)。这里只负责「巡检记录填什么」,版式归那边。
    ⚠️ 免责句是**必填参数**,巡检记录传自己的 ``_DISCLAIMER``,不许套上监理
    文书那句「总监签字后生效」—— 两者定位不同,详见 docgen 里的说明。
  · ``attendance/receipt`` —— 全仓唯一的业务时间权威(香港时区)。以前这里
    走 ``datetime.now(UTC).astimezone()``,那是**宿主时区**:容器 TZ 是
    Asia/Shanghai、本机可能是任意时区,数值一致纯属巧合(方案 §6.3)。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from langchain_core.tools import tool

from gyt.agents.safety.tools import _recognize
from gyt.agents.supervision import docgen
from gyt.attendance.receipt import make_snapshot
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from gyt.core.doc_no import new_report_no
from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard

logger = logging.getLogger(__name__)

LABEL_ZH: Final[dict[str, str]] = {
    "violation": "发现安全隐患",
    "compliant": "未见明显隐患",
    "not_site": "非作业现场照片",
}
"""三态结论 → 文档用语。认不出的 label 原样落进文档并加「(待人工确认)」——
不吞、不猜,和 safety 工具的透传哲学一致。"""

_DISCLAIMER: Final[str] = (
    "本记录由工友通 AI 初筛生成,仅作快速筛查与留档之用;"
    "涉及精确测量与专业资质判断的事项,以持证安全员现场判定为准。"
)
"""落款免责句 —— 「初筛与记录」的定位直接写进每一份产物,这是技术说明边界①的落地。"""

_RENDER_DESCRIPTION = (
    "把一张已检查过的现场照片生成正式的巡检记录文档(Word)。"
    "参数 artifact_id 是照片的产物编号(32 位十六进制),从对话上文里原样复制。"
    "参数 title 选填:**只有用户明确说了要什么标题时才传**,原样转述他说的那几个字,"
    "不要自己拟、不要润色、不要加书名号;用户没提就别传这个参数。"
    "返回巡检记录的报告编号与隐患摘要。用户要「出巡检记录」「留档」「出报告」时调它。"
)

DEFAULT_REPORT_TITLE: Final[str] = "工地安全巡检记录"
"""没有自定义标题时用的标题,也是**文种**本身。

⚠️ 这个值同时出现在两处:文档标题(可被自定义标题替换)与元信息表的「文档类型」
(**永远不变**)。别把元信息表那处也改成可变的 —— 理由见 `_clean_title`。
"""

_TITLE_MAX_LEN: Final[int] = 40
"""自定义标题的字数上限。

40 是按 A4 标题行排得下一行定的(docgen 的标题是居中大字号,超了会折行、
把元信息表挤到第二页)。**这不是安全边界,是排版边界** —— 安全那半靠
「文种进元信息表」那条,见 `_clean_title`。
"""


def _clean_title(raw: str) -> str:
    """把用户/模型给的标题收拾成能进文档的样子;拿不到有效内容就回默认标题。

    🔴 **为什么允许自定义标题、却坚持把文种钉在元信息表里**(2026-08-24):

        这份文档是要留档、可能用于追责的。本仓对「文书标题」一贯的态度是**由类型
        决定、不由人填**:六种监理文书的标题写死在 ``core/doc_no.DOC_TITLE_ZH``,
        而 ``docgen.render_document`` 把 ``disclaimer`` 做成必填无默认,就是为了让
        「漏传参数于是悄悄套上另一句」在结构上不可能发生。

        但「给这一轮巡检起个名字」是真实需求(真实的巡检记录本来就叫
        「XX项目 安全巡检记录」),复验报告 P0-2 要的也是它。

        取法是**两者都要**:标题给人填,而「文档类型:工地安全巡检记录」进元信息表,
        **每一份文档都自我标明它是什么**。这样即使标题被写成
        「现场无隐患确认书」,打开文档第一张表里仍然写着它是一份巡检记录 ——
        一份文档不会因为标题就变成另一个文种。

    净化做四件事,每件都有具体的坏法要防:

      · 折行字符全压成空格 —— docx 的标题是单行段落,塞进 ``\\n`` 不会报错,
        只会渲染成一个看不见的怪空格,而人对着 Word 找不出哪里不对。
      · 去掉控制字符 —— 同上,而且它们会让 docx 在某些阅读器上直接打不开。
      · 连续空白压成一个 —— 「海之子   验收」这种粘贴产物看着像排版事故。
      · 截到 40 字 —— 排版边界,理由见 ``_TITLE_MAX_LEN``。

    ⚠️ **收拾完是空就回默认标题,不抛异常。** 标题是附属信息,为它让整份文档
    生不出来不划算;而「用户传了个纯空格」跟「没传」在意图上没区别。
    """
    if not raw:
        return DEFAULT_REPORT_TITLE
    # 控制字符(含 \n \r \t)一律当空白处理,再把连续空白压成一个
    flattened = "".join(" " if (ch.isspace() or ord(ch) < 32) else ch for ch in raw)
    cleaned = " ".join(flattened.split())
    if not cleaned:
        return DEFAULT_REPORT_TITLE
    return cleaned[:_TITLE_MAX_LEN]


def _render_docx(
    *,
    photo_id: str,
    report_no: str,
    generated_at: str,
    data: dict[str, Any],
    settings: Any,
    title: str = DEFAULT_REPORT_TITLE,
) -> bytes:
    """按巡检记录模板渲染 docx,返回文件字节。纯函数式:不落盘、不改入参。

    版式(标题 / 元信息表 / 各节 / 免责句)在 ``supervision/docgen.py``,与六种
    监理文书共用;这里只决定「巡检记录填什么」。W9 S2 抽骨架时逐项对齐了原来的
    渲染顺序与措辞,产出结构与 2026-08-16 之前**一致**。

    ``generated_at`` 由调用方从香港时间权威取,和 ``report_no`` 里的时刻同源
    —— 不在这里现取 now,理由见 ``render_inspection_report`` 里的注释。
    """
    label = str(data.get("label") or "")
    violations: list[str] = list(data.get("violations") or [])
    severity: dict[str, str] = dict(data.get("severity") or {})
    note = str(data.get("note") or "")

    sections: list[docgen.Section] = [
        docgen.TableSection(
            heading="隐患明细",
            header=("序号", "隐患项", "级别"),
            rows=tuple(
                (str(index), item, severity.get(item, "待定级"))
                for index, item in enumerate(violations, start=1)
            ),
            empty_note="本次巡检未发现受控清单内的隐患。",
        )
    ]
    # 备注为空就整节不出 —— 一个空的「现场备注」小标题会让人以为备注被吞了。
    if note:
        sections.append(docgen.TextSection(heading="现场备注与待复核事项", body=note))
    sections.append(
        docgen.TextSection(
            heading="生成信息",
            body=(
                f"识别模型:{settings.model_vision} / 提示词版本:{settings.prompt_version}。"
                "隐患判定与级别由系统按固定规则直出,未经人工改写。"
            ),
        )
    )

    return docgen.render_document(
        title=title,
        meta=(
            # 🔴 **「文档类型」这一行永远是 DEFAULT_REPORT_TITLE,不跟着 title 走。**
            #    它是这份文档的**文种**:标题可以由人起名(「海之子验收测试」),
            #    但打开文档第一张表里必须写着它到底是什么。理由见 _clean_title 的头注 ——
            #    一句话:一份文档不许因为标题就变成另一个文种。
            ("文档类型", DEFAULT_REPORT_TITLE),
            ("记录编号", report_no),
            ("生成时间", generated_at),
            ("照片编号", photo_id),
            ("巡检结论", LABEL_ZH.get(label, f"{label or '(空)'}(待人工确认)")),
        ),
        sections=sections,
        # 巡检记录用自己那句免责("AI 初筛 vs 持证安全员现场判定"),**不是**监理
        # 文书那句"总监签字后生效" —— 它没有签字这一环。docgen 把 disclaimer 做成
        # 必填无默认,就是为了让"漏传参数于是悄悄套上另一句"在结构上不可能发生。
        disclaimer=_DISCLAIMER,
        # 同理不传签字栏:巡检记录不需要签字生效。
    )


@tool("render_inspection_report", description=_RENDER_DESCRIPTION)
@tool_guard
async def render_inspection_report(artifact_id: str, title: str = "") -> Envelope:
    """生成一张照片的巡检记录 docx,登记为 REPORT 产物。

    title 选填(2026-08-24 加):用户明确要求标题时原样转述,空则用
    ``DEFAULT_REPORT_TITLE``。**它只改标题,不改文种** —— 元信息表里那行
    「文档类型」恒为「工地安全巡检记录」,推演见 ``_clean_title`` 的头注。
    净化(压折行 / 去控制字符 / 截 40 字)也在那儿。

    artifact_id
      │ 编号不合法/照片不存在/识别失败 ──▶ 原样透传 safety 识别的失败信封
      ▼ _recognize(缓存必中,数据与 safety 口径逐字节一致,**不重复登记隐患**)
    五键契约 data
      ▼ python-docx 渲染 + artifacts.register(REPORT)
    ok(data={report_id, filename, label, violations, severity, max_severity})
    """
    settings = get_settings()

    analysis = await _recognize((artifact_id or "").strip())
    if not isinstance(analysis, dict) or not analysis.get("ok"):
        # 失败信封原样透传:里面的 user_msg 已经是中文人话(编号不对/照片没了/模型超时),
        # 在这里重新包装一层只会把「该怎么办」的信息越包越模糊。
        if isinstance(analysis, dict):
            return analysis
        return fail(ErrorCode.INTERNAL, detail=f"analyze 返回了非信封:{type(analysis).__name__}")

    data: dict[str, Any] = dict(analysis.get("data") or {})
    cleaned = (artifact_id or "").strip()
    # 时间只取**一个快照**,记录编号里的时刻和文档里「生成时间」那一行同源。
    # 各取一次 now 的话跨秒/跨午夜时两处会对不上,而这是一份要留档、可能用于
    # 追责的文件 —— 同一份文件上写着两个时间,追责时先被质疑的是文件本身
    # (attendance/receipt.py 头注「单一快照」的原话,那边四处时间同理)。
    #
    # 快照来自香港时间权威。以前这里是 datetime.now(UTC).astimezone(),那是
    # **宿主时区**:容器 TZ=Asia/Shanghai、本机可能是任意时区,数值一致纯属
    # 巧合,而 make test 在同为 UTC+8 的本机全绿也发现不了(方案 §6.3)。
    #
    # 记录编号用「GYT-日期-时刻」而不是产物编号:产物编号要注册后才有,而正文
    # 渲染在注册之前(注册需要文件字节)。时间戳号人念得出来、电话里报得清,
    # 产物编号(32 位 hex)在返回值里一并给出,两者都能唯一定位这份文件。
    # ⚠️ 这个格式与 __init__.py 的 REPORT_RECEIPT_PATTERN 绑死,改格式两处一起改
    # —— 生成器现在住在 core/doc_no.py 的 new_report_no(),那儿有完整说明。
    snap = make_snapshot()
    report_no = new_report_no(snap)

    payload = _render_docx(
        photo_id=cleaned,
        report_no=report_no,
        generated_at=snap.display,
        data=data,
        settings=settings,
        title=_clean_title(title),
    )
    filename = f"巡检记录_{report_no}.docx"
    report_id = artifacts.register(payload, kind=ArtifactKind.REPORT, original_name=filename)
    stored = artifacts.resolve(report_id)
    logger.info("巡检记录已生成:%s(%s,%d 字节)", report_no, report_id, len(payload))

    violations = list(data.get("violations") or [])
    label = str(data.get("label") or "")
    if violations:
        summary = (
            f"巡检记录已生成(编号 {report_no}),"
            f"共 {len(violations)} 处隐患,最高级别 {data.get('max_severity')}。"
        )
    elif label == "not_site":
        summary = f"巡检记录已生成(编号 {report_no}),但照片被判定为非作业现场,请核对后重拍。"
    else:
        summary = f"巡检记录已生成(编号 {report_no}),本次未发现受控清单内的隐患。"

    return ok(
        data={
            "report_id": report_id,
            "report_no": report_no,
            "filename": filename,
            "path": str(stored),
            "label": label,
            "violations": violations,
            "severity": dict(data.get("severity") or {}),
            "max_severity": data.get("max_severity"),
        },
        user_msg=summary,
    )


REPORT_TOOLS: list = [render_inspection_report]

__all__ = ["LABEL_ZH", "REPORT_TOOLS", "render_inspection_report"]
