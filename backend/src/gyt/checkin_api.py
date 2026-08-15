"""打卡直连接口。**本文件的模块头注是 ``POST /checkin`` 请求契约的唯一真相**(T1 冻结)。

表结构的唯一真相在 ``gyt/db/attendance.py``,不在这里 —— 那边是代码,这边是协议。

===========================================================================
为什么打卡不走对话链(D15)
===========================================================================
打卡的**写入**路径一个 LLM 都不经过。三个理由,任何一个单独都不够,合起来是决定性的:

1. **幂等在对话链里做不到。** Regenerate、流重连、run retry 都会让同一次「打卡」
   被重放,而 LangGraph 那一层没有给业务用的幂等键。工友点一次,库里两条。
2. **跨境。** 自拍和精确坐标进对话链 = 发给 DeepSeek。走直连接口它们一步都不出本机。
   ⚠️ 但**别把这条说过头** —— 查询那一半仍然把**姓名**喂给模型(W7 方案 §1.7)。
3. **确定性。** 「拍照 → 出凭证」不该有任何概率成分。

===========================================================================
请求契约 —— 改任何一行都要同步三处(见 CLAUDE.md 同源清单)
===========================================================================

    POST /checkin
      Content-Type: image/jpeg
      X-Api-Key:        <前端显式复用 getApiKey();见下面「鉴权」>
      X-GYT-Event-Id:   <客户端幂等键>
      X-GYT-Worker:     <Base64URL(UTF-8 姓名)>
      X-GYT-Site:       <Base64URL(UTF-8 地盤名)>   —— 可省略
      X-GYT-Geo:        <见下面「定位」>
      X-GYT-Source:     camera | fallback
      X-GYT-Digest:     <照片字节的 sha256,64 位小写十六进制>
      body:             JPEG 原始字节 —— **不是 multipart**

发(``checkin-lib.ts`` 的 ``buildCheckinHeaders``)、收(本文件)、
存(``db/attendance.py``)三处任一改名 = **静默少一个字段**,没有报错。

---------------------------------------------------------------------------
为什么是 raw body 而不是 multipart
---------------------------------------------------------------------------
- ``python-multipart`` 当时**不在依赖里**,``request.form()`` 直接抛 ImportError。
  ⚠️ **2026-08-15 合流后这条不再成立** —— 队友为 webapp.py 的图纸上传把它加成了
  正式依赖(pyproject.toml)。但下面三条不受影响,raw body 的选择不变;
  留着这条是因为它解释了当初为什么连试都没试 multipart。
- ``starlette/formparsers.py:146`` 的 ``spool_max_size = 1MB``:
  **超过 1MB 的 multipart 上传会自动滚到 /tmp**,「原图从不落盘」当场是假话。
- ``event_id`` 和文件在同一个 body 里,**必须先解析完整请求**才拿得到,幂等短路不了。
- 大小检查只能在解析完之后做。

换成 header + raw body,一次解掉这四条:``Request.stream()`` 实测是真流式
(逐 ASGI 事件 yield,不预缓冲),于是能在读第一个字节之前就短路,也能边读边掐大小。

代价:header 的值必须 ASCII 安全 → 姓名与地盤名走 **Base64URL**(见 ``encode_name``)。

---------------------------------------------------------------------------
🔴 鉴权:``langgraph.json`` 里少一个键就是裸奔
---------------------------------------------------------------------------
::

    # langgraph_api/server.py:169(实测)
    enable_auth_on_custom_routes = config.HTTP_CONFIG and config.HTTP_CONFIG.get(
        "enable_custom_route_auth")

**默认是假值。** 不写这个键,本文件的所有路由**完全不过 auth.py**,
而且**不会有任何报错** —— 站点照开、打卡照成,只是那 636 行鉴权一行都不执行。
``langgraph.json`` 必须同时有 ``enable_custom_route_auth: true`` 与
``middleware_order: "auth_first"``(后者见同文件 :167)。

⚠️ ``@auth.on.threads.create_run`` 上挂的限流**管不到这里** ——
那个钩子只处理 LangGraph 的 run 动作。普通 Starlette 路由要自己带限流。

---------------------------------------------------------------------------
🔴 ``X-GYT-Digest`` 是客户端报的,**不可信、不落库**
---------------------------------------------------------------------------
它只有一个用途:让幂等的第一层能在**读 body 之前**判断「这是同一次打卡的重发」。

客户端只算**照片字节的 sha256**,不算完整指纹 —— 完整指纹的其余输入
(姓名、地盤、坐标)本来就全在 header 里,服务端自己拼得出来。
这样前后端**不需要各写一份规范化逻辑**,也就不会漂。

    层① 比的是:  build_digest(header 里的元数据, 客户端报的照片 hash)
    层② 落库的是:build_digest(header 里的元数据, **服务端自己算的**照片 hash)

客户端谎报 ``X-GYT-Digest`` 的后果只是**它自己**拿回一条对不上的记录 ——
因为层①压根不读 body,伪造不出任何服务端状态。
**但如果有人把 header 里那个值直接落库,那就是一个伪造入口。** 别这么干。

---------------------------------------------------------------------------
幂等三层 —— 缺一层都不够
---------------------------------------------------------------------------
::

    ① 读 body 前:SELECT WHERE event_id = X
         ├─ 命中 且 digest 相同 → 200 + 原记录(不读 body、不缩图、不画水印、不写库)
         └─ 命中 但 digest 不同 → 409「这次打卡的信息和之前那次对不上」
    ② 未命中 → 流式收 body → 服务端算 digest → 缩图 → 画水印 → register
    ③ INSERT 撞 UNIQUE(并发同键)→ 回查那条返回它;本次的图成孤儿,交清理器

``event_id UNIQUE`` 只保证「最多一行」,**不保证整个操作幂等**:并发同键时
两张水印图都会被生成,输家只是 INSERT 失败 —— 副作用已经发生了。
①把绝大多数重发挡在读 body 之前,③兜住剩下的并发窗口。
**这是「够用」不是「完美」。**

⚠️ **客户端也要负责:** 只在同一个 React 生命周期里复用 ``event_id`` 不够 ——
刷新、崩溃、App 被杀之后会生成新 ID,用户再提交就是重复记账。
``event_id`` 必须落 ``sessionStorage``,提交成功后才清除。

⚠️ **共享口令下 ``event_id`` 不是用户作用域** —— 任何登录者拿到别人的 event_id
都能取回那条记录。根治要 TODO-3,已记在 TODO-36。

---------------------------------------------------------------------------
定位:一个 header,格式上就不允许「只有 lat 没有 lon」
---------------------------------------------------------------------------
::

    X-GYT-Geo: ok;22.302711;114.177216;12.5      ← 状态;纬度;经度;精度(米)
    X-GYT-Geo: denied                            ← 拿不到坐标时只有状态

**为什么塞进一个 header 而不是拆成四个:** 拆开的话「经纬度必须成对出现」
就成了一条要靠校验代码维护的规则;合成一个,格式本身就把它锁死了 ——
少一段就是格式错误,而不是「一个字段悄悄为 NULL」。

六种状态见 ``db/attendance.py`` 的 ``GEO_STATUSES``。**拿不到坐标照样能打卡** ——
工地上信号差是常态,为了定位挡住打卡是本末倒置。

⚠️ **``crypto.subtle`` 只在安全上下文里存在。** https 与 ``http://localhost`` /
``http://127.0.0.1`` 算安全上下文,而 **``http://<局域网 IP>:3000`` 不算** ——
那种情况下 ``crypto.subtle`` 是 undefined,前端算不出 ``X-GYT-Digest``。
局域网真机联调时要么走 https,要么前端明确报「请用 https 打开」,
**不许静默省掉这个 header** —— 省掉的表现是幂等第一层永远不命中,
每次重发都白烧一次水印,而功能看起来完全正常。

---------------------------------------------------------------------------
响应契约(前端 ``checkin-lib.ts`` 的归一化函数按这里实现)—— T1 一并冻结
---------------------------------------------------------------------------
一旦进了 handler,所有响应体都是 ``core/errors.py`` 的 Envelope
(``ok/data/user_msg/error_code`` 四键)。**前端不能假设「所有错误都是 Envelope」**
—— Caddy 的 302/401/413 长别的样子,归一化见 W7 方案 §3.11。

::

    POST /checkin
      200  ok=True,data = 凭证对象(见下)
           重发命中(同键同指纹)也是 200 + 原凭证 —— 对前端两者无区别,刻意的
      400  INVALID_INPUT   header 缺失/格式错/坐标越界/图不是 JPEG/缺字
      401  UNAUTHORIZED    handler 自查令牌不过(纵深防御,见下)
      409  CONFLICT        同 event_id 但指纹对不上
      413  FILE_TOO_LARGE  流式读到超限,当场断
      429  RATE_LIMITED    带 Retry-After 响应头(建议等待秒数,向上取整)
      500  INTERNAL        兜底;user_msg 是人话,细节只进日志

    凭证对象(POST 200 与 GET recent 共用同一形状):
      { receipt_no, worker_name, site_name, checked_at, work_date,
        geo_status, source, artifact_id, photo_purged_at }
      artifact_id 非空 → 前端 <img src={ARTIFACT_BASE}/by-id/{artifact_id}>
      artifact_id 为 null → 显示「凭证图已过期清理」,**不渲染 <img>**

    GET /checkin/recent?limit=N
      200  data = { records: [凭证对象 …] },按写入序倒排;
           N 缺省与上限都取 settings.attendance_recent_limit(超了就 clamp,不报错)

**纵深防御:handler 自己也查一遍令牌。** langgraph 的鉴权中间件
(``enable_custom_route_auth: true``)在前面拦是第一道(那层的 401 是
``{"detail": …}``);本层再查是第二道,防的是 ``langgraph.json`` 那个键被
漏配时整条裸奔(§「鉴权」)—— 那种失配**没有任何报错**,只有这道自查能兜住,
也只有这道自查能被单元测试钉死(上线闸①)。判定逻辑必须与 ``auth.py``
完全同源(同一个"有效令牌"函数:空 / 占位符 / 过短 = 未配置 = 放行)。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import math
from typing import Any, Final, NamedTuple

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.attendance import messages, receipt, watermark
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.access import TokenBucket, _api_key_from_headers, effective_access_token
from gyt.core.errors import Envelope, ErrorCode, fail, ok
from gyt.db import attendance as att_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# header 名 —— 与 checkin-lib.ts 的 buildCheckinHeaders 同源
# ---------------------------------------------------------------------------
# 全部小写:HTTP/2 强制小写头名,而 Starlette 的 Headers 是大小写不敏感的,
# 这里统一写小写是为了让「常量长什么样」和「网络上传的长什么样」一致 ——
# auth.py:105 的 API_KEY_HEADER 也是这么写的(它自己那段注释解释了为什么
# 不能依赖大小写规范)。

HEADER_EVENT_ID: Final[str] = "x-gyt-event-id"
HEADER_WORKER: Final[str] = "x-gyt-worker"
HEADER_SITE: Final[str] = "x-gyt-site"
HEADER_GEO: Final[str] = "x-gyt-geo"
HEADER_SOURCE: Final[str] = "x-gyt-source"
HEADER_DIGEST: Final[str] = "x-gyt-digest"

GEO_SEPARATOR: Final[str] = ";"
"""``X-GYT-Geo`` 的分隔符。

