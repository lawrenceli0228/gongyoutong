# 工友通 · 开发便捷命令
# ---------------------------------------------------------------------------
# 用法:make <目标>;直接 `make` 会打印目标清单。
# 约定:后端命令一律经 uv 执行,保证跑在锁定的虚拟环境里,不污染系统 Python。
#       所有可调参数集中在下面的变量区,配方里不写字面量。
# ---------------------------------------------------------------------------

BACKEND_DIR    := backend
UV             := uv
PYTEST         := $(UV) run pytest
COMPOSE        := docker compose
# 基础档 = 演示/验收跑的那份(不挂源码、不热重载,和线上一致)。
# 开发档 = 叠在基础档上的覆盖件,只干两件事:挂宿主 backend/src 进去 + 去掉 --no-reload。
# -f 的顺序不能反,后面的文件覆盖前面的。为什么这么切,见 docker-compose.dev.yml 顶部注释。
# 变量名故意不叫 COMPOSE_FILE —— 那是 docker compose 自己认的环境变量名,撞了会互相干扰。
COMPOSE_BASE_FILE := docker-compose.yml
COMPOSE_DEV_FILE  := docker-compose.dev.yml
COMPOSE_DEV       := $(COMPOSE) -f $(COMPOSE_BASE_FILE) -f $(COMPOSE_DEV_FILE)
# 稳态档:在 dev 之上再叠一层,把 --no-reload 加回来。
# 手工点界面 / 演示 / 真机验收走它 —— 理由见 $(COMPOSE_NORELOAD_FILE) 顶部
# (macOS 的 VirtioFS 会持续产生假的文件变更事件,进程每 10 秒重启一次,
#  于是识图那种 20-60 秒的长请求永远做不完,而**日志里一行报错都没有**)。
COMPOSE_NORELOAD_FILE := docker-compose.noreload.yml
COMPOSE_STABLE    := $(COMPOSE_DEV) -f $(COMPOSE_NORELOAD_FILE)
# 覆盖率量哪些包。eval 也在内:它是三条评测门槛的执行者,判错分比模型答错更致命,
# 不上锁的话 W2 改 runner 时覆盖率掉光也不会有一条 CI 变红。与 pyproject 的 addopts 同源。
COV_PACKAGES   := gyt eval
COV_MIN        := 80
E2E_DIR        := tests/e2e
E2E_FLAG       := GYT_E2E=1
EVAL_MODULE    := eval.runner
# 跑哪一套评测:all / routing / safety / rag。用 ?= 是为了 `make eval SUITE=safety` 能覆盖。
# 三条门槛值不在这里 —— 一律由脚本从 gyt.config 的 eval_threshold_* 读,别在 Makefile 里抄第二份。
SUITE          ?= all
# 被测函数表。runner.py 自己一个 Agent 都不 import,全靠这一处注入(见 eval/hooks.py 顶部)。
EVAL_RUNNERS   := eval.hooks:RUNNERS
# eval-smoke 用的小样本数据集目录(1 行),只为探通链路,不是验收。
EVAL_SMOKE_DIR := eval/datasets/smoke
# 评测要真调模型,必须有真 Key。而 `python -m` 不像 langgraph-cli 那样会自己加载
# langgraph.json 里的 "env": "../.env" —— Settings 的 env_file 是按**进程工作目录**
# 解析的,评测在 backend/ 下跑,根本看不见仓库根的 .env。所以这里显式 --env-file。
# (真踩过:不加的话报的是 MissingAPIKeyError,而 Key 其实好好地填在 .env 里。)
CHECK_ENV       = test -f $(ENV_FILE) || { echo "[错误] 找不到 $(ENV_FILE)。评测要真调模型,先 cp $(ENV_TEMPLATE) $(ENV_FILE) 并填两家 Key。"; exit 1; }
FRONTEND_SETUP := scripts/setup-frontend.sh
FRONTEND_URL   := http://localhost:3000
BACKEND_URL    := http://localhost:2024
ENV_FILE       := .env
ENV_TEMPLATE   := .env.example
# 数据根目录,与 gyt.config 的 Settings.data_dir 同源:上传件 / 产物 / LLM 缓存 /
# SQLite 台账全落在这里。本机跑和容器跑(docker-compose.yml 把它挂到 /app/data)
# 读写的是**同一个**仓库根 data/ —— 别再让本机落 backend/data、容器落 <根>/data,
# 那会分裂成两份台账:界面上销了账,重启进容器一看还是 open。
DATA_DIR       := data
# 容器内运行用户的 uid,与 backend/Dockerfile 里创建的 gyt 用户保持一致
DATA_UID       := 10001
# 巡检记录的静态出口(见 serve-artifacts 目标)。端口号与
# scripts/frontend-overrides/tool-calls.tsx 的 ARTIFACT_BASE 同源,**要改一起改**:
# 那边写死了 http://127.0.0.1:8788,这边改了端口而那边没跟着改,界面上的巡检记录
# 卡片就会点开一片空白 —— 不报错,只是打不开,属于最难发现的那一类。
ARTIFACTS_PORT := 8788
# 挂在 DATA_DIR 下面,而不是自己再拼一遍 backend/data —— 产物目录只有一处真相,
# 就是 Settings.artifacts_dir(= data_dir / "artifacts")。用 ?= 是为了
# `make serve-artifacts ARTIFACTS_DIR=...` 能临时指到别处(比如翻某次验收留下的旧产物)。
ARTIFACTS_DIR  ?= $(DATA_DIR)/artifacts

