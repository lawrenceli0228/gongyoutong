/**
 * 「每一步花了多久」的纯逻辑测试 —— 被测物 scripts/frontend-overrides/timing-lib.ts。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run timing-lib.test.ts
 *
 * ===========================================================================
 * 🔴 2026-08-21 整篇重写过一次:数据源从聊天流换成了直连接口 `GET /timing`
 * ---------------------------------------------------------------------------
 * 旧版测的是 `parseTimingEvent` / `recordTiming` —— 收 LangGraph 的 custom 事件。
 * 那两个导出已经没了,**别照着旧版的形状往回改**:走聊天流要开
 * `streamSubgraphs: true` 才收得到子图里的事件,而一开它,子图的 `values` 会
 * **整份替换**主状态,子 Agent 说的话先出现再消失(工友看见的是「话被收回去了」)。
 * 两件是同一个开关的两头。完整推演在被测文件头注「数据从哪来」那一节。
 *
 * ===========================================================================
 * 分七组,各守一件事
 * ---------------------------------------------------------------------------
 *   ① 地址       —— `since` 恒定出现、夹成非负整数、会话号必须转义;
 *   ② 单条解析   —— 十个字段任一对不上就返回 null(跨泳道契约的**唯一**测试);
 *   ③ 信封       —— 拿到一张 HTML 登录页也不许炸;坏一条不许作废整批;
 *   ④ 游标       —— 只前进不后退;换会话归零;换一轮**不**归零;
 *   ⑤ store      —— 按 seq 去重、乱序排好、上限截断、引用稳定、没新东西不通知;
 *   ⑥ coldFill   —— 「本輪」与「最近」两个词的判据;
 *   ⑦ 跨文件镜像 —— AGENT_LABELS ↔ GytStatusCards.tsx,**直接读那份源码**比对。
 *
 * 这一路上钉的几乎全是**静默出错**的东西:字段名漂了、游标退回去了、去重被
 * 「简化」掉了 —— 每一种的表现都是界面上少几行或多几行,而控制台干净、
 * 页面照开、后端日志正常。没有任何别的关卡会说话。
 *
 * ⚠️ 界面文案(輸入/輸出/思考/慢/失敗/本輪/最近)是**繁體**的,这里逐字断言 ——
 *    哪天有人手打成简体,`hant-ui-strings.test.ts` 会先红,但这条也跟着红,
 *    两道闸指向同一处,省得人以为只是守卫太严。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { beforeEach, describe, expect, it } from "vitest";

import {
  advanceTimingCursor,
  AGENT_LABELS,
  agentLabelFor,
  beginTimingRun,
  dropTimingsIfThreadChanged,
  formatSeconds,
  formatTokens,
  getTimingCursor,
  getTimings,
  iconFor,
  ingestTimings,
  isColdFill,
  markColdFill,
  MAX_TIMING_ROWS,
  parseTimingEnvelope,
  parseTimingRecord,
  resetTimingStore,
  subscribeTimings,
  TIMING_LABELS,
  timingUrl,
  toneFor,
  totalSeconds,
  type GytTiming,
} from "@/lib/timing-lib";

// ---------------------------------------------------------------------------
// 夹具
// ---------------------------------------------------------------------------

/**
 * 后端一条 record 的原样形状,**逐字镜像 `backend/src/gyt/core/timing.py` 的
 * `RECORD_KEYS`**(那个元组是契约的唯一真相,那边 `test_timing.py` 拿它逐键
 * 核对 `_record` 的产物)。
 *
 * 别在这儿"顺手改简" —— 这里是两条泳道之间唯一的形状测试,改简了它就不再证明
 * 任何事,而字段名漂移的表现是**界面上一行都不出、控制台干净**。
 */
function rawRecord(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    seq: 7,
    kind: "llm",
    name: "kimi-k3",
    agent: "inspection",
    seconds: 23.0,
    ok: true,
    slow: true,
    input_tokens: 2157,
    output_tokens: 779,
    reasoning_tokens: 596,
    ...over,
  };
}

/** 一个正常的成功信封(四键信封由 `core/errors.py` 的 `ok()` 生成)。 */
function okBody(records: unknown[], nextSince: unknown = 9): string {
  return JSON.stringify({
    ok: true,
    data: { records, next_since: nextSince },
    user_msg: "",
    error_code: null,
  });
}

/** 解析好的一步,给排版 / 图标 / store 那几组用。 */
function timing(over: Partial<GytTiming> = {}): GytTiming {
  return {
    seq: 1,
    kind: "llm",
    name: "deepseek-v4-flash",
    agent: "supervisor",
    seconds: 1.2,
    inputTokens: 2723,
    outputTokens: 29,
    reasoningTokens: null,
    slow: false,
    ok: true,
    ...over,
  };
}

/** 造第 n 步 —— store 那几组要按 seq 断顺序,名字跟着 seq 走方便一眼看出错位。 */
function step(seq: number, over: Partial<GytTiming> = {}): GytTiming {
  return timing({ seq, name: `step-${seq}`, ...over });
}

/**
 * 🔴 **每条用例前都把 store 归零。**
 *
 * store 是**模块级**的(不是 React context —— 那是它能脱开 React 被测的理由),
 * 所以用例之间天然串味:上一条留下的 owner / cursor / coldFill 会让下一条
 * 走进别的分支,而表现是"某一条单独跑是绿的、整文件跑是红的",最难查的那种。
 */
beforeEach(() => {
  resetTimingStore();
});

// ---------------------------------------------------------------------------
// ① 地址
// ---------------------------------------------------------------------------

