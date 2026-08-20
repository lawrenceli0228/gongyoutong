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
# ALLOWED_CAD_EXT 专指「ezdxf 能结构化解析的」——图层/构件/标注读数只有这一支给得了。
ALLOWED_CAD_EXT: Final[frozenset[str]] = frozenset({".dxf"})
# ALLOWED_DRAWING_EXT 是「图纸上传/CAD Agent 认」的全集:DXF + PDF。
# PDF 图纸(多为 AutoCAD/天正「打印成 PDF」的矢量件)只能出预览 + 读图上文字,
# **拿不到结构化图层/构件/标注对象**(那是 DXF 专有)——工具层按后缀分流、如实说清。
ALLOWED_DRAWING_EXT: Final[frozenset[str]] = ALLOWED_CAD_EXT | frozenset({".pdf"})

# 派生目录/文件名。集中在这里,避免"uploads"这种字符串散落各处。
_UPLOADS_SUBDIR: Final[str] = "uploads"
_ARTIFACTS_SUBDIR: Final[str] = "artifacts"
_CACHE_SUBDIR: Final[str] = "cache"
_CHROMA_SUBDIR: Final[str] = "chroma"
_CAD_INDEX_SUBDIR: Final[str] = "cad_index"
_PROJECTS_SUBDIR: Final[str] = "projects"  # 按项目组织的图纸/资料(人类可读镜像,W7 CAD 泳道)
_GLOBAL_SUBDIR: Final[str] = "global"  # 全局规范落地(所有项目通用)
# demo/ 跟上面那几个不是一类东西:上面全是**运行期产生**的可写目录(访问即创建),
# 它是**随仓库走的只读源资产**(规范 PDF / 演示图纸 / 演示照片,进 git)。
# 放在 data_dir 底下是为了让本机 <仓库根>/data/demo 与容器 /app/data/demo 自动对齐 ——
# compose 把仓库根 ./data 整个挂进 /app/data,demo 跟着一起进去,不必再单开一个配置项。
_DEMO_SUBDIR: Final[str] = "demo"
_SQLITE_FILENAME: Final[str] = "gyt.sqlite3"


_ENSURED_DIRS: set[Path] = set()
"""已经建过的目录。**这是个性能护栏,不是缓存语义。**

为什么需要:下面那几个 `*_dir` 属性是「访问即创建」的,而 `llm._cache_read` /
`_cache_write` 每次调用都会访问 `cache_dir` —— 也就是每问一句话就 mkdir 好几次。
在 async 上下文里这是同步阻塞 IO,`langgraph dev` 的 blockbuster 会直接抛
BlockingError,把整条缓存链路打断(表现:每次都真调模型,而日志只说 run succeeded)。

代价说清楚:目录若在进程运行期间被外部删掉(比如有人 rm -rf data/),
这里不会重建。权衡下来可以接受 —— 那属于异常运维操作,而每次问答都做几次
无谓 syscall 是常态开销。
"""


def _ensure_dir(path: Path) -> Path:
    """确保目录存在(含各级父目录)并返回它。同一个目录只真的建一次。

    失败时不静默吞掉:先记日志(带真实系统错误),再抛出一句工人看得懂的中文。
    """
    if path in _ENSURED_DIRS:
        return path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 磁盘满、只读挂载、权限不足等
        logger.error("创建数据目录失败: path=%s, err=%s", path, exc)
        raise RuntimeError(f"数据目录创建失败,请检查磁盘空间和目录权限:{path}") from exc
    _ENSURED_DIRS.add(path)
    return path


