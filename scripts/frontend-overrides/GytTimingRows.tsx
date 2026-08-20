/**
 * 「每一步花了多久」的耗时行 —— 本仓自有新文件,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/GytTimingRows.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 数据从哪来 ────────────────────────────────────────────────────────
 * **直连接口 `GET /timing`,不是聊天流**(2026-08-21 换掉的)。本组件跑着的时候
 * 每秒轮询一次,跑完再补取一次;拿到的记录进 `@/lib/timing-lib` 那个 store,
 * 渲染用 `useSyncExternalStore` 订阅。
 *
 * 🔴 **为什么不走 custom 事件**(第一版就是,一条都没显示出来过):值得看的模型
 * 调用全在子图里,子图的 custom 事件要开 `streamSubgraphs: true` 才出得来,而一开它
 * 子图的 `values` 就会**整份替换**前端主状态 —— 子 Agent 说的话先出现再消失
 * (「话被收回去了」)。两件是同一个开关的两头。完整推演在 `timing-lib.ts` 头注
 * 与 `backend/src/gyt/core/timing.py` 的模块头注。
 *
 * 🔴 **不是照 GytStatusCards 那条路走的。** 那四张状态卡是从
 * `useStreamContext().messages` **倒推**当前活跃 Agent(消息尾部往前找 name /
 * transfer_to_),它压根不取耗时 —— 耗时这件事消息里没有,倒推不出来。
 * 两条路各有各的数据源,别把它们合并。
 *
 * ── 它显示在哪、受谁控制 ──────────────────────────────────────────────
 * 挂在 thread-index.tsx 的消息区末尾,并且**受「隱藏中間步驟」那个开关控制**
 * (`hideToolCalls` 为真时整块不渲染)—— 它和工具调用痕迹是同一类东西:
 * 讲架构时有用,给工地师傅看纯属干扰。观感照 tool-calls.tsx 的 `Trace`:
 * 13px 灰字、max-w-3xl 居中、一行一条。
 *
 * ── 「本輪」与「最近」 ────────────────────────────────────────────────
 * 记录存在后端,所以**刷新页面之后还在**。但那时屏幕上那批**不是这一轮跑出来的**,
 * 表头写「本輪」就是撒谎 —— 所以那种情况下换成「最近」。判据在 `timing-lib` 的
 * `markColdFill` / `isColdFill`。
 *
 * ── 🔴 观测件绝不许打扰工友 ──────────────────────────────────────────
 * 取数失败(401 / 404 / 断网 / 后端没挂这条路由)一律**安静走开**:不弹窗、
 * 不上屏、不 console.error。界面上少几行耗时是小事,给正在干活的人弹一个
 * 他看不懂的红框是大事。这条与后端那侧「拿不到会话号就丢掉这条记录」同源。
 */

import { useEffect, useRef, useSyncExternalStore } from "react";
import { useQueryState } from "nuqs";

import { getApiKey } from "@/lib/api-key";
import { cn } from "@/lib/utils";
import { useStreamContext } from "@/providers/Stream";
import {
  advanceTimingCursor,
  agentLabelFor,
  beginTimingRun,
  dropTimingsIfThreadChanged,
  formatSeconds,
  formatTokens,
  getTimingCursor,
  getTimings,
  iconFor,
  ingestTimings,
  isColdFill,
  markColdFill,
  parseTimingEnvelope,
  subscribeTimings,
  TIMING_LABELS,
  timingUrl,
  toneFor,
  totalSeconds,
  type GytTiming,
  type TimingTone,
} from "@/lib/timing-lib";
// `useApiBase` 刻意从监理那边**取用而不是再抄一份**:它是同一个「后端在哪」的判断
// (本机 dev 直连 :2024;公网是 `${GYT_PUBLIC_ORIGIN}/api`,Caddy 剥前缀转发),
// 抄两份的下场是公网改了转发前缀只改一处,而另一处静默 404。
// ⚠️ 今天这个 import **不增加打包体积** —— supervision-entry.tsx 已经在动作条上
//    静态 import 了同一个模块。哪天有人把监理面板改成懒加载,**这一行要跟着搬走**
//    (搬成一个共用小模块),别让它把整个面板又拖回主包。
import { useApiBase } from "./supervision";

