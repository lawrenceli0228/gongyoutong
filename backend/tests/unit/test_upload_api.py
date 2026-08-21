"""upload_api.py(聊天附件直传接口)的单元测试 —— 不联网、不起图,真往磁盘写文件。

被测物只有一条端点 ``POST /attachments?name=<URL 编码的原文件名>``,请求体是**原始字节**
(不是 multipart、不是 base64)。契约的唯一真相在 ``upload_api.py`` 的模块头注,
分类判据的唯一真相在 ``core/uploads.classify_attachment``。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):``GYT_DATA_DIR`` 指到
``tmp_path/data``,每个用例一个空的产物目录 —— 所以本文件**真落盘**,落的是本用例
独占的临时目录,不需要自建环境夹具。也**不许**把 ``artifacts.register`` mock 掉:
"到底落没落盘"正是这条端点最该被验的一件事,mock 掉等于把它测没了。

这层要钉死的静默错误(照例,全是"不报错但结果错"那一类):

  · **令牌漏配时裸奔** —— ``langgraph.json`` 的 ``enable_custom_route_auth`` 漏了
    没有任何报错(第一道鉴权整条消失、站点照开),只有 handler 自查这一道兜得住。
    🔴 这条端点比另外三条更要紧:**它会往磁盘写文件**。没钥匙的人能往这台机器上
    写 100 MB 一份的东西,那不是"少一道防线",那是一个免费的填盘器。
  · **鉴权排在读请求体之后** —— 「401」与「401 但那 100 MB 已经收进内存了」在回执上
    长得一模一样,只有"先拒还是先读"这个顺序分得开。
  · **「这张不能用」被做成 4xx** —— 那几句给工友看的话**只有一份真相,在
    ``core/uploads.py``**。端点回 4xx 的话前端只剩一个数字,人话就没了;而且
    ``lang-lib.ts`` 的 ``userTypedText()`` 靠数那几句里的简体字判语种
    (``_PDF_HINT`` 一句就投 28 张票),把拼装挪到前端会让那条判据**静默失效**。
  · **硬上限与业务上限混成一条** —— 一张 30 MB 的照片(超了 photo_max_mb、没超硬上限)
    该被**收下来**再如实回 ``photo_too_large``,不是被 413 掉。混了之后工友只看到
    一个数字,而"压缩一下再传"那句指路就没了。
  · **回了编号但盘上没有 / 落了盘但编号回不去** —— 两边各自都不报错,而下一跳
    (safety 按照片编号取图、cad 按图纸编号取图)会说"找不到",工友明明刚传过。
  · **落盘扩展名被剥成空串** —— ``artifacts._safe_ext`` 只收白名单后缀,拿不到就
    落一个没有后缀的文件;而 safety / cad 都按后缀判格式,于是变成一条
    「上传成功但永远看不了」的静默死路。
  · **中文文件名在查询串上往返坏掉** —— 表现是图纸拿不到 ``.dxf``(同上一条),
    或者干脆被判成"格式不支持",而工友传的就是 .dxf。
  · **500 时裸拼 dict / 把异常原文回显** —— 前端那段解信封的代码是四条直连接口共用的,
    这里多一个特例就得多判一次。

⚠️ **这条端点不看魔数,只看 MIME 与文件名后缀**,与 ``/supervision/photo`` 刻意不同:
   那条收的照片是**销项证据**(复查合格能把隐患关掉),所以必须按魔数认;这条收的
   附件下一跳是识图/查图,判据整条抄自 ``_decode_image_part`` / ``_decode_dxf_part``——
   两条路必须给出同一个结论,单方面在这儿加一道魔数闸就是把两条路弄漂了。
   所以下面那几个样本长什么样并不影响分类,写得像真文件只是为了读起来不误导。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gyt import upload_api
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from gyt.core.uploads import (
    ATTACHMENT_OUTCOMES,
    OUTCOME_DRAWING,
    OUTCOME_DRAWING_TOO_LARGE,
    OUTCOME_PDF,
    OUTCOME_PHOTO,
    OUTCOME_PHOTO_TOO_LARGE,
    OUTCOME_UNSUPPORTED,
)

REAL_TOKEN: Final[str] = "gyt-upload-token-0123456789abcdef"
OTHER_TOKEN: Final[str] = "gyt-upload-token-fedcba9876543210"
"""两个"像真的"令牌:≥24 位、不是占位符开头 —— 两条都满足才会真正开启鉴权
(判据在 core/access.py,与 auth.py / checkin_api / supervision_api / timing_api 同源)。
要两个是因为「换了口令当场生效」那条用例得把旧的换掉。"""

SHORT_TOKEN: Final[str] = "gyt-123"
PLACEHOLDER_TOKEN: Final[str] = "替换成openssl_rand_hex_32的输出"
"""两种"设了等于没设"的写法。占位符那条尤其要盯:``.env.vps.example`` 里那行
是非空中文串,compose 的 fail-closed 只认空 —— 当成真令牌的话第二道锁归零,
而全流程零信号。"""

ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"ok", "data", "user_msg", "error_code"})
"""Envelope 的四个键。**每个出口都要比一遍**:裸拼一个 dict 不会报错,
只会让前端那段共用的解信封代码多一个特例。"""

ARTIFACT_ID_SHAPE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
"""编号的长相。与 ``core/uploads._ARTIFACT_ID_RE`` 是同一条判据的两处写法:
那边收编号(客户端发上来的),这边验编号(这条端点发下去的)。
对不上的表现是**直传回来的编号被下一步判成非法**,而两边各自都绿。"""

开发者词汇: Final[tuple[str, ...]] = (
    "参数",
    "MIME",
    "mime",
    "Content-Type",
    "content-type",
    "字节",
    "请求体",
    "上限",
    "artifact",
    "base64",
    "hex",
)
"""面向用户的字符串里**一个都不许出现**的词。

