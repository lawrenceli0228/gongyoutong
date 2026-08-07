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

## ~~TODO-5 让重试与缓存真正落在 Agent 执行路径上~~ ✅ 已收口(2026-08-06,W2 前置)
- **结论:选了方案 (b)** —— `langchain_core.globals.set_llm_cache` + 自实现 `BaseCache`
  (`llm.GytDiskCache`,复用现成的 `_cache_stamp` / `_cache_read` / `_cache_write`),
  由 `get_chat_model()` 幂等装载。`base_agent.py` / `graph.py` 一行未改。
- **为什么不是 (a):** 三组探针实测(见交付说明)表明 —— ① chat-ui 是流式的,而缓存查询在
  `_agenerate_with_cache` 里发生在流式分支**之前**,(b) 天然覆盖 astream / astream_events /
  `stream_mode="messages"` / v2 协议四条路径;② `llm_string` 自动含本次绑定的工具集,
  (a) 得手工把工具揉进键,漏一次就是「safety 的答案被 report 取走」,比没缓存更糟;
  ③ (a) 要覆写 `_agenerate` + `_astream` + `bind_tools` + `_get_ls_params` +
  `with_structured_output` 五个面,两人团队养不起。
- **额外必须做的一件事:** 算键前给 prompt 做归一化(`llm._strip_prompt_noise`)。langchain
  命中缓存时会往 `AIMessage` 上盖 `usage_metadata.total_cost=0`,这条被改过的消息进入下一轮
  prompt 会让第 2 轮键对不上 —— 不归一化的话彩排要跑三遍缓存才收敛。
- **验收:** `backend/tests/unit/test_llm_cache.py`,数的是**内层模型被调了几次**。
  已做变异测试:摘掉装载 / 摘掉归一化 / 键里去掉工具集,分别有 5 / 1 / 1 条用例转红。
- **剩下的欠账(转 TODO-9):** 自研退避的中文错误文案仍只在「直调」路径(`llm.ainvoke`)上生效;
  Agent 路径的重试由 `ChatOpenAI.max_retries` 承担,用户看到的是 openai SDK 的英文异常。
- **演示 checklist 新增一条:** `llm_string` 含 `request_timeout` / `max_retries` / `extra_body`,
  **彩排与演示的 `.env` 必须逐字一致**,改一个数字整份彩排缓存作废。
- **Context:** 完整说明与 ASCII 图在 `backend/src/gyt/core/llm.py` 顶部「图 0 / 图 0.5」。

## TODO-9 Agent 路径上的错误文案中文化(TODO-5 的剩余部分)
- **What:** `llm._classify` / `_user_msg` 那套「工人看得懂的中文人话」目前只覆盖 `llm.ainvoke`
  这条直调路径。Agent 路径的异常直接从 openai SDK 冒上来,是英文 traceback。
- **How:** 在图的出口(`graph.py` 或一层 middleware)统一捕获并过一遍 `_classify` / `_user_msg`,
  而**不要**再往模型层包一个代理 —— 那正是 TODO-5 评估后否掉的方案 (a)。
- **Cons:** 约半天。**Depends:** 无,可与 W2 并行。

## TODO-10 路径乙的重试是两层叠加的(自研退避 × SDK 退避)
- **What:** `llm.ainvoke`(路径乙,评测打分 / 知识综合的单次抽取走它)外面有
  `_invoke_with_retry`(1 + `llm_max_retries` = 4 次尝试),而 `get_chat_model` 造出来的
  `ChatOpenAI` 自己还带 `max_retries=3`(openai SDK 的指数退避)。撞上 429 时同一次
  **逻辑**调用最坏发出 4 × 4 = 16 个 HTTP 请求;30 条视觉评测(6 分钱一次)最坏 ≈ 29 元,
  而且自研退避本身要睡 1+2+4 秒,再叠 SDK 的退避,CI 的 `timeout-minutes: 30` 很容易被吃穿。
- **为什么没在 `_for_direct_call` 里顺手关掉(2026-08-06 实测):**
  `max_retries` 是在 `validate_environment` 里被烤进 openai 客户端的
  (`langchain_openai/chat_models/base.py:1265`),而 `model_copy` 不重跑它 ——
  副本用的还是同一个客户端。实测字段 3 → 0,但 `root_async_client.max_retries` 仍是 3。
- **临时办法:** 路径乙的调用方在**造模型时**就传 `get_chat_model("text", max_retries=0)`,
  让「自研退避 + 中文文案」成为唯一那层;路径甲维持 SDK 自带重试不变(那是它唯一的一层)。
