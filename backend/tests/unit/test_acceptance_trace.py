"""``scripts/acceptance_trace.py`` 的离线验证 —— 不起服务器、不连 :2024、不花钱。

真正的验收标准只有一条,而且只有人能判:
    **把一份 .txt 打印出来给一个没跟这次会话的人看,他 30 秒内能不能说出下一步查哪儿,
      并且**不会被带偏**。**
下面这些机器断言是那条标准的代理 —— 它们锁住的是「判据在不在文件里」「假线索有没有
被删干净」,锁不住「排版读不读得懂」。改了渲染版式,除了跑这些测试,还得自己打印一份看一眼。

===========================================================================
本文件为什么有「真机形状」这么一组用例
---------------------------------------------------------------------------
上一版的 fixture 里塞了子 Agent 的 ``add_task`` tool_call 消息,于是
「核心判据:调了工具和没调工具一眼分得出」这条测试**永远绿**。
但那种 msgs **真机上造不出来**:``graph.py`` 的 ``OUTPUT_MODE="last_message"`` 会让
langgraph_supervisor 在回灌 supervisor 时把子 Agent 带 tool_calls 的那条 AI 消息
和它的 ToolMessage 一起砍掉(``_process_output``,2026-08-10 读过安装包原文,
并用同版本库起真图复核过)。测试绿着,它保护的行为却从未发生 —— 这比没测试更坏。

所以下面 ``真机形状`` 那一组是**按顶层 messages 真正长什么样**造的:
子 Agent 只留得下一条纯文本。它锁的是新规矩:**那种形状下,这份 dump 不许给出
「模型凭记忆编的」这类结论**。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

# 按文件路径加载,理由同 test_acceptance_dates.py:scripts/ 不是包,而且它旁边的
# live_acceptance.py 一 import 就会真跑验收。
_BACKEND: Final[Path] = Path(__file__).resolve().parents[2]
_MODULE_PATH: Final[Path] = _BACKEND / "scripts" / "acceptance_trace.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("acceptance_trace_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None, f"加载不了 {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tr: Final[ModuleType] = _load()


@pytest.fixture(autouse=True)
def _战果清零() -> None:
    """``trace_tally()`` 是模块级计数,不清零测试之间会互相污染。"""
    tr.reset_trace_tally()


# ===========================================================================
# 一、真机形状 —— 顶层 messages 真正长什么样
# ===========================================================================
#
# 七条,一个回合的完整来回。关键是第 4 条:schedule **只**留得下一条纯文本。
# 它到底调没调 add_task,在这份 msgs 里**没有任何痕迹**——这正是要锁住的前提。

真机形状: Final[list[dict[str, Any]]] = [
    {"type": "human", "content": "给我建个任务:明天上午复检三层钢筋"},
    {
        "type": "ai",
        "name": "supervisor",
        "content": "",
        "tool_calls": [{"name": "transfer_to_schedule", "args": {}, "id": "c0"}],
    },
    {
        "type": "tool",
        "name": "transfer_to_schedule",
        "tool_call_id": "c0",
        "content": "Successfully transferred to schedule",
    },
    # ↓ 子 Agent 这一整个回合,顶层就剩这一条。tool_calls / ToolMessage 已经被砍掉了。
    {"type": "ai", "name": "schedule", "content": "记上了:T1 复检三层钢筋,8月11日(周二)。"},
    {
        "type": "ai",
        "name": "schedule",
        "content": "",
        "tool_calls": [{"name": "transfer_back_to_supervisor", "args": {}, "id": "c1"}],
    },
    {
        "type": "tool",
        "name": "transfer_back_to_supervisor",
        "tool_call_id": "c1",
        "content": "Successfully transferred back to supervisor",
    },
    {"type": "ai", "name": "supervisor", "content": "记上了:T1 复检三层钢筋,8月11日(周二)。"},
]
"""真机形状:``add_handoff_back_messages=True`` 生成的那对回程消息也在里面。
子 Agent 有没有调工具,从这份 msgs 里**看不出来**,永远看不出来。"""


# ===========================================================================
# 二、三种病 —— 按真机形状造(子 Agent 的工具痕迹一律不出现)
# ===========================================================================

_乱日期头: Final[list[dict[str, Any]]] = [
    *真机形状,
    {"type": "human", "content": "T2 改到猴年马月"},
    {
        "type": "ai",
        "name": "supervisor",
        "content": "",
        "tool_calls": [{"name": "transfer_to_schedule", "args": {}, "id": "c2"}],
    },
    {
        "type": "tool",
        "name": "transfer_to_schedule",
        "tool_call_id": "c2",
        "content": "Successfully transferred to schedule",
    },
]

甲_没调工具编回执: Final[list[dict[str, Any]]] = [
    *_乱日期头,
    {"type": "ai", "name": "schedule", "content": "改好了,T2 已经改到猴年马月。"},
]
"""病甲:模型一个业务工具都没调,直接编了句回执。

