"""扫码配对内存状态机(``attendance/pairing.py``)的单测 —— 不起服务、不碰库、不联网。

HTTP 那一层(鉴权、限流、指纹不受影响、幂等重放也置 done)在
``test_checkin_api.py`` 里,那边有现成的 TestClient 夹具。这里锁的是存储层
**四类会静默出错**的东西:

  · **单向前进** —— done 被 scanned 覆盖的话,手机重发一次「已扫码」,
    电脑就从「✅ 已打卡成功」退回「请在手机上拍照」,而工友那边其实早打完了。
  · **未知 ≡ 过期 ≡ 等待中** —— 三者只要能被区分开,这就成了探测接口:
    拿 id 试一遍就能数出现场开着几台面板。
  · **容量与租期** —— 没有上限 = 循环刷接口就能吃光内存,而这台机器只有 1.9GB
    且刚经历过一次内存事故(checkpoint 泄漏 242MB)。
  · **「迭代顺序 == 到期顺序」这条不变量** —— 清理器靠它只弹队首,不全表扫。
    哪天有人给单条记录设了不同的 TTL,表现是「有的条目永远清不掉」,零报错。

租期行为全靠时间推进,所以**一律注入假时钟**,一秒都不真等
(判据同 ``core/access.TokenBucket``:真 sleep 写出来的测试又慢又飘)。
"""

from __future__ import annotations

import threading

import pytest

from gyt.attendance import pairing
from gyt.attendance.pairing import (
    PAIR_CAPACITY,
    PAIR_ID_LEN,
    PAIR_TTL_S,
    STATE_DONE,
    STATE_SCANNED,
    STATE_WAITING,
    PairStore,
    normalize_pair_id,
)

PAIR_A = "a" * PAIR_ID_LEN
PAIR_B = "b" * PAIR_ID_LEN

RECEIPT = {
    "receipt_no": "GYT-A-20260815-083000-cafe",
    "worker_name": "张三",
    "site_name": None,
    "checked_at": "2026-08-15T08:30:00+08:00",
    "work_date": "2026-08-15",
    "geo_status": "absent",
    "source": "camera",
    "artifact_id": "0" * 32,
    "photo_purged_at": None,
}
"""一份形状正确的凭证对象(九键,与 ``checkin_api._receipt_payload`` 同源)。
存储层不校验形状 —— 它只负责原样带回来,所以这里只要求它能原样往返。"""


