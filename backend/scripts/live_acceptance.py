"""工友通 · 真机验收脚本(25 断言):台账全流程 / 路由边界 / 英雄链协同 / 规范检索 / 图纸 / 库级铁证。

分组:
    A 台账全流程(10)· B 路由边界(2)· C 英雄链+协同(3)· D 台账库级铁证(4)
    E 规范检索 knowledge(3,含 1 条库级)· F 图纸 cad(3,含 1 条库级)

跑法(会真调模型,几分钱;视觉走缓存):
    0. **E 组要求知识库已建好**(向量库是生成物、不进 git,每台机建一次):
           docker compose run --rm --no-deps backend python -m gyt.agents.knowledge.ingest
       没建就跑,E 组会红 —— 那是对的,别当成 knowledge Agent 坏了。
    1. 确保 langgraph dev 在 :2024(make dev / make dev-docker),仓库根 .env 配好两把 Key
    2. 想从空台账开始(在仓库根执行):rm -f data/gyt.sqlite3
       —— ``data/`` 是 ``Settings.data_dir`` 的默认值(<仓库根>/data);设了 GYT_DATA_DIR
       就删那底下那份。``backend/data/`` 里可能还留着一份旧库,那是历史遗留,
       后端**已经不写那儿**了,删它没用(反而会让人以为清干净了)。
       脚本跑到 D 组时会把自己实际读的库路径原样打出来,一切以那一行为准。
    3. cd backend && uv run --env-file ../.env python scripts/live_acceptance.py

D 组是库级断言 —— 回执说什么不算数,库里有没有才算数(防假账回归,
2026-08-08 真机抓获过:多轮后模型不调工具凭记忆编回执,guard.py 因此而生)。

===========================================================================
这个脚本里**没有**日期字面量 —— 全部现算(W5 · T4)
---------------------------------------------------------------------------
2026-08-09 之前 A1/A3/A4/C3/D1/D2/D3 这七条写死着「8月9日(周日)」「2026-08-14」
这类串,基准日是 2026-08-08(周六)。**过了那一天,这七条天天必红**,
而红的是脚本不是系统 —— 这种红比不测还坏:它会训练人「这几条本来就红,跳过」,
下次真出问题也没人看。

现在的口径:A1 那一轮落库之后,从台账库读 ``tasks.created_at`` 问出**后端进程**的今天
(见 ``read_backend_today``),再交给 ``scripts/acceptance_dates.expected()``
派生出全部期望值。日期算法与 Schedule Agent 用的是同一个 ``dates.py``。

===========================================================================
断言一红就落盘现场(W5 · T3)
---------------------------------------------------------------------------
带 ``msgs`` 的那 19 条断言,红了会把整条线程的消息轨迹写进
``<仓库根>/data/acceptance_dumps/<跑批时刻>/``(两份:人读版 .txt + 原样 .json)。
2026-08-09 那轮 A8 红了却什么都没留下,只能重跑碰运气 —— 这个坑不踩第二次。
D1~D4 / E3 / F3 六条不带 msgs:它们是库级断言,证据是磁盘上的库(路径已原样打过),
硬塞一份消息轨迹只会把「这条本来就该查库」这个信息盖掉。

**轨迹里看不见子 Agent 的工具调用**(``OUTPUT_MODE="last_message"`` 砍的,推演见
``read_backend_today`` 的 docstring)。所以「活到底办没办」不在消息里问,在库里问:
台账类断言(A 组 + C3)落盘时会把 ``ledger_db`` 一并传给 ``dump_trace``,
人读版头部因此带一份 ``tasks`` 表快照 —— 那才是这份文件里的 30 秒判据。
E/F 组不传(它们没有台账可看),那一段整段不出现。

**已知欠账,别当 bug 查**(记在 TODOS.md,都在 acceptance_trace.py 那一侧):
  · ``sched_text()`` 取的是整条线程最后一条 schedule 纯文本,**不是「本轮」的**;
  · dump 头部不告诉你复合断言的哪一半没过。
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
import time
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

BASE = "http://127.0.0.1:2024"
REPO = Path(__file__).resolve().parents[2]

# sys.path 两条,都必须在任何 import 之前就位:
#   ① 本目录 —— 同目录的 acceptance_dates / acceptance_trace 靠它。以
#      `python scripts/live_acceptance.py` 启动时 sys.path[0] 本来就是这里,
#      但显式插一条,免得有人改成 `python -m` 之后莫名其妙 ModuleNotFoundError。
#   ② backend/src —— C 组要 gyt.core.artifacts、D/E/F 组要 gyt.config,
#      而 acceptance_dates 还会顺着 gyt 包把 schedule 那一大坨拖进来(下面细说)。
#
# ⚠️ 这里原来写着「这里只 import config 与 artifacts」—— **那句是错的**,2026-08-10
#    在本机 backend/.venv 上实测推翻:干净解释器里只跑一句 `import acceptance_dates`,
#    sys.modules 就多出**近两千个模块、百来个顶层包**,langchain、langgraph、openai、
#    tiktoken、httpx 全在里面;gyt 这边进来的是 config、core.errors、core.llm、
#    core.focus、core.base_agent、core.require_tool、db.tasks,以及
#    agents.schedule(连 guard、tools、dates 一起)。
#    成因不在本文件:acceptance_dates._load_dates() **优先走包式 import**(为了覆盖率,
#    理由在它自己的模块 docstring),而 `gyt/agents/schedule/__init__.py:13` 头一行就是
#    `from langgraph.graph.state import CompiledStateGraph`。
#    要紧的那半句仍然成立,而且是同一次实测:**不建图、不要 Key** ——
#    那次 import 跑在没有任何 GYT_*_API_KEY、也没加载 .env 的环境里,
#    `gyt.graph` 自始至终没进过 sys.modules(CLAUDE.md 那条警告说的正是它)。
#    代价只是启动慢一点,不是「import 即花钱」。
#    (刻意**不写**精确条数:2026-08-10 三次独立测量得到 170 / 178 / 180 三个顶层包数,
#     随测法和 venv 状态浮动。写死一个数只会让下一个人复跑对不上,进而怀疑上面那句
#     「不建图、不要 Key」—— 而那半句是真的、也是这段注释里唯一要紧的。测法:
#     干净解释器里 `before = set(sys.modules)` → `import acceptance_dates` → 数差集。)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "backend" / "src"))

# 四句提问的原话**只有一份**,就在 acceptance_dates 里:A1/A4 是固定锚(常量),
# A3/C3 按日历从候选表里挑(``D.a3_phrase`` / ``D.c3_phrase``)。
# 这里不许再把「周五」「下周一之前」写死进句子 —— 写死等于每周有两天在验错东西,
# 而且是「屏幕上全绿、实际零覆盖」的那种错(撞车矩阵见 acceptance_dates 的模块 docstring)。
from acceptance_dates import BRANCH_PAST, PHRASE_A1, Expected, expected  # noqa: E402
from acceptance_trace import (  # noqa: E402
    dump_trace,
    final,
    sched_text,
    trace_tally,
    transfers,
)

from gyt.config import get_settings  # noqa: E402
from gyt.core import artifacts  # noqa: E402

RESULTS: list[tuple[bool, str]] = []

# 脚本启动时刻。F3 拿它判"这一轮**新写**的解析索引",而不是"盘上有没有索引文件" ——
# 后者会被上一轮留下的旧文件糊弄过去,正是 D 组 2026-08-09 栽过的那种假绿灯。
# read_backend_today 也用它判「A1 那行是不是这一轮刚落的」,同一套模式。
STARTED_AT = time.time()

# 失败轨迹落这儿。**刻意不走 get_settings().data_dir**,理由两条:
#   ① .gitignore 的 `/data/*` 是根锚定的(例外只有 !/data/demo/),REPO/"data" 落在
#      它下面是**确定**被忽略,不依赖任何环境变量;GYT_DATA_DIR 指到别处时那条保证就没了。
#      (2026-08-10 用 git check-ignore 核过:data/acceptance_dumps/… 命中 .gitignore:36。)
#   ② dump 是脚本自己写、人自己读的东西,后端全程不参与 —— 不存在「后端写哪儿就得读哪儿」
#      的同源要求。那是 D 组的要求(读的是后端写的库),不是这里的。
# 反过来说,这里**不能**宣称「这样就避开了 GYT_DATA_DIR 指到只读路径时的崩溃」——
# 下面 ledger_db 那行照样会碰 settings.data_dir(访问即 mkdir),该崩还是崩。
# 那条是刻意的,见 ledger_db 处的注释。
DUMP_ROOT = REPO / "data" / "acceptance_dumps"
RUN_DIR = DUMP_ROOT / time.strftime("%Y%m%d-%H%M%S", time.localtime(STARTED_AT))

DUMPS_ATTEMPTED = 0
"""这一轮**试着**落过几份轨迹(不管成没成)。收尾那段靠它区分两种「目录不在」:

  试过 0 次  → 失败的全是 D/E/F 那六条库级断言,它们本来就不落轨迹,目录当然不在。
              正确动作是照 FAIL 行上面打的库路径去查库 / 去建向量库。
  试过 N 次  → 真落盘失败(多半 data/ 写不进去),现场是空的,得先修目录再重跑。

