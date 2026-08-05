"""E2E#7 · 冷启动冒烟(对应 T1 计划 S7、验收项 V1,复审 D1 决议)。

它守的是一条硬验收:一台干净机器上 `docker compose up` 之后,**全程不从公网拉任何
模型权重**,就能把第一条消息答出来。BGE-M3 的 2.2GB 权重必须在镜像构建期经
hf-mirror.com 预烧进镜像层;一旦有人改坏了 Dockerfile 的 models 层,权重会退化成
「运行时现下」,演示日断网就当场翻车 —— 这条用例就是专门抓这个的。

为什么默认跳过?
    它真的要起容器,耗时以分钟计,还依赖本机 Docker 和填好真 Key 的 .env。
    放进日常 `make test` 会让单测又慢又飘,所以默认 skip,
    只有显式 GYT_E2E=1 才跑(CI 的 e2e job、彩排前的每日回归会开)。

完整流程与收摊保证:

    ┌──────────────┐   ┌────────────────┐   ┌─────────────────┐   ┌────────────────┐
    │ compose up -d│──▶│ 轮询 GET /ok 就绪│──▶│ POST /runs/wait │──▶│ 查日志无下载痕迹│
    └──────────────┘   └────────────────┘   └─────────────────┘   └────────────────┘
           │                  │ 超时即失败           │ 只验结构            │ 命中即失败
           │                  │ (并附带后端日志)      │ 不验 LLM 文案字面    │
           └────────────── finally: compose down(无论成败都收摊,不留僵尸容器)──┘

断言口径:只验结构,不验 LLM 说了什么。
    大模型每次措辞都不一样,断言「回复里必须含『你好』」这种字面判断必然误报,
    维护成本还全落在后面几周。所以只断言:HTTP 200 + 有 messages 列表 +
    里面至少有一条非空的助手消息。链路通不通,这三条足够证明。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# httpx 随 langgraph-cli[inmem] 一并装上;万一环境里没有,整个模块跳过而不是让收集报错。
httpx = pytest.importorskip(
    "httpx", reason="冷启动冒烟需要 httpx(通常随 langgraph-cli[inmem] 一起装上)"
)

# --- 开关与路径 ---------------------------------------------------------------

E2E_ENV_FLAG = "GYT_E2E"
E2E_ENABLED_VALUE = "1"

# 注意:这个判断必须在「模块导入期」做完。conftest 的 autouse fixture 会清理 GYT_* 变量,
# 但它放行了 GYT_E2E(见 conftest.ENV_VARS_KEPT_DURING_TESTS),两边是配套的。
pytestmark = pytest.mark.skipif(
    os.environ.get(E2E_ENV_FLAG) != E2E_ENABLED_VALUE,
    reason=f"冷启动 E2E 默认跳过。要跑请设 {E2E_ENV_FLAG}={E2E_ENABLED_VALUE},或直接 `make e2e`",
)

# tests/e2e/test_cold_start.py -> e2e -> tests -> backend -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
LANGGRAPH_JSON = REPO_ROOT / "backend" / "langgraph.json"

# --- E2E 专属常量 -------------------------------------------------------------
# 这些值刻意不放进 gyt.config.Settings:Settings 是「跑业务用的配置」,
# 而端口、健康检查路径、compose 服务名是「测试怎么观测这套栈」的知识,
# 塞进业务配置会让线上镜像背上测试概念。需要覆盖时用下面的 GYT_E2E_* 环境变量。

BACKEND_SERVICE = "backend"  # docker-compose.yml 里的后端服务名
BACKEND_BASE_URL = os.environ.get("GYT_E2E_BASE_URL", "http://localhost:2024")
HEALTH_PATH = "/ok"  # LangGraph Server 官方健康端点(已核 langgraph-api 0.12.0 路由表)
STATELESS_RUN_PATH = "/runs/wait"  # 无状态运行并等最终结果,不用自己拼 SSE

HTTP_OK = 200

READY_TIMEOUT_S = float(os.environ.get("GYT_E2E_READY_TIMEOUT_S", "300"))
READY_POLL_INTERVAL_S = 2.0
HEALTH_PROBE_TIMEOUT_S = 5.0
RUN_TIMEOUT_S = float(os.environ.get("GYT_E2E_RUN_TIMEOUT_S", "120"))
COMPOSE_UP_TIMEOUT_S = float(os.environ.get("GYT_E2E_UP_TIMEOUT_S", "1800"))
COMPOSE_DOWN_TIMEOUT_S = 180.0
COMPOSE_LOGS_TIMEOUT_S = 60.0
DOCKER_PROBE_TIMEOUT_S = 20.0

SMOKE_PROMPT = "你好,请回一句话确认你在线。"

DOWNLOAD_MARKERS = ("downloading", "resolve/main")
"""判定「运行时在下模型权重」的两个字样(大小写不敏感)。

    downloading   —— huggingface_hub / pip / uv 下载时的通用输出
    resolve/main  —— HF 权重文件 URL 的路径特征(.../resolve/main/model.safetensors)

