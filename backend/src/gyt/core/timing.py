"""模型调用与工具执行的耗时观测 —— 让「界面卡了三分钟」这种事下次自己说话。

===========================================================================
为什么要这件(2026-08-20,一次花了两小时的日志考古之后)
---------------------------------------------------------------------------
线上一次「拍照 → 出巡检记录」量到 213 秒。日志里能看到的只有 httpx 那几行
「HTTP Request: POST ... 200 OK」,于是只能靠**相邻两行的时间差**倒推:

    02:04:02  moonshot 200 OK        ← 识图完成
         ↓
         ↓  198 秒,**中间一条日志都没有**
         ↓
    02:07:20  deepseek 200 OK        ← safety 组织回答

倒推不出的恰恰是最要紧的一件事:这 198 秒是**在那次 deepseek 调用里面**,
还是**在发出请求之前**(工具后处理、隐患入库、中间件、图调度)?
两者的修法完全不同,而 httpx 的日志只在请求发出后才打印,分不开。

当时排除了一圈(图片 base64 没混进请求:state 只有 5.6KB;CPU 空闲;
DeepSeek 本身 5.8~7.2 秒;流式最慢也才 16.4 秒;隐患入库 1.0 秒),
仍然定不了位 —— **因为没有任何东西在量这一段**。

所以这个模块只干一件事:**把耗时这个数本身变成判据**。

    模型调用记了 198 秒  → 那 198 秒在调用里面,查模型侧 / 网络
    模型调用记了 2 秒    → 那 198 秒在调用之前,查工具与图调度
                            (而工具那一侧由 tool_guard 的计时接上)

两件合起来把那段黑箱**完全切开**,不用再猜。

===========================================================================
为什么用 callback 而不是包一层模型代理
---------------------------------------------------------------------------
🔴 CLAUDE.md 有明令:**不要在 `base_agent.py` 里再包一层模型代理** ——
   那会把工具集这一维从缓存键里弄丢。

callback 不碰模型的构造参数,所以不动缓存键。**这一条是实测过的,不是推理**
(2026-08-20,同一台机上):

    ChatOpenAI(..., callbacks=[H()])._get_llm_string()
      == ChatOpenAI(...)._get_llm_string()        → True

⚠️ 量的时候要走**真实的 get_chat_model 路径**,别拿一个简化参数的 ChatOpenAI 量:
   键里含 timeout / max_retries / extra_body / 工具集,参数少一项键就短一截。
   实测键长(2026-08-20,真实路径):text 档 491、vision 档 410,带不带 callbacks
   **逐字节相同**,且 "callback" 这个词根本不出现在键里。
   —— 这段本来写的是「键长都是 355」,那是拿简化参数量出来的数,**已作废**。
   结论(相等)当时就是对的,但留档的数错了;本仓对过期数字的容忍度是零,别再照抄。

必须实测的理由:缓存键一旦变了,**整份视觉缓存作废** —— 那是演示前要焐
22 分钟的东西(TODO-11 的演示铁律)。这种代价不能靠"我记得应该不影响"。

===========================================================================
挂在哪
---------------------------------------------------------------------------
    core/llm.py  get_chat_model()   ← 路径甲(Agent / Supervisor)唯一必经处
    core/llm.py  ainvoke()          ← 路径乙(识图、评测打分等直调)
    core/errors.py  tool_guard()    ← 所有工具的唯一必经处

三处都是"谁都绕不过"的收口,不用让每个 Agent 各自记得加。

===========================================================================
为什么同一份数还要给工友看一眼
---------------------------------------------------------------------------
上面记的这些**只有能翻日志的人看得见**。工友举着手机在工地上等三分钟,界面上
从头到尾只有一句「正在忙」—— 他分不出是在等识图、在等台账,还是已经卡死了。
于是「慢」这件事对他永远是黑箱,而对我只是一次 grep。

所以同一份数存一份在后端,前端**自己来取**(界面上已有的「隱藏中間步驟」
开关就是这类信息的天然位置)。

===========================================================================
🔴 为什么**不**走 LangGraph 的 custom 事件(2026-08-21 推翻了前一天的写法)
---------------------------------------------------------------------------
2026-08-20 这件事第一版是走 ``stream_mode="custom"`` 推给前端的,理由写得也对:
supervisor 的 ``output_mode="last_message"`` 会把子 Agent 的工具返回整个丢掉,
而 custom 是独立通道、不受消息裁剪影响。

**但它一条都没显示出来过**,原因不在那条通道本身,在它的作用域:

  ① 值得看的模型调用**全在子图里**。safety(识图 23 秒)、schedule 这些都是
     ``create_supervisor`` 挂进去的 Agent —— 每个都是独立编译的 Pregel,
     由 ``_make_call_agent`` 在父节点的**函数体里** ``.invoke()``。
     于是它们发的 custom 事件带着命名空间(``inspection:…|safety:…``)。

  ② 子图里发的 custom 事件,**必须请求方开 ``subgraphs=True`` 才出得来**。
     实测(probe,没调模型):

         不带 subgraphs → 收到 1 条:['外层发的']
         带 subgraphs   → 收到 2 条:['外层发的', '子图里发的']

  ③ 而 ``subgraphs=True`` 会把子图的 **``values``** 事件也放出来,SDK 对每个
     values 事件是**整份替换**、不是合并(``@langchain/langgraph-sdk``
     的 ``dist/ui/manager.js:447`` 那句裸 ``return data``)。子图先推一份长的
     (它自己的内部消息),父节点跑完再推一份短的(last_message 只回灌最后一条),
     于是**子 Agent 说的话先出现、再消失** —— 工友看见的是「话被收回去了」。

  ②③ 是**同一个开关的两头**:开着,耗时行有了但话会被收回;关着,话不收回但
  耗时行一条都没有。走聊天流就只能二选一。

所以耗时改走**自己的直连接口**(``timing_api.py``),照 W7 打卡、W10 监理操作台
的先例 —— CLAUDE.md 里那句原话:**「操作台不是聊天产物,它有自己的入口和自己的
数据源。」** 观测数据是第三个同类。换完之后 ``subgraphs`` 可以关掉,两件一起好。

顺带修掉两件旧毛病:
  · **不再易失。** custom 事件不进检查点,刷新页面就没了;缓冲在后端,刷新还在。
  · **说得出「識隱患」了。** 旧版只带模型名(``kimi-k3``),因为 ``_mark`` 压根
    没取 metadata。而 ``on_chat_model_start`` 的 metadata 里实测就有
    ``langgraph_node='safety'`` —— 界面上那个中文名的来源。

===========================================================================
⚠️ 一条被实测推翻的旧判断,别再照着推理
---------------------------------------------------------------------------
2026-08-20 曾判定「同步 callback 被 langchain 丢进线程执行器,contextvar 断了,
所以 ``get_stream_writer()`` 拿不到」。**那是错的** —— 实测:

    跟图跑在同一个线程:False        ← 线程确实换了
    get_stream_writer():拿得到      ← 但上下文跟过去了

当时那个探针是拿手写的 ``run_in_executor(None, fn)`` 模拟的,**那个默认不拷贝
上下文**,而 langchain 自己那个拷。是模拟错了,不是代码有毛病。

**但线程换了这件事本身是真的,而且有后果**:缓冲会被别的线程写,
所以 ``_TimingBuffer`` 必须自带锁。见那个类的头注。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Any, NamedTuple
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

_MAX_TRACKED = 256
"""同时在飞的调用数上限,防字典泄漏。

