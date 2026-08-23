/** LangGraph SDK 中断值的纯判定器。零依赖，供消息渲染与独立测试共用。 */

/**
 * SDK 会在没有真实 interrupt 数据时合成 `{ when: "breakpoint" }`，用于表达
 * 图停在断点/仍有后续节点。它是运行状态哨兵，不是需要用户处理的 Human Interrupt。
 *
 * 只认字段完全相同的哨兵，避免吞掉带 value、id 等业务信息的真实中断。
 */
export function isSyntheticBreakpointInterrupt(value: unknown): boolean {
  if (!value || Array.isArray(value) || typeof value !== "object") return false;

  const record = value as Record<string, unknown>;
  return Object.keys(record).length === 1 && record.when === "breakpoint";
}
