"""``scripts/serve_login.py`` 单元测试 —— 全站鉴权闸的登录服务。

这个文件在本次(2026-08-15 / W8 扫码配对)之前**一条测试都没有**,
而它是**整个站点唯一的一道门**:改错了不是某个功能坏,是所有人都进不去。
`tests/unit/test_auth.py` 测的是 `backend/auth.py`(LangGraph 的令牌鉴权),
和这里完全是两个东西,别弄混。

断言的重点只有两类:

    ① **开放重定向** —— 本次改动引入的唯一安全面。登录成功后要去哪儿(`next`)
       是外部可控的,而它会被写进 `Location:` 响应头。契约里列的每一条坏形状
       都在这里有**单独一条**用例,断言一律退回 `/` —— 不是"大致挡住了",
       是逐条钉死;
    ② **老行为一个字节没变** —— 不带 `next` 时的 302 目标、登录页仍然公开、
       错口令仍然拒绝、限流仍然生效。新功能坏了是配对不上,老行为坏了是全站进不去。

为什么 ``import`` 要绕一圈
==========================
`scripts/serve_login.py` **不在 gyt 包里**,也不在任何 sys.path 上 ——
线上它是被 `python scripts/serve_login.py` 直接当脚本跑的(见 docker-compose.vps.yml),
仓库里既没有 `scripts/__init__.py` 也没打算有。所以这里用
`importlib.util.spec_from_file_location` 按**文件路径**加载,和 langgraph-api
加载 `backend/auth.py` 的路子是同一种。加载的是同一份源码,契约一致。

副作用为零:该模块顶层只有常量和函数定义,起服务的那些事全在
`main()` 里、由 `if __name__ == "__main__"` 守着,import 它不会占端口、
不会读 `GYT_LOGIN_HASH`、不会碰任何环境。

⚠️ 覆盖率说明:`backend/pyproject.toml` 的 addopts 是 `--cov=gyt --cov=eval`,
   本文件的被测物在**仓库根的 scripts/ 下**,既不会被算进覆盖率分子,
   也不会拉低分母 —— 它对那条 80% 门槛完全中性,别指望在覆盖率报告里看到它。

⚠️ **口令与哈希绝不许出现在测试输出里**(哪怕是错的)。
   所以下面凡是要断言"口令对不对"的地方,一律先把结果落到一个变量再断言 ——
   `assert verify_password(哈希, 口令)` 这种写法在失败时会被 pytest 的断言重写
   把两个实参的 repr 原样打进报告。服务端的 `log_message` 也在夹具里按掉了。
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# 加载被测模块
# --------------------------------------------------------------------------

# 本文件是 backend/tests/unit/test_serve_login.py:
#   parents[0]=unit  parents[1]=tests  parents[2]=backend  parents[3]=仓库根
仓库根 = Path(__file__).resolve().parents[3]
登录脚本 = 仓库根 / "scripts" / "serve_login.py"
登录页模板 = 仓库根 / "scripts" / "login-page.html"


def _加载登录模块():
    规格 = importlib.util.spec_from_file_location("gyt_serve_login_under_test", 登录脚本)
    assert 规格 is not None and 规格.loader is not None
    模块 = importlib.util.module_from_spec(规格)
    规格.loader.exec_module(模块)
    return 模块


登录 = _加载登录模块()


# --------------------------------------------------------------------------
# 假口令(**明显是假的**,只在本文件里活着)
# --------------------------------------------------------------------------

假口令 = "gyt-FAKE-test-password-勿用于任何环境"
"""测试专用的假口令。

