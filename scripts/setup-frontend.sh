#!/usr/bin/env bash
# =============================================================================
# 工友通(GYT)· 前端初始化脚本
#
# 干什么:把 LangChain 官方的 agent-chat-ui 拉到仓库的 frontend/ 目录,
#         剥掉它自带的 .git(我们不要嵌套仓库/子模块),
#         再补上三个上游没带、但本项目需要的文件:
#           frontend/.env           指向本地后端 :2024,图 ID = gyt
#           frontend/Dockerfile     docker-compose.yml 的 frontend 服务要用
#           frontend/.dockerignore  别把 node_modules / .next 塞进构建上下文
#
# 为什么前端不进 git:上游仓库会持续更新,vendored 一整份进来只会让 diff 失控。
#         需要时重跑本脚本即可,一分钟的事。
#
# 用法:
#   bash scripts/setup-frontend.sh            # 常规:已存在则跳过,不覆盖
#   bash scripts/setup-frontend.sh --force    # 强制:重新生成三个配置文件
#                                             # (仍不会删已存在的 frontend/ 源码)
#
# 执行流程与幂等判定:
#
#      开始
#        │
#        ▼
#   frontend/package.json 存在?
#        │
#    ┌───┴────────────────────────┐
#   是│                          否│
#     ▼                            ▼
#  跳过 clone            frontend/ 目录存在且非空?
#  (打印提示)                  │
#     │                    ┌─────┴─────┐
#     │                  是│          否│
#     │                    ▼            ▼
#     │              报错退出      clone 到临时目录
#     │            (半成品目录,     ──► 删掉 .git
#     │              让人工决定)    ──► 整体 mv 成 frontend/
#     │                               (临时目录做中转:clone 失败时
#     │                                不会留下半个 frontend/)
#     └──────────────┬─────────────────┘
#                    ▼
#          写三个生成文件(逐个判断是否已存在)
#                    │
#                    ▼
#             打印下一步中文提示
#
# 注意:仓库根目录路径含空格(buildMate AI Agent),
#       本文件里所有路径变量必须带双引号,少一个引号就会在别人机器上炸。
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# 可调常量(集中在顶部,下面的正文里不许再出现字面量)
# 约定:BACKEND_URL / ASSISTANT_ID 必须与这些地方保持一致 ——
#   ASSISTANT_ID  <-> backend/langgraph.json 里 graphs 的键
#   BACKEND_URL   <-> backend/Dockerfile 的 EXPOSE 与 docker-compose.yml 的端口映射
# -----------------------------------------------------------------------------
readonly FRONTEND_REPO="https://github.com/langchain-ai/agent-chat-ui.git"
readonly CLONE_DEPTH=1
readonly BACKEND_URL="http://localhost:2024"
readonly ASSISTANT_ID="gyt"
readonly FRONTEND_PORT=3000
# 前端基础镜像标签。选 22 是因为上游 package.json 里 @types/node 是 ^22,
# 且 Next 15 官方支持 Node 20/22;换标签前先确认上游没升 Next 大版本。
readonly NODE_IMAGE_TAG="22-slim"
# 打卡二维码库(checkin 的桌面端面板 qrcode.tsx 用,W7 方案 §4.4)。
# 钉死精确版本、装时 --save-exact:Dockerfile 用 --frozen-lockfile,浮动版本会让
# 「本机装出的锁」与「新机器重跑装出的锁」不同。4.2.0 = 2026-08-15 从 npm 查到的
# latest(2024-12-11 发布,此后没动过)。升级前先确认 QRCodeSVG 的 props 没变
# (v4 把 includeMargin 换成了 marginSize,qrcode.tsx 用的是后者)。
readonly QRCODE_REACT_VERSION="4.2.0"

# 简→繁(香港)转换库(W12 第一批,hant-convert.tsx 用)。同样钉死精确版本,
# 理由与 qrcode.react 一致(Dockerfile 用 --frozen-lockfile)。
# 1.4.1 = 2026-08-17 从 npm 查到的 latest。
#
# ⚠️ **它是 lazy import 进来的**(`import("opencc-js/cn2t")`),所以不进首屏包 ——
#    438 KB gzipped 只在用户切到繁體时拉一次,之后浏览器缓存。
# 🔴 **不许为了省体积换成 core + 单字表那个 25 KB 的精简档。** 实测(方案 §5.1)
#    精简档会把「签发」转成「籤發」(抽籤的籤)、「复查」转成「復查」——
#    那两个正是监理链的核心动作,而这个错没有任何测试会发现。
readonly OPENCC_JS_VERSION="1.4.1"

# -----------------------------------------------------------------------------
# 路径解析:一律从脚本自身位置推导,允许在任意工作目录下执行
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly FRONTEND_DIR="${REPO_ROOT}/frontend"

# -----------------------------------------------------------------------------
# 输出小工具(全中文,让不熟悉命令行的人也知道发生了什么)
# -----------------------------------------------------------------------------
log_step() { printf '\n[步骤] %s\n' "$1"; }
log_ok() { printf '  ✓ %s\n' "$1"; }
log_skip() { printf '  · %s\n' "$1"; }
log_warn() { printf '  ! %s\n' "$1" >&2; }
die() {
  printf '\n[失败] %s\n' "$1" >&2
  exit 1
}

# 把模板里的 @@占位符@@ 换成上面的常量。
# 这样常量只在顶部写一遍,模板正文里改不出不一致。
render_template() {
  sed \
    -e "s|@@BACKEND_URL@@|${BACKEND_URL}|g" \
    -e "s|@@ASSISTANT_ID@@|${ASSISTANT_ID}|g" \
    -e "s|@@FRONTEND_PORT@@|${FRONTEND_PORT}|g" \
    -e "s|@@NODE_IMAGE_TAG@@|${NODE_IMAGE_TAG}|g"
}

# -----------------------------------------------------------------------------
# 参数解析
# -----------------------------------------------------------------------------
FORCE_REGEN="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE_REGEN="true"
      shift
      ;;
    -h | --help)
      sed -n '2,40p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      die "未知参数:$1(可用参数:--force / --help)"
      ;;
  esac
done
readonly FORCE_REGEN

# -----------------------------------------------------------------------------
# 步骤 0:前置检查
# -----------------------------------------------------------------------------
log_step "检查前置条件"
command -v git >/dev/null 2>&1 || die "没找到 git 命令,请先安装 git 再重试。"
log_ok "git 已就位"

