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
"""仓库根(本文件是 <仓库根>/backend/eval/hooks.py,往上两级)。照片在 <仓库根>/data/demo/photos/。

⚠️ 这里的**理由已经换过一轮,别照抄旧说法**:

- 旧说法(2026-08 之前,现在是假话):"data_dir 是按进程工作目录解析的运行期目录
  (backend/data),演示素材不在那儿"。那时 ``data_dir`` 的默认值是 ``Path("data")``,
  相对路径跟着 cwd 跑,`make dev` 先 cd 到 backend,数据就落 backend/data/。
  prefilter.py 顶部踩的就是这个坑:27 张照片被静默写到了 backend/data/。
- 现在:``_default_data_dir()`` 改成按 config.py 的 ``__file__`` 推导仓库根,
  默认情况下 ``get_settings().demo_assets_dir`` 就是 <仓库根>/data/demo,
  **和这里算出来的是同一个目录**(2026-08-09 在 backend/ 下实测两者一致)。

之所以仍旧锚 ``__file__`` 而不去读配置:评测集是「safety.csv + 它点名的那些照片」
一整套仓库内资产,必须来自同一份 checkout;而 GYT_DATA_DIR 是给运行期数据
(缓存 / 台账 / 向量库)搬家用的,把它指到别处时照片不该跟着走丢。

⚠️ 已知偏差,未处理:``knowledge/ingest.py`` 与 ``cad/demo_registry.py`` 已经改成读
``demo_assets_dir``(而且是**函数**,取值时刻 = 调用时刻),只有 eval 这边还锚 __file__。
设了 GYT_DATA_DIR 时三者会指向两处。要统一就得连这个模块级常量一起改成函数
(测试靠 monkeypatch 这个名字打桩),不是改一行的事,留给后续决策。"""

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


HANDOFF_PREFIX: Final[str] = "transfer_to_"
"""langgraph_supervisor 自动生成的交接工具名前缀。按模式解析,加 Agent 不用改这里。"""


async def run_routing_row(row: Mapping[str, str]) -> Any:
    """跑路由集的一行:把 user_input 发进真实整图,取 supervisor 的**第一跳**决策。

        user_input ──► graph.astream(updates)
              │ supervisor 的首个更新里有 transfer_to_X 工具调用 ──► 返回 "X"
              │ supervisor 直接给了纯文本(自答/追问/婉拒) ────────► 返回 "none"
              ▼ 随即停止流式 —— 子 Agent 一步都不跑

    为什么在第一跳就掐断:路由评测只判「派给了谁」,让子 Agent 继续跑既慢又烧钱
    (inspection 一旦启动就是整条英雄链),而后面发生什么与判分无关。
    每行成本 = 一次 DeepSeek 文本调用;命中路径甲缓存时为零。

    测的是**真实 supervisor**(真提示词、真登记表、真模型),不是复刻品 ——
    复刻会漂移,漂移了评测就在给一个不存在的系统打分。
    """
    text = str(row.get("user_input") or "").strip()
    if not text:
        raise EvalRunnerError("这一行没填 user_input,没法测路由。")

    # 惰性导入:import gyt.graph 即建图、即要 API Key(模块级 graph,见其顶部结论三)。
    # 放模块顶部会拖累所有不测路由的场景 —— 包括别人只想跑 safety 时。
    from gyt.graph import graph

    async for update in graph.astream({"messages": [("user", text)]}, stream_mode="updates"):
        if not isinstance(update, dict):
            continue
        payload = update.get("supervisor")
        if not isinstance(payload, dict):
            continue
        messages = payload.get("messages") or []
        # 先扫整个更新里的交接调用,再看纯文本 —— supervisor 可能在同一条消息里
        # 既写一句话又发工具调用,顺序反了会把「派活前的铺垫话」误判成 none。
        for message in messages:
            for call in getattr(message, "tool_calls", None) or []:
                name = str(call.get("name") or "")
                if name.startswith(HANDOFF_PREFIX):
                    return name.removeprefix(HANDOFF_PREFIX)
        for message in messages:
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                return "none"
    return "none"


