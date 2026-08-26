/**
 * 工友通首页 · 調度中樞 → 4 個子 Agent 的「流」導線圖(前端提升方案 1b「流」)。
 *
 * 新增组件(上游 agent-chat-ui 没有),由 scripts/setup-frontend.sh 直接拷进
 * frontend/src/components/thread/GytStatusCards.tsx,并在 thread-index.tsx 里挂一次
 * <GytStatusCards />(放在消息区之上,首页/聊天都常驻可见 —— 这样"派活时导线亮"在跑的过程中看得到)。
 *
 * 卡片是**状态灯,不是按钮**(用户定的):当调度中枢把当前这句话派给某个子 Agent 时,
 * 对应的**导线亮起、会走**(flow-dash),卡片亮起(绿边 + 呼吸点 + 进度扫光 + "正在做什么"文案),
 * 其余变暗;没有活跃 Agent 时全部待命、导线是静止的浅灰。
 *
 * "当前活跃 Agent"从 useStreamContext() 的流里推断:isLoading 时,从消息尾部往前找最近的
 * 子 Agent 署名(message.name)或 transfer_to_<name> 交接;推不出就当全待命(不报错、不乱亮)。
 * 后端子 Agent 名 → 卡片的映射见 AGENT_TO_CARD(inspection/report 归到「识隐患」这条链)。
 *
 * ── 1b「流」设计落地时的调色板纪律(2026-08-26) ───────────────────────────────
 * 品牌绿从 `#0E9F6E` 换成 `#16805C`(更深、更沉,方案定的)。实测 WCAG:
 *   `#16805C` 当文字:白底 4.91、页底渐变 4.52 —— **两处都过 AA**,所以它既当品牌色
 *   (背景 / 圆点 / 边框 / 导线)也能当文字。文字要更稳时用 `#0F5F44`(7.66)。
 *   次级灰用 `#5F6B66`(白 5.55 / 页底 5.12,过 AA);**方案里那档 `#8A948F` 不过 AA
 *   (2.88),一律不当文字用** —— 这条与 GytStatusCards 原来的「#626D68 灰阶下限」同理。
 *
 * ⚠️ 关键帧动画(flow-dash / halo / breathe / sweep)**内联在本组件的 <style> 里**,
 *    不写进 globals.css:globals.css 是上游文件、不在覆盖件清单里,动它会脱离
 *    setup-frontend.sh 的管理。keyframes 全局生效,名字加 `gyt-` 前缀避免撞车。
 *
 * ⚠️ 图标从 emoji 换成 lucide-react 线性图标(scan-eye / list-checks / ruler /
 *    book-open-text),与方案 dc.html 一致;lucide-react 本来就是全站图标库。
 */

import { useStreamContext } from "@/providers/Stream";
import { cn } from "@/lib/utils";
import { ScanEye, ListChecks, Ruler, BookOpenText, type LucideIcon } from "lucide-react";

const AGENT_TO_CARD: Record<string, string> = {
  safety: "safety",
  inspection: "safety",
  report: "safety",
  schedule: "schedule",
  cad: "cad",
  knowledge: "knowledge",
};