# -----------------------------------------------------------------------------
# 步骤 1:拉取上游前端(幂等)
# -----------------------------------------------------------------------------
log_step "准备前端源码:${FRONTEND_DIR}"

if [[ -f "${FRONTEND_DIR}/package.json" ]]; then
  log_skip "frontend/ 已存在且看起来完整,跳过 clone。"
  log_skip "想拉最新版就先删掉整个 frontend/ 目录再重跑本脚本。"
else
  # 目录存在但没有 package.json = 上次 clone 中途失败留下的残骸。
  # 不自动删:里面可能有人手改过的东西,交给人判断,别替用户做决定。
  if [[ -d "${FRONTEND_DIR}" ]] && [[ -n "$(ls -A "${FRONTEND_DIR}" 2>/dev/null)" ]]; then
    die "frontend/ 目录已存在但缺少 package.json,像是上次没拉完。
      请确认里面没有你要留的东西,然后手动执行:rm -rf \"${FRONTEND_DIR}\"
      之后重跑本脚本。"
  fi

  # 先 clone 到同级临时目录,成功后再整体改名 ——
  # 中途断网/Ctrl-C 都不会在 frontend/ 留下半成品(上面那条报错分支就是给它兜底的)。
  TMP_CLONE_DIR="$(mktemp -d "${REPO_ROOT}/.frontend-clone.XXXXXX")"
  # 单引号:让变量在 trap 触发时才展开,路径里有空格/特殊字符也不会被拆开
  trap 'rm -rf -- "${TMP_CLONE_DIR}" 2>/dev/null || true' EXIT

  printf '  正在下载 %s(浅克隆,只取最新一次提交)...\n' "${FRONTEND_REPO}"
  git clone --depth "${CLONE_DEPTH}" --quiet "${FRONTEND_REPO}" "${TMP_CLONE_DIR}" \
    || die "克隆失败。检查网络,或给 git 配好代理后重试。"

  # 删掉上游的 .git:我们要的是一份代码快照,不是嵌套仓库。
  # 留着会让根仓库把它当 submodule 处理,git status 一片混乱。
  rm -rf -- "${TMP_CLONE_DIR}/.git"

  mv -- "${TMP_CLONE_DIR}" "${FRONTEND_DIR}"
  trap - EXIT
  log_ok "前端源码已就位,并已剥离上游 .git"
fi

# -----------------------------------------------------------------------------
# 步骤 2:生成本项目特有的配置文件
#
# write_generated <目标文件> —— 从标准输入读内容。
# 已存在则跳过(除非 --force),绝不静默覆盖别人改过的文件。
# -----------------------------------------------------------------------------
# write_generated <目标文件> [always]
#
#   第二个参数传 "always" 时表示**安全相关生成物**,内容与模板不一致就无条件覆盖并告警。
#   为什么要这个开关:上游 agent-chat-ui 自带一份只有 4 行的 .dockerignore,
#   而"存在即跳过"意味着**本项目的加固版永远赢不了先到的上游弱版本**,
#   只在日志里留一行没人看的"已存在,保留原样"。
#   代价很实在:上游那 4 行只挡 .env,挡不住 Next.js 约定的 .env.local /
#   .env.production.local —— 而上游 .env.example 里明说 LANGSMITH_API_KEY 就该放那儿。
#   Dockerfile 的 `COPY . .` + `COPY --from=build /app /app` 会把它一路搬进最终镜像层,
#   镜像给谁谁就能明文读出密钥,推到 registry 就再也收不回来。
#
#        目标文件存在?
#          |
#          +-- 否 -----------------------------► 写入
#          +-- 是 --+-- FORCE_REGEN=true -------► 覆盖
#                   +-- 模式=always 且内容不同 --► 覆盖 + 告警(安全兜底)
#                   +-- 其余 --------------------► 跳过(不动用户改过的东西)
write_generated() {
  local target="$1"
  local mode="${2:-keep}"
  local name
  name="$(basename -- "${target}")"

  if [[ -f "${target}" ]] && [[ "${FORCE_REGEN}" != "true" ]]; then
    if [[ "${mode}" != "always" ]]; then
      cat >/dev/null # 把标准输入吃掉,免得上游 heredoc 写管道时收到 SIGPIPE
      log_skip "${name} 已存在,保留原样(要重新生成就加 --force)"
      return 0
    fi

    local rendered
    rendered="$(render_template)"
    if [[ "${rendered}" == "$(cat -- "${target}")" ]]; then
      log_skip "${name} 已存在且与模板一致,无需改动"
      return 0
    fi
    printf '%s\n' "${rendered}" >"${target}" || die "写入 ${target} 失败,检查目录权限。"
    log_warn "${name} 与项目模板不一致(多半是上游自带的弱版本),已按安全要求覆盖。"
    return 0
  fi

  render_template >"${target}" || die "写入 ${target} 失败,检查目录权限。"
  log_ok "已生成 ${name}"
}

log_step "生成前端配置文件"

# --- frontend/.env:Next.js 读取的运行/编译期变量 -----------------------------
write_generated "${FRONTEND_DIR}/.env" <<'ENVFILE'
# 工友通前端配置(由 scripts/setup-frontend.sh 生成)
#
# 这个文件不进 git —— 换机器重跑 scripts/setup-frontend.sh 就有了。
# 注意:NEXT_PUBLIC_ 开头的变量会被编译进浏览器 JS 包,里面绝不能放密钥。
# 真正的 API Key 只在后端的 .env 里(GYT_ 前缀),前端一个都不碰。

# 后端 LangGraph 服务地址。
# 这里必须写 localhost 而不是 compose 里的服务名 backend ——
# 请求是浏览器发出来的,浏览器跑在宿主机上,解析不到 compose 内网的服务名。
NEXT_PUBLIC_API_URL=@@BACKEND_URL@@

# 图 ID,必须和 backend/langgraph.json 里 graphs 的键一字不差,否则前端连不上图。
NEXT_PUBLIC_ASSISTANT_ID=@@ASSISTANT_ID@@
ENVFILE

