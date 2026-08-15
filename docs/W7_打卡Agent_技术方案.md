# W7 · 打卡功能技术方案(v4)

> 2026-08-15 · `/plan-eng-review` 三轮 + **Codex 两轮独立复审**后定稿。
> 目标场景:**香港建造业**,工友跑多个地盤,自拍打卡、后端盖水印、出可存档凭证。

**版本沿革**(为什么留着:被推翻的判断本身是资产,§1.11)

| 版本 | 触发 | 主要改动 |
|---|---|---|
| v1 | 15 条问答定案 | 打卡走对话链 |
| v2 | Codex 复审 24 条 | **打卡改走直接接口**;7 处事实订正 |
| v3 | Codex 二轮复审 44 条 | **raw body 取代 multipart**;鉴权默认值修正;凭证持久化;清理可实施 |
| **v4** | **eng-review 第三轮 10 条裁决** | **凭证图改走已有出口**(`<img>` 带不了自定义头);幂等指纹进 header;限流维度重定;Caddy 闸改路由级;水印先缩图(1.9GB VPS);文案层次归位;补上没有模块的 cleanup |

> **v4 这 10 条的共同点:全部要读现存代码才看得见**,两轮 Codex 都没抓到。
> 逐条实锤在 §1.10。这也是「复审不能替代读代码」的一次实证。

---

## 0. 决策台账(16 条)

| # | 决策点 | 结果 |
|---|---|---|
| D1 | 前置技能 | 跳过 /office-hours |
| D2 | 打卡的「谁」 | 按「已登录员工」当既定前提 |
| D3 | 范围深度 | 打卡 + 凭证 + 查 |
| D4 | 姓名怎么落地 | 前端存,随请求带 |
| D5 | 取像方式 | 自写 getUserMedia 组件 |
| D6 | 内置浏览器降级 | 特性检测 + 退回 `<input capture>`,标记弱凭证 |
| D7 | 电脑端二维码 | 本站网址,不带 token |
| D8 | 证据链 | 后端画水印;**原图不进产物库**(见 §3.3) |
| D9 | 中文字体 | apt 装 fonts-wqy-microhei |
| D10 | 打卡频次 | 流水账,次数不限 |
| D11 | 定位用法 | 只存坐标 + 精度,地盤名由人给 |
| D12 | 简繁体 | 先做简体,文案集中可替换 |
| D13 | PDPO 声明 | 登录页底部静态文字,不拦,不存同意记录 |
| D14 | 前端测试 | vitest 测纯函数 + 真机核对表 |
| D15 | 打卡走不走 LLM | **走直接接口,LLM 只管查** |
| D16 | 查询要不要单独 Agent | **要,独立 attendance Agent** |

### D16 的推演(下一个人一定会再问)

D15 之后打卡**写入**已完全不经过 Agent,问题只剩「查询这一半放哪」。

| 方案 | 省什么 | 代价 |
|---|---|---|
| **A · 独立 Agent(选定)** | —— | 五个文件固定税:`graph.py`+`scorers.py`+`routing.csv`+`eval/README`+`CLAUDE.md` |
| B · 挂给 schedule | 整个 Agent 目录 | **污染正域**。summary 是路由的**唯一依据**;危险词真实存在:「张三这个月干了几天」是考勤还是任务? |
| C · 界面出一张表 | Agent + eval 三处 | 失去组合查询;写入已拿出 Agent 体系,查询再拿出去 = 对项目主线零贡献 |

`AGENT_REGISTRY` 顶部记着 ping 被摘除的三连实锤 —— **三轮改 summary 措辞都没收住**。
schedule 现在那句很稳,不拿它去赌。

---

## 1. 事实核查留档

### 1.1 🔴 自定义路由**默认不鉴权** —— v2 的致命假设

```python
# langgraph_api/server.py:169   (langgraph-api,实测)
enable_auth_on_custom_routes = config.HTTP_CONFIG and config.HTTP_CONFIG.get(
    "enable_custom_route_auth")
```

**不写这个键 = 假值 = 自定义路由完全不过 `auth.py`。** v2 只写了 `"app"`,
那样 `POST /checkin` 前面只剩 Caddy 的登录 Cookie,**`auth.py` 那 636 行一行都不生效**。

同一文件 `:167` 也确认了 `middleware_order == "auth_first"` 是真键:

```json
{ "http": { "app": "./src/gyt/checkin_api.py:app",
            "enable_custom_route_auth": true,
            "middleware_order": "auth_first" } }
```

⚠️ **`@auth.on.threads.create_run` 的限流管不到它。** 那个钩子只处理 LangGraph 的 run 动作;
普通 Starlette 路由要**自己带限流**(§3.6)。

### 1.2 Starlette 上传会落盘 —— v2 的「原图从不落盘」是假的

```python
# starlette/formparsers.py:146
class MultiPartParser:
    spool_max_size = 1024 * 1024   # 1MB
```

照片上限 10MB,**超过 1MB 的 multipart 上传会自动滚到 `/tmp`**。
而且 `python-multipart` 当时**不在依赖里**(实测 `import multipart` → ImportError),
`request.form()` 直接跑不起来。

> ⚠️ **这一条 2026-08-15 合流后已失效**:队友为 `webapp.py` 的图纸上传把
> `python-multipart` 加成了正式依赖。**但另外三条理由不受影响**(落 `/tmp`、
> 短路不了幂等、大小检查太晚),raw body 的选择不变 —— 留着这条是为了说明
> 当初为什么连试都没试。

> **v3 的解法不是打补丁,是换协议:元数据走 header,照片走 raw body。** 见 §2.1。
> `Request.stream()` 实测是真流式(逐 ASGI 事件 yield,不预缓冲),
> 于是一次解掉四条:不要 multipart 依赖、不落临时盘、读第一个字节前就能短路幂等、
> 能边读边掐大小。

### 1.3 🔴 `<img>` 带不了自定义请求头 —— v3 的 `/checkin/photo` 必然裂图

v3 §3.7 发明了 `GET /checkin/photo/{receipt_no}`。**它和 §1.1 的鉴权是互斥的:**

```tsx
// scripts/frontend-overrides/human.tsx:213(现存代码,线上在跑)
<img src={byIdUrl(id)} />            // byIdUrl = `${ARTIFACT_BASE}/by-id/${id}`
```

```
# Caddyfile 那段注释,原文:
#   同源之后浏览器会自动把口令带到 <img src> 上,不用前端做任何事
```

靠的是 **Caddy 的登录 Cookie**,而 artifacts 服务自己**零鉴权**。
而 `auth.py:105` 的 `API_KEY_HEADER = "x-api-key"` 是**请求头** —— `<img>` 只送 Cookie,
送不了自定义头。两件事凑一起 = 凭证图恒 401,界面上是个裂图标,控制台只有一行。

**发明这个端点的理由本身也不成立。** v3 写的是「静态服务只认 `artifact_id`,而 Agent 故意不返回它」——
这句话把两个东西混为一谈:**做最小化的是 LLM Agent 的工具返回值**,
而 `POST /checkin` 是本人刚拍完照的直连接口,它返回 `artifact_id` 给自己的前端没有最小化问题。

**v4 改法:去掉这个端点**,`POST /checkin` 与 `GET /checkin/recent` 都返回 `artifact_id`,
前端渲染 `${ARTIFACT_BASE}/by-id/<artifact_id>` —— 与 `human.tsx` 完全同一条已验证路径。见 §3.7。

### 1.4 🔴 §3.2 层① 在 v3 里逻辑上跑不通

v3 写:

```
① 读 body 前:SELECT WHERE event_id = X
     ├─ 命中 且 req_digest 相同 → 200 + 原记录(不读 body)
```

而 `req_digest` 的定义是「姓名+地盤+坐标+**照片 sha256**」——
**照片的哈希必须先把照片读完才算得出来**。「不读 body 就比对指纹」自相矛盾。
而且 v3 §2 列的 header(Event-Id / Worker / Site / Geo / Source)**没有指纹这个字段**。

**v4 改法:加 `X-GYT-Digest`,进 T1 的冻结契约。** 见 §3.2。

