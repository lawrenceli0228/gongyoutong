"""工友通 Supervisor 图 —— 整个多智能体系统的总入口。

`langgraph.json` 里 `"gyt": "./src/gyt/graph.py:graph"` 指的就是本文件末尾那个
模块级变量 `graph`。改文件名 / 改变量名 = 改部署配置，动之前先看 langgraph.json。

===========================================================================
API 核实结论（2026-08-05 实测，逐个从 PyPI 下 wheel 解包读源码，不是凭记忆）
---------------------------------------------------------------------------
版本：langgraph 1.2.10 / langgraph-supervisor 0.0.31 / langgraph-prebuilt 1.1.0
      langchain 1.3.14 / langchain-core 1.5.3 / langgraph-api 0.12.0

结论一：`create_supervisor(...)` 返回的是**没编译过的 StateGraph**，必须自己 `.compile()`。
    supervisor.py 末尾一行就是 `return builder`，返回类型标注也写着 `-> StateGraph`。
    （对比 `langchain.agents.create_agent` 返回的是已编译的 CompiledStateGraph ——
      两者一个要 compile 一个不要，很容易搞混，见 core/base_agent.py 顶部说明。）
    实测签名（只列我们用到的）：
        create_supervisor(
            agents: list[Pregel], *,
            model: LanguageModelLike,
            tools=None,                       # 不传则自动为每个 agent 生成交接工具
            prompt: Prompt | None = None,     # str 会被包成 SystemMessage 放在最前
            output_mode: OutputMode = "last_message",
            add_handoff_messages: bool = True,
            add_handoff_back_messages: bool | None = None,   # None → 跟随 add_handoff_messages
            supervisor_name: str = "supervisor",
            ...
        ) -> StateGraph

结论二：recursion_limit 不是 `.compile()` 的参数（compile 只收 checkpointer / cache /
    store / interrupt_before / interrupt_after / debug / name / transformers）。
    正确做法是编译完再 `.with_config(recursion_limit=N)` —— langgraph 把
    `Pregel.with_config` 重写过了（pregel/main.py:927），返回的是 **Pregel 的副本**
    而不是 langchain 那种 RunnableBinding，所以类型仍是 CompiledStateGraph，
    `.name` / `.nodes` / `langgraph dev` 全都不受影响。
    另注：langgraph 1.2.10 的默认 recursion_limit 是 10007（_internal/_config.py:32），
    我们设 8 属于「显著收紧」，merge_configs 会正常保留（只有等于默认值时才被忽略）。

    ⚠️ with_config 的一个暗坑，W2/W3 加 checkpointer 之前务必先读这段：
        Pregel.copy() 的实现是 `self.__class__(**self.__dict__)`，也就是拿实例属性
        重新走一遍 __init__。而 compile() 是在 __init__ 之后才补挂三个私有属性的
        （graph/state.py:1358-1372：_serde_allowlist / _output_mapper / _state_mapper），
        它们会被 Pregel.__init__ 的 **deprecated_kwargs 悄悄吞掉、复制不过去。
        目前对我们无害，两条原因都已核实：
          · _output_mapper / _state_mapper：_pick_mapper() 只在 state schema 是
            pydantic BaseModel 或 dataclass 时才返回非 None（state.py:1718-1725）。
            我们的 state 是 AgentState（TypedDict），本来就是 None，丢了等于没丢。
          · _serde_allowlist：只在挂了 checkpointer 时才被用到（main.py:843）。
            T1 没有 checkpointer。
        **所以：等 W2/W3 真给这张图挂上 checkpointer（做多轮记忆）的那天，
        必须回来重测这一行** —— 届时更稳的写法是先 with_config 再 compile，
        或者干脆改成给编译结果直接赋 .config。

结论三：为什么 `graph` 必须是**货真价实的模块级变量**，不能用 PEP 562 的模块
    `__getattr__` 做惰性构建 —— langgraph-api 0.12.0 取图用的是
    `module.__dict__[spec.variable]`（graph.py:772），**不是 getattr**，
    惰性方案会直接 KeyError，`langgraph dev` 起不来。
    代价：本模块一被 import 就会真的建图，也就会真的要 API Key。
    所以单测里**禁止在文件顶部 import 本模块**，必须在打完桩之后再 import，
    详见 backend/tests/unit/test_graph.py 的 graph_module fixture。

结论四：子 Agent 的 `.name` 不能是 None，也不能是 "LangGraph"（StateGraph.compile()
    不传 name 时的默认值），否则 create_supervisor 直接 ValueError（supervisor.py:399）。
    这条已经在 core/base_agent.py 的 _validate_agent_name() 里提前用中文拦掉了。

来源：
    https://pypi.org/pypi/langgraph-supervisor/json
    github.com/langchain-ai/langgraph-supervisor-py
        → langgraph_supervisor/supervisor.py
    https://reference.langchain.com/python/langgraph-supervisor/supervisor/create_supervisor
===========================================================================

图 1：T1 初始拓扑（**历史快照**,当前真实名单以下方 AGENT_REGISTRY 为准 ——
      已含 safety 与 inspection 英雄链;英雄链内部结构见 build_inspection_chain）

        START
          │
          ▼
    ┌──────────────────────────────────────────────────┐
    │ supervisor                                       │
    │ 模型：settings.model_text（DeepSeek，默认关思考）  │
    │ 职责：只派活 + 汇总，自己一点活不干                 │
    └──┬────────────────────────────────────────┬──────┘
       │ transfer_to_ping                       │ 干完了 / 没人可派
       │ （create_supervisor 自动生成的交接工具） │
       ▼                                        ▼
    ┌────────┐                                 END
    │  ping  │  T1 连通性占位：调 echo 工具把原话回显
    └───┬────┘
        │ 子 Agent 跑完固定回到 supervisor（库自动加的边，不用我们写）
        └────────────────► supervisor

图 2：W2 / W3 挂载位与分工（**2026-08-06 的规划快照,同样以下方 AGENT_REGISTRY 为准**）

      ⚠️ 这张图里有三处已经不是现状了,别照它判断「谁在册」:
        · ping   —— 2026-08-08 已从 AGENT_REGISTRY **摘除**(理由见该常量顶部的三连实锤),
                    包还在,当工具写法样板用,但它**不占路由位**了。
        · knowledge / cad —— 标着「队友 · W2」,其实 2026-08-09 就已落地在册
                    (cad = b4fc154、knowledge = 9b120be)。
        · report —— 画在挂载位上,实际**不在登记表里**:它只作为英雄链 inspection 的
                    第二跳存在,`transfer_to_report` 这条路不存在(见 build_inspection_chain)。
      留着这张图是因为下面那段分工说明记着「为什么这么分」,那是当时的决策记录。

                              supervisor
                                  │
      ┌──────────┬────────────┬───┴────────┬──────────┬──────────┐
      ▼          ▼            ▼            ▼          ▼          ▼
    ping     knowledge     schedule      safety      cad      report
    (T1)     规范检索+页码   SQLite 任务   kimi 识图   DXF 查询  docx 落盘
             队友 · W2      你 · W2       你 · W2    队友       你 · W3
                                                     W2 打地基
                                                     W3 出查询

    注：2026-08-06 分工按队友意愿重排（原为「X=编排线 / Y=多模态线」的整块划分）。
    现在两条线是：队友＝检索线(knowledge + cad)，你＝感知与产出线(safety +
    schedule + report) 外加 supervisor 与英雄链。上面每列各自标注归属，
    不再画整块的泳道括号——因为两人的 Agent 在图上是交错的。
    这么换的直接好处：ingest/ 两个文件都归队友（原本分属两人），
    且英雄链 safety→report 连同本文件全在一人手里，演示主线零跨人联调。
    详见 docs/W2_执行计划.html。

    「巡检英雄链」（确定性子图，W3 接入，位置就在下面 build_graph() 里）：

        safety ──[写死的确定性边，不经过 supervisor 再决策一次]──► report

    即「拍照识违规 → 自动出巡检报告」这一步必须是硬编码的图边，不能交给 LLM 判断，
    否则演示当场会随机漏掉出报告那一环。接法：先用 StateGraph 把 safety 和 report
    串成一个子图并 compile(name="inspection")，再把这个子图当成「一个 Agent」
    塞进 AGENT_REGISTRY —— create_supervisor 的 agents 参数收的是 Pregel，
    编译后的子图本来就是 Pregel，不需要任何额外适配。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph_supervisor import create_supervisor

from gyt.agents.attendance import ATTENDANCE_AGENT_NAME, build_attendance_agent
from gyt.agents.cad import CAD_AGENT_NAME, build_cad_agent
from gyt.agents.knowledge import KNOWLEDGE_AGENT_NAME, build_knowledge_agent
from gyt.agents.report import build_report_agent
from gyt.agents.safety import SAFETY_AGENT_NAME, build_safety_agent
from gyt.agents.schedule import SCHEDULE_AGENT_NAME, build_schedule_agent
from gyt.config import get_settings
from gyt.core.run_context import project_from_config
from gyt.db.projects import get_project

# 导入模块而非函数：单测要用 monkeypatch.setattr(llm, "get_chat_model", ...) 把模型换成假的，
# 写成 from gyt.core.llm import get_chat_model 的话名字会在导入时绑死，打桩就失效了。
from gyt.core import llm
from gyt.core.uploads import ingest_uploads

# —— 模块级常量：禁止在函数体里散落字面量 ——

SUPERVISOR_NAME = "supervisor"
"""调度节点在图里的节点名。它会出现在 chat-ui 的执行轨迹上，也是测试断言的依据。"""

GRAPH_NAME = "gyt"
"""编译后整张图的名字，与 langgraph.json 里的 graph id 保持一致。"""

OUTPUT_MODE = "last_message"
"""子 Agent 结果如何回灌给 supervisor。