注意:这份 msgs 和「真调了工具、工具报了错、模型如实转述」那份**在顶层长得一模一样**
(都只剩一条纯文本)。所以这份 dump **没有资格**判它是哪一种 —— 锁死这一点就是本轮的活。"""

乙_参数传错: Final[list[dict[str, Any]]] = [
    *_乱日期头,
    {"type": "ai", "name": "schedule", "content": "T2 那条没改成:「猴年马月」这个期限我没看懂。"},
]
"""病乙:参数传错、工具如实报错、模型也如实转述。顶层同样只剩这一条纯文本。"""

丙_工具报错模型说成功: Final[list[dict[str, Any]]] = [
    {"type": "human", "content": "把 T2 销了"},
    {
        "type": "ai",
        "name": "supervisor",
        "content": "",
        "tool_calls": [{"name": "complete_task", "args": {"task_id": "T2"}, "id": "c9"}],
    },
    {
        "type": "tool",
        "name": "complete_task",
        "tool_call_id": "c9",
        "content": (
            '{"ok": false, "data": null, "user_msg": "没找到 T2 这条活。", '
            '"error_code": "NOT_FOUND"}'
        ),
    },
    {"type": "ai", "name": "supervisor", "content": "销好了,T2 已完成。"},
]
"""病丙:工具真调了、真返回 ``ok:false``,而模型把失败转述成了成功。

