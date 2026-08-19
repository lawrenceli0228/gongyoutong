// @vitest-environment jsdom
/**
 * useHantText 的**运行时**测试 —— 唯一会真正执行那个 hook 的地方。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *
 * ===========================================================================
 * 这个文件为什么存在(2026-08-18,一次真机崩页之后补的)
 * ---------------------------------------------------------------------------
 * W12 第一批落地时,`hant-convert.tsx` 里写的是
 *
 *     setConverter(conv)       // conv 是个函数
 *
 * React 把「传给 setState 的函数」当成 **updater**(setState(prev => next)),
 * 于是去调 conv(上一个 state) = conv(null) → opencc 里 null.length → 抛,
 * 整页变成 "Application error: a client-side exception has occurred"。
 *
 * 🔴 **当时 334 条测试全绿。** 因为:
 *      lang-lib.test.ts        不碰 hook
 *      conversion-seam.test.ts 读源码,**不执行**
 *      → 没有任何测试执行过这个 hook
 *
 * 那次之前的判断是「不上 jsdom,源码级守卫够了」(方案 §6.2.1 原版)。
 * 那个判断的前半段成立(有两条不变量只有源码级看得见),漏的是后半段:
 * **渲染测试能查到的东西里,有一个真 bug 在等着。**
 *
 * 所以这个文件的验收标准很具体:**把 setConverter(conv) 那个写法改回去,
 * 本文件必须红。** conversion-seam.test.ts 那条源码断言挡的是同一个坑的
 * 「同形回归」,本文件挡的是「hook 到底跑不跑得起来」—— 两件事,都要。
 *
 * ⚠️ **变异验证时发现的一件事,别搞错哪条在守门**(2026-08-18 实跑):
 *    把 bug 写回去,红的是「字典拉到之后,答话真的变成繁體」那条,
 *    **不是**下面那条「渲染不抛」。因为那个 TypeError 发生在
 *    **异步的 setState 里**,不在首次同步渲染 —— `expect(...).not.toThrow()`
 *    压根碰不到它。
 *    换句话说:**真正的回归守卫是「等到转换结果」那条**,
 *    「渲染不抛」只挡「首帧就炸」这一类。两条留着,但别把功劳记错人 ——
 *    记错了下次有人删掉「等结果」那条,会以为回归还有人守。
 *
 * ===========================================================================
 * 依赖面比预估的小
 * ---------------------------------------------------------------------------
 * 评审时估的是「react + react-dom + react-markdown + testing-library + jsdom
 * + 三个路径别名 stub」。实际只要 **react / react-dom / testing-library / jsdom**:
 *   · hant-convert.tsx 只 import react 和 @/lib/lang-lib(一个 alias,配在
 *     vitest.config.ts 里),**不碰 markdown-text.tsx 那棵重依赖树**;
 *   · 它虽然是 .tsx 后缀但**没有 JSX**,所以连 JSX 转换都不用配 ——
 *     本文件用 createElement 写。
 * jsdom 只对本文件生效(文件头的 @vitest-environment),别的测试还是 node。
 */

import { createElement, type ReactNode } from "react";

import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useHantText } from "../frontend-overrides/hant-convert";
import type { Lang, MessageRole } from "../frontend-overrides/lang-lib";

/** 真实答话样本:schedule 那条链的典型回执,含要害词「台账」。 */
const HANS_ANSWER = "记上了:T1 补齐安全带,期限是下周一之前。我先查一下台账。";
const HANT_ANSWER = "記上了:T1 補齊安全帶,期限是下週一之前。我先查一下台賬。";

function render(text: string, role: MessageRole, lang: Lang) {
  return renderHook(() => useHantText(text, role, lang), {
    // 不需要任何 provider —— 这个 hook 只用 react 内建的三件。
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement("div", null, children),
  });
}

describe("useHantText 运行时", () => {
  it("首帧渲染不抛(只挡「首帧就炸」这一类,不挡 2026-08-18 那个)", () => {
    // ⚠️ 这条**不是**那次崩页的回归守卫,见文件头注:那个 TypeError 在
    //    异步 setState 里抛,not.toThrow() 碰不到。守门的是下面那条。
    expect(() => render(HANS_ANSWER, "ai", "zh-Hant")).not.toThrow();
  });

  it("🔴 字典拉到之后答话真的变成繁體 —— 这条才是那次崩页的回归守卫", async () => {
    // 变异验证过(2026-08-18):把 setConverter(conv) 那个写法改回去,
    // 红的就是这一条,报的就是生产上那个
    //     TypeError: Cannot read properties of null (reading 'length')
    // 因为它 await 到「转换后的值」为止,而那条路径正好穿过出事的 setState。
    const { result } = render(HANS_ANSWER, "ai", "zh-Hant");
    // 首帧字典还没到,先原样显示简体 —— 这是刻意的(总比整段消失好)
    await waitFor(() => {
      expect(result.current).toBe(HANT_ANSWER);
    });
  });

  it("要害词转对:簽發 / 複查 —— 精简档会把它们转错", async () => {
    const { result } = render("签发暂停令之后要复查", "ai", "zh-Hant");
    await waitFor(() => {
      expect(result.current).toBe("簽發暫停令之後要複查");
    });
  });

  it("🔴 用户发言恒不转 —— human.tsx 的照片编号正则是简体的", async () => {
    const withPhotoId = "看看这张照片。(照片编号:27ba7e5933f54547b77a30772959b3fb)";
    const { result } = render(withPhotoId, "human", "zh-Hant");
    expect(result.current).toBe(withPhotoId);
    // 等一会儿也不许变 —— 不是「还没转」,是「永远不转」
    await new Promise((r) => setTimeout(r, 50));
    expect(result.current).toBe(withPhotoId);
    expect(result.current).toContain("照片编号");
  });

  it("目标语是简体时**原样返回同一个引用**(零开销契约)", () => {
    const { result } = render(HANS_ANSWER, "ai", "zh-Hans");
    expect(result.current).toBe(HANS_ANSWER);
  });

  it("目标语是英文时也不转 —— 英文走提示词那条路,不是字形转换", () => {
    const { result } = render(HANS_ANSWER, "ai", "en");
    expect(result.current).toBe(HANS_ANSWER);
  });

  it("空串 / 纯符号 / 纯编号 不炸", async () => {
    for (const s of ["", "   ", "!!!", "GYT-20260817-123456", "T1 T2"]) {
      const { result } = render(s, "ai", "zh-Hant");
      await waitFor(() => {
        expect(typeof result.current).toBe("string");
      });
    }
  });

  it("同一段文本渲染两次结果一致(幂等,不会转两遍)", async () => {
    const a = render(HANS_ANSWER, "ai", "zh-Hant");
    await waitFor(() => expect(a.result.current).toBe(HANT_ANSWER));
    const b = render(a.result.current, "ai", "zh-Hant");
    await waitFor(() => expect(b.result.current).toBe(HANT_ANSWER));
  });

  it("切换语种时结果跟着切 —— rerender 不留旧值", async () => {
    const { result, rerender } = renderHook(
      ({ lang }: { lang: Lang }) => useHantText(HANS_ANSWER, "ai", lang),
      { initialProps: { lang: "zh-Hant" as Lang } },
    );
    await waitFor(() => expect(result.current).toBe(HANT_ANSWER));
    rerender({ lang: "zh-Hans" });
    expect(result.current).toBe(HANS_ANSWER);
  });
});