"last_message"：只把子 Agent 的最后一条消息带回来（工具调用的中间过程不回灌）。
选它而不是 "full_history"，是因为工地场景里子 Agent 的中间步骤（检索到的 20 段原文、
DXF 里的几百个实体）对调度决策没用，全带回来只会把 supervisor 的上下文撑爆、烧掉额度。
"""


INSPECTION_AGENT_NAME = "inspection"
"""巡检英雄链在图里的名字。它同时是交接工具名(transfer_to_inspection)与
路由评测 expected_agent 的合法取值之一(scorers.ROUTING_AGENTS,已同步)。"""


def build_inspection_chain() -> CompiledStateGraph:
    """组装「巡检英雄链」:safety ──硬边──► report,编译成一个可当 Agent 挂的子图。

        START ──► safety(看照片,判隐患)──► report(渲染巡检记录 docx)──► END

    为什么必须是**写死的图边**而不是让 Supervisor 决策两次(方案 D 决策原文):
    「拍照识违规 → 自动出巡检报告」是演示主线,交给 LLM 判断第二跳,
    演示当场就会随机漏掉出报告那一环。硬边把这个环节从概率变成必然。

    数据在两环之间怎么保真:report 的工具**不读** safety 的话,而是拿同一个
    照片编号重新调 analyze(提示词已冻结 → 缓存必中 → 与 safety 的判断
    逐字节一致,零成本)。LLM 在链里只搬运编号。详见 agents/report/tools.py 顶部。

    两个子 Agent 都是已编译的 Pregel,直接当节点挂;状态走 MessagesState,
    messages 键靠 add_messages 归并,与 create_agent 的 AgentState 兼容。
    """
    builder = StateGraph(MessagesState)
    builder.add_node(SAFETY_AGENT_NAME, build_safety_agent())
    builder.add_node("report", build_report_agent())
    builder.add_edge(START, SAFETY_AGENT_NAME)
    builder.add_edge(SAFETY_AGENT_NAME, "report")  # ← 英雄链的那条硬边
    builder.add_edge("report", END)
    return builder.compile(name=INSPECTION_AGENT_NAME)


class AgentSpec(NamedTuple):
    """一个子 Agent 的登记项。NamedTuple = 天然不可变，杜绝运行期被人改掉。

    字段:
        name:    子 Agent 名，必须和它自己 compile 时的 name 一致，
                 否则 create_supervisor 生成的交接工具会指向一个不存在的节点。
        summary: 一句中文能力说明，会被拼进 supervisor 的系统提示词，
                 决定「这活该派给谁」。写得越具体，路由准确率越高（D18 评测门槛 0.90 靠它）。
        build:   无参构造函数，返回已编译的子 Agent。
    """

    name: str
    summary: str
    build: Callable[[], CompiledStateGraph]


AGENT_REGISTRY: tuple[AgentSpec, ...] = (
    # ⚠️ ping(T1 连通性探针)已于 2026-08-08 从登记表**摘除**,包本身保留(工具写法样板)。
    # 摘除依据是路由基线的三连实锤:它的正域是空的,于是 summary 里每个词都成了钩子 ——
    #   第一版「看看通不通」→「看看柱距」「看一下这个」被钓来;
    #   第二版否定句「带照片/图纸/任务的不派」→ 三条图纸请求全被钓来
    #     (「不要 X」里的 X 照样吸引词法匹配);
    #   第三版砍到只剩「自检」→ 图纸请求和模糊请求**还是**被钓来。
    # 三轮措辞收不住,说明这不是措辞问题:空正域条目不该占路由位。
    # 要测连通性,直接问 supervisor「系统通不通」由它自答即可。
    AgentSpec(
        name=SAFETY_AGENT_NAME,
        summary=(
            "工地照片安全检查。用户发来现场照片时派给它，它能看出照片里有没有"
            "未戴安全帽、未穿反光衣、高空作业未系安全带、临边无防护、消防通道堵塞、"
            "材料堆放混乱、用电隐患、动火作业无监护这八类问题，也能认出照片根本不是工地。"
            "**不管用户有没有给出照片编号**，只要是在问现场照片里有没有问题就派给它；"
            "编号缺失由它自己向用户追问。它**只**看照片,不出文档、不回答规范条文 ——"
            "用户要「出巡检记录」「留档」时别派它,派 inspection。"
            # 「并给了照片编号」这个合取条件曾经写在这里，是错的:routing.csv 里
            # expected_agent=safety 的行(「这张照片有没有安全隐患」等)原文里都没有编号，
            # supervisor 会把它读成路由前提，于是自己反问要编号、不生成 transfer_to_safety。
            # 缺编号的追问本来就归 safety/prompt.md 管，supervisor 不该在路由层重复把关。
        ),
        build=build_safety_agent,
    ),
    AgentSpec(
        name=INSPECTION_AGENT_NAME,
        summary=(
            "巡检出记录(一条龙):先看照片查隐患,然后**自动**生成一份可存档的"
            "巡检记录文档(Word),不用再单独交代。用户说「巡检」「出个记录」"
            "「出报告」「留档」「检查完给我份文件」时派它。"
            "只想看看照片有没有问题、不要文档的,派 safety。"
            "**不管用户有没有给出照片编号**,只要是「查照片并要文档」就派它,"
            "编号缺失由它自己向用户追问。"
            "它和 safety 一样**只看照片**,不回答规范条文、标准数值该是多少 ——"
            "问「多高才合规」「规范怎么要求」是条文问题,不归它。"
            # 路由基线实测(2026-08-08):「临边防护栏杆得多高才合规」被派到 inspection ——
            # 规范问题撞上「临边」关键词。safety 的 summary 一直带条文免责句,这里此前漏了。
        ),
        build=build_inspection_chain,
    ),
    AgentSpec(
        name=SCHEDULE_AGENT_NAME,
        summary=(
            "工地任务台账:记任务、改期限、销任务、查「某天之前还有啥没干完」。"
            "用户说「记一下/建个任务」「XX改到周五」「XX干完了」"
            "「下周三之前还有哪些任务」这类**安排活儿和期限**的话,派给它。"
            "它只管任务台账,不看照片、不答规范条文。"
            # 「安排活儿和期限」是这条的正域锚点:schedule 的正域天然饱满
            # (记/查/改/销四类都有高频口语说法),不必像 ping 那样靠空泛词占位。
            # 结尾免责句与 safety/inspection 同款 —— 防「验收」「复检」这类词
            # 把照片/条文请求钓过来(路由回归时若 R06 被钓走,先查这条的措辞)。
        ),
        build=build_schedule_agent,
    ),
    AgentSpec(
        name=CAD_AGENT_NAME,
        summary=(
            "看 DXF 图纸:查图纸上标注的尺寸、数构件在哪个图层、列图层清单、出图纸 PNG 预览。"
            "用户问「首层平面图有哪些图层」「这道梁标注多长」「KZ1 在哪层」"
            "「看看结构图」「打开某张图看柱距」这类**看图纸**的话,派给它。"
            "它只看图纸,不看现场照片、不答规范条文该是多少 —— "
            "「柱距多少才合规」是条文问题,不归它。"
            # 正域写足(尺寸/构件/图层/预览四类的口语说法),结尾带同款条文免责句。
            # routing R15-R17 的 expected_agent 已是 cad,这条一上线就把误派/空派归位。
            # ⚠️ 别去改 safety 的 summary 救 R17(TODO-23 明令禁止);若演示图没标柱距,
            #    query_dimension 会如实说做不了 —— 路由对了、能力边界也诚实,可接受。
        ),
        build=build_cad_agent,
    ),
    AgentSpec(
        name=KNOWLEDGE_AGENT_NAME,
        summary=(
            "查施工规范条文:消防/防火/安全/施工的规范要求是什么、数值是多少、要办什么手续。"
            "用户问「消防车道要多宽」「疏散距离怎么要求」「防火分区最大多少平米」"
            "「XX 规范上怎么规定的」「XX 合规标准是多少」这类**规范条文/标准数值**的话,派给它。"
            "它答的是**规范里写了什么**,并给出处页码;查不到会如实说没有。"
            "它只查条文,不看现场照片、不看图纸、不排期 —— "
            "「这张照片合不合规」是看照片的活、「这张图尺寸多少」是看图纸的活,不归它。"
            # routing R08-R11 的 expected_agent 已是 knowledge,这条一上线就把这几条空派归位。
            # 正域写足(消防/防火/安全/施工 + 数值/程序/标准的口语说法),结尾带同款免责句。
        ),
        build=build_knowledge_agent,
    ),
    AgentSpec(
        name=ATTENDANCE_AGENT_NAME,
        summary=(
            "查打卡考勤(只读):某段时间每人出勤几天、打了几次卡,某天谁到了、几点打的。"
            "用户问「张三这个月来了几天」「今天谁到了」「李四昨天几点打的卡」"
            "「上周都谁来过」这类**打卡记录/出勤天数**的话,派给它。"
            "有人想「打卡/补卡」也派它 —— 它会告知打卡要在界面上点按钮,不会替人打卡。"
            "它只查打卡台账,不记任务、不排期 ——「给谁排个活」「改期限」是任务台账的活;"
            "它也不看照片、不答规范条文。"
            # W7(2026-08-15)落地。打卡**写入**是直连接口(src/gyt/checkin_api.py,D15),
            # 不经过任何 Agent —— 这里只有查询一半。正域锚在「打卡/出勤/来了几天/谁到了」,
            # 不与 schedule 的「安排活儿和期限」抢词;「想打卡也派它」是刻意的:
            # 让「请点界面上的打卡按钮」这句标准答复出自 prompt.md,而不是 supervisor 现编。
        ),
        build=build_attendance_agent,
    ),
    # W2/W3 在这里往下追加，一个 Agent 一行。改这里就等于改路由能力，
    # 记得同步更新 D18 的路由评测集（backend/eval/datasets/routing.csv，
    # 跑分入口 backend/eval/runner.py，`make eval SUITE=routing`），别让门槛失守。
)
"""当前挂在 supervisor 下面的全部子 Agent。顺序 = 提示词里名单的顺序，不影响功能。"""


_SUPERVISOR_PROMPT_TEMPLATE = """\
你是「工友通」的调度中枢，服务对象是建筑工地上的班组长和一线工人。