2026-08-10 复验抓到:少了这个计数,新机器第一次跑(还没建向量库,E3/F3 红)时,
屏幕最后一句会叫人去修 data/ 的写权限 —— 而 data/ 好端端的,正确动作(跑 ingest)
就打在两行之上,被这句盖过去了。**又是一条恒真的假线索**,和这一轮删掉的
「★ 本轮业务工具」同一种病,只是换到了收尾那行。"""

CST: Final[timezone] = timezone(timedelta(hours=8))
"""中国标准时。1991 年后中国无夏令时,固定 +8 就是精确值 —— 刻意**不用**
``zoneinfo.ZoneInfo("Asia/Shanghai")``:它依赖系统 tzdata,最小化的 Linux 环境里会抛
ZoneInfoNotFoundError。为了打一条警告去引进一个可能炸的依赖,不划算。"""

CLOCK_SLACK_S: Final[float] = 120.0
"""判断「A1 那行是这一轮刚落的」时允许的时钟余量。要留这个余量是因为两件事:
created_at 是 ``timespec="seconds"`` 截断过的;容器时钟与宿主时钟
(Docker Desktop 那台 Linux VM)可能有秒级漂移。120 秒足够宽,又远小于
「上一轮跑批」的间隔,骗不过陈年快照。"""


def api(path: str, payload: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def new_thread() -> str:
    return api("/threads")["thread_id"]


def ask(tid: str, text: str) -> list[dict]:
    payload = {"assistant_id": "gyt", "input": {"messages": [{"type": "human", "content": text}]}}
    for attempt in (1, 2):
        out = api(f"/threads/{tid}/runs/wait", payload)
        if isinstance(out, dict) and "messages" in out:
            return out["messages"]
        print(f"  [重试{attempt}] runs/wait 返回异常:{json.dumps(out, ensure_ascii=False)[:300]}")
        time.sleep(2)
    raise RuntimeError("runs/wait 连续两次没拿到 messages")


def check(
    ok: bool,
    label: str,
    msgs: list[dict] | None = None,
    *,
    ledger_db: Path | None = None,
) -> None:
    """记一条断言结果;红了且给了 msgs,就把那条线程的完整消息轨迹落盘。

    第三个参数**可选是有意的,不是漏传**:D1~D4 / E3 / F3 六条是库级断言,
    它们的证据是磁盘上的库(路径就在上面几行原样打过),不是消息轨迹 ——
    那六个调用点根本没有 msgs 可给,硬塞个空列表只会落一份看不出任何东西的文件,
    反而把「这条本来就该查库」这个信息盖掉。

    为什么是显式传参而不是让 check 去摸上一轮的 msgs:D1~D4 紧跟在线程 C 之后,
    隐式取最近一轮会给「库里没有整改任务这一行」配上一份线程 C 的巡检轨迹,
    把查库的方向直接带偏到英雄链上。

    ``ledger_db`` 给了就在人读版头部附一份 ``tasks`` 表快照。**台账类断言必须给**
    (A 组十条 + C3),因为轨迹里根本没有子 Agent 的工具调用 —— 「活到底办没办」
    在消息里问不出来,只有库答得了。这也是为什么它是关键字参数:它跟着「这条断言
    验的是不是台账」走,不跟着「有没有 msgs」走,位置传参会让两件事混成一件。
    C1/C2(巡检链)和 E/F 组不给:它们的证据在产物目录和向量库,附一份台账
    只会在头部塞几行不相干的任务,把注意力从真正该查的地方引开。
    """
    global DUMPS_ATTEMPTED

    RESULTS.append((ok, label))
    print(("PASS  " if ok else "FAIL  ") + label)
    if ok or msgs is None:
        return
    # 数「试过几次」而不是只数「成功几次」:收尾那段要靠它区分
    # 「压根没到落盘这一步」和「到了但没落下去」—— 两者的正确动作完全相反。
    DUMPS_ATTEMPTED += 1
    path = dump_trace(label=label, msgs=msgs, out_dir=RUN_DIR, ledger_db=ledger_db)
    if path is not None:
        print(f"      轨迹已存:{path}")


def read_backend_today(db_path: Path) -> date | None:
    """从台账库问出**后端进程**的今天(A1 刚落那行的 ``created_at`` 日期部分)。

    取不到返回 None —— 调用方降级成宿主的今天并打醒目警告,不判红(见调用处)。

    为什么不读 A2 那轮 ``list_tasks`` 的 ToolMessage 里的 ``data.today``:
        **那条消息到不了这里。** ``graph.py:152`` 是 ``OUTPUT_MODE="last_message"``,
        langgraph_supervisor 把子 Agent 结果回灌 supervisor 时会截成最后一条
        (本机 backend/.venv 里 ``langgraph_supervisor/supervisor.py`` 第 77-85 行的
        ``_process_output``),而 schedule 一个回合的最后一条是它自己的纯文本
        AIMessage —— 工具消息在那一步就被丢掉了。msgs 里剩下的 tool 消息只有
        ``transfer_to_*`` 那几对,不带任何业务数据。
        ⚠️ 证据分三档,别混着说:``graph.py:152`` 与 ``supervisor.py:77-85``
        那几行是**本机安装包原文**(2026-08-10 逐行读过);「子 Agent 真调了工具、
        顶层仍然看不到」这件事是在 **Pregel 层**用同版本库起真图验过的
        (案情记在 ``acceptance_trace.py`` 的模块 docstring);而
        **HTTP 层(runs/wait)没有专门做过这个对照实验**。
        所以这条通道仍然不赌 —— 下面 created_at 那条更硬,且已经实测过。

    为什么读 ``tasks.created_at``:
        ``db/tasks.py`` 的 ``_now_iso()`` 是
        ``datetime.now(UTC).astimezone().isoformat(timespec="seconds")``,
        ``.astimezone()`` 无参 = 转成**进程本地时区**,和 ``tools.py`` 里 ``_today()``
        的 ``date.today()`` 是同一个时区源。2026-08-10 本机实测:TZ=UTC 与
        TZ=Asia/Shanghai 两种环境下,``created_at[:10]`` 都逐字等于
        ``date.today().isoformat()``。
        而且它比 ToolMessage 更硬:ToolMessage 只说明「工具这么答」,``created_at``
        说明「后端真的往库里写了一行」—— 和 D 组同一套哲学,不引入第二套取数机制。

    两道防陈年快照(缺一条就会被上一轮留下的旧库骗到,D 组和 F3 都栽过这种):
        ① 新鲜度:这行必须是本次跑批开始之后写的(允许 CLOCK_SLACK_S 的时钟余量);
        ② 标题:最新一行得是 A1 记的那条(含「复检」或「钢筋」)。
    """
    if not db_path.is_file():
        return None
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            # 只读 URI 的理由同 read_ledger:普通 connect 碰上不存在的路径会当场建个空库。
            row = conn.execute(
                "SELECT created_at, title FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        print(f"  [台账库读不出 created_at] {db_path}:{exc}")
        return None
    if row is None:
        print("  [台账库里一行任务都没有] A1 这一轮多半没真的落库。")
        return None

    created_at, title = str(row[0]), str(row[1])
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        print(f"  [created_at 不是合法时间戳] {created_at!r}")
        return None
    if stamp.timestamp() < STARTED_AT - CLOCK_SLACK_S:
        print(f"  [台账最新一行是陈年的] {created_at},比本次跑批还早 —— 库没清干净?")
        return None
    if "复检" not in title and "钢筋" not in title:
        print(f"  [台账最新一行不是 A1 记的那条] 标题是「{title}」")
        return None
    return stamp.date()


TODAY_FROM_BACKEND: Final[str] = "后端台账的 created_at 问出来的"
TODAY_FROM_HOST: Final[str] = "来自宿主机 —— 后端没问出来,是降级值"
"""收尾那行「今天按 X 算」后面跟的出处。