写得这么长这么怪是故意的:万一哪天有人把它复制到 .env 里,
一眼就能看出这不是真口令。真口令只存在于 VPS 上那一份 .env(见部署实录),
**任何测试、任何仓库文件里都不许出现真口令或它的哈希**。
"""

错口令 = "gyt-FAKE-wrong-password"

_固定盐 = b"gyt-unit-test-salt"
"""固定盐 —— 测试要可复现。它配的是上面那个假口令,泄露它没有任何意义。"""


def _造口令哈希() -> str:
    """按 serve_login 自己声明的 scrypt 参数造一份哈希串。

    参数从被测模块的常量取而不是写死数字:哪天有人调了 SCRYPT_N,
    这里跟着走,测的永远是"当前真实参数"下的那条路径。
    """
    dk = hashlib.scrypt(
        假口令.encode("utf-8"),
        salt=_固定盐,
        n=登录.SCRYPT_N,
        r=登录.SCRYPT_R,
        p=登录.SCRYPT_P,
        dklen=登录.SCRYPT_DKLEN,
    )
    return (
        f"{登录.HASH_PREFIX}{登录.SCRYPT_N}${登录.SCRYPT_R}${登录.SCRYPT_P}"
        f"${_固定盐.hex()}${dk.hex()}"
    )


口令哈希 = _造口令哈希()
"""模块级只算一次 —— scrypt n=2^14 单次约 50-100ms,每个用例算一遍会让这套测试明显变慢。"""


# --------------------------------------------------------------------------
# 起一个真服务 + 一个最小客户端
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class 响应:
    """一次 HTTP 往返的结果。不可变 —— 用例之间不会互相改到同一个对象。"""

    状态: int
    头列表: list[tuple[str, str]]
    正文: bytes

    def 头(self, 名: str) -> str:
        """取一个响应头(大小写不敏感);没有就返回空串。"""
        for 键, 值 in self.头列表:
            if 键.lower() == 名.lower():
                return 值
        return ""

    def 有头(self, 名: str) -> bool:
        return any(键.lower() == 名.lower() for 键, _ in self.头列表)


def _请求(
    端口: int,
    方法: str,
    路径: str,
    *,
    头: dict[str, str] | None = None,
    表单: dict[str, str] | None = None,
) -> 响应:
    """打一次请求。**不跟随重定向** —— 这套测试断的就是 302 本身去哪儿。"""
    实际头 = dict(头 or {})
    正文: bytes | None = None
    if 表单 is not None:
        # urlencode 会把非 ASCII 按 UTF-8 百分号编码,与真浏览器提交表单一致
        正文 = urllib.parse.urlencode(表单).encode("ascii")
        实际头["Content-Type"] = "application/x-www-form-urlencoded"
    连接 = http.client.HTTPConnection("127.0.0.1", 端口, timeout=5)
    try:
        连接.request(方法, 路径, body=正文, headers=实际头)
        原始 = 连接.getresponse()
        return 响应(原始.status, list(原始.getheaders()), 原始.read())
    finally:
        连接.close()


@pytest.fixture
def 服务(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """在回环上随机端口起一个真的登录服务,产出端口号。

    几件刻意的事:
      · 端口给 0 让内核分配 —— 写死端口会在并行跑测试时互相撞车;
      · 只绑 127.0.0.1,不用模块的 BIND_HOST(那个在容器里是 0.0.0.0);
      · `throttle=None`:限流默认关掉,由专门那条用例自己装一个空桶。
        不关的话前面几条用例把令牌耗光,后面全变 429,而报错看起来像"口令错了";
      · `log_message` 按掉:服务端那行 `[登录] POST /login` 会刷满测试输出。
        它本身不打 query 也不打 body(源码里那条注释就是为这个),按掉只是为了干净;
      · `_FAIL_SLEEP_S` 归零:错口令那条路径真睡 0.25 秒,几条用例就是一秒多。
        睡的目的是抹平计时差,和本文件要断的东西无关。
    """
    monkeypatch.setattr(登录.LoginHandler, "password_hash", 口令哈希)
    monkeypatch.setattr(登录.LoginHandler, "page_html", 登录._read_page(登录页模板))
    monkeypatch.setattr(登录.LoginHandler, "throttle", None)
    monkeypatch.setattr(登录.LoginHandler, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(登录, "_FAIL_SLEEP_S", 0.0)

    服务器 = ThreadingHTTPServer(("127.0.0.1", 0), 登录.LoginHandler)
    服务器.daemon_threads = True
    # poll_interval 必须调小。默认 0.5 秒是 serve_forever 检查"该停了吗"的间隔,
    # 也就是**每个用例拆台都要白等半秒** —— 30 条服务类用例 = 15 秒纯等待,
    # 而 CI 上还要乘以两个 Python 版本。实测 0.5 → 0.02 之后整套从 15 秒掉到 2 秒。
    线程 = threading.Thread(
        target=服务器.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    线程.start()
    try:
        yield 服务器.server_address[1]
    finally:
        服务器.shutdown()
        服务器.server_close()
        线程.join(timeout=5)


def _有效会话Cookie() -> str:
    return f"{登录.COOKIE_NAME}={登录.issue_session(口令哈希, int(time.time()))}"


# ==========================================================================
# 一、safe_next_path —— 白名单本体
#
# 契约第四节那张清单,一条坏形状一条用例。这些是纯函数测试:
# 不起服务、不发请求,跑得比眨眼快,而它们守的是本次改动唯一的安全面。
# ==========================================================================


@pytest.mark.parametrize(
    "目的地",
    [
        pytest.param("/", id="根路径"),
        pytest.param(
            "/?checkin=1&pair=0123456789abcdef0123456789abcdef", id="扫码打卡的真实目的地"
        ),
        pytest.param("/threads/abc?tab=1#top", id="带query与锚点"),
        pytest.param("/%E9%A1%B9%E7%9B%AE/1?q=%E4%B8%AD%E6%96%87", id="中文路径的百分号编码形态"),
        pytest.param("/项目/1?q=中文", id="中文原字符"),
        pytest.param("/" + "a" * 511, id="恰好512字符的边界"),
    ],
)
def test_合法的next原样通过(目的地: str) -> None:
    assert 登录.safe_next_path(目的地) == 目的地


def test_协议相对URL被拒() -> None:
    """`//evil.com`:浏览器会自己补上当前协议 → https://evil.com,**直接跳出站**。

    它长得极像一个本站路径,是开放重定向里最常被漏掉的一种。
    """
    assert 登录.safe_next_path("//evil.com/steal") == ""


def test_反斜杠开头被拒() -> None:
    # `/\evil.com`:IE/Edge 和部分浏览器把 \ 当 / 用,于是它等价于 //evil.com。
    assert 登录.safe_next_path("/\\evil.com") == ""


def test_反斜杠加斜杠开头被拒() -> None:
    # `/\/evil.com`:上一条再绕一层,专门用来骗"只查了 // 开头"的实现。
    assert 登录.safe_next_path("/\\/evil.com") == ""


def test_带CRLF的next被拒() -> None:
    # 响应头注入:这一串会被拼进 `Location:` 那一行,CRLF 之后的东西就成了
    # **新的一行响应头** —— 等于让外人给受害者种一个伪造的会话 Cookie。
    assert 登录.safe_next_path("/foo\r\nSet-Cookie: gyt_sess=forged") == ""


def test_单独的换行也被拒() -> None:
    # 只有 \n 没有 \r 同样能注入(很多客户端按 \n 断行),不许因为"少一半"就放过。
    assert 登录.safe_next_path("/foo\nX-Injected: 1") == ""


def test_尾部换行被拒() -> None:
    """守 `\\A`/`\\Z` 而不是 `^`/`$` 这个决定。

    Python 的 `$` 会额外匹配「字符串末尾那个换行之前」,所以 `/foo\\n` 用
    `^/[^/\\\\].*$` 是**匹配得上的**。这条一旦破了,上面那条 CRLF 防线
    就等于没写 —— 而正则从表面上完全看不出问题。
    """
    assert 登录.safe_next_path("/foo\n") == ""


def test_带NUL字符的next被拒() -> None:
    # NUL 会让某些下游按 C 字符串截断,前后看到的路径就不是同一个了。
    assert 登录.safe_next_path("/foo\x00/../etc") == ""


def test_绝对URL被拒() -> None:
    assert 登录.safe_next_path("https://evil.com/") == ""


def test_中段藏着协议分隔符的next被拒() -> None:
    # `/go?to=https://evil.com`:自己是本站路径,但会被下游再解析一次。
    assert 登录.safe_next_path("/go?to=https://evil.com") == ""


def test_不以斜杠开头的相对路径被拒() -> None:
    # `login?next=…` 这种相对写法在不同页面上解析出的绝对地址是不一样的,不收。
    assert 登录.safe_next_path("evil.com") == ""


def test_双反斜杠开头被拒() -> None:
    assert 登录.safe_next_path("\\\\evil.com\\share") == ""


def test_超长next被拒() -> None:
    # 上限 512。不设上限 = 每次登录都把一大坨字符串写进响应头再发出去。
    assert 登录.safe_next_path("/" + "a" * 512) == ""


def test_空next被拒() -> None:
    assert 登录.safe_next_path("") == ""


def test_白名单不做任何修补() -> None:
    """`//evil.com` 不会被"洗"成 `/evil.com` —— 要么原样通过,要么整条丢掉。

    这是刻意的:一旦开始修补,下一个人就会默认这里出来的东西都是干净的,
    然后在别处少写一次校验。
    """
    结果 = 登录.safe_next_path("//evil.com")
    assert 结果 == ""
    assert 结果 != "/evil.com"


# ==========================================================================
# 二、login_url / encode_location —— 拼装与编码
# ==========================================================================


def test_login_url把目的地全量编码进查询串() -> None:
    # 全量编码(safe="")是必须的:目的地里的 & 和 = 不编的话会把自己
    # 劈成两个查询参数,页面读到的就是半截路径。
    assert 登录.login_url("/?checkin=1&pair=abc") == "/login?next=%2F%3Fcheckin%3D1%26pair%3Dabc"


def test_login_url遇到坏目的地退化成裸登录页() -> None:
    assert 登录.login_url("//evil.com") == 登录.LOGIN_PATH


def test_login_url对根路径不带next() -> None:
    # `/login?next=%2F` 和裸 `/login` 效果一样,不带它可以让老路径的行为一字不变。
    assert 登录.login_url("/") == 登录.LOGIN_PATH


def test_login_url的错误态同时带e和next() -> None:
    assert 登录.login_url("/a?b=1", error=True) == "/login?e=1&next=%2Fa%3Fb%3D1"


def test_encode_location把中文编成能进响应头的样子() -> None:
    """`send_header` 用 latin-1 严格模式编码,中文原字符会 UnicodeEncodeError → 500。

    表现是"口令明明是对的,点下去却断了",而没人会想到是一句响应头编不出来。
    """
    编码后 = 登录.encode_location("/项目?x=1")
    assert 编码后 == "/%E9%A1%B9%E7%9B%AE?x=1"
    编码后.encode("latin-1")  # 编不出去就直接抛,等于把那个 500 钉在这里


def test_encode_location不二次编码已有的百分号() -> None:
    # 二次编码同样不报错,表现是登录后落到一个 404 的怪路径上。
    assert 登录.encode_location("/%E9%A1%B9?x=1#top") == "/%E9%A1%B9?x=1#top"


# ==========================================================================
# 三、/_auth/verify —— Caddy 的 forward_auth 打这里(丢目的地的第一处)
# ==========================================================================


def test_有效会话时verify放行(服务: int) -> None:
    结果 = _请求(服务, "GET", 登录.VERIFY_PATH, 头={"Cookie": _有效会话Cookie()})
    assert 结果.状态 == 204


def test_未登录时verify把目的地带进next(服务: int) -> None:
    """扫码打卡这条链的第一跳:目的地必须活过这一步,否则后面全白搭。"""
    结果 = _请求(
        服务,
        "GET",
        登录.VERIFY_PATH,
        头={"X-Forwarded-Uri": "/?checkin=1&pair=abc"},
    )
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/login?next=%2F%3Fcheckin%3D1%26pair%3Dabc"


def test_未登录且目的地就是首页时不带next(服务: int) -> None:
    """老行为不许变:X-Forwarded-Uri 是 `/` 时,还是那个裸 `/login`。"""
    结果 = _请求(服务, "GET", 登录.VERIFY_PATH, 头={"X-Forwarded-Uri": "/"})
    assert 结果.状态 == 302
    assert 结果.头("Location") == 登录.LOGIN_PATH


def test_没有转发头时不拿自己的路径当目的地(服务: int) -> None:
    """走到这里时 self.path 恒为 /_auth/verify —— 那是给 Caddy 用的内部端点。

    拿它当目的地会让人登完之后落在一片空白上,而且没有任何报错。
    """
    结果 = _请求(服务, "GET", 登录.VERIFY_PATH)
    assert 结果.状态 == 302
    assert 结果.头("Location") == 登录.LOGIN_PATH
    assert "next" not in 结果.头("Location")


def test_伪造的转发头也过白名单(服务: int) -> None:
    """X-Forwarded-Uri 同样是外部可控的 —— 直连这个端点就能自己写一个。

    线上 Caddy 会覆盖它,但本服务不许把"上游一定干净"当前提。
    """
    结果 = _请求(服务, "GET", 登录.VERIFY_PATH, 头={"X-Forwarded-Uri": "//evil.com/"})
    assert 结果.状态 == 302
    assert 结果.头("Location") == 登录.LOGIN_PATH


def test_api请求仍然回401JSON而不是302(服务: int) -> None:
    """老行为不许变:前端的 fetch 拿到一坨 HTML 会报一个和"登录过期"毫无关系的错。"""
    结果 = _请求(服务, "GET", 登录.VERIFY_PATH, 头={"X-Forwarded-Uri": "/api/checkin"})
    assert 结果.状态 == 401
    assert 结果.有头("Location") is False
    assert "application/json" in 结果.头("Content-Type")


# ==========================================================================
# 四、GET /login —— 登录页(丢目的地的第二处在页面里)
# ==========================================================================


def test_登录页不需要会话就能打开(服务: int) -> None:
    """这一条破了就是死循环:登录页自己也要先登录,谁都进不来。"""
    结果 = _请求(服务, "GET", 登录.LOGIN_PATH)
    assert 结果.状态 == 200
    assert "text/html" in 结果.头("Content-Type")


def test_登录页带着next的hidden字段(服务: int) -> None:
    """页面少了这个字段 = 目的地在提交那一跳丢掉,而且**没有任何报错**。

    serve_login 的 _read_page 会在启动时硬拦这一条(见下面那条用例),
    这里再从真实响应上确认一次:发出去的那份 HTML 里它确实在。
    """
    结果 = _请求(服务, "GET", 登录.LOGIN_PATH)
    页面 = 结果.正文.decode("utf-8")
    assert f'name="{登录.NEXT_PARAM}"' in 页面
    assert 'type="hidden"' in 页面


def test_已登录访问登录页会去next指定的地方(服务: int) -> None:
    结果 = _请求(
        服务,
        "GET",
        f"{登录.LOGIN_PATH}?next=%2F%3Fcheckin%3D1",
        头={"Cookie": _有效会话Cookie()},
    )
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/?checkin=1"


def test_已登录访问登录页且没有next时回首页(服务: int) -> None:
    结果 = _请求(服务, "GET", 登录.LOGIN_PATH, 头={"Cookie": _有效会话Cookie()})
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/"


def test_已登录访问登录页时坏next也退回首页(服务: int) -> None:
    结果 = _请求(
        服务,
        "GET",
        f"{登录.LOGIN_PATH}?next=%2F%2Fevil.com",
        头={"Cookie": _有效会话Cookie()},
    )
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/"


# ==========================================================================
# 五、POST /login —— 提交(丢目的地的第三处)
# ==========================================================================


def test_正确口令加合法next跳到那个地址(服务: int) -> None:
    目的地 = "/?checkin=1&pair=0123456789abcdef0123456789abcdef"
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令, "next": 目的地})
    assert 结果.状态 == 302
    assert 结果.头("Location") == 目的地
    assert 登录.COOKIE_NAME in 结果.头("Set-Cookie")


def test_正确口令但没有next字段时回首页(服务: int) -> None:
    """老行为不许变:不带 next 的登录还是回 `/`。"""
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令})
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/"


def test_正确口令但next为空时回首页(服务: int) -> None:
    """页面上那个 hidden 字段在没有目的地时提交的就是空串,这是最常走的一条路。"""
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令, "next": ""})
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/"


@pytest.mark.parametrize(
    "坏目的地",
    [
        pytest.param("//evil.com/steal", id="协议相对URL"),
        pytest.param("/\\evil.com", id="反斜杠开头"),
        pytest.param("/\\/evil.com", id="反斜杠加斜杠"),
        pytest.param("https://evil.com/", id="绝对URL"),
        pytest.param("/go?to=https://evil.com", id="中段藏协议分隔符"),
        pytest.param("evil.com", id="相对路径"),
        pytest.param("/" + "b" * 512, id="超长"),
    ],
)
def test_正确口令但next不合法一律退回首页(服务: int, 坏目的地: str) -> None:
    """登录本身是成功的(Set-Cookie 照发),只是不认那个目的地。

    退回 `/` 而不是报错 —— 没有任何理由因为一个坏 next 就把人挡在门外。
    """
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令, "next": 坏目的地})
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/"
    assert 登录.COOKIE_NAME in 结果.头("Set-Cookie")


def test_带CRLF的next既不生效也不注入响应头(服务: int) -> None:
    """浏览器发过来的是 `%0D%0A`,parse_qs 解出来就是真的 CRLF。

    只要它被拼进 `Location:` 那一行,后面的内容就成了新的一行响应头 ——
    这里同时断两件事:目的地退回 `/`,且那个伪造的头一个都没出现。
    """
    结果 = _请求(
        服务,
        "POST",
        登录.LOGIN_PATH,
        表单={"pw": 假口令, "next": "/foo\r\nX-Injected: 1\r\nSet-Cookie: gyt_sess=forged"},
    )
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/"
    assert 结果.有头("X-Injected") is False
    assert "forged" not in 结果.头("Set-Cookie")


def test_中文目的地能原样回到(服务: int) -> None:
    """浏览器发出的请求行里中文本来就是百分号编码的,所以它一路都是 ASCII。

    这条同时守住"不许二次编码"—— 编成 %25E9 的话落地就是个 404 的怪路径。
    """
    目的地 = "/%E9%A1%B9%E7%9B%AE/1?q=%E4%B8%AD%E6%96%87"
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令, "next": 目的地})
    assert 结果.状态 == 302
    assert 结果.头("Location") == 目的地


def test_中文原字符的目的地不会把响应头编崩(服务: int) -> None:
    """有人手工构造一个带中文原字符的 POST 时,不许 500。

    响应头是 latin-1 严格模式编的 —— 不做 encode_location 这里就是
    UnicodeEncodeError,连接直接断,而口令其实是对的。
    """
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令, "next": "/项目?q=中文"})
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/%E9%A1%B9%E7%9B%AE?q=%E4%B8%AD%E6%96%87"


def test_错口令仍然被拒且不发会话(服务: int) -> None:
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 错口令})
    assert 结果.状态 == 302
    assert 结果.头("Location").startswith(f"{登录.LOGIN_PATH}?e=1")
    assert 结果.有头("Set-Cookie") is False


def test_错口令时目的地不丢(服务: int) -> None:
    """20 位随机口令在手机上打错一次太常见了。

    一错就把扫码带来的目的地弄丢的话,人就再也回不到打卡面板 ——
    而他完全不知道自己少了什么。
    """
    结果 = _请求(
        服务,
        "POST",
        登录.LOGIN_PATH,
        表单={"pw": 错口令, "next": "/?checkin=1&pair=abc"},
    )
    assert 结果.状态 == 302
    assert 结果.头("Location") == "/login?e=1&next=%2F%3Fcheckin%3D1%26pair%3Dabc"


def test_错口令时坏目的地也不会被带回去(服务: int) -> None:
    """失败那一跳同样要过白名单 —— 否则 `/login?e=1&next=//evil.com` 就成了

    一条现成的鱼饵:页面把它填进 hidden 字段,人再输一次对的口令就跳出站了。
    """
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 错口令, "next": "//evil.com"})
    assert 结果.状态 == 302
    assert 结果.头("Location") == f"{登录.LOGIN_PATH}?e=1"


def test_空口令直接被拒(服务: int) -> None:
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": ""})
    assert 结果.状态 == 302
    assert 结果.有头("Set-Cookie") is False


def test_限流仍然拦得住(服务: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Caddy 官方镜像不带限流模块,这道闸是本服务自己的。

    装一个容量为 0 的桶:第一次 take() 就拿不到令牌。
    用空桶而不是"连打 N 次"是为了不真跑 N 次 scrypt —— 那是秒级的开销,
    而且次数一多这条用例就会开始飘。
    """
    monkeypatch.setattr(登录.LoginHandler, "throttle", 登录._LoginThrottle(0, 0.0))
    结果 = _请求(服务, "POST", 登录.LOGIN_PATH, 表单={"pw": 假口令})
    assert 结果.状态 == 429
    assert 结果.头("Retry-After") == "60"
    assert 结果.有头("Set-Cookie") is False


# ==========================================================================
# 六、启动期的同源闸
# ==========================================================================


def test_真实登录页模板过得了启动校验() -> None:
    """仓库里那份 login-page.html 现在就必须是合格的,否则线上根本起不来。"""
    页面 = 登录._read_page(登录页模板)
    assert b'name="pw"' in 页面
    assert f'name="{登录.NEXT_PARAM}"'.encode() in 页面


def test_模板缺了next字段就拒绝启动(tmp_path: Path) -> None:
    """少了它不会有任何报错:登录照样成功,只是每次都丢回首页。

    电脑那头永远停在 waiting,两边都不打印一个字 —— 宁可在启动时用一句人话拦下来。
    """
    残缺模板 = tmp_path / "login-page.html"
    残缺模板.write_text(
        '<form action="/login" method="post"><input name="pw"></form>',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as 错误:
        登录._read_page(残缺模板)
    assert 登录.NEXT_PARAM in str(错误.value)


def test_模板缺了口令框也拒绝启动(tmp_path: Path) -> None:
    残缺模板 = tmp_path / "login-page.html"
    残缺模板.write_text(
        '<form action="/login" method="post"><input name="next" type="hidden"></form>',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        登录._read_page(残缺模板)