正常情况下 start / end 成对出现,字典是空的。但 callback 不保证一定成对
(进程被打断、异常路径没走到 on_llm_error),所以留一道硬闸:超了就丢最早的。
丢掉的后果只是"那一条没记上耗时",不影响业务 —— 观测件绝不许把主路径拖垮。
"""


def _usage(response: LLMResult) -> Any:
    """把 ``llm_output`` 里 token 用量那一段挖出来,取不到给空字典。

    单拎出来是因为**两个调用方要用同一种取法**(日志那句 ``_fmt_tokens``、
    推事件那份 ``_token_counts``)。抄成两份的下场是哪天上游把键从
    ``token_usage`` 改成 ``usage``,日志里有数而界面上是 None —— 两边对不上,
    而人会先怀疑前端。
    """
    out = response.llm_output or {}
    return out.get("token_usage") or out.get("usage") or {}


def _fmt_tokens(response: LLMResult) -> str:
    """把 token 用量整成一小段人话。

    命中缓存时 langchain 照样会触发 callback,但 ``llm_output`` 里没有用量 ——
    所以"没有用量"基本等于"这次没真的问模型"。写成「无用量」而不是直接断言
    "命中缓存":那是推断不是事实,别把推断写成事实。
    """
    usage = _usage(response)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return "无用量(多半是命中缓存)"
    return f"输入 {prompt} / 输出 {completion} tok"


def _as_int(value: Any) -> int | None:
    """只认整数,别的一律 None。

    🔴 **取不到就是 None,不许退成 0。** 界面上 0 会被读成"这次没花 token",
       而真相是"不知道"—— 把不知道写成 0 就是编数据。``bool`` 也挡掉:它是
       ``int`` 的子类,``True`` 会悄悄变成 1 个 token。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _token_counts(response: LLMResult) -> tuple[int | None, int | None, int | None]:
    """取(输入, 输出, 其中思考)三个数。任何一项取不到都给 None。

    ``reasoning_tokens`` 藏在 ``token_usage.completion_tokens_details.reasoning_tokens``
    里。**2026-08-20 实测 moonshot 确实报这个字段**:一次识图 输出 779 / 其中思考 596——
    76% 的算力花在工友永远看不见的地方,那也是当天把视觉思考关掉的直接依据
    (数据留在 ``config.disable_thinking_for_vision`` 上方)。所以这个数值得单独推:
    它一出现,「为什么这么慢」当场就有了答案。

    不是每家供应商都报,**取不到是常态,不是故障** —— 所以这里安静给 None,
    一条日志都不记(记了就是每次调用刷一行噪声)。

    ⚠️ 整只函数兜住异常:上游哪天把 ``details`` 换成对象、换成 None、换成字符串,
       都不该让一次模型调用失败。观测件绝不许把主路径打断。
    """
    try:
        usage = _usage(response)
        details = usage.get("completion_tokens_details")
        # 实测 langchain_openai 递进来的是 ``response.model_dump()`` 的产物 ——
        # 一层套一层的纯 dict。但别把这当成保证:换供应商 / 换版本完全可能给一个
        # pydantic 对象,所以 dict 取法和属性取法两条都留着。
        if details is None:
            reasoning = None
        elif hasattr(details, "get"):
            reasoning = details.get("reasoning_tokens")
        else:
            reasoning = getattr(details, "reasoning_tokens", None)
        return (
            _as_int(usage.get("prompt_tokens")),
            _as_int(usage.get("completion_tokens")),
            _as_int(reasoning),
        )
    except Exception:  # noqa: BLE001 —— 观测件绝不许把主路径打断
        return (None, None, None)


