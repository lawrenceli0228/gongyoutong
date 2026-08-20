/**
 * 「每一步花了多久」的纯逻辑测试 —— 被测物 scripts/frontend-overrides/timing-lib.ts。
 *
 * 分四组,各守一件事:
 *   ① 契约解析 —— 字段名/类型对不上就必须返回 null(这是跨泳道契约的**唯一**测试);
 *   ② 排版     —— null 不许渲染成 "null"/"0";
 *   ③ 图标与语气 —— 慢/失败/思考各归各档,失败优先;
 *   ④ store    —— 不可变追加、引用稳定(React 的 useSyncExternalStore 靠这条)、
 *                 新一轮清空、上限截断。
 *
 * ⚠️ 界面文案(輸入/輸出/思考/慢/失敗)是**繁體**的,这里逐字断言 ——
 *    哪天有人手打成简体,`hant-ui-strings.test.ts` 会先红,但这条也跟着红,
 *    两道闸指向同一处,省得人以为只是守卫太严。
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  beginTimingRun,
  dropTimingsIfThreadChanged,
  formatSeconds,
  formatTokens,
  getTimings,
  iconFor,
  MAX_TIMING_ROWS,
  parseTimingEvent,
  recordTiming,
  subscribeTimings,
  TIMING_LABELS,
  toneFor,
  totalSeconds,
  type GytTiming,
} from "@/lib/timing-lib";

/**
 * 后端推的那个形状,**逐字镜像 `backend/src/gyt/core/timing.py` 的 `emit_timing()`**
 * (那个字典字面量是唯一真相;那边 `test_timing.py` 的 `_取事件` 守着「顶层只包一层
 * gyt_timing」)。别在这儿"顺手改简" —— 这里是两条泳道之间唯一的形状测试,
 * 改简了它就不再证明任何事,而字段名漂移的表现是**界面上一行都不出、控制台干净**。
 */
function rawEvent(over: Record<string, unknown> = {}) {
  return {
    gyt_timing: {
      kind: "llm",
      name: "kimi-k3",
      seconds: 23.0,
      input_tokens: 2157,
      output_tokens: 779,
      reasoning_tokens: 596,
      slow: true,
      ok: true,
      ...over,
    },
  };
}

/** 解析好的一步,给排版那几组用。 */
function timing(over: Partial<GytTiming> = {}): GytTiming {
  return {
    kind: "llm",
    name: "deepseek-v4-flash",
    seconds: 1.2,
    inputTokens: 2723,
    outputTokens: 29,
    reasoningTokens: null,
    slow: false,
    ok: true,
    ...over,
  };
}