RequireToolCall 对它完全无效(工具确实调了),读模型那句话也看不出来 ——
只有把 Envelope 摊开才现形。这里把工具调用放在 supervisor 层,是因为
**子 Agent 的 ToolMessage 到不了顶层**:能被这份轨迹抓到的丙,只有这一种。"""

畸形: Final[list[Any]] = [
    {"type": "human", "content": "正常的第一条"},
    {"content": "缺 type 键"},
    {"type": "ai", "name": "safety", "content": [{"type": "text", "text": "多模态块"}]},
    "我根本不是字典",
    {"type": "ai", "name": "schedule", "content": "尾巴这条必须还在", "tool_calls": None},
]
"""丁:各种形状不对的东西。真机上不会出现(msgs 是 json.load 出来的),
造它是为了验一条纪律:**取证件不许比被取证的还脆**。"""


def _dump(msgs: list, out: Path, label: str = "A8 乱日期", **kw: Any) -> str:
    path = tr.dump_trace(label=label, msgs=msgs, out_dir=out, **kw)
    assert path is not None, "dump 应该落下东西"
    return path.read_text("utf-8")


# ===========================================================================
# 三、核心:真机形状下不许给误导性结论
# ===========================================================================


def test_真机形状下不许说模型凭记忆编的(tmp_path: Path) -> None:
    """本轮修的那条 CRITICAL 就在这里。

    上一版头部有一行 ``★ 本轮业务工具  (一个都没调)``,底下配着
    「= 模型凭记忆编的,先查提示词和 guard」。真机上子 Agent 的工具调用**永远**到不了
    顶层,所以那行恒为空、那句解读恒被打出来 —— 一条**恒真的假线索**,
    能把查问题的人往错方向带一整天。

    这条测试锁的是:那类结论从此不许出现,一个字都不许。
    """
    text = _dump(甲_没调工具编回执, tmp_path)
    for 假线索 in ("凭记忆编的", "★ 本轮业务工具", "累计业务工具", "先查提示词和 guard"):
        assert 假线索 not in text, f"这句已经被证伪了,不许再印:{假线索}"


def test_真机形状下必须把局限当面说清楚(tmp_path: Path) -> None:
    """删掉假线索还不够 —— 不说清楚,读的人会默认「时间线里没有 = 没调过」,
    等于把假结论从纸上搬进了他脑子里。所以局限要写在头部,而且要给出下一步去哪儿查。"""
    text = _dump(甲_没调工具编回执, tmp_path)
    assert "看不见子 Agent 的工具调用" in text
    assert 'output_mode="last_message"' in text
    assert "不等于" in text  # 「没有业务工具」不等于「没调工具」
    assert "查台账库" in text  # 光说局限不够,得把下一步指出去


def test_交接那一行留着而且是真的(tmp_path: Path) -> None:
    """交接计数不经过 ``_process_output`` 那把刀,顶层留得住。

    它是这份轨迹里**唯一**可信的工具计数,所以不但要留,还得在旁边写明它凭什么可信 ——
    不然下一个人看它跟被删掉的那行长得像,顺手一起清了。

    夹具**必须用带 handoff-back 的那一轮**:2026-08-10 复验抓到,旁边那句理由原本写的是
    「交接由 supervisor 本人发起」,而 ``transfer_back_to_*`` 挂的是子 Agent 的名字
    (``handoff.py:135`` 的 ``name=agent_name``),半句是错的;当时的夹具恰好没有
    handoff-back,于是测试没照出来。夹具选错,断言再多也是白断。
    """
    text = _dump(真机形状, tmp_path)
    assert "本轮交接            " in text
    # 两类交接都出现在计数里 —— 理由句必须把两类的来路分开讲
    assert "transfer_to_schedule×1" in text
    assert "transfer_back_to_supervisor×1" in text
    assert "supervisor 本人的消息" in text
    assert "挂的是子 Agent 的名字" in text
    assert "handoff.py:128" in text  # 说了「哪儿拼上去的」,读的人才查得动
    # 反向护栏:那句被证伪的旧理由不许再回来
    assert "交接由 supervisor 本人发起" not in text


def _头部(text: str) -> list[str]:
    """只取头部摘要,并抹掉三行「本来就该不一样」的:落盘时间和两条判据原文。

    剩下的每一行都是**这份轨迹对这条断言下的判断**。两种病的这部分必须逐字相同。
    """
    head = text.split("--- 时间线", 1)[0]
    抹掉 = ("落盘时间", "  final()", "  sched_text()")
    return [line for line in head.splitlines() if not line.startswith(抹掉)]


def test_两种病在顶层长得一样_轨迹不许替人下结论(tmp_path: Path) -> None:
    """甲(没调工具编回执)和乙(真调了、工具报错、模型如实转述)在顶层**只差模型那句话**。

    子 Agent 的工具痕迹两份都没有,这份轨迹根本拿不到区分它们的信息 —— 那就一个字都不许猜。
    用「两份的头部判断逐字相同」来锁,比逐个禁词更硬:以后谁再往头部塞一句
    「看起来像没调工具」,这条会立刻红。
    """
    # 分开两个目录:同目录下第二份会被加 -2 后缀,那点差异是防覆盖机制,不是判断差异。
    甲 = _dump(甲_没调工具编回执, tmp_path / "甲")
    乙 = _dump(乙_参数传错, tmp_path / "乙")
    assert _头部(甲) == _头部(乙), "两种病顶层信息一样,头部就不许说出不一样的话"
    # 该留的还是留着:模型各自说了什么,原样在时间线里
    assert "改好了,T2 已经改到猴年马月。" in 甲
    assert "「猴年马月」这个期限我没看懂。" in 乙


# ===========================================================================
# 四、本轮可见的工具报错(病丙)
# ===========================================================================


def test_工具返回ok_false而模型说成功_报错要被抓出来(tmp_path: Path) -> None:
    """病丙是三种里最难抓的:工具真调了(guard 拦不住)、模型嘴上说成功。

    Envelope 摊开才现形,所以头部要把 error_code 和 user_msg 直接摆出来。
    """
    text = _dump(丙_工具报错模型说成功, tmp_path, label="A7 销号")
    assert "本轮可见的工具报错  1 条:" in text
    assert "complete_task → NOT_FOUND:没找到 T2 这条活。" in text
    # 模型那句自相矛盾的话也在,两句摆一起才叫证据
    assert "销好了,T2 已完成。" in text


def test_报错那行的命名必须诚实(tmp_path: Path) -> None:
    """叫「本轮工具报错」会让人以为「空 = 没报错」,那又是一条恒真的假线索。

    子 Agent 的 ToolMessage 根本不在轨迹里,这一行只覆盖轨迹里真有的那些 ——
    所以名字里必须有「可见的」,而且旁边要写明它覆盖不到什么。
    """
    text = _dump(甲_没调工具编回执, tmp_path)
    assert "本轮可见的工具报错  (没有)" in text
    assert "空≠没报错" in text
    assert "本轮工具报错  " not in text  # 不许出现不带限定的那个叫法


def test_交接消息不是Envelope_不许被当成解析失败抛出去(tmp_path: Path) -> None:
    """交接工具的 ToolMessage 是一句英文纯文本,``json.loads`` 必然失败 ——
    那是正常现象。跳过就好,不许抛,也不许把它算成一条报错。"""
    assert tr.visible_tool_errors(真机形状) == []
    assert tr.visible_tool_errors(丙_工具报错模型说成功) == [
        "complete_task → NOT_FOUND:没找到 T2 这条活。"
    ]


def test_只在本轮里数报错_不把上一轮的算进来(tmp_path: Path) -> None:
    """msgs 是整条线程的累计历史。上一轮报过的错混进来,会让人以为这一轮也报了。"""
    msgs = [*丙_工具报错模型说成功, {"type": "human", "content": "那再建一条"}]
    msgs.append({"type": "ai", "name": "supervisor", "content": "建好了。"})
    text = _dump(msgs, tmp_path, label="A9 新一轮")
    assert "本轮可见的工具报错  (没有)" in text
    assert "NOT_FOUND" in text  # 上一轮那条仍在时间线里,只是不算进本轮


# ===========================================================================
# 五、台账快照 —— 「活到底办没办」在这个文件里唯一问得出答案的地方
# ===========================================================================


def _建库(path: Path, rows: list[tuple[int, str, str | None, str]]) -> None:
    """造一个形状和 db/tasks.py 一致的临时台账(只建测试要用的四列)。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, due_date TEXT, status TEXT)"
        )
        conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", rows)


