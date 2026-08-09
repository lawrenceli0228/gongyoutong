"""工友通 · 真机验收脚本(19 断言):台账全流程 / 路由边界 / 英雄链协同 / 库级铁证。

跑法(会真调模型,几分钱;视觉走缓存):
    1. 确保 langgraph dev 在 :2024(make dev),仓库根 .env 配好两把 Key
    2. 想从空台账开始(在仓库根执行):rm -f data/gyt.sqlite3
       —— ``data/`` 是 ``Settings.data_dir`` 的默认值(<仓库根>/data);设了 GYT_DATA_DIR
       就删那底下那份。``backend/data/`` 里可能还留着一份旧库,那是历史遗留,
       后端**已经不写那儿**了,删它没用(反而会让人以为清干净了)。
       脚本跑到 D 组时会把自己实际读的库路径原样打出来,一切以那一行为准。
    3. cd backend && uv run --env-file ../.env python scripts/live_acceptance.py

D 组是库级断言 —— 回执说什么不算数,库里有没有才算数(防假账回归,
2026-08-08 真机抓获过:多轮后模型不调工具凭记忆编回执,guard.py 因此而生)。"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
import urllib.request
from contextlib import closing
from pathlib import Path

BASE = "http://127.0.0.1:2024"
REPO = Path(__file__).resolve().parents[2]
RESULTS: list[tuple[bool, str]] = []


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
    import time

    payload = {"assistant_id": "gyt", "input": {"messages": [{"type": "human", "content": text}]}}
    for attempt in (1, 2):
        out = api(f"/threads/{tid}/runs/wait", payload)
        if isinstance(out, dict) and "messages" in out:
            return out["messages"]
        print(f"  [重试{attempt}] runs/wait 返回异常:{json.dumps(out, ensure_ascii=False)[:300]}")
        time.sleep(2)
    raise RuntimeError("runs/wait 连续两次没拿到 messages")


def check(ok: bool, label: str) -> None:
    RESULTS.append((ok, label))
    print(("PASS  " if ok else "FAIL  ") + label)


def final(msgs: list[dict]) -> str:
    return str(msgs[-1]["content"])


def sched_text(msgs: list[dict]) -> str:
    parts = [
        str(m["content"])
        for m in msgs
        if m.get("name") == "schedule" and m.get("type") == "ai" and not m.get("tool_calls")
    ]
    return parts[-1] if parts else ""


def transfers(msgs: list[dict]) -> list[str]:
    return [
        c["name"]
        for m in msgs
        for c in (m.get("tool_calls") or [])
        if str(c["name"]).startswith("transfer_to_")
    ]


def read_ledger(db_path: Path) -> dict[str, tuple]:
    """把台账里的任务读成 ``{标题: (id, title, due_date, status)}``。

    读不到时**不抛栈、也不返回假绿灯**:先打印一段工地师傅看得懂的提示,再返回空表 ——
    空表会让 D1~D4 自然判成四条红(t1/t2/整改行都取不到、条数也不是 3),
    断言总数仍是 19 条,而屏幕上写清了"库在哪儿、为什么读不到"。

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


print("=== 线程 A:台账全流程 ===")
a = new_thread()

m = ask(a, "给我建个任务:明天上午复检三层钢筋")
check(
    "T1" in final(m) and "8月9日(周日)" in (final(m) + sched_text(m)),
    "A1 记任务(明天上午→8月9日周日,T1)",
)

m = ask(a, "下周三之前还有哪些任务没完成")
check("| 任务号 |" in sched_text(m) and "T1" in sched_text(m), "A2 期限查询出表格(下周三截止)")

m = ask(a, "复检钢筋那条改到周五")
check("8月14日(周五)" in (final(m) + sched_text(m)), "A3 改期(先查后改→8月14日周五)")

m = ask(a, "再记一条:后天清点脚手架扣件")
check(
    "T2" in final(m) and "8月10日(周一)" in (final(m) + sched_text(m)),
    "A4 第二条任务(后天→8月10日周一)",
)

m = ask(a, "T1 干完了")
check("销" in final(m), "A5 销项")

m = ask(a, "T1 干完了")
whole = final(m) + sched_text(m)
check(
    ("本来就" in whole or "已经销" in whole or "已经完成" in whole) and "开小差" not in whole,
    "A6 重复销幂等不报错",
)

m = ask(a, "台账里现在都有哪些任务,干完的也列上")
st = sched_text(m)
check("| 任务号 |" in st and "已完成" in st and "未完成" in st, "A7 含已完成的表格(绿徽章数据就位)")

