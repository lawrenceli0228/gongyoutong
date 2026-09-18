/**
 * 监理处置的**常驻入口**(W10)—— 本仓自有新文件,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/supervision-entry.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 它为什么存在 ──────────────────────────────────────────────────────
 * W9 把监理面板的入口挂在「聊天流里那条工具返回」上(tool-calls.tsx 的隐患卡),
 * 而 supervisor 的 `output_mode="last_message"` 会把子 Agent 的工具返回整个丢掉 ——
 * 那张卡从上线起一次都没渲染出来过,面板一次都没打开过。三条取数路径实测全堵:
 * 线程状态 0 条、历史检查点 0 条、`useStream` 没订阅 `subgraphs`。
 * 全过程在 `docs/W10_界面取不到工具返回_方案.md`。
 *
 * 修法照 **W7 打卡面板**的先例(checkin.tsx 的 `CheckinEntry`):
 * **处置面板是操作台,不是聊天产物** —— 它该有自己的常驻按钮和自己的数据源(直连 HTTP)。
 * 判据很实在:刷新一下页面,聊天里渲染出来的东西全没了,而没确认的隐患还在台账里等人。
 *
 * ── 分层 ──────────────────────────────────────────────────────────────
 * 本文件只做两件事:①一颗带待确认计数的按钮;②把面板挂起来。
 * 面板本体、所有处置动作在 ./supervision;可测的纯逻辑(地址、解析、词表)
 * 在 @/lib/supervision-lib —— 那一份零依赖,scripts/frontend-tests/ 的 vitest 直接测。
 */

import { useCallback, useEffect, useState } from "react";
import { ClipboardCheck } from "lucide-react";
import { getApiKey } from "@/lib/api-key";
import {
  HAZARD_SCOPE_PENDING,
  hazardListUrl,
  parseHazardListEnvelope,
  OPEN_SUPERVISION_EVENT,
} from "@/lib/supervision-lib";
import { SupervisionPanel, useApiBase, useProjectFilter } from "./supervision";

/**
 * 产物的静态出口。**这是 `NEXT_PUBLIC_ARTIFACT_BASE` 这条链的第四个读者**
 * (前三个:human.tsx 的历史照片 / 图纸、tool-calls.tsx 的巡检记录卡、
 *  checkin.tsx 的打卡凭证图)—— CLAUDE.md 同源清单「产物出口 ARTIFACT_BASE 这条链」
 * 那一行要把本文件登记进去,少登记一处下一个改公网地址的人就会漏掉它。
 *
 * ⚠️ 为什么不得不多这一处:面板的 `artifactBase` 一直是**由调用方传进来**的
 * (supervision.tsx 自己不读 env,见 `SupervisionDocCards` 头注)。W9 时唯一的
 * 调用方是 tool-calls.tsx,它手里有这个常量;W10 之后本文件成了新链路的根,
 * 而 tool-calls.tsx 里那份是模块私有的 —— 除非改它导出,否则拿不到。
 * 本泳道不动 tool-calls.tsx(少改一个文件,合流时少一处冲突面),所以在这儿读一次。
 * 结构上它与 tool-calls.tsx 是对称的:两条链路各自的根各读一次,面板始终只收 props。
 *
 * 这条链**断在任何一环都不报错**:站点照开、提问照答,只是文书点了没反应 ——
 * 而 https 页面拉 http 资源属于 mixed content,浏览器**连请求都不发**,
 * 界面上一点线索都没有。默认值只绑回环(本机 `make serve-artifacts`);
 * 公网由 docker-compose.vps.yml 的 build args 传 `${GYT_PUBLIC_ORIGIN}/artifacts`。
 * ⚠️ NEXT_PUBLIC_* 是**编译期**变量:改了要重建前端镜像。
 */
const ARTIFACT_BASE =
  process.env.NEXT_PUBLIC_ARTIFACT_BASE || "http://127.0.0.1:8788";