### 1.5 `tool-calls.tsx` 渲染不了打卡凭证

```tsx
export function ToolResult({ message }: { message: ToolMessage })
```

它只消费 **`ToolMessage`**。第一轮复审说「凭证卡片该放这儿」——**在 v1 里那是对的**
(当时 check_in 是工具)。D15 改成直接 HTTP 之后**根本不产生 ToolMessage,卡片永远渲染不出来**。

这是典型的「修了 A 引入 B」。改法见 §3.7。

### 1.6 dev-docker 只挂 `backend/src`

```yaml
# docker-compose.dev.yml:106
- ./backend/src:/app/src:ro
```

v2 把 `checkin_api.py` 放 backend 根(比着 `auth.py`),那样**改了不热重载**,
`test-docker` 也挂不到。**放 `src/gyt/checkin_api.py`** —— dev 挂载覆盖得到。
(`auth.py` 之所以在根,是因为 langgraph 按文件路径 import 它;
`http.app` 同样按文件路径解析,放 `src/` 下一样能被找到,且能 `from gyt.config import …`。
⚠️ **这一条要在实现第一天实测**,不通就退回根目录并同步改 dev 挂载。)

### 1.7 缺字检测:`getbbox` 是废判据(实测推翻)

```python
# ❌ Courier New 里一个汉字都没有,但:
ImageFont.truetype("Courier New.ttf", 40).getmask("张").getbbox()
#   → (2, 0, 23, 25)      ← 这是 FreeType 渲染 .notdef **方块本身的像素**
# 有字和缺字返回同样的东西 —— 判据零区分度,挂上去一次都不会触发。
```

**唯一可靠做法是查字体 cmap**,而且三条要一起(§3.4)。
Pillow 原生无替代 —— 实测 `FreeTypeFont` 不暴露任何 glyph/char 查询方法。

### 1.8 法规与合规(两轮订正后的措辞)

香港适用**《建造業工人註冊條例》(第583章)**:工人须註冊、持智能註冊證、
进工地经读证核验,**每 7 天的每日出勤记录须在 2 个工作日内提交註冊主任(DAR)**。

> **⚠️ 两次订正过头,这里是收敛后的说法:**
> - 存在读证装置豁免与流動 DAR App,但**声明册只处理「已注册工人无法出示證件」的情形,
>   不是读证的通用替代**;读证豁免也**不免除提交 DAR**。
> - **不要宣称「跑多个地盤的管理人员正是法定考勤覆盖场景」** —— DAR 覆盖的是
>   受雇并亲自进行建造工作的注册工人,巡视地盤的管理人员未必在范围内。
>
> **稳的结论只有一条:这套系统不构成法定考勤记录,它是班组内部台账。**

**PDPO(第486章)**:PICS 要写明用途、**是否自愿**、**不提供的后果**、
**可能转交给谁**、**查阅更正方式**、**负责人姓名/职衔/联系地址**。

> **⚠️ v2 说「D15 从根上解掉跨境传输」是错的。** D15 去掉的是**自拍和精确坐标**;
> 查询链里的问题和工具结果**仍然包含姓名、时间、地盤**,照样发给 DeepSeek。
> 另:PDPO 第 33 条(跨境转移)**至今未生效**,不能把「跨境」写成一条已生效的独立硬义务。
> 但**接收方类别披露**是现行要求,PICS 必须写。

### 1.9 坐标与摄像头(措辞收敛)

- **Geolocation 规范本来就规定输出 WGS-84**,与在不在香港无关。GCJ-02 偏移只对大陆境内生效。
  高德接口契约仍要求 GCJ-02 输入 —— **「香港零转换成本」的说法不成立**。本方案不上地图。
- **WebKit 自 iOS 14.3 起已允许符合条件的 WKWebView 暴露 `getUserMedia`。**
  能不能用取决于宿主 App,**不保证,也不必然没有**。
- `capture="user"` 支持**有限且因浏览器与设备而异**,不能断言「安卓一律忽略」。
- 结论:降级路径保留,但**必须特性检测 `navigator.mediaDevices`,不许嗅 UA** ——
  UA 白名单在「因宿主而异」的现实下必然漏(WhatsApp / Facebook / Instagram 全是 WKWebView)。

### 1.10 v4 新增的现存代码事实(全部实测)

| 事实 | 位置 | 影响 |
|---|---|---|
| `<img>` 只送 Cookie,送不了 `X-Api-Key` | `human.tsx:213` + Caddyfile 注释 + `auth.py:105` | **`/checkin/photo` 必裂图** → §1.3 |
| `TokenBucket.take(identity)` 收任意字符串当桶键 | `auth.py:262` | 三种限流维度实现成本一样,差的是**桶键谁说了算** → §3.6 |
| Caddy `request_body max_size 128MB` 在 **`:182`**(v3 写的 `:170` 是错的),且 **128 是算出来的**:DXF 64MB × base64 4/3 ≈ 85.3MB + 余量 | `Caddyfile:170-184` | **v3 的「收到照片同量级」会打死图纸上传** → §3.6 |
| VPS **1.9GB 内存**,检索时 BGE-M3 峰值 **955MB**;`docker-compose.vps.yml:449` **故意不写 `deploy.resources.limits`** | `docs/W6_VPS_队友接入.md:203` | 水印解位图会 OOM 整个容器 → §3.4 |
| `safety/tools.py:315` **已经在缩图**:`image.thumbnail((max_edge, max_edge), LANCZOS)` | 同左 | 水印抄这个写法,**顺带把水印字号固定下来** |
| `setup-frontend.sh` 现有 **11 处** `apply_override`(`:386`–`:418`) | 同左 | `install_new_file` 装的三件**不计入这个数** → §6 |
| `eval_threshold_routing = 0.90` / `eval_min_rows_routing = 20`;`routing.csv` 现 22 行 | `config.py:311,319` | §11.2 的门槛数与配置对得上 |
| `uploads.py` 只查了 `drawing_limit`,照片分支无上限 | `uploads.py:205` vs `:229` | **现存 bug**,T14 |
| `artifacts.py` 只有 `register`/`resolve`/`read_meta`;`ArtifactKind` 五个成员 | `artifacts.py:185,219,229,234` | 清理不可实施 → **必须改 `artifacts.py`** |
| `apply_override` 目标不存在就 warning 跳过 | `setup-frontend.sh:371` | 新增文件不能用它 → `install_new_file()`(§3.8) |
| `serve_login` 登录后重定向写死 `/` | `serve_login.py:350` | 二维码不能带 `?name=` |
| `FROM base AS models` | `Dockerfile:144` | 字体只能装 `app` 阶段。**两轮复审均确认这个修法正确** |
| 容器 `TZ=Asia/Shanghai` | `Dockerfile:253` | **业务时区不许靠宿主**,见 §3.5 |
| `fonttools` 只是传递依赖(ezdxf / matplotlib 带入) | `uv.lock` 4.63.0 | 缺字检测靠它 → **必须提成显式依赖** |
| 工具多传参数**静默忽略,不抛 TypeError** | 实测 langchain 1.3.14 | 早期那条守门测试断言是错的 |
| v3 §4.2 有 `test_attendance_cleanup.py`,而 §4.1 **没有对应模块** | v3 自身 | 测试文件没有被测对象 → v4 补 `attendance/cleanup.py` |

### 1.11 被推翻的判断(留档)

1. **「`InjectedState` 就能保证数据可信」** —— 偷换概念。它只把参数从模型 schema 里藏起来,
   state 里的值仍由客户端控制。D15 之后不再是架构支点。
2. **「打卡走对话链和其它 Agent 同构所以更好」** —— 同构不是价值。
3. **「凭证编号 `GYT-A-日期-秒` 够用」** —— 同秒多人会碰。
4. **「原图从不落盘」(v2)** —— multipart 会 spool,见 §1.2。v3 换协议后才真成立。
5. **「凭证卡片放 tool-calls.tsx」(v2)** —— 直接 HTTP 不产生 ToolMessage,见 §1.5。
6. **「加个 `/checkin/photo` 端点就能取凭证图」(v3)** —— `<img>` 带不了头,见 §1.3。
7. **「读 body 前就能比对 `req_digest`」(v3)** —— 指纹含照片哈希,见 §1.4。
8. **「Caddy 请求体上限收到照片同量级」(v3)** —— 会打死 DXF 图纸,见 §1.10。
9. **「限流按 `event_id` 前缀分桶」(v3)** —— `event_id` 是客户端生成的,换个前缀就是新满桶,零防护。

