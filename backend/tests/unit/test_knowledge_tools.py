"""search_regulation 工具单测:命中带页码 / 查不到 EMPTY / 阈值过滤 / 空 query。

**不加载 BGE-M3、不碰真库**:打桩 tools._search 返回构造好的 (Document, 距离),
这样既快又稳,专测工具的判定逻辑(阈值、字段照抄、无依据路径)。
"""

from __future__ import annotations

from langchain_core.documents import Document

from gyt.agents.knowledge import tools
from gyt.core.require_tool import RequireEvidenceCitation, RequireToolCall


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


async def test_前端选中当前工地经config兜底作用域(monkeypatch):
    """选了当前项目但问句没点名 → config.configurable 里的项目应把检索限到「全局+该项目」。

    修的就是「选了当前项目但问答仍是全局」:project_id 入参为空时,回退到前端注入的当前工地。
    """
    captured: dict = {}

    def fake_search(q, k, where):
        captured["where"] = where
        return [_hit("项目条文", "本项目规范.pdf", 2, 0.4, scope="project", project_id="gyt-a3")]

    monkeypatch.setattr(tools, "_search", fake_search)
    result = await tools.search_regulation.ainvoke(
        {"query": "消防车道要多宽"},
        config={"configurable": {tools.PROJECT_CONFIG_KEY: "gyt-a3"}},
    )

    assert result["ok"] is True
    assert captured["where"] == {
        "$or": [
            {"scope": "global"},
            {"$and": [{"scope": "project"}, {"project_id": "gyt-a3"}]},
        ]
    }


async def test_入参project_id优先于config当前工地(monkeypatch):
    """用户在问句里点名了别的项目 → 以入参为准,不被前端选中的当前工地覆盖。"""
    captured: dict = {}

    def fake_search(q, k, where):
        captured["where"] = where
        return [_hit("条文", "x.pdf", 1, 0.4, scope="project", project_id="gyt-b1")]

    monkeypatch.setattr(tools, "_search", fake_search)
    await tools.search_regulation.ainvoke(
        {"query": "任务要求", "project_id": "gyt-b1"},
        config={"configurable": {tools.PROJECT_CONFIG_KEY: "gyt-a3"}},
    )

    # 入参 gyt-b1 胜出,config 的 gyt-a3 被忽略
    assert captured["where"] == {
        "$or": [
            {"scope": "global"},
            {"$and": [{"scope": "project"}, {"project_id": "gyt-b1"}]},
        ]
    }


async def test_config无当前工地时仍只查全局(monkeypatch):
    """config 里没有项目键(或空)→ 行为与「无项目上下文」一致,只查全局规范。"""
    captured: dict = {}

    def fake_search(q, k, where):
        captured["where"] = where
        return [_hit("全局条文", "GB.pdf", 5, 0.4)]

    monkeypatch.setattr(tools, "_search", fake_search)
    await tools.search_regulation.ainvoke(
        {"query": "消防车道要多宽"},
        config={"configurable": {tools.PROJECT_CONFIG_KEY: ""}},
    )

    assert captured["where"] == {"scope": "global"}


def test_knowledge_agent首答强制调用规范检索工具(monkeypatch):
    """红线不能只写在 prompt 里:首答没调工具时必须由中间件拦住。"""
    import gyt.agents.knowledge as knowledge

    captured: dict = {}

    def fake_create_gyt_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(knowledge, "create_gyt_agent", fake_create_gyt_agent)
    knowledge.build_knowledge_agent()

    guards = captured["extra_middleware"]
    assert len(guards) == 2
    guard = guards[0]
    assert isinstance(guard, RequireToolCall)
    assert guard.agent_name == "knowledge"
    assert guard.on_give_up == "fail"
    assert "search_regulation" in guard.nudge
    assert "检索" in guard.give_up_message
    citation_guard = guards[1]
    assert isinstance(citation_guard, RequireEvidenceCitation)
    assert citation_guard.tool_name == "search_regulation"
    assert "出处" in citation_guard.give_up_message


# ---------------------------------------------------------------------------
# 同源守卫:引用行与「查不到」措辞必须留在中文(W12 定案 甲,2026-08-18)
# ---------------------------------------------------------------------------


def test_引用形态与拒答措辞在提示词里仍是中文() -> None:
    """`eval/scorers.py` 用**中文写死的两张表**给 rag 判分,提示词必须跟它对齐。

    哪天有人「顺手」把 knowledge 的引用行或拒答句翻成英文(英文答话上线后
    这个诱惑很大),后果是两个方向的:

      · ``NO_ANSWER_MARKERS`` 认不出英文拒答 → **老实拒答被判成失败**(误伤);
      · ``CITATION_SHAPES`` 认不出英文引用 → **嘴上说查不到却编了个英文出处,
        判分会放它过去**(放行编造,比误伤更糟)。

    而 ``rag.csv`` 20 行全是中文提问,**评测永远走不到英文那条路** ——
    所以这个退化不会被任何分数暴露出来。这条测试是那个约定唯一的自动化守卫。
    """
    import re

    from eval.scorers import CITATION_SHAPES, NO_ANSWER_MARKERS

    from gyt.agents.knowledge import KNOWLEDGE_DIR
    from gyt.core.base_agent import load_prompt

    body = load_prompt(KNOWLEDGE_DIR)

    # ① **钉那个模板本身**,不是「文件里随便哪儿有中文引用形态」。
    #
    # ⚠️ 这条一开始写的就是后者(`any(re.search(shape, body) …)`),而它是个
    #    **假守卫**:2026-08-18 变异验证时把模板真翻成 `— Per <source>, p. <page>`,
    #    测试照绿 —— 因为提示词里还留着中文**示例**
    #    (「—— 依据《建筑施工安全检查标准》第 45 页」)和「说话方式」那节的
    #    「(《…》第 X 页)」,示例自己把断言满足了。
    #    驱动模型行为的是模板,不是示例,所以要钉模板的占位符形态。
    template = "《<source>》第 <page> 页"
    assert template in body, (
        f"knowledge 提示词里的引用模板 `{template}` 没了 —— 很可能被翻成了英文。"
        "后果见本测试 docstring:no_answer 行会**放行编造**。"
    )
    # 模板本身还得是 CITATION_SHAPES 认得的形态(两边同源的那一半)
    assert any(re.search(shape, template) for shape in CITATION_SHAPES), (
        "引用模板与 scorers.CITATION_SHAPES 对不上了 —— 改了一边没改另一边。"
    )

    # ② 拒答措辞必须落在 NO_ANSWER_MARKERS 白名单里(提示词让模型照说的那句)
    assert any(marker in body for marker in NO_ANSWER_MARKERS), (
        "knowledge 提示词里已经没有 NO_ANSWER_MARKERS 里的规定措辞了 —— "
        "老实拒答会被 rag 判分当成失败。"
    )

    # ③ 正面钉住那条例外说明还在:它是给下一个人看的唯一线索
    assert "任何语言下都写中文" in body or "仍写中文" in body, (
        "「英文模式下引用与拒答保持中文」这条例外的说明没了。说明一没,下一个人一定会去翻译它。"
    )
