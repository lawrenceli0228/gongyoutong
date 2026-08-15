"""supervision_api.py(监理处置直连接口)的单元测试 —— 不联网,库与产物全落 tmp_path。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):GYT_DATA_DIR 指到
tmp_path/data,每个用例一张全新的库、一个空的产物目录,所以本文件不需要自建环境夹具。

docx **是真渲染**(python-docx,毫秒级):这条链最容易出的错就在"渲染 → 落盘 → 写库"
的顺序与件数上,把渲染 mock 掉等于把要测的东西测没了。

这层要钉死的静默错误(全是"不报错但结果错"那一类):
  · **三条硬拦失守** —— 「一般隐患签了暂停令」与「严重隐患只发通知单」是两个方向相反的
    事故,前者平白停一片人的工,后者该停的没停;``needs_grading=1`` 放行则等于让未知
    风险按一般隐患走完闭环并被销项。三条都只在服务端代码里,提示词兜不住。
  · **``due_date`` 留空** —— 期限为空的隐患永远进不了超期清单、永远不会被升级,
    而报表上它一直显示「在办」。
  · **三份文书少出一份** —— ``documents`` 是数组,少一件就少一张下载卡,不会报错。
  · **有记录无文件** —— 库写在文件落盘之前的话,证据链里会出现一份点不开的「文书」。
  · **端点没挂进 webapp.py** —— 现象是 404,**不是启动报错**。
  · **令牌漏配时裸奔** —— langgraph 那道鉴权中间件的键漏了没有任何报错,
    只有 handler 自查这一道兜得住(与打卡链同一条上线闸)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import webapp
from docx import Document as DocxDocument
from starlette.testclient import TestClient

from gyt import supervision_api
from gyt.agents.supervision.docgen import SUPERVISION_DISCLAIMER
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from gyt.core.doc_no import DocKind, DocNoExhaustedError, new_doc_no
from gyt.db import hazards

REAL_TOKEN = "0123456789abcdef0123456789abcdef"
"""一个像样的令牌:够长、不是占位符开头 —— 两条都满足才会真正开启鉴权
(判据在 core/access.py,与 auth.py 同源)。"""

DUE_PHRASE = "下周三"
"""合法的期限原话。解析归 agents/schedule/dates.py,这里只要是它认得的写法就行。"""

FAKE_JPEG = b"\xff\xd8\xff\xe0fake-photo-bytes"

_ALL_ENDPOINTS: list[tuple[str, dict[str, Any]]] = [
    ("/supervision/confirm", {"hazard_nos": ["GYT-H-20260816-090000-0001"]}),
    ("/supervision/grade", {"hazard_no": "GYT-H-20260816-090000-0001", "grade": "一般"}),
    ("/supervision/notice", {"hazard_no": "GYT-H-20260816-090000-0001", "due_phrase": DUE_PHRASE}),
    ("/supervision/suspend", {"hazard_no": "GYT-H-20260816-090000-0001", "due_phrase": DUE_PHRASE}),
    (
        "/supervision/reinspect-result",
        {"hazard_no": "GYT-H-20260816-090000-0001", "result": "pass", "after_photo_id": "0" * 32},
    ),
    ("/supervision/resume", {"hazard_no": "GYT-H-20260816-090000-0001"}),
    ("/supervision/escalate", {"hazard_no": "GYT-H-20260816-090000-0001"}),
]
"""七个端点各配一份格式合法的请求体。鉴权用例拿它逐个打 —— 鉴权必须在业务之前生效,
所以这些编号根本不存在也没关系(过了鉴权会是 404,被拦下则是 401)。"""

_ISSUING_PATHS: list[str] = [
    "/supervision/notice",
    "/supervision/suspend",
    "/supervision/resume",
    "/supervision/escalate",
]
"""四个**签发**端点。硬拦③(needs_grading=1 一律拒)必须四个全拦,漏一个就是一条
未定级的隐患从那个口子走完闭环。"""


@pytest.fixture
def client() -> TestClient:
    """真 Starlette 栈,直打本模块的 app(线上走的是 webapp.py,挂载另有用例盯着)。"""
    return TestClient(supervision_api.app)


def _photo(name: str = "整改后.jpg") -> str:
    """登记一张假照片,返回 artifact_id —— 复查结论必须挂真实存在的照片。"""
    return artifacts.register(FAKE_JPEG, kind=ArtifactKind.PHOTO, original_name=name)


def _new_hazard(
    *,
    grade: str = hazards.GRADE_NORMAL,
    severity: str = "一般",
    needs_grading: bool = False,
    item: str = "未戴安全帽",
    project_id: str = "gyt-a3",
) -> str:
    """登记一条隐患(落 pending),返回它的编号。编号由 core/doc_no.py 现摇,不手拼。"""
    registration = hazards.create(
        hazard_no=new_doc_no(DocKind.HAZARD),
        project_id=project_id,
        photo_sha256=f"sha256-{item}-{project_id}-{grade}-{severity}-{needs_grading}",
        photo_id=_photo("现场.jpg"),
        item=item,
        severity=severity,
        grade=grade,
        grading_version="1",
        needs_grading=needs_grading,
    )
    return registration.row.hazard_no


def _arrive(status: str, **kwargs: Any) -> str:
    """把一条隐患**直接用 db 层**推到指定状态,返回编号。

    刻意绕开端点:端点带着三条硬拦,而这里要搭的台子恰恰包括"硬拦本该拦下的局面"
    (比如一条 ``needs_grading=1`` 却已经停过工的隐患 —— 从端点根本走不到)。
    db 层只管状态合法性、不管业务判断,正好当搭台工具。
    """
    due = "2026-12-31"
    hazard_no = _new_hazard(**kwargs)
    if status == hazards.STATUS_PENDING:
        return hazard_no
    hazards.confirm(hazard_no)
    if status == hazards.STATUS_OPEN:
        return hazard_no
    if status == hazards.STATUS_NOTIFIED:
        hazards.mark_notified(hazard_no, due)
        return hazard_no
    if status == hazards.STATUS_SUSPENDED:
        hazards.mark_suspended(hazard_no, due)
        return hazard_no
    if status == hazards.STATUS_REINSPECT_FAILED:
        hazards.mark_notified(hazard_no, due)
        hazards.mark_reinspect_failed(hazard_no)
        return hazard_no
    if status == hazards.STATUS_RESUMING:
        hazards.mark_suspended(hazard_no, due)
        hazards.pass_reinspection(hazard_no)
        return hazard_no
    raise AssertionError(f"搭台脚本还不会造 {status} 这个状态")  # pragma: no cover


def _docx_files() -> list[Path]:
    """产物目录里所有落了盘的 docx —— 用来分辨"文件出了没"与"库写了没"。"""
    return sorted(get_settings().artifacts_dir.glob("*/*.docx"))


# ---------------------------------------------------------------------------
# 挂载与词表:漏了都不报错
# ---------------------------------------------------------------------------


def test_七条路由都挂进了webapp() -> None:
    """webapp.py 是自定义路由唯一的挂载点,漏铺 = 全部 404 且**没有任何启动报错**。"""
    mounted = {route.path for route in webapp.app.routes}
    declared = {route.path for route in supervision_api.SUPERVISION_ROUTES}

    assert declared <= mounted, f"这些路由没挂进 webapp.py:{sorted(declared - mounted)}"
    assert len(declared) == 7


def test_八个状态都有中文名() -> None:
    """状态词表漂了的表现是回给工友的话里冒出一个英文词。模块导入时就会硬失败,
    这条用例是把那个约束写成人看得见的形式。"""
    assert set(supervision_api._STATUS_ZH) == set(hazards.STATUSES)


# ---------------------------------------------------------------------------
# 鉴权(纵深防御第二道):判定与 auth.py 同源
# ---------------------------------------------------------------------------


class Test鉴权:
    @pytest.mark.parametrize(("path", "body"), _ALL_ENDPOINTS)
    def test_设了令牌但没带钥匙一律拒(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
    ) -> None:
        """七个端点全测:漏一个就是一条能绕过令牌的写入路径。"""
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()

        resp = client.post(path, json=body)

        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHORIZED"

    def test_带对令牌就放行到业务层(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """放行的证据是它开始回业务错误(编号查不到 = 404),而不是 401。"""
        monkeypatch.setenv("GYT_ACCESS_TOKEN", REAL_TOKEN)
        get_settings.cache_clear()

        resp = client.post(
            "/supervision/notice",
            json={"hazard_no": "GYT-H-20260816-090000-0001", "due_phrase": DUE_PHRASE},
            headers={"X-Api-Key": REAL_TOKEN},
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "NOT_FOUND"

    def test_没配令牌时不拦(self, client: TestClient) -> None:
        """本机联调与真机验收都不发令牌头,「未配置也拦」等于当场打死它们。"""
        resp = client.post("/supervision/resume", json={"hazard_no": "查无此号"})

        assert resp.status_code == 404  # 走到业务层了


# ---------------------------------------------------------------------------
# 🔴 三条硬拦
# ---------------------------------------------------------------------------


class Test三条硬拦:
    def test_严重隐患不许只签通知单(self, client: TestClient) -> None:
        """硬拦①:该走三文书那条路的,不许从通知单这个口子溜出去。"""
        hazard_no = _arrive(hazards.STATUS_OPEN, grade=hazards.GRADE_SEVERE, severity="重大")

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 409
        body = resp.json()
        assert body["error_code"] == "CONFLICT"
        assert "暂停令" in body["user_msg"]  # 得告诉人去哪条路,不是光说不行
        # 状态一动没动,盘上也没有半份文书
        assert hazards.fetch(hazard_no).status == hazards.STATUS_OPEN
        assert _docx_files() == []

    def test_一般隐患不许签暂停令(self, client: TestClient) -> None:
        """硬拦②:反方向那条 —— 签下去就是平白停一片人的工。"""
        hazard_no = _arrive(hazards.STATUS_OPEN)

        resp = client.post(
            "/supervision/suspend", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 409
        assert "通知单" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).status == hazards.STATUS_OPEN
        assert _docx_files() == []

    @pytest.mark.parametrize("path", _ISSUING_PATHS)
    def test_未定级的隐患任何签发都被拒(self, client: TestClient, path: str) -> None:
        """硬拦③(Codex#11):四个签发端点全拦。

        各自搭到那个端点该有的状态(用 db 层直推,见 ``_arrive`` 的说明),
        这样被拒的原因只可能是这面旗子,不是状态不对。
        """
        status_for = {
            "/supervision/notice": hazards.STATUS_OPEN,
            "/supervision/suspend": hazards.STATUS_OPEN,
            "/supervision/resume": hazards.STATUS_RESUMING,
            "/supervision/escalate": hazards.STATUS_REINSPECT_FAILED,
        }
        grade = hazards.GRADE_SEVERE if path == "/supervision/suspend" else hazards.GRADE_NORMAL
        hazard_no = _arrive(status_for[path], grade=grade, severity="待定级", needs_grading=True)
        before = hazards.fetch(hazard_no).status

        resp = client.post(path, json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE})

        assert resp.status_code == 409
        assert "定级" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).status == before
        assert _docx_files() == []


# ---------------------------------------------------------------------------
# 整改期限:解析不出就 fail,绝不留空
# ---------------------------------------------------------------------------


class Test整改期限:
    def test_期限看不懂就拒且什么都没发生(self, client: TestClient) -> None:
        """期限留空的隐患永远进不了超期清单,也就永远不会被升级 —— 所以宁可这次不签。"""
        hazard_no = _arrive(hazards.STATUS_OPEN)

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": "改快一点"}
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_INPUT"
        # dates.py 的报错本身就是人话,还附了正确写法 —— 原样透出去比重新包一层有用
        assert "没看懂" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).due_date is None
        assert hazards.fetch(hazard_no).status == hazards.STATUS_OPEN
        assert _docx_files() == []

    def test_不给期限也拒(self, client: TestClient) -> None:
        """空 due_phrase 不许被当成「无期限」放过去(schedule 的任务可以没期限,隐患不行)。"""
        hazard_no = _arrive(hazards.STATUS_OPEN)

        resp = client.post("/supervision/notice", json={"hazard_no": hazard_no})

        assert resp.status_code == 400
        assert "期限" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).status == hazards.STATUS_OPEN

    def test_期限按香港日历日写进台账(self, client: TestClient) -> None:
        """签发成功时 due_date 必须是解析出来的真日期(ISO),不是原话。"""
        hazard_no = _arrive(hazards.STATUS_OPEN)

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": "明天"}
        )

        assert resp.status_code == 200
        due = hazards.fetch(hazard_no).due_date
        assert due is not None
        assert len(due) == len("2026-08-17") and due.count("-") == 2


# ---------------------------------------------------------------------------
# 🔴 暂停令:三份文书、顺序固定、文件先落盘库后写
# ---------------------------------------------------------------------------


class Test暂停令三文书:
    def _suspend(self, client: TestClient) -> tuple[str, dict[str, Any]]:
        hazard_no = _arrive(hazards.STATUS_OPEN, grade=hazards.GRADE_SEVERE, severity="重大")
        resp = client.post(
            "/supervision/suspend", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )
        assert resp.status_code == 200, resp.text
        return hazard_no, resp.json()

    def test_一次三份且顺序固定(self, client: TestClient) -> None:
        """顺序是冻结契约的一部分:前端按数组渲染 N 张下载卡,乱序 = 卡片对不上人的预期。"""
        _, body = self._suspend(client)

        docs = body["data"]["documents"]
        assert [d["doc_type"] for d in docs] == ["notice", "suspension", "owner_report"]

    def test_每一项四个键齐全且编号类型段对得上(self, client: TestClient) -> None:
        """少一个键前端就少渲一块,而且不会报错。编号的类型段是这份文书的对外身份。"""
        _, body = self._suspend(client)

        prefixes = {"notice": "GYT-TZ-", "suspension": "GYT-ZT-", "owner_report": "GYT-JS-"}
        for doc in body["data"]["documents"]:
            assert set(doc) == {"doc_type", "doc_no", "artifact_id", "filename"}
            assert doc["doc_no"].startswith(prefixes[doc["doc_type"]])
            assert doc["filename"].endswith(".docx")

    def test_三份都真落了盘也真进了证据链(self, client: TestClient) -> None:
        """「有记录无文件」是这条链最危险的失效:证据链里一份点不开的文书。"""
        hazard_no, body = self._suspend(client)

        for doc in body["data"]["documents"]:
            assert artifacts.resolve(doc["artifact_id"]).is_file()
        rows = hazards.docs_of([hazard_no])
        assert [r.doc_type for r in rows] == ["notice", "suspension", "owner_report"]
        assert all(r.artifact_id for r in rows)
        assert len(_docx_files()) == 3

    def test_状态与期限一起落库(self, client: TestClient) -> None:
        hazard_no, body = self._suspend(client)

        row = hazards.fetch(hazard_no)
        assert row.status == hazards.STATUS_SUSPENDED == body["data"]["status"]
        assert row.due_date is not None
        assert row.was_suspended == 1  # 此后复查合格也必须先出复工令

    def test_三份共用一个时间快照(self, client: TestClient) -> None:
        """编号里的日期时刻必须一致 —— 各取各的 now,跨秒时同一次签发在纸面上会变成
        三个时间点,而追责时先被质疑的就是文件本身(doc_no.py 头注的原话)。"""
        _, body = self._suspend(client)

        # GYT-TZ-20260816-093000-abcd → 取「20260816-093000」那一段
        时刻 = {"-".join(d["doc_no"].split("-")[2:4]) for d in body["data"]["documents"]}
        assert len(时刻) == 1

    def test_文书正文印着编号且用的是监理那句免责(self, client: TestClient) -> None:
        """两件事一起盯:

        · **编号必须印在纸上** —— 它是这份文书的对外身份,也正因为印在正文里,
          撞号重试才必须连正文一起重渲染;
        · **免责句不许串** —— 巡检记录那句讲的是「AI 初筛 vs 持证安全员」,压根没有
          「总监签字后生效」这一环。串了的表现是一份看起来很正式、却没写明"未签字不算数"
          的停工文书(docgen.py 头注点名的那条)。
        """
        hazard_no, body = self._suspend(client)
        暂停令 = body["data"]["documents"][1]

        parsed = DocxDocument(str(artifacts.resolve(暂停令["artifact_id"])))
        正文 = "\n".join(p.text for p in parsed.paragraphs)
        表格 = [c.text for t in parsed.tables for r in t.rows for c in r.cells]

        assert parsed.paragraphs[0].text == "工程暂停令"
        assert 暂停令["doc_no"] in 表格
        assert hazard_no in 表格
        assert SUPERVISION_DISCLAIMER in 正文
        # 签字栏留空:系统替人填名字就是伪造(D15)
        assert "总监理工程师(签字):" in 正文

    def test_措辞是已出具暂停令而不是已停工(self, client: TestClient) -> None:
        """suspended 只证明文书出了稿,不证明工地真停了工(db 的 STATUSES 头注)。
        对外措辞升级一格,就是拿一份没签字的稿子去宣称已经责令停工。"""
        _, body = self._suspend(client)

        user_msg = body["user_msg"]
        assert "出稿" in user_msg
        assert "签字" in user_msg
        assert "已责令停工" not in user_msg

    def test_库写失败时留孤儿文件但状态一动没动(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§6.4 的取舍:孤儿文件(有文件无记录)可以接受,孤儿记录绝对不行。

        文件先落盘,所以库炸的时候盘上已经有三份 —— 这正是预期,不是 bug。
        真正要断的是**状态没变**:库那一步是一个事务,迁移没成就一行都没写。
        """
        hazard_no = _arrive(hazards.STATUS_OPEN, grade=hazards.GRADE_SEVERE, severity="重大")

        def 炸(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("台账挂了")

        monkeypatch.setattr(hazards, "mark_suspended", 炸)

        resp = client.post(
            "/supervision/suspend", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL"
        assert hazards.fetch(hazard_no).status == hazards.STATUS_OPEN
        assert hazards.docs_of([hazard_no]) == []
        assert len(_docx_files()) == 3  # 孤儿,交清理器


# ---------------------------------------------------------------------------
# 编号:撞库重试与"摇不出来"
# ---------------------------------------------------------------------------


class Test文书编号:
    def test_撞库时整轮重摇号重渲染(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """编号印在文书正文里,所以撞号必须连正文一起重来 —— 只换库里那一列的话,
        纸上的号和台账对不上,那份文书自己证伪自己。"""
        hazard_no = _arrive(hazards.STATUS_OPEN)
        真的 = hazards.mark_notified
        调用次数 = {"n": 0}

        def 先撞一次(*args: Any, **kwargs: Any) -> bool:
            调用次数["n"] += 1
            if 调用次数["n"] == 1:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: hazard_docs.doc_no")
            return 真的(*args, **kwargs)

        monkeypatch.setattr(hazards, "mark_notified", 先撞一次)

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 200
        assert 调用次数["n"] == 2
        # 库里那份与回给前端的那份是同一个号(重渲染之后两边仍然对得上)
        回执号 = resp.json()["data"]["documents"][0]["doc_no"]
        assert [r.doc_no for r in hazards.docs_of([hazard_no])] == [回执号]
        assert len(_docx_files()) == 2  # 第一轮那份成了孤儿

    def test_摇不出编号时接住并说人话(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**不许降级成「用最后那个撞了的号继续」** —— 两份文书顶着同一个编号,
        比这次没出成严重得多。异常原文只进日志。"""
        hazard_no = _arrive(hazards.STATUS_OPEN)

        def 摇不出来(*_args: Any, **_kwargs: Any) -> str:
            raise DocNoExhaustedError("连着摇了 5 次监理通知单编号都已经被占用")

        monkeypatch.setattr(supervision_api, "generate_unique", 摇不出来)

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "INTERNAL"
        assert "没有出" in body["user_msg"]  # 明确告诉工友:什么都没发生
        assert "DocNoExhausted" not in body["user_msg"]  # 内部细节不许露头
        assert hazards.fetch(hazard_no).status == hazards.STATUS_OPEN
        assert _docx_files() == []


# ---------------------------------------------------------------------------
# 状态机:非法迁移一律拒,而且要说清现在是哪一站
# ---------------------------------------------------------------------------


class Test状态机:
    def test_待确认的隐患不能直接签通知单(self, client: TestClient) -> None:
        """没有这道闸,safety 看一眼照片就能开启法律流程(D17 的存在理由)。"""
        hazard_no = _arrive(hazards.STATUS_PENDING)

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 409
        assert "待确认" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).status == hazards.STATUS_PENDING

    def test_签过通知单的不能再签一次(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert resp.status_code == 409
        assert _docx_files() == []

    def test_没停过工的不许发复工令(self, client: TestClient) -> None:
        """滥发复工令:一条从没停过工的隐患拿到一纸复工令,等于凭空承认它停过。"""
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post("/supervision/resume", json={"hazard_no": hazard_no})

        assert resp.status_code == 409
        assert "已签发通知单" in resp.json()["user_msg"]

    def test_没复查过不许上报主管部门(self, client: TestClient) -> None:
        """升级的举证链是「我通知过 + 期限到了 + 复查过 + 他没改」,少一环就是拿
        建立在自己记乱账上的材料去指控施工方。"""
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post("/supervision/escalate", json={"hazard_no": hazard_no})

        assert resp.status_code == 409
        assert hazards.fetch(hazard_no).status == hazards.STATUS_NOTIFIED

    def test_查无此隐患是404(self, client: TestClient) -> None:
        resp = client.post(
            "/supervision/notice",
            json={"hazard_no": "GYT-H-20260101-000000-ffff", "due_phrase": "明天"},
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 批量确认(D17 的人工闸)
# ---------------------------------------------------------------------------


class Test批量确认:
    def test_批量确认部分成功也如实回报(self, client: TestClient) -> None:
        """一条失败不牵连其它条(db 层要求每条一个事务),但失败的必须**出现在回执里** ——
        静默吞掉的话,监理以为都确认了,少的那条从此没人管。"""
        好的 = [_arrive(hazards.STATUS_PENDING, item=f"隐患{i}") for i in range(2)]
        已确认 = _arrive(hazards.STATUS_OPEN, item="已经确认过的")

        resp = client.post(
            "/supervision/confirm",
            json={"hazard_nos": [*好的, 已确认, "GYT-H-20260101-000000-ffff"]},
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["confirmed"] == 好的
        assert [f["hazard_no"] for f in data["failed"]] == [
            已确认,
            "GYT-H-20260101-000000-ffff",
        ]
        assert all(hazards.fetch(no).status == hazards.STATUS_OPEN for no in 好的)

    def test_单条也收(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_PENDING)

        resp = client.post("/supervision/confirm", json={"hazard_no": hazard_no})

        assert resp.status_code == 200
        assert resp.json()["data"]["confirmed"] == [hazard_no]

    def test_重复编号只算一条(self, client: TestClient) -> None:
        """界面上双击会把同一条发两遍,不去重的话第二条被报成「不用再确认」,
        看起来像出了错,其实什么问题都没有。"""
        hazard_no = _arrive(hazards.STATUS_PENDING)

        resp = client.post("/supervision/confirm", json={"hazard_nos": [hazard_no, hazard_no]})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["confirmed"] == [hazard_no]
        assert data["failed"] == []

    def test_一条都没确认成回409(self, client: TestClient) -> None:
        resp = client.post("/supervision/confirm", json={"hazard_nos": ["查无此号"]})

        assert resp.status_code == 409
        assert "查不到" in resp.json()["user_msg"]

    def test_空清单被拒(self, client: TestClient) -> None:
        resp = client.post("/supervision/confirm", json={"hazard_nos": []})

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 人工定级:未定级隐患唯一的解锁通道
# ---------------------------------------------------------------------------


class Test人工定级:
    def test_定级之后旗子清掉且签发解锁(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_OPEN, severity="待定级", needs_grading=True)

        graded = client.post(
            "/supervision/grade", json={"hazard_no": hazard_no, "grade": hazards.GRADE_NORMAL}
        )
        signed = client.post(
            "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
        )

        assert graded.status_code == 200
        assert graded.json()["data"]["needs_grading"] is False
        assert hazards.fetch(hazard_no).needs_grading == 0
        assert signed.status_code == 200  # 解锁了

    def test_级别不在词表被拒(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_OPEN)

        resp = client.post("/supervision/grade", json={"hazard_no": hazard_no, "grade": "特别严重"})

        assert resp.status_code == 400
        assert hazards.fetch(hazard_no).grade == hazards.GRADE_NORMAL

    def test_签过文书之后不许改级(self, client: TestClient) -> None:
        """改了的话,已经发出去的那份文书和台账当场对不上 —— 而本批不做重签。"""
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/grade", json={"hazard_no": hazard_no, "grade": hazards.GRADE_SEVERE}
        )

        assert resp.status_code == 409
        assert hazards.fetch(hazard_no).grade == hazards.GRADE_NORMAL


# ---------------------------------------------------------------------------
# 复查结论:照片是证据,结论由人下,落到哪一站由 db 挑边
# ---------------------------------------------------------------------------


class Test复查结论:
    def test_缺复查照片必拒(self, client: TestClient) -> None:
        """拿不到照片就没有任何路径能把隐患改成 closed(方案 §5.2 红线)。"""
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/reinspect-result", json={"hazard_no": hazard_no, "result": "pass"}
        )

        assert resp.status_code == 400
        assert "照片" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).status == hazards.STATUS_NOTIFIED

    def test_照片编号打错也拒(self, client: TestClient) -> None:
        """只校验"非空"是不够的:编号错一位照样非空,而复查合格是能把隐患销项的。"""
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/reinspect-result",
            json={"hazard_no": hazard_no, "result": "pass", "after_photo_id": "f" * 32},
        )

        assert resp.status_code == 404
        assert hazards.fetch(hazard_no).status == hazards.STATUS_NOTIFIED

    def test_结论不在词表被拒(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/reinspect-result",
            json={"hazard_no": hazard_no, "result": "大概行吧", "after_photo_id": _photo()},
        )

        assert resp.status_code == 400

    def test_没停过工的复查合格直接销项(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)
        photo_id = _photo()

        resp = client.post(
            "/supervision/reinspect-result",
            json={"hazard_no": hazard_no, "result": "pass", "after_photo_id": photo_id},
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == hazards.STATUS_CLOSED
        assert resp.json()["data"]["documents"] == []  # 复查不出文书
        rows = hazards.docs_of([hazard_no])
        assert [(r.doc_type, r.photo_id, r.result) for r in rows] == [
            ("reinspect", photo_id, "pass")
        ]

    def test_停过工的复查合格是待签复工令不是销项(self, client: TestClient) -> None:
        """Codex#6 点名的失效模式:停过工的从这里销项 = **漏发复工令**。
        挑边由 db 按 was_suspended 决定,端点一个字都不许自己挑。"""
        hazard_no = _arrive(hazards.STATUS_SUSPENDED, grade=hazards.GRADE_SEVERE, severity="重大")

        resp = client.post(
            "/supervision/reinspect-result",
            json={"hazard_no": hazard_no, "result": "pass", "after_photo_id": _photo()},
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == hazards.STATUS_RESUMING
        assert "复工令" in resp.json()["user_msg"]
        assert hazards.fetch(hazard_no).closed_at is None  # 还没销

    def test_不合格进复查不合格并留照片(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/reinspect-result",
            json={"hazard_no": hazard_no, "result": "fail", "after_photo_id": _photo()},
        )

        assert resp.status_code == 200
        assert hazards.fetch(hazard_no).status == hazards.STATUS_REINSPECT_FAILED


# ---------------------------------------------------------------------------
# 复工令与上报:一条走完整闭环
# ---------------------------------------------------------------------------


class Test收尾两条路:
    def test_复工令签发之后销项且引用了原暂停令(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_RESUMING, grade=hazards.GRADE_SEVERE, severity="重大")

        resp = client.post("/supervision/resume", json={"hazard_no": hazard_no})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [d["doc_type"] for d in data["documents"]] == ["resumption"]
        assert data["documents"][0]["doc_no"].startswith("GYT-FG-")
        row = hazards.fetch(hazard_no)
        assert row.status == hazards.STATUS_CLOSED
        assert row.closed_at is not None

    def test_上报主管部门出监理报告并附证据链(self, client: TestClient) -> None:
        hazard_no = _arrive(hazards.STATUS_REINSPECT_FAILED)

        resp = client.post("/supervision/escalate", json={"hazard_no": hazard_no})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == hazards.STATUS_ESCALATED
        assert data["documents"][0]["doc_no"].startswith("GYT-JB-")
        assert hazards.fetch(hazard_no).status == hazards.STATUS_ESCALATED

    def test_全链走一遍_严重隐患从确认到复工(self, client: TestClient) -> None:
        """一条严重隐患的完整闭环:确认 → 三文书 → 复查合格 → 复工令 → 销项。

        逐段测过了还要有这一条,是因为**段与段之间的衔接**才是最容易断的地方
        (最典型的:复查合格之后到底该不该出复工令)。
        """
        hazard_no = _new_hazard(grade=hazards.GRADE_SEVERE, severity="重大", item="临边无防护")

        assert client.post("/supervision/confirm", json={"hazard_no": hazard_no}).status_code == 200
        assert (
            client.post(
                "/supervision/suspend",
                json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/supervision/reinspect-result",
                json={"hazard_no": hazard_no, "result": "pass", "after_photo_id": _photo()},
            ).json()["data"]["status"]
            == hazards.STATUS_RESUMING
        )
        assert client.post("/supervision/resume", json={"hazard_no": hazard_no}).status_code == 200

        row = hazards.fetch(hazard_no)
        assert row.status == hazards.STATUS_CLOSED
        # 证据链:三份文书 + 一条复查 + 一份复工令,全在,顺序即发生先后
        assert [r.doc_type for r in hazards.docs_of([hazard_no])] == [
            "notice",
            "suspension",
            "owner_report",
            "reinspect",
            "resumption",
        ]
        assert len(_docx_files()) == 4  # 复查记录不出文书


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


def test_坏JSON回400而不是500(client: TestClient) -> None:
    """解析不了的请求体是用户侧的问题,不该被兜底成「系统开小差」。"""
    resp = client.post(
        "/supervision/notice",
        content=b"{not json at all",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_INPUT"


def test_信封永远是四键(client: TestClient) -> None:
    """成功与失败两条路都得是同一个形状 —— 前端只写一套归一化。"""
    hazard_no = _arrive(hazards.STATUS_OPEN)
    成功 = client.post(
        "/supervision/notice", json={"hazard_no": hazard_no, "due_phrase": DUE_PHRASE}
    ).json()
    失败 = client.post(
        "/supervision/notice", json={"hazard_no": "没这号", "due_phrase": "明天"}
    ).json()

    for body in (成功, 失败):
        assert set(body) == {"ok", "data", "user_msg", "error_code"}
    assert 成功["error_code"] is None
    assert 失败["data"] is None