---

## 2. 架构

```
                          工友的手机 / 电脑
        ┌────────────────────────┴────────────────────────┐
  ①【打卡】点按钮                                  ②【问一句】打字
        │                                                 │
  前端自拍组件                                            │
   ├ navigator.mediaDevices 有 → 前置取景 → canvas 截帧    │
   └ 没有 → <input capture>  → source=fallback            │
   ‖ 并行:getCurrentPosition(3s 超时,失败照样能打)        │
   ‖ 算 sha256 → 拼进 X-GYT-Digest                        │
        │                                                 │
        │ POST /checkin                                   │
        │   Content-Type: image/jpeg                      │
        │   X-Api-Key: <显式复用 getApiKey()>              │
        │   X-GYT-Event-Id / Worker / Site / Geo / Source  │
        │   X-GYT-Digest   ← 只用于短路比对,不可信         │
        │   body = JPEG 原始字节(不是 multipart)          │
        ▼                                                 │
  ┌──────────────────────────────────────────┐            │
  │ src/gyt/checkin_api.py:app                │            │
  │  ⓪ 鉴权(enable_custom_route_auth:true)  │            │
  │  ① 限流(读 body 之前)                    │            │
  │  ② 幂等:查 event_id + 比 header digest    │            │
  │     —— **读第一个字节之前**                │            │
  │  ③ 流式收 body,超限当场断开               │            │
  │  ④ 缩图 → 画水印;原图**不进产物库**       │            │
  │  ⑤ register(kind=ATTENDANCE)             │            │
  │  ⑥ 写库(digest 用**服务端自己算的**)      │            │
  └──────────┬───────────────────────────────┘            │
             │ 200 { receipt_no, artifact_id, ... }        │
             ▼                                            ▼
   GET /checkin/recent → 前端渲染                supervisor → attendance Agent(只读)
   <img src={ARTIFACT_BASE}/by-id/{artifact_id}>  返回值**不含** artifact_id / 坐标
   ↑ 与 human.tsx 完全同一条已验证路径
```

### 2.1 为什么用 raw body 而不是 multipart

| multipart(v2) | raw body(v3 起) |
|---|---|
| ~~需要 `python-multipart` —— 不在依赖里~~(合流后已成正式依赖,此条失效) | 零新增依赖 |
| `spool_max_size=1MB`,4MB 照片**落 `/tmp`** | `Request.stream()` 真流式,**不落盘** |
| `event_id` 和文件在同一个 body 里,**必须先解析完整请求**才拿得到 | `event_id` + `digest` 在 header,**读第一个字节前就能短路** |
| 大小检查在解析完之后 | **边读边数,超限当场 disconnect** |

元数据放 header 的代价:值必须 ASCII 安全 → **姓名与地盤名用 Base64URL 编码后放 header**,
服务端解回。这条要写进字段契约(§6)。

---

## 3. 关键设计

### 3.1 数据模型

```sql
CREATE TABLE IF NOT EXISTS attendance (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id    TEXT    NOT NULL UNIQUE,   -- 客户端幂等键
  req_digest  TEXT    NOT NULL,          -- **服务端自己算的**指纹(姓名+地盤+坐标+照片 sha256)
                                         -- ⚠️ 绝不落 header 里那个客户端报的值,见 §3.2
  worker_name TEXT    NOT NULL,          -- 原样存。⚠️ 绝不做简繁转换
  site_name   TEXT,
  checked_at  TEXT    NOT NULL,          -- **服务器时钟 + 显式 Asia/Hong_Kong**
  work_date   TEXT    NOT NULL,          -- 同一快照派生
  lat         REAL,
  lon         REAL,
  accuracy_m  REAL,
  geo_status  TEXT    NOT NULL           -- ok|denied|timeout|unsupported|error|absent
              CHECK (geo_status IN ('ok','denied','timeout','unsupported','error','absent')),
  source      TEXT    NOT NULL CHECK (source IN ('camera','fallback')),
  receipt_no  TEXT    NOT NULL UNIQUE,
  artifact_id TEXT,                      -- ⚠️ **可空** —— 清理后置 NULL
  photo_purged_at TEXT,                  -- 清理时间;非空 = 界面显示「凭证图已过期清理」
  project_id  TEXT,                      -- 建表即预留、写入恒 NULL(照搬 tasks 表)
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_date   ON attendance(work_date);
CREATE INDEX IF NOT EXISTS idx_att_worker ON attendance(worker_name, work_date);
```

- **`geo_status`:** 光靠 `lat IS NULL` 分不清「拒绝权限 / 超时 / 设备不支持 /
  前端字段漂了 / 程序出错 / 前端压根没发」—— 审计时这六种意义完全不同。
  ⚠️ 六个取值的判定规则要写进 `checkin-lib.ts` 的纯函数并有 vitest 覆盖,
  否则前端会随手塞一个不在 CHECK 里的值,表现是**整条 INSERT 失败而报错在数据库层**。
- **`artifact_id` 可空 + `photo_purged_at`。** v2 写 `NOT NULL` 而清理方案要求置空,直接冲突。
- **`req_digest` 落的是服务端算的值。** header 里那个只用于层①短路,**不可信、不落库**。
- **`work_date` 落库** 是为了走索引。⚠️ 但**不是唯一办法** —— SQLite 支持表达式索引和生成列。
  选落库是因为写侧算一次最省,不是因为别无选择。

### 3.2 幂等:三层,缺一层都不够

```
① 读 body 前:SELECT WHERE event_id = X,与 header 的 X-GYT-Digest 比
     ├─ 命中 且 digest 相同 → 200 + 原记录(不读 body、不缩图、不画水印、不写库)
     └─ 命中 但 digest 不同 → 409,人话:「这次打卡的信息和之前那次对不上」
② 未命中 → 流式收 body → **服务端自己算 digest** → 缩图 → 画水印 → register
③ INSERT 撞 UNIQUE(并发同键)→ 回查那条返回它;本次的图成孤儿,交清理
```

> **`X-GYT-Digest` 是客户端报的,不可信 —— 但这不影响它的用途。**
> 层①压根不读 body,客户端谎报只是让自己拿回一条对不上的记录;
> 层②真读了 body,落库用的是**服务端自己算的那个**。
> **这两句必须写进 `checkin_api.py` 的模块 docstring** —— 少了它,
> 下一个人看到 header 里有个现成的 digest 就会直接拿去落库,而那是伪造入口。

> **`event_id UNIQUE` 只保证「最多一行」,不保证整个操作幂等。** 并发同键时两张水印图都会被
> 生成,输家只是 INSERT 失败 —— 副作用已经发生。①把绝大多数重发挡在读 body 之前,
> ③兜住剩下的并发窗口,孤儿图交清理。**这是「够用」不是「完美」,写明白。**

**⚠️ 客户端也要负责:** 只在同一个 React 生命周期里复用 `event_id` 是不够的 ——
刷新、崩溃、App 被杀之后会生成新 ID,用户再提交就是重复记账。
**`event_id` 必须落 `sessionStorage`,提交成功后才清除。**

**⚠️ 共享口令下 `event_id` 不是用户作用域** —— 任何登录者拿到别人的 event_id 都能取回那条记录。
根治要 TODO-3,记在 §7。

### 3.3 原图、缩图与写入顺序

**「原图从不落盘」在 v3 才真成立**,靠的是 raw body(§2.1),不是靠承诺。
准确措辞是:**原图只在内存里存在,不进产物注册表,请求结束即消失。**

**v4 补一条:进产物库的是缩过的图,不是原图。**

