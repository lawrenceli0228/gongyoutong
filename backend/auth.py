"""工友通(GYT)对外访问闸门 —— 令牌鉴权 + 创建 run 限流。

本文件由 LangGraph 服务在启动时加载(见 ``backend/langgraph.json`` 的 ``auth.path``),
**不是** ``gyt`` 包的一部分:langgraph-api 用 ``importlib.util.spec_from_file_location``
按文件路径把它 import 进来(``langgraph_api/auth/custom.py:770`` 一带)。
所以它放在 backend/ 根、与 langgraph.json 平级,而不是 src/gyt/ 里面。

===========================================================================
为什么要有这个文件
---------------------------------------------------------------------------
``:2024`` 是**零鉴权**的 Agent 执行端点,而两把模型 API Key 就在这个进程里。
``docker-compose.yml`` 把端口钉死在 ``127.0.0.1`` 正是为了挡这个 —— 一旦要给
外部的人做测试,端口必须能被别人摸到,那道物理隔离就没了,得换成软的。

整套门是两道,**不是重复,是两种不同的失效模式**:

    公网 ──▶ Caddy(唯一 publish 端口的服务)
              │  第一道:全局访问口令(basicauth)。给人的门,测试的人只需要这一个。
              │         它挡的是「陌生人根本进不来」。
              ├── /      ──▶ frontend:3000  ┐ 这两个容器都不写 ports,
              └── /api/* ──▶ backend:2024   ┘ 只在 docker 内网互通
                               │
                         第二道:本文件校验 X-Api-Key。
                               │ 它挡的是「Caddy 配错了 / 有人直接打内网端口 /
                               │ 将来某天有人为了调试给 backend 加了 ports」。
                               ▼
                         令牌桶限流(挡刷 run 烧账单)

令牌(``X-Api-Key``)在**构建期**被注进前端包(``NEXT_PUBLIC_API_KEY``),测试的人
不用手动粘。它躺在浏览器包里是可见的 —— 但**能拿到那个包的人已经过了 Caddy 口令**,
所以这个可见性可接受;它防的从来不是「过了第一道门的人」。

===========================================================================
开关语义:``GYT_ACCESS_TOKEN`` 设了才开(以及这个取舍的弱点)
---------------------------------------------------------------------------
    没设 ──► 鉴权关、限流关。启动时打一条醒目的中文警告。
    设了 ──► 鉴权强制、限流生效。

**为什么选「设了才开」而不是「默认开」**:本仓有三条路径必须一字不改照常能跑 ——
``make dev`` / ``make dev-docker`` / ``backend/scripts/live_acceptance.py``(25 断言的
真机验收)。其中验收脚本用的是裸 ``urllib``,请求头里只有 ``Content-Type``
(见该脚本 ``api()`` 函数),**它不会发 X-Api-Key**。默认开等于当场把验收打死,
而红的是脚本不是系统 —— 这种红最坏,它会训练人「这几条本来就红,跳过」。

**这个取舍的弱点说清楚:上公网时忘了设,就是裸奔,而且一声不吭。**
本文件兜不住这一条(它只看得见「有没有」,看不见「该不该有」)。
兜底在部署件那一层:compose 用 ``${GYT_ACCESS_TOKEN:?}`` 这种「没给就拒绝启动」的
写法,让缺失变成起不来,而不是起来了但没锁门。

⚠️ 反过来也有个坑,本机的人要知道:``backend/langgraph.json`` 里写着
``"env": "../.env"``,langgraph-cli 启动时会把**仓库根 .env** 整份灌进 os.environ。
所以只要往仓库根 .env 里写了 ``GYT_ACCESS_TOKEN``,本机 ``make dev`` 也会跟着强制,
``live_acceptance.py`` 立刻 401。VPS 上请走 compose 的 environment 注入,别写进 .env。

===========================================================================
限流卡在哪一层(以及为什么不是 recursion_limit)
---------------------------------------------------------------------------
``supervisor_recursion_limit`` 限的是**单次 run 内部的步数**,防的是 Agent 自己
转圈死循环。它一点都不限**run 的次数** —— 一个脚本每秒发 50 个 run,每个都规规矩矩
只走 3 步,账单照样爆。两件事解决的是完全不同的问题,别拿一个当另一个用。

所以限流挂在**创建 run** 这个动作上:``@auth.on.threads.create_run``。
读线程、列历史、翻记录这些**故意不卡** —— 那些不花钱,测试的人回头翻聊天记录
不该被限流打断。

时序上这个位置是对的(已读 ``langgraph_runtime_inmem/ops.py`` 的 ``Runs.put`` 确认):
``handle_event(ctx, "create_run", ...)`` 在 run 落库、图被调起**之前**跑,
所以被限流拦下的请求一次模型都不会调,不花钱。

===========================================================================
进程内内存实现的局限(不是「做得不好」,是「今天够用」)
---------------------------------------------------------------------------
令牌桶的状态是**这个进程的一个 dict**,于是:

    · 多 worker / 多副本 ──► 每个进程各有一套桶,实际配额 = 配置值 × 进程数;
    · 进程重启          ──► 桶全满,配额白送一轮;
    · 水平扩容后        ──► 彻底失真。

今天的部署是**单容器单进程**(``backend/Dockerfile`` 的 CMD 是一条 ``langgraph dev``),
所以这套够用。下一个人要往上加副本或换成 gunicorn 多 worker 的话,得先把这个桶
换成 Redis 之类的共享计数器,否则限流会静默地按副本数放大 —— 边界在这里,写明白。
"""

