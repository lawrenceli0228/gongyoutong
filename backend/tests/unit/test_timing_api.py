"""timing_api.py(耗时观测直连接口)的单元测试 —— 不联网、不起图、不碰库。

被测物只有一条**只读**端点 ``GET /timing``,契约的唯一真相在 ``timing_api.py`` 的
模块头注。数据从 ``core/timing`` 的进程内缓冲来:``emit_timing`` 写、
``recent_timings`` 读、``reset_timings`` 清。

这层要钉死的静默错误(和别处一样,全是"不报错但结果错"那一类):

  · **令牌漏配时裸奔** —— ``langgraph.json`` 的 ``enable_custom_route_auth`` 漏了
    没有任何报错(第一道鉴权整条消失、站点照开),只有 handler 自查这一道兜得住,
    也只有它能被单测钉死。耗时记录里有会话号、有节点名、有 token 用量 ——
    那是登录之后才该看到的东西,不是公开数据。
  · **令牌读成常量** —— 表现是「换了口令要重启才生效」,而线上换口令的人不会想到
    这一层;测试里的形态就是"同一个进程里换环境变量之后行为不跟着变"。
  · **``next_since`` 退成 0** —— 没有新记录时游标必须**原样退回**。退成 0 的话
    前端下一轮把整段重拉,界面上耗时行成倍重复,**而两边都不报错**。
  · **``since`` 解析不出来时静默当 0** —— 同上,整段重拉、零报错。所以宁可 400。
  · **会话串味** —— 取回别人会话的耗时,界面上多出几行来路不明的记录。
  · **记录少一个键** —— 十个键是恒定存在的契约(``core/timing.RECORD_KEYS``),
    前端按它解构;少一个键的表现是界面上一行都不出、控制台干净。
  · **500 时裸拼 dict** —— 响应体形状一变,前端那段共用的解信封代码就多一个特例。
  · **人话里冒出开发者词汇** —— 400 这两条**是前端的编程错误,工友一辈子碰不到**,
    但真漏到界面上时「参数错误」这种话对工地师傅毫无意义,反而让他以为自己按错了。

⚠️ ``emit_timing`` 在测试里**必须显式传 ``thread_id``**:不传的话它会去问 LangGraph
   运行时,而单测不在图内,那条记录会被安静丢掉(那是正常路径,不是故障)——
   于是"我明明记了两条"其实一条都没进缓冲,而断言看起来还是绿的。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final
from urllib.parse import urlencode

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gyt import timing_api
from gyt.config import get_settings
from gyt.core.timing import KIND_LLM, KIND_TOOL, RECORD_KEYS, emit_timing, reset_timings

THREAD: Final[str] = "会话-甲"
OTHER_THREAD: Final[str] = "会话-乙"
"""两个会话号。刻意带中文:``thread_id`` 是 chat-ui 那边给的字符串,
不保证是 ASCII,而它要经过 URL 查询串往返一趟。"""

REAL_TOKEN: Final[str] = "gyt-timing-token-0123456789abcdef"
OTHER_TOKEN: Final[str] = "gyt-timing-token-fedcba9876543210"
"""两个"像真的"令牌:≥24 位、不是占位符开头 —— 两条都满足才会真正开启鉴权
(判据在 core/access.py,与 auth.py / checkin_api / supervision_api 同源)。
要两个是因为「换了口令当场生效」那条用例得把旧的换掉。"""

SHORT_TOKEN: Final[str] = "gyt-123"
PLACEHOLDER_TOKEN: Final[str] = "替换成openssl_rand_hex_32的输出"
"""两种"设了等于没设"的写法。占位符那条尤其要盯:``.env.vps.example`` 里那行
是非空中文串,compose 的 fail-closed 只认空 —— 当成真令牌的话第二道锁归零,
而全流程零信号。"""

ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"ok", "data", "user_msg", "error_code"})
"""Envelope 的四个键。**每个出口都要比一遍**:裸拼一个 dict 不会报错,
只会让前端那段共用的解信封代码多一个特例。"""

开发者词汇: Final[tuple[str, ...]] = (
    "参数",
    "thread_id",
    "since",
    "query",
    "整数",
    "字段",
    "非空",
)
"""面向用户的字符串里**一个都不许出现**的词。