def _default_data_dir() -> Path:
    """数据根目录的默认值 —— 按**本文件所在的位置**推导仓库根,不按进程工作目录。

    parents 的索引是这么数出来的(本文件是 ``<仓库根>/backend/src/gyt/config.py``):

        Path(__file__).resolve()  = <仓库根>/backend/src/gyt/config.py
                       parents[0] = <仓库根>/backend/src/gyt
                       parents[1] = <仓库根>/backend/src
                       parents[2] = <仓库根>/backend
                       parents[3] = <仓库根>            ← 要的就是这一层

    原来这里写的是 ``Path("data")`` —— **相对路径按进程工作目录解析**,于是同一份代码
    在两处启动就落到两个地方:

        make dev(Makefile 里先 cd backend)  ──►  <仓库根>/backend/data/
        docker compose(挂仓库根 ./data)     ──►  <仓库根>/data/

    后果不是"多个空目录"这么轻:LLM 缓存、SQLite 台账、Chroma 向量库、产物注册表
    四样东西各有两份、互相看不见。换一种方式启动,等于把预热好的视觉缓存整个用不上,
    演示当场每张照片都要真调模型(慢 + 花钱),而日志里一句异常都没有。

    本机现在还留着事故现场(2026-08-09 实测,数的是**文件**、已排除 .DS_Store):
    ``backend/data/cache`` 762 个、``<仓库根>/data/cache`` 779 个;按内容哈希比对,
    前者的 762 个在后者里一个不缺,后者另有 17 个是之后新产生的。
    两处还各有一份 ``gyt.sqlite3``,里面是同样的三行任务 —— 这份"两边长得一样"的
    陈年快照差点把真机验收变成假绿灯:``scripts/live_acceptance.py`` 曾写死读
    ``backend/data`` 那份,四条库级断言逐字命中、全绿,而那一轮其实什么都没验
    (已改成从本配置取 ``sqlite_path``)。
    ⚠️ 上面这几个数字会随着继续跑评测而变,别当成断言去测;它们只是留个现场。

    容器里**不靠**这个默认值:Dockerfile 写死 ``ENV GYT_DATA_DIR=/app/data``,
    环境变量优先级高于字段默认值(见 Settings 的优先级表),照样盖得住 ——
    ``default_factory`` 只在"所有配置来源都没给这个字段"时才被调用,不挡 env。
    这一条是必须的而不是保险:容器的构建上下文是 ``backend/``、``COPY . /app``,
    代码落在 ``/app/src/gyt/config.py``,比本机少一层,parents[3] 会算成 ``/`` →
    ``/data``,是错的。所以容器里那行 ENV 不许删。
    """
    return Path(__file__).resolve().parents[3] / "data"


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

    # 文本档的采样温度。**默认 0 = 尽可能确定**。
    #
    # 为什么必须是 0(2026-08-07 起全栈实测后加的):文本档承担的是**执行类**任务 ——
    # Supervisor 判断"这活派给谁"、子 Agent 把工具结果转述成人话。这两件事都不需要
    # 任何创造性,而不确定性在这里是纯粹的伤害:同一张照片跑两次,
    #   · 一次 safety 正常调工具,一次它说"我已经把话传给 safety 了"然后把活推回去;
    #   · 一次如实转述"这照片不像工地,是不是发错了",一次说成"没有发现明显安全隐患"。
    # 后者尤其危险 —— 「不是工地」被说成「没有隐患」,语义完全变了,
    # 而工人会据此以为现场是安全的。
    #
    # 演示日更受不了这个:同一张图可能对可能错,等于没法预演。
    #
    # ⚠️ 只作用于**文本档**。视觉档(kimi-k3)官方明确要求
    # 「temperature/top_p/n/presence_penalty/frequency_penalty 是固定值,请从请求里省略」,
    # 传了可能 400,所以 get_chat_model 里只在 purpose=="text" 时注入。
    text_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # --- 调度与调用鲁棒性 -------------------------------------------------
    # 熔断上限:Supervisor 转来转去超过这个步数就停,防死循环烧额度。
    supervisor_recursion_limit: int = Field(default=8, ge=1)
    # 客户端**自己传**的 recursion_limit 上限。超过就拒(backend/auth.py)。
    # 为什么需要它:上面那个 8 是编译时 .with_config 钉的,而 2026-08-11 安全复核实测
    # ——调用时 config 里的 recursion_limit **会盖掉它**(传 60 就真跑 60 步)。
    # 于是「限流限住了次数」这句话不成立:次数有上限,单次成本却由客户端说了算。
    # 默认取 supervisor_recursion_limit 的两倍:留一点余量给正当的长任务,
    # 又不至于让好奇的测试者一发把额度打空。改它之前先想清楚要防的是谁。
    max_client_recursion_limit: int = Field(default=16, ge=1)
    # 150 而不是契约 v1 写的 60 —— **对契约的刻意偏离,需团队追认**(同 max_retries 那处)。
    # 2026-08-07 真调 kimi-k3 实测三张,视觉判断比文本慢一个量级:
    #     1600×1067 办公室(一眼判定不是工地)      10.1 秒
    #       440×293 工地(要逐项分辨违规)          42.9 秒
    #      4000×2430 工地(手机原图尺寸,超 4K 线)  59.7 秒  ← 距 60 秒只剩 0.26 秒
    # 也就是说 60 秒不是"余量小",是**演示日随便一张手机照片就会 TIMEOUT**。
    # 延迟同时受尺寸与判断复杂度影响,两者都会把工地照片推向上限。
    #
    # ⚠️ 改这个值只让**路径甲**(Agent 对话)的缓存失效,**不影响视觉缓存**。
    #    两条路径的键构成不同,实测确认过(2026-08-07):
    #      路径甲 GytDiskCache 的键含 llm_string,而 llm_string 里有 request_timeout;
    #      路径乙 llm.ainvoke 的键 = (model, prompt_version, messages, extra),**不含 timeout**。
    #    Safety 的视觉调用走路径乙,所以改这个数字**不会**冲掉预热好的照片缓存。
    #    (这里原先写的是"会让全部缓存失效",是错的,TODO-11 的演示日铁律据此修正过。)
    #    真正会冲掉视觉缓存的是:改 vision_prompt.md 的正文、改 prompt_version、换模型。
    llm_timeout_s: float = Field(default=150.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)  # 0 = 不重试
    llm_retry_base_delay_s: float = Field(default=1.0, ge=0)  # 0 = 测试里免等待

    # --- 慢调用告警阈值(纯观测,不改任何行为)---------------------------
    # 超过阈值的调用会从 info 抬到 warning,好在满屏日志里一眼捞出来。
    # 由来:2026-08-20 线上一次 213 秒的请求,日志里只有 httpx 那几行,
    # 只能靠相邻两行的时间差倒推,而**倒推不出「那 198 秒是在调用里面还是之前」**。
    # 现在 core/timing.py 把这个数直接记出来,它本身就是判据(见那个文件的头注)。
    #
    # 30 秒的来历:识图那一跳实测 5.6~27 秒都属正常(照片复杂度差异),
    # 文本档 5.8~16.4 秒。取 30 是"正常波动的上沿再留点余量",
    # 调小会把正常的识图刷成 warning,调大会漏掉真正的异常。
    llm_slow_warn_s: float = Field(default=30.0, gt=0)
    # 工具那一侧的同款阈值。20 秒:识图工具本身含一次模型调用,所以比上面松不了多少;
    # 其余工具(台账、隐患入库、DXF 解析)正常都在 1 秒内,超 20 秒一定有事。
    tool_slow_warn_s: float = Field(default=20.0, gt=0)

    # --- 文件大小限制(单位:MB)-----------------------------------------
    # DXF 图纸:默认 100MB。原来是 20→64,但真实施工图**一层就 20 多兆**(2026-08-09 实测反馈),
    # 且整栋合图 / 带光栅底图的 DXF 常破 64MB(2026-08-13 反馈),统一放到 100MB 与规范持平。
    # 上限存在的成本是:上传走 base64 进聊天消息(体积 ×约 4/3),100MB 图 → 约 133MB 报文,
    # ingest_uploads 解码落盘后**立即**把大块从 state 里剔除(换成图纸编号),只是一次瞬时内存峰值,
    # 不长期占用。还嫌小就 .env 里调 GYT_DRAWING_MAX_MB,不改代码。
    drawing_max_mb: float = Field(default=100.0, gt=0)  # DXF 图纸
    # 规范/任务书 PDF:默认 100MB(2026-08-13 与图纸拉平)。国标规范扫描件动辄几十兆,
    # 原来的 10MB 会把整本规范挡在门外;.env 里调 GYT_DOCUMENT_MAX_MB 可覆盖。
    document_max_mb: float = Field(default=100.0, gt=0)  # PDF/DOCX/TXT/MD
    photo_max_mb: float = Field(default=10.0, gt=0)  # 工地照片原图
    # DXF 预览/导出渲染的图元数上限:超过就**不渲染**、如实告知(改查图层/尺寸/构件)。
    # 2026-08-20 从 1000 提到 4000:渲染后端从 matplotlib 换成 ezdxf 原生 SVG 后端
    # (见 render.py),同图快约 7x,1000 那道旧闸把绝大多数真图挡在门外已无必要。
    # 为什么不无脑拉到几万:换后瓶颈在 SVG→PDF(svglib),实测大致线性 ——
    # 3000 图元约 9s、9000 约 54s、18000 约 110s。上限本质是**同步请求的墙钟预算**:
    # 4000 图元约 12s 内可控,再高就有超时风险(而且大地坐标系的真图常渲成空白,不值当等)。
    # 单机联调想看更大的图,.env 里调 GYT_DRAWING_RENDER_MAX_ENTITIES 覆盖即可。
    drawing_render_max_entities: int = Field(default=4000, ge=1)
    # PDF 图纸「文字选不中」(文字转图形/扫描件)时,渲染成图交给视觉模型认字用的栅格化倍率
    # (agents/cad/vision.py)。比预览的 _PDF_RENDER_SCALE(2.0)高:小字更清、识别更准。
    # 注意:VLM 输入普遍会被降采样到 ~1568px,一张 A1 大图的小字仍可能糊 —— 再高倍率也救不回,
    # 这是「整图一发」的根本限制(要精读得上分块,不在本期)。
    cad_ocr_render_scale: float = Field(default=3.0, gt=0)
    photo_compress_target_mb: float = Field(default=4.0, gt=0)  # 压到多大再喂视觉模型
    photo_compress_max_edge_px: int = Field(default=2048, ge=1)  # 长边像素上限

    # --- 打卡(W7)---------------------------------------------------------
    # 打卡是直连接口(不走 LLM),但常量入口不破例:全部从这里取,理由同全局。
    # 水印字体:空 = 由 attendance/watermark.py 按候选表探测(Debian 的 WQY、macOS 的
    # PingFang 等)。这里只留"显式覆盖"一个口子,候选表不进配置 —— 那是代码携带的
    # 平台知识,放 .env 里没人会改,反而多一处会漂的拷贝。
    attendance_watermark_font: str = Field(default="")
    # 凭证图长边上限。**这不是画质旋钮,是 1.9GB VPS 上的内存闸**(W7 §3.4):
    # 4000×3000 解开约 36MB,突发 10 人同时打卡会 OOM 掉整个 backend 容器。
    # 缩到 1600 后单张位图约 7MB,同时水印字号才能相对图尺寸固定。
    # ⚠️ 与上面 photo_compress_max_edge_px(喂视觉模型用)是两个旋钮,别合并:
    # 一个管"识图够不够看",一个管"凭证内存与观感",调整动机完全不同。
    attendance_max_edge_px: int = Field(default=1600, ge=320)
    # 解码像素上限(解压炸弹闸)。设得宽:1 亿像素的手机全景图是真实输入,
    # 挡的是"几 KB 的字节声称自己是 30 亿像素"这种恶意构造;
    # 正常大图的内存问题由上面的缩图 + watermark.py 的 draft() 解决,不靠这条。
    attendance_decode_max_pixels: int = Field(default=200_000_000, ge=1_000_000)
    # 凭证图留存天数,到期由清理器删图(行保留,artifact_id 置 NULL)。
    # 90 是工程缺省,不是合规结论 —— PDPO 的留存政策仍欠(TODOS.md 的 TODO-37)。
    attendance_retention_days: int = Field(default=90, ge=1)
    # 限流两层(W7 §3.6)。全局桶是**唯一的真闸**(一个班组的量级);
    # worker 桶只防手抖连点 —— worker_name 由客户端提供,**不是安全控制**。
    attendance_rate_per_minute: float = Field(default=30.0, gt=0)
    attendance_rate_burst: int = Field(default=10, ge=1)
    attendance_worker_rate_per_minute: float = Field(default=6.0, gt=0)
    attendance_worker_rate_burst: int = Field(default=3, ge=1)
    # GET /checkin/recent 的返回条数上限(缺省与 clamp 都用它):凭证列表是给
    # "刚打完卡看一眼"用的,不是报表 —— 翻旧账走查询 Agent。
    attendance_recent_limit: int = Field(default=20, ge=1)
    # 查询侧"某天明细"最多返回多少个工人(D10 允许无限打卡,不设上限就可能几百行)。
    attendance_query_max_workers: int = Field(default=50, ge=1)
    # 清理器老化窗口(小时):register 成功但 INSERT 还没落库的图,在这个窗口内
    # **不许删**(W7 §3.9)。1 小时远大于任何一次请求的寿命,又远小于留存天数。
    attendance_cleanup_min_age_h: float = Field(default=1.0, gt=0)

    # --- 监理隐患(W9)-----------------------------------------------------
    # supervision Agent 列清单时最多列多少条(**只是列出来的行数,不是筛出来的条数**:
    # 「一共还有几条」照实报,截断只影响清单长度,见 agents/supervision/tools.py)。
    # 这是**上下文闸**不是业务上限:一屏几百行隐患既挤爆模型上下文,人也读不完 ——
    # 真要逐条看走界面。50 远大于一次巡检能出的隐患数。
    supervision_list_max_rows: int = Field(default=50, ge=1)

    # --- 知识库(RAG)-----------------------------------------------------
    # 方案 B 启动预置:开则起服务时若规范索引缺失/有改动,自动建库(agents/knowledge/ingest)。
    # **默认 False**——两个原因:① 保护测试(测试 chroma_dir 是 tmp,一开必触发 2.2GB 建库);
    # ② 首次建库会阻塞启动约 15 分钟,该由 dev/生产显式接受。
    # 用法:dev 在 .env、生产在 compose 里设 GYT_KNOWLEDGE_PREBUILD_AT_STARTUP=true;
    # 生产更推荐的是「镜像构建期烤索引」(同 BGE-M3 权重的烤法),那样零冷启动。
    knowledge_prebuild_at_startup: bool = False
    # 检索返回多少条候选(top-k)。真图 top-1 不一定是答案条文(条文说明常排更前),
    # 给 agent 几条挑,别只给一条。
    knowledge_top_k: int = Field(default=5, ge=1)
    # 「无依据」判定阈值:Chroma 返回的是**距离(越小越近)**;最近的一条都比这个还远
    # 就判「知识库没有」,不许硬答。默认值按 GB50016 语料标定(正例命中 ~0.35-0.6,
    # 无关问题更大)。换语料/换 embedding 要重标。
    knowledge_max_distance: float = Field(default=0.85, gt=0)

    # --- 缓存 -------------------------------------------------------------
    llm_cache_enabled: bool = True
    # 提示词版本号,是 LLM 缓存键的组成部分:改了提示词就把它 +1,
    # 老缓存自然失效,不会拿旧提示词的答案糊弄人。
    # v2:W7 CAD/knowledge 改了 cad prompt 与 knowledge 工具描述,老缓存作废
    prompt_version: str = "v2"

    # --- 对外访问闸门(鉴权 + 限流;消费者是 backend/auth.py)---------------
    #
    # 这三个字段只在「把服务开给外部的人测试」时才有意义。本机联调不用管,
    # 默认值就是「关」。判据与整套门的分工写在 backend/auth.py 的文件头,不在这里重复。
    #
    # repr=False 与两把 API Key 同理:挡住 repr()/str() 顺手把口令打进日志。
    # 空串 = 没配 = **鉴权与限流一起关**。
    #
    # ⚠️ 「设了才开」的弱点是「上公网忘了设 = 裸奔且一声不吭」。这一层兜不住它,
    #    兜底在部署件:compose 用 ${GYT_ACCESS_TOKEN:?} 让「没给」变成起不来。
    access_token: str = Field(default="", repr=False)

    # 创建 run 的令牌桶参数。**只卡创建 run**,读线程/列历史不卡(那些不花钱,
    # 测试的人翻记录不该被打断)。recursion_limit 限的是单次 run 的**步数**,
    # 跟这里限的**次数**是两回事,别拿一个当另一个用。
    #
    # 默认值 20 / 20 是这么定的(两条都是可核对的依据,不是拍脑袋):
    #   突发 20:backend/scripts/live_acceptance.py 里 ask() 有 **17** 个调用点
    #            (其中 1 个在 A10 的条件分支里;另外 ask() 内部拿不到 messages 会
    #            重发一次,所以最坏能到 34 个 run)。取 20 是为了万一有人在本机也
    #            设了令牌,那一整轮真机验收不至于开头就被自己人拦下。
    #            ⚠️ 不要把这句读成「20 保证验收一定跑得完」—— 保证它跑得完的是下面
    #            那条补充速率:脚本是**串行**的,每一轮都要等 runs/wait 返回。
    #   每分钟 20:上面 llm_timeout_s 那段记着 2026-08-07 的实测 —— 一张工地照片
    #            端到端要 10.1 / 42.9 / 59.7 秒。也就是说一个人**物理上**不可能
    #            持续超过约 6 轮/分钟(还没算他读答案的时间),串行脚本同理。
    #            20 留了三倍余量,够两三个人同时试;而狂刷会被压到 20×60 = 1200
    #            次/小时这个**有上限**的量级,而不是无限。
    # 觉得紧就在部署那侧调环境变量,不要改代码。
    rate_limit_burst: int = Field(default=20, ge=1)
    rate_limit_per_minute: float = Field(default=20.0, gt=0)

    # --- 数据根目录 -------------------------------------------------------
    # 默认值按**本文件位置**推导出的仓库根(见 _default_data_dir),**不是**相对路径 ——
    # 相对路径会跟着进程工作目录跑,`make dev`(在 backend/ 下跑)和容器因此各写一份数据。
    # 环境变量 GYT_DATA_DIR 照样能覆盖:容器靠它指到 /app/data,测试靠它指到 tmp_path。
    data_dir: Path = Field(default_factory=_default_data_dir)

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
    # 目录树布局 —— 除 demo/ 外,访问哪个属性就自动建哪个目录,调用方不用自己 mkdir:
    #
    #   data_dir/                  <- GYT_DATA_DIR,默认 <仓库根>/data(本机与容器同一份)
    #     |
    #     +-- uploads/             <- uploads_dir    用户上传的原始文件
    #     +-- artifacts/           <- artifacts_dir  产物注册表落盘(core/artifacts.py)
    #     +-- cache/               <- cache_dir      LLM 响应缓存(core/llm.py)
    #     +-- chroma/              <- chroma_dir     向量库持久化(RAG)
    #     +-- cad_index/           <- cad_index_dir  CAD 图纸解析索引落盘(agents/cad/index.py)
    #     +-- projects/            <- projects_dir   按项目组织的图纸/资料(人类可读镜像,W7 CAD)
    #     +-- global/              <- global_dir     全局规范落地(所有项目通用)
    #     +-- gyt.sqlite3          <- sqlite_path    业务库文件
    #     |                           (只保证父目录存在,不预先创建空文件,
    #     |                            留给 sqlite 自己建,免得建出个坏库)
    #     |
    #     +-- demo/                <- demo_assets_dir  ★ 规矩相反:**只读源资产,不自动创建**
    #           +-- docs/              规范原文 PDF(knowledge 建库的输入)
    #           +-- drawings/          演示 DXF 图纸 + names.json(cad 启动预注册的输入)
    #           +-- photos/            演示照片
    #
    # 上面那几个是**运行期产物**,建出来天经地义;demo/ 是随仓库走、进 git 的源资产,
    # 它不存在等于"资产没跟过来 / 容器没挂上卷",必须让调用方自己撞见并报出来。
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
    def cad_index_dir(self) -> Path:
        """CAD 图纸解析索引落盘目录(访问即创建),一张图一份 <drawing_id>.json。"""
        return _ensure_dir(self.data_dir / _CAD_INDEX_SUBDIR)

    @property
    def projects_dir(self) -> Path:
        """按项目组织的图纸/资料目录(访问即创建),供上传面板落人类可读镜像(W7 CAD 泳道)。"""
        return _ensure_dir(self.data_dir / _PROJECTS_SUBDIR)

    @property
    def global_dir(self) -> Path:
        """全局规范落地目录(访问即创建),存所有项目通用的国标/通用规范。"""
        return _ensure_dir(self.data_dir / _GLOBAL_SUBDIR)

    @property
    def sqlite_path(self) -> Path:
        """业务 SQLite 文件路径。只保证父目录存在,文件本身交给 sqlite 创建。"""
        return _ensure_dir(self.data_dir) / _SQLITE_FILENAME

    @property
    def demo_assets_dir(self) -> Path:
        """演示源资产根目录(规范 PDF / 演示图纸 / 演示照片),随仓库走、进 git。

        与运行期数据同在 data_dir 底下:本机 ``<仓库根>/data/demo``、容器 ``/app/data/demo`` ——
        compose 把仓库根 ./data 整个挂进去,两边自动对齐,不用再单开一个配置项。

        刻意**不**走 ``_ensure_dir`` —— 这是本类里唯一一个"访问不创建"的路径属性。
        这个目录不存在本身就是要报出来的错(资产没跟过来 / 容器忘了挂卷 / 换机器没同步),
        替调用方悄悄 mkdir 出一个空目录,只会把"资产丢了"伪装成"知识库是空的"。
        而这两件事在演示当天长得一模一样:knowledge 照样温和地回一句"规范里查不到"。
        """
        return self.data_dir / _DEMO_SUBDIR


@lru_cache
def get_settings() -> Settings:
    """返回全进程共享的配置单例。

    带 ``lru_cache``:第一次调用解析环境变量与 .env,之后直接返回同一个对象。
    测试中改了环境变量要立刻生效,请先调 ``get_settings.cache_clear()``。
    """
    return Settings()
