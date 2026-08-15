"""扫码配对的内存状态机(W8)。**只回答一个问题:哪台电脑在等的那次打卡走到哪了。**

对外的 HTTP 契约在 ``checkin_api.py`` 的模块头注(打卡链契约的唯一真相),
这里是它的存储实现。为什么单独一个文件而不是塞进 ``checkin_api.py``:
那个文件已经 890 多行(上限 800),而这件东西有自己的一组不变量
(单向状态机 + 租期 + 容量上限),塞进去只会让两边都难读、难测。
依赖方向与 ``receipt.py`` / ``watermark.py`` / ``messages.py`` 一致:
``checkin_api`` → ``attendance.*``,本模块不反向 import 任何 handler。

===========================================================================
状态机:只能单向前进,``waiting → scanned → done``
===========================================================================
**表里只存 scanned 和 done 两种。``waiting`` 是「不在表里」的同义词。**

这不是省事,是「未知 / 过期 / 等待中三者必须不可区分」那条要求的**结构保证**:
三种情况走的是同一段代码、返回同一个对象(``WAITING_RECORD``),
handler 那边没有「要记得回同一个值」这回事,也就漏不掉。
反过来说,若哪天有人加一个「登记 waiting」的写操作,探测接口就出现了 ——
外人拿 id 试一遍就能数出现场有几台电脑开着面板。

单向前进的判据是 ``_STATE_RANK``,不是 if 链:
  · ``scanned`` 打在已经 ``done`` 的配对上 = **不动状态**(手机重发一次
    「已扫码」,电脑不该从「打卡成功」退回「已扫码」);
  · ``done`` 覆盖一切(含 done→done):同一台电脑重开面板会换新 pair,
    所以撞到同一个 pair 的只可能是同一次打卡的重放、或紧接着的第二次提交 ——
    两种情况下「显示最新那张凭证」都是对的。

===========================================================================
租期与容量:这台机器只有 1.9GB,且刚栽过一次内存
===========================================================================
- **租期 10 分钟**,读写时惰性清理(没有后台任务 —— 冷启动即正确,
  同 ``core/access.py`` 的令牌桶「按经过时间连续补」那条理由)。
- **容量 256 条**:满了先清过期的,还满就淘汰最老的。没有上限的话,
  一个循环刷 ``POST /checkin/pair/scanned`` 就能把内存吃光,
  而这台机器 1.9GB 且刚经历过 checkpoint 泄漏 242MB 那次事故。
- 计时用 ``time.monotonic``,**不经过 ``receipt.py``**。那个模块是
  「要给人看、要落库」的时间权威(``checked_at`` / ``work_date`` / 水印 / 编号),
  租期是纯内部计时:不进任何输出、不进任何库,而且必须免疫系统改时间与 NTP 回拨。
  两者管的不是一回事,这里不算破那条「唯一时间权威」。

**进程重启即清空,这是刻意接受的。** 配对是十分钟内的临时状态,
容器一重建,电脑那边重开面板换个新码就行 —— 为它上持久化,要么多一张表
(打卡台账里混进一堆十分钟就死的行),要么多一个 Redis,两样都不值当。
唯一的可见后果:重启那一刻正扫着码的人,电脑停在「等扫码」,手机照样能打完卡
(配对失败不影响任何人打卡,这是整条配对链的设计前提)。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final, NamedTuple

logger = logging.getLogger(__name__)

PAIR_ID_LEN: Final[int] = 32
"""配对随机串的长度:32 位十六进制 = 128 bit,电脑端用 ``crypto.getRandomValues`` 生成。

**它不是凭据,不授予任何权限** —— 配对端点仍然全部在登录闸和 ``X-Api-Key`` 后面。
它只是「哪台电脑在等哪次打卡」的关联号,所以二维码里带上它不违反 D7
(二维码里没有令牌、没有姓名)。
"""

_HEX_DIGITS: Final[str] = "0123456789abcdef"

STATE_WAITING: Final[str] = "waiting"
STATE_SCANNED: Final[str] = "scanned"
STATE_DONE: Final[str] = "done"

_STATE_RANK: Final[dict[str, int]] = {
    STATE_WAITING: 0,
    STATE_SCANNED: 1,
    STATE_DONE: 2,
}
"""状态的先后次序。**单向前进靠比这张表,不靠 if 链** ——
将来若真要加第四个状态(比如「已取消」),排进这张表就自动继承了不回退的性质。"""

PAIR_TTL_S: Final[float] = 600.0
"""租期 10 分钟(秒)。够走完「扫码 → 拍照 → 预览 → 提交」,又远短于一次演示的长度。