本仓的硬规矩:``user_msg`` 是给工地师傅看的人话,内部原因只走 ``detail=``
(而 ``fail()`` 保证 detail 只进日志、不进返回值)。
"""


@pytest.fixture(autouse=True)
def _干净的耗时缓冲() -> Iterator[None]:
    """每个用例前后清空缓冲。

    缓冲是 ``core/timing`` 的**模块级全局**(连 ``seq`` 计数器一起),conftest 那套
    Settings 隔离管不到它 —— 不清的话上一条用例记的记录会漏给下一条,表现是
    "我只记了一条,却取回来三条",而人会先去查增量拉取的判据。
    """
    reset_timings()
    yield
    reset_timings()


@pytest.fixture
def client() -> TestClient:
    """真 Starlette 栈,只铺本模块导出的那条路由。

    照 ``supervision_api.app`` 的先例现搭一个壳:线上走 webapp.py(挂载另有用例盯着),
    这里不把整个 webapp 拖进来,免得一条只读端点的用例被 ezdxf / langchain 的导入拖慢。
    """
    return TestClient(Starlette(routes=list(timing_api.TIMING_ROUTES)))


def _记一条(*, thread_id: str = THREAD, name: str = "kimi-k3", **覆盖: Any) -> None:
    """往缓冲里记一条像样的耗时记录,单项可覆盖。

    ``thread_id`` **显式传**,理由见模块头注:不传就等于没记。
    """
    默认: dict[str, Any] = {
        "kind": KIND_LLM,
        "name": name,
        "seconds": 1.5,
        "ok": True,
        "slow": False,
        "agent": "safety",
        "thread_id": thread_id,
        "input_tokens": 120,
        "output_tokens": 34,
        "reasoning_tokens": None,
    }
    emit_timing(**{**默认, **覆盖})


def _拉取(
    client: TestClient,
    *,
    thread_id: str | None = THREAD,
    since: str | None = None,
    api_key: str | None = None,
) -> httpx.Response:
    """发一次取数请求。

    ``None`` = **不发这个查询参数**(与"发了一个空串"是两件事,而这两件事在
    ``since`` 上恰好同义、在 ``thread_id`` 上恰好不同义 —— 所以两者都得测得到)。
    """
    query: dict[str, str] = {}
    if thread_id is not None:
        query["thread_id"] = thread_id
    if since is not None:
        query["since"] = since
    headers = {"X-Api-Key": api_key} if api_key is not None else {}
    return client.get(f"/timing?{urlencode(query)}", headers=headers)


def _设令牌(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """把访问令牌换成 ``token`` 并让 Settings 立刻重读(``get_settings`` 带 lru_cache)。

    漏掉 ``cache_clear()`` 的表现是这条用例拿到上一条的 Settings —— 鉴权用例会
    整组绿得莫名其妙。
    """
    monkeypatch.setenv("GYT_ACCESS_TOKEN", token)
    get_settings.cache_clear()


def _records(resp: httpx.Response) -> list[dict[str, Any]]:
    return resp.json()["data"]["records"]


# ---------------------------------------------------------------------------
# 鉴权(纵深防御第二道):判定与 auth.py / checkin_api / supervision_api 同源
# ---------------------------------------------------------------------------


class Test令牌自查:
    """上线闸①的耗时链版本。

    第一道在 langgraph 的鉴权中间件(``langgraph.json`` 的
    ``enable_custom_route_auth``),那个键漏配时**整条消失且没有任何报错** ——
    只有这道自查兜得住,也只有它能被单元测试钉死。
    """

    def test_设了令牌但没带钥匙一律拒(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET 最容易被当成"只是查一下"而漏掉鉴权 —— 而耗时记录里有会话号、
        有节点名、有 token 用量,是登录之后才该看到的东西。"""
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)

        # Act
        resp = _拉取(client)

        # Assert
        assert resp.status_code == 401
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS, "被拒的响应同样得是信封,不许裸拼 dict"
        assert body["ok"] is False
        assert body["data"] is None
        assert body["error_code"] == "UNAUTHORIZED"

    def test_带错令牌也拒(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """只判"有没有带钥匙"是不够的:带一把错的照样非空,而这条端点的全部
        意义就是别让不该看的人看到。"""
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)

        # Act
        resp = _拉取(client, api_key=OTHER_TOKEN)

        # Assert
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"

    def test_带对令牌就放行到业务层(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """放行的证据是它开始回业务结果(空缓冲也是 ok),而不是 401。"""
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)

        # Act
        resp = _拉取(client, api_key=REAL_TOKEN)

        # Assert
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_没配令牌时不拦(self, client: TestClient) -> None:
        """conftest 已把 GYT_* 清干净,这就是"没配"的真实状态。

        ``make dev`` 与真机验收都不发令牌头,「未配置也拦」等于当场打死本机联调 ——
        这条取舍与 auth.py / checkin_api / supervision_api 一字不差。
        """
        assert _拉取(client).status_code == 200

    @pytest.mark.parametrize(
        ("说法", "token"),
        [("占位符", PLACEHOLDER_TOKEN), ("过短", SHORT_TOKEN)],
    )
    def test_设了等于没设的两种写法一律放行(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, 说法: str, token: str
    ) -> None:
        """判定必须与 auth.py 完全同源。两边分头判的话会出现"对话链开门、耗时锁死"
        这种半开状态 —— 最难查的一种,因为每一半单看都是对的。"""
        # Arrange
        _设令牌(monkeypatch, token)

        # Act & Assert
        assert _拉取(client).status_code == 200, f"{说法}令牌应当被视同没配"

    def test_令牌是每次请求现取_换了当场生效(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 ``effective_access_token()`` 不许在 import 时读成常量。

        读成常量的表现有两层,都很难查:测试里换环境变量测不到(于是整组鉴权用例
        变成假绿灯),线上换口令要重启才生效(而换口令的人不会想到这一层)。

        本用例在**同一个进程、同一个 client** 上把口令换掉,然后拿旧钥匙去敲 ——
        旧钥匙必须当场作废。
        """
        # Arrange:先立一把锁,确认它是真的在拦
        _设令牌(monkeypatch, REAL_TOKEN)
        没带钥匙 = _拉取(client)

        # Act:换锁,再拿新旧两把钥匙各敲一次
        _设令牌(monkeypatch, OTHER_TOKEN)
        旧钥匙 = _拉取(client, api_key=REAL_TOKEN)
        新钥匙 = _拉取(client, api_key=OTHER_TOKEN)

        # Assert
        assert 没带钥匙.status_code == 401
        assert 旧钥匙.status_code == 401, "换了口令之后旧令牌还能进 = 令牌被读成了常量"
        assert 新钥匙.status_code == 200

    def test_令牌自查排在取参数之前(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一个连 ``thread_id`` 都没带的请求,在令牌不过时必须回 401 而不是 400。

        顺序本身是规矩(与另外两个直连接口同一条):先回 400 等于告诉没钥匙的人
        "你的请求格式不对" —— 那是一句不该说给他听的话。
        """
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)

        # Act
        resp = _拉取(client, thread_id=None)

        # Assert
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 查询参数:该 400 的绝不静默兜住
# ---------------------------------------------------------------------------


