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

import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

# 只识别、**不登记**那一半(W9 D14)。别改回 analyze_site_photo:那个工具会把违规项
# 写进隐患台账,而 safety 套每行都新 register 一份同图副本 —— 幂等键
# (project_id, photo_sha256, item)对 30 张**内容各不相同**的照片一条都拦不住,
# 于是 `make eval SUITE=safety` 每跑一次,生产台账就多 30 条没人拍过的「待确认隐患」。
from gyt.agents.safety.tools import _recognize
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
    """跑安全集的一行:把照片登记成产物 → 调**识别**那一半 → 交出结构化判断。

        row["image"] = "photo_01.jpg"
              │ 拼到 <仓库根>/data/demo/photos/
              ▼
        artifacts.register(...)  → artifact_id
              │
              ▼
        _recognize(artifact_id) → Envelope        ← **不是** analyze_site_photo
              │ ok=False ─────────▶ raise EvalRunnerError(中文原因)
              ▼ ok=True
        Envelope["data"] = {"label","violations","note"}   ← scorers.score_safety 读这个

    每次都重新 register 是有意的:artifacts 没有「按内容找已有产物」的接口,
    而重复登记**不影响缓存命中** —— 视觉缓存的键算在图片**内容**上,
    artifact_id 一个字都不参与(这一点由 test_换一张照片不会错误命中上一张的缓存 反证)。
    代价只是 artifacts 目录里多几份同图副本,30 张小图,无所谓。

    ⚠️ 但**登记那一半不能跟着跑**:评测只是在给识别打分,不是在真做巡检。
    走 ``analyze_site_photo`` 的话,每跑一轮全量就往真实隐患台账里灌 30 条
    没人拍过的 pending 隐患(照片内容各不相同,幂等键拦不住),
    整改率、待确认列表、超期清单全被污染。这就是 W9 D14 拆两半的直接动因之一。
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
    envelope = await _recognize(artifact_id)

    if not isinstance(envelope, Mapping):
        raise EvalRunnerError(f"识别没返回信封,而是 {type(envelope).__name__}。")
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


RECURSION_LIMIT_ENV: Final[str] = "GYT_EVAL_RECURSION_LIMIT"
"""临时改递归上限的环境变量,**只给 orchestration 套用**。

为什么走环境变量而不是数据集的一列:递归上限是**跑法**不是**样本属性** ——
同一份数据集要在 8 / 10 / 12 / 16 四档下各跑一遍,把它写进 csv 等于把同一批样本
抄四份。而 CLAUDE.md 记着一条实测:**调用时 config 里的 recursion_limit 会盖掉
编译时 `.with_config` 钉的那个**(2026-08-11 安全复核,传 60 就真跑 60 步)——
所以这条路是通的。

⚠️ 不设它就用图自己编译时钉的值(`config.supervisor_recursion_limit`,当前 8)。
"""

_CLARIFY_MARKS: Final[tuple[str, ...]] = ("?", "？")
"""判「这是在追问」的记号。

