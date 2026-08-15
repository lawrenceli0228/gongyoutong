#!/usr/bin/env python3
"""工友通公网入口的登录服务 —— 替掉浏览器那个原生 basic_auth 弹框。

为什么要有这个文件
==================
`basic_auth` 那道门是能用的,但它的登录界面是**浏览器自己画的**那个灰框:
没有产品名、没有一句人话、没法说「这是内测环境,口令找发你链接的人要」。
而且只要服务端回 `WWW-Authenticate: Basic`,那个框就一定会弹 —— 它不可美化,
唯一的办法是**根本不发那个头**,自己做登录页。

于是变成:

    公网 ──▶ Caddy
              ├── /login, /logout        ──▶ 本服务(**公开**,不然登录页自己也被拦,死循环)
              └── 其余全部
                    └ forward_auth ──▶ 本服务 /_auth/verify
                         204 → 放行
                         无效 → 网页请求 302 去 /login;/api/* 回 401 JSON

顺带解决掉的两件旧账
====================
· **`<img>` 和 SSE 带不带凭据**。basic_auth 靠浏览器「记住并自动重发」,那套行为
  一直没在真浏览器上验过(见 docs/W6 的未验证清单)。换成 Cookie 之后,
  同源请求带 Cookie 是浏览器的**规定动作**,`<img src>`、`fetch`、`EventSource`
  一律带上 —— 不再依赖一个没验证过的假设。
· **登录接口没有限流**。Caddyfile 里那段注释自己承认:换着花样发错口令能持续
  消耗 CPU,而 Caddy 官方镜像不带限流模块。本服务自己带一个(见 `_LoginThrottle`)。

为什么口令哈希从 bcrypt 换成 scrypt
====================================
`hashlib.scrypt` 在**标准库里**,而 bcrypt 不在。本服务和 artifacts 服务一样跑在
`python:3.12-slim` 上、**零 pip 安装** —— 多装一个包就多一条构建期的网络依赖和
一份供应链风险,为了一个哈希函数不值。scrypt 是内存硬的,抗 GPU 爆破比 bcrypt 更好。

**口令本身不用改**:`scripts/make_login_hash.py` 拿旧口令算出新哈希即可。

会话 Cookie 的构成
==================
    gyt_sess = v1.<到期unix秒>.<hmac_sha256(会话密钥, "v1.<到期unix秒>")>

无状态:重启不掉线、不需要任何存储。**会话密钥从口令哈希派生**(见 `_session_key`),
所以不用新增配置项,而且副作用正好是想要的:**改了口令,所有旧会话立刻失效**。

登录成功之后回哪儿去(2026-08-15 / W8 扫码配对)
================================================
以前是硬编码 `Location: /`,于是**一个目的地要被丢三次**:

    ① /_auth/verify   Caddy 把原始地址放在 X-Forwarded-Uri 里递过来了,
                      而这里只拿它判「网页还是 /api/」,判完就扔;
    ② login-page.html 表单里只有 pw 一个字段,没人把目的地带下去;
    ③ POST /login     校验通过后写死 `Location: /`。

三处任意一处不改,目的地都活不到最后。现在的走法是:

    /_auth/verify ──302──▶ /login?next=<urlencode 后的目的地>
                                 │  页面把 next 塞进 hidden 字段(只搬运,不判断)
                                 ▼
                           POST /login (pw + next)
                                 │  safe_next_path() 白名单校验 —— 唯一判据在服务端
                                 ▼
                           302 到那个目的地(不合法就退回 /)

这件事的直接受益者是扫码打卡:二维码编的是 `/?checkin=1&pair=<32位hex>`,
少了 next 就等于扫完落在一张干净的首页上,配对永远停在 waiting。

**这里是本服务唯一一处「外部可控的值会被写进响应头」**,所以 next 的校验是白名单式的,
见 `_NEXT_SHAPE` 与 `safe_next_path`。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.cookies
import os
import re
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_PORT: Final[int] = 8790
"""端口有两处同源:这里 + docker-compose.vps.yml 的 forward_auth 目标。改要一起改。"""

COOKIE_NAME: Final[str] = "gyt_sess"
SESSION_MAX_AGE: Final[int] = 14 * 24 * 3600
"""会话有效期 14 天。

