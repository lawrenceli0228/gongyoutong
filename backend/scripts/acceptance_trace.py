"""真机验收的**现场取证层**:断言一红,就把那条线程的完整消息轨迹落盘。

===========================================================================
它为什么存在
---------------------------------------------------------------------------
2026-08-09 那轮真机验收 A8 红了,屏幕上只有一行「FAIL A8 …」——
到底是模型没调工具凭记忆编了句回执,还是工具真报了错但话说得不对,
**现场什么都没留下**,只能重跑一遍碰运气。这个模块就是为了让那种事不再发生。

===========================================================================
⚠️ 先读这段:这份轨迹**看不见子 Agent 的工具调用**
---------------------------------------------------------------------------
``graph.py`` 里 ``OUTPUT_MODE = "last_message"``。本机
``backend/.venv/…/langgraph_supervisor/supervisor.py`` 的 ``_process_output``
(2026-08-10 逐行读过安装包原文):

    elif output_mode == "last_message":
        if isinstance(messages[-1], ToolMessage):
            messages = messages[-2:]
        else:
            messages = messages[-1:]

子 Agent 一个回合的最后一条**恒是它自己的纯文本 AIMessage**(工具跑完还得再让模型
说句人话),所以走的永远是 ``messages[-1:]`` 那条分支 —— **带 tool_calls 的那条 AI
消息连同它的 ToolMessage 一起被砍掉**。于是 ``/threads/{tid}/runs/wait`` 返回的顶层
messages 里,子 Agent 的业务工具调用**根本不存在**,跟模型到底调没调工具无关。

supervisor 自己发起的 ``transfer_to_*`` 交接调用**留在**顶层(那是 supervisor 本人的
消息,不经过 ``_process_output``),所以「本轮交接」那一行是真的,可以信。

**这是本模块交过学费的地方。** 第一版头部有一行
``★ 本轮业务工具  (一个都没调)``,底下还配着一句解读「= 模型凭记忆编的,先查提示词和
guard」。那一行在真机上**恒为空**,于是它是一条**恒真的假线索**,能把查问题的人
往错误方向带一整天。2026-08-10 用同版本库(langgraph 1.2.10 / langgraph-supervisor
0.0.31)起了一张真的 create_supervisor 图,参数与 graph.py 逐字相同,让子 Agent
**确实调了** add_task —— 顶层 messages 里那条 tool_call 消息依然不见。整行已删。
**一条恒真的假线索,比没有线索贵得多。**

那「到底办了没」怎么问?走这两条:
    · 本文件里的**台账快照** —— D 组的原话:「回执说什么不算,库里有没有才算」;
    · 或者直接查库 / 翻后端日志。
把子 Agent 的消息真正捞回来另有两条路(runs/stream 带 ``subgraphs: true``、
跑完再 POST ``/threads/{tid}/history`` 带 subgraphs),但只在 Pregel 层验过、
HTTP 层没实测,**这一轮不赌**,已记进欠账。

===========================================================================
两份文件,同一个 basename —— 为什么不能只留一份
---------------------------------------------------------------------------
    A8_乱日期报人话带举例.json   原样 msgs,一个字节不删(jq / grep / 复现用)
    A8_乱日期报人话带举例.txt    人读版,为了 30 秒**必须**裁剪

人读版要截断长 content、丢掉 additional_kwargs / response_metadata / id,
不然一段 knowledge 检索原文就能把时间线冲垮。但这个模块的由来恰恰是
「现场没留下东西」—— 再来一次「留下的是裁过头的」同样查不动。
所以裁剪版旁边必须躺一份原样的。

**落盘顺序:先 .json 后 .txt。** 原样那份最保真、最不可能失败,先上盘;
人读版是加工品,即便渲染炸了,证据也已经在磁盘上了。反过来也成立:.json 序列化炸了
(循环引用之类)也照样把 .txt 写出去 —— 裁过的证据仍然是证据,总好过一份都没有。

===========================================================================
硬边界(写清楚,免得下一个人当 bug 查)
---------------------------------------------------------------------------
· 子 Agent 的工具调用与工具返回不在轨迹里(理由见上)。
· ``core/require_tool.py`` 的「打回重试」提示只进 ``request.messages``(送模型的那一份),
  **不进图 state** —— 所以轨迹里看不到「重试发生过」,只看得到重试后的结果。
  要查中间件本身的行为得看后端日志。这不是缺陷,是本模块的范围边界。
· 本模块只依赖 stdlib,**不 import gyt 任何东西**。这样离线用一个裸 3.11 解释器
  就能验它(见 tests/unit/test_acceptance_trace.py),不需要 venv、不需要容器。
  台账快照因此**不自己拼库路径**,由调用方把 ``get_settings().sqlite_path`` 传进来。
· ``dump_trace`` **永不向上抛**:取证是排查辅助,不能反过来把它要辅助的那套验收弄崩。
  但也**绝不静默** —— 落不下去时在 stdout 打一行看得见的中文,并记进 ``trace_tally()``。

===========================================================================
安全:轨迹里会带出什么(2026-08-10 逐项复核过,不是泛泛而谈)
---------------------------------------------------------------------------
· API Key 的**值**不会进消息(密钥只在 Settings → SDK client 这条路上走)。
  但 Key 的**变量名**会:safety/tools.py 把「请在 .env 里加上 GYT_MOONSHOT_API_KEY」
  这句人话原样当 user_msg 返回。只有名字没有值,不脱敏。
· **本机绝对路径不一定进** —— 所以那行提示改成了动态的(``_local_path_hint``)。
  report/tools.py 确实把 ``"path": str(stored)`` 放进 Envelope.data(tools.py:176),
  但那份 Envelope 只走到 report 的 ToolMessage 为止,而 ToolMessage 正是上面那把刀
  砍掉的东西;``agents/report/prompt.md`` 里 ``path`` / ``路径`` **零命中**
  (2026-08-10 grep 过),模型没被要求把路径转述出来。真机形状的轨迹里因此通常
  **一个绝对路径都没有**。上一版把「⚠ 本文件含本机绝对路径」写死印在**每一份**轨迹上,
  每次都不成立 —— 狼来了喊多了,真有敏感内容那一次也没人当回事。现在真扫到才打。
· 稳定会进的是这些:照片 / 产物编号、规范原文摘录、后端报错原话(user_msg)。
  头部固定一行提醒,把「发出去前先扫一眼」这件事交给人。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Final, NamedTuple

# --- 判据抽取(与 live_acceptance.py 的断言共用同一套) -------------------------
#
# 这三个函数从 live_acceptance.py 搬过来,不是为了整理代码,是为了**不漂移**:
# 人读版头部要打「判据实际拿到的文本」,那必须和断言真正吃进去的是同一个字符串。
# 两边各写一份的话,轨迹会变成一个新的假信息源 —— 比没有轨迹更害人。
# 顺带它们第一次变得可单测(以前藏在一个 import 即执行的脚本里,零覆盖)。


def final(msgs: list[dict]) -> str:
    """最后一条消息的正文 —— 一般就是 supervisor 的收尾发言。"""
    return str(msgs[-1]["content"])


def sched_text(msgs: list[dict]) -> str:
    """schedule 子 Agent 这一轮的纯文本发言(最后一条)。

    筛 ``type=="ai" and name=="schedule" and 没有 tool_calls``:带 tool_calls 的那条
    content 是空的,拼进来只会稀释判据。
    """
    parts = [
        str(m["content"])
        for m in msgs
        if m.get("name") == "schedule" and m.get("type") == "ai" and not m.get("tool_calls")
    ]
    return parts[-1] if parts else ""


def transfers(msgs: list[dict]) -> list[str]:
    """整条线程里所有 ``transfer_to_*`` 交接调用的名字,按发生顺序。"""
    return [
        c["name"]
        for m in msgs
        for c in (m.get("tool_calls") or [])
        if str(c["name"]).startswith("transfer_to_")
    ]


# --- 落盘战果(给调用方在跑批总结里打一行) --------------------------------------


class TraceTally(NamedTuple):
    """本进程到目前为止有几份轨迹没落全。

    ``failed``   两份都没落下(``dump_trace`` 返回了 None),现场是真的空了;
    ``degraded`` 只落下一份(原样或人读版少一个),还能查,但证据不全。
    分两个数是因为处置不同:前者必须重跑,后者多半将就能用。
    """

    failed: int = 0
    degraded: int = 0


_TALLY: TraceTally = TraceTally()


def trace_tally() -> TraceTally:
    """读当前战果。跑批收尾时打一行 —— 别让「证据没落下」这件事自己悄悄溜过去。"""
    return _TALLY


def reset_trace_tally() -> None:
    """清零。给测试做隔离用;真机一个进程只跑一轮验收,用不上。"""
    global _TALLY
    _TALLY = TraceTally()


def _bump(*, failed: int = 0, degraded: int = 0) -> None:
    """整体换一个新的 TraceTally,不就地改 —— 计数器也不例外。"""
    global _TALLY
    _TALLY = TraceTally(_TALLY.failed + failed, _TALLY.degraded + degraded)


# --- 落盘 ---------------------------------------------------------------------

_TRANSFER_PREFIXES: Final[tuple[str, ...]] = ("transfer_to_", "transfer_back_to_")
"""交接工具 —— 由 langgraph_supervisor 自动生成。它们是顶层轨迹里**唯一**留得下来的
工具调用,所以单独数一行;数它只能说明「派了活」,说明不了「干了活」。"""

_MAX_CONTENT_CHARS: Final[int] = 2000
"""单条 content 在人读版里的上限。定 2000 是因为 knowledge 的 ToolMessage 会带
knowledge_top_k(默认 5)段规范原文,几千字很常见 —— 不截会把时间线整个冲垮。
超出部分不是丢了,是在 .json 里。"""

_MAX_ARGS_CHARS: Final[int] = 1000
"""tool_calls.args 的上限。args 通常很短,这道闸防的是有人往工具里塞长文本。"""

_MAX_USER_MSG_CHARS: Final[int] = 200
"""工具报错那行里 user_msg 的上限。报错文案是给工地师傅看的,本来就不长。"""

_MAX_LEDGER_ROWS: Final[int] = 50
"""台账快照最多列多少行。验收跑批时库里就三五行;这道闸防的是有人拿一份跑脏了的
陈年库来跑 —— 几百行任务铺下去,头部就不叫「30 秒判据」了。"""

_NAME_KEEP_CHARS: Final[int] = 24
"""文件名里保留的标题长度。够认人就行,长了 ls 一屏放不下。"""

_ASSERT_ID_RE: Final[re.Pattern[str]] = re.compile(r"([A-Z]\d{1,2})")
"""从 label 头部抠断言号(A1 / A10 / D4 …)。25 条 label 全是这个形状。"""

_FILENAME_BAD_RE: Final[re.Pattern[str]] = re.compile(r"[^\w-]+")
"""文件名里不留的字符。Python 3 的 ``\\w`` 对 str 已经含中日韩,所以中文标题原样保留
—— 中文文件名在本仓是既有惯例(docs/W5_巡检链防假账与验收去脆化方案.md)。"""

_LOCAL_PATH_MARKERS: Final[tuple[str, ...]] = ("/Users/", "/home/", "/app/")
"""扫本机绝对路径用的标志。前两个带操作系统用户名,``/app/`` 是容器里的数据根
(``GYT_DATA_DIR=/app/data``)—— 不含用户名,但一起报出来能提醒人这份是容器跑的。"""

_LEDGER_SQL: Final[str] = "SELECT id, title, due_date, status FROM tasks ORDER BY id LIMIT ?"
"""台账快照的查询。带 LIMIT 是纪律(不许无界查询),参数化是纪律(不许拼串)。"""

_LIMIT_NOTE: Final[tuple[str, ...]] = (
    "⚠ 这份轨迹**看不见子 Agent 的工具调用和工具返回**。",
    '  supervisor 的 output_mode="last_message" 只留子 Agent 每回合的最后一条纯文本发言,',
    "  带 tool_calls 的那条 AI 消息连同它的 ToolMessage,在回灌 supervisor 时就被砍掉了。",
    "  所以「时间线里没有业务工具」**不等于**「模型没调工具」—— 别照这个去查提示词。",
    "  想知道活到底办没办:查台账库(台账类断言下面直接附了快照),或者翻后端日志。",
)
"""头部固定的局限声明。整段是常量而不是现拼,是因为它必须每份都一模一样 ——
这句话本身就是给「上一版那条假线索」立的碑,不许被某次改动悄悄改软。"""


def _as_text(content: Any) -> str:
    """content 不一定是 str:多模态是 ``list[dict]``。统一成可打印文本。"""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(还有 {len(text) - limit} 字,全文见 json)"


def _safe_name(label: str) -> str:
    """label → 文件名主干。抠不到断言号就整条清洗,不额外编号(编号会和 RESULTS 漂移)。"""
    matched = _ASSERT_ID_RE.match(label)
    prefix = matched.group(1) if matched else ""
    rest = label[len(prefix) :]
    cleaned = _FILENAME_BAD_RE.sub("_", rest).strip("_")[:_NAME_KEEP_CHARS]
    stem = f"{prefix}_{cleaned}" if prefix and cleaned else (prefix or cleaned)
    return stem or "未命名断言"


def _taken(out_dir: Path, stem: str) -> bool:
    """这个主干被占了没。**两个后缀都要看**:.json 落不下去而 .txt 落下来的情况是存在的
    (序列化炸了那条路),只看 .json 会让下一份把上一份的 .txt 覆盖掉 —— 覆盖等于毁证。"""
    return (out_dir / f"{stem}.json").exists() or (out_dir / f"{stem}.txt").exists()


def _unique_stem(out_dir: Path, stem: str) -> str:
    """同一个断言号被 check 两次时后缀 -2、-3,**不覆盖** —— 覆盖等于毁证。"""
    if not _taken(out_dir, stem):
        return stem
    for n in range(2, 100):
        if not _taken(out_dir, f"{stem}-{n}"):
            return f"{stem}-{n}"
    return f"{stem}-{int(time.time())}"  # pragma: no cover — 同一断言炸 100 次不现实


def _round_start(msgs: list[dict]) -> int | None:
    """本轮从第几条开始 = 最后一条 human 消息的下标(每轮只发一条 human)。找不到返回 None。

    为什么要分「本轮 / 累计」:msgs 是整条线程的累计历史,而红的几乎总是最后一轮。
    只给累计数会被前面九轮的正常调用糊弄过去。

    为什么找不到时返回 None 而不是 0:返回 0 会让头部把第一条 AI 发言当成「本轮提问」
    打出来 —— 一句不是提问的话摆在「本轮提问」后面,比留白更误导人。
    (真机上不会发生:human 消息会被 add_messages 追进 state。这条是拿桩后端跑
     干跑演练时暴露出来的,顺手堵上。)
    """
    for idx in range(len(msgs) - 1, -1, -1):
        # isinstance 这道闸不是防真机的(那边是 json.load 出来的,恒为 dict),
        # 是防"喂进来一条怪东西就整份轨迹渲染不出来" —— 取证件不许比被取证的还脆。
        if isinstance(msgs[idx], dict) and msgs[idx].get("type") == "human":
            return idx
    return None


def _handoff_counts(msgs: list[dict]) -> Counter[str]:
    """交接工具的调用计数。

    **只数交接,不数业务工具** —— 顶层轨迹里根本没有子 Agent 的业务工具调用(见模块开头),
    再摆一个恒为 0 的「业务工具」计数就是又一条假线索。业务工具真出现在顶层时,
    时间线那段照样会原样打出来,不会漏。
    """
    counts: Counter[str] = Counter()
    for m in msgs:
        if not isinstance(m, dict):
            continue  # 同 _round_start 的理由
        for call in m.get("tool_calls") or []:
            name = str(call.get("name", "?")) if isinstance(call, dict) else "?"
            if name.startswith(_TRANSFER_PREFIXES):
                counts[name] += 1
    return counts


def _fmt_counter(counter: Counter[str], empty_hint: str) -> str:
    if not counter:
        return empty_hint
    return "  ".join(f"{name}×{n}" for name, n in sorted(counter.items()))


def visible_tool_errors(msgs: list[dict]) -> list[str]:
    """轨迹里**看得见的**那些 ToolMessage 中 ``ok is False`` 的,渲染成一行一条。

    它专治最难抓的那种病:工具真调了、真返回 ``ok:false``,而模型把失败转述成了成功。
    这种病 ``core/require_tool.py`` 的 RequireToolCall 完全拦不住(工具确实调了),
    光读模型那句「改好了」也看不出来 —— 只有把 Envelope 摊开才现形。

    **「可见的」三个字是较真的,不是谦虚。** 子 Agent 的 ToolMessage 压根不在轨迹里
    (见模块开头),这个函数只覆盖轨迹里真有的那些。叫「本轮工具报错」会让人以为
    「空 = 没报错」,那又是一条恒真的假线索,本模块已经栽过一次了。

    解析不了就跳过、不抛:四键 Envelope(ok/data/user_msg/error_code)是本仓硬契约,
    但交接工具的 ToolMessage 是一句英文纯文本(``Successfully transferred to …``),
    ``json.loads`` 必然失败 —— 那是正常现象,不是异常。
    """
    out: list[str] = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("type") != "tool":
            continue
        raw = m.get("content")
        if not isinstance(raw, str):
            continue
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict) or body.get("ok") is not False:
            continue
        name = str(m.get("name") or "?")
        code = str(body.get("error_code") or "(没给 error_code)")
        said = _clip(_as_text(body.get("user_msg") or ""), _MAX_USER_MSG_CHARS)
        out.append(f"{name} → {code}:{said}")
    return out


def _tool_error_lines(this_round: list[dict]) -> list[str]:
    """把 ``visible_tool_errors`` 排成头部那几行,顺带把「可见」这个限定说清楚。"""
    errors = visible_tool_errors(this_round)
    if errors:
        lines = [f"  本轮可见的工具报错  {len(errors)} 条:"]
        lines.extend(f"      · {item}" for item in errors)
    else:
        lines = ["  本轮可见的工具报错  (没有)"]
    lines.append("      ↑ 只覆盖轨迹里真有的那些工具返回;子 Agent 的不在这儿,空≠没报错。")
    return lines


def ledger_snapshot(db_path: Path | None) -> list[str]:
    """台账快照:``SELECT id,title,due_date,status FROM tasks``,渲染成人读的几行。

    为什么取证件要顺手读一次库:模块开头那段说了,子 Agent 的工具调用不在轨迹里,
    「活到底办没办」在消息里根本问不出来。而这个仓库自己早就有答案 —— D 组的原话:
    **回执说什么不算,库里有没有才算**。库还有一个消息比不了的好处:**离线可验**。
    .txt 落盘三天之后再打开,快照仍然是当时那一刻的样子,不用把后端重新起起来。

    ``db_path=None`` = 调用方没给(E/F 组那些非台账断言本来就没有库可看)→ 返回空列表,
    头部整段不出现。**不自己拼路径**:后端往哪儿写、验收就得读哪儿,路径的唯一真相在
    ``config.get_settings().sqlite_path``,由调用方传进来(本模块不 import gyt)。

    连接走**只读 URI**(``as_uri() + "?mode=ro"``),理由抄 live_acceptance.read_ledger:
    普通 ``sqlite3.connect(str(path))`` 碰上不存在的路径会当场建一个 0 字节空库,
    下一句 SELECT 再报 ``no such table: tasks`` —— 既在磁盘上留个垃圾文件,又把
    「库找错地方了」说成「表没建」,看的人第一反应去查建表逻辑,方向全错。
    只读模式下同一个路径直接报「打不开」,而且文件不会被创建。

    **任何读不出来的情况都只写一行中文说明,不抛栈** —— 取证件不许比被取证的还脆:
    一次台账断言红了、库又恰好不在,不能连带把整份轨迹的头部一起搞没。
    """
    if db_path is None:
        return []
    lines = [
        "台账快照(库里现在到底有什么 —— 回执说什么不算,库里有没有才算):",
        f"  库文件 {db_path}",
    ]
    try:
        if not db_path.is_file():
            lines.append("  ⚠ 这个文件不在。它由后端第一次记任务时自己建;")
            lines.append("    现在没有,通常是后端和验收脚本读的不是同一份配置,或者一条都没记成。")
            return lines
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(_LEDGER_SQL, (_MAX_LEDGER_ROWS + 1,)).fetchall()
    except (sqlite3.Error, OSError, ValueError) as exc:
        # ValueError 是 as_uri() 碰上相对路径抛的;OSError 是 resolve()/is_file() 的份。
        # 底层原话照抄(``no such table`` / ``file is not a database`` 各指一件事),
        # 别自己转述成一个成因 —— 那正是 read_ledger 当年踩的「把 A 说成 B」。
        lines.append(f"  ⚠ 读不出来:{exc}")
        lines.append("    常见原因:文件在但还没有 tasks 表(后端一条任务都没记成),或者文件坏了。")
        return lines

    if not rows:
        lines.append("  (表在,但一行任务都没有。)")
        return lines
    lines.append("  id | title | due_date | status      ← id 就是回执里 T 号后面那个数")
    lines.extend(
        "  " + " | ".join("(空)" if v is None else str(v) for v in row)
        for row in rows[:_MAX_LEDGER_ROWS]
    )
    if len(rows) > _MAX_LEDGER_ROWS:
        lines.append(f"  …(只列了前 {_MAX_LEDGER_ROWS} 行,库里还有更多 —— 这库多半没清干净)")
    return lines


_HINT_SLOT: Final[str] = "\x00本机路径提示占位\x00"
"""头部里给「扫到绝对路径」那行留的占位。

