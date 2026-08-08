# Schedule Agent 落地计划（W3 · 你泳道的收官件）

> 2026-08-08 · 分支 `lawrence/schedule-agent` · 预算 CC ~2.5h（方案 T7 给 2h + 评测回归缓冲）
> 今天是 **2026-08-08（周六）**，本文所有日期示例按这一天推算。

## 一页结论

- **干什么**：工地任务台账 Agent——记任务、查任务、改期限、销项。中文相对日期（「下周三」「后天」）**在代码里算，不让模型算**。
- **三层结构**：`agents/schedule/dates.py`（纯函数中文日历）→ `db/tasks.py`（SQLite CRUD）→ `agents/schedule/tools.py`（4 个单一职责工具）。
- **验收**：方案覆盖图的单测×6 起步（实际按 safety 惯例拉满，新模块行覆盖 100% 目标）；routing 从 12/22 → **15/22**（R12/R13/R14 翻绿，已绿 12 条零回归）；真机四步脚本通过。
- **协同亮点（零代码）**：巡检英雄链出完记录 → 用户说「给这条隐患记个整改任务，周五之前」→ supervisor 派 schedule。多轮对话天然打通，不写一行联动代码，评委看到的就是多 Agent 协同。

---

## 0. 方案里已锁死的口径（不重新讨论）

| 出处 | 内容 |
|---|---|
| 方案 §2 范围表 | SQLite CRUD 工具 + 中文自然语言查询 + 站内提醒；**短信/日历推送不做** |
| 方案 §4 选型 | 业务库 SQLite，建表**预留 `project_id`**（单用户演示足够） |
| 方案 §5 模型分派 | Schedule 走 `deepseek-v4-flash` 文本档（temp=0 已全局生效） |
| 方案目录树 | SQLite 模型放 **`backend/src/gyt/db/`**——不进 core（D12 护栏）、不藏在 agent 包里 |
| 方案红线 4 | SQL 全参数化；列表查询一次取全；禁循环内逐条查 |
| 方案覆盖图 | `[Schedule] CRUD×4 / 中文日期 / 空结果 ─ 单测×6`（这是**底线**不是目标） |
| config.py 现状 | `settings.sqlite_path` → `data/gyt.sqlite3` 已就位（父目录自动建，文件交给 sqlite 建） |
| 本计划裁定 | 「站内提醒」落地形态 = 查询结果里**主动标出已逾期/今天到期**，不做任何主动推送 |

## 1. 真实场景（为什么这块值得做）

- routing.csv 三条在等它：**R12** 查期限（「下周三之前还有哪些任务没完成」）、**R13** 建任务（「明天上午复检三层钢筋」）、**R14** 改期（「后天的模板验收改到周五」，不出现「任务」二字）。
- TODO-21 场景②（安全员日常巡检）：条文归 knowledge、**排整改期限归 schedule**，走真实路由——原规划内的 W3 联动。
- 演示脚本（方案 §7）已排「Schedule 自然语言查任务」一环。

## 2. 总体结构

```
用户:「后天的模板验收改到周五」
        │ supervisor: transfer_to_schedule
        ▼
schedule 本体(deepseek 文本档, temp=0, FocusOnOwnWork 中间件)
   │ ① list_tasks()                    ← 先查后改:找到 T3「模板验收」8-10
   │ ② reschedule_task("T3", "周五")   ← 日期原话透传,模型不换算
   │       │ dates.parse_due("周五", today=2026-08-08) → 2026-08-14
   │       ▼ db.tasks.set_due(3, "2026-08-14")   ← 参数化 SQL,每操作独立连接
   ▼
回复:「模板验收改到 8月14日(周五)了。」 ← 必须复述 due_display(代码算的,照抄)
```

三层各管一事，都可独立测：

| 层 | 文件 | 职责 | 测法 |
|---|---|---|---|
| 日历 | `agents/schedule/dates.py` | 中文日期短语 → `date`；注入 `today` | 表驱动纯函数测试 |
| 存储 | `db/tasks.py`（新包 `gyt/db/`） | 同步 sqlite3 CRUD，每操作一连接 | 直接调同步函数，`tmp_path` 库 |
| 工具 | `agents/schedule/tools.py` | 信封契约、参数校验、`asyncio.to_thread` 包存储层 | `ainvoke` 假库测试 |

## 3. 定案表（本计划新决策 + 理由）

