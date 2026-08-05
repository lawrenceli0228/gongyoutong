# TODOS — 工友通 AI Agent

> 由 /plan-eng-review 于 2026-08-05 评审时与负责人逐条确认后记录。
> 每条含完整上下文,三个月后捡起可直接开工。

## TODO-1 云部署 + 访问口令(W4 加分项)
- **What:** docker-compose 整栈部署到轻量云服务器(2C4G 起),Caddy 反代 + 全局访问口令中间件,公网可访问。
- **Why:** 评委扫码自己体验,超出「看演示」的印象分;同时是演示机故障时的第四层兜底。
- **Pros:** 传播性与印象分;异地队友/导师可随时看进度。
- **Cons:** 约 1 天运维;公网暴面需口令保护(D10 已定)防 API 账单被刷。
- **Context:** D9 决定「本地主跑+三层兜底」,云是 W4 有余力才做的加分项,不是主路。起步:租云→compose up→配口令。
- **Depends:** W3 末主线功能冻结后。

## TODO-2 大文件异步任务队列(生产化)
- **What:** RQ/Celery + Redis,把超限文件(50-200MB 图纸、批量文档)改为后台异步处理 + 进度查询。
- **Why:** 真实工地图纸经常远超 20MB 上限,商用化必需。MVP 阶段 D7 已明确砍掉。
- **Pros:** 任意大小文件可收;架构更接近生产级。
- **Cons:** 新增 Redis+Worker 两个进程;约 3-4 天(人工)/ 1 天(CC)。
- **Context:** `ingest/` 离线脚本天然可改造成 worker 任务,升级路径已在架构中预留。
- **Depends:** 赛后;若做 TODO-3 应先于或同期。

## TODO-3 用户体系与多项目隔离(商业化前提)
- **What:** 登录/注册、项目级数据隔离、角色权限(工人/安全员/项目经理)。
- **Why:** 真实交付必需;按项目计费的前提。MVP 阶段 D10 已明确砍掉。
- **Pros:** 产品形态完整,可对外交付。
- **Cons:** 3-5 天(人工)/ 1-2 天(CC);全部数据表加 scope 字段。
- **Context:** 本期建表规范已要求预留 `project_id` 字段(成本≈0),未来迁移平滑一半。
- **Depends:** 赛后;建议先于 TODO-2 或同期。

## TODO-4 DWG 自动转换
- **What:** 服务器安装 ODA File Converter,DWG 上传后自动转 DXF 进入解析管道(ezdxf `odafc` 插件已支持)。
- **Why:** 工地实际图纸几乎全是 DWG;MVP 当前为「请先离线转换」引导话术(D6 深度上限)。
- **Pros:** 去掉真实用户体验的第一块短板。
- **Cons:** ODA 跨平台安装/打包麻烦;转换耗时需配合异步。约 1-2 天。
- **Context:** 演示图纸已离线预转,比赛不受影响。
- **Depends:** 建议排在 TODO-2 之后。

---

> 以下由 T1 骨架交叉核对 / 三视角评审(2026-08-06)产出。已在代码里就地修掉的不再列,
> 这里只留**需要人拍板或超出 T1 范围**的欠账。

## TODO-5 让重试与缓存真正落在 Agent 执行路径上(W2 开工前必须收口)
- **What:** `gyt.core.llm` 的磁盘缓存与自研指数退避,目前**只有单测在调**。真实路径是
  `get_chat_model()` → 裸 `ChatOpenAI` → `create_agent` / `create_supervisor` 内部
  直接 `model.ainvoke()`,绕开了 `llm.ainvoke()`。
- **已做的止血:** `get_chat_model` 的 `max_retries` 已从契约写的 0 改回 `settings.llm_max_retries`
  (**这是对共享契约 v1 的一处刻意偏离,需团队追认**),保证真实路径至少有一层重试。
- **仍缺的:** 缓存不在路径上 → 「演示断网靠彩排缓存兜底」这个承诺目前是空的。
- **两条方案(二选一,别两条都上):**
  (a) 自定义 `BaseChatModel` 子类覆写 `_agenerate` 转调 `llm.ainvoke`,在 `get_chat_model`
      返回前套上;注意 `create_supervisor` 需要 `bind_tools`,包装层必须原样转发。
  (b) 换官方机制:`langchain_core.globals.set_llm_cache` + 自实现 `BaseCache`
      (复用现成的 `cache_key` / `_cache_read` / `_cache_write`),重试保持交给 langchain。
- **验收:** 补一个「真实 Agent 调用路径确实经过缓存/重试」的断言测试,否则这条链路会静默失效到演示日。
- **Context:** 完整说明与 ASCII 图在 `backend/src/gyt/core/llm.py` 顶部「图 0」。

## TODO-6 API Key 字段改用 `pydantic.SecretStr`(需改契约,要全员拍板)
- **What:** `Settings.deepseek_api_key` / `moonshot_api_key` 目前是裸 `str`。
- **已做的止血:** 两个字段加了 `Field(repr=False)`,`_ProviderConfig` 覆写了 `__repr__`,
  所以 `print/log(get_settings())` 不再泄明文 —— 这条泄露链(stdout → `docker compose logs`
  → CI 里 `cat langgraph.log` → 公开 Actions 日志)已经堵死。
- **仍缺的:** `model_dump()` 依然带出明文。
- **为什么没直接改:** 共享契约 v1 明文规定「字段名/类型/默认值一个字都不能改」,
  换成 `SecretStr` 属于改类型,要同步改所有取值点(`core/llm.py` 的 `_resolve_provider`
  与 `if not provider.api_key`,以及 `_ProviderConfig.api_key` 的标注)并更新契约文档。
- **Depends:** 越早越好,取值点越多越难改。

## TODO-7 LangGraph 服务端鉴权
- **What:** `backend/langgraph.json` 没有 `"auth"` 键,代码里也没有任何鉴权/限流,
  即 `:2024` 是一个零鉴权的 Agent 执行端点。
- **已做的止血:** `docker-compose.yml` 的端口发布已钉死 `127.0.0.1`,不再暴露给局域网。
- **仍缺的:** 一旦需要别的设备访问(手机扫码演示、云部署 TODO-1),就必须加
  `backend/auth.py` 并在 `langgraph.json` 里声明 `"auth": {"path": "./auth.py:auth"}`,
  用共享 token 挡住,顺带给 `/runs/*` 加个简单限流(`supervisor_recursion_limit`
  只限单次 run 的步数,不限 run 的次数,挡不住循环刷额度)。
- **Depends:** TODO-1 的硬前置。

## TODO-8 LLM 缓存条目加 HMAC 签名
- **What:** 缓存正文现在会连同答案回写 `meta`(键/模型/提示词版本/消息摘要),读侧逐项核对,
  能挡住「张冠李戴 + 整份伪造」。但挡不住「能改文件的人连 meta 一起编」。
- **Why:** `data/` 是宿主机绑定挂载,彩排缓存还会被打包在队员之间传递。
- **How:** 对整份 payload 做 HMAC,密钥从 `Settings` 取且**不落在同目录**。
- **Depends:** TODO-5 之后(缓存真正接进执行路径后这条才有实际意义)。
