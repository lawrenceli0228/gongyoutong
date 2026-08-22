"""``RequireVisionTool`` —— safety 的防假账守卫(2026-08-22 补)。

单开一个文件而不是塞进 ``test_require_tool.py``:那份钉的是**公共件本身**的行为
(重试一次、pass/fail 两档、同步异步同源),而这份钉的是 **safety 这条泳道特有的两件事** ——
「首答判据在这条链上真的触发得到」和「那句合法的纯文本首答不许被顶替」。
两者一起改的机会几乎没有,混在一起只会让下一个人分不清哪条在守什么。

===========================================================================
它守的是什么(2026-08-22 线上实测抓到的)
---------------------------------------------------------------------------
velactora.com 上连传四张现场照片,后端日志里 ``analyze_site_photo`` 只出现**两次**,
而四张照片的产物**都登记成功了**。也就是说后两张的识图一次都没发生,
而模型把 ``prompt.md`` 里那段示范句原样背了出来(两张不同的照片、逐字相同的措辞),
还补了一句「这张我登记成「待确认」隐患了」—— 台账里没有,失败清单里也没有。

在这件守卫之前,**safety 是四个动作型 Agent 里唯一裸奔的那个**,而它是头号动作。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from gyt.agents.safety.guard import RequireVisionTool
from gyt.core.require_tool import RequireToolCall


class _FakeRequest:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def override(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest(kwargs.get("messages", self.messages))


class _FakeResponse:
    def __init__(self, result: list[Any]) -> None:
        self.result = result


def _plain(text: str) -> _FakeResponse:
    return _FakeResponse([AIMessage(content=text)])


def _with_tool() -> _FakeResponse:
    return _FakeResponse(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "analyze_site_photo", "args": {"artifact_id": "a" * 32}, "id": "v1"}
                ],
            )
        ]
    )


class _ScriptedHandler:
    """按剧本逐次返回,并记下每次被调时收到的消息数(用来判断有没有追加 nudge)。"""

    def __init__(self, script: list[_FakeResponse]) -> None:
        self.script = list(script)
        self.seen: list[int] = []

    async def __call__(self, request: Any) -> _FakeResponse:
        self.seen.append(len(request.messages))
        return self.script[min(len(self.seen) - 1, len(self.script) - 1)]


# ---------------------------------------------------------------------------
# 🔴 最要紧的一条:首答判据在 safety 这条链上真的触发得到
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("场景", "交接工具名"),
    [
        ("supervisor 直派", "transfer_to_safety"),
        ("英雄链(safety 是子图第一个节点)", "transfer_to_inspection"),
    ],
)
async def test_两条进来的路都触发得到首答判据(场景: str, 交接工具名: str) -> None:
    """🔴 **这条用例是这件守卫存在的前提,别删。**

    2026-08-10 有人给 report 套过首答判据,而 report 是英雄链的**第二跳** ——
    它首答前最后一条是 safety 的纯文本,``_is_turn_start`` 恒为 False,
    那件守卫**一次都没触发过**,而且不报错。CLAUDE.md 把那次记成
    「地图漏一件,下一个人就照着抄错判据」。

    safety 与它相反:两条进来的路最后一条都是 ``transfer_to_*`` 交接回执,
    判据认得出。这条用例把「相反」这件事钉死,而不是让人照着推理相信它。
    """
    handler = _ScriptedHandler([_plain("这张照片有 2 处问题:有人没戴安全帽…"), _with_tool()])
    request = _FakeRequest(
        messages=[
            HumanMessage(content="看看这张照片(照片编号:" + "a" * 32 + ")"),
            AIMessage(content="", tool_calls=[{"name": 交接工具名, "args": {}, "id": "h1"}]),
            ToolMessage(
                content=f"Successfully transferred to {场景}",
                tool_call_id="h1",
                name=交接工具名,
            ),
        ]
    )

    response = await RequireVisionTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 2, f"{场景}:首答没调工具却没被打回 —— 守卫在这条路上失效了"
    assert handler.seen[1] > handler.seen[0], "重试那一次要把 nudge 追加进去"
    assert response.result[0].tool_calls, "重试后该调工具了"


async def test_把示范句原样背出来的那种答话会被打回() -> None:
    """线上实测那句的原样复现 —— 它读起来完全像一份真结果,而工具一次都没调。"""
    编的那份 = (
        "师傅,这张照片有 2 处问题:\n"
        "有人没戴安全帽(较大)…\n材料堆放混乱(一般)…\n"
        "这张我登记成「待确认」隐患了,等监理确认后进正式流程。"
    )
    handler = _ScriptedHandler([_plain(编的那份), _with_tool()])
    request = _FakeRequest(messages=[HumanMessage(content="这张有什么问题")])

    await RequireVisionTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "这正是要拦的那一种,不许放过"


# ---------------------------------------------------------------------------
# 那句合法的纯文本首答不许被顶替
# ---------------------------------------------------------------------------


async def test_没给编号时问师傅要_重试后仍放行不顶替() -> None:
    """``prompt.md`` 明写着:「用户说"看看这张照片"但没给编号 → 用一句话问他要」。

    🔴 这是 safety 选 ``on_give_up="pass"`` 而不是像 report 那样顶替的**全部理由**。
    顶替掉的话,工友没给编号时收到的是一句"系统校验"的话,而不是「把编号发我」——
    他会卡在那儿不知道该干嘛。与 attendance 那件守卫同一种误伤。
    """
    要编号 = "师傅,照片编号发我一下。"
    handler = _ScriptedHandler([_plain(要编号), _plain(要编号)])
    request = _FakeRequest(messages=[HumanMessage(content="看看这张照片")])

    response = await RequireVisionTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 2, "先打回一次是对的(它分不清这次是要编号还是在编)"
    assert 要编号 in str(response.result[0].content), (
        "重试之后必须放行原话 —— 顶替掉的话工友不知道该发什么"
    )


async def test_念真回执不打回() -> None:
    """刚拿到 analyze_site_photo 的结果、正在把它讲成人话 —— 这时纯文本是合法的。

    打回它反而制造循环:模型已经调过工具了,再逼它调一次只会重复识图(那是真花钱的)。
    """
    handler = _ScriptedHandler([_plain("这张照片查出来 3 处隐患,其中一处是重大隐患…")])
    request = _FakeRequest(
        messages=[
            HumanMessage(content="看看这张照片"),
            AIMessage(
                content="", tool_calls=[{"name": "analyze_site_photo", "args": {}, "id": "v1"}]
            ),
            ToolMessage(content='{"ok": true}', tool_call_id="v1", name="analyze_site_photo"),
        ]
    )

    response = await RequireVisionTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 1, "念真回执不许打回"
    assert "3 处隐患" in str(response.result[0].content), "更不许把真结果顶替掉"


async def test_首答就调工具_直接放行() -> None:
    handler = _ScriptedHandler([_with_tool()])
    request = _FakeRequest(messages=[HumanMessage(content="看看这张照片")])

    await RequireVisionTool().awrap_model_call(request, handler)

    assert len(handler.seen) == 1


# ---------------------------------------------------------------------------
# 参数与话术
# ---------------------------------------------------------------------------


def test_挂的是首答判据而不是编号溯源() -> None:
    """safety 编的假账**不带任何编号** —— 它编的是隐患清单本身。

    拿 ``RequireReceiptSource`` 去挡的话一个都认不出、静默全放行 ——
    而那正是同一天在 report 那边实测到的坏法(模型学会了「不报编号,只说已经出好了」)。
    """
    guard = RequireVisionTool()

    assert isinstance(guard, RequireToolCall)
    assert guard.agent_name == "safety"
    assert guard.on_give_up == "pass"
    assert guard.give_up_message == ""


def test_nudge_点名工具但不给隐患项举例() -> None:
    """⚠️ nudge 是以 ``HumanMessage`` 追加进请求的 —— 在里面写「未戴安全帽」这类举例,
    等于在模型正要编的时候再递给它一份现成的词。

    与 supervision 那条「nudge 里绝不许写完整编号样例」是同一个道理,
    只是那边递的是编号、这边递的是隐患项。
    """
    nudge = RequireVisionTool().nudge

    assert "analyze_site_photo" in nudge, "话术要点名具体工具,不然模型不知道调哪个"
    for 隐患项 in ("未戴安全帽", "临边无防护", "未穿反光衣", "材料堆放混乱", "高空作业未系安全带"):
        assert 隐患项 not in nudge, f"nudge 里不许出现隐患项举例:{隐患项}"
