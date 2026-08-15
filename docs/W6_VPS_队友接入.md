# 工友通 VPS · 队友接入说明

> 这份是给**第二个人**看的:怎么连上线上那台机器、上面有什么、哪些事不能干。
> 2026-08-11 写,对应的线上站点是 <https://velactora.com>。
>
> ⚠️ **这个文件不含任何密码、令牌、私钥。** 需要的秘密都在服务器上,下面写了在哪儿取。
> 别往这个文件里粘任何 Key —— 它进 git。

---

## 0. 一句话现状

一台 Debian 12 的小机器(2 vCPU / 1.9GB 内存 / 30GB 盘),上面跑着五个容器:

```
公网 :80/:443
      │
   ┌──┴───┐  整份编排里唯一对公网开口的服务
   │ caddy │  ① 登录页 + 会话 Cookie(forward_auth → login)
   └──┬───┘  ② HTTPS(Let's Encrypt,自动续期)
      │  docker 内网,宿主机上看不到端口
      ├── /login, /logout  ──▶ login      口令校验、发会话
      ├── /api/*           ──▶ backend    LangGraph,再校验 X-Api-Key
      ├── /artifacts/*     ──▶ artifacts  工地照片 / 巡检记录 docx(只读)
      └── /                ──▶ frontend   agent-chat-ui
```

**backend / frontend / login / artifacts 一个对外端口都没有**,只有 caddy 有。
这是本项目最硬的一条红线,理由见 §4。

---

## 1. 怎么连上去(三步,前两步要两个人配合)

### 第 1 步 · 你(队友)生成一把钥匙,把**公钥**发给 Lawrence

```bash
# 在你自己的电脑上。已经有 ~/.ssh/id_ed25519.pub 就跳过这步
ssh-keygen -t ed25519 -C "你的邮箱"

# 把**公钥**内容发给 Lawrence(微信/邮件都行,公钥不是秘密)
cat ~/.ssh/id_ed25519.pub
```

⚠️ **只发 `.pub` 结尾的那个。** 没有 `.pub` 的那份是私钥,任何情况下都不发给任何人。

### 第 2 步 · Lawrence 放行(一条命令)

```bash
# 在 Lawrence 的电脑上执行,把 <公钥> 换成队友发来的那一整行
ssh animego-old "echo '<公钥>' >> /home/cfan/.ssh/authorized_keys"

# 确认加上了
ssh animego-old "wc -l < /home/cfan/.ssh/authorized_keys"
```

> **为什么不直接把 Lawrence 的私钥拷给队友:** 共用一把钥匙就没法单独吊销
> —— 哪天要收回权限,只能把两个人一起踢下来再重发。日志里也分不清谁动的。
> 服务器上已经开好了独立账号 `cfan`(在 `docker` 组,能读写 `/opt/gyt`),
> **现在 authorized_keys 是空的,所以还没人能用它登进来**。
>
> 要收回权限:`ssh animego-old "userdel -r cfan"`,一条命令,不影响别的东西。
>
> ### ⚠️ 但要把话说清楚:`docker` 组**实质等于 root**
>
> 这不是理论,是 2026-08-11 用一把一次性钥匙**实测过**的:`cfan` 用
> `docker run -v /root:/r ...` 可以直接读到 `/root` 下 `600` 权限的口令明文文件。
> 任何能调 docker 守护进程的人,都能挂载宿主机上任意目录、以任意 uid 运行。
>
> 所以:
> - 这个独立账号带来的是**可吊销**和**可追溯**,**不是权限隔离**。
> - 下面 §2 那张表里的文件权限(`.env` 是 `640`、口令明文是 `600`)防的是
>   「手滑 `cat` 出来贴到群里」这类**意外**,**不是防这个账号的持有者**。
> - 换句话说:**给谁开这个账号,就是把这台机器整个交给谁**。
>   两把模型 Key、登录口令、所有工地照片,他都拿得到。开之前想清楚。

### 第 3 步 · 你把这段加进 `~/.ssh/config`