```
raw body(≤10MB,流式,不落盘)
   → Image.open → thumbnail(长边上限, LANCZOS)     ← 与 safety/tools.py:315 同一写法
   → 画水印 → 重新编码 JPEG → register(ATTENDANCE)
```

理由有三,内存只是其中一条(详见 §3.4):
① 峰值内存从一张 4000×3000 的约 36MB 降到约 7MB;
② **水印字号必须相对图尺寸固定** —— 不缩图的话同一号字在 4000px 上是蚂蚁、在 800px 上糊满半张;
③ 缩图本身是解压炸弹的第二道闸。

> ⚠️ **代价要写明白:凭证图不是原始分辨率,放大看不清安全帽细节。**
> 可接受的理由:凭证的用途是「证明这个人这个时间在这里」,不是取证照片 ——
> §7 风险 4 早就写了这套东西没有实名/活体/签名,证明力本来就有限。
> **别把它当成能事后放大查违规的证据库。**

**顺序不许颠倒:先落图后写库。**
图成功库失败 → **孤儿文件**(无害);库成功图失败 → **死链**(工友点了打不开,无提示)。

> ⚠️ **这不是原子提交,别宣称是。** `artifacts.register()` 的文件与 sidecar 没有 `fsync`,
> 掉电时仍可能出现「库有行、图没落全」。可接受的理由:掉电是低频且可人工修复,
> 而死链是高频路径。**清理器必须有老化窗口**(见 §3.9),否则会在 register 与 INSERT 之间
> 误删正常请求的图。

### 3.4 水印

**① 先缩图再画 —— 这是 1.9GB VPS 上的硬要求,不是优化。**

| 实测数 | 值 |
|---|---|
| VPS 内存 | **1.9GB**(`docs/W6_VPS_队友接入.md:203`) |
| 检索时 BGE-M3 峰值 | **955MB** —— 半台机器 |
| `docker-compose.vps.yml:449` | **故意不写 `deploy.resources.limits`** |
| D3 定的突发 | 10 —— 十个人同时打卡就是十份位图同时叠 |

一张 4000×3000 解开约 36MB,加水印副本与编码缓冲,单请求一百多 MB。
**OOM 杀的不是「打卡」这个请求,是整个 backend 容器** ——
现象是全站突然挂掉、历史会话中断,而日志里只有内核 OOM killer 一行,
**没人会把它和「有人打了张卡」连起来**。抄 `safety/tools.py:315` 的 `thumbnail` 写法,
长边上限进 `config.py`。

**② 字体装 `app` 阶段,绝不动 `base`。** `Dockerfile:144` 是 `FROM base AS models`,
动 base 会让 models 缓存失效、重下 2.2GB。(两轮复审均确认这个修法正确。)

**③ 字体路径按候选列表探测。** 写死 Debian 路径会破坏 macOS 本机开发。
默认空 → 启动按候选列表探测 → 一个都找不到才硬失败。
**绝不 fallback 到 `ImageFont.load_default()`**(ASCII 位图字体,汉字画方块)。

> ⚠️ **启动硬失败会打死非 Docker CI。** GitHub 的 Ubuntu runner 没装 WQY。
> **已定死走这条:CI 里 `apt-get install fonts-wqy-microhei`**,写进 `ci.yml`(T7)。
> 另一条路(给水印测试打 skip + 用仓内测试字体)被否决 —— 它会让 CI 和线上测的不是一回事。

**④ 缺字硬失败,判据只有一个是对的(§1.7)。**

```python
from fontTools.ttLib import TTCollection, TTFont
# 三条一起才对:
# 1) .ttc 是集合,按 index 取面;
# 2) **Pillow 的 truetype(path, index=N) 与 fonts[N] 必须同一个 N** ——
#    实测 STHeiti.ttc 两面的 cmap 码点数是 37707 vs 37708,查错面就在那几个字上错;
# 3) 用 getBestCmap()。
faces = TTCollection(p).fonts if p.endswith(".ttc") else [TTFont(p)]
missing = [c for c in text if ord(c) not in set(faces[IDX].getBestCmap())]
```

`fonttools` **必须提成显式依赖** —— 现在只是 ezdxf / matplotlib 的传递依赖,
断的方式是静默的。已在 `uv.lock`(4.63.0),提升零安装成本。

> ⚠️ **「传 𠮶 必须失败」这条测试不可移植** —— macOS 的 PingFang HK 本身支持该字。
> 测试必须**钉死用 WQY**,不能依赖候选探测的结果。

### 3.5 时间与时区

**所有时间取自同一个快照,且时区显式写死:**

```python
NOW = datetime.now(ZoneInfo("Asia/Hong_Kong"))   # ⚠️ 不许 astimezone() 靠宿主猜
```

`checked_at`、`work_date`、水印上的时间、`receipt_no` 里的日期时刻**全部来自 `NOW`**。

> ⚠️ 容器 `TZ=Asia/Shanghai`(`Dockerfile:253`),本机开发可能是任意时区。
> 两地都是 UTC+8 无夏令时所以数值碰巧一致 —— **但这是巧合,不是保证**,
> 靠宿主 TZ 意味着换台机器 `work_date` 就可能不同。
> **这条是「静默失败类」的头号** —— `make test` 在本机全绿也发现不了(本机也是 UTC+8),
> 所以它进 §11 的闸:测试要**显式篡改宿主 TZ 再断言 `work_date` 不变**。
> **夜班跨午夜的业务语义仍未定义**(当前 = 日历日)→ TODO-38。

### 3.6 限流、请求体上限与输入校验

**限流:令牌桶,复用 `auth.py:262` 的 `TokenBucket`(`take(identity)` 收任意字符串当桶键)。**

| 桶 | 取值 | 它是什么 / 它不是什么 |
|---|---|---|
| **全局** | 30 次/分,突发 10 | **这是唯一的真闸。** 一个班组的量级 |
| **按 `worker_name`** | 更小的桶 | **防手抖连点,不是安全控制** —— `worker_name` 由客户端提供,改一下就换个桶 |
| ~~按 `event_id` 前缀~~ | —— | **v3 的写法,已废。** `event_id` 是客户端生成的,换前缀 = 全新满桶,零防护 |

> **注释必须写死那句「worker_name 桶是防手抖,不是安全控制」。**
> 否则下一个人看到「按人限流」会以为滥用问题已经解决了。真正的天花板只有全局那 30/分。

**位置:两个桶都在读 body 之前**,否则限流没挡住流量,只挡住了写库。

**Caddy 请求体上限:给打卡路径单设,全局那条一个字不动。**

```
Caddyfile:170-184 的注释已经把 128MB 算给你看了:
  最大单件是 DXF 图纸(GYT_DRAWING_MAX_MB=64)
  → 前端转 base64 塞进 JSON,体积撑到 4/3 → 约 85.3MB
  → 加 JSON 转义与多文件同发的余量 → 取 128MB
```

**v3 说「收到与照片上限同量级」会直接打死图纸上传**,而报错是 Caddy 的 413,
不是那句写好的中文提示 —— 查的人会去翻 `uploads.py` 和 `DRAWING_MAX_MB`,方向全错。
**v4:新增一个只匹配打卡路径的 route,里面放自己的 `request_body max_size`。**

> ⚠️ **这个 route 块的位置要连同注释一起核。** Caddy 有一张固定的指令顺序表,
> 而 `handle` / `handle_path` 在那张表里的排位是这个文件**已经踩过一次「站点裸奔」**的地方
> (见 `Caddyfile:135-139` 的注释)。位置写错的现象不是报错,是**闸不生效**或**路由错乱**。

**输入校验清单(逐条要有测试):**

`event_id` 格式与长度 · `X-GYT-Digest` 格式(仅十六进制、定长)· 姓名/地盤长度上限与控制字符和换行 ·
经纬度范围与**成对出现** · `accuracy_m` 非负且有限(挡 NaN/Infinity)· 图片字节上限(流式)·
**解码后像素上限**(挡解压炸弹,缩图是第二道)· JPEG 魔数 · EXIF 方向 ·
`source` 与 `geo_status` 枚举。

### 3.7 凭证的渲染与持久化

**不能放 `tool-calls.tsx`**(§1.5),**也不能自己开取图端点**(§1.3)。v4:

- **`POST /checkin` 的 200 响应带 `artifact_id`**;打卡组件拿到后**自己渲染凭证卡片**,
  图走 `<img src={`${ARTIFACT_BASE}/by-id/${artifact_id}`}>` —— 与 `human.tsx:213` 同一条路。
- **`GET /checkin/recent?limit=N`**(同样返回 `artifact_id`)—— 刷新页面、换设备、
  重开线程后凭证还在。v2 承诺「凭证 + 查」却只有组件瞬时状态,是个真缺口。
- **`artifact_id` 为 NULL 时渲染「凭证图已过期清理」,不渲染 `<img>`。**
  这条要做成 `checkin-lib.ts` 的纯函数并有 vitest —— 漏了就是个裂图标,
  而裂图标和「图真的没了」在界面上长得一模一样。
- **没有 `/checkin/photo/{receipt_no}`。** 它在 v3 里存在,v4 删掉,理由见 §1.3。

> **口径差异要写进 §3.10 和 TODO-36:** `artifact_id` 出现在**直连 HTTP 响应**里,
> 但**不出现在 Agent 的工具返回值**里。最小化约束的是喂给 LLM 的东西,不是本人的接口。

### 3.8 `install_new_file()` 的语义

```
目标不存在        → 拷贝,log_ok
目标存在且内容相同 → 跳过,log_skip
目标存在且不同     → **覆盖并 log_warn**,警告文案写明「本文件由本仓管理,
                     若上游新增了同名文件请人工核对」
```

**为什么是覆盖而不是拒绝:** 这些文件是**本仓自有**的,不是上游的补丁点。
拒绝会让脚本永远无法更新自己的文件;跳过会留下旧版本且看不出来。
覆盖 + 警告是唯一「既能更新又能发现撞名」的组合。

### 3.9 留存与清理

- **`ArtifactKind` 加 `ATTENDANCE`** —— 必须改 `artifacts.py`(v2 文件清单漏了它)。
  不加就无法把考勤图与其它业务照片分开,清理器不敢动手。
- **`artifacts.py` 加删除 API** —— 现在只有 `register`/`resolve`/`read_meta`。
- **`attendance/cleanup.py` 是一个真模块** —— v3 只有 `test_attendance_cleanup.py`
  而没有被测对象(§1.10 末行),v4 补上。
- **找孤儿图:一次 SELECT 取回全部在用的 `artifact_id` 建成集合,再扫目录做差集。**
  **不许**在目录遍历里逐个查库 —— CLAUDE.md 明文红线:「列表查询一次取回,
  不许在循环里逐行查」。清理器跑在定时任务里,**慢了也没人看见,会一直慢下去**。
  一行是 32 字符的 id,十万行也就几 MB,这个量级下内存不是问题。
- **老化窗口**(如只删 `created_at` 早于 1 小时且无 DB 引用的),
  否则会误删 register 成功但 INSERT 还没完成的正常请求的图。
- 清理后 `artifact_id` 置 NULL、`photo_purged_at` 记时间,界面显示「凭证图已过期清理」。
- **入口要有:** 一个 `make` 目标 + 容器里的定时/启动任务,否则脚本写了没人跑。

> ⚠️ **照片留存 ≠ 数据留存。** 只清图片而不定义考勤行、姓名、坐标的删除期限,
> PDPO 的留存问题仍未解决 → TODO-37。

### 3.10 查询:聚合在 SQL 里,且必须有上限

| 工具 | 干什么 | SQL |
|---|---|---|
| `list_attendance_days(within, worker_name)` | 「张三这个月来了几天」 | `COUNT(DISTINCT work_date)` + `GROUP BY` |
| `list_attendance_detail(on_date, worker_name)` | 「今天谁到了」「张三今天几点打的」 | 按天取,**每人返回首末两次 + 次数**,`LIMIT` 封顶 |

- **「按人去重」与「张三今天几点打的」是矛盾的**(v2 没定义)。定死:
  **每人返回首次、末次、总次数**;要全部流水就明说「他今天打了 6 次」并让人再问。
- **必须有 `LIMIT` 与最大人数**。D10 允许无限打卡,「某天明细」可能是几百行。
- **最小化约定(只约束 Agent 侧):** 工具返回值**不含经纬度、不含 `artifact_id`**。
  ⚠️ 直连 HTTP 接口返回 `artifact_id` 是**刻意的**,两者不冲突(§3.7)。
  ⚠️ 但**姓名仍会进模型上下文** —— §1.8 已订正,别再说「跨境已解决」。

### 3.11 错误契约:三层不一致,前端必须知道

| 来源 | 长相 |
|---|---|
| Caddy 未登录 | 302 到 `/login`(`/api/*` 是 401 JSON `{"detail":…}`) |
| Caddy 413 | Caddy 自己的错误页,**不是 Envelope** |
| langgraph 鉴权中间件 | `{"detail":…}` |
| 进了 handler 之后 | **`Envelope`** |

**前端不能假设所有错误都是 Envelope。** `checkin-lib.ts` 要有一个归一化函数,
把这四种压成同一个形状再给 UI —— 它是纯函数,**vitest 测得到**。

### 3.12 文案集中(为 D12 破一次惯例)

打卡这条链的用户可见中文串全部收进 **`src/gyt/attendance/messages.py`**,不内联。

**⚠️ 是 `attendance/`,不是 `agents/attendance/`。** D15 之后打卡**写入路径一个 LLM 都不经过**,
而那条路径上的中文不少(409、限流、超大、缺字硬失败、「凭证图已过期清理」)。
文案若放在 `agents/` 下面,`checkin_api.py` / `db/attendance.py` / `watermark.py`
这三个与 Agent 无关的模块全得反过来 import Agent 包 —— **依赖方向反了**。

正确方向是单向:`agents/attendance` → `attendance` → `core`。
仓里已有同型约定:`core/focus.py` 那条 `SUPERVISOR_NAME` 就是「core 层不许反向 import 编排层」,靠注释约束。

> ⚠️ `agents/attendance/prompt.md` 里还有一份给模型看的文案,
> **换简繁时是两个地方** —— 两边头注要互相指认。

理由:负责人明说最后要换简繁,散在十几个分支的 f-string 漏一条就是混排,**没有测试会发现**。
**姓名是例外中的例外:原样存、原样画,任何情况下不进转换。**

---

## 4. 文件清单(40 个文件 / 五段)

> **⚠️ 增长曲线要摆在台面上:** v1 估 **17** → v2 **28** → v3 **39** → v4 **40**。
> 涨的不是需求,是**三轮复审暴露出来的必要工作**。
> v3→v4 净 +1:`attendance/cleanup.py`(v3 有测试没模块)与 `messages.py` 换包,
> 同时**删掉** `/checkin/photo` 这个端点 —— 净增只有一个文件,却少了一整条会裂图的路径。
>
> **计数口径:** 下面每段的括号数字是**文件数**,不是表格行数
> —— §4.3 的 eval 那一行覆盖 3 个文件。

### 4.1 新增 · 后端(10)

`src/gyt/checkin_api.py` · `src/gyt/db/attendance.py` ·
`src/gyt/attendance/{watermark,receipt,messages,cleanup}.py` ·
`src/gyt/agents/attendance/{__init__,tools,ranges}.py` · `src/gyt/agents/attendance/prompt.md`

### 4.2 新增 · 后端测试(6)

`test_checkin_api.py` · `test_db_attendance.py` · `test_attendance_watermark.py` ·
`test_attendance_tools.py` · `test_attendance_ranges.py` · `test_attendance_cleanup.py`

### 4.3 改动 · 后端与部署(11 个文件 / 9 行)

