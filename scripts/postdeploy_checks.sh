#!/usr/bin/env bash
# 上线后功能验证 —— 部署**完成之后**跑,对着线上站点打真请求,逐条验 W7 打卡链。
#
#     bash scripts/postdeploy_checks.sh --help
#
# 和 scripts/preflight_vps.sh 是**两件事,不要互相替代**:
#   preflight  = up 之前,在 VPS 上查配置/端口/构建产物,一个网络请求都不发(除了解析域名);
#   本脚本     = up 之后,从外面打 HTTP,验的是「跑起来的东西行为对不对」。
#   前者能保证「配置写对了」,保证不了「跑起来是对的」。
#
# 它覆盖 W7 方案 §11 那 12 条里**只有线上真机才验得了**的那几条:
#   闸①(鉴权是否真的挂上)、闸④(大图纸回归)、打卡的体积闸、产物出口、
#   队友端点没被碰坏、W6 的登录门禁还在、发布端口仍是 3。
# 它们的共同点是**坏了没有任何信号** —— 站点照开、页面照渲染、日志一行不报。
# 剩下那几条(闸②伪造 digest、闸⑤.ttc 取面、闸⑥时区)要直查 sqlite 或看渲染,
# 从外面打 HTTP 看不见,**本脚本不假装验了**(结尾会把它们再点一遍名)。
#
# 退出码:
#   0 = 全过(跳过项不算失败,但会在汇总里点名);
#   1 = 有检查项失败(线上有问题,别宣布上线成功);
#   2 = 没法开跑(缺环境变量 / 缺 curl / 参数写错)—— 与 1 分开,
#       因为「跑了发现问题」和「压根没跑起来」要采取的动作完全不同,
#       混成一个码的下场是有人看见非 0 就以为线上炸了。
#
# 只读:本脚本不改任何文件、不起任何容器、**不往生产库写任何一条业务数据**。
# 它一共发三种 POST /checkin,**每一种都在「还没落库」的那一关就被挡下**:
#   · 无令牌、空 body           —— 期望停在鉴权那关(401);
#   · 带令牌、空 body           —— 期望停在 header 校验(400)。
#     验的是「鉴权层放行了」,**不是「打卡能成」**;
#   · 带令牌、十几 MB 的零字节  —— 验体积闸。同样不带业务 header,所以照样停在
#     header 校验;就算哪天那一关放宽了,13MB 也远超 GYT_PHOTO_MAX_MB=10,
#     后端的流式上限会在读到 10MB 时断掉,依然落不了库。
# 「打卡到底能不能成」只能真人拿手机做(W7 §11.4 的真机核对表),这个脚本代替不了。
#
# ⚠️ 本文件里**所有**变量展开都写成 ${VAR} 而不是 $VAR,与 preflight_vps.sh 同一条规矩:
# 中文文案里常出现 「$ORIGIN」 这种写法,而在**非 UTF-8 locale** 下(VPS 上很常见)
# bash 会把「」的字节当成变量名的一部分,于是 set -u 当场把脚本打死。
# **一个验证脚本自己中途暴毙,比没有它更坏** —— 人会以为「跑完了没报错」。
#
# ⚠️ 另一条同源规矩(照抄 preflight):不许用 bash 4+ 语法(${v,,}、关联数组、mapfile)。
# macOS 自带的 bash 是 3.2,在那儿会 "bad substitution" 并**打断整个流程**,
# 而这个脚本的主要跑法就是「从开发机(多半是 Mac)打公网」。
#
# ⚠️ 令牌绝不进输出。auth.py 的 authenticate 那段注释写得很清楚:
# 错误令牌常常是正确令牌打错一个字,原样落进日志等于把口令写进了日志。
# 所以本脚本:不打令牌、不打口令、不用 curl -v(-v 会把请求头连同令牌整个吐出来),
# 打印响应体之前还要过一遍 _redact()。

set -uo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
FAIL=0
SKIP=0
WARN=0

ok()   { printf '  %s✓%s %s\n' "${GREEN}" "${OFF}" "$1"; }
bad()  { printf '  %s✗%s %s\n' "${RED}" "${OFF}" "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  %s!%s %s\n' "${YELLOW}" "${OFF}" "$1"; WARN=$((WARN+1)); }
skip() { printf '  %s–%s %s\n' "${DIM}" "${OFF}" "$1"; SKIP=$((SKIP+1)); }
note() { printf '    %s%s%s\n' "${DIM}" "$1" "${OFF}"; }
head_() { printf '\n%s\n' "$1"; }

usage() {
  cat <<'EOF'
工友通 · 上线后功能验证(部署完成之后跑)

用法:
  bash scripts/postdeploy_checks.sh [--remote | --local] [--help]

两种模式:
  --remote  (默认)从开发机对着公网域名打 HTTP。七条里能验六条。
            这是你在自己电脑上该用的那个。
  --local   在 VPS 上跑。同样打 HTTP(但把域名钉到 127.0.0.1,绕开
            某些机房不支持自环回访问自己公网 IP 的问题),**额外**多验一条:
            docker ps 数发布端口。那条要摸得到 docker,只能在机器上做。

必须的环境变量:
  GYT_PUBLIC_ORIGIN   站点地址,例如 https://velactora.com(末尾不带斜杠)
  GYT_ACCESS_TOKEN    后端令牌(X-Api-Key)。绝不会被打印出来。

会话相关(站点有登录门禁,不给会话的话大半条检查会**没有结论**):
  GYT_SESSION_COOKIE  浏览器里 gyt_sess 那个 Cookie 的值。可以只给值,
                      也可以给完整的 "gyt_sess=xxxx"。推荐这条 —— 不用把口令给脚本。
  GYT_LOGIN_PASSWORD  访问口令。给了它脚本自己登一次拿 Cookie。
                      代价:会消耗登录接口的限流额度(serve_login.py 的 _LoginThrottle),
                      连着跑很多遍会被 429。口令不会被打印,也不会进 URL。

可选:
  GYT_PROBE_MB        体积闸探针的请求体大小,默认 13(单位 MB,必须 > 打卡闸的 12)
  GYT_INSECURE_TLS=1  跳过证书校验。**只在自签证书的临时环境用**,
                      平时开着等于把「证书过期了」这类故障验成绿的。

在 VPS 上想省事,可以先把 .env 灌进当前 shell 再跑:
  set -a; . ./.env; set +a; bash scripts/postdeploy_checks.sh --local

退出码:0 全过 / 1 有失败 / 2 没法开跑(缺变量、缺 curl、参数错)
EOF
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
MODE="remote"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) MODE="remote"; shift ;;
    --local)  MODE="local";  shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf '不认识的参数:%s\n\n' "$1"; usage; exit 2 ;;
  esac