# 你的职责边界（最重要，先看这条）

你**只做两件事**：① 判断这活该派给哪位同事；② 把同事干完的结果汇总成人话回给用户。
你**自己不干活**：不要凭自己的知识回答规范条文、不要自己判断照片里有没有违规、
不要自己编任务清单、不要自己算图纸尺寸。这些都必须派给对应的同事去做。
你脑子里记的东西可能是过时的，工地上照着过时信息干活是要出人命的。

# 你手下的同事

{roster}

# 派活规则

1. 一次只派给一位同事。等他把结果交回来，再决定是继续派下一位，还是直接答复用户。
2. 用户一句话里有多件事（比如「这张照片有没有问题，顺便出个日报」），
   就拆开、按顺序一位一位派，不要一次全撒出去。
3. 如果没有哪位同事能干这活，**如实告诉用户「这个我们暂时做不了」**，
   并说清目前能做什么。绝对不许自己硬答一个看起来像模像样的答案。
4. 如果用户**意图不明**（比如只说「看一下这个」「那个处理一下」，根本不知道他要干什么），
   就用一句短话反问清楚，别猜。**反问也是你自己处理**——
   不要为了"总得派个人"而随便挑一位同事把模糊请求塞过去。
   注意：「意图清楚、只是材料还没发来」**不算模糊**——比如他要查照片里有没有戴安全帽、
   照片却还没发，这就照派管照片的同事，照片让同事自己向用户要（谁管要材料，
   同事名单里都写了）。
