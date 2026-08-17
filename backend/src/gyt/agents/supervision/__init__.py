"""supervision —— 监理隐患查询与处置建议 Agent(W9,**只读**)。

本包的东西:

    tools.py     只读三件套:list_hazards / get_hazard / suggest_disposal
    prompt.md    Agent 本体的提示词(文本档)
    grading.py   severity(safety 四档) → grade(监理二档)的确定性映射 + 规则版本号
    docgen.py    八种文书共用的 docx 骨架(标题 + 元信息表 + N 个节 + 统一免责句)
    documents.py 五种文书的正文段落(由 supervision_api 的端点调用,不进对话链)

D15 同款定死:隐患的**写入**(确认、定级、签发五种文书、登记复查结论)走
``supervision_api.py`` 的 HTTP 直连端点,一个 LLM 都不经过;本 Agent 只管「查」
与「建议」这一半。所以这个包里没有任何写库或出文书的工具 —— 这是设计,不是没写完;
有人想签发,``prompt.md`` 会让它引导去界面上点按钮,严禁口头答应「已经帮你签了」。
(与 attendance「请点界面上的打卡按钮」逐条同构,那边的理由是防编造与幂等,
这边更硬:签发暂停令是法律行为。)

挂进图的那一行在 ``graph.AGENT_REGISTRY``,本包**不 import graph**:
编排层依赖 Agent 包,不许反向(与 core/focus.py 那条 SUPERVISOR_NAME 同款约定)。

本体走 **text 档**(DeepSeek):隐患查询是纯文字活,没有任何视觉调用 ——
复查照片合不合格**由人下结论**(D11),这个 Agent 连看都不看。

⚠️ **状态机不在这里。** ``ALLOWED_TRANSITIONS`` 与状态迁移函数一律放
``gyt.db.hazards``。理由是**依赖方向**:``agents/*/tools.py`` import ``gyt.db``,
方向严格是 agents → db。把状态真相放这里会让 db 反向依赖 agents,既有循环导入
风险,也让存储层不能独立使用。(2026-08-16 Codex 复审第 22 条,方案 §4.2 有原文。)

这条方向约束**不等于**「db 层什么都不许 import」。截至 2026-08-16,
``db/*.py`` 除 ``gyt.config`` 外还 import ``gyt.attendance.receipt``
(``hazards.py`` 取香港时间快照)—— 那是 CLAUDE.md 点名的**唯一时间权威**,
且 receipt 是纯标准库叶子模块、不构成环。要守的是「db 不依赖 agents」,
不是「db 谁都不依赖」;`core/doc_no.py` 破同一条规矩的理由与它一致,
两处头注互指。

===========================================================================
为什么挂的是「编号溯源」守卫,而且判据要**放宽到用户发言**(方案 §5.3)
---------------------------------------------------------------------------
``core/require_tool.py`` 现成的两件,给这条泳道用都不对:

  · **首答判据(RequireToolCall)不行** —— 「这条属于严重隐患,建议签发暂停令,
    请到界面上点」是纯文本首答,而且是这个 Agent **最重要的一句正确回答**;
    fail 档会把它顶替掉,pass 档也要白付一轮重试(与 attendance 那次同款误伤,
    见 agents/attendance/guard.py 顶部)。
  · **原样的编号溯源判据也不行** —— 用户会**自己把隐患编号打进来**
    (「GYT-H-… 那条复查了吗」),而原来的 ``_sourced`` 只扫 ``ToolMessage``,
    模型原样带上这个编号会被判成编造,合法回答被顶替。

第三种判据 = 让 ``_sourced`` **同时扫本回合的 HumanMessage**:用户自己报的编号
是合法出处(是他给的,不是模型造的);而模型凭空造一个号,依旧一处出处都没有。
改动落在 ``core/require_tool.py`` 的 ``_sourced`` 一处,report 侧行为不变 ——
工友不会把巡检记录号打进聊天框(那个号是 report 出完文档才生成的),
``test_require_tool.py`` 有一条专门钉住这件事。

抓到编造时**不放行**(与 report 同档、与 schedule 的 pass 档不同):
编造一个隐患编号,监理会拿着它去界面上找、找不到,或者更糟 —— 把它写进
上报材料。宁可当场如实说没查着。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from gyt.agents.supervision.tools import SUPERVISION_TOOLS
from gyt.core.base_agent import create_gyt_agent, load_prompt
from gyt.core.doc_no import PATTERNS, DocKind
from gyt.core.require_tool import RequireReceiptSource

# 节点名 = 交接工具名(transfer_to_supervision)= chat-ui 轨迹上显示的名字。
# 它同时是 eval/datasets/routing.csv 里 expected_agent 的取值之一,
# 也在 scorers.ROUTING_AGENTS 里 —— 改这个字符串等于改对外 API,谨慎。
SUPERVISION_AGENT_NAME = "supervision"

SUPERVISION_DIR = Path(__file__).parent

SUPERVISION_RECEIPT_PATTERN = "|".join(PATTERNS[kind] for kind in DocKind)
"""本 Agent 嘴里可能出现的**全部**编号形状:隐患号 + 五种文书号。