def test_台账快照真的落进人读版(tmp_path: Path) -> None:
    """回执说什么不算,库里有没有才算 —— 这句是 D 组的原话,这里把它落到取证件里。"""
    db = tmp_path / "gyt.sqlite3"
    _建库(
        db,
        [
            (1, "复检三层钢筋", "2026-08-11", "done"),
            (2, "补齐三层临边防护", "2026-08-12", "open"),
            (3, "整改:未戴安全帽", None, "open"),
        ],
    )
    text = _dump(甲_没调工具编回执, tmp_path / "out", ledger_db=db)
    assert "台账快照" in text
    assert str(db) in text  # 读的是哪个库要写明,不然「库找错地方」这条查不了
    assert "1 | 复检三层钢筋 | 2026-08-11 | done" in text
    assert "3 | 整改:未戴安全帽 | (空) | open" in text  # due_date 为 NULL 也要看得懂


def test_库不存在只写一行人话_不抛栈也不建空库(tmp_path: Path) -> None:
    """只读 URI 的理由:普通 connect 碰上不存在的路径会当场建个 0 字节空库,
    再报 no such table,把「库找错地方了」说成「表没建」,排查方向全错。"""
    缺席 = tmp_path / "根本没有这个库.sqlite3"
    text = _dump(甲_没调工具编回执, tmp_path / "out", ledger_db=缺席)
    assert "这个文件不在" in text
    assert "Traceback" not in text
    assert not 缺席.exists(), "只读模式下不许把库文件创建出来"


def test_表还没建也只写一行人话(tmp_path: Path) -> None:
    """文件在、tasks 表没有 = 后端一条任务都没记成。这也是个结论,得说清楚。"""
    空库 = tmp_path / "空.sqlite3"
    with sqlite3.connect(空库) as conn:
        conn.execute("CREATE TABLE 别的表 (x INTEGER)")
    text = _dump(甲_没调工具编回执, tmp_path / "out", ledger_db=空库)
    assert "读不出来" in text
    assert "no such table" in text  # 底层原话照抄,别自己转述
    assert "后端一条任务都没记成" in text


def test_表在但一行都没有_要说出来而不是留白(tmp_path: Path) -> None:
    """留白会被读成「这段没渲染出来」。空表本身是强信息:活一条都没落库。"""
    db = tmp_path / "空表.sqlite3"
    _建库(db, [])
    text = _dump(甲_没调工具编回执, tmp_path / "out", ledger_db=db)
    assert "一行任务都没有" in text


