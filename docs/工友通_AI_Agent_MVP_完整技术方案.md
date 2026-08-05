# 工友通 AI Agent MVP — 完整技术方案

> 版本 v1.2 · 2026-08-05 · 由 `/plan-eng-review` 工程评审产出(20 项决策 D1-D20 逐条确认;v1.1 验证性复审采纳 2 项修正:BGE-M3 权重预烧、照片自动压缩)
> 源文档:《工友通_AI_Agent_MVP_核心设计.md》 · 团队:2 人 · 工期:3-4 周 · 目标:竞赛可演示 MVP

---

## 0. 一页总览

| 维度 | 结论 |
|---|---|
| 目标 | 建筑工地多智能体助手,竞赛演示 MVP,完整展示多 Agent 协同/多模态/RAG/工具调用/工作流自动化 |
| 范围 | 6 个 Agent 全保留,每个设硬深度上限(见 §2),7 个扩展 Agent 及 4 项 TODO 明确延后 |
| 技术栈 | Python + LangGraph;文本 DeepSeek、视觉 Kimi、向量本地 BGE-M3;前端用现成 agent-chat-ui |
| 架构 | Supervisor 动态路由 + 1 条确定性「巡检英雄链」子图;慢操作离线预处理;传引用不传值 |
| 质量 | 单测 24 + 集成 4 + E2E 7 + 评测集 3 套(带门槛);后端覆盖率 ≥80% 进 CI |
| 演示 | 本地一键启动 + 三层兜底(热点/响应缓存/录屏);评测门槛=里程碑验收条件 |
| 分工 | 双人垂直泳道:队友=检索线(Knowledge+CAD)、你=感知与产出线(Safety+Schedule+Report)+编排,每人端到端负责整个 Agent,都在练 Agent 开发 |

## 1. MVP 目标 → 实现载体映射

| 评分能力 | 实现载体 |
|---|---|
| Multi-Agent 协同调度 | LangGraph Supervisor + 6 子 Agent,调用轨迹在 chat-ui 实时可视化 |
| 多模态(图片/文档/CAD) | Safety(kimi-k3 视觉)、Knowledge(文档)、CAD(ezdxf 解析 + PNG 预览) |
| RAG 知识库问答 | Chroma + 本地 BGE-M3,回答强制携带来源文档+页码 |
| Tool Calling | 每 Agent 2-5 个工具函数,统一错误信封 |
| 工作流自动化 | 「巡检英雄链」确定性子图:照片→隐患→报告→docx |
| 完整 Web Demo | agent-chat-ui(现成)+ docker-compose 一键启动 |

## 2. 范围与深度上限(D6 锁定)

| Agent | 做到这就停 | 明确不做 |
|---|---|---|
| Supervisor | LangGraph supervisor + 交接工具,recursion_limit=8 熔断 | 动态创建 Agent |
| Safety | kimi-k3 视觉 + 结构化检查清单输出 + 30 图评测集(≥80%) | 自训模型(不达标才挂现成安全帽检测模型) |
| Knowledge | 10-20 份规范文档,检索+引用到页码,空结果防编造 | GraphRAG、微调 |
| Schedule | SQLite CRUD 工具 + 中文自然语言查询,站内提醒 | 短信/日历推送 |
| Report | 日报+巡检 2 个模板,docx 必做,PDF 尽量(失败降级) | 自定义模板编辑器 |
| CAD | 仅 DXF(演示 DWG 离线预转),4 类查询 + PNG 预览 | 3D、BIM、任意几何语义理解 |

**范围铁律:** 范围已锁定,后续实现阶段不重开谈判;新想法一律先进 TODOS.md。

## 3. 系统架构