# --- 起容器前的两条前置检查(up / dev-docker / test-docker 共用一份) ------------
#
# 前置 1:./data 必须由**当前用户**先建好。
#   不建的话 Docker daemon 会以 root 身份自动创建 0755 root:root,而容器里跑的是
#   uid $(DATA_UID) 的 gyt 用户 —— 首次访问 artifacts_dir / cache_dir 时
#   config.py 的 mkdir 会 PermissionError,现象是"服务健康但什么都干不了",很难查。
#   mkdir 用 || 而不是 ; —— 这段检查存在的理由就是"目录建不出来",
#   要是 mkdir 失败还继续往下走去起容器,等于把这条检查白写了。
PREPARE_DATA_DIR = mkdir -p $(DATA_DIR) \
	|| { echo "[错误] 建不出数据目录 $(DATA_DIR)。看看是不是磁盘满了、目录只读,"; \
	     echo "       或者有个同名文件占了位置。"; exit 1; }; \
	if [ "$$(uname)" = "Linux" ] && [ ! -w "$(DATA_DIR)" ]; then \
		echo "[提示] Linux 下容器内是非 root 用户,请执行:sudo chown -R $(DATA_UID):$(DATA_UID) $(DATA_DIR)"; \
	fi
# 前置 2:没有 .env 就没有 API Key,图在加载期就会抛 MissingAPIKeyError 并退出。
#   这里硬拦下来,比让人对着 crash 的容器猜半天强。
#   写成变量而不是每个目标各抄一份:文案漂移过一次,结果是不同目标报的错不一样,
#   队友照着搜也搜不到。(与上面的 CHECK_ENV 分开,那条是给评测用的、措辞不同。)
CHECK_ENV_KEYS = test -f $(ENV_FILE) \
	|| { echo "[错误] 找不到 $(ENV_FILE)。请先执行:cp $(ENV_TEMPLATE) $(ENV_FILE)"; \
	     echo "       然后填入 GYT_DEEPSEEK_API_KEY 与 GYT_MOONSHOT_API_KEY 再重试。"; exit 1; }