from __future__ import annotations

import hmac
import logging
import math
from collections.abc import Mapping
from typing import Any, Final

from langgraph_sdk import Auth

from gyt.config import get_settings
from gyt.core.access import (
    API_KEY_HEADER,
    TokenBucket,
    _access_token,
    _api_key_from_headers,
    _token_problem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 对外契约常量。改这里等于改对外 API,下游(Caddyfile / 前端覆盖件 / 部署文档)
# 要跟着改,别只改一头。
#
# API_KEY_HEADER 本体已搬到 src/gyt/core/access.py(2026-08-15,W7 打卡):
# checkin_api.py 要与本文件共用同一个头名、同一套取值与令牌判定,而两个文件
# 都是被 langgraph-api 按文件路径加载的,互相 import 不可靠 —— gyt 包是唯一
# 共同地面。顶部 import 按原名 re-export,本文件与下游的旧引用一个不断。
# ---------------------------------------------------------------------------

DENY_MESSAGE: Final[str] = "访问被拒绝,请联系发你链接的人。"
"""鉴权失败时**返回给客户端**的唯一文案。

三种失败(没带头 / 带了空串 / 令牌不对)回的是**一模一样**的一句话,这是故意的:
任何差异都是送给爆破脚本的信号(「这条报错不一样,说明我这把长度对了」)。
真实原因只写日志(见 ``authenticate``),这与 ``gyt/core/errors.py`` 的
``detail=`` 哲学同源:内部细节走日志,用户只拿人话。

这句话本身也得是人话 —— 看到它的是工地上的师傅或来试用的人,不是运维。
"""

TOO_MANY_STEPS_MESSAGE: Final[str] = "这条请求要求跑太多步了,换个简单点的问法试试。"
"""客户端自带的 recursion_limit 超上限时的文案。

同样可以说得具体:这不是秘密,而且**正常客户端根本不会触发它** ——
聊天界面从不传 recursion_limit,能撞上这条的一定是自己写脚本的人,
给他一句能看懂的话比一句「访问被拒绝」有用。
"""

RATE_LIMITED_MESSAGE: Final[str] = "问得太快啦,请等几秒再发一条。"
"""被限流时返回给客户端的文案。

这一句可以说得比 ``DENY_MESSAGE`` 具体 —— 「你太快了」不是秘密,
攻击者不用探也知道自己在刷;而正常用户看到「访问被拒绝」会以为自己被封了。
"""

IDENTITY_TESTER: Final[str] = "gyt-tester"
"""强制模式下所有请求共用的身份。

**一个口令 = 一个身份 = 一个令牌桶**,这不是偷懒,是当前形态的必然:
全体测试的人拿的是同一把令牌,后端没有任何办法把他们分开。
桶护的是**账单**,不是人与人之间的公平 —— 这一点想清楚了再看默认值那几个数字。

刻意**不**用令牌本身(或它的哈希)当身份:identity 会进日志、可能进 run 元数据,
把口令的任何指纹带进去,都是白送一个可以离线比对的验证器。
"""

IDENTITY_LOCAL: Final[str] = "gyt-local"
"""未设令牌(本机联调 / 真机验收)时的身份。与上面区分开,日志里一眼能认出来。"""


# ---------------------------------------------------------------------------
# 「有效令牌」判定已整段搬到 src/gyt/core/access.py(2026-08-15,W7 打卡):
# _access_token / _token_problem / _PLACEHOLDER_PREFIXES / _MIN_TOKEN_LEN 全在那边,
# 语义一字没动 —— 空 / 占位符开头 / 过短 = 未配置 = 鉴权关闭。
# checkin_api.py 的令牌自查必须与这里完全同源,靠的就是共用那一份。
# 顶部 import 按原名 re-export;is_enforcing 留在本文件(它是这道闸门自己的开关语义)。
# ---------------------------------------------------------------------------


def is_enforcing() -> bool:
    """当前是否处于「强制」状态(令牌已配置**且看着像真的**)。鉴权与限流同开同关。

    ⚠️ 占位符和过短的令牌一律**当成没配**处理 —— 于是启动时会打那块感叹号横幅,
    部署的人一眼就能看见「没锁」。这比「假装锁了」安全得多:
    前者会被发现,后者会一直以为自己是安全的。

    为什么同开同关:限流唯一的目的是挡**外部**刷 run 烧账单,而本机没有外部。
    本机若也限流,``live_acceptance.py``(``ask()`` 有 17 个调用点,失败还会各重发
    一次)就可能被自己人拦下,而那个 429 会被当成系统 bug 查半天。
    """
    raw = _access_token()
    return bool(raw) and _token_problem(raw) is None


# ---------------------------------------------------------------------------
# 请求头取值:_api_key_from_headers(连同它依赖的 _decode_header_part)已搬到
# src/gyt/core/access.py,理由同上(checkin_api 的自查要用同一条取值逻辑)。
# 顶部 import 按原名 re-export,本文件的调用点原样没动。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 令牌桶:TokenBucket 类本体已搬到 src/gyt/core/access.py(checkin_api 的两只
# 打卡限流桶复用同一实现)。顶部 import 按原名 re-export —— test_auth.py 与
# 本文件照旧写 auth.TokenBucket。
# ⚠️ **桶的实例与状态没搬**:_limiter / get_limiter / reset_limiter 必须留在
# 这里 —— test_auth.py 靠 monkeypatch.setattr(auth, "_limiter", …) 注入假时钟
# 的桶,get_limiter 读的必须是同一个模块变量,搬走它们这条注入路径就断了;
# 而且这只桶护的是「创建 run」,与打卡的两只桶各管各的账,状态不该合并。
# ---------------------------------------------------------------------------

_limiter: TokenBucket | None = None
"""进程内唯一的限流器。局限(多进程 / 重启失效)已在模块文档字符串里写明。"""


def get_limiter() -> TokenBucket:
    """返回限流器;配置里的容量/速率变了就重建一个。

    「参数变了就重建」是给测试用的(每个用例改完环境变量直接拿到新桶),
    生产里 ``get_settings()`` 被 lru_cache 焐住,这个分支一次都不会走。
    另外还有 ``reset_limiter()`` 供需要显式清空桶内**状态**的用例调用 ——
    参数没变但想从满桶重来时,只能靠它。
    """
    global _limiter
    settings = get_settings()
    if (
        _limiter is None
        or _limiter.capacity != float(settings.rate_limit_burst)
        or _limiter.refill_per_minute != float(settings.rate_limit_per_minute)
    ):
        _limiter = TokenBucket(
            capacity=settings.rate_limit_burst,
            refill_per_minute=settings.rate_limit_per_minute,
        )
    return _limiter


def reset_limiter() -> None:
    """丢掉当前限流器(下次取用时重建成满桶)。给测试隔离用。"""
    global _limiter
    _limiter = None


# ---------------------------------------------------------------------------
# Auth 实例。langgraph.json 里 ``"path": "./auth.py:auth"`` 指的就是这个名字,
# 改名等于改 langgraph.json,而且**改完必须重建镜像**(见文件末尾的说明)。
# ---------------------------------------------------------------------------

auth = Auth()


@auth.authenticate
async def authenticate(
    headers: dict[bytes, bytes] | None,
    method: str,
    path: str,
) -> Auth.types.MinimalUserDict:
    """校验 ``X-Api-Key``。未配置令牌时直接放行。

    参数是**按名字**被上游注入的(``langgraph_api/auth/custom.py`` 的
    ``SUPPORTED_PARAMETERS``),名字写错会在启动时报「unsupported required
    parameters」。``method`` / ``path`` 只用来写日志 —— 被拒的时候得知道
    是谁在打哪条路径,否则线上排查只剩一句「有人 401 了」。
    """
    expected = _access_token()
    if not expected:
        # 鉴权关闭。仍然返回一个正常身份,让下游(授权处理器、日志)有东西可用。
        return {
            "identity": IDENTITY_LOCAL,
            "display_name": "本机联调",
            "is_authenticated": True,
            "permissions": [],
        }

    presented = _api_key_from_headers(headers)

    # 为什么必须是 hmac.compare_digest 而不是 ``==``:
    #   Python 的字符串 ``==`` 逐字符比,**第一个不同的字符就返回** ——
    #   于是「猜对前 3 个字符」比「第 1 个就错」慢那么一点点。单次差异淹没在网络
    #   抖动里,但攻击者可以对同一个前缀打上万次取统计量,把这点差异捞出来,
    #   于是口令就从「整体猜中」退化成「逐位猜中」,难度从指数级掉到线性级。
    #   compare_digest 恒定时间比完全长,不给这个统计量。
    #
    # 为什么要 .encode() 成 bytes:compare_digest 收两个 str 时要求**都是 ASCII**,
    #   否则抛 TypeError。口令是外部输入,谁都可能粘进一个中文全角字符 ——
    #   那会变成 500 而不是 401,既难查又给了攻击者一个可区分的信号。
    #   转 bytes 这条路对任意字节都成立。
    #
    # 已知且可接受的残留:compare_digest 不隐藏**长度**差异。对一串足够长的随机
    #   口令来说,知道长度没有帮助(它不缩小搜索空间的量级)。
    matched = bool(presented) and hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    )
    if not matched:
        # 真实原因只进日志,绝不进响应体。日志里也**不打令牌本身**(哪怕是错的那个)——
        # 错误令牌常常是正确令牌打错一个字,原样落进日志等于把口令写进了 docker logs。
        logger.warning(
            "鉴权失败:method=%s path=%s 原因=%s",
            method,
            path,
            "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
        )
        raise Auth.exceptions.HTTPException(status_code=401, detail=DENY_MESSAGE)

    return {
        "identity": IDENTITY_TESTER,
        "display_name": "测试用户",
        "is_authenticated": True,
        "permissions": [],
    }