# ---------------------------------------------------------------------------
# 存给前端来取的耗时记录(为什么不走 custom 事件,见模块头注那一整节)
# ---------------------------------------------------------------------------

KIND_LLM = "llm"
KIND_TOOL = "tool"
"""记录的两种来源。**唯一真相在这两行。**

``core/errors.py`` 的 ``tool_guard`` 也要记,它从这里取 ``KIND_TOOL`` ——
**不许自己手抄一个字符串**。手抄的下场是前端按 ``kind`` 分流时静默少一整类
(工具那半边的耗时全不显示),而后端前端都不报错。
"""

RECORD_KEYS: tuple[str, ...] = (
    "seq",
    "kind",
    "name",
    "agent",
    "seconds",
    "ok",
    "slow",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
)
"""一条记录的**全部**键,**这就是给前端的契约本身**。

跨语言镜像在 ``scripts/frontend-overrides/timing-lib.ts``,收敛不掉、手工对齐;
改名等于改对外 API(同 CLAUDE.md 对 Agent name 的那条)。

⚠️ **十个键恒定存在,不是"有值才带"**:``kind=tool`` 时三个 token 字段与
   ``agent`` 给 None 而不是缺席。形状固定前端才能安心解构,少一个键就得每处
   判一次 undefined —— 而漏判的表现是界面上一行都不出、控制台干净。
   ``test_timing.py`` 拿这个元组逐键核对 ``_record`` 的产物,别只在心里对。

逐字段的含义:

    seq       单调递增序号,前端拿它做增量拉取(``?since=``)。全进程共用一个
              计数器 —— 会话内递增就够用,共用一个的好处是不必管会话何时新建
    kind      KIND_LLM / KIND_TOOL
    name      模型名(kimi-k3)或工具名(analyze_site_photo)
    agent     这次调用发生在哪个图节点(safety / schedule / supervisor…)。
              界面上那个「識隱患」就是拿它查表来的。**取不到给 None** ——
              路径乙(直调 ainvoke)和图外调用压根没有"节点"这个概念
    seconds   耗时,保留一位小数(与日志里那个 ``%.1f`` 是同一个数)
    ok        成没成。**失败的也记** —— 「这步失败了、还花了 213 秒」和
              「这步很慢」是两回事,只记成功的话最该看见的那类恰好看不见
    slow      是否 ``>=`` 阈值。判据与日志抬 warning 那条同源
    三个 token  只有 kind=llm 才可能有。命中缓存 / 供应商不报用量 / 是工具,
              都给 None。**None 不许写成 0**(理由见 ``_as_int``)
"""