# --- frontend/.dockerignore:缩小构建上下文 + 挡住密钥进镜像 ------------------
# 标 always:这是安全相关生成物,上游自带的弱版本不许靠"先到先得"赢过它(见 write_generated)。
write_generated "${FRONTEND_DIR}/.dockerignore" always <<'DOCKERIGNORE'
# 由 scripts/setup-frontend.sh 生成(安全相关:内容与模板不一致时会被无条件覆盖)。
# 目的一:别把几百兆的 node_modules 和 .next 传给 docker daemon,
#         依赖在镜像里会用 pnpm 重新装一遍,传过去纯属浪费。
# 目的二(更重要):把所有 .env 变体挡在构建上下文之外。
#         Dockerfile 里 build 阶段 `COPY . .`、runner 阶段 `COPY --from=build /app /app`,
#         是"整份搬运";只要 .env.local / .env.production.local 进了上下文,
#         里面的 LANGSMITH_API_KEY 就会明文躺在最终镜像层里,谁拿到镜像谁能读。
node_modules
.next
out
.turbo
.git
.env
.env.*
!.env.example
Dockerfile
.dockerignore
npm-debug.log*
DOCKERIGNORE

# --- frontend/Dockerfile:上游仓库自己不带,compose 的 frontend 服务要用 -------
write_generated "${FRONTEND_DIR}/Dockerfile" <<'DOCKERFILE'
# syntax=docker/dockerfile:1.7
# =============================================================================
# 工友通前端镜像(由 scripts/setup-frontend.sh 生成 —— 手改会在下次 --force 时丢失)
#
#   ┌────────┐    ┌────────┐    ┌──────────┐
#   │  deps  │───▶│ build  │───▶│  runner  │
#   │pnpm 装 │    │next 编译│    │ 非 root  │
#   │ 依赖   │    │静态产物 │    │ 起服务   │
#   └────────┘    └────────┘    └──────────┘
#     只依赖         依赖源码       只拷产物
#   package.json                  不再联网
#   + lock 文件
#
# 关键点:NEXT_PUBLIC_* 是 Next.js 的**编译期**变量,会被烘进浏览器 JS 包。
#         必须在 build 阶段用 ARG 给进来;只在 compose 的 environment 里写是无效的,
#         页面会去连默认地址然后连接失败。这是本文件最容易踩的坑。
# =============================================================================

ARG NODE_IMAGE_TAG=@@NODE_IMAGE_TAG@@

# -----------------------------------------------------------------------------
# 公共底座:统一 Node 版本与包管理器
# corepack 会按 package.json 里的 packageManager 字段激活对应版本的 pnpm,
# 保证镜像里和本地开发用的是同一个 pnpm,不会出现 lock 文件版本不认的情况。
# -----------------------------------------------------------------------------
FROM node:${NODE_IMAGE_TAG} AS base
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
WORKDIR /app

# -----------------------------------------------------------------------------
# 阶段 1:deps —— 只装依赖
# 先只 COPY 依赖清单,改业务代码不会让这一层失效(pnpm install 是最慢的一步)。
# -----------------------------------------------------------------------------
FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile

# -----------------------------------------------------------------------------
# 阶段 2:build —— 编译 Next.js 产物
# -----------------------------------------------------------------------------
FROM base AS build

# 由 docker-compose.yml 的 build.args 传入;这里的默认值只是让裸 docker build 也能用
ARG NEXT_PUBLIC_API_URL=@@BACKEND_URL@@
ARG NEXT_PUBLIC_ASSISTANT_ID=@@ASSISTANT_ID@@
# 公网部署时把后端访问令牌烘进浏览器包,测试的人不用手动粘。
# 本机开发不传,默认空 —— api-key.tsx 覆盖件会退回只读 localStorage,行为与从前一样。
# ⚠️ 这个 ARG **不能省**:Docker 对未声明的 build arg 只警告 "unused build arg",
#    构建照样成功,于是 compose 传了值、镜像里却什么都没有 ——
#    表现是「站点打得开、每次提问 401」,而构建日志一切正常。
#    2026-08-11 安全复核就是这么抓到的(H3)。
ARG NEXT_PUBLIC_API_KEY=
# 产物(工地照片 / 巡检记录 docx)的静态出口。
# 空 = 用覆盖件里的默认值 http://127.0.0.1:8788,即本机 `make serve-artifacts` 那份。
# 公网部署必须传同源路径(如 https://<域名>/artifacts):127.0.0.1 在测试者那边
# 指的是他自己的电脑,而且 https 页面拉 http 资源会被浏览器按 mixed content 拦掉,
# 表现是「照片全是碎图、巡检记录点了没反应」,而控制台之外一点提示都没有。
# 同源清单:本行 + docker-compose.vps.yml 的 build args + Caddyfile 的 handle_path /artifacts/*
#          + human.tsx / tool-calls.tsx 两个 ARTIFACT_BASE。
ARG NEXT_PUBLIC_ARTIFACT_BASE=
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_ASSISTANT_ID=${NEXT_PUBLIC_ASSISTANT_ID} \
    NEXT_PUBLIC_API_KEY=${NEXT_PUBLIC_API_KEY} \
    NEXT_PUBLIC_ARTIFACT_BASE=${NEXT_PUBLIC_ARTIFACT_BASE} \
    NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

# -----------------------------------------------------------------------------
# 阶段 3:runner —— 最终运行镜像
#
# 这里整份拷 /app 而不是只挑 .next/public:
# 上游仓库没有 public/ 目录,挑着拷会因为源不存在直接构建失败;
# 而且上游随时可能加新的运行期资源目录。演示项目里,稳 > 省几百兆。
# 不跑 pnpm prune --prod 同理:省下的体积换不来的确定性,不值。
# -----------------------------------------------------------------------------
FROM base AS runner
ENV NODE_ENV=production

# node 官方镜像自带 uid 1000 的 node 用户,直接复用,不另建
COPY --from=build --chown=node:node /app /app
USER node

EXPOSE @@FRONTEND_PORT@@

# --hostname 0.0.0.0:容器内必须监听所有网卡,只听 127.0.0.1 的话宿主机映射端口连不上
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "@@FRONTEND_PORT@@"]
DOCKERFILE