| 文件 | 改什么 |
|---|---|
| `backend/langgraph.json` | `http.app` + **`enable_custom_route_auth: true`** + `middleware_order: auth_first` |
| `backend/pyproject.toml` | **`fonttools` 提成显式依赖** |
| `src/gyt/core/artifacts.py` | **加 `ATTENDANCE` kind + 删除 API** |
| `src/gyt/config.py` | 字体候选路径 / **缩图长边上限** / 留存天数 / 限流参数 / 像素上限 |
| `src/gyt/graph.py` | `AGENT_REGISTRY` 追加一行,不动 `build_graph()` |
| `src/gyt/core/uploads.py` | **只补 `photo_max_mb`**(现存 bug)。打卡不走这里 |
| `backend/Dockerfile` | **`app` 阶段**装字体 |
| `Caddyfile` | **新增打卡路径专用 route + 它自己的 `request_body`;全局 128MB 不动** |
| `eval/{scorers.py,datasets/routing.csv,README.md}` | 名单同步(22 → 26 行) |

### 4.4 前端(8)

`checkin.tsx`(新)· `checkin-lib.ts`(新)· `qrcode.tsx`(新)· `thread-index.tsx`(改)·
`setup-frontend.sh`(改,加 `install_new_file()`)· `frontend-tests/{package.json,pnpm-lock.yaml,checkin-lib.test.ts}`(新)

⚠️ **二维码要有实现依赖。** 前端现在没有 QR 库,要么装一个,要么手写编码器 ——
后者必须补「真实可扫、纠错级别、长 URL」测试。**推荐装库。**

### 4.5 构建与文档(5)

`Makefile`(加 `test-frontend`)· `.github/workflows/ci.yml`(前端测试一步 + **装字体**)·
`scripts/login-page.html`(PICS)· `CLAUDE.md` · `TODOS.md`

### 4.6 为什么 `ranges.py` 不复用 `schedule/dates.py`

**语义相反:**「周三」在 schedule 是**未来**最近的周三,在考勤查询里是**过去**最近的周三。
共用一份词表必然有一边错。两个文件头注互相指认,防下一个人来「消除重复」。

---

## 5. 测试

### 5.1 覆盖图(实现时按它逐个划掉)

```
后端代码路径                                      用户流程 / 交互
[+] checkin_api.py                                [+] 手机打卡主流程
 ├── POST /checkin                                 ├── 真机 iOS Safari 取景→提交→看凭证
 │   ├── 无 X-Api-Key 被拒          ★★★ 闸①        ├── 真机 安卓 Chrome 同上
 │   ├── 限流:worker 桶 / 全局桶                    ├── 真机 iOS/安卓 WhatsApp 降级
 │   ├── 同键同 digest → 原记录     ★★★             └── 双击提交(sessionStorage 复用)
 │   ├── 同键异 digest → 409        ★★★
 │   ├── header digest ≠ 服务端算的 ★★★ 闸②        [+] 凭证可见性
 │   │      → 落库必须是服务端那个                   ├── artifact_id 为 NULL → 「已过期清理」★★★ 闸③
 │   ├── 超限流式断开,/tmp 无残留  ★★★             ├── 刷新页面后凭证还在(/checkin/recent)
 │   ├── JPEG 魔数 / EXIF / 解压炸弹                 └── 传大 DXF 图纸仍能成 ★★★ 闸④(回归)
 │   ├── 经纬度成对 / NaN / 范围
 │   └── INSERT 撞 UNIQUE → 回查那条                [+] 错误状态
 └── GET /checkin/recent(含 artifact_id)          ├── 未登录 302 / 401 两种形状归一
                                                   ├── Caddy 413(非 Envelope)归一
[+] attendance/watermark.py                        └── 断网重发 → 同一条,不重复记账
 ├── 字体全无 → 启动硬失败         ★★★
 ├── 缺字 → 指出哪个字(钉死 WQY)  ★★★             [+] 定位
 ├── .ttc 取错面(37707 vs 37708) ★★★ 闸⑤         └── 拒绝/超时/不支持/没发 → geo_status 六分支
 └── 缩图后峰值内存在预算内
                                                   LLM 侧
[+] attendance/receipt.py                          └── AGENT_REGISTRY 加一行 = 改了 supervisor
 ├── 篡改宿主 TZ,work_date 不变    ★★★ 闸⑥             提示词 → routing 必重跑(26 行)
 └── receipt_no 撞库重试

[+] attendance/cleanup.py
 ├── 老化窗口:register 成功但 INSERT 未完成的图不许删  ★★★
 └── 一次 SELECT + 内存差集,不在循环里查库

[+] db/attendance.py ····· 建表与读写
[+] uploads.py ··········· 三条老线回归(safety / inspection / cad)
```

### 5.2 六个闸的选取理由:**只有静默失败类进闸**

进 §11 的六条(闸①–⑥)共同点是**坏了没有任何信号**:

| 闸 | 坏了会怎样 |
|---|---|
| ① 无令牌被拒 | 端点裸奔,而且**不会有任何报错** |
| ② header digest 被当可信输入 | 伪造入口,库里的指纹是攻击者说了算 |
| ③ `artifact_id` NULL 渲染 | 裂图标,和「图真的没了」长得一模一样 |
| ④ 大 DXF 回归 | Caddy 413,查的人会去翻 `uploads.py`,方向全错 |
| ⑤ `.ttc` 取错面 | 就在那几个生僻姓名上画方块,其它字全对 |
| ⑥ `work_date` 随宿主 TZ 漂 | **`make test` 本机全绿也发现不了**(本机也是 UTC+8),只在换机器后暴露 |

其余(限流、`/checkin/recent`、`geo_status` 六分支、错误归一、魔数/EXIF/炸弹、
经纬度校验、UNIQUE 回查、`receipt_no` 重试)**全部要写,同分支跟上,由 `make test` + `make cov ≥80` 兜**,
但不单独列进闸 —— 它们坏了至少有个报错能查,而闸太长会让人在上线当天整段跳过。

### 5.3 测试闸目前是假闸,T7 必须一并解决

`scripts/frontend-tests/` 需要**锁文件**、`make test-frontend` 目标、**CI 里加一步**,
并且 **CI 要 `apt-get install fonts-wqy-microhei`**(§3.4 ③)。
否则「上线前 vitest 全绿」无人执行,而水印测试会在 CI 上直接起不来。

---

## 6. 同源清单增补(写进 `CLAUDE.md`)

| 一份真相 | 同步位置 |
|---|---|
| **`enable_custom_route_auth`** | `backend/langgraph.json`。**漏了它 = `/checkin` 完全不过 `auth.py`,而且没有任何报错** |
| 打卡请求字段契约(header 名 + Base64URL 编码 + **`X-GYT-Digest` 不可信**) | `checkin_api.py`(收)+ `checkin-lib.ts` 的 `buildCheckinHeaders`(发)+ `db/attendance.py`(存)。**任一处改名 = 静默少一个字段** |
| **凭证图的出口** | `POST /checkin` 与 `GET /checkin/recent` 的响应(带 `artifact_id`)+ `checkin.tsx` 的 `<img>` + 既有的 `ARTIFACT_BASE` 那条链。**没有 `/checkin/photo`,永远别再加** —— `<img>` 带不了 `X-Api-Key` |
| 凭证编号 `GYT-A-日期-时刻-后缀` | `attendance/receipt.py` + `prompt.md` + `messages.py`。**已实测与 report 的 `GYT-\d{8}-\d{6}` 不串**;反向约束:打卡要守卫必须写自己的模式 |
| 水印字体候选路径 | `config.py` + `Dockerfile` **app 阶段** + **`ci.yml` 的安装步骤**。⚠️ 绝不能加进 `base` |
| 业务时区 `Asia/Hong_Kong` | `receipt.py` / `db/attendance.py`。**不许靠宿主 `TZ`** |
| 打卡链的用户可见文案 | **`src/gyt/attendance/messages.py`**(不是 `agents/` 下面)+ `agents/attendance/prompt.md`。两边头注互指 |
| Caddy 请求体上限 | **全局 128MB(由 `GYT_DRAWING_MAX_MB` × 4/3 推出)** + **打卡 route 自己那条**。改 `GYT_DRAWING_MAX_MB` 要回去重算全局那行 |
| 新文件的安装方式 | `setup-frontend.sh` 的 `install_new_file()`。**`apply_override` 装不了新文件** |
| 前端文件数量 | `CLAUDE.md` 记**两个数**:`apply_override` **仍是十一件**(打上游的补丁),`install_new_file` **三件**(本仓自有)。⚠️ **不许合成一个数** —— 那句话的括号写着「以 `apply_override` 调用为准」,合并之后数出来永远对不上 |