describe("契约解析:形状不对就返回 null", () => {
  it("认得出后端推的那一条完整事件", () => {
    expect(parseTimingEvent(rawEvent())).toEqual({
      kind: "llm",
      name: "kimi-k3",
      seconds: 23.0,
      inputTokens: 2157,
      outputTokens: 779,
      reasoningTokens: 596,
      slow: true,
      ok: true,
    });
  });

  it("工具那一档没有 token,三个字段全是 null", () => {
    const t = parseTimingEvent(
      rawEvent({
        kind: "tool",
        name: "analyze_site_photo",
        seconds: 0.1,
        input_tokens: null,
        output_tokens: null,
        reasoning_tokens: null,
        slow: false,
      }),
    );
    expect(t).not.toBeNull();
    expect(t!.inputTokens).toBeNull();
    expect(t!.outputTokens).toBeNull();
    expect(t!.reasoningTokens).toBeNull();
  });

  // 🔴 这一组是**字段名契约**的唯一测试。后端哪天把 gyt_timing 改名 / 把
  //    reasoning_tokens 改成 thinking_tokens,表现不是报错,是界面上一行都不出。
  it("外壳键名不叫 gyt_timing 就不认", () => {
    expect(parseTimingEvent({ timing: { kind: "llm", name: "x", seconds: 1 } })).toBeNull();
    expect(parseTimingEvent({ gyt_timings: { kind: "llm", name: "x", seconds: 1 } })).toBeNull();
  });

  it("上游的 UI 消息不会被误收(它走的是同一个 onCustomEvent)", () => {
    expect(parseTimingEvent({ type: "ui", id: "a", name: "card", props: {} })).toBeNull();
  });

  it.each([
    ["不是对象", 42],
    ["是数组", [{ gyt_timing: {} }]],
    ["是 null", null],
    ["是 undefined", undefined],
    ["里层不是对象", { gyt_timing: "kimi-k3" }],
  ])("%s 就返回 null", (_why, raw) => {
    expect(parseTimingEvent(raw)).toBeNull();
  });

  it.each([
    ["kind 不在受控词表里", { kind: "vision" }],
    ["kind 缺失", { kind: undefined }],
    ["name 是空串", { name: "" }],
    ["name 只有空白", { name: "   " }],
    ["name 不是字符串", { name: 123 }],
    ["seconds 不是数字", { seconds: "23.0" }],
    ["seconds 是 NaN", { seconds: Number.NaN }],
    ["seconds 是 Infinity", { seconds: Number.POSITIVE_INFINITY }],
    ["seconds 是负数", { seconds: -1 }],
  ])("%s 就返回 null", (_why, over) => {
    expect(parseTimingEvent(rawEvent(over))).toBeNull();
  });

  it("name 两边的空白会被剪掉(不然界面上对不齐)", () => {
    expect(parseTimingEvent(rawEvent({ name: "  kimi-k3 " }))!.name).toBe("kimi-k3");
  });

  it("token 字段是脏数据时当 null,不当 0", () => {
    const t = parseTimingEvent(
      rawEvent({ input_tokens: "2157", output_tokens: -1, reasoning_tokens: Number.NaN }),
    );
    expect(t!.inputTokens).toBeNull();
    expect(t!.outputTokens).toBeNull();
    expect(t!.reasoningTokens).toBeNull();
  });

  // 后端承诺「八个键恒定存在」(timing.py 头注),但这边**两种都收**:
  // 多兜一种形状的成本是零,赌它永远不缺席的代价是整块界面消失。
  it("token 键整个缺席时也当 null(不赌后端永远带着它)", () => {
    const t = parseTimingEvent({
      gyt_timing: { kind: "tool", name: "analyze_site_photo", seconds: 0.1 },
    });
    expect(t).toEqual({
      kind: "tool",
      name: "analyze_site_photo",
      seconds: 0.1,
      inputTokens: null,
      outputTokens: null,
      reasoningTokens: null,
      slow: false,
      ok: true,
    });
  });

  it("token 是 0 就当 0 —— 它是真实观测,不是「没有」", () => {
    const t = parseTimingEvent(rawEvent({ output_tokens: 0, reasoning_tokens: 0 }));
    expect(t!.outputTokens).toBe(0);
    expect(t!.reasoningTokens).toBe(0);
  });

  // 缺省取向刻意不对称,理由见 parseTimingEvent 头注:误报失败的代价比漏报高。
  it("slow 缺省 false、ok 缺省 true(别对调)", () => {
    const t = parseTimingEvent(rawEvent({ slow: undefined, ok: undefined }))!;
    expect(t.slow).toBe(false);
    expect(t.ok).toBe(true);
  });

  it("slow / ok 只认真正的布尔值,不认「truthy」", () => {
    const t = parseTimingEvent(rawEvent({ slow: "yes", ok: 0 }))!;
    expect(t.slow).toBe(false);
    expect(t.ok).toBe(true);
  });
});

describe("排版:null 的字段一个都不渲染", () => {
  it("秒数一律一位小数", () => {
    expect(formatSeconds(23)).toBe("23.0s");
    expect(formatSeconds(0.14)).toBe("0.1s");
    expect(formatSeconds(1.25)).toBe("1.3s");
    expect(formatSeconds(0)).toBe("0.0s");
  });

  it("三段齐全时长这样(与需求里的样例逐字一致)", () => {
    expect(formatTokens(timing({ inputTokens: 2157, outputTokens: 779, reasoningTokens: 596 })))
      .toBe("輸入 2157 / 輸出 779(思考 596)");
  });

  it("没有思考 token 时不带括号", () => {
    expect(formatTokens(timing())).toBe("輸入 2723 / 輸出 29");
  });

  // 🔴 命中缓存那一步 input/output 都是 null。整段必须消失,
  //    退化成「輸入 0 / 輸出 0」等于谎报这次真调了模型。
  it("三个都是 null → 整段返回 null(不许出现 null / 0)", () => {
    const s = formatTokens(timing({ inputTokens: null, outputTokens: null, reasoningTokens: null }));
    expect(s).toBeNull();
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

  it("任何一段里都不许出现字面量 null / undefined", () => {
    const cases = [
      timing(),
      timing({ inputTokens: null }),
      timing({ outputTokens: null }),
      timing({ reasoningTokens: 596 }),
    ];
    for (const t of cases) {
      const s = formatTokens(t) ?? "";
      expect(s).not.toMatch(/null|undefined|NaN/);
    }
  });

  it("合计是各步之和", () => {
    expect(totalSeconds([timing({ seconds: 23 }), timing({ seconds: 0.1 }), timing({ seconds: 1.2 })]))
      .toBeCloseTo(24.3, 5);
    expect(totalSeconds([])).toBe(0);
  });
});

describe("图标与语气", () => {
  it("工具是 📋", () => {
    expect(iconFor(timing({ kind: "tool", name: "analyze_site_photo", reasoningTokens: null }))).toBe("📋");
  });

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
    expect(toneFor(timing({ ok: false, slow: true }))).toBe("fail");
  });

  // 界面恒繁體。手打成「失败」「输入」这类简体的话这条先红。
  it("界面文案是繁體", () => {
    expect(TIMING_LABELS.slow).toBe("慢");
    expect(TIMING_LABELS.fail).toBe("失敗");
    expect(TIMING_LABELS.headPrefix).toBe("本輪");
    expect(TIMING_LABELS.headTotal).toBe("合計");
    expect(formatTokens(timing({ reasoningTokens: 596 }))).toContain("輸入");
    expect(formatTokens(timing({ reasoningTokens: 596 }))).toContain("輸出");
    expect(formatTokens(timing({ reasoningTokens: 596 }))).toContain("思考");
  });
});

