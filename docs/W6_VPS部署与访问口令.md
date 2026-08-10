# W6 · VPS 部署与访问口令(给外部测试用)

> 面向:**拿到一台 VPS 的 root、但没参与过这个项目的人**。
> 目标:一小时内把整套东西跑起来,然后把「网址 + 一个口令」发给几个外部测试者。
> 写作日期 2026-08-11 · 分支 `lawrence/state-claim-audit`
>
> **关于「实测」二字。** 本仓最硬的一条规矩是「不许写没验证过的事实」,所以本手册把来源分三档:
>
> - **实测** —— 写手册的人在本机(macOS + Docker Compose v5.1.3)真跑过命令、真看过输出;
> - **源文件核对** —— 从仓库源文件或依赖库源码读出来的,给了文件名;
> - **⚠️ 未在真机验证** —— 没有在一台真 VPS 上走通过,按标准做法写的。
>
> §9 把每一条逐个列了出来。**你照做时哪一步跟手册对不上,先去 §9 看它是哪一档。**

---

## 0. 先读完这一页,再动手

### 0.1 这台 VPS 不是演示主路径

项目的 **D9 决策是「本地主跑 + 三层兜底」**。这份手册服务的是**「让外部的人自己点点看」**,
不是「演示当天靠它」。两个理由,任何一个都够:

- **视觉缓存跟着机器走。** 照片识别的结果缓存在 `data/cache/` 里,是**这台机器**的目录。
  本机焐热的那 22 分钟(30 张照片跑一轮),VPS 上一张都不认 —— 要么在 VPS 上再烧一轮钱焐,
  要么把几百 MB 的 `data/cache` 同步上去。
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

```
公网
  │
  │  ① Caddy:全站访问口令(basic_auth)          ← 给「人」的门,测试者只需要这一个
  ▼
Caddy ── 整份编排里唯一 publish 端口的服务(80 / 443)
  │
  ├── /       ──▶ frontend:3000  ┐  两个都**不写 ports**,
  └── /api/*  ──▶ backend:2024   ┘  只在 docker 内网互通       ← ④ 端口不暴露
                     │
                     │  ② backend/auth.py 校验 X-Api-Key       ← 给「程序」的门(纵深)
                     │  ③ 令牌桶限流:只卡「创建 run」这个动作
                     ▼
                 Agent 真跑
```

**为什么口令和令牌不是重复劳动 —— 它们防的是不同的失效模式:**

| 防线 | 挡住的是 | 它自己失效的方式 |
|---|---|---|
| ① Caddy 口令 | 陌生人根本进不来 | Caddy 配置写错、某条路由漏配 |
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
ls Caddyfile                             # ③ 反代 + 全站口令
ls docker-compose.vps.yml                # ④ VPS 覆盖件
ls .env.vps.example                      # ⑤ VPS 专用环境变量样例
grep -n 'access_token\|rate_limit_burst' backend/src/gyt/config.py   # ⑥ 闸门配置字段
```

**六条里但凡缺一条,就停下。** 说明你 clone 到的是这套安全件落地之前的提交 ——
那份代码**不能上公网**(理由见 §0.2)。去找项目负责人要正确的分支或 tag,别自己动手补。

⚠️ **环境变量的准确名字,一律以 `.env.vps.example` 为准。**
本手册下文写出来的名字取自 `Caddyfile` 和 `backend/src/gyt/config.py`(**源文件核对**),
但样例文件才是这个仓库约定的「那一份真相」,对不上就以它为准。

---

## 1. 先决条件

### 1.1 机器规格

| 项 | 要求 | 为什么 |
|---|---|---|
| CPU / 内存 | **2C4G 起步** | BGE-M3(知识库用的本地 embedding 模型)推理**吃内存**。**4G 是下限,不是舒适区**,能上 8G 就上 8G |
| 磁盘 | **40GB 以上** | 后端镜像本身就很大 —— **实测**本机 `docker images` 里 `gyt-backend:dev` 占 **6.38GB**(2.2GB 的 BGE-M3 权重烤在镜像里)。加上构建缓存、前端镜像、`data/`,20GB 会很紧张 |
| 系统 | Ubuntu 22.04 / 24.04 LTS(x86_64) | 手册里的命令按 Debian 系写。ARM 机器**没试过**,BGE-M3 权重和 CPU 版 torch 在 ARM 上能不能装,**⚠️ 未在真机验证** |
| 网络 | 能出站访问 DeepSeek / Moonshot / HuggingFace / GitHub / npm | 构建期要下 2.2GB 权重和前端依赖,运行期要调两家模型 |

> **内存不够的典型症状不是报错,是「构建到一半 OOM 被杀」。**
> 尤其前端那步(Next.js 生产构建)在 4G 机器上有风险。
> 保险做法是先挂 swap 再构建(`docker-compose.vps.yml` 的收尾注释建议 **2G 起**,
> 理由是「BGE-M3 加载权重那一下是尖峰,有 swap 兜着就是慢几秒,没有就是 OOM」;
> 磁盘够的话给 4G 更稳)—— 命令见步骤 1,**⚠️ 未在真机验证**(本机是 macOS,没法验)。
>
> 另外注意 `docker-compose.vps.yml` **故意没有写 `deploy.resources.limits`**:
> 在一台 4G 的机器上给唯一一个吃内存的服务再加内存上限,只是把「慢」变成「被 kill」。
> 这台 VPS 上还跑别的东西的话再回来加,加之前先量一下:`docker stats gyt-backend-vps`。

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

### 步骤 6 · 生成两个秘密

#### 秘密一:给人的访问口令(Caddy `basic_auth`)

```bash
docker run --rm caddy:2-alpine caddy hash-password \
  --algorithm bcrypt --bcrypt-cost 12 --plaintext '<你想好的口令>'