**每次写操作续满租期,读操作不续。** 于是「电脑开着面板等了 11 分钟才有人扫」
这种情况下,条目早就没了 —— 而这毫无影响:``mark_scanned`` 对不存在的 id
直接建条目(见下),电脑下一次轮询照样看到 scanned。
真正怕过期的只有 scanned→done 这一段(拍张照十几秒),10 分钟绰绰有余。
"""

PAIR_CAPACITY: Final[int] = 256
"""同时在册的配对上限。远大于任何真实现场(一个地盤不会有 256 台电脑同时开面板),
又小到就算被塞满也只是几十 KB。

⚠️ 这两个数字**刻意不进 ``config.py``**:它们不是部署要调的旋钮,是协议自带的
常数(10 分钟来自「扫码到提交」这段人的耗时,256 来自内存预算的一次性判断)。
同型先例:``checkin_api.RECEIPT_RETRY_MAX``、``core/access._MIN_TOKEN_LEN``。
真要按部署环境调,再搬进 config —— 那时构造参数已经在这儿备好了。
"""


class PairRecord(NamedTuple):
    """一条配对状态。不可变:状态推进 = 造一条新的替换,绝不原地改。"""

    state: str
    receipt: dict[str, Any] | None  # 只有 done 时非空,形状 = POST /checkin 的凭证对象
    deadline: float  # monotonic 时刻,过了就当不存在


WAITING_RECORD: Final[PairRecord] = PairRecord(STATE_WAITING, None, 0.0)
"""「不在表里」的统一答案 —— 未知 id、过期 id、还没人扫的 id 全都得到**这一个对象**。

