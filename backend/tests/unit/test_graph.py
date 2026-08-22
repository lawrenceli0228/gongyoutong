"""gyt.graph 与 ping 占位 Agent 的单元测试 —— Supervisor 骨架连通性。

红线：本文件不允许发生任何真实网络调用。supervisor 与子 Agent 的模型统一换成
test_base_agent.FakeChatModel，凭据由 conftest 的 _isolated_settings 注入假 Key。

⚠️ 为什么这里不在文件顶部 `import gyt.graph`：
    gyt/graph.py 有意在模块级就执行 `graph = build_graph()`（原因见该文件顶部
    「结论三」：langgraph-api 取图用的是 module.__dict__[...] 而不是 getattr，
    没法做惰性）。也就是说**一 import 就会真的去造模型、真的要 API Key**。
    而 conftest 里注入假 Key 的 _isolated_settings 是「每个用例执行前」才生效的，
    收集（collection）阶段还没跑。所以顶部 import 会在没有 .env 的 CI 上
    直接把整个测试文件的收集搞挂。
    正确姿势：一律通过下面的 graph_module fixture 拿模块 —— 它先打桩、再 import。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any, NamedTuple

import pytest

from gyt.agents.ping import PING_AGENT_NAME
from gyt.agents.ping import tools as ping_tools
from gyt.agents.schedule import SCHEDULE_AGENT_NAME
from gyt.config import get_settings
from gyt.core import llm
from gyt.core.errors import ErrorCode

# —— 常量 ——

GRAPH_MODULE_NAME = "gyt.graph"
ENVELOPE_KEYS = {"ok", "data", "user_msg", "error_code"}
LANGGRAPH_DEFAULT_RECURSION_LIMIT = 10007
"""langgraph 1.2.10 自带的默认值（_internal/_config.py:32）。

