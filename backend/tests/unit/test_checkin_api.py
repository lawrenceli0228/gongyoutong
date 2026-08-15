"""打卡请求契约(checkin_api.py)的纯函数单测 —— 不起服务、不碰库、不联网。

T1 只冻结契约,handler 归 T2。这里锁的是**三类会静默出错的东西**:

  · **Base64URL 编解码的往返** —— 前端 checkin-lib.ts 要做同样的事,
    两边共享下面的 ROUNDTRIP_VECTORS。漂了的表现是姓名变乱码画进水印,
    而水印是图片,**没有任何断言会替你发现**。
  · **指纹的构成** —— 层①与层②必须用同一个函数,只有 photo_hash 来源不同。
    若指纹能被字段内容伪造(「张\\x1f三」+ 空地盤 ≡ 「张」+「三」),
    两次不同的打卡会被判成重发,第二次直接被吞掉。
  · **校验必须先查长度再解码** —— 反过来就是拿校验代码当放大器。

姓名用例刻意含繁体、生僻字与 emoji:方案 D12 说最后要换简繁,
而**姓名是例外中的例外 —— 原样存、原样画,任何情况下不进转换**。
"""

from __future__ import annotations

import importlib.util
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from starlette.testclient import TestClient

from gyt import checkin_api
from gyt.attendance import messages, receipt
from gyt.attendance.watermark import FontUnavailableError, MissingGlyphsError
from gyt.checkin_api import (
    DIGEST_HEX_LEN,
    MAX_NAME_BYTES,
    HeaderValueError,
    build_digest,
    decode_name,
    encode_name,
    photo_sha256,
    validate_digest_hex,
)
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.db import attendance as att_db

ROUNDTRIP_VECTORS: tuple[str, ...] = (
    "张三",
    "陳大文",  # 繁体 —— 香港现场的常态
    "李四-B组",
    "Ada Wong",
    "王𠮶",  # HKSCS 平面 2 的字,WQY 未必有 → T4 的缺字检测要抓它
    "工地A栋 3/F",
    "名字里有空格 和 中点·",
)
"""编解码往返向量。**这份表要与 checkin-lib.test.ts 里那份逐条一致。**

不一致的表现:后端解出来的姓名和工友输入的不是一个东西,
而水印照画、凭证照出、库里照存 —— 一路无报错。
"""


class Test名称编解码:
    @pytest.mark.parametrize("name", ROUNDTRIP_VECTORS)
    def test_往返之后原样不变(self, name: str) -> None:
        assert decode_name(encode_name(name)) == name

    @pytest.mark.parametrize("name", ROUNDTRIP_VECTORS)
    def test_编出来的全是_header_安全字符(self, name: str) -> None:
        """header 值必须 ASCII 且不含分隔/控制字符,否则会被中间件截断或拆分。"""
        encoded = encode_name(name)
        assert encoded.isascii()
        assert all(c.isalnum() or c in "-_" for c in encoded), encoded

    def test_不带填充等号(self) -> None:
        """去掉 = 是刻意的(见 encode_name 的头注),解码侧自己补回来。"""
        assert "=" not in encode_name("张")
        assert decode_name(encode_name("张")) == "张"

    def test_空串往返(self) -> None:
        """地盤名可以省略。空串必须能安全往返,不能变成 None 或抛错。"""
        assert decode_name(encode_name("")) == ""

    def test_超长姓名在编码侧就被拒(self) -> None:
        with pytest.raises(HeaderValueError):
            encode_name("张" * MAX_NAME_BYTES)  # 每个汉字 3 字节,必超

    def test_超长输入在解码侧先查长度再解码(self) -> None:
        """1MB 的 header 值必须在**解码之前**被拒。

        反过来的话,校验代码自己就成了放大器 —— 攻击者花 1MB 带宽
        换服务端一次 1MB 的 base64 解码。
        """
        with pytest.raises(HeaderValueError):
            decode_name("A" * 1_000_000)

    def test_非法_base64_收敛成契约异常(self) -> None:
        """任何解码失败都必须是 HeaderValueError,不许把 binascii.Error 漏出去 ——
        漏出去的表现是 500 而不是 400,而这明明是客户端的问题。"""
        with pytest.raises(HeaderValueError):
            decode_name("这不是base64!!!")

    def test_非_utf8_字节被拒(self) -> None:
        import base64 as _b64

        bad = _b64.urlsafe_b64encode(b"\xff\xfe\xfd").decode().rstrip("=")
        with pytest.raises(HeaderValueError):
            decode_name(bad)

    @pytest.mark.parametrize("bad", ["张\n三", "张\r\n三", "张\x00三", "张\x1f三", "张\x7f"])
    def test_控制字符与换行被拒(self, bad: str) -> None:
        """两个理由,任何一个单独都够:
        ① 它们会被画进水印(空白或方块),凭证上就是一处看不懂的东西;
        ② 换行会污染日志的行结构 —— 那是日志注入的入口。
        """
        with pytest.raises(HeaderValueError):
            decode_name(encode_name(bad))


