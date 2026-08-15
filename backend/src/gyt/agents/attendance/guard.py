"""RequireAttendanceTool —— 考勤数字必须先过工具,拦「凭记忆报考勤」。

判定与重试逻辑在公共件 **core/require_tool.py 的 RequireToolCall**(schedule 与
report 两条泳道的案情、以及「两种判据,别混用」的完整推演都在那个文件顶部)。
这里只剩考勤自己的两样东西:考勤口径的系统校验话术,以及 on_give_up="pass" 的处置选择。

为什么用「首答必须调工具」判据,而不是 report 那件 RequireReceiptSource(编号溯源):
两种判据守的是同一条红线的两个侧面,**选错了两头都不对**(CLAUDE.md「防假账」一节
2026-08-10 的教训:有人给 report 套了首答判据,在英雄链里一次都不触发)。考勤这边
反过来 —— 编号溯源在这儿没得溯:查询回执里全是「出勤 N 天」「08:00」这类数字和姓名,
没有一个「只可能由工具生成」的唯一指纹可当锚(receipt.py 头注的反向约束说的就是这事:
考勤要挂溯源类守卫就得有自己的正则,而查询工具压根不产编号)。而「考勤问句的首答
就该调工具」几乎恒真,首答判据在这儿才有的放矢。schedule 真机抓到过模型不调工具
凭记忆编回执(「销了:T1 已完成」而库里还是 open),考勤的同款病是凭上文旧数字答
「张三出勤 5 天」—— 考勤数字连着工钱,必须同样上结构件。

为什么选 on_give_up="pass" 而不是像 report 那样顶替:
  ① 查询是只读的:编的数字不落库,下一轮真查就露馅,还能当场纠正;不像 report 的
     假编号,工友拿着去找管理员就再没有补救机会。严重度不同,处置就不同。
  ② 考勤有**合法的纯文本首答**:「帮我打个卡」该回「请点界面上的打卡按钮」——
     打卡写入不走 LLM(D15),本 Agent 没有任何写入工具,这句纯文本就是正确答案。
     fail 档会把它顶替掉;pass 档最多多付一轮重试,不吃回答。
这不是抄 schedule 抄顺手了,是两条独立成立的理由。
"""

from __future__ import annotations

from typing import Final

from gyt.core.require_tool import RequireToolCall

_NUDGE: Final[str] = (
    "(系统校验)你刚才没有调用任何工具就想直接回答。考勤数字必须先调工具拿真实记录:"
    "查出勤天数用 list_attendance_days、查某天谁到了/几点打的用 list_attendance_detail。"
    "凭记忆或上文猜测报数等于编考勤,考勤数字连着工钱。现在重新处理:先调对应的工具;"
    "如果用户是想打卡(不是查记录),那就不用调工具,直接告诉他去点界面上的打卡按钮。"
)
# nudge 末句给「打卡请求」留了纯文本出口:那是本 Agent 唯一合法的首答纯文本场景,
# 不留这句,模型重试时会为了过校验硬调一次查询工具,答非所问。


class RequireAttendanceTool(RequireToolCall):
    """考勤口径的 RequireToolCall(参数已绑好,挂载处不用重复填)。"""

    def __init__(self) -> None:
        super().__init__(agent_name="attendance", nudge=_NUDGE, on_give_up="pass")


__all__ = ["RequireAttendanceTool"]