class Test查询参数:
    def test_缺thread_id回400(self, client: TestClient) -> None:
        """没有会话号就无从取数。**不许退化成"给全部会话的记录"** ——
        那等于把别人的耗时端到这个人面前。"""
        # Act
        resp = _拉取(client, thread_id=None)

        # Assert
        assert resp.status_code == 400
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["error_code"] == "INVALID_INPUT"
        assert body["data"] is None

    @pytest.mark.parametrize("空白", ["", " ", "\t", "   "])
    def test_thread_id只有空白也回400(self, client: TestClient, 空白: str) -> None:
        """判空发生在 ``.strip()`` 之后。只判 ``is None`` 的话,前端在 threadId
        还没生成时发出来的 ``?thread_id=`` 会一路走到取数,回一个空清单 ——
        看起来"这轮没有耗时",而真相是问错了会话。"""
        assert _拉取(client, thread_id=空白).status_code == 400

    def test_不给since当0(self, client: TestClient) -> None:
        """第一次拉取本来就没有游标 —— 不给**不是错误**,是常态。"""
        # Arrange
        _记一条()

        # Act
        resp = _拉取(client, since=None)

        # Assert
        assert resp.status_code == 200
        assert len(_records(resp)) == 1

    def test_since是空串也当0(self, client: TestClient) -> None:
        """``URLSearchParams`` 拼出来的 ``?since=`` 是完全正常的写法(游标还没拿到时
        就长这样),把它判成非法等于第一轮永远拉不到东西。"""
        # Arrange
        _记一条()

        # Act
        resp = _拉取(client, since="")

        # Assert
        assert resp.status_code == 200
        assert len(_records(resp)) == 1

    @pytest.mark.parametrize("坏游标", ["abc", "1.5", "一", "0x10"])
    def test_since解析不出来就400而不是静默当0(self, client: TestClient, 坏游标: str) -> None:
        """🔴 静默当 0 会把整段重拉,界面上耗时行凭空翻倍,**而没有任何一侧报错**。

        ``1.5`` 不是凑数的:前端 ``String(cursor)`` 遇上一个浮点游标就长这样。
        """
        # Arrange
        _记一条()

        # Act
        resp = _拉取(client, since=坏游标)

        # Assert
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_INPUT"

    def test_since是负数也拒而不是当0(self, client: TestClient) -> None:
        """``since=-1`` 能跑通,但它表达的是"比不存在的记录还早" —— 那是调用方
        算错了游标的信号。放过去(当 0)只会把错误藏到下一层:整段被重拉,
        而算错游标那件事从此没人知道。"""
        # Arrange
        _记一条()

        # Act
        resp = _拉取(client, since="-1")

        # Assert
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_INPUT"
        assert "records" not in resp.text, "被拒的请求不许顺手把整段带回去"