done

command -v curl >/dev/null 2>&1 || { printf '没装 curl,这个脚本全靠它。先装上再来。\n'; exit 2; }

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------
ORIGIN="${GYT_PUBLIC_ORIGIN:-}"
TOKEN="${GYT_ACCESS_TOKEN:-}"
SESSION_COOKIE="${GYT_SESSION_COOKIE:-}"
LOGIN_PASSWORD="${GYT_LOGIN_PASSWORD:-}"
PROBE_MB="${GYT_PROBE_MB:-13}"

if [[ -z "${ORIGIN}" || -z "${TOKEN}" ]]; then
  printf '%s没法开跑:GYT_PUBLIC_ORIGIN 和 GYT_ACCESS_TOKEN 两个都得设。%s\n' "${RED}" "${OFF}"
  printf '\n  export GYT_PUBLIC_ORIGIN=https://你的域名\n'
  printf '  export GYT_ACCESS_TOKEN=你在 .env 里那把令牌\n'
  printf '\n在 VPS 上可以直接把 .env 整份灌进来:set -a; . ./.env; set +a\n'
  printf '(⚠️ 那会把两把模型 Key 也灌进当前 shell,别在共享的机器上这么干。)\n'
  exit 2
fi
# 末尾斜杠会让后面拼出 //api,Caddy 的 path 匹配器认不出来 —— 直接掐掉,不报错。
ORIGIN="${ORIGIN%/}"

# 令牌长度:auth.py 的 _MIN_TOKEN_LEN 是 24,短于它后端会**当成没设**而静默不设防。
# 这里只报长度、不报内容 —— 长度不缩小搜索空间的量级(auth.py 里那段
# compare_digest 的注释解释过同一件事)。
if [[ ${#TOKEN} -lt 24 ]]; then
  printf '%s没法开跑:GYT_ACCESS_TOKEN 只有 %d 位,后端会把它当成没设(auth.py 的 _MIN_TOKEN_LEN=24)。%s\n' \
    "${RED}" "${#TOKEN}" "${OFF}"
  printf '拿一把真的再来,或者先确认线上到底设没设。\n'
  exit 2
fi

case "${PROBE_MB}" in ''|*[!0-9]*) printf 'GYT_PROBE_MB 得是个整数。\n'; exit 2 ;; esac
if [[ "${PROBE_MB}" -le 12 ]]; then
  printf '%sGYT_PROBE_MB=%s,不大于打卡闸的 12MB —— 那两条体积检查会验不出东西。%s\n' \
    "${RED}" "${PROBE_MB}" "${OFF}"
  exit 2
fi

# ---------------------------------------------------------------------------
# curl 基础参数
# ---------------------------------------------------------------------------
# 故意不加 -L:好几条检查断言的就是 302 / 401 本身,跟着跳转走等于把结论跳没了。
CURL_BASE=(-sS)
[[ "${GYT_INSECURE_TLS:-}" == "1" ]] && CURL_BASE+=(-k)

HOSTNAME_ONLY="${ORIGIN#*://}"
HOSTNAME_ONLY="${HOSTNAME_ONLY%%/*}"
HOSTNAME_ONLY="${HOSTNAME_ONLY%%:*}"

if [[ "${MODE}" == "local" && "${ORIGIN}" == https://* ]]; then
  # 把域名钉到回环再走 https。理由与 docker-compose.vps.yml 里 caddy 那条
  # healthcheck 完全一样(那条真机验过三遍):在机器上访问**自己的公网域名**
  # 未必回得来(不少机房不做 hairpin NAT),而 --resolve 既走对 SNI
  # 又把解析钉在本机 —— 证书照样能验过,不需要 -k。
  CURL_BASE+=(--resolve "${HOSTNAME_ONLY}:443:127.0.0.1")
fi

TIMEOUT_S=20
TIMEOUT_BIG_S=90   # 十几 MB 的上传要慢一些,别用同一个数

# ---------------------------------------------------------------------------
# 临时文件
# ---------------------------------------------------------------------------
# macOS 的 BSD mktemp 不给模板时会直接报 usage,所以要两写法兜一下。
# ⚠️ 响应头会落进 ${HDRS},登录那一步的 Set-Cookie(= 一个可直接复用的会话)也在里面。
# 所以整个目录 trap 上退出即删,而且**从不打印它的内容**,只从里面 sed 出需要的那一段。
TMP="$(mktemp -d 2>/dev/null || mktemp -d -t gyt-postdeploy)"
[[ -n "${TMP}" && -d "${TMP}" ]] || { printf '建不出临时目录,没法继续。\n'; exit 2; }
trap 'rm -rf "${TMP}"' EXIT

BODY="${TMP}/body"
HDRS="${TMP}/headers"
CERR="${TMP}/curl.err"
BIG="${TMP}/big.bin"

# ---------------------------------------------------------------------------
# 请求小工具
# ---------------------------------------------------------------------------
STATUS=""     # 上一次请求的 HTTP 状态码(拿不到时是 "000")
CURL_RC=0     # 上一次 curl 的退出码,用来区分「服务器答了个怪的」和「压根没连上」

http_call() {
  # 用法:http_call <curl 参数…> <URL>
  # 响应体落 ${BODY},响应头落 ${HDRS},curl 自己的报错落 ${CERR}。
  local raw rc
  : > "${BODY}"; : > "${HDRS}"; : > "${CERR}"
  raw=$(curl "${CURL_BASE[@]}" -o "${BODY}" -D "${HDRS}" -w '%{http_code}' "$@" 2>"${CERR}")
  rc=$?
  CURL_RC=${rc}
  # ⚠️ 不能写成 `raw=$(curl …) || raw=000`。
  # 大 body 那两条探针里,服务器可能**先回 413 再把连接掐掉**,于是 curl 退出码非 0
  # 但 %{http_code} 已经是 413 了 —— 一律覆盖成 000 会把唯一有用的信息抹掉,
  # 表现是「明明闸生效了却报成连不上」,查的人会去查网络。
  STATUS="${raw}"
  case "${STATUS}" in ''|*[!0-9]*) STATUS="000" ;; esac
}

_redact() {
  # 打印响应体之前过一遍。正常情况下响应体里不会有令牌,但「正常情况下」这四个字
  # 正是本仓反复吃亏的地方 —— 兜一手不亏。
  # 只在令牌是纯 [A-Za-z0-9_-](openssl rand -hex 32 就长这样)时才做替换:
  # 否则令牌里的正则元字符会让 sed 自己出错,反而把响应体吞掉。
  case "${TOKEN}" in
    *[!A-Za-z0-9_-]*) cat ;;
    *) sed "s/${TOKEN}/***/g" ;;
  esac
}

excerpt() {
  # 响应体摘要,给排查用。截断到 300 字节 —— 可能把一个中文字切成半个,
  # 显示上会有个乱码尾巴,但这行只是线索不是判据,不值得为它多背一层依赖。
  tr -d '\r\n' < "${BODY}" 2>/dev/null | _redact | head -c 300
  printf '\n'
}

body_has() { grep -q -- "$1" "${BODY}" 2>/dev/null; }

# 会话门禁的 401 长什么样(serve_login.py 的 JSON_UNAUTHORIZED)。
# **这是全脚本最重要的一个判别式**:站点的每一条 /api/* 都要先过 Caddy 的
# forward_auth,没会话时它自己就回 401 了 —— 请求**根本没到后端**。
# 不把这种 401 认出来,「不带令牌 → 401 → 通过」就是一个假绿灯:
# 打卡端点即使完全裸奔,这条也会绿。
is_login_gate_401() { body_has '登录已过期'; }

curl_hint() {
  [[ ${CURL_RC} -eq 0 ]] && return 0
  local msg; msg=$(tr -d '\r\n' < "${CERR}" 2>/dev/null | head -c 200)
  note "curl 退出码 ${CURL_RC}${msg:+:${msg}}"
}

# ---------------------------------------------------------------------------
printf '工友通 · 上线后功能验证\n站点:%s\n模式:%s\n' "${ORIGIN}" \
  "$([[ "${MODE}" == "local" ]] && printf '在 VPS 上跑(--local,多验发布端口一条)' || printf '从开发机打公网(--remote)')"

# ===========================================================================
head_ "⓪ 前置:会话门禁(拿不到会话,后面七条里有五条会「没有结论」)"
# ===========================================================================
# 站点是两道门(Caddyfile 顶部那张图):
#   ① Caddy 的登录页 + 会话 Cookie —— 挡路过的人;
#   ② 后端 auth.py 校验 X-Api-Key —— 挡「Caddy 这层失效」。
# 本脚本要验的是②那一层的行为,所以必须先过①。过不去的话所有请求都停在
# 门口的 401/302 上,而那种 401 **看起来和「鉴权正常工作」一模一样**。
COOKIE_OK=""
COOKIE_HDR=""

if [[ -n "${SESSION_COOKIE}" ]]; then
  # 允许只给值,也允许给完整的 "gyt_sess=xxx" —— 从浏览器里拷的时候两种都常见。
  case "${SESSION_COOKIE}" in
    *=*) COOKIE_HDR="${SESSION_COOKIE}" ;;
    *)   COOKIE_HDR="gyt_sess=${SESSION_COOKIE}" ;;
  esac
  note "用的是 GYT_SESSION_COOKIE 里给的会话"
