"""``backend/auth.py`` 单元测试 —— 对外访问闸门(令牌鉴权 + 创建 run 限流)。

这个文件测的是**上公网前的最后一道软锁**,所以断言的重点不是「功能跑通」,
而是几件出事就很贵的事:

    1. 没配令牌时**必须放行** —— make dev / make dev-docker /
       scripts/live_acceptance.py(25 断言,请求头里只有 Content-Type)
       全都不发令牌头,这条一破就是把真机验收当场打死;
    2. 配了令牌时,错的 / 缺的 / 空的**都得拒**,而且**回同一句话** ——
       任何差异都是送给爆破脚本的信号;
    3. 限流只卡**创建 run**,读类操作一次都不能被卡(测试的人翻记录不该被打断);
    4. 令牌桶的时间行为用**注入的假时钟**测,不真 sleep —— 真 sleep 的限流测试
       又慢又飘,CI 上负载一高就红,而红了大家会以为是限流坏了。

``import auth`` 为什么成立:``backend/`` 下有 pyproject.toml,pytest 把 rootdir
插进了 sys.path(tests/ 与 tests/unit/ 都有 __init__.py,包的根就落在 backend/)。
线上则完全是另一条路 —— langgraph-api 按 langgraph.json 里的文件路径
``spec_from_file_location`` 加载它。两条路加载的是同一份源码,契约一致。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import auth as auth_module
import pytest
from langgraph_sdk import Auth

from gyt.config import get_settings

# ---------------------------------------------------------------------------
# 夹具与小工具
# ---------------------------------------------------------------------------

REAL_TOKEN = "gyt-test-token-0123456789abcdef"
"""测试用的"正确"令牌。写得明显够长,是为了顺带示范线上该配个什么量级的口令。"""

# backend/langgraph.json —— 本文件是 backend/tests/unit/test_auth.py:
#   parents[0]=unit  parents[1]=tests  parents[2]=backend
LANGGRAPH_JSON = Path(__file__).resolve().parents[2] / "langgraph.json"


@pytest.fixture(autouse=True)
def _干净的限流器() -> Iterator[None]:
    """每个用例前后都把进程内那只桶丢掉,免得上一条用例的剩余令牌漏给下一条。

    ``auth._limiter`` 是**模块级全局**(限流器天然有状态),conftest 里那套
    Settings 隔离管不到它 —— 那条只隔离配置,不隔离别人家的模块变量。
    """
    auth_module.reset_limiter()
    yield
    auth_module.reset_limiter()


@dataclass(frozen=True)
class _假用户:
    """只需要 identity 的最小用户对象。

    线上真正塞进 ctx.user 的是 langgraph-api 的 ProxyUser,但授权处理器只读
    ``.identity`` 一个属性 —— 为了这一个属性去构造真 ProxyUser 会把
    langgraph_api.config 整条链拖进单测,得不偿失。
    """

    identity: str


class _假时钟:
    """可手动推进的单调时钟。``take()`` 的补令牌逻辑全靠它,于是时间成了普通输入。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def 推进(self, 秒: float) -> None:
        self.now += 秒


def _上下文(resource: str, action: str, identity: str) -> Auth.types.AuthContext:
    """造一个授权上下文。字段顺序与 Auth.types.AuthContext 的定义一致。"""
    return Auth.types.AuthContext(
        permissions=[],
        user=_假用户(identity),
        resource=resource,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
    )


def _开启鉴权(monkeypatch: pytest.MonkeyPatch, token: str = REAL_TOKEN) -> None:
    monkeypatch.setenv("GYT_ACCESS_TOKEN", token)
    get_settings.cache_clear()


def _装一只假时钟的桶(
    monkeypatch: pytest.MonkeyPatch,
    *,
    突发: int,
    每分钟: float,
    时钟: _假时钟,
) -> auth_module.TokenBucket:
    """按给定参数装一只用假时钟的桶,并让 ``get_limiter()`` 原样认领它。

    刻意把环境变量也设成同样的数 —— ``get_limiter()`` 发现参数与配置不符会重建,
    重建出来的是真时钟的桶,测试就白做了。这样写等于连 get_limiter 的
    「参数变了才重建」那条分支一起测到。
    """
    monkeypatch.setenv("GYT_RATE_LIMIT_BURST", str(突发))
    monkeypatch.setenv("GYT_RATE_LIMIT_PER_MINUTE", str(每分钟))
    get_settings.cache_clear()
    bucket = auth_module.TokenBucket(capacity=突发, refill_per_minute=每分钟, clock=时钟)
    monkeypatch.setattr(auth_module, "_limiter", bucket)
    assert auth_module.get_limiter() is bucket, "get_limiter 不该重建这只桶"
    return bucket


