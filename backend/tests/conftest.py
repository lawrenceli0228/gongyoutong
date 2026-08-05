"""pytest 全局测试基础设施(工友通 backend)。

本文件是所有泳道测试的共同地基:任何 tests/ 下的用例都能直接使用这里的 fixture,
不需要各自再造一份环境隔离逻辑(DRY 红线)。改动这里等于改动所有人的测试环境,
动之前先在群里说一声。

为什么必须做「设置隔离」这件事?
    Settings 由 pydantic-settings 从「进程环境变量 + .env 文件」读取,而 get_settings()
    上面挂了 @lru_cache —— 也就是说**第一次调用时看到的环境会被整个测试进程记住**。
    Settings 有**两条**来源(进程环境变量 + .env 文件),两条都得堵,只堵一条等于没堵。
    不隔离会同时踩三个坑:
      1. 开发者本机的真实 API Key 漏进测试(环境变量导出的、或写在 backend/.env 里的)。
         万一某个用例忘了 mock,就会真的把请求打到 DeepSeek / Kimi 上,
         既烧钱又让测试依赖网络;.env 里的其它配置项还会造成「CI 绿、我这儿红」。
      2. data_dir 指向仓库里的 ./data,测试写出的 artifacts / cache 污染真实数据目录;
         并且用例之间互相看得见对方的残留文件,失败的可复现性直接归零。
      3. 前一个用例 monkeypatch 出来的环境被 lru_cache 带进下一个用例,
         出现「单跑绿、全跑红」这类最难查的问题。

    每个用例的生命周期(autouse,所有人都自动享受,不用显式声明):

        ┌──────────────────────────────────────────────────────────────────┐
        │ cache_clear() → 清 GYT_* 残留 → 掐掉 .env 来源                    │
        │   → 打补丁(假 Key + tmp 数据目录) → [ 用例执行 ] → cache_clear()  │
        └──────────────────────────────────────────────────────────────────┘
             ↑ 丢掉上一个用例遗留的           ↑ 丢掉本用例的 Settings,
               Settings 单例                    不让它漏给下一个用例

异步用例统一走 pytest-asyncio 的 asyncio_mode=auto(在 backend/pyproject.toml 配),
所以 async def 测试直接写即可,不用挨个加 @pytest.mark.asyncio。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

# 兜底:若 gyt 尚未以可编辑方式装进环境(有人直接 `pytest` 而没走 `uv run`),
# 把 backend/src 补进 sys.path,让 import gyt 仍然成立。正常流程走 uv sync 时这段不生效。
_BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if importlib.util.find_spec("gyt") is None and _BACKEND_SRC.is_dir():
    sys.path.insert(0, str(_BACKEND_SRC))

from gyt.config import Settings, get_settings  # noqa: E402  (必须在 sys.path 兜底之后再导入)

# --- 常量(禁止在下面的 fixture 里写字面量) -------------------------------------

ENV_PREFIX = "GYT_"
"""所有配置项的环境变量前缀,与 Settings.model_config 的 env_prefix 保持一致。"""

ENV_FILE_CONFIG_KEY = "env_file"
"""Settings.model_config 里指向 .env 的键名。测试期要把它按掉,理由见 _isolated_settings。"""

ENV_VARS_KEPT_DURING_TESTS = frozenset({"GYT_E2E"})
"""隔离时要放行的 GYT_* 变量。

GYT_E2E 是「要不要跑 E2E」这个开关本身,属于测试基础设施而非业务配置,
如果被一起清掉,E2E 用例就永远跑不起来了。
"""

FAKE_DEEPSEEK_API_KEY = "test-deepseek-key"
FAKE_MOONSHOT_API_KEY = "test-moonshot-key"
"""明显是假的 Key。取名带 test- 前缀,是为了万一真被发出去,在账单/日志里一眼能认出来。"""

FAKE_AI_MESSAGE_ID = "fake-ai-message-0001"
FAKE_AI_MESSAGE_CONTENT = "这是测试用的假回复,没有调用任何外部接口。"


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义标记,避免 --strict-markers 下报「未知标记」。"""
    config.addinivalue_line(
        "markers",
        "e2e: 端到端冒烟(要真起 docker compose),默认跳过,置环境变量 GYT_E2E=1 才跑",
    )


