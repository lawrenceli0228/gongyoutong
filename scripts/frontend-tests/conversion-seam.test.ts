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

/**
 * 剥掉注释,只留代码。
 *
 * **负向断言必须走这个** —— 本仓的注释密度很高,而且经常**引用被禁的写法**
 * 来解释为什么禁(比如 hant-convert.tsx 的头注里就写着 `setConverter(conv)`
 * 那个崩过的写法)。直接扫全文的话,解释文字自己会把断言弄红,
 * 而人会以为代码坏了 —— 2026-08-18 已经这么误报过一次。
 *
 * 只剥「行首(允许缩进)的 // 注释」和块注释:不动 `https://` 这种行内双斜杠。
 */
function codeOf(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^[ \t]*\/\/.*$/gm, "");
}

const markdownText = read("markdown-text.tsx");
const toolCalls = read("tool-calls.tsx");
const ai = read("ai.tsx");
const hantConvert = read("hant-convert.tsx");

const markdownTextCode = codeOf(markdownText);
const toolCallsCode = codeOf(toolCalls);
const aiCode = codeOf(ai);
const hantConvertCode = codeOf(hantConvert);

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
    expect(markdownTextCode).not.toContain("hant-convert");
    expect(markdownTextCode).not.toContain("useHantText");
  });

  it("🔴 也不 import opencc —— 转换器只许出现在 hant-convert.tsx 一处", () => {
    expect(markdownTextCode).not.toContain("opencc");
    expect(toolCallsCode).not.toContain("opencc");
    expect(aiCode).not.toContain("opencc");
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
    expect(aiCode).not.toMatch(/useHantText\([^)]*"human"/s);
  });

  /**
   * 🔴 **接上了转换 ≠ 所有上屏路径都用它。** 2026-08-18 对抗复审抓到两处漏网:
   *
   *   :308  折叠里 `<div …>{contentString}</div>`   ← 未转换的原文
   *   :356  `<CommandBar content={contentString}>`   ← 复制按钮拿的也是原文
   *
   * 上面那三条断言全绿的时候这两处就是坏的 —— 它们只钉「`displayString` 是
   * `useHantText` 的返回值」,钉不住「屏幕上的每一处都用 `displayString`」。
   *
   * 坏法:同一个 Agent 的同一句话,**折叠起来是简体、带表格时是繁體**;
   * 屏幕上是繁體而点复制粘出来是简体。两者都没有报错。
   *
   * 判据只能是「`contentString` 不许出现在渲染位置」——
   * 它作为中间变量参与计算(`stripLedgerEcho(contentString)`、
   * `contentString.length` 之类)是正常的,所以不能一刀切禁掉这个标识符。
   */
  it("🔴 contentString 不许直接上屏 —— 上屏的每一处都得是 displayString", () => {
    // JSX 表达式插值:{contentString}
    expect(
      aiCode,
      "折叠区把未转换的 contentString 直接渲染了 —— 同一句话折叠时简体、带表格时繁體",
    ).not.toMatch(/\{\s*contentString\s*\}/);

    // 组件属性:content={contentString} / children={contentString} 等
    expect(
      aiCode,
      "有组件属性拿了未转换的 contentString —— 最典型的是复制按钮:屏幕繁體、粘贴简体",
    ).not.toMatch(/\b(content|children|text|value)=\{\s*contentString\s*\}/);
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
    expect(hantConvertCode).not.toContain("opencc-js/core");
    expect(hantConvertCode).not.toContain("STCharacters");
  });

  it("🔴 转换器不许进 useState —— 2026-08-18 真机崩过一次", () => {
    // 病因:setConverter(conv) 里 conv 是函数,React 把它当 updater
    // (setState(prev => next))去调 conv(null) → opencc 里 null.length → 整页崩成
    // "Application error: a client-side exception has occurred"。
    // 结构性修法是「模块级存转换器 + state 只存计数器」,所以 state 里永远不是函数。
    expect(hantConvertCode).not.toContain("setConverter");
    expect(hantConvertCode).not.toMatch(/useState<\s*Converter/);
    // 正面钉住现在的形状:state 只是个数字计数器
    expect(hantConvert).toMatch(/useState\(0\)/);
  });

  it("拉字典失败要退化成恒等函数,不许抛", () => {
    // 网差的工地上「看到简体」远好过「整段答话消失」。
    expect(hantConvert).toMatch(/catch\s*\(/);
    expect(hantConvert).toContain("console.warn");
  });
});