```
                 ┌─────────────────────────────────────┐
                 │  agent-chat-ui(Next.js,现成不写)      │
                 │  聊天 · 文件上传 · 工具调用实时可视化        │
                 └─────────────────┬───────────────────┘
                                   │ HTTP / SSE 流式
                 ┌─────────────────▼───────────────────┐
                 │        LangGraph Server(Python)      │
                 │   ┌───────────────────────────┐     │
                 │   │ Supervisor(deepseek-v4-flash) │     │
                 │   │ 意图路由 + 结果综合             │     │
                 │   │ recursion_limit=8 熔断       │     │
                 │   └─┬────┬────┬────┬────┬────┬┘     │
                 │     ▼    ▼    ▼    ▼    ▼    ▼      │
                 │  Safety Know. Sched. Report CAD     │
                 │  (Kimi) (RAG) (SQLite)(docx)(ezdxf) │
                 │     │                  ▲            │
                 │     └──「巡检英雄链」子图──┘            │
                 │      照片→隐患分析→报告→docx(确定性)    │
                 └──┬───────┬────────┬────────┬────────┘
                    ▼       ▼        ▼        ▼
              DeepSeek/Kimi Chroma  SQLite   文件区
              API(外部)  +BGE-M3   业务数据  uploads/artifacts
                          (全本地)  (本地)   (传引用不传值)
```

### 关键架构决策记录(ADR)

| # | 决策 | 选择 | 理由一句话 |
|---|---|---|---|
| D5 | 技术栈 | Python + LangGraph + 现成 chat-ui | 2026 生产主流,成熟件搭底座,自研只花在亮点 |
| D6 | 范围 | 6 Agent + 深度上限 | 保协同演示价值,砍深度不砍数量 |
| D7 | 慢操作 | 离线预处理 + 小文件同步(图纸≤20MB/文档≤10MB/照片≤10MB 超限自动压缩) | 不为不存在的负载建队列 |
| D8 | 路由 | Supervisor 动态路由 + 确定性英雄链子图 | 演示可靠性靠确定性,智能叙事靠动态 |
| D9 | 部署 | 本地 docker-compose 主跑 + 三层兜底 | 演示日不可控因素当需求设计 |
| D10 | 鉴权 | 免登录单用户;安全基线拉满(§11) | 安全投入匹配实际暴面 |
| D11 | 仓库 | 单仓 + 按 Agent 分包 | 结构即分工,合并冲突≈0 |
| D12 | 基座 | W1 先落 core/ 四件套 | DRY 红线;基座≤4 文件防过度抽象 |
| D13 | 提示词 | 独立 prompt.md + config.py 集中常量 | 提示词当文档管,常量当配置管 |
| D14 | 测试 | 全套 + 80% 覆盖率 CI 门槛 | 宁多勿少,保住 W4 彩排时间 |
| D15 | 评测 | 门槛 = 里程碑验收条件 + 预定备案 | 提示词质量装硬性仪表盘 |
| D16 | E2E | 6 条用户流全自动化(Playwright) | E2E 就是可重复执行的彩排 |
| D17→D19 | 模型 | DeepSeek(文本)+ Kimi(视觉)+ BGE-M3(本地向量) | 负责人指定方向;视觉缺口/embedding 缺口提前补上 |
| D18 | 缓存 | 三处「算一次用多次」+ 版本化缓存键 | 一层设施:性能/账单/兜底三份收益 |
| D20 | 外部质询 | 跳过(负责人决定) | — |

## 4. 技术栈清单

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 后端全部 |
| 编排 | LangGraph(+ langgraph-supervisor) | supervisor 模式,LangSmith 可选观测 |
| 文本 LLM | deepseek-v4-flash(OpenAI 兼容 API) | 路由/任务/报告/知识综合默认模型 |
| 视觉 LLM | kimi-k3(原生视觉,`https://api.moonshot.ai/v1`) | Safety 识别、图纸 PNG 问答;**硬约束:DeepSeek 无视觉 API** |
| 工具调用备胎 | kimi-k3 | 某 Agent 工具调用评测不达标时 config 一行切换 |
| Embedding | BGE-M3(本地,FlagEmbedding/sentence-transformers) | 两家均无 embedding API;本地=免费+断网可用;**权重构建时经国内镜像源预烧进 docker 镜像,冷启动零下载** |
| 向量库 | Chroma(本地持久化) | 零运维 |
| CAD | ezdxf 1.4.x(DXF);ODA File Converter 离线预转 DWG | 绝不自己碰 DWG 二进制 |
| 业务库 | SQLite(建表预留 project_id) | 单用户演示足够 |
| 导出 | python-docx;LibreOffice headless 转 PDF(失败降级 docx) | 中文字体随模板走 |
| 前端 | langchain-ai/agent-chat-ui(Next.js) | 现成,只做品牌化微调 |
| 测试 | pytest + coverage(≥80% 门槛) + Playwright | 见 §8 |
| 交付 | docker-compose 一键启动 | 冷启动即用是 W4 验收项 |