选 ``;`` 而不是 ``,``:逗号在 HTTP header 里有「同名 header 合并」的既有语义,
中间件或代理把两个同名 header 合成 ``a, b`` 之后,用逗号切就切错了。
分号没有这层歧义。
"""

DIGEST_HEX_LEN: Final[int] = 64
"""sha256 的十六进制长度。定长校验是最便宜的一道格式闸。"""

# 长度上限 —— 这些是**协议层**的闸(防止有人拿超长 header 撑爆内存),
# 不是业务规则。「姓名不能为空」之类归业务校验,说中文人话。
# 取值理由:header 单条通常被服务器限在 8KB 量级,Base64URL 会把体积撑到 4/3,
# 留足余量的同时也远大于任何真实姓名/地盤名。
MAX_NAME_BYTES: Final[int] = 256
MAX_EVENT_ID_LEN: Final[int] = 128


class HeaderValueError(ValueError):
    """header 值不符合契约。

    **故意不带中文人话** —— 这一层只判断「格式对不对」,
    面向工友的措辞归 ``attendance/messages.py``(D12 要求文案集中,
    最后要整体换简繁,散在各处的 f-string 漏一条就是混排)。
    """


def encode_name(name: str) -> str:
    """把 UTF-8 文本编成可放进 header 的 Base64URL(**无填充**)。

    前端 ``checkin-lib.ts`` 必须做同样的事,两边有一组共享的测试向量。

    去掉 ``=`` 填充的理由:``=`` 在 header 里合法,但它在 URL、日志检索、
    以及某些 WAF 规则里都是「可疑字符」,而去掉它零成本 —— 解码侧补回来就行。
    """
    raw = name.encode("utf-8")
    if len(raw) > MAX_NAME_BYTES:
        raise HeaderValueError(f"名称字节数 {len(raw)} 超过上限 {MAX_NAME_BYTES}")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_name(value: str) -> str:
    """``encode_name`` 的逆。任何异常都收敛成 ``HeaderValueError``。

    ⚠️ **先查长度再解码。** 反过来的话,一个 100MB 的 header 值会先被完整
    base64 解码一遍才被判超长 —— 那就是拿校验代码本身当放大器。
    """
    if len(value) > MAX_NAME_BYTES * 2:  # Base64 撑 4/3,×2 是宽松上界
        raise HeaderValueError("名称字段过长")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise HeaderValueError("名称字段不是合法的 Base64URL") from exc
    if len(raw) > MAX_NAME_BYTES:
        raise HeaderValueError("名称字段过长")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeaderValueError("名称字段不是合法的 UTF-8") from exc
    # 控制字符与换行必须挡在门外:它们会被画进水印(变成空白或方块),
    # 也会污染日志的行结构 —— 后者是日志注入的入口。
    if any(ch < " " or ch == "\x7f" for ch in text):
        raise HeaderValueError("名称字段含控制字符")
    return text


def photo_sha256(payload: bytes) -> str:
    """照片字节的 sha256,小写十六进制。**服务端算的这个才作数。**"""
    return hashlib.sha256(payload).hexdigest()


def build_digest(
    *,
    worker_name: str,
    site_name: str | None,
    geo_raw: str,
    photo_hash: str,
) -> str:
    """拼请求指纹。**层①与层②用的是同一个函数,只有 ``photo_hash`` 的来源不同。**

    - 层①(读 body 前):``photo_hash`` = 客户端在 ``X-GYT-Digest`` 里报的值
    - 层②(落库):      ``photo_hash`` = ``photo_sha256(真实字节)``

    ``geo_raw`` 传的是 header 的**原始字符串**而不是解析后的 float,
    理由是浮点数格式化会引入歧义(``22.3`` / ``22.30`` / ``2.23e1`` 是同一个数,
    但拼出来的指纹不同),而原始字符串是客户端与服务端都能看到的同一份字节。

    版本前缀 ``v1`` 是留给未来的:哪天指纹的构成要变,前缀一改,
    老记录的指纹自然对不上新记录 —— 而不是「悄悄地都对不上了,不知道从哪天开始」。
    """
    parts = ("v1", worker_name, site_name or "", geo_raw, photo_hash)
    # \x1f 是 ASCII 的单元分隔符,它已经被 decode_name 挡在名称之外,
    # 所以拼接不可能被字段内容伪造(否则「张\x1f三」+ 空地盤 会和
    # 「张」+「三」拼出同一个串)。
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def validate_digest_hex(value: str) -> str:
    """校验客户端报的 ``X-GYT-Digest`` 是不是一个像样的 sha256 十六进制串。"""
    lowered = value.strip().lower()
    if len(lowered) != DIGEST_HEX_LEN:
        raise HeaderValueError(f"指纹长度应为 {DIGEST_HEX_LEN},实际 {len(lowered)}")
    if not all(c in "0123456789abcdef" for c in lowered):
        raise HeaderValueError("指纹含非十六进制字符")
    return lowered


# ===========================================================================
# 以上是 T1 冻结的契约层;以下是 handler 与 app(T2)。
# 流程编号(⓪…⑦)与模块头注、W7 方案 §3.2 对齐,改动前先回读那两处。
# ===========================================================================

_BYTES_PER_MB: Final[int] = 1024 * 1024
"""MB → 字节。与 core/uploads.py、agents/safety/tools.py 同一换算,别另起一套。"""

JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"
"""JPEG 文件头(SOI + 下一个 marker 的 0xFF)。魔数才是判据 ——
Content-Type 是客户端自述,浏览器给的 MIME 不可靠这件事在 DXF 上传那条线
已经证过一次(前端只能按后缀认),这里同理不信它。"""

RECEIPT_RETRY_MAX: Final[int] = 3
"""``receipt_no`` 撞唯一约束后最多再试几次。

