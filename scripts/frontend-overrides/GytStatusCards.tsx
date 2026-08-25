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

// 界面恒繁體(负责人 2026-08-18 定案:中建国际在港,现场语言是繁體)。
// 🔴 `key` 与上面 AGENT_TO_CARD 的键**都是后端 Agent 名 / 内部卡片号,一律留英文**,
//    转了就再也匹配不上、卡片永远不亮,而且**一行报错都不会有**。
// 这三段中文只是给人看的字,所以**源码里就写成繁體**、不走运行时转换:
// 这四张卡是常驻主界面(thread-index.tsx 里无条件挂一次),挂 useHantUI 等于
// 让每个用户首屏都拉 438 KB 字典 —— 包括从不看繁體的简体工友
// (那条红线见 hant-convert.tsx 的「图 3」)。
// 「排期」简繁同形,所以它看着没改,不是漏了。
//
// `sample` 是点这张卡时**填进输入框**的那句话(2026-08-25 设计审计加的,见下面
// GytStatusCards 里那段推演)。它是**给人照抄和改的例句,不是自动发送的指令** ——
// 填完停在输入框里,发不发、改不改由工友决定。
// ⚠️ 写例句的两条:
//   ① 必须是这个 Agent **真答得了**的问法(拿不准就去 eval/datasets/routing.csv
//      找同类的行,那里每一行都标了 expected_agent);
//   ② 「識隱患」那张**故意不给例句**:它的入口是拍照,不是打字。给一句
//      「幫我看看這張照片」只会让人对着空输入框发懵 —— 没有照片,那句话没有意义。
const CARDS: {
  key: string;
  name: string;
  emoji: string;
  idle: string;
  busy: string;
  sample?: string;
}[] = [
  { key: "safety", name: "識隱患", emoji: "📷", idle: "拍照識別現場隱患", busy: "正在看這張照片有沒有隱患…" },
  { key: "schedule", name: "排期", emoji: "📋", idle: "記任務 / 改期限 / 查進度", busy: "正在記任務…",
    sample: "記一下:明天上午整改臨邊防護" },
  { key: "cad", name: "圖紙", emoji: "📐", idle: "讀 DXF 尺寸·標高·構件", busy: "正在讀圖紙參數…",
    sample: "當前項目有哪些圖紙?" },
  { key: "knowledge", name: "規範", emoji: "📖", idle: "查消防 / 防火 / 安全條文", busy: "正在查規範…",
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
 * 待命态的灰 —— **一个数,三处用**(顶上那行 caption、卡片右上「待命」、卡片说明文字)。
 *
 * 🔴 2026-08-25 设计审计实测,原先三处全都不过 WCAG AA:
 *
 *     caption   #9AA5A0 13px  → 2.30:1   (页底 #F1F4F3 上)
 *     待命徽章  #B4BDB8 11px  → 约 2.0:1 (白卡上;11px 粗体**不吃**粗体豁免,
 *                                        那条豁免的门槛是 ≥14px 粗体)
 *     卡片说明  #8A948F 12px  → 3.13:1   (白卡上)
 *
 * 门槛 4.5。#626D68 在**两种背景上都过**:白卡 5.38、页底 4.86 —— 所以三处共用一个数,
 * 不必按背景分叉(分叉了就会有人只改一处)。
 *
 * 🔴 **后来这个数扩成了全仓的文字灰**(同日第二轮审计):清点下来,覆盖件里
 *    28 处文字用的是 `#8A948F` / `#A2ABA6` / `#9AA5A0` / `#B4BDB8` 四档浅灰,
 *    **没有一处是边框或背景,全是文字,而四档全部不过 AA**(1.92 ~ 3.13)。
 *    也就是说这套调色板里**根本没有一档「比 #33403A 浅、又还读得见」的灰** ——
 *    四档都是在同一个不存在的位置上反复取色。
 *
 *    结论:低于 `#626D68` 的层级**不用亮度表达,用字号和字重表达**。
 *    现在文字色只剩六个,每一个都在白底和页底上都过 4.5:
 *
 *        #1B2420 15.9/14.0   #33403A 10.9/9.6   #626D68 5.4/4.7
 *        #0E7A55  5.3/4.7    #8F6318  5.3/4.7   #C0392B 5.4/4.8
 *
 *    ⚠️ **绿色分两个**:`#0E9F6E` 是**品牌绿**,只许当背景 / 圆点 / 边框
 *    (当文字只有 3.39);当文字一律用 `#0E7A55`。这两个本来就都在调色板里,
 *    以前是混着用的。加新文案时别顺手写 `text-[#0E9F6E]`。
 *
 * ⚠️ 这不是「灰得好不好看」的问题。工地是**户外强光**环境,手机屏幕反光,
 *    办公室里勉强能读的 3:1 在太阳底下等于没有。挑更浅的灰之前先拿这个脚本重算:
 *
 *      python3 -c "
 *      def lin(c):
 *          c/=255
 *          return c/12.92 if c<=0.03928 else ((c+0.055)/1.055)**2.4
 *      def L(h):
 *          h=h.lstrip('#'); r,g,b=(int(h[i:i+2],16) for i in (0,2,4))
 *          return 0.2126*lin(r)+0.7152*lin(g)+0.0722*lin(b)
 *      f=L('#626D68')
 *      for bg in ('#FFFFFF','#F1F4F3'):
 *          b=L(bg); print(bg, round((max(f,b)+.05)/(min(f,b)+.05),2))"
 */
const IDLE_GRAY = "text-[#626D68]";

/**
 * @param onPick 点了带例句的那几张卡时调用,把例句**填进输入框**(不自动发送)。
 *               不传就退回纯展示 —— 组件在任何情况下都不该因为少个回调而炸。
 */
export function GytStatusCards({ onPick }: { onPick?: (text: string) => void } = {}) {
  const active = useActiveAgent();
  const activeName = CARDS.find((c) => c.key === active)?.name;

  return (
    <div className="mx-auto w-full max-w-3xl shrink-0 px-4 pt-3 pb-1">
      <div className="mb-2 flex h-5 items-center justify-center gap-2 text-[13px] font-medium">
        {active ? (
          <span className="flex items-center gap-2 text-[#0E7A55]">
            <span className="h-2 w-2 animate-pulse rounded-full bg-[#0E9F6E]" />
            已經交給 <b className="text-[#1B2420]">{activeName}</b> 在處理…
          </span>
        ) : (
          <span className={IDLE_GRAY}>誰在忙,誰就亮起來</span>
        )}
      </div>

      {/* 窄屏 2×2、≥640px 恢复四列一排。
          为什么加这个断点:2026-08-15 用无头浏览器在 390×844(iPhone)视口实测,
          四列并排会把每张卡压到 **56px 宽**,卡名与说明文字被压成竖条、卡片顶部被裁掉。
          页面并没有横向滚动(scrollWidth == innerWidth),所以不是溢出而是 flex/grid 挤压。
          640px 是 Tailwind 的 sm 断点:768(iPad)与 1280(桌面)都在它之上,
          走的仍是原来的 grid-cols-4,观感一个像素不变。 */}
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
        {CARDS.map((c) => {
          const lit = active === c.key;
          const dim = !!active && !lit;
          // 只有「带例句」且「当前没在跑」的卡才是可点的。
          // 跑起来之后全部变回纯展示:那时候它们在报状态,点一下换掉输入框里的字
          // 只会打断人。
          const pickable = !!c.sample && !active && !!onPick;
          return (
            <div
              key={c.key}
              // 🔴 **这四张卡是状态灯,不是按钮** —— 这条 2026-08-15 定的没变,
              //    所以这里仍然是 <div>,不是 <button>,也没有 role="button"。
              //
              //    2026-08-25 设计审计改的是另一件事:它们**长得像能点**
              //    (白卡 + 圆角 + 边框 + 阴影,占着桌面首屏最上方、手机 31% 的屏),
              //    而点下去什么都不发生 —— 实测 `cursor: auto`、无 role、DOM 里是 DIV。
              //    工友会去点「識隱患」,然后得到一个死路。
              //
              //    修法是**给那个点击一个去处,而不是把卡片变成按钮**:
              //    点了往输入框填一句这张卡真答得了的例句,停在那儿等人改或发。
              //    状态灯还是状态灯(该亮照亮、该暗照暗),只是不再是死路。
              //    ⚠️ 别升级成「点了直接发送」:那就真变成按钮了,而且会替工友
              //       做决定 —— 他可能只是想看看这张卡是干嘛的。
              onClick={pickable ? () => onPick!(c.sample!) : undefined}
              // 键盘可达:能点的东西就得能用 Tab 走到、能用回车/空格触发。
              // 不能点的那几张 tabIndex 给 undefined,不进 Tab 序列(它们只是灯)。
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
                "rounded-2xl border bg-white p-3 transition",
                lit ? "border-2 border-[#0E9F6E] bg-[#EEF6F2]" : "border-[#EAEDEB]",
                dim && "opacity-50",
                // 可点的才给手型和悬停反馈 —— 反过来说:**不给例句的那张卡
                // 现在明确不是手型**,它不再假装自己能点。
                pickable &&
                  "cursor-pointer hover:border-[#7FCDAE] hover:shadow-[0_6px_18px_rgba(27,36,32,0.08)] focus-visible:border-[#0E9F6E] focus-visible:ring-2 focus-visible:ring-[#7FCDAE] focus-visible:outline-none",
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
                    lit ? "text-[#0E7A55]" : IDLE_GRAY,
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
                  lit ? "text-[#0E7A55]" : IDLE_GRAY,
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
