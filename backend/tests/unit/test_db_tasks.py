"""db/tasks.py(任务台账 SQLite 存储层)的单元测试 —— 不联网,库落在用例独占的 tmp_path。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):它把 GYT_DATA_DIR
指到 tmp_path/data,并在用例前后各做一次 ``get_settings.cache_clear()``,
所以本文件不需要任何自建 fixture,每个用例天然拿到一张全新的库。

这层要钉死的静默错误:
  · 排序写错(无期限混进有期限中间、或被吞掉)——「下周三之前还有啥」会悄悄漏活,
    评测分数与真机演示都看不出病灶在存储层;
  · SQL 拼值(而非参数化)—— 标题里一个单引号就够炸表,注入用例直接验"表还活着";
  · CHECK 约束没真建上 —— 脏状态静默入库,要到查询端才爆雷,离病灶隔了一层;
  · **迁移没跑到**(W9 §6.6)—— 这是本文件里唯一一条「本机永远复现不出来」的失效:
    测试库每次都是新建的,只改 DDL 也全绿,而线上那张 W3 建的老表没有 hazard_no,
    第一条查询就 `no such column`。所以下面专门造一张老库来测。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from gyt.config import get_settings
from gyt.db import tasks

# 手工做旧用的时间戳:一眼假,断言里出现时绝不会与真实当前时间撞车。
OLD_STAMP = "2000-01-01T00:00:00+00:00"

# 隐患号样例,形状同 db/hazards.py 的 GYT-H-YYYYMMDD-HHMMSS-4hex。
# 这层不校验形状(存取保真而已),取真形状只是为了断言失败时一眼看出这是隐患号。
HAZARD_A = "GYT-H-20260816-101500-a1b2"
HAZARD_B = "GYT-H-20260816-101500-c3d4"

# W3 时代的建表语句 —— **逐字冻结的历史拷贝**,线上 data/gyt.sqlite3 里那张表就长这样。
# 🔴 故意不从 tasks._DDL 派生:派生的话以后 DDL 一改,这份"老表"就跟着变新,
#    迁移用例会静默退化成「用新表测新表」—— 表面全绿,而真正要测的那件事
#    (旧表能不能升上来)一次都没跑过。要加列请改 tasks._MIGRATIONS,别动这份拷贝。
LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  due_date   TEXT,
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
  project_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _raw_connect() -> sqlite3.Connection:
    """绕过存储层直连库文件:做旧数据、硬塞脏状态,都需要"上帝视角"。"""
    return sqlite3.connect(get_settings().sqlite_path)


def _task_columns() -> list[str]:
    """库里 tasks 表当前的物理列名(按物理列序)。迁移用例的唯一判据。"""
    with closing(_raw_connect()) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]


def _make_legacy_table() -> None:
    """造一张 W3 时代的老库:没有 hazard_no 列,里面躺着两行真实数据。

    两行是刻意的:一行有期限、一行没有,一行 open、一行 done ——
    迁移之后这四种取值都要原样还在,才敢说「线上数据没被动过」。
    """
    with closing(_raw_connect()) as conn, conn:
        conn.execute(LEGACY_DDL)
        conn.executemany(
            "INSERT INTO tasks (title, due_date, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("上线前就记着的活", "2026-08-10", "open", OLD_STAMP, OLD_STAMP),
                ("上线前就销了的活", None, "done", OLD_STAMP, OLD_STAMP),
            ],
        )


def _rewind_updated_at(task_id: int, stamp: str) -> None:
    """把某行的 updated_at 手工拨回过去。

    存储层的时间戳只有秒级,同一秒内两次操作分不出先后;靠 sleep 又慢又 flaky。
    先做旧再操作,断言「值不再是旧值」就是确定性的。
    """
    with closing(_raw_connect()) as conn, conn:
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, task_id))


def test_建取往返全字段保真() -> None:
    """create→fetch 的字段级往返:SELECT 列序与 TaskRow 对不上位时在这里现形。"""
    task_id = tasks.create("三层钢筋复检", "2026-08-10")

    row = tasks.fetch(task_id)

    assert row is not None
    assert row.id == task_id
    assert row.title == "三层钢筋复检"
    assert row.due_date == "2026-08-10"
    assert row.status == tasks.STATUS_OPEN
    assert row.project_id is None  # 定案 #11:预留列,MVP 恒 NULL
    assert row.hazard_no is None  # W9 D13:不传就是普通任务,不是隐患整改的活
    assert row.created_at != ""
    assert row.created_at == row.updated_at  # 生辰两枚时间戳必须同源同值


def test_无期限任务due存None取None() -> None:
    """due=NULL 是合法状态(定案 #7),不许被悄悄存成空串 —— 空串会毁掉 IS NULL 分桶。"""
    task_id = tasks.create("给老赵回个电话", None)

    row = tasks.fetch(task_id)

    assert row is not None
    assert row.due_date is None


def test_fetch不存在的编号返回None() -> None:
    """查无此行用 None 说话,不抛异常 —— 「T 号不存在」的人话提示归工具层拼。"""
    assert tasks.fetch(999) is None


def test_列表有期限升序无期限垫底() -> None:
    """排序是「下周三之前还有啥」的地基:已逾期最靠前,无期限垫底但**不丢**(定案 #7)。"""
    tasks.create("无期限的活", None)
    tasks.create("下下周的活", "2026-08-20")
    tasks.create("下周的活", "2026-08-10")
    tasks.create("已逾期的活", "2026-08-01")

    got = [row.due_date for row in tasks.list_rows()]

    assert got == ["2026-08-01", "2026-08-10", "2026-08-20", None]


def test_列表同期限按编号先来先排() -> None:
    """同一天的活按 id 稳定排序 —— 顺序抖动会让「T3」两次指向不同的活。"""
    first = tasks.create("同一天的活甲", "2026-08-10")
    second = tasks.create("同一天的活乙", "2026-08-10")

    assert [row.id for row in tasks.list_rows()] == [first, second]


def test_已完成默认隐藏include_done才露出() -> None:
    """默认视角是"还有啥没干":done 的行必须存在但不打扰;include_done 再全量露出。"""
    open_id = tasks.create("还没干的活", "2026-08-10")
    done_id = tasks.create("干完的活", "2026-08-09")
    assert tasks.set_done(done_id) is True

    assert [row.id for row in tasks.list_rows()] == [open_id]

    all_rows = tasks.list_rows(include_done=True)
    assert [row.id for row in all_rows] == [done_id, open_id]  # 8-09 排在 8-10 前
    assert {row.status for row in all_rows} == {tasks.STATUS_OPEN, tasks.STATUS_DONE}


def test_set_due改期后期限与updated_at都是新的() -> None:
    """改期必须同时刷新 updated_at —— 只改 due 的话,台账"最后动过的时间"会说谎。"""
    task_id = tasks.create("模板验收", "2026-08-10")
    _rewind_updated_at(task_id, OLD_STAMP)

    assert tasks.set_due(task_id, "2026-08-14") is True

    row = tasks.fetch(task_id)
    assert row is not None
    assert row.due_date == "2026-08-14"
    assert row.updated_at != OLD_STAMP


def test_set_done销项且重复销项幂等() -> None:
    """幂等是刻意契约(定案 #9):多轮对话里重复说"干完了"不该报错吓人;
    「本来就完成了」的判断与话术归工具层,它会先 fetch 再措辞。"""
    task_id = tasks.create("要销项的活", None)

    assert tasks.set_done(task_id) is True
    row = tasks.fetch(task_id)
    assert row is not None
    assert row.status == tasks.STATUS_DONE

    assert tasks.set_done(task_id) is True  # 再销一次照样 True,不炸不变卦


def test_对不存在的编号set_due与set_done都返回False() -> None:
    """False 是「T 号不存在」提示的唯一信号源:靠 rowcount 说话,不靠异常。"""
    assert tasks.set_due(999, "2026-08-14") is False
    assert tasks.set_done(999) is False


def test_注入串当标题存取原样表安然无恙() -> None:
    """方案红线 4:值一律走 ? 占位。若有人改成拼接 SQL,这个标题会当场把表炸掉。"""
    evil = "三层'; DROP TABLE tasks;--"
    task_id = tasks.create(evil, None)

    row = tasks.fetch(task_id)
    assert row is not None
    assert row.title == evil  # 原样进出,不许带转义痕迹

    survivor = tasks.create("表还活着的证据", None)
    assert tasks.fetch(survivor) is not None


def test_库文件落在测试独占的临时目录(tmp_path: Path) -> None:
    """conftest 把 GYT_DATA_DIR 指到 tmp_path/data,库文件必须落在那里 ——
    而不是悄悄写进仓库里那份真台账(默认是 <仓库根>/data/gyt.sqlite3)。

    这类坑本项目栽过两次:prefilter.py 的相对路径把 27 张照片写到了 backend/data/;
    ``Settings.data_dir`` 的默认值曾是 ``Path("data")``,`make dev` 在 backend/ 下跑就写
    backend/data/、容器写 /app/data,两份数据互相看不见。默认值现在已按 config.py 的
    __file__ 推导仓库根 —— 但**测试恰恰不能依赖那个默认值**:跑一遍单测就往真台账里
    塞几条测试行,轻则污染演示数据,重则让真机验收的库级断言(D4 要求"恰好 3 条")
    读到测试造出来的幽灵行。所以这条钉的是隔离,不是默认值。"""
    tasks.create("落位检查", None)

    expected = tmp_path / "data" / "gyt.sqlite3"
    assert expected.exists()
    assert get_settings().sqlite_path == expected


def test_CHECK约束真的拦住非法status() -> None:
    """约束只有 DDL 真写进库才有效:绕过存储层硬塞 status='half' 必须当场被拒 ——
    否则脏状态静默入库,要到查询端才爆雷,离病灶隔了一层。"""
    tasks.create("先把表建出来", None)  # 触发幂等建表

    with closing(_raw_connect()) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (title, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("脏数据", "half", OLD_STAMP, OLD_STAMP),
        )


def test_改期_已销项的行被状态守卫拦住() -> None:
    """set_due 的 WHERE 带 status='open':工具层「先 fetch 再改」的两步之间
    存在理论窗口(并发销项),没有这道守卫,已完成的任务会被静默改期、
    绕过「已完成不许改期」的业务规则(审查 LOW 项)。返回值必须说真话。"""
    task_id = tasks.create("模板验收", "2026-08-10")
    tasks.set_done(task_id)

    assert tasks.set_due(task_id, "2026-08-14") is False

    row = tasks.fetch(task_id)
    assert row is not None
    assert row.due_date == "2026-08-10"  # 一个字节都没被改动


# ---------------------------------------------------------------------------
# W9 / S7:hazard_no 这一列 —— 幂等迁移 + 存取 + 按隐患号取回
# ---------------------------------------------------------------------------


def test_迁移_老库补上隐患号列而旧数据一行不丢() -> None:
    """本文件最要紧的一条:线上 data/gyt.sqlite3 里有真实台账。

    `CREATE TABLE IF NOT EXISTS` **不会**给已存在的旧表加列(W9 §6.6),
    只改 DDL 的话新机器一切正常、线上却是「代码认得这列、库里没有」——
    第一条 SELECT 就 `no such column: hazard_no`,而 `make test` 全绿
    (测试库每次都是新建的,永远走不到那条路)。所以这里先造老库再走正常路径。
    """
    _make_legacy_table()
    assert "hazard_no" not in _task_columns()  # 前提成立:这确实是一张老表

    rows = tasks.list_rows(include_done=True)  # 随便一次正常调用,进场处顺手迁移

    assert "hazard_no" in _task_columns()
    # 旧数据原样还在:标题、期限、状态、时间戳一个字节都没动
    assert [r.title for r in rows] == ["上线前就记着的活", "上线前就销了的活"]
    assert [r.due_date for r in rows] == ["2026-08-10", None]
    assert [r.status for r in rows] == [tasks.STATUS_OPEN, tasks.STATUS_DONE]
    assert [r.created_at for r in rows] == [OLD_STAMP, OLD_STAMP]
    # 补出来的列对旧行是 NULL —— 老任务都不是隐患整改的活,不许被误判成带号任务
    assert [r.hazard_no for r in rows] == [None, None]


def test_迁移_连开两次不重复加列也不报错() -> None:
    """幂等:第二次进场必须空转。真去 ALTER 第二遍的话 sqlite 会抛
    `duplicate column name`,而这一层每次操作都开一次连接 —— 那等于第二条语句起
    整个台账全废。"""
    _make_legacy_table()

    tasks.list_rows()  # 第一次:真的执行 ALTER
    tasks.list_rows()  # 第二次:必须认出列已存在、直接跳过
    tasks.create("迁移之后照样能记", None)  # 写路径也得好使

    assert _task_columns().count("hazard_no") == 1


def test_迁移_新建的库列序与老库升级出来的一致() -> None:
    """`ALTER TABLE ADD COLUMN` 只能往**末尾**追加,所以 DDL 也把 hazard_no 写在末尾 ——
    两种来源的库物理列序才一致。不一致的话,任何一句手写的 `SELECT *` 或不带列名的
    `INSERT` 都只会在其中一种库上出事,而那种 bug 在本机永远复现不出来。"""
    tasks.create("先把表建出来", None)  # 全新库,走 DDL 这条路

    assert _task_columns()[-1] == "hazard_no"


def test_迁移_老库升级后的列序同样把隐患号排在最末() -> None:
    """与上一条配对:两边都断言"最末",这个不变量才算被钉住。"""
    _make_legacy_table()

    tasks.list_rows()

    assert _task_columns()[-1] == "hazard_no"


def test_建取往返带隐患号() -> None:
    """带号的任务由 supervision 在签发通知单时创建(W9 §5.3),这层只管存取保真。"""
    task_id = tasks.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_A)

    row = tasks.fetch(task_id)

    assert row is not None
    assert row.hazard_no == HAZARD_A
    assert row.title == "补临边护栏"