| # | 决策 | 定案 | 为什么 |
|---|---|---|---|
| 1 | 日期谁算 | **代码算，模型禁止换算** | LLM 的星期数学不可靠；提示词明令「把用户原话传给工具，工具里有日历」；表驱动可测 |
| 2 | 工具形态 | 4 个单一职责：`add / list / reschedule / finish` | 弱模型参数越少越稳（safety 经验）；不做万能 `update_task` |
| 3 | 改期怎么定位 | **先查后改**，按 T 号改 | LLM 只搬编号（报告编号同哲学）；多条相似→把候选念给用户挑，不许猜 |
| 4 | 复述闭环 | 建/改后必须念出 `due_display`（「8月14日(周五)」，代码格式化） | 解析错了用户当场能纠——日期歧义的最后防线；模型照抄，杜绝它把 ISO 转错星期 |
| 5 | 任务号 | 整数 rowid，展示为 `T3`；工具收参剥 `T/t` 前缀 | 电话里报得清；容错放代码不放提示词 |
| 6 | db 并发姿势 | 同步 sqlite3 + 每操作独立连接；工具里 `asyncio.to_thread` | 无共享连接 = 无线程扯皮；过 blockbuster，不赖 `--allow-blocking` |
| 7 | 无期限任务 | `due NULL` 合法；期限查询时**单列一桶** | 「下周三之前」不该吞掉没定期限的活，也不该混在一起误导 |
| 8 | 逾期标注 | `list` 结果带 `overdue` 标志 | 「站内提醒」的落地形态 |
| 9 | finish 幂等 | 已完成再销 → `ok` + 「这条本来就完成了」 | 多轮重复指令不该报错吓人 |
| 10 | 时区 | 演示机本地日期（北京时间）；`today` 参数注入 | 测试可控；不引时区库 |
| 11 | project_id | 建表即预留、恒 NULL、**不进工具参数** | 方案要求预留；多项目 UI 是 YAGNI |

## 4. 数据模型（`db/tasks.py`）

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  due_date   TEXT,                 -- ISO YYYY-MM-DD;NULL = 无期限
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
  project_id TEXT,                 -- 方案预留,MVP 恒 NULL
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- 每个操作：`connect(settings.sqlite_path)` → `CREATE TABLE IF NOT EXISTS`（幂等、廉价）→ 参数化语句 → commit → close。无模块级连接、无全局状态。
- 存储层函数（同步、纯参数进出）：`create(title, due) -> int`、`fetch(task_id) -> Row|None`、`list_rows(cutoff, include_done) -> list[Row]`（一次取全）、`set_due(task_id, due)`、`set_done(task_id)`。

## 5. 工具契约（信封 `{ok, data, user_msg}`，与全项目一致）

| 工具 | 签名 | 成功 data | 典型失败（`user_msg` 必须是人话） |
|---|---|---|---|
| `add_task` | `(title, due="")` | `{task_id:"T7", title, due_date, due_display}` | 空标题；日期看不懂（列出支持写法） |
| `list_tasks` | `(within="", include_done=False)` | `{tasks:[{id:"T3",title,due_date,due_display,overdue,status}…], undated:[…], today}` | 日期看不懂 |
| `reschedule_task` | `(task_id, due)` | `{task_id, title, old_due, new_due, due_display}` | T 号不存在（提示先查清单）；日期为空/看不懂；任务已完成 |
| `finish_task` | `(task_id)` | `{task_id, title, was_done}` | T 号不存在 |

- `within="下周三"` → 截止 ≤ 2026-08-12 的未完成任务 + `undated` 桶单列。
- 排序：按期限升序、逾期在最前；`due_display` 全部由代码生成。

## 6. 中文日期词表 v1（冻结在 `dates.py` docstring，示例按今天=8-08 周六）

| 写法 | 解析结果 | 备注 |
|---|---|---|
| （空） | `None` | 无期限任务 |
| 今天/今日 · 明天/明日 · 后天 · 大后天 | 8-08 · 8-09 · 8-10 · 8-11 | |
| N天后 / N天内 | 3天后 → 8-11 | 两种说法同义处理 |
| 周X / 星期X / 礼拜X | 周三 → 8-12；周六 → **8-08（今天）** | **未来最近，含今天** |
| 本周X / 这周X | 本周日 → 8-09；本周三 → **报错** | 周一为一周起点；已过**报错不猜** |
| 下周X | 下周三 → 8-12；下周日 → 8-16 | 下个日历周 |
| 下下周X | 下下周三 → 8-19 | |
| 月底 / 本月底 · 下月底 | 8-31 · 9-30 | 当月最后一天 |
| X月X号 / X月X日 | 9月1号 → 2026-09-01 | **一律今年**；已过 → 报错提示补年份 |
| YYYY-MM-DD | 原样校验通过 | 假日期（2-30）报错 |
| 时段词 上午/下午/晚上/中午/早上 | 剥掉：「明天上午」→ 8-09 | 台账只到天粒度 |
| 之前/以前 后缀 | 剥掉：「下周三之前」→ 8-12 | 供 `within` 用 |
| 昨天/前天 等历史日期 | 不支持 | 台账记未来期限；补录历史 = YAGNI |