# -----------------------------------------------------------------------------
# 步骤 2.5:套用项目自己的界面覆盖件
# -----------------------------------------------------------------------------
# 上游把每次工具调用渲染成一个带边框的大方块:原始工具名 + 一长串 UUID + 原始返回值。
# 对开发调试有用,但工友通给的是工地师傅,演示时评委看到的也是这个界面 ——
# 满屏 `transfer_back_to_supervisor` 和 `call_00_bWobucJo6Os...` 会把
# 「拍张照就知道有什么隐患」这件事本身淹掉。
#
# 覆盖件放在 scripts/frontend-overrides/,**进 git**;frontend/ 本身不进。
# 所以改界面只改覆盖件,换机器重跑本脚本就还在 —— 直接改 frontend/ 里那份会丢。
log_step "套用界面覆盖件"

OVERRIDES_DIR="${REPO_ROOT}/scripts/frontend-overrides"

check_retired_override() {
  # 已**退休**的覆盖件在旧机器上留下的残骸检查。
  #
  # 🔴 为什么必须有这一道:`frontend/` 是生成物、不进 git,而本脚本对已存在的
  #    目录是**跳过 clone、原地打补丁**的。删掉一件 apply_override 之后,
  #    脚本再也不会去碰那个目标文件 —— 于是**新机器拿到的是上游原版(对的),
  #    老机器留着上一版的补丁(错的)**,两边行为不一样而没有任何东西说话。
  #    具体到 2026-08-21 删掉的 stream-provider.tsx:老机器上那份 Stream.tsx
  #    还 `import { recordTiming } from "@/lib/timing-lib"`,而那个导出已经没了,
  #    表现是 `pnpm build` 报 Module not found —— 响是响的,但查的人会先去
  #    怀疑 timing-lib 写错了,而不是想到「这台机器的 frontend/ 是旧的」。
  #
  # 判据是**内容里的标志串**,不是文件是否存在:目标文件在上游本来就有,
  # 存在与否说明不了任何事。
  local dst="${FRONTEND_DIR}/$1"
  local marker="$2"
  local what="$3"
  [[ -f "${dst}" ]] || return 0
  grep -q -- "${marker}" "${dst}" 2>/dev/null || return 0
  die "这台机器的 frontend/ 还带着**已退休**的覆盖件:$1
      (${what})
      它不会被自动修好 —— 本脚本对已存在的目录只打补丁、不还原。
      修法:rm -rf \"${FRONTEND_DIR}\" 然后重跑 make frontend(整个目录是生成物,删了没损失)。"
}

# ⚠️ 这里曾经有一条针对 `src/providers/Stream.tsx` 的 check_retired_override,
#    2026-08-21 当天加、当天删。留这段墓碑是因为**删它的理由本身值得记**:
#
#    上午:耗时行改走直连接口 `GET /timing`,stream-provider.tsx 这件覆盖件
#          没职责了 → 删掉覆盖件 + 加这道退休闸(判据是内容里还有 `recordTiming`)。
#    下午:发现上游那行 `fetchStateHistory: true` 被 SDK 读成 `limit=10`,
#          一次拉 7.5 MB → **同一件覆盖件因为完全不相干的理由又装了回来**
#          (现在注册在下面的 apply_override 里)。
#
#    于是这道闸变成了自伤:它在 `apply_override` 之前执行,老机器上那份带
#    `recordTiming` 的 Stream.tsx 会先撞上 `die`、被要求 `rm -rf frontend` ——
#    而下面那条 apply_override **本来就会把它原地覆盖成正确的新版本**。
#    更糟的是 `rm -rf frontend` 正是 W7 手册 §2.17 标红的动作(会顺带把上游
#    TypeScript 从 5.8 升到 6.0,那个坑刚花时间修完)。拦住了自己的修复,
#    还把人推去走最危险的路 —— 顺带训练人「这个守卫报的是误报,下次注释掉」。
#
#    🔴 `check_retired_override` 这个机制本身是对的,留着给**真正退休**的覆盖件用。
#       但下次挂之前先问一句:这件东西下面还有没有 apply_override / install_new_file?
#       有就不该挂 —— 那不叫退休,那叫换了个理由继续活着。

apply_override() {
  local src="${OVERRIDES_DIR}/$1"
  local dst="${FRONTEND_DIR}/$2"

  [[ -f "${src}" ]] || { log_warn "覆盖件不存在,跳过:$1"; return 0; }
  if [[ ! -f "${dst}" ]]; then
    # 目标不存在 = 上游改了目录结构。这时候**不能**闷头拷进去 ——
    # 那样只会多出一个没人 import 的孤儿文件,而界面看起来"没生效",很难查。
    log_warn "上游没有 $2,可能已重构;跳过覆盖(界面会退回上游样式)"
    return 0
  fi
  if cmp -s "${src}" "${dst}"; then
    log_skip "$(basename -- "$2") 已是最新"
    return 0
  fi
  cp -- "${src}" "${dst}" || die "拷贝覆盖件失败:$2"
  log_ok "已覆盖 $2"
}

# install_new_file <本仓文件名> <frontend 内相对路径>
#
# apply_override 装不了**新**文件:它在目标不存在时按「上游重构了」处理,
# warning 跳过(见上面那条分支)—— 对给上游打补丁的文件那是对的,
# 对本仓自有、上游根本没有的文件就成了「永远装不上」。新文件走这条。
#
# 语义(W7 方案 §3.8):
#   目标不存在        → 拷贝(本仓自有文件,目标不存在是常态,不是上游重构)
#   存在且内容相同     → 跳过
#   存在且内容不同     → 覆盖 + 告警
# 为什么「不同 → 覆盖」而不是拒绝或跳过:这些文件的唯一真相在本仓
# (scripts/frontend-overrides/ 进 git,frontend/ 不进)。拒绝会让脚本永远
# 无法更新自己的文件;跳过会留着旧版本且看不出来。覆盖 + 告警是唯一
# 「既能更新又能发现撞名」的组合 —— 告警就是给「上游哪天真的新增了同名文件」
# 这种撞名情形留的人工核对入口。
install_new_file() {
  local src="${OVERRIDES_DIR}/$1"
  local dst="${FRONTEND_DIR}/$2"

  # 源文件进 git、与本脚本同仓同步,缺了 = 仓库自身不完整,直接失败让人去查,
  # 不学 apply_override 的 warning(那是给「上游变了」留的余地,这里没有上游)
  [[ -f "${src}" ]] || die "缺少本仓文件:scripts/frontend-overrides/$1(它应随本脚本一起提交,先 git status 看看)"

  if [[ ! -f "${dst}" ]]; then
    mkdir -p -- "$(dirname -- "${dst}")" || die "建目录失败:$2"
    cp -- "${src}" "${dst}" || die "安装失败:$2"
    log_ok "已安装 $2"
    return 0
  fi
  if cmp -s "${src}" "${dst}"; then
    log_skip "$(basename -- "$2") 已是最新"
    return 0
  fi
  cp -- "${src}" "${dst}" || die "安装失败:$2"
  log_warn "$2 与本仓版本不同,已按本仓版本覆盖。本文件由本仓管理;若是上游新增了同名文件,请人工核对两份内容再定归属。"
}