elif [[ -n "${LOGIN_PASSWORD}" ]]; then
  # 口令走 --data-urlencode(进 body,不进 URL)—— 进 URL 的话会落进 Caddy 的
  # 访问日志。日志里 Cookie/Authorization/X-Api-Key 三个头是被过滤的,
  # 但**请求行里的 query 不是**,那是另一条泄漏路径。
  http_call -X POST --max-time "${TIMEOUT_S}" \
    --data-urlencode "pw=${LOGIN_PASSWORD}" "${ORIGIN}/login"
  COOKIE_HDR=$(grep -i '^set-cookie:' "${HDRS}" 2>/dev/null \
                 | sed -n 's/.*\(gyt_sess=[^;]*\).*/\1/p' | head -1)
  if [[ -z "${COOKIE_HDR}" ]]; then
    warn "拿 GYT_LOGIN_PASSWORD 登录没换到会话(HTTP ${STATUS})。"
    note "口令不对会 302 回 /login?e=1 且不发 Cookie;短时间试太多次会 429(登录接口自带限流)。"
    curl_hint
  fi
else
  warn "既没给 GYT_SESSION_COOKIE 也没给 GYT_LOGIN_PASSWORD。"
  note "下面凡是要过门禁的检查都会变成「跳过」,而不是「通过」—— 那种绿灯是假的。"
fi

if [[ -n "${COOKIE_HDR}" ]]; then
  # 探会话是否真的有效:带着 Cookie 打 /login。
  # serve_login.py 的 _handle_login_page:会话有效 → 302 去 /,无效 → 200 登录页。
  # 挑这条是因为它**不需要令牌、不碰任何业务状态**,是最干净的一个会话探针。
  http_call --max-time "${TIMEOUT_S}" -H "Cookie: ${COOKIE_HDR}" "${ORIGIN}/login"
  if [[ "${STATUS}" == "302" ]]; then
    ok "会话有效(带 Cookie 访问 /login 被弹回首页,说明门禁认得它)"
    COOKIE_OK="yes"
  elif [[ "${STATUS}" == "200" ]]; then
    # 这里给警告不给失败:会话拿错了是**脚本的入参**有问题,不是线上有问题。
    # 判成失败会让退出码变 1,而汇总那句写的是「线上有问题,别宣布上线成功」——
    # 把「你 Cookie 粘错了」说成「线上炸了」,是本仓最忌讳的那种错误指引。
    # 后面依赖会话的五条会各自「跳过」,汇总里点得清清楚楚。
    warn "会话无效或已过期(带 Cookie 访问 /login 仍然返回登录页)。"
    note "Cookie 拷漏了、拷的是别的域名的、或者口令改过了(改口令 = 全员会话失效,"
    note "会话密钥从口令哈希派生,见 serve_login.py 的 _session_key)。"
  else
    bad "探会话时拿到意外的 HTTP ${STATUS} —— 站点可能压根没起来。"
    note "先看:docker compose -f docker-compose.yml -f docker-compose.vps.yml ps"
    curl_hint
  fi
