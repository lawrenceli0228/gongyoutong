"""report —— 巡检记录生成 Agent(英雄链 safety → report 的第二环)。

W3 当前形态:只做「单张照片 → 巡检记录 docx」。日报/周报需要隐患台账
(多条记录的存储与聚合),是赛后项 —— prompt.md 里已明确不许诺。

数据保真设计(为什么它"只搬编号不搬内容")见 tools.py 顶部。

===========================================================================
为什么这里挂了「编号溯源」守卫,而且抓到编造就不放行
---------------------------------------------------------------------------
2026-08-10 真机验收抓获:英雄链的那条硬边只保证 report **被叫起来**,
不保证它**真调工具**。实测它没调 render_inspection_report,直接照着
prompt.md 里的示范句编了一段:

    巡检记录出好了,编号 GYT-20260809-153739。

而磁盘上根本没有那份 docx。工友会拿着这个编号去找管理员,人家查无此件。

schedule 泳道 2026-08-08 踩的是同一种病(嘴上销账、库里没销),当时的解法是
agents/schedule/guard.py 的 RequireLedgerTool。评审决议把那件结构件下沉成
core/require_tool.py 复用,report 这边只是换一套文案、换一种失败语义接上去。

**为什么不是照抄 schedule 的「首答必须调工具」**(2026-08-10 复核纠正过一次):
那个判据给 report 用两头都不对 —— 在英雄链里一次都不触发,一旦触发又会打断
prompt.md 明文要求的两种合法纯文本回合(反问「给哪张出记录?」、如实拒答日报)。
完整推演见 core/require_tool.py 顶部「两种判据,别混用」。

report 用的是 **RequireReceiptSource(编号溯源)**:输出里报出的 `GYT-…` 编号,
必须在本回合的工具结果里真的出现过。编号是工具生成的(tools.py:151),
模型没有合法理由自己造;而反问和拒答压根不含编号,零误伤。

抓到编造时**不放行**(与 schedule 的 pass 档不同):
「这次没出成」工友看得见、也重试得了;「以为出成了」他揣着一个不存在的编号
走人,后面没有任何一环能救回来。所以宁可当场如实说没出成。

提示词管不住这件事(prompt.md 的红线 1 早就写着「没有落盘的文档就是没有记录」,
照样被绕过)—— 本仓三次栽在同一个地方,结论是结构件才兜得住。
(顺带纠正一句本文件写过的假话:改 report/prompt.md **不碰视觉缓存** ——
 视觉缓存的键是 vision_prompt.md 正文 + prompt_version + 视觉模型,与这份提示词无关;
 受影响的只有 report 自己那几条文本缓存,便宜。所以「不要改提示词」的理由是
 「它不解决问题」,不是「代价大」。)
===========================================================================
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.report.tools import REPORT_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt
from gyt.core.require_tool import RequireReceiptSource

# 节点名 = 交接工具名 = 路由评测里 expected_agent 的取值之一(scorers.ROUTING_AGENTS)。
REPORT_AGENT_NAME = "report"

REPORT_DIR = Path(__file__).parent

REPORT_RECEIPT_PATTERN = r"GYT-\d{8}-\d{6}"
"""巡检记录编号的样子。与 tools.py:151 的 ``strftime("GYT-%Y%m%d-%H%M%S")`` 同源,
**要改一起改** —— 那边换了格式而这边没跟,守卫就会漏掉所有编造。

写死位数(8 位日期 + 6 位时刻)而不是 ``GYT-\\S+``:宽模式会把工友随口说的
「那个 GYT-什么来着」也算成报了编号,反而误伤。
"""

REPORT_RETRY_NUDGE = (
    "(系统校验)你刚才回复里的巡检记录编号,在这一轮的工具结果里找不到出处。"
    "记录编号只有一个来路:调 render_inspection_report,由它渲染落盘并把编号返回给你。"
    "自己写一个编号等于伪造记录 —— 磁盘上并没有那份文件,"
    "工友拿着它去取件会扑空,而他要到取件那一刻才知道。"
    "现在重新处理:从上文里原样复制 32 位照片编号,调 render_inspection_report;"
    "如果照片编号不明确,就问一句是哪张,别硬出。"
)
"""抓到编造编号时追加给**模型**的那条系统校验。

三处刻意为之:

1. **不复述任何编号样例。** 初稿写过「凭上文自己写一句『记录已出好,编号 GYT-…』」,
   复核指出那等于在重试那一轮又递了一次现成范例 —— 而本仓记录过的失败机理正是
   few-shot 自我模仿(schedule/guard.py 顶部原话)。给范例是帮倒忙。
2. **说清后果落在谁身上**,而且点明「他要到取件那一刻才知道」。
   只说「你必须调工具」模型会当成客套。
3. **末句给了退路**:照片编号不明确时该问就问。少了这句,模型被逼着「必须出」,
   反而可能瞎填一个照片编号去调工具 —— 那是把伪造从编号挪到了参数上。
"""

REPORT_GIVE_UP_MESSAGE = (
    "这回巡检记录没出成,文件也没存下来,先别往上报。"
    "麻烦把那张照片再发一遍,跟我说一句「出巡检记录」,我立刻重做一份。"
    "要是还不行,先让现场安全员手写一份顶上,别耽误交活。"
)
"""重试之后仍然没调工具时,顶替模型发言的那句话 —— 本次改动里**唯一**会
出现在工地师傅眼前的字符串。

三条硬要求(tests/unit/test_report.py 里有护栏测试钉着,别改回技术腔):
  · 说明白「这次没出成」,不许留任何「好像已经生成了」的余地;
  · 给一个当场能做的动作(重发照片再说一句),再给一个不依赖系统的退路;
  · 一个编号都不许带 —— 带了就等于把这次改动要防的事又做了一遍。
"""

__all__ = [
    "REPORT_AGENT_NAME",
    "REPORT_DIR",
    "REPORT_GIVE_UP_MESSAGE",
    "REPORT_RECEIPT_PATTERN",
    "REPORT_RETRY_NUDGE",
    "build_report_agent",
]


def build_report_agent() -> CompiledStateGraph:
    """组装 report Agent(已编译)。文档渲染是本地纯计算,本体走便宜的 text 档。"""
    return create_gyt_agent(
        name=REPORT_AGENT_NAME,
        prompt=load_prompt(REPORT_DIR),
        tools=list(REPORT_TOOLS),
        purpose="text",
        # 防「口头出记录」结构件:报了没出处的编号就打回重试一次;
        # 重试还在编,就把模型那段话整个换掉。理由见本文件顶部。
        extra_middleware=[
            RequireReceiptSource(
                agent_name=REPORT_AGENT_NAME,
                pattern=REPORT_RECEIPT_PATTERN,
                nudge=REPORT_RETRY_NUDGE,
                give_up_message=REPORT_GIVE_UP_MESSAGE,
            )
        ],
    )