---

## 7. 残余风险

| # | 风险 | 状态 |
|---|---|---|
| 1 | **「已登录员工」前提线上不成立。** 单一共享口令;`worker_name` 不是主键 —— 同名合并、简繁拆分 | D2/D4 裁决 → **TODO-35** |
| 2 | **权限模型缺失。** 任何登录者能查所有人;`event_id` 不是用户作用域;**`artifact_id` 现在也进 HTTP 响应了** | → **TODO-36**。复审定性为「上线前依赖」,已记下这个不同意见 |
| 3 | **PDPO:** 无同意记录;必要性/适度性/监察政策/查阅更正/负责人信息都欠;**姓名仍进模型上下文** | D13 裁决 → **TODO-37** |
| 4 | **自拍证明力有限。** 无实名、无活体、无设备证明、无服务器签名;水印由服务器生成,管理员可重造;**v4 起还降了分辨率** | 设计内已知。**定位是班组内部台账,不是防篡改证据** |
| 5 | 掉电时仍可能「库有行、图缺失」(无 `fsync`) | 低频且可人工修复;死链才是高频路径 |
| 6 | 夜班跨午夜的 `work_date` 语义未定义 | → **TODO-38** |
| 7 | `src/gyt/checkin_api.py` 能否被 `http.app` 按路径加载**未实测** | **实现第一天验**;不通就退回根目录并同步改 dev 挂载 |
| 8 | **限流的真天花板只有全局 30/分。** `worker_name` 桶可被客户端绕过 | 设计内已知,注释写死。根治归 TODO-3 |
| 9 | ARTIFACT_BASE 链断了不报错、无线索 | 沿用已知风险。**v4 把凭证图也压在这条链上,断了的影响面变大了** |
| 10 | **线上镜像比 main 少一个提交**(TODO-33) | 打卡上线前必须先收掉,否则「构建了但线上还是旧的」 |

---

## 8. NOT in scope

上下班两次/工时 · 补卡审批 · 月度统计/导出 · 地理围栏 · 逆地理编码 · 人脸识别/活体 ·
真用户体系(TODO-3)· 免登录扫码端点 · 全站简繁转换 · Playwright E2E · 地图展示 ·
多项目隔离(`project_id` 恒 NULL)· **原始分辨率凭证图**(§3.3 已说明取舍)

> ⚠️ **「多项目隔离」这条 2026-08-15 合流后变得显眼了**:队友的图纸/资料线
> 已经按工地隔离(`projects` 表 + `run_context` 注入),而考勤没有 ——
> 界面上一眼能看出不一致。仍不在本轮范围内,但已记 **TODO-39**,
> 那里写清了两侧「工地」根本不是一个东西(一个是表里的 id,一个是手打文本)、
> 补的话查询侧约 3 行而写入侧要改产品形态,以及**越晚做历史数据越难对**。

---

## 9. 并行泳道

**先做(阻塞,~20 分钟):** 冻结 §3.1 表结构 + `POST /checkin` 的 **header 字段契约**
(含 Base64URL 编码约定、`X-GYT-Digest` 的不可信约定),写进 `checkin_api.py` 模块 docstring。

| 泳道 | 内容 |
|---|---|
| **A · 接口与台账** | `checkin_api.py` + `db/attendance.py` + `receipt.py` + `messages.py` + 测试 |
| **B · 水印/产物/清理/镜像** | `watermark.py`(含缩图)+ `cleanup.py` + `artifacts.py`(ATTENDANCE + 删除 API)+ Dockerfile + config |
| **C · 前端** | `checkin.tsx` + `checkin-lib.ts` + `qrcode.tsx` + vitest + `install_new_file()` |

汇合(**单人串行**):`langgraph.json` + `graph.py` + 查询侧 + eval 三处 + `Caddyfile` + `Makefile`/`ci.yml` + `CLAUDE.md`。

---

## 10. 实施任务

> P1 = 阻塞上线。工时双标:人工 / Claude Code。**编号与正文引用一致,别再漂。**

- [x] **T1(P1,20min/5min)** — 冻结表结构与 header 字段契约(**含 `X-GYT-Digest` 与那句「不可信、不落库」**)
- [x] **T2(P1,1天/45min)** — `checkin_api.py`:**鉴权键 + 两个限流桶 + 流式收 body + 三层幂等**
  - 验收:不带 `X-Api-Key` 被拒;同键同 digest 返回原记录;同键异 digest 409;
    **header digest 与服务端不一致时落库用服务端那个**;超限流式断开
- [x] **T3(P1,5h/30min)** — `db/attendance.py`:`event_id`/`req_digest`/`geo_status`/`artifact_id` 可空
- [x] **T4(P1,6h/35min)** — `watermark.py`:**先缩图** + 候选探测 + **cmap 缺字校验(含 .ttc 取面)** + `fonttools` 提依赖 + Dockerfile **app 阶段**
- [x] **T5(P1,2h/15min)** — `receipt.py`:**显式 `Asia/Hong_Kong`** + 单一快照 + 撞库重试
  - 验收:**篡改宿主 `TZ` 后 `work_date` 不变**
- [x] **T6(P1,1天/50min)** — 前端自拍组件 + 降级 + 定位(**含 `geo_status` 六分支**)+ `sessionStorage` 的 event_id + **前端算 sha256** + `install_new_file()`
- [x] **T7(P1,5h/30min)** — vitest 栈 + **锁文件 + `make test-frontend` + CI 一步 + CI 装字体**
- [x] **T8(P1,6h/35min)** — **`attendance/cleanup.py`**:`ArtifactKind.ATTENDANCE` + `artifacts` 删除 API + 老化窗口 + **一次 SELECT 差集** + make 目标
- [x] **T9(P2,4h/25min)** — 查询侧:`agents/attendance/{__init__,tools,ranges}.py` + `prompt.md` + **LIMIT**
- [x] **T10(P2,2h/15min)** — 挂载:`langgraph.json` + `graph.py` + eval 三处
  - 验收:`make eval SUITE=routing` ≥ 0.90(26 行)。
    **⚠️ 不许改 `GYT_PROMPT_VERSION` / `vision_prompt.md` / 视觉模型** ——
    那三样是视觉缓存键(TODO-11),动一个就是又一轮 22 分钟真花钱,而考勤和视觉链毫无关系
- [x] **T11(P2,3h/20min)** — 凭证渲染(**`<img>` 走 `ARTIFACT_BASE`,NULL 时显示「已过期清理」**)+ **`GET /checkin/recent`** + 二维码(**装 QR 库**)
- [x] **T12(P2,1h/10min)** — PICS 进 `login-page.html`(**用途/是否自愿/不提供后果/接收方类别/查阅更正/负责人信息**,六项齐)
- [x] **T13(P2,1h/10min)** — `Caddyfile` **新增打卡专用 route + 它的 `request_body`;全局 128MB 不动**
  - 验收:**传一张 >10MB 的 DXF 图纸仍能成功**(回归)
- [x] **T14(P2,1h/10min)** — 补 `uploads.py` 的 `photo_max_mb`(**现存 bug**)
- [x] **T15(P2,1h/10min)** — `CLAUDE.md` + `TODOS.md`(**含 §6 的两个前端文件数**)

---

## 11. 上线前必须过的闸

1. `make test` 全绿,`make cov` ≥ 80。
2. `make eval SUITE=routing` ≥ 0.90(26 行)。**没动 `GYT_PROMPT_VERSION`。**
3. **`make test-frontend` 全绿,且它真的在 CI 里跑,且 CI 装了字体。**
4. **真机核对表四种组合逐条勾:** iOS Safari / 安卓 Chrome / iOS WhatsApp / 安卓 WhatsApp。
5. **🔴 闸① `POST /checkin` 鉴权实测:** 不带令牌必须被拒。
   **`langgraph.json` 里没有 `enable_custom_route_auth: true` 就是裸奔,而且不会有任何报错。**