fi

# ===========================================================================
head_ "① 🔴 打卡端点鉴权(W7 上线闸①,全套里最关键的一条)"
# ===========================================================================
# 为什么它排第一:backend/langgraph.json 里少了 "enable_custom_route_auth": true,
# 打卡路由就**完全不过 auth.py,而且不会有任何报错**
# (langgraph_api/server.py:169 实测,那个键默认是假值)——
# 站点照开、打卡照成,只是那几百行鉴权一行都不执行。
#
# 发的请求**故意是残废的**:没有任何 X-GYT-* 业务 header、body 为空。
# post_checkin 的流程是 ⓪令牌自查 → ①限流 → ②header 校验,所以:
#   没令牌  → 期望停在⓪,拿 401;
#   有令牌  → 期望走到②被 header 校验挡下,拿 400。
# 两条都到不了「写库/画水印」那一步 —— 这是刻意的:我们验的是**鉴权层的行为**,
# 不是「打卡能不能成」。后者要真人拿手机打一次(W7 §11.4),脚本代替不了。
if [[ -z "${COOKIE_OK}" ]]; then
  skip "没有有效会话,跳过 —— 没会话时 Caddy 自己就回 401 了,请求根本到不了后端。"
  note "那种 401 和「鉴权正常」长得一模一样,当成通过就是给自己发假绿灯。"
  note "补上 GYT_SESSION_COOKIE 再跑一遍,这条是本次部署最该看的一条。"
else
  # --- ①-a 不带令牌 ---------------------------------------------------------
  http_call -X POST --max-time "${TIMEOUT_S}" \
    -H "Cookie: ${COOKIE_HDR}" -H 'Content-Type: image/jpeg' \
    --data-binary '' "${ORIGIN}/api/checkin"

  case "${STATUS}" in
    401|403)
      if is_login_gate_401; then
        skip "拿到 401,但那是**会话门禁**回的(响应体里是「登录已过期」),这条没有结论。"
        note "会话在中途失效了。重新拿一个 Cookie 再跑。"
      elif body_has '"error_code":"UNAUTHORIZED"'; then
        # 这是 checkin_api.py 的 _deny_if_token_bad 回的(四键信封)。
        # 它能挡住说明纵深第二道在,但同时说明**第一道没先答话** ——
        # langgraph 的鉴权中间件如果生效,它会先回 {"detail": …},轮不到 handler。
        # (W7 §12.2 的实测就是这么区分的:那次的 401 来自 langgraph 层,证明 json 键生效。)
        ok "无令牌的 POST /checkin 被拒(401),端点没有裸奔"
        warn "但拦下它的是 handler 自查(响应是四键信封),不是 langgraph 的鉴权中间件。"
        note "正常部署下 401 应该来自中间件、长这样:{\"detail\":\"访问被拒绝,请联系发你链接的人。\"}"
        note "看到信封 = backend/langgraph.json 的 enable_custom_route_auth 很可能漏了。"
        note "今天还安全(纵深第二道兜住了),但那一层没了就只剩一道 —— 去核那个键。"
      elif body_has '"detail"'; then
        ok "无令牌的 POST /checkin 被 langgraph 鉴权中间件拒掉(${STATUS}),enable_custom_route_auth 生效"
      else
        ok "无令牌的 POST /checkin 被拒(${STATUS}),端点没有裸奔"
        warn "但响应体不是已知的任何一种(既不是中间件的 {\"detail\"…},也不是 handler 的信封)。"
        note "响应:$(excerpt)"
        note "多半是中间还有别的东西在答话 —— 认不出是哪一层挡的,就不知道哪一层其实已经没了。"
      fi
      ;;
    400)
      bad "🔴 无令牌的 POST /checkin 返回 400 —— **打卡端点在裸奔**。"
      note "400 意味着请求一路走到了 header 校验:鉴权中间件没拦、handler 自查也没拦。"
      note "两道同时失守只有一种解释:线上 GYT_ACCESS_TOKEN 没设,或者设成了占位符/短于 24 位"
      note "(auth.py 与 core/access.py 把这三种一律当成「没配」= 放行)。"
      note "去看:VPS 上 docker compose ... logs backend | head,启动时会打一整块感叹号横幅。"
      ;;
    200)
      bad "🔴 无令牌的 POST /checkin 居然返回 200 —— 端点完全敞开,立刻下线排查。"
      note "响应:$(excerpt)"
      ;;
    429)
      skip "被限流(429),这条没有结论。等一分钟再跑。"
      note "打卡全局桶见 config.py 的 attendance_rate_burst / attendance_rate_per_minute。"
      ;;
    000)
      bad "连不上 ${ORIGIN}/api/checkin。"
      curl_hint
      ;;
    *)
      bad "无令牌的 POST /checkin 拿到意外的 HTTP ${STATUS}(期望 401)。"
      note "响应:$(excerpt)"
      ;;
  esac

  # --- ①-b 带正确令牌 -------------------------------------------------------
  # 期望 400:说明⓪那一关放行了,请求走到了 header 校验。
  # **这不证明打卡能成功**,只证明鉴权层认这把令牌。
  # 副作用说清楚:这一发会从打卡的全局限流桶里扣一个令牌(桶在读 body 之前结算),
  # 不写库、不落图、不花模型钱。
  # 预算:config.py 的 attendance_rate_burst=10、每分钟补 30。整份脚本最多打三发
  # /api/checkin(①-a 在鉴权那关就被拒、扣不到桶,所以实际是两发),连跑几遍也撑得住;
  # 真撞上 429 会显示成「跳过」而不是失败,等一分钟即可。
  http_call -X POST --max-time "${TIMEOUT_S}" \
    -H "Cookie: ${COOKIE_HDR}" -H "X-Api-Key: ${TOKEN}" -H 'Content-Type: image/jpeg' \
    --data-binary '' "${ORIGIN}/api/checkin"

  case "${STATUS}" in
    400)
      ok "带正确令牌的 POST /checkin 走到了业务校验(400,残废请求被 header 校验挡下)—— 令牌被认"
      ;;
    401|403)
      bad "带正确令牌仍被拒(${STATUS})—— 你手上这把令牌和线上那把对不上。"
      note "线上以 compose 注入的 GYT_ACCESS_TOKEN 为准;前端包里那把是构建期烘进去的,"
      note "改了令牌只重启不重建前端 = 页面拿旧令牌打,每次提问 401(preflight ④ 组守的是另一半)。"
      ;;
    429)
      skip "被限流(429),这条没有结论。等一分钟再跑。"
      ;;
    000)
      bad "连不上。"
      curl_hint
      ;;
    *)
      warn "带令牌的 POST /checkin 拿到 HTTP ${STATUS}(期望 400)。"
      note "响应:$(excerpt)"
      note "不是 401 就说明鉴权放行了(本条的真正目的已达到),但这个状态码值得看一眼。"
      ;;
  esac