```sshconfig
# 工友通线上机(原 animego 老 VPS)
Host gyt
  HostName 45.152.65.208
  Port 17776
  User cfan
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
```

然后:

```bash
ssh gyt
```

⚠️ **端口是 17776,不是 22。** 直接 `ssh root@45.152.65.208 # ❌ 错:没写端口,默认走 22,永远连不上`
—— **不是墙的问题,是端口不对。** 这台机器**只认密钥不认密码**(`PasswordAuthentication no`),
所以「让我输密码」这种提示不会出现;真出现了说明你连错机器了。

> **上面那行的 `# ❌` 注释是故意写在命令里面的,别删。** 2026-08-15 有人(AI)用正则从本文档
> 提取「怎么连」,抓走的正是这一行 —— 警告被剥掉,只剩一个看起来完全合法的命令,照着敲,
> 然后花一小时把「连不上」诊断成防火墙拦截和 sshd 被 OOM 干掉(ping 通、80/443 通、
> 22 端口超时,证据链自洽,结论全错)。**把否定标记写进命令本身**,一行抽走也带着它。
>
> 还有一个坑同源:**服务商面板上那个 root 密码对 SSH 没用**(只认密钥),而且面板是
> HTML 转义显示的,`&gt;` 其实是 `>`,照着抄必错。要连就用别名,别手敲。

---

## 2. 上面有什么

| 位置 | 是什么 |
|---|---|
| `/opt/gyt` | 代码。**是 rsync 上去的工作副本,不是 git clone** —— 服务器上没有 GitHub 凭据,这是刻意的 |
| `/opt/gyt/.env` | 两把模型 Key、访问令牌、口令哈希。`640 root:docker`,你在 docker 组里所以读得到 |
| `/opt/gyt/data/` | **这台机器的全部状态**:台账 SQLite、上传件、产物、LLM 缓存、向量库、对话历史。备份就是拷这一个目录 |
| `/root/.gyt-basic-auth-plaintext` | **登录口令的明文**。仓库里只有哈希,哈希不可逆 —— 这是唯一一份。标着 `600 root`,但**这不是对你的边界**(见 §1 那段:docker 组实质等于 root)。这个权限防的是手滑,不是防你 |
| `/opt/animego-mongo-final-20260811.tar.gz` | 老 animego 站的 mongo 冷备(214MB)。跟本项目无关,别删,那是拆老栈前留的 |

**代码怎么更新到线上:** 从开发机 rsync 上去(不是在服务器上 `git pull`)。
**排除规则直接让 rsync 读 `.gitignore`,不要手写 `--exclude` 清单** —— 理由是下面那张表。

```bash
# 在开发机的仓库根执行。先带 -n 演练一遍,看清楚要删什么
rsync -azn --delete -i --filter=':- .gitignore' --exclude '.git' ./ gyt:/opt/gyt/

# 确认删除清单里没有 data/ 下的东西,再去掉 -n 真跑
rsync -az --delete --filter=':- .gitignore' --exclude '.git' ./ gyt:/opt/gyt/
```

> **`gyt` 是 §1 第 3 步那段 `~/.ssh/config` 里的别名。** 你机器上要是配成了别的名字
> (Lawrence 这台是 `animego-old`),换成你实际配的那个 —— 同一台机,别照抄。

⚠️ **别把 `--filter` 换回手写的 `--exclude` 清单。** 这不是风格偏好,是 2026-08-15
拿 `rsync -n` 对着**线上机**演练出来的**生产数据丢失**。

这儿以前写的就是一串手写 `--exclude`,`data/` 下只排了 `artifacts`、`uploads`、
`gyt.sqlite3` **三样** —— 而线上 `data/` 下有**八样**
(`artifacts` / `cache` / `cad_index` / `chroma` / `demo` / `gyt.sqlite3` / `langgraph` / `uploads`)。
两版演练对比:

| | 手写清单版(旧) | `.gitignore` 驱动版(现) |
|---|---|---|
| 会删掉的东西 | **605 项** | **3 项** |
| ↳ `data/langgraph/` | **整个目录**,7 个文件 | 0 |
| ↳ `data/cache/` | 288 个 | 0 |
| ↳ `data/cad_index/` | 10 个 | 0 |
| ↳ `.git/objects/` | 296 个 | 0 |
| ↳ `/opt/gyt` 根下的散落文件 | 3 个 | 同样这 3 个 |
| 传输量 | **968MB** / 11388 个文件 | **2.8MB** / 84 个文件 |

新版删的那几项全是 **VPS 侧自己产生的**临时文件,直接躺在 `/opt/gyt` 根下:
`scenario_results.json`、`run_scenarios.py`,以及 `preflight_vps.sh`(根目录下的散落副本,
正主在 `scripts/` 里)。删了不心疼 —— 但反过来说:**别在 VPS 上把有用的东西直接放在
`/opt/gyt` 根下**,下次同步就没了。

> **右列那个数是会飘的**,别当成验收标准。它取决于当时 VPS 上堆了多少临时文件 ——
> 同一天早上量到的是 **8 项**(那会儿还躺着五个 `build-*.log` / `rebuild.log` /
> `redeploy.log` / `scenarios.log`,下午再量就没了)。
> **真正的规矩是「先 `-n` 演练、亲眼看一遍删除清单」,不是「数字对得上就放心」。**
> 左列那 605 项则是结构性的,只要还用手写清单就一直在。

**最要命的是 `data/langgraph/` —— 那是全部对话历史**(线上 88MB:`store.pckl`、
`store.vectors.pckl` 加三个 checkpoint)。commit `08f606d`
(`fix(deploy): 把 langgraph 的线程存储挂出来 —— 容器一重建,所有历史对话清零`)
修的正是这个东西,而旧命令会把那次修复的成果**一次抹干净**,
连带 §7 说的「线上历史记录里留着那 14 个场景的完整问答」也一起没。
`data/cache/` 是 LLM 磁盘缓存(5.4MB),删了下次提问全部真花钱。
`data/chroma`(向量库 12MB)和 `data/demo`(26MB)不在**删除**列里,但别高兴太早 ——
开发机上也有同名目录,所以它们的下场是**被开发机那份盖过去**
(演练里 `chroma.sqlite3` 和几个 HNSW 索引 `.bin` 都标着 `<f`,是真要传的),
外加把 macOS 的 `.DS_Store` 一路推上生产。同样不是你想要的。

**为什么不是「往清单里补五行」:** 补完下次还会漏。手写清单是 `.gitignore` 之外的**第二份平行真相**,
每多一个运行期目录就得记得回来补一次,而漏掉是**静默**的 —— 这次就这么漏了五个
(`cache`、`cad_index`、`chroma`、`demo`、`langgraph`)。`--filter=':- .gitignore'` 让 rsync
直接把 `.gitignore` 当排除规则读,「什么不该上服务器」从此只有一份定义,
以后往 `.gitignore` 里加一行就自动生效。968MB 那个数也是顺带治好的:
仓库根有 870MB 被 gitignore 掉的 YOLO 训练素材(`construction-safety-monitor.v1i.yolov8`
和同名 zip),手写清单里没有它,于是每次同步都往那台小机器上推一遍。

**`--delete` 默认不删被排除的文件**(真要删得显式写 `--delete-excluded`)。
所以 `data/` 和 `.env` 是双保险:先被 filter 挡在传输之外,再被 `--delete` 放过。

⚠️ **`.env` 仍然一步都碰不得** —— 只是现在不用手写 `--exclude '.env'` 了:
`.gitignore` 第 2 行的 `.env` 和第 65 行的 `.env.*` 已经把它和 `.env.vps` 一起罩住。
**但别为了「看着显式」把 filter 换回手写清单**,那等于把上面那张表重新踩一遍。
`.env` 真要是被开发机那份盖掉,里面是线上 Key 和口令哈希,站点当场登不进去。

⚠️ **`--exclude '.git'` 也别去掉。** `/opt/gyt` 底下确实有一份 `.git`(早年 rsync 顺带拷上去的旧快照,
停在 `a304109`,那台机没有 GitHub 凭据、拉不动),没这条就是 296 个 object 被删。
它没用,但删它没有任何好处,排除掉最省事。