5. 你说出口的每一句话都是**直接讲给用户听的**，不是自言自语的盘算。
   决定反问，就把问题本身写出来；决定「做不了」，就把做不了和现在能做什么写出来——
   这两种情况写完就停，**严禁再调任何交接工具**。嘴上说着「派不下去/做不了」
   手上却发了交接，系统只认你的手，结果就是派错人。

# 汇报规则（红线，违反会出安全事故）

1. 同事返回的结果里如果 `ok` 是 false，**必须如实告诉用户这次失败了**，
   把 `user_msg` 原样转述给用户。**严禁**编造成功、严禁假装拿到了结果、
   严禁把失败说成「大概是……」然后自己补一个答案。
2. 同事查不到东西（空结果）就说查不到，让用户换个说法或补充条件。
   不许用你自己的知识去填这个空。
3. 引用规范条文、图纸数据、任务记录时，只能照抄同事给回来的内容，一个字都不要改，
   编号和页码尤其不许自己「顺手补全」。
   同事记任务/改期/销项的回执，转述时**任务号（T几）和日期必须一起带上**，
   照抄他的写法——用户要拿着 T 号跟工友对活，你把号吞了他就对不上了。
4. 同事的结果里带**表格**时，那张表格用户在上面**已经看到了**——你只补一两句短话
   （一句结论，或者下一步怎么办），**严禁**把表格或清单内容重抄一遍，
   也不要逐条复述表格里的行。编号、日期以表格里的为准，你的短话里别再报数字。