> ⚠️ **`--bcrypt-cost 12` 不能省。** `caddy hash-password` 不指定时默认 **cost 14**,
> 而 `basic_auth` 排在所有路由之前 —— 它是整套部署里**唯一不需要任何凭据就能触发的重计算**,
> 而且缓存只对**成功**的凭据生效,换着花样发错口令每一发都是冷校验。
> 2026-08-11 在真 Caddyfile 上横向实测:cost 12 冷校验 0.470s、cost 14 是 1.268s ——
> 差 2.7 倍。2 核机器上一个循环就能把站点打到没响应。
> (`Caddyfile` 和 `.env.vps.example` 里都是 12,这里以前漏了,三份对不上。)
```

**实测**(本机 `caddy:2-alpine`,`caddy version` = v2.11.4)输出形如:

```
$2a$14$JMw9EJgAEKPOoH.PJQ8Ljek.euyS7iQG4ls/c3mRxtSXyxKQ17g8m
```

`$2a$` 开头说明它是 **bcrypt**。这很重要:`Caddyfile` 里那行写的是
`basic_auth bcrypt { ... }`,第二个参数是**算法声明**,Caddy 按它去校验,
**不会从哈希串自己嗅探**。拿别的算法(比如 argon2id)的哈希配这一行,
结果**不是报错,是口令永远对不上**(一直 401)。用上面这条命令生成就不会错。

> ### ⚠️⚠️ 这里有一个会让你查一整晚的坑:bcrypt 哈希里的 `$`
>
> 上面那串哈希里有 **3 个 `$`**。而 **Docker Compose 会把它读到的所有 env 文件里的 `$` 当变量展开**。
>
> **本机实测**(在容器里 `env | grep ^HASH=` 打出来的真实值):
>
> | env 文件里写的 | 容器里实际拿到的 |
> |---|---|
> | `HASH=$2a$14$JMw9EJgAEKPOoH.PJQ8Ljek…` | `HASH=$2a$14.PJQ8Ljek…` ← **`$JMw9EJgAEKPOoH` 被整段吃掉** |
> | `HASH=$$2a$$14$$JMw9EJgAEKPOoH.PJQ8Ljek…` | `HASH=$2a$14$JMw9EJgAEKPOoH.PJQ8Ljek…` ← **正确** |
>
> 被吃掉的哈希**不报任何错**,Caddy 照常起来,只是**你输什么口令都进不去** ——
> 而你会去怀疑口令打错了、怀疑 Caddy 配置、怀疑浏览器缓存,方向全错。
>
> **规矩:写进 env 文件时,把每一个 `$` 写成 `$$`。**
> 别用手数,用下面这条命令一次做完(**本机实测**过,含真实 caddy 哈希):

```bash
cd ~/gongyoutong