⚠️ **跑完顺手查一次 `data/` 的属主。** 内容是安全的,但 `data/` **目录本身**在传输列表里
(演练输出是 `.d..t.og. data/`,要改 mtime + 属主 + 属组),而 `rsync -a` 会把源端 uid 带过去 ——
macOS 上是 `501:staff`,线上容器跑的是 **uid 10001**。改坏了的表现是老一套:
容器 healthy、页面能开,但台账写不进去。复原一条命令:

```bash
ssh gyt "chown 10001:996 /opt/gyt/data"      # 996 是那台机的 docker 组
```

`scripts/preflight_vps.sh` 第 ④ 组有一条「`data/` 属主是 10001」的硬检查,会替你拦。

---

## 3. 日常操作

所有命令在 `/opt/gyt` 下执行。这一串很长,建议先设个别名:

```bash
cd /opt/gyt
alias dc='docker compose -f docker-compose.yml -f docker-compose.vps.yml'
```

```bash
dc ps                      # 五个服务的状态,都该是 Up(backend/caddy 还该 healthy)
dc logs -f backend         # 跟着看后端日志
dc logs --since 10m caddy  # 看最近十分钟的访问日志
dc restart caddy           # 改完 Caddyfile 用这个(秒级),不用重建镜像
dc restart login           # 改完 scripts/serve_login.py 或 login-page.html 用这个
dc up -d                   # 改完 docker-compose.vps.yml 用这个
```

**改什么要做什么:**

| 改了 | 怎么生效 |
|---|---|
| `Caddyfile` | `dc restart caddy`。⚠️ 改完**先验路由树**,见 §4 第 1 条 |
| `scripts/serve_login.py` / `login-page.html` | `dc restart login`(它们是只读挂载,不在镜像里) |
| `scripts/serve_artifacts.py` | `dc restart artifacts` |
| `backend/` 里的 Python | **要重建镜像**,而这台机器上重建有坑,见 §5 |
| 前端 / `NEXT_PUBLIC_*` | 要重建前端镜像。那几个是**编译期**变量,只改 compose 的 environment 不生效 |

**上线前自检**(改完配置、`up` 之前跑):

```bash
bash scripts/preflight_vps.sh
```

它按六组查:环境变量 / 站点地址一致性 / DNS / 端口与目录 / 配置语法 / 机器规格。

⚠️ **内存那条必然报红**(这台机 1966MB,脚本要求 ≥3500MB)—— 那是**明知越过**的,
理由写在 `docs/W6_VPS部署与访问口令.md` 的 §1.1。
**看到那条红先去读那一段,别直接改脚本里的阈值。**

---

## 4. 不能干的事(每条后面都是「干了会怎样」)

1. **改完 `Caddyfile` 别只跑 `caddy validate` 就上线。**
   2026-08-11 踩过:配置语法完全合法、`validate` 说 Valid,但指令顺序被 Caddy
   自己的顺序表重排,**兜底路由把所有请求先吃光,登录校验永远到不了** ——
   表现是未登录访问首页直接 **200**,站点整个裸奔,而且**没有任何报错**。
   唯一能看见真实顺序的办法:

   ```bash
   docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
     -e GYT_SITE_ADDRESS=velactora.com caddy:2-alpine \
     caddy adapt --config /etc/caddy/Caddyfile
   ```

   确认 `forward_auth` 排在所有业务路由**之前**。整个站点块现在包在一个 `route` 里
   就是为了让顺序由书写顺序决定 —— **别把任何一条 handle 挪到 route 外面。**

2. **别给 backend / frontend / login / artifacts 加 `ports`。**
   `:2024` 是**零鉴权**的 Agent 执行端点,容器里揣着两家供应商的真实 API Key;
   artifacts 里是工地现场照片(含可识别人脸)。加一行 `ports` = 直接挂公网,
   **而且不会有任何报错**。preflight 第 ⑤ 组数「发布端口总数」,应为 **3**(80 / 443tcp / 443udp)。