def test_不传隐患号时存的是None不是空串() -> None:
    """空串会让 `if row.hazard_no` 判假、而 SQL 的 `hazard_no IS NOT NULL` 判真 ——
    两处判据当场分家,工具层放行、统计口径却把它算成隐患任务。"""
    task_id = tasks.create("清理西侧通道", None)

    row = tasks.fetch(task_id)

    assert row is not None
    assert row.hazard_no is None


def test_按隐患号一次取回多条隐患的整改任务() -> None:
    """supervision 那边一屏就是一堆隐患,逐条查就是 N+1(CLAUDE.md 红线)。
    排序沿用 list_rows 那一套:有期限的按期限升序。"""
    late = tasks.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_A)
    early = tasks.create("配电箱加锁", "2026-08-09", hazard_no=HAZARD_B)
    tasks.create("普通的活", "2026-08-08")  # 没号,不该被捞进来

    got = tasks.list_by_hazard([HAZARD_A, HAZARD_B])

    assert [r.id for r in got] == [early, late]
    assert [r.hazard_no for r in got] == [HAZARD_B, HAZARD_A]


def test_按隐患号取回_不认得的号与普通任务都不返回() -> None:
    """普通任务的 hazard_no 是 NULL,而 SQL 的 IN 永远匹配不上 NULL ——
    这一条把「隐患查询会不会误伤普通任务」钉死。"""
    tasks.create("普通的活", "2026-08-08")
    tasks.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_A)

    assert tasks.list_by_hazard([HAZARD_B]) == []