PW='<你想好的口令>'   # ⚠️ 一定用单引号。双引号里 shell 自己就会先吃掉一部分 $
H="$(docker run --rm caddy:2-alpine caddy hash-password \
      --algorithm bcrypt --bcrypt-cost 12 --plaintext "$PW")"
echo "原样(万一要填进 Caddyfile 字面量,用这个):$H"
echo "写进 env 文件的样子(每个 \$ 已加倍):${H//\$/\$\$}"
```

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
| `GYT_BASIC_AUTH_HASH` | 步骤 6 秘密一的 **`$` 加倍版** | **`up` 当场失败并打中文**。⚠️ 见上面那个坑 |
| `GYT_PUBLIC_ORIGIN` | 对外完整地址,如 `https://gyt.example.com` 或 `http://203.0.113.10` | **`up` 当场失败并打中文** |
| `GYT_SITE_ADDRESS` | `你的域名` 或 `:80` | 有默认值 `:80`(纯 HTTP),见 §1.3 |
| `GYT_BASIC_AUTH_USER` | 用户名 | 有默认值 `gyt` |
| `TZ` | 一般不动 | 有默认值 `Asia/Shanghai` |
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
  -e GYT_BASIC_AUTH_USER=gyt \
  -e GYT_BASIC_AUTH_HASH='$2a$14$V8/gFXj/yURxTSO0eV37Te7fHfXQ4kwizuVBbvVQskVnVL3iqwD2S' \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

(这条命令抄自 `Caddyfile` 自己的注释,**源文件核对**。里面那串 hash 是注释里的公开示例值,
不是任何真实口令 —— 这一步只验语法,不验口令。三个环境变量必须给,
否则 `{$VAR}` 会替换成空串、站点地址为空直接报错。)

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

⚠️ **第一次构建很久。** 镜像要下 2.2GB 的 BGE-M3 权重再烤进去,
仓库里对冷启动构建的记载是 **10~30 分钟**(`Makefile` 的 `e2e` 目标注释,**源文件核对**;
本手册没在 VPS 上计时过,**⚠️ 未在真机验证**)。

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

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build

docker compose ps      # gyt-backend-vps / gyt-frontend-vps / gyt-caddy 三个都要在
```

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

1. 弹出**浏览器原生的登录框**,标题写着「工友通 GYT 内测」→ 输用户名口令 → 进入聊天界面。
   **没弹框就是 ① 没生效,回 §3.1。**
2. 发一句纯文本:**「明天要绑扎钢筋」** → 应该看到 schedule 记了一条任务。
3. 传一张工地照片 → 等 **10~60 秒**(视觉调用就是这么慢,见 §4.2)→ 应该给出违规项清单。
4. 再问 **「规范里对消防车道宽度是怎么要求的?」** → 应该给出**带页码**的条文出处。
   **如果它说「规范里查不到」,几乎可以肯定是步骤 10 没做或者做失败了。**

---

## 3. 验证清单:四层是不是真的都在

**做完这四条再把地址发出去。** 命令在**你自己的电脑**上跑(除非另有说明)。

### 3.1 ① 口令拦得住

```bash
# 不带口令 —— 期望 401
curl -s -o /dev/null -w '%{http_code}\n' https://<你的域名>/