``deadline=0.0`` 只是占位:它永远不会被存进表里,也就永远不会参与过期判断。
"""


def normalize_pair_id(value: str | None) -> str | None:
    """把外部传来的配对串收敛成规范形式(32 位小写十六进制);不像样就返回 ``None``。

    ⚠️ **先查长度再 lower()。** 反过来的话,一个 1MB 的 header 值会先被完整
    复制一遍才被判超长 —— 那是拿校验代码本身当放大器(判据同
    ``checkin_api.decode_name`` 那条)。

    大小写宽容 + 归一化到小写,与 ``validate_digest_hex`` 同一手法:前端拼十六进制
    的写法五花八门,为大小写回一个 400 只会制造「本机好好的、换个浏览器就不灵」。
    """
    if value is None:
        return None
    if len(value) > PAIR_ID_LEN * 2:  # 宽松上界:合法值只会正好 32 位
        return None
    text = value.strip().lower()
    if len(text) != PAIR_ID_LEN:
        return None
    if not all(c in _HEX_DIGITS for c in text):
        return None
    return text


class PairStore:
    """配对状态的内存表。线程安全,惰性过期,容量封顶。

    形态照 ``core/access.TokenBucket`` 的先例:模块级单例 + 可注入时钟 +
    ``threading.Lock``。锁的理由一字不差 —— langgraph dev 多数时候是单事件循环,
    但框架里有 ``run_in_threadpool`` 这类线程逃逸路径,而锁很便宜(dict 读改写,
    微秒级),不赌「不会并发」。

    ``clock`` 可注入是为了测试:租期行为**全靠时间推进**,真 ``sleep`` 写出来的
    测试又慢又飘(CI 负载一高就红)。注入假时钟,时间就成了普通输入。

    🔴 **不变量:表的迭代顺序 == 到期顺序。** 全表同一个 TTL,而每次写都
    ``move_to_end``,所以队首永远是最早到期的那条 —— 清理器因此能「从队首弹到
    不过期为止」,而不用全表扫。谁哪天要给单条记录设不同的 TTL,**必须**同时把
    ``_purge`` 改成全表扫,否则表现是「有的条目永远清不掉」,而且一点报错都没有。
    """

    def __init__(
        self,
        *,
        ttl_s: float = PAIR_TTL_S,
        capacity: int = PAIR_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self.capacity = int(capacity)
        self._clock = clock
        self._entries: OrderedDict[str, PairRecord] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """在册条数(**含尚未被清理的过期条目**)—— 给测试断言「内存不涨」用。

        故意不在这里顺手清理:测的就是「上限有没有真的封住」,
        而清理由读写路径自己触发,量化时不该被观测行为改写。
        """
        with self._lock:
            return len(self._entries)

    def get(self, pair_id: str) -> PairRecord:
        """查一条。未知 / 过期 / 还没人扫 → 一律 ``WAITING_RECORD``。

        ⚠️ **读操作绝不建条目、绝不续租。** 建条目的话,拿随机 id 轮询就能把表
        撑到上限、把真在用的配对挤掉;续租的话,一个开着不管的面板能把条目
        永远焐着。读只是读。
        """
        now = self._clock()
        with self._lock:
            self._purge(now)
            record = self._entries.get(pair_id)
            if record is None or record.deadline <= now:
                return WAITING_RECORD
            return record

    def mark_scanned(self, pair_id: str) -> str:
        """手机落地上报「已扫码」。返回**结果状态**(不一定是 scanned)。

        不存在就建一条 —— 电脑那边从不向后端登记自己的 pair,后端第一次听说
        这个号就是这一刻。已经 done 的**只续租不改状态**:手机重发一次,
        电脑不该从「打卡成功」退回「已扫码」。
        """
        now = self._clock()
        with self._lock:
            current = self._live(pair_id, now)
            if current is not None and _STATE_RANK[current.state] >= _STATE_RANK[STATE_SCANNED]:
                record = current._replace(deadline=now + self.ttl_s)
            else:
                record = PairRecord(STATE_SCANNED, None, now + self.ttl_s)
            self._put(pair_id, record, now)
            return record.state

    def mark_done(self, pair_id: str, receipt: dict[str, Any]) -> str:
        """打卡落账,挂上凭证。``done`` 是终点,覆盖此前任何状态。

        ``dict(receipt)`` 是一次真的防御性拷贝(凭证对象全是标量),
        免得调用方之后改了自己那份、表里这份跟着变 —— 不可变红线的落地。
        """
        now = self._clock()
        with self._lock:
            self._put(pair_id, PairRecord(STATE_DONE, dict(receipt), now + self.ttl_s), now)
            return STATE_DONE

    # 刻意**没有** clear()/reset() 实例方法:测试隔离走下面的 ``reset_store()``
    # (直接丢掉整个单例,拿到的是货真价实的新表)。多一个「清空但保留对象」的
    # 入口只会让人纠结该用哪个,而这里没有任何调用方需要保留对象身份。

    # -- 以下均要求调用方已持锁 --------------------------------------------

    def _live(self, pair_id: str, now: float) -> PairRecord | None:
        """取一条**还没过期**的记录;过期或不存在都返回 None。"""
        record = self._entries.get(pair_id)
        if record is None or record.deadline <= now:
            return None
        return record

    def _purge(self, now: float) -> None:
        """从队首弹掉所有到期的条目(依赖「迭代顺序 == 到期顺序」那条不变量)。"""
        while self._entries:
            pair_id, record = next(iter(self._entries.items()))
            if record.deadline > now:
                return
            del self._entries[pair_id]

    def _put(self, pair_id: str, record: PairRecord, now: float) -> None:
        """写入并维持两条不变量:容量不超、队尾是最新写的那条。"""
        self._purge(now)
        while pair_id not in self._entries and len(self._entries) >= self.capacity:
            # 走到这里说明清完过期的还是满 —— 淘汰最老的那条**活**记录。
            # 那台电脑的面板会一直停在「等扫码」(它拿到的是 WAITING_RECORD),
            # 所以要留一条 warning:正常现场到不了 256,到了就是有人在刷。
            evicted, _ = self._entries.popitem(last=False)
            logger.warning(
                "配对表已满(上限 %d),淘汰最老的一条:%s…(该电脑的面板会退回「等扫码」)",
                self.capacity,
                evicted[:8],
            )
        self._entries[pair_id] = record
        self._entries.move_to_end(pair_id)


_store: PairStore | None = None


def get_store() -> PairStore:
    """进程内唯一的配对表。照 ``checkin_api.get_global_limiter`` 的先例做成单例 ——
    内存态,单进程假设,代码库已经这么跑着(``_global_limiter`` / ``_worker_limiter``)。"""
    global _store
    if _store is None:
        _store = PairStore()
    return _store


def reset_store() -> None:
    """丢掉整张表(下次取用时重建)。测试用。"""
    global _store
    _store = None


__all__ = [
    "PAIR_CAPACITY",
    "PAIR_ID_LEN",
    "PAIR_TTL_S",
    "STATE_DONE",
    "STATE_SCANNED",
    "STATE_WAITING",
    "WAITING_RECORD",
    "PairRecord",
    "PairStore",
    "get_store",
    "normalize_pair_id",
    "reset_store",
]