def test_没传库路径就整段不出现(tmp_path: Path) -> None:
    """E/F 组那些非台账断言本来就没有库可看。硬塞一段「库不存在」只会制造噪音。"""
    text = _dump(甲_没调工具编回执, tmp_path, label="E2 规范检索")
    assert "台账快照(库里现在到底有什么" not in text
    assert "库文件" not in text
    assert tr.ledger_snapshot(None) == []


def test_库脏到几百行时快照要截断(tmp_path: Path) -> None:
    """头部的价值在 30 秒读完。一份跑脏了的陈年库能把它冲垮 —— 截断并说明还有更多。"""
    db = tmp_path / "脏库.sqlite3"
    _建库(db, [(i, f"活{i}", "2026-08-11", "open") for i in range(1, 121)])
    text = _dump(甲_没调工具编回执, tmp_path / "out", ledger_db=db)
    assert "只列了前 50 行" in text
    assert "50 | 活50" in text
    assert "51 | 活51" not in text


def test_库读不出来不许连累整份轨迹(tmp_path: Path) -> None:
    """取证件不许比被取证的还脆:台账断言红了、库又恰好坏了,头部和时间线都得照常出。"""
    坏库 = tmp_path / "坏.sqlite3"
    坏库.write_bytes("这根本不是 sqlite 文件".encode() * 20)
    text = _dump(甲_没调工具编回执, tmp_path / "out", ledger_db=坏库)
    assert "读不出来" in text
    assert "头部摘要算不出来" not in text  # 没有降级
    assert "改好了,T2 已经改到猴年马月。" in text  # 时间线照常


# ===========================================================================
# 六、本机绝对路径提示要动态,不许每份都印
# ===========================================================================


def test_没有绝对路径就不打那行警告(tmp_path: Path) -> None:
    """上一版把「⚠ 本文件含本机绝对路径」写死印在每一份轨迹上。

    2026-08-10 复核:report/tools.py 那个 ``"path": str(stored)`` 只进 Envelope.data →
    ToolMessage,而 ToolMessage 正是被 last_message 砍掉的东西;report/prompt.md 里
    ``path`` / ``路径`` 零命中,模型没被要求转述路径。于是真机形状的轨迹里一个绝对路径
    都没有 —— 那行警告每次都不成立,狼来了。
    """
    text = _dump(真机形状, tmp_path, label="A1 记任务")
    assert "扫到本机绝对路径" not in text
    assert "\x00" not in text, "回填用的占位符必须删干净,不许漏进人读版"
    # 但「里面可能有别的敏感东西」这句是站得住的,固定留着
    assert "规范原文摘录" in text


def test_真扫到绝对路径才打那行警告(tmp_path: Path) -> None:
    """真有的时候必须打 —— 动态不是为了少说话,是为了说的每一句都成立。"""
    msgs = [
        {"type": "human", "content": "出巡检记录"},
        {
            "type": "ai",
            "name": "report",
            "content": "记录好了,文件在 /Users/somebody/repo/data/artifacts/2026-08-10/abc.docx",
        },
    ]
    text = _dump(msgs, tmp_path, label="C3 出报告")
    assert "扫到本机绝对路径(/Users/)" in text


def test_台账库路径里的用户名也要被扫到(tmp_path: Path) -> None:
    """2026-08-10 打样时当场逮到的漏报:扫描一开始只看 json。

    可库路径和台账每一行**只出现在渲染出来的 .txt 里**,json 里根本没有 —— 而真机上
    ``ledger_db`` 恰恰就是 ``/Users/<用户名>/…/data/gyt.sqlite3``。demo 用 /var/folders
    的临时库没露馅,换成真机形状的路径立刻漏报。所以扫描面必须是「渲染结果 + json」。
    """
    家 = tmp_path / "Users" / "somebody" / "repo" / "data"
    家.mkdir(parents=True)
    db = 家 / "gyt.sqlite3"
    _建库(db, [(1, "复检三层钢筋", "2026-08-11", "open")])
    text = _dump(真机形状, tmp_path / "out", label="D1 库:T1 在", ledger_db=db)
    assert "/Users/somebody" in text  # 路径确实印在文件里了
    assert "扫到本机绝对路径(/Users/)" in text  # 那就必须报出来


# ===========================================================================
# 七、原有那批不变式(落盘、不覆盖、抗畸形、截断……)
# ===========================================================================


