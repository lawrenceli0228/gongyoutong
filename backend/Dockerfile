# syntax=docker/dockerfile:1.7
# =============================================================================
# 工友通(GYT)后端镜像 —— 四阶段流水线
#
# 核心目标(复审 D1 决议):冷启动零下载。
# BGE-M3 权重(约 2.2GB)必须在"构建期"拉下来固化进镜像层,
# 运行时全程离线,演示现场断网也能起来。
# 下载源见下方 `ARG HF_ENDPOINT`(**默认是官方 huggingface.co**);
# 这里以前写着"经 hf-mirror 拉下来",2026-08-06 实测那个源已经不镜像内容、构建必失败,
# 详细的 A/B 对照记录就在 ARG 那一段 —— 别照这行注释把源改回去。
#
#   ┌──────────────────────────────────────────────────────────────────────┐
#   │ 阶段 1  base                                                          │
#   │   python:3.12-slim + 跑 torch 需要的系统库 + uv 二进制                  │
#   └───────────────┬──────────────────────────────┬───────────────────────┘
#                   │                              │
#      (只依赖依赖清单)                    (只依赖模型 ID,不碰任何源码)
#                   │                              │
#         ┌─────────▼──────────┐        ┌──────────▼─────────────────────┐
#         │ 阶段 2  deps       │        │ 阶段 3  models                 │
#         │ COPY pyproject/lock│        │ HF_ENDPOINT(默认官方源)        │
#         │ uv sync 装第三方库  │        │ snapshot_download(BAAI/bge-m3) │
#         │ 产出 /app/.venv    │        │ 产出 /opt/hf (约 2.2GB)        │
#         └─────────┬──────────┘        └──────────┬─────────────────────┘
#                   │                              │
#                   └───────────────┬──────────────┘
#                                   │
#                   ┌───────────────▼──────────────────────────────────────┐
#                   │ 阶段 4  app                                          │
#                   │  COPY 上面两份产物 → 最后一层才 COPY 源码             │
#                   │  非 root 用户 / EXPOSE 2024 / CMD langgraph dev      │
#                   └──────────────────────────────────────────────────────┘
#
# 层缓存失效路径(为什么非要拆成四段):
#
#   改业务代码 (src/**)      ──► 只失效 阶段4 最后几层        (秒级重建)
#   改依赖 (pyproject/lock)  ──► 失效 阶段2 + 阶段4           (分钟级)
#   改模型 ID / 下载参数      ──► 失效 阶段3 + 阶段4           (重下 2.2GB)
#   换基础镜像               ──► 全部失效
#
#   注意:阶段 3 绝对不能 COPY 源码,也绝对不能 FROM deps。
#   一旦这么写,改一行 Python 就会触发 2.2GB 权重重下 —— 这是本文件的命根子。
# =============================================================================


# -----------------------------------------------------------------------------
# 可调参数(集中在顶部,避免散落到各阶段里变成魔法值)
# 约定:EMBEDDING_MODEL 必须与 backend/src/gyt/config.py 里 Settings.embedding_model
#       的默认值保持一致。Dockerfile 无法 import get_settings(),只能靠这条约定维持同步。
# -----------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.12.1
ARG HUGGINGFACE_HUB_VERSION=1.26.0
# 与 Settings.embedding_model 默认值一致
ARG EMBEDDING_MODEL=BAAI/bge-m3
# 权重下载源。
#
# 2026-08-06 实测记录(别再改回 hf-mirror,除非重新验证过):
#   原本这里默认 https://hf-mirror.com,构建在 models 阶段必失败,
#   报 huggingface_hub.errors.LocalEntryNotFoundError。
#   根因不是网络不通 —— hf-mirror.com 已经不再镜像内容,
#   它对 /api/models/... 只回一个 308 跳转到 huggingface.co:
#       HTTP/1.1 308 Permanent Redirect
#       Location: https://huggingface.co/api/models/BAAI/bge-m3
#   huggingface_hub 的 HEAD 元数据探测在这个跨域跳转上拿不到结果,
#   于是判定"既下不到、本地也没有",直接抛错。
#   同一构建环境下做过 A/B 对照(两个 RUN 阶段,ENV 均在 import 前设好):
#       官方 huggingface.co = 成功
#       hf-mirror.com       = 失败 LocalEntryNotFoundError
#
# 因此默认改为官方源。若你的网络确实访问不了官方源(例如国内无代理),
# 用 --build-arg 覆盖成一个**你亲自验证过能下载的**镜像:
#     docker compose build --build-arg HF_ENDPOINT=https://<可用镜像> backend
# 验证方法:见 backend/README.md「权重下载源失效时怎么办」。
ARG HF_ENDPOINT=https://huggingface.co


