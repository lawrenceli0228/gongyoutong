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
FRONTEND_SETUP := scripts/setup-frontend.sh
FRONTEND_URL   := http://localhost:3000
BACKEND_URL    := http://localhost:2024
ENV_FILE       := .env
ENV_TEMPLATE   := .env.example
DATA_DIR       := data
# 容器内运行用户的 uid,与 backend/Dockerfile 里创建的 gyt 用户保持一致
DATA_UID       := 10001

.DEFAULT_GOAL := help

.PHONY: help setup dev test cov eval lint lint-ci fmt up down e2e frontend

help: ## 打印所有可用目标
	@echo "工友通 · 可用命令:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

setup: ## 安装后端依赖(uv sync,含 dev 组)
	cd $(BACKEND_DIR) && $(UV) sync
	@echo "[完成] 依赖就绪。下一步:把 .env.example 复制为 .env 并填两家 API Key。"

dev: ## 本地起后端(LangGraph dev server,:2024,改代码自动重载)
	cd $(BACKEND_DIR) && $(UV) run langgraph dev

test: ## 跑单元/集成测试(不含 E2E,秒级)
	cd $(BACKEND_DIR) && $(PYTEST)

cov: ## 跑测试 + 覆盖率报告,低于 80% 直接失败(与 CI 同一把尺子)
	cd $(BACKEND_DIR) && $(PYTEST) $(addprefix --cov=,$(COV_PACKAGES)) --cov-report=term-missing \
		--cov-report=html --cov-fail-under=$(COV_MIN)
	@echo "[报告] HTML 覆盖率:$(BACKEND_DIR)/htmlcov/index.html"

eval: ## 跑评测门槛(默认三套全跑;make eval SUITE=safety 只跑一套)
	@# 现在跑会全部打印 SKIP —— 这是**正常状态**,不是坏了:
	@#   · 数据集还是仓库自带的示例占位行(标着"待替换",数字全是编的);
	@#   · 被测 Agent 还没接进来(要用 --runners 模块:属性 注入)。
	@# 等数据填完、Agent 接好,同一条命令就会真的出分,低于门槛自动 exit 非 0。
	cd $(BACKEND_DIR) && $(UV) run python -m $(EVAL_MODULE) --suite $(SUITE)

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
	@# 前置 1:./data 必须由**当前用户**先建好。
	@# 不建的话 Docker daemon 会以 root 身份自动创建 0755 root:root,
	@# 而容器里跑的是 uid $(DATA_UID) 的 gyt 用户 —— 首次访问 artifacts_dir / cache_dir 时
	@# config.py 的 mkdir 会 PermissionError,现象是"服务健康但什么都干不了",很难查。
	@mkdir -p $(DATA_DIR)
	@if [ "$$(uname)" = "Linux" ] && [ ! -w "$(DATA_DIR)" ]; then \
		echo "[提示] Linux 下容器内是非 root 用户,请执行:sudo chown -R $(DATA_UID):$(DATA_UID) $(DATA_DIR)"; \
	fi
	@# 前置 2:没有 .env 就没有 API Key,图在加载期就会抛 MissingAPIKeyError 并退出。
	@# 这里硬拦下来,比让人对着 crash 的容器猜半天强。
	@test -f $(ENV_FILE) \
		|| { echo "[错误] 找不到 $(ENV_FILE)。请先执行:cp $(ENV_TEMPLATE) $(ENV_FILE)"; \
		     echo "       然后填入 GYT_DEEPSEEK_API_KEY 与 GYT_MOONSHOT_API_KEY 再重试。"; exit 1; }
	$(COMPOSE) up -d
	@echo "[已启动] 后端 API:$(BACKEND_URL)    健康检查:$(BACKEND_URL)/ok"
	@echo "[要前端] 先 make frontend 备好 frontend/,再 $(COMPOSE) --profile ui up -d"
	@echo "         起来后浏览器开 $(FRONTEND_URL)"
	@echo "[看日志] $(COMPOSE) logs -f backend"

down: ## 停整栈并清理容器(挂载的 ./data 不会被删)
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
