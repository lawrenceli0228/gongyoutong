"""被测 Agent 的接线板 —— runner 与具体 Agent 之间唯一的连接点。

用法(Makefile 的 `make eval` 已经带上了):

    uv run python -m eval.runner --suite safety --runners eval.hooks:RUNNERS

===========================================================================
为什么这份文件在 eval/ 而不在 gyt/
---------------------------------------------------------------------------
它是**为了评测而存在**的胶水,不是产品代码。产品跑起来一行都用不到它。
放进 gyt 会让「哪些是线上真跑的代码」这个边界变模糊。

而 runner.py 自己坚持一个 Agent 都不 import(见其 load_runners 的说明),
是为了让 `test_eval_runner.py` 能在**没有 API Key、没装模型依赖**的环境里跑。
那条约束属于 runner.py,不属于本文件 —— 本文件的全部职责就是 import 真东西。

===========================================================================
为什么直接调工具,而不是走完整的 Agent
---------------------------------------------------------------------------
safety 这一套测的是**识图准不准**(eval/README.md 原话),不是「Agent 会不会说话」。
走完整 Agent 会多引入两层噪音:本体可能不调工具、可能把结构化结果改写成自然语言,
于是一次判错说不清是「看错了」还是「话说岔了」——而这两件事的修法完全不同。

顺带还省钱:少一轮 DeepSeek 文本调用,且工具返回的本来就是结构化的
``{"label","violations","note"}``,正是 scorers.score_safety 要读的形状。

代价(明确记着,别当成没有):**Agent 本体那一层没有被评测覆盖。**
它会不会在该调工具时不调、会不会把 violations 讲漏一项 —— 这套测不到。
那部分要靠 routing 套(该不该派给 safety)和人工过一遍演示话术来兜。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from gyt.agents.safety.tools import analyze_site_photo
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
"""仓库根。照片在 <仓库根>/data/demo/photos/,而**不能**用 Settings.data_dir 去找 ——
data_dir 是按进程工作目录解析的运行期目录(backend/data),演示素材不在那儿。
prefilter.py 顶部踩过同一个坑:相对路径会让 27 张照片被静默写到 backend/data/。"""

PHOTOS_DIR: Final[Path] = REPO_ROOT / "data" / "demo" / "photos"

COLUMN_IMAGE: Final[str] = "image"
"""safety.csv 里放照片文件名的列。列名定义在 eval/README.md 与 runner.SUITES 里。"""


class EvalRunnerError(RuntimeError):
    """被测对象没能给出可判分的输出。

    刻意抛异常而不是返回一个空结果:runner._score_row 会把异常文本原样写进报告的
    ``actual`` 字段,于是「第 7 条为什么挂了」在报告里一眼可见。
    返回空 dict 的话报告只会显示「空 / (无)」,还得回头翻日志。
    """


async def run_safety_row(row: Mapping[str, str]) -> Any:
    """跑安全集的一行:把照片登记成产物 → 调识图工具 → 交出结构化判断。

        row["image"] = "photo_01.jpg"
              │ 拼到 <仓库根>/data/demo/photos/
              ▼
        artifacts.register(...)  → artifact_id
              │
              ▼
        analyze_site_photo(artifact_id) → Envelope
              │ ok=False ─────────▶ raise EvalRunnerError(中文原因)
              ▼ ok=True
        Envelope["data"] = {"label","violations","note"}   ← scorers.score_safety 读这个

    每次都重新 register 是有意的:artifacts 没有「按内容找已有产物」的接口,
    而重复登记**不影响缓存命中** —— 视觉缓存的键算在图片**内容**上,
    artifact_id 一个字都不参与(这一点由 test_换一张照片不会错误命中上一张的缓存 反证)。
    代价只是 artifacts 目录里多几份同图副本,30 张小图,无所谓。
    """
    name = str(row.get(COLUMN_IMAGE) or "").strip()
    if not name:
        raise EvalRunnerError(f"这一行没填 {COLUMN_IMAGE} 列,不知道该看哪张照片。")

    # 只取文件名,挡住 CSV 里写成 "../../etc/passwd" 这种路径穿越。
    # 评测集是人手填的,当不可信输入处理。
    photo = PHOTOS_DIR / Path(name).name
    if not photo.is_file():
        raise EvalRunnerError(
            f"照片不存在:{photo}。请确认它在 data/demo/photos/ 下,且 CSV 里的文件名逐字一致。"
        )

    artifact_id = artifacts.register(photo, kind=ArtifactKind.PHOTO, original_name=photo.name)
    envelope = await analyze_site_photo.ainvoke({"artifact_id": artifact_id})

    if not isinstance(envelope, Mapping):
        raise EvalRunnerError(f"工具没返回信封,而是 {type(envelope).__name__}。")
    if not envelope.get("ok"):
        raise EvalRunnerError(
            f"识图工具返回失败({envelope.get('error_code')}):{envelope.get('user_msg')}"
        )
    return envelope.get("data")


RUNNERS: Final[Mapping[str, Any]] = MappingProxyType({"safety": run_safety_row})
"""套名 → 被测函数。**这是加新 Agent 时唯一要改的地方。**

MappingProxyType 是只读的:runner.load_runners 会 dict(...) 复制一份再用,
但这里仍然只读,防止有人在运行期往里塞东西。

W2/W3 往下加:
    "rag":     队友的 knowledge Agent(W2 末验收)
    "routing": 整图 supervisor 的派活结果(W3 末验收,门槛 0.90 最高)
"""

__all__ = ["PHOTOS_DIR", "RUNNERS", "EvalRunnerError", "run_safety_row"]