def test_按隐患号取回_空入参直接返回空表() -> None:
    """`IN ()` 是语法错误,空入参必须在打库之前就短路掉。"""
    tasks.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_A)

    assert tasks.list_by_hazard([]) == []


def test_按隐患号取回_已销项的也要在里面() -> None:
    """刻意不带 include_done 开关:带号的活被 supervision 关掉之后仍是证据链的一环,
    默认把它藏掉才是真会出事的那种默认值。"""
    task_id = tasks.create("补临边护栏", "2026-08-10", hazard_no=HAZARD_A)
    tasks.set_done(task_id)

    got = tasks.list_by_hazard([HAZARD_A])

    assert [r.id for r in got] == [task_id]
    assert got[0].status == tasks.STATUS_DONE


def test_按隐患号取回_注入串当隐患号表安然无恙() -> None:
    """IN 列表的 `?` 个数由代码算、值一律走占位(方案红线 4)。
    若有人图省事把编号拼进 SQL 文本,这个号会当场把表炸掉。"""
    evil = "GYT-H'); DROP TABLE tasks;--"
    task_id = tasks.create("表还活着的证据", None, hazard_no=evil)

    got = tasks.list_by_hazard([evil])

    assert [r.id for r in got] == [task_id]
    assert got[0].hazard_no == evil  # 原样进出,不许带转义痕迹
    assert tasks.fetch(task_id) is not None
