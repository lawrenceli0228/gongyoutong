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
        uri = self.headers.get("X-Forwarded-Uri", "") or self.path
        if uri.startswith("/api/"):
            self._send_bytes(
                HTTPStatus.UNAUTHORIZED,
                JSON_UNAUTHORIZED.encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        self._redirect(LOGIN_PATH)

    def _handle_login_page(self) -> None:
        if check_session(self.password_hash, self._cookie_token(), int(time.time())):
            self._redirect("/")
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

        if pw and verify_password(self.password_hash, pw):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
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
        self._redirect(f"{LOGIN_PATH}?e=1")

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
    html = path.read_text(encoding="utf-8")
    # 登录页是静态文件,唯一的动态部分是「口令不对」那行,由 ?e=1 在前端切换。
    # 这里只做一次健壮性检查:模板里必须有表单,否则等于发了一张点不动的页面。
    if 'name="pw"' not in html or f'action="{LOGIN_PATH}"' not in html:
        raise SystemExit(
            f"[错误] 登录页模板不对:{path}\n"
            f"       必须包含 action=\"{LOGIN_PATH}\" 的表单和 name=\"pw\" 的输入框。"
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