fi

# ===========================================================================
head_ "② 🔴 大图纸回归:非打卡路径不该被打卡那道 12MB 闸误伤(W7 上线闸④)"
# ===========================================================================
# 本次部署给 Caddy 加了 /api/checkin* 专用的 12MB 请求体闸,而全局那条要大得多
# (Caddyfile 现值 **192MB**,由 GYT_DRAWING_MAX_MB=100 × base64 4/3 ≈ 133MB 加余量推出;
#  W7 方案 §6 里写的「全局 128MB」是 DXF 还是 64MB 时的旧数,别照那个核)。
# 闸④要防的是:@checkin 那个匹配器写宽了、或者两块 request_body 的位置写反了,
# 于是图纸上传也被收到 12MB —— 报错来自 Caddy 的 413,查的人会去翻 uploads.py,方向全错。
#
# ⚠️ 这是一个**代理层探针,不等于真传了一张图纸**:
# 它只证明「Caddy 没在体积这一关把请求拦下」。真正的端到端(选一个 .dxf、
# 走 multipart、指定 project_id、落库、能在资料库里看见)要人拿真图纸传一次。
# 那条才是 W7 §11.8 说的回归,这条只是它的廉价前哨。
PROBE_READY="yes"
if ! dd if=/dev/zero of="${BIG}" bs=1048576 count="${PROBE_MB}" >/dev/null 2>&1; then
  PROBE_READY=""
fi

if [[ -z "${PROBE_READY}" ]]; then
  skip "造不出 ${PROBE_MB}MB 的探针文件(dd 失败),这条跳过。"
  note "临时目录 ${TMP} 所在的盘满了?"
elif [[ -z "${COOKIE_OK}" ]]; then
  skip "没有有效会话,跳过 —— 门禁排在 request_body 之前,没会话时永远是 401,验不到体积闸。"
else
  # 打的是图纸上传那条真路径,但用一个**不存在的 project_id** + 非 multipart 的
  # Content-Type:webapp.py 的 upload_drawing 走 request.form(),
  # 非表单类型解析出来是空的 → 400「没收到图纸文件」,一个字节都不会落库落盘。
  http_call -X POST --max-time "${TIMEOUT_BIG_S}" --expect100-timeout 5 \
    -H "Cookie: ${COOKIE_HDR}" -H "X-Api-Key: ${TOKEN}" \
    -H 'Content-Type: application/octet-stream' \
    --data-binary "@${BIG}" \
    "${ORIGIN}/api/projects/__postdeploy_probe__/drawings"

  case "${STATUS}" in
    413)
      bad "🔴 ${PROBE_MB}MB 的请求体在**非打卡路径**上被 413 掉了 —— 图纸上传路径被误伤。"
      note "现在传大 DXF 一定失败,而工友看到的是 Caddy 的 413,不是那句写好的中文提示。"
      note "去看 Caddyfile:@checkin 匹配器是不是写宽了(它只该是 /api/checkin 和 /api/checkin/*),"
      note "以及 12MB 那个 request_body 是不是漏在了 handle @checkin 外面(那样它就成了全局的)。"
      note "核路由树的唯一可靠手段:caddy adapt --config /etc/caddy/Caddyfile"
      ;;
    000)
      skip "上传中途断了,这条没有结论(curl 退出码 ${CURL_RC})。"
      note "可能是网络,也可能是服务端把连接掐了。上行慢的话把 GYT_PROBE_MB 调小一点(仍要 >12)。"
      curl_hint
      ;;
    *)
      ok "${PROBE_MB}MB 的请求体在非打卡路径上没被体积闸拦(HTTP ${STATUS})—— 图纸上传路径没被误伤"
      note "提醒:这是代理层探针。真机回归还是要选一张 >10MB 的 .dxf 走一遍上传面板。"
      ;;
  esac
fi

# ===========================================================================
head_ "③ 打卡专用的 12MB 请求体闸确实生效"
# ===========================================================================
# 和②是一对:②验「不该拦的没拦」,③验「该拦的拦了」。
# 只做一条的话,两种写错法各有一半查不出来。
#
# 12MB 这个数的账在 Caddyfile 里:GYT_PHOTO_MAX_MB=10,打卡走 raw body 不过 base64
# (体积不膨胀),留 2MB 给 header 与误差。它防的是「拿一个 10GB 的流把带宽打满」,
# 和应用层那道流式上限(给工友人话)不互相替代。
#
# ⚠️ 这条的「不过」只报**警告**不报失败,理由必须写清楚,否则下一个人会以为是漏判:
# 请求体上限在 Caddy 里有两种可能的执行时机 —— 一种是收到请求头时就拿
# Content-Length 判、当场 413;另一种是把 body 包成 MaxBytesReader、**读到超限那一刻**
# 才 413。走第二种时,后端因为拿不到 X-GYT-* 业务 header 会先一步回 400
# (header 校验排在读 body 之前),于是从外面看到的是 400 而不是 413 ——
# 而这**既可能是闸没配上,也可能只是闸的时机不同**,一次 curl 分不开这两种。
# 把分不开的事情判成失败,就是在上线当天制造一个假故障;所以这里给警告 + 一条
# 能真正定性的指令(caddy adapt 看路由树),把判断交回给人。
if [[ -z "${PROBE_READY}" ]]; then
  skip "没有探针文件,跳过(理由同②)。"