为什么非标不可:降级的横幅打在**开头**,而一轮验收要滚十几分钟、刷几十行 ——
等跑完了,横幅早被顶出屏幕了。看的人只剩最后那行「今天按 2026-08-10 算」,
分不清这个「今天」是后端给的还是脚本自己编的。而这两种情况下,那七条日期断言的
可信度完全不同:前者是在验系统,后者是在赌两台机器同一天。"""


def resolve_expectations(db_path: Path) -> tuple[Expected, str]:
    """定下这一轮的「今天」,派生全部日期期望值,并回一句「这个今天哪来的」。
    **任何情况下都不判红。**

    降级策略是三段的:
        ① 从后端库读到、且新鲜 → 用它。
        ② 读不到 → 退回宿主的今天,打一段挡不住的警告。为什么不额外记一条红断言:
           断言总数是对外口径(README / CLAUDE.md 都写着条数),不该因为一次环境问题
           就变。而且降级本身不会造出假绿灯 —— 后端和宿主真的不在同一天时,
           那七条日期断言会自己红,只是标签上的日期看着眼生。宁可报红,不要假绿。
        ③ 与北京时间对不上 → 只警告,不判红(判红会让部署在别的时区时误报)。

    第二个返回值就是 ② 的补丁:出处跟着期望值一路带到收尾那行去,见 TODAY_FROM_*。
    """
    backend_today = read_backend_today(db_path)
    if backend_today is None:
        fallback = date.today()
        print("  " + "!" * 70)
        print(f"  ! 没能从后端台账库问出「今天」,退回用**这台机器**的今天:{fallback}")
        print("  ! 后面 7 条日期断言用的是脚本自己的今天,和后端可能不是同一天。")
        print("  ! 上面几行写了具体是哪一步没取到 —— 先把那个修了再信这一轮的日期断言。")
        print("  " + "!" * 70)
        source = TODAY_FROM_HOST
        backend_today = fallback
    else:
        source = TODAY_FROM_BACKEND
        print(f"  后端说今天是:{backend_today}(从 tasks.created_at 问出来的)")

    cst_today = datetime.now(CST).date()
    if backend_today != cst_today:
        print("  ⚠️ 后端的今天和北京时间对不上 —— 多半是容器裸 UTC(没设 TZ)。")
        print(f"     后端说 {backend_today},北京时间是 {cst_today}。")
        print("     这一轮的期望日期按后端那个算(所以断言仍然有效),但演示前请把容器时区配对。")
        print("     (裸 UTC 这个成因只在北京时间 00:00–08:00 露馅 —— 白天两边同一天,复现不出来。")
        print("      差得不止一天的话就不是时区问题了,是那台机器的时钟本身不对。)")
    return expected(backend_today), source


def count_tasks(db_path: Path) -> int | None:
    """台账里的**物理行数**。读不出来返回 None(→ D4 判红),不抛栈。

    为什么不能拿 ``len(read_ledger(...))`` 顶替(这是 2026-08-10 复核抓到的真洞):
        ``read_ledger`` 的推导式是 ``{r[1]: r for r in …}`` —— **标题当键**。
        同标题的行会塌成一个键,于是幽灵行一条都数不出来。本机拿桩库实测过:
        库里 6 行、``read_ledger`` 只看见 3 行、``len(rows) == 3`` 判 True。
        而它盖掉的恰恰是两种真事故:
          · 模型把同一条活记了两遍(schedule 重复调 add_task);
          · 上一轮的库没清,新旧同名行叠在一起。
        D4 存在的全部意义就是「没有幽灵行」,拿一个会去重的计数去断它,等于自废。

    只读 URI 的理由同 ``read_ledger``,不重复。
    """
    if not db_path.is_file():
        print(f"  [数不了物理行数:没找到台账库] {db_path}")
        return None
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            return int(conn.execute("SELECT count(*) FROM tasks").fetchone()[0])
    except sqlite3.Error as exc:
        print(f"  [数不了物理行数] {db_path}:{exc}")
        return None


def read_ledger(db_path: Path) -> dict[str, tuple]:
    """把台账里的任务读成 ``{标题: (id, title, due_date, status)}``。

    ⚠️ **标题是键,同名行会塌掉。** D1~D3 按标题取行,这正合用;但「一共几条」
    不许问它,那是 ``count_tasks`` 的活(理由写在那儿)。

    读不到时**不抛栈、也不返回假绿灯**:先打印一段工地师傅看得懂的提示,再返回空表 ——
    空表会让 D1~D4 自然判成四条红(t1/t2/整改行都取不到、条数也不是 3),
    断言总数仍是 25 条,而屏幕上写清了"库在哪儿、为什么读不到"。

    连接必须走**只读 URI**(``as_uri() + "?mode=ro"``)。2026-08-09 本机 python3 实测:
    普通 ``sqlite3.connect(str(path))`` 碰上不存在的路径会当场建一个 0 字节的空库,
    下一句 SELECT 再报 ``no such table: tasks`` —— 既在磁盘上留个垃圾文件,
    又把"库找错地方了"说成"表没建",看的人第一反应会去查建表逻辑,方向全错。
    只读模式下同一个路径直接报 ``unable to open database file``,且文件不会被创建。
    """
    if not db_path.is_file():
        print(f"  [没找到台账库] {db_path}")
        print("  这个文件是后端第一次记任务时自己建的。现在没有,通常是两种情况:")
        print("    · 后端和这个脚本读的不是同一份配置(比如只给其中一边设了 GYT_DATA_DIR);")
        print("    · 或者前面 A 组一条任务都没真正落进去。")
        print("  先照着上面这个路径确认后端到底往哪儿写,再重跑 —— D 组这一轮不作数。")
        return {}
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            return {r[1]: r for r in conn.execute("SELECT id,title,due_date,status FROM tasks")}
    except sqlite3.Error as exc:
        print(f"  [台账库打不开或者读不出来] {db_path}")
        print(f"  底层报的原话:{exc}")
        print("  常见原因:文件在但里面还没有 tasks 表(后端一条任务都没记成),或者文件坏了。")
        print("  删掉它,make dev 重起后端再跑一遍 —— D 组这一轮不作数。")
        return {}


def count_chunks(chroma_db: Path) -> int:
    """数向量库里真有多少个 chunk。读不出来一律返回 0(→ E3 判红),不抛栈。

    为什么直接查 chroma 的 sqlite 而不是问 Agent:E3 要的是**库级铁证**。
    Agent 答得头头是道并不能证明库里有东西 —— 它完全可能是拿提示词里的常识在编,
    而"消防车道不小于 4 米"恰恰是模型预训练里就有的知识,最容易蒙对。

    只读 URI 的理由同 read_ledger:普通 connect 碰上不存在的路径会当场建个空库,
    把"库还没建"说成"表不存在",排查方向全错。
    """
    if not chroma_db.is_file():
        print(f"  [没找到向量库] {chroma_db}")
        print("  向量库是生成物、不进 git,每台机器要自己建一次:")
        print("    docker compose run --rm --no-deps backend python -m gyt.agents.knowledge.ingest")
        return 0
    try:
        with closing(sqlite3.connect(chroma_db.as_uri() + "?mode=ro", uri=True)) as conn:
            return int(conn.execute("SELECT count(*) FROM embeddings").fetchone()[0])
    except sqlite3.Error as exc:
        print(f"  [向量库打不开或读不出来] {chroma_db}")
        print(f"  底层报的原话:{exc}")
        return 0


def manifest_pdfs_present(chroma_dir: Path, docs_dir: Path) -> bool:
    """入库清单里记的规范,现在**还在演示资产目录里**吗?

    这条是 E3 的另一半,专治一种假绿灯:向量库是上一轮建的,而演示资产(规范 PDF)
    这一轮根本没跟过来 —— 光数 chunk 会照样绿,可实际上"资产在不在"已经没人验了。
    把清单里的文件名拿回 docs_dir 下核一遍,资产目录一挪走,这条立刻红。
    """
    mf = chroma_dir / "_ingest_manifest.json"
    if not mf.is_file():
        print(f"  [没找到入库清单] {mf}")
        return False
    try:
        names = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  [入库清单读坏了] {mf}:{exc}")
        return False
    missing = [n for n in names if not (docs_dir / n).is_file()]
    if missing:
        print(f"  [清单里的规范找不着了] {docs_dir} 下缺:{missing}")
    return bool(names) and not missing


# 库路径**只能从配置取**,不许在这里自己拼:后端写哪儿,验收就得读哪儿。
#
# 2026-08-09 差点被这行害死:它原来写死 REPO/"backend"/"data"/"gyt.sqlite3",而
# ``Settings.data_dir`` 的默认值已经改成按 config.py 的 __file__ 推导的仓库根
# (<仓库根>/data)。后端往新库写,脚本读 backend/data 里那份上一轮留下的陈年快照 ——
# 本机实测那份快照里正躺着 (1,'复检三层钢筋','2026-08-14','done') 等三行,
# **逐字命中 D1~D4**。于是四条库级断言全绿,而这一轮一个字都没验。
# 这比直接报红危险得多:D 组存在的全部意义就是"回执说什么不算,库里有没有才算",
# 一旦它读的是快照,这套验收就退化成了"脚本自己跟自己合影"。
#
# resolve() 是给 as_uri() 兜底的:GYT_DATA_DIR 允许填相对路径(.env 里就留着
# `# GYT_DATA_DIR=data` 这行示例),而 as_uri() 遇到相对路径会抛 ValueError(实测)。
#
# 这两行**故意提到 A1 之前**(T4 之前它们在 D 组那儿,跑完 A/B/C 才执行)。
# 两个好处:① A1 一落库就要读 created_at 问「今天」,路径得先备好;
# ② 数据目录有问题时**在花钱之前**就炸,而不是把 15 轮模型调用跑完再说。
# `sqlite_path` 是「访问即 mkdir」的属性,所以 GYT_DATA_DIR 指到写不进去的地方
# (比如有人把容器那份 `/app/data` 抄进了宿主的 .env)会在这里抛
# RuntimeError「数据目录创建失败」—— 宿主实测底层是 OSError [Errno 30]
# Read-only file system: '/app'。那正是我们要的:早炸、炸得看得懂。
settings = get_settings()
ledger_db = settings.sqlite_path.resolve()

print("=== 线程 A:台账全流程 ===")
a = new_thread()

# A1 的期限短语只能从模块常量取,**不能**用 D.a1_phrase:D 要等这一轮落库之后
# 才算得出来(今天是从那行的 created_at 问出来的)。两者永不分叉 ——
# acceptance_dates 的 test_五年全量_四个短语都出自候选表且两个锚从不动 钉着
# D.a1_phrase == PHRASE_A1。
m = ask(a, f"给我建个任务:{PHRASE_A1}复检三层钢筋")
# 顺序是 ask → 定今天 → 再 check A1,不是 ask → check → 定今天。
print(f"  台账库:{ledger_db}")
D, today_source = resolve_expectations(ledger_db)
check(
    "T1" in final(m) and D.a1_display in (final(m) + sched_text(m)),
    f"A1 记任务({D.a1_phrase}→{D.a1_display},T1)",
    m,
    ledger_db=ledger_db,
)

# A2 的「下周三之前」是**故意写死的**,不参与四日不撞车那套:它只是一个查询 cutoff,
# 不落库、不产生第二个期望日期,撞谁都不掉判据。别顺手也给它安一个候选表。
m = ask(a, "下周三之前还有哪些任务没完成")
check(
    "| 任务号 |" in sched_text(m) and "T1" in sched_text(m),
    "A2 期限查询出表格(下周三截止)",
    m,
    ledger_db=ledger_db,
)

# A3 的措辞 2026-08-16 动过一次(W9 supervision 落地),原话是「复检钢筋那条改到{周X}」。
# **只改提问措辞,判据一个字没动。** 为什么必须改:
#   · 原句里没有「任务」「台账」「T 号」这三个 schedule 专属锚点中的任何一个,
#     句子的全部内容是「复检…那条 + 改期」——「复检」与监理的「复查」近义,
#     而 supervision 的正域正是「隐患复查了吗 / 这条该怎么处置」。加了 supervision
#     之后,这句最可能被 supervisor 读成「问某条隐患的复查」而派错人。
#   · A 组是**演示前唯一的兜底**,它红了是查不出方向的:表现是 schedule 从没被叫起来,
#     而人会去查 dates.py 的周几解析。
# 加「台账里」与「那条任务」两个锚,句子仍然**不带 T 号**——A3 本来就是要验
# 「先查后改」(模型得自己按标题找到 T1),这一点没被削弱。
m = ask(a, f"台账里复检钢筋那条任务改到{D.a3_phrase}")
check(
    D.a3_display in (final(m) + sched_text(m)),
    f"A3 改期(先查后改,{D.a3_phrase}→{D.a3_display})",
    m,
    ledger_db=ledger_db,
)

m = ask(a, f"再记一条:{D.a4_phrase}清点脚手架扣件")
check(
    "T2" in final(m) and D.a4_display in (final(m) + sched_text(m)),
    f"A4 第二条任务({D.a4_phrase}→{D.a4_display})",
    m,
    ledger_db=ledger_db,
)

m = ask(a, "T1 干完了")
check("销" in final(m), "A5 销项", m, ledger_db=ledger_db)

m = ask(a, "T1 干完了")
whole = final(m) + sched_text(m)
check(
    ("本来就" in whole or "已经销" in whole or "已经完成" in whole) and "开小差" not in whole,
    "A6 重复销幂等不报错",
    m,
    ledger_db=ledger_db,
)

m = ask(a, "台账里现在都有哪些任务,干完的也列上")
st = sched_text(m)
check(
    "| 任务号 |" in st and "已完成" in st and "未完成" in st,
    "A7 含已完成的表格(绿徽章数据就位)",
    m,
    ledger_db=ledger_db,
)

m = ask(a, "T2 改到猴年马月")
whole = final(m) + sched_text(m)
check(
    "开小差" not in whole and ("明天" in whole or "支持" in whole),
    "A8 乱日期报人话带举例,不是系统开小差",
    m,
    ledger_db=ledger_db,
)

# A9 断的是「不存在的号如实报」,没有任何日期判据 —— 期限说成什么都一样,
# 所以这句里的「周五」**不用**跟着 D.a3_phrase 走(跟着走反而让人以为它和 A3 同源)。
m = ask(a, "T99 改到周五")
check(
    "T99" in (final(m) + sched_text(m)) and "没有" in (final(m) + sched_text(m)),
    "A9 不存在的号如实报",
    m,
    ledger_db=ledger_db,
)

# A10 —— 「本周X」这条契约的两半,按日历分支(W5 · T5)。
# 契约只有一条:**「本周X」由日历说了算 —— 已过就报错不猜,没过就照常出结果。**
# 周四~周日走报错支,周一~周三走正常支;两支问不同的话,断的是同一条契约。
# 分支怎么选、正常支为什么改问「本周日」,理由都在 acceptance_dates._a10_branch。
m = ask(a, f"{D.a10_phrase}之前有啥任务没干完")
st = sched_text(m)
whole = final(m) + st
if D.a10_branch == BRANCH_PAST:
    # 报错支。要防的回归是:有人「优化」掉 _parse_this_week 的报错分支,让本周三
    # 静默滚成下周三(dates.py 模块 docstring 明文把这列为「最危险的猜测」)。
    # 真滚了的话 list_tasks 会成功、cutoff 落到本周一+9、T2 进表 —— 所以
    # 「一条查询结果都没产出」是事实级判据,比只看措辞硬得多。
    # D.a10_hint(=「下周三」)抄自 dates.py:213-214 抛出来的真异常原文
    # (prompt.md 红线 1 要求原样转述),acceptance_dates 那边拿真异常核过、没凭记忆写。
    # 断它 = 断一条产品要求:报了错就得给改法,不能给师傅一条死胡同。
    # 这一支里 a10_hint 恒非空(expected() 保证),所以敢直接 in。
    check(
        D.a10_phrase in whole
        and ("已经过" in whole or "过了" in whole)
        and D.a10_hint in whole
        and "开小差" not in whole
        and "| 任务号 |" not in st
        and "T2" not in st,
        f"A10 {D.a10_phrase}已过→报错不猜且不出任何查询结果(今天周{D.weekday_char})",
        m,
        ledger_db=ledger_db,
    )
else:
    # 正常支。三条正向铁证:真出了工作表格(list_tasks 真被调过并返回了数据)、
    # 表里是 T2、期限列**照抄了工具算好的 due_display**。
    #
    # 第三条为什么算「断事实」而不是「断措辞」——理由 2026-08-10 自己 grep 过一遍,
    # 而且和上一版写的**不一样**,上一版是错的,别照抄那个说法:
    #   · 上一版写「全仓没有任何一处把今天的日期注入提示词,模型手上没有日历」。
    #     错。``agents/schedule/tools.py:168`` 的 list_tasks 信封里就有
    #     ``"today": today_iso``,而那条 ToolMessage 正是这一轮送进模型上下文的 ——
    #     模型手上有今天的 ISO 日期。(上一版还说 grep 只有 report 的报告编号用了 strftime,
    #     也漏了:2026-08-10 重 grep 一遍,真正的 now/today 调用点一共**六处** ——
    #     schedule/tools.py:66、report/tools.py:85(生成时间写进 docx)与 :151(报告编号)、
    #     artifacts.py:197 与 :209、db/tasks.py:82。)
    #   · 真正立得住的是这个:判据串是 ``8月12日(周三)`` 这种**带星期字的展示形态**,
    #     **代码里**只有 ``dates.format_display``(dates.py:126)产得出来,它算好的结果
    #     就摆在 ``_task_item``(tools.py:99)的 ``due_display`` 字段里;
    #     ``agents/schedule/prompt.md:36-37`` 还明文禁止模型自己把日期换算成星期几。
    #     ⚠️ 但**不能**说「全仓再无第二处拼这个形状」—— 那句话上一版写了,是错的:
    #     ``schedule/prompt.md`` 里有六处同形状的静态例子(36/46/54/58/59/80 行),
    #     其中 :54 和 :59 就是 ``8月12日(周三)``,而 prompt.md 是直接进模型上下文的。
    #     所以还有第三条路:照抄提示词里的例子。这条断言的强度靠同一行的
    #     ``T2 in st`` 与 ``T1 not in st`` 一起兜 —— prompt.md 的例子行是 T3/T5/T7,
    #     整段照抄过不了那两条。单看日期那一半,它没有原来吹的那么硬。
    # ⚠️ 这条契约仍然值钱,只是前提换了个说法:哪天有人把 due_display 从信封里拿掉、
    #    或者放开 prompt.md 那条禁令,这条断言的前提就没了。
    # 「T1 not in st」验的是 include_done=False 真的把销了的滤掉了 —— 销了的活
    # 不许出现在「没干完」里。
    #
    # 探针:主问「本周日」在周一~周三永远和「周日」解析到同一天,**碰不到**
    # _parse_this_week 的报错分支。不补这一问,有人把那个 raise 删掉、让本周三静默
    # 滚成下周三,周一~周三跑验收照样全绿。补问只在 a10_probe_phrase 非空时发
    # (= 周二/周三;周一那天日历上根本不存在「已经过的本周 X」,发了会变成一次正常
    # 解析,白烧一轮模型调用还让判据似是而非)。
    # 判据 and 进 A10 这一条,**不新开断言** —— 断言总数 25 是对外口径。
    # 探针必须发在主问**之后**、同一条线程里:先发会让「已经过」污染主问那一轮的 whole。
    probe_ok, dump_msgs = True, m
    if D.a10_probe_phrase is not None:
        pm = ask(a, f"{D.a10_probe_phrase}之前有啥任务没干完")
        pst = sched_text(pm)
        pwhole = final(pm) + pst
        probe_ok = (
            D.a10_probe_phrase in pwhole
            and ("已经过" in pwhole or "过了" in pwhole)
            and D.a10_probe_hint in pwhole
            and "开小差" not in pwhole
            and "| 任务号 |" not in pst  # 报错了就不该出任何查询结果
            and "T2" not in pst
        )
        if not probe_ok:
            dump_msgs = pm  # 红在探针就落探针那一轮的现场,不是主问那一轮
    check(
        "| 任务号 |" in st
        and "T2" in st
        and D.a10_due_display in st
        and "T1" not in st
        and "已经过" not in whole
        and "开小差" not in whole
        and probe_ok,
        f"A10 {D.a10_phrase}未过→正常出表格且期限={D.a10_due_display}"
        f"(今天周{D.weekday_char}"
        + (f",含「{D.a10_probe_phrase}」探针)" if D.a10_probe_phrase else ")"),
        dump_msgs,
        ledger_db=ledger_db,
    )

print("=== 线程 B:路由边界 ===")
b = new_thread()
m = ask(b, "看一下这个")
check(not transfers(m), "B1 模糊请求不乱派(supervisor 自己追问)", m)

b2 = new_thread()
m = ask(b2, "帮我算算这个月工资能拿多少")
check(not transfers(m), "B2 超范围如实说做不了,不硬塞", m)

print("=== 线程 C:英雄链 + 协同 ===")
with (REPO / "backend/eval/datasets/safety.csv").open(encoding="utf-8") as f:
    row = next(r for r in csv.DictReader(f) if r["label"] == "violation")

aid = artifacts.register(
    (REPO / "data/demo/photos" / row["image"]).read_bytes(),
    kind=artifacts.ArtifactKind.PHOTO,
    original_name=row["image"],
)
c = new_thread()
m = ask(c, f"查一下这张照片,顺便出份巡检记录(照片编号:{aid})")
whole = " ".join(str(x["content"]) for x in m)
check("transfer_to_inspection" in str(transfers(m)), "C1 「查+出记录」派英雄链 inspection", m)
check("GYT-" in whole, "C2 巡检记录已生成(报告编号 GYT-)", m)

# c3_phrase **自带「之前」**(「下周一之前」),模板里别再拼一次 —— 拼成
# 「下周一之前之前」照样能被 dates 的尾缀剥离救回来,但那就不是脚本真想说的话了。
#
# 这句的措辞 2026-08-16 也动过(同 A3,W9 supervision 落地),原话是
# 「把这些隐患记成一条整改任务,{c3_phrase}搞定」。**判据一个字没动。**
# 原句里「隐患」+「整改」两个词都是 supervision 的核心名词,而它前面刚跑完巡检链、
# 上文里全是隐患清单 —— 这是 A/C 两组里**最容易被钓走**的一句。改法是把宾语换成
# 中性的「这些问题」,再补一个「在任务台账里」的 schedule 锚;「整改任务」四个字必须留着:
# D3 那条库级断言是按标题里有没有「整改/隐患」去捞那一行的(见下面 rect 那句)。
#
# 这么改**没有**削弱这条断言真正要验的东西:「这些问题」照样是个指代,schedule 仍然
# 必须看得见兄弟 Agent(safety)报的那份清单才写得出标题 —— 那正是 core/focus.py
# 顶部记的 2026-08-08 教训(滤网把兄弟 Agent 的话滤了,协同当场断裂)。
m = ask(c, f"把这些问题在任务台账里记成一条整改任务,{D.c3_phrase}搞定")
whole = final(m) + sched_text(m)
# 第二个连接词原来写的是 `"T" in whole` —— 那是**恒真**的:同一轮 whole 里躺着巡检
# 记录编号 GYT-…,里面就有个 T。这半条断言从写下那天起就没起过作用。
# 换成 `T\d`:巡检编号是 "GYT-20260810-…",T 后面跟的是短横不是数字,不会误命中。
check(
    D.c3_display in whole and bool(re.search(r"T\d", whole)),
    f"C3 协同:巡检后一句话记整改任务({D.c3_phrase}→{D.c3_display})",
    m,
    # C3 是台账类断言(整改任务要真落库),所以给库路径;同线程的 C1/C2 是巡检链,
    # 证据在产物目录,不给。
    ledger_db=ledger_db,
)

print("=== 库级断言:回执说什么不算,库里有没有才算 ===")
print(f"  台账库:{ledger_db}")
rows = read_ledger(ledger_db)
t1 = rows.get("复检三层钢筋")
check(
    bool(t1) and t1[3] == "done" and t1[2] == D.d1_iso,
    f"D1 库:T1 改期 {D.d1_iso} 且真的销了(status=done)",
)
t2 = rows.get("清点脚手架扣件")
check(bool(t2) and t2[3] == "open" and t2[2] == D.d2_iso, f"D2 库:T2 后天={D.d2_iso} 未完成")
rect = [r for title, r in rows.items() if "整改" in title or "隐患" in title]
check(
    bool(rect) and rect[0][2] == D.d3_iso,
    f"D3 库:整改任务真实落库且期限={D.c3_phrase} {D.d3_iso}",
)
# D4 断的是**物理行数**,不是 len(rows)。rows 按标题去重,幽灵行会塌进同一个键 ——
# 本机桩库实测过:6 行进库、rows 只剩 3 个键、旧写法 len(rows)==3 判 True,
# 「模型把同一条活记了两遍」和「上一轮的库没清」两种真事故一条都报不出来。
# 两个数都打出来:不一致本身就是「有同名行」的直接证据,比只看总数更快定位。
physical = count_tasks(ledger_db)
check(
    physical == 3,
    f"D4 库:恰好 3 条(物理行数 {physical};按标题去重后 {len(rows)}:{list(rows)})——没有幽灵行",
)

print("=== 线程 E:规范检索(knowledge)===")
# 问题与判据都抄 eval/datasets/rag.csv 的 K01 那一行,不自己另编一套 ——
# 那份数据集的 expected_source / expected_page 是逐条核对过 PDF 的,是现成的真相。
e = new_thread()
m = ask(e, "消防车道的净宽度和净空高度有什么要求")
whole = " ".join(str(x["content"]) for x in m)
check("transfer_to_knowledge" in str(transfers(m)), "E1 规范提问派给 knowledge", m)
check(
    ("4.0" in whole or "4 米" in whole or "4米" in whole) and "50016" in whole,
    "E2 答出「不应小于 4.0」并给规范出处(GB50016)",
    m,
)

docs_dir = settings.demo_assets_dir / "docs"
chunks = count_chunks(settings.chroma_dir / "chroma.sqlite3")
print(f"  向量库:{settings.chroma_dir}({chunks} 个 chunk)")
check(
    chunks > 0 and manifest_pdfs_present(settings.chroma_dir, docs_dir),
    f"E3 库:向量库真有 chunk({chunks}),且清单里的规范还在 {docs_dir}",
)

print("=== 线程 F:图纸(cad)===")
f = new_thread()
m = ask(f, "首层平面图有哪些图层")
whole = " ".join(str(x["content"]) for x in m)
check("transfer_to_cad" in str(transfers(m)), "F1 图纸提问派给 cad", m)
# 真实图层表(用纯 python 读 DXF 组码扫出来的):0 / Defpoints / 墙 / 轴线 / 柱 / 标注。
# 只要 4 个中文图层里报对 3 个就算数 —— 留一格余量给模型的措辞,但绝不放过"泛泛而谈"。
hit_layers = [x for x in ("墙", "轴线", "柱", "标注") if x in whole]
check(len(hit_layers) >= 3, f"F2 出真实图层名(命中 {hit_layers})", m)

# 这条**只认这一轮新写的**索引文件。换成"目录里有没有 json"就会被上一轮留下的旧文件
# 糊弄过去 —— 演示资产整个挪走、cad 一张图都没解析成,它照样绿。
# (D 组 2026-08-09 就是栽在这种"读上一轮快照"上,同一个坑不踩第二次。)
fresh = [p for p in settings.cad_index_dir.glob("*.json") if p.stat().st_mtime >= STARTED_AT]
print(f"  解析索引:{settings.cad_index_dir}(本轮新写 {len(fresh)} 份)")
check(bool(fresh), f"F3 库:本轮真的解析并落盘了图纸索引({[p.name for p in fresh]})")

print()
passed = sum(1 for ok, _ in RESULTS if ok)
print(f"===== {passed}/{len(RESULTS)} 通过 =====")
print(f"      今天按 {D.today_iso} 算({today_source}),A10 走的是「{D.a10_branch}」这一支")
for ok, label in RESULTS:
    if not ok:
        print("未过:", label)

# 轨迹这一段**无条件打**。旧写法是 `if passed < len(RESULTS) and RUN_DIR.is_dir()`,
# 于是 data/ 写不进去时两个条件同时假 —— 屏幕上一个字都不提轨迹,看的人合理推断
# 「T3 压根没接上」,而真相是接上了、只是一份都没落下。沉默是这里最坏的输出。
if passed == len(RESULTS):
    print("全绿,没有失败轨迹要留。")
elif RUN_DIR.is_dir():
    print(f"失败轨迹都在:{RUN_DIR}")
    print("  30 秒判据看人读版 .txt 头部的**台账快照**:回执说什么不算,库里有没有才算。")
    print("  ⚠ 头部**不会**有子 Agent 的工具调用 —— 那是 output_mode=last_message 砍掉的,")
    print("    不代表模型没调工具。别照着「时间线里没有业务工具」去查提示词,那条路是死的。")
elif DUMPS_ATTEMPTED == 0:
    # 目录不在,但一次都没试着落 —— 说明红的全是 D/E/F 那六条库级断言。
    # 这不是故障,是那六条本来就不落轨迹。别在这儿喊「data/ 写不进去」。
    print("这一轮红的都是库级断言(D/E/F),它们的证据在库和盘上,不落消息轨迹。")
    print("  每条 FAIL 行上面都原样打过路径 —— 台账库 / 向量库 / 解析索引,照那个查。")
    print("  向量库是生成物、不进 git,新机器要先建一次:")
    print("    docker compose run --rm --no-deps backend python -m gyt.agents.knowledge.ingest")
else:
    print(f"⚠ 试着落了 {DUMPS_ATTEMPTED} 份轨迹,一份都没落下 —— {RUN_DIR} 这个目录没建起来。")
    print("  多半是 data/ 写不进去(权限、盘满)。每条 FAIL 下面都打过具体原因;")
    print("  现场是空的,把目录修好再重跑一遍。")

# 落盘战果单独报一行:上面那句「轨迹都在 X」只说了目录,说不了「有几条其实没进去」。
tally = trace_tally()
if tally.failed or tally.degraded:
    print(
        f"⚠ 现场没留下的:{tally.failed} 条(两份都没落下,只能重跑);"
        f"只留下一半的:{tally.degraded} 条(证据不全,多半将就能查)。"
    )