async def _发起鉴权(headers: Any) -> dict:
    return await auth_module.authenticate(headers=headers, method="POST", path="/threads")


# ---------------------------------------------------------------------------
# 一、令牌未配置 = 门开着(本机联调 / 真机验收的生命线)
# ---------------------------------------------------------------------------


async def test_未设令牌时放行且不炸() -> None:
    """conftest 已经把所有 GYT_* 清干净,所以这就是"没配令牌"的真实状态。

    这条一红,``make dev`` 与 ``scripts/live_acceptance.py`` 同时死。
    """
    assert auth_module.is_enforcing() is False
    user = await _发起鉴权({})
    assert user["identity"] == auth_module.IDENTITY_LOCAL
    assert user["is_authenticated"] is True


async def test_未设令牌时带不带令牌头都放行() -> None:
    """门关着的时候,乱发一个头也不该被拒 —— 否则等于半开着,最难查。"""
    user = await _发起鉴权({b"x-api-key": "随便写的东西".encode()})
    assert user["identity"] == auth_module.IDENTITY_LOCAL


async def test_未设令牌时创建run不限流(monkeypatch: pytest.MonkeyPatch) -> None:
    """真机验收一轮要连打十几个 run(``ask()`` 17 个调用点),本机不许有任何配额概念。

    桶容量故意设成 1:如果限流在"门开着"时也生效,第二次就会 429。
    """
    时钟 = _假时钟()
    _装一只假时钟的桶(monkeypatch, 突发=1, 每分钟=1, 时钟=时钟)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_LOCAL)
    for _ in range(30):
        assert await auth_module.limit_run_creation(ctx=ctx, value={}) is None


# ---------------------------------------------------------------------------
# 二、令牌已配置 = 门锁着
# ---------------------------------------------------------------------------


async def test_令牌正确时放行(monkeypatch: pytest.MonkeyPatch) -> None:
    _开启鉴权(monkeypatch)
    user = await _发起鉴权({b"x-api-key": REAL_TOKEN.encode()})
    assert user["identity"] == auth_module.IDENTITY_TESTER


@pytest.mark.parametrize(
    ("说明", "headers"),
    [
        ("令牌不对", {b"x-api-key": b"wrong-token"}),
        ("令牌只差一个字符", {b"x-api-key": (REAL_TOKEN[:-1] + "0").encode()}),
        ("压根没带这个头", {b"content-type": b"application/json"}),
        ("一个头都没有", {}),
        ("headers 是 None", None),
        ("带了但值是空串", {b"x-api-key": b""}),
        ("带了但值全是空白", {b"x-api-key": b"   "}),
        ("正确令牌但多带了前缀", {b"x-api-key": f"Bearer {REAL_TOKEN}".encode()}),
    ],
)
async def test_令牌不对或缺失一律被拒(
    monkeypatch: pytest.MonkeyPatch, 说明: str, headers: Any
) -> None:
    _开启鉴权(monkeypatch)
    with pytest.raises(Auth.exceptions.HTTPException) as 现场:
        await _发起鉴权(headers)
    assert 现场.value.status_code == 401, 说明


async def test_所有失败情形返回的文案完全一样(monkeypatch: pytest.MonkeyPatch) -> None:
    """「缺头」「空串」「令牌错」必须一个字都不差。

    差一个字,爆破脚本就能拿它当"我这一步猜对了"的信号 —— 这正是 401 文案
    唯一要防的事。判据写成"两两相等"而不是"都等于某个常量",是为了在有人
    将来给某一路加了"更友好的提示"时也能立刻红。
    """
    _开启鉴权(monkeypatch)
    文案 = []
    for headers in ({}, {b"x-api-key": b""}, {b"x-api-key": b"wrong"}):
        with pytest.raises(Auth.exceptions.HTTPException) as 现场:
            await _发起鉴权(headers)
        文案.append(现场.value.detail)
    assert len(set(文案)) == 1
    assert 文案[0] == auth_module.DENY_MESSAGE