class Test指纹:
    def test_同样的输入给同样的指纹(self) -> None:
        kw = {
            "worker_name": "张三",
            "site_name": "A栋",
            "geo_raw": "ok;22.3;114.1;12.5",
            "photo_hash": "a" * DIGEST_HEX_LEN,
        }
        assert build_digest(**kw) == build_digest(**kw)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("worker_name", "李四"),
            ("site_name", "B栋"),
            ("geo_raw", "denied"),
            ("photo_hash", "b" * DIGEST_HEX_LEN),
        ],
    )
    def test_任一字段变了指纹就变(self, field: str, value: str) -> None:
        """这四个字段都进指纹,是为了让「同 event_id 但内容不同」能被判成 409。

        少算任何一个,那一维的改动就会被当成「同一次打卡的重发」而返回旧记录 ——
        工友以为改过来了,库里还是旧的。
        """
        base = {
            "worker_name": "张三",
            "site_name": "A栋",
            "geo_raw": "ok;22.3;114.1;12.5",
            "photo_hash": "a" * DIGEST_HEX_LEN,
        }
        assert build_digest(**base) != build_digest(**{**base, field: value})

    def test_地盤名为_None_与空串等价(self) -> None:
        """省略 X-GYT-Site 和传一个空的 X-GYT-Site 是同一件事。

        不等价的话,前端两种写法会算出两个指纹,同一次重发被判成 409。
        """
        kw = {"worker_name": "张三", "geo_raw": "denied", "photo_hash": "a" * DIGEST_HEX_LEN}
        assert build_digest(site_name=None, **kw) == build_digest(site_name="", **kw)

    def test_字段边界不可被内容伪造(self) -> None:
        """「张」+ 地盤「三」 不能和 姓名「张\\x1f三」+ 空地盤 撞出同一个指纹。

        分隔符 \\x1f 已经被 decode_name 挡在名称之外,所以真实请求里撞不到;
        这条测试锁的是**那道防线不能被拿掉** —— 拿掉之后两次不同的打卡
        会被判成重发,第二次静默返回第一次的记录。
        """
        h = "a" * DIGEST_HEX_LEN
        a = build_digest(worker_name="张", site_name="三", geo_raw="denied", photo_hash=h)
        b = build_digest(worker_name="张\x1f三", site_name="", geo_raw="denied", photo_hash=h)
        assert a != b

    def test_版本前缀在里面(self) -> None:
        """指纹构成哪天要改,前缀一改老记录自然对不上新记录 ——
        而不是「悄悄地都对不上了,不知道从哪天开始」。"""
        import hashlib

        h = "a" * DIGEST_HEX_LEN
        expected = hashlib.sha256(
            "\x1f".join(("v1", "张三", "", "denied", h)).encode("utf-8")
        ).hexdigest()
        assert (
            build_digest(worker_name="张三", site_name=None, geo_raw="denied", photo_hash=h)
            == expected
        )


class Test照片哈希与格式校验:
    def test_照片哈希是标准_sha256(self) -> None:
        assert photo_sha256(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_合法指纹被原样接受并归一化成小写(self) -> None:
        assert validate_digest_hex("A" * DIGEST_HEX_LEN) == "a" * DIGEST_HEX_LEN
        assert validate_digest_hex(f"  {'f' * DIGEST_HEX_LEN}  ") == "f" * DIGEST_HEX_LEN

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a" * (DIGEST_HEX_LEN - 1),
            "a" * (DIGEST_HEX_LEN + 1),
            "g" * DIGEST_HEX_LEN,  # 非十六进制
            "a" * (DIGEST_HEX_LEN - 1) + "!",
        ],
    )
    def test_不像样的指纹被拒(self, bad: str) -> None:
        with pytest.raises(HeaderValueError):
            validate_digest_hex(bad)


UTC_0030 = datetime(2026, 8, 15, 0, 30, 0, tzinfo=UTC)
"""UTC 00:30 = 香港 08:30 同日 —— 常规路径的基准时刻。"""

UTC_2030 = datetime(2026, 8, 15, 20, 30, 0, tzinfo=UTC)
"""UTC 20:30 = 香港次日 04:30 —— 跨午夜的关键证据时刻。"""


class Test时间快照:
    """attendance/receipt.py 的时间派生。锁的是上线闸⑥那类静默错误:
    本机与容器都是 UTC+8,不显式折腾 TZ,「靠宿主时区」这种写法永远测不出来。"""

    def test_naive_datetime_被拒(self) -> None:
        """收下 naive 值 = 替调用方猜时区,而「猜」正是这个模块要消灭的。"""
        with pytest.raises(ValueError):
            receipt.snapshot_at(datetime(2026, 8, 15, 8, 30, 0))  # noqa: DTZ001 — 故意的

    def test_同一瞬间的四种表示互相一致(self) -> None:
        snap = receipt.snapshot_at(UTC_0030)
        assert snap.checked_at == "2026-08-15T08:30:00+08:00"
        assert snap.work_date == "2026-08-15"
        assert snap.display == "2026-08-15 08:30:00"
        assert snap.stamp.tzinfo is not None

    def test_跨午夜按香港日历日(self) -> None:
        """UTC 晚上 = 香港凌晨:work_date 必须是香港那天,不是 UTC 那天。
        这一条同时是 TODO-38(夜班语义)的现状留档:当前口径 = 香港日历日。"""
        snap = receipt.snapshot_at(UTC_2030)
        assert snap.work_date == "2026-08-16"
        assert snap.checked_at == "2026-08-16T04:30:00+08:00"

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="平台没有 tzset(Windows 本机)")
    def test_篡改宿主TZ后派生结果不变(self) -> None:
        """上线闸⑥的单测形态。

        monkeypatch 不够用:它恢复环境变量但**不会替你再调一次 tzset()**,
        进程会带着最后一个时区继续跑完剩下的测试 —— 所以这里手工 try/finally,
        退场时把 TZ 恢复原样并显式 tzset()。
        """
        baseline = (receipt.snapshot_at(UTC_0030), receipt.snapshot_at(UTC_2030))
        original = os.environ.get("TZ")
        try:
            for tz in ("America/New_York", "UTC", "Pacific/Kiritimati"):
                os.environ["TZ"] = tz
                time.tzset()
                assert (receipt.snapshot_at(UTC_0030), receipt.snapshot_at(UTC_2030)) == (
                    baseline
                ), tz
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    def test_make_snapshot_出的是带偏移的香港时间(self) -> None:
        snap = receipt.make_snapshot()
        assert snap.checked_at.endswith("+08:00")
        assert snap.work_date == snap.checked_at[:10]


