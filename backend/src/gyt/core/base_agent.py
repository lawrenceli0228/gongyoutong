"""Agent 薄工厂 —— 只做「读提示词 + 选模型 + 组装」三件事。

设计红线(D12 护栏):这是一层**薄**封装,不许演化成自建 Agent 框架。
凡是 langchain / langgraph 已经提供的能力(中间件、结构化输出、checkpointer),
一律直接用官方参数,不要在这里包一层自己的抽象。

===========================================================================
API 核实结论(2026-08-05 实测,不是凭记忆写的 import)
---------------------------------------------------------------------------
核实方式:从 PyPI 下载 wheel 解包直接读源码 —— 比文档站更权威,文档站有滞后。
    langgraph            1.2.10   (依赖 langgraph-prebuilt >=1.1.0,<1.2.0)
    langgraph-prebuilt   1.1.0
    langchain            1.3.14
    langchain-core       1.5.3

结论一:`langgraph.prebuilt.create_react_agent` **仍然存在但已废弃**。
    langgraph/prebuilt/chat_agent_executor.py 里它带着装饰器
    `@deprecated(..., category=LangGraphDeprecatedSinceV10)`,docstring 原文:
        "This function is deprecated in favor of `create_agent` from the
         `langchain` package, which provides an equivalent agent factory
         with a flexible middleware system."
    所以历史资料里的 `from langgraph.prebuilt import create_react_agent` 不要再抄。

结论二:当前正确入口 = `from langchain.agents import create_agent`。
    实测签名(langchain/agents/factory.py,已剥掉 @overload 只留真实现):
        create_agent(
            model: str | BaseChatModel,
            tools: Sequence[BaseTool | Callable | dict] | None = None,
            *,
            system_prompt: str | SystemMessage | None = None,  # ← 注意不叫 prompt
            middleware: Sequence[AgentMiddleware] = (),
            response_format=None, state_schema=None, context_schema=None,
            checkpointer=None, store=None,
            interrupt_before=None, interrupt_after=None,
            debug: bool = False,
            name: str | None = None,
            cache=None, transformers=None,
        ) -> CompiledStateGraph
    两个必须记住的坑:
        1) 提示词参数叫 `system_prompt`,旧版 create_react_agent 里叫 `prompt`;
        2) 返回值**已经 compile 过**,不要再调 .compile(),可直接当子 Agent 挂给 Supervisor。

结论三:`CompiledStateGraph` 只能从 `langgraph.graph.state` 导入。
    `langgraph/graph/__init__.py` 的 __all__ 只有 END/START/StateGraph/add_messages/
    MessagesState/MessageGraph,没有 CompiledStateGraph。

结论四:create_agent 是**惰性**绑定工具的(工具在请求期由中间件 bind,不在建图期),
    所以单测里的假模型可以不实现 bind_tools。Supervisor 那层不一样,见 graph.py。

结论五(2026-08-06,TODO-5 收口):缓存**不在本文件里**,一行都不用写。
    本工厂交给 create_agent 的仍然是裸 ChatOpenAI,而 create_agent 内部是自己调
    model 的(factory.py:1467 `await model_.ainvoke(...)`)。缓存靠的是 langchain
    的进程级全局缓存(`langchain_core.globals.set_llm_cache`),由
    `llm.get_chat_model()` 在返回模型前顺手装上:

        create_gyt_agent --> llm.get_chat_model() --+-- install_llm_cache()(幂等)
                                                    +-- ChatOpenAI
                                  |
                                  +--> create_agent(model=...)
                                            |
                                            +--> langgraph 内部 model.ainvoke()
                                                      |
                                                      +--> _agenerate_with_cache
                                                            先查 GytDiskCache,命中就 0 次网络
                                                            (流式路径同样先查,实测已钉死)

    所以这里**不要**再包一层自己的模型代理 —— 那正是当初评估后否掉的方案(a),
    理由(流式覆盖、工具串味、bind_tools 形参坑)全部写在 gyt/core/llm.py 顶部
    「图 0」里,动手改之前先读那一段。
    重试仍由 ChatOpenAI 自带的 max_retries 承担(get_chat_model 里已设成配置值)。
    对应的断言测试在 tests/unit/test_llm_cache.py —— 它数的是**内层模型被调了几次**,
    因为这条链路一旦断开是完全静默的(答案照出、测试照绿,只是每次都在花钱)。

来源:
    https://pypi.org/pypi/langchain/json
    https://pypi.org/pypi/langgraph/json
    github.com/langchain-ai/langchain
        → libs/langchain_v1/langchain/agents/factory.py
    https://docs.langchain.com/oss/python/migrate/langgraph-v1
===========================================================================
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

# 用「导入模块」而不是「导入函数」,这样单测 monkeypatch.setattr(llm, "get_chat_model", ...)
# 能真正生效 —— 若写成 from ... import get_chat_model,名字会在导入时被绑死在本模块命名空间里。
from gyt.core import llm
from gyt.core.focus import FocusOnOwnWork
from gyt.core.llm import Purpose

# 每个 Agent 包里提示词文件的固定文件名。提示词一律外置成 .md,不许写死在 .py 里。
PROMPT_FILENAME = "prompt.md"

# langgraph_supervisor 0.0.31 的 supervisor.py:399 会拒绝名字为 None 或 "LangGraph" 的子 Agent
# (那是 StateGraph.compile() 不传 name 时的默认名)。这里提前拦下来,报错更早也更好懂。
RESERVED_AGENT_NAME = "LangGraph"

# 剥 HTML 注释用。DOTALL 让 . 能匹配换行,才吃得下跨行的 <!-- ... --> 注释块。
# 非贪婪 .*? 保证「一段注释配一个结束标记」,不会把两段注释之间的正文一起吃掉。
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

__all__ = [
    "PROMPT_FILENAME",
    "RESERVED_AGENT_NAME",
    "create_gyt_agent",
    "load_prompt",
]


def load_prompt(agent_dir: Path, filename: str = PROMPT_FILENAME) -> str:
    """读取某个 Agent 目录下的提示词文件,剥掉 HTML 注释后返回正文。

    为什么要剥注释:prompt.md 里会写给队友看的维护说明(改动记录、评测集位置、
    "这段话别删,评测集依赖它" 之类)。这些说明对模型是纯噪音,还白烧 token,
    所以约定用 <!-- --> 包起来,加载时统一剥掉。

        prompt.md 文本
            │
            ├─ 文件不存在 ─────────▶ raise FileNotFoundError(中文说明)
            ├─ 不是 UTF-8 ────────▶ raise ValueError(中文说明)
            ▼
        正则剥掉所有 <!-- ... --> 注释块(跨行、可多段)
            │
            ▼
        strip() 去掉首尾空白
            │
            ├─ 剥完是空的 ─────────▶ raise ValueError(空提示词是配置事故,不许静默放过)
            ▼
        返回正文 str

    参数:
        agent_dir: Agent 包所在目录,一般传 Path(__file__).parent。
        filename:  提示词文件名,默认 prompt.md(Agent 本体那份)。
                   传别的名字是给「同一个 Agent 需要不止一份提示词」的场景用的 ——
                   safety 就有两份:prompt.md 给本体(文本档),vision_prompt.md
                   给工具内部那次视觉调用。两份职责不同、模型不同,不能合成一份。
                   ⚠️ 无论几份,都必须外置成 .md,不许写死进 .py ——
                   提示词是要被非工程师队友改的,埋在代码里就改不动了。

    返回:
        剥注释、去首尾空白之后的提示词正文。

    抛出:
        FileNotFoundError: 目录下没有这个文件。
        ValueError: 文件不是 UTF-8,或剥完注释后内容为空。
    """
    prompt_path = Path(agent_dir) / filename
    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"找不到提示词文件:{prompt_path}。每个 Agent 目录下必须有一份 {filename}。"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"提示词文件不是 UTF-8 编码:{prompt_path}。请用 UTF-8 重新保存(注意别存成 GBK)。"
        ) from exc

    body = _HTML_COMMENT_RE.sub("", raw).strip()
    if not body:
        raise ValueError(
            f"提示词文件剥掉 HTML 注释后是空的:{prompt_path}。"
            "空提示词会让 Agent 行为完全不可控,这里直接拦下。"
        )
    return body


def create_gyt_agent(
    *,
    name: str,
    prompt: str,
    tools: list[BaseTool | Callable[..., Any]],
    purpose: Purpose = "text",
    extra_middleware: Sequence[AgentMiddleware] = (),
) -> CompiledStateGraph:
    """组装一个工友通子 Agent(已编译,可直接挂给 Supervisor)。

    参数:
        name: 子 Agent 名字。必须唯一且稳定 —— 它同时是 Supervisor 图里的节点名、
              交接工具名(transfer_to_<name>)和 chat-ui 轨迹上显示的名字,改名等于改 API。
        prompt: 系统提示词正文,通常来自 load_prompt()。
        tools: 该 Agent 的工具列表。函数内部会复制一份,不会原地修改调用方传进来的列表。
        purpose: 选哪一档模型 —— "text" 走 DeepSeek,"vision"/"tool" 走 Kimi。
                 具体型号全部由 gyt.config 决定,这里一个模型名都不写死。

    返回:
        已 compile 的 CompiledStateGraph。

    抛出:
        ValueError: name 为空/是保留名,或 prompt 为空白。
        MissingAPIKeyError: 对应供应商的 API Key 没配(由 gyt.core.llm 抛出)。
    """
    _validate_agent_name(name)
    if not prompt.strip():
        raise ValueError(f"Agent「{name}」的提示词是空的,拒绝创建。请检查 {PROMPT_FILENAME}。")

    model = llm.get_chat_model(purpose)
    # list(tools) 复制一份:遵守不可变约定,防止 create_agent 内部或未来的中间件
    # 回头改到调用方那个模块级常量(例如 ping 包里的 PING_TOOLS)。
    return create_agent(
        model=model,
        tools=list(tools),
        system_prompt=prompt,
        name=name,
        # 子 Agent 与 Supervisor 共享同一份 messages,于是它能看到 Supervisor
        # 说过的每一句话 —— 包括「我这就安排 safety 同事看一下」。实测子 Agent
        # 会跟着模仿那个语气、把活推回去而不干,两边互相等,用户永远收不到答复。
        # 这道中间件在**喂给模型之前**把调度中枢的派活腔滤掉(不改 state),
        # 让它只看见「用户要什么」「同事查到什么」和「自己做过什么」。详见 core/focus.py。
        # extra_middleware 追加在 Focus **之后**(= 内层):先裁上下文,再做各 Agent
        # 自己的行为守卫(例如 schedule 的 RequireLedgerTool 防假账件)。
        middleware=[FocusOnOwnWork(name), *extra_middleware],
    )


def _validate_agent_name(name: str) -> None:
    """校验子 Agent 名字。在这里拦,比等到 create_supervisor 报英文错更好排查。"""
    if not name or not name.strip():
        raise ValueError("Agent 名字不能为空:它要当图节点名和交接工具名用。")
    if name == RESERVED_AGENT_NAME:
        raise ValueError(
            f"Agent 名字不能叫「{RESERVED_AGENT_NAME}」,那是 LangGraph 的默认占位名,"
            "langgraph_supervisor 会直接拒绝。请换一个有业务含义的名字。"
        )