async def test_拒绝文案不含任何可用于试探的细节(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回给客户端的那句话里,不许出现令牌、请求头名、长度、状态码、内部术语。

    这条与 ``gyt/core/errors.py`` 的 ``detail=`` 哲学同源:内部细节走日志,
    用户只拿人话。
    """
    _开启鉴权(monkeypatch)
    with pytest.raises(Auth.exceptions.HTTPException) as 现场:
        await _发起鉴权({b"x-api-key": b"wrong-token"})
    文案 = 现场.value.detail
    assert REAL_TOKEN not in 文案
    assert "wrong-token" not in 文案
    for 禁词 in (
        "token",
        "Token",
        "key",
        "Key",
        "api",
        "API",
        "header",
        "长度",
        "不匹配",
        "缺少",
        "401",
        "auth",
    ):
        assert 禁词 not in 文案, f"拒绝文案里不该出现「{禁词}」,那是在教人怎么试"


@pytest.mark.parametrize(
    "头名",
    [b"x-api-key", b"X-Api-Key", b"X-API-KEY", b"x-Api-Key", "X-Api-Key", "x-api-key"],
)
async def test_请求头名大小写不敏感(monkeypatch: pytest.MonkeyPatch, 头名: Any) -> None:
    """HTTP 头名本来就大小写不敏感(RFC 9110)。

    ASGI 规范要求传到应用层时已经小写,但取值逻辑刻意不依赖这一条 ——
    上游两处文档对 headers 的键类型说法都不一致(str 还是 bytes),
    这种地方不值得赌。顺带把 str 键也测了。
    """
    _开启鉴权(monkeypatch)
    user = await _发起鉴权({头名: REAL_TOKEN})
    assert user["identity"] == auth_module.IDENTITY_TESTER


async def test_令牌值前后有空白也认(monkeypatch: pytest.MonkeyPatch) -> None:
    """复制粘贴口令时很容易带上换行/空格。这属于"人之常情",不该判成入侵。"""
    _开启鉴权(monkeypatch)
    user = await _发起鉴权({b"x-api-key": f"  {REAL_TOKEN}\n".encode()})
    assert user["identity"] == auth_module.IDENTITY_TESTER


async def test_令牌含非ascii字符不会把服务打成500(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hmac.compare_digest`` 收两个 str 时要求都是 ASCII,否则抛 TypeError。

    口令是外部输入,谁都可能粘进一个中文全角字符。实现里两边都 ``.encode()``
    成 bytes 就没这个问题 —— 这条测的就是那次 encode 不许被"简化"掉。
    抛 TypeError 会变成 500,既难查又给了攻击者一个可区分的信号。
    """
    _开启鉴权(monkeypatch, token="口令带中文-abc")
    user = await _发起鉴权({b"x-api-key": "口令带中文-abc"})
    assert user["identity"] == auth_module.IDENTITY_TESTER

    with pytest.raises(Auth.exceptions.HTTPException) as 现场:
        await _发起鉴权({b"x-api-key": "口令带中文-xyz"})
    assert 现场.value.status_code == 401


async def test_畸形utf8请求头不会把服务打成500(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法字节走 errors="replace",照常落到 401,而不是崩成 500。"""
    _开启鉴权(monkeypatch)
    with pytest.raises(Auth.exceptions.HTTPException) as 现场:
        await _发起鉴权({b"x-api-key": b"\xff\xfe\xfd"})
    assert 现场.value.status_code == 401


# ---------------------------------------------------------------------------
# 三、令牌桶本身
# ---------------------------------------------------------------------------


def test_令牌桶用完就拒并给出等待秒数() -> None:
    时钟 = _假时钟()
    桶 = auth_module.TokenBucket(capacity=3, refill_per_minute=60, clock=时钟)
    assert [桶.take("甲") for _ in range(3)] == [None, None, None]
    等待 = 桶.take("甲")
    assert 等待 is not None
    # 每分钟补 60 枚 = 每秒 1 枚,所以攒回 1 枚正好要 1 秒。
    assert 等待 == pytest.approx(1.0)


def test_时间推进后令牌桶恢复() -> None:
    时钟 = _假时钟()
    桶 = auth_module.TokenBucket(capacity=2, refill_per_minute=60, clock=时钟)
    assert 桶.take("甲") is None
    assert 桶.take("甲") is None
    assert 桶.take("甲") is not None

    时钟.推进(2.0)  # 每秒补 1 枚 → 攒回 2 枚
    assert 桶.take("甲") is None
    assert 桶.take("甲") is None
    assert 桶.take("甲") is not None


def test_令牌不会补过容量上限() -> None:
    """桶闲置一整天也只能攒到 capacity。没有这条,限流就成了"攒够就能一次性爆发"。"""
    时钟 = _假时钟()
    桶 = auth_module.TokenBucket(capacity=2, refill_per_minute=60, clock=时钟)
    assert 桶.take("甲") is None
    时钟.推进(86_400.0)
    assert 桶.take("甲") is None
    assert 桶.take("甲") is None
    assert 桶.take("甲") is not None


def test_不同身份各用各的桶() -> None:
    时钟 = _假时钟()
    桶 = auth_module.TokenBucket(capacity=1, refill_per_minute=60, clock=时钟)
    assert 桶.take("甲") is None
    assert 桶.take("甲") is not None
    assert 桶.take("乙") is None, "乙不该受甲的影响"


def test_时钟倒流不会凭空补令牌() -> None:
    """``time.monotonic`` 不会倒流,但注入的时钟可能被写错。

    宁可"少补"也不能"多补" —— 多补一次就是限流被绕过一次。
    """
    时钟 = _假时钟()
    桶 = auth_module.TokenBucket(capacity=1, refill_per_minute=60, clock=时钟)
    assert 桶.take("甲") is None
    时钟.推进(-3_600.0)
    assert 桶.take("甲") is not None


def test_限流器参数跟着配置走(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止在业务代码里硬编码 —— 桶的两个参数必须来自 get_settings()。"""
    monkeypatch.setenv("GYT_RATE_LIMIT_BURST", "7")
    monkeypatch.setenv("GYT_RATE_LIMIT_PER_MINUTE", "3.5")
    get_settings.cache_clear()
    桶 = auth_module.get_limiter()
    assert 桶.capacity == 7.0
    assert 桶.refill_per_minute == 3.5


# ---------------------------------------------------------------------------
# 四、限流挂在哪个动作上(卡创建 run,不卡读)
# ---------------------------------------------------------------------------


async def test_创建run超额时抛429并带RetryAfter头(monkeypatch: pytest.MonkeyPatch) -> None:
    _开启鉴权(monkeypatch)
    时钟 = _假时钟()
    _装一只假时钟的桶(monkeypatch, 突发=2, 每分钟=60, 时钟=时钟)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)

    assert await auth_module.limit_run_creation(ctx=ctx, value={}) is None
    assert await auth_module.limit_run_creation(ctx=ctx, value={}) is None

    with pytest.raises(Auth.exceptions.HTTPException) as 现场:
        await auth_module.limit_run_creation(ctx=ctx, value={})
    assert 现场.value.status_code == 429
    assert 现场.value.detail == auth_module.RATE_LIMITED_MESSAGE
    # Retry-After 必须是个 >= 1 的整数秒。回 0 等于叫人立刻重试,那是自找雪崩。
    assert int(现场.value.headers["Retry-After"]) >= 1


async def test_限流恢复后能继续创建run(monkeypatch: pytest.MonkeyPatch) -> None:
    _开启鉴权(monkeypatch)
    时钟 = _假时钟()
    _装一只假时钟的桶(monkeypatch, 突发=1, 每分钟=60, 时钟=时钟)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)

    assert await auth_module.limit_run_creation(ctx=ctx, value={}) is None
    with pytest.raises(Auth.exceptions.HTTPException):
        await auth_module.limit_run_creation(ctx=ctx, value={})

    时钟.推进(1.0)
    assert await auth_module.limit_run_creation(ctx=ctx, value={}) is None


