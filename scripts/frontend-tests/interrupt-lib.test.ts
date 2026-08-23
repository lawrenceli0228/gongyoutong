import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { isSyntheticBreakpointInterrupt } from "../frontend-overrides/interrupt-lib";

describe("isSyntheticBreakpointInterrupt", () => {
  it("识别 SDK 在空中断状态下合成的 breakpoint 哨兵", () => {
    expect(isSyntheticBreakpointInterrupt({ when: "breakpoint" })).toBe(true);
  });

  it("不吞掉真实人工审批或带业务内容的通用中断", () => {
    expect(
      isSyntheticBreakpointInterrupt({
        value: { action_requests: [{ name: "approve" }] },
      }),
    ).toBe(false);
    expect(
      isSyntheticBreakpointInterrupt({
        when: "breakpoint",
        value: { reason: "需要用户确认" },
      }),
    ).toBe(false);
    expect(isSyntheticBreakpointInterrupt({ when: "during" })).toBe(false);
    expect(isSyntheticBreakpointInterrupt(undefined)).toBe(false);
  });
});

describe("AI 消息的中断渲染接缝", () => {
  it("通用中断卡会过滤 breakpoint 哨兵", () => {
    const source = readFileSync(
      join(process.cwd(), "..", "frontend-overrides", "ai.tsx"),
      "utf8",
    );

    expect(source).toContain("!isSyntheticBreakpointInterrupt(interrupt)");
  });
});