m = ask(a, "T2 改到猴年马月")
whole = final(m) + sched_text(m)
check(
    "开小差" not in whole and ("明天" in whole or "支持" in whole),
    "A8 乱日期报人话带举例,不是系统开小差",
)

m = ask(a, "T99 改到周五")
check(
    "T99" in (final(m) + sched_text(m)) and "没有" in (final(m) + sched_text(m)),
    "A9 不存在的号如实报",
)

m = ask(a, "本周三之前有啥任务没干完")
whole = final(m) + sched_text(m)
check("已经过" in whole or "过了" in whole, "A10 本周三已过→报错不猜")

print("=== 线程 B:路由边界 ===")
b = new_thread()
m = ask(b, "看一下这个")
check(not transfers(m), "B1 模糊请求不乱派(supervisor 自己追问)")

b2 = new_thread()
m = ask(b2, "帮我算算这个月工资能拿多少")
check(not transfers(m), "B2 超范围如实说做不了,不硬塞")

print("=== 线程 C:英雄链 + 协同 ===")
sys.path.insert(0, str(REPO / "backend" / "src"))
with (REPO / "backend/eval/datasets/safety.csv").open(encoding="utf-8") as f:
    row = next(r for r in csv.DictReader(f) if r["label"] == "violation")
from gyt.core import artifacts  # noqa: E402  (必须等 sys.path 指到 backend/src 之后)

aid = artifacts.register(
    (REPO / "data/demo/photos" / row["image"]).read_bytes(),
    kind=artifacts.ArtifactKind.PHOTO,
    original_name=row["image"],
)
c = new_thread()
m = ask(c, f"查一下这张照片,顺便出份巡检记录(照片编号:{aid})")
whole = " ".join(str(x["content"]) for x in m)
check("transfer_to_inspection" in str(transfers(m)), "C1 「查+出记录」派英雄链 inspection")
check("GYT-" in whole, "C2 巡检记录已生成(报告编号 GYT-)")

m = ask(c, "把这些隐患记成一条整改任务,下周一之前搞定")
whole = final(m) + sched_text(m)
check("8月10日(周一)" in whole and ("T" in whole), "C3 协同:巡检后一句话记整改任务(下周一→8月10日)")

print("=== 库级断言:回执说什么不算,库里有没有才算 ===")
# 库路径**只能从配置取**,不许在这里自己拼:后端写哪儿,验收就得读哪儿。
#
# 2026-08-09 差点被这行害死:它原来写死 REPO/"backend"/"data"/"gyt.sqlite3",而
# ``Settings.data_dir`` 的默认值已经改成按 config.py 的 __file__ 推导的仓库根
# (<仓库根>/data)。后端往新库写,脚本读 backend/data 里那份上一轮留下的陈年快照 ——
# 本机实测那份快照里正躺着 (1,'复检三层钢筋','2026-08-14','done') 等三行,
# **逐字命中 D1~D4**。于是四条库级断言全绿,而这一轮一个字都没验。
# 这比直接报红危险得多:D 组存在的全部意义就是"回执说什么不算,库里有没有才算",
# 一旦它读的是快照,这套验收就退化成了"脚本自己跟自己合影"。
from gyt.config import get_settings  # noqa: E402  (同 artifacts:必须等 sys.path 就位)

# resolve() 是给 as_uri() 兜底的:GYT_DATA_DIR 允许填相对路径(.env 里就留着
# `# GYT_DATA_DIR=data` 这行示例),而 as_uri() 遇到相对路径会抛 ValueError(实测)。
ledger_db = get_settings().sqlite_path.resolve()
print(f"  台账库:{ledger_db}")
rows = read_ledger(ledger_db)
t1 = rows.get("复检三层钢筋")
check(
    bool(t1) and t1[3] == "done" and t1[2] == "2026-08-14",
    "D1 库:T1 改期 8-14 且真的销了(status=done)",
)
t2 = rows.get("清点脚手架扣件")
check(bool(t2) and t2[3] == "open" and t2[2] == "2026-08-10", "D2 库:T2 后天=8-10 未完成")
rect = [r for title, r in rows.items() if "整改" in title or "隐患" in title]
check(bool(rect) and rect[0][2] == "2026-08-10", "D3 库:整改任务真实落库且期限=下周一 8-10")
check(len(rows) == 3, f"D4 库:恰好 3 条(实际 {len(rows)}:{list(rows)})——没有幽灵行")

print()
passed = sum(1 for ok, _ in RESULTS if ok)
print(f"===== {passed}/{len(RESULTS)} 通过 =====")
for ok, label in RESULTS:
    if not ok:
        print("未过:", label)
