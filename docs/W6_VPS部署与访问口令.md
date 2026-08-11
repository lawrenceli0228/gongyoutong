# W6 · VPS 部署与访问口令(给外部测试用)

> 面向:**拿到一台 VPS 的 root、但没参与过这个项目的人**。
> 目标:一小时内把整套东西跑起来,然后把「网址 + 一个口令」发给几个外部测试者。
> 写作日期 2026-08-11 · 分支 `lawrence/state-claim-audit`
> **2026-08-11 凌晨补:整套流程已经在一台真 VPS 上从头跑通过一次,见 §0.0。**
>
> **2026-08-11 上午再补(提交 `ccb8d27`):第一道门整个换掉了。**
> `basic_auth`(浏览器原生弹框)→ **自建登录页 + 会话 Cookie**,口令哈希 bcrypt → `hashlib.scrypt`,
> 环境变量 `GYT_BASIC_AUTH_USER` / `GYT_BASIC_AUTH_HASH` → **`GYT_LOGIN_HASH`**(一个,没有用户名了),
> 服务从四个变**五个**。全部门层已从外部机器重新实测,见 §3.1。
> **换的过程中把站点弄裸奔过一次 —— 那是这一轮最贵的一课,单开一节写在 §0.5,先读它。**
>
> **关于「实测」二字。** 本仓最硬的一条规矩是「不许写没验证过的事实」,所以本手册把来源分四档:
>
> - **VPS 实测** —— 2026-08-11 凌晨那次真实上线(Debian 12 / velactora.com)跑出来的,给了具体数值;
> - **实测** —— 写手册的人在本机(macOS + Docker Compose v5.1.3)真跑过命令、真看过输出;
> - **源文件核对** —— 从仓库源文件或依赖库源码读出来的,给了文件名;
> - **⚠️ 未在真机验证** —— 那次上线也没覆盖到,按标准做法或按代码判据写的。
>
> §9 把每一条逐个列了出来。**你照做时哪一步跟手册对不上,先去 §9 看它是哪一档。**
>
> ⚠️ **新增的「VPS 实测」是一次、一台机器、并发 1 的样本。**
> 它证明的是「这条路走得通」,**不是**「任何机器上都这样」。凡是能从中推出的更强结论
> (「1.9GB 内存够用」「30GB 磁盘够用」),本文一律没写 —— 因为没验过。

---

## 0. 先读完这一页,再动手

### 0.0 ✅ 这套流程已经在一台真 VPS 上跑通过一次(2026-08-11 凌晨)

**本手册最初写完时,一步都没在真 VPS 上走过 —— 全流程是拼出来的。现在不是了。**
那次上线的机器与结果:

| 项 | 实际值 |
|---|---|
| 系统 | **Debian 12**,x86_64 |
| 规格 | **2 vCPU / 1.9GB 内存**(`free -m` 报 1966MB)/ **30GB 磁盘** |
| 域名 | `velactora.com`,DNS 托管在 **Cloudflare**(**灰云 DNS only**,见 §1.3) |
| 证书 | Let's Encrypt 签发成功:`CN=velactora.com`,有效期 **2026-08-10 → 2026-11-08** |
| 结果 | **整套跑通** —— 门锁、令牌、业务链路、持久化都实测过 |

证书那一行不是看 Caddy 自己的日志得出的,是**从外部另一台机器**用 `openssl s_client` 独立验的
—— 服务自己说签成了不算数,外面能握上手才算。

#### ✅ 已经在真机验证过的部分

- **步骤 1~12 的完整链路**:从空机器到「输口令、提问、拿到答案」。
  ⚠️ 但**全程是用 `curl` 打的,没有开过真浏览器** —— 登录页在手机/电脑上长什么样、
  前端 JS 起来之后会不会报错、SSE 在浏览器里逐不逐字,这三件都还没人看过。
  见下面「仍然没验证的部分」。
- **Caddy 自动签发 Let's Encrypt 证书**(域名模式)。手册原来把这条列在「未验证」里,现在成立了。
- **三层门的实际返回码**:口令层(§3.1)、令牌层(§3.2 的 A/B 两条)。
  连带抓到一个差点误判的陷阱:**`/api/ok` 不能用来验令牌层**(§3.2)。
  > ⚠️ **口令层那几个数在当天上午被整体作废又重新测过一遍。**
  > 凌晨那次测的是 `basic_auth`(未登录 → **401**);上午换成登录页之后,
  > 未登录变成 **302 跳 `/login`**。§3.1 现在给的是**换完之后重测的那一套**,
  > 令牌层(§3.2)那几个数没有重测,理由见那一节。
- **登录页 + 会话 Cookie 那道门的全套返回码**(2026-08-11 上午,提交 `ccb8d27` 之后,
  同样从外部机器打):未登录 302、登录页 200、错口令不发 Cookie、对口令发 Cookie、
  伪造会话全部被拒、连发错口令会 429。完整实测表在 §3.1。
- **域名模式下的自检命令跟裸 IP 模式不一样**:`curl http://<VPS_IP>/` 回 **308**,
  而不是门层该给的那个码(凌晨 basic_auth 时代是 401,现在是 302)——
  那个 308 来自 Caddy 专管 `:80` 的自动跳转,跟哪种门一点关系都没有(§3.1)。
- **1.9GB 内存 + 8GB swap 的机器上,加载 BGE-M3 做检索没被 OOM 杀掉**(§1.1,带采样数据)。
- **业务链路真的通**:排期条目直查 SQLite 确认落库、knowledge 如实说查不到而没编造(§步骤 12)。
- **持久化与自启**:**真按了两次 `reboot`**。第二次是在**五个容器都齐了之后**跑的:
  开机 **8 秒内五个容器全部 Up**、caddy 直接 healthy(证书从命名卷里读到,没重新向
  Let's Encrypt 申请)、两块 swap 按 fstab 自动挂回、ufw 仍 active;
  从外部复验 302/200/401/200/200 全对、证书 `notAfter` 未变,
  而且**重启前的 19 条历史对话一条没少**(§7)。
- **令牌真的烘进了浏览器包**(前端构建产物里精确匹配到那 64 位令牌)。
  手册原来把这条列成「已知目前接不上」,现在**已经接上了**(§步骤 11、§9)。

#### ⚠️ 仍然没验证的部分

- **Windows 侧**。那次全程从 macOS 开发机操作,Windows 上的 SSH / rsync / 换行符没碰过。
- **多并发**。整场并发是 **1**(一个人在操作)。§4.5 说的排队现象**没有被观察到,也没有被证伪**。
- **长期运行稳定性**。只是一次上线 + 几次提问,**没有连续跑过几天**。内存会不会缓慢涨、
  单进程会不会累积状态,都不知道。
- **证书自动续期**。首签成功 ≠ 续期成功,这两件事走的代码路径不一样。
  这张证书 **2026-11-08 到期**,到那之前谁也不知道。
- **真浏览器**。整场验收全是 `curl` / `openssl s_client` 打的,**一次都没开过浏览器**
  —— 上午换成自建登录页之后**仍然没有**。于是这两件仍然没有证据:
  ① 登录页和登录后的界面在真设备上长什么样;② 前端 JS 起来之后会不会报错。
  > **原来挂在这里的另外两条,依据变了(但没有变成「已验证」)。**
  > 原文是:③ 浏览器会不会把 **basic_auth** 的凭据自动带到 SSE 长连接上;
  > ④ `<img>` 取 `/artifacts/*` 时凭据带不带得上。
  > 这两条当年之所以是悬案,是因为 basic_auth 靠的是「浏览器记住口令并自动重发」——
  > **那是一个我们从没在真浏览器上验过的行为假设**。
  > 换成会话 Cookie 之后,**同源请求带 Cookie 是浏览器的规定动作**,
  > `<img src>` / `fetch` / `EventSource` 一律带上,不再依赖那个假设。
  > **依据从「假设」变成了「浏览器规范行为」,但没人在真浏览器上跑过 —— 别写成「已验证」。**
- **§3.2 C) 那条 `x-auth-scheme: langsmith` 后门检查**、**§3.3 限流与 cron 的所有 curl**、
  **§3.4 里扫 3000 端口那条** —— 那次都没跑,仍是按代码判据写的。
  (§3.4 的 **2024 端口那条跑了**:从外部机器 `curl -m 6 http://<VPS_IP>:2024/ok` 超时,
  `%{http_code}` 是 `000` —— 后端端口确实没对公网开。)
- ARM 架构、中国大陆 ICP 备案 —— 一如既往地没验(§1.1、§1.3)。

完整逐条见 §9。

### 0.1 这台 VPS 不是演示主路径

项目的 **D9 决策是「本地主跑 + 三层兜底」**。这份手册服务的是**「让外部的人自己点点看」**,
不是「演示当天靠它」。两个理由,任何一个都够:

- **视觉缓存跟着机器走。** 照片识别的结果缓存在 `data/cache/` 里,是**这台机器**的目录。
  在 VPS 上重新焐一轮就是再烧一次钱。

  > **⚠️ 这一条 2026-08-11 被实测修正了一半。**
  > 原来这儿写的是「VPS 上一张都不认,要么再烧一轮,要么把**几百 MB** 的 `data/cache` 同步上去」。
  > 实测:那次把开发机的 `data/cache` 一起 rsync 过去了,**只有 4.1MB**(不是几百 MB),
  > 而且**视觉缓存真的跨机命中**。判据:传同一张演示照片跑英雄链两次,
  > 缓存目录里 `kimi` 条目共 160 条、**当晚 02:00 之后新增 0 条** ——
  > 也就是说 kimi 视觉那一跳一次都没真调,贵的那半是免费的。
  >
  > **但便宜的那半每次都真花钱。** 同样两次跑,每次都**新增 6 条 `deepseek-v4-flash` 条目**,
  > 耗时 7 秒 / 11 秒(第二次反而更慢)—— **重复同一张照片的演示不会变便宜**。
  > 至于文本档为什么不命中,**没查**。
  > (一个没验证的猜测,别当结论:`ingest_uploads` 每次上传都用新的 `uuid4().hex` 登记照片,
  > 改写出来的 `(照片编号:<id>)` 每次都不一样,而那串进了文本档的输入。)
  >
  > 对**给外部的人测**这件事的直接含义:每个人每张照片大约 6 次文本调用是真账单,
  > 视觉那笔只在照片没见过时才付。
- **会场网络、跨网延迟、供应商临时限流**,任何一个抽风都能毁掉现场。

所以:**VPS 是加分项和第四层兜底,演示主路径仍然是本地那台。**
(出处:`docs/W4_跨平台运行方案.md` §7、`TODOS.md` TODO-1 的补充第 4 条。)

### 0.2 上公网之前必须理解的一条红线

> `:2024` 是**零鉴权**的 Agent 执行端点,而两把 API Key(DeepSeek / Moonshot)就在容器里。
> `docker-compose.yml` 钉死 `127.0.0.1` 正是为了挡这个。**公网裸奔 = 账单敞开给人刷。**

这不是修辞。LangGraph 服务的 `POST /runs/stream` 谁都能打,一次请求就是一次真实模型调用;
`POST /threads/search` 能读走别人刚传上去的工地照片和图纸。
**别用「没人知道我的 IP」当防线** —— VPS 的 IP 一上线就会被全网扫描器在几分钟内摸到。

### 0.3 四层防线,各挡各的失效模式

> **⚠️ 2026-08-11 上午,① 这一层整个换掉了。**
> ~~① Caddy:全站访问口令(`basic_auth`)~~ → **登录页 + 会话 Cookie**。
> 原因不是安全性:`basic_auth` 那道门是能用的,但它的登录界面是**浏览器自己画的**那个灰框,
> **不可美化** —— 只要服务端回 `WWW-Authenticate: Basic`,它就一定会弹,
> 没有产品名、没有一句人话、没法告诉测试的人「口令找发你链接的人要」。
> 唯一的办法是**根本不发那个头**,自己做登录页。
> 层数没变,还是四层;换的只是 ① 的实现。

```
公网
  │
  │  ① 登录页 + 会话 Cookie                       ← 给「人」的门,测试者只需要这一个口令
  ▼
Caddy ── 整份编排里唯一 publish 端口的服务(80 / 443)
  │
  ├── /login, /logout ──▶ login:8790   ← **公开**,不然登录页自己也要先登录 = 死循环
  │
  └── 其余全部
        └ forward_auth ──▶ login:8790 /_auth/verify
             204        → 放行,继续往下走业务路由
             无效       → 网页请求 302 去 /login;/api/* 回 401 JSON
                  │
                  ├── /       ──▶ frontend:3000  ┐  两个都**不写 ports**,
                  └── /api/*  ──▶ backend:2024   ┘  只在 docker 内网互通   ← ④ 端口不暴露
                                     │
                                     │  ② backend/auth.py 校验 X-Api-Key   ← 给「程序」的门(纵深)
                                     │  ③ 令牌桶限流:只卡「创建 run」这个动作
                                     ▼
                                 Agent 真跑
```

> **`/api/*` 为什么不跟着 302,而是回 401 JSON:** 打 `/api/*` 的是前端的 `fetch` / SSE,
> 给它 302 到一张 HTML 登录页,它会把那坨 HTML 当 JSON 解析,
> 然后报出一个**和「登录过期」毫无关系**的错。(**源文件核对** `scripts/serve_login.py` 的 `_handle_verify`。)

**为什么口令和令牌不是重复劳动 —— 它们防的是不同的失效模式:**

| 防线 | 挡住的是 | 它自己失效的方式 |
|---|---|---|
| ① 登录页 + 会话 Cookie | 陌生人根本进不来 | Caddy 配置写错、某条路由漏配(**§0.5 就是这么裸奔的**) |
| ② X-Api-Key | **绕过 Caddy 直接打内网端口的人**;将来有人为了排障给 backend 加回 `ports` | 令牌泄漏(它在浏览器包里,能拿到包的人已经过了①) |
| ③ 限流 | **已经进来的人**反复刷 run 烧钱 | 参数给太松;多副本部署(见 §6.2) |
| ④ 端口只走内网 | 直接 `curl http://<你的IP>:2024` | 有人手贱把 `ports` 改成 `0.0.0.0` |

①塌了②还在,②塌了④还在,而③管的是「合法用户也可能把你刷穷」这件①②④都管不了的事。

> 令牌(`X-Api-Key`)在**构建期**烤进前端浏览器包,测试者不用手动粘。
> 它躺在包里是**可见**的 —— 但**能拿到那个包的人已经过了 Caddy 口令**,所以可接受。
> 它防的从来不是「过了第一道门的人」。别把它当机密管理。

### 0.4 动手前先核对:这套安全件在不在你 clone 到的提交里

**本手册和它依赖的代码是同一周落地的。** 先 clone 完(§2 步骤 3)再回来跑这段:

```bash
cd ~/gongyoutong
ls backend/auth.py                       # ① 令牌鉴权 + 令牌桶限流
grep -n '"auth"' backend/langgraph.json  # ② 声明:应看到 "auth": {"path": "./auth.py:auth"}
ls Caddyfile                             # ③ 反代 + 会话门(forward_auth)
ls docker-compose.vps.yml                # ④ VPS 覆盖件
ls .env.vps.example                      # ⑤ VPS 专用环境变量样例
grep -n 'access_token\|rate_limit_burst' backend/src/gyt/config.py   # ⑥ 闸门配置字段
ls scripts/serve_login.py scripts/login-page.html scripts/make_login_hash.py   # ⑦ 登录服务三件
```

**⑦ 是 2026-08-11 上午新增的**(提交 `ccb8d27`)。缺了它们说明你 clone 到的是
`basic_auth` 时代的提交:那份代码里 `Caddyfile` 用的是 `basic_auth`、
`.env.vps.example` 里的变量还叫 `GYT_BASIC_AUTH_USER` / `GYT_BASIC_AUTH_HASH`。
**那份也能上线**(门是锁着的),只是登录界面是浏览器那个不可美化的灰框,
而本手册下文写的是登录页那一套 —— **两边对不上时,以你手上那份代码为准,别照着手册硬改**。

**七条里但凡缺一条,就停下。** 说明你 clone 到的是这套安全件落地之前的提交 ——
那份代码**不能上公网**(理由见 §0.2)。去找项目负责人要正确的分支或 tag,别自己动手补。

⚠️ **环境变量的准确名字,一律以 `.env.vps.example` 为准。**
本手册下文写出来的名字取自 `Caddyfile` 和 `backend/src/gyt/config.py`(**源文件核对**),
但样例文件才是这个仓库约定的「那一份真相」,对不上就以它为准。

### 0.5 ⚠️⚠️ 这一轮最贵的一课:站点裸奔过一次,而 `caddy validate` 说 Valid

**2026-08-11 上午换登录页的过程中,把站点弄成了完全敞开的状态。**
写在这里不是为了忏悔,是因为**它属于「一切看起来都正常」那一类故障**,
而这套东西一旦栽进去,代价是把 API Key 敞开给全网。

#### 怎么发生的

第一版只把「登录 handle + `forward_auth`」包进了 `route`,**业务的 `handle` / `handle_path` 留在外面**。
看起来完全合理 —— 直到发现 **Caddy 有一张固定的指令顺序表,而 `handle` / `handle_path` 在那张表里
排在 `route` 之前**。也就是说,你写在文件里的先后顺序**不算数**。实际编译出来是:

```
request_body → handle_path /artifacts/* → handle_path /api/*
→ handle{} 兜底(把所有请求吃光)→ route{登录 + forward_auth}(永远到不了)
```

#### 表现有多危险

- 未登录访问首页 **直接 200**,聊天界面完整打开;
- `/login` 是**前端的 404**(因为请求根本没到 login 服务);
- **没有任何报错** —— 日志里也看不出异常;
- **`caddy validate` 照样说 `Valid configuration`**;
- 而访问令牌 `X-Api-Key` **就烘在前端 JS 包里**(§0.3 末尾那条设计,前提是「能拿到包的人已经过了第一道门」)
  —— 第一道门没了,那个前提就塌了,**等于把令牌连同两把模型 Key 一起敞开**。

#### 唯一可靠的发现手段:`caddy adapt`,不是 `caddy validate`

`validate` 只回答「这份配置**语法**合不合法」,它**不回答「编译出来的路由是什么顺序」**。
要看顺序,只能让 Caddy 把路由树打出来:

```bash
cd ~/gongyoutong
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e GYT_SITE_ADDRESS=<你的域名> caddy:2-alpine \
  caddy adapt --config /etc/caddy/Caddyfile
```

**看的是:`forward_auth`(输出里是 `authentication` / `forward_auth` 相关的 handler)
有没有排在所有业务 handler 之前。** 排在后面 = 那些路径全是敞开的。

#### 正解

**整个站点块都包进一个 `route`。** `route` 内部**按书写顺序执行**,
所以只要全在一个 `route` 里,顺序就由这个文件说了算,那张固定顺序表管不着。
这不是风格问题,是必须的。

> **规矩:谁要往那个 `route` 外面挪任何一条 `handle`,必须先跑上面那条 `adapt`,
> 确认 `forward_auth` 仍然排在所有业务 handle 之前。**
> `Caddyfile` 末尾「这个文件永远不许出现的东西」那一段已经把这条钉死了(**源文件核对**)。

> 顺带说清楚一件事:**§3.1 那几条 curl 能抓到这个问题**(未登录访问首页拿到 200 而不是 302)。
> 所以那一节不是走过场 —— 它就是为这种事存在的。**改完 `Caddyfile` 一定重跑一遍。**