# 带口令 —— 期望 200
curl -s -o /dev/null -w '%{http_code}\n' -u '<用户名>:<口令>' https://<你的域名>/
```

第一条回 200 = **门是开的,立刻下线**(§6.4)。

> 顺带说一件设计得很聪明、你应该知道的事:**caddy 容器的 healthcheck 断言的是「返回 401」,
> 不是「返回 200」**(**源文件核对** `docker-compose.vps.yml`)。
> 401 同时证明两件事:Caddy 活着、**并且口令那道门是开着的**。
> 所以哪天 `basic_auth` 被误删或写错位置、站点变成 200 全放行,
> **caddy 容器会直接变 unhealthy**,而不是高高兴兴地报健康。
> 也就是说 `docker compose ps` 里 caddy 那一行的 `(healthy)`,本身就是一条持续的口令检查。

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

正确的验法(**必须用受保护路由**;`/api` 前缀来自 `Caddyfile` 的 `handle_path /api/*`,
它会自动剥掉前缀再转给后端,**源文件核对**):

```bash
# A) 过了口令、不带令牌 —— 期望 401
curl -s -u '<用户名>:<口令>' -X POST https://<你的域名>/api/threads/search \
  -H 'Content-Type: application/json' -d '{"limit":1}' -w '\n[%{http_code}]\n'
# 响应体里应该只有一句中文:访问被拒绝,请联系发你链接的人。
# ⚠️ 它对「没带头 / 带了空串 / 令牌不对」回的是**同一句话**,这是故意的 ——
#    任何差异都是送给爆破脚本的信号。真实原因只进服务端日志。

# B) 过了口令、带正确令牌 —— 期望 200
curl -s -o /dev/null -w '%{http_code}\n' -u '<用户名>:<口令>' \
  -X POST https://<你的域名>/api/threads/search \
  -H 'Content-Type: application/json' -H 'X-Api-Key: <你的令牌>' -d '{"limit":1}'
```

**C) 还要验一条后门 —— 这条最容易被漏:**

```bash
# 不带令牌,但加一个 x-auth-scheme: langsmith 头 —— 期望仍然 401
curl -s -u '<用户名>:<口令>' -X POST https://<你的域名>/api/threads/search \
  -H 'Content-Type: application/json' -H 'x-auth-scheme: langsmith' \
  -d '{"limit":1}' -w '\n[%{http_code}]\n'
```

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
    -u '<用户名>:<口令>' -X POST https://<你的域名>/api/runs/stream \
    -H 'Content-Type: application/json' -H 'X-Api-Key: <你的令牌>' -d "$BODY"
done
```

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
curl -s -o /dev/null -w '%{http_code}\n' -u '<用户名>:<口令>' \
  -X POST https://<你的域名>/api/runs/crons \
  -H 'Content-Type: application/json' -H 'X-Api-Key: <你的令牌>' \
  -d '{"assistant_id":"gyt","schedule":"*/1 * * * *","input":{"messages":[]}}'
```

拦它的理由:一个 cron 是「创建一次、以后自己反复跑 run」,而 cron 触发的 run
**未必罩得进那只令牌桶** —— 等于绕过限流开了一条长期烧钱的管子。
来测试的人不需要定时任务,拦掉的代价是零。(**源文件核对**:`backend/auth.py` 的 `deny_cron_create`。)

⚠️ 本节所有 curl **未在真机验证**(没有 VPS)。命令是照着代码里的判据写的,
但「实际打过去是不是这个码」我们没跑过。

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

### 4.1 ⚠️ 巡检记录卡片和历史照片,在外网点不开

聊天界面里那张「巡检记录」卡片(以及历史消息里的照片缩略图)指向的地址是
**写死的 `http://127.0.0.1:8788`**(**源文件核对**:`scripts/frontend-overrides/tool-calls.tsx`
和 `human.tsx` 里的 `ARTIFACT_BASE` 常量)。

那个端口由 `make serve-artifacts` 起的静态服务提供,而它**只绑 `127.0.0.1`,连命令行开关都没留**
(`scripts/serve_artifacts.py` 的 `BIND_HOST` 常量,注释里写明「想突破得改代码、得过 review」)——
因为那个目录里是**工地现场照片和巡检记录,含可识别人脸**。

**对外部测试者的实际影响:** 他们浏览器里的 `127.0.0.1:8788` 指的是**他们自己的电脑**,
所以卡片点开是空的。

**结论:巡检记录**在 VPS 上**能生成、能看到编号,但下载链接点不开。**
这是设计使然,不是故障。要把生成的 docx 拿出来给人看,在 VPS 上:

```bash
ls -R ~/gongyoutong/data/artifacts | tail -20
# 然后从你自己的电脑上取:
# scp root@<你的VPS>:~/gongyoutong/data/artifacts/<日期>/<文件名> .
```

**这一条要提前告诉测试者**(§5 的话术里已经写进去了),否则一定会被当成 bug 报回来。

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

### 4.6 上传体积上限有两道,报错长相不一样

- **应用层**(`GYT_PHOTO_MAX_MB` / `DOCUMENT` / `DRAWING`)超了 → 给一句**看得懂的中文**。
- **Caddy 层**(`request_body max_size 128MB`)超了 → 一个干巴巴的 **413**。

