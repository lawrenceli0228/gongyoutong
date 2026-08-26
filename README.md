# 工友通(GYT)

建筑工地多智能体助手。一个 Supervisor 负责派活,底下挂若干专职 Agent:
规范检索、任务排期、拍照识隐患、图纸问答、日报/巡检报告落盘。
后端是 Python + LangGraph,前端复用 LangChain 官方的 agent-chat-ui。

> 这份 README 只负责把人送上路。**详细手册在 [`backend/README.md`](backend/README.md)**,
> 设计取舍在 [`docs/`](docs/)。两边不复制内容,免得漂移。

---

## 环境前提

命令面向 **macOS / Linux / WSL2**。Windows 请在 WSL2(Ubuntu)里操作 ——
原生 Windows 没有 `make`,`scripts/setup-frontend.sh` 也是 bash-only。

| 工具 | 版本 | 检查命令 |
|---|---|---|
| Python | ≥ 3.11 | `python3 --version` |
| uv | 最新 | `uv --version` |
| Docker | 带 Compose v2.24+ | `docker compose version` |
| make | Linux/WSL 自带;macOS 需 `xcode-select --install` | `make -v` |

---

## 三条命令跑起来

所有命令都在**仓库根目录**执行,`make` 会自己 `cd` 到 `backend/`。

```bash
# 1. 配密钥(必做,漏了服务起不来)
cp .env.example .env
#    编辑 .env,至少填这两行:
#      GYT_DEEPSEEK_API_KEY=sk-xxx     申请:https://platform.deepseek.com/api_keys
#      GYT_MOONSHOT_API_KEY=sk-xxx     申请:https://platform.moonshot.ai/console/api-keys

# 2. 装后端依赖
make setup

# 3. 起后端(:2024,改代码自动重载)
make dev
```

验证一下,另开一个终端:

```bash
curl -s http://localhost:2024/ok
# 期望:{"ok":true}
```

想连聊天界面一起跑:

```bash
make frontend                       # 把 agent-chat-ui clone 到 frontend/(该目录不进 git)
make up                             # 起后端容器
docker compose --profile ui up -d   # 再起前端,浏览器开 http://localhost:3000
```

`make` 不带参数会列出全部可用命令。

---

## 目录导航

| 路径 | 是什么 |
|---|---|
| `backend/` | 后端全部源码、测试、依赖与镜像定义 |
| `backend/README.md` | **详细上手手册**:排错、契约、加 Agent 的步骤、模型映射表 |
| `backend/src/gyt/config.py` | 所有常量的唯一入口(模型名/超时/阈值/路径都从这儿取) |
| `backend/src/gyt/graph.py` | Supervisor 图,加新 Agent 就改这里的 `AGENT_REGISTRY` |
| `docs/` | 技术方案与 T1 骨架计划(为什么这么设计) |
| `scripts/setup-frontend.sh` | 生成 `frontend/`(上游快照,不进 git) |
| `data/demo/` | 演示素材(照片 / 规范 / DXF),见该目录下的 README |
| `TODOS.md` | 已知欠账与后续待办 |
| `LICENSE` / `NOTICE.md` | 授权条款,以及第三方素材的来源与署名 |

---

## 两条红线

1. **`.env` 永远不提交。** 密钥不慎推上去 → 立刻去控制台吊销重发。
2. **服务端口只绑回环。** `docker-compose.yml` 里写的是 `127.0.0.1:2024:2024`,
   不要改成 `2024:2024` —— 那会把一个零鉴权的 Agent 执行端点连同你的 API Key
   暴露给整个局域网。

---

## 许可

**MIT License**,全文见 [`LICENSE`](LICENSE)。

    Copyright (c) 2026 Lawrence L, Chenghao Fan

⚠️ **MIT 只覆盖本项目自己写的代码。** 仓库里还带着一批第三方素材 ——
前端上游(agent-chat-ui,MIT)、30 张演示照片(Pexels / CC BY 4.0)、
一份国标规范 PDF —— 它们各自有各自的许可,**不随本项目的 MIT 再许可**。
来源、署名与已知局限全在 [`NOTICE.md`](NOTICE.md),拿去二次分发前先看那一份。