class 假时钟:
    """可手动推进的单调时钟。``PairStore`` 只从它取「现在几点」。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def 前进(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def 时钟() -> 假时钟:
    return 假时钟()


@pytest.fixture
def store(时钟: 假时钟) -> PairStore:
    """短租期、小容量的表 —— 边界行为用小数字测,断言才读得懂。"""
    return PairStore(ttl_s=60.0, capacity=4, clock=时钟)


class Test配对串校验:
    def test_合法串原样通过(self) -> None:
        assert normalize_pair_id(PAIR_A) == PAIR_A

    def test_大小写与空白被归一(self) -> None:
        """判据同 validate_digest_hex:前端拼十六进制的写法五花八门,
        为大小写回一个 400 只会制造「本机好好的、换个浏览器就不灵」。"""
        assert normalize_pair_id("  " + "AB" * 16 + "  ") == "ab" * 16

    @pytest.mark.parametrize(
        ("说明", "坏值"),
        [
            ("None(没带这个头)", None),
            ("空串", ""),
            ("短一位", "a" * (PAIR_ID_LEN - 1)),
            ("长一位", "a" * (PAIR_ID_LEN + 1)),
            ("非十六进制", "g" * PAIR_ID_LEN),
            ("带连字符的 uuid", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            ("超长垃圾", "a" * 100_000),
        ],
    )
    def test_不像样的一律_None(self, 说明: str, 坏值: str | None) -> None:
        assert normalize_pair_id(坏值) is None, 说明

    def test_超长输入先查长度再处理(self) -> None:
        """1MB 的 header 值必须在 lower() 之前被拒 ——
        反过来就是拿校验代码本身当放大器(decode_name 同款判据)。
        这条测的是行为的可观测面:巨大输入不抛异常、不卡住,直接 None。"""
        assert normalize_pair_id("A" * 2_000_000) is None


class Test状态机:
    def test_没人扫过就是_waiting(self, store: PairStore) -> None:
        """未知 id 的答案 —— 与「过期」「还没扫」是同一个,不可区分。"""
        record = store.get(PAIR_A)
        assert record.state == STATE_WAITING
        assert record.receipt is None

    def test_三态依次前进(self, store: PairStore) -> None:
        assert store.get(PAIR_A).state == STATE_WAITING
        assert store.mark_scanned(PAIR_A) == STATE_SCANNED
        assert store.get(PAIR_A).state == STATE_SCANNED
        assert store.get(PAIR_A).receipt is None  # 只有 done 才挂凭证
        assert store.mark_done(PAIR_A, RECEIPT) == STATE_DONE
        assert store.get(PAIR_A).state == STATE_DONE

    def test_done_不被_scanned_顶回去(self, store: PairStore) -> None:
        """契约第一条硬要求。手机断网重发一次「已扫码」,电脑不该从
        「✅ 已打卡成功」退回「请在手机上拍照」—— 工友那边早打完了。"""
        store.mark_done(PAIR_A, RECEIPT)
        assert store.mark_scanned(PAIR_A) == STATE_DONE
        record = store.get(PAIR_A)
        assert record.state == STATE_DONE
        assert record.receipt == RECEIPT  # 凭证也不许被抹掉

    def test_scanned_重复上报是幂等的(self, store: PairStore) -> None:
        for _ in range(5):
            assert store.mark_scanned(PAIR_A) == STATE_SCANNED
        assert len(store) == 1  # 重复上报不该变成五条

    def test_done_可以被新的_done_覆盖(self, store: PairStore) -> None:
        """同一台电脑重开面板会换新 pair,所以撞到同一个 pair 的只可能是
        同一次打卡的重放、或紧接着的第二次提交 —— 两种情况显示最新那张都对。"""
        store.mark_done(PAIR_A, RECEIPT)
        新凭证 = {**RECEIPT, "receipt_no": "GYT-A-20260815-084000-beef"}
        assert store.mark_done(PAIR_A, 新凭证) == STATE_DONE
        assert store.get(PAIR_A).receipt == 新凭证

    def test_可以直接跳到_done(self, store: PairStore) -> None:
        """没人上报过 scanned 也能落 done:手机上报那一步是「失败不重试」的,
        丢了照样要能打卡 —— 配对断了不影响任何人打卡。"""
        assert store.mark_done(PAIR_A, RECEIPT) == STATE_DONE
        assert store.get(PAIR_A).state == STATE_DONE

    def test_两个配对互不干扰(self, store: PairStore) -> None:
        store.mark_scanned(PAIR_A)
        store.mark_done(PAIR_B, RECEIPT)
        assert store.get(PAIR_A).state == STATE_SCANNED
        assert store.get(PAIR_B).state == STATE_DONE

    def test_凭证是防御性拷贝(self, store: PairStore) -> None:
        """调用方之后改了自己那份,表里这份不许跟着变(不可变红线的落地)。"""
        原件 = dict(RECEIPT)
        store.mark_done(PAIR_A, 原件)
        原件["receipt_no"] = "被改过了"
        assert store.get(PAIR_A).receipt["receipt_no"] == RECEIPT["receipt_no"]


class Test租期:
    def test_过期后回到_waiting(self, store: PairStore, 时钟: 假时钟) -> None:
        store.mark_scanned(PAIR_A)
        时钟.前进(59.9)
        assert store.get(PAIR_A).state == STATE_SCANNED
        时钟.前进(0.1)  # 正好到期 —— deadline <= now 即算过期
        assert store.get(PAIR_A).state == STATE_WAITING

    def test_过期的_done_也回_waiting(self, store: PairStore, 时钟: 假时钟) -> None:
        """凭证不许在内存里赖着不走。前端拿到 done 就停轮询,所以这一跳看不见;
        真看得见的场景只有「面板开着放了十分钟没管」,那本来也该重开。"""
        store.mark_done(PAIR_A, RECEIPT)
        时钟.前进(60.0)
        record = store.get(PAIR_A)
        assert record.state == STATE_WAITING
        assert record.receipt is None

    def test_过期条目会被真的清掉而不只是查不到(self, store: PairStore, 时钟: 假时钟) -> None:
        """惰性清理必须真的释放内存 —— 只在读的时候「假装看不见」的话,
        表会一直涨到容量上限,把还在用的配对挤掉。"""
        store.mark_scanned(PAIR_A)
        时钟.前进(60.0)
        store.get(PAIR_A)  # 一次读就该把它清掉
        assert len(store) == 0

    def test_写操作续满租期(self, store: PairStore, 时钟: 假时钟) -> None:
        """scanned→done 那一段(拍张照十几秒)不该被前面的等待时间吃掉。"""
        store.mark_scanned(PAIR_A)
        时钟.前进(50.0)
        store.mark_scanned(PAIR_A)  # 又收到一次上报
        时钟.前进(50.0)  # 距首次已 100 秒 > 60 秒租期
        assert store.get(PAIR_A).state == STATE_SCANNED

    def test_读操作不续租(self, store: PairStore, 时钟: 假时钟) -> None:
        """一个开着不管的面板每 2 秒读一次,若读也续租,条目就永远焐着 ——
        256 个这样的面板能把表钉死,真在用的配对全被挤掉。"""
        store.mark_scanned(PAIR_A)
        for _ in range(6):
            时钟.前进(10.0)
            store.get(PAIR_A)
        assert store.get(PAIR_A).state == STATE_WAITING

    def test_过期后再上报等于重新开始(self, store: PairStore, 时钟: 假时钟) -> None:
        """「电脑开着面板等了很久才有人扫」这条真实路径:条目早没了,
        而 mark_scanned 对不存在的 id 直接建 —— 电脑下一次轮询照样看到 scanned。"""
        store.mark_scanned(PAIR_A)
        时钟.前进(600.0)
        assert store.get(PAIR_A).state == STATE_WAITING
        assert store.mark_scanned(PAIR_A) == STATE_SCANNED
        assert store.get(PAIR_A).state == STATE_SCANNED

    def test_默认租期是十分钟(self) -> None:
        """契约数字的留档:够走完「扫码 → 拍照 → 预览 → 提交」。"""
        assert PAIR_TTL_S == 600.0


class Test容量上限:
    def _塞满(self, store: PairStore, n: int, 首字: str = "0") -> list[str]:
        """写入 n 条配对,返回写入顺序。``首字`` 用来造两批互不相同的 id。"""
        ids = [f"{首字}{i:0{PAIR_ID_LEN - 1}x}" for i in range(n)]
        for pair_id in ids:
            store.mark_scanned(pair_id)
        return ids

    def test_塞满之后仍然能用且内存不涨(self, store: PairStore) -> None:
        """没有上限 = 一个循环刷 /checkin/pair/scanned 就能把内存吃光,
        而这台机器只有 1.9GB 且刚经历过一次内存事故。
        1000 次写入之后,表里必须还是 capacity 条 —— 一条都不许多。"""
        self._塞满(store, 1000)
        assert len(store) == store.capacity
        # 还得是**能用**的:最后写进去的那条查得到。
        assert store.get(f"{999:0{PAIR_ID_LEN}x}").state == STATE_SCANNED

    def test_淘汰的是最老的那条(self, store: PairStore) -> None:
        ids = self._塞满(store, store.capacity)  # 表正好满
        store.mark_scanned(PAIR_A)  # 再来一条 → 挤掉队首
        assert store.get(ids[0]).state == STATE_WAITING  # 最老的走了
        for 还在 in ids[1:]:
            assert store.get(还在).state == STATE_SCANNED
        assert store.get(PAIR_A).state == STATE_SCANNED

    def test_更新已有条目不占新名额(self, store: PairStore) -> None:
        """满表时对**已在册**的配对做 scanned→done,不该把别人挤掉 ——
        否则正在打卡的人会互相踢。"""
        ids = self._塞满(store, store.capacity)
        store.mark_done(ids[0], RECEIPT)
        assert len(store) == store.capacity
        for 还在 in ids:
            assert store.get(还在).state != STATE_WAITING

    def test_写操作会把自己挪到队尾(self, store: PairStore) -> None:
        """🔴 「迭代顺序 == 到期顺序」那条不变量的行为面:
        续过租的条目必须排到队尾,否则清理器从队首弹的时候会弹掉活的。"""
        ids = self._塞满(store, store.capacity)
        store.mark_scanned(ids[0])  # 队首续租 → 挪到队尾
        store.mark_scanned(PAIR_A)  # 挤掉当前队首(应该是 ids[1] 而不是 ids[0])
        assert store.get(ids[0]).state == STATE_SCANNED
        assert store.get(ids[1]).state == STATE_WAITING

    def test_满表时先清过期再淘汰活的(
        self, store: PairStore, 时钟: 假时钟, caplog: pytest.LogCaptureFixture
    ) -> None:
        """契约原话:「满了先清过期的;还满就淘汰最老的」。
        顺序反了的表现是:过期的死条目占着名额,活的反被踢出去 —— 而表面看
        条数完全正常,查不出哪里不对。所以这里同时断言**一条淘汰告警都没有**:
        过期的腾出来的名额够用,轮不到淘汰活条目。"""
        旧的 = self._塞满(store, store.capacity, 首字="0")
        时钟.前进(61.0)  # 全过期
        with caplog.at_level("WARNING", logger="gyt.attendance.pairing"):
            新的 = self._塞满(store, store.capacity, 首字="f")
        assert len(store) == store.capacity
        assert all(store.get(p).state == STATE_SCANNED for p in 新的)
        assert all(store.get(p).state == STATE_WAITING for p in 旧的)
        assert "配对表已满" not in caplog.text

    def test_默认容量是_256(self) -> None:
        """契约数字的留档:远大于任何真实现场,又小到塞满也只有几十 KB。"""
        assert PAIR_CAPACITY == 256


class Test模块级单例:
    def test_取两次是同一张表(self) -> None:
        assert pairing.get_store() is pairing.get_store()

    def test_reset_store_之后是新的空表(self) -> None:
        """测试隔离的开关:表是模块级单例,上一条用例留下的状态会漏给下一条。"""
        pairing.get_store().mark_scanned(PAIR_A)
        assert pairing.get_store().get(PAIR_A).state == STATE_SCANNED
        pairing.reset_store()
        assert pairing.get_store().get(PAIR_A).state == STATE_WAITING

    def test_单例用的是真实的默认参数(self) -> None:
        pairing.reset_store()
        store = pairing.get_store()
        assert (store.ttl_s, store.capacity) == (PAIR_TTL_S, PAIR_CAPACITY)


class Test并发:
    def test_多线程同时写不丢条目也不炸(self) -> None:
        """langgraph 多数时候是单事件循环,但框架里有 run_in_threadpool 这类
        线程逃逸路径(TokenBucket 头注的同一条理由)。锁很便宜,不赌「不会并发」。
        这条测的是最起码的:并发写不抛异常、不把表写坏。"""
        store = PairStore(ttl_s=60.0, capacity=64)
        ids = [f"{i:0{PAIR_ID_LEN}x}" for i in range(32)]

        def 猛写(pair_id: str) -> None:
            for _ in range(50):
                store.mark_scanned(pair_id)
                store.get(pair_id)

        线程 = [threading.Thread(target=猛写, args=(i,)) for i in ids]
        for t in 线程:
            t.start()
        for t in 线程:
            t.join()
        assert len(store) == len(ids)
        assert all(store.get(i).state == STATE_SCANNED for i in ids)
