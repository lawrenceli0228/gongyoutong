/**
 * 「每一步花了多久」的纯逻辑库 —— 解析后端推的 custom 事件 + 排版成一行字。
 *
 * ⚠️ 本文件必须保持**纯 TS、零依赖、零环境假设**(不 import react、不 import "@/…"、
 *    不碰 window / document):scripts/frontend-tests/ 那个独立 vitest 包按相对路径
 *    直接 import 它,多一个依赖那个包就装不动了。
 *    渲染在 GytTimingRows.tsx,接线在 stream-provider.tsx,两边都只调本文件的函数。
 *
 * 安装:scripts/setup-frontend.sh 的 install_new_file 拷到 frontend/src/lib/timing-lib.ts。
 * frontend/ 不进 git,**别直接改那边** —— 换台机器就没了。
 *
 * ===========================================================================
 * 事件契约(后端推什么,这里认什么)
 * ---------------------------------------------------------------------------
 * **唯一真相在 `backend/src/gyt/core/timing.py`**(`EVENT_KEY` + `emit_timing()`
 * 那个字典字面量),本文件是它的**跨语言镜像** —— 收敛不掉,只能手工对齐,
 * 与 `supervision-lib.ts` 镜像后端词表是同一种关系。
 *
 * 后端通过 LangGraph 的 custom 流推一个对象,形状固定:
 *
 *     { gyt_timing: {
 *         kind: "llm" | "tool",
 *         name: string,                    // "kimi-k3" | "analyze_site_photo"
 *         seconds: number,
 *         input_tokens: number | null,     // kind=llm 才有;命中缓存为 null
 *         output_tokens: number | null,
 *         reasoning_tokens: number | null, // 思考 token,没有为 null
 *         slow: boolean,
 *         ok: boolean,
 *     } }
 *
 * 🔴 **字段名是跨泳道契约,一个字节都不许在这边改。** 改名的表现不是报错,是
 *    `parseTimingEvent` 全部返回 null —— 界面上一行耗时都不出,而**控制台干净**、
 *    页面照开、测试(如果只测格式化)照绿。所以下面每个字段都有专门的用例钉着。
 *
 * ⚠️ 后端那边**八个键恒定存在**(`kind=tool` 时三个 token 字段是 `None` 而不是
 *    缺席,timing.py 头注写明)。这边**两种都收** —— null 和缺席在这里等价:
 *    多兜一种形状的成本是零,而赌它永远不缺席的代价是整块界面消失。
 *
 * ===========================================================================
 * 为什么解析要这么较真(而不是 `as GytTiming` 一把梭)
 * ---------------------------------------------------------------------------
 * `onCustomEvent` 是**所有** custom 事件的公共入口:今天走这条路的还有
 * langgraph-sdk 自己的 UI 消息(`{type:"ui",…}`),明天可能还有别人的。
 * 拿到什么都当耗时事件用的下场是界面上冒出 `undefined  NaNs` 这种行 ——
 * 而它同样不报错。所以判据是**白名单式的**:形状不对就返回 null,让调用方原样放过。
 *
 * ===========================================================================
 * 🔴 这份数据是**易失**的,别把它当历史记录
 * ---------------------------------------------------------------------------
 * custom 事件只在 run 进行中推一次,**不进线程状态、不进检查点** ——
 * 刷新页面 / 切回历史会话,这些行就没了(而聊天消息还在)。
 * 这是取舍不是缺陷:要留存就得让后端把耗时写进 state,那是另一件事。
 * 所以本文件的存储是一个 run 级的内存数组,`beginTimingRun()` 在新一轮开始时清空。
 */

// ---------------------------------------------------------------------------
// 契约类型
// ---------------------------------------------------------------------------

export type TimingKind = "llm" | "tool";

