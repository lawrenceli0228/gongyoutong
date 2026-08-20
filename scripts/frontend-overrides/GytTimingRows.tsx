/**
 * 「每一步花了多久」的耗时行 —— 本仓自有新文件,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/GytTimingRows.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 数据从哪来 ────────────────────────────────────────────────────────
 * 后端经 LangGraph 的 custom 流推 `{ gyt_timing: {…} }`,
 * `providers/Stream.tsx`(覆盖件 stream-provider.tsx)的 `onCustomEvent` 收下来,
 * 存进 `@/lib/timing-lib` 那个 run 级 store,本组件用 `useSyncExternalStore` 订阅。
 *
 * 🔴 **不是照 GytStatusCards 那条路走的。** 那四张状态卡是从
 * `useStreamContext().messages` **倒推**当前活跃 Agent(消息尾部往前找 name /
 * transfer_to_),它压根不消费 custom 事件 —— 耗时这件事消息里没有,倒推不出来。
 * 两条路各有各的数据源,别把它们合并。
 *
 * ── 它显示在哪、受谁控制 ──────────────────────────────────────────────
 * 挂在 thread-index.tsx 的消息区末尾,并且**受「隱藏中間步驟」那个开关控制**
 * (`hideToolCalls` 为真时整块不渲染)—— 它和工具调用痕迹是同一类东西:
 * 讲架构时有用,给工地师傅看纯属干扰。观感照 tool-calls.tsx 的 `Trace`:
 * 13px 灰字、max-w-3xl 居中、一行一条。
 *
 * ── 🔴 它是易失的 ─────────────────────────────────────────────────────
 * custom 事件不进线程状态、不进检查点:刷新页面 / 翻回历史会话,这些行就没了
 * (而聊天消息还在)。所以**不要**给它加「历史耗时」这类入口 —— 数据根本不在。
 * 完整推演在 timing-lib.ts 头注。
 */

import { useSyncExternalStore } from "react";
import { cn } from "@/lib/utils";
import {
  formatSeconds,
  formatTokens,
  getTimings,
  iconFor,
  subscribeTimings,
  TIMING_LABELS,
  toneFor,
  totalSeconds,
  type GytTiming,
  type TimingTone,
} from "@/lib/timing-lib";

/**
 * 三档语气的配色。
 *
 * 🔴 **颜色不是唯一信号**:慢的那行除了变琥珀色,右边还挂一颗写着「慢」的小标签;
 *    失败的除了变红,还挂「失敗」。理由不是洁癖 —— 工地上看屏幕多半是强光下、
 *    戴着手套斜着看,而色觉障碍在建筑工人里的比例本来也不低。
 *    只靠颜色 = 对一部分人等于没做。
 */
const TONE_ROW: Record<TimingTone, string> = {
  fail: "bg-red-50/70",
  slow: "bg-amber-50/70",
  normal: "",
};

const TONE_SECONDS: Record<TimingTone, string> = {
  fail: "font-bold text-red-600",
  slow: "font-bold text-amber-600",
  normal: "text-gray-500",
};

const TONE_CHIP: Record<TimingTone, string> = {
  fail: "bg-red-100 text-red-700 ring-red-200",
  slow: "bg-amber-100 text-amber-700 ring-amber-200",
  normal: "",
};

/** 该挂哪个小标签;normal 档不挂。 */
function chipText(tone: TimingTone): string | null {
  if (tone === "fail") return TIMING_LABELS.fail;
  if (tone === "slow") return TIMING_LABELS.slow;
  return null;
}

function TimingRow({ timing }: { timing: GytTiming }) {
  const tone = toneFor(timing);
  const tokens = formatTokens(timing);
  const chip = chipText(tone);

  return (
    <li
      className={cn(
        // flex-wrap 而不是定死的网格:窄屏(390px)放不下四段,让 token 那段折到
        // 第二行,而不是把工具名压成竖条 —— 后者是本仓在动作条上踩过的原病
        // (thread-index.tsx 里那几段实录)。
        "flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-md px-1.5 py-0.5",
        TONE_ROW[tone],
      )}
    >
      {/* emoji 只是给眼睛的锚点,读屏念它没有意义(「警告 高压电」之类) ——
          真正的语义由右边那颗「失敗」/「慢」标签承担,那是真文字。 */}
      <span
        aria-hidden="true"
        className="shrink-0 leading-none"
      >
        {iconFor(timing)}
      </span>
      {/* 🔴 模型名 / 工具名是**英文标识符,任何情况下不转简繁**(kimi-k3、
          analyze_site_photo)。等宽字体是刻意的:它们是标识符不是人话。
          ≥640px 给一个固定列宽,让右边的秒数对齐成一列(窄屏让它自己伸缩)。 */}
      <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-gray-600 sm:w-44 sm:flex-none">
        {timing.name}
      </span>
      <span className={cn("shrink-0 tabular-nums", TONE_SECONDS[tone])}>
        {formatSeconds(timing.seconds)}
      </span>
      {chip && (
        <span
          className={cn(
            "shrink-0 rounded-full px-1.5 text-[11px] font-bold ring-1 ring-inset",
            TONE_CHIP[tone],
          )}
        >
          {chip}
        </span>
      )}
      {/* tokens 为 null 时**整段不渲染** —— 命中缓存那一步没有 token 可言,
          写「輸入 0」是谎报它真调了模型(判据在 timing-lib.formatTokens)。 */}
      {tokens && (
        <span className="min-w-0 truncate text-gray-400 tabular-nums">
          {tokens}
        </span>
      )}
    </li>
  );
}

export function GytTimingRows() {
  // getServerSnapshot 直接复用 getTimings:它初始返回的是模块级的那个冻结空数组,
  // SSR 与首帧拿到的是同一个引用,不会触发 hydration 不匹配。
  const rows = useSyncExternalStore(subscribeTimings, getTimings, getTimings);

  // 一行都没有就整块消失(不留空壳)—— 后端还没推、或者这一轮刚被清空。
  if (rows.length === 0) return null;

  return (
    <section
      aria-label={TIMING_LABELS.ariaLabel}
      className="mx-auto w-full max-w-3xl text-[13px]"
    >
      <div className="px-1.5 py-1 text-[12px] font-medium text-gray-400">
        {/* 「合計」不是「耗時」:它是各步之和,不是这一轮的墙上时间
            (排队和空档不算,真并行还会重复计)。理由在 timing-lib.totalSeconds。 */}
        {TIMING_LABELS.headPrefix} {rows.length} {TIMING_LABELS.headStep} ·{" "}
        {TIMING_LABELS.headTotal} {formatSeconds(totalSeconds(rows))}
      </div>
      <ul className="grid gap-px">
        {rows.map((t, idx) => (
          // 用下标做 key:同一轮里同一个模型会被调很多次,name + seconds 都不唯一,
          // 而这个列表**只追加不重排**,下标天然稳定。
          <TimingRow
            key={idx}
            timing={t}
          />
        ))}
      </ul>
    </section>
  );
}