/** 跑着的时候多久取一次。1 秒:识图那种十几秒的步,慢一秒看不出来;再密就是白费请求。 */
const POLL_MS = 1000;

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
  const agent = agentLabelFor(timing);

  return (
    <li
      className={cn(
        // flex-wrap 而不是定死的网格:窄屏(390px)放不下五段,让 token 那段折到
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
      {/* Agent 的中文名。它是这一行**唯一给工友看的字**(旁边那些是标识符和数字),
          所以给它最重的字重。取不到就整段不渲染 —— 路径乙和图外调用没有 Agent
          这个概念,硬塞一个「未知」只是噪声。 */}
      {agent && (
        <span className="shrink-0 font-medium text-gray-700">{agent}</span>
      )}
      {/* 🔴 模型名 / 工具名是**英文标识符,任何情况下不转简繁**(kimi-k3、
          analyze_site_photo)。等宽字体是刻意的:它们是标识符不是人话。
          ≥640px 给一个固定列宽,让右边的秒数对齐成一列(窄屏让它自己伸缩)。 */}
      <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-gray-600 sm:w-40 sm:flex-none">
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
  const cold = useSyncExternalStore(subscribeTimings, isColdFill, isColdFill);

  const [threadId] = useQueryState("threadId");
  const apiBase = useApiBase();
  const stream = useStreamContext();
  const isLoading = stream.isLoading;

  // 换会话就清空 —— 上一条会话的耗时行挂在别人的对话底下是**错的数据**,
  // 而它不报错,只会让人以为这条会话刚跑过。游标同时归零(seq 是后端全进程
  // 一个计数器,带着旧游标去拉新会话 = 开头那几步凭空少掉)。
  useEffect(() => {
    dropTimingsIfThreadChanged(threadId);
  }, [threadId]);

  // 新一轮开始就清掉上一轮的行。
  //
  // 🔴 判据是 `isLoading` 从 false 翻到 true,**不是挂在提交按钮上**:提交有三条
  //    路径(发送、重新生成、编辑后重发),挂按钮要挂三处,漏一处的表现是新旧两轮
  //    的行叠在一起、越叠越长。翻转这一下是三条路径的共同下游,只有一处。
  const wasLoading = useRef(false);
  useEffect(() => {
    if (isLoading && !wasLoading.current) beginTimingRun(threadId);
    wasLoading.current = isLoading;
  }, [isLoading, threadId]);

  // 取数:跑着的时候每秒一轮,跑完(isLoading 翻 false 让本 effect 重跑)再补取一次
  // —— 最后那一步的耗时往往就在那一下才写进缓冲。
  useEffect(() => {
    if (!threadId || !apiBase) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const fetchOnce = async (): Promise<void> => {
      try {
        const key = getApiKey();
        const res = await fetch(timingUrl(apiBase, threadId, getTimingCursor()), {
          headers: key ? { "x-api-key": key } : undefined,
        });
        // 🔴 非 2xx 一律安静走开。后端没挂这条路由(404)、令牌不对(401)、
        //    公网转发挂了 —— 每一种都不该让正在干活的人看见任何东西。
        if (!res.ok) return;
        const parsed = parseTimingEnvelope(await res.text());
        if (cancelled || !parsed.ok) return;
        // 先记游标再进数据:反过来的话,ingest 抛了(理论上不会)就会把这批
        // 重复拉一辈子。游标只前进不后退,重复记是幂等的。
        advanceTimingCursor(parsed.nextSince);
        // 「这批是刷新后补的」的判据:还没有任何行 **且** 这会儿没在跑。
        // 正在跑 = 这批就是本轮的;已经有行 = 本轮跑出来的,别改口。
        if (parsed.records.length > 0 && rows.length === 0 && !isLoading) markColdFill();
        ingestTimings(parsed.records);
      } catch {
        // 断网 / 请求被中断 —— 界面上少几行,不打扰工友。这里连 console 都不写:
        // 轮询一秒一次,写了就是刷屏,而刷屏会把真正的报错埋掉。
      }
    };

    const loop = async (): Promise<void> => {
      await fetchOnce();
      if (cancelled || !isLoading) return;
      timer = setTimeout(loop, POLL_MS);
    };
    void loop();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
    // rows.length 刻意**不进依赖**:它每收一批就变,进了依赖等于每轮重启一次
    // 轮询循环(旧循环的 setTimeout 被清掉、新循环立刻再发一次请求)——
    // 表现是请求频率翻倍且不稳定。fetchOnce 里读它读的是本次渲染的闭包值,
    // 而那正好够用:markColdFill 只关心「第一批之前有没有行」。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, apiBase, isLoading]);

  // 一行都没有就整块消失(不留空壳)—— 还没取到、或者这一轮刚被清空。
  if (rows.length === 0) return null;

  return (
    <section
      aria-label={TIMING_LABELS.ariaLabel}
      className="mx-auto w-full max-w-3xl text-[13px]"
    >
      <div className="px-1.5 py-1 text-[12px] font-medium text-gray-400">
        {/* 「合計」不是「耗時」:它是各步之和,不是这一轮的墙上时间
            (排队和空档不算,真并行还会重复计)。理由在 timing-lib.totalSeconds。
            前缀「本輪 / 最近」的判据见文件头注。 */}
        {cold ? TIMING_LABELS.headPrefixCold : TIMING_LABELS.headPrefix} {rows.length}{" "}
        {TIMING_LABELS.headStep} · {TIMING_LABELS.headTotal}{" "}
        {formatSeconds(totalSeconds(rows))}
      </div>
      <ul className="grid gap-px">
        {rows.map((t) => (
          // key 用后端发的 seq:它在一个进程里唯一且稳定。
          // (上一版用的是下标 —— 那时列表只追加;现在 ingest 会按 seq 排序合并,
          //  下标不再稳定,继续用它会让 React 把行认错、状态串位。)
          <TimingRow
            key={t.seq}
            timing={t}
          />
        ))}
      </ul>
    </section>
  );
}
