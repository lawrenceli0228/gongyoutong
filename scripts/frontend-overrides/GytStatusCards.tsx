/**
 * 工友通首页 · 4 张能力状态卡片(W7 前端重设计 · 方案 B「清爽卡片」· 第 1 块)。
 *
 * 新增组件(上游 agent-chat-ui 没有),由 scripts/setup-frontend.sh 直接拷进
 * frontend/src/components/thread/GytStatusCards.tsx,并在 thread-index.tsx 里挂一次
 * <GytStatusCards />(放在消息区之上,首页/聊天都常驻可见 —— 这样"派活时卡片亮"在跑的过程中看得到)。
 *
 * 卡片是**状态灯,不是按钮**(用户定的):当调度中枢把当前这句话派给某个子 Agent 时,对应卡片亮起
 * (绿底 + 呼吸点 + 进度条 + "正在做什么"文案),其余变暗;没有活跃 Agent 时全部待命。
 *
 * "当前活跃 Agent"从 useStreamContext() 的流里推断:isLoading 时,从消息尾部往前找最近的
 * 子 Agent 署名(message.name)或 transfer_to_<name> 交接;推不出就当全待命(不报错、不乱亮)。
 * 后端子 Agent 名 → 卡片的映射见 AGENT_TO_CARD(inspection/report 归到「识隐患」这条链)。
 *
 * ⚠️ 未在无 Node 环境编译过:首次 apply 后在 WSL2/Node 里 pnpm dev 肉眼过一遍,修 TS/样式。
 */

import { useStreamContext } from "@/providers/Stream";
import { cn } from "@/lib/utils";

const AGENT_TO_CARD: Record<string, string> = {
  safety: "safety",
  inspection: "safety",
  report: "safety",
  schedule: "schedule",
  cad: "cad",
  knowledge: "knowledge",
};

const CARDS: { key: string; name: string; emoji: string; idle: string; busy: string }[] = [
  { key: "safety", name: "识隐患", emoji: "📷", idle: "拍照识别现场隐患", busy: "正在看这张照片有没有隐患…" },
  { key: "schedule", name: "排期", emoji: "📋", idle: "记任务 / 改期限 / 查进度", busy: "正在记任务…" },
  { key: "cad", name: "图纸", emoji: "📐", idle: "读 DXF 尺寸·标高·构件", busy: "正在读图纸参数…" },
  { key: "knowledge", name: "规范", emoji: "📖", idle: "查消防 / 防火 / 安全条文", busy: "正在查规范…" },
];

/** 从流里推断"当前活跃的子 Agent";not loading → null(全待命)。推不出也返回 null,不乱亮。 */
function useActiveAgent(): string | null {
  const stream = useStreamContext();
  if (!stream.isLoading) return null;
  const messages: any[] = (stream.messages as any[]) ?? [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i] ?? {};
    const name: string | undefined = m.name;
    if (name) {
      if (AGENT_TO_CARD[name]) return AGENT_TO_CARD[name];
      const t = /^transfer_to_(\w+)/.exec(name)?.[1];
      if (t && AGENT_TO_CARD[t]) return AGENT_TO_CARD[t];
    }
    const tcs: any[] = m.tool_calls ?? m.additional_kwargs?.tool_calls ?? [];
    for (const tc of tcs) {
      const tn: string | undefined = tc?.name ?? tc?.function?.name;
      const t = tn ? /^transfer_to_(\w+)/.exec(tn)?.[1] : undefined;
      if (t && AGENT_TO_CARD[t]) return AGENT_TO_CARD[t];
    }
  }
  return null;
}

export function GytStatusCards() {
  const active = useActiveAgent();
  const activeName = CARDS.find((c) => c.key === active)?.name;

  return (
    <div className="mx-auto w-full max-w-3xl shrink-0 px-4 pt-3 pb-1">
      <div className="mb-2 flex h-5 items-center justify-center gap-2 text-[13px] font-medium">
        {active ? (
          <span className="flex items-center gap-2 text-[#0E9F6E]">
            <span className="h-2 w-2 animate-pulse rounded-full bg-[#0E9F6E]" />
            已经交给 <b className="text-[#1B2420]">{activeName}</b> 在处理…
          </span>
        ) : (
          <span className="text-[#9AA5A0]">有事就问工友通 · 谁在忙谁就亮</span>
        )}
      </div>

      <div className="grid grid-cols-4 gap-2.5">
        {CARDS.map((c) => {
          const lit = active === c.key;
          const dim = !!active && !lit;
          return (
            <div
              key={c.key}
              className={cn(
                "rounded-2xl border bg-white p-3 transition",
                lit ? "border-2 border-[#0E9F6E] bg-[#EEF6F2]" : "border-[#EAEDEB]",
                dim && "opacity-50",
              )}
            >
              <div className="flex items-center justify-between">
                <div
                  className={cn(
                    "flex h-9 w-9 items-center justify-center rounded-xl text-lg",
                    lit ? "bg-[#0E9F6E]" : "bg-[#F1F4F3]",
                  )}
                >
                  {c.emoji}
                </div>
                <span
                  className={cn(
                    "flex items-center gap-1.5 text-[11px] font-bold",
                    lit ? "text-[#0E9F6E]" : "text-[#B4BDB8]",
                  )}
                >
                  <span
                    className={cn(
                      "h-2 w-2 rounded-full",
                      lit ? "animate-pulse bg-[#0E9F6E]" : "bg-[#D5DBD8]",
                    )}
                  />
                  {lit ? "正在忙" : "待命"}
                </span>
              </div>
              <div className="mt-2 text-[15px] font-black text-[#1B2420]">{c.name}</div>
              <div
                className={cn(
                  "mt-0.5 text-[12px] font-medium leading-tight",
                  lit ? "text-[#0E7A55]" : "text-[#8A948F]",
                )}
              >
                {lit ? c.busy : c.idle}
              </div>
              {lit && (
                <div className="mt-2 h-1.5 overflow-hidden rounded bg-[#D6EFE4]">
                  <span className="block h-full w-1/3 animate-pulse rounded bg-[#0E9F6E]" />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