def test_一红就落下人读版和原样两份文件(tmp_path: Path) -> None:
    path = tr.dump_trace(label="A8 乱日期报人话带举例", msgs=乙_参数传错, out_dir=tmp_path)
    assert path is not None
    assert path.suffix == ".txt"
    assert path.with_suffix(".json").is_file()


def test_工具的入参和返回都落进人读版(tmp_path: Path) -> None:
    """「谁调的」之后的第二个问题:喂进去的是什么、工具答了什么。

    args 里的东西直接说明模型把什么串给了解析器;ToolMessage 里的 error_code
    说明这是输入问题还是系统故障。两样都不落盘的话,查到一半还得重跑一次。
    (这里只验轨迹里**真有**的那些 —— 子 Agent 那部分没法验,因为它不在。)
    """
    text = _dump(丙_工具报错模型说成功, tmp_path, label="A7 销号")
    assert 'complete_task({"task_id": "T2"})' in text
    assert "NOT_FOUND" in text


def test_原样那份一个字节都没裁(tmp_path: Path) -> None:
    """人读版为了 30 秒必须裁剪,所以旁边必须躺一份没裁过的 —— round-trip 相等即证。"""
    path = tr.dump_trace(label="A8 乱日期", msgs=乙_参数传错, out_dir=tmp_path)
    raw = json.loads(path.with_suffix(".json").read_text("utf-8"))
    assert raw == 乙_参数传错


def test_同一条断言红两次不覆盖前一份(tmp_path: Path) -> None:
    """覆盖等于毁证。第二份加 -2 后缀。"""
    first = tr.dump_trace(label="A8 乱日期", msgs=乙_参数传错, out_dir=tmp_path)
    second = tr.dump_trace(label="A8 乱日期", msgs=甲_没调工具编回执, out_dir=tmp_path)
    assert first != second
    assert first.is_file() and second.is_file()
    assert second.stem.endswith("-2")


def test_一条消息形状不对不许带走整份轨迹(tmp_path: Path) -> None:
    """丁里第 4 条根本不是字典。要的效果是:那一条降级成一行提示,其余照常渲染。"""
    path = tr.dump_trace(label="D4 库:恰好 3 条", msgs=畸形, out_dir=tmp_path)
    assert path is not None
    text = path.read_text("utf-8")
    assert "尾巴这条必须还在" in text  # 后面的消息没被连累
    assert "正常的第一条" in text  # 前面的消息也没被连累
    assert "这条渲染不出来" in text  # 坏掉那条如实说,不是悄悄跳过


@pytest.mark.parametrize(
    "怪东西",
    [
        [{"type": "tool", "name": "x", "content": None}],  # content 是 None
        [{"type": "tool", "name": "x", "content": '{"ok": "false"}'}],  # ok 是字符串不是布尔
        [{"type": "tool", "name": "x", "content": "[1,2,3]"}],  # 合法 json 但不是对象
        [{"type": "ai", "tool_calls": ["我不是字典"]}],  # tool_calls 里塞了个字符串
        [{"type": "ai", "name": None, "content": 12345}],  # content 是数字
    ],
)
def test_畸形输入一律不打崩(tmp_path: Path, 怪东西: list[dict]) -> None:
    """这五种是复核那一轮造出来的。每加一条新的头部渲染,就得再跑一遍这组 ——
    头部是跨消息聚合,最容易被一条怪东西整段带走。"""
    path = tr.dump_trace(label="Z1 畸形", msgs=怪东西, out_dir=tmp_path)
    assert path is not None
    assert path.read_text("utf-8").strip() != ""


def test_空消息列表不抛也不留空文件(tmp_path: Path) -> None:
    path = tr.dump_trace(label="A0 空消息", msgs=[], out_dir=tmp_path)
    assert path is not None
    assert "没有任何消息可看" in path.read_text("utf-8")


def test_没有human消息时不许把AI发言冒充成本轮提问(tmp_path: Path) -> None:
    """真机上 human 消息一定在 state 里,这条防的是**别的**:

    拿桩后端干跑演练时,桩返回的 messages 里没有 human,头部就把第一条 supervisor
    发言当成「本轮提问」打了出来 —— 一句不是提问的话摆在那个标签后面,比留白更误导。
    现在退化成一句说清楚的话,并且「本轮」按全量算(也说清楚了)。
    """
    msgs = [{"type": "ai", "name": "supervisor", "content": "记上了,明天上午去复检。"}]
    text = _dump(msgs, tmp_path, label="A1 记任务")
    assert "没有 human 消息" in text
    assert "本轮提问   记上了" not in text


