"""工友通(GYT)全局配置 —— 项目里所有常量的唯一入口。

所有泳道必须遵守的约定:
- 任何模型名、超时、阈值、大小限制、路径,一律从这里取,禁止在业务代码里硬编码。
- 读配置只走 ``get_settings()``;它带 ``lru_cache``,一个进程内只解析一次。
- 测试里改完环境变量后,必须调用 ``get_settings.cache_clear()`` 让新值生效。
- ``Settings`` 实例是全进程共享的,请当成只读对象用;需要局部变体时用
  ``settings.model_copy(update={...})`` 拿一份新副本,不要原地改字段。

事实基线(2026-08 已联网核实,不要用旧型号):
- DeepSeek:``deepseek-chat`` / ``deepseek-reasoner`` 已于 2026-07-24 宣布弃用;
  当前文本模型是 ``deepseek-v4-flash``(1M 上下文,内置思考模式,可显式关闭)。
- Moonshot:``kimi-latest`` / ``kimi-k2`` 系列 / ``moonshot-v1`` 系列已全部停用;
  当前唯一在用模型是 ``kimi-k3``(2026-07-16 上线,2.8T,原生视觉,1M 上下文),
  base_url 是 ``https://api.moonshot.ai/v1``(注意是 .ai 不是 .cn)。
- Embedding:两家供应商都不提供 embedding API,统一用本地 ``BAAI/bge-m3``,
  国内下载走 ``HF_ENDPOINT=https://hf-mirror.com``。

关于 .env 的位置:``env_file`` 是相对当前工作目录解析的,而本仓库的 .env 放在仓库根。
所以有两种正确用法 —— 要么在仓库根目录启动进程,要么用 langgraph-cli 启动
(backend/langgraph.json 里写了 ``"env": "../.env"``,CLI 会先把它灌进 os.environ,
此时环境变量优先级高于 .env 文件,配置照样能读到)。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# 环境变量统一前缀。改这里等于改全项目的环境变量命名,慎动。
ENV_PREFIX: Final[str] = "GYT_"

# ---------------------------------------------------------------------------
# 文件扩展名白名单(模块级常量,上传校验层直接引用,不要各自再抄一份)
# ---------------------------------------------------------------------------
# 注意:DWG 故意不在 CAD 白名单里 —— MVP 要求用户离线转成 DXF 再传,
# 免得后端背上一个又重又不稳的 DWG 解析依赖。
ALLOWED_IMAGE_EXT: Final[frozenset[str]] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
ALLOWED_DOC_EXT: Final[frozenset[str]] = frozenset({".pdf", ".docx", ".txt", ".md"})
ALLOWED_CAD_EXT: Final[frozenset[str]] = frozenset({".dxf"})

# 派生目录/文件名。集中在这里,避免"uploads"这种字符串散落各处。
_UPLOADS_SUBDIR: Final[str] = "uploads"
_ARTIFACTS_SUBDIR: Final[str] = "artifacts"
_CACHE_SUBDIR: Final[str] = "cache"
_CHROMA_SUBDIR: Final[str] = "chroma"
_SQLITE_FILENAME: Final[str] = "gyt.sqlite3"


def _ensure_dir(path: Path) -> Path:
    """确保目录存在(含各级父目录)并返回它。

    失败时不静默吞掉:先记日志(带真实系统错误),再抛出一句工人看得懂的中文。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 磁盘满、只读挂载、权限不足等
        logger.error("创建数据目录失败: path=%s, err=%s", path, exc)
        raise RuntimeError(f"数据目录创建失败,请检查磁盘空间和目录权限:{path}") from exc
    return path


