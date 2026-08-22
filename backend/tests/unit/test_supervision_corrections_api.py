"""监理那两处**订正**端点 + 登记失败出口的用例(2026-08-22)。

    POST /supervision/extend            改整改期限(宽限几天),不出文书
    POST /supervision/reassign          改归属(挪工地),不出文书
    GET  /supervision/ingest-failures   有哪几条隐患没能写进台账

单开一个文件而不是往 ``test_supervision_api.py``(已 2300+ 行)里塞:那份钉的是
**签发流程**(三条硬拦、文书原子产出、证据链),而这三条与它不同类 —— 它们不签发
任何文书,回答的是「当初记错了」和「有什么根本没记上」。

三条端点共同的来路是同一个发现:**用户被指向一条不存在的路**。
  · ``agents/schedule/tools.py`` 明写「要宽限几天,得让监理去改这条隐患的整改期限」,
    而监理那边没有这个动作;
  · 隐患拍照时没选工地就永远待在「未归属」,全仓唯一写 ``project_id`` 的地方是
    删项目时的整批置空;
  · D10 说登记失败要能「事后统计」,而 ``list_ingest_failures()`` 全仓零生产调用方。

这份要钉死的静默错误:
  · **签过文书的隐患被改了工地** —— 纸上写着 A、台账写着 B,两份都拿得出来,零报错。
  · **幂等键撞车与"状态不对"给同一句人话** —— 监理回去查状态,而状态好好的。
  · **改期不留痕 / 拒了还留痕** —— 前者让「展了几次期」这个问题永远答不出,
    后者让台账与留痕自相矛盾。
  · **``reason`` 漏进登记失败的响应** —— 那是异常类型与约束名,摆到工地负责人面前。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from typing import Any, Final

import pytest
from starlette.testclient import TestClient

from gyt import supervision_api
from gyt.agents.supervision import scoping
from gyt.config import get_settings
from gyt.db import hazards

DUE_PHRASE: Final[str] = "下周三"
"""期限原话。与 ``test_supervision_api.py`` 那份同源 —— 换算交给
``agents/schedule/dates.py``,这层一行日期数学都不写。"""

LATER_PHRASE: Final[str] = "下周五"
"""改期后的原话,比 ``DUE_PHRASE`` 晚两天。「宽限几天」是这条端点的主用途。"""

FAR_FUTURE_ISO: Final[str] = "2026-12-31"
"""造数用的期限,故意远到不会超期 —— 超期与否不是这一组要测的东西。"""

REASON: Final[str] = "連續下雨停工三天"
"""合法理由(≥4 字)。刻意用繁體:监理那一侧的界面恒繁體(W12),
而后端对理由只管长度、不管字形 —— 写简体的话这条隐含前提就没被测到。"""

_SEQ = iter(range(1, 10_000))


@pytest.fixture
def client() -> TestClient:
    """真 Starlette 栈,直打本模块的 app(线上走 webapp.py,挂载另有用例盯着)。"""
    return TestClient(supervision_api.app)


TODAY: Final[date] = date(2026, 8, 20)
"""钉死的「今天」(周四)。选周四是因为「下周三」「下周五」两个说法都要跨到下一周,
换算结果不会因为跑测试的那天恰好是周几而变。"""


@pytest.fixture(autouse=True)
def _钉住今天(monkeypatch: pytest.MonkeyPatch) -> None:
    """期限换算里的「今天」不许跟着真实日子飘。

    ⚠️ 桩必须打在 ``scoping.today_hk`` 这个**模块属性**上 —— 理由与
    ``test_supervision_api.py`` 那条逐字相同(按名 ``from … import today_hk`` 的调用点
    在导入那一刻就把函数对象绑死了,桩打不进去;而**打不进去的表现不是红**,
    是"今天绿、明天红",查起来极费劲)。
    这一组本身不测超期,但 ``_resolve_due`` 会拿今天算「下周三」是哪天。
    """
    monkeypatch.setattr(scoping, "today_hk", lambda: TODAY)


def _raw_status(hazard_no: str, status: str) -> None:
    """绕过状态机把一行摆到某个状态(补集用例要从八档各出发一次)。"""
    with closing(sqlite3.connect(get_settings().sqlite_path)) as conn, conn:
        conn.execute("UPDATE hazards SET status = ? WHERE hazard_no = ?", (status, hazard_no))


def _new_hazard(*, project_id: str = "gyt-a3", **overrides: Any) -> str:
    """登记一条隐患,返回编号。"""
    seq = next(_SEQ)
    params: dict[str, Any] = {
        "hazard_no": f"GYT-H-20260822-090000-{seq:04x}",
        "project_id": project_id,
        "photo_sha256": f"sha256-{seq}",
        "photo_id": f"{seq:032x}",
        "item": "未戴安全帽",
        "severity": "一般",
        "grade": hazards.GRADE_NORMAL,
        "grading_version": "1",
        "needs_grading": False,
    }
    params.update(overrides)
    return hazards.create(**params).row.hazard_no


def _arrive(status: str, *, due: str = FAR_FUTURE_ISO, **kwargs: Any) -> str:
    """把一条隐患**直接用 db 层**推到指定状态,返回编号。

    刻意绕开端点:这一组要搭的台子包括"端点本该拦下的局面",而 db 层只管状态合法性。
    姿势与 ``test_supervision_api._arrive`` 同款(那份更全,这里只要用得到的几档)。
    """
    hazard_no = _new_hazard(**kwargs)
    grade = kwargs.get("grade", hazards.GRADE_NORMAL)
    if status == hazards.STATUS_PENDING:
        return hazard_no
    hazards.confirm(hazard_no)
    if status == hazards.STATUS_OPEN:
        return hazard_no
    if status == hazards.STATUS_NOTIFIED:
        hazards.mark_notified(hazard_no, due, expected_grade=grade)
        return hazard_no
    if status == hazards.STATUS_SUSPENDED:
        hazards.mark_suspended(hazard_no, due, expected_grade=grade)
        return hazard_no
    hazards.mark_notified(hazard_no, due, expected_grade=grade)
    hazards.mark_reinspect_failed(hazard_no)
    assert status == hazards.STATUS_REINSPECT_FAILED, f"这个 helper 还不会造 {status}"
    return hazard_no


# ===========================================================================
# POST /supervision/extend —— 改整改期限
# ===========================================================================


class Test改期限:
    def test_改期成功_回执带新旧两个日期且不出文书(self, client: TestClient) -> None:
        """回执里必须**同时**有新旧期限。只报新的话,界面上没法让人确认"挪了几天"。"""
        no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post(
            "/supervision/extend",
            json={
                "hazard_no": no,
                "due_phrase": LATER_PHRASE,
                "reason": REASON,
                "issued_by": "陳大文",
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["previous_due_date"] == FAR_FUTURE_ISO
        assert data["due_date"] != FAR_FUTURE_ISO
        assert data["due_display"], "界面上念的是这个,空的话人看到一串 ISO 日期"
        assert data["status"] == hazards.STATUS_NOTIFIED, "改期不是状态迁移"
        assert data["documents"] == [], (
            "🔴 订正不出文书 —— 出了就是把「我们记错了」变成一份对外文件"
        )
        assert hazards.fetch(no).due_date == data["due_date"]  # type: ignore[union-attr]

    def test_改期留痕进了详情且不混进证据链(self, client: TestClient) -> None:
        """``due_changes`` 与 ``documents`` **平级**。

        混进 documents 的表现:前端那张证据链表多出几行点不开的东西(它们没有编号、
        没有产物),而「一共签了几份文书」这个数会跟着错。
        """
        no = _arrive(hazards.STATUS_NOTIFIED)
        client.post(
            "/supervision/extend",
            json={"hazard_no": no, "due_phrase": LATER_PHRASE, "reason": REASON, "issued_by": "陳"},
        )

        data = client.get(f"/supervision/hazards/{no}").json()["data"]
        (痕,) = data["due_changes"]
        assert 痕["old_due"] == FAR_FUTURE_ISO
        assert (痕["reason"], 痕["changed_by"]) == (REASON, "陳")
        assert data["documents"] == [], "这条隐患从头到尾没签过文书"

    @pytest.mark.parametrize(
        "status",
        [s for s in hazards.STATUSES if s not in hazards.DUE_CHANGEABLE_STATUSES],
    )
    def test_没有在跑的期限的档一律409(self, client: TestClient, status: str) -> None:
        """补集穷举。逐个列举的失效方式很具体:哪天加了第九档,清单不会提醒它漏了,
        而那一档默认是"能改"—— 往**放行**的方向漏。"""
        no = _new_hazard()
        _raw_status(no, status)

        resp = client.post(
            "/supervision/extend",
            json={"hazard_no": no, "due_phrase": LATER_PHRASE, "reason": REASON},
        )

        assert resp.status_code == 409
        assert resp.json()["error_code"] == "CONFLICT"
        assert hazards.due_changes_of([no]) == [], "拒了就不许留痕"

    def test_理由太短拦下_而且状态闸排在它前面(self, client: TestClient) -> None:
        """⚠️ **顺序是刻意的**:一个 pending 的隐患不该先被要求补写理由、
        人认真写完提交后才被告知「这条路你根本走不通」—— 白费一趟,而且他会以为是理由的问题。
        """
        # ① 状态对、理由太短 → 400 说理由
        可改的 = _arrive(hazards.STATUS_NOTIFIED)
        短 = client.post(
            "/supervision/extend",
            json={"hazard_no": 可改的, "due_phrase": LATER_PHRASE, "reason": "。"},
        )
        assert 短.status_code == 400
        assert "为什么" in 短.json()["user_msg"]

        # ② 状态不对、理由也太短 → 报**状态**那一条,不是理由
        不能改的 = _arrive(hazards.STATUS_OPEN)
        两样都不对 = client.post(
            "/supervision/extend",
            json={"hazard_no": 不能改的, "due_phrase": LATER_PHRASE, "reason": "。"},
        )
        assert 两样都不对.status_code == 409, "状态闸必须排在理由闸前面"

    def test_期限说法看不懂时原样透出dates那句人话(self, client: TestClient) -> None:
        """``DueParseError`` 的文本本来就是给工地师傅看的(还附了正确写法),
        重新包装一层反而不如原样透出。

        断法是**拿 dates.py 真抛出来的那句去比**,不是断某个关键词 ——
        断关键词的话,端点哪天在外面套一层"这次提交有问题"的壳,用例照样绿,
        而工友看到的是一句没有正确写法的废话。
        """
        from gyt.agents.schedule.dates import DueParseError, parse_due

        看不懂的说法 = "等有空吧"
        try:
            parse_due(看不懂的说法, today=TODAY)
        except DueParseError as exc:
            dates那句 = str(exc)
        else:  # pragma: no cover —— 真解析出来了说明词表变了,这条用例本身要重写
            pytest.fail(f"「{看不懂的说法}」现在解析得出来了,换一个真解析不出的说法")

        no = _arrive(hazards.STATUS_NOTIFIED)
        resp = client.post(
            "/supervision/extend",
            json={"hazard_no": no, "due_phrase": 看不懂的说法, "reason": REASON},
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_INPUT"
        assert resp.json()["user_msg"] == dates那句, "原样透出,不许在外面套壳"

    def test_缺期限时拒_不许留空(self, client: TestClient) -> None:
        """Codex#12:``due_date`` 为空的隐患永远进不了超期清单,也就永远没人来催。"""
        no = _arrive(hazards.STATUS_NOTIFIED)

        resp = client.post("/supervision/extend", json={"hazard_no": no, "reason": REASON})

        assert resp.status_code == 400
        assert hazards.fetch(no).due_date == FAR_FUTURE_ISO  # type: ignore[union-attr]

    def test_编号查不到回404(self, client: TestClient) -> None:
        resp = client.post(
            "/supervision/extend",
            json={"hazard_no": "GYT-H-不存在", "due_phrase": LATER_PHRASE, "reason": REASON},
        )
        assert resp.status_code == 404