async def run_rag_row(row: Mapping[str, str]) -> Any:
    """跑知识集的一行:把 question 丢进检索工具,交出 {answer, source, page} 给 score_rag。

        row["question"] ──► search_regulation(query)
              │ ok=True  ─► {answer: 命中原文拼接, source: 完整文件名, page: 命中页码}
              │ EMPTY    ─► {answer: 「查不到」的人话, source: "", page: ""}  ← no_answer 判定用
              │ 其它失败  ─► raise EvalRunnerError

    为什么直接调**工具**而不是走完整 knowledge Agent(与 safety 同源):
      rag 测的是**检索准不准、有没有编造**(eval/README.md 原话),不是「Agent 会不会说话」。
      走本体会多引入两层噪音(可能不调工具、可能把原文改写得走样),还多烧一轮 DeepSeek。
      工具返回的 passages 本来就带 source/page,正是 score_rag 要读的三要素来源。
      代价同 safety:**Agent 本体那层(会不会照抄出处/会不会扩写)没被这套覆盖** ——
      那部分靠 routing 套 + 人工过演示话术兜。

    answer 用命中原文的拼接(要点应当逐字在原文里),所以 rag.csv 的 expected_answer_points
    要按**原文里真实出现的字样**填(原文有 OCR 噪声,别填一个原文里没有的漂亮说法)。
    page 把命中的几页都给上(score_rag 只要求与 expected_page 有交集),别自己缩成一页反而漏。
    """
    question = str(row.get("question") or "").strip()
    if not question:
        raise EvalRunnerError("这一行没填 question,没法测检索。")

    # 惰性导入:import 会带进 knowledge.store(向量库/embedding 的门面)。放模块顶部会拖累
    # 只想跑 safety/routing 的场景。真正加载 2.2GB 模型是在工具第一次被调时。
    from gyt.agents.knowledge.tools import search_regulation

    envelope = await search_regulation.ainvoke({"query": question})
    if not isinstance(envelope, Mapping):
        raise EvalRunnerError(f"检索工具没返回信封,而是 {type(envelope).__name__}。")

    if not envelope.get("ok"):
        if envelope.get("error_code") == "EMPTY_RESULT":
            # 检索判「无依据」→ 交出「承认查不到、无出处」的形状,给 score_rag 的 no_answer 判定。
            return {"answer": str(envelope.get("user_msg") or ""), "source": "", "page": ""}
        raise EvalRunnerError(f"检索失败({envelope.get('error_code')}):{envelope.get('user_msg')}")

    passages = (envelope.get("data") or {}).get("passages") or []
    return {
        "answer": " ".join(str(p.get("text") or "") for p in passages),
        "source": str(passages[0].get("source") or "") if passages else "",
        "page": ",".join(str(p.get("page")) for p in passages if p.get("page") is not None),
    }


RUNNERS: Final[Mapping[str, Any]] = MappingProxyType(
    {"safety": run_safety_row, "routing": run_routing_row, "rag": run_rag_row}
)
"""套名 → 被测函数。**这是加新 Agent 时唯一要改的地方。**

MappingProxyType 是只读的:runner.load_runners 会 dict(...) 复制一份再用,
但这里仍然只读,防止有人在运行期往里塞东西。

三套已齐(safety 2026-08-07;routing / rag 2026-08-09 随 knowledge 落地一起接上)。
这段以前挂着一行「W2/W3 往下加:"rag": 队友的 knowledge Agent」—— 那是在描述
它自己上面 8 行的代码,而代码里 "rag" 早就在了。加第四套时往上面的字典里加一个键即可。
"""

__all__ = [
    "PHOTOS_DIR",
    "RUNNERS",
    "EvalRunnerError",
    "run_rag_row",
    "run_routing_row",
    "run_safety_row",
]
