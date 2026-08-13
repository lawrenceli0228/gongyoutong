"""search_regulation 工具单测:命中带页码 / 查不到 EMPTY / 阈值过滤 / 空 query。

**不加载 BGE-M3、不碰真库**:打桩 tools._search 返回构造好的 (Document, 距离),
这样既快又稳,专测工具的判定逻辑(阈值、字段照抄、无依据路径)。
"""

from __future__ import annotations

from langchain_core.documents import Document

from gyt.agents.knowledge import tools


def _hit(
    text: str,
    source: str,
    page: int,
    dist: float,
    *,
    scope: str = "global",
    doc_type: str = "regulation",
    project_id: str = "",
) -> tuple[Document, float]:
    meta = {
        "source": source,
        "page": page,
        "scope": scope,
        "doc_type": doc_type,
        "project_id": project_id,
    }
    return Document(page_content=text, metadata=meta), dist


async def test_命中返回原文并照抄出处页码(monkeypatch):
    monkeypatch.setattr(
        tools,
        "_search",
        lambda q, k, where: [
            _hit("车道的净宽度和净空高度均不应小于 4.0m", "GB50016.pdf", 124, 0.50),
            _hit("消防车道的路面要求…", "GB50016.pdf", 125, 0.58),
        ],
    )
    result = await tools.search_regulation.ainvoke({"query": "消防车道要多宽"})
    assert result["ok"] is True
    passages = result["data"]["passages"]
    assert passages[0]["source"] == "GB50016.pdf"  # 完整文件名,评测按它判
    assert passages[0]["page"] == 124  # 页码照抄 metadata
    assert "4.0m" in passages[0]["text"]
    assert passages[0]["scope"] == "global"  # 作用域透出到结果
    assert "第 124 页" in result["user_msg"]
    assert "全局规范" in result["user_msg"]  # 出处标注带作用域


async def test_查不到返回EMPTY且信封无passages(monkeypatch):
    # 最近的一条也超过阈值 → 判无依据,失败信封里没有 data,模型无从编造出处。
    monkeypatch.setattr(tools, "_search", lambda q, k, where: [_hit("无关内容", "x.pdf", 1, 1.10)])
    result = await tools.search_regulation.ainvoke({"query": "工地食堂吃什么"})
    assert result["ok"] is False
    assert result["error_code"] == "EMPTY_RESULT"
    assert result["data"] is None
    assert "没查到" in result["user_msg"] or "查不到" in result["user_msg"]


async def test_阈值只留够近的候选(monkeypatch):
    # 一近一远:远的(> 阈值)要被过滤掉,只留近的。
    monkeypatch.setattr(
        tools,
        "_search",
        lambda q, k, where: [_hit("近的条文", "a.pdf", 3, 0.4), _hit("远的噪声", "a.pdf", 9, 0.95)],
    )
    result = await tools.search_regulation.ainvoke({"query": "某条文"})
    assert result["ok"] is True
    passages = result["data"]["passages"]
    assert len(passages) == 1
    assert passages[0]["page"] == 3


async def test_空query给可操作提示(monkeypatch):
    called = {"n": 0}

    def _boom(q, k, where):
        called["n"] += 1

    monkeypatch.setattr(tools, "_search", _boom)
    result = await tools.search_regulation.ainvoke({"query": "   "})
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert called["n"] == 0  # 空 query 不该真去查库


async def test_无项目上下文只查全局规范(monkeypatch):
    captured: dict = {}

    def fake_search(q, k, where):
        captured["where"] = where
        return [_hit("全局条文", "GB50016.pdf", 10, 0.4)]

    monkeypatch.setattr(tools, "_search", fake_search)
    result = await tools.search_regulation.ainvoke({"query": "消防车道要多宽"})

    assert result["ok"] is True
    # 不给 project_id → 只查全局
    assert captured["where"] == {"scope": "global"}


async def test_带项目查全局加该项目且标注可溯源(monkeypatch):
    captured: dict = {}

    def fake_search(q, k, where):
        captured["where"] = where
        return [
            _hit(
                "任务书里的条文",
                "A3施工任务书.pdf",
                3,
                0.4,
                scope="project",
                doc_type="task_book",
                project_id="gyt-a3",
            )
        ]

    monkeypatch.setattr(tools, "_search", fake_search)
    result = await tools.search_regulation.ainvoke({"query": "任务要求", "project_id": "gyt-a3"})

    # 给了 project_id → 查「全局 OR 该项目」,永不串别的项目
    assert captured["where"] == {
        "$or": [
            {"scope": "global"},
            {"$and": [{"scope": "project"}, {"project_id": "gyt-a3"}]},
        ]
    }
    assert result["data"]["passages"][0]["doc_type"] == "task_book"
    assert "任务书·gyt-a3" in result["user_msg"]  # 出处标清来自哪个项目的任务书