断言我们的值 != 它，才能证明「熔断真的收紧了」，而不是碰巧读到了库的默认值。
"""


class GraphFixture(NamedTuple):
    """graph_module fixture 的返回值。"""

    module: ModuleType
    purposes: list[str]  # get_chat_model 被调用时传的 purpose，按调用顺序


def _load_graph_module_fresh() -> ModuleType:
    """在当前打桩状态下重新执行一遍 gyt.graph 的模块体。

    已经在 sys.modules 里就 reload（重新执行模块体 → 用当前的桩重建 graph），
    否则首次 import。两条路径都保证模块级 `graph` 是用假模型造出来的。
    """
    existing = sys.modules.get(GRAPH_MODULE_NAME)
    if existing is not None:
        return importlib.reload(existing)
    return importlib.import_module(GRAPH_MODULE_NAME)


@pytest.fixture
def graph_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[GraphFixture]:
    """打桩换掉模型 → 加载 gyt.graph → 用例结束把模块从 sys.modules 摘干净。

    摘干净这一步不能省：模块级 `graph` 会一直攥着假模型，留在 sys.modules 里
    会污染后面的用例（尤其是那些想验证「不同配置下重新建图」的用例）。
    """
    # FakeChatModel 复用 test_base_agent 里那一份，不再抄第二遍（DRY）。
    # 之所以不放 conftest.py：那是 L6 泳道的文件，T1 并行期不跨泳道改文件；
    # 之所以在函数体里 import：tests 包不在 ruff 的 src 根下，放文件顶部会让
    # isort 把它归成第三方、和 gyt 的导入块打架。等 L6 把它挪进 conftest 就能删掉这行。
    from tests.unit.test_base_agent import FakeChatModel

    model = FakeChatModel()
    purposes: list[str] = []

    def _fake_get_chat_model(purpose: str = "text", **overrides: Any) -> FakeChatModel:
        purposes.append(purpose)
        return model

    monkeypatch.setattr(llm, "get_chat_model", _fake_get_chat_model)
    module = _load_graph_module_fresh()
    yield GraphFixture(module=module, purposes=purposes)
    sys.modules.pop(GRAPH_MODULE_NAME, None)


# ===========================================================================
# 模块级 graph：langgraph.json 的部署契约
# ===========================================================================


def test_模块级_graph_必须真实存在于_module_dict_中(graph_module: GraphFixture) -> None:
    # Assert：langgraph-api 0.12.0 取图用的是 module.__dict__[变量名]（graph.py:772），
    #         不是 getattr —— 所以任何惰性化（PEP 562 的模块 __getattr__）都会让
    #         `langgraph dev` 直接 KeyError 起不来。这条断言就是钉死这个契约。
    assert "graph" in graph_module.module.__dict__


def test_模块级_graph_是已编译的图且名字与_langgraph_json_一致(
    graph_module: GraphFixture,
) -> None:
    from langgraph.graph.state import CompiledStateGraph

    graph = graph_module.module.graph

    assert isinstance(graph, CompiledStateGraph)
    assert graph.name == graph_module.module.GRAPH_NAME


# ===========================================================================
# 拓扑：Supervisor -> 子 Agent
# ===========================================================================


def test_图里有supervisor_且ping已被摘除(graph_module: GraphFixture) -> None:
    """ping 于 2026-08-08 从登记表摘除(路由基线三连实锤:空正域条目的 summary
    每个词都是钩子,图纸/模糊请求接连被钓去回声探针)。这条测试锁住摘除本身 ——
    谁把它挂回来,先去读 graph.py 登记表里的摘除说明再动手。"""
    node_names = set(graph_module.module.graph.nodes)

    assert graph_module.module.SUPERVISOR_NAME in node_names
    assert PING_AGENT_NAME not in node_names


def test_schedule已挂上登记表(graph_module: GraphFixture) -> None:
    """W3 台账泳道的接线锁:routing.csv 的 R12~R14 三条以 schedule 为标准答案,
    谁把它从登记表摘掉,路由分数会**静默**掉回 12/22 —— 这条让摘除在单测阶段就红。"""
    node_names = set(graph_module.module.graph.nodes)
    registry_names = {spec.name for spec in graph_module.module.AGENT_REGISTRY}

    assert SCHEDULE_AGENT_NAME in registry_names
    assert SCHEDULE_AGENT_NAME in node_names


def test_登记表里的每个名字都能在图里找到对应节点(graph_module: GraphFixture) -> None:
    # 这条防的是「登记表改了名、Agent 自己没改」这类会让交接工具指向空节点的漂移
    node_names = set(graph_module.module.graph.nodes)
    registry_names = {spec.name for spec in graph_module.module.AGENT_REGISTRY}

    assert registry_names <= node_names


def test_登记表里的名字与_Agent_编译后的名字一致(graph_module: GraphFixture) -> None:
    for spec in graph_module.module.AGENT_REGISTRY:
        agent = spec.build()
        assert agent.name == spec.name, (
            f"登记表写的是「{spec.name}」，实际编译出来是「{agent.name}」"
        )


def test_建图期只取_text_档模型(graph_module: GraphFixture) -> None:
    """每个子 Agent 各取一次模型，supervisor 自己再取一次，**全部**走 text 档。

    两层意思，都要锁住：
      · supervisor 走 text（DeepSeek，默认关思考保低延迟）；
      · **建图期不许出现 vision** —— safety 的视觉调用发生在工具内部、
        真的要看图的那一刻，不在建图期。这里一旦冒出 vision，说明有人
        把某个 Agent 的 purpose 写成了视觉档，那会让每一轮对话都按
        $3/M 计价（text 档是 $0.14/M），而且是静默的。

    刻意不写死数字:写死的话每加一个 Agent 这条就红,改起来的人只会把数字 +1,
    久而久之没人记得它本来要守的是什么。也不再假设「一个登记项 = 一个模型」——
    inspection(英雄链)是复合 Agent,内部装着 safety+report 两个模型,
    所以只锁两件真正要守的事:总数不少于「登记项 + supervisor」,且**一个 vision 都没有**。
    """
    minimum = len(graph_module.module.AGENT_REGISTRY) + 1  # +1 是 supervisor 自己
    assert len(graph_module.purposes) >= minimum
    assert set(graph_module.purposes) == {"text"}, (
        "建图期出现了非 text 档,谁把 purpose 写成视觉档了?"
    )


# ===========================================================================
# recursion_limit 熔断
# ===========================================================================


def test_recursion_limit_取自配置而不是硬编码(graph_module: GraphFixture) -> None:
    expected = get_settings().supervisor_recursion_limit
    config = graph_module.module.graph.config or {}

    assert config.get("recursion_limit") == expected
    # 证明真的收紧了，而不是碰巧读到 langgraph 自带的默认值
    assert expected != LANGGRAPH_DEFAULT_RECURSION_LIMIT


def test_改配置能改出不同的_recursion_limit(
    graph_module: GraphFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange：改环境变量并清缓存，模拟「运维把熔断调紧成 3」
    tightened = 3
    monkeypatch.setenv("GYT_SUPERVISOR_RECURSION_LIMIT", str(tightened))
    get_settings.cache_clear()

    # Act
    rebuilt = graph_module.module.build_graph()

    # Assert：值确实跟着配置走（若代码里写死了数字，这条必红）
    assert (rebuilt.config or {}).get("recursion_limit") == tightened


# ===========================================================================
# 登记表校验
# ===========================================================================


def test_build_graph_拒绝空登记表(graph_module: GraphFixture) -> None:
    with pytest.raises(ValueError) as excinfo:
        graph_module.module.build_graph(specs=())
    assert "空" in str(excinfo.value)


def test_build_graph_拒绝重名的子_agent(graph_module: GraphFixture) -> None:
    # Arrange：同一个 spec 登记两次
    spec = graph_module.module.AGENT_REGISTRY[0]

    # Act / Assert：名字同时是节点名和交接工具名，重了必须当场报中文错
    with pytest.raises(ValueError) as excinfo:
        graph_module.module.build_graph(specs=(spec, spec))
    assert "重复" in str(excinfo.value)


@pytest.mark.parametrize(
    ("bad_field", "bad_value", "keyword"),
    [
        ("name", "", "名字"),
        ("name", "   ", "名字"),
        ("summary", "   ", "说明"),
    ],
)
def test_build_graph_拒绝残缺的登记项(
    graph_module: GraphFixture, bad_field: str, bad_value: str, keyword: str
) -> None:
    # Arrange：NamedTuple._replace 返回新对象，不改原登记表（不可变原则）
    broken = graph_module.module.AGENT_REGISTRY[0]._replace(**{bad_field: bad_value})

    # Act / Assert
    with pytest.raises(ValueError) as excinfo:
        graph_module.module.build_graph(specs=(broken,))
    assert keyword in str(excinfo.value)


def test_build_graph_每次都返回新的图对象(graph_module: GraphFixture) -> None:
    first = graph_module.module.build_graph()
    second = graph_module.module.build_graph()

    assert first is not second


# ===========================================================================
# Supervisor 提示词：红线不能被改没了
# ===========================================================================


def test_supervisor_提示词写清了只调度不干活(graph_module: GraphFixture) -> None:
    prompt = graph_module.module.build_supervisor_prompt()

    assert "自己不干活" in prompt


def test_supervisor_提示词要求如实转述失败且禁止编造(
    graph_module: GraphFixture,
) -> None:
    prompt = graph_module.module.build_supervisor_prompt()

    # D18 的硬要求：工具报错必须如实说，编造成功在工地上是要出安全事故的
    assert "如实" in prompt
    assert "编造" in prompt


def test_supervisor_提示词交代了切换工地要走switch_project工具(
    graph_module: GraphFixture,
) -> None:
    # 切换工地是 supervisor 自己用工具做的真动作,提示词必须点名 switch_project ——
    # 否则模型会用嘴回一句「已切换」而不真切(前端 ProjectSwitchSync 收不到信号,选中工地不变)。
    prompt = graph_module.module.build_supervisor_prompt()

    assert "switch_project" in prompt
    assert "切换工地" in prompt


def test_supervisor_提示词里的名单跟着登记表自动变(graph_module: GraphFixture) -> None:
    # Arrange：造一个假登记项，验证名单不是写死的
    fake_spec = graph_module.module.AGENT_REGISTRY[0]._replace(
        name="tunnel", summary="隧道断面复核"
    )

    # Act
    prompt = graph_module.module.build_supervisor_prompt(specs=(fake_spec,))

    # Assert
    assert "tunnel" in prompt
    assert "隧道断面复核" in prompt
    assert PING_AGENT_NAME not in prompt


def test_render_roster_每个登记项一行(graph_module: GraphFixture) -> None:
    specs = graph_module.module.AGENT_REGISTRY
    roster = graph_module.module.render_roster(specs)

    assert len(roster.splitlines()) == len(specs)
    for spec in specs:
        assert spec.name in roster
        assert spec.summary in roster


# ===========================================================================
# requires_project：「这活儿离不开当前工地」这条知识必须真的进提示词
#
# 修的是生产上那批「未归属」隐患：工友没在界面上选工地就拍了照，隐患照样进台账、
# 只是 project_id 是空串。⚠️ 那**不是 bug 是 D6 这个有记录的决定**（db/hazards.py）——
# 幂等键 (project_id, photo_sha256, item) 用 NULL 会失效，所以后端不许在写入侧硬拦。
# 唯一能做的就是让 supervisor 提前提醒用户去选工地，这几条守的就是那句提醒。
# ===========================================================================


def test_每个登记项都填了_requires_project(graph_module: GraphFixture) -> None:
    """字段在不在，问 NamedTuple 自己（_fields）—— 不手抄七个 Agent 名。

    手抄的话，加第八个 Agent 时这条照绿，而那恰恰是漏填最可能发生的时刻。
    """
    field = "requires_project"
    spec_cls = graph_module.module.AgentSpec

    assert field in spec_cls._fields
    # 没有默认值 = 必填，漏填会在 import 期当场 TypeError（响的）。
    # 这条守的是「有人图省事给它补个默认值」那种改法 —— 补了之后漏填重新变成静默的：
    # 新同事的活儿离不开工地，提示词里却悄悄没它的名字，而且不会有任何报错。
    assert field not in spec_cls._field_defaults

    for spec in graph_module.module.AGENT_REGISTRY:
        value = getattr(spec, field)
        assert isinstance(value, bool), (
            f"「{spec.name}」的 {field} 填的是 {value!r}，不是 bool —— "
            "None / 空串这类假值会被静默当成 False，提醒名单就少一位同事。"
        )


def test_supervisor_提示词点名了那些离不开工地的同事(graph_module: GraphFixture) -> None:
    """期望值**从登记表现算**：写死七个名字的话，加第八个 Agent 时这条会假绿。"""
    module = graph_module.module
    prompt = module.build_supervisor_prompt()
    notice = module.render_project_notice()

    needs = [spec.name for spec in module.AGENT_REGISTRY if spec.requires_project]
    assert needs, "登记表里一个 requires_project=True 都没有？先回去读那几行旁边的依据注释。"

    # 这一节真的被拼进提示词了，不是拼了个寂寞
    assert notice
    assert notice in prompt

    for name in needs:
        assert name in notice, f"「{name}」的活儿离不开工地，提示词却没点它的名"

    # 不需要工地的不许混进来 —— 混进去 = 每次记任务 / 查考勤都白挨一次提醒，
    # 提醒喊多了就没人听了（这正是 schedule / attendance 标 False 的理由）。
    for spec in module.AGENT_REGISTRY:
        if not spec.requires_project:
            assert spec.name not in notice, f"「{spec.name}」不需要选工地，却出现在提醒名单里"

    # 是给模型看的**可执行指令**,不是一句形容:要说清让它干什么。
    # 🔴 2026-08-22 换过一次口径,别改回去 ——
    #    原来这两条断言是 `"顶栏" in notice` + `"别闷头派活" in notice`,
    #    而真机上模型正是把「别闷头派活」执行成了「**别派活**」:
    #    「给我建个任务」在没选工地时交接记录是(没派活),4/4 确定性复现,
    #    真机验收 A 组十条挂了九条。
    #    现在的口径是「**问 + 摆清单 + 用 switch_project 落实**」。
    assert "问他是哪个工地" in notice
    assert "switch_project" in notice
    assert "清单" in notice
    # ⚠️ **一个「顶栏」都不许有,连否定式的也不行**(「别把他支去点顶栏」也不行):
    #    否定式提及照样是把那个概念植入,而模型对显著名词会照抓不误 ——
    #    这段提示词已经误触过一次。做法是**只说该做什么**,让「顶栏」这个词
    #    在 supervisor 的世界里根本不存在。
    assert "顶栏" not in notice


def test_离不开工地的名单跟着登记表走而不是写死(graph_module: GraphFixture) -> None:
    """造两个假登记项验证名单是现算的：一个要工地、一个不要，只许列前者。"""
    module = graph_module.module
    base = module.AGENT_REGISTRY[0]
    needs = base._replace(name="tunnel", summary="隧道断面复核", requires_project=True)
    free = base._replace(name="weather", summary="报天气", requires_project=False)

    notice = module.render_project_notice((needs, free))

    assert "tunnel" in notice
    assert "weather" not in notice


def test_没有同事需要选工地时那句提醒干脆不出现(graph_module: GraphFixture) -> None:
    """防的是「拼了一句空话进提示词」。

    一个 True 都没有时，那段话对模型**无从执行**：白占 token，还可能被读成
    「工地这事不重要」。所以退化的正确形态是返回空串，不是「暂时没有同事需要选工地」。

    顺带钉住排版：退化后必须与「压根没有这一节」逐字节相同 —— 模板里那个占位符
    独占一行，少留或多留一个换行都会在名单和「# 派活规则」之间留下空档／挤在一起。
    """
    module = graph_module.module
    all_false = tuple(spec._replace(requires_project=False) for spec in module.AGENT_REGISTRY)

    assert module.render_project_notice(all_false) == ""

    prompt = module.build_supervisor_prompt(all_false)
    assert "要先选中工地才做得准" not in prompt
    # 名单还在，且「# 派活规则」紧跟其后，中间不多不少正好一个空行
    assert f"{module.render_roster(all_false)}\n\n# 派活规则" in prompt


# ===========================================================================
# 当前工地：supervisor 也要拿得到「用户此刻选中的工地」
# 修的是「图纸同事说得出当前工地、supervisor 却说查不到」这个矛盾。
# ===========================================================================


def test_当前工地_选中且库里有_supervisor直接报得出工地名(
    graph_module: GraphFixture,
) -> None:
    from gyt.db import projects as db

    db.create_project("gyt-sc", "遂川垃圾处理中心", "SC")

    section = graph_module.module.render_current_project(
        {"configurable": {"gyt_project_id": "gyt-sc"}}
    )

    # 报得出工地名,且明说「确认工地本身」可以直接答、不用派人
    assert "遂川垃圾处理中心" in section
    assert "直接回答" in section
    # 但工地里的具体数据仍要派同事 —— 别把「有哪些图纸」也自己答了
    assert "派给对应同事" in section


def test_当前工地_没选_把工地清单摆出来让用户选_而不是支他去点顶栏(
    graph_module: GraphFixture,
) -> None:
    """🔴 **2026-08-22 真机验收逼出来的改动,别改回去。**

    原来这一段说的是「先提醒他到顶栏选一个工地」,而真机上 supervisor 把它执行成了
    「**先别派活**」—— 4/4 确定性复现:「给我建个任务」在没选工地时交接记录是
    (没派活),真机验收 A 组十条挂了九条。

    现在的做法是**问 + 摆清单**:人在聊天框里,就在聊天框里把事办完。
    """
    from gyt.db import projects as db

    db.create_project("gyt-sc", "遂川垃圾处理中心", "SC")
    db.create_project("gyt-yg", "阳光花园", "YG")

    for config in ({}, {"configurable": {}}, {"configurable": {"gyt_project_id": ""}}, None):
        section = graph_module.module.render_current_project(config)

        # ① 清单真的摆出来了(从库里现查,与 switch_project 同一份真相)
        assert "遂川垃圾处理中心" in section
        assert "阳光花园" in section
        # ② 指的是「问他 + 用 switch_project 落实」,不是「让他去点界面」
        assert "switch_project" in section
        assert "顶栏" not in section, "别再把人支去点顶栏 —— 那正是被真机否掉的做法"
        # ③ 明说认模糊说法 —— resolve_target 本来就认 id/全名/双向子串,
        #    不写这句的话模型会要求用户报全名,而那对工地师傅是多余的门槛
        assert "模糊" in section


def test_当前工地_没选且一个工地都没建_不许问他选哪个(graph_module: GraphFixture) -> None:
    """没得选的时候问「你要哪个工地」是句废话,只会让人卡住。

    措辞与 ``site_switch.switch_project`` 的 "none" 分支刻意对齐 ——
    同一件事在对话里和在工具回执里得是同一个说法。
    """
    section = graph_module.module.render_current_project(None)

    assert "一个工地都还没建" in section
    assert "资料归档" in section
    assert "遂川" not in section, "没选工地时绝不能编一个工地名"


def test_当前工地_选了但库里查无_给可操作的话不报乱码编号(
    graph_module: GraphFixture,
) -> None:
    # 选中的工地已被删:库里查不到。不能把内部编号甩给师傅,要给「重新选」的话。
    from gyt.db import projects as db

    db.create_project("gyt-sc", "遂川垃圾处理中心", "SC")

    section = graph_module.module.render_current_project(
        {"configurable": {"gyt_project_id": "gyt-deleted"}}
    )

    # 与「没选」那一支走同一条路:**摆清单让他挑**,而不是支他去点界面
    # (2026-08-22 改口径,理由见 _render_project_choices)。
    assert "遂川垃圾处理中心" in section
    assert "switch_project" in section
    assert "顶栏" not in section
    # 🔴 内部编号一个字都不许甩给师傅 —— 他既看不懂也没法拿它做任何事
    assert "gyt-deleted" not in section


def test_当前工地_拼在静态提示之后(graph_module: GraphFixture) -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from gyt.db import projects as db

    db.create_project("gyt-sc", "遂川垃圾处理中心", "SC")
    prompt_fn = graph_module.module.build_supervisor_prompt_runnable()

    messages = prompt_fn(
        {"messages": [HumanMessage(content="当前项目是遂川垃圾处理中心吧")]},
        {"configurable": {"gyt_project_id": "gyt-sc"}},
    )

    # 第一条是系统消息,静态红线与动态工地名都在里面;原用户消息原样跟在后面
    system = messages[0]
    assert isinstance(system, SystemMessage)
    assert "自己不干活" in system.content  # 静态红线还在
    assert "遂川垃圾处理中心" in system.content  # 动态工地名接上了
    assert isinstance(messages[-1], HumanMessage)


# ===========================================================================
# ping 的 echo 工具：链路最末端那一环
# ===========================================================================


def test_echo_工具已登记且名字叫_echo() -> None:
    assert [t.name for t in ping_tools.PING_TOOLS] == ["echo"]


def test_echo_工具返回结构合法的成功信封() -> None:
    # Act：走 LangChain 的 invoke 入口，跟 Agent 真正调用它的路径一致
    envelope = ping_tools.echo.invoke({"text": "钢筋绑扎完了"})

    # Assert：四个键一个不少，且各自取值符合契约
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["error_code"] is None
    assert envelope["data"] == {"echo": "钢筋绑扎完了", "length": 6}
    assert "钢筋绑扎完了" in envelope["user_msg"]


def test_echo_工具对空字符串也照样回显() -> None:
    # 能把空串原样传回来，本身就是链路通的证据，不该当成非法输入拦掉
    envelope = ping_tools.echo.invoke({"text": ""})

    assert envelope["ok"] is True
    assert envelope["data"] == {"echo": "", "length": 0}


def test_echo_工具内部炸了也不抛异常而是返回失败信封(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange：把工具内部用到的 ok() 换成必炸的，模拟「工具实现里出了意外」
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("内部细节：/private/path/leak.txt")

    monkeypatch.setattr(ping_tools, "ok", _boom)

    # Act：tool_guard 必须把异常吃掉，Agent 循环不能因为一个工具炸掉就中断
    envelope = ping_tools.echo.invoke({"text": "测试"})

    # Assert
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is False
    assert envelope["error_code"] == ErrorCode.INTERNAL.value
    assert envelope["data"] is None
    # 内部细节只许进日志，绝不能出现在给 LLM / 用户看的文案里
    assert "leak.txt" not in envelope["user_msg"]
    assert envelope["user_msg"]


# ===========================================================================
# ping 提示词：test_graph 依赖的两条硬约束
# ===========================================================================


def test_ping_提示词要求必须调工具且不许编造成功() -> None:
    from gyt.agents.ping import PING_DIR
    from gyt.core.base_agent import load_prompt

    body = load_prompt(PING_DIR)

    assert "必须调用 echo 工具" in body
    assert "禁止" in body