# ---------------------------------------------------------------------------
# 取数:增量、有序、按会话隔离
# ---------------------------------------------------------------------------


class Test取数:
    def test_两条记录按seq升序取回(self, client: TestClient) -> None:
        """顺序是契约的一部分:前端直接 append,不自己排。乱序 = 界面上的步骤
        顺序和实际发生的顺序对不上,而那正是这几行字唯一的用处。"""
        # Arrange
        _记一条(name="kimi-k3")
        _记一条(name="deepseek-v4-flash")

        # Act
        records = _records(_拉取(client))

        # Assert
        assert [r["name"] for r in records] == ["kimi-k3", "deepseek-v4-flash"]
        seqs = [r["seq"] for r in records]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq 必须严格递增"

    def test_带上next_since再拉是空且游标原样退回(self, client: TestClient) -> None:
        """🔴 没有新记录时 ``next_since`` **原样退回传入的 since**,不是 0。

        退成 0 的话前端下一轮把整段重拉,界面上表现为耗时行成倍重复,
        而后端前端都不报错 —— 只有这条断言分得开。
        """
        # Arrange
        _记一条()
        _记一条(name="deepseek-v4-flash")
        第一轮 = _拉取(client).json()["data"]
        游标 = 第一轮["next_since"]

        # Act
        第二轮 = _拉取(client, since=str(游标)).json()["data"]

        # Assert
        assert 游标 == 第一轮["records"][-1]["seq"], "游标就是最后一条的 seq"
        assert 第二轮["records"] == []
        assert 第二轮["next_since"] == 游标

    def test_只回游标之后新增的那几条(self, client: TestClient) -> None:
        """增量拉取的正路。少给了没人发现(界面上少一行),多给了就是重复行。"""
        # Arrange
        _记一条(name="kimi-k3")
        游标 = _拉取(client).json()["data"]["next_since"]
        _记一条(name="analyze_site_photo", kind=KIND_TOOL, agent=None)

        # Act
        records = _records(_拉取(client, since=str(游标)))

        # Assert
        assert [r["name"] for r in records] == ["analyze_site_photo"]

    def test_取不到别的会话的记录(self, client: TestClient) -> None:
        """串味的表现是界面上多出几行来路不明的耗时(还带着别人的节点名),
        而不会有任何报错。"""
        # Arrange
        _记一条(thread_id=THREAD, name="我这条")
        _记一条(thread_id=OTHER_THREAD, name="别人那条")

        # Act
        records = _records(_拉取(client, thread_id=THREAD))

        # Assert
        assert [r["name"] for r in records] == ["我这条"]

    def test_没见过的会话回空清单而不是报错(self, client: TestClient) -> None:
        """新开一个会话、还没发生任何调用,是**最常见**的一次请求(界面一挂上就问)。
        判成 404 的话前端每开一个会话都要先吞一个错误。"""
        # Arrange
        _记一条(thread_id=OTHER_THREAD)

        # Act
        resp = _拉取(client, thread_id="会话-从没出现过", since="7")

        # Assert
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["records"] == []
        assert data["next_since"] == 7, "空会话也要原样退回游标,理由同上一条"

    def test_成功响应就是四键信封(self, client: TestClient) -> None:
        """前端那段解信封的代码是三个直连接口共用的,这里多一个特例就得多判一次。"""
        # Arrange
        _记一条()

        # Act
        body = _拉取(client).json()

        # Assert
        assert set(body) == ENVELOPE_KEYS
        assert body["ok"] is True
        assert body["error_code"] is None
        assert body["user_msg"] == ""
        assert set(body["data"]) == {"records", "next_since"}

    def test_记录的键与RECORD_KEYS逐键一致(self, client: TestClient) -> None:
        """契约的唯一真相是 ``core/timing.RECORD_KEYS`` —— **拿那个元组来比**,
        别在这儿手抄一份十个键的清单(抄了就是第二份真相,改名时两边各绿各的)。

        ⚠️ 比的是**集合不是顺序**:``seq`` 由缓冲在最后补上,而 ``RECORD_KEYS``
        把它写在第一个;JSON 对象本来也无序,前端按键名取。
        """
        # Arrange
        _记一条()

        # Act
        记录 = _records(_拉取(client))[0]

        # Assert
        assert set(记录) == set(RECORD_KEYS)

    def test_十个键恒定存在_没有值的给null而不是缺席(self, client: TestClient) -> None:
        """``kind=tool`` 时 ``agent`` 与三个 token 字段都没有值。

        **要的是 null,不是把键去掉**:形状固定前端才能安心解构,少一个键就得每处
        判一次 undefined —— 而漏判的表现是界面上一行都不出、控制台干净。
        ⚠️ 也**不许退成 0**:界面上 0 会被读成"这次没花 token",而真相是"不知道"。
        """
        # Arrange
        _记一条(
            kind=KIND_TOOL,
            name="analyze_site_photo",
            agent=None,
            input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )

        # Act
        记录 = _records(_拉取(client))[0]

        # Assert
        assert set(记录) == set(RECORD_KEYS), "键一个都不许少"
        for 空着的键 in ("agent", "input_tokens", "output_tokens", "reasoning_tokens"):
            assert 记录[空着的键] is None, f"{空着的键} 取不到时必须是 null"

    def test_失败与慢这两个字段原样带出来(self, client: TestClient) -> None:
        """「这步失败了、还花了 213 秒」和「这步很慢」是两回事,前端按 ``ok`` /
        ``slow`` 分流 —— 中间这一层把它们弄丢的话,最该看见的那一类恰好看不见。"""
        # Arrange
        _记一条(seconds=213.4, ok=False, slow=True)

        # Act
        记录 = _records(_拉取(client))[0]

        # Assert
        assert 记录["ok"] is False
        assert 记录["slow"] is True
        assert 记录["seconds"] == 213.4