内测环境,重点是「别让测试的人每天重新登一次」而不是「把窗口压到最小」。
真要下线一个人,改口令即可 —— 会话密钥从口令哈希派生,改口令 = 全员下线。
"""

LOGIN_PATH: Final[str] = "/login"
LOGOUT_PATH: Final[str] = "/logout"
VERIFY_PATH: Final[str] = "/_auth/verify"

# 绑定地址:与 scripts/serve_artifacts.py 完全同一套推理,那边有完整注释。
# 宿主机上只绑回环且**没有开关**;容器里绑 0.0.0.0,而它只在该容器的网络命名空间内 ——
# 前提是这个服务**不许有 ports**(docker-compose.vps.yml + preflight ⑤ 组在守)。
_IN_CONTAINER: Final[bool] = Path("/.dockerenv").exists()
BIND_HOST: Final[str] = "0.0.0.0" if _IN_CONTAINER else "127.0.0.1"  # noqa: S104

# scrypt 参数。n=2^14 / r=8 / p=1 ≈ 16MB 内存、单次约 50-100ms。
# **不许为了「快一点」调低 n**:这个数同时是离线爆破的成本。
# 也不许调太高 —— 登录接口是公开的,单次耗时直接变成 DoS 的杠杆(见 _LoginThrottle)。
SCRYPT_N: Final[int] = 1 << 14
SCRYPT_R: Final[int] = 8
SCRYPT_P: Final[int] = 1
SCRYPT_DKLEN: Final[int] = 32

HASH_PREFIX: Final[str] = "scrypt$"
"""哈希串长相:``scrypt$<n>$<r>$<p>$<salt hex>$<dk hex>``。由 make_login_hash.py 生成。"""

# 失败时固定睡这么久。scrypt 本身已经慢,这一条是为了把「口令对不对」的耗时差抹平,
# 免得有人靠计时区分「口令格式不对」和「口令错了」。
_FAIL_SLEEP_S: Final[float] = 0.25

TEXT_BAD_METHOD: Final[str] = "只支持 GET / POST。\n"
TEXT_NOT_FOUND: Final[str] = "没有这个页面。\n"
TEXT_TOO_MANY: Final[str] = "试得太频繁了,请等一分钟再试。\n"
JSON_UNAUTHORIZED: Final[str] = '{"detail":"登录已过期,请刷新页面重新登录。"}'


# --------------------------------------------------------------------------
# 登录成功后的去向(next)—— 白名单校验
#
# 【同源三处,改一处必须三处一起改】
#   · 本文件的 NEXT_PARAM(读取点有三个:_handle_verify / _handle_login_page /
#     _handle_login_submit,都走同一个常量,不许写字面量);
#   · scripts/login-page.html 里那个 hidden 字段的 name;
#   · _read_page() 的启动期硬校验(缺了这个字段直接拒绝启动,理由见那里)。
# 改名而不同步的表现是**没有任何报错**:登录照样成功,只是每次都丢回首页。
# --------------------------------------------------------------------------

NEXT_PARAM: Final[str] = "next"
"""目的地参数名。查询串里叫它,表单 hidden 字段也叫它,两边必须同名。"""

NEXT_MAX_LEN: Final[int] = 512
"""next 的长度上限。

不设上限 = 任何人都能让这台机器把一个 100KB 的字符串拼进 `Location:` 头再发出去,
一条请求就放大成一次带宽消耗。512 对真实目的地绰绰有余 ——
本项目最长的那个 `/?checkin=1&pair=<32位hex>` 也才 47 个字符。
"""

_NEXT_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"\A/(?:[^/\\\x00-\x1f\x7f][^\x00-\x1f\x7f]*)?\Z"
)
r"""合法 next 的形状。**这是白名单,不是黑名单** —— 只有匹配上的才采用。

    \A/                        必须以**单个** / 开头 ⇒ 只可能是本站的绝对路径
       [^/\\\x00-\x1f\x7f]     第二个字符不许是 / 、\ 或控制字符
                       [^…]*   后面也不许再出现任何控制字符
                            \Z 到此为止,后面一个字符都不许有