describe("timingUrl 拼接口地址", () => {
  const BASE = "http://localhost:2024";

  it("正常一条长这样(路径固定 /timing,两个 query 参数)", () => {
    expect(timingUrl(BASE, "thread-a", 12)).toBe(
      "http://localhost:2024/timing?thread_id=thread-a&since=12",
    );
  });

  /**
   * 🔴 **`since=0` 也要写进去。**
   *
   * 后端把「不给」和「给 0」当同一件事,所以省掉它**不会出错** —— 正因为不出错,
   * 才最容易被当成多余的一截删掉。删掉之后请求地址会随游标在「带 since」和
   * 「不带 since」之间跳,而浏览器 / 中间层的缓存是按**完整 URL** 分桶的:
   * 第一轮和后面每一轮落在两个不同的桶里,命中率白掉一截,而没有任何报错。
   */
  it("🔴 since 恒定出现在 query 里,哪怕是 0(地址形状要稳定)", () => {
    const url = timingUrl(BASE, "thread-a", 0);
    expect(url).toContain("since=0");
    expect(new URL(url).searchParams.has("since")).toBe(true);
  });

  /**
   * 负数 / 小数是脏输入(游标理论上不会是,但它从 JSON 来)。夹成非负整数,
   * 而不是原样拼进去:`since=-1` 后端会拿去比大小,`since=3.7` 更是没法比 ——
   * 两种都不报错,只是拉回来的段不对。
   */
  it("负数 / 小数的 since 被夹成非负整数", () => {
    expect(timingUrl(BASE, "t", -1)).toContain("since=0");
    expect(timingUrl(BASE, "t", -999.9)).toContain("since=0");
    expect(timingUrl(BASE, "t", 12.9)).toContain("since=12");
    // 往零截断,不是四舍五入 —— 少拉一条会被下一轮补上,多跳一条就永远丢了
    expect(timingUrl(BASE, "t", 0.9)).toContain("since=0");
  });

  it("apiBase 末尾多余的斜杠会被剥掉(不然会拼出 //timing)", () => {
    expect(timingUrl("http://x/api/", "t", 0)).toContain("http://x/api/timing?");
    expect(timingUrl("http://x/api///", "t", 0)).toContain("http://x/api/timing?");
    expect(timingUrl("http://x/api", "t", 0)).toContain("http://x/api/timing?");
  });

  /**
   * 公网那条是相对地址(`${GYT_PUBLIC_ORIGIN}/api`,Caddy 剥前缀转发),
   * 本机 dev 是绝对地址。两种都要拼得出来 —— 这一条钉的是别把 `new URL(base)`
   * 之类的东西塞进实现(那会让相对地址当场抛)。
   */
  it("相对的 apiBase 也拼得出来(公网走 /api,Caddy 剥前缀)", () => {
    expect(timingUrl("/api", "t", 3)).toBe("/api/timing?thread_id=t&since=3");
  });

  /**
   * 🔴 会话号必须转义。它的格式归 langgraph 管、不归我们管:今天是 uuid
   * (转不转一样),哪天它带上斜杠或 `#`,裸拼进去的下场是斜杠变成**路径的一段**、
   * `#` 之后整个 query 被当成 fragment 丢掉 —— 请求打到一个不存在的地址,
   * 前端安静走开(非 2xx 不上屏),界面上就是一行耗时都没有。
   *
   * 断言写成**回读**而不是比对字面量:URLSearchParams 走的是
   * application/x-www-form-urlencoded(空格是 `+` 不是 `%20`),
   * 比字面量等于把那套编码细节钉死,而我们真正在乎的只有「原样收得回来」。
   */
  it("🔴 threadId 被转义(带 / 与中文的会话号也原样收得回来)", () => {
    const weird = "线程/a#b c";
    const url = timingUrl("http://x", weird, 0);

    expect(url, "裸斜杠会变成路径的一段").not.toContain("/a#b");
    expect(url, "中文没转义").not.toContain("线程");
    expect(url).toContain("%2F");

    expect(new URL(url).searchParams.get("thread_id")).toBe(weird);
    expect(new URL(url).pathname, "转义漏了就会多出几段路径").toBe("/timing");
  });

  it("threadId 两边的空白会被剪掉(粘贴会话号时常带空格)", () => {
    expect(new URL(timingUrl("http://x", "  t-1  ", 0)).searchParams.get("thread_id")).toBe("t-1");
  });

  it("只有两个参数,别的一个都不带", () => {
    const params = new URL(timingUrl("http://x", "t", 5)).searchParams;
    expect([...params.keys()].sort()).toEqual(["since", "thread_id"]);
  });
});

// ---------------------------------------------------------------------------
// ② 单条解析 —— 契约本身
// ---------------------------------------------------------------------------