# -----------------------------------------------------------------------------
# uv 官方发布的独立二进制镜像。取成一个具名阶段,后面用 COPY --from=uvbin 取二进制,
# 比 pip install uv 快,也不会污染业务 venv。
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uvbin


# =============================================================================
# 阶段 1:base —— 公共底座
# 只放"所有后续阶段都要用"的东西:系统运行库 + uv 二进制 + 全局环境变量。
# 这一层要尽量稳定,任何变动都会让下游三个阶段全部重建。
# =============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

COPY --from=uvbin /uv /uvx /bin/

# UV_LINK_MODE=copy       容器里没有 hardlink 优势,copy 模式可避免跨文件系统告警
# UV_PYTHON_DOWNLOADS=never 禁止 uv 自作主张下载 Python,统一用基础镜像自带的解释器
# UV_PROJECT_ENVIRONMENT   固定 venv 位置,方便阶段 4 整个拷走
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# libgomp1     : torch / onnxruntime 依赖的 OpenMP 运行时,slim 镜像默认没有,缺了 import 就炸
# ca-certificates: 拉模型和调用 DeepSeek / Moonshot 的 https 请求必需
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install --no-install-recommends -y \
        ca-certificates \
        libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app


# =============================================================================
# 阶段 2:deps —— 只装第三方依赖
# 关键顺序:先 COPY 依赖清单再 sync,源码要到阶段 4 才出现。
# --no-install-project = 只装依赖不装 gyt 自身,这样改源码这一层照样命中缓存。
# =============================================================================
FROM base AS deps

# uv.lock 用通配符是为了在 lock 尚未提交时也能构建。
# Docker 的规则是"只要至少有一个源匹配就不报错",所以 pyproject.toml 保底。
COPY pyproject.toml uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        echo "[构建] 使用 uv.lock 锁定版本安装依赖" ; \
        uv sync --frozen --no-install-project --no-dev ; \
    else \
        echo "[构建] 警告:未找到 uv.lock,退化为在线解析依赖。" ; \
        echo "[构建] 请尽快提交 backend/uv.lock,否则构建结果不可复现。" ; \
        uv sync --no-install-project --no-dev ; \
    fi


# =============================================================================
# 阶段 3:models —— 独立的权重预下载层(冷启动零下载的命根子)
# 本阶段 FROM base(不是 FROM deps),且不 COPY 任何源码,
# 所以它的缓存只在"模型 ID / 下载脚本 / 基础镜像"变化时才失效。
# =============================================================================
FROM base AS models

ARG EMBEDDING_MODEL
ARG HF_ENDPOINT
ARG HUGGINGFACE_HUB_VERSION

# HF_HOME                     权重落盘位置,阶段 4 会整份拷走
# HF_HUB_DISABLE_PROGRESS_BARS 关掉进度条,否则构建日志被刷屏
# GYT_BUILD_EMBEDDING_REPO     传给下面的 python 脚本
#                              (heredoc 内不做 shell 展开,统一走环境变量读取)
ENV HF_ENDPOINT=${HF_ENDPOINT} \
    HF_HOME=/opt/hf \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    GYT_BUILD_EMBEDDING_REPO=${EMBEDDING_MODEL}

# 单独装 huggingface_hub,不复用 deps 的 venv —— 让本阶段与业务依赖彻底解耦
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system "huggingface_hub==${HUGGINGFACE_HUB_VERSION}"

# 只拉推理真正要用的文件:
#   - 跳过 onnx/ (另一份约 2GB 的 ONNX 权重,本项目走 PyTorch 路径)
#   - 跳过 imgs/ 与 README 里的示意图,纯文档资源
# 下载失败必须让构建红掉,绝不能产出"看起来成功、运行时才现下"的镜像。
RUN python <<'PY'
import os

from huggingface_hub import snapshot_download

