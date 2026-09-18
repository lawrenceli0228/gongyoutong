/**
 * 「每一步花了多久」的纯逻辑库 —— 拼接口地址 + 解信封 + 排版成一行字。
 *
 * ⚠️ 本文件必须保持**纯 TS、零依赖、零环境假设**(不 import react、不 import "@/…"、
 *    不碰 window / document):scripts/frontend-tests/ 那个独立 vitest 包按相对路径
 *    直接 import 它,多一个依赖那个包就装不动了。
 *    渲染与轮询在 GytTimingRows.tsx,它只调本文件的函数。
 *
 * 安装:scripts/setup-frontend.sh 的 install_new_file 拷到 frontend/src/lib/timing-lib.ts。
 * frontend/ 不进 git,**别直接改那边** —— 换台机器就没了。
 *
 * ===========================================================================
 * 🔴 数据从哪来:直连接口,**不是**聊天流(2026-08-21 换掉的)
 * ---------------------------------------------------------------------------
 * 第一版走 LangGraph 的 custom 事件,靠 `useStream({onCustomEvent})` 收 ——
 * **一条都没显示出来过**,而且换回去会连带把另一个 bug 带回来:
 *
 *   · 值得看的模型调用**全在子图里**(safety 识图、schedule 记台账,都是
 *     create_supervisor 挂进去的独立 Pregel);
 *   · 子图里发的 custom 事件,必须请求方开 `streamSubgraphs: true` 才出得来;
 *   · 而一开它,子图的 `values` 事件也跟着出来 —— SDK 对每个 values 事件是
 *     **整份替换**主状态(`@langchain/langgraph-sdk` 的 `dist/ui/manager.js:447`
 *     那句裸 `return data`)。子图先推一份长的、父节点跑完再推一份短的
 *     (`output_mode="last_message"` 只回灌最后一条),于是子 Agent 说的话
 *     **先出现、再消失** —— 工友看见的是「话被收回去了」。
 *
 * 两件是同一个开关的两头,走聊天流只能二选一。所以耗时改走自己的入口,照 W7 打卡、
 * W10 监理操作台的先例:**操作台不是聊天产物,它有自己的入口和自己的数据源。**
 * 后端那侧的完整推演在 `backend/src/gyt/core/timing.py` 的模块头注。
 *
 * ===========================================================================
 * 记录契约(后端存什么,这里认什么)
 * ---------------------------------------------------------------------------
 * **唯一真相在 `backend/src/gyt/core/timing.py` 的 `RECORD_KEYS`**,本文件是它的
 * **跨语言镜像** —— 收敛不掉,只能手工对齐,与 `supervision-lib.ts` 镜像后端词表
 * 是同一种关系。
 *
 *     GET /timing?thread_id=<会话号>&since=<游标>
 *     → { ok, data: { records: [...], next_since }, user_msg, error_code }
 *
 *     每条 record:
 *       seq: number,                     // 单调递增,增量拉取的游标就是它
 *       kind: "llm" | "tool",
 *       name: string,                    // "kimi-k3" | "analyze_site_photo"
 *       agent: string | null,            // "inspection" | "schedule" | …(见 AGENT_LABELS)
 *       seconds: number,
 *       input_tokens: number | null,     // kind=llm 才有;命中缓存为 null
 *       output_tokens: number | null,
 *       reasoning_tokens: number | null, // 思考 token,没有为 null
 *       slow: boolean,
 *       ok: boolean,
 *
 * 🔴 **字段名是跨泳道契约,一个字节都不许在这边改。** 改名的表现不是报错,是
 *    `parseTimingRecord` 全部返回 null —— 界面上一行耗时都不出,而**控制台干净**、
 *    页面照开、测试(如果只测格式化)照绿。所以下面每个字段都有专门的用例钉着。
 *
 * ⚠️ 后端那边**十个键恒定存在**(`kind=tool` 时 `agent` 与三个 token 字段是
 *    `None` 而不是缺席)。这边**两种都收** —— null 和缺席在这里等价:
 *    多兜一种形状的成本是零,而赌它永远不缺席的代价是整块界面消失。
 *
 * ===========================================================================
 * 为什么解析要这么较真(而不是 `as GytTiming` 一把梭)
 * ---------------------------------------------------------------------------
 * 接口回来的是 JSON,而 JSON 是从网上来的:后端换版本、Caddy 转发到别的地方、
 * 登录会话过期换回一张 HTML 登录页 —— 拿到什么都当耗时记录用的下场是界面上冒出
 * `undefined  NaNs` 这种行,而它同样不报错。所以判据是**白名单式的**:
 * 形状不对就丢掉那一条,别的照收。
 *
 * ===========================================================================
 * 「本輪」与「最近」是两个词,别合并
 * ---------------------------------------------------------------------------
 * 记录存在后端,所以**刷新页面之后还在**(这一点比 custom 事件那版强)。但那时
 * 屏幕上那批行**不是这一轮跑出来的**,写「本輪 5 步」就是撒谎。所以存储里记了
 * 一个 `coldFill` 标记,表头据此在「本輪」和「最近」之间切。
 * 合成一个词的代价:工友刷新一下,就看见「本輪」挂着上一轮的数,而这一轮
 * 其实一步都还没跑。
 */