---

## 1. 先决条件

### 1.1 机器规格

| 项 | 要求 | 为什么 |
|---|---|---|
| CPU / 内存 | **2C4G 起步** | BGE-M3(知识库用的本地 embedding 模型)推理**吃内存**。**4G 是下限,不是舒适区**,能上 8G 就上 8G |
| 磁盘 | **40GB 以上** | 后端镜像本身就很大 —— **实测**本机 `docker images` 里 `gyt-backend:dev` 占 **6.38GB**(2.2GB 的 BGE-M3 权重烤在镜像里)。加上构建缓存、前端镜像、`data/`,20GB 会很紧张。<br>**VPS 实测**:一块 **30GB** 的盘走完全流程,最终占用约 **20GB**(两个镜像 8.3GB + 系统),中途靠 `docker builder prune -af` 回收了 **9.34GB**。详见步骤 9 |
| 系统 | Ubuntu 22.04 / 24.04 LTS(x86_64) | 手册里的命令按 Debian 系写。**VPS 实测**跑通的那台是 **Debian 12 / x86_64**。ARM 机器**没试过**,BGE-M3 权重和 CPU 版 torch 在 ARM 上能不能装,**⚠️ 未在真机验证** |
| 网络 | 能出站访问 DeepSeek / Moonshot / HuggingFace / GitHub / npm | 构建期要下 2.2GB 权重和前端依赖,运行期要调两家模型 |

> **内存不够的典型症状不是报错,是「构建到一半 OOM 被杀」。**
> 尤其前端那步(Next.js 生产构建)在 4G 机器上有风险。
> 保险做法是先挂 swap 再构建(`docker-compose.vps.yml` 的收尾注释建议 **2G 起**,
> 理由是「BGE-M3 加载权重那一下是尖峰,有 swap 兜着就是慢几秒,没有就是 OOM」;
> 磁盘够的话给 4G 更稳)—— 命令见步骤 1。
> **2026-08-11 那台机器上 swap 确实挂着(原有 4GB + 新增 4GB = 8GB),整场没有 OOM**;
> 但**用的是不是步骤 1 那几条命令,没有记录**,所以那几条命令本身仍标 ⚠️ 未在真机验证。
>
> 另外注意 `docker-compose.vps.yml` **故意没有写 `deploy.resources.limits`**:
> 在一台 4G 的机器上给唯一一个吃内存的服务再加内存上限,只是把「慢」变成「被 kill」。
> 这台 VPS 上还跑别的东西的话再回来加,加之前先量一下:`docker stats gyt-backend-vps`。

#### ⚠️ 内存这一格,2026-08-11 那次上线拿到了真数据 —— 但结论比你想的窄

**先说会挡路的那件事:`scripts/preflight_vps.sh` 第 233-236 行的内存闸要求 ≥ 3500MB。**
那台机器 1966MB,**被判 FATAL、脚本直接 `exit 1`**(**源文件核对** + **VPS 实测**)。

**而它给的理由在那次上线里并不适用。** 脚本里那句话是:

> `内存只有 ${MEM}MB。BGE-M3 推理吃内存,**4G 是下限不是舒适区**,建库那一步会被 OOM 杀掉。`

那次**根本没在 VPS 上跑过 `ingest`** —— `data/chroma`(12MB)是从开发机整个搬过去的
(做法见步骤 10)。所以「建库那一步会被 OOM」这个**具体后果**没有被验证,也没有被证伪。

**真正被验证的是另一件事:查询时把 BGE-M3 加载进来那一下。** 每 5 秒采样一次(**VPS 实测**):

```
02:25:09   可用 745MB    swap 18MB    backend 容器 430MB     ← 加载前
02:25:23   可用 381MB    swap 18MB    backend 容器 846MB     ← 加载中
02:25:31   可用 454MB    swap 77MB    backend 容器 955MB     ← 峰值
```

**backend 峰值只到 955MB,不是 2.2GB。** 原因是权重从镜像里 **mmap** 进来,
算**文件页缓存**(可回收),不占匿名内存 —— 所以「权重 2.2GB ⇒ 至少吃 2.2GB 内存」这个
直觉推算是错的。

那台机器上 swap 是 **原有 4GB + 新增 4GB = 8GB**。
整场构建 + 查询下来,`dmesg | grep -ci "out of memory"` = **0**。

> ### ⚠️ 结论只能写到这里:**这台机器上跑通了。**
>
> **不许**据此改写成「1.9GB 够用」,更**不许**据此去下调 `preflight_vps.sh` 的内存闸。
> 理由:样本只有一次,而且**那次的并发是 1**。
> 一个人自己点,和三五个人同时提问,内存曲线不是一回事,而这条我们没测过。
>
> 想跑在 4G 以下的机器上,你要自己接受这个风险,并且**至少把 swap 挂足**
> —— 上面那份采样是在 8GB swap 之下取的,换成没有 swap 的机器,这份数据不适用。

### 1.2 要装的东西

只有两样:**Docker Engine + Docker Compose 插件**、**git**。
其它全在容器里 —— VPS 上不需要 Python、不需要 Node、不需要 uv。

上机第一件事跑一下 `docker compose version`,**别用太老的版本**。
(本手册所有 compose 相关的行为都是在**本机 v5.1.3** 上实测的;更老的版本我们没核实。)

### 1.3 域名:两种玩法,靠一个变量切换

仓库的 `Caddyfile` 把站点地址做成了变量 `GYT_SITE_ADDRESS`(**源文件核对**),两种取值:

| 取值 | 效果 | 代价 |
|---|---|---|
| `gyt.example.com`(**推荐**) | Caddy **自动申请 Let's Encrypt 证书、自动续期、80 自动跳 443** | 需要一个 A 记录已经指到这台 VPS 的域名;80 和 443 都要能从公网进来(证书校验走 80) |
| `:80` | 没有域名,直接用 VPS 的 IP 访问,**纯 HTTP** | ⚠️ **HTTP 下口令是明文过网的**,同一个 Wi-Fi 下抓包就能看到。临时测试可以接受,**别拿它当长期方案,更别用你在别处用过的密码** |

> ⚠️ **合规提醒,不是结论。**
> 如果机器买在**中国大陆**的云服务商(阿里云 / 腾讯云 / 华为云等),
> 用域名对外提供 HTTP 服务通常涉及 **ICP 备案**流程,周期以周为单位;
> 香港 / 新加坡 / 日本 / 海外的机器一般不涉及这套流程。
>
> **我们没有替你核实任何一条现行监管细节,政策也会变。**
> 请在买机器**之前**直接去你的服务商控制台或客服那里确认清楚 ——
> 这件事卡住的话,后面所有步骤都白做。
> 只想快速给几个人试的话,买在香港/海外能省掉这个不确定性。

#### ⚠️⚠️ DNS 托管在 Cloudflare 的话:**必须用灰云(DNS only),不能开橙云代理**

**本节是 2026-08-11 那次上线新增的 —— 手册原来对 Cloudflare 一个字都没提。**
而域名放在 Cloudflare 上是很常见的做法,那个云朵图标默认就是**橙色**(已代理)。

在 Cloudflare 的 DNS 记录列表里,把那条 A 记录的云朵点成**灰色**(Proxy status = **DNS only**)。
**橙云(Proxied)会从三个方向同时把这套东西弄坏:**

1. **证书死锁。** 橙云下 Cloudflare 在它自己的边缘卸载 TLS,回源走 443;
   而我们这边的 Caddy 是**先要拿到证书才有 443 可听**。两边互相等,谁也起不来。
2. **SSE 会被缓冲。** 整个应用靠**流式推送**把 Agent 的中间过程吐给页面
   (`/api/runs/stream`)。Cloudflare 免费版代理对 SSE 有**缓冲**,还有 **100 秒空闲超时** ——
   表现不是报错,是「转圈很久,然后一次性蹦出全部内容」或者干脆断掉。
3. **`scripts/preflight_vps.sh` 的 DNS 检查会判红。** 那一段(第 ③ 组)拿 `dig` 的结果
   跟本机公网 IP 比对,橙云下 `dig` 返回的是 Cloudflare 的 IP,两边对不上 →
   `bad "…指到别的机器上了。"`。
   **这是该脚本的设计,不是 bug** —— 它挡的正是「证书一定申请不下来」这件事(**源文件核对**)。

第 1、2 条是**原理说明**(Cloudflare 的行为,不是我们测出来的);第 3 条是**读脚本源码**得出的。
**那次上线实际用的就是灰云,证书一次签成**(**VPS 实测**)。

> 顺带说:灰云意味着**你的 VPS 真实 IP 是公开的**,没有 CDN 挡在前面。
> 这套编排本来就假定了这一点 —— §0.3 的四层防线全在你这台机器上,
> 不依赖任何 CDN。真实 IP 暴露不改变风险模型,但它确实意味着**扫描器几分钟内就能摸到你**(§0.2)。

### 1.4 两把 API Key

- DeepSeek(文本档:路由 / 任务拆解 / 报告 / 知识综合):<https://platform.deepseek.com/api_keys>
- Moonshot / Kimi(视觉档:照片识别 / 图纸问答):<https://platform.moonshot.ai/console/api-keys>
  **注意是国际站 `.ai`,不是 `.cn`,两边的 Key 不通用。**

(两个地址抄自 `.env.example`,**源文件核对**。)

**上线前先去这两个控制台看一眼有没有「消费上限 / 额度告警」可以设** ——
那是比我们自己写的限流更硬的一道闸。**⚠️ 这两家控制台具体有没有这个功能,我们没核实**,
自己去看,有就设上。

---

## 2. 一步步上线

> 下面每一段都可以整段复制粘贴。以 `root` 身份在 VPS 上执行。
> **凡是标 ⚠️ 的地方,慢一点、看一眼输出再往下走。**

### 步骤 1 · 系统基础与防火墙

```bash
apt-get update && apt-get install -y git curl ca-certificates ufw

# 只开三个口。2024(后端)和 3000(前端)绝对不要开 —— 它们只走 docker 内网。
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp      # SSH,别把自己关在门外
ufw allow 80/tcp      # HTTP:签证书要用(ACME HTTP-01),同时把访客跳到 443
ufw allow 443/tcp     # HTTPS
ufw --force enable
ufw status verbose
```

**4G 机器建议先挂 swap 再往下走**(⚠️ 未在真机验证,但这是标准做法):

```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h    # Swap 那一行应该有 4G
```

（没有 swap 时的典型症状:建库或首次检索的那一下容器被 OOM killer 干掉,
`docker compose ps` 显示 `Exited (137)`,而日志里什么都看不出来。）

> **swap 这一步别省。** 2026-08-11 那台 1.9GB 内存的机器**是带着 8GB swap 跑通的**
> (原有 4GB + 新增 4GB),全场 `dmesg | grep -ci "out of memory"` = **0**(**VPS 实测**)。
> §1.1 那份内存采样也是在这个前提下取的 —— **换成没有 swap 的机器,那份数据不适用。**
> (那次具体用的是不是上面这几条命令,没有记录,所以命令本身仍是 ⚠️ 未在真机验证。)

### 步骤 2 · 装 Docker

用 Docker 官方安装脚本(⚠️ 未在真机验证,但这是 Docker 官方给的路径):

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

docker version
docker compose version
```

### 步骤 3 · 拉代码

仓库是**私有**的(`git@github.com:lawrenceli0228/gongyoutong.git`),
所以要么给这台机器配一把**只读 deploy key**,要么用带权限的 HTTPS token。
**别把你个人账号的 SSH 私钥丢到 VPS 上。**

```bash
# 推荐:在 VPS 上生成一把新钥匙,公钥加到 GitHub 仓库的 Deploy keys(只勾读)
ssh-keygen -t ed25519 -N '' -f ~/.ssh/gyt_deploy
cat ~/.ssh/gyt_deploy.pub          # 复制这一行,粘到 GitHub → Settings → Deploy keys
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/gyt_deploy
  IdentitiesOnly yes
EOF

