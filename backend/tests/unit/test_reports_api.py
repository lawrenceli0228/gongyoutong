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


def _make_report(
    name: str = "巡检记录_GYT-20260822-153012.docx",
    *,
    title: str | None = None,
) -> str:
    """真走 ``artifacts.register`` 登记一份 REPORT 产物,返回 artifact_id。

    **不手搓 sidecar**:手搓的那份是测试自己编的形状,比对了也不说明
    这条端点读得懂 ``register()`` 真正写出来的东西 —— 而那正是唯一要验的事。

    ``title=None`` 时**不传 extra**,模拟 2026-08-25 之前生成的老记录
    (线上现存的那些全都是这样)—— 那条路径才是常态,别只测有标题的。
    """
    extra = {"title": title} if title is not None else None
    return artifacts.register(FAKE_DOCX, kind=ArtifactKind.REPORT, original_name=name, extra=extra)


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


def _plant(
    day: str,
    artifact_id: str,
    original_name: str,
    *,
    kind: str = "REPORT",
    at: str = "09:00:00",
) -> None:
    """往指定日期目录里种一份 sidecar + 正文。

    形状**逐字照 ``core/artifacts.register()`` 写出来的那份**(两边注释互指):
    它哪天改存法,这里种出来的东西就不再代表真库,而用例会照绿。

    ``at``(2026-08-25 加):当天的时刻,进 ``created_at``。默认全是 ``09:00:00`` ——
    那对「跨天」类用例够用,但**测天内顺序时必须逐条给不同的值**:
    几条的 ``created_at`` 完全相等时排序退化成「保持输入顺序」,
    用例会因为 glob 的巧合而绿,不是因为排序真的对。

    ⚠️ **用 ``_make_report`` 测天内顺序是测不出来的**:它走真的 ``artifacts.register``,
    ``created_at`` 取的是注册那一刻的时钟,与文件名里那个编号无关。
    写这组用例时先踩了这个坑 —— 三份按 090000 / 230000 / 120000 的名字注册,
    实际时间序却是注册顺序,断言当场对不上。
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
                "created_at": f"{day[:4]}-{day[4:6]}-{day[6:]}T{at}+00:00",
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

    def test_列出巡检记录_六个键齐全(self, client: TestClient) -> None:
        """少一个键前端就少渲一块,而且不会报错。

        ⚠️ 这条是**契约的同源闸**:模块头注里写着「每条六个键」。
           加键要连它一起改 —— 那是刻意的,别把 `set(行) ==` 改成 `<=`。
        """
        artifact_id = _make_report()

        (行,) = _get(client)["data"]["reports"]

        assert set(行) == {
            "artifact_id",
            "report_no",
            "filename",
            "size_bytes",
            "created_at",
            "title",
        }
        assert 行["artifact_id"] == artifact_id
        assert 行["report_no"] == "GYT-20260822-153012"
        assert 行["filename"] == "巡检记录_GYT-20260822-153012.docx"
        assert 行["size_bytes"] == len(FAKE_DOCX)
        assert 行["created_at"], "没有它前端排不了序、也显示不出「什么时候出的」"

    def test_老记录没有标题时给_null_不许拿编号或文件名顶上(self, client: TestClient) -> None:
        """🔴 2026-08-25 上线时**线上现存 9 份记录全都没有这个键**,所以这条是常态路径。

        在后端编一个出来(比如回 filename 或 report_no),前端就再也分不清
        「用户真给这份起了名」和「我们替他编的」—— 而抽屉的主行显示的就是它。
        默认文种由前端在渲染那一刻兜(`reportHeadline`),那里兜是显示逻辑;
        在这儿兜是**篡改数据**。
        """
        _make_report()  # 不传 title

        (行,) = _get(client)["data"]["reports"]

        assert 行["title"] is None
        assert 行["report_no"] == "GYT-20260822-153012", "编号该有的还得有"

    def test_出记录时起过名的_标题原样回出来(self, client: TestClient) -> None:
        """名字是给人认的:抽屉九行长得一模一样时,这是唯一能分辨的东西。"""
        _make_report(title="海之子驗收測試")

        (行,) = _get(client)["data"]["reports"]

        assert 行["title"] == "海之子驗收測試"

    def test_标题是空白时当成没有_不是回一个空串(self, client: TestClient) -> None:
        """空串和 None 在 JS 里是两种假值,但**在界面上是两种不同的话**:
        `null` → 显示默认文种;`""` → 主行是一片空白,那一行看着像坏了。
        """
        _make_report(title="   ")

        (行,) = _get(client)["data"]["reports"]

        assert 行["title"] is None

    def test_照片不进这个抽屉(self, client: TestClient) -> None:
        """线上绝大多数产物是照片。kind 判据松掉的表现是抽屉里混进一堆
        点开是图片的"巡检记录",而且没有任何报错。"""
        _make_photo()
        _make_photo()
        artifact_id = _make_report()

        reports = _get(client)["data"]["reports"]

        assert [r["artifact_id"] for r in reports] == [artifact_id]

    def test_抠不出编号的整条跳过_不是列出来让编号那格空着(self, client: TestClient) -> None:
        """🔴 **这一条是 2026-08-22 真机照出来的 bug 的钉子。**

        原来的行为是「列出来,编号那格给 None」。而 ``ArtifactKind.REPORT``
        那一档**监理文书也在用**,于是《工程暂停令》一类全部混进了工友的
        「巡檢記錄」抽屉 —— 本机真数据实测 39 份 REPORT 里 **28 份是监理文书**。

        现在判据是两道:kind 对 **且** 文件名里解得出巡检记录号。
        """
        _make_report(name="不知道谁生成的.docx")
        真记录 = _make_report()

        reports = _get(client)["data"]["reports"]

        assert [r["artifact_id"] for r in reports] == [真记录]
        assert all(r["report_no"] for r in reports), "列出来的每一份都必须有编号"

    def test_监理五种文书一份都不许混进巡检记录抽屉(self, client: TestClient) -> None:
        """🔴 判别信号是**编号带不带类型段** —— 本仓早就有并且守着这条:
        六种监理编号都带 ``-ZT-`` / ``-TZ-`` / ``-JS-`` 这样的类型段,
        巡检记录号不带(``REPORT_RECEIPT_PATTERN`` 与 ``SUPERVISION_RECEIPT_PATTERN``
        故意互不匹配就是它)。

        ⚠️ 下面这些文件名是**从本机真实产物里抄出来的**,不是编的 ——
        编的名字证明不了这条判据在真数据上成立(那正是第一版漏掉这个 bug 的原因:
        测试只造过 ``巡检记录_…`` 这一种名字)。
        """
        监理文书 = [
            "监理通知单_GYT-TZ-20260819-134037-70ed.docx",
            "工程暂停令_GYT-ZT-20260819-134037-f539.docx",
            "致建设单位报告_GYT-JS-20260819-134037-27cb.docx",
            "工程复工令_GYT-FG-20260819-140000-1a2b.docx",
            "监理报告_GYT-BG-20260819-140000-3c4d.docx",
        ]
        for name in 监理文书:
            _make_report(name=name)  # 真走 register,kind 与监理那条链一模一样
        真记录 = _make_report()

        reports = _get(client)["data"]["reports"]

        assert [r["artifact_id"] for r in reports] == [真记录], (
            f"监理文书混进来了:{[r['filename'] for r in reports]}"
        )

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
        """🔴 **2026-08-25 改过判据,别照旧版注释理解这条。**

        旧版断的是「同一天内按 sidecar **文件名**倒序」,并在注释里承认那是个
        **近似**(artifact_id 是 uuid4,与时间无关),说「真正的时间序在
        ``created_at`` 里,前端要严格排序就按那个字段排」。

        问题是**前端那半从来没实现过**,而模块头注对外承诺的是
        「按**生成时间倒序**(最近的在最前)」—— 契约比代码给的强。
        线上实测照出来了:8/25 三份显示成 08:30 → 10:52 → 08:58。

        现在服务端自己排(``_by_time_desc``),这条也跟着断真时间序:
        **artifact_id 与顺序刻意反着来** —— 全 f 的那份时间更早,
        真按文件名排的实现会在这里红。
        """
        _plant("20260822", "f" * 32, "巡检记录_GYT-20260822-080000.docx", at="08:00:00")
        _plant("20260822", "0" * 32, "巡检记录_GYT-20260822-180000.docx", at="18:00:00")

        reports = _get(client)["data"]["reports"]

        assert [r["report_no"] for r in reports] == [
            "GYT-20260822-180000",
            "GYT-20260822-080000",
        ]

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

        # 🔴 **2026-08-25 判据从「读满 limit 就停」放宽成「不碰用不着的日期目录」。**
        #
        #   为什么放宽:同一天内的顺序原来是按随机 artifact_id 排的(见 Test排序 的
        #   头注),要给出真的时间序就必须把**当天目录读完再排** —— 边读边判 limit
        #   会在同一天里丢掉更新的那几份,而它们本该排最前。
        #
        #   代价量过再决定的,不是拍脑袋:线上 13 个日期目录、167 份 sidecar、
        #   **单日最多 43 份**。而默认 limit=20,要凑够 20 份巡检记录本来就得翻遍
        #   所有日期目录(记录在照片里是稀疏的)—— 也就是说「天内提前退出」
        #   在生产上几乎从不触发,这次放宽的实际成本接近零。
        #   真正的性能保险仍然是 _MAX_SIDECARS_SCANNED(2000),它一个字没动。
        #
        #   仍然要守住的是**跨天**那一层:够了就不许再去翻更旧的日期目录 ——
        #   那才是「读上万个文件」的来源。下面按这个断。
        assert len(读过的) == 6, f"当天目录该读完再排,实际读了 {len(读过的)} 份"


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


class Test排序:
    """🔴 「按生成时间倒序」这条契约,**2026-08-25 之前只在跨天成立**。

    sidecar 文件名是 ``<artifact_id>.json``,而 artifact_id 是 ``uuid4().hex`` ——
    随机。原来按文件名倒序 = 按一串随机十六进制倒序,同一天内顺序是乱的。
    线上实测(改之前)8/25 三份显示成 08:30 → 10:52 → 08:58。

    ⚠️ 这条 bug 活了这么久是因为「大部分时候看着没问题」(跨天那部分一直是对的),
       而且在抽屉主行改成显示标题之前,每行长得一模一样,没人会去核对顺序。
    """

    def test_同一天内按时间倒序_不是按随机的产物编号(self, client: TestClient) -> None:
        # 三份同一天,**artifact_id 的字典序与时间序刻意相反**:
        #   文件名序(升)  000…09:00 → aaa…23:00 → fff…12:00
        #   时间序(降)    23:00 → 12:00 → 09:00
        # 真按文件名排的实现在这里必红。
        _plant("20260822", "0" * 32, "巡检记录_GYT-20260822-090000.docx", at="09:00:00")
        _plant("20260822", "a" * 32, "巡检记录_GYT-20260822-230000.docx", at="23:00:00")
        _plant("20260822", "f" * 32, "巡检记录_GYT-20260822-120000.docx", at="12:00:00")

        编号 = [r["report_no"] for r in _get(client)["data"]["reports"]]

        assert 编号 == [
            "GYT-20260822-230000",
            "GYT-20260822-120000",
            "GYT-20260822-090000",
        ], "同一天内没有按时间倒序 —— 最近那份该在最前"

    def test_缺_created_at_的排最后_而不是让整个抽屉打不开(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一份缺字段的 sidecar 不该炸掉整个列表(同 _read_meta 的取舍)。"""
        好的 = "1" * 32
        坏的 = "2" * 32
        _plant("20260822", 好的, "巡检记录_GYT-20260822-120000.docx", at="12:00:00")
        _plant("20260822", 坏的, "巡检记录_GYT-20260822-230000.docx", at="23:00:00")

        # 把「坏的」那份的 created_at 抹掉(它本该因为 23:00 排最前)
        (sidecar,) = list(get_settings().artifacts_dir.glob(f"*/{坏的}.json"))
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        del meta["created_at"]
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        reports = _get(client)["data"]["reports"]

        assert len(reports) == 2, "缺字段的那份该还在列表里,不是被丢掉"
        assert reports[0]["artifact_id"] == 好的, "有时间的排前面"
        assert reports[-1]["artifact_id"] == 坏的, "没时间的垫底"

    def test_limit_截断发生在排序之后_不是之前(self, client: TestClient) -> None:
        """🔴 这条钉的是修法本身。

        原来的写法是「边扫边收,够 N 条就 return」—— 而目录内顺序随机,
        那样会在同一天里**丢掉更新的那几份**,而它们本该排最前。
        改成「整天收完再排再截」。这里三份同天、只要一份,拿到的必须是最新那份。
        """
        # 最新那份的 artifact_id 排在文件名序的**中间**:
        # 「先收够 1 条就停」会拿到 000…(09:00),而正确答案是 aaa…(23:00)。
        _plant("20260822", "0" * 32, "巡检记录_GYT-20260822-090000.docx", at="09:00:00")
        _plant("20260822", "a" * 32, "巡检记录_GYT-20260822-230000.docx", at="23:00:00")
        _plant("20260822", "f" * 32, "巡检记录_GYT-20260822-120000.docx", at="12:00:00")

        reports = _get(client, "?limit=1")["data"]["reports"]

        assert [r["report_no"] for r in reports] == ["GYT-20260822-230000"]


class Test跨天不多读:
    """够了就不许再翻更旧的日期目录 —— 那才是「线上读上万个文件」的来源。

    (天内那一层 2026-08-25 起是读完再排的,理由与代价见
     ``test_凑够了就停_不再往下扫`` 里那段。)
    """

    def test_够了就不碰更旧的日期目录(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        读过的: list[str] = []
        真读 = reports_api._read_meta

        def 记账(sidecar: Path) -> Any:
            读过的.append(sidecar.parent.name)
            return 真读(sidecar)

        monkeypatch.setattr(reports_api, "_read_meta", 记账)

        _plant("20260822", "a" * 32, "巡检记录_GYT-20260822-090000.docx")
        for i in range(20):
            _plant("20260801", f"{i:032x}", f"巡检记录_GYT-20260801-0900{i:02d}.docx")

        reports = _get(client, "?limit=1")["data"]["reports"]

        assert [r["report_no"] for r in reports] == ["GYT-20260822-090000"]
        assert set(读过的) == {"20260822"}, f"只该读最新那个日期目录,实际读了 {sorted(set(读过的))}"