class Test凭证编号:
    def test_长相与格式(self) -> None:
        no = receipt.new_receipt_no(receipt.snapshot_at(UTC_0030))
        assert re.fullmatch(r"GYT-A-20260815-083000-[0-9a-f]{4}", no), no

    def test_同一快照多次生成随机尾不同(self) -> None:
        """随机尾是撞库重试的前提:重试 = 再调一次本函数,
        若两次必然相同,重试就是死循环。8 连抽全同的概率是 (1/65536)^7,可忽略。"""
        snap = receipt.snapshot_at(UTC_0030)
        assert len({receipt.new_receipt_no(snap) for _ in range(8)}) > 1

    def test_与巡检记录的守卫正则不串(self) -> None:
        """同源清单那条「已实测不串」的守门测试:report 的守卫正则要求 GYT- 后紧跟
        8 位数字,考勤编号紧跟的是 A-。哪天有人改了任一边的格式,这里先红。"""
        from gyt.agents.report import REPORT_RECEIPT_PATTERN

        no = receipt.new_receipt_no(receipt.snapshot_at(UTC_0030))
        assert re.search(REPORT_RECEIPT_PATTERN, no) is None


class Test按langgraph的文件路径方式可加载:
    """风险#7 的钉子。

    langgraph_api/api/__init__.py:195 对 ``http.app`` 用 spec_from_file_location
    按文件路径加载 —— 与 graphs 的 ``./src/gyt/graph.py:graph`` 同一机制,后者已在
    线上跑着。这里复刻同样的加载方式:若 checkin_api 哪天引入了只有包内导入才成立
    的写法(相对导入、依赖 ``__package__``),这条会先于部署翻红。
    """

    def test_spec_from_file_location_能加载并拿到契约常量(self) -> None:
        path = Path(__file__).resolve().parents[2] / "src" / "gyt" / "checkin_api.py"
        spec = importlib.util.spec_from_file_location("checkin_api_by_path", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.HEADER_EVENT_ID == "x-gyt-event-id"
        # T2 已落地:langgraph 取的就是这个模块级属性(module.__dict__["app"]),
        # 属性不在 = 部署时 http.app 解析失败,而 make test 不跑这条就全绿。
        assert hasattr(module, "app")


# ===========================================================================
# 以下:handler 与 app 的测试(T2)。上面是 T1 的契约层测试,只追加不改。
# 水印一律 mock(B 泳道并行填实现,接口冻结);artifacts.register 真调
# (conftest 已把数据目录隔离到 tmp);SQLite 台账真写真查 —— 回执说什么不算,
# 库里有没有才算(与真机验收同一哲学)。
# ===========================================================================

FAKE_JPEG = bytes.fromhex("ffd8ffe000104a46494600") + b"gyt-checkin-test-photo"
"""最小「像 JPEG」的字节:魔数 FF D8 FF 打头。真正的解码归水印层,
而水印在这儿全程 mock,所以只需要过 handler 的魔数闸。"""

WATERMARKED = bytes.fromhex("ffd8ffe1") + b"gyt-watermarked"
"""mock 水印的固定返回值。与 FAKE_JPEG 刻意不同 —— 好断言产物库里躺的是
水印后的字节而不是原图(原图不进产物库,W7 §3.3)。"""

REAL_TOKEN = "gyt-checkin-token-0123456789abcdef"
"""测试用「真」令牌:≥24 位、不是占位符,能过 access.py 的「像真的」判定。"""


@pytest.fixture(autouse=True)
def _干净的打卡限流器() -> Iterator[None]:
    """每个用例前后丢掉打卡的两只桶。

    桶是 checkin_api 的模块级全局,conftest 的 Settings 隔离管不到它 ——
    参数不变就不重建,上一条用例烧掉的令牌会漏给下一条
    (test_auth.py 为 auth 的那只桶设过同款夹具,坑是同一个)。
    """
    checkin_api.reset_limiters()
    yield
    checkin_api.reset_limiters()


@pytest.fixture
def 假水印() -> Iterator[mock.MagicMock]:
    """把真渲染换成固定字节。

    patch 的路径就是 handler 运行期解析的那条(``gyt.attendance.watermark`` 的
    模块属性)—— watermark.py 头注写明 A 泳道的测试**必须**这么替换,绝不真渲染。
    """
    with mock.patch(
        "gyt.attendance.watermark.render_attendance_photo", return_value=WATERMARKED
    ) as fake:
        yield fake


@pytest.fixture
def client() -> TestClient:
    """真 Starlette 栈:TestClient 走完整 ASGI 流程,``request.stream()`` 是真流式。"""
    return TestClient(checkin_api.app)


def _headers(
    *,
    event_id: str,
    name: str = "张三",
    site: str | None = "A栋地盤",
    geo: str | None = "ok;22.302711;114.177216;12.5",
    source: str = "camera",
    photo: bytes = FAKE_JPEG,
    digest: str | None = None,
    api_key: str | None = None,
) -> dict[str, str]:
    """按模块头注的请求契约拼一套合法 header,单项可覆盖(None = 不发这个头)。"""
    headers = {
        "x-gyt-event-id": event_id,
        "x-gyt-worker": encode_name(name),
        "x-gyt-source": source,
        "x-gyt-digest": digest if digest is not None else photo_sha256(photo),
    }
    if site is not None:
        headers["x-gyt-site"] = encode_name(site)
    if geo is not None:
        headers["x-gyt-geo"] = geo
    if api_key is not None:
        headers["x-api-key"] = api_key
    return headers


def _post(
    client: TestClient, *, event_id: str, photo: bytes = FAKE_JPEG, **overrides: Any
) -> httpx.Response:
    """发一次合法形状的打卡(body 与 digest 缺省自洽,单项可覆盖造反例)。"""
    return client.post(
        "/checkin", content=photo, headers=_headers(event_id=event_id, photo=photo, **overrides)
    )


def _seed_row(n: int) -> att_db.AttendanceRow:
    """直接落一条台账(绕开 handler),给 GET 侧与撞库测试当数据。"""
    snap = receipt.snapshot_at(UTC_0030)
    return att_db.insert_checkin(
        att_db.CheckinDraft(
            event_id=f"evt-seed-{n}",
            req_digest=f"{n:064x}",
            worker_name=f"工友{n}",
            site_name=None,
            checked_at=snap.checked_at,
            work_date=snap.work_date,
            lat=None,
            lon=None,
            accuracy_m=None,
            geo_status="absent",
            source="camera",
            receipt_no=f"GYT-A-20260815-083000-{n:04x}",
            artifact_id=f"{n:032x}",
            created_at=snap.checked_at,
        )
    )


class Test令牌自查:
    """上线闸①。这道自查是 langgraph.json 漏配 enable_custom_route_auth 时的
    唯一兜底 —— 那种漏配没有任何报错,站点照开、打卡照成,只是鉴权整条不执行。"""

    def test_设了令牌后不带钥匙的POST被拒(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()
        resp = _post(client, event_id="evt-auth-1")
        assert resp.status_code == 401
        body = resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "UNAUTHORIZED"
        假水印.assert_not_called()  # 被拒的请求一步业务都不许走
        assert att_db.find_by_event_id("evt-auth-1") is None

    def test_带对令牌则放行(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """handler 必须**每请求**现取 settings:本用例与上一条在同一进程里换环境变量,
        import 时缓存令牌的写法在这里当场翻红 —— 测试就是靠这个换环境的。"""
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()
        assert _post(client, event_id="evt-auth-2", api_key=REAL_TOKEN).status_code == 200

    def test_未配置令牌时放行(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """conftest 清干净了 GYT_*,这就是「没配」的真实状态 —— make dev 与真机验收
        不发令牌头,未配置必须放行(auth.py 同一取舍,那边解释了为什么)。"""
        assert _post(client, event_id="evt-auth-3").status_code == 200

    def test_占位符令牌当成没配(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """判定必须与 auth.py 完全同源:占位符 = 没配 = 放行。两边分头判的话,
        会出现「对话链开门、打卡锁死」的半开状态 —— 最难查的一种。"""
        monkeypatch.setenv("GYT_ACCESS_TOKEN", "替换成openssl_rand_hex_32的输出")
        get_settings.cache_clear()
        assert _post(client, event_id="evt-auth-4").status_code == 200

    def test_GET同样自查令牌(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """recent 是同一扇门里的读端:POST 锁了它不锁,等于凭证台账裸奔。"""
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()
        assert client.get("/checkin/recent").status_code == 401
        with_key = client.get("/checkin/recent", headers={"x-api-key": REAL_TOKEN})
        assert with_key.status_code == 200


class Test幂等:
    def test_同键同指纹重发返回原凭证且不烧水印(
        self, client: TestClient, 假水印: mock.MagicMock
    ) -> None:
        """幂等层①:重发不读 body、不画水印、不写库。水印 mock 的调用次数是
        「不烧」的直接证据 —— 线上它是真金白银的 CPU、内存与磁盘。"""
        first = _post(client, event_id="evt-idem-1")
        assert first.status_code == 200
        second = _post(client, event_id="evt-idem-1")
        assert second.status_code == 200
        assert second.json()["data"]["receipt_no"] == first.json()["data"]["receipt_no"]
        assert 假水印.call_count == 1

    def test_同键异指纹409(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """同 event_id 但照片指纹换了 —— 不是同一次打卡的重发,必须如实 409;
        静默返回旧记录等于把第二次打卡吞掉,工友以为打上了。"""
        assert _post(client, event_id="evt-idem-2").status_code == 200
        conflict = _post(client, event_id="evt-idem-2", digest="0" * DIGEST_HEX_LEN)
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "CONFLICT"
        assert 假水印.call_count == 1  # 409 也不烧第二次水印

    def test_同键但元数据变了也是409(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """指纹不只含照片:姓名/地盤/坐标任何一维变了都不算「同一次」——
        改了名字重发却拿回旧凭证的话,工友以为改成功了,库里还是旧名字。"""
        assert _post(client, event_id="evt-idem-3").status_code == 200
        assert _post(client, event_id="evt-idem-3", name="李四").status_code == 409


class Test服务端指纹:
    def test_header指纹造假时落库的是服务端算的(
        self, client: TestClient, 假水印: mock.MagicMock
    ) -> None:
        """上线闸②:X-GYT-Digest 是客户端报的,**绝不落库**。落了它,库里的指纹
        就是攻击者说了算,后续所有幂等比对都在拿谎言当基准。
        直接查库断言,不信响应回执。"""
        forged = "f" * DIGEST_HEX_LEN
        assert forged != photo_sha256(FAKE_JPEG)  # 前提自检:确实是假的
        resp = _post(client, event_id="evt-digest-1", digest=forged)
        assert resp.status_code == 200
        row = att_db.find_by_event_id("evt-digest-1")
        assert row is not None
        server_side = build_digest(
            worker_name="张三",
            site_name="A栋地盤",
            geo_raw="ok;22.302711;114.177216;12.5",
            photo_hash=photo_sha256(FAKE_JPEG),
        )
        claimed = build_digest(
            worker_name="张三",
            site_name="A栋地盤",
            geo_raw="ok;22.302711;114.177216;12.5",
            photo_hash=forged,
        )
        assert row.req_digest == server_side
        assert row.req_digest != claimed


class Test照片收取:
    def test_超限body当场413且一步不往下走(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """流式闸:超过 photo_max_mb 边收边断。把上限压到 ~100 字节来测,
        免得真造 10MB;上限必须走 settings(禁止硬编码)——环境变量能改到它
        本身就是断言的一半。"""
        monkeypatch.setenv("GYT_PHOTO_MAX_MB", "0.0001")  # ≈104 字节
        get_settings.cache_clear()
        big = FAKE_JPEG + b"x" * 4096
        resp = _post(client, event_id="evt-big", photo=big)
        assert resp.status_code == 413
        assert resp.json()["error_code"] == "FILE_TOO_LARGE"
        假水印.assert_not_called()
        assert att_db.find_by_event_id("evt-big") is None

    def test_空body400(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        resp = _post(client, event_id="evt-empty", photo=b"")
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_INPUT"

    def test_非JPEG魔数400(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """魔数是唯一判据 —— Content-Type 不可信(DXF 那条线证过浏览器 MIME 会漂)。"""
        fake_png = b"\x89PNG\r\n\x1a\n" + b"whatever"
        resp = _post(client, event_id="evt-png", photo=fake_png)
        assert resp.status_code == 400
        假水印.assert_not_called()

    def test_正常照片200且凭证形状齐全(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """快乐路径把响应契约整个钉住:四键信封、9 键凭证对象、GYT-A- 编号、
        产物库里躺的是**水印后的**字节(原图不进产物库,W7 §3.3)。"""
        resp = _post(client, event_id="evt-ok-1")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"ok", "data", "user_msg", "error_code"}
        assert body["ok"] is True and body["error_code"] is None
        data = body["data"]
        assert set(data) == {
            "receipt_no",
            "worker_name",
            "site_name",
            "checked_at",
            "work_date",
            "geo_status",
            "source",
            "artifact_id",
            "photo_purged_at",
        }
        assert re.fullmatch(r"GYT-A-\d{8}-\d{6}-[0-9a-f]{4}", data["receipt_no"])
        assert data["worker_name"] == "张三"
        assert data["photo_purged_at"] is None
        stored = artifacts.resolve(data["artifact_id"]).read_bytes()
        assert stored == WATERMARKED
        assert stored != FAKE_JPEG
        # kind 必须是 ATTENDANCE —— 清理器全靠它把考勤图和巡检照片分开(§3.9)。
        assert artifacts.read_meta(data["artifact_id"])["kind"] == "ATTENDANCE"
        # 水印收到的是原图字节 + 出自 messages.py 的文字行(姓名与编号都在行里)。
        photo_arg, lines_arg = 假水印.call_args.args
        assert photo_arg == FAKE_JPEG
        assert any("张三" in line for line in lines_arg)
        assert any(data["receipt_no"] in line for line in lines_arg)


class Test定位契约:
    @pytest.mark.parametrize(
        "坏geo",
        [
            "ok",  # ok 却没坐标
            "ok;22.3;114.1",  # 少精度段
            "ok;22.3;114.1;12.5;x",  # 多一段
            "ok;91;114.1;5",  # 纬度越界
            "ok;22.3;181;5",  # 经度越界
            "ok;22.3;114.1;-1",  # 精度为负
            "ok;nan;114.1;5",  # NaN —— float() 照单全收,必须显式挡
            "ok;22.3;inf;5",  # Infinity 同理
            "ok;22.3;114.1;1e999",  # 溢出成 inf 的写法
            "ok;;114.1;5",  # 空段
            "denied;22.3;114.1;5",  # 非 ok 带坐标:自相矛盾的记录
            "absent",  # absent 不许明发,它的语义是「压根没发」
            "gps-ok",  # 词表之外
            "",  # 发了个空 header
        ],
    )
    def test_geo违约一律400(self, client: TestClient, 假水印: mock.MagicMock, 坏geo: str) -> None:
        """geo 是唯一带数值解析的 header,违约形态最多;全部要在读 body 之前
        被打回,否则脏坐标进库后 CHECK 只拦得住枚举、拦不住 NaN。"""
        resp = _post(client, event_id="evt-geo-bad", geo=坏geo)
        assert resp.status_code == 400, 坏geo
        body = resp.json()
        assert body["error_code"] == "INVALID_INPUT"
        assert body["user_msg"] == messages.BAD_REQUEST  # 具体字段名只进日志,不进人话
        假水印.assert_not_called()

    def test_denied单段能打卡且库里没坐标(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """拿不到坐标照样能打卡(工地信号差是常态)——为定位挡打卡是本末倒置。"""
        assert _post(client, event_id="evt-geo-denied", geo="denied").status_code == 200
        row = att_db.find_by_event_id("evt-geo-denied")
        assert row is not None
        assert row.geo_status == "denied"
        assert (row.lat, row.lon, row.accuracy_m) == (None, None, None)

    def test_header缺失落absent(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """absent 只能由「header 整个没发」产生 —— 它和 denied 在审计里意义完全
        不同(一个查前端,一个问工友),两种来路必须在库里分得开。"""
        assert _post(client, event_id="evt-geo-absent", geo=None).status_code == 200
        row = att_db.find_by_event_id("evt-geo-absent")
        assert row is not None
        assert row.geo_status == "absent"

    def test_ok时坐标原样落库(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        assert _post(client, event_id="evt-geo-ok").status_code == 200
        row = att_db.find_by_event_id("evt-geo-ok")
        assert row is not None
        assert row.geo_status == "ok"
        assert (row.lat, row.lon, row.accuracy_m) == (22.302711, 114.177216, 12.5)


class Test其它header校验:
    @pytest.mark.parametrize(
        ("说明", "覆盖"),
        [
            ("source 空", {"source": ""}),
            ("source 词表外", {"source": "selfie"}),
            ("source 大小写不对", {"source": "CAMERA"}),
            ("digest 太短", {"digest": "abc"}),
            ("digest 非十六进制", {"digest": "g" * DIGEST_HEX_LEN}),
        ],
    )
    def test_source与digest违约400(
        self,
        client: TestClient,
        假水印: mock.MagicMock,
        说明: str,
        覆盖: dict[str, str],
    ) -> None:
        """枚举与定长指纹是最便宜的两道格式闸;漏了 source 校验的话,
        脏值会一路走到 INSERT 才撞 CHECK,报错在数据库层、方向全错
        (db/attendance.py 头注写明这道校验必须在 handler)。"""
        resp = _post(client, event_id="evt-hdr-bad", **覆盖)
        assert resp.status_code == 400, 说明

    def test_缺姓名400(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        headers = _headers(event_id="evt-no-worker")
        del headers["x-gyt-worker"]
        resp = client.post("/checkin", content=FAKE_JPEG, headers=headers)
        assert resp.status_code == 400

    def test_缺指纹400(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        """X-GYT-Digest 是必填:静默放行「没发指纹」的请求,幂等层①永远不命中,
        每次重发都白烧一次水印,而功能看起来完全正常(模块头注点名的静默失败)。"""
        headers = _headers(event_id="evt-no-digest")
        del headers["x-gyt-digest"]
        resp = client.post("/checkin", content=FAKE_JPEG, headers=headers)
        assert resp.status_code == 400

    def test_缺event_id_400(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        headers = _headers(event_id="evt-x")
        del headers["x-gyt-event-id"]
        resp = client.post("/checkin", content=FAKE_JPEG, headers=headers)
        assert resp.status_code == 400

    def test_超长event_id_400(self, client: TestClient, 假水印: mock.MagicMock) -> None:
        too_long = "e" * (checkin_api.MAX_EVENT_ID_LEN + 1)
        assert _post(client, event_id=too_long).status_code == 400


class Test限流:
    def test_全局桶超发429并带RetryAfter(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全局桶是唯一的真闸(W7 §3.6)。Retry-After ≥ 1:回 0 等于叫人立刻重试,
        自找雪崩。参数从环境变量注入 = 顺带验证「参数变了就重建」那条分支。"""
        monkeypatch.setenv("GYT_ATTENDANCE_RATE_BURST", "2")
        monkeypatch.setenv("GYT_ATTENDANCE_RATE_PER_MINUTE", "1")
        get_settings.cache_clear()
        checkin_api.reset_limiters()
        assert _post(client, event_id="evt-rl-1").status_code == 200
        assert _post(client, event_id="evt-rl-2").status_code == 200
        third = _post(client, event_id="evt-rl-3")
        assert third.status_code == 429
        assert third.json()["error_code"] == "RATE_LIMITED"
        assert int(third.headers["Retry-After"]) >= 1
        assert att_db.find_by_event_id("evt-rl-3") is None  # 被限住的请求不落库

    def test_worker桶只拦同名不误伤别人(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """worker 桶防手抖连点:同名第二发被拦,换个名字照常过。
        这同时就是「它不是安全控制」的行为证据 —— 桶键是客户端报的姓名,
        换名字 = 新满桶,真正的天花板只有全局那只。"""
        monkeypatch.setenv("GYT_ATTENDANCE_WORKER_RATE_BURST", "1")
        monkeypatch.setenv("GYT_ATTENDANCE_WORKER_RATE_PER_MINUTE", "1")
        get_settings.cache_clear()
        checkin_api.reset_limiters()
        assert _post(client, event_id="evt-w-1", name="张三").status_code == 200
        blocked = _post(client, event_id="evt-w-2", name="张三")
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        assert _post(client, event_id="evt-w-3", name="李四").status_code == 200

    def test_限流卡在读body之前(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """429 的请求一张图都不许处理 —— 限流挡的是流量,不只是写库
        (W7 §3.6:「否则限流没挡住流量,只挡住了写库」)。
        水印零新增调用是「没读没画」的直接证据。"""
        monkeypatch.setenv("GYT_ATTENDANCE_RATE_BURST", "1")
        monkeypatch.setenv("GYT_ATTENDANCE_RATE_PER_MINUTE", "1")
        get_settings.cache_clear()
        checkin_api.reset_limiters()
        assert _post(client, event_id="evt-rb-1").status_code == 200
        assert 假水印.call_count == 1
        assert _post(client, event_id="evt-rb-2").status_code == 429
        assert 假水印.call_count == 1


class Test凭证编号撞库:
    def test_撞库后换编号重画水印重登记(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DuplicateReceiptError → 换编号**从水印起重来**:编号画在图上,
        只换库里那一列的话,图上的编号和台账对不上 —— 凭证自己证伪自己。
        断言水印画了两次、两次的行里编号不同、最终落库的是第二个编号。"""
        taken = "GYT-A-20260815-083000-dead"
        snap = receipt.snapshot_at(UTC_0030)
        att_db.insert_checkin(
            att_db.CheckinDraft(
                event_id="evt-occupied",
                req_digest="d" * 64,
                worker_name="占位",
                site_name=None,
                checked_at=snap.checked_at,
                work_date=snap.work_date,
                lat=None,
                lon=None,
                accuracy_m=None,
                geo_status="absent",
                source="camera",
                receipt_no=taken,
                artifact_id=None,
                created_at=snap.checked_at,
            )
        )
        号源 = iter([taken, "GYT-A-20260815-083000-beef"])
        monkeypatch.setattr(receipt, "new_receipt_no", lambda _snap: next(号源))
        resp = _post(client, event_id="evt-retry-1")
        assert resp.status_code == 200
        assert resp.json()["data"]["receipt_no"] == "GYT-A-20260815-083000-beef"
        assert 假水印.call_count == 2
        first_lines = 假水印.call_args_list[0].args[1]
        second_lines = 假水印.call_args_list[1].args[1]
        assert any(taken in line for line in first_lines)
        assert any("beef" in line for line in second_lines)


class Test并发同键:
    def test_插入撞唯一约束时回查返回赢家(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """幂等层③:层①没查到、真 INSERT 时才撞 UNIQUE(两个请求几乎同时进来)。
        单进程测试造不出真并发,用「层①装作没查到,而行其实已在」复刻同一时序。
        指纹相同 → 把赢家那条当成自己的结果返回,工友看不出输赢。"""
        expected_digest = build_digest(
            worker_name="张三",
            site_name="A栋地盤",
            geo_raw="ok;22.302711;114.177216;12.5",
            photo_hash=photo_sha256(FAKE_JPEG),
        )
        snap = receipt.snapshot_at(UTC_0030)
        att_db.insert_checkin(
            att_db.CheckinDraft(
                event_id="evt-race-1",
                req_digest=expected_digest,
                worker_name="张三",
                site_name="A栋地盤",
                checked_at=snap.checked_at,
                work_date=snap.work_date,
                lat=22.302711,
                lon=114.177216,
                accuracy_m=12.5,
                geo_status="ok",
                source="camera",
                receipt_no="GYT-A-20260815-083000-cafe",
                artifact_id=None,
                created_at=snap.checked_at,
            )
        )
        real_find = att_db.find_by_event_id
        calls = {"n": 0}

        def 第一次装没有(event_id: str) -> att_db.AttendanceRow | None:
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # 层①这次:装作赢家还没落库
            return real_find(event_id)

        monkeypatch.setattr(att_db, "find_by_event_id", 第一次装没有)
        resp = _post(client, event_id="evt-race-1")
        assert resp.status_code == 200
        assert resp.json()["data"]["receipt_no"] == "GYT-A-20260815-083000-cafe"

    def test_撞键且指纹不同则409(
        self, client: TestClient, 假水印: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """并发撞键但内容对不上 —— 与层①的 409 同一句话、同一个码,
        前端不用分辨冲突发生在哪一层。"""
        snap = receipt.snapshot_at(UTC_0030)
        att_db.insert_checkin(
            att_db.CheckinDraft(
                event_id="evt-race-2",
                req_digest="0" * 64,  # 与本请求的服务端指纹必然不同
                worker_name="别人",
                site_name=None,
                checked_at=snap.checked_at,
                work_date=snap.work_date,
                lat=None,
                lon=None,
                accuracy_m=None,
                geo_status="absent",
                source="camera",
                receipt_no="GYT-A-20260815-083000-feed",
                artifact_id=None,
                created_at=snap.checked_at,
            )
        )
        real_find = att_db.find_by_event_id
        calls = {"n": 0}

        def 第一次装没有(event_id: str) -> att_db.AttendanceRow | None:
            calls["n"] += 1
            return None if calls["n"] == 1 else real_find(event_id)

        monkeypatch.setattr(att_db, "find_by_event_id", 第一次装没有)
        resp = _post(client, event_id="evt-race-2")
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "CONFLICT"


class Test水印异常翻译:
    def test_缺字400且点名是哪些字(self, client: TestClient) -> None:
        """MissingGlyphsError → 400,user_msg 必须带上那几个字(W7 §3.4)——
        工友换个同音字自己就能解决;不点名的话他只会反复重试反复失败。"""
        with mock.patch(
            "gyt.attendance.watermark.render_attendance_photo",
            side_effect=MissingGlyphsError(["𠮶", "𡃁"]),
        ):
            resp = _post(client, event_id="evt-glyph-1")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "INVALID_INPUT"
        assert "𠮶" in body["user_msg"] and "𡃁" in body["user_msg"]
        # 出不了凭证就不许留半截台账(先图后库的顺序保证,W7 §3.3)。
        assert att_db.find_by_event_id("evt-glyph-1") is None

    def test_字体不可用500且不漏部署细节(self, client: TestClient) -> None:
        """FontUnavailableError 是部署错误:500 + 人话;字体路径这类细节只进日志
        (errors.py 的 detail= 哲学),user_msg 里出现路径就是在教人摸内网。"""
        with mock.patch(
            "gyt.attendance.watermark.render_attendance_photo",
            side_effect=FontUnavailableError("/usr/share/fonts 里一个候选都没有"),
        ):
            resp = _post(client, event_id="evt-font-1")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "INTERNAL"
        assert "/usr/share" not in body["user_msg"]
        assert att_db.find_by_event_id("evt-font-1") is None


class Test最近凭证:
    def test_按写入序倒排(self, client: TestClient) -> None:
        """刚打完卡的人第一眼要看到自己那条。排序键是自增 id(插入序)而不是
        checked_at —— 同秒多条时后者顺序不定(db 层 _RECENT_SQL 的头注)。"""
        for n in (1, 2, 3):
            _seed_row(n)
        records = client.get("/checkin/recent").json()["data"]["records"]
        assert [r["receipt_no"] for r in records] == [
            "GYT-A-20260815-083000-0003",
            "GYT-A-20260815-083000-0002",
            "GYT-A-20260815-083000-0001",
        ]

    def test_limit的clamp与缺省(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """契约:缺省与上限都取 settings.attendance_recent_limit,超了 clamp
        不报错;坏值按缺省 —— 它只影响条数,报 400 徒增前端分支。"""
        monkeypatch.setenv("GYT_ATTENDANCE_RECENT_LIMIT", "2")
        get_settings.cache_clear()
        for n in (1, 2, 3):
            _seed_row(n)

        def 条数(url: str) -> int:
            return len(client.get(url).json()["data"]["records"])

        assert 条数("/checkin/recent") == 2  # 缺省 = 上限
        assert 条数("/checkin/recent?limit=999") == 2  # 超了 clamp
        assert 条数("/checkin/recent?limit=1") == 1
        assert 条数("/checkin/recent?limit=abc") == 2  # 坏值按缺省,不报错
        assert 条数("/checkin/recent?limit=0") == 1  # 下限 clamp 到 1

    def test_photo_purged_at透传且artifact_id为空(self, client: TestClient) -> None:
        """清理器动过的行必须原样透出:前端靠 artifact_id 为 null + purged_at
        非空渲染「凭证图已过期清理」而不是裂图(上线闸③的后端半边 ——
        这两个字段少透传一个,前端就只剩裂图标和真丢图两种一模一样的界面)。"""
        row = _seed_row(7)
        att_db.mark_photos_purged([row.id], "2026-08-15T09:00:00+08:00")
        record = client.get("/checkin/recent").json()["data"]["records"][0]
        assert record["artifact_id"] is None
        assert record["photo_purged_at"] == "2026-08-15T09:00:00+08:00"


class Test文案与词表同源:
    def test_水印定位说明覆盖全部非ok状态(self) -> None:
        """messages.GEO_STATUS_LABELS 必须与 db 的 GEO_STATUSES 同源(ok 除外,
        它画坐标)。少一个键**不会报错** —— 那种状态的凭证只会画出兜底文案,
        审计时六种状态就并糊了;这条守门测试让漂移在 CI 先红。"""
        assert set(messages.GEO_STATUS_LABELS) == set(att_db.GEO_STATUSES) - {"ok"}