为什么要占位而不是当场算:能扫的东西要等**整份 .txt 渲染完**才齐 —— 消息正文来自
msgs,可库路径和台账每一行来自库,它们只出现在渲染结果里,不在 json 里。
只扫 json 会漏掉真机上最常见的那一种:``/Users/<用户名>/…/data/gyt.sqlite3``。
(这条是 2026-08-10 打样时当场发现的:demo 用的是 /var/folders 的临时库没露馅,
换成真机路径就漏报了。)所以先占位、后回填。"""


def _local_path_hint(scan_text: str) -> list[str]:
    """真扫到本机绝对路径才打这一行。

    上一版是把「⚠ 本文件含本机绝对路径」写死印在每一份轨迹上。2026-08-10 复核发现
    那句每次都不成立(理由见模块开头的安全段),于是它变成了狼来了 —— 真有敏感内容
    那一次也没人会当回事。动态扫比写死的措辞诚实,也比脱敏诚实(脱敏 = 又一次裁剪)。
    """
    hits = [mark for mark in _LOCAL_PATH_MARKERS if mark in scan_text]
    if not hits:
        return []
    return [f"⚠ 扫到本机绝对路径({' '.join(hits)}),里面多半带操作系统用户名 —— 发出去前先看一眼。"]


def _fill_hint_slot(text: str, raw_json: str | None, msgs: list[dict]) -> str:
    """回填占位:扫**这两份文件的全部内容**,有命中就把那行填进去,没有就把占位行删干净。

    扫描面 = 渲染好的 .txt(含库路径、台账每一行)+ 原样 json(含被裁掉的长正文)。
    json 序列化失败时退回 ``repr`` —— 那条路上更该扫一眼,不能因为序列化炸了就漏报。
    """
    extra = raw_json if raw_json is not None else _safe_repr(msgs)
    hint = _local_path_hint(text.replace(_HINT_SLOT, "") + extra)
    return text.replace(f"{_HINT_SLOT}\n", ("\n".join(hint) + "\n") if hint else "")


def _safe_repr(msgs: list[dict]) -> str:
    """json 序列化那条路走不通时的兜底扫描文本。``repr`` 几乎不可能失败,失败也不许往上抛。"""
    try:
        return repr(msgs)
    except Exception:  # pragma: no cover — 只有自定义 __repr__ 抛异常才走得到
        return ""


def _render_message(index: int, msg: dict) -> list[str]:
    """渲染一条消息。**调用方逐条包 try** —— 一条烂不许带走整份轨迹。"""
    kind = str(msg.get("type", "?"))
    name = msg.get("name")
    head = f"[{index:02d}] {kind}" + (f" / {name}" if name else "")

    calls = msg.get("tool_calls") or []
    if calls:
        # 工具名 **和完整 args** 都要打:args 里的 {"due": "猴年马月"} 直接告诉你
        # 模型到底把什么喂给了解析器 —— 这是「工具调了没」之后的第二个问题。
        parts = []
        for call in calls:
            args = _clip(json.dumps(call.get("args", {}), ensure_ascii=False), _MAX_ARGS_CHARS)
            parts.append(f"{call.get('name', '?')}({args})")
        head += "  → " + "  ".join(parts)

    lines = [head]
    body = _clip(_as_text(msg.get("content", "")), _MAX_CONTENT_CHARS)
    lines.extend(f"     {line}" for line in body.splitlines() if line.strip())
    return lines


_BAR: Final[str] = "=" * 80


def _render_head(
    label: str,
    msgs: list[dict],
    json_name: str | None,
    start: int | None,
    ledger_db: Path | None,
) -> list[str]:
    """头部摘要 —— 30 秒判据全在这儿。调用方包 try,算不出来就降级(见 _render)。"""
    this_round = msgs if start is None else msgs[start:]
    asked = (
        "(这条线程里没有 human 消息 —— 「本轮」退化成了全量,下面几行照全量算)"
        if start is None
        else _clip(_as_text(msgs[start].get("content", "")), 200)
    )
    ledger = ledger_snapshot(ledger_db)
    raw_line = (
        f"原样轨迹   {json_name}(未裁剪,jq / grep 用这份)"
        if json_name
        else "原样轨迹   ⚠ 没落下来 —— 手上只剩这份人读版,而它是裁过的"
    )

    return [
        _BAR,
        f"FAIL  {label}",
        _BAR,
        f"落盘时间   {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"消息条数   {len(msgs)}(整条线程的累计历史,不只这一轮)",
        f"本轮提问   {asked}",
        "",
        *_LIMIT_NOTE,
        "",
        "  本轮交接            " + _fmt_counter(_handoff_counts(this_round), "(没派活)"),
        "      ↑ 这一行是真的,但两类交接的来路不一样,别拿它去推别的:",
        "        transfer_to_* 是 supervisor 本人的消息,压根不进那把刀;",
        "        transfer_back_to_* 挂的是子 Agent 的名字,却不是子 Agent 模型产的 ——",
        "        它由 _process_output 在裁剪**之后**自己拼上(handoff.py:128)。",
        "        所以「name 是子 Agent」不等于「这条被裁过」,反之亦然。",
        *_tool_error_lines(this_round),
        "",
        *([*ledger, ""] if ledger else []),
        "判据实际拿到的文本(与断言吃进去的是同一套抽取函数):",
        f"  final()      | {_clip(final(msgs), 400)}",
        f"  sched_text() | {_clip(sched_text(msgs), 400) or '(空)'}",
        f"  transfers()  | {transfers(msgs) or '(空)'}",
        "",
        raw_line,
        "提醒       轨迹里可能带照片 / 产物编号、规范原文摘录、后端报错原话"
        "(user_msg 里会出现 .env 变量名,只有名字没有值)。",
    ]


def _render(
    label: str,
    msgs: list[dict],
    json_name: str | None,
    raw_json: str | None,
    ledger_db: Path | None,
) -> str:
    if not msgs:
        # 真机路径上不会发生(ask() 拿不到 messages 会先 raise),但合成数据会喂空列表,
        # 而「渲染空列表时崩掉」正好是最没用的一种崩法 —— 早点返回,别让下面去索引 msgs[0]。
        return f"{_BAR}\nFAIL  {label}\n{_BAR}\n这条断言没有任何消息可看(msgs 是空的)。\n"

    start = _round_start(msgs)
    try:
        out = _render_head(label, msgs, json_name, start, ledger_db)
    except Exception as exc:
        # 头部摘要要跨消息聚合(抽判据文本、读库),一条形状不对就可能连累整段。
        # 降级而不是放弃:时间线照常渲染 —— 有时间线的轨迹仍然能查,没有的查不了。
        out = [
            _BAR,
            f"FAIL  {label}",
            _BAR,
            f"⚠ 头部摘要算不出来:{type(exc).__name__}:{exc}",
            f"  多半是消息形状不对。时间线在下面,原样数据见 {json_name or '(没落下来)'}。",
        ]

    # 占位挂在头部末尾;整份渲染完再回填 —— 库路径和台账每一行只出现在渲染结果里,
    # 现在就扫会漏掉真机上最常见的那种 /Users/<用户名>/…/data/gyt.sqlite3。
    out.append(_HINT_SLOT)

    out.extend(["", "--- 时间线(→ 发起工具调用)" + "-" * 46])
    for idx, msg in enumerate(msgs, start=1):
        if start is not None and idx - 1 == start and start > 0:
            out.append("─" * 20 + " 本轮从这里开始 " + "─" * 20)
        try:
            out.extend(_render_message(idx, msg))
        except Exception as exc:
            # 逐条包 try:轨迹的价值在完整性,不能因为第 37 条怪就把前 36 条一起丢掉。
            out.append(f"[{idx:02d}] ⚠ 这条渲染不出来:{type(exc).__name__},原文见 json 第 {idx} 条")
    return _fill_hint_slot("\n".join(out) + "\n", raw_json, msgs)


def _serialize(msgs: list[dict]) -> str | None:
    """原样 msgs → json 文本;序列化不了返回 None 并打一行(不抛)。

    default=str 是给合成数据兜底的:msgs 来自 json.load(resp),按 json 模块语义
    只可能是 dict/list/str/int/float/bool/None,真机路径上不会触发。
    """
    try:
        return json.dumps(msgs, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:  # 循环引用之类,default=str 也救不了
        print(f"      [原样轨迹序列化失败] {type(exc).__name__}:{exc}")
        print("      人读版照写 —— 裁过的证据也是证据,总好过一份都没有。")
        return None


def dump_trace(
    *,
    label: str,
    msgs: list[dict],
    out_dir: Path,
    ledger_db: Path | None = None,
) -> Path | None:
    """把一条失败断言的完整消息轨迹落盘,返回人读版路径;两份都落不下去返回 None。

    ``ledger_db``:台账库路径,给了就在人读版里附一份 ``tasks`` 表快照。
    台账类断言(A / D 组)务必传 ``get_settings().sqlite_path`` —— 子 Agent 的工具调用
    不在轨迹里,这份快照是「活到底办没办」在这个文件里唯一问得出答案的地方。
    非台账断言(E / F 组)不传即可,那一段整段不出现。

    永不抛异常(理由见模块 docstring),但每一种落不下去都会在 stdout 打一行中文,
    缩进对齐在 FAIL 行下方,并累加进 ``trace_tally()``。
    **不设「出错就关掉 dump」的标志位** —— 那又是一份隐式状态;25 条断言最多刷 25 行,
    而「每条都告诉你没存下」比「只说一次」更不容易被漏看。
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"      [轨迹没存下来] {out_dir}:{exc}")
        _bump(failed=1)
        return None

    stem = _unique_stem(out_dir, _safe_name(label))
    json_path = out_dir / f"{stem}.json"
    txt_path = out_dir / f"{stem}.txt"

    # 顺序是刻意的:原样那份最保真、最不可能失败,先上盘。
    payload = _serialize(msgs)
    json_ok = False
    if payload is not None:
        try:
            json_path.write_text(payload, encoding="utf-8")
            json_ok = True
        except OSError as exc:
            print(f"      [原样轨迹没写成] {json_path}:{exc}")

    try:
        text = _render(label, msgs, json_path.name if json_ok else None, payload, ledger_db)
        txt_path.write_text(text, encoding="utf-8")
    except Exception as exc:
        print(f"      [人读版渲染失败] {type(exc).__name__}:{exc}")
        if json_ok:
            print(f"      原样轨迹已存:{json_path}")
            _bump(degraded=1)
            return json_path
        print("      这条断言现场是空的 —— 两份都没落下,只能重跑。")
        _bump(failed=1)
        return None

    if not json_ok:
        _bump(degraded=1)  # 人读版有了,但它是裁过的;原样那份没了,查不了细节
    return txt_path


__all__ = [
    "TraceTally",
    "dump_trace",
    "final",
    "ledger_snapshot",
    "reset_trace_tally",
    "sched_text",
    "trace_tally",
    "transfers",
    "visible_tool_errors",
]