# ===========================================================================
# POST /supervision/reassign —— 改归属
# ===========================================================================


class Test改归属:
    @pytest.mark.parametrize("status", hazards.REASSIGNABLE_STATUSES)
    def test_没签过文书的两档可以挪(self, client: TestClient, status: str) -> None:
        no = _arrive(status)

        resp = client.post("/supervision/reassign", json={"hazard_no": no, "project_id": "gyt-b7"})

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert (data["project_id"], data["previous_project_id"]) == ("gyt-b7", "gyt-a3")
        assert data["documents"] == [], "改归属不出文书"
        assert hazards.fetch(no).project_id == "gyt-b7"  # type: ignore[union-attr]

    def test_挪回未归属要传空串而不是省略这个键(self, client: TestClient) -> None:
        """🔴 **判据是"这个键在不在",不是"值空不空"。**

        空串是 D6 定下的合法一档(未归属),写成"非空才算传了"的话,
        「挪回未归属」这个动作在结构上就不存在 —— 而它恰恰是最常用的反向操作
        (拍的时候选错了工地)。
        """
        挪回去 = client.post(
            "/supervision/reassign",
            json={"hazard_no": _arrive(hazards.STATUS_OPEN), "project_id": ""},
        )
        assert 挪回去.status_code == 200, 挪回去.text
        assert 挪回去.json()["data"]["project_id"] == ""
        assert "未归属" in 挪回去.json()["user_msg"]

        漏参 = client.post(
            "/supervision/reassign", json={"hazard_no": _arrive(hazards.STATUS_OPEN)}
        )
        assert 漏参.status_code == 400, "缺键才是漏参"
        assert 漏参.json()["error_code"] == "INVALID_INPUT"

    @pytest.mark.parametrize(
        "status", [s for s in hazards.STATUSES if s not in hazards.REASSIGNABLE_STATUSES]
    )
    def test_签过文书的一律409(self, client: TestClient, status: str) -> None:
        """每份文书正文里都写着工地名 —— 改台账不会改那张已经发出去的纸。"""
        no = _new_hazard()
        _raw_status(no, status)

        resp = client.post("/supervision/reassign", json={"hazard_no": no, "project_id": "gyt-b7"})

        assert resp.status_code == 409
        assert hazards.fetch(no).project_id == "gyt-a3"  # type: ignore[union-attr]

    def test_目标工地已有同一条时_人话说的是重复登记而不是状态不对(
        self, client: TestClient
    ) -> None:
        """🔴 这一条是整组里最要紧的。

        db 层刻意**不吞** ``IntegrityError``(吞成 False 的话它和"状态不对"就成了
        同一个信号)。这里验的是端点真的把两件事说成了两句话 —— 说成"状态刚被改过"
        的话,监理回去查状态,而状态好好的,方向全错。
        """
        撞车用的照片, 撞车用的项 = "sha256-同一张照片", "未戴安全帽"
        _new_hazard(project_id="gyt-b7", photo_sha256=撞车用的照片, item=撞车用的项)
        我的 = _new_hazard(project_id="gyt-a3", photo_sha256=撞车用的照片, item=撞车用的项)
        hazards.confirm(我的)

        resp = client.post(
            "/supervision/reassign", json={"hazard_no": 我的, "project_id": "gyt-b7"}
        )

        assert resp.status_code == 409
        msg = resp.json()["user_msg"]
        assert "已经有这条隐患" in msg, f"这句话得说清是重复,不是状态:{msg}"
        assert "状态" not in msg
        assert hazards.fetch(我的).project_id == "gyt-a3"  # type: ignore[union-attr]

    def test_挪到它本来就在的工地时明说不用挪(self, client: TestClient) -> None:
        """不拦的话它会撞上自己那行的幂等键,回一句"目标工地已经有这条了"——
        技术上没错,但对着同一个工地说这句话只会让人以为系统坏了。"""
        no = _arrive(hazards.STATUS_OPEN)

        resp = client.post("/supervision/reassign", json={"hazard_no": no, "project_id": "gyt-a3"})

        assert resp.status_code == 409
        assert "本来就在" in resp.json()["user_msg"]

    def test_编号查不到回404(self, client: TestClient) -> None:
        resp = client.post(
            "/supervision/reassign", json={"hazard_no": "GYT-H-不存在", "project_id": "gyt-b7"}
        )
        assert resp.status_code == 404