# ---------------------------------------------------------------------------
# 兜底与人话
# ---------------------------------------------------------------------------


class Test兜底:
    def test_取数炸了回500且仍是信封(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """观测接口炸了绝不能连累别的,更不能把堆栈端到工友面前。

        打桩打在 ``timing_api.recent_timings`` 这个**模块属性**上 —— handler 是在
        ``run_in_threadpool`` 里按模块全局解析它的,打在别处进不去
        (进不去的表现不是红,是这条用例拿到 200 然后"顺利"绿掉)。
        """

        # Arrange
        def 炸(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], int]:
            raise RuntimeError("耗时缓冲挂了")

        monkeypatch.setattr(timing_api, "recent_timings", 炸)

        # Act
        resp = _拉取(client)

        # Assert
        assert resp.status_code == 500
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["ok"] is False
        assert body["data"] is None
        assert body["error_code"] == "INTERNAL"
        assert "RuntimeError" not in resp.text
        assert "耗时缓冲挂了" not in resp.text, "异常原文只进日志"

    def test_只有一条只读路由_POST打不进来(self, client: TestClient) -> None:
        """「这是只读接口,没有任何写入口」是头注里的明文承诺:耗时记录只由
        ``emit_timing`` 在进程内产生,外面往里塞的话,界面上那几行就不再是
        "实际发生了什么"。多挂一个方法不会有任何报错,所以拿 405 钉住它。"""
        # Assert
        assert [route.path for route in timing_api.TIMING_ROUTES] == ["/timing"]
        assert client.post("/timing", json={"thread_id": THREAD}).status_code == 405