elif [[ -z "${COOKIE_OK}" ]]; then
  skip "没有有效会话,跳过(理由同②)。"
else
  # --expect100-timeout:curl 对 >1KB 的 body 默认会先发 Expect: 100-continue 再等。
  # 给它 5 秒,让「收到头就判 Content-Length」那种实现有机会在**一个字节都没上传**时
  # 就回 413 —— 既快,又能把这条检查的结论变干净。
  http_call -X POST --max-time "${TIMEOUT_BIG_S}" --expect100-timeout 5 \
    -H "Cookie: ${COOKIE_HDR}" -H "X-Api-Key: ${TOKEN}" -H 'Content-Type: image/jpeg' \
    --data-binary "@${BIG}" "${ORIGIN}/api/checkin"

  case "${STATUS}" in
    413)
      if body_has '"error_code":"FILE_TOO_LARGE"'; then
        # 四键信封 = 后端 _read_photo 的流式上限(10MB)回的,不是 Caddy。
        # 能走到这一步说明整个 body 都进了后端 —— 工友那句中文提示是有的,
        # 但「别让人拿大流量打满带宽」这件事没人管。
        warn "拿到 413,但响应是后端的四键信封 —— 拦下它的是应用层(10MB),不是 Caddy 那道闸。"
        note "整个 body 都被转发到了后端。去 caddy adapt 看 handle @checkin 里有没有那块 request_body。"
      else
        ok "${PROBE_MB}MB 的请求体打 /api/checkin 被 413(且不是后端回的,Caddy 那道闸生效)"
      fi
      ;;
    400)
      warn "${PROBE_MB}MB 的请求体没被 413,一路到了后端(400 来自 header 校验)。"
      note "两种可能,一次 curl 分不开:(a) Caddy 的 12MB 闸没配上;"
      note "(b) 配上了,但它是「读到超限才 413」那种时机,而后端的 header 校验抢先答了 400。"
      note "定性的办法只有一个 —— 在 VPS 上打出路由树,看 handle @checkin 里到底有没有那块 request_body:"
      note "  docker compose -f docker-compose.yml -f docker-compose.vps.yml exec caddy \\"
      note "    caddy adapt --config /etc/caddy/Caddyfile"
      note "顺带确认它排在 handle_path /api/* **之前** —— route{} 里 handle 按书写顺序择一匹配,"
      note "写到后面 = 永远不生效,而且没有任何报错。"
      ;;
    000)
      skip "上传中途断了,这条没有结论(curl 退出码 ${CURL_RC})。"
      note "注意:闸生效时服务端也可能先回 413 再把连接掐掉。看一眼 caddy 日志能确认。"
      curl_hint
      ;;
    429)
      skip "被限流(429),这条没有结论。等一分钟再跑。"
      ;;
    *)
      warn "打 /api/checkin 的 ${PROBE_MB}MB 请求拿到 HTTP ${STATUS}(期望 413)。"
      note "响应:$(excerpt)"
      ;;
  esac
fi

# ===========================================================================
head_ "④ 凭证图出口可达(ARTIFACT_BASE 那条链)"
# ===========================================================================
# 这条链断在任何一环都**不报错**:站点照开、打卡照成,只是凭证图是碎图。
# 而 https 页面拉 http 资源属于 mixed content,浏览器连请求都不发,界面上一点线索都没有。
# 链路:checkin_api 的响应带 artifact_id → checkin.tsx 的 <img> 拼 ARTIFACT_BASE
#      → Caddyfile 的 handle_path /artifacts/* → artifacts 服务的 /by-id/<32位>。
if [[ -z "${COOKIE_OK}" ]]; then
  skip "没有有效会话,跳过(/artifacts/* 也在门禁后面,这是刻意的 —— 里面是工地现场照片)。"
else
  http_call --max-time "${TIMEOUT_S}" \
    -H "Cookie: ${COOKIE_HDR}" -H "X-Api-Key: ${TOKEN}" \
    "${ORIGIN}/api/checkin/recent?limit=5"

  if [[ "${STATUS}" != "200" ]]; then
    bad "GET /api/checkin/recent 拿到 HTTP ${STATUS}(期望 200)。"
    note "响应:$(excerpt)"
    note "这条也是打卡界面「刷新页面后凭证还在」依赖的接口,它挂了界面就是空列表。"
  else
    ok "GET /api/checkin/recent 通(200)"
    # 只认 32 位小写十六进制 —— core/artifacts.py 的编号就长这样,
    # 顺带把 "artifact_id":null 那种自然排除掉(那是凭证图已被清理,不是故障)。
    ART=$(tr ',' '\n' < "${BODY}" 2>/dev/null \
            | sed -n 's/.*"artifact_id":"\([0-9a-f]\{32\}\)".*/\1/p' | head -1)
    if [[ -z "${ART}" ]]; then
      skip "线上还没有带凭证图的打卡记录,取不到 artifact_id —— 跳过,这不算失败。"
      note "真人用手机打一次卡之后重跑本脚本,这条才验得了。"
      note "如果**有**打卡记录但 artifact_id 全是 null,那是凭证图已过期被清理(attendance/cleanup.py),"
      note "界面该显示「凭证图已过期清理」而不是裂图标(W7 上线闸③,那条要肉眼看)。"
    else
      # ⚠️ 这里绝不能再往 http_call 里塞 -w:它内部已经用 -w '%{http_code}' 取状态码,
      # 后来的 -w 会盖掉前面那个,STATUS 直接变空 → 被归一成 000 → 好端端的一条报成连不上。
      http_call --max-time "${TIMEOUT_S}" -H "Cookie: ${COOKIE_HDR}" \
        "${ORIGIN}/artifacts/by-id/${ART}"
      CTYPE=$(grep -i '^content-type:' "${HDRS}" 2>/dev/null | head -1 | tr -d '\r')
      case "${STATUS}" in
        200)
          if printf '%s' "${CTYPE}" | grep -qi 'image/'; then
            ok "凭证图取得到(200,${CTYPE#*: })"
          else
            warn "凭证图 URL 回了 200,但 Content-Type 不是图片(${CTYPE:-无})。"
            note "浏览器多半渲染不出来。看 serve_artifacts.py 的扩展名 → MIME 映射。"
          fi
          ;;
        404)
          bad "凭证图 404(编号 ${ART})—— 台账里有这一行,盘上没有这个文件。"
          note "两种可能:清理器把图删了但没把 artifact_id 置 NULL(那是死链,比裂图更坏);"
          note "或者 artifacts 服务挂的 ./data/artifacts 不是 backend 写的那一份。"
          ;;
        302)
          bad "取凭证图被弹去登录(302)—— 会话没被带上。"
          note "浏览器里是同源自动带 Cookie 的,这里是脚本没带对,多半不是线上的问题。"
          ;;
        502|503)
          bad "取凭证图拿到 ${STATUS} —— artifacts 服务没起来。"
          note "docker compose -f docker-compose.yml -f docker-compose.vps.yml ps artifacts"
          ;;
        *)
          bad "取凭证图拿到意外的 HTTP ${STATUS}。"
          note "响应:$(excerpt)"
          ;;
      esac
    fi
  fi