apply_override "tool-calls.tsx" "src/components/thread/messages/tool-calls.tsx"
apply_override "ai.tsx" "src/components/thread/messages/ai.tsx"
apply_override "markdown-text.tsx" "src/components/thread/markdown-text.tsx"

# CAD Agent 方案 A:让工友现场上传 DXF 图纸(见 .personal/CAD_Agent_落地文档.md §1.6)。
# DXF 的浏览器 MIME 不可靠,前端一律按 .dxf 后缀认;后端 uploads.py 登记成 DRAWING 产物。
apply_override "use-file-upload.tsx" "src/hooks/use-file-upload.tsx"
apply_override "multimodal-utils.ts" "src/lib/multimodal-utils.ts"
apply_override "MultimodalPreview.tsx" "src/components/thread/MultimodalPreview.tsx"
apply_override "thread-index.tsx" "src/components/thread/index.tsx"

# 上传前把照片缩到「服务端反正要缩到」的那个尺寸(2026-08-19)。
# 与上面那件 multimodal-utils.ts 是**一组**:那份里
# `import { compressImageFile } from "@/lib/image-compress"`,而它的 fileToContentBlock
# 是「选文件 / 拖进来 / 粘贴」三条路唯一的汇合处(上游把校验抄了三遍,这一步没有)。
#
# **走 install_new_file 而不是 apply_override**:它是**本仓自有**的新文件、上游没有
# 对应物,而 apply_override 对不存在的目标只 warning 跳过(见函数头注),永远装不上。
# ⚠️ 它是唯一一件**不在下面那一整段** install_new_file 里的,摆这儿是为了挨着使用者。
#    所以审计件数只认下面「数法」那两条 grep,别靠肉眼数那一段有几行。
# ⚠️ 漏装 = 前端构建直接 Module not found(与监理那三件同款)。这个坏法是响的,不难查。
# 🔴 安静的坏法在**常量漂移**:image-compress.ts 的 MAX_EDGE / TARGET_BYTES /
#    JPEG_QUALITY 三个数镜像服务端(config.py 的 photo_compress_max_edge_px、
#    photo_compress_target_mb + agents/safety/tools.py 的 _JPEG_QUALITY_STEPS 首档 85)。
#    漂了一声不吭,表现是客户端缩一次、服务端再缩一次 —— 两代有损叠加,
#    而「有没有戴安全帽」这种判断经不起反复有损压缩。
install_new_file "image-compress.ts" "src/lib/image-compress.ts"

# 上游 LangGraph logo 用了 JSX 里非法的 clip-path(应为 clipPath),控制台每次报
# "Invalid DOM property `clip-path`"。这里改成 clipPath 消掉这个警告。
apply_override "langgraph.tsx" "src/components/icons/langgraph.tsx"

# 历史记录里把上传的照片显示出来,而不是一串 32 位编号。
# 成因不在前端:backend/src/gyt/core/uploads.py 的 ingest_uploads 是 supervisor 的
# pre_model_hook,它返回 {RemoveMessage(原消息), 改写后的纯文本} —— **带图那条消息
# 从 state 里被永久删掉了**(必须永久换,否则子 Agent 共享同一份 messages 时照样
# 会拿到 image 块,文本档模型收到直接 400)。所以图只能从产物目录捞,靠
# `make serve-artifacts` 的 GET /by-id/<32位编号>(scripts/serve_artifacts.py)。
apply_override "human.tsx" "src/components/thread/messages/human.tsx"

# 线程历史加删除按钮。上游只能点进去,一场演示攒下几十条废线程,
# 下次上台要在里面翻找要演的那条;历史里还躺着已经修掉的老问题,评委随手点开就看到。
apply_override "thread-history.tsx" "src/components/thread/history/index.tsx"

# 历史侧栏的**取数**(上面那件只管渲染)。上游 getThreads() 不带任何字段裁剪,
# 服务端于是把每条会话的完整 values(整份 messages)都回过来,而侧栏只显示第一句话。
# 2026-08-15 线上实测:POST /api/threads/search 570ms / 177KB,是整页最慢的一条资源,
# 而后端本身 0.06s —— 慢的全是传输。这里给 search() 加上 select + extract,
# 本机实测 493,666 → 19,021 字节(省 96.1%)。
# ⚠️ 与上面那件同源:标题的键名 first_message_content 两边必须一字不差,
#    只改一边的症状是「标题全变成 32 位 thread_id」,而且一声不吭。
apply_override "thread-provider.tsx" "src/providers/Thread.tsx"

# 公网部署:把后端访问令牌从构建期环境变量里读出来(localStorage 优先,本机调试能覆盖)。
# 上游 getApiKey() 只读 localStorage —— 于是 VPS 上每个测试者都得自己去控制台
# localStorage.setItem 一遍,而那等于把令牌用聊天工具发给一群人。
# ⚠️ 与上面 Dockerfile 模板里的 `ARG NEXT_PUBLIC_API_KEY` 同源,少哪一半都是
#    「站点打得开、每次提问 401」。
apply_override "api-key.tsx" "src/lib/api-key.tsx"

# 线程历史两阶段取数(2026-08-21)—— 治「打开历史会话要等好几秒,而且是纯白板」。
#
# 上游那份写的是 `fetchStateHistory: true`,而 SDK 把 `true` 读成 `limit = 10`
# (`stream.lgp.js:26` 那个 `: 10`)—— 一次拉十份完整状态快照。而照片的 base64
# 会永久留在某个检查点里,于是本机实测 **7,501,329 字节**,而首屏真正要的 head
# 只有 **8,248 字节**。本件把它换成 `thread:` 这个公开选项,自己两阶段供:
# 先 `getState()` 出字,再后台补 `getHistory()`(分支功能靠后者)。
#
# ⚠️ **这件覆盖件是「二进宫」**:2026-08-20 因为耗时的 custom 事件加过一次,
#    08-21 因为耗时改走直连接口删过一次,当天又因为**另一个理由**回来。
#    别把它当成那件事的复活 —— 耗时行**不**经过这里,它自己轮询 `GET /timing`。
# ⚠️ 漏装本件的表现:功能全对,**只是每次打开会话都慢几秒**,而且没有任何报错。
apply_override "stream-provider.tsx" "src/providers/Stream.tsx"