_BUFFER_PER_THREAD = 200
_BUFFER_MAX_THREADS = 64
"""缓冲的两道硬闸,同 ``_MAX_TRACKED`` 那条:**防泄漏,不是可调旋钮**,
所以不进 ``config.py``(那里放的是"会有人想改"的东西)。

单会话 200 条:一次「拍照 → 出巡检记录」大约十几条,200 条够翻十几轮。
会话数 64:超了按最久没动过的先淘汰。淘汰掉的后果只是"那条老会话翻不出耗时了",
业务一点不受影响 —— 观测件绝不许把进程吃垮。
"""


def _current_thread_id() -> str | None:
    """从 LangGraph 运行时取当前会话号。不在图里就给 None。

    🔴 **``from langgraph.config import ...`` 必须留在函数体里。** 两个理由,
       任一条都足够(与旧版取 writer 时同一条,没变):
       ① ``import langgraph.config`` 实测 555.8ms(2026-08-20,还是在
          langchain_core 已加载的前提下量的)。本模块被 ``core/llm.py`` 模块级
          import,提上去等于给每个 import 到 llm 的入口(含评测)白加半秒。
       ② 提上去就把"langgraph 装没装好"变成 **import 期硬失败** ——
          观测件把主路径打断,正是本模块头注明令禁止的那件事。
       代价只有一次 ``sys.modules`` 字典查找:真在图里跑时 langgraph 早加载好了。

    ⚠️ **拿不到是正常路径,不是故障。** 评测、单测、CLI、清理脚本全都不在图内,
       ``get_config()`` 在运行时之外会直接抛(实测 RuntimeError「Called get_config
       outside of a runnable context」)。所以这里连 debug 都不记 —— 记了就是
       每跑一轮评测刷一屏噪声,而噪声会把真正的告警埋掉。

    实测(2026-08-21):``tool_guard`` 包着的工具里调这个,拿到
    ``thread_id='会话-abc'``,子图套两层也照样拿得到。
    """
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable") or {}
    except Exception:  # noqa: BLE001 —— 见 docstring:这是正常路径
        return None
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _agent_name(metadata: dict[str, Any]) -> str | None:
    """从 callback 的 metadata 里推出「这次调用属于哪个 Agent」。

    🔴 **不许用 ``langgraph_node``。** 那是本能反应,而且在裸 StateGraph 上试会
       "看着正好" —— 但真实的子 Agent 是 ``create_agent(...)`` 编出来的图,模型
       调用发生在**它内部的节点**里。实测(2026-08-21,真形状:create_agent
       套进英雄链子图、再套进父图):

           langgraph_node = 'model'          ← safety 和 report 两次调用**都是它**
           checkpoint_ns  = 'inspection:…|safety:…|model:…'
                            'inspection:…|report:…|model:…'

       拿 ``langgraph_node`` 当名字,界面上每一行都会写「model」,而且不报错。

    判据:**取命名空间的第一段**。理由不是"第一段碰巧对",是它恰好等于
    ``AGENT_REGISTRY`` 里注册的那个名字 —— 也就是前端 ``AGENT_LABELS``
    查表用的键、``GytStatusCards`` 的 ``AGENT_TO_CARD`` 用的键。英雄链里
    safety 与 report 都归到 ``inspection``,这正是界面想要的粒度
    (那两步在工友眼里就是「識隱患」这一件事)。

    ⚠️ 命名空间为空时退回 ``langgraph_node``,**这不是兜底而是正解**:空命名空间
       意味着这次调用就发生在最外层图的节点里,那里的节点名本来就是有意义的
       (supervisor 那种)。'model' 这种内部名只可能出现在非空命名空间下。

    取不到就 None —— 路径乙(直调 ainvoke)和图外调用压根没有"节点"这个概念。
    """
    ns = metadata.get("langgraph_checkpoint_ns") or metadata.get("checkpoint_ns") or ""
    if isinstance(ns, str) and ns:
        # 段的长相是 `<节点名>:<任务 uuid>`,多段之间用 `|` 隔开。
        first = ns.split("|", 1)[0].split(":", 1)[0].strip()
        if first:
            return first
    node = metadata.get("langgraph_node")
    return str(node) if node else None