> **2026-08 型号变更备忘(T1 开工时联网核实,已更新到本表):** 两家供应商都在近期换代过。
> DeepSeek 旧名 `deepseek-chat` / `deepseek-reasoner` 于 2026-07-24 宣布弃用,当前为 **`deepseek-v4-flash`**
> (1M 上下文,内置思考模式,$0.14/$0.28 每百万 token);路由等低延迟场景须显式关思考:
> `extra_body={"thinking": {"type": "disabled"}}`。Kimi 侧 `kimi-latest`(2026-01-28)、`kimi-k2`(2026-05-25)、
> `moonshot-v1` 系列(2026-08-31)已全部停用/退役,当前唯一在用为 **`kimi-k3`**(2.8T,原生视觉,1M 上下文),
> base_url 同时从 `.cn` 改为 **`https://api.moonshot.ai/v1`**。**结论:选型决策 D19 不变(文本 DeepSeek、视觉 Kimi),
> 只是型号随供应商换代刷新;型号全部集中在 `config.py`,后续再变一处改完。**

### 模型映射与换模规则(D19)

| 任务 | 模型 | 换模触发条件 |
|---|---|---|
| Supervisor 路由 / Schedule | deepseek-v4-flash | 路由评测 <90% → 试 kimi-k3 |
| Knowledge 综合 / Report 文笔 | deepseek-v4-flash | 人工评审文本质量不满意 → kimi-k3 |
| Safety 识图 / CAD PNG 问答 | kimi-k3 视觉 | 30 图评测 <80% → 挂开源安全帽检测模型做前置增强 |
| 向量化 | BGE-M3(本地) | 检索召回差 → 换 bge-large-zh / 混合 BM25 |

全部模型名只在 `config.py` 一处定义;`core/llm.py` 是双供应商客户端(两套 key/base_url)。

## 5. 仓库结构与编码规范(D11-D13)

```
gongyoutong/
├── docker-compose.yml            # 一键起(D9)
├── .env.example                  # GYT_DEEPSEEK_API_KEY / GYT_MOONSHOT_API_KEY;真 .env 不进 git
├── backend/
│   ├── src/gyt/
│   │   ├── graph.py              # ★ LangGraph 拼装+Supervisor+英雄链子图(X)
│   │   ├── config.py             # 全部常量:模型名/上限/熔断轮次/阈值——禁散落
│   │   ├── core/                 # 共享基座,W1 落地,仅限 4 文件(D12 护栏)
│   │   │   ├── llm.py            # 双供应商客户端+重试/超时/内容哈希缓存(D18/D9)
│   │   │   ├── errors.py         # 统一工具错误信封 {ok, data, user_msg}
│   │   │   ├── artifacts.py      # 文件引用注册表(传引用不传值,防路径穿越)
│   │   │   └── base_agent.py     # create_agent 薄工厂(≠自造框架)
│   │   ├── agents/               # 每 Agent 一包:prompt.md + tools.py + 测试
│   │   │   ├── knowledge/ (X) ── schedule/ (X)
│   │   │   └── safety/ (Y) ── cad/ (Y) ── report/ (Y)
│   │   ├── ingest/               # 离线预处理(D7):rag.py(X) / cad.py(Y) 分文件防冲突
│   │   └── db/                   # SQLite 模型(预留 project_id)
│   ├── tests/                    # 单测+集成+e2e/
│   └── eval/                     # 三套评测集+跑分脚本(进 CI)
├── frontend/                     # agent-chat-ui 定制(尽量少改)
├── data/demo/                    # 预置演示数据:照片30/规范文档/DXF样例(含GBK)
└── docs/                         # 本方案/演示脚本/彩排清单
```