cd ~
git clone git@github.com:lawrenceli0228/gongyoutong.git
cd ~/gongyoutong
git log --oneline -3               # 记下这个提交号,出问题时要报给项目组
```

**拉完立刻回去跑 §0.4 那六条核对命令。** 缺件就停,别往下走。

### 步骤 4 · ⚠️ 确认演示资产真的拉下来了

`data/demo/` 里的照片、规范 PDF、DXF 图纸**是进 git 的**(`.gitignore` 里 `/data/*`
之后紧跟着一行 `!/data/demo/`,**源文件核对**),而且**没有用 git-lfs**
(`.gitattributes` 里只声明了 `binary`),所以普通 `git clone` 就该全带下来。

但值得亲眼确认一次 —— 缺了它们,knowledge 会建出个空库,而**空库的表现和「规范里查不到」
一模一样**,极难发现。

```bash
cd ~/gongyoutong
ls data/demo/docs/*.pdf                 # 至少 1 份规范 PDF(GB 50016 建筑设计防火规范)
ls data/demo/photos/*.jpg | wc -l       # 应为 30
ls data/demo/drawings/*.dxf             # plan_utf8.dxf / plan_gbk.dxf / broken.dxf
du -sh data/demo                        # 本机实测约 26MB(docs 17M + photos 8.8M + drawings 64K)
```

**PDF 那一行是空的 = 后面的知识库这一整块功能作废。** 别继续,先解决。

### 步骤 5 · 建数据目录并交出所有权

```bash
cd ~/gongyoutong
mkdir -p data

# 容器里跑的是 uid 10001 的非 root 用户(backend/Dockerfile 里 useradd --uid 10001)。
# Linux 上不 chown,容器一访问 data/ 就 PermissionError ——
# 而症状是「服务显示健康,但什么都干不了」,是最难查的那一类。
chown -R 10001:10001 data
ls -ld data
```

⚠️ 这条 `chown -R` 会把 `data/demo/` 里那些 git 管着的资产也改成 10001 所有。
**这没问题**(容器只读它们,git 也不跟踪属主),但你以后 `git pull` 更新演示资产时得是 root。

> ### ⚠️ 你要是用 `rsync` 从开发机搬代码过来的(而不是 §步骤 3 的 `git clone`),这一步更要命
>
> **VPS 实测(2026-08-11)**:`rsync -a` 会**保留源端的 uid**。从 macOS 开发机搬过去,
> `data/` 到了 VPS 上属主是 **501:staff** —— 那台 Linux 上根本没有这个 uid,
> 而容器里跑的是 **uid 10001**(`backend/Dockerfile:288` 的 `useradd --uid 10001`,**源文件核对**)。
>
> **后果:台账 SQLite、产物注册表、LLM 缓存全都写不进去。**
> 而且照例是那种最难查的症状 —— 容器 healthy、页面能开、问答有回复,只是什么都没落盘。
>
> 修法两条,都要做:
>
> ```bash
> cd ~/gongyoutong
> mkdir -p data/artifacts data/uploads     # rsync 排除掉这两个目录的话,它们压根不存在
> chown -R 10001:10001 data
> ```
>
> **这条这次是被 `scripts/preflight_vps.sh` 第 ④ 组拦下来的** —— 那一组里有一条
> 「`data/` 属主是 10001」的硬检查,不是 10001 就判 FATAL 并打出 `sudo chown -R 10001:10001 data`。
> (它在 **非 Linux** 上会跳过这条,因为 Docker Desktop 自己做 uid 映射 —— 所以在你的 mac 上跑
> 是绿的,搬到 VPS 上才红。见步骤 8.5。)

### 步骤 6 · 生成两个秘密

#### 秘密一:给人的访问口令(登录页校验的那个)

```bash
cd ~/gongyoutong
python scripts/make_login_hash.py --random     # 自动生成一个 20 位口令,并算好哈希
python scripts/make_login_hash.py              # 或者自己想一个(交互输入,不回显)
```

它会打出**已经可以直接粘进 `.env` 的整行**:

```
GYT_LOGIN_HASH=scrypt$$16384$$8$$1$$<salt hex>$$<hash hex>
```

**两件事这个脚本替你做了,别自己再动手:**

- **`$` 已经加倍好了**(`$$`)。粘完别再改 —— 理由见下面那个坑。
- **口令是从去掉 `0/O/o/1/l/I` 的字母表里随机取的 20 位**(**源文件核对** `scripts/make_login_hash.py`)。
  这个口令要发给工地上的人在手机上手打,少一个「这是零还是欧」的来回。

> ### ⚠️ 2026-08-11 上午:这一步整个换过了
>
> **原来写的是:**
>
> ```bash
> docker run --rm caddy:2-alpine caddy hash-password \
>   --algorithm bcrypt --bcrypt-cost 12 --plaintext '<你想好的口令>'
> ```
>
> ~~输出形如 `$2a$14$JMw9EJgAEKPOoH.PJQ8Ljek.euyS7iQG4ls/c3mRxtSXyxKQ17g8m`,`$2a$` 开头说明它是 **bcrypt**;~~
> ~~`Caddyfile` 里那行写的是 `basic_auth bcrypt { ... }`,第二个参数是算法声明,Caddy 按它去校验。~~
>
> **为什么不成立了:** 口令校验从 Caddy 搬到了 `scripts/serve_login.py`(一个跑
> `python:3.12-slim`、**零 pip 安装**的服务),而 **`hashlib.scrypt` 在 Python 标准库里,bcrypt 不在**。
> 为了一个哈希函数多背一个 pip 包和一条构建期网络依赖不值,
> 而且 scrypt 是**内存硬**的,抗 GPU 爆破比 bcrypt 更好。
> (**源文件核对** `scripts/serve_login.py` 顶部「为什么口令哈希从 bcrypt 换成 scrypt」。)
>
> **口令本身不用改。** 拿旧口令重新跑一次 `make_login_hash.py` 就行。
>
> ~~**`--bcrypt-cost 12` 不能省**:`caddy hash-password` 不指定时默认 cost 14,~~
> ~~2026-08-11 在真 Caddyfile 上横向实测 cost 12 冷校验 0.470s、cost 14 是 1.268s。~~
> **那条取舍本身没有消失,只是换了个数字:** 单次校验耗时既是**离线爆破的成本**,
> 也是**登录接口的 DoS 杠杆**(登录接口是公开的,任何陌生请求都能触发一次)。
> **VPS 实测:scrypt 单次 46ms。** 而且这一版还多了一道 basic_auth 没有的保护 ——
> 登录接口自带令牌桶限流,见 §3.1 末尾。
> `scripts/serve_login.py` 的注释把这条钉死了:**不许为了「快一点」调低 n,也不许调太高**。

> ### ⚠️⚠️ 这个坑没有消失,只是搬了家:哈希里的 `$`
>
> scrypt 哈希同样用 `$` 分段(`scrypt$n$r$p$salt$dk`),
> 而 **Docker Compose 会把它读到的所有 env 文件里的 `$` 当变量展开**。
>
> **本机实测**(bcrypt 时代造的最小复现,机制完全一样;在容器里 `env | grep ^HASH=` 打出来的真实值):
>
> | env 文件里写的 | 容器里实际拿到的 |
> |---|---|
> | `HASH=$2a$14$JMw9EJgAEKPOoH.PJQ8Ljek…` | `HASH=$2a$14.PJQ8Ljek…` ← **`$JMw9EJgAEKPOoH` 被整段吃掉** |
> | `HASH=$$2a$$14$$JMw9EJgAEKPOoH.PJQ8Ljek…` | `HASH=$2a$14$JMw9EJgAEKPOoH.PJQ8Ljek…` ← **正确** |
>
> 被吃掉的哈希**不报任何错**,login 服务照常起来,只是**你输什么口令都进不去** ——
> 而你会去怀疑口令打错了、怀疑配置、怀疑浏览器缓存,方向全错。
>
> **规矩:写进 env 文件时,把每一个 `$` 写成 `$$`。**
> `make_login_hash.py` 已经替你加好了,**直接粘、别再改**。
> `scripts/preflight_vps.sh` 还加了两条硬检查兜底:`$` 必须成对、且必须以 `scrypt$` 开头
> (**源文件核对**;后一条挡的是「还留着旧 bcrypt 串」这种情况,那同样是不报错、口令永远不对)。

#### 秘密二:给程序的 API 令牌(`X-Api-Key`)

```bash
openssl rand -hex 32
```

用十六进制不是随手选的:**它里面不会出现 `$`**
(实测 `openssl rand -hex 32 | grep -c '\$'` 回 `0`),天然绕开上面那个坑。

后端拿它做的是**恒定时间比对**(`hmac.compare_digest`,**源文件核对** `backend/auth.py`),
所以别用短口令凑合 —— 64 位随机十六进制就很好。

### 步骤 7 · 写环境变量

> ### ⚠️ 文件名只有一个:**`.env`**。别自作聪明起名叫 `.env.vps`
>
> ```bash
> cd ~/gongyoutong
> cp .env.vps.example .env       # ← 必须复制成 .env,不是 .env.vps
> nano .env
> ```
>
> **为什么不能用 `docker compose --env-file .env.vps ...` 那种写法**
> (**源文件核对**:`docker-compose.vps.yml` 末尾「关于 --env-file」):
> `--env-file` 只影响 compose 自己做变量插值,**管不到**基础档里 backend 的
> `env_file: .env`。而那条是 `required: false` —— 文件不存在时**不报错**。
> 结果是后端启动时一把模型 Key 都没有,报错发生在图加载期,
> 现象是**容器反复重启**,而根因(你把变量放在另一个文件里了)一点提示都没有。

按作用对照(**变量名以 `.env.vps.example` 为准**;下表的名字取自 `docker-compose.vps.yml`、
`Caddyfile` 与 `config.py`,**源文件核对**):

| 变量 | 填什么 | 没填会怎样 |
|---|---|---|
| `GYT_DEEPSEEK_API_KEY` | DeepSeek Key | **留空**→ 容器起来又退出(建图期 `llm.py:312` 抛);**填了占位符** → 容器正常起来,然后每次对话都在第一次模型调用时报错 |
| `GYT_MOONSHOT_API_KEY` | Moonshot Key | 同上 |
| `GYT_ACCESS_TOKEN` | 步骤 6 秘密二 | **`up` 当场失败并打中文** —— 这是 fail-closed,故意的 |
| `GYT_LOGIN_HASH` | 步骤 6 秘密一那一行(脚本已把 `$` 加倍) | **`up` 当场失败并打中文**。⚠️ 见上面那个坑 |
| `GYT_PUBLIC_ORIGIN` | 对外完整地址,如 `https://gyt.example.com` 或 `http://203.0.113.10` | **`up` 当场失败并打中文** |
| `GYT_SITE_ADDRESS` | `你的域名` 或 `:80` | 有默认值 `:80`(纯 HTTP),见 §1.3 |
| `TZ` | 一般不动 | 有默认值 `Asia/Shanghai` |

> **⚠️ 2026-08-11 上午起,这张表里少了一行、改了一行:**
> ~~`GYT_BASIC_AUTH_USER`(用户名,默认 `gyt`)~~ —— **没有用户名这个概念了**。
> 登录页只要口令:内测环境,一个口令发给几个人,再让人记一个 `gyt` 用户名纯属增加摩擦。
> ~~`GYT_BASIC_AUTH_HASH`~~ → **`GYT_LOGIN_HASH`**(bcrypt → scrypt,理由见步骤 6)。
> 手上还是旧变量名的话,`up` 会在 login 服务那一步当场失败并打中文,不会静默放行。
| `GYT_RATE_LIMIT_BURST` | 一般不动 | 代码默认 `20`,见 §6.2 |
| `GYT_RATE_LIMIT_PER_MINUTE` | 一般不动 | 代码默认 `20`,见 §6.2 |

> **前三个「当场失败」是刻意设计的一道闸**(compose 里的 `${VAR:?中文提示}` 写法,
> **源文件核对** `docker-compose.vps.yml`)。**别为了图省事给它们加默认值** ——
> 那等于把「忘了设」从「起不来」变成「起来了但没锁门」,而后者一声不吭。

> ### ⚠️ 为什么 `TZ` 那一行不能省
>
> VPS 默认基本都是 UTC,而本项目的排期台账是按「今天 / 明天 / 本周五」算日期的。
> 差 8 小时的直接后果:**北京时间晚上 8 点之后建的任务,日期全部算成前一天** ——
> 界面不报错,只是日子悄悄不对,而这种错最难被测试的人描述清楚。
> `docker-compose.vps.yml` 已经给了默认值 `Asia/Shanghai`,别把它删了。

> ### ⚠️ `GYT_ACCESS_TOKEN` 千万别同步回你**本机**的 `.env`
>
> `backend/langgraph.json` 里写着 `"env": "../.env"` —— langgraph-cli 启动时会把
> **仓库根 `.env`** 整份灌进环境变量。所以只要本机 `.env` 里有 `GYT_ACCESS_TOKEN`,
> 本机 `make dev` 也会跟着强制鉴权,而 `backend/scripts/live_acceptance.py`
> (25 条断言的真机验收)**用的是裸 urllib、不发 `X-Api-Key`** —— 会当场 401 全红。
>
> **而那种红最坏:红的是脚本不是系统,它会训练人「这几条本来就红,跳过」。**
> (**源文件核对**:`backend/auth.py` 模块文档字符串里专门写了这一条。)
>
> VPS 上没有这个问题(那台机器不跑 `make dev`、不跑验收脚本),所以 VPS 上它就该在 `.env` 里。
> 但你在两台机器间同步配置时,**这一行只能留在 VPS 上**。

**为什么两把模型 Key 空着就起不来:** `graph.py` 是**模块级建图**,import 的那一刻就要 Key。
缺 Key 的现象是容器 `Application startup failed` 然后退出(不是「跑起来但答不了」)。
`docker-compose.yml` 里 `restart: on-failure:3` 会让它试三次就停住,方便你看见。

**⚠️ 限流参数一律从 `backend/src/gyt/config.py` 的 `get_settings()` 走。**
这个仓库的铁规矩是「所有常量的唯一入口是 `get_settings()`」——
想调松/调紧就改环境变量,**不要去业务代码里找数字改**。

### 步骤 8 · 先验一遍 Caddy 配置语法(30 秒,值回票价)

`Caddyfile` 写坏了 = Caddy 起不来 = **整个站点连不上**。上线前先单独验:

```bash
cd ~/gongyoutong
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e GYT_SITE_ADDRESS=":80" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

(这条命令抄自 `Caddyfile` 自己的注释,**源文件核对**。`GYT_SITE_ADDRESS` 必须给,
否则 `{$VAR}` 会替换成空串、站点地址为空直接报错。)

> **⚠️ 2026-08-11 上午起,这条命令少了两个环境变量。**
> 原来还要带 `-e GYT_BASIC_AUTH_USER=gyt` 和一串示例 bcrypt 哈希;
> 现在**口令校验搬到了 login 服务**,`Caddyfile` 里一个口令相关的变量都没有了。
> 多带无害,但别以为不带就验不了。

> ### ⚠️⚠️ `validate` 通过 ≠ 门是锁着的。改过 `Caddyfile` 的话,这一步不够
>
> **`caddy validate` 只回答语法。** 2026-08-11 那次把站点弄裸奔时,
> `validate` 全程说 `Valid` —— 而实际编译出来的路由里,`forward_auth` 排在兜底 `handle` 后面,
> 整站敞开。**完整经过和判据在 §0.5,动 `Caddyfile` 之前先读那一节。**
>
> 判顺序只能用 `adapt`:
>
> ```bash
> docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
>   -e GYT_SITE_ADDRESS=<你的域名> caddy:2-alpine \
>   caddy adapt --config /etc/caddy/Caddyfile
> ```
>
> 没动过 `Caddyfile` 的话,`validate` 够用;**只要动过一个字,就跑一遍 `adapt`,
> 而且上线后一定要再跑 §3.1 那几条 curl。**

### 步骤 8.5 · 跑一遍上线前自检脚本

**仓库里有 `scripts/preflight_vps.sh`,它会把上面这些步骤里最容易漏的检查一次性做完。**
只读:不改文件、不起容器、不发网络请求(除了解析域名)。退出码 0 = 可以 `up`,1 = 有致命项。

```bash
cd ~/gongyoutong
bash scripts/preflight_vps.sh
```

它查的六组(**源文件核对** `scripts/preflight_vps.sh`):

| 组 | 查什么 | 漏了会怎样 |
|---|---|---|
| ① | `.env` 存在、六个变量非占位符、令牌 ≥24 位、口令哈希里的 `$` 已加倍、**哈希以 `scrypt$` 开头** | 步骤 6/7 的两个坑 |
| ② | `GYT_SITE_ADDRESS` 与 `GYT_PUBLIC_ORIGIN` 描述**同一个入口** | 差一个字 → 浏览器判跨源 → 界面报「连不上服务器」,方向全错 |
| ③ | 域名模式下 `dig` 结果与本机公网 IP 一致 | 证书一定申请不下来,还可能撞 Let's Encrypt 频率限制 |
| ④ | :80/:443 空闲(**会先认自家 `gyt-caddy`** —— 站点已在跑时不误报)、**`data/` 属主是 10001**、`frontend/` 已生成且 `frontend/Dockerfile` **两条 ARG 都在**(`NEXT_PUBLIC_API_KEY` 与 `NEXT_PUBLIC_ARTIFACT_BASE`)、演示资产在 | 见步骤 5、步骤 11、§4.1 |
| ⑤ | `Caddyfile` 语法、compose 合并档能解析、`published:` 端口不超过 caddy 应有的数量(**应为 3**:80 / 443tcp / 443udp。多一条就说明有服务偷偷加了 `ports` —— artifacts 服务的 `0.0.0.0` 绑定安全性就押在这条上,见 §4.1) | 见步骤 8、§3.4 |
| ⑥ | 内存 ≥3500MB、可用磁盘 ≥10000MB | 见 §1.1 |

**这次上线真跑过它,`data/` 属主那条就是被它拦下来的**(见步骤 5 的 rsync 警告,**VPS 实测**)。

> **2026-08-11 上午跟着换登录页动过两处**(**源文件核对** `scripts/preflight_vps.sh`):
> ① 组读的变量从 `GYT_BASIC_AUTH_HASH` 换成 **`GYT_LOGIN_HASH`**,并**新增一条
> 「必须以 `scrypt$` 开头」**;⑤ 组那条 `caddy validate` 不再需要传口令相关的环境变量。
> **⑤ 组数的发布端口总数仍然是 3** —— login 服务和 artifacts 一样**一个 `ports` 都没有**,
> 所以这个数没变。哪天它变成 4,就是有人给某个内网服务加了 `ports`。

> ### ⚠️ 两件跑之前要知道的事,否则你会被它自己的红吓到
>
> - **第 ④ 组会检查 `frontend/`,而前端要到步骤 11 才生成。** 所以在这个位置跑,
>   「`frontend/` 还没生成」那条**必然红**,这是预期的。
>   实用的做法是**跑两次**:这里跑一次(前端那两条先忽略),步骤 11 生成前端之后再跑一次,
>   拿到全绿再 `up`。
> - **第 ⑥ 组的内存闸要求 ≥3500MB,不到就是 FATAL + `exit 1`。**
>   2026-08-11 那台 1966MB 的机器**就是被它判红的**,而最后整套跑通了(理由与边界见 §1.1)。
>   **看到这条红,先去读 §1.1 那一整段再决定要不要往下走** —— 别直接去改脚本里的阈值。

### 步骤 9 · 构建并起后端

```bash
cd ~/gongyoutong

# 先只起后端(显式点名 backend)。这样出问题时排查面小一半。
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build backend
```

> 顺手做一次**零成本的端口自检** —— 在真起之前就能看出红线在不在:
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.vps.yml config | grep -n -A3 'ports:'
> ```
>
> **只有 `caddy` 那一组该出现端口(80 / 443 / 443udp)。**
> 只要 backend 或 frontend 也带着 `ports`,就是有人把 VPS 档里的 `!reset []` 删了 ——
> 停下,回 §3.4 看后果。(这条验证命令抄自 `docker-compose.vps.yml` 自己的注释。)

⚠️ **第一次构建很久。** 镜像要下 2.2GB 的 BGE-M3 权重再烤进去。
仓库里对冷启动构建的记载是 **10~30 分钟**(`Makefile` 的 `e2e` 目标注释,**源文件核对**)。

**这里以前写着「本手册没在 VPS 上计时过,⚠️ 未在真机验证」—— 现在有数了。**
2026-08-11 那台 2 vCPU / 1.9GB / 30GB 的 Debian 12,**单机单次**实测(**VPS 实测**):

| 项 | 实测值 |
|---|---|
| backend 镜像 | **6.37GB**,构建约 **6 分半**(含从 huggingface.co 下 `BAAI/bge-m3` 权重) |
| frontend 镜像 | **1.93GB**,构建约 **3 分半**,其中 `next build` 本身 **75.9 秒** |
| 构建缓存回收 | 构建完 `docker builder prune -af` 回收了 **9.34GB** |
| 磁盘终态 | 30GB 的盘最终占用约 **20GB**(两个镜像 8.3GB + 系统) |

> ⚠️ **这是一台机器、一次构建的数字,只用来估算,不是承诺。**
> 下权重那段完全取决于你到 huggingface.co 的带宽,换台机器差几倍都正常。
>
> 顺带回答手册里悬了很久的一个问题:**「4G 内存能不能扛住前端 `next build` 而不 OOM」**
> ——原文把它标成「**这是我最不放心的一处**」。那次是在 **1.9GB 内存 + 8GB swap** 上跑的,
> `next build` 75.9 秒完成,整场 `dmesg | grep -ci "out of memory"` = **0**。
> **注意这解答的是「1.9GB + 8GB swap」这一种配置**,不是「4G 内存无 swap」那一种 ——
> 后者仍然没验过。

**磁盘紧的话,构建完顺手回收一次构建缓存:**

```bash
docker builder prune -af      # 这次回收了 9.34GB
df -h                         # 看一眼还剩多少
```

盯着日志等它健康:

```bash
docker compose logs -f backend        # Ctrl-C 只是退出看日志,不会停容器
docker compose ps                     # STATUS 要出现 (healthy)
```

`healthcheck` 的 `start_period` 是 120 秒 —— **前两分钟显示 starting 是正常的**,
那段时间在把 BGE-M3 权重加载进内存。

> ### 🔍 这时候日志里有一条**必须看到**的行
>
> `backend/auth.py` 在加载时会把「门有没有锁」打到日志上(**源文件核对**):
>
> - 锁了 → `访问校验已开启:请求必须带 x-api-key 请求头;创建 run 限流 = 突发 20 次、每分钟补 20.0 次…`
> - **没锁 → 一整块 `!!!!!!` 感叹号横幅**,大意是「当前没有开启访问校验……账单敞开给人刷」
>
> ```bash
> docker compose logs backend | grep -A6 '访问校验\|!!!!!!'
> ```
>
> **看到感叹号横幅就说明 `GYT_ACCESS_TOKEN` 没生效,立刻停下回步骤 7。**
> 别往下走 —— 这时候起前端,等于把一个零鉴权的 Agent 端点接上公网。

自检(在 VPS 上打本机回环):

```bash
curl -s http://127.0.0.1:2024/ok       # 期望 200
```

### 步骤 10 · ★★★ 单独建一次知识库 ★★★

> **这一步最容易被漏,而漏了的后果是 knowledge(查规范条文)整块功能作废 ——
> 并且它不报错,只会一直回「规范里查不到」,和真的查不到长得一模一样。**

向量库是**生成物**,不进 git,**每台机器都要单独建一次**:

```bash
cd ~/gongyoutong
docker compose -f docker-compose.yml -f docker-compose.vps.yml \
  run --rm --no-deps backend python -m gyt.agents.knowledge.ingest
```

> ⚠️ **别用 `run --rm backend make build-knowledge`。**
> `docker-compose.vps.yml` 的收尾注释里给的是那个写法,但它跑不起来 ——
> **实测**镜像里既没有 `make` 这个可执行文件、`/app` 下也没有 `Makefile`
> (Dockerfile 的构建上下文是 `backend/`,而 `Makefile` 在仓库根)。
> 用上面这条 `python -m …` 的写法,它和 `Makefile` 里 `build-knowledge` 目标注释给的
> 容器写法是一致的。(已反馈给项目组。)

**跑多久:** 首次约 **15 分钟**(切分 PDF + 逐块向量化;
2.2GB 的模型权重已经烤在镜像里了,这一步不再下载)。
幂等,可以反复跑;第二次起 manifest 命中就秒过。

> ### 另一条路:把开发机上建好的 `data/chroma` 整个搬过去(**2026-08-11 那次实际走的就是这条**)
>
> 向量库是**生成物**,内容只取决于「哪些 PDF + 哪个 embedding 模型」,和机器无关。
> 所以在开发机上建好之后整个目录搬过去也行 ——
> `docker-compose.vps.yml` 的收尾注释里本来就写了这条(**源文件核对**)。
>
> **VPS 实测**:那次搬过去的 `data/chroma` 是 **12MB**,**VPS 上一次都没跑过 `ingest`**。
> 好处是省掉 VPS 上那 15 分钟,更重要的是**省掉一次在小内存机器上做批量向量化的风险**。
>
> ⚠️ **搬完必须回步骤 5 重做属主** —— `rsync -a` / `scp -p` 都会把源端 uid 带过去,
> 而容器是 uid 10001。`data/chroma` 属主不对的表现和「库没建」一模一样:
> 一直回「规范里查不到」。
>
> ⚠️ 这条路**不适用于**「你在 VPS 上加了新的规范 PDF」的情况 —— 那种时候还是得在
> VPS 上跑一次上面那条 `python -m …`,或者在开发机上重建完再搬一次。

**怎么算成功 —— 看输出,别只看有没有报错:**

| 输出 | 含义 |
|---|---|
| `已建库:` 后面跟 `<文件名>: N 个 chunk` | ✅ 真成功 |
| `全部已入库、无需重建(共 N 份规范)` | ✅ 也是成功(重复跑时的正常输出) |
| `[错误] 规范目录不存在:…` | ❌ 卷没挂上 / 资产没拉下来 → 回步骤 4 |
| `[错误] … 里一份 PDF 都没有` | ❌ 同上 |

(这四种输出**源文件核对**自 `backend/src/gyt/agents/knowledge/ingest.py` 的 `main()`。
那个函数的 docstring 里记着为什么要分这么细:以前「目录不存在」和「已入库无需重建」
共用一句成功文案 + exit 0,于是**「资产根本没进来」在验收里显示为通过**,
一路拖到演示时才暴露。)

**⚠️ 千万别改成「起服务时自动建库」。** 配置里那个
`GYT_KNOWLEDGE_PREBUILD_AT_STARTUP` 开关**只适合本机 dev**:
容器 healthcheck 的 `start_period` 只有 120 秒,15 分钟的启动建库会让它
**反复重启、永远不 healthy**,而前端 `depends_on: service_healthy`,于是整栈永远起不来。
(**源文件核对**:`.env.example` 该变量的注释、`Makefile` 的 `build-knowledge` 注释。)

### 步骤 11 · 生成前端目录并起整栈

`frontend/` **不在 git 里**(`.gitignore` 里 `/frontend/`,**源文件核对**),
由脚本从 LangChain 上游 clone 生成,并自动打上本项目的界面覆盖件:

```bash
cd ~/gongyoutong
bash scripts/setup-frontend.sh
ls frontend/package.json frontend/Dockerfile     # 两个都在才算成功
```

> ### ⚠️⚠️ 起整栈之前,先确认这一件事 —— 否则测试者一提问就被挡在门外
>
> 令牌要靠 `NEXT_PUBLIC_API_KEY` 在**构建期**注进前端包。
> `docker-compose.vps.yml` 已经把这个 build arg 传下去了,**但**
> `scripts/setup-frontend.sh` 生成的 `frontend/Dockerfile` 里必须有对应的
> `ARG NEXT_PUBLIC_API_KEY` 才接得住 —— **Docker 对没声明的 build arg 只警告一句
> `unused build arg` 就过去了**,构建照样成功、页面照样能开,
> **只是令牌永远没进包**,表现是每次提问都被后端 401。
> (这段风险 `docker-compose.vps.yml` 自己的注释里也写明了,**源文件核对**。)
>
> ```bash
> grep -n 'NEXT_PUBLIC_API_KEY' frontend/Dockerfile
> ```
>
> - **有输出** → 好,继续。
> - **没有输出** → 令牌不会自动注入。两条路:
>   ① 让项目组补上 `scripts/setup-frontend.sh` 的 Dockerfile 模板(**正解**,
>      改完重跑 `bash scripts/setup-frontend.sh --force` 再构建);
>   ② 临时兜底:先照常上线,然后告诉每个测试者在浏览器 F12 控制台里种一次令牌
>      —— 每个浏览器只需一次,做法见 §9 末尾。**这条兜底会把令牌交到测试者手上,
>      能接受再用。**
>
> #### ✅ 这条链现在是通的(2026-08-11 更正)
>
> **手册原来把这条列成「已知目前接不上」**(§9 末尾原文:
> 「在 `scripts/setup-frontend.sh` 的 Dockerfile 模板补上 `ARG NEXT_PUBLIC_API_KEY` 之前,
> 令牌不会进浏览器包」)。**那个说法现在不成立了**,两头都补齐了:
>
> - **模板端**:`scripts/setup-frontend.sh:313` 已经有 `ARG NEXT_PUBLIC_API_KEY=`,
>   并且写进了 build 阶段的 `ENV`(**源文件核对**);
> - **读取端**:同一个脚本注册了 `api-key.tsx` 覆盖件,让 `getApiKey()` 除了 localStorage
>   之外也读构建期变量(**源文件核对**)。上游那份只读 localStorage;
> - **拦截端**:`scripts/preflight_vps.sh` 第 ④ 组会硬查 `frontend/Dockerfile` 里有没有这个 ARG,
>   没有就判 FATAL(见步骤 8.5)。
>
> **VPS 实测**:那次构建完,在前端产物 `/app/.next/static/chunks/app/page-*.js` 里
> **精确匹配到了完整的 64 位令牌**,以及 `https://velactora.com/api` ——
> 也就是说地址和令牌确实都烘进了浏览器包。上面那条 `grep` 仍然值得跑,**但它现在应该是有输出的**;
> 没输出说明你的 `frontend/` 是用旧版脚本生成的,`bash scripts/setup-frontend.sh --force` 重来一次。
>
> ⚠️ 顺带提醒一句这件事的另一面:**令牌就明明白白躺在浏览器包里,任何能打开页面的人都能扒出来。**
> 这是设计上接受的(理由见 §0.3 末尾),不是疏忽 —— 但别把它当机密。

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build

# 五个都要在:
docker compose ps      # gyt-backend-vps / gyt-frontend-vps / gyt-artifacts / gyt-login / gyt-caddy
```

> **⚠️ `gyt-login` 是 2026-08-11 上午新加的第五个服务**(手册这里以前写的是「四个」)。
> 它**在首屏路径上** —— caddy 的 `forward_auth` 每个请求都要问它一次。
> 它没起来的话**整站每个请求都会 502,包括登录页自己**。
> (compose 里 caddy 对它是 `depends_on: service_started`,**源文件核对** `docker-compose.vps.yml`。)

> **注意这里没有 `--profile ui`。** 基础档把 frontend 关在 `profiles: [ui]` 里,
> 而 VPS 档用 `profiles: !reset []` 把它放了出来(**源文件核对** `docker-compose.vps.yml`)——
> 因为 caddy `depends_on` 前端,前端不起 caddy 就一路 502。
> 同理,VPS 档的镜像 tag 是 `gyt-backend:vps` / `gyt-frontend:vps`(不是 `:dev`),
> 这是**刻意换的**:换 tag 保证机器上不会有一个「加 auth 之前构建的旧镜像」被
> `up -d` 直接拿去起来 —— 那种情况下鉴权会静默消失,而一切看起来都正常。

> ### ⚠️ 前端构建就是把「地址和令牌」烤进浏览器包的那一刻
>
> Next.js 的 `NEXT_PUBLIC_*` 是**编译期**变量 —— 只写在 `environment:` 里是**没用的**,
> 必须走 `build.args`(**源文件核对**:`docker-compose.yml` 的 `frontend.build.args` 注释、
> `frontend/Dockerfile` 的 `ARG NEXT_PUBLIC_API_URL`)。所以:
>
> **改了域名(`GYT_PUBLIC_ORIGIN`)或令牌(`GYT_ACCESS_TOKEN`),必须 `--build` 重新构建前端。**
> 只 `restart` 页面还会去连旧地址、带旧令牌。
>
> 最快的确认办法:浏览器打开站点 → F12 → Network → 看请求打到哪个 host、有没有带 `X-Api-Key` 头。

### 步骤 12 · 人肉冒烟

浏览器打开 `https://<你的域名>/`:

1. 跳到 **`/login`,一张写着「工友通」的登录页**(不是浏览器那个灰框)→ **只输口令、没有用户名**
   → 进入聊天界面。
   **直接看到聊天界面、根本没跳登录页,就是 ① 没生效 —— 立刻停下,回 §0.5 和 §3.1。**

   > ~~原来这里写的是:「弹出**浏览器原生的登录框**,标题写着『工友通 GYT 内测』→ 输用户名口令。~~
   > ~~**没弹框就是 ① 没生效**」~~ —— **2026-08-11 上午起不成立了**:现在**不会有任何弹框**,
   > 服务端已经不发 `WWW-Authenticate` 头了(**VPS 实测**:响应头里已无这个头)。
   > 判据也跟着反过来了:以前是「没弹框 = 出事」,现在是「**没跳登录页 = 出事**」。
2. 发一句纯文本:**「明天要绑扎钢筋」** → 应该看到 schedule 记了一条任务。
3. 传一张工地照片 → 等 **10~60 秒**(视觉调用就是这么慢,见 §4.3)→ 应该给出违规项清单。
4. 再问 **「规范里对消防车道宽度是怎么要求的?」** → 应该给出**带页码**的条文出处。
   **如果它说「规范里查不到」,几乎可以肯定是步骤 10 没做或者做失败了。**

> ### ✅ 2026-08-11 那次冒烟实际跑出来的东西(**VPS 实测**)
>
> 记在这儿是因为:**门锁通了不等于业务通了。** 一套只剩登录框的站点也能把 §3 四条验证全走绿。
>
> **① 排期链路 —— 不看回执,直查库。**
> 提问「记一条:明天上午复核 3-5 轴柱距」→ 路由到 `transfer_to_schedule` →
> 然后**直接查 VPS 上那个 SQLite**,确认真有这一行:
>
> ```
> {'id': 1, 'title': '复核 3-5 轴柱距', 'due_date': '2026-08-12',
>  'status': 'open', 'created_at': '2026-08-11T02:24:18+08:00'}
> ```
>
> 两件事同时被证明:**任务真落库了**(不是模型编的回执 —— 本仓在这上面栽过,见 CLAUDE.md
> 的防假账守卫),以及 **`TZ` 那一行真生效了** —— `created_at` 是 `+08:00`,
> 「明天」被算成 `2026-08-12` 而不是差一天(步骤 7 那个坑)。
>
> **② 知识链路 —— 验的是它肯不肯认怂。**
> 提问「脚手架的连墙件规范上是怎么要求的?」→ 路由到 `transfer_to_knowledge` →
> 检索两次都只命中防火规范(库里目前只有 GB 50016)→ **如实回答「知识库里查不到」,
> 并给出 JGJ 130 作为去处,没有编造条文号。**
>
> ⚠️ **注意这跟上面第 4 条不矛盾,但很容易看混:**
> 「查不到」在**库里没有那本规范**时是**正确行为**;在**库里有、却查不到**时才是步骤 10 出了问题。
> 分辨方法是拿一个**你确信库里有**的问题去问(比如消防车道宽度,GB 50016 里就有)。
>
> **③ 确认是流式,不是转半天一次性蹦出来。**
> schedule 那次的 SSE 事件类型分布:`messages/metadata` ×6、`messages/complete` ×6、
> `updates` ×3、`metadata` ×1。
>
> **④ 非视觉链路的端到端耗时**(单次,并发 1):
>
> | 提问 | 耗时 |
> |---|---|
> | 一次 knowledge 提问 | **19 秒**(含**首次**把 BGE-M3 加载进内存那一下,见 §1.1 的采样) |
> | 一次 schedule 提问 | **6 秒** |
>
> 视觉那一档仍然是 10~60 秒,那个数没变(§4.3)。

---

## 3. 验证清单:四层是不是真的都在

**做完这四条再把地址发出去。** 命令在**你自己的电脑**上跑(除非另有说明)。

### 3.1 ① 口令拦得住

> **⚠️ 这一整节 2026-08-11 上午重写过。**
> ~~原来是两条 curl:不带口令期望 **401**、`-u '<用户名>:<口令>'` 期望 200。~~
> **换成登录页之后,那两条都不再成立** —— 未登录是 **302 跳 `/login`**(不是 401),
> 而 `-u` 那套 basic auth 凭据**服务端已经不认了**(它压根不发 `WWW-Authenticate` 头)。
> 下面这一套是**换完之后从外部机器重新实测的**。

```bash
# ① 未登录访问首页 —— 期望 302,Location 是 /login
curl -s -o /dev/null -w '%{http_code} → %{redirect_url}\n' https://<你的域名>/

# ② 确认那个原生弹框真的不会再出现 —— 期望这条一行都不输出
curl -sI https://<你的域名>/ | grep -i 'www-authenticate'

# ③ 登录页本身是公开的 —— 期望 200,标题是「工友通 · 内测登录」
curl -s https://<你的域名>/login | grep -o '<title>.*</title>'

# ④ 错口令 —— 期望 302 到 /login?e=1,而且**一个 Set-Cookie 都不发**
curl -si -X POST --data-urlencode 'pw=显然是错的' https://<你的域名>/login \
  | grep -iE '^HTTP/|^location:|^set-cookie:'

# ⑤ 对口令 —— 期望 302 到 /,并发一个 gyt_sess Cookie(把它存进 jar,后面几条要用)
curl -si -c /tmp/gyt.jar -X POST --data-urlencode 'pw=<你的口令>' https://<你的域名>/login \
  | grep -iE '^HTTP/|^location:|^set-cookie:'

# ⑥ 带会话访问首页 —— 期望 200
curl -s -b /tmp/gyt.jar -o /dev/null -w '%{http_code}\n' https://<你的域名>/

# ⑦ 未登录打 /api/* —— 期望 401 + 中文 JSON(**不是 302**)
curl -s https://<你的域名>/api/ok -w '\n[%{http_code}]\n'

# ⑧ 未登录取产物 / 带会话取产物 —— 期望 302 / 200
curl -s -o /dev/null -w '未登录:%{http_code}\n' https://<你的域名>/artifacts/by-id/<32位编号>
curl -s -b /tmp/gyt.jar -o /dev/null -w '带会话:%{http_code}\n' https://<你的域名>/artifacts/by-id/<32位编号>
```

**① 回 200 = 门是开的,立刻下线**(§6.4)。**这正是 §0.5 那次裸奔的现象** ——
那次未登录访问首页拿到的就是 200,而 `caddy validate` 说 Valid、日志里一个错都没有。

#### VPS 实测(2026-08-11 上午,换完登录页之后,从外部机器打)

| 检查 | 结果 |
|---|---|
| 未登录访问 `/` | **302 → `/login`** |
| 响应头里的 `WWW-Authenticate` | **已无** —— 浏览器原生弹框不会再出现 |
| `/login` | **200**,`<title>工友通 · 内测登录</title>` |
| 错口令 | 302 → `/login?e=1`,且**一个 Cookie 都不发** |
| 对口令 | 302 → `/`,`Set-Cookie: gyt_sess=...; Path=/; Max-Age=1209600; HttpOnly; SameSite=Lax; Secure` |
| 带会话访问 `/` | **200** |
| 未登录打 `/api/*` | **401** + `{"detail":"登录已过期,请刷新页面重新登录。"}` |
| 未登录取产物 `/artifacts/*` | **302** |
| 带会话取产物 | **200** |
| 会话签名改一位 / 过期时间改大 / 空 / 垃圾 | **全部 302**(拒绝) |
| 有会话但**无令牌**打 API | **401** —— 两层门仍然独立,过了①不等于过了② |
| 连发 12 次错口令 | **302×4 然后 429×8** |
| scrypt 单次耗时 | **46ms** |

> **`/api/*` 回 401 而不是 302,是刻意的。** 打 `/api/*` 的是前端的 `fetch` / SSE,
> 给它 302 到一张 HTML 登录页,它会把那坨 HTML 当 JSON 解析,
> 然后报出一个**和登录毫无关系**的错 —— 人会去查后端、查网络,方向全错。
> (**源文件核对** `scripts/serve_login.py` 的 `_handle_verify`。)

> ### 会话 Cookie 长什么样、能撑多久
>
> 无状态签名 Cookie,**不需要任何存储、重启不掉线**:
>
> ```
> gyt_sess = v1.<到期unix秒>.<hmac_sha256>
> ```
>
> 有效期 **14 天**(上面实测里的 `Max-Age=1209600`)。
> **会话密钥从口令哈希派生**(**源文件核对** `scripts/serve_login.py` 的 `_session_key`),
> 所以不用多一个配置项,而且副作用正好是想要的:
>
> **⚠️ 改口令 = 所有已发出的会话立刻失效。** 这是「踢人下线」的唯一办法,
> 也意味着**换口令之后要重新通知所有测试者**,不能只告诉新来的那个人。
>
> 上面那四种伪造(签名改一位 / 把过期时间改大 / 空 / 垃圾)全部被拒,
> 说明**先验签再看过期**这个顺序是对的 —— 反过来写就等于让人拿伪造的过期时间去试探。

> ### 登录接口自带限流(这是 basic_auth 时代没有的)
>
> `Caddyfile` 原来那段注释自己承认过:**换着花样发错口令能持续消耗 CPU,
> 而 Caddy 官方镜像不带限流模块**。现在这道门在 login 服务里,那个洞跟着补上了 ——
> `scripts/serve_login.py` 自带令牌桶(**源文件核对** `_LoginThrottle`)。
>
> **全局一个桶,不按 IP 分**,理由和 `backend/auth.py` 那只桶一样:
> 这里只有一个身份(一个口令),分不出人;桶护的是**这台 2 vCPU 机器的 CPU**,
> 不是人与人之间的公平。**代价说清楚:有人在乱试的时候,正常测试的人也会被挡一会儿。**
> 内测环境,这个代价可以接受。
>
> **VPS 实测:连发 12 次错口令 → 302×4,然后 429×8。**

> ### ⚠️⚠️ 域名模式下的「反裸奔」自检,跟裸 IP 模式**不是同一条命令**(2026-08-11 补)
>
> **手册这一节原来只给了一种写法,而它在域名模式下验不到东西。**
> `docker-compose.vps.yml` 第 310 行附近的注释块已经按模式分好岔了,**这一节现在与它对齐**。
>
> **裸 IP 模式**(`GYT_SITE_ADDRESS=":80"`)—— 直接打 IP 就行:
>
> ```bash
> curl -sS -o /dev/null -w '%{http_code}\n' http://<VPS_IP>/         # 期望 302
> curl -sS -o /dev/null -w '%{http_code}\n' http://<VPS_IP>/api/ok   # 期望 401
> ```
>
> **域名模式**(`GYT_SITE_ADDRESS=<域名>`)—— **上面那两条会回 308,而 308 不代表出事,
> 也不代表没事**:
>
> ```bash
> curl -sk --resolve <域名>:443:<VPS_IP> -o /dev/null -w '%{http_code}\n' https://<域名>/
> # 期望 302(VPS 实测:302,Location 是 /login)
>
> curl -sk --resolve <域名>:443:<VPS_IP> -o /dev/null -w '%{http_code}\n' https://<域名>/api/ok
> # 期望 401 —— ⚠️ 注意这条**不带会话**,挡它的是这道门;带上会话它就变 200 了,见本框末尾
> ```
>
> **⚠️ 期望值 2026-08-11 上午改过:首页那条从 `401` 变成 `302`。**
> 换成登录页之后,未登录不再是 401 而是跳登录页。`/api/*` 那条仍然是 401
> (前端 fetch 拿 HTML 会更难查,理由见上面)。
>
> **为什么 `curl http://<VPS_IP>/` 在域名模式下回 308(VPS 实测,实测值就是 308):**
> Caddy 一旦启用 automatic HTTPS,就会在站点块**之外**另起一台独立服务器
> (日志里叫 `remaining_auto_https_redirects`)专管 `:80`,对**任意 Host、任意路径**无差别 308 到 https。
> 也就是说 `Caddyfile` 里 `{$GYT_SITE_ADDRESS} {` 那个**带门的站点块根本没被进入** ——
> 这条命令验证到的只是「重定向存在」,**验不到口令层有没有生效**。
> 而 308 又足够像「服务活着」,紧张的时候很容易被当成通过。
>
> **⚠️⚠️ 308 和 302 现在长得很像,别看错。** 换登录页之前,这一节的正常值是 401、
> 异常值是 308,一眼能分。现在正常值是 **302**、这个坑仍然是 **308** —— 差一个数字。
> 判法:**看 `Location`**。`302 → /login` 是门在工作;`308 → https://…` 是那个 :80 跳转器,
> **什么都没验到**。上面那条 `-w '%{http_code} → %{redirect_url}'` 就是为这个写的。
>
> **别想着用 `-L` 追下去。** 追下去是 `https://<VPS_IP>/`,SNI 是裸 IP、选不出域名的证书,
> 握手直接炸 —— `docker-compose.vps.yml` 的注释记着实测结果是 `tlsv1 alert internal error`、
> curl 的 `%{http_code}` 是 `000`(**源文件核对**)。既不是 302 也不是 200,更难判断。
> `--resolve` 的作用就是**让 SNI 走域名、而连接落到你指定的那台机器上**。
>
> ⚠️ **`/api/ok` 这两条更要当心 —— 它在两种会话状态下含义完全不同:**
> **不带**会话打 `/api/ok` 回 401,那是**口令层**在挡,是这一节要验的东西;
> **带上**会话再打它回 **200**,那是 `/ok` 被后端鉴权框架豁免,**跟令牌层一点关系都没有**。
> 拿后者去验令牌层就会得出「令牌层没生效」的假结论 —— 详见 §3.2 开头那个陷阱。

> 顺带说一件设计得很聪明、你应该知道的事:**caddy 容器的 healthcheck 断言的是「返回 302」,
> 不是「返回 200」**(**源文件核对** `docker-compose.vps.yml`)。
> 302 同时证明两件事:Caddy 活着、**并且会话那道门是开着的**(未登录访问 `/` 被打回登录页)。
> 所以哪天那道门被误删或写错位置、站点变成 200 全放行,
> **caddy 容器会直接变 unhealthy**,而不是高高兴兴地报健康。
> 也就是说 `docker compose ps` 里 caddy 那一行的 `(healthy)`,本身就是一条持续的门禁检查。
>
> **⚠️ 这个断言 2026-08-11 上午从 401 改成了 302。** 改登录页时**漏改这一条的表现是:
> caddy 永远 unhealthy,而站点其实好好的** —— 人会去查一个不存在的故障
> (查证书、查反代、重启 caddy),而真因只是健康检查还在按旧返回码判。
>
> **而这条 healthcheck 自己也是按模式分岔的**,理由跟上面那个 308 完全一样:
> 裸 IP 模式探 `http://127.0.0.1:80/`,域名模式探 `https://<域名>/` 并用 `curl --resolve`
> 把域名钉回本机回环(这样 SNI 才走得对)。用 busybox 的 `wget` 不行 —— 它走 https 时不发 SNI。
> (**源文件核对** `docker-compose.vps.yml` 的 caddy `healthcheck`,那段注释写着这条真机验过三遍才写对。)
>
> **VPS 实测**:`docker compose up -d` 之后 **20 秒内** caddy 就 `healthy`。
> ⚠️ 那次测的是 basic_auth 版本(判据「HTTPS 上拿到 401」),**换成 302 之后没有重新计时** ——
> 「20 秒」这个数当参考,别当承诺。当时的推论仍然成立:
> 由于判据要走 HTTPS,**healthy 同时说明证书已经签发就绪了**,不用另外去等。

### 3.2 ② 令牌拦得住

> ### ⚠️ 真陷阱:**不要拿 `/ok` 或 `/info` 来验令牌**
>
> **实测(读 `langgraph_api` 0.12.0 源码确认)**:LangGraph 服务把路由分成两拨 ——
>
> - **不过鉴权**:`GET /ok`、`GET /info`、`GET /`、`GET /docs`、`GET /openapi.json`、`GET /metrics`
>   (源码里的 `unshadowable_meta_routes` / `shadowable_meta_routes`,挂在鉴权中间件**外面**)
> - **过鉴权**:`/assistants/*`、`/threads/*`、`/runs/*`、`/store/*`
>   (`protected_routes`,包在 `middleware_for_protected_routes = [auth_middleware]` 里)
>
> 也就是说**不带令牌 curl `/ok` 照样回 200**。拿它验会得出「鉴权根本没生效」的错误结论,
> 然后去改一个本来就是对的配置。
>
> 顺带说明两件事:
> ① 这也正是**容器 healthcheck 加了鉴权之后仍然健康**的原因(它探的就是 `/ok`),这是好事;
> ② `/docs` 和 `/openapi.json` 是**公开**的 —— 挡它们的是 Caddy 口令,不是令牌。
>
> #### ⚠️⚠️ 这条陷阱 2026-08-11 那次上线**差点真的踩下去**,所以补一个实测值
>
> 原来这一段是**读源码**推出来的。现在有实打实的数了(**VPS 实测**):
>
> **过了第一道门、但完全不带 `X-Api-Key`,打 `https://<域名>/api/ok` → 回 `200`。**
> (那次过门用的是 basic auth 口令;换成会话 Cookie 之后**门的形态变了、这条结论没变** ——
>  `/ok` 被鉴权框架豁免这件事跟第一道门长什么样一点关系都没有。)
>
> 当时看到这个 200,第一反应就是「令牌层没生效」—— **而令牌层好好的**。
> `/ok` 是 langgraph-api 的健康端点,被 Auth 框架豁免,**后端自己的 healthcheck 就是靠
> 在容器内不带令牌打它才能探活的**。拿它验令牌层,得到的是一个百分之百的假结论,
> 然后你会去改一个本来就对的配置,越改越乱。
>
> **规矩:验令牌层,只用 `/api/threads` 或 `/api/threads/search` 这类受保护路由。**

正确的验法(**必须用受保护路由**;`/api` 前缀来自 `Caddyfile` 的 `handle_path /api/*`,
它会自动剥掉前缀再转给后端,**源文件核对**。`/tmp/gyt.jar` 是 §3.1 第 ⑤ 条存下的会话):

```bash
# A) 过了第一道门、不带令牌 —— 期望 401
curl -s -b /tmp/gyt.jar -X POST https://<你的域名>/api/threads/search \
  -H 'Content-Type: application/json' -d '{"limit":1}' -w '\n[%{http_code}]\n'
# 响应体里应该只有一句中文:访问被拒绝,请联系发你链接的人。
# ⚠️ 它对「没带头 / 带了空串 / 令牌不对」回的是**同一句话**,这是故意的 ——
#    任何差异都是送给爆破脚本的信号。真实原因只进服务端日志。

# B) 过了第一道门、带正确令牌 —— 期望 200
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/gyt.jar \
  -X POST https://<你的域名>/api/threads/search \
  -H 'Content-Type: application/json' -H 'X-Api-Key: <你的令牌>' -d '{"limit":1}'
```

> **⚠️ 命令形态 2026-08-11 上午换过:`-u '<用户名>:<口令>'` → `-b /tmp/gyt.jar`。**
> 服务端已经不认 basic auth 了,`-u` 只会让请求停在第一道门(拿 401,不是 A) 要验的那个 401)。
> **下面那张表里的数是 basic_auth 时代打出来的**,当时过门的方式是 `-u`;
> 令牌层本身这次一个字都没动,而且换完之后**「有会话但无令牌打 API → 401」是重新实测过的**
> (§3.1 那张表最后几行)—— 但 B) 的 200 **没有在换完之后重跑**,照实说明在这儿。

**A/B 两条 2026-08-11 凌晨那次真打过了,连同一个受保护路由。以下全是实测值(第一道门都过了):**

| 请求 | 无令牌 | 令牌不对 | 令牌正确 |
|---|---|---|---|
| `POST /api/threads` | **401** | **401** | **200** |
| `POST /api/threads/search` | **401** | 未单独试 | **200** |
| `GET /api/ok` | **200** ⚠️ | — | 200 |

最后一行就是上面那个陷阱 —— **它对令牌层没有任何鉴别力,别拿它当验证**。

**无令牌时后端回的响应体原文(VPS 实测):**

```json
{"detail":"访问被拒绝,请联系发你链接的人。"}
```

中文人话、没有堆栈、没有类名、没有内部路径 —— 符合本仓对面向用户字符串的要求。
**「令牌不对」和「没带令牌」回的是同一句话**,这是故意的(理由见上面那条注释)。

**C) 还要验一条后门 —— 这条最容易被漏:**

```bash
# 不带令牌,但加一个 x-auth-scheme: langsmith 头 —— 期望仍然 401
curl -s -b /tmp/gyt.jar -X POST https://<你的域名>/api/threads/search \
  -H 'Content-Type: application/json' -H 'x-auth-scheme: langsmith' \
  -d '{"limit":1}' -w '\n[%{http_code}]\n'
```

⚠️ **这一条 2026-08-11 那次没跑**(A/B 两条跑了,C 没跑)。它仍然是**按代码判据写的**,
不是实测值 —— 别因为上面那张表全是实测就顺手把这条也当验过了。**它反而是三条里最该跑的一条。**

**这一条回 200 就是重大问题,立刻下线。**
langgraph-api 里有一条旁路:满足特定条件时,带上 `x-auth-scheme: langsmith` 会让
**自定义鉴权整个被跳过**,换成一个内置的 Studio 用户。而那些条件在本项目里恰好成立
(`langgraph dev` 启动时会设 `LANGSMITH_LANGGRAPH_API_VARIANT=local_dev`)。
仓库已经堵了两道:`langgraph.json` 里的 `"disable_studio_auth": true` 是源头,
`backend/auth.py` 里的身份白名单是第二道。**这条 curl 就是验这两道还在不在。**
(**源文件核对**:`backend/auth.py` 的 `_reject_foreign_identity` 文档字符串。)

### 3.3 ③ 限流拦得住

> ### ⚠️ 先搞清楚它卡的是**哪个动作**,否则你会验错东西
>
> 限流**只卡「创建 run」**(`@auth.on.threads.create_run`,**源文件核对** `backend/auth.py`)。
> 读线程、列历史、翻记录**故意不卡** —— 那些不花钱,测试的人回头翻聊天记录不该被打断。
>
> 所以**拿 `/threads/search` 连打一百次是永远不会 429 的**,那不是限流坏了。

**便宜的验法(推荐):把额度临时调到 1,只花一次很便宜的纯文本调用。**

在 **VPS 上**:

```bash
cd ~/gongyoutong

# 1) 临时把突发和速率都压到 1
#    (改的是环境变量,不是代码 —— 这个仓库的规矩)
printf 'GYT_RATE_LIMIT_BURST=1\nGYT_RATE_LIMIT_PER_MINUTE=1\n' >> .env
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d backend
docker compose logs backend | grep '访问校验已开启'   # 应显示 突发 1 次、每分钟补 1.0 次
```

在**你自己的电脑**上连发两次:

```bash
BODY='{"assistant_id":"gyt","input":{"messages":[{"role":"human","content":"你好"}]}}'
for i in 1 2; do
  curl -s -o /dev/null -w "第${i}次:%{http_code}\n" --max-time 25 \
    -b /tmp/gyt.jar -X POST https://<你的域名>/api/runs/stream \
    -H 'Content-Type: application/json' -H 'X-Api-Key: <你的令牌>' -d "$BODY"
done
```

> **⚠️ 本节所有 curl 的过门方式 2026-08-11 上午从 `-u '<用户名>:<口令>'` 换成了
> `-b /tmp/gyt.jar`(§3.1 第 ⑤ 条存下的会话)。** 本节那次本来就没跑通(状态见下方表),
> 换的只是命令形态,期望值没动。

**期望:第 1 次 200,第 2 次 `429`。**

- 第 1 次是一次真实的纯文本调用(便宜,不是视觉调用)。
- 第 2 次的 429 **一次模型都不会调、不花钱** —— 限流卡在 run 落库、图被调起**之前**
  (**源文件核对**:`backend/auth.py` 里已核对过 `langgraph_runtime_inmem/ops.py` 的 `Runs.put`)。
- 429 的响应体是一句中文:**「问得太快啦,请等几秒再发一条。」**,还会带标准的 `Retry-After` 头。

验完**记得改回去**(把刚才追加的两行删掉,让它回到代码里的默认值 20 / 20):

```bash
cd ~/gongyoutong
sed -i '/^GYT_RATE_LIMIT_BURST=/d;/^GYT_RATE_LIMIT_PER_MINUTE=/d' .env
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d backend
docker compose logs backend | tail -30 | grep '访问校验已开启'   # 确认回到 20 / 20.0
```

**顺带验一条 cron:**

```bash
# 期望 403 —— 测试实例不开放定时任务
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/gyt.jar \
  -X POST https://<你的域名>/api/runs/crons \
  -H 'Content-Type: application/json' -H 'X-Api-Key: <你的令牌>' \
  -d '{"assistant_id":"gyt","schedule":"*/1 * * * *","input":{"messages":[]}}'