# 打卡三件(W7 §4.4):**本仓自有**的新文件,上游没有对应物,走 install_new_file
# (apply_override 对不存在的目标只会 warning 跳过,永远装不上,见函数头注)。
#   checkin-lib.ts —— 纯函数库,scripts/frontend-tests/ 的 vitest 直接测它
#   qrcode.tsx     —— 电脑端二维码面板(依赖下面步骤 2.6 装的 qrcode.react)
#   checkin.tsx    —— 自拍打卡组件,thread-index.tsx 的动作条里是它的入口
# ⚠️ 计数口径(CLAUDE.md「前端覆盖件」):apply_override **十三件** + install_new_file
#    **十五件**,是**两个数**,别合成一个 —— 「以 apply_override 调用为准」那句话
#    合并之后数出来永远对不上。
#
# 🔴 **2026-08-22 起这两个数有机器盯着了**,别再靠手改:
#    `scripts/frontend-tests/override-count.test.ts` 拿两条 grep 的真值去比
#    本段这两个中文数字 + CLAUDE.md「前端覆盖件」那一节的两个数。
#    `make test-frontend` 会跑它。补这道闸的理由写在那个文件里 ——
#    简单说:这段注释自己记着这条教训**复发了四次**(2026-08-11 之前写「八件」
#    而实际十一;W7 合流后写「十一 + 三」而实际 12 + 5;08-19 头数在「八」上
#    停了两批;08-21 同一次编辑里头数改对了而结论段没跟上)。四次之后还靠纪律,
#    就是明知故犯。
#    (2026-08-15 校过:上一版这里写的是「十一件 + 三件」,而 apply_override 那时
#     确实是十一件、install_new_file 却已经是五件 —— 队友那两件
#     ProjectUploadPanel/GytStatusCards 合流时没回来改这个数。
#     数不对的坏处不是难看,是下一个人照着数会以为「剩下的不用管」。)
#    (2026-08-16 W9·S6:install_new_file 五 → 七,加了监理那两件。)
#    (2026-08-16 W10·S3:install_new_file 七 → 八,加了 supervision-entry.tsx
#     那个常驻入口。apply_override 仍是十二 —— 那次只改了已有的 thread-index.tsx。)
#    (2026-08-17 W12·第一批:install_new_file 八 → **十**,加了 lang-lib.ts
#     那个三语判别库 + hant-convert.tsx 那个懒加载转换器。
#     apply_override 仍是**十二** —— 这批动的 ai.tsx / markdown-text.tsx /
#     tool-calls.tsx 三份本来就在十二件里。)
#    (2026-08-19 perf·上传压缩:install_new_file 十 → **十一**,加了 image-compress.ts
#     —— 上传前把照片缩到服务端反正要缩到的尺寸,治「点了发送先干等十几秒」。
#     apply_override 仍是**十二** —— 这次只改了已有的 multimodal-utils.ts
#     (它 import 那个新库),没有新增打补丁的目标。
#     ⚠️ 那一行**不在本段里**:为了挨着使用者 multimodal-utils.ts,它写在上面
#        DXF 那组的末尾。所以「肉眼数本段」从这次起就是错的数法,只认下面两条 grep。
#     ⚠️ 顺手补正:上面那个头数在「八」上停了两批 —— W12 两批都只改了历史记录、
#        没回头改它,而真值那时已经是十。**改数是两处一起**:头一行的数 + 追加历史。
#        这就是本段开头那条教训的第三次复发,而它照旧一声不吭。)
#    (2026-08-20 perf·每步耗时:**两个数同时动** —— apply_override 十二 → **十三**
#     (加了 stream-provider.tsx:上游那份 Stream.tsx 订阅了 custom 却把非 UI 的事件
#      整个扔掉,而它是全站唯一能收 custom 事件的地方);
#     install_new_file 十一 → **十三**(timing-lib.ts 那个零依赖纯 TS 库 +
#      GytTimingRows.tsx 那一坨耗时行)。
#     这是本段历史上**第一次两个数一起变**,前面几批都只动一个 ——
#     照着「上一批只改了 install 这个数」的惯性只改一半,又会复发一次。
#     ⚠️ 两个数**现在都等于十三,那是巧合**,不是可以合并的信号。)
#    (2026-08-21 fix·耗时换通道:apply_override 十三 → **十二**,删掉了
#     stream-provider.tsx。install_new_file 仍是**十三** —— timing-lib.ts 与
#     GytTimingRows.tsx 都还在,只是内部从「收 custom 事件」改成了「轮询直连接口」。
#     ⚠️ **这是本段第一次有数变小。** 上一批那句「两个数都等于十三是巧合」
#        当天就应验了:一批之后它们又不相等了。)
#    (2026-08-21 perf·线程历史两阶段:apply_override 十二 → **十三**,
#     stream-provider.tsx **同一天回来了** —— 因为**另一个**理由(上游那行
#     `fetchStateHistory: true` 被 SDK 读成 limit=10,一次拉 7.5 MB)。
#     install_new_file 仍是**十三**。
#     ⚠️ 一天之内同一件覆盖件删了又加,而两次的理由**毫不相干**。
#        下次看见它别顺手按「上次为什么删」去推理。)
#    (2026-08-22 P2·巡检记录抽屉:install_new_file 十三 → **十五**,加了
#     reports-lib.ts(零依赖纯 TS)与 reports-entry.tsx(动作条上那颗「記錄」按钮)。
#     apply_override 仍是**十三** —— 这批动的 thread-index.tsx / use-file-upload.tsx /
#     supervision.tsx 里,前两件本来就在十三件里,supervision.tsx 走的是 install_new_file。
#     ⚠️ **两个数从这一批起又不相等了(13 + 15)**,而上一批它们碰巧都是十三。
#        那句「相等是巧合」第二次应验。)
#    数法:grep -cE '^\s*apply_override ' scripts/setup-frontend.sh
#          grep -cE '^\s*install_new_file ' scripts/setup-frontend.sh
install_new_file "checkin-lib.ts" "src/lib/checkin-lib.ts"
# W12 三语切换(第一批:繁體答话)。纯 TS 零依赖的判别库 ——
# 判「用户在打什么字」+ 判「这条要不要转」。转换器本身是懒加载的,不在这个文件里
# (理由见 lang-lib.ts 头注:opencc-js 的 cn2t 是 438 KB gzipped,
#  而 300 KB 的预算管的是首屏)。
install_new_file "lang-lib.ts" "src/lib/lang-lib.ts"
# 转换器本体(懒加载 opencc-js)。与 lang-lib 分开的理由:lang-lib 必须保持
# 零依赖(独立 vitest 包按相对路径 import 它),而本文件 import react + opencc。
install_new_file "hant-convert.tsx" "src/lib/hant-convert.tsx"
install_new_file "qrcode.tsx" "src/components/thread/qrcode.tsx"
install_new_file "checkin.tsx" "src/components/thread/checkin.tsx"
# W7 CAD/knowledge(队友分支):项目 / 图纸 / 资料上传面板与状态卡片。
# 同样是**本仓自有**的新文件,同样不能走 apply_override。
# ⚠️ 2026-08-15 合流:两条分支各自造了一个「装新文件」的函数
#    (这边 install_new_file、那边 add_new_file)—— 同一个问题、同一个发现
#    (「apply_override 对不存在的目标只会跳过」),两个名字。已统一到
#    install_new_file:它多做两件事 —— 内容相同就跳过(重复跑不吵)、
#    不同则覆盖并**告警**(上游哪天新增同名文件时看得见)。
install_new_file "ProjectUploadPanel.tsx" "src/components/thread/ProjectUploadPanel.tsx"
install_new_file "GytStatusCards.tsx" "src/components/thread/GytStatusCards.tsx"