# --- test-docker(在容器里跑测试)专用 ----------------------------------------
# tests/ 被 backend/.dockerignore 挡在镜像外(那份清单里确实有 tests 这一行),
# 所以非挂不可,不挂就没有测试可跑。
# eval/ 则**不在** .dockerignore 里 —— 它本来就在镜像中(pyproject 的 addopts 有
# --cov=eval,少了它 pytest 起手就报错,而镜像里那份能满足)。这里仍然挂一份宿主的,
# 理由只有一个:让 eval 与 tests 同步走**你当前的源码**。只挂 tests 不挂 eval 的话,
# 改了 eval/hooks.py 再跑 test-docker,测的是镜像里烤死的旧 eval —— 一半新一半旧最难查。
# scripts/ 同理,而且更隐蔽:镜像里**有**一份(.dockerignore 没挡它,阶段 4 的
# `COPY . /app` 照抄进去了),所以不挂也不报错 —— 测的是那份**烤死的旧脚本**,
# 现象是「明明改了 scripts/acceptance_dates.py,test-docker 还是旧结果」。
# tests/unit/test_acceptance_{dates,trace}.py 按文件路径加载 scripts/ 下的模块,
# 必须挂宿主这份。(scripts/ 里其余脚本容器不执行,挂上去只读没有副作用。)
# uv.lock 挂进去是为了和镜像里那份对账,见 TEST_IN_CONTAINER。
IMAGE_LOCK_PATH := /app/uv.lock
HOST_LOCK_PATH  := /tmp/host-uv.lock
# ⚠️ auth.py 单独挂一条,别忘。它跟 langgraph.json 平级住在 backend/ 根,
#    **不在开发档那条 src/ 挂载覆盖的范围里** —— 少了这行,改完 auth.py 跑
#    `make test-docker`,容器里 import 到的仍是**镜像里烤死的旧版**,
#    于是「改了没生效」却一路绿灯。2026-08-11 加 auth 时踩到,当场补上。
TEST_MOUNTS     := -v "$(PWD)/$(BACKEND_DIR)/tests:/app/tests:ro" \
                   -v "$(PWD)/$(BACKEND_DIR)/eval:/app/eval:ro" \
                   -v "$(PWD)/$(BACKEND_DIR)/scripts:/app/scripts:ro" \
                   -v "$(PWD)/$(BACKEND_DIR)/auth.py:/app/auth.py:ro" \
                   -v "$(PWD)/$(BACKEND_DIR)/uv.lock:$(HOST_LOCK_PATH):ro"
# pytest 三件套。版本区间与 backend/pyproject.toml 的 [dependency-groups].dev **同源**,
# 那边动了这里要跟着动 —— 没法直接引用,因为容器里的 pyproject 是镜像烤死的那份。
# 不装 ruff:跑测试用不着,少下一个包。
TEST_DEPS       := --with "pytest>=9.1.1,<10" --with "pytest-asyncio>=1.4.0,<2" --with "pytest-cov>=7.1.0,<8"
# 容器里真正执行的那条命令,两步:
#   ① 对账:宿主的 uv.lock 与镜像里那份必须一字不差。不一致就说明镜像比代码旧,
#      而镜像旧的表现是一连串 ModuleNotFoundError(本机现有镜像就缺 torch/ezdxf/docx),
#      不拦的话人会去查测试、查挂载,唯独想不到是镜像该重建了。
#   ② 跑 pytest:三件套用 `uv run --with` 装进一个**临时叠加环境**,不碰 /app/.venv ——
#      .venv 是 root 所有而容器跑的是 uid $(DATA_UID),想装也装不进去;叠加环境随容器消失,
#      不会把测试依赖沉淀进任何镜像。代价是每次现下几个包(实测几秒)。
TEST_IN_CONTAINER = sh -c 'cmp -s $(IMAGE_LOCK_PATH) $(HOST_LOCK_PATH) || { echo "[错误] 镜像里的依赖清单和 $(BACKEND_DIR)/uv.lock 对不上,镜像比代码旧。" >&2; echo "       先重建再跑:$(COMPOSE) build backend" >&2; exit 1; }; exec uv run --no-sync $(TEST_DEPS) pytest'

.DEFAULT_GOAL := help

# 所有目标都得在这儿登记,一个都不能漏:漏了的目标一旦和同名文件/目录撞上,
# make 会认为"文件已存在且是最新的"而直接跳过,现象是命令看着跑了其实什么都没干。
# (build-knowledge 就漏过一次 —— 合并 knowledge agent 时忘了加。)
.PHONY: help setup dev dev-docker dev-docker-stable test test-docker cov build-knowledge eval eval-smoke \
        lint lint-ci fmt up down e2e frontend serve-artifacts attendance-clean test-frontend