class _TimingBuffer:
    """按会话号归档的耗时记录,进程内、有上限、**自带锁**。

    🔴 **锁不是防御性编程,是必需的。** 实测(2026-08-21):langchain 把同步
       callback 丢进线程执行器跑 —— ``跟图跑在同一个线程:False``。也就是说
       ``add()`` 真的会被别的线程调,而读那一侧(HTTP handler)跑在事件循环上。
       没有锁的话 ``OrderedDict`` 在扩容/淘汰的当口被并发改,轻则漏一条,
       重则 RuntimeError 冒到 handler 里变成 500。

    为什么是进程内而不是落库:耗时是**观测数据,不是业务数据** —— 丢了不影响
    任何人干活,而为它加一张表就要管迁移、清理、备份。同样的判断在
    ``attendance/cleanup.py`` 那边反过来:考勤凭证是业务数据,所以它落盘。

    ⚠️ 进程内的代价要认:**多 worker 时各存各的**。今天线上是
       ``--n-jobs-per-worker 2`` 的单 worker,不是问题;哪天真起多 worker,
       表现是"有时候取回来的耗时不全",到那天再谈共享存储,别现在预支复杂度。
    """

    def __init__(self, per_thread: int, max_threads: int) -> None:
        self._per_thread = per_thread
        self._max_threads = max_threads
        self._lock = threading.Lock()
        self._threads: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        self._seq = 0

    def add(self, thread_id: str, record: dict[str, Any]) -> None:
        """记一条。``seq`` 由这里统一发号 —— 调用方不许自己编。"""
        with self._lock:
            self._seq += 1
            rows = self._threads.get(thread_id)
            if rows is None:
                rows = deque(maxlen=self._per_thread)
                self._threads[thread_id] = rows
                # 淘汰按"最久没动过"来,不是按"最早建的":一条老会话只要还在用,
                # 就不该因为后面新建了一堆会话而被挤掉。
                while len(self._threads) > self._max_threads:
                    self._threads.popitem(last=False)
            self._threads.move_to_end(thread_id)
            # `seq` 放在**最前面**,与 `RECORD_KEYS` 的顺序对齐。dict 比对和 JSON
            # 取值都不看顺序,所以这纯粹是为了让 `tuple(记录) == RECORD_KEYS`
            # 这种写法不会踩空 —— 有人早晚会那么写。
            rows.append({"seq": self._seq, **record})

    def since(self, thread_id: str, seq: int) -> tuple[list[dict[str, Any]], int]:
        """取这个会话里 ``seq`` 之后的记录,连同新的游标一起给。

        游标在**没有新记录时原样退回**(不是给 0):退成 0 的话前端下一轮会把
        整段重新拉一遍,界面上表现为耗时行成倍重复,而两边都不报错。

        ⚠️ 回的是**拷贝**,不是缓冲里那些 dict 本身。今天的唯一调用方
        (``timing_api``)只是把它们塞进 JSONResponse、不会改 —— 但哪天有人在
        handler 里给记录补一个字段,改的就是缓冲里那份,而且下一轮取出来还带着。
        先筛后拷:真正新增的通常只有几条,拷贝那几个十键小 dict 的代价可以忽略。
        """
        with self._lock:
            rows = [row for row in (self._threads.get(thread_id) or ()) if row["seq"] > seq]
        fresh = [dict(row) for row in rows]
        return fresh, (fresh[-1]["seq"] if fresh else seq)

    def clear(self) -> None:
        """只给测试用 —— 用例之间必须互不串味。"""
        with self._lock:
            self._threads.clear()
            self._seq = 0


_BUFFER = _TimingBuffer(_BUFFER_PER_THREAD, _BUFFER_MAX_THREADS)