fi

# ===========================================================================
head_ "⑤ 队友的端点没被本次部署碰坏"
# ===========================================================================
# 本次合流动了 backend/webapp.py —— 把打卡两条路由铺进了他们那个 app
# (langgraph.json 的 http.app 只能有一个,那里是唯一的汇合点)。
# 铺错的表现不是启动报错,而是**某几条路由悄悄没了**,所以要正面打一遍。
if [[ -z "${COOKIE_OK}" ]]; then
  skip "没有有效会话,跳过。"
else
  for pair in "projects:项目列表" "library:资料库总览"; do
    ep="${pair%%:*}"; cn="${pair#*:}"
    http_call --max-time "${TIMEOUT_S}" \
      -H "Cookie: ${COOKIE_HDR}" -H "X-Api-Key: ${TOKEN}" "${ORIGIN}/api/${ep}"
    if [[ "${STATUS}" != "200" ]]; then
      bad "GET /api/${ep}(${cn})拿到 HTTP ${STATUS}(期望 200)。"
      note "响应:$(excerpt)"
      note "404 = 路由丢了,去看 webapp.py 的 routes 列表;500 = 看 backend 日志。"
    elif body_has '"ok":true'; then
      ok "GET /api/${ep}(${cn})返回 200 且是合法四键信封"
    else
      bad "GET /api/${ep}(${cn})返回 200,但响应体不是 {ok,data,user_msg,error_code} 信封。"
      note "响应:$(excerpt)"
      note "前端按信封解析,不是信封就是「页面一片空白但没报错」。"
    fi
  done
fi

# ===========================================================================
head_ "⑥ 登录门禁仍在(W6 的既有防线,这次部署不该弄坏)"
# ===========================================================================
# 2026-08-11 真踩过一次「站点当场裸奔」:handle/handle_path 在 Caddy 的固定指令
# 顺序表里排在 route 之前,把它们挪出 route 之后,forward_auth 永远到不了 ——
# 表现是未登录访问首页直接 200、没有任何报错、caddy validate 也说 Valid。
# 而令牌就烘在前端 JS 里。所以这条必须每次部署后正面打一发。
#
# ⚠️ 这两发**故意不带 Cookie**。
http_call --max-time "${TIMEOUT_S}" "${ORIGIN}/"
LOC=$(grep -i '^location:' "${HDRS}" 2>/dev/null | head -1 | tr -d '\r')
case "${STATUS}" in
  302)
    if printf '%s' "${LOC}" | grep -q '/login'; then
      ok "未登录访问首页 → 302 去 /login,门禁在"
    else
      warn "未登录访问首页返回 302,但 Location 不是 /login(${LOC:-无})。"
    fi
    ;;
  200)
    bad "🔴 未登录访问首页直接 200 —— **站点在裸奔**,令牌就烘在前端包里。"
    note "这正是 2026-08-11 那次事故的长相。立刻看 Caddyfile:"
    note "整个站点块必须包在**同一个** route{} 里,forward_auth 排在所有业务 handle 之前。"
    note "核路由树:caddy adapt --config /etc/caddy/Caddyfile(validate 说 Valid 也可能是裸奔的)"
    ;;
  000)
    bad "连不上站点首页。"
    curl_hint
    ;;
  *)
    warn "未登录访问首页拿到 HTTP ${STATUS}(期望 302)。"
    ;;
esac

http_call --max-time "${TIMEOUT_S}" "${ORIGIN}/api/projects"
case "${STATUS}" in
  401)
    if is_login_gate_401; then
      ok "未登录访问 /api/* → 401 JSON(而不是 302 到一张 HTML 登录页)"
      note "分两种回法是刻意的:前端的 fetch/SSE 拿到 302+HTML 会报一个和「登录过期」毫无关系的错。"
    else
      ok "未登录访问 /api/* → 401"
      warn "但响应体不是登录服务那句「登录已过期」,回它的可能是后端而不是门禁。"
      note "响应:$(excerpt)"
    fi
    ;;
  200)
    bad "🔴 未登录就能读 /api/projects —— 门禁对 /api/* 失效了。"
    note "响应:$(excerpt)"
    ;;
  302)
    warn "未登录访问 /api/* 拿到 302(期望 401 JSON)。"
    note "serve_login.py 靠 X-Forwarded-Uri 判断要回哪种;Caddy 的 forward_auth 少传那个头就会退化成 302。"
    note "后果:前端 fetch 拿到一坨 HTML 当 JSON 解析,报错完全指不到「登录过期」上。"
    ;;
  000)
    bad "连不上 /api/projects。"
    curl_hint
    ;;
  *)
    warn "未登录访问 /api/* 拿到 HTTP ${STATUS}(期望 401)。"
    ;;