help: ## 打印所有可用目标
	@# 宽度按最长的目标名留(serve-artifacts 15 字),窄了会把说明挤得参差不齐。
	@echo "工友通 · 可用命令:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-16s %s\n", $$1, $$2}'

setup: ## 安装后端依赖(uv sync,含 dev 组)
	cd $(BACKEND_DIR) && $(UV) sync
	@echo "[完成] 依赖就绪。下一步:把 .env.example 复制为 .env 并填两家 API Key。"

dev: ## 本地起后端(LangGraph dev server,:2024,改代码自动重载)
	@# --allow-blocking 必须加,而且要与 backend/Dockerfile 的启动命令保持一致。
	@# 不加的话 langgraph 的 blockbuster 会把同步 IO 判成违规并抛 BlockingError,
	@# 而 config.cache_dir / artifacts_dir 是「访问即 mkdir」的属性 ——
	@# 结果是**磁盘缓存的读写全部失败**,每次提问都真的掏钱调模型,
	@# 而日志里只有一句 "Background run succeeded",看不出任何异常。
	@# (踩过:本地 make dev 里 Safety Agent 每次都要等 60 秒,而 docker 里是秒回,
	@#  差别就在这一个参数上。)
	cd $(BACKEND_DIR) && $(UV) run langgraph dev --allow-blocking

dev-docker: ## 容器里起后端,改 backend/src 的 .py 自动重载(本机装不上依赖时走这条)
	@# 与上面的 dev 的分工:dev 在**宿主**的 .venv 里跑,前提是本机装得上全部依赖
	@# (Intel Mac 装不上 torch,这条直接跑不起来);dev-docker 在容器里跑,
	@# 宿主只负责编辑文件 —— 两人两台机(Intel Mac + Windows)统一走这条,
	@# 省掉"我这儿跑不起来"的来回。
	@#
	@# 叠加档只干两件事:挂 $(BACKEND_DIR)/src → /app/src(只读)+ 去掉 CMD 里的 --no-reload。
	@# 判据别记错(uvicorn 的监视清单写死是 *.py,查过源码):
	@#   改 .py        → 自动重载,几秒生效
	@#   改 prompt.md  → **不会**自动重载,要 `$(COMPOSE_DEV) restart backend`(秒级)
	@# 但两种都**不用重建镜像**(分钟级那种)—— 这才是这条目标省下来的东西。
	@# 改 pyproject.toml / uv.lock / langgraph.json / Dockerfile 仍然要重建。
	@# 完整来龙去脉见 $(COMPOSE_DEV_FILE) 顶部注释。
	@#
	@# --build 不能省(2026-08-09 差点栽在这儿):挂载只换掉 /app/src 里的**源码**,
	@# 换不掉镜像层里的**依赖**。镜像但凡比 pyproject/uv.lock 旧一步,容器就会在
	@# 图加载期 ModuleNotFoundError(实测:旧镜像缺 ezdxf,`import gyt.graph` 当场炸),
	@# 配上基础档的 restart: on-failure:3 重启三次后停住 —— 而 `up -d` 早就返回 0,
	@# 下面那句"[已启动]"照打不误,人对着一个根本没起来的服务查半天。
	@# 代价:依赖没动时 --build 全程命中层缓存,实测几秒;动了才真重装,该等的。
	@$(PREPARE_DATA_DIR)
	@$(CHECK_ENV_KEYS)
	$(COMPOSE_DEV) up -d --build
	@echo "[已启动] 后端 API:$(BACKEND_URL)    健康检查:$(BACKEND_URL)/ok"
	@echo "[热重载] 改 $(BACKEND_DIR)/src 下的 .py 自动生效;改 .md 用下面这条重启(不重建镜像):"
	@echo "         $(COMPOSE_DEV) restart backend"
	@echo "[没反应] 宿主文件系统不传文件变更事件时(WSL2/网络盘常见),改用轮询重来:"
	@echo "         GYT_WATCH_POLLING=1 make dev-docker"
	@echo "[看日志] $(COMPOSE_DEV) logs -f backend"
	@echo "[停掉]   make down"