⚠️ **派生**自 ``core/doc_no.PATTERNS``,不是手抄 —— 那边改了编号格式,这里自动跟上。
手抄一份的下场 doc_no.py 头注写得很清楚:守卫会**一个编号都认不出、静默全放行**,
而测试照绿(report 那条 ``REPORT_RECEIPT_PATTERN`` 就是这么被点名的)。

为什么六种一起认,不是只认 ``GYT-H-``:``get_hazard`` 会把已签的文书编号一并返回,
模型编一个通知单号和编一个隐患号一样危险 —— 前者更像"文书已经出了"。

⚠️ 它**故意与 report 的 ``GYT-\\d{8}-\\d{6}`` 互不匹配**(六种都带类型段与随机尾)。
两边各认各的,是 doc_no.py 头注点名的硬要求;``test_supervision_tools.py`` 有守门断言。
"""

SUPERVISION_RETRY_NUDGE = (
    "(系统校验)你刚才回复里报的隐患或文书编号,在这一轮的工具结果里、"
    "在用户说的话里,都找不到出处。编号只有两个来路:调 list_hazards / get_hazard / "
    "suggest_disposal 由工具返回,或者用户自己报给你。自己写一个编号等于编台账 —— "
    "监理会拿着它到界面上找那一条,找不到;要是写进了上报材料,那是一份指着"
    "不存在的隐患去追责的材料。现在重新处理:先调工具查真实台账;"
    "不知道是哪一条就用 list_hazards 列出来让用户挑,或者直接问一句是哪条,别硬报。"
)
"""抓到编造编号时追加给**模型**的那条系统校验。

三处刻意为之(前两处照抄 report 那条的经验,第三处是本泳道自己的):

1. **不复述任何编号样例。** 本仓记录过的失败机理正是 few-shot 自我模仿,
   递一个现成范例等于帮倒忙。🔴 这里还有一条更硬的理由:``_sourced`` 现在
   **也扫用户发言**,而 nudge 是以 ``HumanMessage`` 追加进请求的 ——
   万一有人在这句话里写了个完整编号样例,那个号就会变成"合法出处"。
   (当前实现下不会:守卫比对用的是**追加之前**那份消息列表,nudge 不在里面。
   但别把安全性押在那个细节上,这句话里就是不许出现编号。)
2. **说清后果落在谁身上**,只说「你必须调工具」模型会当成客套。
3. **末句给了退路**:不知道是哪条就列出来让人挑、或者直接问。少了这句,
   模型被逼着"必须报",反而可能瞎填一个编号去调工具 —— 那是把编造从回答挪到了参数上。
"""

SUPERVISION_GIVE_UP_MESSAGE = (
    "这条隐患我没查着,刚才报的号做不得数,别照着它去签发或者上报。"
    "麻烦把隐患编号再发一遍(GYT-H- 开头的那串),或者说一句「还有几条隐患没销」,"
    "我把清单列出来给你挑。"
)
"""重试之后仍在编编号时,顶替模型发言的那句话 —— 本 Agent **唯一**会
出现在工地师傅眼前的硬编码字符串。

三条硬要求(``test_supervision_tools.py`` 有护栏测试钉着,别改回技术腔):
  · 说明白「这次没查着、报的号做不得数」,不许留任何"大概是那条"的余地;
  · 点名"别照着它去签发或上报" —— 这是这次改动真正要防的后果;
  · 给一个当场能做的动作(重发编号 / 让我列清单),再一个完整编号都不许带。
"""

__all__ = [
    "SUPERVISION_AGENT_NAME",
    "SUPERVISION_DIR",
    "SUPERVISION_GIVE_UP_MESSAGE",
    "SUPERVISION_RECEIPT_PATTERN",
    "SUPERVISION_RETRY_NUDGE",
    "build_supervision_agent",
]


def build_supervision_agent() -> CompiledStateGraph:
    """组装 supervision Agent(已编译,可直接挂给 Supervisor)。

    返回:
        已 compile 的 CompiledStateGraph,.name 为 "supervision"。

    抛出:
        FileNotFoundError: prompt.md 不见了。
        MissingAPIKeyError: DeepSeek 的 Key 没配(隐患查询只用 text 档)。
    """
    return create_gyt_agent(
        name=SUPERVISION_AGENT_NAME,
        prompt=load_prompt(SUPERVISION_DIR),
        tools=list(SUPERVISION_TOOLS),
        # 纯文字查询,走便宜的 DeepSeek;这个包里不存在视觉调用。
        purpose="text",
        # 防编造编号结构件:报了没出处的隐患号/文书号就打回重试一次;重试还在编,
        # 就把模型那段话整个换掉。判据选择(以及为什么不能用首答判据)见本文件顶部。
        extra_middleware=[
            RequireReceiptSource(
                agent_name=SUPERVISION_AGENT_NAME,
                pattern=SUPERVISION_RECEIPT_PATTERN,
                nudge=SUPERVISION_RETRY_NUDGE,
                give_up_message=SUPERVISION_GIVE_UP_MESSAGE,
            )
        ],
    )
