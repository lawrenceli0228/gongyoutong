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

图 1：T1 当前拓扑（本文件此刻真正构建出来的东西）

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

图 2：W2 / W3 挂载位（往 AGENT_REGISTRY 追加一行即可，本文件其它地方不用动）

                              supervisor
                                  │
      ┌──────────┬────────────┬───┴────────┬──────────┬──────────┐
      ▼          ▼            ▼            ▼          ▼          ▼
    ping     knowledge     schedule      safety      cad      report
    (T1)     └── X 泳道 W2 ──┘            └───── Y 泳道 W2/W3 ─────┘
             规范检索+页码    SQLite 任务    kimi 识图   DXF 查询   docx 落盘

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
from typing import NamedTuple

from langgraph.graph.state import CompiledStateGraph
from langgraph_supervisor import create_supervisor

from gyt.agents.ping import PING_AGENT_NAME, build_ping_agent
from gyt.config import get_settings

# 导入模块而非函数：单测要用 monkeypatch.setattr(llm, "get_chat_model", ...) 把模型换成假的，
# 写成 from gyt.core.llm import get_chat_model 的话名字会在导入时绑死，打桩就失效了。
from gyt.core import llm

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
    AgentSpec(
        name=PING_AGENT_NAME,
        summary=(
            "连通性自检。把用户说的话原样回显一遍，用来确认系统是否正常。"
            "只有当用户明确要求「测试」「ping」「看看通不通」时才派给它，别的活它一概不会。"
        ),
        build=build_ping_agent,
    ),
    # W2/W3 在这里往下追加，一个 Agent 一行。改这里就等于改路由能力，
    # 记得同步更新 D18 的路由评测集（backend/evals/routing/），别让门槛失守。
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
4. 如果用户说得太模糊、派不下去（比如只说「看看这个」却没给照片），
   就用一句短话反问清楚，别猜。

# 汇报规则（红线，违反会出安全事故）

1. 同事返回的结果里如果 `ok` 是 false，**必须如实告诉用户这次失败了**，
   把 `user_msg` 原样转述给用户。**严禁**编造成功、严禁假装拿到了结果、
   严禁把失败说成「大概是……」然后自己补一个答案。
2. 同事查不到东西（空结果）就说查不到，让用户换个说法或补充条件。
   不许用你自己的知识去填这个空。
3. 引用规范条文、图纸数据、任务记录时，只能照抄同事给回来的内容，一个字都不要改，
   编号和页码尤其不许自己「顺手补全」。

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
    """拼出 supervisor 的中文系统提示词（纯函数，不碰任何全局状态）。"""
    return _SUPERVISOR_PROMPT_TEMPLATE.format(roster=render_roster(specs))


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

    # 先把子 Agent 一个个造出来。任何一个造不出来（缺 Key、提示词文件丢了）都直接抛，
    # 绝不「跳过坏的、剩下的照常挂」—— 少挂一个 Agent 意味着那类问题会被 supervisor
    # 静默地答不上来，比起动直接失败要难查得多。
    agents = [spec.build() for spec in specs]

    builder = create_supervisor(
        agents=agents,
        model=llm.get_chat_model("text"),
        prompt=build_supervisor_prompt(specs),
        supervisor_name=SUPERVISOR_NAME,
        output_mode=OUTPUT_MODE,
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
    "graph",
    "render_roster",
]