def _reject_foreign_identity(ctx: Auth.types.AuthContext) -> None:
    """强制模式下,身份不是本文件签发的就拒。**这是一道具体的后门补丁,不是洁癖。**

    langgraph-api 里有一条绕过 ``@auth.authenticate`` 的旁路
    (``langgraph_api/auth/custom.py`` 的 ``CustomAuthBackend.authenticate`` 开头):
    只要同时满足

        ① ``disable_studio_auth`` 没开(默认就是没开);
        ② ``LANGGRAPH_AUTH_TYPE`` 是 ``noop``(默认值)且环境变量
           ``LANGSMITH_LANGGRAPH_API_VARIANT == "local_dev"``;
        ③ 请求带 ``x-auth-scheme: langsmith`` 头,

    自定义 authenticate 就**整个被跳过**,直接换成
    ``StudioNoopAuthBackend`` 返回的 ``StudioUser("langgraph-studio-user")``。

    而条件 ② 在本项目里是**恒成立**的:``langgraph_api/cli.py`` 启动 ``langgraph dev``
    时会把 ``LANGSMITH_LANGGRAPH_API_VARIANT="local_dev"`` 打进环境,
    而 ``backend/Dockerfile`` 的 CMD 就是 ``langgraph dev``。
    也就是说:不处理的话,一个 curl 加一个头就能白嫖整个 Agent。

    正面堵法是 langgraph.json 里的 ``"disable_studio_auth": true``(已加,那是源头)。
    这里是第二道:即便哪天有人把那个键删了、或上游换了默认值,身份对不上照样拒。
    判据故意写成**白名单**(只认本文件签发的那个 identity),而不是
    「黑名单掉 StudioUser」—— 黑名单要求我们预知所有旁路的长相,白名单不用。
    """
    if not is_enforcing():
        return
    identity = getattr(getattr(ctx, "user", None), "identity", None)
    if identity == IDENTITY_TESTER:
        return
    logger.warning(
        "拒绝来路不明的身份:identity=%r resource=%s action=%s(通常意味着有人绕过了 X-Api-Key 校验)",
        identity,
        getattr(ctx, "resource", None),
        getattr(ctx, "action", None),
    )
    raise Auth.exceptions.HTTPException(status_code=401, detail=DENY_MESSAGE)