# W9 监理业务闭环(S6 泳道)+ W10 常驻入口(S3 泳道):监理确认与处置界面、
# 文书下载出口、动作条上那颗带待确认计数的按钮。
# 同样是**本仓自有**的新文件,上游没有对应物,同样不能走 apply_override。
#   supervision-lib.ts    —— 纯函数库(端点地址、Envelope 解析、状态中文名、
#                             「这条隐患现在能做什么」),scripts/frontend-tests/ 的
#                             vitest 直接测它,所以必须保持零依赖
#   supervision.tsx       —— 确认面板 + 处置动作 + 文书下载卡;面板自己去打两个
#                             GET 端点取数(W10 之前是从聊天流里那张卡拿的)
#   supervision-entry.tsx —— 动作条上的常驻入口(W10)。**它才是面板今天唯一的入口**
# ⚠️ 漏了前两件 = 监理那六种文书在界面上一个出口都没有:演示时说「暂停令已签发」,
#    而屏幕上只有一行灰色折叠,点不开、下不到、**不报错**(方案 §6.5 那一行)。
#    tool-calls.tsx(上面 apply_override 第一件)会 import 它们两个,
#    少装任何一件,前端构建直接 Module not found。
# ⚠️ 漏了第三件 = **面板一次都打不开**,而且同样不报错(thread-index.tsx import 它,
#    所以真漏了是构建期 Module not found —— 但 W9→W10 之间的病状正是「构建没问题、
#    界面上就是没有入口」,别把「构建过了」当成入口通了。W10 方案 §1 记了全过程)。
install_new_file "supervision-lib.ts" "src/lib/supervision-lib.ts"
install_new_file "supervision.tsx" "src/components/thread/supervision.tsx"
install_new_file "supervision-entry.tsx" "src/components/thread/supervision-entry.tsx"

# 「每一步花了多久」两件(2026-08-20 加,2026-08-21 换了数据源)。
# **这两件现在是自足的** —— 原本还有第三件 stream-provider.tsx 负责收 custom 事件,
# 换成直连接口之后它没用了、已删(墓碑在上面 apply_override 那一段)。
#   timing-lib.ts     —— 零依赖纯 TS:拼地址 + 解信封 + 排版 + store。
#                        scripts/frontend-tests/ 的 vitest 直接测它,所以必须保持零依赖。
#                        ⚠️ 加进 scripts/frontend-tests/tsconfig.json 的 include 了 ——
#                           漏了不报错,只是 import 进来全成 any(CLAUDE.md 明写)。
#   GytTimingRows.tsx —— 消息区末尾那一坨耗时行,自己轮询 `GET /timing`,
#                        受「隱藏中間步驟」开关控制。
# ⚠️ 漏装任一件 = 前端构建 Module not found(thread-index.tsx import 它们),
#    这个坏法是响的、不难查。安静的坏法有两种,都是一行不出而控制台干净:
#      ① **契约漂移** —— timing-lib.ts 的字段名镜像后端 `core/timing.RECORD_KEYS`,
#         漂了一声不吭(解析全返回 null);
#      ② **路由没挂** —— 后端 webapp.py 少铺 `*TIMING_ROUTES` 时接口 404,
#         而前端对非 2xx 是**故意安静走开**的(观测件绝不许打扰工友)。
install_new_file "timing-lib.ts" "src/lib/timing-lib.ts"
install_new_file "GytTimingRows.tsx" "src/components/thread/GytTimingRows.tsx"

# 巡检记录抽屉两件(2026-08-22)。
#
# 🔴 它们补的是「拍照 → 自动出 Word」这条链的**终点** —— 那条链一直是断的:
#    文档真的生成了、真的落盘了,而那份 Envelope 被 supervisor 的
#    output_mode="last_message" 整个丢掉,于是 tool-calls.tsx 里那张巡检记录卡
#    (W3 就写好了)**一次都没渲染出来过**;而 agents/report/prompt.md 教模型说
#    「要打印或转发跟管理员说编号就行」——**那个管理员不存在**(TODO-34 的真机原话)。
#    结果是:文件就在服务器上,而谁都拿不到。
#
#   reports-lib.ts    —— 零依赖纯 TS:拼地址 + 解信封 + 排版。
#                        scripts/frontend-tests/ 的 vitest 直接测它,必须保持零依赖。
#                        ⚠️ 加进 scripts/frontend-tests/tsconfig.json 的 include 了 ——
#                           漏了不报错,只是 import 进来全成 any(CLAUDE.md 明写)。
#   reports-entry.tsx —— 动作条上那颗「記錄」按钮 + 抽屉本体。
#                        形状照 W7 打卡、W10 监理操作台:**操作台不是聊天产物**。
# ⚠️ 漏装任一件 = 前端构建 Module not found(thread-index.tsx import 它们),
#    这个坏法是响的。安静的坏法是**后端 webapp.py 少铺 `*REPORTS_ROUTES`** ——
#    那时 GET /reports 是 404,而抽屉里永远是空的(前端对非 2xx 安静走开)。
install_new_file "reports-lib.ts" "src/lib/reports-lib.ts"
install_new_file "reports-entry.tsx" "src/components/thread/reports-entry.tsx"