128MB 这个数是按 `GYT_DRAWING_MAX_MB=64` 算出来的(base64 把体积撑到 4/3 → 约 85MB,
再留余量)。**改了 `GYT_DRAWING_MAX_MB` 就要回 `Caddyfile` 重算这一行**,
否则表现是「传大图纸必失败」,而报错来自 Caddy、不是那句写好的中文提示。
(**源文件核对**:`Caddyfile` 的 `request_body` 段。)

---

## 5. 给测试者的话术(可以直接整段转发)

> **【工友通 · 建筑工地 AI 助手】内测邀请**
>
> 网址:https://<你的域名>
> 打开后浏览器会弹一个登录框(标题写着「工友通 GYT 内测」):
>   用户名:`<用户名>`
>   口令:`<口令>`
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
> - 📄 巡检记录**能生成、能看到编号**,但**页面上那个下载链接在你这边点不开**(已知限制)。
>   需要那份 Word 文件的话找我要,我从服务器上取给你。
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
>   (如果你看到「访问被拒绝,请联系发你链接的人。」,说明凭据出了问题,找我。)

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

`Caddyfile` 已经配了日志过滤,**把 `Authorization` / `X-Api-Key` / `Cookie` 三个头删掉再落盘**
(**源文件核对**)—— 所以 `docker compose logs caddy` 的输出可以放心截图问人,
不会顺手把口令一起发出去。**别去掉那段过滤。**

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

⚠️ **第 2 档下线时别加 `-v`。** `docker compose down -v` 会连命名卷一起删,
而 Caddy 的证书和 ACME 账号密钥就在 `caddy_data` 卷里。
删了要重新申请,而 Let's Encrypt 对同一域名有签发频率限制(一周几次)——
反复重建足够把自己关在门外,现象是「HTTPS 突然就申请不下来了」。
(**源文件核对**:`docker-compose.vps.yml` 的 `caddy_data` 卷注释。)

---

## 7. 日常运维小抄

```bash
cd ~/gongyoutong
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vps.yml"

$COMPOSE ps                     # 谁在跑、健不健康(caddy 那行 healthy = 口令门还在,见 §3.1)
$COMPOSE logs -f backend        # 跟后端日志
$COMPOSE logs --tail=100 caddy  # 口令进不去时先看这儿
$COMPOSE restart                # 重启整栈(不需要 --profile ui,VPS 档已经把前端放出来了)
docker stats --no-stream        # 内存吃了多少(4G 机器要常看这个)
df -h                           # 磁盘,镜像很大
```

**⚠️ 改 `Caddyfile` 之后用 `restart`,不要找 `caddy reload`。**
配置里写了 `admin off`(关掉了 Caddy 那个**无鉴权**的 `:2019` 管理端点),
走 admin API 的 reload 已经不可用了 —— 这是刻意的取舍,代价就是改配置要 restart(同样是秒级):

```bash
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e GYT_SITE_ADDRESS=":80" -e GYT_BASIC_AUTH_USER=gyt \
  -e GYT_BASIC_AUTH_HASH='$2a$14$V8/gFXj/yURxTSO0eV37Te7fHfXQ4kwizuVBbvVQskVnVL3iqwD2S' \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile   # 先验语法
$COMPOSE restart caddy
```

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
| `GYT_SITE_ADDRESS` / `GYT_BASIC_AUTH_*` | `$COMPOSE up -d caddy` |
| `Caddyfile` | 先 `caddy validate`,再 `$COMPOSE restart caddy` |
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

### 实测(写手册时真跑过命令、真看过输出)