def test_文件名带断言号且中文原样保留(tmp_path: Path) -> None:
    """按断言号命名是为了和屏幕上那行 FAIL 对得上;中文保留是因为标题就是中文。"""
    path = tr.dump_trace(
        label="A10 本周三已过→报错不猜(今天周六)", msgs=乙_参数传错, out_dir=tmp_path
    )
    assert path is not None
    assert path.stem.startswith("A10_")
    assert "本周三已过" in path.stem


def test_超长正文在人读版里截断但在原样里完整(tmp_path: Path) -> None:
    """knowledge 的 ToolMessage 会带 top_k 段规范原文,不截会把时间线整个冲垮。"""
    长文 = "规范原文" * 2000
    msgs = [
        {"type": "human", "content": "消防车道要求"},
        {"type": "tool", "name": "search_spec", "content": 长文},
    ]
    path = tr.dump_trace(label="E2 规范检索", msgs=msgs, out_dir=tmp_path)
    assert path is not None
    assert "全文见 json" in path.read_text("utf-8")
    assert json.loads(path.with_suffix(".json").read_text("utf-8"))[1]["content"] == 长文


def test_抽取函数与断言共用同一套(tmp_path: Path) -> None:
    """final / sched_text / transfers 从 live_acceptance.py 搬到这里,是为了**不漂移**:

    人读版头部要打「判据实际拿到的文本」,那必须和断言真正吃进去的是同一个字符串。
    两边各写一份的话,轨迹会变成一个新的假信息源 —— 比没有轨迹更害人。
    (顺带:它们以前藏在一个 import 即执行的脚本里,零覆盖,这是第一次被测到。)
    """
    assert tr.final(乙_参数传错) == "T2 那条没改成:「猴年马月」这个期限我没看懂。"
    assert tr.sched_text(乙_参数传错) == "T2 那条没改成:「猴年马月」这个期限我没看懂。"
    assert tr.transfers(乙_参数传错) == ["transfer_to_schedule", "transfer_to_schedule"]
    # 带 tool_calls 的那条 schedule 消息 content 是空的,不许混进 sched_text
    assert tr.sched_text(甲_没调工具编回执) == "改好了,T2 已经改到猴年马月。"


# ===========================================================================
# 八、落不下去要大声说,而且要能被上游数出来
# ===========================================================================