# 说话方式

用简体中文。说人话，句子短，别用书面语和专业术语绕。
面对的是戴着安全帽、可能在噪音里看手机的师傅，一句话讲不明白就分成两句讲。
"""


def render_roster(specs: Sequence[AgentSpec] = AGENT_REGISTRY) -> str:
    """把子 Agent 名单渲染成提示词里的一段列表。

    单独抽出来是为了「名单只有一份真相」：加 Agent 只改 AGENT_REGISTRY，
    提示词自动跟着变，不会出现「代码里挂了 6 个、提示词里只写了 4 个」这种经典事故。
    """
    return "\n".join(f"- **{spec.name}**：{spec.summary}" for spec in specs)


def build_supervisor_prompt(specs: Sequence[AgentSpec] = AGENT_REGISTRY) -> str:
    """拼出 supervisor 的**静态**中文系统提示词（纯函数，不碰任何全局状态）。

    「当前工地」这类每轮都会变的现场信息不在这里 —— 它随 config 逐轮变化，
    由 build_supervisor_prompt_runnable 在调模型前动态续到这段后面。
    """
    return _SUPERVISOR_PROMPT_TEMPLATE.format(roster=render_roster(specs))


def render_current_project(config: RunnableConfig | None) -> str:
    """把「用户此刻在顶栏选中的工地」渲染成一段提示词，逐轮拼到静态提示后面。

    为什么要有这段(修的就是「图纸同事说得出当前工地、supervisor 却说查不到」这个矛盾):
    「当前工地」只存在 config.configurable[gyt_project_id] 里,原本**只有** cad / knowledge
    的工具去读它、并能从库里查出工地名;supervisor 的提示词是静态的、从来拿不到它,
    于是被直接问「当前项目是不是 X」时,它既没有这条现场信息、又没有哪位同事专管
    「报当前工地名」,只能按「做不了、不许瞎猜」如实回绝 —— 而同一套系统里图纸同事
    却张口就报得出工地名。这段就是把这条现场信息也交到 supervisor 手上,让它对
    「确认工地本身」的问题能直接答,和子 Agent 看到的是同一个「当前工地」。

    工地名从库里现查(get_project),与 cad / knowledge 走的是同一份真相;查不到 / 没选
    都给一句能指导用户下一步的话,绝不编一个工地名。
    """
    project_id = project_from_config(config)
    if not project_id:
        return (
            "# 当前工地\n\n"
            "用户还没在顶栏选中「当前工地」。凡是要落到某个工地的具体数据"
            "(图纸、规范、任务)时,先提醒他到顶栏选一个工地,别替他猜是哪个。"
        )
    row = get_project(project_id)
    if row is None:
        # 选中的工地在库里查无 —— 多半刚被删。不要报编号里的乱码给师傅,给可操作的话。
        return (
            "# 当前工地\n\n"
            "用户顶栏选中的工地在库里查不到了(可能刚被删)。请提醒他重新到顶栏选一个工地。"
        )
    return (
        "# 当前工地\n\n"
        f"用户现在选中的工地是「{row.name}」。他说「当前工地 / 当前项目」时指的就是它 —— "
        "这是你**已经知道**的现场信息,像「今天几号」一样。\n"
        "所以像「当前项目是不是 X」「现在这个工地叫啥」这种只是**确认工地本身**的问题,"
        "你直接回答就行,不用、也不该派同事去查(没有哪位同事专管报工地名)。\n"
        "但工地里的**具体数据**——有哪些图纸、有哪些任务、规范怎么规定——照旧派给对应同事,"
        "别自己答。"
    )


def build_supervisor_prompt_runnable(
    specs: Sequence[AgentSpec] = AGENT_REGISTRY,
) -> Callable[[dict[str, Any], RunnableConfig], list[Any]]:
    """把「静态提示 + 逐轮的当前工地」组装成 create_supervisor 收的 prompt 可调用体。

    签名必须是 ``(state, config: RunnableConfig)``:langgraph 的 RunnableCallable 按参数名
    + 注解决定要不要把 config 注进来(``config`` 且注解为 RunnableConfig 才注),写漏了
    config 就永远拿不到当前工地。返回「系统消息 + 原 messages」,与库对 prompt 可调用体的约定一致。
    """
    static_prompt = build_supervisor_prompt(specs)

    def supervisor_prompt(state: dict[str, Any], config: RunnableConfig) -> list[Any]:
        system = f"{static_prompt}\n\n{render_current_project(config)}"
        return [SystemMessage(content=system), *state["messages"]]

    return supervisor_prompt


def _validate_registry(specs: Sequence[AgentSpec]) -> None:
    """先在这儿用中文把明显的登记错误拦下来。

    不拦的话，同样的问题会在 create_supervisor 内部炸出一句英文报错，
    队友对着 "Agent with name 'x' already exists" 得先愣三秒才反应过来是登记表写重了。
    """
    if not specs:
        raise ValueError("子 Agent 登记表是空的：Supervisor 手下一个人都没有，建图没有意义。")

    seen: set[str] = set()
    for spec in specs:
        if not spec.name or not spec.name.strip():
            raise ValueError("子 Agent 登记表里有一项名字是空的，请检查 AGENT_REGISTRY。")
        if spec.name in seen:
            raise ValueError(
                f"子 Agent 名字重复：「{spec.name}」在 AGENT_REGISTRY 里出现了不止一次。"
                "名字同时是图节点名和交接工具名，必须唯一。"
            )
        seen.add(spec.name)
        if not spec.summary.strip():
            raise ValueError(
                f"子 Agent「{spec.name}」没写能力说明（summary）。"
                "这句说明是 Supervisor 判断该派给谁的唯一依据，不能留空。"
            )


def build_graph(specs: Sequence[AgentSpec] = AGENT_REGISTRY) -> CompiledStateGraph:
    """组装并编译整张 Supervisor 图。

    流程（对应文件顶部图 1）：

        AGENT_REGISTRY
            │ ① 校验：非空 / 名字不重复 / 说明不为空
            ▼
        逐个 spec.build() → 已编译的子 Agent 列表
            │ ② supervisor 自己的模型：走 text 档（DeepSeek，默认关思考保低延迟）
            ▼
        create_supervisor(...) → 未编译的 StateGraph
            │ ③ .compile(name="gyt")
            ▼
        CompiledStateGraph
            │ ④ .with_config(recursion_limit=N) —— 熔断，防 Supervisor 自己跟自己
            ▼      来回踢皮球把额度烧光；超限时 langgraph 抛 GraphRecursionError
        可直接 invoke / 挂给 langgraph dev 的图

    参数:
        specs: 子 Agent 登记表。默认用模块级 AGENT_REGISTRY；
               测试可以传一个精简的子集进来，不必改全局。

    返回:
        已编译、已带上 recursion_limit 配置的 CompiledStateGraph。

    抛出:
        ValueError: 登记表不合法（空表 / 重名 / 缺说明）。
        MissingAPIKeyError: DeepSeek 的 Key 没配（由 gyt.core.llm 抛出，带中文提示）。
    """
    _validate_registry(specs)
    settings = get_settings()

    # 方案 B 知识库启动预置:开关默认 False(见 config),开了才在起服务时自动建规范索引。
    # import 放进守卫内 —— 关的时候连 knowledge/ingest 都不碰。已建好则秒过(manifest 命中)。
    if settings.knowledge_prebuild_at_startup:
        from gyt.agents.knowledge.ingest import ensure_index_built

        ensure_index_built()

    # 先把子 Agent 一个个造出来。任何一个造不出来（缺 Key、提示词文件丢了）都直接抛，
    # 绝不「跳过坏的、剩下的照常挂」—— 少挂一个 Agent 意味着那类问题会被 supervisor
    # 静默地答不上来，比起动直接失败要难查得多。
    agents = [spec.build() for spec in specs]

    builder = create_supervisor(
        agents=agents,
        model=llm.get_chat_model("text"),
        # 可调用体而非静态字符串:每轮把「当前工地」现查现拼到提示后面(见该函数说明),
        # 修「supervisor 说不出当前工地、子 Agent 却说得出」的矛盾。
        prompt=build_supervisor_prompt_runnable(specs),
        supervisor_name=SUPERVISOR_NAME,
        output_mode=OUTPUT_MODE,
        # ⚠️ 必须保持开启(显式写出来防止有人再"优化"掉)。
        # 2026-08-08 踩过一次大坑:嫌「Transferring back to supervisor」这对消息
        # 是英文装饰、还烧 token,曾把它关掉 —— 结果它其实是 supervisor 的**收工信号**。
        # 关掉后上下文里只剩「Successfully transferred to schedule + 子 Agent 的回复」,
        # 没有「已交回」标记,DeepSeek 会把这读成「交接还在进行」,于是对同一件事
        # **再转一次**,循环到 recursion_limit=8 熔断(记任务这句真机连炸两发,
        # 而查询类问法碰巧都没踩 —— 属于抽样运气,不是没病)。
        # 英文观感问题归前端管:ai.tsx 覆盖件按内容把这对消息折叠成灰行。
        add_handoff_back_messages=True,
        # 聊天界面的「Upload Image」按钮会把图片作为多模态 content 块塞进消息,
        # 而这里的模型是 DeepSeek 文本档 —— 收到 image 块直接 400,
        # 前端还不渲染这个错,用户只看到"点了发送没反应"。
        # 这个钩子在调模型之前把图片存成产物、把消息换成一句带编号的文本,
        # 于是文本档见不到图片,而 Safety 工具拿到的正是它要的 artifact_id。
        # 详见 core/uploads.py 顶部。
        pre_model_hook=ingest_uploads,
    )

    return builder.compile(name=GRAPH_NAME).with_config(
        recursion_limit=settings.supervisor_recursion_limit
    )


# 模块级图实例。必须是真正的模块变量（不能用 PEP 562 惰性化），原因见文件顶部「结论三」。
# 副作用：import gyt.graph 就会立刻建图并要求 API Key —— 这是刻意为之的快速失败，
# 生产上宁可启动时就报「.env 里没填 GYT_DEEPSEEK_API_KEY」，也不要跑到用户发第一条
# 消息时才崩。测试请在打完桩之后再 import 本模块。
graph = build_graph()

__all__ = [
    "AGENT_REGISTRY",
    "GRAPH_NAME",
    "OUTPUT_MODE",
    "SUPERVISOR_NAME",
    "AgentSpec",
    "build_graph",
    "build_supervisor_prompt",
    "build_supervisor_prompt_runnable",
    "graph",
    "render_current_project",
    "render_roster",
]