class Test人话:
    """400 这两条**是前端的编程错误,工友一辈子碰不到** —— 但真漏到界面上时,
    「参数错误」那种话对工地师傅毫无意义,反而会让他以为自己按错了。"""

    @pytest.mark.parametrize(
        ("说法", "thread_id", "since"),
        [
            ("缺会话号", None, None),
            ("游标写坏了", THREAD, "abc"),
        ],
    )
    def test_400的user_msg是人话不是开发者词汇(
        self, client: TestClient, 说法: str, thread_id: str | None, since: str | None
    ) -> None:
        # Act
        resp = _拉取(client, thread_id=thread_id, since=since)

        # Assert
        assert resp.status_code == 400
        user_msg = resp.json()["user_msg"]
        assert user_msg, f"{说法}:总得说句话,空着等于界面上什么都不显示"
        for 禁词 in 开发者词汇:
            assert 禁词 not in user_msg, f"{说法}:人话里冒出了「{禁词}」"

    def test_内部原因只走detail不进返回值(self, client: TestClient) -> None:
        """``_parse_since`` 把那个解析不出来的原值拼进了 ``detail=`` —— 而
        ``fail()`` 的契约是 detail 只进日志。原值回显到界面上是两件坏事:
        对工友没有意义,对拿它当反射面的人则是一条免费的回显通道。"""
        # Arrange:一个一眼认得出、绝不会出现在中文人话里的原值
        坏游标 = "zzz-echo-probe"

        # Act
        resp = _拉取(client, since=坏游标)

        # Assert
        assert resp.status_code == 400
        assert 坏游标 not in resp.text


# ---------------------------------------------------------------------------
# 挂载:这条路由真的铺进 webapp.py 了吗
# ---------------------------------------------------------------------------


def test_这条路由挂进了webapp() -> None:
    """webapp.py 是自定义路由唯一的挂载点,漏铺 = 404 且**没有任何启动报错**。

    这条比另外两组(打卡、监理)更值得钉:那两组漏铺时用户会看见「打不开」
    「面板是空的」,而耗时行漏铺时**前端是故意安静走开的**(观测件不许打扰工友)——
    界面上一行不出、控制台干净、后端日志干净。没有任何东西会说话。

    ⚠️ 与 ``test_supervision_api`` 那条同款局限,别把它当全部保障:它在宿主的
    Python 进程里直接 import,验的只是「代码里铺上了」。容器里还要求 webapp.py
    这个文件真的在镜像/挂载里、``langgraph.json`` 真的有 http 块 ——
    W10 那次两样都缺,端点全 404 而这类用例照样全绿。
    """
    # ⚠️ `import webapp` 而不是 `from gyt import webapp`:它住在 backend/ 根下、
    #    **不在 gyt 包里**(langgraph.json 的 http.app 按文件路径指向它)。
    #    照 test_supervision_api.py 的同一条 import。
    import webapp

    mounted = {route.path for route in webapp.app.routes}
    declared = {route.path for route in timing_api.TIMING_ROUTES}

    assert declared <= mounted, f"这些路由没挂进 webapp.py:{sorted(declared - mounted)}"
    assert declared == {"/timing"}, "加了路由要连同这个集合一起改 —— 它是「有没有漏铺」的对账锚"