// 界面恒繁體(负责人 2026-08-18 定案:中建国际在港,现场语言是繁體)。
// 🔴 `key` 与上面 AGENT_TO_CARD 的键**都是后端 Agent 名 / 内部卡片号,一律留英文**,
//    转了就再也匹配不上、卡片永远不亮,而且**一行报错都不会有**。
// 这几段中文只是给人看的字,所以**源码里就写成繁體**、不走运行时转换。
// 「排期」简繁同形,所以它看着没改,不是漏了。
//
// `sample` 是点这张卡时**填进输入框**的那句话(2026-08-25 设计审计加的)。
// 它是**给人照抄和改的例句,不是自动发送的指令** —— 填完停在输入框里,发不发、改不改由工友决定。
//   ①「識隱患」故意不给例句:它的入口是拍照,不是打字。
const CARDS: {
  key: string;
  name: string;
  Icon: LucideIcon;
  idle: string;
  busy: string;
  sample?: string;
}[] = [
  { key: "safety", name: "識隱患", Icon: ScanEye, idle: "拍照識別現場隱患", busy: "正在看這張照片有沒有隱患…" },
  { key: "schedule", name: "排期", Icon: ListChecks, idle: "記任務 / 改期限 / 查進度", busy: "正在記任務…",
    sample: "記一下:明天上午整改臨邊防護" },
  { key: "cad", name: "圖紙", Icon: Ruler, idle: "讀 DXF 尺寸·標高·構件", busy: "正在讀圖紙參數…",
    sample: "當前項目有哪些圖紙?" },
  { key: "knowledge", name: "規範", Icon: BookOpenText, idle: "查消防 / 防火 / 安全條文", busy: "正在查規範…",
    sample: "腳手架的防護欄杆高度,規範怎麼要求?" },
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

/**
 * 次级灰(卡片右上「待命」、卡片说明、副标题)。方案 1b 用的 `#8A948F` 不过 WCAG AA
 * (白底 3.13、页底 2.88),这里落到过 AA 的 `#5F6B66`(白 5.55 / 页底 5.12)。
 * 🔴 低于此的层级用字号/字重表达,不用更浅的灰 —— 工地是户外强光,3:1 在太阳下等于没有。
 */
const SUBTLE = "text-[#5F6B66]";

/**
 * 关键帧 + 导线基础样式。内联注入,scoped 靠 `gyt-` 前缀。
 * flow-dash:亮起的导线上有一段更亮的绿在走,做出「派活流过去」的方向感。
 */
function FlowStyle() {
  return (
    <style>{`
      @keyframes gyt-flowdash { to { background-position: 220px 0; } }
      @keyframes gyt-halo { 0%,100% { box-shadow: 0 0 0 0 rgba(22,128,92,.16) } 50% { box-shadow: 0 0 0 8px rgba(22,128,92,0) } }
      @keyframes gyt-breathe { 0%,100% { opacity:.35 } 50% { opacity:1 } }
      @keyframes gyt-sweep { 0% { transform: translateX(-100%) } 100% { transform: translateX(320%) } }
      .gyt-wire { background:#E4E8E6; }
      .gyt-wire-lit { background:linear-gradient(180deg,#16805C,#8FC4B0); background-size:100% 80px; animation:gyt-flowdash 1.6s linear infinite; }
      .gyt-halo { animation:gyt-halo 2.4s ease-out infinite; }
      .gyt-breathe { animation:gyt-breathe 1.6s ease-in-out infinite; }
      .gyt-sweep { animation:gyt-sweep 2.4s linear infinite; }
      @media (prefers-reduced-motion: reduce) {
        .gyt-wire-lit,.gyt-halo,.gyt-breathe,.gyt-sweep { animation:none !important; }
      }
    `}</style>
  );
}

/**
 * @param onPick 点了带例句的那几张卡时调用,把例句**填进输入框**(不自动发送)。
 *               不传就退回纯展示 —— 组件在任何情况下都不该因为少个回调而炸。
 */
export function GytStatusCards({ onPick }: { onPick?: (text: string) => void } = {}) {
  const active = useActiveAgent();
  const activeIndex = CARDS.findIndex((c) => c.key === active);
  const activeName = CARDS[activeIndex]?.name;

  return (
    <div className="mx-auto w-full max-w-3xl shrink-0 px-4 pt-3 pb-1">
      <FlowStyle />

      {/* ── 調度中樞:导线的起点 ───────────────────────────────────────── */}
      <div className="flex items-center justify-center">
        <span
          className={cn(
            "flex items-center gap-2 rounded-full bg-white px-4 py-[7px] text-[13px] font-bold shadow-[0_1px_2px_rgba(23,28,26,.06)]",
            active ? "text-[#0F5F44] gyt-halo" : SUBTLE,
          )}
        >
          <span
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded-md text-[11px] font-black",
              active ? "bg-[#16805C] text-white" : "bg-[#EEF3F1] text-[#16805C]",
            )}
          >
            工
          </span>
          {active ? (
            <>
              已經交給 <b className="text-[#171C1A]">{activeName}</b> 在處理…
            </>
          ) : (
            "調度中樞 · 全部待命"
          )}
        </span>
      </div>

      {/* 中枢往下的竖导线 */}
      <div className="flex justify-center">
        <span className={cn("h-4 w-0.5", active ? "gyt-wire-lit" : "gyt-wire")} />
      </div>
      {/* 横向总线(对齐到四列的中心:12.5% ~ 87.5%) */}
      <div className={cn("mx-[12.5%] h-0.5 rounded", active ? "bg-[#16805C]" : "gyt-wire")} />
      {/* 四条降到卡片的竖导线;只有活跃那条会走 */}
      <div className="grid grid-cols-2 sm:grid-cols-4">
        {CARDS.map((c, i) => (
          <span
            key={c.key}
            className={cn(
              "hidden h-4 w-0.5 justify-self-center sm:block",
              active === c.key ? "gyt-wire-lit" : "gyt-wire",
            )}
          />
        ))}
      </div>

      {/* ── 4 张能力卡 ─────────────────────────────────────────────────── */}
      <div className="mt-2 grid grid-cols-2 gap-2.5 sm:mt-0 sm:grid-cols-4">
        {CARDS.map((c) => {
          const lit = active === c.key;
          const dim = !!active && !lit;
          const pickable = !!c.sample && !active && !!onPick;
          const Icon = c.Icon;
          return (
            <div
              key={c.key}
              // 🔴 **这四张卡是状态灯,不是按钮**(2026-08-15 定),所以仍是 <div>,无 role="button"。
              //    2026-08-25 起给「可点」的卡一个去处:点了往输入框填一句例句,停在那儿等人改或发。
              //    ⚠️ 别升级成「点了直接发送」:那就替工友做决定了。
              onClick={pickable ? () => onPick!(c.sample!) : undefined}
              tabIndex={pickable ? 0 : undefined}
              onKeyDown={
                pickable
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onPick!(c.sample!);
                      }
                    }
                  : undefined
              }
              title={pickable ? `試試:${c.sample}` : undefined}
              className={cn(
                "rounded-[20px] bg-white p-3.5 shadow-[0_1px_2px_rgba(23,28,26,.05),0_14px_34px_rgba(23,28,26,.05)] transition",
                lit && "shadow-[0_1px_2px_rgba(23,28,26,.05),0_14px_34px_rgba(22,128,92,.12)] outline outline-[1.5px] outline-[#16805C] gyt-halo",
                dim && "opacity-50",
                pickable &&
                  "cursor-pointer hover:-translate-y-px hover:shadow-[0_6px_18px_rgba(23,28,26,.09)] focus-visible:outline focus-visible:outline-[1.5px] focus-visible:outline-[#16805C] focus-visible:ring-2 focus-visible:ring-[#8FC4B0] focus-visible:outline-none",
              )}
            >
              <div className="flex items-center justify-between">
                <span
                  className={cn(
                    "flex h-9 w-9 items-center justify-center rounded-[13px]",
                    lit ? "bg-[#16805C]" : "bg-[#F2F5F4]",
                  )}
                >
                  <Icon className="h-[18px] w-[18px]" strokeWidth={2} color={lit ? "#ffffff" : "#5F6B66"} />
                </span>
                <span
                  className={cn(
                    "flex items-center gap-1.5 text-[11px] font-bold",
                    lit ? "text-[#0F5F44]" : SUBTLE,
                  )}
                >
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      lit ? "gyt-breathe bg-[#16805C]" : "bg-[#C9D2CD]",
                    )}
                  />
                  {lit ? "正在忙" : "待命"}
                </span>
              </div>
              <div className="mt-3 text-[15px] font-black text-[#171C1A]">{c.name}</div>
              <div
                className={cn(
                  "mt-0.5 text-[12px] font-medium leading-tight",
                  lit ? "text-[#0F5F44]" : SUBTLE,
                )}
              >
                {lit ? c.busy : c.idle}
              </div>
              {lit && (
                <div className="mt-3 h-1 overflow-hidden rounded bg-[#E3EFE9]">
                  <span className="gyt-sweep block h-full w-1/3 rounded bg-[#16805C]" />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