describe("run 级 store", () => {
  beforeEach(() => {
    // 归属也一起归零(传 null),否则上一条用例留下的 owner 会影响下一条。
    beginTimingRun(null);
  });

  it("认出来就存下,认不出来什么都不做", () => {
    expect(recordTiming(rawEvent())).not.toBeNull();
    expect(getTimings()).toHaveLength(1);

    expect(recordTiming({ type: "ui", id: "a" })).toBeNull();
    expect(getTimings()).toHaveLength(1);
  });

  // 🔴 useSyncExternalStore 要求 getSnapshot 在「没变」时返回**同一个引用**,
  //    否则 React 判定每次渲染快照都变了 → 无限重渲染 → 页面卡死。
  it("没变时快照引用不变,变了才换新数组(不是原地 push)", () => {
    const before = getTimings();
    expect(getTimings()).toBe(before);

    recordTiming(rawEvent());
    const after = getTimings();
    expect(after).not.toBe(before);
    expect(before).toHaveLength(0); // 旧快照没被就地改掉
  });

  it("追加是不可变的:旧快照拿在手里也不会被改", () => {
    recordTiming(rawEvent({ name: "a" }));
    const snap = getTimings();
    recordTiming(rawEvent({ name: "b" }));
    expect(snap.map((t) => t.name)).toEqual(["a"]);
    expect(getTimings().map((t) => t.name)).toEqual(["a", "b"]);
  });

  it("订阅者能收到通知,退订之后不再收", () => {
    let hits = 0;
    const off = subscribeTimings(() => {
      hits += 1;
    });
    recordTiming(rawEvent());
    expect(hits).toBe(1);
    recordTiming({ nope: true });
    expect(hits).toBe(1); // 不是耗时事件 → 不通知,免得白重渲染
    off();
    recordTiming(rawEvent());
    expect(hits).toBe(1);
  });

  it("新一轮开始时清空", () => {
    recordTiming(rawEvent());
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

  it("超过上限丢最旧的,留最后那几步(慢在哪儿、错在哪儿都在末尾)", () => {
    for (let i = 0; i < MAX_TIMING_ROWS + 5; i++) {
      recordTiming(rawEvent({ name: `step-${i}` }));
    }
    const rows = getTimings();
    expect(rows).toHaveLength(MAX_TIMING_ROWS);
    expect(rows[0].name).toBe("step-5");
    expect(rows[rows.length - 1].name).toBe(`step-${MAX_TIMING_ROWS + 4}`);
  });
});

/**
 * 🔴 换会话的判据 —— 这一组守的是一个**竞态**,不是一个功能。
 *
 * 「threadId 变了就清空」看着最自然,但新建会话时 `onThreadId` 是**在 run 开始
 * 之前**触发的(langgraph-sdk 的 submit:先建线程、再发 runs.stream),
 * effect 刷晚一点就会把正在流的那一轮擦掉 —— 零报错、不可复现。
 * 所以判据落在「这批行归谁」上,与刷新时机无关。推演在 timing-lib 的 `owner`。
 */
describe("换会话时的归属判据", () => {
  beforeEach(() => {
    beginTimingRun(null);
  });

  it("切到别的会话就清掉(这正是要治的:行挂在别人对话底下)", () => {
    beginTimingRun("thread-a");
    recordTiming(rawEvent());
    dropTimingsIfThreadChanged("thread-b");
    expect(getTimings()).toHaveLength(0);
  });

  it("点「新對話」(threadId 变 null)也清", () => {
    beginTimingRun("thread-a");
    recordTiming(rawEvent());
    dropTimingsIfThreadChanged(null);
    expect(getTimings()).toHaveLength(0);
  });

  it("还是同一个会话就不动(effect 刷晚了也幂等)", () => {
    beginTimingRun("thread-a");
    recordTiming(rawEvent());
    dropTimingsIfThreadChanged("thread-a");
    dropTimingsIfThreadChanged("thread-a");
    expect(getTimings()).toHaveLength(1);
  });

  // 🔴 这条就是那个竞态本身:新建会话时 onThreadId 先于 onCreated,
  //    那一刻还没有归属人 —— 此时**绝不许清**,否则擦的是正在流的这一轮。
  it("还没有归属人时一律不清(新建会话那一刻)", () => {
    recordTiming(rawEvent());
    dropTimingsIfThreadChanged("thread-new");
    expect(getTimings()).toHaveLength(1);
  });
});