async def test_读类操作不被限流(monkeypatch: pytest.MonkeyPatch) -> None:
    """桶已经见底,翻记录照样畅通 —— 这是"限流只卡花钱动作"的行为证据。"""
    _开启鉴权(monkeypatch)
    时钟 = _假时钟()
    _装一只假时钟的桶(monkeypatch, 突发=1, 每分钟=1, 时钟=时钟)

    创建 = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)
    assert await auth_module.limit_run_creation(ctx=创建, value={}) is None
    with pytest.raises(Auth.exceptions.HTTPException):
        await auth_module.limit_run_creation(ctx=创建, value={})

    for resource, action in (
        ("threads", "read"),
        ("threads", "search"),
        ("threads", "update"),
        ("assistants", "read"),
        ("assistants", "search"),
        ("store", "get"),
    ):
        ctx = _上下文(resource, action, auth_module.IDENTITY_TESTER)
        for _ in range(5):
            assert await auth_module.allow_authenticated(ctx=ctx, value={}) is None


def test_只有花钱的动作注册了专用处理器() -> None:
    """结构证据:除了 create_run 与 crons.create,其余全部落到兜底放行的处理器。

    行为测试(上一条)证明"现在不卡";这一条证明"将来也别不小心卡上" ——
    有人给 ``@auth.on.threads`` 挂个资源级处理器,读类就会改走那条路,
    而行为测试未必覆盖得到新加的分支。这里直接盯注册表。

    ``_handlers`` / ``_global_handlers`` 是 langgraph_sdk 的私有属性,但它们是
    langgraph-api 自己派发时读的同一份数据(``langgraph_api/auth/custom.py``
    的 ``_get_handler``),拿它当判据比自己造一套派发更贴近真实。
    """
    assert set(auth_module.auth._handlers) == {("threads", "create_run"), ("crons", "create")}
    assert len(auth_module.auth._global_handlers) == 1


