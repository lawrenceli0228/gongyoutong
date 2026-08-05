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

⚠️⚠️ 图 0:本模块的重试/缓存**当前不在 Agent 的真实执行路径上**(T1 已知缺口,W2 前必须收口)

    真实路径(Supervisor / 各子 Agent):
        graph.py / base_agent.py --> get_chat_model() --> ChatOpenAI 实例
              |
              +--> 交给 create_supervisor / create_agent
                        |
                        +--> langgraph 内部直接 model.ainvoke(...)   ← 本模块的 ainvoke 没被调用
                                  |
                                  +--> 唯一活着的重试层 = ChatOpenAI 自己的 max_retries
                                       (所以 get_chat_model 里它必须 = settings.llm_max_retries,
                                        契约 v1 写的 0 会导致线上零重试,比不做还差)
                                  +--> 缓存:**没有**。_cache_read 一次都不会被调用,
                                       "断网复演彩排路径"这个承诺目前不成立。

    本模块 ainvoke() 路径(目前只有单测和将来的"直调"场景走):
        ainvoke() --> 磁盘缓存 --> _invoke_with_retry(指数退避,见图 1)

    收口方案(二选一,W2 开工前定,别让 5 个真 Agent 照着 ping 抄一遍同样的空档):
      (a) 下沉到模型层:自定义 BaseChatModel 子类覆写 _agenerate 转调本模块 ainvoke,
          在 get_chat_model 返回前套上 —— create_agent / create_supervisor 内部调用自动享受;
          注意 create_supervisor 需要 bind_tools,包装层必须原样转发。
      (b) 换用官方机制:缓存改 langchain_core.globals.set_llm_cache(需自实现 BaseCache
          复用本模块的 cache_key/_cache_read/_cache_write),重试保持交给 langchain。
    无论选哪条,都要补一个「真实 Agent 调用路径确实经过缓存/重试」的断言测试,
    否则这条链路会一直静默失效到演示日。

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

    <hex>.json = {"meta": {key, model, prompt_version, messages_digest},
                  "message": {AIMessage 的 model_dump}}

    读:命中文件 --> meta 四项与本次请求逐项比对 --+-- 全等 --> 返回 AIMessage
                                                 +-- 任一不等 --> logger.warning + 按未命中处理

    为什么必须核对:cache_dir 落在 ./data 这个宿主机绑定挂载里,而彩排缓存是要被打包、
    拷贝、在队员机器之间传递的"演示资产"。若读侧不核对,任何能往该目录写文件的人,
    只要本地算出同名 hex 再写一份 {"message": {"content": "照片中未发现安全隐患"}},
    Safety Agent 走到同一条消息序列时就会原样返回这句话 —— 零次模型调用,日志里
    只有一行"命中大模型缓存"。对一个判断工地危险的产品,这是不可接受的降级。
    注意核对只防"张冠李戴/整份伪造",不防"能改文件的人连 meta 一起编"——
    那需要 HMAC 签名(密钥不落在同目录),是 W2 之后的事,见 TODOS。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeAlias
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
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

    ⚠️ max_retries 的取值是**对契约 v1 的一处刻意偏离**,原因见下面的注释。
    """
    settings = get_settings()
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
    """核对缓存文件里自带的身份信息与本次请求是否一致。"""
    if not isinstance(meta, Mapping):
        return False
    return (
        meta.get("key") == stamp.key
        and meta.get("model") == stamp.model
        and meta.get("prompt_version") == stamp.prompt_version
        and meta.get("messages_digest") == stamp.messages_digest
    )


def _cache_read(stamp: _CacheStamp, settings: Settings) -> AIMessage | None:
    """读缓存并**核对身份**,对不上一律按未命中处理。

        <hex>.json ── 读出来 ──► {"meta": {...}, "message": {...}}
                                      |
                                      +-- meta 四项与本次请求全等? --否--> warning + 未命中
                                      |                                   (不许拿来当答案)
                                      +-- 是 --> AIMessage(**message)

    任何异常(文件损坏/字段对不上/权限不足)同样降级成「未命中」——
    缓存只是加速手段,坏了就当没有,绝不能让它把正事搞崩。
    """
    path = _cache_path(stamp.key, settings)
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or not _meta_matches(payload.get("meta"), stamp):
            # 可能是旧格式的遗留文件,也可能是有人往 cache_dir 里塞了伪造答案。
            # 两种情况都只有一个正确处理方式:当没命中,老老实实去问模型。
            logger.warning("大模型缓存身份核对不通过,按未命中处理:%s", path)
            return None
        return AIMessage(**payload["message"])
    except Exception:  # noqa: BLE001 —— 见 docstring:缓存故障必须降级,不许上抛
        logger.warning("大模型缓存读取失败,按未命中处理:%s", path, exc_info=True)
        return None


def _cache_write(stamp: _CacheStamp, message: AIMessage, settings: Settings) -> None:
    """原子写缓存:先写随机名 .tmp,再 os.replace 改名。写失败只告警,不影响已拿到的回答。

    正文里连同答案一起落 meta(键 / 模型 / 提示词版本 / 消息摘要),供读侧核对。
    """
    path = _cache_path(stamp.key, settings)
    tmp_path = path.with_name(f"{stamp.key}.{uuid4().hex[:_TMP_TOKEN_LEN]}{_CACHE_TMP_SUFFIX}")
    try:
        body = json.dumps(
            {"meta": stamp._asdict(), "message": message.model_dump(mode="json")},
            ensure_ascii=False,
        )
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:  # noqa: BLE001 —— 同上,缓存写失败不是业务失败
        logger.warning("大模型缓存写入失败,主流程继续:%s", path, exc_info=True)
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
    model: BaseChatModel, messages: MessagesInput, settings: Settings
) -> AIMessage:
    """带指数退避的实际调用(流程见文件顶部图 1)。"""
    total_attempts = 1 + max(0, settings.llm_max_retries)
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


async def ainvoke(
    model: BaseChatModel, messages: MessagesInput, *, cache_extra: str = ""
) -> AIMessage:
    """调用大模型:先查缓存,未命中再走带退避的真实调用,成功后回写缓存。

    cache_extra 是参与缓存键的额外维度(如 Agent 名),防串味;失败抛 LLMCallError,.user_msg 是中文。
    """
    settings = get_settings()
    stamp: _CacheStamp | None = None
    if settings.llm_cache_enabled:
        stamp = _cache_stamp(_model_name_of(model), messages, cache_extra)
        cached = _cache_read(stamp, settings)
        if cached is not None:
            logger.debug("命中大模型缓存:%s", stamp.key)
            return cached

    message = await _invoke_with_retry(model, messages, settings)
    if stamp is not None:
        _cache_write(stamp, message, settings)
    return message


__all__ = [
    "LLMCallError",
    "MessagesInput",
    "MissingAPIKeyError",
    "Purpose",
    "ainvoke",
    "cache_key",
    "clear_cache",
    "get_chat_model",
]