歧义约定（写进 docstring + prompt.md 各一份，措辞同源）：**「周X」= 未来最近含今天；「本周X」已过则报错**——宁可让用户换个说法，不做静默猜测。

## 7. 提示词要点（`prompt.md`）

- 身份：工地**任务台账员**。反甩锅段沿用 safety 措辞骨架——「记任务这件事就是你的活，后面没有人了」。
- 铁律五条：① 日期不许自己算，用户原话传给工具；② 改期/销项**先查后改**、必须带 T 号；③ 结果复述 `due_display`，一个字不改；④ 工具失败如实转述 `user_msg`；⑤ 多条相似任务把候选念给用户挑，不许猜。
- 台账外话题（照片/规范/图纸）不接——`create_gyt_agent` 统一挂的 FocusOnOwnWork 中间件 + 提示词双保险。

## 8. 路由接线（`graph.py` AGENT_REGISTRY 追加一行）

Summary 草案（正域饱满 + 免责尾巴，吸取 ping 三连教训）：

> 工地任务台账：记任务、改期限、销任务、查「某天之前还有啥没干完」。用户说「记一下／建个任务」「XX改到周五」「XX干完了」「下周三之前还有哪些任务」这类**安排活儿和期限**的话，派给它。它只管任务台账，不看照片、不答规范条文。

- 预期：R12/R13/R14 翻绿 → **15/22 = 68.2%**；剩余 7 条红全是 knowledge(4)/cad(3)，队友泳道落地后冲 0.90 门槛。
- 回归纪律：routing **全量重跑**逐行对比，已绿 12 条必须仍绿。重点盯 R06（「留个档」vs「记一下」的词法相近）；被钩走就按 ping 的处置流程改措辞重测。
- 成本：纯文本档，22 条一遍几分钱；**不碰视觉缓存**（vision_prompt.md / 视觉模型均未动）。

## 9. TDD 步骤（RED → GREEN，严格顺序）

| 步 | 内容 | 规模 |
|---|---|---|
| 1 | `test_schedule_dates.py` 表驱动 → `dates.py`。边界：周日说「下周三」、12月说「月底」、跨年月日、假日期 | ~30 例 |
| 2 | `test_db_tasks.py` → `db/tasks.py`。往返、过滤、`'; DROP TABLE` 当标题存原样、undated、project_id NULL | ~10 例 |
| 3 | `test_schedule_tools.py` → `tools.py`。信封、剥 T、坏号、空标题、解析失败人话、排序、overdue、幂等销项、改已完成拒绝、due_display | ~18 例 |
| 4 | `prompt.md` + `__init__.py`（`build_schedule_agent`，text 档）+ registry 接线 + `test_graph.py` 适配 | ~5 例 |
| 5 | `make eval SUITE=routing` 全量对比；真机四步脚本：记 → 查 → 改期 → 销项；协同一手：巡检后「记个整改任务」 | 真机 |
| 6 | ruff + 全套 + 覆盖率 → merge main | |

方案底线单测×6；本计划落点 **~60 条新增**（safety 惯例），新模块行覆盖 100% 目标、CI 总覆盖 ≥80% 不降。

## 10. 明确不做（记录在案，防范围爬行）

重复任务（「每周三例会」）· 主动推送提醒 · 删除任务（销项即闭环；记错就销掉重记）· 改标题 · 时刻粒度（上午/下午剥掉）· 多项目切换 · 整改自动关联（violation→task 外键——多轮对话已可演协同，外键是赛后活）· 历史日期补录。

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| R14 两步链（list→reschedule）是全计划唯一的多跳工具调用 | 提示词把流程写死 + 真机重点验证；若 flaky，备案 = 给 reschedule 加 keyword 参数（记录在案，不预先做） |
| 「记一下」与 inspection 的「留档」词法相近，可能钩走 R06 | 回归全量对比；犯了改 summary 措辞（有 ping 处置先例） |
| 日期歧义伤演示 | 复述闭环 + due_display 兜底；演示脚本只用词表内说法 |
| blockbuster 阻塞报错 | `asyncio.to_thread` 根治，不依赖 `--allow-blocking` |
| `data/gyt.sqlite3` 混进 git | 提交前验证被 .gitignore 覆盖 |

## 12. 验收清单

- [ ] 新增单测全绿；全套（448 + 新增）全绿；ruff 干净
- [ ] `dates.py` / `db/tasks.py` / `tools.py` 行覆盖 100%；CI 总覆盖 ≥80% 不降
- [ ] routing 全量 15/22；已绿 12 条零回归（逐行对比留档）
- [ ] 真机四步脚本通过 + 巡检→整改任务协同一手跑通
- [ ] TODOS.md 更新（场景②联动状态）；merge 到 main