class Settings(BaseSettings):
    """全局配置。字段名与环境变量的映射规则:``字段名`` -> ``GYT_字段名大写``。

    配置来源优先级(高 -> 低):

        +---------------------------------------------------------+
        |  1. 显式构造参数      Settings(model_text="xxx")          |
        |            |            (只在测试/局部变体里用)           |
        |            v                                             |
        |  2. 进程环境变量      GYT_MODEL_TEXT=xxx                  |
        |            |            (env_prefix="GYT_",大小写不敏感)  |
        |            v                                             |
        |  3. .env 文件         仓库根 .env,UTF-8 编码             |
        |            |                                             |
        |            v                                             |
        |  4. 字段默认值        本文件里写死的那份基线              |
        +---------------------------------------------------------+

    未知的 GYT_ 变量会被忽略(``extra="ignore"``),这样别人往 .env 里塞了
    前端或运维用的变量时,后端不会直接启动失败。
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 供应商凭据与入口 -------------------------------------------------
    # repr=False:把密钥挡在 repr()/str() 之外。
    # pydantic v2 的 __repr__ / __str__ 默认会把**所有**字段原值打出来,而契约规定
    # 「其余泳道只准用 get_settings()」—— 也就是 W2-W4 的 6 个 Agent、上传校验层、
    # 评测脚本人手一份这个对象。只要有一个人为了排查写下
    #     logger.info("当前配置:%s", get_settings())
    # 明文密钥就会进 stdout → `docker compose logs` → CI 里 `cat langgraph.log`
    # (ci.yml 收尾步骤,if: always())→ 公开的 Actions 日志,顺带还进演示录屏。
    # 字段名/类型/默认值一个都没改,只是不让它出现在字符串化结果里。
    #
    # ⚠️ 遗留缺口:model_dump() 仍会带出明文(那是"我确实要取值"的语义,repr 管不着)。
    #    彻底堵死需要把类型换成 pydantic.SecretStr —— 那是**改契约字段类型**,
    #    要全员拍板 + 同步改所有取值点,不是单条泳道能定的,见交付说明。
    deepseek_api_key: str = Field(default="", repr=False)  # 空 = 未配置,由 core/llm.py 抛中文提示
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    moonshot_api_key: str = Field(default="", repr=False)
    moonshot_base_url: str = "https://api.moonshot.ai/v1"  # 是 .ai,不是 .cn

    # --- 模型选型 ---------------------------------------------------------
    model_text: str = "deepseek-v4-flash"  # 路由 / 任务 / 报告 / 知识综合
    model_vision: str = "kimi-k3"  # Safety 识图 / CAD 预览问答
    model_tool_fallback: str = "kimi-k3"  # 工具调用评测不达标时的备胎
    embedding_model: str = "BAAI/bge-m3"  # 本地跑,无供应商 embedding API

    # 文本模型默认关思考模式:路由这类场景要的是低延迟,不是长篇推理。
    disable_thinking_for_text: bool = True

    # --- 调度与调用鲁棒性 -------------------------------------------------
    # 熔断上限:Supervisor 转来转去超过这个步数就停,防死循环烧额度。
    supervisor_recursion_limit: int = Field(default=8, ge=1)
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)  # 0 = 不重试
    llm_retry_base_delay_s: float = Field(default=1.0, ge=0)  # 0 = 测试里免等待

    # --- 文件大小限制(单位:MB)-----------------------------------------
    drawing_max_mb: float = Field(default=20.0, gt=0)  # DXF 图纸
    document_max_mb: float = Field(default=10.0, gt=0)  # PDF/DOCX/TXT/MD
    photo_max_mb: float = Field(default=10.0, gt=0)  # 工地照片原图
    photo_compress_target_mb: float = Field(default=4.0, gt=0)  # 压到多大再喂视觉模型
    photo_compress_max_edge_px: int = Field(default=2048, ge=1)  # 长边像素上限

    # --- 缓存 -------------------------------------------------------------
    llm_cache_enabled: bool = True
    # 提示词版本号,是 LLM 缓存键的组成部分:改了提示词就把它 +1,
    # 老缓存自然失效,不会拿旧提示词的答案糊弄人。
    prompt_version: str = "v1"

    # --- 数据根目录 -------------------------------------------------------
    # 相对路径按进程工作目录解析。测试里由 GYT_DATA_DIR 指到 tmp_path。
    data_dir: Path = Path("data")

    # --- 评测门槛(0~1)---------------------------------------------------
    eval_threshold_routing: float = Field(default=0.90, ge=0.0, le=1.0)
    eval_threshold_safety: float = Field(default=0.80, ge=0.0, le=1.0)
    eval_threshold_rag: float = Field(default=0.80, ge=0.0, le=1.0)

    # --- 评测最小样本量(条)-----------------------------------------------
    # 光有门槛是不够的:可判分的行只剩 1 条时,「100%(1/1)」照样是 PASS + exit 0,
    # 门槛形同虚设。这三个数是方案定的下限(eval/README.md 第一节第 3 条),
    # 低于它跑分脚本会判 FAIL 并说清「这个百分比不作数」。
    eval_min_rows_routing: int = Field(default=20, ge=1)
    eval_min_rows_safety: int = Field(default=30, ge=1)
    eval_min_rows_rag: int = Field(default=20, ge=1)

    # ------------------------------------------------------------------
    # 派生路径(只读 property)
    #
    # 目录树布局 —— 访问哪个属性就自动建哪个目录,调用方不用自己 mkdir:
    #
    #   data_dir/                  <- GYT_DATA_DIR,默认 ./data
    #     |
    #     +-- uploads/             <- uploads_dir    用户上传的原始文件
    #     +-- artifacts/           <- artifacts_dir  产物注册表落盘(core/artifacts.py)
    #     +-- cache/               <- cache_dir      LLM 响应缓存(core/llm.py)
    #     +-- chroma/              <- chroma_dir     向量库持久化(RAG)
    #     +-- gyt.sqlite3          <- sqlite_path    业务库文件
    #                                 (只保证父目录存在,不预先创建空文件,
    #                                  留给 sqlite 自己建,免得建出个坏库)
    # ------------------------------------------------------------------

    @property
    def uploads_dir(self) -> Path:
        """用户上传原始文件的落地目录(访问即创建)。"""
        return _ensure_dir(self.data_dir / _UPLOADS_SUBDIR)

    @property
    def artifacts_dir(self) -> Path:
        """产物(照片/图纸/文档/报告)注册表的落盘根目录(访问即创建)。"""
        return _ensure_dir(self.data_dir / _ARTIFACTS_SUBDIR)

    @property
    def cache_dir(self) -> Path:
        """LLM 响应缓存目录(访问即创建),一个缓存键一个文件。"""
        return _ensure_dir(self.data_dir / _CACHE_SUBDIR)

    @property
    def chroma_dir(self) -> Path:
        """向量库持久化目录(访问即创建)。"""
        return _ensure_dir(self.data_dir / _CHROMA_SUBDIR)

    @property
    def sqlite_path(self) -> Path:
        """业务 SQLite 文件路径。只保证父目录存在,文件本身交给 sqlite 创建。"""
        return _ensure_dir(self.data_dir) / _SQLITE_FILENAME


@lru_cache
def get_settings() -> Settings:
    """返回全进程共享的配置单例。

    带 ``lru_cache``:第一次调用解析环境变量与 .env,之后直接返回同一个对象。
    测试中改了环境变量要立刻生效,请先调 ``get_settings.cache_clear()``。
    """
    return Settings()