命中任意一条就说明权重没被烧进镜像,V1 验收不成立。
"""

ASSISTANT_MESSAGE_KINDS = frozenset({"ai", "assistant"})
LOG_TAIL_CHARS = 4000  # 报错时附带的日志尾巴长度,够定位又不刷屏


# --- 小工具 -------------------------------------------------------------------


def _tail(text: str) -> str:
    """截取文本末尾一段,用于失败信息里附日志(避免把几万行日志糊到断言里)。"""
    if len(text) <= LOG_TAIL_CHARS:
        return text
    return "...(前略)...\n" + text[-LOG_TAIL_CHARS:]


def _compose_env() -> dict[str, str]:
    """构造 docker compose 子进程用的环境(返回新 dict,不改 os.environ)。

    关键:必须把所有 GYT_* 变量剔掉再传给 compose。
    因为 conftest 的 autouse fixture 往进程环境里塞了假 Key(test-deepseek-key),
    而 docker compose 做 ${GYT_DEEPSEEK_API_KEY} 插值时,shell 环境优先级高于 .env 文件 ——
    不剔除的话,容器会拿着假 Key 起来,第一条消息必然 401,冒烟白跑还查半天。
    剔干净之后,compose 只会去读仓库根的 .env,那才是真 Key 所在。
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GYT_")}


