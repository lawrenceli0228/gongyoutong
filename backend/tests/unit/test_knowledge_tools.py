"""search_regulation 工具单测:命中带页码 / 查不到 EMPTY / 阈值过滤 / 空 query。

**不加载 BGE-M3、不碰真库**:打桩 tools._search 返回构造好的 (Document, 距离),
这样既快又稳,专测工具的判定逻辑(阈值、字段照抄、无依据路径)。
"""

from __future__ import annotations

from langchain_core.documents import Document

from gyt.agents.knowledge import tools


def _hit(text: str, source: str, page: int, dist: float) -> tuple[Document, float]:
    return Document(page_content=text, metadata={"source": source, "page": page}), dist


async def test_命中返回原文并照抄出处页码(monkeypatch):
    monkeypatch.setattr(
        tools,
        "_search",
        lambda q, k: [
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
    assert "第 124 页" in result["user_msg"]


async def test_查不到返回EMPTY且信封无passages(monkeypatch):
    # 最近的一条也超过阈值 → 判无依据,失败信封里没有 data,模型无从编造出处。
    monkeypatch.setattr(tools, "_search", lambda q, k: [_hit("无关内容", "x.pdf", 1, 1.10)])
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
        lambda q, k: [_hit("近的条文", "a.pdf", 3, 0.40), _hit("远的噪声", "a.pdf", 9, 0.95)],
    )
    result = await tools.search_regulation.ainvoke({"query": "某条文"})
    assert result["ok"] is True
    passages = result["data"]["passages"]
    assert len(passages) == 1
    assert passages[0]["page"] == 3


async def test_空query给可操作提示(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(tools, "_search", lambda q, k: called.__setitem__("n", called["n"] + 1))
    result = await tools.search_regulation.ainvoke({"query": "   "})
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert called["n"] == 0  # 空 query 不该真去查库