# ---------------------------------------------------------------------------
# 五、绕过 authenticate 的旁路(langgraph studio 那条)
# ---------------------------------------------------------------------------


async def test_强制模式下来路不明的身份被拒(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 ``x-auth-scheme: langsmith`` 旁路塞进来的 StudioUser。

    正面堵法在 langgraph.json 的 ``disable_studio_auth``(下面单独有条测试盯着它),
    这里测的是第二道:身份不是本文件签发的,一律拒。
    """
    _开启鉴权(monkeypatch)
    ctx = _上下文("threads", "create_run", "langgraph-studio-user")
    with pytest.raises(Auth.exceptions.HTTPException) as 现场:
        await auth_module.limit_run_creation(ctx=ctx, value={})
    assert 现场.value.status_code == 401
    assert 现场.value.detail == auth_module.DENY_MESSAGE

    with pytest.raises(Auth.exceptions.HTTPException):
        await auth_module.allow_authenticated(ctx=ctx, value={})


async def test_门开着时不追究身份来路(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机联调时 langgraph studio 得照常能用,不许被这道补丁误伤。"""
    ctx = _上下文("threads", "read", "langgraph-studio-user")
    assert await auth_module.allow_authenticated(ctx=ctx, value={}) is None


# ---------------------------------------------------------------------------
# 六、定时任务
# ---------------------------------------------------------------------------


async def test_强制模式下禁止建定时任务(monkeypatch: pytest.MonkeyPatch) -> None:
    """cron 触发的 run 未必带得上身份上下文,令牌桶罩不住 —— 直接不给建。"""
    _开启鉴权(monkeypatch)
    ctx = _上下文("crons", "create", auth_module.IDENTITY_TESTER)
    assert await auth_module.deny_cron_create(ctx=ctx, value={}) is False


async def test_门开着时不拦定时任务() -> None:
    ctx = _上下文("crons", "create", auth_module.IDENTITY_LOCAL)
    assert await auth_module.deny_cron_create(ctx=ctx, value={}) is None


# ---------------------------------------------------------------------------
# 七、接线:langgraph.json 与启动警告
# ---------------------------------------------------------------------------


def test_langgraph_json_声明了auth并关掉了studio旁路() -> None:
    """auth.py 写得再对,langgraph.json 不声明它就一行都不会被执行。

    ``disable_studio_auth`` 也在这里盯着:它一旦掉了,带
    ``x-auth-scheme: langsmith`` 头的请求就能整个跳过 authenticate
    (条件在本项目里恒成立 —— CMD 是 ``langgraph dev``,
    它会设 ``LANGSMITH_LANGGRAPH_API_VARIANT=local_dev``)。
    ⚠️ 改这个文件**必须重建镜像**,它不在 dev 档挂载的 src/ 里。
    """
    配置 = json.loads(LANGGRAPH_JSON.read_text(encoding="utf-8"))
    assert 配置["auth"]["path"] == "./auth.py:auth"
    assert 配置["auth"]["disable_studio_auth"] is True
    # 顺带守住原有键没被这次改动碰掉。
    assert 配置["graphs"]["gyt"] == "./src/gyt/graph.py:graph"
    assert 配置["env"] == "../.env"


def test_启动警告在两种状态下都不炸(monkeypatch: pytest.MonkeyPatch) -> None:
    """门开着时打醒目警告、锁着时打一条 info。它在模块导入时就跑过一次了。"""
    auth_module.warn_if_open()
    _开启鉴权(monkeypatch)
    auth_module.warn_if_open()


def test_关门时的警告说清了后果(caplog: pytest.LogCaptureFixture) -> None:
    """警告不能只说"没开启",得说清"所以会怎样"——否则没人会去处理它。"""
    with caplog.at_level("WARNING", logger=auth_module.__name__):
        auth_module.warn_if_open()
    正文 = caplog.text
    assert "GYT_ACCESS_TOKEN" in 正文
    assert "API Key" in 正文


# ===========================================================================
# 2026-08-11 安全复核补的三条(H1 / H2 / M3)——都是「看着锁了其实没锁」那一类
# ===========================================================================


@pytest.mark.parametrize(
    "假令牌",
    [
        "替换成openssl_rand_hex_32的输出",  # .env.vps.example 的原文
        "CHANGE_ME",
        "your-token-here",
        "TODO",
        "xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "短的",
        "gyt123",
        "   ",  # 纯空白:strip 之后是空
    ],
)
def test_假令牌一律当成没设(monkeypatch: pytest.MonkeyPatch, 假令牌: str) -> None:
    """占位符 / 过短 / 纯空白,都必须走「没锁」分支,而不是「锁了」。

    为什么这条比看上去重要:compose 的 ``${GYT_ACCESS_TOKEN:?…}`` 只认**空**,
    认不出**假**。2026-08-11 实测,占位符能顺利通过那道闸,后端于是打出
    「访问校验已开启」——而令牌是一串公开写在 git 仓库里的字。
    判成「没设」之后启动会打感叹号横幅,部署的人一眼看得见。
    """
    _开启鉴权(monkeypatch, 假令牌)
    assert auth_module.is_enforcing() is False


def test_真随机令牌认得出来(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向护栏:别把上面那张表写得太宽,把真令牌也误杀了。"""
    _开启鉴权(monkeypatch, "a" * 64)  # openssl rand -hex 32 的长相
    assert auth_module.is_enforcing() is True


def test_假令牌的横幅要说清是哪一种(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """「压根没填」和「填了个假的」下一步动作不同,日志必须分得开。"""
    _开启鉴权(monkeypatch, "替换成openssl_rand_hex_32的输出")
    with caplog.at_level("WARNING"):
        auth_module.warn_if_open()
    文本 = caplog.text
    assert "不作数" in 文本
    assert "占位符" in 文本
    assert "openssl rand -hex 32" in 文本  # 给出可直接执行的修法


# 🔴 **上限 +1 是现算的,别再写死一个数**(2026-08-22)。
#    原来这里的第一档写的是字面量 **17** —— 它假定 max_client_recursion_limit 恒等于 16。
#    那天把 supervisor_recursion_limit 从 8 提到 12、上限跟着 16 → 24 之后,
#    17 变成了**合法值**,于是这条用例红了。红得对,但它红的方式误导人:
#    报的是「DID NOT RAISE」,看起来像鉴权失守,而真相只是那个数过期了。
#    现算之后,以后再动上限,这条自己就跟上了。
_超限一步 = get_settings().max_client_recursion_limit + 1


@pytest.mark.parametrize("要的步数", [_超限一步, 100, 999, 10**6])
async def test_客户端自带超限的_recursion_limit_要被拒(
    monkeypatch: pytest.MonkeyPatch, 要的步数: int
) -> None:
    """客户端可以在请求体里传 config.recursion_limit,**它会盖掉编译时钉的那个数**。

    2026-08-11 实测(容器内 langgraph 1.2.10):不传 → "Recursion limit of 8 reached";
    传 60 → "of 60 reached"。于是令牌桶那句「狂刷也有上限」不成立 ——
    次数有上限,单次成本却由客户端说了算,两者相乘就没边了。
    """
    _开启鉴权(monkeypatch)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)
    with pytest.raises(Auth.exceptions.HTTPException) as 抓到:
        await auth_module.limit_run_creation(
            ctx, {"kwargs": {"config": {"recursion_limit": 要的步数}}}
        )
    assert 抓到.value.status_code == 400
    assert 抓到.value.detail == auth_module.TOO_MANY_STEPS_MESSAGE


@pytest.mark.parametrize(
    "要的步数",
    [None, 1, get_settings().supervisor_recursion_limit, get_settings().max_client_recursion_limit],
)
async def test_不超限的_recursion_limit_正常放行(
    monkeypatch: pytest.MonkeyPatch, 要的步数: int | None
) -> None:
    """反向护栏:别把正常请求也拦了。None = 聊天界面的真实形态(压根不传)。

    ⚠️ 三个数同样**现算**(1 / 编译时钉的那个 / 客户端上限本身)——
    写死 8 和 16 的话,调了配置这条会静默变成「测了两个跟真值无关的数」。
    """
    _开启鉴权(monkeypatch)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)
    值: dict[str, Any] = (
        {} if 要的步数 is None else {"kwargs": {"config": {"recursion_limit": 要的步数}}}
    )
    assert await auth_module.limit_run_creation(ctx, 值) is None


@pytest.mark.parametrize("垃圾值", ["999", None, {"a": 1}, [1, 2]])
async def test_recursion_limit_是垃圾值时按超限拒(
    monkeypatch: pytest.MonkeyPatch, 垃圾值: Any
) -> None:
    """传了个非整数就拒,不替它猜想干什么 —— 猜是安全代码最不该做的事。

    注意 ``None`` 在这里是「显式传了 null」,与上一条的「压根没有 config」不同:
    前者是客户端主动发的怪东西,后者是正常形态。
    """
    _开启鉴权(monkeypatch)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)
    值 = {"kwargs": {"config": {"recursion_limit": 垃圾值}}}
    if 垃圾值 is None:
        # config 里显式写了 recursion_limit: null —— 取出来是 None,与「没传」同形,
        # 按放行处理是对的(拿不到就当没要求),这条只是把行为钉住。
        assert await auth_module.limit_run_creation(ctx, 值) is None
        return
    with pytest.raises(Auth.exceptions.HTTPException) as 抓到:
        await auth_module.limit_run_creation(ctx, 值)
    assert 抓到.value.status_code == 400


async def test_浮点_recursion_limit_按截断后的整数判(monkeypatch: pytest.MonkeyPatch) -> None:
    """``int(3.7) == 3``,截断后不超限所以放行 —— 这是真实行为,不是漏网。

    为什么可以放行:截断只会把值变**小**,不会变大,所以它绕不过上限。
    而 ``int(99.9) == 99`` 仍然超限,照样被拒。真正要防的是「变大」,截断不会。
    (下游 langgraph 自己会不会接受浮点是另一回事,不归这道闸管。)
    """
    _开启鉴权(monkeypatch)
    ctx = _上下文("threads", "create_run", auth_module.IDENTITY_TESTER)
    assert (
        await auth_module.limit_run_creation(ctx, {"kwargs": {"config": {"recursion_limit": 3.7}}})
        is None
    )
    with pytest.raises(Auth.exceptions.HTTPException):
        await auth_module.limit_run_creation(ctx, {"kwargs": {"config": {"recursion_limit": 99.9}}})
