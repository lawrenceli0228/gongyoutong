"""慢调用观测的单元测试 —— core/timing.py 与 core/errors.py 里那两处计时。

被测物的由来(完整推演在 ``core/timing.py`` 的模块头注):2026-08-20 线上一次
「拍照 → 出巡检记录」量到 213 秒,日志里只有 httpx 那几行「HTTP Request: POST
... 200 OK」,只能靠**相邻两行的时间差**倒推 —— 而倒推不出的恰恰是最要紧的那件事:
那 198 秒是**在模型调用里面**,还是**在发出请求之前**。三处计时(路径甲的回调、
路径乙的 ainvoke、所有工具的 tool_guard)合起来才把那段黑箱切开。

所以本文件盯的不是"有没有日志",而是三条判据:

1. **耗时那个数对不对** —— 它本身就是判据,写错了整件事就白做。
   用假时钟把数钉死,真时钟只能断言"大于 0",断不出日志里写的是不是那个数。
2. **慢/不慢分级对不对** —— 超阈值必须抬到 warning,不然线上满屏日志里捞不出来。
3. 🔴 **观测件坏掉时业务一点不受影响** —— 这条最要紧。为了看清楚慢调用而把
   用户的提问弄失败,是这件东西能犯的最蠢的错。

铁律:秒级、全 mock、绝不联网,也不真等。模型一律用假替身;缓存读写在需要时
打桩掉,不碰磁盘。**本文件不许在顶部 import gyt.graph**(那会真建图、真要 API Key)。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import LLMResult

from gyt.config import get_settings
from gyt.core import errors as errors_mod
from gyt.core import llm as llm_mod
from gyt.core import timing as timing_mod
from gyt.core.errors import ErrorCode, ok, tool_guard
from gyt.core.timing import _MAX_TRACKED, LlmCallTiming, _fmt_tokens

# --- 常量:测试里同样不许散落魔法值 --------------------------------------------

TIMING_LOGGER = "gyt.core.timing"
ERRORS_LOGGER = "gyt.core.errors"
LLM_LOGGER = "gyt.core.llm"

# 阈值取两个极端,用来把"慢/不慢"这条判据钉死,而不用去动时钟:
#   0 秒 → 任何耗时都 >= 阈值,必抬 warning
#   一天 → 任何耗时都不到,必留在 info
永远算慢 = 0.0
永远不算慢 = 86_400.0

# 日志里耗时那一段的长相。两处计时共用同一种写法,格式漂了这条正则先红。
耗时正则 = re.compile(r"耗时=(\d+\.\d)s")

QUESTION = [HumanMessage(content="工地上安全帽要怎么检查?")]


# --- 测试替身 ------------------------------------------------------------------


class 假时钟:
    """冒充 ``time`` 模块的假时钟,按脚本回放 ``monotonic()``。

    为什么非要它:耗时本身就是被测物 —— 用真时钟只能断言"大于 0",
    断不出"日志里写的到底是不是那个数",也断不出"45 秒该抬 warning、
    19 秒不该"这条边界。

    ⚠️ **用法是替换被测模块里的 ``time`` 这个名字**(timing.py / errors.py /
       llm.py 写的都是 ``import time`` + ``time.monotonic()``,没有可注入的口子,
       与 ``attendance/pairing.py`` 那种构造函数注入时钟的写法不同):

           monkeypatch.setattr(llm_mod, "time", 假时钟([200.0, 263.4]))

       🔴 **绝不能改 stdlib 的 ``time.monotonic``**(一开始就是这么写的,当场翻车):
          asyncio 的事件循环自己也在问 ``time.monotonic()``,``asyncio.run``
          一起步就把脚本里的刻度吃掉了,被测代码拿到的是真时钟 —— 断言出来
          308528.8 而不是 63.4。换模块里的名字则只影响被测模块,谁都碰不到。

    脚本用完后退回真时钟,免得被测代码多问几次就没数可给。
    """

    def __init__(self, 刻度: Sequence[float]) -> None:
        self._刻度 = list(刻度)
        self._真时钟 = time.monotonic

    def monotonic(self) -> float:
        if self._刻度:
            return self._刻度.pop(0)
        return self._真时钟()


class 会抛的日志器:
    """每个方法都抛异常的假 logger,用来模拟"观测件自己坏了"。

    ⚠️ 连 ``debug`` 也抛 —— 兜底那句 debug 本身也在被测范围里。
       ``_log_tool_elapsed`` 的兜底如果没有再套一层 suppress,这个替身当场把它抓出来。
    """

    def __init__(self) -> None:
        self.被调用次数 = 0

    def __getattr__(self, 方法名: str) -> Any:
        def _炸(*_args: Any, **_kwargs: Any) -> None:
            self.被调用次数 += 1
            raise RuntimeError(f"日志器坏了({方法名})")

        return _炸


class 假模型:
    """按脚本回放的假 chat model,绝不联网(形状同 test_llm.py 的 _FakeModel)。"""

    model_name = "fake-model"

    def __init__(self, 脚本: Sequence[Any]) -> None:
        self._脚本 = tuple(脚本)
        self.调用次数 = 0

    async def ainvoke(self, _messages: Any, **_kwargs: Any) -> Any:
        self.调用次数 += 1
        项 = self._脚本[min(self.调用次数 - 1, len(self._脚本) - 1)]
        if isinstance(项, BaseException):
            raise 项
        return 项


# --- 工具函数 --------------------------------------------------------------------


def _起一次(处理器: LlmCallTiming, *, model: str = "deepseek-v4-flash") -> UUID:
    """模拟 langchain 发出的 on_chat_model_start,返回这次调用的 run_id。"""
    run_id = uuid4()
    处理器.on_chat_model_start({}, [], run_id=run_id, invocation_params={"model": model})
    return run_id


def _用量(prompt: int, completion: int) -> LLMResult:
    """造一个带 token 用量的 LLMResult(真调模型时 langchain 给的形状)。"""
    return LLMResult(
        generations=[],
        llm_output={"token_usage": {"prompt_tokens": prompt, "completion_tokens": completion}},
    )


def _无用量() -> LLMResult:
    """造一个没有用量的 LLMResult —— 命中缓存时 langchain 给的就是这个形状。"""
    return LLMResult(generations=[], llm_output=None)


def _取耗时(caplog: pytest.LogCaptureFixture) -> float:
    """从日志正文里把耗时那个数抠出来。抠不到就让用例红在这儿,别静默放过。"""
    命中 = 耗时正则.search(caplog.text)
    assert 命中 is not None, f"日志里没有「耗时=X.Xs」这一段:{caplog.text!r}"
    return float(命中.group(1))


def _级别集合(caplog: pytest.LogCaptureFixture, logger_name: str) -> set[int]:
    """这个 logger 一共出过哪些级别的日志。"""
    return {r.levelno for r in caplog.records if r.name == logger_name}


def _设阈值(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """改环境变量并让配置重新读取。get_settings 挂了 lru_cache,不清就还是旧值。"""
    for 名, 值 in env.items():
        monkeypatch.setenv(名, 值)
    get_settings.cache_clear()


# ==============================================================================
# A 组:LlmCallTiming —— 路径甲(Agent / Supervisor)的模型调用回调
# ==============================================================================


def test_模型调用记出耗时的数就是start到end的差(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """耗时这个数本身就是判据,所以用假时钟把它钉死到小数点后一位。"""
    # Arrange:start 拿 100.0,end 拿 142.5 —— 期望日志里写的是 42.5 秒
    monkeypatch.setattr(timing_mod, "time", 假时钟([100.0, 142.5]))
    处理器 = LlmCallTiming(永远不算慢)

    # Act
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        run_id = _起一次(处理器)
        处理器.on_llm_end(_用量(120, 45), run_id=run_id)

    # Assert
    assert _取耗时(caplog) == pytest.approx(42.5)
    assert "deepseek-v4-flash" in caplog.text  # 模型名要能看出来,不然多模型时分不清
    assert "输入 120 / 输出 45 tok" in caplog.text


def test_超过阈值的模型调用抬到warning(caplog: pytest.LogCaptureFixture) -> None:
    """线上日志量很大,慢调用不抬级别就等于没记 —— 捞不出来。"""
    # Arrange
    处理器 = LlmCallTiming(永远算慢)

    # Act
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        处理器.on_llm_end(_用量(1, 1), run_id=_起一次(处理器))

    # Assert
    assert _级别集合(caplog, TIMING_LOGGER) == {logging.WARNING}
    # 抬级别的理由要写在日志里,不然看的人不知道"凭什么算慢"
    assert "阈值" in caplog.text


def test_没超过阈值的模型调用只记info(caplog: pytest.LogCaptureFixture) -> None:
    """正常调用刷成 warning 的话,warning 就不再是信号了。"""
    # Arrange
    处理器 = LlmCallTiming(永远不算慢)

    # Act
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        处理器.on_llm_end(_用量(1, 1), run_id=_起一次(处理器))

    # Assert
    assert _级别集合(caplog, TIMING_LOGGER) == {logging.INFO}
    assert "阈值" not in caplog.text


def test_模型调用失败也记耗时并且把run_id从字典里清掉(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """两件事一起验:

    1. 「慢且最终失败」那一类不记就永远看不见 —— 恰恰是最该看见的一类;
    2. on_llm_error 不 pop 的话字典只进不出,进程跑久了就泄漏。
    """
    # Arrange
    monkeypatch.setattr(timing_mod, "time", 假时钟([10.0, 22.0]))
    处理器 = LlmCallTiming(永远不算慢)

    # Act
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        run_id = _起一次(处理器)
        assert 处理器._started, "前提没成立:起点根本没记上,后面的断言就没意义了"
        处理器.on_llm_error(TimeoutError("上游超时"), run_id=run_id)

    # Assert:耗时记上了,而且失败一律 warning(不看阈值 —— 失败本身就值得看见)
    assert _取耗时(caplog) == pytest.approx(12.0)
    assert _级别集合(caplog, TIMING_LOGGER) == {logging.WARNING}
    assert "TimeoutError" in caplog.text
    # 🔴 这条是防泄漏的正主:失败路径必须把 run_id 清干净
    assert 处理器._started == {}


def test_收到没见过的run_id时安静返回不抛(caplog: pytest.LogCaptureFixture) -> None:
    """callback 不保证成对:进程被打断、上游只回了 end、或者被 _MAX_TRACKED 挤掉。

    这时候必须安静走开 —— 观测件为了记账把业务打断,是本末倒置。
    """
    # Arrange
    处理器 = LlmCallTiming(永远不算慢)

    # Act / Assert:两个终点回调都不许抛
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        处理器.on_llm_end(_用量(1, 1), run_id=uuid4())
        处理器.on_llm_error(RuntimeError("没头没尾"), run_id=uuid4())

    # 没见过的 run_id 一条日志都不该产生:编不出耗时就别编
    assert [r for r in caplog.records if r.name == TIMING_LOGGER] == []
    assert 处理器._started == {}


def test_在飞调用数有上限字典不会无限涨() -> None:
    """_MAX_TRACKED 那道防泄漏闸。

    正常情况下 start / end 成对,字典是空的。但只要有一种路径漏了终点回调,
    字典就只进不出 —— 长跑的服务进程上这就是一条慢性内存泄漏。
    这里只塞起点、一个终点都不给,就是在模拟那种最坏情况。
    """
    # Arrange
    处理器 = LlmCallTiming(永远不算慢)
    超量 = _MAX_TRACKED + 50

    # Act
    for _ in range(超量):
        _起一次(处理器)

    # Assert:涨到上限就不再涨了(丢最早的那条)
    assert len(处理器._started) <= _MAX_TRACKED
    assert len(处理器._started) < 超量


def test_老式llm入口也记账(caplog: pytest.LogCaptureFixture) -> None:
    """chat model 走 on_chat_model_start,老式 LLM 走 on_llm_start,两个都得实现。

    只实现前者的话,哪天有人加一个非 chat 的模型就**静默不记了** —— 而"没有日志"
    看起来和"这条路径没被走到"一模一样,极难发现。
    """
    # Arrange
    处理器 = LlmCallTiming(永远不算慢)
    run_id = uuid4()

    # Act
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        处理器.on_llm_start(
            {}, ["随便一句提问"], run_id=run_id, invocation_params={"model": "老模型"}
        )
        处理器.on_llm_end(_用量(3, 4), run_id=run_id)

    # Assert
    assert "老模型" in caplog.text
    assert 耗时正则.search(caplog.text) is not None


def test_起点记不上时安静吞掉不打断模型调用() -> None:
    """🔴 观测件绝不许把主路径打断 —— 这条对回调那一侧同样成立。

    langchain 递进来的 run_id 理论上是 UUID,但**观测件不该替上游做这个假设**:
    这里塞一个不可哈希的 list 进去,``self._started[run_id] = ...`` 当场 TypeError。
    它必须被吞掉 —— 一次模型调用不该因为"记不上耗时"而失败。
    """
    # Arrange
    处理器 = LlmCallTiming(永远不算慢)

    # Act / Assert:不许抛
    处理器.on_chat_model_start({}, [], run_id=[], invocation_params={})  # type: ignore[arg-type]
    assert 处理器._started == {}


@pytest.mark.parametrize("回调名", ["on_llm_end", "on_llm_error"])
def test_终点记不上时也安静吞掉(monkeypatch: pytest.MonkeyPatch, 回调名: str) -> None:
    """同上,验两个终点回调的兜底。

    做法是让**时钟**抛(而不是让 logger 抛):时钟坏了会落进 except,兜底那句
    debug 用的还是真 logger,能正常记 —— 这样测到的是"兜住了"这件事本身。
    (反过来把 logger 整个换成会抛的替身,``on_llm_end`` 的兜底里那句 debug
    并没有再套一层 suppress,那测的就是另一回事了,不在这次改动范围内。)
    """

    # Arrange:先用真时钟正常记上起点
    class 会抛的时钟:
        @staticmethod
        def monotonic() -> float:
            raise RuntimeError("时钟坏了")

    处理器 = LlmCallTiming(永远不算慢)
    run_id = _起一次(处理器)
    assert 处理器._started, "前提没成立:起点没记上,后面就测不到终点的兜底了"
    monkeypatch.setattr(timing_mod, "time", 会抛的时钟)

    # Act / Assert:不许抛
    if 回调名 == "on_llm_end":
        处理器.on_llm_end(_用量(1, 1), run_id=run_id)
    else:
        处理器.on_llm_error(RuntimeError("上游炸了"), run_id=run_id)

    # 就算记账失败,run_id 也已经被 pop 掉了 —— 否则这条就成了泄漏
    assert 处理器._started == {}


# ==============================================================================
# B 组:_fmt_tokens —— 别把推断写成事实
# ==============================================================================


def test_有用量时报出输入输出的token数() -> None:
    """真调了模型就有用量,数字要原样报出来(对账花了多少钱靠它)。"""
    assert _fmt_tokens(_用量(120, 45)) == "输入 120 / 输出 45 tok"


def test_没有用量时报无用量而不是一口咬定命中缓存() -> None:
    """🔴 这条钉的是「别把推断写成事实」。

    命中缓存时 langchain 照样触发回调,但 llm_output 里没有用量 —— 所以"没有用量"
    **基本等于**"这次没真问模型"。但那是推断:上游改了响应形状、换个 provider
    不回 usage、或者流式聚合丢了统计,都会走到同一个分支。写死"命中缓存"的话,
    下一个人拿着这行日志去查缓存,而真正的原因在别处 —— 白花两小时。

    所以措辞必须留着不确定性:报「无用量」这个**事实**,把「多半是命中缓存」
    标成推测。
    """
    # Act
    文案 = _fmt_tokens(_无用量())

    # Assert
    assert "无用量" in 文案
    assert "多半" in 文案, "推断必须带不确定语气,不许写成断言"
    # 反向守卫:不许哪天有人把它"简化"成一口咬定的说法
    assert 文案 != "命中缓存"


def test_只有一半用量时也照常报数() -> None:
    """上游偶尔只给 prompt_tokens 不给 completion_tokens。

    这种半残数据不该掉进"无用量"分支 —— 掉进去就等于说"没问模型",而实际问了。
    """
    # Arrange
    半残 = LLMResult(generations=[], llm_output={"token_usage": {"prompt_tokens": 88}})

    # Act / Assert
    assert _fmt_tokens(半残) == "输入 88 / 输出 None tok"


# ==============================================================================
# C 组:tool_guard —— 所有工具的唯一必经处
#
# ⚠️ 同步和 async 两个包装器是**两份代码**,所以每条判据都要各测一遍。
#    漏一份的表现是"有的工具有耗时日志、有的没有",而人会以为那个工具没被调用过。
# ==============================================================================


def test_同步工具正常返回值原样透传并记了耗时(caplog: pytest.LogCaptureFixture) -> None:
    """加计时**不许改变返回值语义** —— 返回的必须还是原来那个对象本身。"""
    # Arrange
    信封 = ok({"count": 3}, user_msg="查到 3 条记录。")

    @tool_guard
    def 查台账() -> Any:
        return 信封

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        结果 = 查台账()

    # Assert:同一个对象,不是"内容相等的另一个"——中间不许有任何拷贝或重建
    assert 结果 is 信封
    assert "工具执行完成" in caplog.text
    assert "查台账" in caplog.text, "日志里得能看出是哪个工具,不然一堆耗时认不出主"
    assert 耗时正则.search(caplog.text) is not None


def test_async工具正常返回值原样透传并记了耗时(caplog: pytest.LogCaptureFixture) -> None:
    """同上,验 async 那一份包装器(它是独立的一段代码)。"""
    # Arrange
    信封 = ok({"hazards": []}, user_msg="没查到隐患。")

    @tool_guard
    async def 查隐患() -> Any:
        return 信封

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        结果 = asyncio.run(查隐患())

    # Assert
    assert 结果 is 信封
    assert "工具执行完成" in caplog.text
    assert "查隐患" in caplog.text
    assert 耗时正则.search(caplog.text) is not None


def test_同步工具抛异常仍返回INTERNAL信封并记了耗时(caplog: pytest.LogCaptureFixture) -> None:
    """异常路径也要记耗时 —— 「慢且最终失败」那一类不记就永远看不见。

    同时守住 tool_guard 的老契约:抛异常也只返回信封,永不向上抛。
    """

    # Arrange
    @tool_guard
    def 会炸的工具() -> Any:
        raise ValueError("数据库连不上")

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        结果 = 会炸的工具()

    # Assert:返回值语义一点没变
    assert 结果["ok"] is False
    assert 结果["error_code"] == ErrorCode.INTERNAL.value
    assert 结果["data"] is None
    # detail 只进日志不进返回值(老契约,顺带守一道)
    assert "数据库连不上" not in 结果["user_msg"]
    # 新加的:失败路径同样有耗时
    assert "工具执行失败" in caplog.text
    assert "ValueError" in caplog.text
    assert 耗时正则.search(caplog.text) is not None


def test_async工具抛异常仍返回INTERNAL信封并记了耗时(caplog: pytest.LogCaptureFixture) -> None:
    """同上,验 async 那一份。"""

    # Arrange
    @tool_guard
    async def 会炸的async工具() -> Any:
        raise TimeoutError("上游没响应")

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        结果 = asyncio.run(会炸的async工具())

    # Assert
    assert 结果["ok"] is False
    assert 结果["error_code"] == ErrorCode.INTERNAL.value
    assert "工具执行失败" in caplog.text
    assert "TimeoutError" in caplog.text
    assert 耗时正则.search(caplog.text) is not None


def test_同步工具的耗时数就是执行前后的差(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """把工具那一侧的耗时数也钉死 —— 它和模型那一侧合起来才是判据。"""
    # Arrange:进入前 500.0,执行完 507.3 → 期望 7.3 秒
    monkeypatch.setattr(errors_mod, "time", 假时钟([500.0, 507.3]))

    @tool_guard
    def 慢工具() -> Any:
        return ok()

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        慢工具()

    # Assert
    assert _取耗时(caplog) == pytest.approx(7.3)


def test_超过工具阈值抬到warning_没超只记info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """分级判据走的是真配置(GYT_TOOL_SLOW_WARN_S),不是测试里另抄一个数。

    ⚠️ 耗时用假时钟造,**不靠"真跑一下自然会超"** —— 一个空函数的真实耗时是
       亚微秒级,想让它超过阈值就得把阈值压到 1e-9,那等于在赌时钟分辨率,
       是典型的飘测试。这里两边都把耗时钉死:30 秒必超 20,1 秒必不超。
    """
    # Arrange:阈值用配置默认值(20 秒),两次调用分别造出 30 秒和 1 秒
    _设阈值(monkeypatch, GYT_TOOL_SLOW_WARN_S="20")

    @tool_guard
    def 普通工具() -> Any:
        return ok()

    # Act:30 秒 —— 超阈值
    monkeypatch.setattr(errors_mod, "time", 假时钟([0.0, 30.0]))
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        普通工具()
    慢的级别 = _级别集合(caplog, ERRORS_LOGGER)
    慢的正文 = caplog.text
    caplog.clear()

    # Act:1 秒 —— 没超
    monkeypatch.setattr(errors_mod, "time", 假时钟([0.0, 1.0]))
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        普通工具()

    # Assert
    # ⚠️ 断言必须用**带括号的完整标记**,不能只搜"阈值"两个字:日志里带着工具的
    #    qualname,而 qualname 里含本用例的函数名 —— 函数名恰好也有"阈值"。
    #    只搜两个字的话,"没超那次不该出现阈值"这条永远为假(踩过一次)。
    超阈值标记 = "(超过 20s 阈值)"
    assert 慢的级别 == {logging.WARNING}
    assert 超阈值标记 in 慢的正文, "抬级别的理由要写清楚,不然看的人不知道凭什么算慢"
    assert _级别集合(caplog, ERRORS_LOGGER) == {logging.INFO}
    assert 超阈值标记 not in caplog.text


def test_耗时正好等于阈值时算慢(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """判据是 ``>=`` 不是 ``>``,边界那一下归"慢"。

    单拎一条出来钉边界,是因为 30 秒 / 1 秒那种一眼分明的用例**盖不住**
    把 ``>=`` 写成 ``>`` 的改动 —— 两条判据在非边界值上表现完全一样。
    """
    # Arrange:阈值 20,耗时正好 20.0
    _设阈值(monkeypatch, GYT_TOOL_SLOW_WARN_S="20")
    monkeypatch.setattr(errors_mod, "time", 假时钟([0.0, 20.0]))

    @tool_guard
    def 工具() -> Any:
        return ok()

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        工具()

    # Assert
    assert _级别集合(caplog, ERRORS_LOGGER) == {logging.WARNING}


def test_阈值读不出来时不猜数字只是不再抬warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """配置炸了(.env 写坏、校验不过)也不许把工具打断。

    退路刻意选"永不告警"而不是"抄一份 20.0":抄了就是第二份真相,
    而真读不出来的时候猜一个数只会误报。耗时那个数照样在日志里,人自己看得出。
    """

    # Arrange
    def _读配置就炸() -> Any:
        raise RuntimeError("配置校验不过")

    monkeypatch.setattr(errors_mod, "get_settings", _读配置就炸)

    @tool_guard
    def 工具() -> Any:
        return ok({"v": 1})

    # Act
    with caplog.at_level(logging.INFO, logger=ERRORS_LOGGER):
        结果 = 工具()

    # Assert:业务照常,耗时照记,只是级别没抬
    assert 结果["ok"] is True
    assert 结果["data"] == {"v": 1}
    assert _级别集合(caplog, ERRORS_LOGGER) == {logging.INFO}
    assert 耗时正则.search(caplog.text) is not None
    assert errors_mod._tool_slow_threshold() == errors_mod._NO_SLOW_THRESHOLD


def test_观测件坏掉时同步工具照常返回正确结果(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 本文件最要紧的一条。

    为了看清楚慢调用而把用户的提问弄失败,是这件东西能犯的最蠢的错。
    这里把 logger 整个换成"每个方法都抛"的替身 —— 连兜底那句 debug 也抛,
    所以 ``_log_tool_elapsed`` 的兜底必须再套一层 suppress,否则当场红。
    """
    # Arrange
    坏日志器 = 会抛的日志器()
    monkeypatch.setattr(errors_mod, "logger", 坏日志器)
    信封 = ok({"count": 7}, user_msg="查到 7 条。")

    @tool_guard
    def 工具() -> Any:
        return 信封

    # Act
    结果 = 工具()

    # Assert:返回值一个字节都没变
    assert 结果 is 信封
    # 反向守卫:确认替身**真的被调到了**(不然这个用例可能什么都没测到)
    assert 坏日志器.被调用次数 > 0


