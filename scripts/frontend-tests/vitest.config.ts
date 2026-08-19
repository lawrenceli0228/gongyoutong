import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const require_ = createRequire(import.meta.url);

/**
 * 默认环境仍是 node —— 绝大多数被测物是纯函数库(checkin-lib / supervision-lib /
 * lang-lib),给它们套 jsdom 只会白花启动时间。
 *
 * 需要 DOM 的那一个文件(hant-convert.render.test.ts)在自己文件头写
 *   // @vitest-environment jsdom
 * 按文件覆盖。这样 jsdom 的代价只落在真用得上的地方。
 *
 * ⚠️ **别把默认环境改成 jsdom。** 那会让「库代码不许有 window 假设」这条
 *    前提失效:checkin-lib.ts 头注写着它必须零环境假设,而全局 jsdom 会让
 *    误用 window 的代码在测试里悄悄跑通,换到 SSR / Node 才炸。
 */
export default defineConfig({
  resolve: {
    alias: {
      // 覆盖件里写的是 "@/lib/xxx"(前端的路径别名)。测试包按相对路径直接
      // import 覆盖件源码,所以这里把 @/lib 指回 frontend-overrides/。
      // 只映 @/lib:其它 @/… (@/components 之类)本包一律不该碰 ——
      // 碰了就说明被测物已经不是纯逻辑了,该拆。
      "@/lib": fileURLToPath(new URL("../frontend-overrides", import.meta.url)),

      // 🔴 **react / react-dom 必须显式指到本包的那份。**
      //
      // 被测的覆盖件躺在 ../frontend-overrides/,那个目录**没有 node_modules** ——
      // Vite 从文件所在目录往上找依赖,找不到就报
      //     Failed to resolve import "react" from "…/hant-convert.tsx"
      //
      // 以前没碰到是因为在此之前所有被测覆盖件都是**零依赖**的
      // (checkin-lib / supervision-lib / lang-lib,头注都写着这条约束)。
      // hant-convert.tsx 是第一个 import react 的,所以第一次撞上。
      //
      // 顺带保证只有一份 react 实例:两份会让 hook 报
      // "Invalid hook call",而那个报错完全不指向真因。
      react: require_.resolve("react"),
      "react-dom": require_.resolve("react-dom"),

      // 同一个原因:hant-convert.tsx 里 `await import("opencc-js/cn2t")` 也解析不到。
      // 用 import.meta.resolve 拿 **ESM** 那个入口(dist/esm/cn2t.js);
      // require.resolve 会落到 UMD 那份,在 vitest 的 ESM 加载器里形状不对。
      "opencc-js/cn2t": fileURLToPath(import.meta.resolve("opencc-js/cn2t")),
    },
    dedupe: ["react", "react-dom"],
  },
  test: {
    // 纯函数库,不需要 DOM 环境;TextEncoder/TextDecoder 是 Node 全局自带的
    environment: "node",
    // .tsx 也要收:hant-convert.tsx 是 .tsx 后缀但里面**没有 JSX**
    // (它只是个 hook 文件),测试用 createElement 写,不必配 JSX 转换。
    include: ["*.test.ts", "*.test.tsx"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
