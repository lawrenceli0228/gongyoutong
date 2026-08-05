"""统一错误信封的单元测试。

重点验证三件事:
1. 信封结构固定四个键,ok/fail 都返回全新对象;
2. detail 只进日志、绝不进返回值(防内部细节泄露给 LLM 与用户);
3. tool_guard 同步 / async 都能兜住异常,返回信封而不是抛出去。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging

import pytest

from gyt.core.errors import (
    DEFAULT_USER_MSG,
    ErrorCode,
    fail,
    ok,
    tool_guard,
)

ENVELOPE_KEYS = {"ok", "data", "user_msg", "error_code"}
# 故意放在异常信息里的「内部细节」,用来验证它不会出现在返回值中。
SECRET_DETAIL = "内部堆栈-/opt/secret/path-token-abc123"


# --- 枚举与默认文案 ----------------------------------------------------------


def test_错误码取值等于名字():
    # Arrange / Act / Assert
    for code in ErrorCode:
        assert code.value == code.name


def test_每个错误码都配了中文默认提示():
    # Arrange
    codes = list(ErrorCode)

    # Act
    missing = [code for code in codes if code not in DEFAULT_USER_MSG]

    # Assert
    assert missing == []
    for code in codes:
        msg = DEFAULT_USER_MSG[code]
        assert msg.strip(), f"{code} 的默认提示不能为空"
        assert msg.isascii() is False, f"{code} 的默认提示必须是中文人话"


# --- ok() --------------------------------------------------------------------


def test_ok_返回成功信封():
    # Act
    envelope = ok({"count": 3}, user_msg="查到 3 条记录。")

    # Assert
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"] == {"count": 3}
    assert envelope["user_msg"] == "查到 3 条记录。"
    assert envelope["error_code"] is None


def test_ok_默认参数下_data_为_none_且提示为空串():
    envelope = ok()

    assert envelope["ok"] is True
    assert envelope["data"] is None
    assert envelope["user_msg"] == ""
    assert envelope["error_code"] is None


def test_ok_每次返回新对象():
    first = ok()
    second = ok()

    assert first is not second


# --- fail() ------------------------------------------------------------------


def test_fail_未给提示时使用默认中文文案():
    envelope = fail(ErrorCode.TIMEOUT)

    assert envelope["ok"] is False
    assert envelope["data"] is None
    assert envelope["user_msg"] == DEFAULT_USER_MSG[ErrorCode.TIMEOUT]
    assert envelope["error_code"] == "TIMEOUT"


def test_fail_自定义提示优先于默认文案():
    envelope = fail(ErrorCode.FILE_TOO_LARGE, "这张照片太大了,请重拍一张小的。")

    assert envelope["user_msg"] == "这张照片太大了,请重拍一张小的。"
    assert envelope["error_code"] == "FILE_TOO_LARGE"


def test_fail_的_detail_只进日志不进返回值(caplog: pytest.LogCaptureFixture):
    # Arrange
    caplog.set_level(logging.WARNING, logger="gyt.core.errors")

    # Act
    envelope = fail(ErrorCode.UPSTREAM_ERROR, detail=SECRET_DETAIL)

    # Assert:返回值里搜不到 detail,但日志里有
    assert SECRET_DETAIL not in json.dumps(envelope, ensure_ascii=False)
    assert set(envelope) == ENVELOPE_KEYS
    assert SECRET_DETAIL in caplog.text


def test_fail_接受裸字符串错误码():
    envelope = fail("RATE_LIMITED")

    assert envelope["error_code"] == "RATE_LIMITED"
    assert envelope["user_msg"] == DEFAULT_USER_MSG[ErrorCode.RATE_LIMITED]


def test_fail_未知错误码降级为_internal():
    envelope = fail("这不是一个错误码")

    assert envelope["error_code"] == "INTERNAL"
    assert envelope["user_msg"] == DEFAULT_USER_MSG[ErrorCode.INTERNAL]


# --- tool_guard:同步 ---------------------------------------------------------


@tool_guard
def _同步正常工具(value: int) -> dict:
    """一个正常返回信封的同步工具。"""
    return ok(value * 2)


@tool_guard
def _同步爆炸工具() -> dict:
    """故意抛异常的同步工具。"""
    raise RuntimeError(SECRET_DETAIL)


def test_tool_guard_同步正常返回原样透传():
    envelope = _同步正常工具(21)

    assert envelope["ok"] is True
    assert envelope["data"] == 42


def test_tool_guard_同步异常返回信封而不是抛出():
    envelope = _同步爆炸工具()

    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is False
    assert envelope["error_code"] == "INTERNAL"
    assert envelope["user_msg"] == DEFAULT_USER_MSG[ErrorCode.INTERNAL]


def test_tool_guard_异常细节不出现在返回值里():
    envelope = _同步爆炸工具()

    assert SECRET_DETAIL not in json.dumps(envelope, ensure_ascii=False)
    assert "RuntimeError" not in json.dumps(envelope, ensure_ascii=False)


def test_tool_guard_把完整堆栈写进日志(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.ERROR, logger="gyt.core.errors")

    _同步爆炸工具()

    assert "RuntimeError" in caplog.text  # logger.exception 带上了堆栈
    assert SECRET_DETAIL in caplog.text


def test_tool_guard_保留原函数元信息():
    assert _同步正常工具.__name__ == "_同步正常工具"
    assert _同步正常工具.__doc__ == "一个正常返回信封的同步工具。"


def test_tool_guard_不吞键盘中断():
    @tool_guard
    def _被打断的工具() -> dict:
        raise KeyboardInterrupt

    # BaseException 必须继续往上走,否则进程没法正常退出
    with pytest.raises(KeyboardInterrupt):
        _被打断的工具()


# --- tool_guard:异步 ---------------------------------------------------------


@tool_guard
async def _异步正常工具(value: int) -> dict:
    """一个正常返回信封的异步工具。"""
    await asyncio.sleep(0)
    return ok(value + 1)


@tool_guard
async def _异步爆炸工具() -> dict:
    """故意抛异常的异步工具。"""
    await asyncio.sleep(0)
    raise ValueError(SECRET_DETAIL)


def test_tool_guard_包装后仍然是协程函数():
    assert inspect.iscoroutinefunction(_异步正常工具) is True
    assert inspect.iscoroutinefunction(_同步正常工具) is False


async def test_tool_guard_异步正常返回原样透传():
    envelope = await _异步正常工具(1)

    assert envelope["ok"] is True
    assert envelope["data"] == 2


async def test_tool_guard_异步异常返回信封():
    envelope = await _异步爆炸工具()

    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is False
    assert envelope["error_code"] == "INTERNAL"
    assert SECRET_DETAIL not in json.dumps(envelope, ensure_ascii=False)