/** 一步的耗时。字段与后端 `gyt_timing` 一一对应,别加派生字段进来。 */
export interface GytTiming {
  readonly kind: TimingKind;
  /** 模型名或工具名。**英文标识符,任何情况下不做简繁转换。** */
  readonly name: string;
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
 * 把一个 custom 事件解析成一步耗时;**不是耗时事件就返回 null**。
 *
 * 入参是**整个 custom 事件**(带 `gyt_timing` 外壳),不是里面那层 ——
 * 这样契约边界只有一处,调用方不用先剥壳再判断。
 *
 * 两个字段的缺省取向是刻意的,别对调:
 *   · `slow` 缺省 **false** —— 「没说慢」不该染成慢,否则满屏黄色没人再当回事;
 *   · `ok`   缺省 **true**  —— 「没说失败」不该报失败。报错的代价比漏报高得多:
 *      工友看见一片红会以为活没干成,而其实巡检记录已经出好了。
 */
export function parseTimingEvent(raw: unknown): GytTiming | null {
  if (!isRecord(raw)) return null;
  const t = raw.gyt_timing;
  if (!isRecord(t)) return null;

  const kind = t.kind;
  if (typeof kind !== "string" || !KINDS.includes(kind)) return null;

  const name = t.name;
  if (typeof name !== "string" || name.trim() === "") return null;

  const seconds = t.seconds;
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return null;
  }

  return {
    kind: kind as TimingKind,
    name: name.trim(),
    seconds,
    inputTokens: toTokenCount(t.input_tokens),
    outputTokens: toTokenCount(t.output_tokens),
    reasoningTokens: toTokenCount(t.reasoning_tokens),
    slow: t.slow === true,
    ok: t.ok !== false,
  };
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

/** 供渲染层用的固定文案,集中在这儿,组件里不许再出现中文字面量。 */
export const TIMING_LABELS = Object.freeze({
  slow: LABEL_SLOW,
  fail: LABEL_FAIL,
  /** 折叠行的表头前缀,拼成「本輪 5 步 · 合計 24.3s」。 */
  headPrefix: "本輪",
  headStep: "步",
  headTotal: "合計",
  /** 读屏用(视觉上那一坨 emoji + 数字念不出意思)。 */
  ariaLabel: "本輪各步耗時",
});

/**
 * 秒数排版:一律一位小数,与后端推什么无关。
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
// run 级存储(零依赖的外部 store,渲染层用 useSyncExternalStore 订阅)
// ---------------------------------------------------------------------------
//
// 为什么是模块级 store 而不是 React context:写入方是
// `useStream({ onCustomEvent })` 里的一个**回调**(stream-provider.tsx),
// 它不在任何组件的渲染里,拿不到 context 的 setter。模块级 store 是两边
// 唯一能碰面的地面 —— 与 core/access.py 在后端那两个文件之间的角色一样。

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
 * 收一个 custom 事件。**不是耗时事件就返回 null 且什么都不做** ——
 * 调用方(stream-provider.tsx)据此把事件原样交给上游的 UI 消息分支。
 */
export function recordTiming(raw: unknown): GytTiming | null {
  const t = parseTimingEvent(raw);
  if (!t) return null;
  const next = [...rows, t];
  rows = Object.freeze(
    next.length > MAX_TIMING_ROWS ? next.slice(next.length - MAX_TIMING_ROWS) : next,
  );
  emit();
  return t;
}

/**
 * 新一轮开始:清空上一轮,并记下这一轮属于哪个会话。
 * 挂在 `useStream` 的 `onCreated` 上 —— 「重新生成」也走同一条 run 创建路径,
 * 挂在提交按钮那侧会漏掉它,表现是新旧两轮的行叠在一起、越叠越长。
 *
 * 已经是空的就不通知 —— 免得每次提问都白重渲染一次。
 */
export function beginTimingRun(threadId?: string | null): void {
  owner = threadId ?? null;
  clearRows();
}

/**
 * 换会话了就清掉 —— 上一轮的耗时行挂在别人的对话底下是**错的数据**,
 * 而它不会报错,只会让人以为这条会话刚跑过。
 *
 * 归属对不上才清(判据与竞态推演见上面 `owner` 的注释)。
 */
export function dropTimingsIfThreadChanged(threadId?: string | null): void {
  const next = threadId ?? null;
  if (owner === null || owner === next) return;
  owner = null;
  clearRows();
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