repo_id = os.environ["GYT_BUILD_EMBEDDING_REPO"]
endpoint = os.environ.get("HF_ENDPOINT") or "(默认 huggingface.co)"
print(f"[构建] 开始预下载嵌入模型权重:{repo_id}", flush=True)
print(f"[构建] 下载源:{endpoint}", flush=True)
try:
    local_path = snapshot_download(
        repo_id=repo_id,
        ignore_patterns=["onnx/*", "imgs/*", "*.jpg", "*.jpeg", "*.png", "*.webp"],
        max_workers=4,
    )
except Exception as exc:
    # 这里刻意把排查路径直接印在构建日志里 —— 这个失败点离演示日最近,
    # 而报错原文(LocalEntryNotFoundError)会把人误导到"检查网络"的死胡同上。
    print(
        "\n[构建] 权重下载失败,镜像不会产出。\n"
        f"[构建]   下载源:{endpoint}\n"
        f"[构建]   错误:  {type(exc).__name__}: {exc}\n"
        "[构建] 排查顺序:\n"
        "[构建]   1. 先确认这个源是不是真的在提供文件,而不是只回一个跳转:\n"
        f"[构建]      curl -sI {endpoint}/api/models/{repo_id} | head -3\n"
        "[构建]      若看到 308/301 且 Location 指向别的域名,这个源就是失效的\n"
        "[构建]      (hf-mirror.com 在 2026-08 就是这样坏掉的)。\n"
        "[构建]   2. 换一个你验证过的源重建:\n"
        "[构建]      docker compose build --build-arg HF_ENDPOINT=https://<可用源> backend\n"
        "[构建]   3. 完全无法联网时,见 backend/README.md 的离线导入权重方案。\n",
        flush=True,
    )
    raise
print(f"[构建] 权重已固化到镜像层:{local_path}", flush=True)
PY

# 冒烟校验:核心权重与分词器缺一不可,缺了就让构建失败,别把问题留到演示现场
RUN python <<'PY'
import pathlib
import sys

root = pathlib.Path("/opt/hf")
missing = [name for name in ("config.json", "tokenizer.json") if not any(root.rglob(name))]
weights = list(root.rglob("pytorch_model.bin")) + list(root.rglob("*.safetensors"))
if missing or not weights:
    print(f"[构建] 模型预下载校验失败,缺失:{missing or '权重文件'}", file=sys.stderr)
    sys.exit(1)
total_mb = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) / 1024 / 1024
print(f"[构建] 模型缓存校验通过,合计约 {total_mb:.0f} MB", flush=True)
PY


# =============================================================================
# 阶段 4:app —— 最终运行镜像
# COPY 顺序刻意从"最不常变"到"最常变":venv → 模型 → 源码,
# 这样日常改业务代码只会重建最后两三层。
# =============================================================================
FROM base AS app

