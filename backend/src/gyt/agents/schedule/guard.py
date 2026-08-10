"""RequireLedgerTool —— 台账动作必须先过工具,拦「凭记忆做假账」。

2026-08-10 起,判定与重试逻辑下沉到了 **core/require_tool.py 的 RequireToolCall**
(report 泳道栽了同一种病:没调出文件的工具,直接编了个巡检记录编号)。
案情、做法与边界全在那个文件的 docstring 里,这里只剩台账自己的两样东西:
台账口径的系统校验话术,以及「重试还不调工具就放行」这个处置选择。

为什么台账选 on_give_up="pass" 而不是像 report 那样顶替:
台账嘴上销了库里没销,下一轮查清单就会露馅、还能补销;而 report 一旦让工友
拿到一个不存在的编号,人已经跑去找管理员了,没有补救机会。两边严重度不同,
所以处置不同 —— 这不是抄漏了,是有意为之。
"""

from __future__ import annotations

from typing import Final

from gyt.core.require_tool import RequireToolCall

_NUDGE: Final[str] = (
    "(系统校验)你刚才没有调用任何工具就想直接回答。台账问题必须先调工具拿真实数据:"
    "记任务用 add_task、查清单用 list_tasks、改期用 reschedule_task、销项用 finish_task。"
    "凭记忆或上文猜测回答等于做假账。现在重新处理:先调对应的工具。"
)


class RequireLedgerTool(RequireToolCall):
    """台账口径的 RequireToolCall(参数已绑好,挂载处不用重复填)。"""

    def __init__(self) -> None:
        super().__init__(agent_name="schedule", nudge=_NUDGE, on_give_up="pass")


__all__ = ["RequireLedgerTool"]