为什么不写成「排除已知坏形状」:坏形状列不完,而漏一条就是一个开放重定向。
下面几串都是真在野外用过的,逐条对着上面的分解看一遍就知道白名单省心在哪:

    //evil.com          协议相对 URL。浏览器自己补上当前协议 → https://evil.com,
                        **直接跳出站**。它长得极像一个本站路径,是最常被漏掉的一种。
    /\evil.com          IE/Edge 和部分浏览器把 \ 当 / 用 ⇒ 等价于 //evil.com。
    /\/evil.com         同上,多绕一层。
    https://evil.com    压根不以 / 开头。
    /foo\r\nSet-Cookie: gyt_sess=伪造的会话
                        响应头注入。这一串会被原样拼进 `Location:` 那一行,
                        CRLF 之后的内容就成了**新的一行响应头** ——
                        等于让外人给受害者种任意 Cookie,包括一个伪造的会话 Cookie。

⚠️ 用 `\A` / `\Z` 而**不是** `^` / `$`。Python 的 `$` 会额外匹配「末尾换行之前」,
   于是 `/foo\n` 拿 `^/[^/\\].*$` 是**匹配得上的** —— 一个带尾换行的 next 就这么
   混进 Location 头,上面那条 CRLF 防线当场作废。
   tests/unit/test_serve_login.py 里「尾部换行」那条用例专门钉这个。