dev-docker-stable: ## 容器里起后端,**关掉热重载**(手工点界面 / 演示 / 真机验收走这条)
	@# 与 dev-docker 的分工只有一条:那边改 .py 自动重载,这边不。
	@#
	@# 什么时候必须走这条(2026-08-16 实测):macOS 上 Docker Desktop 的绑定挂载
	@# 会**持续产生假的文件变更事件**(日志每 10 秒一次 "12 changes detected",
	@# 而容器里 find -newermt 查下去一个文件都没真变)。进程于是每 10 秒重启一次,
	@# 识图那种 20-60 秒的长请求永远做不完 —— 后端其实干完了、隐患已经进库,
	@# 但 SSE 流断了,界面永远停在「正在忙」。
	@#
	@# 🔴 **日志里没有任何一行报错。** 别指望从日志看出来,直接换这条。
	@#
	@# 代价:改 backend/ 下的 .py 不再自动生效,要重启(秒级,不重建镜像):
	@#     $(COMPOSE_STABLE) up -d --no-deps --force-recreate backend
	@# 手工点界面时这个代价是零 —— 那会儿本来就不该有人在改代码。
	@$(PREPARE_DATA_DIR)
	@$(CHECK_ENV_KEYS)
	$(COMPOSE_STABLE) up -d --build
	@echo "[已启动] 后端 API:$(BACKEND_URL)    健康检查:$(BACKEND_URL)/ok"
	@echo "[稳态]   热重载已关。改了 backend/ 下的 .py 之后手动重启(秒级,不重建镜像):"
	@echo "         $(COMPOSE_STABLE) up -d --no-deps --force-recreate backend"
	@echo "[看日志] $(COMPOSE_STABLE) logs -f backend"
	@echo "[停掉]   make down"

test: ## 跑单元/集成测试(不含 E2E,秒级)
	cd $(BACKEND_DIR) && $(PYTEST)

test-docker: ## 在容器里跑测试(本机装不上 torch 时的替代路径,比 make test 慢十几秒)
	@# 为什么不能直接 `docker run ... pytest`:镜像是 `uv sync --frozen --no-dev` 装的,
	@# 里面**没有 pytest**;backend/.dockerignore 又把 tests/ 挡在镜像外。
	@# 这条目标把缺的补齐(细节见变量区的 TEST_MOUNTS / TEST_DEPS / TEST_IN_CONTAINER)。
	@#
	@# 走 $(COMPOSE_DEV) 而不是裸 $(COMPOSE),是为了蹭开发档那条 src 挂载 ——
	@# 跑的是你**当前**的源码,不是镜像里烤死的那份;否则"改完测试还是旧结果",
	@# 比没有这个目标更害人。
	@#
	@# 不检查 .env:测试全程 mock,tests/conftest.py 会把进程里的 GYT_* 全清掉、
	@# 掐断 .env 这条来源、再塞两个假 Key,所以有没有真 Key 都不影响结果。
	@# 但 ./data 还是得先建好 —— 基础档里那条绑定挂载照样生效。
	@$(PREPARE_DATA_DIR)
	$(COMPOSE_DEV) run --rm $(TEST_MOUNTS) backend $(TEST_IN_CONTAINER)

cov: ## 跑测试 + 覆盖率报告,低于 80% 直接失败(与 CI 同一把尺子)
	cd $(BACKEND_DIR) && $(PYTEST) $(addprefix --cov=,$(COV_PACKAGES)) --cov-report=term-missing \
		--cov-report=html --cov-fail-under=$(COV_MIN)
	@echo "[报告] HTML 覆盖率:$(BACKEND_DIR)/htmlcov/index.html"