def test_观测件坏掉时async工具照常返回正确结果(monkeypatch: pytest.MonkeyPatch) -> None:
    """同上,验 async 那一份包装器。"""
    # Arrange
    坏日志器 = 会抛的日志器()
    monkeypatch.setattr(errors_mod, "logger", 坏日志器)
    信封 = ok({"ok": True})

    @tool_guard
    async def 工具() -> Any:
        return 信封

    # Act
    结果 = asyncio.run(工具())

    # Assert
    assert 结果 is 信封
    assert 坏日志器.被调用次数 > 0


@pytest.mark.parametrize("失败类型", [None, "ValueError"], ids=["成功路径", "失败路径"])
def test_耗时观测自己坏掉时不抛(monkeypatch: pytest.MonkeyPatch, 失败类型: str | None) -> None:
    """直接给 ``_log_tool_elapsed`` 灌一个"每个方法都抛"的 logger,两条形状都试。

    为什么单拎出来测这个函数,而不是端到端地拿全坏的 logger 去跑异常路径:
    ``tool_guard`` 的异常路径上,``logger.exception`` 和 ``fail()`` 里那句
    ``logger.warning`` 都排在计时**之前**,而它们**都是这次改动之前就有的**。
    拿全坏的替身跑端到端,红的是那两句老代码,测不到新加的兜底 —— 那是把
    "既有行为"冒充成"我这次的洞",结论会误导下一个人。

    所以这里精确地只验一件事:**新加的这个函数在 logger 全坏时不抛**。
    端到端那一侧由上面两条"成功路径"的用例守着(那条路上计时是第一个用 logger 的)。
    """
    # Arrange
    坏日志器 = 会抛的日志器()
    monkeypatch.setattr(errors_mod, "logger", 坏日志器)

    # Act / Assert:不许抛,一次都不许
    errors_mod._log_tool_elapsed("某工具", 1.5, 失败类型)
    assert 坏日志器.被调用次数 > 0, "替身没被调到,这个用例什么都没测到"