"""

_LOCATION_SAFE: Final[str] = "".join(chr(c) for c in range(0x21, 0x7F))
"""`Location:` 头里允许原样出现的字符 = 全部可打印 ASCII(不含空格)。给 encode_location 用。"""


def safe_next_path(raw: str) -> str:
    r"""把外部可控的 next 收敛成「要么是本站的一个绝对路径,要么什么都不是」。

    返回原串 = 采用;返回**空串** = 不采用(所有调用方一律退回 ``/``)。

    刻意**不做任何修补**:`//evil.com` 不会被"洗"成 `/evil.com`。修补是黑名单思维
    换了件衣服 —— 一旦开始修补,下一个人就会默认这里出来的东西都是干净的,
    然后在别处少写一次校验。要么原样通过,要么整条丢掉。
    """
    if not raw or len(raw) > NEXT_MAX_LEN:
        return ""
    if "://" in raw:
        # 形状检查已经挡掉了「以 // 或 /\ 开头」,这一条挡的是藏在中段的绝对 URL,
        # 例如 `/go?to=https://evil.com` 这种会被下游再解析一次的形态。
        # 对本站真实目的地零误伤:正常路径里不会出现 :// 。
        return ""
    if _NEXT_SHAPE.match(raw) is None:
        return ""
    return raw


def encode_location(value: str) -> str:
    r"""把一个已经过 :func:`safe_next_path` 的值编码成能塞进 ``Location:`` 头的样子。

    为什么非要这一步:``BaseHTTPRequestHandler.send_header`` 拼完头是用
    **latin-1 严格模式**编码的(``.encode('latin-1', 'strict')``)。目的地里只要有一个
    中文字符,这里就是 ``UnicodeEncodeError`` —— 表现是口令明明是对的,点下去却 500、
    连接直接断。查的人会去翻口令、翻 Cookie、翻 Caddy,没人会想到是一句响应头编不出来。

    ``safe`` 取「除空格外的全部可打印 ASCII」,是为了**只**动那些编不出去的字节:
      · ``%`` 在 safe 里 ⇒ 已经是 ``/%E9%A1%B9`` 的目的地不会被二次编码成 ``/%25E9``
        (二次编码同样不报错,表现是登录后落到一个 404 的怪路径);
      · ``?`` ``&`` ``=`` ``#`` 在 safe 里 ⇒ query 与锚点原样保留。
    """
    return urllib.parse.quote(value, safe=_LOCATION_SAFE)


def login_url(next_path: str, *, error: bool = False) -> str:
    """拼一个指向登录页的地址,顺带把目的地带上(不合法就悄悄丢掉)。

    ``next`` 是塞进**查询串的值**,所以用 ``safe=""`` 全量编码 ——
    目的地里的 ``&`` ``=`` 不编的话会把自己劈成两个参数,页面读到的就是半截路径。
    全量编码的输出必然是纯 ASCII,所以这条路径不需要再过 encode_location。
    """
    query: list[str] = []
    if error:
        query.append("e=1")
    target = safe_next_path(next_path)
    # 目的地就是首页时不带 next:`/login?next=%2F` 与裸 `/login` 效果完全一样,
    # 少一段噪音,也让「不带 next」这条老路径的行为一个字节都没变。
    if target and target != "/":
        query.append(f"{NEXT_PARAM}={urllib.parse.quote(target, safe='')}")
    return f"{LOGIN_PATH}?{'&'.join(query)}" if query else LOGIN_PATH


def _next_from_query(path_with_query: str) -> str:
    """从一个 ``/login?next=…`` 形态的请求行里取出 next(**未校验**,调用方自己过白名单)。"""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path_with_query).query)
    return (query.get(NEXT_PARAM) or [""])[0]


# --------------------------------------------------------------------------
# 口令校验
# --------------------------------------------------------------------------


def _parse_hash(raw: str) -> tuple[int, int, int, bytes, bytes] | None:
    """拆 ``scrypt$n$r$p$salt$dk``。格式不对返回 None(**不抛异常**)。

    起服务的时候格式错要能给一句人话,而不是一串 traceback ——
    这个服务起不来 = 整个站点进不去,报错必须让人一眼知道去改哪儿。
    """
    if not raw.startswith(HASH_PREFIX):
        return None
    parts = raw.split("$")
    if len(parts) != 6:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        dk = bytes.fromhex(parts[5])
    except ValueError:
        return None
    if not salt or not dk:
        return None
    return n, r, p, salt, dk


def verify_password(raw_hash: str, plaintext: str) -> bool:
    """口令对不对。任何异常一律当成「不对」,绝不把细节漏出去。"""
    parsed = _parse_hash(raw_hash)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = hashlib.scrypt(
            plaintext.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------
# 会话签名
# --------------------------------------------------------------------------


def _session_key(raw_hash: str) -> bytes:
    """从口令哈希派生会话签名密钥。

    **刻意不另开一个 GYT_SESSION_SECRET**:多一个必填项就多一处能忘、能填错、
    能在两台机器上不一致的地方,而它换来的隔离在这个形态下没有意义 ——
    能读到会话密钥的人本来就能读到口令哈希(同一个 .env、同一个容器环境)。

    副作用正好是想要的:**改口令 ⇒ 派生密钥变 ⇒ 所有已发出的会话立刻失效**。
    """
    return hmac.new(b"gyt-session-v1", raw_hash.encode("utf-8"), hashlib.sha256).digest()


def issue_session(raw_hash: str, now: int, max_age: int = SESSION_MAX_AGE) -> str:
    exp = now + max_age
    payload = f"v1.{exp}"
    sig = hmac.new(_session_key(raw_hash), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_session(raw_hash: str, token: str, now: int) -> bool:
    """会话有效吗。**先验签再看过期** —— 顺序反了等于让人拿伪造的过期时间试探。"""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False
    payload = f"{parts[0]}.{parts[1]}"
    expect = hmac.new(_session_key(raw_hash), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, parts[2]):
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    return exp > now


# --------------------------------------------------------------------------
# 登录限流
# --------------------------------------------------------------------------


class _LoginThrottle:
    """POST /login 的全局限流器 —— 补上 Caddyfile 自己承认没解决的那条。

    形态和粒度都刻意选得很粗:**全局一个桶,不按 IP 分**。
    理由和 backend/auth.py 的令牌桶一样 —— 这里只有一个身份(一个口令),
    分不出人;桶护的是**这台 2 vCPU 机器的 CPU**,不是人与人之间的公平。

    不按 IP 分还有一个正面效果:换 IP 刷不动。代价是有人乱试的时候
    正常测试的人也会被挡一会儿 —— 内测环境,这个代价可以接受。
    """

    def __init__(self, capacity: int, per_minute: float) -> None:
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._rate = per_minute / 60.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class LoginHandler(BaseHTTPRequestHandler):
    server_version = "GytLogin/1.0"

    # 由 build() 注入
    password_hash: str = ""
    page_html: bytes = b""
    throttle: _LoginThrottle | None = None

    # --- 工具 -------------------------------------------------------------

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _is_https(self) -> bool:
        """当前这条链路是不是 https —— 决定 Cookie 要不要打 Secure 标。

        Caddy 反代默认会带 X-Forwarded-Proto。裸 IP 模式(GYT_SITE_ADDRESS=":80")
        下是 http,**这时打 Secure 会让 Cookie 根本存不下来**,表现是「登录成功
        然后马上又跳回登录页」,而且没有任何报错 —— 死循环,最难查的那一类。
        """
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _set_session_cookie(self, value: str, max_age: int) -> None:
        bits = [
            f"{COOKIE_NAME}={value}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",  # JS 读不到 —— XSS 偷不走会话
            "SameSite=Lax",  # 跨站表单/跳转不带,同源的 img/fetch/SSE 照常带
        ]
        if self._is_https():
            bits.append("Secure")
        self.send_header("Set-Cookie", "; ".join(bits))

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_bytes(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 登录页不该被嵌进别人的 iframe 里骗点击
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    # --- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的分发约定
        path = urllib.parse.urlsplit(self.path).path
        if path == VERIFY_PATH:
            self._handle_verify()
        elif path == LOGIN_PATH:
            self._handle_login_page()
        elif path == LOGOUT_PATH:
            self._handle_logout()
        else:
            self._send_text(HTTPStatus.NOT_FOUND, TEXT_NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlsplit(self.path).path != LOGIN_PATH:
            self._send_text(HTTPStatus.NOT_FOUND, TEXT_NOT_FOUND)
            return
        self._handle_login_submit()

    def __getattr__(self, name: str):
        """GET/HEAD/POST 之外一律 405。理由同 serve_artifacts.py 里那段。"""
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def _reject_method(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD, POST")
        self.send_header("Content-Length", str(len(TEXT_BAD_METHOD.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(TEXT_BAD_METHOD.encode("utf-8"))

    # --- 各端点 -----------------------------------------------------------

    def _handle_verify(self) -> None:
        """Caddy 的 forward_auth 打这里。204 = 放行,其余 = 把响应原样回给客户端。"""
        if check_session(self.password_hash, self._cookie_token(), int(time.time())):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # 没登录 / 过期。**分两种回法**,这一步很关键:
        # 网页请求给 302 去登录页;而 /api/* 是前端的 fetch/SSE 在打,
        # 给它 302 到一张 HTML 登录页只会让前端拿到一坨 HTML 当 JSON 解析,
        # 报出一个和「登录过期」毫无关系的错。
        forwarded = self.headers.get("X-Forwarded-Uri", "")
        uri = forwarded or self.path
        if uri.startswith("/api/"):
            self._send_bytes(
                HTTPStatus.UNAUTHORIZED,
                JSON_UNAUTHORIZED.encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        # 目的地**只认 X-Forwarded-Uri**(Caddy 的 forward_auth 会把原始的「路径+query」
        # 放在这个头里递过来)。上面那行拿 self.path 兜底只是为了判「是不是 /api/」——
        # 走到这儿时 self.path 恒为 /_auth/verify,拿它当目的地会让人登完之后
        # 落在一个给 Caddy 用的内部端点上,看到的是一片空白。
        # 值不合法(比如有人自己伪造一个 X-Forwarded-Uri: //evil.com 来钓)时
        # login_url 会把它整条丢掉,退化成裸 /login —— 与改动前的行为完全一致。
        self._redirect(login_url(forwarded))

    def _handle_login_page(self) -> None:
        if check_session(self.password_hash, self._cookie_token(), int(time.time())):
            # 已经有会话还落到登录页(两个标签页、或者手机上先前登过):
            # 直接送去本来要去的地方,而不是一律丢回首页 ——
            # 扫码那条链上,「首页」和「带 pair 的首页」是两个完全不同的目的地。
            self._redirect(encode_location(safe_next_path(_next_from_query(self.path)) or "/"))
            return
        self._send_bytes(HTTPStatus.OK, self.page_html, "text/html; charset=utf-8")

    def _handle_logout(self) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", LOGIN_PATH)
        self._set_session_cookie("", 0)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _handle_login_submit(self) -> None:
        if self.throttle is not None and not self.throttle.take():
            # 429 而不是 302:让脚本能立刻看懂,也让浏览器不会以为口令错了
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("Retry-After", "60")
            body = TEXT_TOO_MANY.encode("utf-8")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        # 登录表单就一个字段,给 4KB 绰绰有余。不设上限等于让人拿一个大 body 占内存。
        if length < 0 or length > 4096:
            time.sleep(_FAIL_SLEEP_S)
            self._redirect(f"{LOGIN_PATH}?e=1")
            return

        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        fields = urllib.parse.parse_qs(raw, keep_blank_values=True)
        pw = (fields.get("pw") or [""])[0]
        # 目的地由登录页那个 hidden 字段原样带回来。**校验只在这里做**:
        # 页面上那份 JS 只负责搬运,对着手工构造的 POST 一点作用都没有。
        next_path = (fields.get(NEXT_PARAM) or [""])[0]

        if pw and verify_password(self.password_hash, pw):
            self.send_response(HTTPStatus.FOUND)
            # 白名单不通过就退回 /(而不是报错)—— 登录本身是成功的,
            # 没有任何理由因为一个坏 next 就把人挡在门外。
            self.send_header("Location", encode_location(safe_next_path(next_path) or "/"))
            self._set_session_cookie(
                issue_session(self.password_hash, int(time.time())), SESSION_MAX_AGE
            )
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        time.sleep(_FAIL_SLEEP_S)
        # ?e=1 只是个开关,页面靠它显示一行「口令不对」。
        # **不回显用户输入的任何内容** —— 那会直接开一个反射面。
        # next 是唯一被带回去的外部输入,它的安全性靠两件事,不靠"信任":
        #   ① 已过 safe_next_path 白名单,坏形状在这一步就被整条丢掉;
        #   ② 用 urlencode 塞进查询串,页面拿到后是用 `input.value = …` 赋值的,
        #      **服务端一个字节都不往 HTML 里拼**,不存在反射型 XSS 的落点。
        # 带上它的理由很实在:20 位随机口令在手机上打错一次太常见了,
        # 一错就把扫码带来的目的地弄丢,人就再也回不到打卡面板。
        self._redirect(login_url(next_path, error=True))

    # --- 日志 -------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        """只打方法 + 路径 + 状态,**绝不打 query 和 body**。

        原始 log_message 会把整行请求打出来,而这个服务的 body 里就是口令本身。
        """
        sys.stderr.write(f"[登录] {self.command} {urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")
        sys.stderr.flush()


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def _read_page(path: Path) -> bytes:
    """读登录页模板,顺带做一次启动期硬校验。

    登录页是静态文件,两处动态内容都由页面自己读 URL 切换(?e=1 显示「口令不对」、
    ?next= 填进 hidden 字段)—— 服务端一个字节的用户输入都不往 HTML 里拼。
    这里只确认模板没缺件,否则等于发了一张点不动、或者点了就丢目的地的页面。

    ⚠️ ``name="next"`` 这一条是 2026-08-15(W8 扫码配对)加的**同源闸**。
       少了它**不会有任何报错**:登录照样成功、站点照样能用,只是每次都丢回首页 ——
       手机扫码带来的 `?checkin=1&pair=…` 悄无声息地消失,电脑那头永远停在 waiting,
       而两边都不会打印一个字。宁可在启动时用一句人话拦下来。
       代价要认清:只更新了 .py 没更新 .html 的话,服务起不来 = 全站进不去。
       两个文件在同一个 scripts/ 目录、同一次部署里走,本来就不该分开更新。
    """
    html = path.read_text(encoding="utf-8")
    required = (f'action="{LOGIN_PATH}"', 'name="pw"', f'name="{NEXT_PARAM}"')
    missing = [token for token in required if token not in html]
    if missing:
        raise SystemExit(
            f"[错误] 登录页模板不对:{path}\n"
            f"       缺了这些标记:{'、'.join(missing)}\n"
            f'       表单必须是 action="{LOGIN_PATH}" method="post",'
            f'口令框 name="pw",\n'
            f'       另外要有一个 hidden 的 name="{NEXT_PARAM}" 用来带回登录后的去向。'
        )
    return html.encode("utf-8")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="工友通登录服务(替代 basic_auth 弹框)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--page",
        type=Path,
        default=Path(__file__).resolve().parent / "login-page.html",
        help="登录页 HTML",
    )
    parser.add_argument("--login-attempts-per-minute", type=float, default=10.0)
    parser.add_argument("--login-burst", type=int, default=5)
    args = parser.parse_args(argv)

    raw_hash = os.environ.get("GYT_LOGIN_HASH", "").strip()
    if not raw_hash:
        # 这条必须是**硬失败**。默默放行等于整个站点对全网敞开,
        # 而表现是「一切正常」—— 那是最坏的一种坏。
        print(
            "[错误] 没设 GYT_LOGIN_HASH。这是登录页要校验的口令哈希,不设等于整个站点不设防。\n"
            "       生成:python scripts/make_login_hash.py",
            file=sys.stderr,
        )
        return 2
    if _parse_hash(raw_hash) is None:
        print(
            "[错误] GYT_LOGIN_HASH 格式不对,应形如 scrypt$16384$8$1$<salt hex>$<hash hex>。\n"
            "       重新生成:python scripts/make_login_hash.py",
            file=sys.stderr,
        )
        return 2

    page = _read_page(args.page)
    throttle = _LoginThrottle(args.login_burst, args.login_attempts_per_minute)

    def build(*a, **kw):
        handler = LoginHandler(*a, **kw)
        return handler

    LoginHandler.password_hash = raw_hash
    LoginHandler.page_html = page
    LoginHandler.throttle = throttle

    try:
        server = ThreadingHTTPServer((BIND_HOST, args.port), build)
    except OSError as exc:
        print(f"[错误] {args.port} 端口起不来:{exc}", file=sys.stderr)
        return 1

    # flush=True 不能省,理由同 serve_artifacts.py:输出不是终端时 stdout 变块缓冲,
    # 而这个进程会一直跑下去 —— 横幅永远出不来,而它正是排查「登录进不去」的第一条线索。
    print(f"[登录服务] http://{BIND_HOST}:{args.port}{LOGIN_PATH}", flush=True)
    print(f"           校验端点:{VERIFY_PATH}(给 Caddy 的 forward_auth 用)", flush=True)
    print(f"           会话有效期:{SESSION_MAX_AGE // 86400} 天", flush=True)
    if _IN_CONTAINER:
        print("           容器内绑 0.0.0.0 —— 只有同一个 compose 网络里的服务能连,", flush=True)
        print("           对外仍然只有 caddy 那一个入口(本服务不许有 ports)。", flush=True)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