/**
 * 徽章的轮询间隔。
 *
 * ── 为什么需要轮询(而不是只在挂载和关面板时刷)────────────────────────
 * 待确认隐患的**主要来源是聊天**:工友在同一个标签页里传一张工地照片,safety 判读完
 * 就把违规项登记成 pending。这条路径**不会触发任何浏览器事件** —— 没有页面跳转、
 * 没有 focus/visibilitychange(人一直在这个页面上)、也没有 storage 事件。
 * 没有轮询的话,那几条新隐患要等到下次刷新页面才在徽章上出现,而 pending 是
 * **不催办**的(D17 定的:待确认不算整改率、不进超期清单、不会被升级)——
 * 没人知道有东西要确认,就等于没登记。
 *
 * ── 为什么是 60 秒 ────────────────────────────────────────────────────
 * 它只驱动一个计数徽章,不是取数主路径(面板一打开就自己拉完整清单)。
 * 一分钟的延迟对「有活要干」这件事完全够用,而这条 GET 每次都真查一遍 sqlite;
 * 打卡那条配对轮询敢用 2 秒是因为它守着「人正拿着手机等」的几十秒窗口,
 * 这里没有那种窗口。**别照抄 PAIR_POLL_INTERVAL_MS。**
 */
const PENDING_POLL_INTERVAL_MS = 60_000;

/**
 * 徽章上最多显示到几。
 *
 * 上限不是为了好看:动作条在 390×844 上是**被挤爆过的**(checkin.tsx 的
 * `CheckinEntry` 注释里记着实测 —— 那颗按钮当时只剩 42px 宽、两个字竖排)。
 * 徽章里放一个三位数就是又往那条已经很紧的一行里塞宽度。超过就显示「99+」:
 * 到了这个量级,精确到个位对「今天要处理多少」这个判断毫无帮助。
 */
const BADGE_MAX = 99;

/**
 * 监理处置入口 —— thread-index.tsx 把它放在输入框动作条上(`<CheckinEntry />` 旁边)。
 *
 * `type="button"` **不能省**:它被塞在聊天输入的 `<form>` 里,裸 button 默认
 * `type=submit`,点一下会把聊天输入连带发出去(与 CheckinEntry 同一条)。
 *
 * hydration:本组件在渲染路径上**不读任何浏览器专有状态**(没有 localStorage /
 * navigator / location,面板的开关是纯 state、初值 false),所以不需要
 * `CheckinEntry` 那道 `mounted` 开关 —— 那道开关是给「地址栏 ?checkin=1 也能开面板」
 * 那条路准备的(服务端同样看得见 query,不挡就会 SSR 出一份、客户端出另一份)。
 * 计数的 fetch 在 `useEffect` 里,服务端根本不跑。
 * ⚠️ 哪天给这颗按钮也加「URL 直接打开面板」之类的能力,那道开关就得补上。
 */
