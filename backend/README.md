# 工友通(GYT)后端 —— 上手手册

面向队友的操作文档。目标:**新机器 5 分钟跑起来,W2 写业务 Agent 时照着契约抄,不用问人**。

- 技术方案(为什么这么设计):`docs/工友通_AI_Agent_MVP_完整技术方案.md`
- T1 骨架计划(做到哪一步):`docs/T1_骨架搭建计划.html`
- 本文只讲**怎么用**,不重复讲设计取舍。

---

## 一、5 分钟跑起来

### 0. 前置

| 工具 | 版本 | 检查命令 |
|---|---|---|
| Python | ≥ 3.11 | `python3 --version` |
| uv | 最新 | `uv --version`(没有:`curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| Docker | 带 Compose v2.24+ | `docker compose version` |
| make | Linux/WSL 自带;macOS 需先 `xcode-select --install` | `make -v` |

**环境前提:本项目的命令面向 macOS / Linux / WSL2。**
Windows 用户请在 WSL2(Ubuntu)里操作,不要用 PowerShell 或 cmd —— 原生 Windows 没有
`make`,`scripts/setup-frontend.sh` 也是 bash-only(用了 `set -euo pipefail`、`mktemp -d`、
heredoc、`[[ ]]`)。Docker Desktop 记得开启 WSL2 集成;仓库要 clone 到 WSL 的 Linux 文件系统
(例如 `~/gyt`)而**不是** `/mnt/c` —— 后者会让 `uv sync` 和 `next build` 慢一个量级。

所有命令都在**仓库根目录**执行(不是 `backend/`),`make` 会自己 `cd`。

---

### 0.5 Windows 用户:先装 WSL2(约 15 分钟,只需一次)

没用过 WSL 的话照着走,每一步都有验证命令。**装完之后你和 macOS 用户敲的命令一模一样**,
下面第 1 步开始的所有内容对你都适用,不用再区分平台。

**① 装 WSL2 + Ubuntu**(管理员身份打开 PowerShell)

```powershell
wsl --install -d Ubuntu
```

装完**重启电脑**。重启后 Ubuntu 会自动打开,让你设一个 Linux 用户名和密码
(跟 Windows 账号无关,自己记住即可)。

验证:PowerShell 里 `wsl -l -v`,应看到 `Ubuntu  Running  2`。**VERSION 必须是 2**,
是 1 的话跑 `wsl --set-version Ubuntu 2`。

**② 之后所有开发都在 Ubuntu 窗口里做**(开始菜单搜 "Ubuntu",或 Windows Terminal 选 Ubuntu 标签页)。

```bash
sudo apt update && sudo apt install -y git make curl unzip   # make 一般已自带
curl -LsSf https://astral.sh/uv/install.sh | sh              # 装 uv
source ~/.bashrc
uv --version && make -v | head -1 && git --version           # 三个都要有输出
```

**③ 前端需要 Node**(只有要跑界面时才用得上,队友做 Knowledge/CAD 可以先跳过)

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
sudo corepack enable pnpm && pnpm --version
```

**④ Docker Desktop 开 WSL2 集成**

在 Windows 里装 [Docker Desktop](https://www.docker.com/products/docker-desktop/),
然后 **Settings → Resources → WSL Integration → 打开 Ubuntu 那一项 → Apply & Restart**。

验证:回到 Ubuntu 窗口敲 `docker compose version`,有版本号就通了。
**没开这一项的话,WSL 里根本看不到 docker 命令** —— 这是 Windows 上最常见的卡点。

**⑤ clone 到 Linux 文件系统,不要放在 `/mnt/c`**

```bash
cd ~ && git clone git@github.com:lawrenceli0228/gongyoutong.git gyt && cd gyt
```

`/mnt/c/...` 是 Windows 磁盘的跨系统挂载,`uv sync`、`pnpm install`、`pytest`
在上面都会慢一个量级(几百上千个小文件的 IO 全走转换层)。**务必放在 `~` 下。**

**⑥ 把 Windows 里的素材拷进来**(你准备的 DXF 图纸、规范 PDF 多半在 Windows 侧)

```bash
cp /mnt/c/Users/你的Windows用户名/Desktop/图纸.dxf ~/gyt/data/demo/drawings/
```

反过来,在 Windows 文件资源管理器地址栏输入 `\\wsl$\Ubuntu\home\你的用户名\gyt`
就能像普通文件夹一样浏览仓库,拖拽文件也行。

**⑦ 中文别乱码**

```bash
sudo apt install -y language-pack-zh-hans
echo 'export LANG=C.UTF-8' >> ~/.bashrc && source ~/.bashrc
python3 -c "print('中文测试正常')"      # 这行必须正常显示
```

> **换行符不用你操心。** 仓库根的 `.gitattributes` 已经把策略钉死了:
> 所有文本一律 LF、`.dxf` 和图片按二进制处理。
> 这防的是两个具体的坑 —— `scripts/*.sh` 变 CRLF 后 bash 会报
> `$'\r': command not found`(报错完全不提换行符,能查半天);
> 以及**那份 GBK 编码的中文 DXF 样例被 git 转换后,CAD 的编码测试会变成假绿**
> ——测的是被改坏的文件而不是真实图纸,这条测试的意义就没了。
>
> 保险起见首次 clone 前跑一次:`git config --global core.autocrlf input`

**⑧ 编辑器**:装 VS Code 后再装 **WSL 扩展**,在 Ubuntu 窗口里敲 `code .`,
就能用 Windows 的 VS Code 直接编辑 WSL 里的文件,体验和本机一样。

#### 实在不想装 WSL?原生 Windows 的降级路线

**能跑,但会在几个地方硌手,而且和队友环境不一致**(联调时「我这儿是好的」会变成常态)。
`uv`、`pytest`、`docker compose` 在原生 Windows 上都正常;卡的是 `make` 和 bash 脚本。
用 **PowerShell 7**(不是 cmd,cmd 默认 GBK 代码页会让中文输出乱码):

| Makefile 目标 | PowerShell 等价命令 |
|---|---|
| `make setup` | `cd backend; uv sync` |
| `make test` | `cd backend; uv run pytest` |
| `make cov` | `cd backend; uv run pytest --cov=gyt --cov-fail-under=80` |
| `make lint` | `cd backend; uv run ruff check .` |
| `make fmt` | `cd backend; uv run ruff format .; uv run ruff check --fix .` |
| `make dev` | `cd backend; uv run langgraph dev --allow-blocking` ← **`--allow-blocking` 不能省**,见下方 |
| `make up` | `mkdir data -Force; docker compose up -d` |
| `make down` | `docker compose down --remove-orphans` |
| `make frontend` | **没有等价命令,而且手抄不出来了** —— 见下方专门那段 |
| `make lint-ci` | `docker run --rm -v "${PWD}:/repo" -w /repo rhysd/actionlint:latest -no-color .github/workflows/ci.yml` |

**`--allow-blocking` 为什么不能省**(与 `Makefile` 的 `dev` 配方、`backend/Dockerfile` 的 CMD、
`docker-compose.dev.yml` 是同一条铁律):缺了它,langgraph 的 blockbuster 会把
`config.cache_dir` / `artifacts_dir` 那种「访问即 mkdir」的属性判成违规,
**磁盘缓存的读写会全部静默失败** —— 每次提问都真的掏钱调模型,而日志里只有一句
"Background run succeeded",看不出任何异常。踩过:本机 Safety Agent 每次等 60 秒,
容器里却是秒回,差别就在这一个参数上。

**`make frontend` 别再照「clone + 写两行 .env」手抄了。** 那是 2026-08-06 的写法,
现在 `scripts/setup-frontend.sh` 做的事已经远不止 clone:

- **生成** `frontend/.env`、`frontend/.dockerignore`,以及 **`frontend/Dockerfile`**
  ——上游仓库自己不带这个文件,缺了它 `docker compose --profile ui up` 直接起不来。
- **覆盖 8 个界面文件**(`scripts/frontend-overrides/`,靠脚本末尾的 `apply_override` 注册):
  观感三件 `tool-calls.tsx` / `ai.tsx` / `markdown-text.tsx`,
  **DXF 上传四件** `use-file-upload.tsx` / `multimodal-utils.ts` / `MultimodalPreview.tsx` / `thread-index.tsx`,
  消警告一件 `langgraph.tsx`。

照旧写法手抄拿到的界面:没有交接痕迹折叠、没有巡检记录卡片、没有工作表格,
**也没有图纸上传入口**(整条 cad 演示路径就没有门)。
原生 Windows 上的正解是装 WSL2 或 Git Bash 跑那个脚本,别手抄 —— 清单会继续变,
**唯一真相是 `scripts/setup-frontend.sh` 本身**。

再加一句防中文乱码(建议写进 PowerShell 配置文件):

```powershell
$env:PYTHONUTF8 = "1"
```

**做 CAD 这条线的人尤其建议用 WSL2** —— 你要处理 GBK 编码的中文 DXF,
而 Windows 原生环境的默认编码本身就是 GBK,出问题时很难分清是「代码没处理好编码」
还是「终端显示的问题」。WSL2 里环境统一是 UTF-8,变量少一个。

---

### 1. 配密钥(必做,一次)

```bash
cp .env.example .env
# 编辑 .env,至少填这两行:
#   GYT_DEEPSEEK_API_KEY=sk-xxx      申请:https://platform.deepseek.com/api_keys
#   GYT_MOONSHOT_API_KEY=sk-xxx      申请:https://platform.moonshot.ai/console/api-keys
```

`.env` 在 `.gitignore` 里,**永远不要提交**。密钥不小心推上去了 → 立刻去控制台吊销重发。

### 1.5 先选路线:你这台机器装得上依赖吗

三条路线不是口味问题,**先用一条命令定生死**:

```bash
cd backend && uv sync --dry-run     # 只解析不安装,几秒出结果
```

| 结果 | 走哪条 |
|---|---|
| 成功 | **路线 A**(本地跑,最快)。想更接近演示环境时再切路线 B |
| 报 `doesn't have a source distribution or wheel for the current platform` | **路线 A2**(容器里开发)。本机这条路是**断的**,不是慢 |

已知会失败的机器:**Intel Mac**。`torch 2.13.0` 的 macOS 轮子只有 `arm64`
(实测报 `You're on macOS (macosx_14_0_x86_64), but torch only has wheels for ...`),
而 `sentence-transformers` → `torch` 是 knowledge Agent 的硬依赖,绕不开。
Windows 与 Apple Silicon 不受影响,PyPI 上有对应轮子。

### 2. 路线 A:本地跑(开发日常,改代码秒生效)

```bash
make setup     # uv sync,装依赖 + 把 gyt 以可编辑方式装进 .venv
make dev       # 起 LangGraph 服务,监听 http://localhost:2024
```

另开一个终端验证:

```bash
curl -s http://localhost:2024/ok
curl -s http://localhost:2024/runs/wait \
  -H 'content-type: application/json' \
  -d '{"assistant_id":"gyt","input":{"messages":[{"role":"human","content":"你好"}]}}'
```

第二条是纯闲聊,应该看到 **supervisor 自己回答**、不产生任何交接 —— 图起来了就算通电。

> 这里以前写的是「应该看到 ping Agent 走了一遭 echo 工具再回话」。**`ping` 已于 2026-08-08
> 从 `AGENT_REGISTRY` 摘除**(包还留着,当工具写法样板用),所以现在不会有那一跳,
> 看不到它是正常的。摘除理由写在 `graph.py` 的 `AGENT_REGISTRY` 顶部:它的正域是空的,
> 于是 summary 里每个词都成了钩子,三轮改措辞都收不住 —— 空正域条目不该占路由位。
> 想验哪个子 Agent 通不通,直接问它那一域的话(比如「这张照片有没有安全隐患」)。

> `make dev` 的工作目录是 `backend/`,但配置照样读得到仓库根的 `.env`:
> `backend/langgraph.json` 里写了 `"env": "../.env"`,langgraph CLI 会先把它灌进环境变量。
>
> **数据目录不受工作目录影响**:`Settings.data_dir` 的默认值是按 `src/gyt/config.py`
> 自身的位置往上数出来的仓库根(见 `_default_data_dir`),所以本地跑写的是**仓库根的 `data/`**;
> 容器里 `GYT_DATA_DIR=/app/data`,而 compose 把宿主 `./data` 挂到那儿 —— **两边是同一份数据**。
> 这一点不是锦上添花:LLM 缓存(尤其预热过的视觉缓存)、SQLite 台账、Chroma 向量库、
> 产物注册表全在这个目录下,分成两份的时候「本地销掉的账,进容器一看还是 open」。
>
> 这个默认值以前是相对路径 `data`,会跟着进程工作目录跑,`make dev` 因此落在 `backend/data/`。
> 你的机器上如果还留着那个旧目录,跑一次搬迁脚本把它并进来:
>
> ```bash
> python3 backend/scripts/migrate_data_dir.py            # 默认只演练,先看清楚要搬什么
> python3 backend/scripts/migrate_data_dir.py --apply    # 真搬(复制,不删源)
> ```
>
> 只用标准库、在哪个目录敲都行(路径是从脚本自身位置推的)。确认新目录一切正常之后,
> 再加 `--remove-source` 或自己手动删掉 `backend/data/`。

### 2.5 路线 A2:容器里开发(本机装不上依赖时走这条)

**后端跑在容器里,你在宿主正常编辑文件。** 不是"退而求其次" —— 除了首次构建镜像那一次,
日常体验和路线 A 几乎一样。

> ⚠️ **「首次构建」要留出 10~30 分钟,不是「几分钟」。**(这里以前写的是「几分钟」,
> 而下面路线 B 对**同一次构建**写的是 10~30 分钟 —— 两个数字说的是一件事。)
> `docker-compose.dev.yml` 里没有 build 段,`make dev-docker` 的 `--build` 走的就是基础档那份,
> 而 app 阶段要 `COPY --from=models`,必然先构建 models 阶段、先拉 2.2GB 的 BGE-M3 权重。
> 被分流到这条路线的恰恰是本机装不上依赖的人(Intel Mac),按「几分钟」的预期开跑会以为卡死了。
> 之后命中层缓存是秒级。

```bash
make dev-docker    # 起容器 + 把宿主 backend/src 挂进去(只读)+ 打开热重载
make test-docker   # 同一套用例在容器里跑
make down          # 收摊
```

改了东西之后要做什么,**照这张表,别猜**:

| 改了什么 | 要做什么 | 多久 |
|---|---|---|
| `backend/src/**/*.py` | **什么都不用做**,自动重载 | 秒级(实测 0.9s) |
| `prompt.md` 等非 `.py` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml restart backend` | 秒级 |
| `pyproject.toml` / `uv.lock` / `langgraph.json` / `Dockerfile` | 重来一次 `make dev-docker`(它带 `--build`) | 分钟级 |

为什么 `.md` 不自动重载:`langgraph dev` 底下是 uvicorn 的 reload,监视清单写死
`default_includes = ["*.py"]`,而 langgraph CLI 没有透出 `--reload-include`。
**这是查过源码的,不是猜的** —— 别把「改提示词立刻生效」写进任何文档。

`make dev-docker` 里的 `--build` 不能省:挂载换掉的是**源码**,换不掉镜像层里的**依赖**。
镜像旧一步,容器会在图加载期 `ModuleNotFoundError` 直接崩,而 `docker compose up -d`
早就返回 0 了 —— 你只会看到一句"[已启动]"和一个根本没起来的服务。

**改了代码没反应?** 宿主文件系统不往容器传 inotify 事件(WSL2、网络盘常见)。
macOS Docker Desktop 实测是能传的;传不到就改用轮询:

```bash
GYT_WATCH_POLLING=1 make dev-docker
```

### 3. 路线 B:容器跑(接近演示环境,验证"冷启动零下载")

```bash
mkdir -p data
# Linux 还要给挂载目录换主人,否则容器内的非 root 用户写不进去:
#   sudo chown -R 10001:10001 data
make up        # 只起后端(前端在 ui profile 里,默认不起)
make down      # 收摊
```

**首次构建要拉 2.2GB 的 BGE-M3 权重,10~30 分钟很正常**,之后命中层缓存是秒级。
这一步不是浪费:权重在构建期就烧进镜像层,运行期 `HF_HUB_OFFLINE=1`,演示现场断网也起得来。

前端(可选,`frontend/` 由 `scripts/setup-frontend.sh` 准备):

```bash
make frontend                      # 拉取并初始化 agent-chat-ui
docker compose --profile ui up -d  # 前端在 http://localhost:3000
```

### 3.5 知识库(RAG)首次建库(问规范前必做一次)

规范检索(knowledge Agent)靠一个**本地向量库**:把 `data/demo/docs/` 里的规范 PDF 切分、
用本地 BGE-M3 向量化,写进 `data/chroma/`。**建库是生成物,不进 git**(和 `frontend/`、
BGE-M3 权重一个道理:提交「源 PDF + 建库代码」,各处重建),所以**每台机器、每个部署都要建一次**。

```bash
make build-knowledge   # 首次约 15 分钟(下 2.2GB 权重 + 抽嵌全书);之后 manifest 命中秒过
```

- **本地开发**:上面这条即可(建到仓库根 `data/chroma`,和 `make dev` 读的是同一份)。
  容器挂的也是这个 `./data`,所以本机建过一次、`make up` 起来就能直接用 ——
  前提是本机和镜像用的是同一个 embedding 模型(见本节末尾那条同步提醒),换模型要带 `--rebuild`。
  嫌手动麻烦,也可以在 `.env` 里设 `GYT_KNOWLEDGE_PREBUILD_AT_STARTUP=true` ——
  `make dev` 首次启动会自动建(会卡那 15 分钟一次),之后每次秒起。
- **容器 / 生产**:**先把挂载的 `./data` 卷建好,再起服务**:
  ```bash
  docker compose run --rm backend python -m gyt.agents.knowledge.ingest   # 建到 ./data/chroma
  make up
  ```
  `./data` 是绑定挂载,建一次就持久,容器重建不丢。

> ⚠️ **容器里千万别开 `GYT_KNOWLEDGE_PREBUILD_AT_STARTUP`**:15 分钟的启动建库会拖垮
> healthcheck(`start_period` 120s)→ 容器判成 unhealthy → `restart: on-failure:3` 反复重启,
> 永远建不完。**镜像构建期烤索引也没用**:`./data` 绑定挂载运行期会把镜像里那份盖掉。
> 所以生产的正解就是上面那条「先 `run --rm` 建卷、再 `up`」。
>
> 加了新规范 PDF、或想换 embedding 模型,重跑 `make build-knowledge`(换模型加 `--rebuild`);
> 换模型还要同步 `backend/Dockerfile` 的 `ARG EMBEDDING_MODEL` 与 `config.embedding_model`。

### 4. 跑测试

```bash
make test   # 单元 + 集成,秒级,全 mock,绝不联网
make cov    # 加覆盖率门槛(<80% 直接失败,与 CI 同一把尺子)
make lint   # ruff 只报不改
make fmt    # ruff 格式化 + 自动修
make e2e    # 冷启动冒烟 —— 真起 compose、**首跑要拉 2.2GB 权重 10~30 分钟**,
            # 而且会**产生真实调用费用**(它真发一次 /runs/wait)。平时别跑。
```

评测与演示辅助(**都不在上面这几条里,而且都花钱或有前置**):

```bash
make eval-smoke        # 1 张照片探链路,几十秒、几毛钱 —— 上全量前先跑这条
make eval              # 三套全跑,真花钱(safety 约 22 分钟;rag 要求本机向量库已建好)
make build-knowledge   # 建规范知识库(首次含 2.2GB 权重,约 15 分钟);rag 与 E 组的前置
make serve-artifacts   # 起只读静态服务。**不起它,界面里的巡检记录卡片点开是空白**
```

真机验收(25 断言,含直查 sqlite 的库级铁证)不在 Makefile 里,直接跑脚本 ——
**跑法与清库路径的唯一真相是脚本头注**(`backend/scripts/live_acceptance.py`
第 1 行的断言条数 + 第 7-17 行的跑法块,其中第 12-16 行是清库路径):

```bash
# E 组要求知识库已建好(每台机建一次);想从空台账开始,在**仓库根**执行 rm -f data/gyt.sqlite3
cd backend && uv run --env-file ../.env python scripts/live_acceptance.py
```

⚠️ **别删 `backend/data/gyt.sqlite3`,那是空操作** —— 后端只写 `<仓库根>/data/`,
删错的后果不是「没生效」而是「以为清干净了」,真库带着旧台账上台会红一片还查不到方向。

**跑单个测试必须先 `cd backend`**(pytest 的 `testpaths` 和覆盖率配置都在 `backend/pyproject.toml`;
从仓库根裸跑会大面积报错,那是跑法不对不是回归):

```bash
cd backend && uv run pytest tests/unit/test_schedule_dates.py
cd backend && uv run pytest -k "改期"      # 测试名是中文,-k 直接写中文
```

`make test` 的前提是本机 `make setup` 成功过。装不上依赖的机器(见路线 A 末尾那条)
用 `make test-docker`:同一套用例在容器里跑,比 `make test` 慢十几秒。
它会先拿宿主的 `backend/uv.lock` 和镜像里那份对账,对不上就直接拦下来让你
`docker compose build backend` —— 因为镜像比代码旧的表现是一连串 `ModuleNotFoundError`,
不拦的话人会去查测试、查挂载,唯独想不到是镜像该重建了。

---

## 二、目录说明

```text
backend/
├── src/gyt/
│   ├── config.py             全项目常量的唯一入口(get_settings())。禁止在别处硬编码
│   ├── graph.py              Supervisor 图,导出已 compile 的 graph;langgraph.json 指向它
│   │                         唯一挂载点 = 模块级常量 AGENT_REGISTRY
│   ├── core/
│   │   ├── errors.py         错误信封 Envelope / ErrorCode / tool_guard   ← 契约 1
│   │   ├── artifacts.py      文件引用注册表 register/resolve/read_meta    ← 契约 2
│   │   ├── llm.py            双供应商客户端:选模型 + 退避重试 + 磁盘缓存
│   │   ├── base_agent.py     Agent 薄工厂:load_prompt + create_gyt_agent
│   │   ├── focus.py          FocusOnOwnWork 中间件:只滤 supervisor 的纯文本发言
│   │   ├── require_tool.py   防假账两件套 RequireToolCall / RequireReceiptSource
│   │   └── uploads.py        pre_model_hook:把上传的图片/图纸换成「编号」文本
│   ├── db/                   SQLite 台账(schedule 用)
│   └── agents/               ← 六个包。**在册的五个**见 graph.AGENT_REGISTRY:
│       ├── safety/             识图查隐患(工具内部走 kimi 视觉档)
│       ├── schedule/           任务台账(挂 RequireLedgerTool)
│       ├── cad/                DXF 图纸
│       ├── knowledge/          规范条文检索(Chroma + BGE-M3)
│       ├── report/             出巡检记录 docx。**不在册** —— 只作为英雄链
│       │                       inspection 的第二跳存在,transfer_to_report 这条路不存在
│       └── ping/               **已于 2026-08-08 从 AGENT_REGISTRY 摘除**,包留着当工具写法样板
│           ├── prompt.md       提示词外置成 .md,不许写死在 .py 里
│           ├── tools.py        工具函数,统一返回 Envelope
│           └── __init__.py     build_xxx_agent(),组装并返回已编译的子图
├── eval/                     三套评测(判分是纯函数;接线点只有 hooks.py 的 RUNNERS)
│                             **它也在覆盖率门槛里**(pyproject 的 --cov=eval)
├── scripts/                  真机验收 live_acceptance.py(25 断言)+ acceptance_dates.py
│                             + migrate_data_dir.py。容器里也有一份,test-docker 会挂宿主这份
├── tests/
│   ├── conftest.py           全局 fixture:环境隔离 + 假 AI 回复(所有人共用)
│   ├── unit/                 单元测试,全 mock
│   └── e2e/                  冷启动冒烟,默认 skip,GYT_E2E=1 才跑
├── Dockerfile                四阶段构建;阶段 3 单独预下载权重(冷启动零下载的命根子)
├── langgraph.json            图注册清单:图 id = "gyt" -> ./src/gyt/graph.py:graph
└── pyproject.toml            依赖 + pytest/ruff/coverage 配置(版本核实记录在文件顶部)

仓库根/
├── .env / .env.example       所有 GYT_* 配置(.env 不进 git)
├── docker-compose.yml        backend(默认) + frontend(--profile ui)
├── docker-compose.dev.yml    开发档覆盖件(make dev-docker / test-docker 用)
├── Makefile                  所有开发命令的入口
└── data/                     运行期数据,不进 git;只有 data/demo/ 例外(演示数据集)
                              本机跑和容器跑都是这一份(容器里挂成 /app/data)
```

---

## 三、两条硬契约(W2 写 Agent 前必读)

W2 两人各写 3 个 Agent,只有**统一的返回结构 + 统一的文件传法**才不会互相踩。
下面两条不是建议,是红线。

### 契约 1:所有工具函数返回错误信封 `Envelope`

信封就是一个固定四键的 dict:

| 键 | 类型 | 说明 |
|---|---|---|
| `ok` | `bool` | 这次调用成不成功 |
| `data` | `Any \| None` | 成功时的业务数据;失败时恒为 `None` |
| `user_msg` | `str` | **给工地师傅看的中文人话**,不许出现堆栈/类名/内部路径 |
| `error_code` | `str \| None` | 失败时是 `ErrorCode` 的字符串;成功时 `None` |

**标准写法**(单个工具的形状照 `src/gyt/agents/ping/tools.py` 抄即可 —— ping 虽然已从
`AGENT_REGISTRY` 摘除,包特意留着就是当这个样板用的;但**整个 Agent 的组装照 `agents/safety/` 抄**,
理由见第五节「加一个新 Agent」):

```python
from langchain_core.tools import tool

from gyt.core.errors import Envelope, ErrorCode, fail, ok, tool_guard


@tool("check_safety", description="检查工地照片里的安全隐患。参数 artifact_id 传照片编号。")
@tool_guard  # ← 必须贴着函数,顺序不能反
def check_safety(artifact_id: str) -> Envelope:
    """检查照片隐患。返回统一信封。"""
    if not artifact_id:
        # 自定义中文提示,盖过默认文案
        return fail(ErrorCode.INVALID_INPUT, "没收到照片,请先拍一张再问我。")
    # detail 只进日志,绝不进返回值 —— 防止内部细节被 LLM 复述给用户
    return ok(data={"risks": []}, user_msg="这张照片没看出明显隐患。")
```

两个装饰器的顺序:`@tool` 在外、`@tool_guard` 在内。
反了的话异常会先被 LangChain 抓成英文报错,工人就看不懂了。

**错误码与默认中文提示**(`errors.DEFAULT_USER_MSG`,不够贴切就自己传 `user_msg`):

| ErrorCode | 什么时候用 |
|---|---|
| `TIMEOUT` | 上游超时 |
| `RATE_LIMITED` | 被限流(429) |
| `UPSTREAM_ERROR` | 供应商 5xx / 密钥无效 |
| `FILE_TOO_LARGE` | 超过 `drawing/document/photo_max_mb` |
| `FILE_UNSUPPORTED` | 扩展名不在白名单(DWG 请离线转 DXF) |
| `FILE_CORRUPT` | 文件打不开 |
| `NOT_FOUND` | artifact / 记录不存在 |
| `EMPTY_RESULT` | 检索没命中 |
| `INVALID_INPUT` | 参数不合法 |
| `INTERNAL` | 兜底,`tool_guard` 自动用 |

三条禁止:
1. 禁止工具函数直接 `raise` 到 Agent 循环里(`tool_guard` 会兜住,但别依赖它兜业务错)。
2. 禁止把异常信息拼进 `user_msg`,内部细节一律走 `detail=`(只写日志)。
3. 禁止返回裸 dict/字符串 —— 一律 `ok()` / `fail()`。

### 契约 2:大文件传 `artifact_id`,不传内容

LangGraph 的消息状态里**只允许出现 32 位十六进制字符串**,图片/图纸/docx 一律落盘。
既省 token,也避免把二进制塞进 LLM 上下文。

```python
from pathlib import Path

from gyt.core.artifacts import ArtifactKind, ArtifactNotFound, read_meta, register, resolve
from gyt.core.errors import ErrorCode, fail, ok

# 入口层:收到上传 -> 登记 -> 之后全流程只传这个 id
artifact_id = register(
    photo_bytes,  # bytes 或 Path 都行
    kind=ArtifactKind.PHOTO,  # PHOTO / DRAWING / DOCUMENT / REPORT / OTHER
    original_name="工地隐患.JPG",  # 只进元数据,永不参与拼路径
)
# -> "9f2c8b1e4a7d0c5f3e6b2a9d8c1f0e4a"

# 工具层:拿 id 换真实路径
try:
    path: Path = resolve(artifact_id)
    meta = read_meta(artifact_id)  # id/kind/original_name/ext/size_bytes/sha256/created_at
except ArtifactNotFound:
    envelope = fail(ErrorCode.NOT_FOUND, "没找到这张照片,可能已经过期了,请重新上传。")
else:
    envelope = ok(data={"size": meta["size_bytes"]})
```

安全规则(已在 `artifacts.py` 里实现,**不要自己另写一套落盘逻辑**):
- 落盘文件名一律 `artifact_id + 白名单扩展名`,原始文件名只进元数据 sidecar。
- `original_name` 传 `../../../etc/passwd.jpg` 也只会落在 `artifacts_dir` 内。
- `resolve()` 先用 `ARTIFACT_ID_RE`(纯 32 位 hex)卡一道,带 `/` 或 `..` 的 id 直接拒绝。

### 加一个新 Agent 的五步

**照 `agents/safety/` 抄**(它是在册的、挂了中间件的真实形状)。
`agents/ping/` 只能当**单个工具**的写法样板看:它已从 `AGENT_REGISTRY` 摘除,
而且它的 `create_gyt_agent` 调用**不带 `extra_middleware`** —— 现役 Agent 都挂了行为守卫
(`schedule/__init__.py` 挂 `RequireLedgerTool`、`report/__init__.py` 挂 `RequireReceiptSource`),
照 ping 抄拿不到那一层,而少了它模型会凭记忆编回执。

```text
1. 建目录 src/gyt/agents/<名字>/
2. 写 prompt.md            提示词外置;<!-- --> 注释会被 load_prompt 剥掉
3. 写 tools.py             每个工具 @tool + @tool_guard,返回 Envelope
4. 写 __init__.py          build_<名字>_agent() -> create_gyt_agent(
                               name=..., prompt=load_prompt(<目录>),
                               tools=list(<工具列表>), purpose="text"|"vision")
5. 去 graph.py 往模块级常量 AGENT_REGISTRY 追加一条:
       AgentSpec(name=..., summary=..., build=build_<名字>_agent,
                 requires_project=...)   # 2026-08-22 加的第四个字段,漏填当场 TypeError
                                          # 判据:不选工地时**回给用户的那句话本身**
                                          # 是不是错的(不是「有没有副作用」)
   summary 必填 —— 它是 Supervisor 判断"这活派给谁"的唯一依据,留空会直接报错。
   挂载点只有 AGENT_REGISTRY 这一处,别去改 build_graph() 的函数体。
```

`name` 同时是 Supervisor 节点名、交接工具名 `transfer_to_<name>`、chat-ui 轨迹上的显示名 ——
**改名等于改对外 API**,定了就别动。

---

## 四、模型映射表(2026-08 基线)

| 用途 | `Settings` 字段 | 当前型号 | 供应商 / base_url |
|---|---|---|---|
| 路由 / 任务拆解 / 报告 / 知识综合 | `model_text` | `deepseek-v4-flash` | DeepSeek · `https://api.deepseek.com/v1` |
| Safety 识图 / CAD 预览问答 | `model_vision` | `kimi-k3` | Moonshot · `https://api.moonshot.ai/v1` |
| 工具调用不达标时的备胎 | `model_tool_fallback` | `kimi-k3` | 同上 |
| 向量化(RAG) | `embedding_model` | `BAAI/bge-m3` | **本地跑**,两家都没有 embedding API |

代码里选模型只写用途,不写型号:

```python
from gyt.core.llm import ainvoke, get_chat_model

model = get_chat_model("text")  # "text" -> DeepSeek;"vision"/"tool" -> Kimi
reply = await ainvoke(model, messages, cache_extra="safety")  # 带缓存 + 退避重试
```

**两条路径,别搞混**(细节见 `core/llm.py` 顶部「图 0」):

| | 谁走 | 缓存 | 重试 |
|---|---|---|---|
| 路径甲 Agent | Supervisor + 5 个子 Agent(演示主线) | langchain 全局缓存 `llm.GytDiskCache`,`get_chat_model()` 自动装 | `ChatOpenAI.max_retries` |
| 路径乙 直调 | `await ainvoke(model, ...)`,评测打分 / 一次性抽取 | 同一个 `cache_dir`、同一套文件格式 | 自研指数退避 + 中文错误文案 |

两条路径共用缓存目录但缓存键构成不同,互不串味。**建 Agent 时不要自己再包一层模型代理** ——
缓存已经在模型层之下生效了,再包一层只会把工具集这一维从缓存键里弄丢。

### 2026-08 的型号变更(别用记忆里的旧名字)

- DeepSeek `deepseek-chat` / `deepseek-reasoner`:2026-07-24 宣布弃用。
- Moonshot `kimi-latest` / `kimi-k2` 系列 / `moonshot-v1` 系列:全部停用退役。
- Moonshot 的域名是 **`.ai` 不是 `.cn`**,两边的 key 不通用。
- `deepseek-v4-flash` 自带思考模式;路由这类要低延迟的场景默认关掉
  (`GYT_DISABLE_THINKING_FOR_TEXT=true`,底层传 `extra_body={"thinking":{"type":"disabled"}}`)。

### 换模规则

1. **只改 `.env`,不改代码**:`GYT_MODEL_TEXT=xxx` / `GYT_MODEL_VISION=xxx`。
2. 换供应商要连 `GYT_*_BASE_URL` 和对应 API Key 一起改;两家都是 OpenAI 兼容协议。
3. 换完必须重跑评测:路由 ≥ 0.90、Safety ≥ 0.80、RAG ≥ 0.80(阈值也在 `Settings` 里)。
4. 换完**必须把 `GYT_PROMPT_VERSION` 往上加一版**,否则会命中旧模型留下的缓存,评测结果失真。
5. Embedding 换型号要连带重建向量库(`data/chroma/`),并同步改 `backend/Dockerfile` 的
   `ARG EMBEDDING_MODEL` —— Dockerfile 没法 import 配置,靠这条约定手工保持一致。

---

## 五、测试怎么写

`tests/conftest.py` 已经把地基铺好,**不要在自己的测试里再造一套环境隔离**:

| fixture | 作用 |
|---|---|
| `_isolated_settings` | autouse,自动生效:清 `get_settings` 缓存、抹掉本机 `GYT_*`、`data_dir` 指到 `tmp_path`、塞两个假 Key |
| `_isolated_llm_cache` | autouse,自动生效:每个用例从「没装 langchain 全局缓存」起步,结束后还原(那是个进程级模块变量,不还原会跨用例串) |
| `fake_ai_message` | 一条假的 `AIMessage`,mock LLM 时当返回值用 |

```python
from unittest.mock import AsyncMock


async def test_safety_agent(fake_ai_message, monkeypatch):
    fake_model = AsyncMock()
    fake_model.ainvoke.return_value = fake_ai_message
    monkeypatch.setattr("gyt.core.llm.get_chat_model", lambda *a, **k: fake_model)
    ...
```

三条规矩:
1. **绝不真调外部 API**。测试跑完不该产生任何账单。
2. 自己写的模块覆盖率 ≥ 80%,`make cov` 是硬门槛(CI 同样开着 `--cov-fail-under=80`)。
3. 异步测试直接 `async def`,不用加 `@pytest.mark.asyncio`(`asyncio_mode=auto` 已配)。

E2E(`tests/e2e/test_cold_start.py`)默认跳过,`make e2e` 才跑。它守两条:
链路能答出第一条消息 + **后端日志里没有权重下载痕迹**(证明冷启动零下载)。

---

## 六、常见问题

### 报"请在 .env 里配置 GYT_XXX_API_KEY"

1. `.env` 必须在**仓库根目录**,不是 `backend/.env`。
2. 填完要**重启进程**:`get_settings()` 带 `lru_cache`,进程内只读一次。
3. 容器里改完 `.env` 要 `make down && make up`(env_file 在容器创建时注入)。
4. 视觉功能报错但文本正常 → 缺的是 `GYT_MOONSHOT_API_KEY`,两家 key 不通用。

### 端口被占用(2024 / 3000)

```bash
lsof -i :2024                       # macOS/Linux 查谁占着
docker compose ps                   # 多半是上次没 down 干净的容器
make down                           # 先规规矩矩收摊
```

还占着就 `kill <PID>`。要换端口:改 `docker-compose.yml` 里那一行的**中间那段**,
容器内的 2024 别动 —— `Dockerfile` 的 `EXPOSE`、healthcheck、前端 `NEXT_PUBLIC_API_URL` 都对着它。

⚠️ 那一行现在是 `"127.0.0.1:2024:2024"`,**三段**,所以「冒号左边」指到的是 IP 不是端口
(这句话以前就是这么写的,会把人指错)。要改的是中间那个 2024。
**最左边的 `127.0.0.1` 一个字都不许动** —— 去掉它写成 `2024:2024`,等于把零鉴权的
Agent 执行端点连同容器里的两把 API Key 暴露给整个局域网。同一条红线还写在
`docker-compose.yml` 顶部与 `docker-compose.dev.yml`(那份文件永远不许出现 `ports`)。

### 镜像构建特别慢 / 卡在下载权重

- 首次构建要拉 2.2GB 的 BGE-M3,**10~30 分钟是正常的**,只发生一次。
- 卡住不等于慢。**先分清是"在下载"还是"下载源坏了"** —— 见下一节。
- 看进度:`docker compose build backend --progress=plain`。
- **改业务代码不该触发重下权重**。如果触发了,说明 `Dockerfile` 阶段 3(models)被改成
  依赖源码或 `FROM deps` 了 —— 那是这个文件的命根子,必须改回 `FROM base` 且不 COPY 源码。

### 权重下载源失效时怎么办

构建在 models 阶段报 `huggingface_hub.errors.LocalEntryNotFoundError`
(原文是"Please check your connection")时,**十有八九不是你的网络问题**。

这个报错会把人骗去查网络,但真实原因通常是:下载源还活着、能连通、
甚至能返回 200,但它已经不再提供文件了,只回一个跳转。
`huggingface_hub` 的元数据探测拿不到结果,就报"既下不到、本地也没有"。

**2026-08-06 我们踩过的原型:** 默认源 `hf-mirror.com` 就是这么坏的 ——

```bash
curl -sI https://hf-mirror.com/api/models/BAAI/bge-m3 | head -3
# HTTP/1.1 308 Permanent Redirect
# Location: https://huggingface.co/api/models/BAAI/bge-m3    ← 跳到别的域名 = 这个源已失效
```

#### 三步排查

**第一步:确认这个源是不是真在提供文件**

```bash
SRC=https://huggingface.co          # 换成你正在用的源
curl -sI $SRC/api/models/BAAI/bge-m3 | head -3
```

- `200` → 源是好的,问题在别处(看第三步)
- `308` / `301` 且 `Location` 指向**别的域名** → 这个源已经失效,换掉它
- 连不上 / 超时 → 真的是网络,换源或挂代理

**第二步:A/B 实测两个源到底哪个能下**

别猜,直接在容器里跑一次真实下载(只拉一个几 KB 的 config.json,十几秒出结果):

```bash
docker run --rm python:3.12-slim-bookworm sh -c "
  pip install -q huggingface_hub &&
  HF_ENDPOINT=$SRC python -c \"
from huggingface_hub import snapshot_download
snapshot_download('BAAI/bge-m3', allow_patterns=['config.json'], cache_dir='/tmp/probe')
print('这个源可用')\" "
```

注意:`HF_ENDPOINT` 必须在 **python 进程启动前**就设好。
`huggingface_hub` 在 import 时把它读进模块常量,进程内再改环境变量是无效的
(我们第一版探针就栽在这上面,得出了完全相反的结论)。

**第三步:换成验证过的源重建**

```bash
docker compose build --build-arg HF_ENDPOINT=https://<你验证过的源> backend
```

固定下来就在根目录 `.env` 里设 `HF_ENDPOINT=...`(compose 会透传成 build arg)。

#### 这个值住在哪(改之前先看这里)

同一个值有两个地方要同步,**改一处必须改两处**:

| 位置 | 角色 |
|---|---|
| `backend/Dockerfile` 的 `ARG HF_ENDPOINT` | 事实上的默认值,单独 `docker build` 时生效 |
| `docker-compose.yml` 的 `args.HF_ENDPOINT` | 走 compose 时生效;compose 读不到 Dockerfile 的 ARG 默认值,只能重复一遍 |

根目录 `.env` 里的 `HF_ENDPOINT` 是**覆盖**用的,默认注释掉。
在那儿填值会盖掉上面两个默认值 —— 我们就是这样第二次构建失败的:
Dockerfile 已经改成官方源了,`.env` 里还留着失效的 `hf-mirror.com`。

#### 完全无法联网时:离线导入权重

在一台能联网的机器上下好,再拷到构建机:

```bash
# 联网机器上
pip install huggingface_hub
python -c "
from huggingface_hub import snapshot_download
p = snapshot_download('BAAI/bge-m3',
    ignore_patterns=['onnx/*','imgs/*','*.jpg','*.jpeg','*.png','*.webp'])
print(p)"
tar czf bge-m3.tar.gz -C ~/.cache/huggingface .

# 构建机上:解到 ~/.cache/huggingface,构建时挂进去
# 并在 Dockerfile 的 models 阶段把 snapshot_download 换成
# COPY --from=<挂载源> 或 --mount=type=bind 的方式复用本地缓存
```

W2 若真遇到这种极端网络环境再动手改 Dockerfile,现在别提前埋这条路径。

### 磁盘不够 / `no space left on device`

```bash
docker system df            # 看谁占地方
docker builder prune        # 清构建缓存(最容易膨胀的一块)
docker image prune -a       # 清没被容器引用的镜像(会导致下次重新构建)
```

预算:镜像本体约 3~6GB(W2 把 torch / sentence-transformers 装进来后取上限)+ 构建缓存,
**至少留 15GB 空闲**再开始构建。

### 测试"单跑绿、全跑红"

多半是绕过了 `_isolated_settings`(比如自己在模块级调了 `get_settings()` 缓存住了配置)。
在用例里改环境变量后必须 `get_settings.cache_clear()`。

### `data/` 里的东西会进 git 吗

不会。`.gitignore` 忽略 `/data/*` 和 `backend/data/`,唯独放行 `data/demo/`(演示数据集要进仓库)。
新建演示数据目录记得放个 `.gitkeep`,否则 git 记不住空目录,别人冷启动会缺目录。

`backend/data/` 那条现在只是**兜住历史遗留目录**:数据根目录已经统一到仓库根 `data/`,
新装的机器不会再自己生成 `backend/data/`(除非你把 `GYT_DATA_DIR` 手动设成相对路径 `data`
再从 `backend/` 下启动 —— 别这么干,理由见 `.env.example` 里那一段)。
留着这条规则是因为老机器上那份还在,删掉规则会让一堆缓存/台账文件突然冒出来要求提交。
搬迁方式见上面「路线 A」那段。