```

拦它的理由:一个 cron 是「创建一次、以后自己反复跑 run」,而 cron 触发的 run
**未必罩得进那只令牌桶** —— 等于绕过限流开了一条长期烧钱的管子。
来测试的人不需要定时任务,拦掉的代价是零。(**源文件核对**:`backend/auth.py` 的 `deny_cron_create`。)

⚠️ **本节(§3.3)的 429 / `Retry-After` / cron 403 都还没在真机上见过。**
cron 那半那次**完全没跑**;限流那半**跑了两次、但两次都没打到令牌桶上** ——
这两条死路记在这儿,免得你重走一遍:

- **刷「建线程」没用。** 连发 26 次 `POST /api/threads` 全是 200。
  `backend/auth.py:62` 写明限流只挂在 `@auth.on.threads.create_run` 上,
  **建线程是被刻意排除的**(读操作和线程管理不该被限流打断)。
- **发畸形 run 也没用**(本想借此不花钱地把桶抽干)。连发 26 次缺 `assistant_id` 的
  `POST /api/threads/<id>/runs`,全是 **422**、一次 429 都没有 ——
  **请求体的 JSON schema 校验跑在 auth 处理器之前,压根没消耗令牌。**

想真验只能发 20+ 次**合法** run,那要真花钱,那次没做。
⚠️ **但别据此说限流坏了**:`auth.py:201` 写明鉴权与限流**同开同关**,
而鉴权那次是实测在工作的(401 全对),所以桶是 **armed** 的 ——
只是 429 那条路在这台机器上没被走过。(本机彩排里走过:20 通过后 429 带 `Retry-After: 3`。)

> **§3 各条的验证状态,一句话说清**(免得被上面那些实测值带偏):
>
> | 小节 | 状态 |
> |---|---|
> | §3.1 口令(登录页 + 会话) | ✅ **VPS 实测,2026-08-11 上午换完登录页之后重测的整套**(含 308 那个坑) |
> | §3.2 A) 无令牌 / B) 对令牌 | ✅ **VPS 实测** —— 但那是 basic_auth 时代打的;换完之后只重测了 A)(「有会话无令牌 → 401」),**B) 没重跑** |
> | §3.2 C) `x-auth-scheme` 后门 | ⚠️ 未跑 |
> | §3.3 限流 | ⚠️ **试过两次,都没打到桶上**(两条死路见上方),429 没见过;桶是 armed 的 |
> | §3.3 cron | ⚠️ 未跑 |
> | §3.4 扫 **2024** 端口 | ✅ **VPS 实测** —— 从外部机器超时,`%{http_code}` 是 `000` |
> | §3.4 扫 **3000** 端口 | ⚠️ 未跑 |

### 3.4 ④ backend / frontend 端口没暴露到公网

在**你自己的电脑**上扫:

```bash
# 期望:两条都连不上(超时或 refused)
nc -z -w 3 <你的VPS公网IP> 2024 && echo "❌ 2024 暴露了!" || echo "✅ 2024 不可达"
nc -z -w 3 <你的VPS公网IP> 3000 && echo "❌ 3000 暴露了!" || echo "✅ 3000 不可达"
```

在 **VPS 上**再确认一次发布情况:

```bash
cd ~/gongyoutong
docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
# PORTS 一列:只有 caddy 该有 0.0.0.0:80 / 0.0.0.0:443
ss -tlnp | grep -E ':2024|:3000|:443|:80'
```

**判据:`2024` 和 `3000` 要么完全不出现在监听列表里,要么只绑在 `127.0.0.1`。
只要看见 `0.0.0.0:2024`,立刻按 §6.4 下线。**

> 基础档 `docker-compose.yml` 本来就把端口钉死在 `127.0.0.1`,
> VPS 覆盖件要做的是把发布整条**去掉**(compose 的列表是「合并」而不是「替换」,
> 想删掉基础档已有的 `ports` 需要用 `!reset` 这类显式清空语法 —— **本机实测** `ports: !reset []`
> 在 Compose v5.1.3 上确实能移除基础档的 `ports`)。
> **这条检查的意义是:确认没有人在中间「优化」掉这条红线。**

---

## 4. 已知限制 —— 上线前先知道,别当 bug 报回来

### 4.1 ~~巡检记录卡片和历史照片,在外网点不开~~ ✅ 2026-08-11 已修

> **这一整条已经不成立了,但原文留在下面**,因为它描述的失效模式仍然值得认识 ——
> 而且**同源清单少改一环就会退回这个状态**。

**原来是这样(2026-08-11 之前):** 聊天界面里那张「巡检记录」卡片、以及历史消息里的照片,
指向的地址是**写死的 `http://127.0.0.1:8788`**。对外部测试者来说,那个地址指的是
**他们自己的电脑** —— 照片是碎图、docx 点了没反应。当时的说法是「设计使然,
要看文件请 `scp` 下来」,并且要提前跟测试者打招呼免得被当 bug 报回来。