export function SupervisionEntry() {
  const apiBase = useApiBase();
  // 与面板同一个口径(空串 = 没选工地 = 不筛;转换只写在 useProjectFilter 里)。
  // 徽章必须跟着工地走:顶栏切到 A 工地、徽章却数着全部工地的待确认,
  // 点开面板一看只有两条 —— 人会以为面板漏了。
  const projectFilter = useProjectFilter();
  const [open, setOpen] = useState(false);
  /** null = 还没成功读到过(不显示徽章);数字 = 台账里待确认几条。 */
  const [pendingCount, setPendingCount] = useState<number | null>(null);

  /**
   * 数一次待确认。
   *
   * 三个触发点靠下面那个 effect 的依赖数组同时兜住:挂载、**面板关闭之后**、
   * 以及每 `PENDING_POLL_INTERVAL_MS` 一次。
   *
   * 🔴 **面板开着的时候一次都不发**(effect 首行那个 early return):面板自己在
   * 拉完整清单、还在往台账上写动作,这时候徽章再去数一遍,数到的是一个正在变的中间态,
   * 白白多一条请求还可能自相矛盾。关面板时 `open` 翻回 false,effect 重跑 ——
   * **那一下正好就是「关面板后刷新」**,不用另写一个 onClose 回调。
   *
   * 失败的处理是「**保持上一次的数,什么都不说**」:
   * 这颗徽章是提醒,不是事实来源 —— 网络抖一下就把数字抹掉(或者归零),
   * 反而制造出「待确认清零了」的假象,那比不更新坏得多。真要知道台账里有什么,
   * 点开面板 —— 那里有完整的「读不出来 / 真的没有」两种状态分开画。
   */
  useEffect(() => {
    if (open) return;
    let cancelled = false;
    const controller = new AbortController();
    let inFlight = false;

    const count = async () => {
      // 上一发还没回来就跳过这一拍。信号一差(工地常态)请求会叠着发,
      // 而叠起来换到的只是同一个问题问两遍(同 checkin.tsx 那条配对轮询)。
      if (inFlight) return;
      inFlight = true;
      try {
        const apiKey = getApiKey();
        const res = await fetch(
          hazardListUrl(apiBase, { scope: HAZARD_SCOPE_PENDING, projectId: projectFilter }),
          {
            headers: apiKey ? { "x-api-key": apiKey } : undefined,
            signal: controller.signal,
          },
        );
        if (!res.ok) return; // 401 / 404 / 429 / 5xx 一律当「这一轮没数到」,见上面
        const result = parseHazardListEnvelope(await res.text());
        // `total` 是**过了 scope 筛子之后台账里的总数**,不是这一屏的行数 ——
        // 后端有 50 行的上限,拿 `hazards.length` 数的话超过 50 就永远显示 50。
        if (!cancelled && result.ok) setPendingCount(result.total);
      } catch {
        // 网络异常、后端没起、关面板/切工地时的 abort —— 全部静默,下一轮再说
      } finally {
        // 必须放 finally:早退(!res.ok)和异常都得把闸放开,否则一次失败之后
        // 这个循环再也不发第二发,而界面上一点征兆都没有
        inFlight = false;
      }
    };

    void count(); // 先来一次:挂载时、以及面板刚关上那一下
    const timer = setInterval(() => void count(), PENDING_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
      controller.abort();
    };
  }, [apiBase, projectFilter, open]);

  const close = useCallback(() => setOpen(false), []);

  // 拍完照那張隱患卡上的「去監理處置 →」(hazard-result-card.tsx,2026-09-18 FINDING-001):
  // 它在對話流裏、這顆按鈕在動作條裏,中間隔着上游的 thread-index —— 用一個 DOM 事件
  // 約定(supervision-lib.OPEN_SUPERVISION_EVENT),不把 open 狀態提三層。
  useEffect(() => {
    const onOpen = () => setOpen(true);
    window.addEventListener(OPEN_SUPERVISION_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_SUPERVISION_EVENT, onOpen);
  }, []);

  const hasPending = pendingCount !== null && pendingCount > 0;
  const badgeText = hasPending && pendingCount > BADGE_MAX ? `${BADGE_MAX}+` : `${pendingCount}`;

  return (
    <>
      {/* ⚠️ 触摸目标三件套,与 CheckinEntry 一字不差,理由也一样(那边记着 2026-08-15
          在 390×844 的实测:动作条被 flex 挤压,「打卡」只剩 42px 宽、两个字竖排):
          · whitespace-nowrap —— 不许竖排(治本,与被挤多窄无关);
          · shrink-0 —— 不被兄弟元素压;
          · min-h-11 / min-w-11(44px)—— 触摸目标下限,sm: 之后归零,桌面观感不变。
          按钮上只放两个字也是为这条:这一行现在有四件东西了,每多一个字都是宽度。 */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex pointer-coarse:min-h-11 pointer-coarse:min-w-11 shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap"
        aria-label={
          hasPending ? `監理確認與處置,有 ${badgeText} 條待確認` : "監理確認與處置"
        }
      >
        <ClipboardCheck className="size-5 text-gray-600" />
        <span className="text-sm text-gray-600">隱患</span>
        {hasPending && (
          // 徽章走**行内**而不是 absolute 定位的角标:这一行是 flex-wrap 的,
          // 绝对定位的角标会被换行/挤压切掉一半,而那时它看着像个装饰,没人会去数。
          // 行内的坏处是按钮会变宽一点 —— 但宽度是可预期的(上面 BADGE_MAX 封了顶)。
          <span
            aria-hidden="true"
            className="inline-flex min-w-5 items-center justify-center rounded-full bg-amber-500 px-1.5 py-0.5 text-[11px] font-bold text-white tabular-nums"
          >
            {badgeText}
          </span>
        )}
      </button>
      {open && (
        // 徽章上写着「3」的时候,点开就该先看到那 3 条 —— 所以有待确认时开在
        // 「待确认」档,没有时才用面板自己的缺省(「在办」)。
        // 不这么做的话,点开落在「在办」那一屏:待确认的确实也在里面(在办 =
        // 没销项也没上报的全部),但混在一堆已签发/待复查里,人得自己找 ——
        // 而他刚刚是冲着那个数字点进来的。四颗筛子就在面板顶上,想看别的一点就换。
        <SupervisionPanel
          artifactBase={ARTIFACT_BASE}
          initialScope={hasPending ? HAZARD_SCOPE_PENDING : undefined}
          onClose={close}
        />
      )}
    </>
  );
}