# 中文水印字体(W7 打卡):Debian 的文泉驿微米黑,attendance/watermark.py 候选表首位
# (/usr/share/fonts/truetype/wqy/wqy-microhei.ttc)。缺了它容器里渲染水印会
# FontUnavailableError 硬失败 —— 那是故意的,绝不退回 ASCII 位图字体画方块。
# ⚠️ 只装在 app 阶段,**绝不挪进 base**:阶段 3 是 `FROM base AS models`(:144),
# base 一动它缓存全失效,2.2GB 的 BGE-M3 权重重下一遍(W7 §3.4 ②,两轮复审确认)。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install --no-install-recommends -y \
        fonts-wqy-microhei \
 && rm -rf /var/lib/apt/lists/*

# HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE
#   运行期强制离线:任何试图联网拉模型的代码会立刻报错,
#   而不是在演示现场悄悄卡住等下载。这就是"冷启动零下载"的运行期保险丝。
# GYT_DATA_DIR
#   对应 Settings.data_dir;compose 会把宿主 ./data 挂到这里
# TZ
#   **不设的话容器跑在 UTC 上,而工地在中国(CST = UTC+8)。**
#   agents/schedule/tools.py 用 date.today() 取"今天",再由 dates.py 把
#   「明天」「后天」「下周三」算成具体日期 —— 时区错了,整条日期链跟着错。
#
#   错的是哪一段:UTC 落后 CST 8 小时,所以**中国时间每天 00:00–07:59**,
#   容器还停在前一个日历日。工友一早交班时说"明天绑扎钢筋",系统按前一天 +1,
#   记成的是**今天**。而它一句错都不报,台账上就是个看着很正常、实际早了一天的日期。
#   (剩下 16 小时两边同日,所以这个洞很难在白天的测试里撞见 —— 它专挑早班时段发作。)
#
#   2026-08-09 实测确认容器确实是裸 UTC:`date` 回 "15:39 UTC",TZ 环境变量为空。
#   ⚠️ 别拿"宿主时间和容器时间差一天"当判据 —— 那取决于**宿主**在哪个时区
#   (本机 AEST=UTC+10,当时宿主 08-10 而容器 08-09,可那一刻 CST 也是 08-09,
#    容器其实没错)。判据只有一条:容器的日期必须等于**中国时间**的日期。
#
#   基础镜像 python:3.12-slim-bookworm 自带 /usr/share/zoneinfo,不用额外装 tzdata
#   (实测 /usr/share/zoneinfo/Asia/Shanghai 在)。
ENV PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    GYT_DATA_DIR=/app/data \
    TZ=Asia/Shanghai

# 1) 第三方依赖(来自阶段 2)
COPY --from=deps /app/.venv /app/.venv
# 2) 模型权重(来自阶段 3,约 2.2GB;root 所有、755 权限,普通用户可读)
COPY --from=models /opt/hf /opt/hf
# 3) 源码与配置(改动最频繁,放最后)。.dockerignore 已排除 .venv / data / tests 等。
COPY . /app

# 把 gyt 自身装进 venv(依赖已在阶段 2 装好,这步只处理项目本身,很快)
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then uv sync --frozen --no-dev ; else uv sync --no-dev ; fi

# 启动命令依赖 langgraph CLI 与内存版服务端(langgraph-cli[inmem])。
# 它必须是主依赖而不是 dev 依赖,否则 --no-dev 装出来的镜像根本起不来。
# 这里在构建期就查,给出人话提示,免得线上 crash-loop 才发现。
RUN command -v langgraph >/dev/null 2>&1 || { \
        echo "[构建] 失败:镜像里找不到 langgraph 命令。" >&2 ; \
        echo "[构建] 请把 langgraph-cli[inmem] 放进 backend/pyproject.toml 的主依赖" >&2 ; \
        echo "[构建] (不是 dev 依赖组),否则容器无法启动服务。" >&2 ; \
        exit 1 ; \
    }
# langgraph.json 是 LangGraph 服务的图注册清单,缺了服务起不来,同样构建期拦截
RUN test -f /app/langgraph.json || { \
        echo "[构建] 失败:缺少 backend/langgraph.json。" >&2 ; \
        echo "[构建] 该文件需声明图 \"gyt\" -> ./src/gyt/graph.py:graph,并与前端 assistant id 一致。" >&2 ; \
        exit 1 ; \
    }

# 非 root 运行。只 chown 真正需要写的小目录:
#   /app        —— langgraph dev 会在工作目录下写 .langgraph_api 之类的临时状态
#   /app/data   —— 上传件 / 产物 / 缓存 / SQLite,compose 里挂载宿主目录
# 故意不 chown -R /app/.venv 与 /opt/hf:它们只读即可。
# 递归 chown 会把上千兆的内容整层复制一遍,镜像体积直接翻倍。
RUN groupadd --system gyt \
 && useradd --system --gid gyt --uid 10001 --home-dir /app --shell /usr/sbin/nologin gyt \
 && mkdir -p /app/data /app/.langgraph_api \
 && chown gyt:gyt /app /app/data /app/.langgraph_api

USER gyt

# 2024 是 LangGraph 官方约定端口,docker-compose.yml 与 frontend/.env 必须与之一致
EXPOSE 2024

# 探活统一在 docker-compose.yml 里定义(单一事实来源,避免两处配置漂移)。
# 探活端点是 LangGraph 服务自带的 GET /ok。

# --host 0.0.0.0 :容器内必须监听所有网卡,否则宿主机映射端口连不上
# --no-browser   :容器里没有浏览器可开
# --no-reload    :演示/生产不需要热重载,省内存也更稳
# --allow-blocking:BGE-M3 本地推理是同步阻塞调用,不放行会被框架判定为违规操作
CMD ["langgraph", "dev", \
     "--host", "0.0.0.0", \
     "--port", "2024", \
     "--no-browser", \
     "--no-reload", \
     "--allow-blocking"]
