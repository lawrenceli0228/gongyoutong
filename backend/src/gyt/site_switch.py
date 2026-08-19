"""自然语言「切换当前工地」——挂在 supervisor 名下的一个工具。

===========================================================================
为什么是「supervisor 工具」而不是子 Agent,也不是后端偷偷改 config
---------------------------------------------------------------------------
「当前工地」这条链路是**前端每轮注入、后端只读**的(见 core/run_context.py):
前端顶栏选中的工地存在 React state + localStorage,每次提交经
``config.configurable.gyt_project_id`` 带给后端;后端所有工具只用
``project_from_config`` 读它。**后端改不了下一轮的注入值** —— 下一条消息前端又会
把它自己那份旧值盖回来。所以「真切换」必须让**前端**更新它自己的选择。

本工具就是那条「后端 → 前端」反向信号的源头:supervisor 识别到用户要切工地,
调用它;它返回的 ``Envelope`` 会成为一条 ToolMessage(name=switch_project、
content 是干净 JSON)随 values 流到前端;前端的 ProjectSwitchSync 监听到这条消息,
读出 ``data.project_id`` 调 setProjectId —— React + localStorage 都更新,
**下一轮 submit 就带上新工地**。

一个诚实的时序约束:切换**从下一条消息起生效**。当前这一轮里、在本工具之后被派出去的
子 Agent,拿到的仍是本轮请求携带的旧 ``gyt_project_id``(config 一轮之内不可变)。
supervisor 提示词里已说明这一点,遇到「既切换又查数据」的一句话请求要提醒用户。

装饰器顺序照全项目契约:``@tool`` 在外、``@tool_guard`` 在内。
===========================================================================
"""

from __future__ import annotations

import asyncio
from typing import Literal

from langchain_core.tools import tool

from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard
from gyt.db import projects as db
from gyt.db.projects import ProjectRow

# 「切到全部 / 不限项目」这类要清空当前工地(回到全局)的说法。命中即切成空串 project_id。
_GLOBAL_ALIASES = frozenset(
    {"全部", "全部工地", "全部项目", "所有", "所有工地", "全局", "不限", "不限项目", "all"}
)


def _is_global(target: str) -> bool:
    """判断用户是不是要「切到全部 / 不限项目」(回到全局作用域)。"""
    t = target.strip()
    if t in _GLOBAL_ALIASES:
        return True
    # 「全部工地」「不限项目」等带修饰词的说法,含关键词即算。
    return any(key in t for key in ("全部", "所有", "不限", "全局"))


def resolve_target(
    target: str, rows: list[ProjectRow]
) -> tuple[Literal["global", "one", "many", "none"], ProjectRow | list[ProjectRow] | None]:
    """把用户说的工地(名字 / 编号 / 「全部」)解析到具体项目。

    返回 (状态, 载荷):
      · "global" → 载荷 None:切到不限项目(全局)。
      · "one"    → 载荷 ProjectRow:唯一命中。
      · "many"   → 载荷 list[ProjectRow]:对上了多个,得让用户说清。
      · "none"   → 载荷 None:一个都没对上。

    匹配顺序(先精确、后模糊,避免「项目1」误命中「项目12」这种):
      ① 「全部 / 不限」关键词 → global;
      ② id 精确;③ name 或 code 精确;④ name/code 子串(双向:名字包含在输入里也算,
         接得住「切到阳光花园工地」这种带后缀的说法)。
    """
    t = target.strip()
    if _is_global(t):
        return "global", None
    if not t:
        return "none", None

    # ② id 精确(id 是短码,大小写不敏感更宽容)
    for r in rows:
        if r.id.lower() == t.lower():
            return "one", r

    # ③ name / code 精确
    exact = [r for r in rows if r.name == t or (r.code and r.code == t)]
    if len(exact) == 1:
        return "one", exact[0]
    if len(exact) > 1:
        return "many", exact

    # ④ 子串模糊(双向):输入包含项目名、或项目名/编号包含输入
    subs = [
        r for r in rows if t in r.name or r.name in t or (r.code and (t in r.code or r.code in t))
    ]
    if len(subs) == 1:
        return "one", subs[0]
    if len(subs) > 1:
        return "many", subs

    return "none", None


_SWITCH_DESCRIPTION = (
    "切换「当前工地/项目」。用户说「切到XX工地」「换成项目2」「以后都按阳光花园来」"
    "「不限项目/看全部工地」这类要改『现在针对哪个工地』的话时,调它 —— "
    "这是唯一会真正改变界面选中工地的动作,光用嘴回一句『已切换』是假的、不生效。"
    "target 填用户说的工地名字或编号;要切到不限项目就填「全部」。"
)


@tool("switch_project", description=_SWITCH_DESCRIPTION)
@tool_guard
async def switch_project(target: str) -> Envelope:
    """把当前工地切到 target(名字/编号/「全部」)。成功时 data 带 project_id 给前端落地。"""
    rows = await asyncio.to_thread(db.list_projects)
    status, payload = resolve_target(target, rows)

    if status == "global":
        # project_id 空串 = 不限项目(全局)。前端据此 setProjectId("") 清空选择。
        return ok(
            data={"project_id": "", "name": "全部工地"},
            user_msg="好,已切到「全部工地」(不限项目)。从下一条消息起就按全局来了。",
        )

    if status == "one":
        assert isinstance(payload, ProjectRow)
        return ok(
            data={"project_id": payload.id, "name": payload.name},
            user_msg=(
                f"好,已切到「{payload.name}」工地。从下一条消息起,"
                "图纸、规范、任务这些都按这个工地来。"
            ),
        )

    if status == "many":
        assert isinstance(payload, list)
        names = "、".join(r.name for r in payload[:6])
        return fail(
            ErrorCode.INVALID_INPUT,
            user_msg=f"「{target}」对上了好几个工地:{names}。说得再具体点,或者直接说工地全名。",
        )

    # none
    if not rows:
        return fail(
            ErrorCode.NOT_FOUND,
            user_msg="现在系统里还没建任何工地。先在「资料归档」面板新建工地,再切过去。",
        )
    listed = "、".join(r.name for r in rows[:8])
    return fail(
        ErrorCode.NOT_FOUND,
        user_msg=f"没找到「{target}」这个工地。现在有这些:{listed}。说一个准确的名字,我再给你切。",
    )


__all__ = ["resolve_target", "switch_project"]
