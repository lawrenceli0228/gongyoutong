"""ping Agent 的工具集 —— T1 阶段只有一个 echo,用来打通链路。

这个文件同时是「工友通工具函数的写法样板」,W2/W3 的真工具照着这个骨架写:

    @tool(名字, description=给模型看的中文说明)   ← 外层:登记成 LangChain 工具
    @tool_guard                                  ← 内层:兜底,任何异常都转成错误信封
    def 工具名(参数) -> Envelope:
        return ok(...) / fail(...)

两个装饰器的顺序不能反:tool_guard 必须贴着函数,先把异常收干净,
再由 @tool 把「永远不抛异常、永远返回信封」的那个可调用对象登记成工具。
反过来的话,异常会先被 LangChain 捕获成它自己的英文报错,工人就看不懂了。
"""

from __future__ import annotations

from langchain_core.tools import tool

from gyt.core.errors import Envelope, ok, tool_guard

# 给模型看的工具说明。单独提出来是为了让 @tool(...) 那行短一点、好读。
_ECHO_DESCRIPTION = (
    "回声工具:把收到的文本原样返回。"
    "用于验证「Supervisor → 子 Agent → 工具」这条链路是否畅通。"
    "参数 text 传用户的原话。"
)


@tool("echo", description=_ECHO_DESCRIPTION)
@tool_guard
def echo(text: str) -> Envelope:
    """把 text 原样回显,返回统一错误信封。

    这是 T1 的连通性探针,不做任何业务判断,也不校验内容 ——
    空字符串也照样回显,因为「能把空串原样传回来」本身就是链路通的证据。

    参数:
        text: 需要回显的文本,一般是用户原话。

    返回:
        Envelope 信封,data 形如 {"echo": <原文>, "length": <字符数>}。
    """
    return ok(
        data={"echo": text, "length": len(text)},
        user_msg=f"链路自检正常,收到的原话是:{text}",
    )


# 供 gyt.agents.ping 组装时使用。模块级常量,外部拿去用之前请先 list(...) 复制一份,
# 别原地 append —— create_gyt_agent() 内部已经做了复制。
PING_TOOLS: list = [echo]

__all__ = ["PING_TOOLS", "echo"]
