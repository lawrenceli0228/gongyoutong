"""``gyt.config`` 单元测试。

覆盖四件事(契约要求):默认值、GYT_ 前缀环境变量覆盖、派生目录属性会自动建目录、
以及 ``get_settings`` 的 lru_cache 清理行为。另外补上边界校验与扩展名白名单。

注意:``tests/conftest.py`` 里的 autouse fixture ``_isolated_settings`` 会预置
GYT_DATA_DIR / GYT_DEEPSEEK_API_KEY / GYT_MOONSHOT_API_KEY。所以测"默认值"时必须
先把 GYT_ 开头的环境变量清干净,并用 ``_env_file=None`` 屏蔽磁盘上真实的 .env,
否则测的就不是默认值,而是别人的环境。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from gyt.config import (
    ALLOWED_CAD_EXT,
    ALLOWED_DOC_EXT,
    ALLOWED_IMAGE_EXT,
    ENV_PREFIX,
    Settings,
    get_settings,
)

# 由字段名反推出的环境变量名(全小写),用来精确清场。
# 刻意不按 "GYT_" 前缀一刀切:conftest 里的 GYT_E2E 是测试开关而不是 Settings 字段,
# 误删会让 E2E 用例永远跑不起来。
_SETTINGS_ENV_NAMES: frozenset[str] = frozenset(
    f"{ENV_PREFIX}{field_name}".lower() for field_name in Settings.model_fields
)


def _pristine_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """构造一个不受环境变量和 .env 影响的 Settings,用来断言纯默认值。

    conftest 的 autouse fixture 会预置几个 GYT_ 变量,这里先按字段名把它们摘干净,
    再用 ``_env_file=None`` 屏蔽磁盘上的真实 .env —— 两道都做了,断言的才是真默认值。
    """
    for name in tuple(os.environ):
        if name.lower() in _SETTINGS_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


def test_default_credentials_are_empty_and_urls_match_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange & Act
    settings = _pristine_settings(monkeypatch)

    # Assert:密钥默认必须为空(空 = 未配置,由 core/llm.py 给中文提示)
    assert settings.deepseek_api_key == ""
    assert settings.moonshot_api_key == ""
    assert settings.deepseek_base_url == "https://api.deepseek.com/v1"
    # Moonshot 是 .ai 不是 .cn,写错会连不上
    assert settings.moonshot_base_url == "https://api.moonshot.ai/v1"


def test_default_models_match_2026_08_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange & Act
    settings = _pristine_settings(monkeypatch)

    # Assert:旧型号(deepseek-chat / kimi-k2 / moonshot-v1)全部已弃用,不能出现
    assert settings.model_text == "deepseek-v4-flash"
    assert settings.model_vision == "kimi-k3"
    assert settings.model_tool_fallback == "kimi-k3"
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.disable_thinking_for_text is True


def test_default_runtime_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange & Act
    settings = _pristine_settings(monkeypatch)

    # Assert
    assert settings.supervisor_recursion_limit == 8
    # 150 而非契约 v1 的 60:2026-08-07 实测一张 4000×2430 的工地照片要 59.7 秒,
    # 距 60 秒只剩 0.26 秒。理由与实测数据写在 config.py 该字段上方。
    assert settings.llm_timeout_s == 150.0
    assert settings.llm_max_retries == 3
    assert settings.llm_retry_base_delay_s == 1.0
    assert settings.llm_cache_enabled is True
    assert settings.prompt_version == "v1"


def test_default_file_limits_and_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange & Act
    settings = _pristine_settings(monkeypatch)

    # Assert
    assert settings.drawing_max_mb == 64.0
    assert settings.document_max_mb == 10.0
    assert settings.photo_max_mb == 10.0
    assert settings.photo_compress_target_mb == 4.0
    assert settings.photo_compress_max_edge_px == 2048
    assert settings.data_dir == Path("data")
    assert settings.eval_threshold_routing == 0.90
    assert settings.eval_threshold_safety == 0.80
    assert settings.eval_threshold_rag == 0.80


# ---------------------------------------------------------------------------
# GYT_ 前缀环境变量覆盖
# ---------------------------------------------------------------------------


def test_env_var_with_gyt_prefix_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setenv("GYT_MODEL_TEXT", "deepseek-v4-flash-preview")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.model_text == "deepseek-v4-flash-preview"


def test_env_var_name_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange:pydantic-settings 默认大小写不敏感,小写写法同样生效
    monkeypatch.setenv("gyt_prompt_version", "v7")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.prompt_version == "v7"


def test_env_var_values_are_coerced_to_field_types(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange:环境变量永远是字符串,必须被转成 int / float / bool
    monkeypatch.setenv("GYT_LLM_MAX_RETRIES", "7")
    monkeypatch.setenv("GYT_LLM_TIMEOUT_S", "12.5")
    monkeypatch.setenv("GYT_LLM_CACHE_ENABLED", "false")
    monkeypatch.setenv("GYT_DATA_DIR", "/tmp/gyt-somewhere")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.llm_max_retries == 7
    assert settings.llm_timeout_s == 12.5
    assert settings.llm_cache_enabled is False
    assert settings.data_dir == Path("/tmp/gyt-somewhere")


def test_unknown_gyt_env_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange:extra="ignore",别人往 .env 里塞前端/运维变量不该让后端起不来
    monkeypatch.setenv("GYT_SOMETHING_NOBODY_DECLARED", "1")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.model_text == "deepseek-v4-flash"
    assert not hasattr(settings, "something_nobody_declared")


def test_env_file_is_parsed_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange:.env 里带中文注释和中文值,不能因为编码问题炸掉
    monkeypatch.delenv("GYT_PROMPT_VERSION", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "# 提示词版本,改提示词就改这里\nGYT_PROMPT_VERSION=第二版\n",
        encoding="utf-8",
    )

    # Act
    settings = Settings(_env_file=str(env_file))

    # Assert
    assert settings.prompt_version == "第二版"


# ---------------------------------------------------------------------------
# 派生目录属性
# ---------------------------------------------------------------------------


def test_derived_dirs_are_created_on_first_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    data_root = tmp_path / "gyt-data"
    monkeypatch.setenv("GYT_DATA_DIR", str(data_root))
    get_settings.cache_clear()
    settings = get_settings()
    assert not data_root.exists(), "只构造 Settings 不该产生任何副作用"

    # Act
    dirs = [settings.uploads_dir, settings.artifacts_dir, settings.cache_dir, settings.chroma_dir]

    # Assert
    assert [d.name for d in dirs] == ["uploads", "artifacts", "cache", "chroma"]
    for created in dirs:
        assert created.is_dir()
        assert created.parent == data_root


def test_derived_dir_access_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "gyt-data"))
    get_settings.cache_clear()
    settings = get_settings()

    # Act:目录已存在时再访问不该报错
    first = settings.cache_dir
    (first / "占位.txt").write_text("x", encoding="utf-8")
    second = settings.cache_dir

    # Assert
    assert first == second
    assert (second / "占位.txt").exists(), "重复访问不能清空已有内容"


def test_sqlite_path_creates_parent_but_not_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    data_root = tmp_path / "gyt-data"
    monkeypatch.setenv("GYT_DATA_DIR", str(data_root))
    get_settings.cache_clear()

    # Act
    sqlite_path = get_settings().sqlite_path

    # Assert:父目录建好,空文件留给 sqlite 自己建
    assert sqlite_path == data_root / "gyt.sqlite3"
    assert data_root.is_dir()
    assert not sqlite_path.exists()


# ---------------------------------------------------------------------------
# lru_cache 行为
# ---------------------------------------------------------------------------


def test_get_settings_returns_the_same_instance() -> None:
    # Act
    first = get_settings()
    second = get_settings()

    # Assert:全进程单例,避免每次读配置都重新解析 .env
    assert first is second


def test_cache_clear_is_required_for_env_changes_to_take_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    monkeypatch.setenv("GYT_PROMPT_VERSION", "v1")
    get_settings.cache_clear()
    assert get_settings().prompt_version == "v1"

    # Act:改了环境变量但没清缓存
    monkeypatch.setenv("GYT_PROMPT_VERSION", "v9")

    # Assert:仍是旧值 —— 这是测试里最容易踩的坑,故意固化成用例
    assert get_settings().prompt_version == "v1"

    # Act & Assert:清了缓存才生效
    get_settings.cache_clear()
    assert get_settings().prompt_version == "v9"


# ---------------------------------------------------------------------------
# 边界校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name",
    ["GYT_EVAL_THRESHOLD_ROUTING", "GYT_EVAL_THRESHOLD_SAFETY", "GYT_EVAL_THRESHOLD_RAG"],
)
@pytest.mark.parametrize("bad_value", ["-0.1", "1.5"])
def test_eval_thresholds_outside_zero_to_one_are_rejected(
    monkeypatch: pytest.MonkeyPatch, env_name: str, bad_value: str
) -> None:
    # Arrange
    monkeypatch.setenv(env_name, bad_value)

    # Act & Assert:评测门槛必须落在 0~1,写错了要在启动时就炸,别等跑评测才发现
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("env_name", "bad_value"),
    [
        ("GYT_SUPERVISOR_RECURSION_LIMIT", "0"),
        ("GYT_LLM_TIMEOUT_S", "0"),
        ("GYT_LLM_MAX_RETRIES", "-1"),
        ("GYT_LLM_RETRY_BASE_DELAY_S", "-1"),
        ("GYT_DRAWING_MAX_MB", "0"),
        ("GYT_DOCUMENT_MAX_MB", "-5"),
        ("GYT_PHOTO_MAX_MB", "0"),
        ("GYT_PHOTO_COMPRESS_TARGET_MB", "0"),
        ("GYT_PHOTO_COMPRESS_MAX_EDGE_PX", "0"),
    ],
)
def test_non_positive_limits_are_rejected(
    monkeypatch: pytest.MonkeyPatch, env_name: str, bad_value: str
) -> None:
    # Arrange
    monkeypatch.setenv(env_name, bad_value)

    # Act & Assert
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_zero_retry_and_zero_delay_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange:core/llm.py 的测试需要把重试关掉、把退避等待压成 0,不能被校验挡住
    monkeypatch.setenv("GYT_LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("GYT_LLM_RETRY_BASE_DELAY_S", "0")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.llm_max_retries == 0
    assert settings.llm_retry_base_delay_s == 0.0


# ---------------------------------------------------------------------------
# 扩展名白名单
# ---------------------------------------------------------------------------


def test_extension_whitelists_contents() -> None:
    # Assert:内容锁死,任何一方改动都要走契约评审
    assert ALLOWED_IMAGE_EXT == frozenset({".jpg", ".jpeg", ".png", ".webp"})
    assert ALLOWED_DOC_EXT == frozenset({".pdf", ".docx", ".txt", ".md"})
    assert ALLOWED_CAD_EXT == frozenset({".dxf"})


def test_dwg_is_not_accepted_as_cad() -> None:
    # Assert:MVP 要求用户离线转成 DXF 再传,后端不背 DWG 解析依赖
    assert ".dwg" not in ALLOWED_CAD_EXT


def test_all_whitelisted_extensions_are_lowercase_and_dotted() -> None:
    # Arrange
    every_ext = ALLOWED_IMAGE_EXT | ALLOWED_DOC_EXT | ALLOWED_CAD_EXT

    # Assert:校验代码统一按"小写 + 带点"比对,白名单本身必须先守规矩
    for ext in every_ext:
        assert ext.startswith("."), f"{ext} 缺少前导点"
        assert ext == ext.lower(), f"{ext} 必须是小写"


def test_whitelists_are_immutable() -> None:
    # Assert:frozenset,谁都别想在运行时往白名单里偷加扩展名
    with pytest.raises(AttributeError):
        ALLOWED_IMAGE_EXT.add(".gif")  # type: ignore[attr-defined]


def test_isolation_fixture_blocks_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归测试:conftest 的隔离必须同时堵住「进程环境变量」与「.env 文件」两条来源。

    以前只清了环境变量,于是队友在 backend/ 建一个 .env 调试,测试就会莫名其妙红,
    而 CI 里没这个文件 —— 典型的「CI 绿、我这儿红」。这条用例把那个洞钉死。
    """
    # Arrange:在一个临时工作目录里放一份"有毒"的 .env,并把进程 cwd 挪进去。
    # env_file 是相对工作目录解析的,所以这就等价于队友本机 backend/.env 的场景。
    (tmp_path / ".env").write_text(
        "GYT_LLM_CACHE_ENABLED=false\nGYT_DEEPSEEK_API_KEY=sk-should-never-be-read\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    # Act
    settings = get_settings()

    # Assert:磁盘上那份 .env 的值一个都不许生效
    assert settings.llm_cache_enabled is True
    assert settings.deepseek_api_key != "sk-should-never-be-read"