**⚠️ 而且它比原文写的还糟一层:** 站点上了 HTTPS 之后,https 页面去拉 http 资源属于
**mixed content**,浏览器**连请求都不会发**,只在控制台留一行 —— 界面上一点线索都没有。

**现在是这样:** 走**同源**路径 `<你的域名>/artifacts/*`,在口令门**后面**:

```
浏览器 ──▶ caddy ──handle_path /artifacts/*──▶ artifacts 服务(python:3.12-slim)
             │                                    只读挂 data/artifacts,uid 10001
             └ forward_auth 先拦一道               **一个 ports 都没有**
               (2026-08-11 上午前是 basic_auth)
```

五处同源,**断一环就退回上面那个状态,而且不报错**:

| 环 | 位置 |
|---|---|
| 两个覆盖件读编译期变量 | `scripts/frontend-overrides/human.tsx`、`tool-calls.tsx` 的 `ARTIFACT_BASE` |
| Dockerfile 模板声明 ARG | `scripts/setup-frontend.sh`(漏了**不报错**,同 `NEXT_PUBLIC_API_KEY` 那个坑) |
| 编排传值 + artifacts 服务 | `docker-compose.vps.yml` 的 frontend `build.args` 与 `artifacts:` |
| 反代路由 | `Caddyfile` 的 `handle_path /artifacts/*`(**必须写在兜底 `handle {}` 之前**) |
| 自检 | `scripts/preflight_vps.sh` ④ 组查那条 ARG、⑤ 组数发布端口总数(应为 3) |

**VPS 实测(2026-08-11,从外部机器)**:

| 请求 | 结果 |
|---|---|
| 照片 `/artifacts/by-id/<32位编号>` | **200** `image/jpeg` 358893B,落地校验是真 JPEG 1600×1066 |
| 巡检记录 同上 | **200** `application/vnd.openxmlformats-...wordprocessingml.document` 37454B,真 OOXML |
| ~~**不带口令**取产物~~ | ~~**401**~~ → **2026-08-11 上午换成登录页之后重测:302**(§3.1) |
| sidecar `.json` | **404**(`.json` / `%2ejson` / `HEAD` 三种走法都拦住) |
| 路径穿越 `../etc/passwd` | **404** |
| 编号格式非法 | **400** |

> **上表除「不带口令取产物」那一行外,都是 basic_auth 时代打的**,验的是 artifacts 服务本身
> (它这次一个字没改)。**带会话取产物 200 / 不带会话 302 这两条,换完之后重测过**,见 §3.1 那张表。