6. **闸② header digest 造假:** 传一个和照片对不上的 `X-GYT-Digest`,库里落的必须是服务端算的那个。
7. **闸③ 凭证图清理后:** 把一行的 `artifact_id` 置 NULL,界面显示「凭证图已过期清理」,**不是裂图标**。
8. **闸④ 回归:** 传一张 >10MB 的 DXF 图纸仍能成功(T13 动了 Caddy)。
9. **闸⑤ `.ttc` 取面:** 用一个双面 ttc,断言缺字检测查的是 Pillow 实际渲染的那一面。
10. **闸⑥ 时区:** 篡改宿主 `TZ` 后 `work_date` 不变。**这条本机全绿也可能是错的,必须显式改 TZ 跑。**
11. `scripts/preflight_vps.sh` 全组过(第 ⑤ 组:发布端口总数仍为 3)。
12. **先收掉 TODO-33(线上镜像落后 main 一个提交),再清 VPS 磁盘,再构建。别跑 `docker builder prune -af`。**
    字体在 app 阶段 —— **若发现 models 在重建,立刻停手,说明加错阶段了。**

---

## 12. 落地对账(2026-08-15 实施完成)

T1–T15 全部落地(上表已勾)。三条后端泳道 + 前端泳道 + 汇合并行完成,
以下是**与 v4 计划的偏差**与**实测结果** —— 本仓规矩:计划说了什么不算,跑出来什么才算。

### 12.1 与计划的偏差(4 条,全部是加法或细化)

| 偏差 | 为什么 |
|---|---|
| **+`src/gyt/core/access.py`**(文件数 40 → 42) | `checkin_api.py` 与 `auth.py` 都被 langgraph 按文件路径加载,互相 import 不可靠 —— 把 `TokenBucket`/有效令牌判定/取头逻辑抽到 gyt 包里,auth.py 只 re-export(`test_auth.py` 零改动 63/63 全绿,行为不变的证据) |
| **+`agents/attendance/guard.py`** | §4.1 漏列了守卫文件;判据与取舍已进 CLAUDE.md 防假账地图 |
| 客户端 `X-GYT-Digest` = **仅照片 sha256**,不是完整指纹 | 完整指纹的其余输入本来就全在 header 里,服务端自己拼 —— 前后端不用各写一份规范化逻辑,少一个漂移面。层①短路语义不变 |
| **handler 纵深防御自查令牌** | `enable_custom_route_auth` 漏配是「无报错的裸奔」,只有 handler 自查能被单测钉死(闸①);正常部署下 langgraph 层先拦(实测 401 来自那层) |

### 12.2 实测结果(全部真跑,非计划值)

| 闸 | 结果 |
|---|---|
| §11.1 `pytest` | **1107 通过 / 4 跳过 / 0 失败**(基线 889;4 跳过 = E2E 闸 + 钉死 WQY 的缺字用例在 macOS 预期 skip) |
| §11.2 routing | **26/26 = 100%**(门槛 0.90)—— 新 summary 拼进 supervisor 提示词**之后**实跑,R23-R26 全对、老 22 条零误伤;`GYT_PROMPT_VERSION` 未动 |
| §11.3 前端 | vitest **61/61**;`make test-frontend` 真跑通;CI 独立 job 已加(pnpm 钉 10.5.1);`make lint-ci` 两步全过 |
| §11.5 闸① | 单测钉死 + **真机实测**:本机起 `langgraph dev`(带令牌),无钥匙 `POST /checkin` → **401**(来自 langgraph 层,证明 json 键生效);带钥匙 → 200 |
| 闸②–⑥ | 全部有对应测试:header 指纹造假落库取服务端值 / `artifact_id` NULL 渲染纯函数 / 大 DXF 回归(Caddy 12MB 专用闸 + `caddy adapt` 路由树核过顺序)/ `.ttc` FontChoice 单源断言 / 篡改宿主 TZ 后 `work_date` 不变 |
| 端到端冒烟 | 起真服务五连:401 → 200(凭证)→ 重发同键同 digest 返**同一编号** → 假 digest **409** → recent 1 条;**真水印图肉眼验证**:姓名/地盤/时间/凭证/定位五行齐,汉字渲染正常 |
| 风险 #7 | 正式关闭:dev 日志原句 `Loaded custom app from ./src/gyt/checkin_api.py:app`,且有复刻 `spec_from_file_location` 的守门单测 |

### 12.3 留给上线日的(不是没做,是只能在那天做)

1. **§11.4 真机核对表**:iOS Safari / 安卓 Chrome / iOS WhatsApp / 安卓 WhatsApp 四种组合(要真手机)。
2. **VPS 部署**:先收 TODO-33(线上镜像落后 main),再清磁盘,再构建(`langgraph.json`/`pyproject`/`uv.lock` 都改了 = 必须重建镜像);`preflight_vps.sh` 全组(发布端口仍应为 3)。
3. **`make test-docker`** 容器内全量(同因:镜像未重建;本机 `.venv` 已 1107 全绿)。
4. 本机 dev 直连 `:2024` 的 **CORS**:`x-gyt-*` 自定义头会触发 preflight —— 公网同源无此事,本机联调若撞上,在 `langgraph.json` 的 http 块补 `cors` 配置(前端泳道交接单第 4 条)。

### 12.4 本机联调抓到的一个设计缺陷:首次打卡必然拿不到定位

**1107 个单测 + routing 26/26 全绿,都没抓到它** —— 因为没有任何自动化测试知道
「这台机器拿一次定位要 5.3 秒」。只有真机点一次才看得见。

**症状:** 第一次打卡,凭证水印上写「定位:超时没取到」,库里 `geo_status='timeout'`、
坐标三列全 NULL。

**诊断(做了个诊断页量的,不是猜的):**

| 场景 | 耗时 | |
|---|---|---|
| 冷启动首次 fix(权限已授,`enableHighAccuracy`) | **5344ms** | 3 秒预算在 3001ms 准时超时 |
| 系统缓存热了之后,同样 3 秒参数 | **2ms** | 秒回 |
| 精度 | **±35 米** | 分辨地盘绰绰有余,不需要更高 |

**根因是三条叠加,而且都出在同一个判断上:**

1. `GEO_TIMEOUT_MS = 3000` **一个数兼了两个职** —— 既当「定位本身的预算」,
   又当「提交前最多等多久」。这两件事的合理取值差一个数量级,合成一个必然顾此失彼;
2. 定位从**按快门**那一刻才起跑,零提前量;
3. 没设 `maximumAge`(默认 0)= **拒绝一切缓存、每次强制重新定位一遍**。

**改法:**

| 常量 | 值 | 管什么 |
|---|---|---|
| `GEO_ACQUIRE_TIMEOUT_MS` | 20s | 定位本身的预算(实测 5.3s,留 4 倍余量 —— 工地信号只会更差) |
| `GEO_SUBMIT_WAIT_MS` | 1.5s | 提交最多再等多久。**「不为定位挡住打卡」这条原则改由它保证** |
| `GEO_MAX_AGE_MS` | 60s | 接受一分钟内的缓存位置 |

加上**起跑点从「按快门」提到「面板一打开」** —— 用户框取景、打姓名的十几秒本来就是白等的。

一个容易写错的细节:提交超时用 `Promise.race`,**不是**给 `getCurrentPosition` 一个短
timeout。后者会**取消**那次定位,下次又从冷启动重来;race 只是不再等它,浏览器那边
照跑完、进系统缓存 —— 所以第二次打卡是热的。

**实测验证:** 同一台机、同一个人连打三次 —— 第 1 次(改前)`timeout`;
第 2、3 次(改后)`ok`,坐标真实、精度 ±35 米。
回归测试 7 条锁在 `checkin-lib.test.ts`,其中两条专门锁「三个常量不许合回一个数」。

> **这条对后面的启示:** W7 的验收清单里「真机核对表」原本只列了四种浏览器组合
> (§11.4),看的是**能不能跑通**。这次说明还要看**数值对不对** ——
> 定位、时间、精度这类「跑通了但值是空的」的东西,自动化测试天然看不见。