build-knowledge: ## 建/更新规范知识库(PDF 切分 + BGE-M3 向量化 → data/chroma)
	@# 规范 PDF 放 data/demo/docs/。首次约 15 分钟(下 2.2GB 权重 + 抽嵌全书),
	@# 之后 manifest 命中秒过(规范文件有增改才重嵌)。幂等,可反复跑。
	@# 落到 $(DATA_DIR)/chroma —— data_dir 现在按 config.py 的文件位置推导仓库根,
	@# 与在哪个目录敲 make 无关(以前是相对路径,本机会落 backend/data,见变量区 DATA_DIR)。
	@# ⚠️ 这条配方跑在**宿主** .venv 里,Intel Mac 装不上 torch 就跑不了 —— 改用下面的容器写法。
	@#
	@# 容器/生产:改用 `$(COMPOSE) run --rm backend python -m gyt.agents.knowledge.ingest`
	@#   把挂载的 ./data 卷先建好,再 make up。
	@#   ⚠️ **别在容器里靠 GYT_KNOWLEDGE_PREBUILD_AT_STARTUP 启动时自动建** ——
	@#      15 分钟的启动建库会拖垮 healthcheck(start_period 120s)→ 容器反复重启永不 healthy。
	@#      而镜像构建期烤索引也没用:./data 是绑定挂载,运行期会把镜像里那份盖掉。
	cd $(BACKEND_DIR) && $(UV) run python -m gyt.agents.knowledge.ingest

eval: ## 跑评测门槛(默认三套全跑;make eval SUITE=safety 只跑一套)
	@# 三套**都已接上**(eval/hooks.py:RUNNERS 里 safety/routing/rag 三个键齐)。
	@# 这里以前写着「routing / rag 还没接,会打印 SKIP —— 那是正常状态」,现在不是了:
	@# 不带 SUITE 跑就是**三套全真跑**,今天再看到 SKIP 说明数据集或注入点出了问题,别当正常。
	@# ⚠️ 三套都花钱,而且花法不同,别只按 safety 估:
	@#    safety  30 张 —— **真的调 kimi-k3**,实测单张 10~60 秒,串行约 22 分钟。
	@#    routing 22 行 —— 每行一次 DeepSeek 文本调用(只跑第一跳,拿到首个交接就停)。
	@#    rag     20 行 —— 直调 search_regulation 不过模型,但**要求本机向量库已建好**
	@#                     (先 make build-knowledge),没建会整套判失败。
	@# safety 那 22 分钟只花一次:第二轮起命中磁盘缓存,秒级返回、零花费 ——
	@# 但改了 vision_prompt.md 正文或 prompt_version 会让缓存全失效,又是一轮(见 TODO-11)。
	@# 先用 `make eval-smoke` 拿 1 张探链路,别一上来就押 22 分钟。
	@# (2026-08-11:这里原本把 safety 的花费警告连着写了两遍 —— 本文件自己的规矩是
	@#  「同一句话别各抄一份,文案漂移过一次」,已合并成上面这一处。)
	@$(CHECK_ENV)
	cd $(BACKEND_DIR) && $(UV) run --env-file ../$(ENV_FILE) \
		python -m $(EVAL_MODULE) --suite $(SUITE) --runners $(EVAL_RUNNERS)

eval-smoke: ## 只跑 1 张照片探通链路(几毛钱,几十秒),别拿全量试水
	@# 把样本量下限临时压到 1,并用一个只有 1 行的临时数据集目录。
	@# 这条**不是**验收,只用来确认「照片找得到 / Key 有效 / 判分接得上」。
	@$(CHECK_ENV)
	cd $(BACKEND_DIR) && GYT_EVAL_MIN_ROWS_SAFETY=1 $(UV) run --env-file ../$(ENV_FILE) \
		python -m $(EVAL_MODULE) --suite safety --runners $(EVAL_RUNNERS) \
		--datasets-dir $(EVAL_SMOKE_DIR) --verbose

lint: ## 静态检查(ruff,只报不改)
	cd $(BACKEND_DIR) && $(UV) run ruff check .