⚠️ **`serve_artifacts.py` 的 `BIND_HOST` 改了,但那条红线没有变松。**
它现在按 `/.dockerenv` 判断:宿主机上仍然是 `127.0.0.1` 且**依然没有 `--host` 开关**;
只有在容器里才绑 `0.0.0.0`,而那**只在该容器的网络命名空间内**。
保证从「写死在这个文件里」挪到了「编排里可被自检的不变量」——
**artifacts 服务不许有 `ports`**,preflight ⑤ 组数发布端口总数就是在守这个。
谁给它加一行 `ports`,就是把含可识别人脸的工地照片(`TODOS.md` TODO-22)直接挂公网。

> **⚠️ 这里原来挂着一条悬案,依据变了(但没变成「已验证」)。**
> ~~原文:「**仍然没验的**:`<img>` 标签去取 `/artifacts/*` 时浏览器带不带 **basic_auth** 凭据。」~~
>
> 当年它是悬案,是因为 basic_auth 靠的是「浏览器记住口令并自动重发」——
> **那是一个我们从没在真浏览器上验过的行为假设**。
> 换成会话 Cookie 之后,**同源请求带 Cookie 是浏览器的规定动作**,`<img src>` 一律带上。
>
> **依据从「假设」升级成了「浏览器规范行为」。但仍然没有人用真浏览器打开过这个站点** ——
> 上面那些数是 `curl` 打的,验的是**路由通**,不是浏览器行为。**别写成「已验证」**(同 §9 里 SSE 那条)。

### 4.2 ⚠️ 所有测试者共用一个身份,**能互相看到对方的会话历史**

全体测试者拿的是**同一把令牌**,所以后端看到的是同一个身份(`gyt-tester`),
线程没有按人隔离 —— **A 能在历史列表里看到 B 刚才问了什么、传了什么照片**。
(**源文件核对**:`backend/auth.py` 的 `allow_authenticated` 文档字符串已明确写出这个后果。)

这不是新增的退化(上线前是零鉴权,谁都看得见),但**必须提前告诉测试者** ——
尤其他们可能会传自己项目的照片。§5 的话术里写了。

### 4.3 视觉识别慢是正常的,10~60 秒

仓库里 2026-08-07 的实测记录(`config.py` 里 `llm_timeout_s` 上方的注释,**源文件核对**):

| 照片 | 耗时 |
|---|---|
| 1600×1067 办公室(一眼判定不是工地) | 10.1 秒 |
| 440×293 工地(要逐项分辨违规) | 42.9 秒 |
| 4000×2430 工地(手机原图尺寸) | 59.7 秒 |

所以超时阈值定的是 150 秒而不是 60。**测试者传手机原图,等一分钟是正常的** ——
界面上没有别的提示,不说清楚他们会以为卡死然后连点几次,而每一次点都是真金白银。

### 4.4 缓存是冷的,所以每一次提问都真的花钱

`data/cache/` 里的 LLM 响应缓存**跟着机器走**,新 VPS 上是空的。
本机焐热过的那些照片,在这台机器上一张都不认(见 §0.1)。

### 4.5 单进程,并发要排队

后端是单容器单进程(`backend/Dockerfile` 的 CMD 是一条 `langgraph dev`)。
**同时几个人提问会互相排队**,不是挂了。
给三五个人内部测试够用;要给几十个人同时用,得先做进程/队列这一层,不在本手册范围内。

⚠️ **2026-08-11 那次上线的并发是 1**(一个人在操作)。所以这一节说的排队现象
**既没有被观察到,也没有被证伪** —— 它是从「单进程」这个结构推出来的,不是实测。
§1.1 那份内存采样同样是并发 1 之下取的,**别拿它去推多人同时提问时的内存曲线**。

### 4.6 上传体积上限有两道,报错长相不一样

- **应用层**(`GYT_PHOTO_MAX_MB` / `DOCUMENT` / `DRAWING`)超了 → 给一句**看得懂的中文**。
- **Caddy 层**(`request_body max_size 128MB`)超了 → 一个干巴巴的 **413**。

128MB 这个数是按 `GYT_DRAWING_MAX_MB=64` 算出来的(base64 把体积撑到 4/3 → 约 85MB,
再留余量)。**改了 `GYT_DRAWING_MAX_MB` 就要回 `Caddyfile` 重算这一行**,
否则表现是「传大图纸必失败」,而报错来自 Caddy、不是那句写好的中文提示。
(**源文件核对**:`Caddyfile` 的 `request_body` 段。)

### 4.7 ⚠️ 后端日志里有一条**会把你带偏**的警告(不影响功能,未修)

**VPS 实测**:后端起来之后,日志里会出现这样一条 ——

```
Ignoring corrupted tree cache file /opt/hf/hub/models--BAAI--bge-m3/trees/xxx.json:
  [Errno 13] Permission denied: '...'
```

**它说的是 "corrupted"(损坏),但真实原因写在同一行的后半截:`Permission denied`** ——
镜像里 `/opt/hf` 的属主与运行用户(uid 10001)不一致。
(`backend/Dockerfile` 的注释写明「**故意不 `chown -R` `/app/.venv` 与 `/opt/hf`**:
它们只读即可,递归 chown 会把上千兆内容整层复制一遍,镜像体积直接翻倍」,**源文件核对**。)

**不影响功能。** 那次就是**带着这条警告跑通的**:模型照常加载、检索照常返回
(§步骤 12 的 knowledge 那条就是在这条警告之后跑出来的)。

**目前未修。** 记在这里只有一个目的:**别被 "corrupted" 这个词带去查权重文件损坏** ——
去 `docker exec` 里翻 `.safetensors` 的校验和、去重新拉权重、去怀疑镜像构建坏了,
全是白费,而这条路一走就是半小时。

---

## 5. 给测试者的话术(可以直接整段转发)

> **【工友通 · 建筑工地 AI 助手】内测邀请**
>
> 网址:https://<你的域名>
> 打开后会看到一张「工友通」的登录页,**只要输一个口令,没有用户名**:
>   口令:`<口令>`
>
> 登一次能管 **14 天**,这期间不用反复输。
>
> **它能干什么(直接用大白话问就行,不用记命令):**
> 1. **拍照查隐患** —— 传一张工地照片,它会指出没戴安全帽、没系安全带、料堆乱放这类问题,
>    并给每一条定个严重程度。
> 2. **自动出巡检记录** —— 顺手说一句「顺便出份巡检记录」,它会生成一份 Word 巡检记录。
> 3. **记工期任务** —— 「明天要绑扎钢筋」「三号楼下周三前完成模板」会被记进台账,
>    也能问「这周还有什么没干完」「T2 销掉」。
> 4. **查规范条文** —— 「消防车道宽度规范怎么要求的」,它会给出条文**和页码**。
> 5. **看图纸** —— 传一份 DXF 图纸,可以问图层、构件、尺寸。
>
> **几件要先说明白的事:**
> - ⏳ **传照片以后要等 10~60 秒**,手机原图更慢。**这是正常的,请不要连点** ——
>   每点一次都是一次真实的模型调用(会产生费用)。
>   如果你点太快,它会回一句「问得太快啦,请等几秒再发一条。」,等几秒再发就行。
> - 📄 巡检记录会生成一张卡片,**点上面的链接就能下载那份 Word**;传过的照片也会显示在历史里。
>   (2026-08-11 之前这两样在外网都打不开,那条限制已经没有了。
>   ⚠️ 但**没有人用真浏览器试过** —— 万一照片是碎图或者点了没反应,请**立刻告诉我**,
>   那是真 bug 不是设计如此。)
> - 👥 **这是共享的测试环境:大家用的是同一个账号,所以你在左边历史列表里能看到别人的会话,
>   别人也能看到你的。** 请不要在这里聊任何你不想被同组人看到的内容。
> - 🔒 **⚠️ 请不要上传含真实人脸的工地照片。**
>   照片里有工人或路人的清晰面孔的话,请**先打码再传**。
>   系统会把上传的图片留在服务器上,而这是测试环境、不是正式产品 ——
>   也请不要传涉密图纸和任何敏感资料。
> - 🧪 这是**内测**:它会答错,也会有答不上来的时候。
>   **它答「查不到 / 做不了」是正确行为,不是故障** —— 我们宁可它认怂也不许它编。
>   遇到明显不对的,请把**你的原话 + 它的回答截图**发给我。
> - 💬 上面那个口令**请不要外传**,这套东西每答一句都在花真钱。
>   (如果你看到「访问被拒绝,请联系发你链接的人。」或者「登录已过期,请刷新页面重新登录。」,
>   说明凭据出了问题,找我。)
> - ⌨️ 口令输错几次之后会看到「试得太频繁了,请等一分钟再试。」—— **那是限流,不是你被封了**,
>   等一分钟再输一次就行。

**⚠️ 上面这段话术 2026-08-11 上午改过。**
~~原文写的是「打开后浏览器会弹一个登录框(标题写着「工友通 GYT 内测」):用户名 `<用户名>` / 口令 `<口令>`」~~
—— 现在**不会弹任何框**,是一张自己做的登录页,而且**没有用户名这一项**。
照旧话术转发出去的后果:测试者盯着找那个弹框,或者在只有一个输入框的页面上找「用户名」填哪儿。

⚠️ **另外别忘了**:改口令 = **所有人的会话立刻失效**(会话密钥从口令哈希派生,§3.1)。
所以换口令时要通知**每一个**已经在用的人,不能只告诉新来的那个。

---

## 6. 成本与风险

### 6.1 钱花在哪

| 动作 | 花费 |
|---|---|
| 一句纯文本提问 | 一次或几次 DeepSeek 文本调用 —— 便宜 |
| **传一张照片** | **一次 Kimi 视觉调用 —— 最贵的那一档**,而且 10~60 秒 |
| 查规范条文 | 检索本身在本机跑(BGE-M3,不花钱),综合成人话那步花 DeepSeek |

**要命的是照片。** 一个人反复传图,或者写个脚本循环打 `/runs/stream`,
就是在直接刷你的额度。

### 6.2 限流是唯一的闸,别指望别的东西挡

**特别注意:`supervisor_recursion_limit`(默认 8)不是限流。**
它限的是**单次 run 内部转来转去的步数**,防的是 Agent 自己死循环;它**一点都不限 run 的次数**。
一个脚本每秒发 50 个 run、每个都规规矩矩只走 3 步,账单照样爆。
(**源文件核对**:`config.py` 该字段注释、`backend/auth.py` 模块文档、`TODOS.md` TODO-7。)

**默认值 20 / 20 的由来**(**源文件核对** `config.py`,不是拍脑袋):

- **突发 20** —— 真机验收脚本 `live_acceptance.py` 全程发 18 个 run。取 20 是为了
  「万一有人在本机也设了令牌,那 25 条验收光靠突发额度也能跑完」,不会被自己人拦下。
- **每分钟 20** —— 一张工地照片端到端 10~60 秒,一个人**物理上**不可能持续超过约 6 轮/分钟。
  20 留了三倍余量,够两三个人同时试;而脚本狂刷会被压到 **20×60 = 1200 次/小时**这个
  **有上限**的量级,而不是无限。

**⚠️ 这只桶是「进程内内存」实现的**,所以:多副本 → 实际配额 = 配置值 × 副本数;
进程重启 → 桶全满、白送一轮。今天是单容器单进程,够用;
**哪天想加副本,得先把桶换成 Redis 之类的共享计数器**,否则限流会静默地按副本数放大。
(**源文件核对**:`backend/auth.py` 模块文档字符串已把这条边界写明。)

### 6.3 怎么看用量

**唯一可信的是供应商控制台**(链接见 §1.4)。容器日志能看到调用发生,但看不到花了多少钱。
上线第一天建议每隔几小时看一眼,摸清正常水位长什么样 —— 后面才有「异常」可言。

服务端能看到的迹象:

```bash
cd ~/gongyoutong
docker compose logs --since 1h backend | grep '创建 run 被限流'   # 有人在刷的直接证据
docker compose logs --since 1h backend | grep '鉴权失败'          # 有人在试令牌
docker compose logs --since 1h caddy | tail -50                   # 访问日志
```

`Caddyfile` 已经配了日志过滤,**把 `Authorization` / `X-Api-Key` / `Cookie` 三个头替换成 `REDACTED`
再落盘**(**源文件核对**)—— 所以 `docker compose logs caddy` 的输出可以放心截图问人,
不会顺手把凭据一起发出去。**别去掉那段过滤。**

> **⚠️ `Cookie` 那一条从「预防性」变成「必须」了。** `Caddyfile` 里原来的注释写着
> 「Cookie 一并处理:本项目暂时没用到,但它属于同一类东西」——
> **2026-08-11 上午起,会话 Cookie 就是第一道门的凭据本身**,
> 拿到它等于拿到一个 14 天有效的通行证。这条过滤现在直接护着那道门。
>
> login 服务自己的日志也按同一条规矩写:**只打方法 + 路径 + 状态,绝不打 query 和 body**
> —— 因为它的 body 里就是口令明文(**源文件核对** `scripts/serve_login.py` 的 `log_message`)。

### 6.4 出事了怎么最快止血

**按严重程度递进,三档:**

```bash
cd ~/gongyoutong
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vps.yml"

# 第 1 档(约 5 秒):停后端。前端还在,但谁问都答不了 —— 花钱立刻停止。
$COMPOSE stop backend

# 第 2 档:整栈下线,网址直接打不开。data/ 不会被删,台账和产物都还在。
#         Caddy 的证书在命名卷里,也不会被删(用了 -v 才会,别加)。
$COMPOSE down

# 第 3 档(怀疑令牌/口令泄漏,或者账单已经异常):
#   去 DeepSeek 和 Moonshot 控制台 **吊销这两把 Key**,再各生成一把新的。
#   ⚠️ 这一档才是真止血 —— 模型 Key 就在容器里,只要有人拿到过它,
#      停你的容器并不能阻止他继续用那把 Key 花你的钱。
```

**换完 Key / 换完令牌重新上线的顺序:**
改 `.env` → `$COMPOSE up -d --build`
(**令牌变了必须 `--build`**,它烤在前端包里)→ 重跑 §3 的四条验证 → 把新口令发给测试者。

> **⚠️ 换口令(`GYT_LOGIN_HASH`)这一档,2026-08-11 上午多了一个后果:所有人当场掉线。**
> 会话密钥从口令哈希派生,改哈希 = 所有已发出的会话立刻失效(§3.1)。
> **这既是代价也是工具** —— 想踢掉某个不该继续访问的人,**改口令是目前唯一的办法**
> (没有按人隔离的账号,见 §4.2)。改完记得**通知每一个**在用的人,不能只告诉新来的那个。
> 换口令不需要 `--build`,`$COMPOSE up -d login` 就够(哈希只挂在 login 服务上)。

⚠️ **第 2 档下线时别加 `-v`。** `docker compose down -v` 会连命名卷一起删,
而 Caddy 的证书和 ACME 账号密钥就在 `caddy_data` 卷里。
删了要重新申请,而 Let's Encrypt 对同一域名有签发频率限制(一周几次)——
反复重建足够把自己关在门外,现象是「HTTPS 突然就申请不下来了」。
(**源文件核对**:`docker-compose.vps.yml` 的 `caddy_data` 卷注释。)

**VPS 实测**:命名卷 `gyt_caddy_data` / `gyt_caddy_config` 确实建出来了,证书文件就在

```
/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/velactora.com/velactora.com.crt
```

(路径里的域名换成你自己的)。所以**重建容器不会丢证书,也就不会反复向 Let's Encrypt 申请** ——
这正是不能加 `-v` 的原因,现在这句话有实物撑着了。

---

## 7. 日常运维小抄

```bash
cd ~/gongyoutong
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vps.yml"

$COMPOSE ps                     # 谁在跑、健不健康(caddy 那行 healthy = 会话门还在,见 §3.1)
$COMPOSE logs -f backend        # 跟后端日志
$COMPOSE logs --tail=100 caddy  # 路由/证书出问题时看这儿
$COMPOSE logs --tail=100 login  # **口令进不去时先看这儿**(2026-08-11 上午新增的服务)
$COMPOSE restart                # 重启整栈(不需要 --profile ui,VPS 档已经把前端放出来了)
docker stats --no-stream        # 内存吃了多少(4G 机器要常看这个)
df -h                           # 磁盘,镜像很大
```

> ### ✅ 机器重启之后会不会自己回来 —— 这次确认过了(**VPS 实测**)
>
> **不是推的,是真按了两次 `reboot`。**(先说这个,因为下面那几条配置项只能推出
> 「应该回得来」,而「应该」和「真回来了」在上线这件事上不是一回事。)
>
> 第一次在**四个容器**时(basic_auth 时代),第二次在**五个容器齐了之后**。
> **下面记的是第二次的数** —— 它把第一次的结论整个覆盖了。
>
> - **开机 8 秒内五个容器全部 Up**(backend / frontend / login / artifacts / caddy),
>   `caddy` 直接 healthy(= 证书从卷里读到了,**没有重新向 Let's Encrypt 申请**
>   —— 这点很重要,否则每次重启都在消耗签发额度);
> - 两块 swap 按 `/etc/fstab` **自动挂回**,`ufw` 仍 active;
> - 从**外部机器**复验(登录页时代的码):未登录首页 **302**、登录页 **200**、
>   登录拿到 Cookie、带会话首页 **200**、API 无令牌 **401** / 有令牌 **200**、产物 **200**,
>   证书 `notAfter` 与重启前一致;
> - **重启前的 19 条历史对话一条没少** —— 这条同时验证了
>   `./data/langgraph` 那条挂载(见 §4.1 后面那段:没有它,重启/重建就是历史清零)。
>
> 支撑它的三条配置(**源文件核对** + 机器上核过):
>
> - `systemctl is-enabled docker` = **`enabled`**(Docker 自己开机自启);
> - **五个**容器全是 **`restart=unless-stopped`**(当时是四个,login 加进来时也带了这一行,
>   **源文件核对** `docker-compose.vps.yml`);
> - 证书在命名卷 `gyt_caddy_data` 里,不随容器走(见 §6.4)。
>
> 合起来:**VPS 重启之后这套东西会自己回来,不需要人上去 `up` 一遍。**
>
> ⚠️ 一个**没有**跟着恢复的东西:上线时给 `sshd` 设过 `oom_score_adj = -1000`
> (防构建期 OOM killer 把 ssh 连接掐掉,把自己关在门外)。那是写进 `/proc` 的,
> **重启即失效**。日常运行不需要它;下次要在这台机器上做大构建,记得重设。
>
> ⚠️ 但**「重启后回得来」不等于「长期跑得住」**。那次只是一次上线加几次提问,
> **没有连续跑过几天** —— 内存会不会缓慢涨、单进程会不会累积状态,都还不知道(§9)。
> 真放着长期开,记得按 §6.3 隔几小时看一眼用量,并且**用完就关**(§8)。

**⚠️ 改 `Caddyfile` 之后用 `restart`,不要找 `caddy reload`。**
配置里写了 `admin off`(关掉了 Caddy 那个**无鉴权**的 `:2019` 管理端点),
走 admin API 的 reload 已经不可用了 —— 这是刻意的取舍,代价就是改配置要 restart(同样是秒级):

```bash
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e GYT_SITE_ADDRESS=":80" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile   # 先验语法

# ⚠️ 动过 route / handle 的话,validate 不够 —— 必须再看一眼编译出来的路由顺序:
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e GYT_SITE_ADDRESS=<你的域名> \
  caddy:2-alpine caddy adapt --config /etc/caddy/Caddyfile      # forward_auth 必须在业务 handle 之前

$COMPOSE restart caddy
```

**为什么多了 `adapt` 这一步:`validate` 说 Valid 的配置,可以是整站敞开的。** 见 §0.5。