| 事实 | 怎么核的 |
|---|---|
| `caddy hash-password` 可用,输出 bcrypt(`$2a$14$…`) | 真跑 `docker run --rm caddy:2-alpine caddy hash-password --plaintext '…'`;`caddy version` = **v2.11.4** |
| **Compose 会吃掉 env 文件里的 `$`,`$$` 才对** | 造最小复现:`env_file` 写单 `$` → 容器里 `env` 打出 `$2a$14.PJQ8Ljek…`(`$JMw9EJgAEKPOoH` 段消失);写 `$$` → 完整正确。**这是本手册价值最高的一条** |
| `${H//\$/\$\$}` 这条加倍命令有效 | 用真实 caddy 哈希跑过,输出 `$$2a$$14$$k1Q5…` |
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
| 站点变量 `GYT_SITE_ADDRESS`(`:80` 或域名)、`GYT_BASIC_AUTH_USER` / `HASH`、`GYT_PUBLIC_ORIGIN`;`handle_path /api/*` 会剥前缀;`admin off`;`request_body max_size 128MB`;日志过滤删 `Authorization`/`X-Api-Key`/`Cookie`;`basic_auth` 是 Caddy v2.8 才改的名字 | `Caddyfile` |
| **环境变量文件必须叫 `.env`(由 `.env.vps.example` 复制而来),不能用 `--env-file .env.vps`**;`GYT_ACCESS_TOKEN` / `GYT_BASIC_AUTH_HASH` / `GYT_PUBLIC_ORIGIN` 是 `${VAR:?}` 硬性前置(没设就 `up` 失败);`GYT_SITE_ADDRESS` 默认 `:80`、`GYT_BASIC_AUTH_USER` 默认 `gyt`、`TZ` 默认 `Asia/Shanghai`;backend/frontend 都用 `ports: !reset []`;frontend 用 `profiles: !reset []`(**所以不需要 `--profile ui`**);镜像 tag 换成 `:vps`、容器名 `gyt-backend-vps` / `gyt-frontend-vps` / `gyt-caddy`;caddy 是唯一有 ports 的服务(80/443/443udp)、**没有 env_file**(不碰模型 Key);`NEXT_PUBLIC_API_URL = ${GYT_PUBLIC_ORIGIN}/api`;**caddy healthcheck 断言 401 而不是 200**;`caddy_data` / `caddy_config` 命名卷存证书;`restart: unless-stopped`;建议 2G swap;故意不设 `deploy.resources.limits` | `docker-compose.vps.yml` |
| **`NEXT_PUBLIC_API_KEY` 目前是空转** —— `frontend/Dockerfile` 没有对应的 `ARG`,Docker 只警告 `unused build arg`,构建照样成功但令牌没进包 | `docker-compose.vps.yml` 的 `frontend.build.args` 注释 + `frontend/Dockerfile` 实际内容 |
| `GYT_ACCESS_TOKEN` 设了才开鉴权+限流;缺失时打感叹号横幅;**写进仓库根 `.env` 会让本机 `make dev` 也强制,`live_acceptance.py` 立刻 401**;失败文案「访问被拒绝,请联系发你链接的人。」;限流文案「问得太快啦,请等几秒再发一条。」;429 带 `Retry-After`;cron 创建被拒(返回 `False` → 403);`x-auth-scheme: langsmith` 旁路及两道堵法;令牌桶是进程内内存实现 | `backend/auth.py` |
| `"auth": {"path": "./auth.py:auth", "disable_studio_auth": true}` | `backend/langgraph.json` |
| `access_token` / `rate_limit_burst=20` / `rate_limit_per_minute=20.0` 三个字段,以及 20/20 的定值依据 | `backend/src/gyt/config.py` |
| 后端容器用户 uid 10001、`WORKDIR /app`、`PATH` 含 `/app/.venv/bin`、**无 `ENTRYPOINT`**(所以 `run … python -m …` 直接生效)、`ENV TZ=Asia/Shanghai`、CMD 是单条 `langgraph dev` | `backend/Dockerfile` |
| `healthcheck` 探 `/ok`、`start_period: 120s`、`restart: on-failure:3`、`ports: 127.0.0.1:2024:2024`、frontend 在 `profiles: [ui]`、`NEXT_PUBLIC_*` 必须走 build args | `docker-compose.yml` |
| 建库 CLI 的四种输出与退出码 | `backend/src/gyt/agents/knowledge/ingest.py` 的 `main()` |
| 「容器里别开启动时自动建库」 | `.env.example` + `Makefile` 的 `build-knowledge` 注释 |
| `data/demo/` 不被 gitignore、`frontend/` 被 gitignore | `.gitignore` |
| `ARTIFACT_BASE = "http://127.0.0.1:8788"` 写死在两个覆盖件里;`serve_artifacts.py` 的 `BIND_HOST` 写死回环、**没有 `--host` 开关** | `scripts/frontend-overrides/{tool-calls,human}.tsx`、`scripts/serve_artifacts.py` |
| 视觉调用 10.1 / 42.9 / 59.7 秒;超时定 150 秒 | `config.py` 的 `llm_timeout_s` 注释(仓库 2026-08-07 的实测记录) |
| 冷启动构建 10~30 分钟、首次建库约 15 分钟 | `Makefile` 的 `e2e` / `build-knowledge` 注释 + `.env.example` |
| 两家 Key 的申请地址 | `.env.example` |
| D9「本地主跑 + 三层兜底」、安全最小集四条 | `docs/W4_跨平台运行方案.md` §7、`TODOS.md` TODO-1 / TODO-7 |
| 演示照片含可识别人脸、公开前要打码 | `TODOS.md` TODO-22 |