本仓的硬规矩:``user_msg`` 是给工地师傅看的人话,内部原因只走 ``detail=``
(而 ``fail()`` 保证 detail 只进日志、不进返回值)。这条端点的两句人话
(``_TOO_LARGE_MSG`` / ``_UPLOAD_FAILED_MSG``)都得过这张表。"""

# --- 样本字节 -----------------------------------------------------------------
# 分类只看 MIME 与文件名(见模块头注那条 ⚠️),这些字节的内容不参与判定。

FAKE_JPEG: Final[bytes] = b"\xff\xd8\xff\xe0fake-photo-bytes"
FAKE_PNG: Final[bytes] = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
FAKE_WEBP: Final[bytes] = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 fake"
FAKE_DXF: Final[bytes] = b"  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n  0\nEOF\n"
FAKE_PDF: Final[bytes] = b"%PDF-1.4 fake"
FAKE_GIF: Final[bytes] = b"GIF89a fake"

CN_DXF_NAME: Final[str] = "首层平面图.dxf"
"""带中文的图纸名。它要经过**查询串**往返一趟(URL 编码),而这正是这条端点
与打卡那条链的分岔:打卡的姓名走自定义头 + Base64URL,这里走标准的查询串编码。"""

# --- 上限档位 -----------------------------------------------------------------
# 硬上限 = max(photo_max_mb, drawing_max_mb),所以"超了业务上限"与"超了硬上限"
# 这两件事只能靠**两条上限一大一小**才分得开。下面两档就是为此存在的。

TINY_LIMIT_MB: Final[str] = "0.0001"
"""≈104 字节。"""

SMALL_LIMIT_MB: Final[str] = "0.001"
"""≈1048 字节。与 TINY 的比例(1:10)刻意照抄生产的 photo 10 MB : drawing 100 MB。"""

OVER_TINY: Final[bytes] = b"x" * 512
"""超 TINY、没超 SMALL —— "超了自己的业务上限,但还没到硬上限"那一档。"""

OVER_HARD: Final[bytes] = b"x" * 2048
"""两档都超 —— 只有它该拿到 413。"""


@pytest.fixture
def client() -> TestClient:
    """真 Starlette 栈,只铺本模块导出的那条路由。

    照 ``timing_api`` / ``supervision_api`` 那两组的先例现搭一个壳:线上走 webapp.py
    (挂载另有用例盯着),这里不把整个 webapp 拖进来,免得一条上传端点的用例被
    ezdxf / langchain 的导入拖慢。
    """
    return TestClient(Starlette(routes=list(upload_api.UPLOAD_ROUTES)))


def _传(
    client: TestClient,
    payload: bytes,
    *,
    name: str | None = None,
    mime: str | None = None,
    api_key: str | None = None,
) -> httpx.Response:
    """打一次直传。``content=`` 发的是**原始字节**。

    三个 ``None`` 各有各的意思,别合并:
      · ``name=None`` —— **不发 name 这个查询参数**(传照片时前端就是这样);
        ``name=""`` 是"发了个空的",两者在 handler 里同义,但只有前者是真会发的形状。
      · ``mime=None`` —— **一个 Content-Type 头都不发**。httpx 对 ``content=bytes``
        默认不加这个头,而这正好是浏览器给 .dxf 时的常见形态(空串或 octet-stream)。
      · ``api_key=None`` —— 不发 ``X-Api-Key``,即"没带钥匙的人"。
    """
    query = f"?{urlencode({'name': name})}" if name is not None else ""
    headers: dict[str, str] = {}
    if mime is not None:
        headers["Content-Type"] = mime
    if api_key is not None:
        headers["X-Api-Key"] = api_key
    return client.post(f"/attachments{query}", content=payload, headers=headers)


def _设令牌(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """把访问令牌换成 ``token`` 并让 Settings 立刻重读(``get_settings`` 带 lru_cache)。

    漏掉 ``cache_clear()`` 的表现是这条用例拿到上一条的 Settings —— 鉴权用例会
    整组绿得莫名其妙。
    """
    monkeypatch.setenv("GYT_ACCESS_TOKEN", token)
    get_settings.cache_clear()


def _压上限(monkeypatch: pytest.MonkeyPatch, *, photo_mb: str, drawing_mb: str) -> None:
    """把两条业务上限压到几百字节,免得真造一个 100 MB 的请求体。

    ⚠️ **两条都得给**:硬上限是 ``max(photo, drawing)``,只压一条的话"超了自己的
    业务上限"与"超了硬上限"就分不开,而它们的正确出口完全不同(200 + 一个 outcome
    vs 413)。顺带,"环境变量改得到它"本身就是断言的一半 —— 改不到说明有人把上限写死了。
    """
    monkeypatch.setenv("GYT_PHOTO_MAX_MB", photo_mb)
    monkeypatch.setenv("GYT_DRAWING_MAX_MB", drawing_mb)
    get_settings.cache_clear()


def _产物文件() -> set[Path]:
    """产物目录里现有的**全部**文件(正文 blob + sidecar)。

    "被拒 / 不收下"的几条用例拿它前后对比:没收下的附件**一个字节都不许落盘**。
    只断状态码是不够的 —— 「拒了」和「拒了但也把那份垃圾存下来了」在回执上长得
    一模一样,而后者会让产物目录被随手一传就撑大,且没有任何报错。

    ⚠️ 断"集合相等"而不是"数量没变":同一秒里另一份文件恰好被删又被建的话,
    数量能对上而内容已经变了。
    """
    return {p for p in get_settings().artifacts_dir.rglob("*") if p.is_file()}


def _结局(resp: httpx.Response) -> str:
    return str(resp.json()["data"]["outcome"])


# ---------------------------------------------------------------------------
# 鉴权(纵深防御第二道):判定与 auth.py / 另外三条直连接口同源
# ---------------------------------------------------------------------------


class Test令牌自查:
    """上线闸①的直传版本。

    第一道在 langgraph 的鉴权中间件(``langgraph.json`` 的
    ``enable_custom_route_auth``),那个键漏配时**整条消失且没有任何报错** ——
    只有这道自查兜得住,也只有它能被单元测试钉死。
    """

    def test_设了令牌但没带钥匙一律拒(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 不拦 = 给任何路过的人一个**往这台机器写文件**的入口。"""
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)
        之前 = _产物文件()

        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg")

        # Assert
        assert resp.status_code == 401
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS, "被拒的响应同样得是信封,不许裸拼 dict"
        assert body["ok"] is False
        assert body["data"] is None
        assert body["error_code"] == "UNAUTHORIZED"
        assert _产物文件() == 之前, "被拒的请求不许在盘上留下任何东西"

    def test_带错令牌也拒(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """只判"有没有带钥匙"是不够的:带一把错的照样非空。"""
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)

        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg", api_key=OTHER_TOKEN)

        # Assert
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"

    def test_带对令牌就放行到业务层(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """放行的证据是它开始回业务结果(收下这张图、给一个编号),而不是 401。"""
        # Arrange
        _设令牌(monkeypatch, REAL_TOKEN)

        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg", api_key=REAL_TOKEN)

        # Assert
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert _结局(resp) == OUTCOME_PHOTO

    def test_没配令牌时不拦(self, client: TestClient) -> None:
        """conftest 已把 GYT_* 清干净,这就是"没配"的真实状态。

        ``make dev`` 与真机验收都不发令牌头,「未配置也拦」等于当场打死本机联调 ——
        这条取舍与 auth.py / checkin_api / supervision_api / timing_api 一字不差。
        """
        assert _传(client, FAKE_JPEG, mime="image/jpeg").status_code == 200

    @pytest.mark.parametrize(
        ("说法", "token"),
        [("占位符", PLACEHOLDER_TOKEN), ("过短", SHORT_TOKEN)],
    )
    def test_设了等于没设的两种写法一律放行(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, 说法: str, token: str
    ) -> None:
        """判定必须与 auth.py 完全同源。两边分头判的话会出现"对话链开门、上传锁死"
        这种半开状态 —— 最难查的一种,因为每一半单看都是对的。"""
        # Arrange
        _设令牌(monkeypatch, token)

        # Act & Assert
        assert _传(client, FAKE_JPEG, mime="image/jpeg").status_code == 200, (
            f"{说法}令牌应当被视同没配"
        )

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
        没带钥匙 = _传(client, FAKE_JPEG, mime="image/jpeg")

        # Act:换锁,再拿新旧两把钥匙各敲一次
        _设令牌(monkeypatch, OTHER_TOKEN)
        旧钥匙 = _传(client, FAKE_JPEG, mime="image/jpeg", api_key=REAL_TOKEN)
        新钥匙 = _传(client, FAKE_JPEG, mime="image/jpeg", api_key=OTHER_TOKEN)

        # Assert
        assert 没带钥匙.status_code == 401
        assert 旧钥匙.status_code == 401, "换了口令之后旧令牌还能进 = 令牌被读成了常量"
        assert 新钥匙.status_code == 200

    def test_令牌自查排在读请求体之前_超大body也先401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 顺序这条在**这条端点**上比别处都要紧,因为读体就是把最多 100 MB 收进内存,
        而收完还要往磁盘写。

        构造的是"超大 body + 坏令牌":先验令牌就是 401,先读体就会变成 413 ——
        两个状态码正好把顺序区分开。这也是唯一能从外面看见这个顺序的办法
        (与 ``supervision_api._serve`` 那条同一个手法)。
        """
        # Arrange:把硬上限压到 ~104 字节,再发一个 2048 字节的体
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=TINY_LIMIT_MB)
        _设令牌(monkeypatch, REAL_TOKEN)
        之前 = _产物文件()

        # Act
        resp = _传(client, OVER_HARD, mime="image/jpeg", api_key=OTHER_TOKEN)

        # Assert
        assert resp.status_code == 401, "回了 413 = 先读体再验令牌,顺序反了"
        assert resp.json()["error_code"] == "UNAUTHORIZED"
        assert _产物文件() == 之前


# ---------------------------------------------------------------------------
# 分类:六个结局,唯一真相是 core/uploads.ATTACHMENT_OUTCOMES
# ---------------------------------------------------------------------------


class Test分类:
    @pytest.mark.parametrize(
        ("样本", "mime", "扩展名"),
        [
            (FAKE_JPEG, "image/jpeg", ".jpg"),
            (FAKE_PNG, "image/png", ".png"),
            (FAKE_WEBP, "image/webp", ".webp"),
        ],
        ids=["jpeg", "png", "webp"],
    )
    def test_三种图片都收下且真落了盘(
        self, client: TestClient, 样本: bytes, mime: str, 扩展名: str
    ) -> None:
        """快乐路径,把回执与磁盘两头一起钉住。

        只断"回了个 32 位串"是不够的:**编号回来了而盘上没有**(或者落在另一个
        名字上)的表现是下一跳说"找不到那张照片",而工友明明刚传过 —— 两边各自都绿。
        扩展名同理:剥成空串的话文件没有后缀,而 safety 按后缀判格式。
        """
        # Act
        resp = _传(client, 样本, mime=mime)

        # Assert
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["ok"] is True
        assert body["error_code"] is None
        assert _结局(resp) == OUTCOME_PHOTO

        编号 = body["data"]["artifact_id"]
        assert ARTIFACT_ID_SHAPE.fullmatch(编号), f"编号不是 32 位小写 hex:{编号!r}"
        assert artifacts.resolve(编号).read_bytes() == 样本, "落盘的不是这份字节"
        assert artifacts.read_meta(编号)["kind"] == ArtifactKind.PHOTO.value
        assert artifacts.resolve(编号).suffix == 扩展名, "没有后缀的照片,识图那步认不出格式"

    def test_照片的原文件名不进产物_落盘名恒是upload点扩展名(self, client: TestClient) -> None:
        """照片这一档**刻意丢掉原名**(``classify_attachment`` 回的是 ``upload{ext}``)——
        与老路 ``_decode_image_part`` 同一姿势(它也是 ``f"upload{ext}"``)。

        写成用例是为了让下一个人看见这是**决定**而不是遗漏:顺手"把原名传下去"的话,
        同一张照片走两条路会在下载卡上显示两个名字,而没有任何东西会说话。
        ⚠️ 图纸那一档相反 —— 原名要留(见下面中文文件名那条)。
        """
        # Act
        resp = _传(client, FAKE_JPEG, name="工地实拍-东南角.jpg", mime="image/jpeg")

        # Assert
        编号 = resp.json()["data"]["artifact_id"]
        assert artifacts.read_meta(编号)["original_name"] == "upload.jpg"

    @pytest.mark.parametrize(
        ("说法", "mime"),
        [
            ("一个头都不发", None),
            ("发了个空串", ""),
            ("给了 octet-stream", "application/octet-stream"),
        ],
    )
    def test_DXF按文件名认_浏览器给什么类型都收(
        self, client: TestClient, 说法: str, mime: str | None
    ) -> None:
        """🔴 **一律按 .dxf 后缀认,不信 MIME** —— 浏览器给 .dxf 的类型极不稳定
        (常是空串或 application/octet-stream,偶尔才 image/vnd.dxf)。

        三种写法各一条:不认的话工友传的图纸会被判成"格式不支持",而他传的就是 .dxf,
        整条图纸演示路径当场没有入口。
        """
        # Act
        resp = _传(client, FAKE_DXF, name=CN_DXF_NAME, mime=mime)

        # Assert
        assert resp.status_code == 200, f"{说法}时 DXF 没被认出来:{resp.text}"
        assert _结局(resp) == OUTCOME_DRAWING
        编号 = resp.json()["data"]["artifact_id"]
        assert ARTIFACT_ID_SHAPE.fullmatch(编号)
        assert artifacts.read_meta(编号)["kind"] == ArtifactKind.DRAWING.value
        assert artifacts.resolve(编号).read_bytes() == FAKE_DXF

    def test_中文图纸名在查询串上原样往返(self, client: TestClient) -> None:
        """名字要经过 URL 编码往返一趟,两头必须逐字相同。

        坏掉有两种表现,都不报错:① 解成乱码 → ``_safe_ext`` 取不到 ``.dxf``,
        文件落盘没有后缀,而 cad 按后缀判格式 —— 一条「传上去了但永远打不开」的死路;
        ② 后缀先掉了 → 压根判不成图纸,工友被告知"格式不支持"。
        """
        # Act
        resp = _传(client, FAKE_DXF, name=CN_DXF_NAME)

        # Assert
        编号 = resp.json()["data"]["artifact_id"]
        assert artifacts.read_meta(编号)["original_name"] == CN_DXF_NAME
        assert artifacts.resolve(编号).suffix == ".dxf"

    def test_没给文件名但类型里带dxf_落盘名兜底成upload点dxf(self, client: TestClient) -> None:
        """兜底那一支(``classify_attachment`` 里 ``"upload.dxf"`` 那个分支)。

        没有它的话落盘名是空的 → ``_safe_ext`` 拿不到后缀 → 又是一条
        「上传成功但永远看不了」的静默死路。判据与 ``_decode_dxf_part`` 的兜底同源。
        """
        # Act
        resp = _传(client, FAKE_DXF, mime="image/vnd.dxf")

        # Assert
        assert _结局(resp) == OUTCOME_DRAWING
        编号 = resp.json()["data"]["artifact_id"]
        assert artifacts.read_meta(编号)["original_name"] == "upload.dxf"
        assert artifacts.resolve(编号).suffix == ".dxf"

    def test_PDF不收下但也不报错(self, client: TestClient) -> None:
        """上传按钮上明写着「Upload PDF or Image」,所以工友真的会传 PDF。

        它得拿到一个**准确**的结局(``pdf``,后面由 ``_rewrite`` 拼成"去资料归档面板"
        那句),而不是被归进"格式不支持" —— 说「请转成 JPG」是错的,传 PDF 本来
        就是这个按钮允许的操作,那句话会让他以为自己搞错了。
        """
        # Arrange
        之前 = _产物文件()

        # Act
        resp = _传(client, FAKE_PDF, name="规范.pdf", mime="application/pdf")

        # Assert
        assert resp.status_code == 200
        assert _结局(resp) == OUTCOME_PDF
        assert resp.json()["data"]["artifact_id"] is None, "没收下就不该有编号"
        assert _产物文件() == 之前, "没收下的附件不许落盘"

    def test_不认识的图片格式归格式不支持(self, client: TestClient) -> None:
        """GIF / BMP / AVIF / SVG 这些白名单外的格式。**不许静默丢掉** ——
        丢了的话工友传了东西、对话里一个字都没提,他会以为传上去了然后等一个
        永远不来的答复。"""
        # Arrange
        之前 = _产物文件()

        # Act
        resp = _传(client, FAKE_GIF, name="动图.gif", mime="image/gif")

        # Assert
        assert resp.status_code == 200
        assert _结局(resp) == OUTCOME_UNSUPPORTED
        assert resp.json()["data"]["artifact_id"] is None
        assert _产物文件() == 之前

    def test_超上限的图纸也是200加一个结局(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **这里的上限配置是刻意反过来的**(图纸上限比照片小)——
        这样"超了图纸上限、但没超硬上限"这一档一定成立,与配置无关。

        > 🔴 **2026-08-21 补记:这条 docstring 原来写的是「这个结局在生产配置下
        > 根本产不出来」,那是当时的事实,现在已经不成立了。**
        >
        > 当时硬上限 = ``max(照片, 图纸)`` = 图纸那条,于是一张超过图纸上限的 DXF
        > **必然先撞 413** —— 而 413 只能给一句格式无关的话,说不出「这是图纸,
        > 先精简一下」。也就是说工友传一张 120 MB 的图纸,拿到的指路是错的。
        >
        > 修法是给硬上限留 25% 余量(``upload_api._HARD_LIMIT_HEADROOM``),
        > 现在生产配置下也够得着了 —— 下一条用例专门盯这件事。
        """
        # Arrange
        _压上限(monkeypatch, photo_mb=SMALL_LIMIT_MB, drawing_mb=TINY_LIMIT_MB)
        之前 = _产物文件()

        # Act
        resp = _传(client, OVER_TINY, name=CN_DXF_NAME)

        # Assert
        assert resp.status_code == 200
        assert _结局(resp) == OUTCOME_DRAWING_TOO_LARGE
        assert resp.json()["data"]["artifact_id"] is None
        assert _产物文件() == 之前

    def test_图纸上限最大时超限的图纸仍然拿得到对症的那句话(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **这条盯的是 `_HARD_LIMIT_HEADROOM` 那 25% 余量,别把它删了。**

        生产配置是照片 10 MB / 图纸 100 MB —— **图纸那条就是两条里最大的**,
        所以硬上限如果等于 ``max(两条)``,一张 110 MB 的图纸会先撞 413,
        而 413 那句是格式无关的,说不出「这是图纸,先精简一下」。
        工友拿到的会是一句对图纸没用的指路。

        这里把配置摆成同样的形状(**图纸是较大的那条**),再发一份
        「超了图纸上限、但还在 125% 以内」的字节:必须是 **200 + drawing_too_large**,
        不是 413。余量被去掉的话这条当场红。
        """
        # Arrange:图纸上限 = 较大的那条(与生产同形),照片上限更小
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=SMALL_LIMIT_MB)
        之前 = _产物文件()
        图纸上限字节 = int(float(SMALL_LIMIT_MB) * 1024 * 1024)
        # 超了业务上限、但没超 125% 的硬上限
        刚好超一点 = b"x" * (图纸上限字节 + 1)
        assert len(刚好超一点) < int(图纸上限字节 * 1.25), "样本造错了:它该落在余量区间里"

        # Act
        resp = _传(client, 刚好超一点, name=CN_DXF_NAME)

        # Assert
        assert resp.status_code == 200, "撞了 413 = 那 25% 余量没了,对症的话说不出来"
        assert _结局(resp) == OUTCOME_DRAWING_TOO_LARGE
        assert _产物文件() == 之前, "没收下就不该落盘"

    @pytest.mark.parametrize(
        ("说法", "样本", "name", "mime"),
        [
            ("PDF", FAKE_PDF, "规范.pdf", "application/pdf"),
            ("格式不支持", FAKE_GIF, "动图.gif", "image/gif"),
            ("照片太大", OVER_TINY, None, "image/jpeg"),
            ("图纸太大", OVER_TINY, CN_DXF_NAME, None),
        ],
    )
    def test_这张不能用一律200而不是4xx(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        说法: str,
        样本: bytes,
        name: str | None,
        mime: str | None,
    ) -> None:
        """🔴 **「这张附件不能用」不是 HTTP 错误。**

        理由有两条,都写在 ``upload_api.py`` 的头注里:
          ① 那几句给工友看的话**只有一份真相,在 ``core/uploads.py``**。回 4xx 的话
             前端手上只剩一个数字,要么什么都不说、要么自己抄一份文案 —— 抄了就是
             第二份真相,改文案时两边各绿各的;
          ② ``lang-lib.ts`` 的 ``userTypedText()`` 靠数那几句里的简体字判语种
             (``_PDF_HINT`` 一句就投 28 张票)。拼装一旦挪到前端,那条判据
             **静默失效**,港人打的字会被判成简体票。

        所以这四种结局全部走 200 + 一个 outcome。顺带钉住 ``user_msg`` 必须是**空的**:
        在这儿写一句提示 = 上面那两条同时破。
        """
        # Arrange:两档上限一大一小,让"太大"那两条够得着(理由见 _压上限)
        _压上限(
            monkeypatch,
            photo_mb=TINY_LIMIT_MB if 说法 == "照片太大" else SMALL_LIMIT_MB,
            drawing_mb=TINY_LIMIT_MB if 说法 == "图纸太大" else SMALL_LIMIT_MB,
        )

        # Act
        resp = _传(client, 样本, name=name, mime=mime)

        # Assert
        assert resp.status_code == 200, f"{说法}被做成了 HTTP 错误:{resp.text}"
        body = resp.json()
        assert body["ok"] is True, f"{说法}是一个正常的结局,不是一次失败的调用"
        assert body["error_code"] is None
        assert body["user_msg"] == "", f"{说法}的人话不许在这儿写第二份(唯一真相在 uploads.py)"
        assert body["data"]["artifact_id"] is None

    def test_回执的data形状是冻结的两个键(self, client: TestClient) -> None:
        """``{outcome, artifact_id}`` 两个键,多一个少一个前端都要多判一次。
        少的表现是界面上一次上传毫无反应,多的表现是前端把不该有的东西读进消息块。"""
        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg")

        # Assert
        assert set(resp.json()["data"]) == {"outcome", "artifact_id"}

    def test_六种结局都产得出来而且没有第七种(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 ``ATTACHMENT_OUTCOMES`` 是这条端点与前端之间的**受控词表**,
        唯一真相在 ``core/uploads.py``,前端(消息块的 ``outcome``)镜像它。

        这条把"六个值真的都是这条端点产得出来的"变成可执行的对账:加了第七个值
        而端点产不出来,它当场红 —— 而漏掉的表现是那种附件被 ``_rewrite`` 静默
        归进"格式不支持",工友传了张好照片却被告知请转成 JPG。

        ⚠️ 为什么要**两档上限**:``photo_too_large`` 要求 photo 上限 < drawing 上限,
        ``drawing_too_large`` 要求反过来 —— 一套配置下这两个结局不可能同时够得着
        (硬上限恒等于两者里大的那个)。这不是测试写别扭了,是那条判据本身的形状。
        """
        # Arrange & Act:甲档 = 生产的方向(照片上限小),够得着 photo_too_large
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=SMALL_LIMIT_MB)
        产出 = {
            _结局(_传(client, FAKE_JPEG, mime="image/jpeg")),
            _结局(_传(client, FAKE_DXF, name=CN_DXF_NAME)),
            _结局(_传(client, FAKE_PDF, mime="application/pdf")),
            _结局(_传(client, FAKE_GIF, mime="image/gif")),
            _结局(_传(client, OVER_TINY, mime="image/jpeg")),
        }
        # 乙档 = 反方向,只有它够得着 drawing_too_large(理由见上面那条 ⚠️)
        _压上限(monkeypatch, photo_mb=SMALL_LIMIT_MB, drawing_mb=TINY_LIMIT_MB)
        产出.add(_结局(_传(client, OVER_TINY, name=CN_DXF_NAME)))

        # Assert
        assert 产出 == set(ATTACHMENT_OUTCOMES)


# ---------------------------------------------------------------------------
# 硬上限:只管"别把内存撑爆",不管"这张能不能用"
# ---------------------------------------------------------------------------


class Test硬上限:
    def test_超硬上限当场413且盘上不留东西(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """流式闸:``Request.stream()`` 边收边数,超了当场断,不会先把 500 MB 攒进内存。

        被拒的响应**仍然是信封** —— 前端那段解信封的代码是四条直连接口共用的。
        """
        # Arrange
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=TINY_LIMIT_MB)
        之前 = _产物文件()

        # Act
        resp = _传(client, OVER_HARD, mime="image/jpeg")

        # Assert
        assert resp.status_code == 413
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["ok"] is False
        assert body["data"] is None
        assert body["error_code"] == "FILE_TOO_LARGE"
        assert _产物文件() == 之前, "超硬上限的东西一个字节都不许落盘"

    def test_超业务上限的照片是200加一个结局_不是413(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **这条与上一条的边界,是这条端点最容易被做错的地方。**

        一张 30 MB 的照片(超了 photo_max_mb=10、没超硬上限=100)该被**收下来**,
        然后如实回 ``photo_too_large`` —— 由 ``_rewrite`` 拼成
        「(你传的照片太大了,压缩一下或者截个图再传一次)」。
        413 到了前端只剩一个数字,那句指路的人话就没了。

        用例把 10 / 100 这个比例原样缩到 104 / 1048 字节来演,判据一模一样。
        """
        # Arrange
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=SMALL_LIMIT_MB)
        之前 = _产物文件()

        # Act
        resp = _传(client, OVER_TINY, mime="image/jpeg")

        # Assert
        assert resp.status_code == 200, "被 413 掉了 —— 两道闸混成了一条"
        assert _结局(resp) == OUTCOME_PHOTO_TOO_LARGE
        assert resp.json()["data"]["artifact_id"] is None
        assert _产物文件() == 之前

    def test_硬上限取的是两条里大的那条(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_hard_limit_bytes`` 是 ``max(photo, drawing)``,不是 photo。

        取错成 photo 的话,生产上一张 20 MB 的**图纸**(它自己的上限是 100 MB)
        会被 413 掉 —— 而且报的是"文件太大"这种查不出方向的话,人会去翻图纸上限,
        那儿写着 100,一切看起来都对。
        """
        # Arrange:照片上限 104 字节、图纸上限 1048 字节,发一个 512 字节的图纸
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=SMALL_LIMIT_MB)

        # Act
        resp = _传(client, OVER_TINY, name=CN_DXF_NAME)

        # Assert
        assert resp.status_code == 200
        assert _结局(resp) == OUTCOME_DRAWING

    def test_上限每次请求现取_改了当场生效(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_hard_limit_bytes`` 不许被读成模块常量。

        读成常量的表现与令牌那条同款:测试里换环境变量测不到(整组变成假绿灯),
        线上改上限要重启才生效,而改上限的人不会想到这一层。
        """
        # Arrange & Act:同一个进程、同一个 client,把上限从 ~104 放到 ~1048
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=TINY_LIMIT_MB)
        紧 = _传(client, OVER_TINY, mime="image/jpeg")
        _压上限(monkeypatch, photo_mb=SMALL_LIMIT_MB, drawing_mb=SMALL_LIMIT_MB)
        松 = _传(client, OVER_TINY, mime="image/jpeg")

        # Assert
        assert 紧.status_code == 413
        assert 松.status_code == 200, "放宽上限之后还 413 = 上限被读成了常量"


# ---------------------------------------------------------------------------
# 兜底与人话
# ---------------------------------------------------------------------------


class Test兜底:
    def test_落盘炸了回500且仍是信封_异常原文不回显(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """磁盘满了、目录没权限、sidecar 写一半 —— 这条端点真会炸,而炸了绝不能把
        堆栈端到工友面前。

        打桩打在 ``artifacts.register`` 这个**模块属性**上:handler 是
        ``run_in_threadpool(artifacts.register, ...)``,调用那一刻才去模块上取这个名字,
        打在别处进不去 —— 而进不去的表现不是红,是这条用例拿到 200 然后"顺利"绿掉。
        """

        # Arrange
        def 炸(*_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("产物注册表挂了")

        monkeypatch.setattr(artifacts, "register", 炸)

        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg")

        # Assert
        assert resp.status_code == 500
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["ok"] is False
        assert body["data"] is None
        assert body["error_code"] == "INTERNAL"
        assert "RuntimeError" not in resp.text
        assert "产物注册表挂了" not in resp.text, "异常原文只进日志"

    def test_收到一半断了也回500而不是把异常抛出去(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """这条是**真会发生**的:工友在电梯里传照片,传到一半信号没了。

        ``Request.stream()`` 在客户端半路断开时会抛,而抛穿出去就是 ASGI 层的 500 ——
        那个 500 **不是信封**,前端那段共用的解信封代码拿到一坨 HTML 会自己再炸一次
        (表现是界面上一句话都没有,而后端日志里躺着一个看不懂的 ClientDisconnect)。

        打桩同样打在**模块属性**上(``upload_api._read_raw_body``),理由与上一条一样:
        handler 在调用那一刻才去模块全局取这个名字,打在别处进不去 ——
        而进不去的表现不是红,是这条用例拿到 200 然后"顺利"绿掉。
        """

        # Arrange
        async def 半路断了(*_args: Any, **_kwargs: Any) -> bytes:
            raise OSError("客户端提前断开")

        monkeypatch.setattr(upload_api, "_read_raw_body", 半路断了)

        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg")

        # Assert
        assert resp.status_code == 500
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS, "断线时也得是信封,不许让异常穿到 ASGI 层"
        assert body["error_code"] == "INTERNAL"
        assert "客户端提前断开" not in resp.text, "异常原文只进日志"

    def test_只有一条路由而且只收POST(self, client: TestClient) -> None:
        """GET 打不进来。

        多挂一个方法不会有任何报错,所以拿 405 钉住它 —— 这条端点是**只写**的:
        取回产物走的是 ``make serve-artifacts`` 那个静态服务(线上是 Caddy 的
        ``handle_path /artifacts/*``),不是这里。在这儿顺手开一个读口子,
        等于把工地现场照片(含可识别人脸)挂到一个零鉴权的路径上。

        路径叫 ``/attachments`` 而不是 ``/uploads`` 也一并钉住:webapp.py 里已经有
        一批「资料归档」的上传端点,那是**归档**,这条是**聊天附件**,
        两者的去向、生命周期、谁能看都不一样。名字混了,下一个人会往错的那条加功能。
        """
        # Assert
        assert [route.path for route in upload_api.UPLOAD_ROUTES] == ["/attachments"]
        assert client.get("/attachments").status_code == 405


class Test人话:
    """413 与 500 这两条工友都可能真碰到(手机原图动辄十几兆、磁盘也真会满),
    所以这两句必须是人话 —— 「请求体超过硬上限」那种话对工地师傅毫无意义,
    反而会让他以为是自己按错了。"""

    def test_413的user_msg是人话且不回显内部原因(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        _压上限(monkeypatch, photo_mb=TINY_LIMIT_MB, drawing_mb=TINY_LIMIT_MB)

        # Act
        resp = _传(client, OVER_HARD, mime="image/jpeg")

        # Assert
        user_msg = resp.json()["user_msg"]
        assert user_msg, "总得说句话,空着等于界面上什么都不显示"
        for 禁词 in 开发者词汇:
            assert 禁词 not in user_msg, f"人话里冒出了「{禁词}」"
        # detail= 里那句「请求体超过硬上限 N」是给日志看的,fail() 保证它不进返回值
        assert "硬上限" not in resp.text

    def test_500的user_msg是人话(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        def 炸(*_args: Any, **_kwargs: Any) -> str:
            raise OSError("No space left on device")

        monkeypatch.setattr(artifacts, "register", 炸)

        # Act
        resp = _传(client, FAKE_JPEG, mime="image/jpeg")

        # Assert
        user_msg = resp.json()["user_msg"]
        assert user_msg
        for 禁词 in 开发者词汇:
            assert 禁词 not in user_msg, f"人话里冒出了「{禁词}」"
        assert "No space left" not in resp.text, "上游的英文错误一个字都不许上屏"


# ---------------------------------------------------------------------------
# 挂载:这条路由真的铺进 webapp.py 了吗
# ---------------------------------------------------------------------------


def test_这条路由挂进了webapp() -> None:
    """webapp.py 是自定义路由唯一的挂载点,漏铺 = 404 且**没有任何启动报错**。

    这条漏铺的表现比另外三组都隐蔽:打卡漏铺时工友看见「打不开」、监理漏铺时
    面板是空的、耗时漏铺时界面上少几行 —— 而附件直传漏铺时**前端会退回老路**
    (base64 进消息),功能一点不坏,只是那个 1.1 GB 内存库的慢病悄悄复发,
    而且没有任何东西会说话。

    ⚠️ 与 ``test_timing_api`` / ``test_supervision_api`` 那两条同款局限,别当成全部保障:
    它在宿主的 Python 进程里直接 import,验的只是「代码里铺上了」。容器里还要求
    webapp.py 这个文件真的在镜像/挂载里、``langgraph.json`` 真的有 http 块 ——
    W10 那次两样都缺,端点全 404 而这类用例照样全绿。
    """
    # ⚠️ `import webapp` 而不是 `from gyt import webapp`:它住在 backend/ 根下、
    #    **不在 gyt 包里**(langgraph.json 的 http.app 按文件路径指向它)。
    import webapp

    mounted = {route.path for route in webapp.app.routes}
    declared = {route.path for route in upload_api.UPLOAD_ROUTES}

    assert declared <= mounted, f"这些路由没挂进 webapp.py:{sorted(declared - mounted)}"
    assert declared == {"/attachments"}, (
        "加了路由要连同这个集合一起改 —— 它是「有没有漏铺」的对账锚"
    )