**编码规范要点(实现阶段的红线):**
1. 工具函数一律返回 `{ok, data, user_msg}` 信封;`user_msg` 必须是给工人看的中文人话;Agent 提示词强制「工具报错如实转述,禁止编造成功」。
2. 提示词全部放各 Agent 的 `prompt.md`(中文、带注释);魔法数字全进 `config.py`。
3. 大对象(图片/图纸/docx)传 artifact ID,不进消息状态。
4. SQLite 全参数化;列表查询一次取全,禁循环内逐条查询;建表预留 `project_id`。
5. 缓存键 = 内容哈希 + prompt 版本号(防改提示词后命中旧缓存)。
6. `graph.py` 只归 X 改;`ingest/` 按 rag.py/cad.py 分人,不共享文件。
7. 复杂逻辑处内嵌 ASCII 图注释:`graph.py`(路由图)、英雄链子图(链路图)、`ingest/*`(管道图);改代码时同步改图,过期图当 bug 修。

## 6. 英雄链数据流(D8)

```
用户上传工地照片(chat-ui)
      │ artifact_id
      ▼
[Safety Agent · kimi-k3 视觉]
  结构化检查清单 JSON:{违规项[], 位置, 等级, 建议}
      │ 确定性边(代码写死,不经 LLM 决策)
      ▼
[Report Agent · deepseek-v4-flash]
  巡检报告模板填充:隐患汇总+整改建议+责任建议
      │
      ▼
python-docx 渲染 → artifacts/ 落盘 → chat-ui 下载卡片
(每步流式播报进度;任一步失败→错误信封→中文提示,不静默)
```

其余请求走 Supervisor 动态路由(展示多智能体自主性);英雄链为确定性子图(保演示 100% 接力)。

## 7. 演示与兜底(D9)

- **一键启动:** `docker-compose up` 冷启动→首条消息可答(W4 验收项)。
- **三层兜底:** ①手机热点备网络;②`core/llm.py` 内容哈希响应缓存——彩排跑过的演示路径断网可复演;③全程备份录屏。
- **演示脚本(docs/demo-script.md,W4 产出):** 开场问答(Knowledge 引用)→ 英雄链(照片→报告→下载)→ CAD 查询(尺寸+PNG)→ Schedule 自然语言查任务 → 展示 Supervisor 轨迹可视化收尾。彩排 ×2,通过率要求 100%。

## 8. 测试计划(D14-D16)

**方法:** 工具函数/解析器走 TDD(先写失败测试);提示词走 eval-first(先定评测集与门槛再调 prompt)。

**覆盖图(绿地,39 项 = 38 自动化 + 1 手动清单,全部随实现落地):**

```
[核心] llm.py:正常/超时重试/429退避/缓存命中写入 ······· 单测×4
[核心] artifacts.py:往返/坏ID/路径穿越拒绝 ············· 单测×3
[编排] 路由 20 条中文用例(门槛≥90%) ··················· EVAL
[编排] 熔断生效 / 未知意图澄清 ·························· 单测×2
[英雄链] mock 全链出 docx / 真实链路 ····················· 集成+E2E
[Safety] 30 图评测(门槛≥80%)/ 超限→压缩→通过 / 压不下→拒 / 损坏图 · EVAL+单测×4
[Knowledge] 20 问答对引用命中(≥80%)/ 空检索防编造 / 幂等 · EVAL+单测+集成
[Schedule] CRUD×4 / 中文日期 / 空结果 ··················· 单测×6
[Report] 模板渲染×2 / docx 可打开 / PDF 降级 ············ 单测×2+集成
[CAD] DXF解析 / GBK样例★ / 损坏文件 / DWG引导 / 4类查询×2 · 单测×8
[E2E] 英雄链 / 问答引用 / CAD查询 / 超限报错 / 断网兜底 / 重复发送 / 冷启动冒烟
[手动] 处理中刷新恢复 + 1440 投影截图 + 键盘可达抽查
合计:单测24 · 集成4 · E2E7 · 评测3套 · 手动清单1 | 后端覆盖率≥80% 进 CI
```

