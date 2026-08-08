"""db/tasks.py(任务台账 SQLite 存储层)的单元测试 —— 不联网,库落在用例独占的 tmp_path。

环境隔离整套复用 conftest 的 ``_isolated_settings``(autouse):它把 GYT_DATA_DIR
指到 tmp_path/data,并在用例前后各做一次 ``get_settings.cache_clear()``,
所以本文件不需要任何自建 fixture,每个用例天然拿到一张全新的库。

这层要钉死的静默错误:
  · 排序写错(无期限混进有期限中间、或被吞掉)——「下周三之前还有啥」会悄悄漏活,
    评测分数与真机演示都看不出病灶在存储层;
  · SQL 拼值(而非参数化)—— 标题里一个单引号就够炸表,注入用例直接验"表还活着";
  · CHECK 约束没真建上 —— 脏状态静默入库,要到查询端才爆雷,离病灶隔了一层。
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


def _raw_connect() -> sqlite3.Connection:
    """绕过存储层直连库文件:做旧数据、硬塞脏状态,都需要"上帝视角"。"""
    return sqlite3.connect(get_settings().sqlite_path)


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
    而不是悄悄写进仓库的 backend/data(prefilter.py 踩过同类相对路径坑)。"""
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