@auth.on
async def allow_authenticated(ctx: Auth.types.AuthContext, value: Any) -> None:
    """兜底处理器:身份对得上就放行,**不做任何按人过滤**。

    两点要说清楚:

    1. 为什么不做按人隔离(不给线程打 owner、不按 owner 过滤):全体测试的人共用
       同一把令牌 ⇒ 后端看到的是同一个 identity ⇒ 隔离在这里根本无从谈起。
       真要多租户,前提是先发多把令牌,那是另一件事。
       **后果得让下游知道:测试的人能互相看见对方的会话历史。**
       这和上线前的现状一致(现在是零鉴权,谁都看得见),不是新增的退化。

    2. 为什么非要注册这个「什么都不做」的处理器:langgraph-api 的授权是
       **fail-open** 的 —— 没有匹配处理器的 (resource, action) 直接放行,
       并且它会在启动时对每一条未覆盖路径打一串 warning
       (``_warn_on_missing_handlers``)。注册一个显式的全局处理器,
       既让「放行是想清楚的决定」写在代码里,也把那串噪音关掉;
       更具体的 ``@auth.on.threads.create_run`` 优先级更高,会盖住它
       (匹配顺序:精确 → 资源通配 → 动作通配 → 全局)。
    """
    _reject_foreign_identity(ctx)
    return None


