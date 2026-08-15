import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // 纯函数库,不需要 DOM 环境;TextEncoder/TextDecoder 是 Node 全局自带的
    environment: "node",
    include: ["*.test.ts"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