- **进度(2026-08-07):Safety 这条路径已按临时办法收口。**
  `agents/safety/tools.py` 里写的是 `get_chat_model("vision", max_retries=0)`,
  并由 `tests/unit/test_safety.py::test_视觉调用走vision档且传了max_retries为0` 锁住 ——
  这行**看不出**有什么用,删掉也不会报错、不会变慢,只会在月底账单上多一个零,
  所以必须有测试守着。视觉档是三档里最贵的($3/M),它先收口收益最大。
  **仍未处理:** 评测 runner 与知识综合(队友泳道)的路径乙调用方还没传这个参数。
  谁先写到那儿谁顺手加上,照抄 safety 那行即可。
- **根治:** 把重试整体下沉到模型层(与 TODO-9 同一次改动里做最省事)。
- **Depends:** 无,但**在评测真正开始烧额度之前**必须处理。

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
- **What:** 缓存正文现在会连同答案回写 `meta`(键/模型/提示词版本/消息摘要/**答案摘要**),
  读侧逐项核对,能挡住「张冠李戴」「整份伪造」「改正文不改 meta」三种。
  (`answer_digest` 是 2026-08-06 补的:前四项全是关于**问题**的,一项都不核对答案 ——
  在补它之前,把 `message.content` 换掉、meta 一个字节不动,伪造内容就会零调用原样流出,
  连编 meta 都不用。)但仍挡不住「能改文件的人连 answer_digest 一起重算」。
- **Why:** `data/` 是宿主机绑定挂载,彩排缓存还会被打包在队员之间传递。
- **How:** 对整份 payload 做 HMAC,密钥从 `Settings` 取且**不落在同目录**。
- **Depends:** TODO-5 之后(缓存真正接进执行路径后这条才有实际意义)。

## TODO-11 视觉调用要 43 秒,演示日必须预热缓存
- **实测(2026-08-07,真调 kimi-k3 一次):** 一张 440×293、28KB 的小图,
  端到端 **42.89 秒**。而 `llm_timeout_s` 默认 60 秒 —— **只剩 17 秒余量**,
  网络抖一下就是 TIMEOUT。手机原图(4000×3000)只会更久。
- **同一次实测的另一面:** 第二次调用命中磁盘缓存,**0.005 秒**,结果逐字一致。
  也就是说延迟问题有现成解 —— 但只对**已经跑过的图**有效。
- **演示日铁律(与 TODO-5 那条 `.env` 铁律同级):**
  1. 彩排时把演示要用的每张照片都真跑一遍,把缓存焐热;
  2. 焐热之后**不许再改提示词** —— 缓存键里含 `prompt_version` 与提示词正文,
     改一个字整份彩排缓存作废,演示当场退回 43 秒/张;
  3. `data/` 是绑定挂载,缓存要跟着演示机走,别在别的机器上焐热。
- **待办:** ① `llm_timeout_s` 是否要为视觉档单独调高(现在两档共用一个值);
  ② 评估视觉档要不要也关思考模式(现在只有 text 档关,见 `llm.get_chat_model`),
  但视觉判断可能真需要推理,**要用评测分数说话,不能拍脑袋关**。
- **Depends:** 无。演示前必做。

## TODO-12 动图会绕过格式白名单,被当视频计费
- **What:** animated GIF / animated WebP 的 MIME 就是 `image/gif` / `image/webp`,
  **与静态图完全相同**(magic bytes 也一样)。`ALLOWED_IMAGE_EXT` 里有 `.webp`,
  所以 `agents/safety/tools.py` 的格式校验对动图**必然放行**。
  而月之暗面官方说动图**可能**被当作视频解码并按视频计费 token —— 一张动图的账单
  可以是静态图的几十倍,且完全静默。
- **为什么现在没修:** 挡动图只能读帧数(`PIL.Image.n_frames`),
  而 backend **没装 Pillow**(实测 `ModuleNotFoundError: No module named 'PIL'`)。
- **How:** 加 Pillow 依赖后,在 `analyze_site_photo` 的格式校验之后补一道帧数探测,
  `n_frames > 1` 就 `fail(FILE_UNSUPPORTED)` 或抽首帧。
- **顺带做掉的:** Pillow 一旦装上,同一处还能补两件事 ——
  ① **降采样**:图片按像素面积计 token,手机原图贴着 4K 线,
     官方建议先 resize 到 1080p~2K,这是真金白银的省(评测集的图都 ≤1600px,不受影响,
     所以这条只影响**演示**,不影响评测分数);
  ② **真伪校验**:现在只看扩展名,一个改名成 .jpg 的文本文件照样会被 base64 发出去。
- **Depends:** 无。风险主要在演示日(工人随手发的表情包/动图)。
