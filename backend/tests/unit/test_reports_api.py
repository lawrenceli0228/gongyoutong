"""``GET /reports`` —— 巡检记录抽屉的数据源(2026-08-22)。

它补的是这条链的终点:文档真的生成了、真的落盘了,而那份 Envelope 被 supervisor 的
``output_mode="last_message"`` 整个丢掉,于是界面上**没有任何下载出口**,
而提示词教模型说「跟管理员说编号就行」—— 那个管理员不存在。

这份要钉死的静默错误:
  · **编造编号** —— 抠不出编号时凑一个(比如拿 artifact_id 前八位)。
    一个长得像编号、却对不上任何文档的串,比一格空白坏得多:它会被人报给别人、
    写进留档,而事后谁也查不到那份文件。
  · **排序反了** —— 日期目录名靠 ``YYYYMMDD`` 的字典序当时间序,
    ``register()`` 那边换个带分隔符的格式,这里会**静默乱序**。
  · **扫穿整个产物目录** —— 线上绝大多数产物是照片,"找 20 份记录"要翻过上千份照片的
    sidecar。没有上限的话,跑了半年的站点点开抽屉会读上万个小文件。
  · **扫到上限却报"就这么多"** —— 人以为记录只有这些,而其实是没扫完。
  · **鉴权漏掉** —— 回执里的编号能直接换出那份 docx(隐患明细 + 现场照片)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gyt import reports_api
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind

REAL_TOKEN: Final[str] = "0123456789abcdef0123456789abcdef"
"""32 位,过得了 ``core/access`` 那道"太短就当没配"的闸。"""

FAKE_DOCX: Final[bytes] = b"PK\x03\x04fake-docx-bytes"


@pytest.fixture
def client() -> TestClient:
    """真 Starlette 栈,只挂本模块的路由。

    ⚠️ 不直接打 ``webapp.app``:那份要 import 整个 cad / knowledge 栈
    (ezdxf、chromadb…),在装不上 torch 的机器上根本 import 不了。
    「这条路由有没有挂进 webapp」由另一条用例单独盯(它只比路径,不建 app)。
    """
    return TestClient(Starlette(routes=list(reports_api.REPORTS_ROUTES)))


def _make_report(name: str = "巡检记录_GYT-20260822-153012.docx") -> str:
    """真走 ``artifacts.register`` 登记一份 REPORT 产物,返回 artifact_id。

    **不手搓 sidecar**:手搓的那份是测试自己编的形状,比对了也不说明
    这条端点读得懂 ``register()`` 真正写出来的东西 —— 而那正是唯一要验的事。
    """
    return artifacts.register(FAKE_DOCX, kind=ArtifactKind.REPORT, original_name=name)


def _make_photo() -> str:
    """登记一张照片。抽屉里**不许**出现它 —— 线上绝大多数产物都是这个。"""
    return artifacts.register(
        b"\xff\xd8\xff\xe0fake", kind=ArtifactKind.PHOTO, original_name="现场.jpg"
    )


def _day_dir(name: str) -> Path:
    """手工造一个日期目录(排序用例要造出"昨天""前天",而 register 只会写今天)。"""
    path = get_settings().artifacts_dir / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plant(day: str, artifact_id: str, original_name: str, *, kind: str = "REPORT") -> None:
    """往指定日期目录里种一份 sidecar + 正文。

    形状**逐字照 ``core/artifacts.register()`` 写出来的那份**(两边注释互指):
    它哪天改存法,这里种出来的东西就不再代表真库,而用例会照绿。
    """
    folder = _day_dir(day)
    (folder / f"{artifact_id}.docx").write_bytes(FAKE_DOCX)
    (folder / f"{artifact_id}.json").write_text(
        json.dumps(
            {
                "id": artifact_id,
                "kind": kind,
                "original_name": original_name,
                "ext": ".docx",
                "size_bytes": len(FAKE_DOCX),
                "sha256": "0" * 64,
                "created_at": f"{day[:4]}-{day[4:6]}-{day[6:]}T09:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _get(client: TestClient, query: str = "") -> dict[str, Any]:
    resp = client.get(f"/reports{query}")
    assert resp.status_code == 200, resp.text
    return resp.json()


class Test列表:
    def test_一份都没有时说的是好好说话而不是空数组(self, client: TestClient) -> None:
        """空态那句话得告诉人**下一步做什么**。只回 `[]` 的话界面上是一片空白。"""
        信封 = _get(client)

        assert 信封["data"] == {"reports": [], "total": 0, "scan_truncated": False}
        assert "拍张照" in 信封["user_msg"]

    def test_列出巡检记录_五个键齐全(self, client: TestClient) -> None:
        """少一个键前端就少渲一块,而且不会报错。"""
        artifact_id = _make_report()

        (行,) = _get(client)["data"]["reports"]

        assert set(行) == {"artifact_id", "report_no", "filename", "size_bytes", "created_at"}
        assert 行["artifact_id"] == artifact_id
        assert 行["report_no"] == "GYT-20260822-153012"
        assert 行["filename"] == "巡检记录_GYT-20260822-153012.docx"
        assert 行["size_bytes"] == len(FAKE_DOCX)
        assert 行["created_at"], "没有它前端排不了序、也显示不出「什么时候出的」"

    def test_照片不进这个抽屉(self, client: TestClient) -> None:
        """线上绝大多数产物是照片。kind 判据松掉的表现是抽屉里混进一堆
        点开是图片的"巡检记录",而且没有任何报错。"""
        _make_photo()
        _make_photo()
        artifact_id = _make_report()

        reports = _get(client)["data"]["reports"]

        assert [r["artifact_id"] for r in reports] == [artifact_id]

    def test_抠不出编号时给None而不是编一个(self, client: TestClient) -> None:
        """🔴 一个长得像编号、却对不上任何文档的串,比一格空白坏得多 ——
        它会被人报给别人、写进留档,而事后谁也查不到那份文件。"""
        _make_report(name="不知道谁生成的.docx")

        (行,) = _get(client)["data"]["reports"]

        assert 行["report_no"] is None
        assert 行["filename"] == "不知道谁生成的.docx", "文件名照给,只有编号那格空着"

    def test_最近的排最前面(self, client: TestClient) -> None:
        """日期目录名靠 ``YYYYMMDD`` 的**字典序当时间序**。

        ``core/artifacts.register`` 哪天换成带分隔符的格式(``2026-08-22``),
        排序会**静默错**:列表顺序乱掉,而不会有任何报错。
        """
        _plant("20260820", "a" * 32, "巡检记录_GYT-20260820-090000.docx")
        _plant("20260822", "c" * 32, "巡检记录_GYT-20260822-090000.docx")
        _plant("20260821", "b" * 32, "巡检记录_GYT-20260821-090000.docx")

        reports = _get(client)["data"]["reports"]

        assert [r["report_no"] for r in reports] == [
            "GYT-20260822-090000",
            "GYT-20260821-090000",
            "GYT-20260820-090000",
        ]

    def test_同一天里也按新的在前(self, client: TestClient) -> None:
        """同一个日期目录内部按 sidecar 文件名倒序 —— 这是个**近似**(artifact_id 是
        uuid4,与时间无关),写在这儿是为了让人知道它是近似:
        真正的时间序在 ``created_at`` 里,前端要严格排序就按那个字段排。
        跨天那一层是准的,而抽屉里绝大多数情况就是跨天。"""
        _plant("20260822", "f" * 32, "巡检记录_GYT-20260822-180000.docx")
        _plant("20260822", "0" * 32, "巡检记录_GYT-20260822-080000.docx")

        reports = _get(client)["data"]["reports"]

        assert [r["artifact_id"] for r in reports] == ["f" * 32, "0" * 32]

    def test_坏掉的sidecar跳过而不是让整个抽屉打不开(self, client: TestClient) -> None:
        """一份坏元数据不该让人连别的记录都看不到(同 ``cleanup.py``:宁可漏,不可炸)。"""
        _day_dir("20260822").joinpath(("e" * 32) + ".json").write_text(
            "{不是 json", encoding="utf-8"
        )
        _plant("20260822", "d" * 32, "巡检记录_GYT-20260822-120000.docx")

        reports = _get(client)["data"]["reports"]

        assert [r["report_no"] for r in reports] == ["GYT-20260822-120000"]


class Test条数与扫描上限:
    def test_limit截断(self, client: TestClient) -> None:
        for i in range(5):
            _plant("20260822", f"{i:032x}", f"巡检记录_GYT-20260822-09000{i}.docx")

        data = _get(client, "?limit=2")["data"]

        assert data["total"] == 2 and len(data["reports"]) == 2

    def test_limit超上限是截断不是报错(self, client: TestClient) -> None:
        """只读列表,为一个过大的数字挡住整页内容不划算(同 ``_issued_by`` 超长截断)。"""
        _make_report()
        assert client.get("/reports?limit=999999").status_code == 200

    @pytest.mark.parametrize("bad", ["0", "-1", "很多", "3.5"])
    def test_limit解析不出来回400(self, client: TestClient, bad: str) -> None:
        """静默当默认值的话,人以为自己传的数生效了。"""
        resp = client.get(f"/reports?limit={bad}")

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_INPUT"

    def test_扫到上限还没凑够时如实说只列了最近这些(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **不许假装"就这么多"。**

        线上"找 20 份记录"要翻过上千份照片的 sidecar。扫描预算用完时,
        人看到的必须是「只列了最近这些」,而不是一个看起来完整的短清单 ——
        后者会让人以为某份记录丢了。
        """
        monkeypatch.setattr(reports_api, "_MAX_SIDECARS_SCANNED", 3)
        for i in range(5):
            _plant("20260822", f"{i:032x}", "现场.jpg", kind="PHOTO")
        _plant("20260821", "f" * 32, "巡检记录_GYT-20260821-090000.docx")

        信封 = _get(client)

        assert 信封["data"]["scan_truncated"] is True
        assert 信封["data"]["reports"] == [], "预算在照片上耗光了,一份记录都没翻到"
        assert "只列了最近这些" in 信封["user_msg"]
        assert "还没有巡检记录" not in 信封["user_msg"], "🔴 那是一句谎 —— 真相是没扫到,不是没有"

    def test_凑够了就停_不再往下扫(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """收够 limit 就 return —— 这是那个上限之外的第二道保险。

        断法是**数真实读了几个文件**:只断"返回了 2 条"的话,一个"先全扫完再切片"
        的实现照样绿,而它在线上会读上万个文件。
        """
        读过的: list[str] = []
        真读 = reports_api._read_meta

        def 记账(sidecar: Path) -> Any:
            读过的.append(sidecar.name)
            return 真读(sidecar)

        monkeypatch.setattr(reports_api, "_read_meta", 记账)
        for i in range(6):
            _plant("20260822", f"{i:032x}", f"巡检记录_GYT-20260822-09000{i}.docx")

        _get(client, "?limit=2")

        assert len(读过的) == 2, f"收够就该停,实际读了 {len(读过的)} 份"


class Test鉴权:
    def test_设了令牌但没带钥匙一律拒(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 回执里的编号能直接换出那份 docx(隐患明细 + 现场照片),
        不是公开数据。而 GET 最容易被当成"只是查一下"漏掉鉴权。"""
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()
        try:
            resp = client.get("/reports")

            assert resp.status_code == 401
            assert resp.json()["error_code"] == "UNAUTHORIZED"
        finally:
            get_settings.cache_clear()

    def test_带对钥匙就放行(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()
        try:
            resp = client.get("/reports", headers={"X-Api-Key": REAL_TOKEN})
            assert resp.status_code == 200
        finally:
            get_settings.cache_clear()

    def test_没配令牌时放行(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """空 / 占位符 / 过短 = 未配置 = 放行 —— 「未配置也拦」等于当场打死本机联调。"""
        monkeypatch.delenv("GYT_ACCESS_TOKEN", raising=False)
        get_settings.cache_clear()
        try:
            assert client.get("/reports").status_code == 200
        finally:
            get_settings.cache_clear()


def test_路由挂进了webapp() -> None:
    """webapp.py 是自定义路由唯一的挂载点,漏铺 = 404。

    ⚠️ 而 404 在这条链上是**安静的**:前端对非 2xx 是不出声走开的
    (观测/附属功能坏了不许打扰工友),所以界面上表现为"抽屉永远是空的",
    控制台干净、日志干净。只有这条用例会说话。
    """
    import webapp

    mounted = {route.path for route in webapp.app.routes}
    declared = {route.path for route in reports_api.REPORTS_ROUTES}

    assert declared <= mounted, f"这些路由没挂进 webapp.py:{sorted(declared - mounted)}"
    assert "/reports" in mounted