describe("parseTimingRecord 单条解析:形状不对就返回 null", () => {
  it("认得出后端推的那一条完整记录(十个键)", () => {
    expect(parseTimingRecord(rawRecord())).toEqual({
      seq: 7,
      kind: "llm",
      name: "kimi-k3",
      agent: "inspection",
      seconds: 23.0,
      inputTokens: 2157,
      outputTokens: 779,
      reasoningTokens: 596,
      slow: true,
      ok: true,
    });
  });

  it("工具那一档:agent 与三个 token 全是 null", () => {
    const t = parseTimingRecord(
      rawRecord({
        seq: 8,
        kind: "tool",
        name: "analyze_site_photo",
        agent: null,
        seconds: 0.1,
        slow: false,
        input_tokens: null,
        output_tokens: null,
        reasoning_tokens: null,
      }),
    );
    expect(t).not.toBeNull();
    expect(t!.agent).toBeNull();
    expect(t!.inputTokens).toBeNull();
    expect(t!.outputTokens).toBeNull();
    expect(t!.reasoningTokens).toBeNull();
  });

  /**
   * 收的东西是从网上来的:后端换版本、Caddy 转发到别的地方、登录会话过期换回
   * 一张 HTML 页 —— 拿到什么都当耗时记录用的下场是界面上冒出 `undefined NaNs`,
   * 而它同样不报错。所以先把「压根不是个对象」这一层挡掉。
   */
  const NOT_RECORDS: ReadonlyArray<[string, unknown]> = [
    ["不是对象(数字)", 42],
    ["不是对象(字符串)", "kimi-k3"],
    ["是数组", [rawRecord()]],
    ["是 null", null],
    ["是 undefined", undefined],
    ["是空对象", {}],
  ];
  it.each(NOT_RECORDS)("%s → null", (_why, raw) => {
    expect(parseTimingRecord(raw)).toBeNull();
  });

  /**
   * 🔴 **这一组是字段名契约的唯一测试,十个字段逐个来。**
   *
   * 后端哪天把 `reasoning_tokens` 改成 `thinking_tokens`、把 `seq` 改成 `id`,
   * 表现不是报错,是 `parseTimingRecord` 全部返回 null → **界面上一行都不出**,
   * 而控制台干净、页面照开、后端日志正常。这里逐个钉住,漂了当场红。
   */
  const BAD_FIELDS: ReadonlyArray<[string, Record<string, unknown>]> = [
    // seq —— 增量拉取与去重全靠它,没有它这条记录进了 store 也没法定位
    ["seq 缺失(改名了?)", { seq: undefined }],
    ["seq 不是数(字符串)", { seq: "7" }],
    ["seq 是 NaN", { seq: Number.NaN }],
    ["seq 是 Infinity", { seq: Number.POSITIVE_INFINITY }],
    ["seq 是负数", { seq: -1 }],
    ["seq 是 null", { seq: null }],
    // kind —— 前端按它分流(工具那半边 / 模型那半边)
    ["kind 不在受控词表里", { kind: "vision" }],
    ["kind 缺失", { kind: undefined }],
    ["kind 不是字符串", { kind: 1 }],
    ["kind 大小写不对(白名单是全小写)", { kind: "LLM" }],
    // name —— 那一行上唯一能说清"这步在干嘛"的标识符
    ["name 是空串", { name: "" }],
    ["name 只有空白", { name: "   " }],
    ["name 不是字符串", { name: 123 }],
    ["name 缺失", { name: undefined }],
    // seconds —— 这整件事就是为了这个数
    ["seconds 不是数字", { seconds: "23.0" }],
    ["seconds 是 NaN", { seconds: Number.NaN }],
    ["seconds 是 Infinity", { seconds: Number.POSITIVE_INFINITY }],
    ["seconds 是负数", { seconds: -1 }],
    ["seconds 缺失", { seconds: undefined }],
  ];
  it.each(BAD_FIELDS)("%s → null", (_why, over) => {
    expect(parseTimingRecord(rawRecord(over))).toBeNull();
  });

  it("seq / seconds 的 0 是合法值 —— 它们不是「没有」", () => {
    const t = parseTimingRecord(rawRecord({ seq: 0, seconds: 0 }))!;
    expect(t.seq).toBe(0);
    // 0.0s 是有信息量的:这一步命中了缓存 / 根本没干活
    expect(t.seconds).toBe(0);
  });

  it("seq 是小数就往零截断(它要当 Map 的键和排序依据,得是整数)", () => {
    expect(parseTimingRecord(rawRecord({ seq: 12.9 }))!.seq).toBe(12);
  });

  it("name 两边的空白会被剪掉(不然界面上对不齐)", () => {
    expect(parseTimingRecord(rawRecord({ name: "  kimi-k3 " }))!.name).toBe("kimi-k3");
  });

  /**
   * agent 取不到是**正常路径不是故障**:路径乙(直调 ainvoke)和图外调用压根
   * 没有"节点"这个概念。所以它宽松 —— 认不出就给 null,那一段整个不渲染。
   */
  const BAD_AGENTS: ReadonlyArray<[string, unknown]> = [
    ["空串", ""],
    ["只有空白", "   "],
    ["不是字符串(数字)", 3],
    ["是 null", null],
    ["缺席", undefined],
    ["是对象", { name: "safety" }],
  ];
  it.each(BAD_AGENTS)("agent 是%s → null(整段不渲染,不硬塞「未知」)", (_why, agent) => {
    expect(parseTimingRecord(rawRecord({ agent }))!.agent).toBeNull();
  });

  it("agent 有值时两边空白剪掉", () => {
    expect(parseTimingRecord(rawRecord({ agent: "  schedule  " }))!.agent).toBe("schedule");
  });

  /**
   * 🔴 **两个缺省方向刻意不对称,别对调。**
   *
   *   · `slow` 缺省 **false** —— 「没说慢」不该染成慢,否则满屏黄色没人再当回事;
   *   · `ok`   缺省 **true**  —— 「没说失败」不该报失败。误报的代价比漏报高得多:
   *      工友看见一片红会以为活没干成,而其实巡检记录已经出好了。
   */
  it("🔴 slow 缺省 false、ok 缺省 true(这两个方向不许对调)", () => {
    const t = parseTimingRecord(rawRecord({ slow: undefined, ok: undefined }))!;
    expect(t.slow).toBe(false);
    expect(t.ok).toBe(true);
  });

  it("slow / ok 只认真正的布尔值,不认「truthy」", () => {
    const t = parseTimingRecord(rawRecord({ slow: "yes", ok: 0 }))!;
    expect(t.slow, "字符串「yes」被当成真 → 满屏都是慢").toBe(false);
    expect(t.ok, "数字 0 被当成假 → 满屏都是失败").toBe(true);
    // 反向也钉一下:真的 false / true 要收得住
    expect(parseTimingRecord(rawRecord({ slow: false }))!.slow).toBe(false);
    expect(parseTimingRecord(rawRecord({ ok: false }))!.ok).toBe(false);
  });

  /**
   * token 的**三态**:数字 / null(命中缓存、供应商不报用量)/ 缺席。
   * 后端承诺十个键恒定存在,但这边两种都收 —— 多兜一种形状的成本是零,
   * 赌它永远不缺席的代价是整块界面消失。
   */
  it("token 三态之一:是数字就照收", () => {
    const t = parseTimingRecord(rawRecord())!;
    expect(t.inputTokens).toBe(2157);
    expect(t.outputTokens).toBe(779);
    expect(t.reasoningTokens).toBe(596);
  });

  it("token 三态之二:显式 null → null", () => {
    const t = parseTimingRecord(
      rawRecord({ input_tokens: null, output_tokens: null, reasoning_tokens: null }),
    )!;
    expect(t.inputTokens).toBeNull();
    expect(t.outputTokens).toBeNull();
    expect(t.reasoningTokens).toBeNull();
  });

  it("token 三态之三:整个键缺席也当 null(不赌后端永远带着它)", () => {
    const t = parseTimingRecord({
      seq: 3,
      kind: "tool",
      name: "analyze_site_photo",
      seconds: 0.1,
    });
    expect(t).toEqual({
      seq: 3,
      kind: "tool",
      name: "analyze_site_photo",
      agent: null,
      seconds: 0.1,
      inputTokens: null,
      outputTokens: null,
      reasoningTokens: null,
      slow: false,
      ok: true,
    });
  });

  it("token 是脏数据时当 null,不当 0", () => {
    const t = parseTimingRecord(
      rawRecord({ input_tokens: "2157", output_tokens: -1, reasoning_tokens: Number.NaN }),
    )!;
    expect(t.inputTokens).toBeNull();
    expect(t.outputTokens).toBeNull();
    expect(t.reasoningTokens).toBeNull();
  });

  /**
   * 🔴 **0 和 null 不许混成一档。** 后端明说「命中缓存为 null」,而 0 是一个
   * 真实的观测值。混了之后界面上就分不出「没这个概念」和「量到了 0」——
   * 而这两件事对"为什么这么慢"的判断是相反的。
   */
  it("🔴 token 是 0 就当 0 —— 它是真实观测,不是「没有」", () => {
    const t = parseTimingRecord(rawRecord({ output_tokens: 0, reasoning_tokens: 0 }))!;
    expect(t.outputTokens).toBe(0);
    expect(t.reasoningTokens).toBe(0);
  });

  it("token 是小数就往零截断(token 不可能是半个)", () => {
    expect(parseTimingRecord(rawRecord({ input_tokens: 2157.8 }))!.inputTokens).toBe(2157);
  });

  it("多出来的键不影响解析(后端加字段不该让前端整块消失)", () => {
    const t = parseTimingRecord(rawRecord({ 未来新字段: "随便什么", cost_usd: 0.03 }));
    expect(t).not.toBeNull();
    expect(t!.name).toBe("kimi-k3");
  });
});

// ---------------------------------------------------------------------------
// ③ 信封
// ---------------------------------------------------------------------------