def _run_compose(args: tuple[str, ...], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """执行一条 docker compose 子命令,不抛异常,返回结果由调用方判读。"""
    command = ("docker", "compose", "-f", str(COMPOSE_FILE), *args)
    return subprocess.run(  # noqa: S603  (命令全部来自本文件常量,无外部输入拼接)
        command,
        cwd=REPO_ROOT,
        env=_compose_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _docker_available() -> bool:
    """探一下本机有没有 docker compose,没有就跳过而不是报一堆红。"""
    try:
        probe = subprocess.run(  # noqa: S603
            ("docker", "compose", "version"),
            capture_output=True,
            timeout=DOCKER_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _backend_logs() -> str:
    """取后端容器日志(stdout+stderr 合并)。

    显式点名 backend 服务,不用不带参数的 `compose logs`:
    前端在 ui profile 里(默认不启动),但只要有人加了 --profile ui,
    它安装 npm 依赖时打印的 "Downloading" 就会让本用例稳定误报 ——
    而我们要守的是「后端不在运行时下模型权重」这一条,与前端无关。
    """
    result = _run_compose(("logs", "--no-color", BACKEND_SERVICE), timeout_s=COMPOSE_LOGS_TIMEOUT_S)
    return result.stdout + result.stderr


def _find_download_markers(logs: str) -> tuple[str, ...]:
    """挑出日志里疑似「正在下载权重」的行,返回新元组(不改入参)。"""
    markers = tuple(marker.lower() for marker in DOWNLOAD_MARKERS)
    return tuple(
        line for line in logs.splitlines() if any(marker in line.lower() for marker in markers)
    )


def _read_assistant_id() -> str:
    """从 backend/langgraph.json 读出图的 id。

    刻意不写死 "agent" 之类的字面量:图名归 L4 泳道在 langgraph.json 里定,
    这里跟着配置走,改名不会让 E2E 莫名其妙地 404。
    """
    if not LANGGRAPH_JSON.is_file():
        pytest.fail(f"找不到 {LANGGRAPH_JSON},请先完成 langgraph.json(指向 gyt.graph:graph)")
    config = json.loads(LANGGRAPH_JSON.read_text(encoding="utf-8"))
    graphs = config.get("graphs") or {}
    if not graphs:
        pytest.fail("langgraph.json 里没有配置 graphs,冷启动冒烟无从下手")
    return next(iter(graphs))


def _is_assistant_message(message: Any) -> bool:
    """判断一条消息是不是助手(AI)发的。type/role 两种键都认,兼容序列化差异。"""
    if not isinstance(message, dict):
        return False
    kind = message.get("type") or message.get("role") or ""
    return str(kind).lower() in ASSISTANT_MESSAGE_KINDS


def _message_text(message: dict[str, Any]) -> str:
    """把消息内容抽成纯文本。content 可能是字符串,也可能是多模态块列表。"""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _wait_until_ready(client: Any, *, timeout_s: float) -> None:
    """轮询健康端点直到后端就绪,超时就把最后一次失败原因连同日志如实报出来。

    轮询节奏(固定 2s 间隔,不做指数退避 —— 这里的目标是「尽早发现已就绪」,
    退避只会让本来 30s 能跑完的冒烟拖成 2 分钟):

        t=0 ─▶ GET /ok ─失败─▶ sleep 2s ─▶ GET /ok ─失败─▶ … ─▶ 超过 READY_TIMEOUT_S ─▶ fail
                  │
                  └─ 200 ─▶ 立刻返回
    """
    deadline = time.monotonic() + timeout_s
    last_error = "(还没来得及发出第一次请求)"
    while time.monotonic() < deadline:
        try:
            response = client.get(HEALTH_PATH, timeout=HEALTH_PROBE_TIMEOUT_S)
        except httpx.HTTPError as exc:
            last_error = f"连不上后端:{exc!r}"
        else:
            if response.status_code == HTTP_OK:
                return
            last_error = f"健康检查返回 {response.status_code}"
        time.sleep(READY_POLL_INTERVAL_S)
    pytest.fail(
        f"等了 {timeout_s:.0f} 秒后端仍未就绪({BACKEND_BASE_URL}{HEALTH_PATH})。"
        f"最后一次失败:{last_error}\n--- backend 日志尾部 ---\n{_tail(_backend_logs())}"
    )


# --- fixture ------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[None]:
    """把整栈拉起来,用完无论成败都收摊(module 级,两条断言共用一次启动)。"""
    if not COMPOSE_FILE.is_file():
        pytest.fail(f"找不到 {COMPOSE_FILE},冷启动冒烟依赖它")
    if not _docker_available():
        pytest.skip("本机没有可用的 docker compose,跳过冷启动冒烟")

    # 必须带 --build:本用例守的是 V1 硬验收(BGE-M3 权重烧进镜像层、冷启动零下载),
    # 验的对象就是**当前 Dockerfile 构建出来的镜像**。不加 --build 的话,
    # docker compose 会直接复用上一次留下的 gyt-backend:dev —— 有人把 models 阶段改坏了,
    # 本地照样两条断言全绿,真到演示日在干净机器上首次构建才翻车,
    # 那正好是这条用例存在的全部意义被绕过去了。
    started = _run_compose(("up", "-d", "--build"), timeout_s=COMPOSE_UP_TIMEOUT_S)
    if started.returncode != 0:
        _run_compose(("down", "--remove-orphans"), timeout_s=COMPOSE_DOWN_TIMEOUT_S)
        pytest.fail(
            f"docker compose up 失败(退出码 {started.returncode}):\n{_tail(started.stderr)}"
        )
    try:
        yield
    finally:
        # 收摊只 down 容器,不带 -v:./data 是宿主机 bind mount,里面有演示数据,不能删。
        stopped = _run_compose(("down", "--remove-orphans"), timeout_s=COMPOSE_DOWN_TIMEOUT_S)
        if stopped.returncode != 0:
            print(  # noqa: T201  (收摊失败必须让人看见,否则残留容器会占端口)
                "[警告] docker compose down 未正常退出,请手动检查残留容器:\n"
                + _tail(stopped.stderr)
            )


@pytest.fixture(scope="module")
def smoke_response(compose_stack: None) -> dict[str, Any]:
    """等后端就绪后发一条消息,返回解析后的最终状态(module 级,只发一次)。"""
    assistant_id = _read_assistant_id()
    payload = {
        "assistant_id": assistant_id,
        "input": {"messages": [{"role": "human", "content": SMOKE_PROMPT}]},
    }
    with httpx.Client(base_url=BACKEND_BASE_URL, timeout=RUN_TIMEOUT_S) as client:
        _wait_until_ready(client, timeout_s=READY_TIMEOUT_S)
        response = client.post(STATELESS_RUN_PATH, json=payload)

    if response.status_code != HTTP_OK:
        pytest.fail(
            f"发消息失败:HTTP {response.status_code},图 id={assistant_id}\n"
            f"响应:{_tail(response.text)}\n--- backend 日志尾部 ---\n{_tail(_backend_logs())}"
        )
    return response.json()


# --- 用例 ---------------------------------------------------------------------


@pytest.mark.e2e
def test_cold_start_answers_first_message(smoke_response: dict[str, Any]) -> None:
    """V1 前半段:compose up 之后,Supervisor 链路能把第一条消息答出来。

    只验结构不验文案:messages 是列表、里面有助手消息、助手消息内容非空。
    """
    messages = smoke_response.get("messages")
    assert isinstance(messages, list) and messages, (
        f"最终状态里没有 messages 列表,拿到的是:{smoke_response!r}"
    )

    replies = tuple(m for m in messages if _is_assistant_message(m))
    assert replies, f"消息列表里一条助手回复都没有,链路没走通:{messages!r}"

    assert any(_message_text(m).strip() for m in replies), (
        f"助手回复全是空内容,说明模型调用或工具链路出了问题:{replies!r}"
    )


@pytest.mark.e2e
def test_cold_start_downloads_no_model_weights(smoke_response: dict[str, Any]) -> None:
    """V1 后半段:整条链路跑完,后端日志里不该出现任何下载权重的痕迹。

    依赖 smoke_response 而不是只依赖 compose_stack,是为了保证日志覆盖到
    「启动 + 真跑过一次对话」的完整路径 —— 有些懒加载的权重是第一次调用才下。
    """
    hits = _find_download_markers(_backend_logs())
    assert not hits, (
        "后端日志里出现了下载痕迹,说明 BGE-M3 权重没被烧进镜像(违反 V1 零下载验收)。"
        "请检查 backend/Dockerfile 的 models 层是否被改坏。命中的行:\n" + "\n".join(hits[:20])
    )
