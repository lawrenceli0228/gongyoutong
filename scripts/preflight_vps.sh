#!/usr/bin/env bash
# 上线前自检 —— 在 VPS 上、`docker compose up` 之前跑。
#
#     bash scripts/preflight_vps.sh
#
# 为什么要有这个脚本(而不是在手册里多写几句):
# 本仓反复吃过同一种亏 —— **写进文档的约定只是概率性生效,结构件才兜得住**。
# 部署尤其如此:手册 1000 多行,人在紧张状态下照着做,漏一步是常态。
# 下面每一条都是「漏了会怎样」有具体答案的,所以做成会**当场拦住你**的检查。
#
# 退出码:0 = 可以 up;1 = 有致命项,别 up。
# 只读:本脚本不改任何文件、不起任何容器、不发任何网络请求(除了解析域名)。

set -uo pipefail

# ⚠️ 本文件里**所有**变量展开都写成 ${VAR} 而不是 $VAR,这不是风格洁癖:
# 中文文案里常出现 「$ORIGIN」 这种写法,而在**非 UTF-8 locale** 下(VPS 上很常见)
# bash 会把「」的字节当成变量名的一部分,于是 set -u 当场把脚本打死。
# 2026-08-11 实测踩到:line 105: ORIGIN<乱码>: unbound variable,
# 而死的位置正好是最重要的那条(站点地址与公开地址对不对得上)。
# **一个上线前自检脚本自己中途暴毙,比没有它更坏** —— 人会以为「跑完了没报错」。

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
FATAL=0
WARN=0

ok()   { printf '  %s✓%s %s\n' "${GREEN}" "${OFF}" "$1"; }
bad()  { printf '  %s✗%s %s\n' "${RED}" "${OFF}" "$1"; FATAL=$((FATAL+1)); }
warn() { printf '  %s!%s %s\n' "${YELLOW}" "${OFF}" "$1"; WARN=$((WARN+1)); }
note() { printf '    %s%s%s\n' "${DIM}" "$1" "${OFF}"; }
head_() { printf '\n%s\n' "$1"; }

cd "$(dirname -- "$0")/.." || exit 1
REPO="$PWD"

printf '工友通 · 上线前自检\n仓库:%s\n' "${REPO}"

# ---------------------------------------------------------------------------
head_ "① 环境变量文件"
# ---------------------------------------------------------------------------
# ⚠️ 必须叫 .env(不是 .env.vps):compose 默认只读 .env,而 `--env-file` 只影响
#    变量插值、不进容器的 env_file。名字写错的表现是「变量像是没生效」。
if [[ ! -f .env ]]; then
  bad ".env 不存在。先 cp .env.vps.example .env 再填。"
  note "注意必须叫 .env —— compose 默认只认这个名字。"
  exit 1
fi
ok ".env 在"

get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- ; }

TOKEN=$(get GYT_ACCESS_TOKEN)
HASH=$(get GYT_LOGIN_HASH)
SITE=$(get GYT_SITE_ADDRESS)
ORIGIN=$(get GYT_PUBLIC_ORIGIN)
DS=$(get GYT_DEEPSEEK_API_KEY)
MS=$(get GYT_MOONSHOT_API_KEY)

# 占位符判据与 backend/auth.py 的 _PLACEHOLDER_PREFIXES 同源 —— 那边改了这里要跟。
looks_fake() {
  local v="${1// /}"
  [[ -z "${v}" ]] && return 0
  # 用 tr 转小写而不是 ${v,,} —— 后者是 bash 4+ 语法,macOS 自带的 bash 是 3.2,
  # 在那儿会 "bad substitution" 并且**打断整个循环**(六项一项都没查,还看不出来)。
  # 这个脚本要在别人机器上跑,不能假设 bash 版本。
  local low; low=$(printf '%s' "${v}" | tr '[:upper:]' '[:lower:]')
  case "${low}" in 替换成*|change*|your*|todo*|xxx*) return 0 ;; esac
  return 1
}