# ===========================================================================
# GET /supervision/ingest-failures —— 登记失败的出口
# ===========================================================================


class Test登记失败清单:
    def test_没有失败时说的是好消息而不是空清单(self, client: TestClient) -> None:
        resp = client.get("/supervision/ingest-failures")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert (data["failures"], data["total"]) == ([], 0)
        assert "都进台账了" in resp.json()["user_msg"]

    def test_失败项列出来_而且那句人话说清该做什么(self, client: TestClient) -> None:
        """监理能做的动作只有一个:让人回去重拍。说不清这一点的话,
        他会去点每一条找按钮 —— 而这几条压根不是隐患行(没编号、没状态、点不开)。"""
        hazards.record_ingest_failure(
            project_id="gyt-a3", photo_id="a" * 32, item="临边无防护", reason="UNIQUE 约束撞了"
        )

        resp = client.get("/supervision/ingest-failures")

        data = resp.json()["data"]
        assert data["total"] == 1
        (行,) = data["failures"]
        assert (行["photo_id"], 行["item"], 行["project_id"]) == ("a" * 32, "临边无防护", "gyt-a3")
        assert "重新拍" in resp.json()["user_msg"]

    def test_内部细节不进响应(self, client: TestClient) -> None:
        """🔴 ``reason`` 是异常类型与约束名(``IngestFailureRow`` 头注:「不进人话」)。

        照抄给监理就是把「UNIQUE constraint failed: hazards.project_id」摆到
        工地负责人面前。要排查的人去看日志与库 —— 那儿有全文。
        """
        内部细节 = "sqlite3.IntegrityError: UNIQUE constraint failed: hazards.project_id"
        hazards.record_ingest_failure(
            project_id="gyt-a3", photo_id="b" * 32, item="未戴安全帽", reason=内部细节
        )

        resp = client.get("/supervision/ingest-failures")

        assert 内部细节 not in resp.text
        assert "IntegrityError" not in resp.text
        assert "reason" not in resp.json()["data"]["failures"][0]

    def test_按工地筛的三态_一态都不许塌(self, client: TestClient) -> None:
        """与隐患清单同一套三态。塌成两态的表现是「全部工地」悄悄变成「只有未归属」,
        界面上少一大半而没有任何报错。"""
        hazards.record_ingest_failure(
            project_id="gyt-a3", photo_id="c" * 32, item="有工地的", reason="x"
        )
        hazards.record_ingest_failure(project_id="", photo_id="d" * 32, item="未归属的", reason="x")

        不筛 = client.get("/supervision/ingest-failures").json()["data"]
        assert 不筛["total"] == 2 and 不筛["project_id"] is None

        只看未归属 = client.get("/supervision/ingest-failures?project_id=").json()["data"]
        assert [r["item"] for r in 只看未归属["failures"]] == ["未归属的"]
        assert 只看未归属["project_id"] == ""

        某个工地 = client.get("/supervision/ingest-failures?project_id=gyt-a3").json()["data"]
        assert [r["item"] for r in 某个工地["failures"]] == ["有工地的"]

    def test_截断时留下的是最近失败的那批(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """早的那些多半早就被重拍覆盖过了 —— 截断该从尾巴上切,不是从头上切。

        ``get_settings.cache_clear()`` 收尾放在 finally 里:漏了的话这个 2 会**留给
        同一进程里后面的用例**,而它们量的是隐患清单的条数 —— 表现是别处莫名其妙地少几行。
        """
        monkeypatch.setenv("GYT_SUPERVISION_LIST_MAX_ROWS", "2")
        get_settings.cache_clear()
        try:
            for i in range(4):
                hazards.record_ingest_failure(
                    project_id="gyt-a3", photo_id=f"{i:032x}", item=f"第{i}条", reason="x"
                )

            data = client.get("/supervision/ingest-failures").json()["data"]

            assert data["total"] == 4 and data["truncated"] is True
            assert [r["item"] for r in data["failures"]] == ["第3条", "第2条"]
        finally:
            get_settings.cache_clear()