def test_落不下去时返回None并且大声说出来(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """红线是禁**静默**吞错,不是禁兜底。

    这里把 out_dir 指到一个已存在的**文件**上,mkdir 必然 OSError。
    要的效果:返回 None、不抛、stdout 上有一行看得见的中文、战果里记一笔。
    """
    occupied = tmp_path / "我其实是个文件"
    occupied.write_text("挡路", encoding="utf-8")
    assert tr.dump_trace(label="A9 落不下去", msgs=乙_参数传错, out_dir=occupied) is None
    assert "轨迹没存下来" in capsys.readouterr().out
    assert tr.trace_tally() == (1, 0)


def test_原样序列化炸了也照样把人读版写出去(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """上一版在 json 序列化失败时直接 return None,**连人读版都不写**。

    可 ``_render`` 本来就逐条包了 try,扛得住脏数据 —— 白白把唯一一份能落的证据也丢了。
    现在:原样那份没了就明说没了,人读版照写,并记一笔 degraded。
    """
    环: list[Any] = [{"type": "human", "content": "循环引用"}]
    环[0]["self"] = 环  # json.dumps 会 ValueError: Circular reference detected

    path = tr.dump_trace(label="A8 循环引用", msgs=环, out_dir=tmp_path)
    assert path is not None and path.suffix == ".txt"
    text = path.read_text("utf-8")
    assert "循环引用" in text  # 时间线还在
    assert "原样轨迹   ⚠ 没落下来" in text  # 但明说了少一份,不假装完整
    assert "序列化失败" in capsys.readouterr().out
    assert tr.trace_tally() == (0, 1)
    assert not path.with_suffix(".json").exists()


def _只让某个后缀写失败(monkeypatch: pytest.MonkeyPatch, 后缀: str) -> None:
    """模拟「盘满了 / 目录被人 chmod 了」这类只影响一次写入的故障。

    直接 chmod 目录会让两份一起失败,验不出「一份挂了另一份还在不在」——
    而那恰恰是落盘顺序想保住的东西,所以这里按后缀精确打靶。
    """
    真写 = Path.write_text

    def 假写(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.suffix == 后缀:
            raise OSError("盘满了")
        return 真写(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", 假写)


def test_原样那份写不下去时人读版照写并记degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """.json 落不下去不算完 —— 裁过的人读版仍然能定位问题,还是要写出去。

    但必须在文件里**明说**原样那份没了:不然下一个人会照着「原样轨迹 xxx.json」去找一个
    根本不存在的文件,以为是自己弄丢了。
    """
    _只让某个后缀写失败(monkeypatch, ".json")
    path = tr.dump_trace(label="A8 盘满", msgs=乙_参数传错, out_dir=tmp_path)
    assert path is not None and path.suffix == ".txt"
    assert "原样轨迹   ⚠ 没落下来" in path.read_text("utf-8")
    assert "原样轨迹没写成" in capsys.readouterr().out
    assert tr.trace_tally() == (0, 1)


def test_人读版写不下去时至少把原样那份交出去(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """落盘顺序(先 json 后 txt)就是为了这一刻:加工品挂了,原始证据已经在盘上了。

    返回的是 .json 的路径,好让屏幕上那行「轨迹已存」指到真存在的东西。
    """
    _只让某个后缀写失败(monkeypatch, ".txt")
    path = tr.dump_trace(label="A8 人读版挂了", msgs=乙_参数传错, out_dir=tmp_path)
    assert path is not None and path.suffix == ".json"
    assert json.loads(path.read_text("utf-8")) == 乙_参数传错
    out = capsys.readouterr().out
    assert "人读版渲染失败" in out
    assert "原样轨迹已存" in out
    assert tr.trace_tally() == (0, 1)


def test_两份都写不下去时明说现场是空的(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """最坏的一种。这时候唯一有用的动作是重跑,所以屏幕上就得这么说。"""

    def 全都失败(self: Path, *args: Any, **kwargs: Any) -> int:
        raise OSError("盘满了")

    monkeypatch.setattr(Path, "write_text", 全都失败)
    assert tr.dump_trace(label="A8 全挂", msgs=乙_参数传错, out_dir=tmp_path) is None
    assert "只能重跑" in capsys.readouterr().out
    assert tr.trace_tally() == (1, 0)


def test_战果计数让上游知道有几份没落全(tmp_path: Path) -> None:
    """25 条断言刷屏时,「有两份证据没落下」这件事很容易被滚上去看不见。

    ``trace_tally()`` 让调用方在跑批总结里补一行。分 failed / degraded 是因为处置不同:
    前者现场是空的必须重跑,后者证据不全但多半将就能用。
    """
    assert tr.trace_tally() == (0, 0)
    tr.dump_trace(label="A1 好的", msgs=乙_参数传错, out_dir=tmp_path)
    assert tr.trace_tally().failed == 0

    occupied = tmp_path / "挡路的文件"
    occupied.write_text("x", encoding="utf-8")
    tr.dump_trace(label="A2 坏的", msgs=乙_参数传错, out_dir=occupied)
    tr.dump_trace(label="A3 也坏", msgs=乙_参数传错, out_dir=occupied)
    tally = tr.trace_tally()
    assert (tally.failed, tally.degraded) == (2, 0)
    assert tally.failed == 2  # NamedTuple:字段名读得懂,位置也对得上


def test_原样没落下时不许覆盖上一份的人读版(tmp_path: Path) -> None:
    """``_unique_stem`` 原来只看 .json 存不存在。序列化失败那条路只落 .txt,
    下一份同名断言就会把它悄悄盖掉 —— 覆盖等于毁证,两个后缀都得看。"""
    环: list[Any] = [{"type": "human", "content": "第一份"}]
    环[0]["self"] = 环
    first = tr.dump_trace(label="A8 同名", msgs=环, out_dir=tmp_path)
    second = tr.dump_trace(label="A8 同名", msgs=乙_参数传错, out_dir=tmp_path)
    assert first is not None and second is not None
    assert first != second
    assert "第一份" in first.read_text("utf-8")  # 没被盖掉