esac

# ===========================================================================
head_ "⑦ 发布端口仍是 3(80 / 443tcp / 443udp)"
# ===========================================================================
# 整份编排里**只有 caddy 允许有 ports**。多出来一条就是把零鉴权的 Agent 执行端点
# (揣着两把模型 Key)、或者工地现场照片(含可识别人脸,TODO-22)直接挂上公网 ——
# 而且不会有任何报错。preflight ⑤ 组在 up **之前**数 compose 合并档;
# 这里在 up **之后**数真实运行的容器,两个数的是不同的东西:
# 合并档对不代表跑起来对(有人可能手工 docker run 了点什么)。
if [[ "${MODE}" != "local" ]]; then
  skip "这条要摸 docker,只能在 VPS 上跑:bash scripts/postdeploy_checks.sh --local"
elif ! command -v docker >/dev/null 2>&1; then
  skip "这台机器上没有 docker,跳过。"
elif ! docker ps >/dev/null 2>&1; then
  # 这个分支不能省。少了它,「daemon 没起 / 当前用户没进 docker 组」会被数成 0 个端口,
  # 而 0 ≤ 3,于是显示**通过** —— 把「没查」说成「查过了没问题」,是最坏的一种绿灯。
  skip "docker 装了但当前用户使不动(daemon 没起,或不在 docker 组)—— 这**不是**端口有问题。"
  note "sudo 一下重跑,或者 docker info 看原因。这条没查成,别当它过了。"
else
  # docker ps 的 Ports 列会把 IPv4 和 IPv6 各列一遍(0.0.0.0:80-> 和 [::]:80->),
  # 所以要按「宿主端口/协议」去重之后再数,否则永远是 6 条,永远报红。
  PUBLISHED=$(docker ps --format '{{.Ports}}' 2>/dev/null \
                | tr ',' '\n' | grep -F -- '->' \
                | sed -E 's#.*:([0-9]+)->[0-9]+/([a-z]+).*#\1/\2#' | sort -u)
  PCOUNT=$(printf '%s\n' "${PUBLISHED}" | grep -c '[0-9]')
  PUBLISHERS=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
                 | grep -F -- '->' | awk '{print $1}' | sort -u | tr '\n' ' ')
  RUNNING=$(docker ps -q 2>/dev/null | grep -c .)
  if [[ "${RUNNING}" -eq 0 ]]; then
    # 「一个容器都没跑」和「跑着但端口少了」要分开说。合成一句的话,
    # 人会照着「caddy 没起来」去 restart caddy,而真相是整栈都没 up
    # (或者当前用户看到的是另一个 docker 上下文 / 另一个 daemon)。
    bad "这台机器上一个容器都没在跑 —— 站点没起来,或者你连的不是跑着它的那个 docker。"
    note "docker compose -f docker-compose.yml -f docker-compose.vps.yml ps"
    note "如果上面那条列得出容器而 docker ps 列不出,多半是 DOCKER_HOST / docker context 指错了。"
  elif [[ "${PCOUNT}" -eq 3 ]]; then
    ok "发布端口共 3 条($(printf '%s' "${PUBLISHED}" | tr '\n' ' ')),发布者:${PUBLISHERS}"
    if [[ "${PUBLISHERS}" != "gyt-caddy " ]]; then
      warn "但发布者不只是 gyt-caddy(${PUBLISHERS})—— 数目对了不代表是对的那三条。"
    fi
  elif [[ "${PCOUNT}" -lt 3 ]]; then
    bad "发布端口只有 ${PCOUNT} 条(期望 3):$(printf '%s' "${PUBLISHED}" | tr '\n' ' ')"
    note "caddy 可能没起来,站点这会儿是打不开的。docker compose ... ps 看一眼。"
  else
    bad "🔴 发布端口有 ${PCOUNT} 条(期望 3):$(printf '%s' "${PUBLISHED}" | tr '\n' ' ')"
    note "发布者:${PUBLISHERS}"
    note "多出来的那条极可能是 backend(:2024,零鉴权 + 两把模型 Key)或 artifacts(工地照片)。"
    note "backend / frontend / artifacts / login 四个服务在 docker-compose.vps.yml 里"
    note "都必须 ports: !reset [] 或干脆没有 ports。立刻查,这是本仓最硬的红线。"
  fi
fi

# ===========================================================================
printf '\n'
if [[ "${FAIL}" -gt 0 ]]; then
  printf '%s失败 %d 项,警告 %d 项,跳过 %d 项 —— 线上有问题,别宣布上线成功。%s\n' \
    "${RED}" "${FAIL}" "${WARN}" "${SKIP}" "${OFF}"
  exit 1
fi
if [[ "${SKIP}" -gt 0 || "${WARN}" -gt 0 ]]; then
  printf '%s没有失败项,但跳过 %d 项、警告 %d 项 —— 跳过**不等于通过**,逐条看一眼是不是你能接受的。%s\n' \
    "${YELLOW}" "${SKIP}" "${WARN}" "${OFF}"
else
  printf '%s全部通过。%s\n' "${GREEN}" "${OFF}"
fi

printf '\n这个脚本验不到的(必须人做,别以为全绿就完事了):\n'
printf '  · 真人拿手机打一次卡 —— 自拍能不能拍、定位取不取得到、水印五行汉字有没有方块。\n'
printf '    W7 §12.4 的教训:1107 个单测 + routing 26/26 全绿,都没发现「首次打卡必然拿不到定位」。\n'
printf '  · 真机核对表四种组合(iOS Safari / 安卓 Chrome / iOS WhatsApp / 安卓 WhatsApp),W7 §11.4。\n'
printf '  · 真传一张 >10MB 的 .dxf 走完上传面板(②只是代理层探针,不是端到端)。\n'
printf '  · 闸②(伪造 X-GYT-Digest 落库取服务端值)、闸⑥(篡改 TZ 后 work_date 不变)——\n'
printf '    要直查 sqlite 才看得见,从外面打 HTTP 看不出来。\n'
exit 0