describe("parseTimingEnvelope 解信封", () => {
  it("正常一批:records 逐条解析,nextSince 收下", () => {
    const got = parseTimingEnvelope(
      okBody([rawRecord({ seq: 7 }), rawRecord({ seq: 8, kind: "tool", name: "save_hazard" })], 9),
    );
    expect(got.ok).toBe(true);
    expect(got.records.map((r) => r.seq)).toEqual([7, 8]);
    expect(got.nextSince).toBe(9);
  });

  it("空批也是成功(轮询大多数轮次都是空的,不是错)", () => {
    const got = parseTimingEnvelope(okBody([], 9));
    expect(got.ok).toBe(true);
    expect(got.records).toEqual([]);
    expect(got.nextSince).toBe(9);
  });

  /**
   * 🔴 **拿到一整页 HTML 登录页时,`nextSince` 必须是 null 不是 0。**
   *
   * 登录会话过期时 Caddy 会回一张 HTML 登录页,`JSON.parse` 当场抛 ——
   * 那一下必须在库自己的 try 里,不能抛到轮询循环去。
   *
   * 而失败时给 0 的话,调用方一旦"顺手"用了它,下一轮就把整段从头拉一遍,
   * 界面上表现为耗时行**成倍重复**,而两边都不报错。null 逼调用方显式决定
   * 「保持原游标不动」—— 这也正是 `advanceTimingCursor(null)` 什么都不做的原因。
   */
  it("🔴 不是 JSON(一整页 HTML 登录页)→ 失败,且 nextSince 是 null 不是 0", () => {
    const html = "<!DOCTYPE html><html><body><h1>請先登入</h1></body></html>";
    expect(() => parseTimingEnvelope(html)).not.toThrow();

    const got = parseTimingEnvelope(html);
    expect(got.ok).toBe(false);
    expect(got.records).toEqual([]);
    expect(got.nextSince, "给 0 的话调用方会把整段重拉一遍,耗时行成倍重复").toBeNull();
  });

  it("空串 / 半截 JSON 也一样,不抛", () => {
    for (const body of ["", "   ", "{", '{"ok": tru', "undefined"]) {
      expect(() => parseTimingEnvelope(body)).not.toThrow();
      expect(parseTimingEnvelope(body).ok, `${JSON.stringify(body)} 不该被当成成功`).toBe(false);
    }
  });

  const BAD_BODIES: ReadonlyArray<[string, unknown]> = [
    ["ok 是 false(后端自己说这次不成)", { ok: false, data: { records: [], next_since: 1 } }],
    ["ok 缺席", { data: { records: [], next_since: 1 } }],
    ["ok 是 truthy 但不是 true", { ok: 1, data: { records: [], next_since: 1 } }],
    ["顶层不是对象(数字)", 42],
    ["顶层不是对象(数组)", [{ records: [] }]],
    ["顶层是 null", null],
    ["data 缺席", { ok: true }],
    ["data 不是对象", { ok: true, data: "records" }],
    ["data 是数组", { ok: true, data: [] }],
    ["records 缺席", { ok: true, data: { next_since: 1 } }],
    ["records 不是数组(对象)", { ok: true, data: { records: {}, next_since: 1 } }],
    ["records 不是数组(字符串)", { ok: true, data: { records: "[]", next_since: 1 } }],
    ["next_since 缺席", { ok: true, data: { records: [] } }],
    ["next_since 是负数", { ok: true, data: { records: [], next_since: -1 } }],
    ["next_since 不是数", { ok: true, data: { records: [], next_since: "9" } }],
    ["next_since 是 null", { ok: true, data: { records: [], next_since: null } }],
    ["next_since 是 NaN(JSON 里是 null)", { ok: true, data: { records: [], next_since: Number.NaN } }],
  ];
  it.each(BAD_BODIES)("%s → 失败,且 records 为空、nextSince 为 null", (_why, body) => {
    const got = parseTimingEnvelope(JSON.stringify(body));
    expect(got.ok).toBe(false);
    // 失败时 records 恒为空数组(不是 null)—— 调用方因此不必判空
    expect(got.records).toEqual([]);
    expect(got.nextSince).toBeNull();
  });

  /**
   * 🔴 **单条解析不出来只丢那一条,整批不作废。**
   *
   * 后端哪天多推一种 kind(比如 `embedding`),整批作废等于界面上一行都没有;
   * 只丢一条至少别的还看得见 —— 而且"少了一行"比"全没了"更容易被发现是哪儿变了。
   */
  it("🔴 坏一条只丢那一条,好的照收", () => {
    const got = parseTimingEnvelope(
      okBody(
        [
          rawRecord({ seq: 7 }),
          rawRecord({ seq: 8, kind: "embedding" }), // 未来新增的 kind
          { 完全不是记录: true },
          "一个字符串",
          null,
          rawRecord({ seq: 9 }),
        ],
        10,
      ),
    );
    expect(got.ok).toBe(true);
    expect(got.records.map((r) => r.seq), "好的那三条被一起作废了").toEqual([7, 9]);
    expect(got.nextSince).toBe(10);
  });

  it("整批全是坏的也算成功(空批 + 游标照样前进,否则会卡在原地反复拉)", () => {
    const got = parseTimingEnvelope(okBody([{ 坏的: 1 }, 42], 11));
    expect(got.ok).toBe(true);
    expect(got.records).toEqual([]);
    expect(got.nextSince).toBe(11);
  });

  it("next_since 是小数就往零截断", () => {
    expect(parseTimingEnvelope(okBody([], 9.9)).nextSince).toBe(9);
  });

  it("next_since 是 0 是合法的(还没有任何记录时后端就回 0)", () => {
    const got = parseTimingEnvelope(okBody([], 0));
    expect(got.ok).toBe(true);
    expect(got.nextSince).toBe(0);
  });

  it("信封里多出来的键不影响解析(user_msg / error_code 之外还有别的也行)", () => {
    const body = JSON.stringify({
      ok: true,
      data: { records: [rawRecord()], next_since: 8, 服务器时间: "2026-08-21" },
      user_msg: "",
      error_code: null,
      trace_id: "abc",
    });
    expect(parseTimingEnvelope(body).records).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// ④ 游标
// ---------------------------------------------------------------------------

describe("游标语义", () => {
  it("初始是 0(第一轮把这个会话已有的全拉回来)", () => {
    expect(getTimingCursor()).toBe(0);
  });

  it("记下新游标就前进", () => {
    advanceTimingCursor(9);
    expect(getTimingCursor()).toBe(9);
  });

  /**
   * 🔴 **只许前进。** 接口回来的 `next_since` 理论上单调,但**网络会乱序** ——
   * 慢响应后到是常态。退回去就等于把已经收过的那段再拉一遍,而 store 那边按 seq
   * 去重、界面看不出重复,于是表现只是"每轮都在白拉一大段",没有任何报错。
   */
  it("🔴 只前进不后退(慢响应后到时不许把游标拽回去)", () => {
    advanceTimingCursor(20);
    advanceTimingCursor(12); // 上一轮的响应现在才到
    expect(getTimingCursor(), "游标被旧响应拽回去了 —— 那一段会被反复重拉").toBe(20);
    advanceTimingCursor(20); // 相等也不算前进
    expect(getTimingCursor()).toBe(20);
    advanceTimingCursor(21);
    expect(getTimingCursor()).toBe(21);
  });

  /**
   * `null` 是 `parseTimingEnvelope` 失败时给的值(它刻意不给 0)。
   * 这里什么都不做 = 保持原游标 —— 两处合起来才是完整的「解析失败就当这轮没发生」。
   */
  it("advanceTimingCursor(null) 什么都不做(保持原游标)", () => {
    advanceTimingCursor(15);
    advanceTimingCursor(null);
    expect(getTimingCursor()).toBe(15);
  });

  it("NaN / Infinity 也什么都不做", () => {
    advanceTimingCursor(15);
    advanceTimingCursor(Number.NaN);
    advanceTimingCursor(Number.POSITIVE_INFINITY);
    expect(getTimingCursor()).toBe(15);
  });

  it("小数游标往零截断", () => {
    advanceTimingCursor(9.9);
    expect(getTimingCursor()).toBe(9);
  });

  it("负数不许把游标拽到 0 以下", () => {
    advanceTimingCursor(5);
    advanceTimingCursor(-3);
    expect(getTimingCursor()).toBe(5);
  });

  /**
   * 🔴 **换会话必须归零。**
   *
   * `seq` 是后端**全进程**的一个计数器,不是每个会话各数各的。带着上一个会话的
   * 游标去拉新会话,拿回来的是「新会话里 seq 比它大的那部分」—— 也就是
   * **开头那几步凭空少掉**,而没有任何报错:界面上就是一段耗时行,少了前面几行
   * 没人看得出来,合计秒数还跟着少算。
   */
  it("🔴 换会话时游标归零(seq 是全进程一个计数器)", () => {
    beginTimingRun("thread-a");
    advanceTimingCursor(120);
    dropTimingsIfThreadChanged("thread-b");
    expect(getTimingCursor(), "带着旧游标去拉新会话 = 开头几步凭空少掉").toBe(0);
  });

  /**
   * 🔴 **换一轮不归零 —— 那正是「只显示本轮」的实现方式。**
   *
   * 行清掉、游标留着,下一轮拉回来的自然只有新的。归零的话,新一轮会把这个会话
   * 从头到尾的记录全拉回来,表头写着「本輪 37 步」而这一轮其实只跑了 5 步。
   */
  it("🔴 beginTimingRun 不动游标(行清掉、游标留着 = 只显示本轮)", () => {
    beginTimingRun("thread-a");
    advanceTimingCursor(120);
    beginTimingRun("thread-a");
    expect(getTimingCursor(), "新一轮把游标归零了 —— 会把整个会话的历史当成本轮").toBe(120);
  });

  it("还是同一个会话时不动游标(effect 刷晚了也幂等)", () => {
    beginTimingRun("thread-a");
    advanceTimingCursor(120);
    dropTimingsIfThreadChanged("thread-a");
    expect(getTimingCursor()).toBe(120);
  });
});

// ---------------------------------------------------------------------------
// ⑤ store
// ---------------------------------------------------------------------------

describe("ingestTimings 收一批记录", () => {
  it("返回实际收进去几条", () => {
    expect(ingestTimings([step(1), step(2)])).toBe(2);
    expect(getTimings().map((r) => r.seq)).toEqual([1, 2]);
  });

  it("空批返回 0", () => {
    expect(ingestTimings([])).toBe(0);
    expect(getTimings()).toHaveLength(0);
  });

  /**
   * 🔴 **按 seq 去重 —— 本组最值钱的一条。**
   *
   * 轮询天然会重叠:上一轮的响应还在路上,下一轮已经带着没更新的游标发出去了
   * (慢网络下必然发生)。不去重的表现是**同一步出现两次**,而它看起来非常像
   * 「模型真的被调了两次」—— 那会把人送去查一个不存在的 bug,
   * 顺带把「合計」那个数也算成两倍。
   */
  it("🔴 按 seq 去重:整批重复 → 收 0 条,行数不变", () => {
    ingestTimings([step(1), step(2)]);
    expect(ingestTimings([step(1), step(2)]), "重叠的那批被重复收进去了").toBe(0);
    expect(getTimings().map((r) => r.seq)).toEqual([1, 2]);
  });

  it("🔴 按 seq 去重:半重叠 → 只收新的那半", () => {
    ingestTimings([step(1), step(2)]);
    expect(ingestTimings([step(2), step(3), step(4)])).toBe(2);
    expect(getTimings().map((r) => r.seq)).toEqual([1, 2, 3, 4]);
  });

  it("去重认的是 seq 不是内容 —— 同一步重传时以先收到的为准", () => {
    ingestTimings([step(1, { name: "kimi-k3" })]);
    ingestTimings([step(1, { name: "改了名字" })]);
    expect(getTimings()[0].name).toBe("kimi-k3");
  });

  /**
   * 后端按 seq 递增推,但两批之间乱序到达是可能的(重试、慢响应后到)。
   * 排好序不是洁癖:界面上那一列是**执行顺序**,乱了之后"先识图再记台账"
   * 会看着像反过来的,而没有任何东西会说它错了。
   * 顺带一条:`GytTimingRows` 拿 seq 当 React key,顺序乱了行会被认错、状态串位。
   */
  it("乱序进来会按 seq 排好", () => {
    ingestTimings([step(9), step(3), step(7)]);
    expect(getTimings().map((r) => r.seq)).toEqual([3, 7, 9]);

    ingestTimings([step(5)]); // 迟到的中间那条也要插回正确位置
    expect(getTimings().map((r) => r.seq)).toEqual([3, 5, 7, 9]);
  });

  /**
   * 无上限的话,一个坏掉的循环能把内存吃到页面卡死,而**那之前不会有任何报错**。
   * 丢**最旧**的:一轮里最值得看的是最后那几步(慢在哪儿、错在哪儿都在末尾)。
   */
  it("超过 MAX_TIMING_ROWS 丢最旧的,留最后那几步", () => {
    const many = Array.from({ length: MAX_TIMING_ROWS + 5 }, (_, i) => step(i));
    ingestTimings(many);

    const got = getTimings();
    expect(got).toHaveLength(MAX_TIMING_ROWS);
    expect(got[0].seq).toBe(5);
    expect(got[got.length - 1].seq).toBe(MAX_TIMING_ROWS + 4);
  });

  it("分多批攒过上限也一样丢最旧的", () => {
    for (let i = 0; i < MAX_TIMING_ROWS + 5; i++) ingestTimings([step(i)]);
    const got = getTimings();
    expect(got).toHaveLength(MAX_TIMING_ROWS);
    expect(got[0].seq).toBe(5);
  });

  /**
   * 🔴 `useSyncExternalStore` 要求 getSnapshot 在「没变」时返回**同一个引用**,
   * 否则 React 判定每次渲染快照都变了 → 无限重渲染 → 页面卡死
   * (报的是 "The result of getSnapshot should be cached",而现象是页面卡死)。
   */
  it("🔴 没变时快照引用不变,变了才换新数组(不是原地 push)", () => {
    const before = getTimings();
    expect(getTimings()).toBe(before);

    ingestTimings([step(1)]);
    const after = getTimings();
    expect(after).not.toBe(before);
    expect(before, "旧快照被就地改掉了").toHaveLength(0);

    // 全是重复的那一批不改变任何东西 → 引用必须还是同一个
    ingestTimings([step(1)]);
    expect(getTimings()).toBe(after);
  });

  it("追加是不可变的:旧快照拿在手里也不会被改", () => {
    ingestTimings([step(1)]);
    const snap = getTimings();
    ingestTimings([step(2)]);
    expect(snap.map((r) => r.seq)).toEqual([1]);
    expect(getTimings().map((r) => r.seq)).toEqual([1, 2]);
  });

  it("订阅者能收到通知,退订之后不再收", () => {
    let hits = 0;
    const off = subscribeTimings(() => {
      hits += 1;
    });
    ingestTimings([step(1)]);
    expect(hits).toBe(1);
    off();
    ingestTimings([step(2)]);
    expect(hits).toBe(1);
  });

  /**
   * 🔴 **没有新东西就不许通知。**
   *
   * 轮询是一秒一轮,而绝大多数轮次是空的 / 全重复的。每轮都通知 = 每秒一次
   * 整块重渲染,在低端安卓上是看得见的卡顿,而"页面有点卡"这件事没人会
   * 联想到一个观测件 —— 它连报错都不会有。
   */
  it("🔴 空批 / 全重复时不通知订阅者(否则每轮轮询白重渲染)", () => {
    ingestTimings([step(1)]);

    let hits = 0;
    const off = subscribeTimings(() => {
      hits += 1;
    });
    ingestTimings([]);
    expect(hits, "空批也通知了").toBe(0);
    ingestTimings([step(1)]);
    expect(hits, "全重复也通知了").toBe(0);

    ingestTimings([step(2)]); // 真有新的才通知
    expect(hits).toBe(1);
    off();
  });

  it("多个订阅者都收得到(store 是给整棵树用的)", () => {
    let a = 0;
    let b = 0;
    const offA = subscribeTimings(() => {
      a += 1;
    });
    const offB = subscribeTimings(() => {
      b += 1;
    });
    ingestTimings([step(1)]);
    expect([a, b]).toEqual([1, 1]);
    offA();
    offB();
  });
});

describe("beginTimingRun / dropTimingsIfThreadChanged 清行", () => {
  it("新一轮开始时清空上一轮的行", () => {
    beginTimingRun("thread-a");
    ingestTimings([step(1), step(2)]);
    beginTimingRun("thread-a");
    expect(getTimings()).toHaveLength(0);
  });

  it("已经空了就不再通知(免得每次提问都白重渲染一次)", () => {
    let hits = 0;
    const off = subscribeTimings(() => {
      hits += 1;
    });
    beginTimingRun("thread-a");
    expect(hits).toBe(0);
    off();
  });

  it("切到别的会话就清掉(这正是要治的:行挂在别人对话底下)", () => {
    beginTimingRun("thread-a");
    ingestTimings([step(1)]);
    dropTimingsIfThreadChanged("thread-b");
    expect(getTimings()).toHaveLength(0);
  });

  it("点「新對話」(threadId 变 null / undefined)也清", () => {
    beginTimingRun("thread-a");
    ingestTimings([step(1)]);
    dropTimingsIfThreadChanged(null);
    expect(getTimings()).toHaveLength(0);

    beginTimingRun("thread-a");
    ingestTimings([step(1)]);
    dropTimingsIfThreadChanged();
    expect(getTimings()).toHaveLength(0);
  });

  it("还是同一个会话就不动(effect 刷晚了也幂等)", () => {
    beginTimingRun("thread-a");
    ingestTimings([step(1)]);
    dropTimingsIfThreadChanged("thread-a");
    dropTimingsIfThreadChanged("thread-a");
    expect(getTimings()).toHaveLength(1);
  });

  /**
   * 🔴 **这条就是那个竞态本身。**
   *
   * 新建会话时 `onThreadId` 是**在 run 开始之前**触发的(langgraph-sdk 的 submit:
   * 先建线程、再发 runs.stream),effect 排到什么时候刷不由我们定 —— 刷晚了就把
   * **正在流的那一轮**的行擦掉,表现是「跑着跑着耗时行突然全没了」,
   * 零报错、不可复现。所以判据落在「这批行归谁」上,与刷新时机无关:
   * 还没有归属人(owner 还是 null)时**一律不清**。
   *
   * ⚠️ 这条判据有一个已知推论:`dropTimingsIfThreadChanged` 清完之后把 owner
   *    置回 null,于是**连着换两次会话时第二次会被这条早退挡住**。已在报告里
   *    单独提出,这里只钉住当前行为 —— 改判据前先把那个推论一起想清楚。
   */
  it("🔴 还没有归属人时一律不清(新建会话那一刻的竞态)", () => {
    ingestTimings([step(1)]);
    dropTimingsIfThreadChanged("thread-new");
    expect(getTimings()).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// ⑥ coldFill —— 「本輪」与「最近」
// ---------------------------------------------------------------------------

/**
 * 记录存在后端,所以**刷新页面之后还在**(这一点比 custom 事件那版强)。
 * 但那时屏幕上那批行**不是这一轮跑出来的**,表头写「本輪 5 步」就是撒谎。
 *
 * 合成一个词的代价很具体:工友刷新一下,就看见「本輪」挂着上一轮的数,
 * 而这一轮其实一步都还没跑 —— 他会以为自己刚问的那句已经跑完了。
 */
describe("coldFill:刷新之后补出来的那批", () => {
  it("初始是 false", () => {
    expect(isColdFill()).toBe(false);
  });

  it("markColdFill 标上,并通知一次", () => {
    let hits = 0;
    const off = subscribeTimings(() => {
      hits += 1;
    });
    markColdFill();
    expect(isColdFill()).toBe(true);
    expect(hits).toBe(1);
    off();
  });

  /**
   * 幂等 —— 轮询一秒一次,每轮都通知一遍等于每秒白重渲染一次整块。
   * 和 `ingestTimings` 那条「没新东西不通知」是同一个理由。
   */
  it("🔴 markColdFill 幂等:第二次不再通知", () => {
    markColdFill();
    let hits = 0;
    const off = subscribeTimings(() => {
      hits += 1;
    });
    markColdFill();
    markColdFill();
    expect(isColdFill()).toBe(true);
    expect(hits, "已经标过了还在通知 —— 每轮轮询白重渲染一次").toBe(0);
    off();
  });

  it("🔴 beginTimingRun 把它清回 false(这一轮是真跑的,该写「本輪」)", () => {
    markColdFill();
    beginTimingRun("thread-a");
    expect(isColdFill()).toBe(false);
  });

  it("🔴 换会话也清(新会话那批还没判定是冷是热)", () => {
    beginTimingRun("thread-a");
    ingestTimings([step(1)]);
    markColdFill();
    dropTimingsIfThreadChanged("thread-b");
    expect(isColdFill()).toBe(false);
  });

  it("同一个会话的 drop 是早退,不碰 coldFill(刷新后翻旧会话要一直写「最近」)", () => {
    beginTimingRun("thread-a");
    markColdFill();
    dropTimingsIfThreadChanged("thread-a");
    expect(isColdFill()).toBe(true);
  });

  it("resetTimingStore 把三件状态一起归零(测试之间不许串味)", () => {
    beginTimingRun("thread-a");
    ingestTimings([step(1)]);
    advanceTimingCursor(50);
    markColdFill();

    resetTimingStore();

    expect(getTimings()).toHaveLength(0);
    expect(getTimingCursor()).toBe(0);
    expect(isColdFill()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 排版:null 的字段一个都不渲染
// ---------------------------------------------------------------------------

describe("排版:null 的字段一个都不渲染", () => {
  it("秒数一律一位小数", () => {
    expect(formatSeconds(23)).toBe("23.0s");
    expect(formatSeconds(0.14)).toBe("0.1s");
    expect(formatSeconds(1.25)).toBe("1.3s");
    // 0.0s 是有信息量的真实观测(命中缓存 / 根本没干活),不许变成 "<0.1s" 那种话术
    expect(formatSeconds(0)).toBe("0.0s");
  });

  it("三段齐全时长这样(与需求里的样例逐字一致)", () => {
    expect(
      formatTokens(timing({ inputTokens: 2157, outputTokens: 779, reasoningTokens: 596 })),
    ).toBe("輸入 2157 / 輸出 779(思考 596)");
  });

  it("没有思考 token 时不带括号", () => {
    expect(formatTokens(timing())).toBe("輸入 2723 / 輸出 29");
  });

  /**
   * 🔴 命中缓存那一步 input/output 都是 null。整段必须消失,
   *    退化成「輸入 0 / 輸出 0」等于谎报这次真调了模型。
   */
  it("🔴 三个都是 null → 整段返回 null(不许出现 null / 0)", () => {
    expect(
      formatTokens(timing({ inputTokens: null, outputTokens: null, reasoningTokens: null })),
    ).toBeNull();
  });

  it("只有一半时只渲染那一半", () => {
    expect(formatTokens(timing({ outputTokens: null, reasoningTokens: null }))).toBe("輸入 2723");
    expect(formatTokens(timing({ inputTokens: null, reasoningTokens: null }))).toBe("輸出 29");
  });

  it("只有思考 token 时不加括号(前面没东西可附)", () => {
    expect(
      formatTokens(timing({ inputTokens: null, outputTokens: null, reasoningTokens: 596 })),
    ).toBe("思考 596");
  });

  it("0 会照实渲染,不会被当成「没有」吞掉", () => {
    expect(formatTokens(timing({ inputTokens: 0, outputTokens: 0, reasoningTokens: null }))).toBe(
      "輸入 0 / 輸出 0",
    );
  });

  it("任何一段里都不许出现字面量 null / undefined / NaN", () => {
    const cases = [
      timing(),
      timing({ inputTokens: null }),
      timing({ outputTokens: null }),
      timing({ reasoningTokens: 596 }),
      timing({ inputTokens: 0, outputTokens: 0, reasoningTokens: 0 }),
    ];
    for (const t of cases) {
      expect(formatTokens(t) ?? "").not.toMatch(/null|undefined|NaN/);
    }
  });

  /**
   * 合计是**各步之和**,不是这一轮的墙上时间:子 Agent 之间有排队和空档,
   * 真并行还会重复计。界面上那个词因此是「合計」不是「耗時」。
   */
  it("合计是各步之和", () => {
    expect(
      totalSeconds([timing({ seconds: 23 }), timing({ seconds: 0.1 }), timing({ seconds: 1.2 })]),
    ).toBeCloseTo(24.3, 5);
    expect(totalSeconds([])).toBe(0);
  });

  it("合计吃的是 store 里那份只读数组(不许在里面改元素)", () => {
    ingestTimings([step(1, { seconds: 2 }), step(2, { seconds: 3 })]);
    expect(totalSeconds(getTimings())).toBeCloseTo(5, 5);
  });
});

// ---------------------------------------------------------------------------
// 图标与语气
// ---------------------------------------------------------------------------

describe("图标与语气", () => {
  it("工具是 📋", () => {
    expect(iconFor(timing({ kind: "tool", name: "analyze_site_photo", reasoningTokens: null }))).toBe(
      "📋",
    );
  });

  /**
   * 🧠 与 ⚡ 的分界是 `reasoningTokens > 0`,**不是模型名** ——
   * 契约里没有「这是视觉档还是文本档」这个字段,而思考 token 是实测出来的事实。
   * 2026-08-20 那次实测:视觉档 779 个输出 token 有 596 个是思考,占 76%,
   * 工友在工地举着手机等的就是这一段。
   */
  it("思考过的模型是 🧠,没思考的是 ⚡", () => {
    expect(iconFor(timing({ reasoningTokens: 596 }))).toBe("🧠");
    expect(iconFor(timing({ reasoningTokens: null }))).toBe("⚡");
  });

  it("思考 token 是 0 不算思考", () => {
    expect(iconFor(timing({ reasoningTokens: 0 }))).toBe("⚡");
  });

  it("失败压过一切 —— 出错时要先看见是哪一步炸的", () => {
    expect(iconFor(timing({ ok: false }))).toBe("⚠️");
    expect(iconFor(timing({ ok: false, kind: "tool" }))).toBe("⚠️");
    expect(iconFor(timing({ ok: false, reasoningTokens: 596 }))).toBe("⚠️");
  });

  it("语气三档,失败优先于慢", () => {
    expect(toneFor(timing())).toBe("normal");
    expect(toneFor(timing({ slow: true }))).toBe("slow");
    expect(toneFor(timing({ ok: false }))).toBe("fail");
    // 又慢又失败的那一行,要说的是它失败了
    expect(toneFor(timing({ ok: false, slow: true }))).toBe("fail");
  });
});

// ---------------------------------------------------------------------------
// 界面文案:恒繁體
// ---------------------------------------------------------------------------

describe("界面文案恒繁體", () => {
  /**
   * 耗时行挂在**聊天主界面**上、是常驻的,所以按 CLAUDE.md「语言的两条规则」
   * 第②条一律**静态繁體**:挂 useHantUI 等于让每个用户首屏拉 438 KB 字典,
   * 包括从不看繁體的简体工友。手打成简体的话这条先红。
   */
  it("固定文案逐字是繁體", () => {
    expect(TIMING_LABELS.slow).toBe("慢");
    expect(TIMING_LABELS.fail).toBe("失敗");
    expect(TIMING_LABELS.headPrefix).toBe("本輪");
    expect(TIMING_LABELS.headPrefixCold).toBe("最近");
    expect(TIMING_LABELS.headStep).toBe("步");
    expect(TIMING_LABELS.headTotal).toBe("合計");
    expect(TIMING_LABELS.ariaLabel).toBe("本輪各步耗時");
  });

  it("token 那三个词也是繁體", () => {
    const s = formatTokens(timing({ reasoningTokens: 596 }))!;
    expect(s).toContain("輸入");
    expect(s).toContain("輸出");
    expect(s).toContain("思考");
  });

  /**
   * 🔴 **两个表头前缀不许合并成一个词。**
   *
   * 「本輪」= 这一轮真跑出来的;「最近」= 刷新之后从后端补回来的。
   * 合了之后工友刷新一下就看见「本輪」挂着上一轮的数,而这一轮一步都没跑。
   */
  it("🔴 「本輪」与「最近」是两个词", () => {
    expect(TIMING_LABELS.headPrefixCold).not.toBe(TIMING_LABELS.headPrefix);
  });

  it("AGENT_LABELS 的值也全是繁體", () => {
    expect(AGENT_LABELS.supervisor).toBe("調度中樞"); // 與狀態行的藥丸同名(FINDING-005)
    expect(AGENT_LABELS.inspection).toBe("識隱患");
    expect(AGENT_LABELS.schedule).toBe("排期"); // 简繁同形,看着没改不是漏了
    expect(AGENT_LABELS.cad).toBe("圖紙");
    expect(AGENT_LABELS.knowledge).toBe("規範");
    expect(AGENT_LABELS.attendance).toBe("考勤");
    expect(AGENT_LABELS.supervision).toBe("監理");
  });

  /**
   * 🔴 **键是后端 Agent 名,一律留英文。**
   *
   * 哪天有人把整个文件过一遍简繁转换器,键会跟着变成中文 —— 于是
   * `AGENT_LABELS[t.agent]` 再也匹配不上、永远落到兜底分支(原样显示英文名),
   * 而**一行报错都不会有**。这条按 ASCII 钉住。
   */
  it("🔴 AGENT_LABELS 的键必须是纯 ASCII 标识符(转了就永远匹配不上)", () => {
    for (const key of Object.keys(AGENT_LABELS)) {
      expect(key, `「${key}」不是英文 Agent 名 —— 被简繁转换器扫过了?`).toMatch(/^[a-z][a-z_]*$/);
    }
  });
});

// ---------------------------------------------------------------------------
// agentLabelFor
// ---------------------------------------------------------------------------

describe("agentLabelFor 查显示名", () => {
  it("表里有的查得到中文名", () => {
    expect(agentLabelFor(timing({ agent: "inspection" }))).toBe("識隱患");
    expect(agentLabelFor(timing({ agent: "supervision" }))).toBe("監理");
  });

  /**
   * 英雄链上报的是 `inspection` 而不是 `safety`/`report`(后端取命名空间第一段),
   * 但三个都得能查 —— 那两步在工友眼里就是「識隱患」这一件事。
   */
  it("英雄链那三个名字都指向同一个中文名", () => {
    expect(agentLabelFor(timing({ agent: "safety" }))).toBe("識隱患");
    expect(agentLabelFor(timing({ agent: "report" }))).toBe("識隱患");
    expect(agentLabelFor(timing({ agent: "inspection" }))).toBe("識隱患");
  });

  /**
   * 🔴 **认不出的名字原样返回,不吞成「未知」。**
   *
   * 界面上冒出一个没见过的英文名(比如新加的 `procurement`),是
   * 「有人加了 Agent 而 AGENT_LABELS 没跟上」的**唯一信号** —— 吞成「未知」
   * 就再也没人发现,那一行从此永远写着「未知」而没人知道它本该是什么。
   */
  it("🔴 认不出的名字原样返回(那是「该补一行」的唯一信号)", () => {
    expect(agentLabelFor(timing({ agent: "procurement" }))).toBe("procurement");
    expect(agentLabelFor(timing({ agent: "某个没见过的" }))).toBe("某个没见过的");
  });

  /**
   * 没有 agent 时返回 null,渲染层据此**整段不渲染**。
   * 路径乙(直调 ainvoke)和图外调用压根没有"节点"这个概念,硬塞一个占位只是噪声。
   */
  it("没有 agent 时返回 null(那一段整个不渲染)", () => {
    expect(agentLabelFor(timing({ agent: null }))).toBeNull();
    // 空串走的是同一条兜底(解析层本来就不会放空串进来,这里是双保险)
    expect(agentLabelFor(timing({ agent: "" }))).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ⑦ 🔴 跨文件镜像:AGENT_LABELS ↔ GytStatusCards.tsx
// ---------------------------------------------------------------------------

/**
 * 读一份覆盖件的**源码文本**。
 *
 * 🔴 **不是 import 它。** `GytStatusCards.tsx` import 了 react 与
 * `@/providers/Stream`(上游那个组件),而本测试包是零依赖的、也没有
 * `@/providers` 这个别名 —— import 进来当场解析失败。所以只读文本、
 * 自己把那两张表挑出来,与 `image-compress.test.ts` 直接读后端 .py 源码是同一招。
 */
function readOverride(name: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../frontend-overrides/${name}`, import.meta.url)),
    "utf8",
  );
}

/**
 * 从 GytStatusCards.tsx 里挑出 `AGENT_TO_CARD`:后端 Agent 名 → 卡片号。
 *
 * ⚠️ 读不到就**当场抛**,不是返回空表。空表会让下面那两条 for 循环一圈都不跑、
 *    守卫全绿 —— 而"守卫空转"比"没有守卫"更坏:它还会让人以为这条链有人盯着。
 *    抛出来的话整个文件在收集阶段就红,消息直接指向"那边改写法了"。
 */
function readAgentToCard(src: string): Record<string, string> {
  const block = /const AGENT_TO_CARD[^=]*=\s*\{([\s\S]*?)\n\};/.exec(src);
  if (!block) throw new Error("GytStatusCards.tsx 里读不到 AGENT_TO_CARD —— 那边改写法了?");

  const out: Record<string, string> = {};
  for (const m of block[1].matchAll(/(["']?)([A-Za-z_]\w*)\1\s*:\s*"([^"]+)"/g)) {
    out[m[2]] = m[3];
  }
  return out;
}

/** 从 GytStatusCards.tsx 里挑出 `CARDS`:卡片号 → 卡上那个中文名。读不到同样当场抛。 */
function readCardNames(src: string): Record<string, string> {
  const start = src.indexOf("const CARDS");
  if (start < 0) throw new Error("GytStatusCards.tsx 里读不到 CARDS —— 那边改写法了?");

  const out: Record<string, string> = {};
  for (const m of src.slice(start).matchAll(/\{\s*key:\s*"([^"]+)"\s*,\s*name:\s*"([^"]+)"/g)) {
    out[m[1]] = m[2];
  }
  return out;
}

/**
 * 🔴 **本文件最值钱的一组。**
 *
 * `AGENT_LABELS`(这儿,按「哪个 Agent 在干活」逐个列)与 `GytStatusCards.tsx` 的
 * 四张能力卡(按「四张卡」分组,safety/inspection/report 合成一张)是**两个粒度**,
 * 收敛不掉 —— 合并会让其中一边失真。所以只能靠守卫盯着别漂。
 *
 * 只断字面量挡得住**这边**被改,挡不住**那边**被改 —— 而后者一样会让两处对不上号:
 * 卡片上写着「識隱患」而耗时行写着别的,同一件事在同一屏上有两个名字,工友以为是
 * 两个不同的东西在跑。所以这里**直接读那个组件的源码**比对,与
 * `image-compress.test.ts` 那条读后端 config.py 的守卫是同一个理由。
 */
describe("🔴 AGENT_LABELS 与 GytStatusCards.tsx 的跨文件镜像", () => {
  const src = readOverride("GytStatusCards.tsx");
  const agentToCard = readAgentToCard(src);
  const cardNames = readCardNames(src);

  it("先自检:两张表都真的读出来了(空转的守卫比没有守卫更坏)", () => {
    expect(Object.keys(agentToCard).length, "AGENT_TO_CARD 一条都没解析出来").toBeGreaterThan(0);
    expect(Object.keys(cardNames).length, "CARDS 一条都没解析出来").toBeGreaterThan(0);
    // 那个组件自己内部也得接得上:AGENT_TO_CARD 指到的卡片号必须真有那张卡
    for (const [agent, card] of Object.entries(agentToCard)) {
      expect(cardNames[card], `${agent} 指向的卡片号 ${card} 在 CARDS 里不存在`).toBeTruthy();
    }
  });

  it("🔴 那个组件认得的每个 Agent,AGENT_LABELS 里都有", () => {
    for (const agent of Object.keys(agentToCard)) {
      expect(
        AGENT_LABELS[agent],
        `卡片认得 ${agent},耗时行不认得 —— 那一行会原样显示英文名 ${agent}`,
      ).toBeTruthy();
    }
  });

  it("🔴 同一个 Agent,两边给出的中文名一致", () => {
    for (const [agent, card] of Object.entries(agentToCard)) {
      expect(
        AGENT_LABELS[agent],
        `${agent}:卡片上写「${cardNames[card]}」,耗时行写「${AGENT_LABELS[agent]}」—— ` +
          "同一件事在同一屏上有两个名字",
      ).toBe(cardNames[card]);
    }
  });

  /**
   * 反方向**故意不要求**:`AGENT_LABELS` 比卡片多(supervisor / attendance /
   * supervision 没有自己的能力卡)。这条钉住那几个多出来的键,防有人拿上面那两条
   * 当"两张表该一样"的依据、顺手把它们删掉 —— 删了的表现是监理、考勤那几步
   * 在耗时行里显示成英文名,而卡片那边一点变化都没有。
   */
  it("AGENT_LABELS 比卡片多几个是刻意的(别拿上面两条当依据删掉)", () => {
    for (const extra of ["supervisor", "attendance", "supervision"]) {
      expect(AGENT_LABELS[extra], `${extra} 没有能力卡,但耗时行要认得它`).toBeTruthy();
      expect(agentToCard[extra], `${extra} 什么时候有卡了?有的话这条注释要重写`).toBeUndefined();
    }
  });

  it("留档:当前两边各有几条(变了要知道为什么)", () => {
    expect(Object.keys(agentToCard).sort()).toEqual([
      "cad",
      "inspection",
      "knowledge",
      "report",
      "safety",
      "schedule",
    ]);
    expect(Object.keys(AGENT_LABELS).sort()).toEqual([
      "attendance",
      "cad",
      "inspection",
      "knowledge",
      "report",
      "safety",
      "schedule",
      "supervision",
      "supervisor",
    ]);
  });
});