def test_tool_guard没把函数元信息弄丢() -> None:
    """functools.wraps 那一层还在 —— 名字丢了的话耗时日志会全写成 _sync_wrapper。"""

    # Arrange / Act
    @tool_guard
    def 记任务() -> Any:
        """记一条任务。"""
        return ok()

    # Assert
    assert 记任务.__name__ == "记任务"
    assert 记任务.__doc__ == "记一条任务。"


# ==============================================================================
# D 组:ainvoke —— 路径乙(识图、评测打分等直调)
#
# 路径乙走不到 LlmCallTiming(那个回调挂在 get_chat_model 造的模型上,而直调用的是
# 调用方自己的模型),而且这一段里还夹着缓存查找与自研退避重试 —— 那些时间全在
# 回调之外。所以它单记一条"整段"的账。
# ==============================================================================


def test_命中缓存与真调模型的日志能分开(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 命中缓存只花几毫秒,和真调用写成同一句话的话:

    - 看见"耗时=0.0s"会以为模型突然变快了;
    - 真慢的时候也没法立刻排除"是不是缓存没命中"。

    所以结论直接写进日志,不让人猜。
    """
    # Arrange:缓存关掉 → 必定真调模型
    _设阈值(monkeypatch, GYT_LLM_CACHE_ENABLED="false")
    模型 = 假模型([AIMessage(content="真调模型给的回答")])

    # Act
    with caplog.at_level(logging.INFO, logger=LLM_LOGGER):
        asyncio.run(llm_mod.ainvoke(模型, QUESTION))
    真调正文 = caplog.text
    caplog.clear()

    # Arrange:缓存开着,且把读缓存打桩成必命中(不碰磁盘)
    _设阈值(monkeypatch, GYT_LLM_CACHE_ENABLED="true")
    monkeypatch.setattr(llm_mod, "_cache_read", lambda *_a, **_k: AIMessage(content="缓存里的"))
    地雷模型 = 假模型([RuntimeError("命中缓存就不该走到这儿")])

    with caplog.at_level(logging.INFO, logger=LLM_LOGGER):
        回答 = asyncio.run(llm_mod.ainvoke(地雷模型, QUESTION))

    # Assert
    assert 回答.content == "缓存里的"
    assert 地雷模型.调用次数 == 0, "前提没成立:压根没命中缓存,这个用例就白测了"
    assert llm_mod._OUTCOME_CALLED in 真调正文
    assert llm_mod._OUTCOME_CACHED not in 真调正文
    assert llm_mod._OUTCOME_CACHED in caplog.text
    assert llm_mod._OUTCOME_CALLED not in caplog.text
    # 两条都得有耗时,不然分开也没意义
    assert 耗时正则.search(真调正文) is not None
    assert 耗时正则.search(caplog.text) is not None


def test_直调失败也记耗时且不说成完成(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """「重试几轮、每轮都等满超时」正是最值得看见的一种慢,而它永远走不到成功分支。

    所以 ainvoke 那一处用的是 finally 而不是 else 段。落点写"未拿到回答",
    不许把失败说成"完成"。
    """
    # Arrange:关缓存(不碰磁盘)+ 不重试(不真等)
    _设阈值(monkeypatch, GYT_LLM_CACHE_ENABLED="false", GYT_LLM_MAX_RETRIES="0")
    模型 = 假模型([RuntimeError("上游炸了")])

    # Act
    with caplog.at_level(logging.INFO, logger=LLM_LOGGER):
        with pytest.raises(llm_mod.LLMCallError):
            asyncio.run(llm_mod.ainvoke(模型, QUESTION))

    # Assert
    assert llm_mod._OUTCOME_FAILED in caplog.text
    assert 耗时正则.search(caplog.text) is not None


def test_直调耗时的数是整段的差(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """记的是**整个 ainvoke**(含缓存查找与重试),不是单次模型请求。

    这正是它和 LlmCallTiming 的分工:那个回调只量"一次 HTTP 请求",
    两个数一比才知道 198 秒到底花在哪儿。
    """
    # Arrange
    _设阈值(monkeypatch, GYT_LLM_CACHE_ENABLED="false")
    monkeypatch.setattr(llm_mod, "time", 假时钟([200.0, 263.4]))
    模型 = 假模型([AIMessage(content="回答")])

    # Act
    with caplog.at_level(logging.INFO, logger=LLM_LOGGER):
        asyncio.run(llm_mod.ainvoke(模型, QUESTION))

    # Assert
    assert _取耗时(caplog) == pytest.approx(63.4)


def test_超过直调阈值抬到warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """分级判据走真配置(GYT_LLM_SLOW_WARN_S);耗时同样用假时钟钉死,不赌时钟分辨率。"""
    # Arrange:阈值 30 秒(配置默认),造一次 213 秒的调用 —— 就是线上那次的数
    _设阈值(monkeypatch, GYT_LLM_CACHE_ENABLED="false", GYT_LLM_SLOW_WARN_S="30")
    monkeypatch.setattr(llm_mod, "time", 假时钟([0.0, 213.0]))
    模型 = 假模型([AIMessage(content="回答")])

    # Act
    with caplog.at_level(logging.INFO, logger=LLM_LOGGER):
        asyncio.run(llm_mod.ainvoke(模型, QUESTION))

    # Assert
    assert logging.WARNING in _级别集合(caplog, LLM_LOGGER)
    assert "超过 30s 阈值" in caplog.text
    assert _取耗时(caplog) == pytest.approx(213.0)


def test_直调观测坏掉时回答照常返回(monkeypatch: pytest.MonkeyPatch) -> None:
    """同 tool_guard 那条:观测件绝不许把主路径打断。"""
    # Arrange
    _设阈值(monkeypatch, GYT_LLM_CACHE_ENABLED="false")
    monkeypatch.setattr(llm_mod, "logger", 会抛的日志器())
    模型 = 假模型([AIMessage(content="照样要拿到的回答")])

    # Act
    回答 = asyncio.run(llm_mod.ainvoke(模型, QUESTION))

    # Assert
    assert 回答.content == "照样要拿到的回答"


# ==============================================================================
# E 组:同源守卫 —— 两处计时的分级判据必须是同一条
# ==============================================================================


def test_两处计时的阈值都来自配置而不是硬编码() -> None:
    """所有常量的唯一入口是 config.get_settings(本仓硬约定)。

    这条守的是"别有人为了图省事在计时代码里写死 20 / 30"。
    """
    # Arrange
    settings = get_settings()

    # Assert:配置里两个阈值都在,且是独立的两个数(工具那侧比模型那侧严)
    assert settings.tool_slow_warn_s > 0
    assert settings.llm_slow_warn_s > 0
    # errors 那侧的退路是 inf,不是抄来的 20.0 —— 抄了就是第二份真相
    assert errors_mod._NO_SLOW_THRESHOLD == float("inf")
    assert errors_mod._NO_SLOW_THRESHOLD != settings.tool_slow_warn_s


def test_errors模块没有被拉进langchain依赖() -> None:
    """🔴 ``core/errors.py`` 至今是 langchain-free 的,这次加计时**刻意没有**
    去共用 ``core/timing.py`` 里那套分级代码,就是为了保住这条。

    实测(2026-08-20):单独 import errors 24.8ms,import timing 249.6ms —— 后者
    要拉 langchain(``LlmCallTiming`` 的基类 ``BaseCallbackHandler``)。
    attendance/cleanup.py、core/doc_no.py、attendance/messages.py 三个轻量入口
    都还靠着这条。哪天有人"顺手把重复的十几行合并了",这个用例当场红。
    """
    # Arrange:直接看源码的 import 行,不看 sys.modules
    # (整套测试跑起来时 langchain 早被别的用例拉进来了,查 sys.modules 一定假绿)
    源码 = errors_mod.__file__ or ""
    assert 源码, "拿不到 errors.py 的路径"
    with open(源码, encoding="utf-8") as f:
        正文 = f.read()

    # Act:只看模块级 import(顶格那些),注释里提到 langchain 是**应该**的 ——
    # 那正是记录"为什么不共用"的地方,不该被这条守卫误伤。
    导入行 = [行 for 行 in 正文.splitlines() if 行.startswith(("import ", "from "))]

    # Assert
    assert [行 for 行 in 导入行 if "langchain" in 行] == [], (
        "core/errors.py 不许 import langchain,理由见本用例 docstring"
    )
    assert [行 for 行 in 导入行 if "timing" in 行] == [], (
        "core/errors.py 不许 import core.timing —— 那等于间接把 langchain 拉进来"
    )
    # 反向守卫:确认这个用例确实读到了源码本身(而不是读了个空文件在空转)
    assert "def tool_guard" in 正文