3. **别把口令明文或 Key 写进任何进 git 的文件。** 包括这一份。

4. **改口令 = 所有人立刻下线。** 会话签名密钥是从口令哈希派生的,这是设计,不是 bug。
   改口令:`python scripts/make_login_hash.py`,把它吐出来那行填进 `.env`,`dc restart login`。

5. **别在服务器上 `git pull`。** 那里不是 git 工作树,代码是 rsync 上去的。

---

## 5. 这台机器的两个已知窄口

**内存 1.9GB(下限是 4GB)。** 实测跑得动:检索时加载 BGE-M3 峰值只占 955MB
(权重是 mmap 的文件页缓存,不占匿名内存),全场内核 OOM 计数 0。
**但样本只有一次、并发是 1** —— 几个人同时提问会怎样没测过。

**磁盘 30GB,重建后端镜像时会不够。** 后端镜像 6.37GB(含 2.2GB 的 BGE-M3 权重),
新旧两份同时存在时装不下。2026-08-11 连着撞了两次 `no space left on device`。

⚠️ **重建前千万别跑 `docker builder prune -af`。** 它会把依赖层缓存一起清掉,
本该几十秒的增量重建变成全量(155 个包 + 2.2GB 权重重下),然后撑爆磁盘 ——
上次就是这么把自己坑了的。要腾地方优先删 `gyt-frontend:known-good` 这类**不同摘要的旧镜像**。

⚠️ **构建失败之后必须查一眼 `docker images` 里 tag 指到哪儿。**
失败可能发生在「镜像名已经打上、层还没解完」之间,那时 `gyt-backend:vps` 指着一个坏镜像,
**机器一重启就会用它起后端**。「构建失败」不等于「什么都没变」。

**因此:线上跑的后端镜像目前比 `main` 少一个提交**(`4cb2809`,cad 索引竞态修复)。
完整来龙去脉和补法在 `TODOS.md` 的 **TODO-33**。

---

## 6. 站点坏了先看什么

```bash
dc ps                                   # ① 五个都 Up 吗?哪个不是就看哪个的日志
df -h /                                 # ② 盘满了吗(满了 Docker 会行为异常)
free -h                                 # ③ 内存还剩多少
dc logs --since 5m caddy | tail -40     # ④ caddy 的访问日志,状态码一眼能看出卡在哪层
```

**按状态码定位在哪一层出的事:**

| 现象 | 大概率是 |
|---|---|
| 打不开、连接被拒 | caddy 没起来,或 80/443 被别的进程占了 |
| 一直跳回登录页 | login 服务没起来,或口令哈希写坏了(`.env` 里 `$` 必须写成 `$$`) |
| 页面能开,一提问就 401 | 令牌那条链断了 —— 前端包里没烘进令牌,跑 preflight 第 ④ 组 |
| 页面能开,提问转圈没反应 | backend 没起来或没 healthy |
| 照片是碎图 / 巡检记录点不开 | artifacts 服务或 `/artifacts/*` 路由 |

**caddy 显示 unhealthy 但站点其实好好的:** 它的健康检查**故意断言「返回 302」**
(未登录被打回登录页 = 门还在岗)。哪天有人把登录校验去掉,站点会变成 200 全放行,
而这条检查会立刻变红。所以 unhealthy 时先确认是不是有人动了那道门。

---

## 7. 想更深入

| 想知道 | 看哪儿 |
|---|---|
| 完整部署流程、每一步的坑、所有实测数据 | `docs/W6_VPS部署与访问口令.md`(有配套 html) |
| 这套东西整体怎么设计的 | `CLAUDE.md`、`backend/README.md` |
| 已知欠账(每条带完整上下文) | `TODOS.md` —— 尤其 TODO-33(镜像漂移)、TODO-34(英雄链偶尔不出 docx) |
| 各 Agent 怎么联动、14 个使用场景 | `docs/Agent联动与使用场景分析.html` |

线上站点的历史记录里留着那 14 个场景的完整问答,登进去左边就能翻,不用自己造数据。
