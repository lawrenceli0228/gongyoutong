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