**评测门槛 = 里程碑验收(D15):** W2 末 Safety ≥80%(不达标→挂开源安全帽检测模型增强);W3 末路由 ≥90%、RAG 引用 ≥80%。E2E 断言只验结构(有隐患卡片/有下载链接/引用可见),不验 LLM 文案字面。
**回归规则:** 绿地无存量代码,无回归项。

## 9. 失败模式清单(全部三有:有测试/有处理/用户可见)

| # | 场景 | 测试 | 处理 | 用户所见 |
|---|---|---|---|---|
| 1 | LLM API 限流/超时 | 单测 mock | 重试+退避+缓存 | 「网络繁忙已重试」 |
| 2 | Supervisor 死循环 | 单测 | recursion_limit=8 熔断 | 友好报错 |
| 3 | GBK 中文 DXF | GBK fixture 单测 | ezdxf 编码探测 | 正常解析 |
| 4 | 损坏/伪装文件 | 单测 | 校验拒绝 | 中文人话报错 |
| 5 | 超限大文件 | E2E+单测 | 照片先自动等比压缩,压不下/其余类型超限拒绝(D7) | 明确提示+可继续对话 |
| 6 | RAG 空检索编造 | 评测+单测 | 强制「知识库无依据」 | 诚实答复 |
| 7 | PDF 转换失败 | 单测 | 降级 docx | 「已提供 docx」 |
| 8 | 演示断网 | E2E 兜底路径 | 三层兜底(D9) | 彩排路径可续演 |
| 9 | 重复点击发送 | E2E | 去重 | 单任务 |
| 10 | 工具失败被 LLM 圆谎 | 信封+提示词规则+评测抽查 | errors.py 统一信封 | 如实转述 |
| 11 | 新机器冷启动缺模型权重 | E2E 冷启动冒烟 | BGE-M3 权重预烧进镜像 | 正常启动,零下载 |

**关键缺口:0**(每条均有测试+处理+可见反馈)。

## 10. 里程碑与双人泳道

> **2026-08-06 分工重排:** 队友希望做 CAD + Knowledge,泳道整体换过。
> 原为「X=编排线 / Y=多模态线」的整块划分,现改为 **队友=检索线(Knowledge + CAD)、
> 你=感知与产出线(Safety + Schedule + Report)+ Supervisor 与英雄链**。
> 换法更优的原因:`ingest/` 两个文件都归队友(原本分属两人)、英雄链 safety→report
> 连同 `graph.py` 全在一人手里,**两人的共享写入点从 2 处降到 1 处**(只剩
> `AGENT_REGISTRY` 各加一行)。代价是队友不碰视觉、你不碰 RAG,靠每周交叉 review 补。
> W2 详细计划见 `docs/W2_执行计划.html`。

| 周 | 队友(检索线) | 你(感知与产出线 + 编排) | 验收(硬门槛) |
|---|---|---|---|
| W1 | 演示数据集与评测集(各自准备自己 Agent 的) | 仓库+CI+compose;core/ 四件套;Supervisor+熔断 | ✅ 已完成:compose up 冷启动 9 秒可对话 |
| W2 | Knowledge 完整交付;CAD 解析地基(DXF+GBK+索引落盘) | Safety 完整交付;Schedule 完整交付 | RAG≥80%;Safety≥80%(否则触发备案) |
| W3 | CAD 上层(4 类查询+PNG 预览) | Report(2 模板);英雄链确定性子图;Supervisor 真实路由;三处缓存 | 路由≥90%;7 条 E2E 全绿;**功能冻结** |
| W4 | 彩排×2+演示脚本+bug 修复 | 评测回归+录屏+一键启动打磨 | 彩排 100% 通过;(余力)TODO-1 云部署 |