lint-ci: ## 校验 GitHub Actions 工作流语法(改过 .github/workflows/ 后必跑)
	@# 为什么必须在本地跑:workflow 文件不合法时,GitHub 压根不会创建 job,
	@# 表现为 0 秒失败、无日志、check-runs API 也查不到注解 —— 靠 CI 自己是抓不到的。
	@# 典型例子:job 级 env 里写 $${{ runner.temp }}(runner 上下文只在 step 级可用)。
	@docker run --rm -v "$(PWD):/repo" -w /repo rhysd/actionlint:latest -no-color \
		.github/workflows/*.yml && echo "[1/2] actionlint 语法检查通过"
	@# 第二道:核实每个 uses: 引用的 tag 真实存在。
	@# actionlint 不做这件事(它不联网),但 tag 不存在会让 job 挂在 "Set up job",
	@# 报 "unable to find version" —— 而且要等 push 到 GitHub 才看得到。
	@# 易错点:releases/latest 查到 v9.0.0 不代表存在浮动 tag v9,
	@# 有的 action 推浮动大版本 tag,有的不推。必须查 tags 而不是 releases。
	@grep -hoE 'uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+' .github/workflows/*.yml \
		| sed 's/uses: //' | sort -u | while IFS='@' read -r repo ref; do \
			if gh api "repos/$$repo/git/ref/tags/$$ref" >/dev/null 2>&1; then \
				echo "  OK   $$repo@$$ref"; \
			else \
				echo "  FAIL $$repo@$$ref  <- 这个 tag 不存在,CI 会挂在 Set up job"; \
				echo "       可用 tag:$$(gh api "repos/$$repo/tags?per_page=6" --jq '[.[].name]|join(\" \")' 2>/dev/null)"; \
				exit 1; \
			fi; \
		done && echo "[2/2] action tag 存在性检查通过"

fmt: ## 自动格式化 + 可自动修的 lint 问题(ruff)
	cd $(BACKEND_DIR) && $(UV) run ruff format . && $(UV) run ruff check --fix .

up: ## 起后端容器(后台运行;前端在 ui profile 里,默认不起)
	@# 两条前置检查(数据目录 + .env)提到变量区了,理由和踩坑记录都在那儿,
	@# dev-docker / test-docker 共用同一份文案 —— 别在这里再抄一遍。
	@$(PREPARE_DATA_DIR)
	@$(CHECK_ENV_KEYS)
	@# 注意这里是**纯基础档**:不挂源码、不热重载,跑的是镜像里烤死的那份代码。
	@# 演示和验收必须走这条(要的就是"和线上一样");日常改代码走 make dev-docker。
	$(COMPOSE) up -d
	@echo "[已启动] 后端 API:$(BACKEND_URL)    健康检查:$(BACKEND_URL)/ok"
	@echo "[要前端] 先 make frontend 备好 frontend/,再 $(COMPOSE) --profile ui up -d"
	@echo "         起来后浏览器开 $(FRONTEND_URL)"
	@echo "[看日志] $(COMPOSE) logs -f backend"

down: ## 停整栈并清理容器(挂载的 ./data 不会被删;dev-docker 起的也一并收掉)
	@# 只带基础档也能停掉 dev-docker 起的容器:两个档的项目名都是 docker-compose.yml
	@# 顶部那个 `name: gyt`,compose 是按项目名找容器的,跟当时叠了几个 -f 无关。
	$(COMPOSE) down --remove-orphans

e2e: ## 冷启动冒烟 E2E(会 compose up --build:首跑要拉 2.2GB 权重,10~30 分钟,且会产生真实调用费用)
	@# 与 up 目标同样的两条前置:数据目录 + 真实 .env(冒烟会真的起容器、真的调模型)
	@mkdir -p $(DATA_DIR)
	@test -f $(ENV_FILE) \
		|| { echo "[错误] 找不到 $(ENV_FILE)。冷启动冒烟要真起容器,必须先 cp $(ENV_TEMPLATE) $(ENV_FILE) 并填真 Key。"; exit 1; }
	cd $(BACKEND_DIR) && $(E2E_FLAG) $(PYTEST) $(E2E_DIR) -m e2e -v

frontend: ## 拉取并初始化前端(agent-chat-ui)
	@test -f $(FRONTEND_SETUP) \
		|| { echo "[错误] 找不到 $(FRONTEND_SETUP),前端初始化脚本还没就位"; exit 1; }
	bash $(FRONTEND_SETUP)

serve-artifacts: ## 起只读静态服务,让聊天界面里的巡检记录能点开(演示前和后端一起起)
	@# 为什么需要它:docx 落在 $(ARTIFACTS_DIR),而 langgraph.json 只声明了图 ——
	@# **没有任何 HTTP 端点能把文件给出去**。于是「拍照自动出 Word」这个卖点,
	@# 在界面上的最终形态曾经只是折叠 JSON 里的一个 path 字符串。
	@# 前端覆盖件 tool-calls.tsx 的巡检记录卡片指向的就是这个端口。
	@#
	@# 和哪条启动路径一起起都行(make dev **或** make dev-docker / make up)——
	@# 它读的是宿主 $(ARTIFACTS_DIR),而容器把同一个 ./data 挂进去,两边是同一份产物。
	@# 别写成「和 make dev 一起起」:Intel Mac 上 make dev 根本跑不起来(装不上 torch)。
	@#
	@# 只绑 127.0.0.1 —— 与 docker-compose.yml 同一条红线。这里面是工地现场照片和
	@# 巡检记录(含可识别人脸,见 TODO-22),绑 0.0.0.0 等于把它们发给整个局域网。
	@# 绑定地址**写死在脚本的 BIND_HOST 常量里,没有命令行开关** —— 想突破得改代码、得过 review。
	@#
	@# 2026-08-11 从 `python3 -m http.server` 换成自己的脚本,为的是多一条
	@# `GET /by-id/<32位编号>`:产物在盘上是 <UTC日期>/<32位id>.<扩展名>,而前端手里
	@# 只有编号、没有日期段 —— 历史里的照片(human.tsx 覆盖件)就靠这条路取件。
	@# 老路径 `/<日期>/<文件名>` 原样保留,巡检记录卡片(tool-calls.tsx)用的是那条。
	@# mkdir 和启动横幅都由脚本自己做了,这里不再重复打印。
	@# 仍然用裸 python3(不走 uv / venv):脚本纯 stdlib,Intel Mac 装不上 torch 也不影响。
	python3 scripts/serve_artifacts.py --port $(ARTIFACTS_PORT) --directory $(ARTIFACTS_DIR)

attendance-clean: ## 清理到期考勤凭证图与孤儿(默认演练;真删 make attendance-clean APPLY=1)
	@# 两段活:①留存到期(GYT_ATTENDANCE_RETENTION_DAYS,默认 90 天)删图、行置 NULL;
	@# ②孤儿清扫(register 成功但没写进台账的图),带 1 小时老化窗口防误删在途请求。
	@# 只碰 kind=ATTENDANCE 的产物,巡检链照片(PHOTO)永不涉及 —— 细节见
	@# backend/src/gyt/attendance/cleanup.py 头注。退出码:0=干净,1=有该删没删掉的(接告警用)。
	@# ⚠️ Intel Mac 本机 uv run 会因 torch 装不上而失败(见 CLAUDE.md),
	@#    本机想跑用:cd backend && .venv/bin/python -m gyt.attendance.cleanup
	cd backend && uv run --env-file ../.env python -m gyt.attendance.cleanup $(if $(APPLY),--apply,)

test-frontend: ## 跑前端纯函数测试(vitest,scripts/frontend-tests,不碰 frontend/)
	@# 测的是 scripts/frontend-overrides/checkin-lib.ts —— 打卡链前端的全部可测逻辑
	@# (Base64URL 编码、geo header、错误归一化、eventId 存取)都下沉在这个零依赖纯 TS 里。
	@# 七条编码测试向量与 backend/tests/unit/test_checkin_api.py 的 ROUNDTRIP_VECTORS
	@# 同源(两边注释互指):后端改了编码,这里会先红。
	@# --frozen-lockfile:锁文件就是契约,CI 与本机装的必须一字不差。
	@# tsc 那一步是 2026-08-18 补的。补的理由是当天实证:往包里加了三个 .mjs 工具,
	@# vitest 全绿而 `tsc --noEmit` 当场十几条红(.mjs 没进 include → import 进来全是 any),
	@# **而没有任何一道关卡会说话**。CLAUDE.md 早就写着「加 lib 要同步 tsconfig 的 include」,
	@# 但那是靠人记的 —— 靠人记的结果就是这次漏了。
	@# 静默降级的表现:测试里写错字段名也不报,覆盖看着在、其实是 any 在放行。
	cd scripts/frontend-tests && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm install --frozen-lockfile \
		&& COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm tsc --noEmit \
		&& COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm vitest run