for pair in "GYT_ACCESS_TOKEN:${TOKEN}" "GYT_LOGIN_HASH:${HASH}" \
            "GYT_DEEPSEEK_API_KEY:$DS" "GYT_MOONSHOT_API_KEY:$MS" \
            "GYT_SITE_ADDRESS:${SITE}" "GYT_PUBLIC_ORIGIN:${ORIGIN}"; do
  n=${pair%%:*}; v=${pair#*:}
  if looks_fake "${v}"; then bad "${n} 没填,或者还是模板占位符。"; else ok "${n} 已填"; fi
done

# 令牌长度:auth.py 里 _MIN_TOKEN_LEN 是 24,短于它会被判成「没设」而**静默不设防**。
if [[ -n "${TOKEN}" && ${#TOKEN} -lt 24 ]]; then
  bad "GYT_ACCESS_TOKEN 只有 ${#TOKEN} 位,后端会把它当成没设(auth.py 的 _MIN_TOKEN_LEN=24)。"
  note "生成一个真的:openssl rand -hex 32"
fi

# 口令哈希里的 $ 必须写成 $$ —— compose 会把单个 $ 当变量插值吃掉,
# 结果是 login 服务拿到一个残缺哈希、**照常启动**,然后所有口令都对不上。
# (2026-08-11 从 bcrypt 换成 scrypt 之后这条依然成立:scrypt 哈希同样用 $ 分段。)
if [[ -n "${HASH}" && "${HASH}" != *'$$'* ]]; then
  bad "GYT_LOGIN_HASH 里的 \$ 没有写成 \$\$。"
  note "compose 会把单个 \$ 当变量吃掉;login 服务照常起来但所有口令都对不上,排查方向全错。"
fi
# 算法前缀写错(比如还留着 bcrypt 的 \$2a\$ 串)同样是「不报错、口令永远不对」
if [[ -n "${HASH}" && "${HASH}" != scrypt* ]]; then
  bad "GYT_LOGIN_HASH 不是 scrypt 哈希(应以 scrypt\$ 开头)。"
  note "2026-08-11 起登录改用 hashlib.scrypt(标准库),旧的 bcrypt 串不再能用。"
  note "重新生成:python scripts/make_login_hash.py"
fi

# ---------------------------------------------------------------------------
head_ "② 站点地址与公开地址必须描述同一个入口(最容易错、最难查的一条)"
# ---------------------------------------------------------------------------
# 2026-08-11 本机彩排真踩过:GYT_PUBLIC_ORIGIN 写 http://localhost,而人从
# http://127.0.0.1 访问 —— 浏览器判成**跨源**,前端一片 CORS 红,
# 而界面给用户看的错是「Unable to connect to LangGraph server」,**指向后端**。
# 真相是这两个变量对不上。差一个字(www、http/https、IP vs 域名)都是这个下场。
DOMAIN=""
case "${SITE}" in
  :*)
    ok "站点是**裸 IP 模式**(${SITE}),Caddy 只跑 HTTP"
    case "${ORIGIN}" in
      https://*) bad "GYT_SITE_ADDRESS 是裸 IP 模式(无 HTTPS),而 GYT_PUBLIC_ORIGIN 却写了 https://" ;;
      http://*)  ok "GYT_PUBLIC_ORIGIN 用的 http://,与裸 IP 模式相符" ;;
      *)         bad "GYT_PUBLIC_ORIGIN 必须以 http:// 或 https:// 开头,现在是「${ORIGIN}」" ;;
    esac
    ;;
  *)
    DOMAIN="${SITE%%:*}"
    ok "站点是**域名模式**(${DOMAIN}),Caddy 会自动申请 HTTPS 证书"
    if [[ "${ORIGIN}" != "https://${DOMAIN}" ]]; then
      bad "GYT_PUBLIC_ORIGIN 应该恰好是 https://${DOMAIN},现在是「${ORIGIN}」"
      note "这两个必须描述**同一个入口**。差一个字(带不带 www、http/https)="
      note "浏览器判跨源 → 前端 CORS 全红 → 界面报「连不上服务器」,方向全错。"
    else
      ok "GYT_PUBLIC_ORIGIN 与站点域名一致"
    fi
    ;;
esac
[[ "${ORIGIN}" == */ ]] && bad "GYT_PUBLIC_ORIGIN 末尾多了一个斜杠,去掉它(拼出来会变成 //api)。"

# ---------------------------------------------------------------------------
head_ "③ DNS —— 域名模式下这条最贵,错了要等好几天"
# ---------------------------------------------------------------------------
# Caddy 一起来就去 Let's Encrypt 申请证书。DNS 还没指过来就会失败,
# 而 LE 对同一域名有签发频率限制 —— 反复重试足够把自己关在门外,
# 现象是「HTTPS 突然就申请不下来了」,只能干等。所以必须先解析通再 up。
if [[ -z "${DOMAIN}" ]]; then
  ok "裸 IP 模式,不涉及证书申请,跳过"
else
  MYIP=$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || echo "")
  if command -v dig >/dev/null 2>&1;      then RES=$(dig +short "${DOMAIN}" A | tail -1)
  elif command -v getent >/dev/null 2>&1; then RES=$(getent ahostsv4 "${DOMAIN}" | awk '{print $1}' | head -1)
  else                                          RES=""; fi

  if [[ -z "${RES}" ]]; then
    bad "${DOMAIN} 解析不出 IP。**先把 A 记录加上、等生效,再回来跑。**"
    note "现在 up 会让 Caddy 反复向 Let's Encrypt 申请失败,撞上频率限制就得干等。"
  elif [[ -z "${MYIP}" ]]; then
    warn "${DOMAIN} 解析到 ${RES},但查不出本机公网 IP,没法比对 —— 请自己确认这就是这台机器。"
  elif [[ "${RES}" == "${MYIP}" ]]; then
    ok "${DOMAIN} → ${RES},与本机公网 IP 一致"
  else
    bad "${DOMAIN} 解析到 ${RES},而本机公网 IP 是 ${MYIP} —— 指到别的机器上了。"
    note "证书申请一定失败。改对 A 记录、等生效,再 up。"
  fi