def _reject_oversized_recursion(value: Any) -> None:
    """客户端自带的 ``config.recursion_limit`` 超过上限就**拒绝**这次创建。

    ===========================================================================
    为什么必须有这一道 —— 只限「次数」拦不住烧钱
    ---------------------------------------------------------------------------
    2026-08-11 安全复核实测(三段链路逐段跑通,不是推演):

      ① 客户端在请求体里传的 ``config.recursion_limit`` 会**原样落进 run**:
         POST /runs 带 {"config":{"recursion_limit":999}} → 落库的 config 就是 999;
      ② ``graph.py`` 编译时 ``.with_config(recursion_limit=8)`` 钉的那个 8,
         **会被调用时的 config 盖掉**(容器里同版本 langgraph 1.2.10 最小复现:
         不传 → "Recursion limit of 8 reached";传 60 → "of 60 reached");
      ③ 落库的 config 确实会交给图(``langgraph_api/stream.py:177`` → ``:462``)。

    于是令牌桶那句「狂刷最多 1200 次/小时,是个**有上限**的量级」不成立 ——
    上限被乘上了一个**客户端可控**的系数。而令牌就烘在测试者打开的那个页面里,
    不需要恶意,一个「我试试上限」的好奇测试者就够。

    ===========================================================================
    为什么是**拒绝**而不是就地改写
    ---------------------------------------------------------------------------
    改写 ``value["kwargs"]["config"]`` 能不能生效**没有被验证过** ——
    ``models/run.py`` 在加密环节可能已经换过对象引用,改了也可能白改,
    而白改的表现是「看起来限住了、其实没有」。这种赌不划算。
    拒绝是确定的:请求根本进不到落库那一步。
    """
    limit = get_settings().max_client_recursion_limit
    kwargs = value.get("kwargs") if isinstance(value, Mapping) else None
    config = kwargs.get("config") if isinstance(kwargs, Mapping) else None
    raw = config.get("recursion_limit") if isinstance(config, Mapping) else None
    if raw is None:
        return
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        # 传了个非整数(字符串/None/对象)。不在这儿替它兜底解释,直接拒 ——
        # 正常客户端不会这么发,而「猜它想干什么」正是安全代码最不该做的事。
        requested = limit + 1
    if requested > limit:
        logger.warning("拒绝超限 run:客户端要 recursion_limit=%r,上限 %d", raw, limit)
        raise Auth.exceptions.HTTPException(
            status_code=400,
            detail=TOO_MANY_STEPS_MESSAGE,
        )