**改了什么 → 要做什么**(**源文件核对**:`docker-compose.dev.yml` 顶部的三档对照 + `Makefile` 注释):

| 改动 | 需要的动作 |
|---|---|
| `backend/src/**.py` | VPS 档没挂源码,**要重建**:`$COMPOSE up -d --build backend` |
| 任何 `prompt.md` | 同上,重建后端 |
| **`backend/auth.py`** | 同上,重建后端(它在镜像里,不是挂载进去的) |
| **`backend/langgraph.json`**(动了 auth 声明) | **必须重建镜像** —— 它没被挂载,容器读的是镜像里那份 |
| `pyproject.toml` / `uv.lock` / `Dockerfile` | 重建后端 |
| `.env` 里**后端**的值(超时、限流、阈值) | `$COMPOSE up -d backend` 即可(重建容器,不重建镜像) |
| **令牌 `GYT_ACCESS_TOKEN` / 域名 `GYT_PUBLIC_ORIGIN`** | **要 `--build` 重建前端** —— 它们烤在浏览器包里 |
| `GYT_SITE_ADDRESS` | `$COMPOSE up -d caddy` |
| **`GYT_LOGIN_HASH`(换口令)** | `$COMPOSE up -d login`(变量挂在 login 服务上,**源文件核对** `docker-compose.vps.yml`)。⚠️ **所有人当场掉线** —— 会话密钥从口令哈希派生,见 §3.1 |
| `Caddyfile` | 先 `caddy validate`(动过 route/handle 再加一次 `caddy adapt`),再 `$COMPOSE restart caddy` |
| **`scripts/serve_login.py` / `scripts/login-page.html`** | 两个都是**只读挂载**进容器的,不在镜像里 → `$COMPOSE restart login` 即可,不用重建 |
| `data/demo/docs/` 里加了新规范 PDF | **重跑步骤 10 建库**(幂等,只嵌新增的那份) |

**更新代码:**

```bash
cd ~/gongyoutong
git pull
$COMPOSE up -d --build
$COMPOSE logs backend | grep -A6 '访问校验\|!!!!!!'   # 每次更新后都确认一次门还锁着
$COMPOSE ps                                          # caddy 那行必须是 healthy
# 规范 PDF 有增删的话,再跑一遍步骤 10
```

---

## 8. 收尾:这台机器该怎么定位

- ✅ **给外部的人自己点点看** —— 这是它存在的理由。
- ✅ **演示机故障时的第四层兜底** —— 但要知道视觉缓存是冷的,速度和现场彩排不一样。
- ❌ **演示当天的主路径** —— 与 D9 决策冲突,理由见 §0.1,别拿它赌。

测试跑完、不再需要的时候,**记得把它关掉**(§6.4 第 2 档)。
一台开着的、带真 API Key 的公网机器,是一笔一直在走的风险。

---

## 9. 这份手册核实到什么程度(逐条)

本仓的红线是「不许写没验证过的事实」,所以把来源摊开。

### ✅ VPS 实测 · 第二轮(2026-08-11 上午,提交 `ccb8d27` 之后,同一台机器、从外部机器打)

**这一轮只测第一道门 —— 因为只有它换了。** 业务链路、令牌层、资源占用那些没有重跑。

| 事实 | 实测值 |
|---|---|
| **未登录访问 `/`** | **302 → `/login`**(basic_auth 时代是 401) |
| **响应头里的 `WWW-Authenticate`** | **已无** —— 浏览器原生弹框不会再出现 |
| **`/login` 是公开的** | **200**,`<title>工友通 · 内测登录</title>` |
| **错口令** | 302 → `/login?e=1`,且**一个 Cookie 都不发** |
| **对口令** | 302 → `/`,`Set-Cookie: gyt_sess=...; Path=/; Max-Age=1209600; HttpOnly; SameSite=Lax; Secure` |
| **带会话访问 `/`** | **200** |
| **未登录打 `/api/*`** | **401** + `{"detail":"登录已过期,请刷新页面重新登录。"}` —— **不是 302**(给前端 fetch 一张 HTML 登录页会让它把 HTML 当 JSON 解析,报出一个和登录毫无关系的错) |
| **未登录取产物 `/artifacts/*`** | **302** |
| **带会话取产物** | **200** |
| **伪造会话全部被拒** | 签名改一位 / 过期时间改大 / 空 / 垃圾 → **全部 302** |
| **两层门仍然独立** | 有会话、但**不带令牌**打 API → **401** |
| **登录接口限流真的在** | 连发 12 次错口令 → **302×4 然后 429×8** |
| **scrypt 单次耗时** | **46ms** —— 这个数同时是**离线爆破成本**和**登录接口的 DoS 杠杆**,两头都不许乱调 |

> ⚠️ **这一轮没有覆盖到的:** caddy 换成 302 判据之后的 healthy 耗时。
> 它的旧数在下面那张表里,**旧数的前提是 basic_auth**,照抄前先看清标注。
>
> ~~重启后自恢复(login 服务没经历过 `reboot`)~~ —— **当天晚些时候补上了**:
> 第二次 `reboot` 是在五个容器齐了之后跑的,login 跟着回来了(见 §7)。
> ~~令牌层的 B)(有会话 + 对令牌 → 200)~~ —— **也补上了**,见上表。

### ✅ VPS 实测 · 第一轮(2026-08-11 凌晨那次真实上线;机器规格见 §0.0)

**⚠️ 这张表里凡是涉及「第一道门返回码」的行,前提都是 `basic_auth`,当天上午已经作废** ——
每一条都标了。其余各行(证书、内存、构建、业务链路、持久化)这次没动,照旧成立。

| 事实 | 实测值 / 怎么核的 |
|---|---|
| **步骤 1~12 的完整链路走得通** | Debian 12 / 2 vCPU / 1.9GB / 30GB,域名 `velactora.com`(Cloudflare **灰云**) |
| **Caddy 自动签发 Let's Encrypt 证书** | `CN=velactora.com`,有效期 **2026-08-10 → 2026-11-08**。**从外部另一台机器**用 `openssl s_client` 独立验的,不是看 Caddy 自己的日志 |
| **域名模式下 `curl http://<VPS_IP>/` 回 308,不是门层该给的码** | 实测 **308**。Caddy 在站点块之外另起了 `remaining_auto_https_redirects` 专管 :80,**带门的站点块根本没被进入** → §3.1。⚠️ 这条**仍然成立**(它跟哪种门无关);变的是对照物 —— 当时是 401,今天是 302,**两个数长得更像了,判法改成看 `Location`** |
| ~~**域名模式的正确自检回 401**~~ | ~~`curl -sk --resolve <域名>:443:<VPS_IP> … https://<域名>/` → 实测 **401**~~ **已作废** —— 换登录页之后同一条命令实测 **302**(见上面第二轮那张表) |
| **`/api/ok` 验不了令牌层** | 过了第一道门、**不带** `X-Api-Key` 打 `https://<域名>/api/ok` → **200**。`/ok` 被鉴权框架豁免(后端 healthcheck 就靠这个探活)。**这次差点据此误判「令牌层没生效」** → §3.2。(那次过门用的是 basic auth 口令;结论跟第一道门长什么样无关) |
| **受保护路由的实际返回码** | `POST /api/threads`:无令牌 **401** / 错令牌 **401** / 对令牌 **200**;`POST /api/threads/search`:无令牌 **401** / 对令牌 **200** |
| **无令牌时后端回的原文** | `{"detail":"访问被拒绝,请联系发你链接的人。"}` —— 中文人话,无堆栈 |
| **1.9GB 内存机器上查询时加载 BGE-M3 不 OOM** | 每 5 秒采样:可用 745→381→454MB;backend 容器 430→846→**955MB**(峰值)。权重是 mmap 进来的、算可回收文件页缓存,所以峰值不是 2.2GB。该机 swap 共 8GB(原有 4G + 新增 4G),全场 `dmesg \| grep -ci "out of memory"` = **0** → §1.1 |
| **`preflight_vps.sh` 的内存闸会把这台机器判 FATAL** | 脚本第 233-236 行要求 ≥3500MB,该机 1966MB → `exit 1`。⚠️ 它给的理由是「**建库那一步**会被 OOM」,而那次**根本没在 VPS 上跑过 ingest** —— 那个具体后果既没被验证也没被证伪 |
| **`data/chroma` 可以从开发机整个搬过去** | 搬过去的是 **12MB**,VPS 上一次 `ingest` 都没跑 → 步骤 10 |
| **`rsync -a` 会把 `data/` 属主搞错** | macOS 源端 uid **501:staff** 被原样带过去,而容器跑 uid **10001**(`backend/Dockerfile:288`)→ 台账 / 产物注册表 / LLM 缓存全写不进去。**被 `preflight_vps.sh` 第 ④ 组的「data/ 属主是 10001」拦下** → 步骤 5 |
| **构建耗时与体积** | backend 镜像 **6.37GB** / 约 6 分半(含下 BGE-M3 权重);frontend 镜像 **1.93GB** / 约 3 分半,其中 `next build` 本身 **75.9 秒**;`docker builder prune -af` 回收 **9.34GB**;30GB 盘最终占用约 **20GB** → 步骤 9 |
| **令牌真的烘进了浏览器包** | 前端构建产物 `/app/.next/static/chunks/app/page-*.js` 里**精确匹配到完整的 64 位令牌**,以及 `https://velactora.com/api`。**这推翻了手册原来「这条路已知目前接不上」的说法** → 步骤 11 |
| **排期链路真落库** | 提问「记一条:明天上午复核 3-5 轴柱距」→ `transfer_to_schedule` → **直查 SQLite**:`{'id': 1, 'title': '复核 3-5 轴柱距', 'due_date': '2026-08-12', 'status': 'open', 'created_at': '2026-08-11T02:24:18+08:00'}`。时区 `+08:00` 正确,「明天」算对了 |
| **知识链路会认怂而不是编造** | 提问「脚手架的连墙件规范上是怎么要求的?」→ `transfer_to_knowledge` → 检索两次都只命中防火规范 → **如实回答「知识库里查不到」并给出 JGJ 130 作为去处,没有编造条文号** |
| **确实是流式推送** | schedule 那次的 SSE 事件分布:`messages/metadata` ×6、`messages/complete` ×6、`updates` ×3、`metadata` ×1 |
| **非视觉链路端到端耗时** | knowledge 一次 **19 秒**(含首次加载模型);schedule 一次 **6 秒**。并发 1 |
| **证书在命名卷里、重建不丢** | `gyt_caddy_data` / `gyt_caddy_config` 存在,证书在 `/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/velactora.com/velactora.com.crt` |
| **重启之后自己回得来** | **真按了两次 `reboot`**,第二次是在五个容器齐了之后:开机 **8 秒内五个容器全 Up**、caddy 直接 healthy(证书从卷里读,**没重新申请**)、两块 swap 按 fstab 自动挂回、ufw 仍 active;从外部复验 302/200/401/200/200 全对、证书 `notAfter` 未变;**重启前的 19 条历史对话一条没少**(验证了 `./data/langgraph` 那条挂载)。支撑配置:`systemctl is-enabled docker` = `enabled`,五个容器全是 `restart: unless-stopped` |
| **caddy 20 秒内 healthy** | `docker compose up -d` 之后 20 秒内。由于它的判据要走 HTTPS,healthy 同时意味着证书已就绪。⚠️ 当时判据是「拿到 **401**」;上午改成「拿到 **302**」之后**没有重新计时** |
| **HF tree cache 那条 "corrupted" 警告不影响功能** | 真实原因是同一行里的 `Permission denied`(`/opt/hf` 属主与运行用户不一致)。**带着这条警告跑通的**,模型照常加载、检索照常返回。未修 → §4.7 |

> ⚠️ **整张表是一次、一台机器、并发 1 的样本。** 它证明「这条路走得通」,不证明「任何机器上都这样」。

### 实测(写手册时真跑过命令、真看过输出)

| 事实 | 怎么核的 |
|---|---|
| ~~`caddy hash-password` 可用,输出 bcrypt(`$2a$14$…`)~~ | ~~真跑 `docker run --rm caddy:2-alpine caddy hash-password --plaintext '…'`~~ —— **2026-08-11 上午起这条命令在本项目里没用了**(口令哈希换成 `hashlib.scrypt`,由 `scripts/make_login_hash.py` 生成)。`caddy version` = **v2.11.4** 这半仍然成立 |
| **Compose 会吃掉 env 文件里的 `$`,`$$` 才对** | 造最小复现:`env_file` 写单 `$` → 容器里 `env` 打出 `$2a$14.PJQ8Ljek…`(`$JMw9EJgAEKPOoH` 段消失);写 `$$` → 完整正确。**这是本手册价值最高的一条**,而且**换成 scrypt 之后照样成立** —— scrypt 哈希同样用 `$` 分段 |
| ~~`${H//\$/\$\$}` 这条加倍命令有效~~ | ~~用真实 caddy 哈希跑过,输出 `$$2a$$14$$k1Q5…`~~ —— 现在不用手工加倍了,`make_login_hash.py` 直接吐加倍好的整行 |
| `openssl rand -hex 32` 不含 `$` | `grep -c '\$'` 回 `0` |
| `ports: !reset []` 能移除基础档发布的端口 | 造最小 compose 双档跑 `docker compose config`,`ports` 确实消失;本机 Compose **v5.1.3** |
| **`/ok` `/info` `/docs` `/openapi.json` `/metrics` 不过鉴权;`/threads/*` `/runs/*` `/assistants/*` 过鉴权** | 读 `langgraph_api` 0.12.0 源码:`api/__init__.py` 的 `unshadowable_meta_routes` / `shadowable_meta_routes` vs `protected_routes`,以及 `server.py` 里只有 `protected_mount` 套了 `middleware_for_protected_routes`。**§3.2「别拿 /ok 验令牌」那个陷阱就是这么发现的** |
| **限流挂的 `@auth.on.threads.create_run` 事件名是对的** | 读源码:`langgraph_runtime_inmem/ops.py` 里 `class Runs(Authenticated): resource = "threads"`,`Runs.put` 内部发 `handle_event(ctx, "create_run", …)`,而且发在 run 落库之前 → 事件确实是 `threads.create_run`,被拦下的请求不花钱 |
| `langgraph_sdk` **0.4.2** 提供 `Auth`;`auth.on` 下有 `assistants/crons/runs/store/threads/value` | 在 `backend/.venv` 里真 import 过 |
| 后端镜像约 **6.38GB** | `docker images` 里 `gyt-backend:dev` 的 DISK USAGE |
| 演示资产体积:docs 17M / photos 8.8M / drawings 64K;共 40 个文件进 git;**没用 git-lfs** | `du -sh` + `git ls-files data/` + 看 `.gitattributes`(只声明 `binary`,无 lfs filter) |
| **后端镜像里没有 `make`,`/app` 下也没有 `Makefile`** | `docker run --rm --entrypoint sh gyt-backend:dev -c 'command -v make; ls /app'` → 回 `NO make`,`/app` 里只有 `Dockerfile README.md auth.py data eval langgraph.json pyproject.toml scripts src uv.lock`。**所以 `docker-compose.vps.yml` 收尾注释里那条 `run --rm backend make build-knowledge` 跑不起来**,步骤 10 用的是 `python -m …` 那条 |

### 源文件核对(读了代码/配置,没跑)