刻意只认问号,**不认「哪」「是要」这类词**:那些字在正常答话里也大量出现
(「哪个工地」可以是追问,也可以是「这条隐患在哪个工地」的陈述),
拿它们当判据会把正常回答误判成追问,而误判的方向是**放行**——
一条本该派活却自己答了的样本会被判成「追问,正确」。
判据宁可窄,漏判是红灯,误判是假绿灯。
"""


def classify_outcome(handoffs: int, final_text: str, error: str | None) -> str:
    """把一次整链跑的结果归成 orchestration.csv 的三档之一。**纯函数,不碰模型。**

    ===========================================================================
    判据
    ---------------------------------------------------------------------------
        error 非空                       → fail   (熔断 / 超时 / 图自己抛的)
        handoffs > 0                     → success(派了活并且跑完了)
        handoffs == 0 且末尾有问号        → clarify(没派活,回头问了一句)
        handoffs == 0 且末尾没问号        → fail   (没派活,自己编了个答案)

    ===========================================================================
    🔴 这套测的是**调度**,不是**答得对不对**
    ---------------------------------------------------------------------------
    所以 ``handoffs > 0`` 一律算 success,哪怕子 Agent 回的是「知识库里查不到依据」。
    「查到 0 条」是合法答案 —— 调度做对了,内容对不对归 rag / safety 那两套管。
    把内容判据混进来的下场是:一条路由完全正确的样本因为库里恰好没数据而判红,
    而人会去查 supervisor 的提示词。

    ⚠️ 最后那条(0 跳 + 没问号 = fail)是**刻意往严里判**:supervisor 在该派活时
    自己编一个答案,正是这套要抓的东西。代价是它也会把「你好」这种正当的闲聊自答
    判成 fail —— 所以 **orchestration.csv 里不许放闲聊行**,那类归 routing 套的
    ``expected_agent=none``(两套的分工写在 eval/README.md 里)。
    """
    if error:
        return "fail"
    if handoffs > 0:
        return "success"
    tail = (final_text or "").strip()
    return "clarify" if any(mark in tail for mark in _CLARIFY_MARKS) else "fail"


async def run_orchestration_row(row: Mapping[str, str]) -> Any:
    """跑整链集的一行:把 user_input 发进真实整图并**跑完**,交出这一轮的调度轨迹。

        user_input ──► graph.astream(updates)   ← 与 routing 套相反:**不掐断**
              │ 逐个更新扫 transfer_to_X 工具调用,按出现顺序记成 path
              │ 记住最后一条有正文的消息 —— classify_outcome 用它分辨追问与自答
              ▼ 跑到图自己结束(或熔断)

        返回 {"path": [...], "status": ..., "handoffs": N}

    ===========================================================================
    与 ``run_routing_row`` 的分工(别把两套合并)
    ---------------------------------------------------------------------------
    routing 只判**第一跳派给谁**,并且在第一跳就停流 —— 它的头注写着理由:
    「让子 Agent 继续跑既慢又烧钱」。那条判断今天依然成立,所以 routing 不动。

    本套判的是**整件事有没有做完**,所以必须跑完。代价是每行贵 10-20 倍
    (子 Agent 的整个工具循环都要跑,含识图的那几条每次 7-10 秒)。
    ⚠️ 因此**别把 routing.csv 的 33 行搬进来** —— 那 33 行里绝大多数只需要验第一跳。

    ===========================================================================
    ⚠️ ``transfer_back_to_supervisor`` 不算一跳
    ---------------------------------------------------------------------------
    ``add_handoff_back_messages=True`` 生成的回程工具叫 ``transfer_back_to_…``,
    它不以 ``transfer_to_`` 开头,所以现有的 ``HANDOFF_PREFIX`` 判据**天然把它排除**
    (2026-08-22 实测确认过)。别为它加特判 —— 加了反而会在有人改前缀时静默算错。

    ⚠️ **按工具调用 id 去重**:同一个交接可能出现在多个更新里(supervisor 那个节点
    会把消息重放),不去重的话双 Agent 流程会被数成四跳,而 max_handoffs 当场误判。
    """
    text = str(row.get("user_input") or "").strip()
    if not text:
        raise EvalRunnerError("这一行没填 user_input,没法测调度。")

    # 惰性导入,理由同 run_routing_row(import gyt.graph 即建图、即要 API Key)。
    from langgraph.errors import GraphRecursionError

    from gyt.core.run_context import PROJECT_CONFIG_KEY
    from gyt.graph import graph

    config: dict[str, Any] = {}

    # 「当前工地」—— 界面顶栏选的那个,经 config.configurable 注入(契约在
    # core/run_context.PROJECT_CONFIG_KEY;前端 thread-index.tsx 用的是同一个键)。
    #
    # 🔴 **这一路 2026-08-22 之前是缺的**,而它不是可有可无:
    # `AgentSpec.requires_project=True` 的那几个(cad / supervision)在没选工地时,
    # supervisor 按提示词会**先回头要工地而不是派活** —— 于是这类行永远只测得到未选状态,
    # 「选了工地之后这条复合链走不走得通」压根表达不了。
    #
    # 那天 O09(cad>knowledge)就是这么红的:实际行为完全正确(它去要工地了),
    # 而数据集写在那句提示词之前、期望的是直接派活。**两件都对,是评测缺一维。**
    # 留空 = 不选工地,那也是一种要测的状态(该被提醒的那一档)。
    project_id = str(row.get("project_id") or "").strip()
    if project_id:
        config["configurable"] = {PROJECT_CONFIG_KEY: project_id}

    raw_limit = os.environ.get(RECURSION_LIMIT_ENV, "").strip()
    if raw_limit:
        try:
            config["recursion_limit"] = int(raw_limit)
        except ValueError as exc:
            raise EvalRunnerError(
                f"{RECURSION_LIMIT_ENV} 要填整数,现在是「{raw_limit}」。"
            ) from exc

    path: list[str] = []
    seen_calls: set[str] = set()
    final_text = ""
    error: str | None = None

    try:
        async for update in graph.astream(
            {"messages": [("user", text)]}, stream_mode="updates", config=config or None
        ):
            if not isinstance(update, dict):
                continue
            for payload in update.values():
                if not isinstance(payload, dict):
                    continue
                for message in payload.get("messages") or []:
                    for call in getattr(message, "tool_calls", None) or []:
                        name = str(call.get("name") or "")
                        call_id = str(call.get("id") or f"{name}@{len(seen_calls)}")
                        if name.startswith(HANDOFF_PREFIX) and call_id not in seen_calls:
                            seen_calls.add(call_id)
                            path.append(name.removeprefix(HANDOFF_PREFIX))
                    content = getattr(message, "content", "")
                    if isinstance(content, str) and content.strip():
                        final_text = content
    except GraphRecursionError as exc:
        # 熔断**不是异常,是一个观测结果** —— 这套评测存在的一半理由就是量它。
        # 抛出去的话 runner 会把整行记成「跑挂了」,而我们要的是「这一档上限不够」。
        error = f"熔断:{exc}"
    except Exception as exc:  # noqa: BLE001 —— 见下
        # 其余异常同样收敛成事实。评测的职责是**如实记录发生了什么**,
        # 而不是替被测系统决定「这算不算一次失败」。异常类型进 error 文本,只进报告。
        error = f"{type(exc).__name__}: {exc}"

    return {
        "path": path,
        "status": classify_outcome(len(path), final_text, error),
        "handoffs": len(path),
    }


RUNNERS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "safety": run_safety_row,
        "routing": run_routing_row,
        "rag": run_rag_row,
        "orchestration": run_orchestration_row,
    }
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
    "RECURSION_LIMIT_ENV",
    "RUNNERS",
    "EvalRunnerError",
    "classify_outcome",
    "run_rag_row",
    "run_routing_row",
    "run_safety_row",
]