@auth.on.threads.create_run
async def limit_run_creation(ctx: Auth.types.AuthContext, value: Any) -> None:
    """创建 run 时过一次令牌桶。这是**唯一**被限流的动作。

    读线程 / 列历史 / 翻记录走上面的 ``allow_authenticated``,压根不碰桶 ——
    测试的人回头翻聊天记录不该被限流打断,而且那些操作不花钱。
    """
    _reject_foreign_identity(ctx)
    if not is_enforcing():
        # 本机联调 / 真机验收:不限流。理由见 is_enforcing 的文档字符串。
        return None

    _reject_oversized_recursion(value)

    identity = getattr(getattr(ctx, "user", None), "identity", None) or IDENTITY_TESTER
    wait_s = get_limiter().take(identity)
    if wait_s is None:
        return None

    retry_after = max(1, math.ceil(wait_s))
    logger.warning(
        "创建 run 被限流:identity=%s 建议等待=%.1f 秒(桶容量=%s,每分钟补=%s)",
        identity,
        wait_s,
        get_limiter().capacity,
        get_limiter().refill_per_minute,
    )
    raise Auth.exceptions.HTTPException(
        status_code=429,
        detail=RATE_LIMITED_MESSAGE,
        # Retry-After 是 HTTP 标准头(RFC 9110 §10.2.3)。前端/脚本可以照它退避,
        # 不用去猜。给的是**秒数**形式,向上取整且至少 1 —— 回 0 等于叫人立刻重试。
        headers={"Retry-After": str(retry_after)},
    )


@auth.on.crons.create
async def deny_cron_create(ctx: Auth.types.AuthContext, value: Any) -> bool | None:
    """强制模式下**禁止建定时任务**。

    为什么单独拦它:一个 cron 是「一次创建、以后自己反复跑 run」。而 cron 触发的 run
    未必带得上发起人的身份上下文(没有 ctx 时 ``handle_event`` 会直接返回、
    什么处理器都不跑),也就是说上面那只桶**不保证罩得住它们** ——
    等于绕过限流开了一条长期烧钱的管子。

    来做测试的人不需要定时任务,拦掉的代价是零。返回 ``False`` 会被上游转成 403。
    """
    _reject_foreign_identity(ctx)
    if not is_enforcing():
        return None
    logger.warning("拒绝创建定时任务(测试实例不开放 cron)")
    return False


def warn_if_open() -> None:
    """启动时把「门有没有锁」这件事打到日志上。

    在模块底部调用一次 —— 也就是 LangGraph 服务加载本文件的那一刻,每进程一次。
    刻意用多行 + 感叹号横幅:``langgraph dev`` 的启动日志很长,一行普通 WARNING
    会被冲走,而这条信息漏看的代价是「以为锁了其实没锁」。
    """
    if is_enforcing():
        settings = get_settings()
        logger.info(
            "访问校验已开启:请求必须带 %s 请求头;创建 run 限流 = 突发 %d 次、"
            "每分钟补 %.1f 次(按身份计,进程内内存实现)。",
            API_KEY_HEADER,
            settings.rate_limit_burst,
            settings.rate_limit_per_minute,
        )
        return

    bar = "!" * 68

    # 先分清是「压根没填」还是「填了个假的」—— 两者的下一步动作完全不同,
    # 而后者才是最危险的:部署的人以为自己锁了。
    problem = _token_problem(_access_token())
    if problem is not None:
        logger.warning(
            "\n%s\n"
            "!! GYT_ACCESS_TOKEN 填了,但【不作数】——%s。\n"
            "!! 现在等于**完全没有访问校验**,任何摸得到这个端口的人都能刷你的模型账单。\n"
            "!!\n"
            "!! compose 的 ${GYT_ACCESS_TOKEN:?…} 那道闸只认「空」,认不出「假」——\n"
            "!! 占位符是非空字符串,能顺利通过检查,所以这条得由后端自己挡。\n"
            "!!\n"
            "!! 生成一个真的:openssl rand -hex 32\n"
            "%s",
            bar,
            problem,
            bar,
        )
        return

    logger.warning(
        "\n%s\n"
        "!! 工友通后端当前【没有开启访问校验】。\n"
        "!! 任何能连到这个端口的人,都能直接用它跑 Agent —— 而两把模型 API Key\n"
        "!! 就在这个进程里,也就是账单敞开给人刷。\n"
        "!!\n"
        "!! 本机联调 / 真机验收这样是对的(验收脚本不发令牌头,开了就跑不通)。\n"
        "!! 但只要这个端口能被外面摸到,就必须先设 GYT_ACCESS_TOKEN\n"
        "!! (一串足够长的随机口令),否则不要上公网。\n"
        "%s",
        bar,
        bar,
    )


warn_if_open()