fi

# ---------------------------------------------------------------------------
head_ "④ 端口 / 数据目录 / 前端"
# ---------------------------------------------------------------------------
# 占用者是不是**我们自己**要分开说。
# 这个脚本叫 preflight,本意是 up 之前跑;但人一定会在站点已经跑起来之后
# 再跑一遍来"体检"。那时 80/443 必然被自家 caddy 占着,而原来的提示语是
# 「常见是系统自带的 nginx / apache」——**把正常状态描述成故障,还指错了方向**,
# 照着去 `systemctl stop nginx` 是白忙,去 kill 掉那个进程则是把自己站点弄挂。
OWN_CADDY=""
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^gyt-caddy$'; then
  OWN_CADDY="yes"
fi
for p in 80 443; do
  if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :${p}" 2>/dev/null | grep -q LISTEN; then
    if [[ -n "${OWN_CADDY}" ]]; then
      ok ":${p} 被本站自己的 gyt-caddy 占着(站点已在运行 —— 这不是问题)"
    else
      bad ":${p} 已经被别的进程占着(常见是系统自带的 nginx / apache)。Caddy 起不来。"
      note "看是谁:ss -ltnp 'sport = :${p}'"
    fi
  else
    ok ":${p} 空闲"
  fi
done

if [[ ! -d data ]]; then
  bad "data/ 目录不存在。先 mkdir -p data"
elif [[ "$(uname -s)" != "Linux" ]]; then
  # macOS / Docker Desktop 会自动做 uid 映射,属主不需要是 10001;
  # 这条只对 Linux 上的绑定挂载成立。在别处报错等于制造假警报。
  ok "data/ 在(非 Linux,跳过属主检查 —— Docker Desktop 自己做 uid 映射)"
else
  OWNER=$(stat -c '%u' data 2>/dev/null || echo "?")
  if [[ "${OWNER}" == "10001" ]]; then
    ok "data/ 属主是 10001(容器里的非 root 用户)"
  else
    bad "data/ 属主是 ${OWNER},不是 10001 —— 容器写不进去,台账和缓存全部落不了盘。"
    note "sudo chown -R 10001:10001 data"
  fi
fi

if [[ ! -f frontend/package.json ]]; then
  bad "frontend/ 还没生成。先 bash scripts/setup-frontend.sh"
  note "它不在 git 里,是从上游 clone 生成的。"
else
  ok "frontend/ 已就位"
  # 令牌注入这条链少一半就是「站点打得开、每次提问 401」,而构建全程不报错。
  if grep -q "NEXT_PUBLIC_API_KEY" frontend/Dockerfile 2>/dev/null; then
    ok "frontend/Dockerfile 有 ARG NEXT_PUBLIC_API_KEY(令牌能烘进浏览器包)"
  else
    bad "frontend/Dockerfile 缺 ARG NEXT_PUBLIC_API_KEY。"
    note "Docker 对没声明的 build arg 只警告不报错 —— 表现是站点打得开、每次提问 401。"
    note "修法:bash scripts/setup-frontend.sh --force"
  fi
  # 同一个失效模式的第二处:产物出口。漏了它站点一切正常,只是照片全是碎图、
  # 巡检记录点了没反应 —— 而且 https 拉 http 属于 mixed content,浏览器连请求都不发,
  # 界面上一点线索都没有,只有控制台里一行。
  if grep -q "NEXT_PUBLIC_ARTIFACT_BASE" frontend/Dockerfile 2>/dev/null; then
    ok "frontend/Dockerfile 有 ARG NEXT_PUBLIC_ARTIFACT_BASE(照片/巡检记录能显示)"
  else
    bad "frontend/Dockerfile 缺 ARG NEXT_PUBLIC_ARTIFACT_BASE。"
    note "表现是站点一切正常,但工地照片是碎图、巡检记录点了没反应。"
    note "修法:bash scripts/setup-frontend.sh --force"
  fi
