"""评测跑分框架 —— 三套评测的共享基座,一个人搭、全组共用。

为什么必须共用一套:两个人各写一套「读数据集 → 逐条跑 → 算分 → 比门槛 → 进 CI」,
等于重复劳动 + 两套口径。到时候「RAG 82%、Safety 79%」这两个数字根本没法横向比。

用法:

    make eval                  # 三套全跑
    make eval SUITE=safety     # 只跑一套
    uv run python -m eval.runner --suite rag --runners mypkg.evalhooks:RUNNERS

整体流程:

    datasets/<套>.csv
        │ ① load_rows   utf-8-sig 读盘 + 必需列检查(缺列直接抛中文错)
        ▼
    全部行
        │ ② 剔掉「示例待替换」占位行 —— 示例行里的数字全是编的,
        ▼    拿它算分等于自欺欺人
    可判分的行
        │ ③ 没有可判分的行 / 没注入被测函数 ──► SKIP(不是失败!)
        ▼
    逐条 await runner(row) → 该套的判分函数 → RowScore
        │ ④ 汇总:总分 = 对的条数 / 可判分条数
        ▼
    与 settings.eval_threshold_<套> 比
        │ ⑤ 低于门槛 → exit 非 0,CI 拦门
        ▼
    打印:总分 + **逐条明细**(哪条挂了、期望什么、模型实际给了什么)

被测对象为什么是「注入」进来的:
    本文件写于 W2 第一天,那时 Safety / Knowledge Agent 还不存在。
    runner 一旦 import 具体 Agent,今天就跑不起来,而且以后每加一个 Agent
    都要回来改这里。所以约定成:调用方传一个 ``{套名: async 函数}`` 的字典,
    没传的套打印 SKIP 而不是崩 —— 数据填多少跑多少,Agent 好了哪个跑哪个。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from eval.scorers import (
    DatasetError,
    RowScore,
    score_orchestration,
    score_rag,
    score_routing,
    score_safety,
    validate_orchestration_row,
    validate_rag_row,
    validate_routing_row,
    validate_safety_row,
)
from gyt.config import get_settings

# --- 模块级常量:配方里不写字面量 -------------------------------------------

DATASETS_DIR: Final[Path] = Path(__file__).resolve().parent / "datasets"

CSV_ENCODING: Final[str] = "utf-8-sig"
"""utf-8-sig:Excel 存出来的 CSV 前面带 BOM,用 utf-8 读会让第一列列名变成 '\\ufeffid'。"""

PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = ("待替换", "请替换")
"""占位行特征词。仓库里的示例行数字全是编的,必须先被剔出去再算分。"""

ALL_SUITES: Final[str] = "all"

EXIT_OK: Final[int] = 0
EXIT_BELOW_THRESHOLD: Final[int] = 1
EXIT_DATASET_ERROR: Final[int] = 2

Row = Mapping[str, str]
AgentRunner = Callable[[Row], Awaitable[Any]]
"""被测对象:收一行数据集,返回模型输出(裸字符串或结构化 dict 都行)。"""


class Status(StrEnum):
    """一套评测的最终状态。SKIPPED 不是失败,不参与红/绿判定。"""

    PASSED = "PASS"
    FAILED = "FAIL"
    SKIPPED = "SKIP"
    DIAGNOSTIC = "DIAG"
    """跑了、算了分,但**不参与红绿** —— 给命中率天然不稳的套用(见 SuiteSpec.diagnostic)。

    为什么不复用 SKIPPED:那个表示「压根没跑」(没接 runner / 数据集是空的),
    报告里读起来是「这套被跳过了」。而 DIAG 是真跑过、真有分数、只是那个分数
    不该拿来卡门。两件事在排查时的下一步完全不同,混成一档会让人去查「为什么被跳过」。
    """


class RunnerSpecError(ValueError):
    """``--runners 模块:属性`` 写错了(模块导不进来、属性不存在、类型不对)。"""


@dataclass(frozen=True, slots=True)
class SuiteSpec:
    """一套评测的静态定义。frozen:注册表在运行期不许被改。"""

    name: str
    dataset: str
    threshold_field: str
    min_rows_field: str
    required_columns: tuple[str, ...]
    validate: Callable[[Row], None]
    score: Callable[[Row, Any], RowScore]
    diagnostic: bool = False
    """这一套的命中率**不参与红绿**(仍然照跑、照算、照进报告)。

    🔴 什么时候该开:命中率本身不稳到没法画线的时候。判据不是「分数低」,
    而是**「同样的数据集、同样的代码,跑两次得两个不同的数」**。

    orchestration 就是这样(2026-08-22 四次实测:85.0% / 71.4% / 71.4% / 80.0%,
    而且每次红的行都不一样)。原因是它跑**多跳**链,每一跳的输出喂给下一跳,
    小差异逐跳放大;routing 只判第一跳、一次调用定胜负,所以那套的数是稳的。

    ⚠️ **一把刻度不稳的尺子当门槛用,比没有尺子更坏** —— 第一次假红就会让人
    开始忽略它的红,而它照出来的恰恰是最难发现的那类问题
    (第 4 跑照出 `schedule>schedule>schedule>schedule`,四次派同一个人撞熔断)。

    要摘掉这个标记,得先拿出**真方差**的数据。⚠️ 量真方差必须跑**独立进程** ——
    同一个进程里连跑 N 轮量到的是缓存回放:2026-08-22 那次三轮拿到完全相同的结果、
    极差 0.0%,而三轮耗时是 226s / 35s / 33s,后两轮几乎全命中缓存。
    """


SUITES: Final[Mapping[str, SuiteSpec]] = MappingProxyType(
    {
        "routing": SuiteSpec(
            name="routing",
            dataset="routing.csv",
            threshold_field="eval_threshold_routing",
            min_rows_field="eval_min_rows_routing",
            required_columns=("id", "type", "user_input", "expected_agent"),
            validate=validate_routing_row,
            score=score_routing,
        ),
        "safety": SuiteSpec(
            name="safety",
            dataset="safety.csv",
            threshold_field="eval_threshold_safety",
            min_rows_field="eval_min_rows_safety",
            required_columns=("id", "type", "image", "label", "violations"),
            validate=validate_safety_row,
            score=score_safety,
        ),
        "rag": SuiteSpec(
            name="rag",
            dataset="rag.csv",
            threshold_field="eval_threshold_rag",
            min_rows_field="eval_min_rows_rag",
            required_columns=(
                "id",
                "type",
                "question",
                "expected_answer_points",
                "expected_source",
                "expected_page",
            ),
            validate=validate_rag_row,
            score=score_rag,
        ),
        # orchestration(2026-08-22 加的第四套)—— 与 routing 的分工写在 eval/README.md:
        # routing 只判**第一跳派给谁**并在第一跳停流;这一套**跑完整条链**,
        # 判「整件事有没有做完、有没有多跳、该追问时有没有追问」。
        # 🔴 别把 routing.csv 的行搬进来:那 33 行绝大多数只需要验第一跳,
        #    而这一套每行贵 10-20 倍(子 Agent 的整个工具循环都要跑)。
        "orchestration": SuiteSpec(
            name="orchestration",
            dataset="orchestration.csv",
            threshold_field="eval_threshold_orchestration",
            min_rows_field="eval_min_rows_orchestration",
            required_columns=(
                "id",
                "type",
                "user_input",
                "expected_path",
                "expected_status",
                "max_handoffs",
            ),
            validate=validate_orchestration_row,
            score=score_orchestration,
            # 命中率不参与红绿,理由见 SuiteSpec.diagnostic 的红字(四次实测的数在那儿)。
            diagnostic=True,
        ),
    }
)
"""四套评测的注册表。门槛只写字段名,真值一律现从 get_settings() 取。"""


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """一套评测的结果。逐条明细留在 results 里,不做任何聚合丢弃 ——
    调提示词的人要的就是「哪条挂了、模型当时说了什么」。"""

    suite: str
    status: Status
    threshold: float
    results: tuple[RowScore, ...] = ()
    skipped_ids: tuple[str, ...] = ()
    message: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def score(self) -> float:
        """得分 = 对的条数 / 可判分条数。没有可判分的行时记 0(此时状态必然是 SKIP)。"""
        return self.passed_count / self.total if self.total else 0.0


# ---------------------------------------------------------------------------
# 一、读盘
# ---------------------------------------------------------------------------


def _clean_row(path: Path, reader: csv.DictReader[str], row: Mapping[Any, Any]) -> Row:
    """把 DictReader 读出的一行收成干净的 {列名: 字符串}。字段数对不上表头就当场炸。

    csv.DictReader 对「字段数与表头不符」是静默处理的:多出来的落在 restkey(None)下,
    少了的补 None。早先这里写的是 ``if k is not None``,等于把溢出部分悄悄丢掉 ——
    而这恰恰是最常见的 CSV 损坏(字段里有逗号忘了加双引号,README 第四节自己警告过)。
    后果:标准答案被悄悄改小/错位,validate 照样放行,模型答对了反而判 FAIL。
    """
    if any(key is None for key in row) or any(value is None for value in row.values()):
        raise DatasetError(
            f"{path.name} 第 {reader.line_num} 行的字段数与表头不符"
            "(多半是某个字段里有逗号却没加双引号,或者少写了一列)。"
            "这种行会让标准答案错位,必须先改对再跑。"
        )
    return {str(key): value for key, value in row.items()}


def load_rows(spec: SuiteSpec, datasets_dir: Path = DATASETS_DIR) -> tuple[Row, ...]:
    """读一套数据集。缺文件 / 缺必需列 / 编码不对 / 字段数错位,都抛中文 DatasetError。

    静默降级的后果:表头写错一个字母 → 那一列全读成空 → 全套判错 → 分数极低,
    然后所有人花半天去调提示词。宁可当场炸,也不要一个错误的数字。
    """
    path = datasets_dir / spec.dataset
    if not path.is_file():
        raise DatasetError(f"找不到数据集文件:{path}。请按 eval/README.md 建好再跑。")
    try:
        with path.open(encoding=CSV_ENCODING, newline="") as handle:
            reader = csv.DictReader(handle)
            header = tuple(reader.fieldnames or ())
            missing = [name for name in spec.required_columns if name not in header]
            if missing:
                raise DatasetError(
                    f"{path.name} 表头缺少这些列:{'、'.join(missing)}。"
                    f"当前表头是:{'、'.join(header) or '(空文件)'}。"
                )
            return tuple(_clean_row(path, reader, row) for row in reader)
    except UnicodeDecodeError as exc:
        # 数据集是给标注同学用 Excel 填的,中文 Windows 上 Excel 默认存 GBK/GB18030。
        # 不转成 DatasetError 的话,他们看到的是一行 "codec can't decode byte 0xb9",
        # 既不知道是哪个文件,也不知道该怎么救。
        raise DatasetError(
            f"{path.name} 不是 UTF-8 编码(Excel 存出来的多半是 GBK)。"
            "请用「另存为 → CSV UTF-8(逗号分隔)」重存一次再跑。"
        ) from exc
    except OSError as exc:
        raise DatasetError(f"读不了数据集文件 {path}:{exc}") from exc


def is_placeholder(row: Row) -> bool:
    """这一行还是仓库自带的示例占位行吗?

    判据:任意字段里出现「待替换 / 请替换」。示例行的数字全是编的,
    拿它算出来的分数是假的 —— 宁可少算几条,也不要一个好看的假分数。
    """
    return any(marker in value for value in row.values() for marker in PLACEHOLDER_MARKERS)


def split_placeholders(rows: Sequence[Row]) -> tuple[tuple[Row, ...], tuple[str, ...]]:
    """把占位行摘出去,返回 (可判分的行, 被摘掉的 id 列表)。"""
    scorable = tuple(row for row in rows if not is_placeholder(row))
    skipped = tuple(row.get("id", "?") for row in rows if is_placeholder(row))
    return scorable, skipped


# ---------------------------------------------------------------------------
# 二、跑一套
# ---------------------------------------------------------------------------


def _threshold_of(spec: SuiteSpec) -> float:
    """门槛只有一个真相:gyt.config。**禁止**在本文件里再写一遍数字。"""
    return float(getattr(get_settings(), spec.threshold_field))


def _min_rows_of(spec: SuiteSpec) -> int:
    """最小样本量,同样只从 gyt.config 取。"""
    return int(getattr(get_settings(), spec.min_rows_field))


async def _score_row(spec: SuiteSpec, row: Row, runner: AgentRunner) -> RowScore:
    """调一次被测对象并判分。被测对象炸了只算这一条挂,不连累整套。"""
    try:
        actual = await runner(row)
    except Exception as exc:  # noqa: BLE001 —— 兜底:一条炸不能让整套评测中断
        return RowScore(
            row_id=str(row.get("id", "?")),
            passed=False,
            expected="(未能取得模型输出)",
            actual=f"{type(exc).__name__}: {exc}",
            reason="调用被测 Agent 时抛异常,这条按错计。先去修 Agent,别改判分。",
        )
    return spec.score(row, actual)


async def run_suite(
    spec: SuiteSpec,
    runner: AgentRunner | None,
    *,
    datasets_dir: Path = DATASETS_DIR,
) -> SuiteReport:
    """跑一套评测并返回报告。SKIP 的两种正当理由都在这里判掉。"""
    threshold = _threshold_of(spec)
    if runner is None:
        return SuiteReport(
            suite=spec.name,
            status=Status.SKIPPED,
            threshold=threshold,
            message="被测 Agent 还没接进来(用 --runners 注入后本套才会真的跑)。",
        )

    rows = load_rows(spec, datasets_dir)
    scorable, placeholders = split_placeholders(rows)
    if not scorable:
        return SuiteReport(
            suite=spec.name,
            status=Status.SKIPPED,
            threshold=threshold,
            skipped_ids=placeholders,
            message=f"数据集还没填({spec.dataset} 里只有示例占位行或干脆是空的)。",
        )

    # 样本量下限:在调模型**之前**判,别为一个不作数的百分比白烧额度。
    # 判 FAIL 而不是 SKIP —— SKIP 是绿的,而「只剩 3 条却报 100%」正是要拦的东西。
    #
    # 🔴 **这一条对 `diagnostic=True` 的套照样判 FAIL,不许「顺手统一」成 DIAG。**
    # 两件事守的不是同一样东西:
    #   · 命中率 —— 会抖(多跳链逐跳放大),所以诊断套不拿它卡门;
    #   · 样本量 —— **确定性的**,数据集被掏空就是被掏空,跟模型抖不抖没关系。
    # 把它也放成 DIAG 的后果:有人把 orchestration.csv 删到只剩 3 行,
    # 报告上是一片「诊断」的中性色,而那套其实已经什么都测不了了。
    min_rows = _min_rows_of(spec)
    if len(scorable) < min_rows:
        return SuiteReport(
            suite=spec.name,
            status=Status.FAILED,
            threshold=threshold,
            skipped_ids=placeholders,
            message=(
                f"只有 {len(scorable)} 条可判分,不足 {min_rows} 条,这个百分比不作数,"
                f"所以本套直接判不通过。请先把 {spec.dataset} 填够"
                "(下限见 eval/README.md 第一节第 3 条)。"
            ),
        )

    for row in scorable:
        spec.validate(row)  # 数据集写错要在跑模型**之前**炸,别白烧额度

    results = tuple([await _score_row(spec, row, runner) for row in scorable])
    hit_rate = sum(1 for item in results if item.passed) / len(results)
    return SuiteReport(
        suite=spec.name,
        status=(
            Status.DIAGNOSTIC
            if spec.diagnostic
            else (Status.PASSED if hit_rate >= threshold else Status.FAILED)
        ),
        threshold=threshold,
        results=results,
        skipped_ids=placeholders,
    )


async def run_suites(
    names: Sequence[str],
    runners: Mapping[str, AgentRunner],
    *,
    datasets_dir: Path = DATASETS_DIR,
) -> tuple[SuiteReport, ...]:
    """按顺序跑若干套。串行是刻意的:并行跑会同时打模型,又贵又容易触限流。"""
    reports = [
        await run_suite(SUITES[name], runners.get(name), datasets_dir=datasets_dir)
        for name in names
    ]
    return tuple(reports)


# ---------------------------------------------------------------------------
# 三、出报告
# ---------------------------------------------------------------------------


def format_diagnostics(results: Sequence[RowScore]) -> list[str]:
    """算「集合类答案」的诊断指标。**不参与判定红绿,只帮人看清失败长什么样。**

    为什么需要:判分是集合完全相等、不给部分分,于是「一项没答对」与
    「三项答对两项」在分数上都是 0。一个 43% 的报告里分不清模型是压根不会,
    还是每张只差一项 —— 而这两种的修法完全相反(换模型 vs 改提示词措辞)。

    两个指标:
      · 平均重合度 = 各行 Jaccard(交集/并集)的平均。**只统计 label 判对的行** ——
        label 都答错的行,比违规项没有意义。
      · 逐类召回 = 每个违规词「被标注了几次 / 其中模型答中几次」。
        新引入的类别就靠它看清是「完全没概念」还是「认得但措辞对不上」。
    """
    usable = [r for r in results if r.expected_items is not None and r.actual_items is not None]
    if not usable:
        return []

    lines: list[str] = []
    # label 对了才谈违规项的重合度
    scored = [r for r in usable if "判断结论就不对" not in r.reason]
    if scored:
        total = 0.0
        for item in scored:
            exp, got = item.expected_items or frozenset(), item.actual_items or frozenset()
            union = exp | got
            total += 1.0 if not union else len(exp & got) / len(union)
        lines.append(f"  平均重合度 {total / len(scored):.1%}(仅统计结论判对的 {len(scored)} 行)")

    hit: dict[str, int] = {}
    seen: dict[str, int] = {}
    for item in usable:
        for word in item.expected_items or frozenset():
            seen[word] = seen.get(word, 0) + 1
            if word in (item.actual_items or frozenset()):
                hit[word] = hit.get(word, 0) + 1
    if seen:
        lines.append("  逐类召回(标注里出现过的类):")
        for word in sorted(seen, key=lambda w: (-seen[w], w)):
            got = hit.get(word, 0)
            lines.append(f"      {word:<12s} {got}/{seen[word]}")

    # 模型报了、但标注里从没出现过的词 —— 过触发的信号
    over: dict[str, int] = {}
    for item in usable:
        for word in (item.actual_items or frozenset()) - (item.expected_items or frozenset()):
            over[word] = over.get(word, 0) + 1
    if over:
        lines.append("  多报的类(标注没有、模型报了):")
        for word in sorted(over, key=lambda w: -over[w]):
            lines.append(f"      {word:<12s} {over[word]} 次")
    return lines


def format_report(report: SuiteReport, *, verbose: bool = False) -> str:
    """把一套的结果渲染成人能读的文本。默认只展开挂掉的条目。"""
    lines = [f"===== {report.suite} [{report.status}] ====="]
    if report.message:
        lines.append(f"  {report.message}")
    if report.skipped_ids:
        lines.append(f"  跳过的占位行:{'、'.join(report.skipped_ids)}")
    if not report.results:
        # 两种情况没有逐条明细:SKIP,以及「可判分的行不足最小样本量」。
        # 后者绝不能去打印「0.0%(0/0)」—— 那是个会被误读成"模型全错"的假数字。
        return "\n".join(lines)

    lines.append(
        f"  得分 {report.score:.1%}({report.passed_count}/{report.total})"
        f"  门槛 {report.threshold:.0%}"
    )
    lines.extend(format_diagnostics(report.results))
    shown = [r for r in report.results if verbose or not r.passed]
    if not shown:
        lines.append("  全部通过。")
    for item in shown:
        mark = "OK  " if item.passed else "FAIL"
        lines.append(f"  [{mark}] {item.row_id}  {item.reason}")
        lines.append(f"         期望:{item.expected}")
        lines.append(f"         实际:{item.actual}")
    return "\n".join(lines)


def format_summary(reports: Sequence[SuiteReport]) -> str:
    """收尾总览:一眼看清三套各自什么状态。"""
    lines = ["===== 总览 ====="]
    for report in reports:
        if not report.results:  # SKIP,或样本量不足 —— 两种都没有分数可报,只报原因
            lines.append(f"  {report.suite:<8} {report.status.value:<6} {report.message}")
            continue
        if report.status is Status.DIAGNOSTIC:
            # 诊断套**不显示门槛** —— 它压根不卡那条线,显示出来会让人以为
            # 「差一点就过了/已经过了」,而这个数本身是抖的。
            # 直接把该看什么写在这一行上:这套的价值在**逐条明细**,不在总分。
            lines.append(
                f"  {report.suite:<8} {report.status.value:<6} "
                f"{report.score:.1%} —— 仅供诊断,不参与红绿;看上面的逐条明细,别看这个数"
            )
            continue
        lines.append(
            f"  {report.suite:<8} {report.status.value:<6} "
            f"{report.score:.1%}(门槛 {report.threshold:.0%})"
        )
    return "\n".join(lines)


def exit_code_of(reports: Sequence[SuiteReport]) -> int:
    """有任何一套低于门槛就返回非 0,好让 CI 拦门。全 SKIP 时返回 0。

    ⚠️ `Status.DIAGNOSTIC` 与 SKIP 一样**不影响退出码** —— 那正是它存在的意义
    (见 SuiteSpec.diagnostic)。但它和 SKIP 有一处关键不同:诊断套是**真跑过**的,
    所以报告里有逐条明细。CI 上它不拦门,人要看的是那些明细。"""
    return EXIT_BELOW_THRESHOLD if any(r.status is Status.FAILED for r in reports) else EXIT_OK


# ---------------------------------------------------------------------------
# 四、命令行入口
# ---------------------------------------------------------------------------


def load_runners(spec: str | None) -> Mapping[str, AgentRunner]:
    """按 ``模块:属性`` 取被测函数表。不传就是「一个都没接」,三套全 SKIP。

    这是 runner 与具体 Agent 之间唯一的连接点 —— 本文件不 import 任何 Agent,
    W2/W3 加 Agent 时只需要在自己那边导出一个 {套名: async 函数} 的字典。
    """
    if not spec:
        return {}
    module_name, _, attr_name = spec.partition(":")
    if not module_name or not attr_name:
        raise RunnerSpecError(f"--runners 要写成「模块:属性」,收到的是「{spec}」。")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RunnerSpecError(f"导不进模块「{module_name}」:{exc}") from exc
    try:
        table = getattr(module, attr_name)
    except AttributeError as exc:
        raise RunnerSpecError(f"模块「{module_name}」里没有「{attr_name}」。") from exc
    if not isinstance(table, Mapping):
        raise RunnerSpecError(f"「{spec}」不是字典,应形如 {{'safety': 某个 async 函数}}。")
    unknown = set(table) - set(SUITES)
    if unknown:
        raise RunnerSpecError(f"「{spec}」里有不认识的套名:{'、'.join(sorted(unknown))}。")
    return dict(table)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval.runner", description="工友通评测跑分")
    parser.add_argument(
        "--suite",
        default=ALL_SUITES,
        choices=(ALL_SUITES, *SUITES),
        help="跑哪一套,默认三套全跑",
    )
    parser.add_argument(
        "--runners",
        default=None,
        help="被测函数表,写成「模块:属性」;不传则全部 SKIP",
    )
    parser.add_argument(
        "--datasets-dir",
        default=str(DATASETS_DIR),
        help="数据集目录,默认 eval/datasets",
    )
    parser.add_argument("--verbose", action="store_true", help="连通过的条目也打印出来")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。返回值就是退出码:0 通过 / 1 低于门槛 / 2 数据集或参数有问题。"""
    args = _build_parser().parse_args(argv)
    names = tuple(SUITES) if args.suite == ALL_SUITES else (args.suite,)
    try:
        runners = load_runners(args.runners)
        reports = asyncio.run(run_suites(names, runners, datasets_dir=Path(args.datasets_dir)))
    except (DatasetError, RunnerSpecError) as exc:
        print(f"[评测跑不起来] {exc}")
        return EXIT_DATASET_ERROR

    for report in reports:
        print(format_report(report, verbose=args.verbose))
    print(format_summary(reports))
    return exit_code_of(reports)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DATASETS_DIR",
    "EXIT_BELOW_THRESHOLD",
    "EXIT_DATASET_ERROR",
    "EXIT_OK",
    "SUITES",
    "AgentRunner",
    "RunnerSpecError",
    "Status",
    "SuiteReport",
    "SuiteSpec",
    "exit_code_of",
    "format_diagnostics",
    "format_report",
    "format_summary",
    "is_placeholder",
    "load_rows",
    "load_runners",
    "main",
    "run_suite",
    "run_suites",
    "split_placeholders",
]