4 位十六进制随机尾,同秒单次碰撞概率 1/65536,连撞 4 次(首发 + 3 重试)
是天文数字 —— 真走到重试耗尽,多半是随机源或台账坏了,该 500 引人来看,
不该无限转圈。写死在这不进 config:它不是可调的业务参数,是对概率的一次性判断
(同型先例:core/access.py 的 _MIN_TOKEN_LEN)。"""

GLOBAL_BUCKET_IDENTITY: Final[str] = "gyt-checkin-global"
"""全局限流桶的桶键 —— 固定串,所有打卡请求共用一只桶。

与 auth.IDENTITY_TESTER 的道理相同:全体用的是同一把口令,后端分不开人;
这只桶护的是 1.9GB VPS 上的水印渲染内存与磁盘写入,不是人与人之间的公平。
"""

# ---------------------------------------------------------------------------
# 限流:两只桶,全部在读 body 之前结算(W7 §3.6)
#
# · 全局桶(attendance_rate_*):**唯一的真闸**,量级按「一个班组」定 ——
#   收工时全组排队打卡不该被拦,脚本长跑必须被压住。
# · worker 桶(attendance_worker_rate_*):**防手抖连点,不是安全控制。**
#   桶键是客户端报的 worker_name,改个名字就是一只新满桶,恶意方绕它零成本。
#   别看到「按人限流」就以为滥用问题解决了 —— 真正的天花板只有全局那只。
#
# 「参数变了就重建」照抄 auth.get_limiter 的写法:生产里 get_settings() 被
# lru_cache 焐住,这个分支一次都不会走;它是给测试换环境变量用的。
# 想清空桶内**状态**(参数没变但要从满桶重来)只能靠 reset_limiters()。
# ---------------------------------------------------------------------------

_global_limiter: TokenBucket | None = None
_worker_limiter: TokenBucket | None = None


def get_global_limiter() -> TokenBucket:
    """全局打卡限流桶;配置里的容量/速率变了就重建一个。"""
    global _global_limiter
    settings = get_settings()
    if (
        _global_limiter is None
        or _global_limiter.capacity != float(settings.attendance_rate_burst)
        or _global_limiter.refill_per_minute != float(settings.attendance_rate_per_minute)
    ):
        _global_limiter = TokenBucket(
            capacity=settings.attendance_rate_burst,
            refill_per_minute=settings.attendance_rate_per_minute,
        )
    return _global_limiter


def get_worker_limiter() -> TokenBucket:
    """按 worker_name 计的手抖桶;同样「参数变了就重建」。"""
    global _worker_limiter
    settings = get_settings()
    if (
        _worker_limiter is None
        or _worker_limiter.capacity != float(settings.attendance_worker_rate_burst)
        or _worker_limiter.refill_per_minute != float(settings.attendance_worker_rate_per_minute)
    ):
        _worker_limiter = TokenBucket(
            capacity=settings.attendance_worker_rate_burst,
            refill_per_minute=settings.attendance_worker_rate_per_minute,
        )
    return _worker_limiter


def reset_limiters() -> None:
    """丢掉两只桶(下次取用时重建成满桶)。给测试隔离用 ——
    桶是模块级全局,conftest 的 Settings 隔离管不到它,上一条用例烧掉的令牌
    会漏给下一条(test_auth.py 的同名坑)。"""
    global _global_limiter, _worker_limiter
    _global_limiter = None
    _worker_limiter = None


# ---------------------------------------------------------------------------
# 响应与鉴权小件
# ---------------------------------------------------------------------------


def _respond(env: Envelope, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    """Envelope → JSON 响应。进了 handler 之后所有出口都走这里(响应契约:
    四键信封,一个出口都不许裸拼 dict)。"""
    return JSONResponse(env, status_code=status, headers=headers)


def _rate_limited(wait_s: float) -> JSONResponse:
    """429 + ``Retry-After``(秒,向上取整、至少 1 —— 回 0 等于叫人立刻重试,
    那是自找雪崩;判据与 auth.limit_run_creation 一致)。"""
    return _respond(
        fail(ErrorCode.RATE_LIMITED, messages.RATE_LIMITED),
        429,
        headers={"Retry-After": str(max(1, math.ceil(wait_s)))},
    )


def _deny_if_token_bad(request: Request) -> JSONResponse | None:
    """⓪ 纵深防御的第二道:handler 自查令牌。放行返回 None,不过返回 401。

    第一道在 langgraph 的鉴权中间件(``enable_custom_route_auth: true``);
    这道防的是那个键被漏配 —— 漏配时第一道**整条消失且没有任何报错**
    (模块头注「鉴权」一节),只有这道自查兜得住,也只有它能被单测钉死(上线闸①)。

    判定与 auth.py 完全同源(core/access.py 的同一份代码):
    空 / 占位符 / 过短 = 未配置 = 放行 —— make dev 与真机验收不发令牌头,
    「未配置也拦」等于当场打死本机联调。

    ⚠️ 每次请求现走 get_settings()(effective_access_token 内部),**不许在
    import 时把令牌读成常量** —— 那样测试换环境变量测不到,生产换口令要重启
    才生效还没人知道。
    """
    expected = effective_access_token()
    if not expected:
        return None
    presented = _api_key_from_headers(request.headers)
    # compare_digest + encode 的理由原样见 auth.authenticate:恒定时间比对,
    # 不给逐位试探留统计量;encode 成 bytes 防非 ASCII 口令把 401 变成 500。
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return None
    # 真实原因只进日志,日志里也不打令牌本身(错的令牌常是对的令牌打错一个字)。
    # 响应文案三种失败一个字不差,理由见 messages.DENY 的头注。
    logger.warning(
        "打卡接口鉴权失败:path=%s 原因=%s",
        request.url.path,
        "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
    )
    return _respond(fail(ErrorCode.UNAUTHORIZED, messages.DENY), 401)


# ---------------------------------------------------------------------------
# header 解析与校验(② 的前半步;任何违约抛 HeaderValueError → 400)
# ---------------------------------------------------------------------------


class _CheckinMeta(NamedTuple):
    """一次打卡请求 header 里的全部业务字段(已解码、已校验)。"""

    event_id: str
    worker_name: str
    site_name: str | None
    geo_raw: str  # 进指纹的原始串;header 缺失时为 ""(「没发」本身也是指纹的一部分)
    geo_status: str
    lat: float | None
    lon: float | None
    accuracy_m: float | None
    source: str
    client_digest: str  # 客户端报的照片 hash —— 只喂幂等层①,不可信、不落库


def _validate_event_id(value: str) -> str:
    """幂等键的协议层校验:非空、限长、可见 ASCII。

    字符集收紧到可见 ASCII(0x21–0x7E):event_id 会进日志与 SQL 参数,
    控制字符是日志注入的入口(理由同 decode_name);正常客户端发的是 UUID,
    这个集合绰绰有余。
    """
    if not value:
        raise HeaderValueError("event_id 缺失或为空")
    if len(value) > MAX_EVENT_ID_LEN:
        raise HeaderValueError(f"event_id 长度 {len(value)} 超过上限 {MAX_EVENT_ID_LEN}")
    if not all("!" <= c <= "~" for c in value):
        raise HeaderValueError("event_id 含空白或不可见字符")
    return value


def _finite_float(text: str, what: str) -> float:
    """把 geo 的一段解析成**有限**浮点。

    ``float()`` 会把 "nan" / "inf" / "1e999" 照单全收,而 NaN 一旦进库,
    此后所有比较都是 False,排查起来像闹鬼 —— 必须在门口显式挡掉。
    """
    try:
        value = float(text)
    except ValueError as exc:
        raise HeaderValueError(f"{what}不是数字:{text[:32]!r}") from exc
    if not math.isfinite(value):
        raise HeaderValueError(f"{what}不是有限数:{text[:32]!r}")
    return value


def _parse_geo(raw: str | None) -> tuple[str, str, float | None, float | None, float | None]:
    """解析 ``X-GYT-Geo``,返回 (geo_raw, status, lat, lon, accuracy_m)。

    契约(模块头注「定位」一节):
      · header 整个缺失 → absent。**absent 不许由前端明发** —— 它的语义是
        「前端压根没发」,允许明发的话,这两种情况在库里就并成一种,
        审计想分「前端坏了」还是「用户拒了」就再也分不开;
      · ok → 恰好 4 段,纬度 ∈ [-90,90]、经度 ∈ [-180,180]、精度非负有限;
      · 其余状态 → 恰好 1 段 —— 「denied 还带坐标」一定是前端拼串的 bug,
        收下就是一条自相矛盾的记录。
    """
    if raw is None:
        return ("", "absent", None, None, None)
    parts = raw.split(GEO_SEPARATOR)
    status = parts[0]
    if status == "absent" or status not in att_db.GEO_STATUSES:
        raise HeaderValueError(f"定位状态不合法:{status[:32]!r}")
    if status == "ok":
        if len(parts) != 4:
            raise HeaderValueError(f"ok 状态应为 4 段,实际 {len(parts)} 段")
        lat = _finite_float(parts[1], "纬度")
        lon = _finite_float(parts[2], "经度")
        accuracy = _finite_float(parts[3], "精度")
        if not -90.0 <= lat <= 90.0:
            raise HeaderValueError(f"纬度越界:{lat}")
        if not -180.0 <= lon <= 180.0:
            raise HeaderValueError(f"经度越界:{lon}")
        if accuracy < 0:
            raise HeaderValueError(f"精度为负:{accuracy}")
        return (raw, status, lat, lon, accuracy)
    if len(parts) != 1:
        raise HeaderValueError(f"{status} 状态应只有 1 段,实际 {len(parts)} 段")
    return (raw, status, None, None, None)


def _parse_meta(request: Request) -> _CheckinMeta:
    """校验并解码全部业务 header。Starlette 的 Headers 大小写不敏感,
    直接用小写常量取值即可。"""
    headers = request.headers
    event_id = _validate_event_id(headers.get(HEADER_EVENT_ID, ""))
    worker_raw = headers.get(HEADER_WORKER)
    if worker_raw is None:
        raise HeaderValueError("缺少姓名字段")
    worker_name = decode_name(worker_raw)
    if not worker_name:
        raise HeaderValueError("姓名为空")
    site_raw = headers.get(HEADER_SITE)
    # 省略 X-GYT-Site 与传空值等价(build_digest 也这么看),统一归一成 None。
    site_name = (decode_name(site_raw) or None) if site_raw is not None else None
    geo_raw, geo_status, lat, lon, accuracy_m = _parse_geo(headers.get(HEADER_GEO))
    source = headers.get(HEADER_SOURCE, "")
    if source not in att_db.SOURCES:
        raise HeaderValueError(f"source 不在词表:{source[:32]!r}")
    digest_raw = headers.get(HEADER_DIGEST)
    if digest_raw is None:
        raise HeaderValueError("缺少照片指纹字段")
    return _CheckinMeta(
        event_id=event_id,
        worker_name=worker_name,
        site_name=site_name,
        geo_raw=geo_raw,
        geo_status=geo_status,
        lat=lat,
        lon=lon,
        accuracy_m=accuracy_m,
        source=source,
        client_digest=validate_digest_hex(digest_raw),
    )


def _meta_digest(meta: _CheckinMeta, photo_hash: str) -> str:
    """同一份元数据 + 两种来源的照片 hash → 指纹。
    层①(客户端报的 hash)与层②(服务端算的 hash)的唯一差异点收在这里。"""
    return build_digest(
        worker_name=meta.worker_name,
        site_name=meta.site_name,
        geo_raw=meta.geo_raw,
        photo_hash=photo_hash,
    )


def _receipt_payload(row: att_db.AttendanceRow) -> dict[str, Any]:
    """凭证对象 —— 模块头注冻结的那 9 个键,POST 200 与 GET recent 共用。
    刻意不透出 event_id / req_digest / id:前端用不上,少给少错。"""
    return {
        "receipt_no": row.receipt_no,
        "worker_name": row.worker_name,
        "site_name": row.site_name,
        "checked_at": row.checked_at,
        "work_date": row.work_date,
        "geo_status": row.geo_status,
        "source": row.source,
        "artifact_id": row.artifact_id,
        "photo_purged_at": row.photo_purged_at,
    }


# ---------------------------------------------------------------------------
# body 收取与落账(③–⑦)
# ---------------------------------------------------------------------------


async def _read_photo(request: Request) -> bytes | JSONResponse:
    """③ 流式收 body,边收边数,超限**当场**返回,不等收完。

    这正是 raw body 协议的意义(模块头注):multipart 要解析完整请求才知道
    多大,而 ``Request.stream()`` 逐 ASGI 事件 yield,读到哪算到哪。
    """
    settings = get_settings()
    limit = int(settings.photo_max_mb * _BYTES_PER_MB)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            return _respond(
                fail(
                    ErrorCode.FILE_TOO_LARGE,
                    messages.photo_too_large(settings.photo_max_mb),
                    detail=f"body 已收 {received} 字节,超过上限 {limit}",
                ),
                413,
            )
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        return _respond(fail(ErrorCode.INVALID_INPUT, messages.PHOTO_EMPTY), 400)
    if not payload.startswith(JPEG_MAGIC):
        return _respond(fail(ErrorCode.INVALID_INPUT, messages.PHOTO_NOT_JPEG), 400)
    return payload


def _render_receipt_photo(
    meta: _CheckinMeta, photo: bytes, snap: receipt.TimeSnapshot, receipt_no: str
) -> bytes | JSONResponse:
    """⑤ 画水印。文字行全部出自 messages.py(D12);渲染实现归 B 泳道,
    这里只把三种冻结的异常翻译成响应。

    必须经 ``watermark.`` 模块属性调用 —— 测试(与 B 泳道并行期的所有测试)
    靠 ``mock.patch("gyt.attendance.watermark.render_attendance_photo")`` 替换,
    from-import 会把名字焐死在本模块里,patch 就落空了。
    """
    lines = messages.watermark_lines(
        worker_name=meta.worker_name,
        site_name=meta.site_name,
        display_time=snap.display,
        receipt_no=receipt_no,
        geo_status=meta.geo_status,
        lat=meta.lat,
        lon=meta.lon,
        accuracy_m=meta.accuracy_m,
    )
    try:
        return watermark.render_attendance_photo(photo, lines)
    except watermark.MissingGlyphsError as exc:
        # 用户错误:点名是哪些字,工友换个写法自己就能解决(W7 §3.4)。
        return _respond(fail(ErrorCode.INVALID_INPUT, messages.missing_glyphs(exc.chars)), 400)
    except watermark.FontUnavailableError as exc:
        # 部署错误:该硬失败引人来修,细节只进日志,工友只拿「转给管理员」这句话。
        return _respond(
            fail(ErrorCode.INTERNAL, messages.FONT_BROKEN, detail=f"水印字体不可用:{exc}"),
            500,
        )
    except watermark.WatermarkError as exc:
        # 魔数对但图解不开(截断/伪造尾部):还是用户侧的问题,引导重拍。
        return _respond(
            fail(
                ErrorCode.INVALID_INPUT,
                messages.PHOTO_UNREADABLE,
                detail=f"凭证图渲染失败:{exc}",
            ),
            400,
        )


def _make_draft(
    meta: _CheckinMeta,
    snap: receipt.TimeSnapshot,
    req_digest: str,
    receipt_no: str,
    artifact_id: str,
) -> att_db.CheckinDraft:
    """拼待写入的台账行。created_at 与 checked_at 同源(单一快照,
    receipt.py 头注)—— 这层绝不自己取 now。"""
    return att_db.CheckinDraft(
        event_id=meta.event_id,
        req_digest=req_digest,
        worker_name=meta.worker_name,
        site_name=meta.site_name,
        checked_at=snap.checked_at,
        work_date=snap.work_date,
        lat=meta.lat,
        lon=meta.lon,
        accuracy_m=meta.accuracy_m,
        geo_status=meta.geo_status,
        source=meta.source,
        receipt_no=receipt_no,
        artifact_id=artifact_id,
        created_at=snap.checked_at,
    )


def _resolve_event_race(event_id: str, req_digest: str) -> JSONResponse:
    """幂等层③:INSERT 撞了 event_id 唯一约束 —— 并发同键,本请求是输家。

    回查赢家那条:指纹相同就把它当成自己的结果返回(工友看不出输赢,也不该
    看出);不同则如实 409。本请求刚 register 的图成了孤儿 —— **不当场删**,
    交清理器(W7 §3.9 的老化窗口):当场删要再背一条删除路径的复杂度,
    而这个窗口的触发频率撑不起它。
    """
    winner = att_db.find_by_event_id(event_id)
    if winner is None:
        # 撞了唯一约束却查不回来:台账状态异常,交给兜底 500(logger.exception 记全)。
        raise RuntimeError("event_id 撞唯一约束但回查不到,台账状态异常")
    if winner.req_digest == req_digest:
        return _respond(ok(_receipt_payload(winner), messages.CHECKIN_REPLAYED), 200)
    return _respond(fail(ErrorCode.CONFLICT, messages.CHECKIN_CONFLICT), 409)


def _persist_checkin(meta: _CheckinMeta, photo: bytes, req_digest: str) -> JSONResponse:
    """⑤⑥⑦:时间快照 → 画水印 → register → 写台账。

    顺序不许颠倒(先落图后写库,W7 §3.3):图成功库失败 = 孤儿文件(无害,
    清理器收);库成功图失败 = 死链(工友点了打不开,还没提示)。

    ``receipt_no`` 撞库时换编号**从水印起重来**:编号画在图上,只换库里那一列,
    图上的编号就和台账对不上 —— 凭证自己证伪自己。旧图成孤儿,交清理器。
    时间快照不重取:重试的是编号(随机尾),不是这次打卡的时刻。
    """
    snap = receipt.make_snapshot()
    for _ in range(1 + RECEIPT_RETRY_MAX):
        receipt_no = receipt.new_receipt_no(snap)
        stamped = _render_receipt_photo(meta, photo, snap, receipt_no)
        if isinstance(stamped, JSONResponse):
            return stamped
        artifact_id = artifacts.register(
            stamped,
            kind=artifacts.ArtifactKind.ATTENDANCE,
            original_name=f"{receipt_no}.jpg",
        )
        draft = _make_draft(meta, snap, req_digest, receipt_no, artifact_id)
        try:
            row = att_db.insert_checkin(draft)
        except att_db.DuplicateEventError:
            return _resolve_event_race(meta.event_id, req_digest)
        except att_db.DuplicateReceiptError:
            continue
        return _respond(ok(_receipt_payload(row), messages.CHECKIN_OK), 200)
    return _respond(
        fail(
            ErrorCode.INTERNAL,
            detail=f"receipt_no 连续 {1 + RECEIPT_RETRY_MAX} 次撞库,疑似随机源或台账异常",
        ),
        500,
    )


# ---------------------------------------------------------------------------
# 两个端点
# ---------------------------------------------------------------------------


async def post_checkin(request: Request) -> JSONResponse:
    """POST /checkin。响应契约见模块头注,这里只按编号走流程。

    首行不用反引号是刻意的:starlette 生成 /docs 时把 docstring 喂给 yaml,
    ` 开头必炸(无害但每次启动打两条 traceback,查日志的人会被带偏)。
    """
    try:
        denied = _deny_if_token_bad(request)  # ⓪ 令牌自查
        if denied is not None:
            return denied

        # ① 限流,两只桶都在读 body 之前结算 —— 挡住的必须是流量,不只是写库。
        wait_s = get_global_limiter().take(GLOBAL_BUCKET_IDENTITY)
        if wait_s is not None:
            return _rate_limited(wait_s)

        # ② 前半步:校验并取 header。worker 桶的桶键是**解码后的**姓名,
        # 所以它排在解码之后 —— 校验只看 header、不碰 body,①的约束没有破。
        try:
            meta = _parse_meta(request)
        except HeaderValueError as exc:
            return _respond(
                fail(ErrorCode.INVALID_INPUT, messages.BAD_REQUEST, detail=str(exc)), 400
            )

        wait_s = get_worker_limiter().take(meta.worker_name)
        if wait_s is not None:
            return _rate_limited(wait_s)

        # ② 幂等层①:读 body 之前短路。比对用的是**客户端报的** hash ——
        # 谎报的后果只是它自己拿到 409/对不上,伪造不出任何服务端状态(模块头注)。
        existing = att_db.find_by_event_id(meta.event_id)
        if existing is not None:
            if _meta_digest(meta, meta.client_digest) == existing.req_digest:
                return _respond(ok(_receipt_payload(existing), messages.CHECKIN_REPLAYED), 200)
            return _respond(fail(ErrorCode.CONFLICT, messages.CHECKIN_CONFLICT), 409)

        photo = await _read_photo(request)  # ③ 流式收 body
        if isinstance(photo, JSONResponse):
            return photo

        # ④ 落库指纹用**服务端自己算的** hash —— header 那个到此为止,绝不落库。
        req_digest = _meta_digest(meta, photo_sha256(photo))
        return _persist_checkin(meta, photo, req_digest)  # ⑤⑥⑦
    except Exception:
        # 兜底:任何没料到的炸都收敛成 500 信封。堆栈只进日志;user_msg 走
        # DEFAULT_USER_MSG[INTERNAL](已是人话),不在这里另造第二句。
        logger.exception("打卡请求处理失败")
        return _respond(fail(ErrorCode.INTERNAL), 500)


def _clamp_recent_limit(raw: str | None) -> int:
    """``?limit=`` 的宽容解析:缺省与上限都取 settings.attendance_recent_limit,
    超了 clamp、不报错(响应契约明文);非整数按缺省 —— 这个参数只影响条数,
    为它报 400 只会多一条前端要处理的分支;下限 1 与 db 层 list_recent 一致。"""
    ceiling = get_settings().attendance_recent_limit
    if raw is None:
        return ceiling
    try:
        requested = int(raw)
    except ValueError:
        return ceiling
    return min(max(1, requested), ceiling)


async def get_checkin_recent(request: Request) -> JSONResponse:
    """GET /checkin/recent —— 最近凭证,插入序倒排(首行不用反引号,理由同 POST;为什么不按 checked_at
    排,见 db 层 _RECENT_SQL 的头注)。同样自查令牌;读操作不限流(与 auth.py
    「只卡花钱动作」同一取舍 —— 这里不花钱的是水印,查列表不画图)。"""
    try:
        denied = _deny_if_token_bad(request)
        if denied is not None:
            return denied
        rows = att_db.list_recent(_clamp_recent_limit(request.query_params.get("limit")))
        return _respond(ok({"records": [_receipt_payload(r) for r in rows]}), 200)
    except Exception:
        logger.exception("查询最近打卡失败")
        return _respond(fail(ErrorCode.INTERNAL), 500)


# ---------------------------------------------------------------------------
# app —— langgraph.json 的 ``http.app`` 指到这里(``./src/gyt/checkin_api.py:app``)。
# 必须是货真价实的模块级变量:langgraph-api 按文件路径加载后直接从模块字典取,
# 与 graph.py 的 ``graph`` 同一条规矩。路由挂绝对路径,由 langgraph 合并进主应用。
# 注意模块 import 期**不碰 get_settings()**:令牌、限流参数、大小上限全都在
# 请求期现取 —— 这是「不许 import 时缓存令牌」那条要求的结构保证。
# ---------------------------------------------------------------------------
CHECKIN_ROUTES: Final[list[Route]] = [
    Route("/checkin", post_checkin, methods=["POST"]),
    Route("/checkin/recent", get_checkin_recent, methods=["GET"]),
]
"""打卡这两条路由。**真正挂上去的入口是 backend/webapp.py**,不是下面那个 app。

为什么要把路由单拎出来:``langgraph.json`` 的 ``http.app`` **只能有一个**,
而这个项目现在有两拨自定义路由 —— 队友的项目/图纸/资料管理(webapp.py)与
这里的打卡。2026-08-15 合流时撞上,解法是 webapp.py 把本列表铺进它的 routes。

⚠️ 所以**改这里的路径要去 webapp.py 确认它确实收了**;反过来,
webapp.py 那侧删掉这一铺,打卡端点就整个消失 —— 而现象是 404,
不是启动报错(langgraph 不知道有谁本该在)。
"""

app = Starlette(routes=list(CHECKIN_ROUTES))
"""只给本模块的单元测试用(``TestClient(app)``),**线上不走它**。

留着的理由:测试要能脱离 webapp.py 单独验打卡的鉴权/限流/幂等 ——
webapp.py 在 backend/ 根、不属于 gyt 包,把它拖进单测会连带整个项目管理栈。
"""


__all__ = [
    "HEADER_DIGEST",
    "HEADER_EVENT_ID",
    "HEADER_GEO",
    "HEADER_SITE",
    "HEADER_SOURCE",
    "HEADER_WORKER",
    "HeaderValueError",
    "app",
    "build_digest",
    "decode_name",
    "encode_name",
    "photo_sha256",
    "validate_digest_hex",
]
