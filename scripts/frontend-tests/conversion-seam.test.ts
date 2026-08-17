/**
 * 转换接缝的**源码级**守卫 —— 三条不变量,一条都不该靠人记住。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *
 * ===========================================================================
 * 为什么是读源码而不是渲染
 * ---------------------------------------------------------------------------
 * 这三条不变量都是「某个文件里有没有某件事」,而不是「渲染出来长什么样」:
 *
 *   ① 两张徽章表必须由 withHantKeys 派生 —— 不许有人回头手写回简体一套;
 *   ② markdown-text.tsx **不许** import hant-convert —— 它有四个消费者,
 *      其中两个是上游的英文调试界面(agent-inbox 的 state-view /
 *      inbox-item-input),转它们是错的;
 *   ③ ai.tsx 必须真的调 useHantText —— 否则整条繁體链是死的。
 *
 * 渲染测试(jsdom + @testing-library + react-markdown + 三个路径别名 stub)
 * 能覆盖 ① 和 ③,但覆盖不了 ② —— 「这个文件不许 import 那个」只有源码级看得见。
 * 而 ① 的渲染部分已经由 badge-keys.test.ts 在纯函数层测了
 * (withHantKeys 的输出含简繁两套键)。
 *
 * ⚠️ **所以这不是「懒得上 jsdom」。** 上 jsdom 要给这个包加
 *    react / react-dom / react-markdown / @testing-library/react + jsdom,
 *    而本包的立身之本是零依赖(package.json 的 description 写着理由:
 *    frontend/ 不进 git,测试不能指望它存在)。这个取舍记在
 *    docs/W12_三语切换_方案.md §6.2,要上 jsdom 也得先改那条。
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const OVERRIDES = join(import.meta.dirname, "..", "frontend-overrides");
const read = (name: string) => readFileSync(join(OVERRIDES, name), "utf8");

const markdownText = read("markdown-text.tsx");
const toolCalls = read("tool-calls.tsx");
const ai = read("ai.tsx");
const hantConvert = read("hant-convert.tsx");

describe("① 徽章表必须由 withHantKeys 派生", () => {
  it("markdown-text.tsx 从 lang-lib 取词表并调 withHantKeys", () => {
    expect(markdownText).toContain("from \"@/lib/lang-lib\"");
    expect(markdownText).toContain("TASK_STATUS_WORDS");
    expect(markdownText).toContain("TASK_STATUS_WORDS_HANT");
    expect(markdownText).toMatch(/STATUS_CHIPS[^=]*=\s*withHantKeys\(/);
  });

  it("tool-calls.tsx 从 lang-lib 取词表并调 withHantKeys", () => {
    expect(toolCalls).toContain("from \"@/lib/lang-lib\"");
    expect(toolCalls).toContain("SEVERITY_WORDS");
    expect(toolCalls).toContain("SEVERITY_WORDS_HANT");
    expect(toolCalls).toMatch(/SEVERITY_CHIP[^=]*=\s*withHantKeys\(/);
  });
});

describe("② markdown-text.tsx 不许自己转换", () => {
  it("🔴 不 import hant-convert —— 它的四个消费者里有两个是上游英文界面", () => {
    // frontend/src/components/thread/agent-inbox/components/state-view.tsx
    // frontend/src/components/thread/agent-inbox/components/inbox-item-input.tsx
    // 那两个显示的是原始 state,转成繁體是错的。
    expect(markdownText).not.toContain("hant-convert");
    expect(markdownText).not.toContain("useHantText");
  });

  it("🔴 也不 import opencc —— 转换器只许出现在 hant-convert.tsx 一处", () => {
    expect(markdownText).not.toContain("opencc");
    expect(toolCalls).not.toContain("opencc");
    expect(ai).not.toContain("opencc");
  });
});

describe("③ ai.tsx 必须真的接上转换", () => {
  it("import 了 useHantText 与 resolveLang", () => {
    expect(ai).toContain("useHantText");
    expect(ai).toContain("resolveLang");
  });

  it("displayString 是 useHantText 的返回值 —— 不是直接用裁完的原文", () => {
    // 挪位 / 改回原样的表现:繁體链整条静默失效,界面照常显示简体。
    expect(ai).toMatch(/const\s+displayString\s*=\s*useHantText\(/);
  });

  it("传给 useHantText 的角色是 \"ai\" —— 用户发言绝不能走这条路", () => {
    expect(ai).toMatch(/useHantText\([^)]*"ai"/s);
    expect(ai).not.toMatch(/useHantText\([^)]*"human"/s);
  });
});

describe("懒加载不许被改成静态 import", () => {
  it("hant-convert.tsx 里 opencc 只能是动态 import()", () => {
    // 静态 import 会把 438 KB gzipped 的字典拉进首屏包 —— 而这件事
    // 只有真跑 next build 才看得出来,本地一切正常。
    expect(hantConvert).toMatch(/await import\(\s*"opencc-js\/cn2t"\s*\)/);
    // 顶部不许有静态的 opencc import
    const header = hantConvert.slice(0, hantConvert.indexOf("export"));
    expect(header).not.toMatch(/^import .*opencc/m);
  });

  it("走的是 cn2t 全量入口,不是 core + 单字表那个精简档", () => {
    // 精简档实测把「签发」转成「籤發」、「复查」转成「復查」,
    // 而那两个是监理链的核心动作(方案 §5.1)。
    expect(hantConvert).toContain("opencc-js/cn2t");
    expect(hantConvert).not.toContain("opencc-js/core");
    expect(hantConvert).not.toContain("STCharacters");
  });

  it("拉字典失败要退化成恒等函数,不许抛", () => {
    // 网差的工地上「看到简体」远好过「整段答话消失」。
    expect(hantConvert).toMatch(/catch\s*\(/);
    expect(hantConvert).toContain("console.warn");
  });
});