fi

[[ -d data/demo/photos && -d data/demo/drawings ]] \
  && ok "演示资产在(照片 / 图纸)" \
  || warn "data/demo/ 下缺照片或图纸 —— 演示路径会跑不起来,确认 git 真的拉全了。"

# ---------------------------------------------------------------------------
head_ "⑤ 配置文件语法"
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  warn "没装 docker,跳过配置语法检查"
elif ! docker info >/dev/null 2>&1; then
  # ⚠️ 这个分支不能省。少了它,daemon 没起的时候 `docker run` 失败会被当成
  # 「Caddyfile 语法不过」——**把「工具没起」说成「你的配置错了」,排查方向全反**。
  # 这正是本仓反复强调的那类错:一条错误的指引比没有指引贵得多。
  warn "docker 装了但 daemon 没起(或没权限),跳过配置语法检查 —— 这**不是**配置有问题"
  note "起 daemon 之后重跑本脚本,把这两项补上再 up。"
else
  if docker run --rm -v "${REPO}/Caddyfile:/etc/caddy/Caddyfile:ro" \
       -e GYT_SITE_ADDRESS="${SITE:-:80}" \
       caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    ok "Caddyfile 语法通过"
  else
    bad "Caddyfile 语法不过 —— Caddy 起不来,整站连不上。"
    note "手动看原因:docker run --rm -v \"\$PWD/Caddyfile:/etc/caddy/Caddyfile:ro\" caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile"
  fi
  if docker compose -f docker-compose.yml -f docker-compose.vps.yml config >/dev/null 2>&1; then
    ok "compose 合并档解析通过"
    PORTS=$(docker compose -f docker-compose.yml -f docker-compose.vps.yml config 2>/dev/null | grep -c 'published:')
    if [[ "${PORTS}" -le 3 ]]; then
      ok "只有 caddy 发布端口(published 共 ${PORTS} 条)"
    else
      bad "published 端口有 ${PORTS} 条,超过 caddy 应有的数量 —— backend/frontend 可能漏了 ports: !reset []"
      note "零鉴权的 Agent 端点 + 两把 API Key 直接对公网,这是本仓最硬的红线。"
    fi
  else
    bad "compose 合并档解析不过。跑一遍看原因:docker compose -f docker-compose.yml -f docker-compose.vps.yml config"
  fi
fi

# ---------------------------------------------------------------------------
head_ "⑥ 机器规格"
# ---------------------------------------------------------------------------
MEM=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')
if [[ -n "${MEM}" ]]; then
  if [[ "${MEM}" -ge 3500 ]]; then ok "内存 ${MEM}MB"
  else bad "内存只有 ${MEM}MB。BGE-M3 推理吃内存,**4G 是下限不是舒适区**,建库那一步会被 OOM 杀掉。"; fi
fi
DISK=$(df -m . 2>/dev/null | awk 'NR==2{print $4}')
if [[ -n "${DISK}" ]]; then
  if [[ "${DISK}" -ge 10000 ]]; then ok "可用磁盘 ${DISK}MB"
  else warn "可用磁盘只有 ${DISK}MB。光 BGE-M3 权重就 2.2GB,再加镜像,建议留 10GB 以上。"; fi
fi

# ---------------------------------------------------------------------------
printf '\n'
if [[ "${FATAL}" -gt 0 ]]; then
  printf '%s致命 %d 项,警告 %d 项 —— 先修掉致命项再 up。%s\n' "${RED}" "${FATAL}" "${WARN}" "${OFF}"
  exit 1
fi
if [[ "${WARN}" -gt 0 ]]; then
  printf '%s可以 up,但有 %d 项警告,扫一眼确认是你知道的。%s\n' "${YELLOW}" "${WARN}" "${OFF}"
else
  printf '%s全部通过,可以 up。%s\n' "${GREEN}" "${OFF}"
fi
printf '\n下一步(知识库那步别跳,漏了 knowledge 全废):\n'
printf '  docker compose -f docker-compose.yml -f docker-compose.vps.yml run --rm --no-deps backend \\\n'
printf '      python -m gyt.agents.knowledge.ingest\n'
printf '  docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build\n'
printf '\n⚠️ 后端建图要十几秒。这段窗口里 Caddy 会回 502,那是正常的 —— 等 healthy 再试。\n'
printf '   docker compose -f docker-compose.yml -f docker-compose.vps.yml ps\n'