def recent_timings(thread_id: str, since: int = 0) -> tuple[list[dict[str, Any]], int]:
    """给 ``timing_api.py`` 的读口。**这是唯一的读入口**,别让谁再摸 ``_BUFFER``。"""
    return _BUFFER.since(thread_id, since)


def reset_timings() -> None:
    """清空缓冲。只给测试用。"""
    _BUFFER.clear()


def emit_timing(
    *,
    kind: str,
    name: str,
    seconds: float,
    ok: bool,
    slow: bool,
    agent: str | None = None,
    thread_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> None:
    """记一条耗时到缓冲。**不在会话里就安静走开,任何情况下都不抛。**

    字段含义见 ``RECORD_KEYS`` 的 docstring(那儿是契约的唯一真相)。这里只说
    两个参数本身的事:

        agent:      调用方知道就传(``LlmCallTiming`` 从 metadata 里取得到);
                    不知道就留空
        thread_id:  调用方知道就传,不传则**自己去运行时里问**。
                    🔴 ``LlmCallTiming`` 必须显式传 —— 它拿的是
                    ``on_chat_model_start`` 的 metadata,那是**当参数递进来的**,
                    比 contextvar 可靠(那个 callback 实测跑在别的线程里)。
                    ``tool_guard`` 那侧不传,靠 ``_current_thread_id()`` ——
                    这样 ``core/errors.py`` 一行 langgraph 都不用碰,
                    它那条"模块级不许 import langchain"的守卫继续成立。

    ⚠️ 拿不到会话号就丢掉这条,**这是正常路径**:评测、单测、CLI 全都不在会话里。
    """
    try:
        thread_id = thread_id or _current_thread_id()
        if not thread_id:
            return
        _BUFFER.add(
            thread_id,
            _record(
                kind, name, agent, seconds, ok, slow, input_tokens, output_tokens, reasoning_tokens
            ),
        )
    except Exception:  # noqa: BLE001 —— 记坏了最多是界面上少一行,不能连累调用
        logger.debug("耗时记录写入失败,已忽略", exc_info=True)


def _record(
    kind: str,
    name: str,
    agent: str | None,
    seconds: float,
    ok: bool,
    slow: bool,
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
) -> dict[str, Any]:
    """拼一条记录(``seq`` 由缓冲发号,所以这里没有)。

    单拎出来是为了让 ``test_timing.py`` 能拿 ``RECORD_KEYS`` 逐键核对它的产物 ——
    契约有没有漏键这件事,要有东西替人数。
    """
    return {
        "kind": kind,
        "name": name,
        "agent": agent,
        # 保留一位小数:界面上「7.5 秒」够用,存 7.483210 只是噪声,而且和日志里
        # 那个 %.1f 对得上 —— 两边说的必须是同一个数,不然对账的人会以为
        # 看见了两次不同的调用。
        "seconds": round(seconds, 1),
        "ok": ok,
        "slow": slow,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


class _Started(NamedTuple):
    """一次模型调用在**起点**记下的东西,等终点来配对。

    做成具名的而不是四元组:``started[2]`` 这种写法在半年后没人认得出是会话号,
    而且加一项字段时所有解包处都要跟着数位置。
    """

    at: float
    model: str
    thread_id: str | None
    agent: str | None


class LlmCallTiming(BaseCallbackHandler):
    """给每次模型调用记一条「耗时 + 用量」。

    ⚠️ 这是**观测件**:任何情况下都不许抛异常把业务打断。所以每个回调里都兜了
       try/except —— 观测坏了最多是少一条日志,不能变成"用户提问失败"。
    """

    def __init__(self, slow_seconds: float) -> None:
        self._slow_seconds = slow_seconds
        self._started: dict[UUID, _Started] = {}

    # -- 起点 ---------------------------------------------------------------
    # langchain 对 chat model 走 on_chat_model_start,对老式 LLM 走 on_llm_start。
    # 两个都实现:只实现前者的话,哪天有人加一个非 chat 的模型就静默不记了。

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._mark(run_id, kwargs)

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._mark(run_id, kwargs)

    def _mark(self, run_id: UUID, kwargs: dict[str, Any]) -> None:
        """记起点,顺便把**只有这个钩子拿得到的两样东西**收下来。

        🔴 会话号与节点名必须在**起点**取,终点取不到:实测(2026-08-21)同一次
           调用的两个钩子拿到的东西完全不同 ——

               on_chat_model_start  metadata 有 11 个键,含
                                    thread_id='会话-123'、langgraph_node='safety'
               on_llm_end           metadata **是空的**

           所以别"顺手"挪到 ``on_llm_end`` 里去取,挪了就是每条记录的
           ``agent`` 恒为 None、``thread_id`` 只能退回问运行时(而那是另一条
           不如它可靠的路 —— 这个回调跑在别的线程里)。

        ⚠️ 记下来的是**图节点名(英文)**,不是给人看的名字。中文名在前端查表
           (``timing-lib.ts`` 的 ``AGENT_LABELS``)—— 后端一个中文都不带,
           因为界面恒繁體而这里是简体源码,让后端出中文等于把繁簡这件事漏一处。
        """
        try:
            if len(self._started) >= _MAX_TRACKED:
                self._started.pop(next(iter(self._started)), None)
            params = kwargs.get("invocation_params") or {}
            model = str(params.get("model") or params.get("model_name") or "?")
            metadata = kwargs.get("metadata") or {}
            thread_id = metadata.get("thread_id")
            self._started[run_id] = _Started(
                at=time.monotonic(),
                model=model,
                thread_id=str(thread_id) if thread_id else None,
                agent=_agent_name(metadata),
            )
        except Exception:  # noqa: BLE001 —— 观测件绝不许把主路径打断
            logger.debug("模型调用计时:记起点失败", exc_info=True)

    # -- 终点 ---------------------------------------------------------------

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            started = self._started.pop(run_id, None)
            if started is None:
                return
            elapsed, model = time.monotonic() - started.at, started.model
            # 超过阈值抬到 warning:线上日志量很大,慢调用要能一眼捞出来。
            # 判据本身就是这个数 —— 见模块头注「为什么要这件」。
            slow = elapsed >= self._slow_seconds
            level = logging.WARNING if slow else logging.INFO
            logger.log(
                level,
                "模型调用完成 model=%s 耗时=%.1fs %s%s",
                model,
                elapsed,
                _fmt_tokens(response),
                f"(超过 {self._slow_seconds:.0f}s 阈值)" if level == logging.WARNING else "",
            )
        except Exception:  # noqa: BLE001
            logger.debug("模型调用计时:记终点失败", exc_info=True)
            return
        # 日志记完**再**记缓冲,顺序是刻意的:日志是最后的兜底 —— 前端那条路断了
        # 我还能翻日志,反过来不成立。所以绝不许让记缓冲的任何环节挡在日志前面。
        input_tokens, output_tokens, reasoning_tokens = _token_counts(response)
        emit_timing(
            kind=KIND_LLM,
            name=model,
            seconds=elapsed,
            ok=True,
            slow=slow,
            agent=started.agent,
            thread_id=started.thread_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """失败的调用也要记 —— 不记的话「慢且最终失败」那一类永远看不见,
        而且字典会泄漏(start 进去了没人 pop)。"""
        try:
            started = self._started.pop(run_id, None)
            if started is None:
                return
            elapsed, model = time.monotonic() - started.at, started.model
            logger.warning(
                "模型调用失败 model=%s 耗时=%.1fs 类型=%s",
                model,
                elapsed,
                type(error).__name__,
            )
        except Exception:  # noqa: BLE001
            logger.debug("模型调用计时:记失败态失败", exc_info=True)
            return
        # 失败的也记。界面上「这一步失败了、还花了 213 秒」和「这一步很慢」是两回事,
        # 前端按 ok 分流 —— 只记成功的话,最值得看见的那一类恰好看不见。
        #
        # ⚠️ `slow` 照样按阈值如实算:上面那句日志"失败一律 warning"是**日志侧**的
        #    分级(失败本身就值得看见),和"这次是不是超阈值"不是同一件事,别混。
        # 三个 token 字段留 None:失败时压根没有 LLMResult,编不出数来。
        emit_timing(
            kind=KIND_LLM,
            name=model,
            seconds=elapsed,
            ok=False,
            slow=elapsed >= self._slow_seconds,
            agent=started.agent,
            thread_id=started.thread_id,
        )


__all__ = [
    "KIND_LLM",
    "KIND_TOOL",
    "RECORD_KEYS",
    "LlmCallTiming",
    "emit_timing",
    "recent_timings",
    "reset_timings",
]
