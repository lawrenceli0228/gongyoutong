"""工友通 —— 双供应商大模型客户端(DeepSeek 管文本,Kimi 管视觉/工具)。

本模块只干四件事:①按用途挑对供应商、模型名、base_url 与凭据;②指数退避重试,严格区分
「等一等就好」与「重试也没用,立刻失败」两类上游错误;③内容哈希磁盘缓存,一举三得 ——
跑得快、省开发期账单、演示断网时能复演彩排过的路径;④抛出的每个错误都带一句中文人话。

依赖行为已核实(2026-08-05 解包 PyPI wheel 读源码,非凭记忆;langchain-openai 1.4.1 / core 1.5.3):
  · ChatOpenAI 的字段别名 model / api_key / base_url / timeout 均有效(populate_by_name=True)。
  · 关思考的参数直接写成原生 extra_body=,不走 model_kwargs。
    两种写法行为等价(langchain-core 的 _build_model_kwargs 会把 model_kwargs 里的 extra_body
    提升到原生字段),但 model_kwargs 那条路径每次建模型都会打一条 UserWarning ——
    Supervisor 加各子 Agent,一次冷启动就是好几条。演示日的日志噪音会盖住真问题,
    所以这里用不产生告警的写法。共享契约 v1 原本写的是 model_kwargs,已按此实测结论修订。
  · 显式钉死 use_responses_api=False:两家都只实现 /chat/completions,走 Responses API 必然失败。

图 0:两条调用路径,以及缓存/重试各由谁负责(TODO-5 已收口,2026-08-06)

    路径甲 —— Agent 真实执行路径(Supervisor / 5 个子 Agent,演示走的就是这条):

        graph.py / base_agent.py --> get_chat_model() --+--> install_llm_cache()
                                                        |      装上全局磁盘缓存(仅一次)
                                                        +--> 裸 ChatOpenAI 实例
              |
              +--> create_supervisor / create_agent 内部直接 model.ainvoke(...)
                        |
                        v
              BaseChatModel.ainvoke -> agenerate -> _agenerate_with_cache
                        |
                        +-- ① 先查全局缓存 GytDiskCache.alookup ── 命中 ──> 直接返回(0 次网络)
                        |                                              v2 流式协议下还会把
                        |                                              事件重放一遍,UI 观感不变
                        +-- ② 未命中 --> 真调模型(流式或非流式)--> GytDiskCache.aupdate 落盘
                        |
                        +-- 重试层 = ChatOpenAI 自带的 max_retries(openai SDK 的指数退避,
                            会读 Retry-After)。所以 get_chat_model 里它必须 = 配置值,
                            契约 v1 写的 0 会导致线上零重试,比不做还差。

    路径乙 —— 本模块 ainvoke()「直调」路径(不经过 Agent 的场景:评测打分、
              知识综合里的一次性问答等。README「直接调模型」那一节讲的就是它):

        ainvoke() --> 磁盘缓存(同一个 cache_dir、同一套文件格式与身份核对,
                  |            但键里带 cache_extra 而不带 llm_string)
                  --> _for_direct_call:关掉库自带的全局缓存,免得两层键叠加串味
                  --> _invoke_with_retry(自研指数退避 + 中文错误文案,见图 1)
                      ⚠️ 这一层与 ChatOpenAI 自带的 max_retries 是**叠加**的,
                         最坏 4×4=16 个 HTTP 请求。要单层就在造模型时传
                         get_chat_model(..., max_retries=0),见 _for_direct_call。

    为什么选「全局缓存」而不是「自定义 BaseChatModel 包装层」(2026-08-06 三组探针实测后的决策,
    三个月后的人请先看完这段再动手改):
      · 覆盖面:chat-ui 是流式的。实测 langchain 1.3.14 / core 1.5.3 的
        _agenerate_with_cache(chat_models.py:2030-2059)**先查缓存、后分流式**,
        所以 astream / astream_events / stream_mode="messages" / v2 消息协议
        四条真实路径全都命中缓存。唯一绕开的是「直接对 chat model 调 .astream()」,
        而 langchain/langgraph 的 Agent 节点里一处都没有这么写(实测 factory.py:1467
        与 chat_agent_executor.py:705 都是 await model.ainvoke)。
      · 正确性:缓存键的第二维 llm_string 由 _get_llm_string(stop, **kwargs) 生成,
        **自动含本次绑定的工具集与 tool_choice**。包装层方案得自己把工具揉进键,
        漏一次就是「safety 的答案被 report 原样取走」——比没有缓存更糟。
      · 代码量:包装层方案要覆写 _agenerate + _astream + bind_tools(还得显式写出
        parallel_tool_calls 形参,否则 langgraph_supervisor:60 的 inspect.signature
        检查会静默判定「不支持关并行工具调用」)+ _get_ls_params + with_structured_output。
        两人竞赛团队养不起这么多面。
      代价(已知,别当成 bug):① 缓存命中时 v1 消息协议下没有逐字打字机效果,
      整条 AIMessage 一次到达(v2 协议会重放事件,无差别);② llm_string 含
      request_timeout / max_retries / extra_body —— 演示前改 .env 里这几项会让
      整份彩排缓存作废,写进演示 checklist;③ 自研重试的中文文案仍只在路径乙上生效。

图 0.5:为什么必须先给 prompt 做归一化,否则多轮/整图只能复演第一轮

    (一)剥运行时噪声(_strip_prompt_noise)
    langchain 命中缓存时会改写消息(chat_models.py:_convert_cached_generations):
        gen.message = gen.message.model_copy(update={"usage_metadata": {..., "total_cost": 0}})
    这条被改过的 AIMessage 会进入**下一轮**的 prompt,于是第 2 轮的键对不上、又打一次模型。
    实测:不归一化时,彩排要跑三遍缓存才收敛;演示当天靠这个赌不起。
    处置:算键前把 langchain 序列化消息里的运行时噪声(id / usage_metadata /
    response_metadata / additional_kwargs 等)全剥掉,只留会改变答案的字段。

    (二)工具调用 id 位置归一化(_renumber_tool_call_ids)
    光剥噪声还不够 —— tool_call_id 与 tool_calls[].id 是「会改变答案」的字段(它们决定
    哪条工具结果配哪次调用),不能整个剥掉;但库每一轮都会**新造 uuid4** 塞进去:
        langgraph_supervisor/handoff.py:132  tool_call_id = str(uuid.uuid4())
    这对合成消息(AIMessage「Transferring back to supervisor」+ 对应 ToolMessage)会进入
    **supervisor 汇总那一次**调用的 prompt,于是整图跑第二遍时 supervisor 的最后一次调用
    永远不可能命中 —— 子 Agent 复演得了,supervisor 说不出最后那句话,而用户在 chat-ui 上
    看到的恰恰就是那句话。实测:未归一化时第二遍仍有 1 次真调,两遍 prompt 的 diff 只有
    id / tool_call_id 两行。
    处置:算键前把出现过的每个工具调用 id 按**首次出现顺序**映射成 tc0 / tc1 / ……
    id 只需要在同一个 prompt 内部保持配对一致,不需要跨轮稳定,所以不损失任何正确性
    (不同工具调用仍然落在不同序号上)。实测这一改之后第二遍新增真调 = 0。

图 1:重试退避链(llm_max_retries=3, llm_retry_base_delay_s=1.0 时)

    ainvoke ---> 缓存命中? --是--> 直接返回 AIMessage(0 次网络调用,断网也能演)
                    | 否
                    v
    第 1 次尝试 --成功--> 校验是 AIMessage --> 原子写缓存 --> 返回
        | 失败
        +-- 不可重试(400/401/403/404/422/未知异常) --> 立刻 raise,不浪费剩余轮次
        |   否(超时 / 429 / 5xx / 连接失败): await _sleep(base * 2^0 = 1.0)
    第 2 次尝试 -- 同上 --> await _sleep(base * 2^1 = 2.0)
    第 3 次尝试 -- 同上 --> await _sleep(base * 2^2 = 4.0)
    第 4 次尝试(总次数 = 1 次首发 + llm_max_retries 次重试)仍失败
        v  raise LLMCallError(user_msg=中文人话, error_code=最后一次归类出的 ErrorCode)

图 2:缓存键组成

    model_name(区分 deepseek-v4-flash / kimi-k3)---+
    prompt_version(改提示词就改它,旧缓存全失效)---+--> json.dumps(sort_keys)
    messages 稳定序列化(递归 json 化 + 键排序)-----+          |
    cache_extra(调用方维度,如 Agent 名,防串味)---+          v
                                        cache_dir/<64 位小写 hex>.json <-- sha256 hexdigest

    写盘:先写 <hex>.<随机 8 位>.tmp,再 os.replace 原子改名 —— 并发写不互相截断,读侧读不到半截。

图 3:缓存文件格式与读侧核对(防"谁能写这个目录谁说了算")

    <hex>.json = {"meta": {key, model, prompt_version, messages_digest, answer_digest},
                  "message": {AIMessage 的 model_dump}}

    读:命中文件 --> meta 前四项与本次请求逐项比对 --+-- 任一不等 --> warning + 按未命中处理
                    (这四项全是关于**问题**的)      |
                                                    +-- 全等 --> 再比 answer_digest 与
                                                                 **正文实际内容**
                                                                 --+-- 不等 --> 按未命中处理
                                                                   +-- 相等 --> 返回 AIMessage

    为什么必须核对:cache_dir 落在 ./data 这个宿主机绑定挂载里,而彩排缓存是要被打包、
    拷贝、在队员机器之间传递的"演示资产"。若读侧不核对,任何能往该目录写文件的人,
    只要本地算出同名 hex 再写一份 {"message": {"content": "照片中未发现安全隐患"}},
    Safety Agent 走到同一条消息序列时就会原样返回这句话 —— 零次模型调用,日志里
    只有一行"命中大模型缓存"。对一个判断工地危险的产品,这是不可接受的降级。

    为什么 answer_digest 是单独一项:前四项(key / model / prompt_version /
    messages_digest)**全是关于问题的,一项都不核对答案** —— 也就是说投毒者根本不用
    编 meta,把 message.content 换掉、meta 一个字节不动就能原样流出。answer_digest
    专门堵这个口子。
    ⚠️ 现在的防护级别(别把它说高了):挡得住「改正文不改 meta」与「张冠李戴/整份伪造」;
    **挡不住**能写这个目录的人连 meta 里的 answer_digest 一起重算 —— 那需要 HMAC 签名
    (密钥不落在同目录),是 W2 之后的事,见 TODOS。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeAlias
from uuid import uuid4

from langchain_core.caches import BaseCache
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    message_chunk_to_message,
)
from langchain_core.outputs import ChatGeneration, Generation
from langchain_openai import ChatOpenAI

from gyt.config import Settings, get_settings
from gyt.core.errors import ErrorCode

logger = logging.getLogger(__name__)

# 模型用途:text=路由/任务/报告/知识综合;vision=识图/图纸问答;tool=工具调用备胎。
Purpose = Literal["text", "vision", "tool"]
# 能喂给 chat model 的输入形态:纯字符串 / 单条消息 / 消息列表 / (角色, 文本) 元组列表。
MessagesInput: TypeAlias = str | BaseMessage | Mapping[str, Any] | list[Any] | tuple[Any, ...]

# --- 模块级常量:禁止把这些数字散落进逻辑里 -------------------------------------
_BACKOFF_FACTOR = 2.0  # 第 n 次重试等待 base_delay * 2^n 秒
_CACHE_SUFFIX, _CACHE_TMP_SUFFIX = ".json", ".tmp"
_TMP_TOKEN_LEN = 8  # 临时文件名里的随机后缀长度,足够躲开并发写的撞名
_SERVER_ERROR_MIN_STATUS = 500

# --- langchain 全局缓存适配层用到的常量 ------------------------------------------
# _get_llm_string 的格式是 `json.dumps(序列化后的模型) + "---" + str(sorted(调用参数))`。
_LLM_STRING_SEPARATOR = "---"
_UNKNOWN_MODEL_NAME = "unknown-model"  # 模型名抠不出来时的占位,只影响缓存文件里的 meta 可读性
# langchain 序列化对象的两个标志字段:有 "lc" 说明这是个 Serializable,正文都在 "kwargs" 里。
_LC_MARKER, _LC_KWARGS = "lc", "kwargs"
# 参与缓存键的消息字段:只留「会改变模型答案」的那些,其余一律当噪声剥掉(理由见文件顶部图 0.5)。
_SIGNIFICANT_MESSAGE_FIELDS = frozenset({"content", "type", "name", "tool_calls", "tool_call_id"})
# 工具调用 id 的两个落点,以及归一化后的别名前缀(理由见文件顶部图 0.5 第二段)。
_TOOL_CALL_ID_FIELD, _TOOL_CALLS_FIELD, _ID_FIELD = "tool_call_id", "tool_calls", "id"
_TOOL_CALL_ID_ALIAS_PREFIX = "tc"
_SINGLE_GENERATION = 1  # 本项目从不做 n>1 的多候选采样,缓存文件格式也只存一条

# 「等一等就好」的状态码;5xx 由 _SERVER_ERROR_MIN_STATUS 统一兜底,不必逐个列。
_RETRYABLE_STATUS: dict[int, ErrorCode] = {429: ErrorCode.RATE_LIMITED}

# 重试也不会变好的状态码:参数错、密钥错、模型名错。立刻失败,别烧额度。
_FATAL_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_INPUT,
    401: ErrorCode.UPSTREAM_ERROR,
    403: ErrorCode.UPSTREAM_ERROR,
    404: ErrorCode.NOT_FOUND,
    422: ErrorCode.INVALID_INPUT,
}

# LLM 场景专用文案:比 errors.DEFAULT_USER_MSG 更贴合「问 AI 没问出来」这件事,故单独维护。
_USER_MSG_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.TIMEOUT: "AI 助手这次没及时回话,请稍等一下再问一遍。",
    ErrorCode.RATE_LIMITED: "现在用 AI 助手的人太多,正在排队,请过一会儿再试。",
    ErrorCode.UPSTREAM_ERROR: "AI 助手暂时连不上,请稍后再试;要是一直不行就找管理员看看。",
    ErrorCode.INVALID_INPUT: "这次的内容 AI 助手没看明白,请换个说法再问一遍。",
    ErrorCode.NOT_FOUND: "找不到对应的 AI 模型,请让管理员检查一下配置。",
    ErrorCode.EMPTY_RESULT: "AI 助手这次没给出回答,请再问一遍。",
    ErrorCode.INTERNAL: "系统开小差了,请稍后再试一次。",
}
_FALLBACK_USER_MSG = "AI 助手出了点问题,请稍后再试一次。"


class MissingAPIKeyError(RuntimeError):
    """.env 里没配对应供应商的密钥。user_msg 会指名道姓说清该填哪个变量。"""

    def __init__(self, provider_display: str, env_var: str) -> None:
        self.provider_display = provider_display
        self.env_var = env_var
        self.user_msg = (
            f"还没有配置 {provider_display} 的接口密钥。请在项目根目录的 .env 文件里"
            f"加上一行 {env_var}=你的密钥,保存后重启服务。"
        )
        super().__init__(self.user_msg)


class LLMCallError(RuntimeError):
    """大模型调用最终失败(重试用尽,或撞上重试也没用的错误)。"""

    def __init__(self, user_msg: str, error_code: ErrorCode) -> None:
        self.user_msg = user_msg
        self.error_code = error_code
        super().__init__(user_msg)


class _ProviderConfig(NamedTuple):
    """一家供应商的凭据与端点。NamedTuple 天然不可变,杜绝被下游偷偷改掉。"""

    display_name: str
    api_key: str
    base_url: str
    key_field: str

    def __repr__(self) -> str:
        """遮蔽 api_key。

        NamedTuple 的默认 __repr__ 会把所有字段原样打出来(含 api_key='sk-...'),
        一旦有人在排查时 print/log 这个对象,明文密钥就进日志了。
        Settings 那边已经用 repr=False 挡过一道,这里是复制品,必须同样挡住。
        """
        return (
            f"_ProviderConfig(display_name={self.display_name!r}, api_key='***', "
            f"base_url={self.base_url!r}, key_field={self.key_field!r})"
        )


def _env_var_name(field_name: str, settings: Settings) -> str:
    """由 Settings 的 env_prefix 推出环境变量名,免得把 "GYT_" 前缀硬编码进文案。"""
    prefix = getattr(type(settings), "model_config", {}).get("env_prefix", "")
    return f"{prefix}{field_name}".upper()


def _resolve_provider(purpose: Purpose, settings: Settings) -> tuple[_ProviderConfig, str]:
    """用途 -> (供应商配置, 模型名)。模型名一律从配置取,绝不写死在代码里。"""
    if purpose == "text":
        deepseek = _ProviderConfig(
            display_name="DeepSeek(深度求索)",
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            key_field="deepseek_api_key",
        )
        return deepseek, settings.model_text
    if purpose not in ("vision", "tool"):
        raise ValueError(f"不支持的模型用途:{purpose!r},只能是 'text' / 'vision' / 'tool'。")
    # 视觉与「工具调用备胎」共用 Kimi 的同一套凭据,区别只在模型名。
    moonshot = _ProviderConfig(
        display_name="Kimi(月之暗面)",
        api_key=settings.moonshot_api_key,
        base_url=settings.moonshot_base_url,
        key_field="moonshot_api_key",
    )
    model = settings.model_vision if purpose == "vision" else settings.model_tool_fallback
    return moonshot, model


def get_chat_model(purpose: Purpose = "text", **overrides: Any) -> BaseChatModel:
    """按用途造 chat model:text 走 DeepSeek,vision / tool 走 Kimi。

    overrides 直接透传 ChatOpenAI 且优先级最高;密钥没配抛 MissingAPIKeyError,用途写错抛 ValueError。

    副作用(有意为之):顺手把磁盘缓存装成 langchain 的全局缓存。装在这里而不是
    graph.py / base_agent.py,是因为**所有**拿模型的地方都必经此处 —— 谁都不会忘,
    也不用让 5 个 Agent 各自记得调一次。详见文件顶部图 0。

    ⚠️ 两处刻意行为,别顺手"修"掉:
      1. max_retries = 配置值(对契约 v1 的偏离),原因见下面的注释;
      2. **不传 cache=**。ChatOpenAI 的 cache 保持默认 None 才会走全局缓存 ——
         langchain 的判定是 `check_cache = self.cache or self.cache is None`
         (chat_models.py:2034),写成 cache=False 会把整条缓存链路关掉。
    """
    settings = get_settings()
    install_llm_cache(settings)
    provider, model_name = _resolve_provider(purpose, settings)
    if not provider.api_key:
        raise MissingAPIKeyError(provider.display_name, _env_var_name(provider.key_field, settings))

    params: dict[str, Any] = {
        "model": model_name,
        "api_key": provider.api_key,
        "base_url": provider.base_url,
        "timeout": settings.llm_timeout_s,
        # 契约 v1 写的是 max_retries=0,理由是"重试由我们自己做,避免双重重试"。
        # 但那个前提在真实执行路径上**不成立**(见文件顶部「图 0」):
        # Agent / Supervisor 的模型调用由 langgraph 内部直接 model.ainvoke(),
        # 根本不经过本模块的 ainvoke(),于是 0 的实际效果是"线上完全没有重试"——
        # 一次 429 或 502 就把这一轮打穿,用户看到英文 traceback 而不是我们写好的中文。
        # 在把重试真正下沉到模型层之前,这里必须交回给 langchain(openai SDK 自带
        # 指数退避且会读 Retry-After),这是当前路径上唯一活着的重试层。
        "max_retries": settings.llm_max_retries,
        # 钉死 Chat Completions:两家供应商都没实现 Responses API(见文件顶部核实记录)。
        "use_responses_api": False,
    }
    # 路由这类场景要的是低延迟而不是长篇推理,所以文本模型默认关掉思考模式。
    # 字面量每次新建,不共享可变默认值(不可变优先)。
    if purpose == "text" and settings.disable_thinking_for_text:
        params["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**{**params, **overrides})


def _stable(value: Any) -> Any:
    """递归转成「同内容必同哈希」的结构。

    字典按键排序;集合按 repr 排序(集合无序,不排就会算出飘忽的键);消息只取影响模型输出的字段,
    忽略 id / 时间戳这类噪声;认不出的对象退化成 repr —— 宁可少命中缓存,也绝不误命中别人的答案。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, BaseMessage):
        return {
            "type": value.type,
            "name": value.name,
            "content": _stable(value.content),
            "tool_calls": _stable(getattr(value, "tool_calls", [])),
        }
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda kv: str(kv[0]))
        return {str(k): _stable(v) for k, v in ordered}
    if isinstance(value, (set, frozenset)):
        return [_stable(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    return repr(value)


def _stable_messages(messages: MessagesInput) -> list[Any]:
    """把各种形态的消息输入统一成列表,再逐条稳定化。"""
    if isinstance(messages, (str, BaseMessage, Mapping)):
        return [_stable(messages)]
    try:
        items = list(messages)
    except TypeError:  # 不可迭代的怪东西,当成单条处理,别在这里炸
        return [_stable(messages)]
    return [_stable(item) for item in items]


class _CacheStamp(NamedTuple):
    """一次调用的缓存身份:键 + 用来做二次核对的三个维度。

    为什么光有键不够(见文件顶部图 3):键只体现在**文件名**上,文件正文里没有任何
    能证明"这份答案确实是这个问题的答案"的信息。谁能往 cache_dir 里写文件,谁就能
    决定 Agent 说什么 —— 对一个判断工地有没有危险的产品,这个后果不可接受。
    """

    key: str
    model: str
    prompt_version: str
    messages_digest: str


def _cache_stamp(model_name: str, messages: MessagesInput, extra: str = "") -> _CacheStamp:
    """算出缓存键与随件回写的核对维度(组成见文件顶部图 2)。

    键里含 prompt_version:改了提示词就改配置里的版本号,旧缓存自然全失效,不会拿旧答案糊弄人(D18)。
    """
    settings = get_settings()
    stable_messages = _stable_messages(messages)
    payload = {
        "model": model_name,
        "prompt_version": settings.prompt_version,
        "messages": stable_messages,
        "extra": extra,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    messages_raw = json.dumps(
        stable_messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return _CacheStamp(
        key=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        model=model_name,
        prompt_version=settings.prompt_version,
        messages_digest=hashlib.sha256(messages_raw.encode("utf-8")).hexdigest(),
    )


def cache_key(model_name: str, messages: MessagesInput, extra: str = "") -> str:
    """算缓存键(契约要求的公开入口)。组成见文件顶部图 2。"""
    return _cache_stamp(model_name, messages, extra).key


def _cache_path(key: str, settings: Settings) -> Path:
    """cache_dir 是 Settings 的只读属性,访问时会自动把目录建好。"""
    return settings.cache_dir / f"{key}{_CACHE_SUFFIX}"


def _meta_matches(meta: Any, stamp: _CacheStamp) -> bool:
    """核对缓存文件里自带的身份信息与本次请求是否一致。

    ⚠️ 这四项**全是关于「问题」的**,一项都不核对答案。答案正文由 _answer_matches
    单独核对,两者缺一不可 —— 只核对这四项的话,把 message.content 换掉、meta
    一个字节不动,伪造内容就会原样流出(见文件顶部图 3)。
    """
    if not isinstance(meta, Mapping):
        return False
    return (
        meta.get("key") == stamp.key
        and meta.get("model") == stamp.model
        and meta.get("prompt_version") == stamp.prompt_version
        and meta.get("messages_digest") == stamp.messages_digest
    )


def _answer_digest(message_payload: Any) -> str:
    """对答案正文(AIMessage 的 json 形态)算摘要,写侧落进 meta、读侧拿来比对。"""
    raw = json.dumps(message_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _answer_matches(meta: Any, message_payload: Any) -> bool:
    """核对正文没被人改过:meta 里的 answer_digest 必须与实际正文算出来的一致。

    挡住「改正文不改 meta」这条最省事的投毒路径。挡不住连 answer_digest
    一起重算的人 —— 那要 HMAC,见文件顶部图 3 的说明。
    """
    if not isinstance(meta, Mapping):
        return False
    return meta.get("answer_digest") == _answer_digest(message_payload)


def _cache_read(stamp: _CacheStamp, settings: Settings) -> AIMessage | None:
    """读缓存并**核对身份**,对不上一律按未命中处理。

        <hex>.json ── 读出来 ──► {"meta": {...}, "message": {...}}
                                      |
                                      +-- meta 四项与本次请求全等? --否--> warning + 未命中
                                      |                                   (不许拿来当答案)
                                      +-- 是 --> answer_digest 与正文对得上? --否--> 未命中
                                                        |
                                                        +-- 是 --> AIMessage(**message)

    任何异常(文件损坏/字段对不上/权限不足/缓存目录整个建不出来)同样降级成「未命中」——
    缓存只是加速手段,坏了就当没有,绝不能让它把正事搞崩。所以连 _cache_path
    (它会 mkdir,失败抛 RuntimeError)也必须待在 try 里面。
    """
    try:
        path = _cache_path(stamp.key, settings)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or not _meta_matches(payload.get("meta"), stamp):
            # 可能是旧格式的遗留文件,也可能是有人往 cache_dir 里塞了伪造答案。
            # 两种情况都只有一个正确处理方式:当没命中,老老实实去问模型。
            logger.warning("大模型缓存身份核对不通过,按未命中处理(键 %s)", stamp.key)
            return None
        if not _answer_matches(payload.get("meta"), payload.get("message")):
            logger.warning("大模型缓存正文与摘要对不上,疑似被改过,按未命中处理(键 %s)", stamp.key)
            return None
        return AIMessage(**payload["message"])
    except Exception:  # noqa: BLE001 —— 见 docstring:缓存故障必须降级,不许上抛
        logger.warning("大模型缓存读取失败,按未命中处理(键 %s)", stamp.key, exc_info=True)
        return None


def _cache_write(stamp: _CacheStamp, message: AIMessage, settings: Settings) -> None:
    """原子写缓存:先写随机名 .tmp,再 os.replace 改名。写失败只告警,不影响已拿到的回答。

    正文里连同答案一起落 meta(键 / 模型 / 提示词版本 / 消息摘要 / **答案摘要**),供读侧核对。
    与 _cache_read 同理:路径计算也在 try 里面,缓存目录建不出来只当没有缓存。
    """
    tmp_path: Path | None = None
    try:
        path = _cache_path(stamp.key, settings)
        tmp_path = path.with_name(f"{stamp.key}.{uuid4().hex[:_TMP_TOKEN_LEN]}{_CACHE_TMP_SUFFIX}")
        dumped = message.model_dump(mode="json")
        meta = {**stamp._asdict(), "answer_digest": _answer_digest(dumped)}
        body = json.dumps({"meta": meta, "message": dumped}, ensure_ascii=False)
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:  # noqa: BLE001 —— 同上,缓存写失败不是业务失败
        logger.warning("大模型缓存写入失败,主流程继续(键 %s)", stamp.key, exc_info=True)
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()


def clear_cache() -> int:
    """清空磁盘缓存,返回删掉的缓存条目数(.tmp 残留会顺手清掉但不计数)。"""
    settings = get_settings()
    removed = 0
    for path in sorted(settings.cache_dir.glob("*")):
        if not path.is_file() or path.suffix not in (_CACHE_SUFFIX, _CACHE_TMP_SUFFIX):
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("缓存文件删除失败,已跳过:%s", path, exc_info=True)
            continue
        if path.suffix == _CACHE_SUFFIX:
            removed += 1
    return removed


# ===========================================================================
# langchain 全局缓存适配层(TODO-5 收口件)
#
# 这一层的全部工作,就是把 langchain 的 (prompt, llm_string) 二元组翻译成本模块
# 上面那套 _cache_stamp / _cache_read / _cache_write。磁盘格式、原子写、身份核对
# 一个字都不重写 —— 路径甲(Agent)与路径乙(直调)共用同一份实现与同一个目录。
#
#   langchain _agenerate_with_cache
#         │  prompt   = dumps(消息列表,id 已被置 None)
#         │  llm_string = _get_llm_string(stop, **kwargs)  ← 自动含 tools / tool_choice
#         ▼
#   GytDiskCache.alookup / aupdate
#         │  prompt ──► json 解析 ──► _strip_prompt_noise(剥运行时噪声,见图 0.5)
#         │  llm_string ──► 整串塞进 _cache_stamp 的 extra 维度(只参与算键,不落盘)
#         ▼
#   _cache_stamp ──► _cache_read / _cache_write ──► cache_dir/<sha256>.json
# ===========================================================================


def _strip_prompt_noise(value: Any) -> Any:
    """递归剥掉 langchain 序列化消息里的运行时噪声,只留会改变模型答案的字段。

    只对「带 lc 标志的序列化对象」动手,而且只筛它的 kwargs —— 对象自身那个
    `"id": ["langchain","schema","messages","HumanMessage"]` 是**类型标识**,
    跟消息 id 同名但不是一回事,剥错了 HumanMessage 和 AIMessage 就会撞进同一个键。

        [{"lc":1,"id":[...类路径...],"kwargs":{content, type, id, usage_metadata,...}}]
                     └── 保留 ──┘        └── 只留 _SIGNIFICANT_MESSAGE_FIELDS ──┘
    """
    if isinstance(value, list):
        return [_strip_prompt_noise(item) for item in value]
    if not isinstance(value, dict):
        return value
    is_lc_object = _LC_MARKER in value
    stripped: dict[str, Any] = {}
    for key, item in value.items():
        if is_lc_object and key == _LC_KWARGS and isinstance(item, dict):
            stripped[key] = {
                k: _strip_prompt_noise(v)
                for k, v in item.items()
                if k in _SIGNIFICANT_MESSAGE_FIELDS
            }
            continue
        stripped[key] = _strip_prompt_noise(item)
    return stripped


def _tool_call_alias(raw: str, aliases: dict[str, str]) -> str:
    """把一个工具调用 id 换成「它在本 prompt 里第几个出现」的别名(tc0 / tc1 / …)。"""
    if raw not in aliases:
        aliases[raw] = f"{_TOOL_CALL_ID_ALIAS_PREFIX}{len(aliases)}"
    return aliases[raw]


def _renumber_one_tool_call(call: Any, aliases: dict[str, str]) -> Any:
    """处理 tool_calls 列表里的单个工具调用:只换它的 id,其余字段原样递归。"""
    if not isinstance(call, dict):
        return _renumber_tool_call_ids(call, aliases)
    return {
        key: (
            _tool_call_alias(item, aliases)
            if key == _ID_FIELD and isinstance(item, str)
            else _renumber_tool_call_ids(item, aliases)
        )
        for key, item in call.items()
    }


def _renumber_tool_call_ids(value: Any, aliases: dict[str, str]) -> Any:
    """把工具调用 id 按首次出现顺序换成 tc0 / tc1 / ……(理由见文件顶部图 0.5 第二段)。

        [AIMessage  tool_calls=[{id:"9f2c-…"}]      ]  ->  [{id:"tc0"}]
         ToolMessage tool_call_id="9f2c-…"           ->   tool_call_id="tc0"
         AIMessage  tool_calls=[{id:"6d82-…"}]       ->  [{id:"tc1"}]

    只动这两个落点:``tool_call_id`` 字段,以及 ``tool_calls`` 列表里每一项的 ``id``。
    别处的 ``id``(尤其是 lc 序列化对象自身那个类路径 id)一律不碰 —— 剥错了
    HumanMessage 和 AIMessage 就会撞进同一个键。
    """
    if isinstance(value, list):
        return [_renumber_tool_call_ids(item, aliases) for item in value]
    if not isinstance(value, dict):
        return value
    renumbered: dict[str, Any] = {}
    for key, item in value.items():
        if key == _TOOL_CALL_ID_FIELD and isinstance(item, str):
            renumbered[key] = _tool_call_alias(item, aliases)
        elif key == _TOOL_CALLS_FIELD and isinstance(item, list):
            renumbered[key] = [_renumber_one_tool_call(call, aliases) for call in item]
        else:
            renumbered[key] = _renumber_tool_call_ids(item, aliases)
    return renumbered


def _normalize_prompt(prompt: str) -> Any:
    """把 langchain 传来的 prompt 字符串解析并归一化,失败就原样用。

    两步:先剥运行时噪声,再把工具调用 id 按位置重编号(两步的理由都在文件顶部图 0.5)。

    解析不了时**不**报错也**不**跳过缓存:原样参与算键,顶多少命中几次,
    绝不会误命中别人的答案 —— 这是本模块贯穿始终的取舍。
    """
    try:
        stripped = _strip_prompt_noise(json.loads(prompt))
    except (ValueError, TypeError):
        logger.debug("大模型缓存:prompt 不是合法 JSON,按原文参与算键")
        return prompt
    return _renumber_tool_call_ids(stripped, {})


def _model_name_from_llm_string(llm_string: str) -> str:
    """从 llm_string 里尽力抠出模型名,只为让缓存文件的 meta 可读、可排查。

    抠不出来也无所谓:llm_string 整串已经在 extra 维度里参与算键了,
    模型换了键一定会变,不依赖这里抠得准不准。
    """
    head = llm_string.split(_LLM_STRING_SEPARATOR, 1)[0]
    try:
        kwargs = json.loads(head).get(_LC_KWARGS)
    except (ValueError, TypeError, AttributeError):
        return _UNKNOWN_MODEL_NAME
    if not isinstance(kwargs, Mapping):
        return _UNKNOWN_MODEL_NAME
    for field in ("model_name", "model"):
        value = kwargs.get(field)
        if isinstance(value, str) and value:
            return value
    return _UNKNOWN_MODEL_NAME


def _stamp_for_llm_cache(prompt: str, llm_string: str) -> _CacheStamp:
    """langchain 的 (prompt, llm_string) -> 本模块的缓存身份(组成见文件顶部图 2)。"""
    return _cache_stamp(
        _model_name_from_llm_string(llm_string),
        _normalize_prompt(prompt),
        llm_string,
    )


def _single_ai_message(generations: Sequence[Generation]) -> AIMessage | None:
    """从 langchain 的结果里取出唯一那条 AIMessage;取不到就返回 None(= 本次不缓存)。

    只认「一问一答」:本项目从不用 n>1 的多候选采样,而缓存文件格式是一键一条。
    真遇上多条,宁可不缓存,也不能落一份读回来会缺斤少两的答案。
    流式路径攒出来的是 AIMessageChunk,先合并回普通 AIMessage 再落盘 ——
    否则 chunk 独有的字段会让读侧 AIMessage(**payload) 直接炸,退化成"永远不命中"。
    """
    if len(generations) != _SINGLE_GENERATION:
        logger.debug("大模型缓存:本次结果有 %d 条,不落盘", len(generations))
        return None
    message = getattr(generations[0], "message", None)
    if isinstance(message, AIMessageChunk):
        message = message_chunk_to_message(message)
    return message if isinstance(message, AIMessage) else None


class GytDiskCache(BaseCache):
    """工友通的 langchain 全局缓存实现:一键一个 json 文件,落在 settings.cache_dir。

    装上之后,create_agent / create_supervisor 内部的每一次模型调用都会先查这里 ——
    这就是「演示断网时靠彩排跑过的缓存复演」(方案 D9 三层兜底的第二层)的兑现方式。

    异步方法直接转调同步实现,没走 BaseCache 默认的 run_in_executor:
    缓存文件是几 KB 的小 json,一次读写在微秒级,为它起线程池不划算,
    而且线程池里再出异常的排查成本远高于省下的这点时间。

    对 langchain 的契约:**这四个方法永不上抛**。它们被接在
    _agenerate_with_cache 上,一旦抛异常就会把用户正在进行中的那一轮对话打穿,
    而缓存本该只是个可有可无的加速层。所以最外层再兜一道 except。
    """

    def lookup(self, prompt: str, llm_string: str) -> Sequence[Generation] | None:
        """查缓存。返回 None = 未命中,langchain 会照常去问模型。"""
        try:
            settings = get_settings()
            if not settings.llm_cache_enabled:
                return None
            stamp = _stamp_for_llm_cache(prompt, llm_string)
            cached = _cache_read(stamp, settings)
            if cached is None:
                return None
            logger.debug("命中大模型缓存(Agent 路径):%s", stamp.key)
            return [ChatGeneration(message=cached)]
        except Exception:  # noqa: BLE001 —— 见类 docstring:对 langchain 永不上抛
            logger.warning("大模型缓存查询异常,按未命中处理", exc_info=True)
            return None

    def update(self, prompt: str, llm_string: str, return_val: Sequence[Generation]) -> None:
        """写缓存。写失败只告警,不影响已经拿到的回答(_cache_write 内部已保证)。"""
        try:
            settings = get_settings()
            if not settings.llm_cache_enabled:
                return
            message = _single_ai_message(return_val)
            if message is None:
                return
            _cache_write(_stamp_for_llm_cache(prompt, llm_string), message, settings)
        except Exception:  # noqa: BLE001 —— 同上
            logger.warning("大模型缓存写入异常,主流程继续", exc_info=True)

    def clear(self, **_kwargs: Any) -> None:
        """清空缓存目录(与 clear_cache() 同一实现,别再造第二份删除逻辑)。"""
        clear_cache()

    async def alookup(self, prompt: str, llm_string: str) -> Sequence[Generation] | None:
        """异步查缓存 —— Agent 路径走的是这一条,必须自己实现,理由见类 docstring。"""
        return self.lookup(prompt, llm_string)

    async def aupdate(self, prompt: str, llm_string: str, return_val: Sequence[Generation]) -> None:
        """异步写缓存 —— 同上。"""
        self.update(prompt, llm_string, return_val)

    async def aclear(self, **kwargs: Any) -> None:
        """异步清空缓存。"""
        self.clear(**kwargs)


def install_llm_cache(settings: Settings | None = None) -> BaseCache | None:
    """把 GytDiskCache 装成 langchain 的进程级全局缓存,返回当前生效的缓存对象。

    幂等:已经装过就原样返回,不会每建一个模型就换一个新实例。
    配置里关掉缓存时会**只**卸掉自己装的那个 —— 别人(比如测试或未来的其它模块)
    装的缓存不动,这个全局单例不归我们独占。

        llm_cache_enabled?
            │否── 当前是我们装的? ──是──► set_llm_cache(None),返回 None
            │                    └─否──► 什么都不做,返回 None
            └是── 当前是我们装的? ──是──► 原样返回
                                 └─否──► set_llm_cache(GytDiskCache())
    """
    settings = settings if settings is not None else get_settings()
    current = get_llm_cache()
    if not settings.llm_cache_enabled:
        if isinstance(current, GytDiskCache):
            set_llm_cache(None)
        return None
    if isinstance(current, GytDiskCache):
        return current
    cache = GytDiskCache()
    set_llm_cache(cache)
    # 这里**故意不打 settings.cache_dir**:日志实参是立即求值的,而 cache_dir 是个会
    # mkdir 的属性,建不出来就抛 RuntimeError。本函数被 get_chat_model 无条件调用,
    # 一旦从这里抛出去,缓存目录有问题就会让整张图连建都建不出来 ——
    # 直接违反「缓存只是加速手段,坏了就当没有」这条铁律。目录到底在哪由 data_dir 推得出来。
    logger.debug("已装载大模型磁盘缓存(目录由 data_dir 派生:%s)", settings.data_dir)
    return cache


def _status_code_of(exc: BaseException) -> int | None:
    """从各家 SDK 的异常里把 HTTP 状态码抠出来(openai / httpx 的字段名并不统一)。"""
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


def _classify(exc: BaseException) -> tuple[bool, ErrorCode]:
    """异常归类 -> (是否值得重试, 错误码)。判定顺序先窄后宽,避免误判:

    超时(TimeoutError 或类名含 Timeout) --------> 重试,  TIMEOUT
    取到状态码 +-- 命中 _FATAL_STATUS ----------> 不重试, 表里的码
               +-- 命中 _RETRYABLE_STATUS -----> 重试,  表里的码
               +-- >= 500 --------------------> 重试,  UPSTREAM_ERROR
               +-- 其余 4xx ------------------> 不重试, UPSTREAM_ERROR
    连接类(ConnectionError/OSError/类名含 Connect) --> 重试, UPSTREAM_ERROR
    其余未知异常 -------------------------------> 不重试, UPSTREAM_ERROR
                                                 (多半是自家代码 bug,重试纯浪费额度)
    """
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return True, ErrorCode.TIMEOUT
    status = _status_code_of(exc)
    if status is not None:
        if status in _FATAL_STATUS:
            return False, _FATAL_STATUS[status]
        if status in _RETRYABLE_STATUS:
            return True, _RETRYABLE_STATUS[status]
        if status >= _SERVER_ERROR_MIN_STATUS:
            return True, ErrorCode.UPSTREAM_ERROR
        return False, ErrorCode.UPSTREAM_ERROR
    if isinstance(exc, (ConnectionError, OSError)) or "Connect" in name:
        return True, ErrorCode.UPSTREAM_ERROR
    return False, ErrorCode.UPSTREAM_ERROR


def _user_msg(code: ErrorCode) -> str:
    """错误码 -> 中文人话。查不到时给一句通用的,绝不把英文错误名甩给用户。"""
    return _USER_MSG_BY_CODE.get(code, _FALLBACK_USER_MSG)


async def _sleep(seconds: float) -> None:
    """退避等待。单独抽成函数,测试里替换掉它就不会真的干等。"""
    await asyncio.sleep(seconds)


def _model_name_of(model: BaseChatModel) -> str:
    """取模型名用于组缓存键 —— 不同模型的回答必须落在不同的键上,不能串味。"""
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def _ensure_ai_message(result: Any) -> AIMessage:
    """上游偶尔会返回 None 或非 AIMessage,这里挡一道,不让脏数据流进 Agent。"""
    if isinstance(result, AIMessage):
        return result
    logger.error("大模型返回了非 AIMessage 结果,类型=%s", type(result).__name__)
    raise LLMCallError(_user_msg(ErrorCode.EMPTY_RESULT), ErrorCode.EMPTY_RESULT)


async def _invoke_with_retry(
    model: BaseChatModel,
    messages: MessagesInput,
    settings: Settings,
    max_attempts: int | None = None,
) -> AIMessage:
    """带指数退避的实际调用(流程见文件顶部图 1)。

    max_attempts 不传就用 1 + llm_max_retries(默认 4 次)。
    调用方在**单次调用本身就很慢**时应该压低它 —— 重试次数是乘在超时上的,
    详见 ainvoke 的同名参数。
    """
    total_attempts = max(1, max_attempts or (1 + max(0, settings.llm_max_retries)))
    last_error: LLMCallError | None = None
    last_exc: BaseException | None = None

    for attempt in range(total_attempts):
        try:
            result = await model.ainvoke(messages)
        except LLMCallError:  # 已经是带中文文案的自家错误,原样上抛,别二次包装
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一归类后再抛带中文文案的错误
            retryable, code = _classify(exc)
            last_error, last_exc = LLMCallError(_user_msg(code), code), exc
            logger.warning(
                "大模型调用失败(第 %d/%d 次,错误码 %s):%s",
                attempt + 1,
                total_attempts,
                code.value,
                exc,
            )
            if not retryable:
                logger.error("该错误重试也不会好,立即失败,不再消耗剩余轮次", exc_info=exc)
                raise last_error from exc
            if attempt + 1 < total_attempts:
                await _sleep(settings.llm_retry_base_delay_s * (_BACKOFF_FACTOR**attempt))
            continue
        return _ensure_ai_message(result)

    # 走到这里说明每一轮都抛了可重试异常且轮次已耗尽。
    logger.error("大模型调用连续 %d 次都失败,已放弃", total_attempts)
    upstream = ErrorCode.UPSTREAM_ERROR
    raise (last_error or LLMCallError(_user_msg(upstream), upstream)) from last_exc


def _for_direct_call(model: BaseChatModel) -> BaseChatModel:
    """给路径乙做一份关掉全局缓存的模型副本(不改调用方手里那个实例)。

    为什么必须关:全局缓存的键是 (prompt, llm_string),里面**没有 cache_extra**。
    不关的话,两个 cache_extra 不同、消息相同的调用在外层各算各的键(看起来隔开了),
    到了内层却撞进同一条全局缓存 —— 第二个调用方原样取走第一个的答案,零次网络调用,
    日志里只有一行 debug。评测打分器与知识抽取正是这么用路径乙的,串了就是
    「拿上一次的摘要文本当判分结果」。关掉之后路径乙只剩自己那一层键(带 cache_extra),
    顺带也消除了「一次问答落 3 个缓存文件」的双写。

    判定见 chat_models.py:2034 `check_cache = self.cache or self.cache is None`:
    **False 才关得掉**,None 反而是"走全局"—— 别顺手把它改成 None。

    ⚠️ 这里**不**顺手关 max_retries(2026-08-06 实测过,关不掉):
        ChatOpenAI 的 max_retries 是在 validate_environment 里被烤进 openai 客户端的
        (base.py:1265 `client_params["max_retries"] = self.max_retries`),
        而 model_copy 不会重跑 validate_environment,副本用的还是同一个客户端对象。
        实测:字段 3 -> 0,但 root_async_client.max_retries 仍然是 3。
        也就是说路径乙目前仍是「自研退避 4 次 × SDK 退避 4 次」两层叠加,
        最坏 16 个 HTTP 请求。要真关掉只能在**造模型时**传
        `get_chat_model("text", max_retries=0)`,调用方按需自己传;
        或者等以后把重试整体下沉到模型层(见 TODOS)。

    不是 pydantic 模型、或没有 cache 字段就原样返回 —— 传进来的可能是测试替身,
    也可能是以后换供应商时的别家实现,不能因为"改不动"就把调用打断。
    """
    if "cache" not in getattr(type(model), "model_fields", {}):
        return model
    if not hasattr(model, "model_copy"):
        return model
    return model.model_copy(update={"cache": False})


async def ainvoke(
    model: BaseChatModel,
    messages: MessagesInput,
    *,
    cache_extra: str = "",
    is_usable: Callable[[AIMessage], bool] | None = None,
    max_attempts: int | None = None,
) -> AIMessage:
    """**直调**大模型:先查缓存,未命中再走带退避的真实调用(图 1),成功后回写缓存。

    ⚠️ 适用范围(别搞混,见文件顶部图 0):这是**路径乙**,给「不经过 Agent 的一次性问答」用 ——
    评测打分、知识综合里的单次抽取之类。Supervisor 和 5 个子 Agent 走的是路径甲
    (langchain 内部自己调模型 + GytDiskCache 全局缓存),**不会**经过这个函数,
    所以这里的中文错误文案在 Agent 路径上是看不到的。

    两条路径共用同一个缓存目录与同一套文件格式/身份核对,但缓存键的构成维度不同
    (路径甲的键里有 llm_string 没有 cache_extra,路径乙反之)。为了让这两套键**不叠加**,
    路径乙调模型前会先把库自带的全局缓存关掉,详见 _for_direct_call ——
    不关的话 cache_extra 这个防串味维度会被内层全局缓存整个架空。

    cache_extra 是参与缓存键的额外维度(如 Agent 名),防串味;失败抛 LLMCallError,.user_msg 是中文。

    ``is_usable`` 是**写缓存前的最后一道闸**:返回 False 的回答照常返回给调用方,
    但**不落盘**。不传就是原行为(一律缓存),所以对既有调用方完全无影响。

    为什么需要它(2026-08-07 实测确认的一个真洞):
        调用方拿到回答后往往还要再解析一层(safety 要从里面抠 JSON)。解析失败时
        它会返回「这次没看明白,请再试一次」——**但用户照做也永远不会成功**:
        那条坏回答已经被写进缓存,第二次直接命中,0 次网络调用、0.005 秒,
        一字不差的同一句失败。这张照片对该 prompt_version 就被永久钉死了。
        重试次数再多也没用,因为根本没发出请求。

        更糟的是缓存键算在**消息内容**上(图片以 base64 进 messages),
        所以让用户「重新传一次同一个文件」也逃不掉 —— 新的 artifact_id、
        同一个缓存键。只有重新拍一张(字节不同)才行。

        而唯一的清理手段(clear_cache 或换 prompt_version)会把**整份**缓存烧掉,
        与 TODO-11「演示日必须预热缓存」正面冲突:为救一张图得毁掉全部预热。
        何况预热跑的就是这条路径 —— 预热时抽到一次坏输出,就把失败烤进去了。

        自觉的代价:提示词系统性坏掉时,每一次调用都会真的掏钱问模型,
        而不是"坏答案缓存一次了事"。这个取舍是对的 —— 正确性优先于省钱,
        而且它与预热是正向配合:带闸的预热会拒绝把失败烤进缓存。
    """
    settings = get_settings()
    stamp: _CacheStamp | None = None
    if settings.llm_cache_enabled:
        stamp = _cache_stamp(_model_name_of(model), messages, cache_extra)
        cached = _cache_read(stamp, settings)
        if cached is not None:
            logger.debug("命中大模型缓存:%s", stamp.key)
            return cached

    message = await _invoke_with_retry(_for_direct_call(model), messages, settings, max_attempts)
    if stamp is not None:
        if is_usable is None or is_usable(message):
            _cache_write(stamp, message, settings)
        else:
            logger.info("回答未通过调用方校验,不写缓存(避免把失败永久钉住):%s", stamp.key)
    return message


__all__ = [
    "GytDiskCache",
    "LLMCallError",
    "MessagesInput",
    "MissingAPIKeyError",
    "Purpose",
    "ainvoke",
    "cache_key",
    "clear_cache",
    "get_chat_model",
    "install_llm_cache",
]