> **W2 开工前两个前置(见 W2 计划 §02):** ① 收口 TODO-5(缓存/重试不在真实执行路径,
> 「断网兜底」目前是空的);② 评测集标注 —— 新分工后数据集责任跟着 Agent 走:
> 规范文档与 DXF 样例归队友,30 张标注照片归你。

**并行依赖表:**

| 步骤 | 模块 | 依赖 |
|---|---|---|
| 骨架/CI | 根、frontend | — |
| core 基座 | core/ | 骨架 |
| Supervisor | graph.py | core |
| Knowledge / Safety / Schedule / Report / CAD | agents/*(互不相交) | core |
| 英雄链 | graph.py + safety + report | Supervisor、Safety、Report |
| E2E 全绿 | tests/e2e | 全部 Agent |

**冲突旗标(按新分工更新):** 唯一的共享写入点是 `graph.py` 的 `AGENT_REGISTRY`——两人各加一行,约定「只加自己那行、不重排他人行、冲突了保留双方」;`graph.py` 其余部分仅你修改;`ingest/` 两个文件全归队友;`core/` 四件套改动需两人一致同意(D12 护栏);`config.py` 各自常量加在带注释的分区里。英雄链不再是跨人协作点(safety/report/graph.py 同属一人)。两人每周交叉 code review 对方一个 Agent——新分工下两人技术面各缺一块(队友不碰视觉、你不碰 RAG),这条从「好习惯」升级为「必要动作」。

## 11. 安全基线(不论部署形态,一律执行)

- API Key 只存 `.env`,不进 git(`.env.example` 占位)。
- 上传:扩展名白名单 + 大小上限(图纸 20MB/文档 10MB/照片 10MB,照片超限先自动等比压缩)+ 随机化存储路径(防路径穿越)。
- SQL 全参数化;错误信息不泄内部细节。
- 免登录仅限本机/内网演示;若执行 TODO-1 云部署,必须加全局访问口令。

## 12. NOT in scope(考虑过并明确不做)

| 项 | 理由 |
|---|---|
| 7 个扩展 Agent(材料/质量/风险/考勤/采购/天气/语音) | 源文档已定为后续扩展;MVP 聚焦 6 核心 |
| 大文件异步队列 | D7:为不存在的负载建基础设施 → TODO-2 |
| 用户体系/多租户 | D10:3-5 天换零评分 → TODO-3(建表预留 project_id) |
| DWG 在线转换 | D6:演示图纸离线预转即可 → TODO-4 |
| 自训视觉模型 | 评测驱动:kimi-k3 视觉 不达标才挂现成检测模型,绝不自训 |
| GraphRAG/微调/自定义模板编辑器/3D/BIM | 深度上限(§2),竞赛周期内收益不抵风险 |
| 云部署(主路) | D9:本地主跑;云为 W4 余力加分项 → TODO-1 |
| 多用户并发优化 | 单用户演示场景,SQLite 单写者足够 |

## 13. What already exists(复用清单,零重造)

| 子问题 | 复用 | 层级 |
|---|---|---|
| 多智能体编排/supervisor | LangGraph + langgraph-supervisor | [Layer 1] |
| 聊天前端(含流式/工具可视化/上传) | langchain-ai/agent-chat-ui,整个前端不写 | [Layer 1] |
| DXF 解析 | ezdxf(纯 Python,v1.4 稳定) | [Layer 1] |
| DWG→DXF | ODA File Converter(免费,离线预转) | [Layer 1] |
| 向量检索 | Chroma + BGE-M3 | [Layer 1] |
| docx 生成 | python-docx 模板 | [Layer 1] |
| 视觉识别 | kimi-k3 视觉 提示词方案;备案=开源安全帽检测模型 | [Layer 2,评测把关] |

自研只有三块:英雄链子图、CAD 查询工具集、六份 Agent 提示词——全部是竞赛亮点本身。

## 14. Implementation Tasks

- [ ] **T1 (P1, 人工~1天/CC~2h)** — 骨架 — 单仓+docker-compose+CI(pytest+80% 门槛+E2E job);镜像构建经国内镜像源预烧 BGE-M3 权重
- [ ] **T2 (P1, 人工~1天/CC~2h)** — core — 四件套:双供应商 llm.py(重试/缓存)、errors、artifacts、base_agent
- [ ] **T3 (P1, 人工~1天/CC~3h)** — 编排 — Supervisor+recursion_limit=8+路由评测集 20 条
- [ ] **T4 (P1, 人工~2天/CC~4h)** — Knowledge — ingest/rag.py(BGE-M3+Chroma+幂等)+引用页码+防编造+RAG 评测
- [ ] **T5 (P1, 人工~2天/CC~4h)** — Safety — kimi-k3 视觉 结构化清单+30 图评测+照片自动压缩+超限/损坏处理;开工前 pin 具体 vision 型号并实测图片限制
- [ ] **T6 (P1, 人工~3天/CC~1天)** — CAD — ezdxf 解析+GBK 样例测试+4 类查询+PNG 预览+索引落盘
- [ ] **T7 (P2, 人工~1天/CC~2h)** — Schedule — SQLite CRUD+中文日期解析+project_id 预留
- [ ] **T8 (P1, 人工~1天/CC~3h)** — Report — 2 模板 docx+PDF 降级
- [ ] **T9 (P1, 人工~1天/CC~3h)** — 英雄链 — 确定性子图+mock 全链集成测试
- [ ] **T10 (P1, 人工~2天/CC~4h)** — E2E — chat-ui 接入+7 条 Playwright(含冷启动冒烟)
- [ ] **T11 (P2, 人工~1天/CC~3h)** — 兜底 — 缓存兜底彩排路径+录屏+一键启动+演示脚本
- [ ] **T12 (P2, 人工~半天/CC~1h)** — 评测 CI — 三套跑分脚本进 CI,门槛红线

## 15. Sources(选型依据)

- [LangGraph in Production: Choosing and Building Multi-Agent Systems](https://subratpati.medium.com/langgraph-in-production-choosing-and-building-multi-agent-systems-c2b955f16429)(supervisor 主流地位与熔断footgun)
- [langchain-ai/agent-chat-ui](https://github.com/langchain-ai/agent-chat-ui) · [LangChain Docs: Agent Chat UI](https://docs.langchain.com/oss/python/langgraph/ui)
- [ezdxf ODA File Converter Support](https://ezdxf.readthedocs.io/en/stable/addons/odafc.html) · [mozman/ezdxf](https://github.com/mozman/ezdxf)
- [VLM 安全合规细粒度检测(裸用 VLM 精度不足,微调后 89.4%)](https://www.sciencedirect.com/science/article/abs/pii/S0957417424026368)
- [VLM+结构化输入的工地安全实时检测(87.6% 合规判定)](https://www.sciencedirect.com/science/article/abs/pii/S1474034625007827)

> 注:视觉研究数据基于 Qwen 系;本项目按负责人决定改用 kimi-k3 视觉,公开数据更少 → §8 的评测门槛与备案机制因此更为关键。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — (用户跳过 Outside Voice) | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | CLEAR (PLAN) | 14 issues(初审 12+复审 2), 0 critical gaps, 39 项测试计划全部入方案 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 0(D1-D20 全部裁决;D17 由负责人改向 DeepSeek+Kimi,经 D19 落地映射确认;v1.1 复审 2 项发现均已采纳)
- **VERDICT:** ENG CLEARED — ready to implement(范围/架构/测试/性能四节全过,评测门槛已绑定里程碑)