### ⚠️ 未在真机验证(手上没有 VPS,这些是按标准做法或按代码判据写的)

- **在一台真 VPS 上完整走完步骤 1~12 的任何一步。全流程是拼出来的,不是跑出来的。**
- §3 里**所有的 curl**。命令是照着代码判据写的,但「打过去实际回哪个码」没跑过。
- `curl -fsSL https://get.docker.com | sh` 装 Docker(Docker 官方路径,但我们没在这台机器上跑)。
- `fallocate` 挂 swap 那几条。
- **4G 内存能不能扛住前端 `next build` 而不 OOM。这是我最不放心的一处。**
- Caddy 自动签发 HTTPS 证书的完整流程;`:80` 裸 IP 那条路。
- **浏览器通过 basic_auth 之后,会不会把凭据自动带到同源的 `/api/*` SSE 长连接上。**
  `Caddyfile` 的注释把「同源 ⇒ 自动带」当成设计前提写了下来,理论上也确实如此,但没实测。
  **万一前端能打开却一直连不上后端,先怀疑这条。**
- §6.3 用日志估请求量与真实账单的对应关系。
- ARM 架构机器。
- **前端构建期注入 `NEXT_PUBLIC_API_KEY` 这条路。**
  ⚠️ 这一条不是「没验证」,是**已知目前接不上**:写手册时 `frontend/Dockerfile`
  只声明了 `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_ASSISTANT_ID` 两个 ARG(**源文件核对**),
  而 `getApiKey()` 只读 localStorage。`docker-compose.vps.yml` 已经把这个 build arg 传下去了,
  但 Docker 对未声明的 arg 只警告一句就过 —— 所以在 `scripts/setup-frontend.sh` 的
  Dockerfile 模板补上 `ARG NEXT_PUBLIC_API_KEY` 之前,**令牌不会进浏览器包**。
  现象是网页能开,但一提问就回「访问被拒绝,请联系发你链接的人。」
  检查命令见步骤 11。应急办法是在浏览器 F12 控制台里手动种一次,每个浏览器只需一次:
  ```js
  localStorage.setItem("lg:chat:apiKey", "<你的令牌>"); location.reload();
  ```
  (key 名 `lg:chat:apiKey` 是**源文件核对**过的,见 `frontend/src/lib/api-key.tsx`;
  前端已经在发 `X-Api-Key` 头,见 `frontend/src/providers/Stream.tsx:57`。)
  ⚠️ 注意**不能靠界面上那个填 API Key 的表单** —— 只要 `NEXT_PUBLIC_API_URL` 和
  `NEXT_PUBLIC_ASSISTANT_ID` 都在构建期给了,那个表单**根本不渲染**
  (`Stream.tsx:184` 的 `if (!finalApiUrl || !finalAssistantId)`)。
- 中国大陆 ICP 备案的任何具体规则 —— §1.3 已经明说了「请自己去服务商那儿确认」。

### 明确没做的事

- **没有改动任何代码文件。** 这份手册只是文档。
- `Caddyfile` / `docker-compose.vps.yml` / `backend/auth.py` 的**具体内容**没有抄进手册,
  只写了「它们必须做到什么」和「怎么验它们还在」。
  理由是本仓的「一份真相」规矩:配置的真相在配置文件里,手册再抄一份两边就会漂移 ——
  而**漂移的安全配置比没有配置更危险**(你以为它挡着)。
