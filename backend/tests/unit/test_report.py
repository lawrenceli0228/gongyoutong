"""Report Agent(巡检记录渲染)的单元测试 —— 全程不联网。

重点不是覆盖率,是锁住两件「坏了就出错误文档」的事:
  · 文档里的隐患项/级别必须与 analyze 返回的契约数据**逐字一致**(留档材料的底线);
  · analyze 失败时**透传失败信封**,绝不渲染一份看起来正常的空文档。
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from docx import Document

from gyt.agents.report import REPORT_AGENT_NAME, build_report_agent
from gyt.agents.report import tools as report_tools
from gyt.core import artifacts
from gyt.core.errors import ErrorCode


class _FakeAnalyze:
    """替身:analyze_site_photo 的 BaseTool.ainvoke 入口。"""

    def __init__(self, envelope: Any) -> None:
        self.envelope = envelope
        self.seen: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any]) -> Any:
        self.seen.append(payload)
        return self.envelope


def _ok_envelope(**data: Any) -> dict[str, Any]:
    base = {
        "label": "violation",
        "violations": ["高空作业未系安全带", "未穿反光衣"],
        "severity": {"高空作业未系安全带": "重大", "未穿反光衣": "一般"},
        "max_severity": "重大",
        "note": "左侧工人腰部无挂钩;右侧远处一人看不清",
    }
    base.update(data)
    return {"ok": True, "data": base, "user_msg": "", "error_code": None}


def _patch(monkeypatch: pytest.MonkeyPatch, envelope: Any) -> _FakeAnalyze:
    fake = _FakeAnalyze(envelope)
    monkeypatch.setattr(report_tools, "analyze_site_photo", fake)
    return fake


async def _render(artifact_id: str = "a" * 32) -> dict[str, Any]:
    result = await report_tools.render_inspection_report.ainvoke({"artifact_id": artifact_id})
    assert isinstance(result, dict)
    return result


def _docx_text(report_id: str) -> str:
    """把落盘的 docx 读回来拼成一段文本,断言用。"""
    payload = artifacts.resolve(report_id).read_bytes()
    doc = Document(io.BytesIO(payload))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


async def test_文档内容与契约数据逐字一致(monkeypatch: pytest.MonkeyPatch) -> None:
    """留档材料的底线:隐患项、级别、备注一个字都不许走样。"""
    fake = _patch(monkeypatch, _ok_envelope())
    result = await _render("f" * 32)

    assert result["ok"] is True
    assert fake.seen == [{"artifact_id": "f" * 32}], "必须原样把编号交给 analyze"

    data = result["data"]
    text = _docx_text(data["report_id"])
    assert "工地安全巡检记录" in text
    assert "f" * 32 in text, "照片编号要进文档,追责时靠它对回原图"
    assert "高空作业未系安全带" in text and "重大" in text
    assert "未穿反光衣" in text and "一般" in text
    assert "左侧工人腰部无挂钩" in text, "note 是复核线索,必须落进文档"
    assert "持证安全员" in text, "初筛定位的免责句必须出现在每份产物里"
    assert data["max_severity"] == "重大"
    assert data["report_no"].startswith("GYT-")


async def test_登记为REPORT产物且可解析(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _ok_envelope())
    result = await _render()
    meta = artifacts.read_meta(result["data"]["report_id"])
    assert meta["kind"] == "REPORT"
    assert meta["ext"] == ".docx"


async def test_合规照片渲染为未见隐患(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        _ok_envelope(label="compliant", violations=[], severity={}, max_severity=None, note=""),
    )
    result = await _render()
    text = _docx_text(result["data"]["report_id"])
    assert "未见明显隐患" in text
    assert "未发现受控清单内的隐患" in text
    assert "未发现受控清单内的隐患" in result["user_msg"]


async def test_非工地照片如实写进文档并提醒(monkeypatch: pytest.MonkeyPatch) -> None:
    """not_site 也要出记录(留下"这张不是现场"的痕迹),但话必须说明白。"""
    _patch(
        monkeypatch,
        _ok_envelope(
            label="not_site", violations=[], severity={}, max_severity=None, note="办公室场景"
        ),
    )
    result = await _render()
    text = _docx_text(result["data"]["report_id"])
    assert "非作业现场照片" in text
    assert "核对" in result["user_msg"]


async def test_识别失败时透传失败信封而不是渲染空文档(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这条是底线。** 渲染一份"看起来正常"的空文档比不渲染危险得多 ——
    它会变成一份内容为「无隐患」的正式留档。"""
    failure = {
        "ok": False,
        "data": None,
        "user_msg": "没找到这张照片,请重新传一次。",
        "error_code": "NOT_FOUND",
    }
    _patch(monkeypatch, failure)
    result = await _render()
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.NOT_FOUND.value
    assert result["user_msg"] == "没找到这张照片,请重新传一次。"


async def test_词表外级别缺失时落为待定级(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch, _ok_envelope(violations=["未佩戴安全帽"], severity={}, max_severity="待定级")
    )
    result = await _render()
    text = _docx_text(result["data"]["report_id"])
    assert "未佩戴安全帽" in text and "待定级" in text


def test_能组装出report_agent() -> None:
    agent = build_report_agent()
    assert agent.name == REPORT_AGENT_NAME


def test_提示词存在且含红线() -> None:
    from gyt.agents.report import REPORT_DIR
    from gyt.core.base_agent import load_prompt

    body = load_prompt(REPORT_DIR)
    assert "render_inspection_report" in body
    assert "原样照抄" in body
    assert "日报" in body, "必须明说日报还没有,别让它口头许诺"


# ---------------------------------------------------------------------------
# 英雄链(inspection)的结构 —— 那条硬边必须真的是图边,不是提示词里的许愿
# ---------------------------------------------------------------------------


def test_英雄链的safety到report是写死的图边() -> None:
    """「拍照识违规 → 自动出记录」交给 LLM 决策第二跳,演示当场会随机漏掉
    出报告那一环(方案 D 决策原文)。这条测试锁住:它是图边,不是概率。"""
    from gyt.graph import build_inspection_chain

    chain = build_inspection_chain()
    assert chain.name == "inspection"

    drawable = chain.get_graph()
    assert {"safety", "report"} <= set(drawable.nodes)

    edges = {(e.source, e.target) for e in drawable.edges}
    assert ("safety", "report") in edges, "硬边丢了 —— 出报告变回了 LLM 的心情"
    assert ("__start__", "safety") in edges
    assert ("report", "__end__") in edges


def test_inspection已入登记表且路由词表同步() -> None:
    """加 Agent 的规矩:AGENT_REGISTRY 与 scorers.ROUTING_AGENTS 一起改。
    只改一边的话,routing.csv 里写 inspection 会当场炸,或者路由永远派不到它。"""
    from eval.scorers import ROUTING_AGENTS

    from gyt.graph import AGENT_REGISTRY, INSPECTION_AGENT_NAME

    spec = next((s for s in AGENT_REGISTRY if s.name == INSPECTION_AGENT_NAME), None)
    assert spec is not None, "inspection 没挂进登记表,Supervisor 永远派不到英雄链"
    assert "巡检" in spec.summary and "记录" in spec.summary
    assert INSPECTION_AGENT_NAME in ROUTING_AGENTS