// ---------------------------------------------------------------------------
// 契约类型
// ---------------------------------------------------------------------------

export type TimingKind = "llm" | "tool";

/** 一步的耗时。字段与后端 `RECORD_KEYS` 一一对应,别加派生字段进来。 */
export interface GytTiming {
  /** 后端发的号。**渲染层不显示它**,它只用来做增量拉取与去重。 */
  readonly seq: number;
  readonly kind: TimingKind;
  /** 模型名或工具名。**英文标识符,任何情况下不做简繁转换。** */
  readonly name: string;
  /** 图里的 Agent 名(英文标识符)。查表见 `AGENT_LABELS`;取不到为 null。 */
  readonly agent: string | null;
  readonly seconds: number;
  readonly inputTokens: number | null;
  readonly outputTokens: number | null;
  readonly reasoningTokens: number | null;
  readonly slow: boolean;
  readonly ok: boolean;
}

/** 一行的语气 —— 决定配色。渲染层只认这三档,不许自己再判 slow/ok。 */
export type TimingTone = "fail" | "slow" | "normal";

// ---------------------------------------------------------------------------
// 接口地址
// ---------------------------------------------------------------------------

function stripTrailingSlash(base: string): string {
  return base.replace(/\/+$/, "");
}

/**
 * 拼 `GET /timing` 的地址。`apiBase` 的来路与监理那条一样
 * (本机 dev 直连 :2024;公网是 `${GYT_PUBLIC_ORIGIN}/api`,Caddy 剥前缀转发)。
 *
 * ⚠️ `since` **恒定写进 query,哪怕是 0**。后端把「不给」和「给 0」当成同一件事,
 *    所以省掉它不会出错 —— 但省掉之后,请求地址会随游标在「带 since」和「不带 since」
 *    之间跳,而浏览器/中间层的缓存是按完整 URL 分桶的。稳定的形状值一行代码。
 *
 * ⚠️ `threadId` 一律 `encodeURIComponent`:它是 uuid 时转不转一样,但它的格式归
 *    langgraph 管,不归我们管。
 */