# -----------------------------------------------------------------------------
# 步骤 2.6:装二维码库(checkin 三件里唯一的新依赖)
#
# 为什么在脚本里装而不是「让人记得手动装」:frontend/ 不进 git,换台机器重跑
# 本脚本就该得到能编译的前端 —— checkin.tsx import 了 "qrcode.react",
# 不装的话 clone 出来第一次 `pnpm build` 就 Module not found,查的人会以为
# 覆盖件坏了。装进 package.json + pnpm-lock.yaml 之后,Dockerfile 的
# `pnpm install --frozen-lockfile` 一并接住,compose 构建不用任何额外步骤。
# -----------------------------------------------------------------------------
# ⚠️⚠️ **所有 install_new_file / apply_override 必须排在本步之前。**
# 本步在没有 pnpm 的机器上会 die(整脚本终止),排在它后面的拷贝一个都不会执行。
# 2026-08-15 线上就这么栽过:队友那两个组件被排在本步之后,VPS 没装 pnpm →
# 脚本在这里退出 → 组件没拷 → 前端构建报 `Module not found: ./GytStatusCards`,
# 而那个报错看起来像覆盖件坏了,离真因(脚本提前退出)隔着好几层。
log_step "安装二维码库 qrcode.react@${QRCODE_REACT_VERSION}"

if grep -q "\"qrcode.react\": \"${QRCODE_REACT_VERSION}\"" "${FRONTEND_DIR}/package.json"; then
  log_skip "qrcode.react@${QRCODE_REACT_VERSION} 已在 package.json,无需重装"
else
  command -v pnpm >/dev/null 2>&1 || die "没找到 pnpm,装不了 qrcode.react。
      先执行 corepack enable(Node 自带)或安装 pnpm,再重跑本脚本;
      或手动执行:cd \"${FRONTEND_DIR}\" && pnpm add --save-exact qrcode.react@${QRCODE_REACT_VERSION}"
  # COREPACK_ENABLE_DOWNLOAD_PROMPT=0:corepack 首次匹配 frontend/ 钉的 pnpm 版本时
  # 会交互式问「要不要下载」,脚本里没人按回车,不关掉会卡死在这一步。
  (cd "${FRONTEND_DIR}" && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm add --save-exact "qrcode.react@${QRCODE_REACT_VERSION}") \
    || die "qrcode.react 安装失败。检查 npm 源与网络后重跑本脚本;
      package.json 与 pnpm-lock.yaml 必须一起更新,缺一半会拖到 next build 才炸。"
  log_ok "已装 qrcode.react@${QRCODE_REACT_VERSION}(package.json 与 pnpm-lock.yaml 已同步)"
fi

# -----------------------------------------------------------------------------
# 步骤 2.7:装简→繁转换库(W12 第一批唯一的新依赖)
#
# 同一条理由:hant-convert.tsx 里 `import("opencc-js/cn2t")` 是**动态** import,
# 但 Next 的打包器仍要在构建期解析得到这个包 —— 不装的话 `next build` 报
# Module not found,而那个报错离真因(依赖没装)隔着好几层。
# 与上一步同样排在所有覆盖件拷贝**之后**、且同样会在没有 pnpm 的机器上 die。
# -----------------------------------------------------------------------------
log_step "安装简繁转换库 opencc-js@${OPENCC_JS_VERSION}"

if grep -q "\"opencc-js\": \"${OPENCC_JS_VERSION}\"" "${FRONTEND_DIR}/package.json"; then
  log_skip "opencc-js@${OPENCC_JS_VERSION} 已在 package.json,无需重装"
else
  command -v pnpm >/dev/null 2>&1 || die "没找到 pnpm,装不了 opencc-js。
      先执行 corepack enable(Node 自带)或安装 pnpm,再重跑本脚本;
      或手动执行:cd \"${FRONTEND_DIR}\" && pnpm add --save-exact opencc-js@${OPENCC_JS_VERSION}"
  (cd "${FRONTEND_DIR}" && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm add --save-exact "opencc-js@${OPENCC_JS_VERSION}") \
    || die "opencc-js 安装失败。检查 npm 源与网络后重跑本脚本;
      package.json 与 pnpm-lock.yaml 必须一起更新,缺一半会拖到 next build 才炸。"
  log_ok "已装 opencc-js@${OPENCC_JS_VERSION}(package.json 与 pnpm-lock.yaml 已同步)"
fi


# -----------------------------------------------------------------------------
# 步骤 3:收尾提示
# -----------------------------------------------------------------------------
log_step "完成"
cat <<NEXTSTEPS

  前端已就位:${FRONTEND_DIR}
  它连的后端是 ${BACKEND_URL},图 ID 是 "${ASSISTANT_ID}"。

  接下来二选一:

  A. 容器方式(和演示环境一致)
       docker compose --profile ui up -d --build
       浏览器打开 http://localhost:${FRONTEND_PORT}
       说明:frontend 服务挂在 ui profile 下,不加 --profile ui 只会起后端。

  B. 本地开发方式(改前端代码时用,热重载快)
       先起后端:  make dev
       再起前端:  cd "${FRONTEND_DIR}" && pnpm install && pnpm dev

  提醒:
   · frontend/ 是从上游 clone 下来的快照,不进 git;换机器重跑本脚本即可。
   · 界面改动请改 scripts/frontend-overrides/(那些进 git),别直接改 frontend/。
   · 后端的 API Key 在仓库根目录的 .env 里(GYT_ 前缀),别往前端塞密钥。

NEXTSTEPS
