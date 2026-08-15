"""考勤链的时间权威与凭证编号。**打卡链上的每一个时间都必须从这里出。**

为什么要一个"时间权威"模块而不是各处 ``datetime.now()``:

  · **业务时区显式写死 Asia/Hong_Kong。** 容器 ``TZ=Asia/Shanghai``
    (Dockerfile:253)、本机开发可能是任意时区 —— 两地数值碰巧一致(都是
    UTC+8 无夏令时)**是巧合不是保证**,靠宿主 TZ 意味着换台机器
    ``work_date`` 就可能不同,而 ``make test`` 在同为 UTC+8 的本机全绿也
    发现不了(W7 方案 上线闸⑥)。
  · **单一快照。** ``checked_at``、``work_date``、水印上的时间、凭证编号里的
    日期时刻,四处若各取一次 now,跨秒/跨午夜时就会互相对不上 ——
    凭证上写着 23:59:59 而 ``work_date`` 已经是第二天。
  · **可测性。** 派生逻辑是纯函数(``snapshot_at`` 显式收 instant),
    照搬 agents/schedule/dates.py 的"today 全部显式注入"手法 ——
    偷偷调 ``datetime.now()`` 的实现,时区篡改测试当场翻红。

夜班跨午夜的业务语义**未定义**(当前 = 香港日历日),见 TODOS.md 的 TODO-38。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Final, NamedTuple
from zoneinfo import ZoneInfo

HK: Final = ZoneInfo("Asia/Hong_Kong")
"""业务时区。⚠️ 不许改成 ``astimezone()`` 无参调用 —— 那是"靠宿主猜"。"""

RECEIPT_PREFIX: Final[str] = "GYT-A-"
"""凭证编号前缀。

``A`` = attendance。**已实测与巡检记录的编号守卫不串**:report 的
``REPORT_RECEIPT_PATTERN = r"GYT-\\d{8}-\\d{6}"`` 要求 ``GYT-`` 后面紧跟
8 个数字,而这里紧跟的是 ``A-``,永远匹配不上(test_checkin_api.py 有守门断言)。
反向约束:若哪天考勤也要挂 RequireReceiptSource 一类守卫,**必须写自己的正则**,
拿 report 那条来用会一个编号都认不出、静默全放行。
"""


class TimeSnapshot(NamedTuple):
    """一次打卡用到的全部时间表示 —— 同一瞬间的四种写法,天然一致。"""

    checked_at: str  # ISO 秒级,带 +08:00 偏移 → 入库
    work_date: str  # YYYY-MM-DD(香港日历日)→ 入库、走索引
    display: str  # 「2026-08-15 08:30:00」→ 画进水印、给工友看(偏移是噪音,不带)
    stamp: datetime  # HK 本地化的 aware datetime → 生成凭证编号用


def snapshot_at(instant: datetime) -> TimeSnapshot:
    """把一个 aware 时刻派生成打卡用的四种时间表示。纯函数,时区篡改免疫。

    拒收 naive datetime:收下就等于替调用方猜了一个时区,而"猜"正是这个
    模块要消灭的东西。
    """
    if instant.tzinfo is None:
        raise ValueError("snapshot_at 只收带时区的 datetime —— naive 值意味着有人在靠宿主时区")
    local = instant.astimezone(HK)
    return TimeSnapshot(
        checked_at=local.isoformat(timespec="seconds"),
        work_date=local.date().isoformat(),
        display=local.strftime("%Y-%m-%d %H:%M:%S"),
        stamp=local,
    )


def make_snapshot() -> TimeSnapshot:
    """当前时刻的快照。业务代码只该调这一个;测试直接喂 ``snapshot_at``。"""
    return snapshot_at(datetime.now(UTC))


def new_receipt_no(snap: TimeSnapshot) -> str:
    """生成凭证编号:``GYT-A-YYYYMMDD-HHMMSS-xxxx``(4 位十六进制随机尾)。

    随机尾不是装饰:同一秒内多人打卡是真实场景(收工时全组排队),
    只有日期时刻的编号**必碰**(v1 被推翻的判断 #3)。4 位 = 65536 空间,
    同秒 10 人的碰撞概率约 0.07%,兜底靠库里 ``receipt_no UNIQUE`` +
    调用方撞库重试(重试就是再调一次本函数 —— 每次随机尾都是新的)。
    """
    return f"{RECEIPT_PREFIX}{snap.stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


__all__ = ["HK", "RECEIPT_PREFIX", "TimeSnapshot", "make_snapshot", "new_receipt_no", "snapshot_at"]