def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉进程里所有会影响 Settings 的 GYT_* 变量。

    只清「读取 Settings 用的」那批,放行 ENV_VARS_KEPT_DURING_TESTS。
    用 monkeypatch.delenv 而不是直接改 os.environ,是为了让 pytest 在用例结束后
    自动把开发者本机的真实环境原样还回去(不可变原则的工程化落地)。
    """
    for name in tuple(os.environ):
        if not name.startswith(ENV_PREFIX) or name in ENV_VARS_KEPT_DURING_TESTS:
            continue
        monkeypatch.delenv(name, raising=False)


def _disable_dotenv_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 pydantic-settings 的「.env 文件」这条配置来源整条掐掉。

    只清环境变量是**不够**的 —— Settings 有两条来源:

        进程环境变量  ──┐
                        ├──► Settings 实例
        .env 文件     ──┘   (env_file=".env",相对**进程工作目录**解析)

    `make test` 恰好在 backend/ 下跑,所以队友为了调试建的 backend/.env 会被读到;
    更糟的是环境变量被上面清干净之后,.env 反而成了唯一生效的来源。
    实测:backend/.env 里只写一行 GYT_LLM_CACHE_ENABLED=false,
    test_cache_hit_skips_model / test_clear_cache_counts_only_cache_entries 就会红,
    而 CI 里没有这个文件 —— 典型的「CI 绿、我这儿红」,还会把人往 lru_cache 上带偏。

    model_config 是普通 dict(SettingsConfigDict 是 TypedDict),
    monkeypatch.setitem 会在用例结束后自动还原,不会污染同进程的其它代码。
    """
    monkeypatch.setitem(Settings.model_config, ENV_FILE_CONFIG_KEY, None)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """给每个用例一套干净、独立、绝不联网的配置环境(autouse,自动生效)。

    做五件事:
      1. 先清 lru_cache,保证本用例拿到的是重新构造的 Settings;
      2. 抹掉开发者本机导出的 GYT_* 变量(尤其是真实 API Key);
      3. 掐掉 .env 文件这条来源 —— 这是 Settings 的**第二条**来源,只清环境变量堵不住,
         详见 _disable_dotenv_source 的说明;
      4. data_dir 指向本用例独占的 tmp_path,artifacts/cache/chroma/sqlite 全落在里面,
         用例结束由 pytest 自动清理,互不干扰;
      5. 塞两个假 Key —— 让「Key 是否为空」这条分支走通(不至于误触 MissingAPIKeyError),
         但任何真实调用都必须被 mock 掉,绝不放行到外网。
    """
    get_settings.cache_clear()
    _clear_config_env(monkeypatch)
    _disable_dotenv_source(monkeypatch)
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GYT_DEEPSEEK_API_KEY", FAKE_DEEPSEEK_API_KEY)
    monkeypatch.setenv("GYT_MOONSHOT_API_KEY", FAKE_MOONSHOT_API_KEY)
    yield
    # 用例结束再清一次:本用例的 Settings 不许被下一个用例继承。
    get_settings.cache_clear()


@pytest.fixture
def fake_ai_message() -> AIMessage:
    """一条假的模型回复,给各泳道 mock LLM 时当返回值用。

    用法示例(L2 的 llm 测试、L4 的 Agent 测试都照这个来):

        async def test_xxx(fake_ai_message, monkeypatch):
            fake_model = AsyncMock()
            fake_model.ainvoke.return_value = fake_ai_message
            ...

    带上 id 与 response_metadata,是为了让缓存/日志相关的断言有真东西可断;
    model_name 从 Settings 取(而不是写死型号),这样 2026-08 之后再换模型,
    测试夹具会自动跟着走,不需要全仓改字符串。
    """
    return AIMessage(
        content=FAKE_AI_MESSAGE_CONTENT,
        id=FAKE_AI_MESSAGE_ID,
        response_metadata={
            "model_name": get_settings().model_text,
            "finish_reason": "stop",
        },
    )