export function timingUrl(apiBase: string, threadId: string, since: number): string {
  const params = new URLSearchParams();
  params.set("thread_id", threadId.trim());
  params.set("since", String(Math.max(0, Math.trunc(since))));
  return `${stripTrailingSlash(apiBase)}/timing?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

const KINDS: readonly string[] = ["llm", "tool"];

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * token 数的归一:只认**有限的非负数**,别的一律当 null(= 不渲染)。
 *
 * 为什么不认 0 以外的假值兜底:后端明说「命中缓存为 null」,而 `0` 是一个
 * **真实的观测值**(比如工具那一档本来就没有 token)。把 0 和 null 混成一档,
 * 界面上就分不出「没这个概念」和「量到了 0」。
 */
function toTokenCount(v: unknown): number | null {
  if (typeof v !== "number" || !Number.isFinite(v) || v < 0) return null;
  return Math.trunc(v);
}

/**
 * 把接口回来的**一条** record 解析成一步耗时;形状不对就返回 null。
 *
 * 两个字段的缺省取向是刻意的,别对调:
 *   · `slow` 缺省 **false** —— 「没说慢」不该染成慢,否则满屏黄色没人再当回事;
 *   · `ok`   缺省 **true**  —— 「没说失败」不该报失败。报错的代价比漏报高得多:
 *      工友看见一片红会以为活没干成,而其实巡检记录已经出好了。
 */
export function parseTimingRecord(raw: unknown): GytTiming | null {
  if (!isRecord(raw)) return null;

  const seq = raw.seq;
  if (typeof seq !== "number" || !Number.isFinite(seq) || seq < 0) return null;

  const kind = raw.kind;
  if (typeof kind !== "string" || !KINDS.includes(kind)) return null;

  const name = raw.name;
  if (typeof name !== "string" || name.trim() === "") return null;

  const seconds = raw.seconds;
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return null;
  }

  const agent = typeof raw.agent === "string" && raw.agent.trim() !== "" ? raw.agent.trim() : null;

  return {
    seq: Math.trunc(seq),
    kind: kind as TimingKind,
    name: name.trim(),
    agent,
    seconds,
    inputTokens: toTokenCount(raw.input_tokens),
    outputTokens: toTokenCount(raw.output_tokens),
    reasoningTokens: toTokenCount(raw.reasoning_tokens),
    slow: raw.slow === true,
    ok: raw.ok !== false,
  };
}

/** 解信封的结果。失败时 `records` 恒为空数组,调用方不用判 null。 */
export interface TimingFetchResult {
  readonly ok: boolean;
  readonly records: readonly GytTiming[];
  /** 下一轮该带的游标。**解析失败时给 null,调用方据此保持原游标不动。** */
  readonly nextSince: number | null;
}

const FAILED: TimingFetchResult = Object.freeze({
  ok: false,
  records: Object.freeze([]) as readonly GytTiming[],
  nextSince: null,
});

/**
 * 解 `GET /timing` 的响应体。
 *
 * 🔴 **失败时 `nextSince` 给 null,不给 0。** 给 0 的话调用方一旦"顺手"用了它,
 *    下一轮就会把整段重新拉一遍,界面上表现为耗时行**成倍重复** —— 而两边
 *    都不报错。null 逼调用方显式决定"保持原样"。
 *
 * ⚠️ 单条 record 解析不出来只丢那一条,不整批作废:后端哪天多推一种 kind,
 *    整批作废等于界面上一行都没有,而只丢一条至少别的还看得见。
 *
 * ⚠️ 收的是**文本**不是已经 parse 好的对象:接口回来的可能压根不是 JSON
 *    (登录会话过期时 Caddy 会回一张 HTML 登录页),`JSON.parse` 那一下必须
 *    在我们自己的 try 里,不能让它抛到轮询循环里去。
 */
export function parseTimingEnvelope(bodyText: string): TimingFetchResult {
  let body: unknown;
  try {
    body = JSON.parse(bodyText);
  } catch {
    return FAILED;
  }
  if (!isRecord(body) || body.ok !== true) return FAILED;

  const data = body.data;
  if (!isRecord(data)) return FAILED;

  const rawRecords = data.records;
  if (!Array.isArray(rawRecords)) return FAILED;

  const records: GytTiming[] = [];
  for (const raw of rawRecords) {
    const parsed = parseTimingRecord(raw);
    if (parsed) records.push(parsed);
  }

  const nextSince = data.next_since;
  if (typeof nextSince !== "number" || !Number.isFinite(nextSince) || nextSince < 0) {
    return FAILED;
  }

  return { ok: true, records, nextSince: Math.trunc(nextSince) };
}

// ---------------------------------------------------------------------------
// 排版(界面恒繁體 —— 下面这几个中文串在源码里就是繁體,不走运行时转换)
// ---------------------------------------------------------------------------
//
// 🔴 耗时行挂在**聊天主界面**上,是常驻的。按 CLAUDE.md「语言的两条规则」第②条,
//    常驻界面一律静态繁體:挂 useHantUI 等于让每个用户首屏拉 438 KB 字典,
//    包括从不看繁體的简体工友。`hant-ui-strings.test.ts` 有守卫钉着这条。
// ⚠️ 这几个字是 `opencc-js` 的 cn→hk 转出来的,不是手打的 ——
//    手打会打成错的繁體形,而那条守卫**只抓「还是简体」,抓不住「转错了」**。

/** 图标。四档判据写在 `iconFor` 上。 */
const ICON_FAIL = "⚠️";
const ICON_TOOL = "📋";
const ICON_THINKING = "🧠";
const ICON_LLM = "⚡";

const LABEL_INPUT = "輸入";
const LABEL_OUTPUT = "輸出";
const LABEL_REASONING = "思考";
const LABEL_SLOW = "慢";
const LABEL_FAIL = "失敗";

/**
 * Agent 名 → 界面上那个中文名。
 *
 * 🔴 **键是后端 Agent 名(`graph.AGENT_REGISTRY` 里那些),一律留英文** ——
 *    转了就再也匹配不上、永远落到兜底分支,而且**一行报错都不会有**。
 *    与 `GytStatusCards.tsx` 的 `AGENT_TO_CARD` 是同一批键,那边的注释同款。
 *
 * ⚠️ 这张表与 `GytStatusCards.tsx` 的四张卡**是跨文件镜像,收敛不掉**:
 *    那个组件按「四张能力卡」分组(safety/inspection/report 合成一张),
 *    这里按「哪个 Agent 在干活」逐个列 —— 粒度不同,合并会让其中一边失真。
 *    `timing-lib.test.ts` 有一条**直接读那个组件源码比对**的守卫,防两边漂
 *    (只断字面量挡得住这边被改,挡不住那边被改,而后者一样会让人对不上号)。
 *
 * ⚠️ 英雄链上报的是 `inspection` 而不是 `safety`/`report`:后端取的是命名空间
 *    **第一段**(理由见 `core/timing.py` 的 `_agent_name`)。这正是界面要的粒度 ——
 *    那两步在工友眼里就是「識隱患」这一件事。
 *
 * 认不出的名字**原样显示**(见 `agentLabelFor`),不吞掉:界面上冒出一个没见过的
 * 英文名,是"有人加了 Agent 而这张表没跟上"的唯一信号 —— 吞掉就再也没人发现。
 */
export const AGENT_LABELS: Readonly<Record<string, string>> = Object.freeze({
  // 「調度中樞」而不是「調度」:狀態行(GytStatusCards 的藥丸)叫它調度中樞,同一屏上
  // 不許有兩個名字(2026-09-18 設計審查 FINDING-005:同一個能力在界面上曾有 9 個名字)。
  // 🔴 這張表現在是**全站唯一**的 Agent 中文名:tool-calls.tsx 的交接行、ai.tsx 的折疊行
  //    都從這兒取(`AGENT_NAMES` 只是它的別名),別在別處再抄一份。
  supervisor: "調度中樞",
  inspection: "識隱患",
  safety: "識隱患",
  report: "識隱患",
  schedule: "排期",
  cad: "圖紙",
  knowledge: "規範",
  attendance: "考勤",
  supervision: "監理",
});

/** 供渲染层用的固定文案,集中在这儿,组件里不许再出现中文字面量。 */
export const TIMING_LABELS = Object.freeze({
  slow: LABEL_SLOW,
  fail: LABEL_FAIL,
  /** 折叠行的表头前缀,拼成「本輪 5 步 · 合計 24.3s」。 */
  headPrefix: "本輪",
  /**
   * 刷新之后补出来的那批用这个前缀 —— 它们不是这一轮跑的。
   * 两个词不许合并,理由见文件头注最后一节。
   */
  headPrefixCold: "最近",
  headStep: "步",
  headTotal: "合計",
  /** 读屏用(视觉上那一坨 emoji + 数字念不出意思)。 */
  ariaLabel: "本輪各步耗時",
});

/**
 * Agent 名 → 显示名。表里没有就原样返回,没有 agent 就返回 null。
 *
 * 原样返回而不是显示「未知」:「未知」对谁都没用,而一个眼生的英文名
 * (比如新加的 `procurement`)会让看见的人立刻想起这张表该补一行。
 */
export function agentLabelFor(t: GytTiming): string | null {
  if (!t.agent) return null;
  return AGENT_LABELS[t.agent] ?? t.agent;
}

/**
 * 秒数排版:一律一位小数,与后端存什么无关。
 *
 * 为什么不做「小于 0.1 秒就写 <0.1s」那种花样:那会让「0.0s」这个**真实观测**
 * 变成一句话术,而 0.0s 恰恰是有信息量的 —— 它说明这一步命中了缓存 / 根本没干活。
 */
export function formatSeconds(seconds: number): string {
  return `${seconds.toFixed(1)}s`;
}

/**
 * token 那一段:`輸入 2157 / 輸出 779(思考 596)`。
 *
 * 🔴 **null 的字段一个都不渲染** —— 不许退化成 "null" 或 "0"。
 *    命中缓存时 input/output 都是 null,那一整段就该消失(返回 null),
 *    而不是留一句「輸入 0 / 輸出 0」谎报这次真调了模型。
 *
 * 三种都没有 → null;只有思考 → 不加括号(括号是「附在输出后面」的意思,
 * 前面没东西可附时它只是噪声)。
 */
export function formatTokens(t: GytTiming): string | null {
  const parts: string[] = [];
  if (t.inputTokens !== null) parts.push(`${LABEL_INPUT} ${t.inputTokens}`);
  if (t.outputTokens !== null) parts.push(`${LABEL_OUTPUT} ${t.outputTokens}`);

  const head = parts.join(" / ");
  if (t.reasoningTokens === null) return head === "" ? null : head;

  const thinking = `${LABEL_REASONING} ${t.reasoningTokens}`;
  return head === "" ? thinking : `${head}(${thinking})`;
}

/**
 * 图标四档。**失败优先** —— 出了错就得一眼看见是哪一步炸的,
 * 这时候「它是模型还是工具」是次要信息(名字那一列还写着)。
 *
 * 🧠 与 ⚡ 的分界是 `reasoningTokens > 0`,不是模型名:
 * 契约里没有「这是视觉档还是文本档」这个字段,而**思考 token 是实测出来的事实**。
 * 分出这一档的理由很实在 —— 2026-08-20 那次实测里,视觉档 779 个输出 token
 * 有 596 个是思考,占 76%,工友在工地举着手机等的就是这一段。
 * 谁在思考、思考了多久,值得单独一个图标。
 */
export function iconFor(t: GytTiming): string {
  if (!t.ok) return ICON_FAIL;
  if (t.kind === "tool") return ICON_TOOL;
  if (t.reasoningTokens !== null && t.reasoningTokens > 0) return ICON_THINKING;
  return ICON_LLM;
}

/**
 * 一行的语气。**失败优先于慢**:又慢又失败的那一行,要说的是它失败了。
 *
 * 判据下沉到这里(而不是让组件写 `t.slow ? … : …`)是为了能单测 ——
 * 配色本身测不了,但「哪一行该是哪一档」测得了。
 */
export function toneFor(t: GytTiming): TimingTone {
  if (!t.ok) return "fail";
  if (t.slow) return "slow";
  return "normal";
}

/**
 * 合计秒数。
 *
 * ⚠️ 它是**各步之和**,不是这一轮的墙上时间:子 Agent 之间有排队和空档,
 *    真并行的话还会重复计。所以界面上写「合計」不写「耗時」——
 *    前者是加法的结果,后者会被读成「这一轮一共等了这么久」,那是另一个数。
 */
export function totalSeconds(rows: readonly GytTiming[]): number {
  return rows.reduce((sum, r) => sum + r.seconds, 0);
}

// ---------------------------------------------------------------------------
// 存储(零依赖的外部 store,渲染层用 useSyncExternalStore 订阅)
// ---------------------------------------------------------------------------
//
// 为什么是模块级 store 而不是 React context:写入方是轮询的那个 effect,
// 而读取方是同一棵树上的组件 —— 用 context 也行,但 store 让
// scripts/frontend-tests/ 那个包能脱开 React 直接测状态机,这是它存在的主要理由。

/**
 * 一轮最多留多少行。真实一轮撑死几十行,这个数是防「后端哪天推疯了」的闸:
 * 无上限的话,一个坏掉的循环能把内存吃到页面卡死,而**那之前不会有任何报错**。
 * 超了丢**最旧**的:一轮里最值得看的是最后那几步(慢在哪儿、错在哪儿)。
 */
export const MAX_TIMING_ROWS = 200;

const EMPTY: readonly GytTiming[] = Object.freeze([]);

let rows: readonly GytTiming[] = EMPTY;

/**
 * 这批行属于哪个会话。**它存在的唯一理由是躲开一个竞态**,不是为了做多会话缓存。
 *
 * 不记归属的话,清空只能挂在「threadId 变了」这个 effect 上,而新建会话时
 * `onThreadId` 是**在 run 开始之前**触发的(langgraph-sdk 的 submit:先建线程、
 * 再发 runs.stream)—— effect 排到什么时候刷不由我们定,刷晚了就把**正在流的
 * 那一轮**的行擦掉,表现是「跑着跑着耗时行突然全没了」,而且零报错、不可复现。
 *
 * 记了归属之后判据变成事实比对,与刷新时机无关:
 *   · 新建会话时 owner 还是 null(onCreated 没跑)→ 不清,躲开竞态;
 *   · 切到别的会话 owner !== 新 threadId        → 清,这正是要治的;
 *   · effect 刷晚了 owner 已等于新 threadId     → 不清,幂等。
 */
let owner: string | null = null;

/**
 * 增量拉取的游标。
 *
 * 🔴 **换会话必须归零**,因为 `seq` 是后端**全进程**的一个计数器,不是每个会话
 *    各数各的。带着上一个会话的游标去拉新会话,拿到的是「新会话里 seq 比它大的
 *    那部分」—— 也就是**开头那几步凭空少掉**,而没有任何报错。
 *
 * 而同一个会话里换一轮**不归零**:那正是「只显示本轮」的实现方式 ——
 * 行清掉、游标留着,下一轮拉回来的自然只有新的。
 */
let cursor = 0;

/**
 * 这批行是不是刷新之后补出来的(而非本轮跑出来的)。表头据此在
 * 「本輪」「最近」之间切 —— 两个词不许合并,理由见文件头注最后一节。
 */
let coldFill = false;

const listeners = new Set<() => void>();

function emit(): void {
  for (const fn of listeners) fn();
}

function clearRows(): void {
  if (rows === EMPTY) return;
  rows = EMPTY;
  emit();
}

/**
 * 收一批拉回来的记录。返回**实际收进去几条**(调用方拿它决定要不要记日志)。
 *
 * 🔴 **按 `seq` 去重**。轮询天然会重叠:上一轮的响应还在路上,下一轮已经带着
 *    没更新的游标发出去了(慢网络下必然发生)。不去重的表现是同一步出现两次,
 *    而它看起来非常像「模型真的被调了两次」—— 那会把人送去查一个不存在的 bug。
 */
export function ingestTimings(incoming: readonly GytTiming[]): number {
  if (incoming.length === 0) return 0;
  const seen = new Set(rows.map((r) => r.seq));
  const fresh = incoming.filter((r) => !seen.has(r.seq));
  if (fresh.length === 0) return 0;
  const next = [...rows, ...fresh].sort((a, b) => a.seq - b.seq);
  rows = Object.freeze(
    next.length > MAX_TIMING_ROWS ? next.slice(next.length - MAX_TIMING_ROWS) : next,
  );
  emit();
  return fresh.length;
}

/** 读游标 —— 轮询那侧每次拿它去拼地址。 */
export function getTimingCursor(): number {
  return cursor;
}

/**
 * 记下新游标。**只许前进**:接口回来的 `next_since` 理论上单调,但网络会乱序
 * (慢响应后到),退回去就等于把已经收过的那段再拉一遍。
 */
export function advanceTimingCursor(next: number | null): void {
  if (next === null || !Number.isFinite(next)) return;
  const value = Math.trunc(next);
  if (value > cursor) cursor = value;
}

/**
 * 新一轮开始:清空上一轮的行,游标**留着**(那正是「只显示本轮」的实现)。
 *
 * 挂在 `useStream` 的 `onCreated` 上 —— 「重新生成」也走同一条 run 创建路径,
 * 挂在提交按钮那侧会漏掉它,表现是新旧两轮的行叠在一起、越叠越长。
 */
export function beginTimingRun(threadId?: string | null): void {
  owner = threadId ?? null;
  coldFill = false;
  clearRows();
}

/**
 * 换会话了就清掉 —— 上一轮的耗时行挂在别人的对话底下是**错的数据**,
 * 而它不会报错,只会让人以为这条会话刚跑过。
 *
 * 归属对不上才清(判据与竞态推演见上面 `owner` 的注释)。游标同时归零,
 * 理由见 `cursor` 的注释:seq 是全进程一个计数器。
 */
export function dropTimingsIfThreadChanged(threadId?: string | null): void {
  const next = threadId ?? null;
  if (owner === null || owner === next) return;
  owner = null;
  cursor = 0;
  coldFill = false;
  clearRows();
}

/**
 * 标记「接下来这批是刷新后补的」。轮询那侧在**本会话还没跑过任何一轮**时调它。
 * 幂等 —— 已经标过就不再通知,免得每轮轮询白重渲染一次。
 */
export function markColdFill(): void {
  if (coldFill) return;
  coldFill = true;
  emit();
}

/** 这批行是不是刷新后补的。渲染层据此选表头前缀。 */
export function isColdFill(): boolean {
  return coldFill;
}

/**
 * `useSyncExternalStore` 的 getSnapshot。
 *
 * 🔴 **返回值必须在「没变」时保持同一个引用**,否则 React 会判定每次渲染
 *    快照都变了,进入无限重渲染(报的是 "The result of getSnapshot should be
 *    cached",而现象是页面卡死)。上面所有写入都是**整份替换**,不是原地 push,
 *    这条前提才成立 —— 别为了省一次拷贝改成可变数组。
 */
export function getTimings(): readonly GytTiming[] {
  return rows;
}

export function subscribeTimings(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

/** 只给测试用 —— 用例之间必须互不串味。 */
export function resetTimingStore(): void {
  rows = EMPTY;
  owner = null;
  cursor = 0;
  coldFill = false;
  listeners.clear();
}
