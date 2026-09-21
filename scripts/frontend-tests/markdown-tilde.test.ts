/**
 * 「单波浪号不许当删除线」—— 读 `markdown-text.tsx` 的源码钉住那一处插件配置。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *
 * 为什么这条测试非有不可(2026-09-21 线上体验测试抓到的真事):
 *
 * GFM 默认**单个** `~` 就构成删除线。而中文图纸/规范里 `~` 表范围是家常便饭
 * ——「①~㉑軸」「第5~8層」「3.300~6.600」。一句话里出现两个 `~`,中间那段
 * 就被整体划掉,**而且两个波浪号本身消失**。实测那次 cad 答立面标高:
 *
 *   原文   左側立面(㉑~①)標高:20.300… 右側立面(①~㉑)標高:10.000…
 *   渲染   左側立面(㉑<del>①)標高:20.300… 右側立面(①</del>㉑)標高:10.000…
 *
 * 两行标高全被划了删除线,工友看到的是「这答案是不是作废了」;
 * 而「①~㉑軸」当场读成「①㉑軸」,是**读数错误**,不只是难看。
 *
 * 🔴 为什么是读源码而不是真渲染一遍:本包**刻意不依赖 `frontend/`**
 *    (它不进 git,由 setup-frontend.sh 生成 —— 见 package.json 的 description),
 *    而 react-markdown / remark-gfm 只在那边。为这一条把整套渲染链拖进来不划算,
 *    所以钉的是「那一处配置长什么样」。判据够窄:改回裸 `remarkGfm` 就红。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const 源码 = readFileSync(
  fileURLToPath(new URL("../frontend-overrides/markdown-text.tsx", import.meta.url)),
  "utf8",
);

/** 去掉注释,免得注释里提到的写法把断言喂成假绿(本文件头上就写着裸 remarkGfm)。 */
const 正文 = 源码
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .split("\n")
  .filter((行) => !行.trim().startsWith("//"))
  .join("\n");

describe("markdown 渲染:单波浪号不许当删除线", () => {
  it("remarkGfm 必须带 singleTilde: false", () => {
    expect(正文).toMatch(/\[\s*remarkGfm\s*,\s*\{\s*singleTilde:\s*false\s*\}\s*\]/);
  });

  it("源码里每一处 remarkGfm 都必须带着那个选项,不许有裸的", () => {
    // 数出现次数而不是匹配「裸写法」的形状:插件既可以写成 `[remarkGfm, opts]`,
    // 也可能哪天被挪进一个数组常量里。只要「remarkGfm 的总数」== 「带 singleTilde:false
    // 的那种写法的数量」,就不可能存在一处漏配的。
    const 用到的地方 = 正文.replace(/^\s*import .*$/gm, ""); // 摘掉 import 那一行
    const 总数 = (用到的地方.match(/remarkGfm/g) ?? []).length;
    const 带选项的 = (
      用到的地方.match(/remarkGfm\s*,\s*\{\s*singleTilde:\s*false\s*\}/g) ?? []
    ).length;

    expect(总数, "源码里没有 remarkGfm 了?那这条测试要跟着改").toBeGreaterThan(0);
    expect(带选项的, `${总数} 处 remarkGfm,只有 ${带选项的} 处带了 singleTilde:false`).toBe(总数);
  });
});