| 事实 | 出处 |
|---|---|
| 站点变量 `GYT_SITE_ADDRESS`(`:80` 或域名)、`GYT_PUBLIC_ORIGIN`;**整个站点块包在一个 `route` 里**、`/login*` 与 `/logout*` 公开、其余全过 `forward_auth login:8790 { uri /_auth/verify }`;`handle_path /api/*` 会剥前缀;`admin off`;`request_body max_size 128MB`;日志过滤把 `Authorization`/`X-Api-Key`/`Cookie` 替换成 `REDACTED`(全局 logger 和站点 logger **两处都要**);文件末尾「永远不许出现的东西」清单 | `Caddyfile` |
| ~~`GYT_BASIC_AUTH_USER` / `HASH` 两个站点变量;`basic_auth` 是 Caddy v2.8 才改的名字~~ | **2026-08-11 上午已从 `Caddyfile` 移除** —— 口令校验搬到 login 服务。`basic_auth` 那条版本知识本身没错,只是这个文件里已经用不到了 |
| **登录/会话服务的全部行为**:`/login`(GET 发页面、POST 校验)、`/logout`、`/_auth/verify`(204 放行 / 网页 302 / `/api/*` 回 401 JSON);会话 = `v1.<到期unix秒>.<hmac_sha256>`、**14 天**、**会话密钥从口令哈希派生**(⇒ 改口令 = 全员下线);**先验签再看过期**;哈希格式 `scrypt$n$r$p$salt$dk`,n=2^14/r=8/p=1;Cookie 打 `HttpOnly` + `SameSite=Lax`,**`Secure` 只在 `X-Forwarded-Proto: https` 时打**(裸 IP 模式下硬打 Secure 会让 Cookie 存不下来 → 登录成功又马上跳回登录页的死循环);登录限流是**全局一个令牌桶、不按 IP 分**;失败固定睡 0.25s 抹平计时差;**服务端一个字节的用户输入都不回显**(错误态靠 URL 上的 `?e=1` 在前端切);日志只打方法+路径+状态 | `scripts/serve_login.py` |
| 口令生成:交互输入或 `--random` 生成 20 位(字母表去掉 `0/O/o/1/l/I`,因为要发给人手打);**故意不提供 `--password` 这类会进 shell 历史的参数**;输出**已经把 `$` 加倍好**;`SCRYPT_*` 四个参数与 `serve_login.py` **同源,改一处必须改两处** | `scripts/make_login_hash.py` |
| **环境变量文件必须叫 `.env`(由 `.env.vps.example` 复制而来),不能用 `--env-file .env.vps`**;`GYT_ACCESS_TOKEN` / **`GYT_LOGIN_HASH`** / `GYT_PUBLIC_ORIGIN` 是 `${VAR:?}` 硬性前置(没设就 `up` 失败);`GYT_SITE_ADDRESS` 默认 `:80`、`TZ` 默认 `Asia/Shanghai`;backend/frontend 都用 `ports: !reset []`;frontend 用 `profiles: !reset []`(**所以不需要 `--profile ui`**);镜像 tag 换成 `:vps`、容器名 `gyt-backend-vps` / `gyt-frontend-vps` / `gyt-artifacts` / **`gyt-login`** / `gyt-caddy`;caddy 是唯一有 ports 的服务(80/443/443udp)、**没有 env_file**(不碰模型 Key);`NEXT_PUBLIC_API_URL = ${GYT_PUBLIC_ORIGIN}/api`;**caddy healthcheck 断言 302 而不是 200**;caddy `depends_on` login 是 `service_started`(**它在首屏路径上**);`caddy_data` / `caddy_config` 命名卷存证书;`restart: unless-stopped`;建议 2G swap;故意不设 `deploy.resources.limits`;**artifacts 与 login 两个服务同一套形态**(`python:3.12-slim`、uid 10001、挂载全 `:ro`、**一个 ports 都没有**、`PYTHONUNBUFFERED=1`);**五个**服务共用 `x-logging` 锚点(`json-file` / `max-size 10m` / `max-file 3`) | `docker-compose.vps.yml` |
| ~~`GYT_BASIC_AUTH_USER` 默认 `gyt`;`GYT_BASIC_AUTH_HASH` 是硬性前置;caddy healthcheck 断言 **401**;**四个**服务共用 `x-logging`~~ | **2026-08-11 上午全部作废**,替代说法见上一行。⚠️ healthcheck 那条**漏改的表现是 caddy 永远 unhealthy 而站点其实好好的** |
| ~~**`NEXT_PUBLIC_API_KEY` 目前是空转** —— `frontend/Dockerfile` 没有对应的 `ARG`,Docker 只警告 `unused build arg`,构建照样成功但令牌没进包~~ **已不成立,见下一行**(原文保留,因为「Docker 对未声明 arg 只警告」这个机制本身没变,值得记住) | `docker-compose.vps.yml` 的 `frontend.build.args` 注释 |
| **✅ 更正:这条链现在是通的。** `scripts/setup-frontend.sh:313` 有 `ARG NEXT_PUBLIC_API_KEY=` 并写进 build 阶段 `ENV`;同一脚本注册了 `api-key.tsx` 覆盖件让 `getApiKey()` 也读构建期变量(上游那份只读 localStorage);`preflight_vps.sh` 第 ④ 组硬查这个 ARG。**并且 2026-08-11 在构建产物里实测到了令牌** | `scripts/setup-frontend.sh`(Dockerfile 模板 + `apply_override "api-key.tsx"`)+ `scripts/preflight_vps.sh` |
| **上线前自检脚本的六组检查**(① 环境变量 ② 站点/公开地址同源 ③ DNS 与本机公网 IP 比对 ④ 端口/`data/` 属主/前端 ⑤ 配置语法与 `published:` 端口数 ⑥ 内存 ≥3500MB、磁盘 ≥10000MB);只读、不改文件、不起容器 | `scripts/preflight_vps.sh` → 步骤 8.5 |
| **① 组 2026-08-11 上午跟着换了**:读的变量从 `GYT_BASIC_AUTH_HASH` 变成 **`GYT_LOGIN_HASH`**,并**新增一条「必须以 `scrypt$` 开头」**(挡「还留着旧 bcrypt 串」—— 那同样是不报错、口令永远不对);⑤ 组的 `caddy validate` 不再传口令相关环境变量;**⑤ 组数的发布端口总数仍是 3**(login 和 artifacts 一样没有 `ports`) | `scripts/preflight_vps.sh` |
| **`caddy validate` 只验语法,不验路由顺序** —— 顺序要用 `caddy adapt` 打出路由树看。`Caddyfile` 里那段注释记着 2026-08-11 上午的裸奔经过:`handle`/`handle_path` 在 Caddy 的固定指令顺序表里排在 `route` **之前**,把业务 handle 留在 route 外面 = 兜底 handle 先把所有请求吃光 → **未登录访问首页 200、`/login` 是前端 404、没有任何报错、`validate` 说 Valid** | `Caddyfile` 的 route 注释 → §0.5 |
| **③ 组的 DNS 检查在 Cloudflare 橙云下必然判红** —— 它拿 `dig` 结果比对本机公网 IP,橙云返回的是 Cloudflare 的 IP。**这是设计,不是 bug** | `scripts/preflight_vps.sh` 第 ③ 组 → §1.3 |
| **caddy healthcheck 按模式分岔**:裸 IP 探 `http://127.0.0.1:80/`,域名探 `https://<域名>/` 并用 `curl --resolve` 走对 SNI;不能用 busybox 的 `wget`(走 https 不发 SNI) | `docker-compose.vps.yml` 的 caddy `healthcheck` 及其注释 |
| **域名模式下追 `-L` 到 `https://<VPS_IP>/` 会握手失败**:`tlsv1 alert internal error`,curl 的 `%{http_code}` = `000` | `docker-compose.vps.yml` 第 310 行附近的注释块(那里记的是它自己的实测) |
| **故意不 `chown -R` `/opt/hf`**(只读即可;递归 chown 会把上千兆内容整层复制一遍,镜像体积翻倍)—— 这正是 §4.7 那条 `Permission denied` 的来源 | `backend/Dockerfile` |
| `GYT_ACCESS_TOKEN` 设了才开鉴权+限流;缺失时打感叹号横幅;**写进仓库根 `.env` 会让本机 `make dev` 也强制,`live_acceptance.py` 立刻 401**;失败文案「访问被拒绝,请联系发你链接的人。」;限流文案「问得太快啦,请等几秒再发一条。」;429 带 `Retry-After`;cron 创建被拒(返回 `False` → 403);`x-auth-scheme: langsmith` 旁路及两道堵法;令牌桶是进程内内存实现 | `backend/auth.py` |
| `"auth": {"path": "./auth.py:auth", "disable_studio_auth": true}` | `backend/langgraph.json` |
| `access_token` / `rate_limit_burst=20` / `rate_limit_per_minute=20.0` 三个字段,以及 20/20 的定值依据 | `backend/src/gyt/config.py` |
| 后端容器用户 uid 10001、`WORKDIR /app`、`PATH` 含 `/app/.venv/bin`、**无 `ENTRYPOINT`**(所以 `run … python -m …` 直接生效)、`ENV TZ=Asia/Shanghai`、CMD 是单条 `langgraph dev` | `backend/Dockerfile` |
| `healthcheck` 探 `/ok`、`start_period: 120s`、`restart: on-failure:3`、`ports: 127.0.0.1:2024:2024`、frontend 在 `profiles: [ui]`、`NEXT_PUBLIC_*` 必须走 build args | `docker-compose.yml` |
| 建库 CLI 的四种输出与退出码 | `backend/src/gyt/agents/knowledge/ingest.py` 的 `main()` |
| 「容器里别开启动时自动建库」 | `.env.example` + `Makefile` 的 `build-knowledge` 注释 |
| `data/demo/` 不被 gitignore、`frontend/` 被 gitignore | `.gitignore` |
| ~~`ARTIFACT_BASE` 写死 `http://127.0.0.1:8788`~~ → **2026-08-11 改成读编译期变量 `NEXT_PUBLIC_ARTIFACT_BASE`**,默认值仍是那个回环地址(本机开发行为不变);公网由编排传 `${GYT_PUBLIC_ORIGIN}/artifacts`。详见 §4.1 | `scripts/frontend-overrides/{tool-calls,human}.tsx` |
| ~~`serve_artifacts.py` 的 `BIND_HOST` 写死回环~~ → **改成按 `/.dockerenv` 判容器**:宿主机上仍是 `127.0.0.1` 且**依然没有 `--host` 开关**,只有容器内绑 `0.0.0.0`。红线没变松,只是挪到「artifacts 服务不许有 `ports`」这个可被 preflight 自检的不变量上 | `scripts/serve_artifacts.py`、`docker-compose.vps.yml`、`scripts/preflight_vps.sh` |
| `/artifacts/*` 反代路由必须写在兜底 `handle {}` **之前**(Caddy 的 handle 系列按书写顺序择一匹配) | `Caddyfile` |
| 视觉调用 10.1 / 42.9 / 59.7 秒;超时定 150 秒 | `config.py` 的 `llm_timeout_s` 注释(仓库 2026-08-07 的实测记录) |
| 冷启动构建 10~30 分钟、首次建库约 15 分钟 | `Makefile` 的 `e2e` / `build-knowledge` 注释 + `.env.example` |
| 两家 Key 的申请地址 | `.env.example` |
| D9「本地主跑 + 三层兜底」、安全最小集四条 | `docs/W4_跨平台运行方案.md` §7、`TODOS.md` TODO-1 / TODO-7 |
| 演示照片含可识别人脸、公开前要打码 | `TODOS.md` TODO-22 |

### ✅ 原来列在「未在真机验证」里、现在成立的几条(2026-08-11 更正)

**这几条以前明确写着没验过,别再照着旧说法转述。**

| 原来写的 | 现在 |
|---|---|
| 「**在一台真 VPS 上完整走完步骤 1~12 的任何一步。全流程是拼出来的,不是跑出来的。**」 | ✅ 走完了一次,见 §0.0 |
| 「Caddy 自动签发 HTTPS 证书的完整流程」 | ✅ 签成了,证书从外部独立验过(有效期 2026-08-10 → 2026-11-08) |
| 「§3 里**所有的** curl」 | 部分成立:**§3.1 和 §3.2 的 A/B 跑了**;§3.2 C、§3.3、§3.4 **仍然没跑**(见下) |
| 「**4G 内存能不能扛住前端 `next build` 而不 OOM。这是我最不放心的一处。**」 | 部分成立:**1.9GB 内存 + 8GB swap** 上 `next build` 75.9 秒完成、全场 OOM 计数 0。**「4G 无 swap」那一种仍然没验** |
| 「**前端构建期注入 `NEXT_PUBLIC_API_KEY` 这条路** —— 已知目前接不上」 | ✅ **接上了**,并且在构建产物里实测到了令牌。详见步骤 11 与上面 §9 的更正行 |

#### 2026-08-11 上午,换登录页顺带清掉的两条旧账

| 原来写的 | 现在 |
|---|---|
| 「浏览器会不会把 basic_auth 的凭据自动带到同源的 SSE / `<img>` 上 —— **没在真浏览器上验过**」 | **依据换了,状态没换。** 那条悬案的根子是 basic_auth 靠「浏览器记住口令并自动重发」,而那是个**没验过的行为假设**。换成会话 Cookie 之后,**同源请求带 Cookie 是浏览器的规定动作**,不再依赖那个假设。⚠️ **但仍然没有人用真浏览器打开过这个站点,所以这两条照旧留在下面的未验证清单里** —— 变的只是「依据」那一栏,从「假设」变成「浏览器规范行为」 |
| 「**登录接口没有限流**」——`Caddyfile` 那段注释自己承认「换着花样发错口令能持续消耗 CPU,而 Caddy 官方镜像不带限流模块」 | ✅ **补上了。** login 服务自带令牌桶(全局一个、不按 IP 分,理由同 `backend/auth.py`)。**VPS 实测:连发 12 次错口令 → 302×4 然后 429×8。** 代价说清楚:有人乱试时正常测试的人也会被挡一会儿 |

### ⚠️ 仍然未在真机验证(按标准做法或按代码判据写的)

**上面那次上线覆盖不到的部分,一条都没少。**

- **Windows 侧的任何一步。** 那次全程从 macOS 开发机操作;Windows 上的 SSH、rsync、
  换行符、路径这些都没碰过。
- **多并发。** 整场并发是 **1**。§4.5 说的排队、§1.1 那份内存采样,都建立在「一个人在用」之上。
  **几个人同时提问会怎样,没测过。**
- **长期运行稳定性。** 只是一次上线加几次提问,**没有连续跑过几天**。
  内存会不会缓慢涨、单进程会不会累积状态,都不知道。
  (**日志把盘写满这一条已经堵上了**:2026-08-11 给所有服务都加了
  `logging: json-file / max-size 10m / max-file 3`。当时是**四个**服务、加起来封顶 120MB,
  `docker inspect` 核过真生效。堵之前是 Docker 默认的**无上限**。
  当天上午 login 服务加进来,**用的是同一个 `x-logging` 锚点**(**源文件核对**),
  于是变成**五个 × 30MB = 封顶 150MB** —— ⚠️ **这一份没有再用 `docker inspect` 核过**。
  但**产物目录仍然无上限**:每张测试照片约 350KB 落在 `data/artifacts/` 里,没有清理机制。)
- **证书自动续期。** 首签成功 ≠ 续期成功,两件事走的代码路径不一样。
  这张证书 **2026-11-08 到期**,到那之前谁也不知道。
  (证书在命名卷里、不随容器重建丢失这一条**已经验过**,那是续期能成立的前提,但不是续期本身。)
- **§3.2 C) 的 `x-auth-scheme: langsmith` 后门检查。** 三条里最该跑的一条,那次偏偏没跑。
- **§3.3 的 429 / `Retry-After` / cron 被拒的 403。** 那次**试了两次都没打到桶上**,
  两条死路都记下来,免得下一个人再走一遍:
  - **刷「建线程」没用。** 连发 26 次 `POST /api/threads` 全是 200。
    `backend/auth.py:62` 写明限流只挂在 `@auth.on.threads.create_run` 上,
    **建线程是被刻意排除的**(读操作和线程管理不该被限流打断)。
  - **发畸形 run 也没用**(本想借此不花钱地抽干令牌桶)。连发 26 次缺 `assistant_id` 的
    `POST /api/threads/<id>/runs`,全是 **422**,一次 429 都没有 ——
    **请求体的 JSON schema 校验跑在 auth 处理器之前,压根没消耗令牌。**

  想真验只能发 20+ 次**合法** run,那是要真花钱的,那次没做。
  ⚠️ 但**别据此说限流坏了**:`auth.py:201` 写明鉴权与限流**同开同关**,
  而鉴权那次是实测在工作的(401 全对),所以桶是**armed** 的 ——
  只是 429 那条路在这台机器上没被走过。(本机彩排里走过:20 通过后 429 带 `Retry-After: 3`。)
- **§3.4 里从外部扫 3000 端口那条。** 那次没扫。
  (**2024 那条跑了**:从外部机器 `curl -m 6 http://<VPS_IP>:2024/ok` 超时、
  `%{http_code}` 是 `000`,后端端口确实没对公网开。)
- `curl -fsSL https://get.docker.com | sh` 装 Docker(Docker 官方路径,但没在这台机器上跑)。
- **`fallocate` 挂 swap 那几条命令本身。** 那台机器上确实有 swap(原有 4GB + 新增 4GB = 8GB)
  并且生效了,但**用的是不是手册里这几条命令,没有记录** —— 别把「有 swap」当成「这几条验过了」。
- **真浏览器会不会把会话 Cookie 自动带到同源的 `/api/*` SSE 长连接、以及 `<img src>` 上。**
  **依据比以前硬了,但状态还是没验。**
  > ~~原文:「**浏览器通过 basic_auth 之后**,会不会把凭据自动带到同源的 `/api/*` SSE 长连接上。~~
  > ~~`Caddyfile` 的注释把『同源 ⇒ 自动带』当成设计前提写了下来。」~~
  >
  > 那条之所以是悬案,是因为 basic_auth 靠的是「浏览器**记住口令并自动重发**」——
  > **那是一个我们从没在真浏览器上验过的行为假设**。2026-08-11 上午换成会话 Cookie 之后,
  > **同源请求带 Cookie 是浏览器的规定动作**,`fetch` / `EventSource` / `<img src>` 一律带上,
  > 而且 Cookie 打的是 `SameSite=Lax` —— 它管的是**跨站**,同源不受影响
  > (**源文件核对** `scripts/serve_login.py` 的 `_set_session_cookie`)。

  **但仍然没有人用真浏览器打开过这个站点。** 那次看到的 SSE 事件流(§步骤 12 ③)是 `curl` 打的
  —— 这不是「没留下记录」,是**明确知道不是浏览器**。
  **所以这两条照旧留在这张未验证清单里。别写成「已验证」。**
  **万一前端能打开却一直连不上后端,仍然先怀疑这条。**
- **登录页在真设备上长什么样。** 它刻意**不做深色**(工地师傅在户外太阳底下看手机,深色屏读不了),
  带一个「显示口令」开关(20 位随机串要在手机上手打)—— **这些设计意图没有一条在真手机上看过**。
  (**源文件核对** `scripts/login-page.html`、提交 `ccb8d27` 的说明。)
- **裸 IP 模式下的会话 Cookie。** 代码里 `Secure` 标只在 `X-Forwarded-Proto: https` 时才打,
  正是为了裸 IP(纯 HTTP)那条路 —— **但裸 IP 模式这次和上次都没走过**。
  写坏了的表现是「登录成功然后马上又跳回登录页」的死循环,而且没有任何报错。
- **`:80` 裸 IP 那条路。** 那次走的是域名模式。裸 IP 模式下 §3.1 的两条自检没跑过
  (会话 Cookie 在这条路上的额外风险见上面那一条)。
- **login 服务经历 `reboot` 之后会不会自己回来。** `reboot` 那次机器上还没有这个服务。
  配置上它和另外四个一样是 `restart: unless-stopped`(**源文件核对**),但没验过。
- **caddy healthcheck 判据从 401 换成 302 之后的 healthy 耗时。** 旧数是 20 秒内,没重新计时。
- §6.3 用日志估请求量与真实账单的对应关系。
- ARM 架构机器。
- **`preflight_vps.sh` 内存闸那句「建库那一步会被 OOM 杀掉」。** 那次没在 VPS 上跑 `ingest`
  (`data/chroma` 是搬过去的),所以这个具体后果**既没被验证也没被证伪**(§1.1)。
- 中国大陆 ICP 备案的任何具体规则 —— §1.3 已经明说了「请自己去服务商那儿确认」。
- Cloudflare **橙云**下的实际表现。§1.3 那三条理由里,前两条(证书死锁、SSE 缓冲)是
  **原理说明**,不是我们测出来的;第三条(preflight 判红)是读脚本得出的。
  **那次用的是灰云,橙云下究竟坏成什么样,没试过 —— 也不建议你去试。**

> **令牌进不了浏览器包时的应急办法**(现在这条链是通的,这段只作为兜底保留):
> 在浏览器 F12 控制台里手动种一次,每个浏览器只需一次:
>
> ```js
> localStorage.setItem("lg:chat:apiKey", "<你的令牌>"); location.reload();
> ```
>
> (key 名 `lg:chat:apiKey` 是**源文件核对**过的,见 `frontend/src/lib/api-key.tsx`;
> 前端已经在发 `X-Api-Key` 头,见 `frontend/src/providers/Stream.tsx:57`。)
> ⚠️ **不能靠界面上那个填 API Key 的表单** —— 只要 `NEXT_PUBLIC_API_URL` 和
> `NEXT_PUBLIC_ASSISTANT_ID` 都在构建期给了,那个表单**根本不渲染**
> (`Stream.tsx:184` 的 `if (!finalApiUrl || !finalAssistantId)`)。
> ⚠️ 这条兜底会把令牌交到测试者手上,能接受再用。

### 明确没做的事

- **2026-08-11 凌晨那一轮(补录实测)没有改动任何代码或配置文件,只改了这份文档。**
  与那次上线相关的代码侧改动(`scripts/preflight_vps.sh` 上线前自检、
  `docker-compose.vps.yml` 里按模式分岔的反裸奔注释与 caddy healthcheck)在各自的提交里,
  本文只是**与它们对齐**。哪天两边对不上,**以那两个文件为准**——理由见本节最后一条。
- **2026-08-11 上午那一轮不一样:代码先改,文档后跟。** 改动全在提交 `ccb8d27` 里
  (新增 `scripts/serve_login.py` / `login-page.html` / `make_login_hash.py`,
  改 `Caddyfile` / `docker-compose.vps.yml` / `.env.vps.example` / `scripts/preflight_vps.sh`),
  这份文档是**事后与它对齐**的。同样:两边对不上以代码为准。
- **登录页那一轮只重测了第一道门。** 业务链路、令牌层 B)、资源占用、重启自恢复
  **都没有重跑** —— 上面各表逐条标了哪些数是 basic_auth 时代的。
  **别因为这次门层全绿就以为整套重验过了。**
- `Caddyfile` / `docker-compose.vps.yml` / `backend/auth.py` 的**具体内容**没有抄进手册,
  只写了「它们必须做到什么」和「怎么验它们还在」。
  理由是本仓的「一份真相」规矩:配置的真相在配置文件里,手册再抄一份两边就会漂移 ——
  而**漂移的安全配置比没有配置更危险**(你以为它挡着)。
