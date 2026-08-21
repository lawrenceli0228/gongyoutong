/**
 * 监理确认与处置界面(W9 · S6 泳道)—— **本仓自有新文件**,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/supervision.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 它要解决的问题 ────────────────────────────────────────────────────
 * safety 看完照片会把违规项登记成 **pending 隐患**(D17:自动写入,但不算整改率、
 * 不进超期清单、不能被升级)。**必须有人确认**,它才进正式流程 —— 而在这个界面
 * 出现之前,确认这一步在系统里没有任何入口:隐患躺在库里,谁也看不见。
 *
 * 后面那六种文书(通知单/暂停令/致建设单位报告/复工令/监理报告)全部落进 artifacts,
 * 而 tool-calls.tsx 此前只认 `render_inspection_report` 一个工具 —— 于是演示时说
 * 「暂停令已签发」,界面上只有一行灰色折叠:**点不开、下不到,而且不会报错**。
 *
 * ── 为什么写入不走对话链 ──────────────────────────────────────────────
 * 签发《监理通知单》《工程暂停令》是**法律行为**,不能由概率性系统单方面触发
 * (方案 §5.1)。supervision Agent 只能查、只能建议;所有改状态 / 出文书的动作
 * 都是本面板直接打 HTTP 端点 —— 与 W7 打卡「写入不走 LLM」同构,这次理由更硬。
 * **请求/响应契约的唯一真相是 `backend/src/gyt/supervision_api.py` 的模块 docstring**,
 * 本文件一个字段都不许偏。
 *
 * ── 分层 ──────────────────────────────────────────────────────────────
 * 所有可测逻辑都在 @/lib/supervision-lib(scripts/frontend-overrides/supervision-lib.ts,
 * 纯 TS 零依赖,scripts/frontend-tests/ 的 vitest 直接测它)。本文件只留「碰浏览器」
 * 的部分:portal、fetch、表单状态。**判断哪条隐患现在能做什么,一律问
 * `availableActions`,别在 JSX 里就地写 `status === "open" && …`** —— 那些判据要
 * 逐条对齐服务端四道闸,散进 JSX 就没法测、也没法一眼看出哪条漂了。
 *
 * ── 挂在哪(W10 改过一次,原委必须留着)────────────────────────────────
 * **今天的入口是输入框动作条上的常驻按钮**(`supervision-entry.tsx` 的
 * `SupervisionEntry`,挂在 thread-index.tsx 的 `<CheckinEntry />` 旁边),
 * 数据源是两个 GET 端点,**一个字都不经过聊天流**。
 *
 * W9 当初挂的是另一处:tool-calls.tsx 里那张「待确认隐患」卡片(理由写得很好听 ——
 * 隐患在哪张照片上发现的就在那条消息底下处置,上下文就在眼前)。**那条路一次都没通过。**
 * 根因不在这两个文件里:supervisor 的 `output_mode="last_message"` 会把子 Agent 的
 * 工具返回整个丢掉,而那张卡的判据是「工具返回里有 hazards 数组」——
 * 于是它从来没渲染出来过,面板自然一次没打开过(实测三条路全堵:线程状态 0、
 * 历史检查点 0、`useStream` 没订阅 subgraphs。全过程在 `docs/W10_界面取不到工具返回_方案.md`)。
 *
 * 教训不是「卡片挂错地方」,是**操作台不该建在聊天产物上**:聊天流里有什么、
 * 留不留得住,是编排层说了算的事,而处置面板要能在刷新之后照样打开。
 * 修法照 W7 打卡面板的先例(`checkin.tsx`):常驻按钮 + 直连 HTTP。
 * `HazardIntakeCard` 留着没删,但它现在**不可达** —— 那条注释在它头上,别当它是活的。
 *
 * ── 复查照片:从「每行一个」改成「面板一个」(2026-08-16)────────────────
 * 真人反馈原话:**「上传照片的地方太多了,一般都是在一张照片里」**。
 * 而这与事实相反 —— 隐患本来就是从**同一张**照片里认出来的(`analyze_site_photo`
 * 一张图登记多条),整改后监理也只拍一张。改之前每条可复查的隐患各有一个大虚线
 * 上传框,三条就是三个框,把面板撑得老长,人手上却只有一张照片。
 *
 * 今天的形状:**照片一格在面板顶上、多条隐患共用;结论(合格/不合格)仍然一条一条下。**
 * 「共用会不会让批量放行变容易」这个取舍的完整推演在 supervision-lib.ts
 * 那一节的头注里(结论:不构成新风险,原先那道摩擦买到的是麻烦不是安全)——
 * 看见「共用照片」先去读那段,别当成图省事。
 *
 * 落在本文件里的三件:
 *   ① `SharedReinspectPhotoField` —— 面板级那一格,三态照旧(在传 / 传失败 / 传好了);
 *   ② `RowPhotoChoice` —— 行里只剩「这一条用的是哪张」的标注 + 折叠的手填编号逃生口;
 *   ③ 复查登记成功后**不清照片**,改为记下「这一行用过哪个编号」(`usedPhotos`),
 *      判不合格的那一条要再复查必须换一张新的 —— 推演在 `reinspectBlocker` 头注。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryState } from "nuqs";
import {
  AlertTriangle,
  Camera,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  ExternalLink,
  FileText,
  Gavel,
  ImageOff,
  LoaderCircle,
  RefreshCcw,
  ShieldAlert,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { getApiKey } from "@/lib/api-key";
import { useCurrentProjectId } from "./ProjectUploadPanel";
import {
  ACTION_LABEL,
  ACTION_ENDPOINT,
  actionBody,
  actionNeedsConfirm,
  actionNeedsDuePhrase,
  actionNeedsPhoto,
  availableActions,
  currentGrade,
  confirmBatchPrompt,
  confirmBody,
  confirmPrompt,
  SIGNER_NAME_STORAGE_KEY,
  describeDocuments,
  describePhotoFile,
  describeSharedPhotoCoverage,
  DisposalAction,
  documentUrl,
  docTypeZh,
  effectivePhotoId,
  evidenceRows,
  formatHkMoment,
  GRADE_NORMAL,
  GRADE_SEVERE,
  hazardsUsingSharedPhoto,
  isPhotoId,
  HAZARD_SCOPE_ACTIVE,
  HAZARD_SCOPE_PENDING,
  HAZARD_SCOPES,
  HazardBrief,
  HazardDetail,
  HazardListResult,
  HazardScope,
  hazardDetailUrl,
  hazardListUrl,
  hazardStatusZh,
  isDownloadableDoc,
  NETWORK_ERROR_STATUS,
  normalizeError,
  parseActionEnvelope,
  parseConfirmEnvelope,
  parseHazardDetailEnvelope,
  parseHazardListEnvelope,
  parsePhotoEnvelope,
  patchHazard,
  pendingHazards,
  PHOTO_MESSAGES,
  photoSizeProblem,
  photoSourceOf,
  photoTypeProblem,
  reinspectableHazards,
  reinspectBlocker,
  REJECTED_STATUS,
  removeHazard,
  SharedPhotoState,
  SHARED_PHOTO_MESSAGES,
  SupervisionContractError,
  SupervisionDoc,
  SupervisionEndpoint,
  SUPERVISION_MESSAGES,
  supervisionPhotoUrl,
  supervisionUrl,
  toggleSelected,
} from "@/lib/supervision-lib";
/**
 * 界面恒繁體(负责人 2026-08-18 定案:中建国际在港,现场语言是繁體)。
 *
 * 本文件里**静态文案已经在源码里写成繁體**,这三件只管两种源码转不了的字:
 *   ① **后端来的显示文本** —— `status_display` / `due_display` / `result_display` /
 *      `user_msg` / `item` / `severity` / `grade`。它们运行时才存在;
 *   ② **本地那几张兜底表** —— `HAZARD_STATUS_ZH` / `DOC_TYPE_ZH` / `HAZARD_SCOPES` /
 *      `GRADE_*`。那些表的**值必须留简体**(送后端 / 跟后端返回值比 / 当对象键,
 *      逐条理由在 scripts/frontend-tests/hant-keep-hans.mjs),所以只能在**上屏那一处**转。
 *
 * 🔴 ①②必须走**同一道**转换。状态徽章那一行是 `status_display || hazardStatusZh(status)`
 * —— 两条路供同一颗徽章的字,只有都过这一道,工友才不会看见「点一下确认、
 * 徽章的字忽然换了一种写法」。
 *
 * 🔴 字典是懒加载的 438 KB,**只在面板/组件里用**:面板点开才挂载,字典跟着面板走。
 * 铺到常驻主界面 = 每个用户首屏都拉,包括从不开面板的简体工友。
 */
// ⚠️ 转的只有「枚举 / 生成值」这一类(状态名、级别、文书类型、筛子词表)。
// **后端 Envelope 的 `user_msg` 一律不转**(W12 复审定案)—— 它内插了用户数据
// (工友姓名、项目名、文件名、隐患描述),整句转换在原理上分不出哪一半是系统写的字,
// 而分不出时默认转是危险的那一侧。守卫在 scripts/frontend-tests/hant-ui-strings.test.ts。
import { ensureHantConverter, hantSync, useHantUI, useHantUIAll } from "@/lib/hant-convert";

/**
 * 举手到可确认之间的静默期。**与 thread-history.tsx 的 ARM_QUIET_MS 同一个数、
 * 同一条理由**(那边的头注写全了:300ms 是人有意识看清一句话的下限,系统双击
 * 判定通常在 500ms 内,400 卡在中间)。这里挡的东西更贵:双击穿透一次
 * = 平白停掉一片人的工,或者往建设主管部门报一份指控。
 */
const ARM_QUIET_MS = 400;

/** 级别徽章配色。两个取值来自 db/hazards.py 的 GRADES,别自由发挥。 */
const GRADE_CHIP: Record<string, string> = {
  [GRADE_SEVERE]: "bg-red-50 text-red-700 ring-red-200",
  [GRADE_NORMAL]: "bg-sky-50 text-sky-700 ring-sky-200",
};

/**
 * 定级那两颗按钮的取值,顺序就是屏幕上的顺序(轻的在左)。
 *
 * 提到模块级是为了**引用稳定** —— `useHantUIAll` 内部 `useMemo` 的依赖里有这个数组,
 * 在渲染里写 `[GRADE_NORMAL, GRADE_SEVERE]` 的话每次渲染都是新引用、memo 每次作废。
 *
 * 🔴 **数组里放的必须是简体原值**:它同时要送后端(`grade` 字段)、要跟后端返回值比
 * (`已判级别 === grade`)、还要当 `GRADE_CHIP` 的键。上屏的繁體另取一份
 * (`useHantUIAll(GRADE_CHOICES)`),两者按下标配对 —— 别合并成一张「繁體数组」。
 */
const GRADE_CHOICES = [GRADE_NORMAL, GRADE_SEVERE] as const;

/**
 * 现场判定档次的徽章配色。四个取值来自 agents/safety/severity.py。
 * **与 tool-calls.tsx 的 SEVERITY_CHIP 是同一张表**(那边给识别回执的卡片用),
 * 配色对不上的话同一条隐患在两处颜色不一样,看着像两条不同的东西。
 */
const SEVERITY_CHIP: Record<string, string> = {
  重大: "bg-red-50 text-red-700 ring-red-200",
  较大: "bg-amber-50 text-amber-700 ring-amber-200",
  一般: "bg-sky-50 text-sky-700 ring-sky-200",
  待定级: "bg-gray-100 text-gray-500 ring-gray-200",
};

/**
 * 二次确认条上那颗红按钮的字。**与 `ACTION_LABEL` 不是一回事** ——
 * 那份是动作按钮上的字(「签发暂停令(三份)」),这份是「你已经看完后果、
 * 现在真的要下手」那一下。
 *
 * 🔴 三个动作不许共用一句「确认签发」:`reject` 是**把那一行从台账里删掉**,
 * 在删除的确认条上写「确认签发」等于告诉人「这一下会出一份文书」——
 * 而真实后果正好相反。W10 接上 reject 时它一度就是这么写的。
 */
const CONFIRM_BUTTON_LABEL: Readonly<Record<string, string>> = Object.freeze({
  suspend: "確認簽發",
  escalate: "確認簽發",
  reject: "確認刪掉",
});

/**
 * 整改期限那一格的例句。🔴 **这两条必须留简体,界面繁體化时是唯一的例外。**
 *
 * 它们不是「给人看的字」,是**让人照抄进输入框的原话** —— 而那格原话原样送后端,
 * 由 `agents/schedule/dates.py` 解析,那边的词表和正则**从头到尾只认简体**
 * (`_NEXT_WEEKS_RE` 是 `(下下周|下周)`、`_DAYS_RE` 是 `(.+)天[后内]`、
 * `_BARE_WEEKDAY_RE` 收的是 `周|星期|礼拜`),后端也**没有任何繁→简归一化**。
 *
 * ✅ **2026-08-18 后端补上了繁→简归一化,这两条已经可以是繁體了。**
 * `dates.py` 的 `_strip_noise()` 第一行做一次折叠(週→周 / 後→后 / 禮→礼 /
 * 這→这 / 個→个 / 號→号 / 內→内 / 兩→两),所以「下週三」「後天」「9月1號」
 * 现在都解析得出来,而且与对应简体写法**结果完全一致**(测试是
 * `parse(繁) == parse(简)` 那种写法,不写死日期)。
 *
 * 🔴 **但这条约束本身没有消失,只是判据变了。** 例句仍然是「让人照抄进输入框、
 * 抄完原样送后端」的原话 —— 它不是普通标签。往这两个常量里加新写法之前,
 * 必须先确认 `agents/schedule/dates.py` 认得那个写法:
 *
 *     cd backend && .venv/bin/python -c "
 *     import sys; sys.path.insert(0,'src')
 *     from datetime import date
 *     from gyt.agents.schedule.dates import parse_due
 *     print(parse_due('你要加的写法', today=date.today()))"
 *
 * 已知**仍然不认**的:「下星期三」「下禮拜三」—— 那是**词表缺口不是简繁缺口**
 * (简体的「下星期三」一样被拒:`_NEXT_WEEKS_RE` 只收「周」,不收「星期|礼拜」)。
 * 别往例句里写它们。
 *
 * 「3天后」的「后」保持不变是对的:opencc 把「天后」当成天后娘娘那个词、本来就不转,
 * 而后端两种都认,所以这一项转不转都行。
 */
const DUE_PHRASE_EXAMPLES = "明天 / 3天后 / 下週三 / 月底";
/** 同上 —— 输入框 placeholder 里那个单独的例子。 */
const DUE_PHRASE_SAMPLE = "下週三";

/** 状态徽章配色:能动的暖色、收尾的灰、出事的红。认不出的状态退回灰色,不炸。 */
const STATUS_CHIP: Record<string, string> = {
  pending: "bg-amber-50 text-amber-700 ring-amber-200",
  open: "bg-blue-50 text-blue-700 ring-blue-200",
  notified: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  suspended: "bg-red-50 text-red-700 ring-red-200",
  reinspect_failed: "bg-orange-50 text-orange-700 ring-orange-200",
  resuming: "bg-teal-50 text-teal-700 ring-teal-200",
  closed: "bg-gray-100 text-gray-500 ring-gray-200",
  escalated: "bg-purple-50 text-purple-700 ring-purple-200",
};

/**
 * 后端地址的解析**照抄 checkin.tsx 的 useApiBase**(它又照抄 Stream.tsx:147-180):
 * URL 参数 apiUrl 优先,退 NEXT_PUBLIC_API_URL。本机 dev 是 http://localhost:2024;
 * 公网是 `${GYT_PUBLIC_ORIGIN}/api` —— Caddy 的 handle_path /api/* 剥前缀转发,
 * 同源所以登录 Cookie 自动带上。别发明第三种拿地址的路子。
 *
 * **export 是给 supervision-entry.tsx 用的**(W10):那颗常驻按钮自己也要打
 * `GET /supervision/hazards` 取待确认条数。宁可从这里 import,也不要在那边再抄
 * 一份 —— 全仓已经有两份了(本文件 + checkin.tsx),第三份迟早在某次改动里漏掉。
 */
export function useApiBase(): string {
  const envApiUrl = process.env.NEXT_PUBLIC_API_URL;
  const [apiUrlParam] = useQueryState("apiUrl", { defaultValue: envApiUrl || "" });
  return apiUrlParam || envApiUrl || "";
}

/**
 * 顶栏那个「当前工地」→ `hazardListUrl` 的 `projectId` 入参。
 *
 * 🔴 **这层转换不能省,而且方向只有一个是对的。** 两边的空串意思正好相反:
 *   · `ArchiveProvider` 的 `projectId` —— 空串 = **没选工地**(顶栏那颗按钮上写着
 *     「全部」,`setProjectId("")` 就是选它);
 *   · `hazardListUrl` 的 `projectId` —— 空串 = **只看未归属那一批**(D6),
 *     不筛工地要传 `null`(那样 query 里压根没有这个键)。
 * 直接把前者喂给后者,屏幕上就只剩没人认领的那几条,而且**没有任何报错** ——
 * 监理会以为台账里就这么点东西。反过来把 null 当成 "" 也一样静默。
 * 转换写在这里、只写一次,两个调用方(面板、入口徽章)必然同口径。
 *
 * 顺带:界面上**没有**「只看未归属」这颗按钮 —— 那一批在「全部工地」这一档里
 * 本来就看得见,再给一颗按钮只是多一个能选错的地方。它们会不会被忽略,靠的是
 * 面板里那句 `unassigned` 提示(见 `UnassignedNotice`),不是靠多一个筛子。
 *
 * ⚠️ 依赖 `<ArchiveProvider>`(`useArchive` 在 provider 外会抛)。今天两个调用方
 * 都在 thread-index.tsx 那棵树里,天然满足;要在别处挂面板,先把 provider 带上。
 */
export function useProjectFilter(): string | null {
  const currentProjectId = useCurrentProjectId();
  return currentProjectId || null;
}

type CallOutcome =
  | { ok: true; text: string }
  | { ok: false; message: string };

/**
 * 打一次监理端点。**所有写入都从这一个出口走** —— 散着写 fetch 的下场是
 * 某一处漏了 x-api-key(公网上表现为每次都 401)或者漏了错误归一化
 * (于是屏幕上出现一行 "Failed to fetch",而这是给工地上的人看的界面)。
 */
async function callSupervision(
  apiBase: string,
  endpoint: SupervisionEndpoint,
  body: Record<string, unknown>,
): Promise<CallOutcome> {
  const apiKey = getApiKey();
  let res: Response;
  try {
    res = await fetch(supervisionUrl(apiBase, endpoint), {
      method: "POST",
      headers: {
        "content-type": "application/json",
        // 令牌与 Stream.tsx 同一来源(getApiKey():localStorage 优先、构建期注入兜底)。
        // 没有令牌就不发这个头 —— 本机未开鉴权时后端也不看它。
        ...(apiKey ? { "x-api-key": apiKey } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, message: normalizeError(NETWORK_ERROR_STATUS, null, "").message };
  }
  const text = await res.text();
  if (!res.ok) {
    return {
      ok: false,
      message: normalizeError(res.status, res.headers.get("content-type"), text).message,
    };
  }
  return { ok: true, text };
}

/** 小徽章。ring-inset 让它在浅底上也有边界,不至于糊成一片。 */
function Chip({ tone, children }: { tone: string; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[12px] font-medium ring-1 ring-inset ${tone}`}
    >
      {children}
    </span>
  );
}

/**
 * 文书下载卡 —— 一份一张,**按 `documents` 数组渲染 N 张**(契约冻结形状的用处:
 * 「N 张卡」与「N 份文书」天然对齐,三份里少出一份当场看得出来)。
 *
 * `artifactBase` 由调用方传进来,**本文件一个 `process.env` 都不读**:
 * `NEXT_PUBLIC_ARTIFACT_BASE` 那条链(CLAUDE.md 同源清单)**现在有四个读者** ——
 * human.tsx(历史照片/图纸)、tool-calls.tsx(巡检记录卡)、checkin.tsx(打卡凭证图),
 * 外加 W10 新增的 supervision-entry.tsx(常驻入口这条链路的根)。
 * 这条链**断在任何一环都不报错**(站点照开、提问照答,只是文书点了没反应;
 * 而 https 页面拉 http 资源属于 mixed content,浏览器连请求都不发)——
 * 少一个读者就少一处能断的地方,所以规矩不变:**读只在链路的根上读一次,
 * 面板与卡片一律收 props**。这两条链路各自的根一个是 tool-calls.tsx、
 * 一个是 supervision-entry.tsx,不要再多第三个。
 *
 * ⚠️ **它有两个调用方,而繁體字典跟着调用方走**(W12 界面繁體化):
 *   · 面板里那两处(证据链 / 「本次出的文书」)—— 点开才挂载,字典跟着面板,这是想要的;
 *   · tool-calls.tsx 那处 —— 长在**聊天流**里。它今天不可达(supervisor 的
 *     `output_mode="last_message"` 把工具返回丢了),所以不构成首屏开销;
 *     哪天那条路通了,`IssuedDocCard` 里的 `useHantUI` 会在聊天流里触发一次
 *     438 KB 的懒加载 —— 那时要重新想一遍这张卡该不该转,别默认照旧。
 */
export function SupervisionDocCards({
  documents,
  artifactBase,
}: {
  documents: readonly SupervisionDoc[];
  artifactBase: string;
}) {
  if (documents.length === 0) return null;
  const 有文书 = documents.some(isDownloadableDoc);
  return (
    <div className="flex flex-col gap-1.5">
      {/* D15 的定位:AI 出稿、总监理工程师签字生效。
          这句话在**每份文书正文里**也印着(docgen 的 SUPERVISION_DISCLAIMER,
          那儿逐份印是对的 —— 每份 docx 都会被单独打印、单独归档)。
          界面上仍要说一遍,是因为「已签发」三个字在屏幕上太容易被读成「已经生效」。

          🔴 但**只说一次,不逐份说**(2026-08-17 改):真人看到证据链里三份文书
          各印一遍同一句话,问的是「这个都需要签字吗」—— 重复本身制造了
          「这是三件不同的事」的错觉。这句话对清单里每一份都成立,说一次就够。

          只在真有文书时才说:复查记录不是文书、不用签字,一堆复查记录上面顶一句
          「签字盖章」是凭空制造一个不存在的手续。 */}
      {有文书 && (
        <div className="text-[12px] text-gray-500">
          下面這些都是出稿,要總監理工程師簽字蓋章後才是正式文件。
        </div>
      )}
      {/* 顺序原样照抄后端给的(= 挂进台账的先后),**不按类型分组** ——
          理由整段在 supervision-lib 的 `evidenceRows` 头注:证据链的意义就是这个先后。
          `evidenceRows` 顺手把「第几次复查」数出来,那是唯一需要跨行才算得出的东西,
          所以它在 lib 里(有测试),这一层只管画。 */}
      {evidenceRows(documents).map(({ doc, reinspectionNo }) =>
        reinspectionNo === null ? (
          <IssuedDocCard key={doc.doc_no} doc={doc} artifactBase={artifactBase} />
        ) : (
          <ReinspectionLine
            key={doc.doc_no}
            doc={doc}
            ordinal={reinspectionNo}
            artifactBase={artifactBase}
          />
        ),
      )}
      {/* 只在真有东西可下的时候说这句 —— 一堆复查记录底下挂一行「打不开?」
          等于凭空制造一个不存在的问题。 */}
      {有文书 && (
        <div className="text-[11px] text-gray-400">
          打不開?本機要先在倉庫根執行 <code className="font-mono">make serve-artifacts</code>
        </div>
      )}
    </div>
  );
}

/**
 * 一份**文书**的卡片(通知单 / 暂停令 / 复工令 / 致建设单位报告 / 监理报告)。
 *
 * 从 `SupervisionDocCards` 里拆出来的(W10 详情):证据链要按行挑模板
 *(文书一种、复查记录另一种),而拆之前这段 markup 长在那个 map 里,
 * 想复用只能整个数组传进去 —— 那样每行底下都会跟一句「打不开?make serve-artifacts」。
 * 拆出来之后两个调用方共用同一张卡:**「本次出的文书」与「证据链」里的同一份文书
 * 必须长得一模一样**,不然人会以为是两份不同的东西。
 */
function IssuedDocCard({ doc, artifactBase }: { doc: SupervisionDoc; artifactBase: string }) {
  const url = documentUrl(doc, artifactBase);
  const issuedAt = formatHkMoment(doc.created_at ?? "");
  /**
   * 🔴 **只转上屏这一份,`doc.filename` 一个字都不许碰**(见下面 title 那处)。
   * `docTypeZh` 那张表同时被拿去**拼文件名兜底**(`${docTypeZh(t)}_${no}.docx`),
   * 而后端 `_filename()` 拼的是简体那份 —— 表本身留简体、上屏这一处转,
   * 是这两个用途唯一能同时满足的形状。
   */
  const docTypeText = useHantUI(docTypeZh(doc.doc_type));
  /** 违规项名(「高空作业未系安全带」)是后端来的字,8 类受控词里有 5 类简繁不同形。 */
  const hazardItemText = useHantUI(doc.hazard_item);
  return (
    <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50/40 px-3 py-2.5">
      <FileText className="mt-0.5 h-5 w-5 shrink-0 text-amber-700" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="font-medium text-gray-900">{docTypeText}</span>
          {/* 编号 select-all:监理要在电话里报它、上报时按它排证据链,
              一点就能整串复制(同 checkin.tsx 凭证卡片的做法)。 */}
          <span className="font-mono text-[13px] break-all text-gray-600 select-all">
            {doc.doc_no}
          </span>
          {/* 签发时刻只有详情端点会给(动作回执的四键里没有 created_at)——
              **拿不到就整格不出现**,不摆一个空的「签发时间」。
              时刻一律走 formatHkMoment:它禁止用 new Date 换算,理由在那个函数头注
              (浏览器时区一偏,追责用的时刻就差八小时)。 */}
          {issuedAt && (
            <span className="text-[11px] text-gray-400 tabular-nums">簽發 {issuedAt}</span>
          )}
        </div>
        {/* 🔴 这份是**给哪条隐患**的。
            「本次出的文书」是跨隐患的汇总:连着给三条签复工令,那里就是三张
            一模一样的「工程复工令」,只有编号不同 —— 而编号对人不说明任何事。
            2026-08-17 真人反馈原话:「意义不明,缺加上什么的复工令」。

            证据链那一侧整块就挂在某一条隐患下面、上下文自明,所以后端不给这个字段,
            由 runAction 在收到回执时补(见那儿的注释)。**没有就不显示** ——
            两个调用方共用同一张卡,证据链里多印一行「针对:xxx」是废话。

            念的是**违规项**而不是隐患编号:人认的是「高空作业未系安全带」,
            编号是拿来对账的,已经在上面那一行了。编号也带上,因为汇总区里
            可能有同一个违规项的两条隐患(不同工位),光看名字分不开。 */}
        {doc.hazard_item && (
          <div className="mt-0.5 text-[12px] text-gray-600">
            針對:{hazardItemText}
            {doc.hazard_no && (
              <span className="ml-1.5 font-mono text-[11px] text-gray-400 select-all">
                {doc.hazard_no}
              </span>
            )}
          </div>
        )}
        {/* ⚠️ 这里原来每张卡各印一遍「这份是出稿,要总监理工程师签字盖章后才是正式
            文件。」——2026-08-17 真人反馈「这个都需要签字吗」时数了一下:证据链里
            三份文书 = 同一句话连着出现三遍,读起来像三件不同的事各要一次签字。
            现在**整份清单上方说一次**(见 SupervisionDocCards 里那条),
            那句话对每一份都成立,不必逐份复述。
            🔴 别搬回来。也别删掉上面那条 —— 「已签发」三个字在屏幕上太容易被读成
            「已经生效」,而这几份签字之前不得据以停工、复工或对外发出。 */}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {url ? (
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              // title 给文件名:落到磁盘上叫什么,点之前就知道
              // (取件端点按编号给文件,浏览器另存时用的就是这个名字)。
              // `?? undefined`:`filename` 可以是 null(复查记录行),而 title 只收
              // string | undefined。走到这个分支时 url 非空、也就一定是文书行、
              // 文件名一定有 —— 但类型上证不了,所以在这儿收口而不是在上面断言。
              //
              // 🔴 **这一处绝不许过 useHantUI。** 它不是给人读的标签,是**盘上那份
              // docx 真实的名字**:后端 `_filename()` 拼的是简体,这里显示繁體的话,
              // 监理按 title 上的名字去归档目录里找那份文件会找不到,而卡片、
              // 下载、控制台一切正常(取件走 artifact_id,不走名字)——
              // 没有任何一处会报错,只有对账那天才发现。
              title={doc.filename ?? undefined}
              // pointer-coarse:戴手套的手指按不中 28px 的链接(同 checkin.tsx 那颗关闭按钮)
              className="inline-flex items-center gap-1.5 rounded-md bg-amber-600 px-2.5 py-1 text-[13px] font-medium text-white transition-colors hover:bg-amber-700 pointer-coarse:min-h-11 pointer-coarse:px-4"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              打開文書
            </a>
          ) : (
            // artifact_id 为空 = 文书签发了(编号已进台账)但取不了件。
            // **绝不渲染死链接**:点了没反应和「文件真没了」在界面上分不开
            // (同 checkin-lib.receiptImageUrl 那条规矩)。
            <span className="text-[12px] text-red-600">
              這份文書沒有取件編號,下不了 —— 編號已進台賬,找管理員按編號取。
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * 证据链里的一条**复查记录**。
 *
 * 🔴 它跟文书**不是一类东西**,所以长得不一样:复查不出文件(`DocKind` 里根本没有
 * 这一档),它留下的是「第几次、什么时候、拿哪张照片、判了合格还是不合格」。
 * 长成一张灰卡再挂一句「下不了」的话,真正出事的那种(文书签了却取不了件)
 * 会被这种噪声淹掉 —— 而那一种恰恰是要人去查的。
 *
 * 🔴 **`photo_id` 是「这一次复查拍的那张」,不是隐患首次发现那张。**
 * 后端 `_hazard_doc_payload` 那条红线写得很直白:前者在 `hazard_docs.photo_id`,
 * 后者在 `hazards.photo_id`、压根不在这个数组里。所以屏幕上必须写全「复查照片」——
 * 只写「照片」的话,事后追责时没人分得清手上这张是整改前还是整改后的,
 * 而「复查必须挂照片」这条红线(方案 §5.2)的全部意义就是分清这两张。
 */
function ReinspectionLine({
  doc,
  ordinal,
  artifactBase,
}: {
  doc: SupervisionDoc;
  /** 第几次复查(从 1 起),由 `evidenceRows` 数出来。 */
  ordinal: number;
  artifactBase: string;
}) {
  /**
   * 结论的中文一律取 `result_display`(后端 `scoping.result_zh` 那张全仓唯一的表)。
   *
   * 🔴 **拿不到就说「没记结论」,绝不许退回 `doc.result`**:那是 `pass` / `fail`
   * 两个英文枚举值,本仓明令不许上屏;更要命的是**也不许猜成「不合格」** ——
   * `hazard_docs.result` 的 CHECK 是「可以为 NULL」,历史行/补录行真的可能没结论,
   * 念成不合格 = 在没有结论的情况下对外声称施工方复查没过(后端 tools 那边修过同款)。
   */
  // 后端那份过一道繁體(`scoping.result_zh` 给的是简体「合格 / 不合格」);
  // 兜底那句源码里已经是繁體,所以只把后端来的那半边喂进去。
  const verdict = useHantUI(doc.result_display?.trim()) || "沒記結論";
  /**
   * 颜色只看机器值 `result`,**文字只看 `result_display`**。
   * 拿英文值挑配色不算「上屏」(屏幕上出现的仍是中文),而拿中文去比配色
   * 等于把词表抄成了第三份 —— 后端哪天把「合格」改成「已合格」,颜色就悄悄退回灰的。
   */
  const tone =
    doc.result === "pass"
      ? "border-emerald-200 bg-emerald-50/50"
      : doc.result === "fail"
        ? "border-orange-200 bg-orange-50/50"
        : "border-gray-200 bg-gray-50";
  const icon =
    doc.result === "pass" ? (
      <Check className="mt-0.5 size-4 shrink-0 text-emerald-600" />
    ) : (
      <ClipboardCheck className="mt-0.5 size-4 shrink-0 text-gray-400" />
    );
  const checkedAt = formatHkMoment(doc.created_at ?? "");
  /**
   * 复查照片走的是**同一个按编号取件的端点**(`/by-id/<32位编号>`,human.tsx 的历史
   * 照片也走它),所以直接复用 `documentUrl` —— 它顺带守着那条「空就返回 null、
   * 绝不渲染死链接」的规矩,自己拼一遍就得把那道守卫也抄一遍。
   */
  const photoUrl = documentUrl({ artifact_id: doc.photo_id ?? null }, artifactBase);

  return (
    <div className={`flex items-start gap-2.5 rounded-xl border px-3 py-2.5 ${tone}`}>
      {icon}
      {/* 🔴 缩略图。2026-08-17 真人反馈「没有图片还需要图片预览」——
          在这之前这一行只有一串 32 位十六进制 + 一颗「打开」,而**那串编号对人
          不说明任何事**,跟上一轮修掉的「照片不能是编号意义不明」是同一件事:
          那次修的是**上传**那半边(拍完就出缩略图),证据链这半边漏了。

          这里为什么可以直接拿产物出口取件、而上传那一格坚持用本地 objectURL:
          上传那三态里前两态**压根还没有编号**,取不了件;而这一行的照片是台账里
          已经挂住的一张,编号一定有。
          ⚠️ 代价是它吃 ARTIFACT_BASE 那条链:链断了(本机没起
          `make serve-artifacts`、或者公网 mixed content)这儿会变成「预览不出来」
          的占位框 —— `PhotoThumb` 的 onError 兜着,不会留一个破图。
          旁边那颗「打开」与编号本身都留着:预览是**多一道**,不是替代 ——
          追责时要报的是编号,要看原图大小的是那颗按钮。 */}
      {photoUrl && <PhotoThumb src={photoUrl} alt={`第 ${ordinal} 次複查的照片`} />}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[13px]">
          <span className="font-medium text-gray-900">第 {ordinal} 次複查</span>
          <span className="text-gray-700">{verdict}</span>
          {checkedAt && (
            <span className="text-[11px] text-gray-400 tabular-nums">{checkedAt}</span>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
          {doc.photo_id ? (
            <>
              <Camera className="size-3.5 shrink-0 text-gray-400" />
              {/* 「复查照片」四个字不许简写成「照片」,理由见本组件头注。 */}
              <span className="text-gray-500">複查照片</span>
              <span className="font-mono text-[11px] break-all text-gray-500 select-all">
                {doc.photo_id}
              </span>
              {photoUrl && (
                <a
                  href={photoUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-2 py-0.5 text-[12px] text-gray-700 transition-colors hover:bg-gray-50 pointer-coarse:min-h-11 pointer-coarse:px-3"
                >
                  <ExternalLink className="size-3" />
                  打開
                </a>
              )}
            </>
          ) : (
            // 复查照片是必填的(`after_photo_id` 在端点那一侧就拦),所以这一格空着
            // **是台账层面的异常**,不是「这次没拍」。说出来 —— 证据链缺一环,
            // 而缺的正是「整改后长什么样」那一张,事后没法举证。
            <span className="text-red-600">
              這次複查沒留照片編號 —— 證據鏈缺一環,找管理員查台賬。
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/** 举手确认条 —— 顶掉原来那颗按钮,不是 window.confirm。
 *
 * **不用 window.confirm 的理由原样见 thread-history.tsx 的 DeleteConfirmBar 头注**:
 * 它阻塞主线程、文案被浏览器套壳,而且用户勾了「阻止此页面再次弹出对话框」之后
 * **会被静默跳过**(直接返回 false)。一个不可逆的法律动作,不能建在一个可能被
 * 浏览器悄悄关掉的东西上。
 *
 * 「取消」放最右、并且拿焦点:双击穿透时第二下落在取消上,键盘敲回车的默认结果是不签。
 */
function IssueConfirmBar({
  prompt,
  confirmLabel,
  armedAt,
  onCancel,
  onConfirm,
}: {
  prompt: string;
  /** 红按钮上的字。签发说「确认签发」、否决说「确认删掉」—— 见 CONFIRM_BUTTON_LABEL。 */
  confirmLabel: string;
  armedAt: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus({ preventScroll: true });
  }, []);

  return (
    <div
      role="group"
      aria-label={confirmLabel}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }}
      className="flex flex-col gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2"
    >
      <div className="text-[13px] whitespace-pre-line text-red-800">{prompt}</div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => {
            // 静默期:举手后 400ms 内的「确认」当误触丢掉,一声不吭
            // (真误触的人根本没看见这个条,弹提示只会更懵;真要签的人稍等半秒就过了)。
            if (Date.now() - armedAt < ARM_QUIET_MS) return;
            onConfirm();
          }}
          className="rounded-md bg-red-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-red-700 focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:outline-none pointer-coarse:min-h-11 pointer-coarse:px-4"
        >
          {confirmLabel}
        </button>
        <button
          ref={cancelRef}
          type="button"
          onClick={onCancel}
          className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[13px] text-gray-700 hover:bg-gray-50 focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:outline-none pointer-coarse:min-h-11 pointer-coarse:px-4"
        >
          取消
        </button>
      </div>
    </div>
  );
}

type FormState = { due: string; photo: string };

const EMPTY_FORM: FormState = { due: "", photo: "" };

/**
 * 一条隐患的**详情/证据链**取数三态(W10)。
 *
 * 🔴 三者必须在屏幕上长得不一样,判据与面板那份 `LoadPhase` 一字不差,
 * 但这里的代价更具体:「读不出来」画成「这条还没签过文书」的话,
 * 监理会据此认为**昨天那份暂停令没签出来**,然后再签一次 —— 同一件事出两份法律文书,
 * 而两份的编号、日期都不一样,事后没人说得清哪份作数。
 *
 * `undefined`(这一条压根不在 `details` 里)= **还没开始读**,与 `loading` 画一样的东西:
 * 展开那一下到 effect 补上 `loading` 之间有一帧,那一帧不许闪一下空白。
 */
type DetailState =
  | { phase: "loading" }
  | { phase: "unreadable"; message: string }
  /** `userMsg` 是后端拼好的那句总结(「已签 3 份文书,复查过 1 次,最近一次不合格」)。 */
  | { phase: "ready"; detail: HazardDetail; userMsg: string };

/**
 * 一条隐患的证据链区块 —— 展开之后出现的那一块。
 *
 * 纯展示 + 一个重试回调,自己不发请求(与 `HazardRow` 同一条分工:请求全在面板那一层)。
 */
function HazardEvidence({
  state,
  artifactBase,
  onRetry,
}: {
  /** `undefined` = 还没开始读,与 loading 同画(见 `DetailState` 头注)。 */
  state: DetailState | undefined;
  artifactBase: string;
  onRetry: () => void;
}) {
  // 🔴 这两句上屏的字**都不过繁體转换器**(W12 复审定案:后端 Envelope 的 user_msg
  // 一律不转)。`message` 是后端 user_msg 或 normalizeError 的固定中文,
  // `userMsg` 是后端 `_detail_user_msg` 拼的那句总结 —— 两句都可能内插用户数据
  // (隐患描述、项目名、文件名),整句转换分不出哪一半是系统写的字。
  // 本地兜底那几句(SUPERVISION_MESSAGES.*)源码里已经是繁體,不需要转。
  if (!state || state.phase === "loading") {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-dashed border-gray-300 px-3 py-4 text-[12px] text-gray-500">
        <LoaderCircle className="size-4 animate-spin" />
        正在讀這條的文書和複查記錄…
      </div>
    );
  }

  if (state.phase === "unreadable") {
    return (
      <div className="flex flex-col items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-600" />
          {/* 🔴 这一句**永远不许写成「这条没有文书」**。W10 的全部教训就在这儿:
              读不出来和真的没有,在屏幕上长一样就等于骗人 —— 而这一侧骗人的后果是
              监理以为文书没签出来,回头再签一份。后半句是后端的人话或
              normalizeError 那几句固定中文,原样上屏。 */}
          <div className="text-[13px] leading-snug text-red-700">
            這條的文書和複查記錄沒讀出來(不是沒有,是沒讀到)。{state.message}
          </div>
        </div>
        <Button size="sm" variant="outline" onClick={onRetry} className="pointer-coarse:min-h-11">
          <RefreshCcw className="mr-1 size-3.5" />
          重試
        </Button>
      </div>
    );
  }

  const { detail } = state;
  const foundAt = formatHkMoment(detail.foundAt);
  const closedAt = formatHkMoment(detail.closedAt ?? "");

  return (
    <div className="flex flex-col gap-2">
      {/* 后端拼好的那句总结原样上屏。「这条复查了吗」是监理翻这一页最常问的一句,
          后端 `_detail_user_msg` 就是专为它写的 —— 前端再算一遍等于把判据抄成第二份。 */}
      {state.userMsg && (
        <div className="text-[12px] leading-snug text-gray-600">{state.userMsg}</div>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-400 tabular-nums">
        {/* 发现时刻是证据链的第一环(「什么时候发现的」),而它只有详情端点会给。
            拿不到就整格不出现 —— 摆一个「发现时间 —」看着像库里没记。 */}
        {foundAt && <span>發現 {foundAt}</span>}
        {closedAt && <span>銷項 {closedAt}</span>}
      </div>

      {detail.documents.length > 0 ? (
        <SupervisionDocCards documents={detail.documents} artifactBase={artifactBase} />
      ) : (
        // 「真的没有」这一档:话要说满,让人一眼看出这是**台账的事实**,
        // 不是上面那种「没读到」。两句话在屏幕上必须一眼分得开(颜色、图标、措辞全不同)。
        <div className="rounded-lg border border-dashed border-gray-300 px-3 py-3 text-[12px] text-gray-500">
          這條還沒簽過任何文書,也還沒登記過複查 —— 台賬裏就是空的。
        </div>
      )}
    </div>
  );
}

/**
 * 选中的那张复查照片(还没传 / 正在传 / 传好了,三态共用这一份)。
 *
 * `file` 留着是给**重试**用的:传失败之后要能原地再传一次,而不是逼人重拍 ——
 * 工地上那个部位可能已经不方便再爬上去了。
 */
type PhotoDraft = {
  file: File;
  /** `URL.createObjectURL` 出来的本地预览地址。**换图/关面板必须 revoke**,否则图堆在内存里。 */
  previewUrl: string;
  /** 「整改后.jpg · 2.3 MB」,由 `describePhotoFile` 排的人话。 */
  label: string;
};

/**
 * 复查照片直传的三态。
 *
 * 🔴 **「传失败」绝不许长得像「还没传」。** 这是本面板已经落过两次的规矩
 *(清单三态、证据链三态),这一处的代价同样具体:失败画成「还没传」的话,
 * 人以为自己忘了点,再点一次「拍照」重来一遍 —— 而真正的原因(照片太大 /
 * 接口没开通)一次都没被看见,他会一直循环下去。
 *
 * 三态在屏幕上的区分是**颜色 + 图标 + 措辞**三样一起变(见 `SharedReinspectPhotoField`),
 * 只靠一行小字的话,阳光下的手机屏幕上根本读不出来。
 *
 * ⚠️ 2026-08-16 起**整屏只有一份**(共用一张照片),不再按隐患编号存。
 * 它面向 lib 的那一份形状是 `SharedPhotoState` —— 那边是判别联合,编号只在
 * 「传好了」那一档存在;这边多带一个 `draft`,那是浏览器对象(File、objectURL),
 * 不许下沉到零依赖的 lib 里去。
 */
type PhotoUpload =
  | { phase: "uploading"; draft: PhotoDraft }
  /** `message` 一律是人话:后端的 `user_msg`、`normalizeError` 的固定中文,或本地预检那句。 */
  | { phase: "failed"; draft: PhotoDraft; message: string }
  | { phase: "done"; draft: PhotoDraft; photoId: string };

/**
 * 缩略图。**三态一律用本地 objectURL,不走产物出口取件。**
 *
 * 理由分两半:
 *   ① 「正在传」和「传失败」这两态压根还没有编号,取不了件 —— 只有本地这张能画。
 *      三态里两态用本地、一态用远端的话,传成功那一下同一张照片会在同一个位置
 *      重新加载一次(闪一下),而那正是人最需要确认「传对了没有」的时刻;
 *   ② 产物出口那条链(`NEXT_PUBLIC_ARTIFACT_BASE`)**断在任何一环都不报错**,
 *      https 页面拉 http 资源更是连请求都不发。断了的话,屏幕上会是一张绿色的
 *      「照片已上传」卡配一个碎图 —— 读起来就是「传成功了但图没了」,而事实是
 *      照片好好地在产物库里,只是这台机器的取件地址没配对。
 *
 * 产物出口那条路仍然要走一遍,但走在旁边那颗「打开」上(见 done 分支):
 * 它验的是「这张确实进了产物库、取得回来」,是**多一道核对**,不是替代预览。
 *
 * 预览画不出来的兜底:HEIC 在多数浏览器里渲染不了(iPhone 设成「保留原片」时
 * 从相册选图就是它),点错文件选到 PDF 也一样。给一个说明框比一个破图框强 ——
 * 这一格的全部意义就是让人确认自己选对了那张。
 *
 * `broken` 记的是**哪一张坏了**而不是一个布尔:换图之后 src 变了,布尔会把
 * 上一张的失败带到新的这张上,表现是换了一张好图却还写着「预览不出来」。
 */
function PhotoThumb({ src, alt }: { src: string; alt: string }) {
  const [broken, setBroken] = useState<string | null>(null);
  if (broken === src) {
    return (
      <div className="flex size-20 shrink-0 flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-gray-300 bg-gray-50 text-center">
        <ImageOff className="size-5 text-gray-400" />
        <span className="px-1 text-[10px] leading-tight text-gray-400">這張預覽不出來</span>
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      onError={() => setBroken(src)}
      className="size-20 shrink-0 rounded-lg border border-gray-200 bg-white object-cover"
    />
  );
}

/**
 * 「这次复查的照片」那一格 —— **面板级,整屏只有一处**(2026-08-16)。
 *
 * ── 为什么从「填编号」改成「拍照」(上一轮)────────────────────────────
 * 真人测试的原话:**「照片不能是编号意义不明」**。原先这一格是个文本框,
 * 标签写着「整改后照片的编号(32 位,在聊天里那张图下面)」—— 做复查的人
 * 手机里刚拍完那张照片,却要他退出去翻聊天记录、找到那张图、把图底下那串 hex
 * 抄回来。戴着手套、太阳底下、一只手扶梯子,这条路在工地上不成立。
 *
 * ── 为什么从「每行一个」改成「面板一个」(这一轮)──────────────────────
 * 真人原话:**「上传照片的地方太多了,一般都是在一张照片里」**。而这与事实相反 ——
 * 隐患本来就是从同一张照片里认出来的,整改后也只拍一张。三条隐患三个大虚线框,
 * 撑长了面板、还暗示人要拍三次。今天一张管一屏,**结论仍然一条一条下**
 * (那条取舍的完整推演在 supervision-lib 那一节的头注,别当成图省事)。
 *
 * 🔴 **手填编号那条逃生口没有删掉,只是挪进了每一行**(见 `RowPhotoChoice`)。
 * 它在这一层没有意义:共用那张不该被一个手打的编号顶掉,而「某一条要用别的照片」
 * 天然是**行**的事。有人确实会先在对话里发照片、再来登记复查,那条路仍然通。
 *
 * ── `capture="environment"` 不能省 ────────────────────────────────────
 * 手机上它直接调起**后置**摄像头 —— 复查拍的是墙面、临边、脚手架,不是脸。
 * 少了它会先弹一个「拍照 / 照片图库 / 浏览」的选择菜单,多一步,而且默认那一档
 * 在部分机型上是前置。桌面浏览器一律忽略这个属性、退回文件选择框,两边都对。
 * (打卡那边写的是 `capture="user"`,同一个属性、相反的取向 —— 那边要自拍。)
 */
function SharedReinspectPhotoField({
  upload,
  busy,
  artifactBase,
  coverage,
  onPick,
  onRetry,
}: {
  /** `undefined` = 还没选过图。 */
  upload: PhotoUpload | undefined;
  busy: boolean;
  artifactBase: string;
  /** 「下面 3 条隐患的复查结论都用这张」—— 由 `describeSharedPhotoCoverage` 排的人话。 */
  coverage: string;
  onPick: (file: File) => void;
  onRetry: () => void;
}) {
  /** 整屏只有一格,所以 id 是定值 —— 不再拼隐患编号(拼了反而像每行各有一个)。 */
  const inputId = "gyt-shared-photo-file";
  // 传失败那句话**不过繁體转换器**(W12 复审定案:后端 user_msg 一律不转)。
  // 三个来源里有一个是后端的 `user_msg`(413「照片太大了(12.4 MB),上限 10MB」、
  // 400「不是照片文件」),它可能内插用户数据;另两个来源(`normalizeError` 的
  // 固定中文、本地预检那句 PHOTO_MESSAGES)源码里已经是繁體,本来就不用转。
  /** 传好之后那张图在产物库里的地址 —— 与证据链里的复查照片走的是同一条取件路。 */
  const storedUrl =
    upload?.phase === "done" ? documentUrl({ artifact_id: upload.photoId }, artifactBase) : null;

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-gray-200 bg-gray-50/60 px-3 py-2.5">
      <div className="text-[12px] font-medium text-gray-700">這次複查的照片</div>

      {/* 文件选择框**始终挂着**(靠 htmlFor 触发),三种状态下的「换一张」共用它。
          按状态条件渲染的话,每换一次状态 input 就重建一次,选到一半的对话框会被吞掉。 */}
      <input
        id={inputId}
        type="file"
        accept="image/*"
        // 🔴 后置摄像头,见组件头注。别改成 user,那是打卡自拍那条线。
        capture="environment"
        disabled={busy}
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          // 🔴 先取到文件,再把 value 清空。清 value 是为了「重试时又选了同一张」
          //    也能触发 change —— 不清的话浏览器认为值没变、根本不发事件,
          //    界面上就是「点了没反应」,而这恰恰是传失败之后最常见的下一步动作。
          event.currentTarget.value = "";
          if (file) onPick(file);
        }}
        className="hidden"
      />

      {!upload ? (
        <>
          <Label
            htmlFor={inputId}
            aria-disabled={busy}
            className={`flex items-center justify-center gap-2 rounded-xl border border-dashed border-gray-300 bg-white px-4 py-6 text-sm text-gray-600 transition-colors ${
              busy ? "pointer-events-none opacity-50" : "cursor-pointer hover:bg-gray-100"
            }`}
          >
            <Camera className="size-5" />
            拍整改後的照片
          </Label>
          {/* 传之前就把「它管着哪几条」说清楚 —— 不说的话,人看见一个孤零零的上传框
              会以为它只管第一条,于是拍完第一张又去找第二个框(而第二个框已经没有了)。
              后半句是这次改造的红线:共用的是**照片**,不是**结论**。 */}
          <div className="text-[11px] leading-snug text-gray-500">
            {coverage}結論仍然一條一條下。
          </div>
        </>
      ) : upload.phase === "uploading" ? (
        // 底色用白:外面那圈已经是浅灰了,再套一层同色的话边框看不出来,
        // 三态里就少了「颜色」这一维(见 `PhotoUpload` 头注:三样一起变才读得出)。
        <div className="flex items-start gap-3 rounded-xl border border-gray-200 bg-white px-3 py-2.5">
          <PhotoThumb src={upload.draft.previewUrl} alt="正在上傳的複查照片" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 text-[13px] font-medium text-gray-700">
              <LoaderCircle className="size-4 animate-spin" />
              正在上傳照片…
            </div>
            <div className="mt-0.5 truncate text-[12px] text-gray-500">{upload.draft.label}</div>
            <div className="mt-1 text-[11px] text-gray-400">傳完才能下複查結論,稍等一下。</div>
          </div>
        </div>
      ) : upload.phase === "failed" ? (
        // 🔴 红边 + 红字 + 警告图标 + 「这张还没传上去」这句话,四样一起上 ——
        //    见 `PhotoUpload` 头注:失败长得像「还没传」的话,人会以为自己忘了点。
        <div className="flex items-start gap-3 rounded-xl border border-red-300 bg-red-50 px-3 py-2.5">
          <PhotoThumb src={upload.draft.previewUrl} alt="沒能上傳的複查照片" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 text-[13px] font-medium text-red-700">
              <AlertTriangle className="size-4 shrink-0" />
              這張還沒傳上去
            </div>
            {/* 后端那句人话(或本地预检那句)原样上屏,不重新包装成「上传失败」——
                「照片太大了(12.4 MB),上限 10MB」能让人自己解决,「上传失败」不能。 */}
            <div className="mt-0.5 text-[12px] leading-snug text-red-700">{upload.message}</div>
            <div className="mt-0.5 truncate text-[12px] text-gray-500">{upload.draft.label}</div>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={onRetry}
                className="pointer-coarse:min-h-11"
              >
                <RefreshCcw className="mr-1 size-3.5" />
                重新上傳這張
              </Button>
              <Label
                htmlFor={inputId}
                aria-disabled={busy}
                className={`inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1 text-[12px] text-gray-700 transition-colors pointer-coarse:min-h-11 pointer-coarse:px-4 ${
                  busy ? "pointer-events-none opacity-50" : "cursor-pointer hover:bg-gray-50"
                }`}
              >
                <Camera className="size-3.5" />
                換一張
              </Label>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex items-start gap-3 rounded-xl border border-emerald-200 bg-emerald-50/60 px-3 py-2.5">
          <PhotoThumb src={upload.draft.previewUrl} alt="已上傳的複查照片" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 text-[13px] font-medium text-emerald-800">
              <Check className="size-4 shrink-0" />
              照片已上傳,可以下複查結論了
            </div>
            <div className="mt-0.5 truncate text-[12px] text-gray-500">{upload.draft.label}</div>
            {/* 编号仍然摆出来,但它现在是**结果**不是**输入** —— 监理要在电话里报它、
                事后按它对证据链,一点就能整串复制(同文书卡片的做法)。 */}
            <div className="mt-0.5 font-mono text-[11px] break-all text-gray-400 select-all">
              {upload.photoId}
            </div>
            {/* 🔴 **它管着哪几条,必须写在这张卡上。** 共用之后照片和结论不在同一行了,
                不说清的话人得自己数,而数错的方向是「以为它管的比实际多」——
                「那三条我都传过照片了」,然后有一条的证据其实来自别处。
                这个数由 `hazardsUsingSharedPhoto` 算(三道筛子:能复查 + 用的是这张 +
                现在真点得动),不是拿清单长度凑的。 */}
            <div className="mt-1 text-[12px] leading-snug text-emerald-900">{coverage}</div>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <Label
                htmlFor={inputId}
                aria-disabled={busy}
                className={`inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1 text-[12px] text-gray-700 transition-colors pointer-coarse:min-h-11 pointer-coarse:px-4 ${
                  busy ? "pointer-events-none opacity-50" : "cursor-pointer hover:bg-gray-50"
                }`}
              >
                <Camera className="size-3.5" />
                換一張
              </Label>
              {storedUrl && (
                // 走产物出口取一遍 —— 多一道核对:这张确实进了产物库、取得回来。
                // 取不回来的话下一步的证据链里也会是碎图,早发现比晚发现好。
                <a
                  href={storedUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-2.5 py-1 text-[12px] text-gray-700 transition-colors hover:bg-gray-50 pointer-coarse:min-h-11 pointer-coarse:px-4"
                >
                  <ExternalLink className="size-3" />
                  打開
                </a>
              )}
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

/**
 * 一行的「这一条用哪张照片」—— 标注 + 折叠着的手填编号逃生口。
 *
 * ── 为什么行里只剩这么点东西 ──────────────────────────────────────────
 * 照片本身搬到面板顶上去了(见 `SharedReinspectPhotoField` 头注)。行里留下的
 * 只有两件**只有行才回答得了**的事:这一条到底用的哪张、以及某一条要用别的照片时
 * 从哪儿填。一个虚线上传框都不该再出现在这里。
 *
 * ── 🔴 「用的不是共用那张」必须看得出来 ──────────────────────────────
 * 这是行内逃生口的配套。人以为清单里几条都用同一张、而证据链里其实不是,
 * 是这一层唯一能悄悄挂错证据的路子 —— 而复查合格会把隐患**销项**,事后翻证据链
 * 只会看到一张跟这次复查无关的照片,屏幕上那两样东西当时看着都对。
 * 所以 `own` 那一档用琥珀色 + 常驻一句话,不藏进 title(触屏没有 hover)。
 *
 * 判据一律问 `photoSourceOf`,**不在这儿就地比字符串** —— 归一化(trim + 转小写)
 * 和「手填的恰好等于共用那张就算同一张」两条规矩都在那个函数里,抄一份必然漂。
 */
function RowPhotoChoice({
  hazardNo,
  rowPhotoId,
  shared,
  busy,
  onPhotoIdChange,
}: {
  hazardNo: string;
  /** 行内手填的那个编号(= `form.photo`);空 = 用共用那张。 */
  rowPhotoId: string;
  shared: SharedPhotoState;
  busy: boolean;
  onPhotoIdChange: (value: string) => void;
}) {
  const source = photoSourceOf(rowPhotoId, shared);
  /**
   * 手填那一格折起来没有。纯 UI 状态,不发请求,所以留在这一层。
   *
   * 初值跟着 `source` 走:已经填了编号的行**一进来就是展开的** —— 收起来的话,
   * 屏幕上写着「这一条用的不是共用那张」,而那个编号在哪儿改却看不见。
   */
  const [manualOpen, setManualOpen] = useState(source === "own");
  const manualId = `gyt-photo-id-${hazardNo}`;

  return (
    <div className="flex flex-col gap-1">
      {source === "own" ? (
        <div className="flex items-start gap-1.5 rounded-lg bg-amber-50 px-2 py-1.5 text-[12px] leading-snug text-amber-900">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <div className="min-w-0">
            {/* 上面还没传共用照片时不提「共用那张」—— 没有的东西不该被拿来对照,
                那只会让人去找一个屏幕上不存在的参照物。 */}
            <div>
              {shared.phase === "ready"
                ? SHARED_PHOTO_MESSAGES.usesOwn
                : SHARED_PHOTO_MESSAGES.usesOwnAlone}
            </div>
            {/* 编号跟着一起摆出来,这一格才是自足的:手填框折起来之后
                「下面填的这个编号」底下就什么都没有了,而这条标注的意义正是
                让人看清这一条挂的到底是哪张。原样显示人打进去的那串(不归一化)——
                他要核对的是自己刚才粘的东西。 */}
            <div className="mt-0.5 font-mono text-[11px] break-all text-amber-800 select-all">
              {rowPhotoId.trim()}
            </div>
          </div>
        </div>
      ) : source === "shared" ? (
        // 常态,说得轻:一句灰字就够了。这一格的意义是让上面那一档(用了别的照片)
        // 有得对照 —— 全屏都没有标注的话,琥珀色那条也就没有参照系了。
        <div className="text-[11px] text-gray-400">{SHARED_PHOTO_MESSAGES.usesShared}</div>
      ) : null}

      {/* ── 手填编号:折叠着,不删 ──────────────────────────────────────
          它是引用「聊天里已经传过的那张图」的唯一途径 —— 有人确实会先在对话里
          发照片、再来登记复查。删掉等于把那条路堵死,而那时人手上只有一个编号,
          连个能粘的地方都没有。默认折起来是因为它是**少数路径**:摆在明面上的话,
          人又会以为「那才是正经做法」,而那正是上一轮就修掉的东西。
          **什么时候用它这句话没有丢,只是在 title 里**(收起时它是一行常驻文字,
          而清单里每条可复查的隐患都要重复一遍)。 */}
      <div>
        <button
          type="button"
          onClick={() => setManualOpen((open) => !open)}
          aria-expanded={manualOpen}
          title={manualOpen ? undefined : "這一條要用別的照片:填它的編號,填了就不吃上面那張"}
          className="cursor-pointer text-[11px] text-gray-400 underline-offset-2 transition-colors hover:text-gray-600 hover:underline pointer-coarse:min-h-11"
        >
          {manualOpen ? "收起" : "這條用別的照片"}
        </button>
        {manualOpen && (
          <div className="mt-1.5 flex flex-col gap-1">
            <Label htmlFor={manualId} className="text-[12px]">
              照片編號(32 位,在聊天裏那張圖下面)
            </Label>
            <Input
              id={manualId}
              value={rowPhotoId}
              onChange={(e) => onPhotoIdChange(e.target.value)}
              placeholder="如:0123456789abcdef0123456789abcdef"
              disabled={busy}
              className="font-mono text-[12px]"
            />
            {/* 打了半截就说一句 —— 空着不说(那是还没开始填,不是填错了)。
                判据用 isPhotoId,与 actionBody 同一个正则:不然会出现
                「这里说没问题、点下去被拦住」。
                🔴 半截编号**不会**悄悄回落成共用那张(`photoSourceOf` 头注),
                所以这句红字和那两颗灰按钮说的是同一件事。 */}
            {rowPhotoId.trim() !== "" && !isPhotoId(rowPhotoId) && (
              <div className="text-[11px] text-red-600">{PHOTO_MESSAGES.badManualId}</div>
            )}
            {/* 空着是**有意义的**(= 用共用那张),所以这句话不写成「必填」。
                「怎么回到共用那张」得有人说 —— 不说的话,填错了的人只会再去找一颗
                「取消」按钮,而正确做法就是把这一格清空。 */}
            <div className="text-[11px] text-gray-400">
              留空就用上面那張共用的照片。
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ⚠️ 本组件里原来还有一行常驻文字:「复查结论由人来下:模型分不清『问题已消除』
//    和『这张没拍到那个部位』。」
//
//    2026-08-16 真人反馈「太多手续复杂」后**挪到了面板顶部、只说一次**(搜 D11_NOTE)。
//    它讲的是 D11 —— 模型给建议、人下结论。复查照片的角度光线取景都变了,
//    「没拍到那个部位」和「问题已消除」在模型眼里一样,那是往「误判合格」方向错,
//    而这一侧会死人。
//
//    🔴 **它是产品立场,不是每行的操作提示。** 挂在每一行上时,清单里每条可复查的
//    隐患都要重复一遍(实测 3 条 → 同一句话在屏幕上出现 3 次),而它要传达的东西
//    一个人只需要知道一次。**删掉不行** —— 那样「为什么系统不替我判」就没人回答了。

/** 一条隐患的处置区。纯展示 + 回调,自己不发请求 —— 请求全在面板那一层,
 *  这样「同一时刻只有一个动作在飞」才好保证(法律文书不能并发点两下)。 */
function HazardRow({
  hazard,
  selected,
  busy,
  armedAction,
  armedAt,
  form,
  failure,
  artifactBase,
  expanded,
  detail,
  sharedPhoto,
  usedPhotoId,
  onToggleSelect,
  onFormChange,
  onArm,
  onDisarm,
  onAct,
  onToggleDetail,
  onRetryDetail,
}: {
  hazard: HazardBrief;
  selected: boolean;
  /** 整个面板有请求在飞时为 true:此时所有按钮禁用。 */
  busy: boolean;
  armedAction: DisposalAction | null;
  armedAt: number;
  form: FormState;
  /** 这条隐患上一次动作的失败原因(后端的人话,原样上屏)。 */
  failure: string | null;
  /** 产物出口,给证据链里的文书下载与复查照片用。**面板收 props、自己不读 env。** */
  artifactBase: string;
  /** 证据链展开着没有。 */
  expanded: boolean;
  /** 这条的详情三态;`undefined` = 还没开始读(见 `DetailState` 头注)。 */
  detail: DetailState | undefined;
  /** 面板顶上那张共用照片现在什么样(整屏一份,不是每行一份)。 */
  sharedPhoto: SharedPhotoState;
  /** 这一行上一次登记复查用掉的编号;null = 这一轮还没登记过(见 `reinspectBlocker`)。 */
  usedPhotoId: string | null;
  onToggleSelect: () => void;
  onFormChange: (patch: Partial<FormState>) => void;
  onArm: (action: DisposalAction) => void;
  onDisarm: () => void;
  onAct: (action: DisposalAction, grade?: string, result?: "pass" | "fail") => void;
  onToggleDetail: () => void;
  onRetryDetail: () => void;
}) {
  const actions = availableActions(hazard);
  const isPending = hazard.status === "pending";
  const needsDue = actions.some(actionNeedsDuePhrase);
  const needsPhoto = actions.some(actionNeedsPhoto);
  /**
   * 期限那一行只在**源里真给了这个键**时出现(`due_date !== undefined`)。
   * 老路(聊天里的工具返回)没有这几个字段,渲成「期限:—」看着像后端没下期限,
   * 而真相是这条路不带这个信息 —— supervision-lib 的 HazardBrief 头注写了同一件事。
   */
  /**
   * 发现照片的取件地址;没有照片(或链没配)就是 null,那时整格不渲染。
   * 走 `documentUrl` 而不是自己拼:它守着「空就返回 null、绝不渲染死链接」那条规矩。
   */
  const photoUrl = documentUrl({ artifact_id: hazard.photo_id ?? null }, artifactBase);
  const hasDueInfo = hazard.due_date !== undefined;
  /**
   * 这条隐患的级别**有没有人判过**。
   *
   * 🔴 **不是 `hazard.grade` 有没有值** —— 它永远有值。`needs_grading=1` 时那个值是
   * `agents/supervision/grading.py` 的映射表给的**默认档**(一般),不是结论。
   * 把默认档当结论用,轻则界面上锁掉一颗本该能点的按钮(定级为一般),
   * 重则对外念出「这条是一般隐患」—— 而它真实级别是未知的。
   * 后端 `_advise` 与端点 `_require_graded` 认的都是这同一个旗子。
   */
  const 已判级别 = currentGrade(hazard);
  /**
   * 「合格 / 不合格」那两颗现在点不点得动 —— `null` = 点得动,否则里面就是原因。
   *
   * 🔴 判据整个下沉到 `reinspectBlocker`,**这一层一个 if 都不许自己写**:
   * 它要同时回答四件事(有没有照片 / 在不在传 / 传没传上去 / 这一行是不是刚用过
   * 这张),而**提交时发哪个编号**是同一个 `effectivePhotoId` 算的。
   * 在这儿另写一遍的下场就是那两处漂开:按钮亮着而请求被自己人拦、
   * 或者更坏 —— 按钮说用 A 张、发出去的是 B 张,而复查合格是**销项**,不可撤销。
   */
  const 复查拦路 = reinspectBlocker({
    rowPhotoId: form.photo,
    shared: sharedPhoto,
    usedPhotoId,
  });
  /** 展开区的 id —— 给 `aria-controls` 用。隐患编号只含字母数字和连字符,直接拼安全。 */
  const evidenceId = `gyt-evidence-${hazard.hazard_no}`;

  // ── 上屏文字的繁體化(界面恒繁體)。**一律只转显示的那一份,原值全部留着** ──────
  //
  // 🔴 这一段每一条底下都有一个「原值还在被谁用」,漂了就静默出错:
  //   · itemText     —— 只显示。8 类受控违规项里 5 类简繁不同形(临边无防护 / 用电隐患 …)
  //   · severityText —— 原值 `hazard.severity` 还要当 `SEVERITY_CHIP` 的键(较大→較大 会丢配色)
  //   · gradeText    —— 原值 `hazard.grade` 还要当 `GRADE_CHIP` 的键(严重→嚴重 会丢配色)
  //   · statusText   —— 后端 `status_display` 与本地兜底表**两条路供同一颗徽章**,
  //                     所以整个表达式一起过转换,不是只转其中一条 —— 只转一条的表现是
  //                     「点一下确认,徽章的字忽然换了一种写法」(patchHazard 会丢掉
  //                     status_display,渲染当场从左边回落到右边)
  //   · dueText      —— `due_display` 是后端排的人话;`due_date` 是 ISO 日期、没有汉字
  //   · gradeLabels  —— 与 `GRADE_CHOICES` **按下标配对**;送后端和比较用的仍是原值
  //   · confirmText  —— `confirmPrompt` 里插了 `hazard.item`(后端字),所以整句要过
  //
  // ⚠️ `failure`(后端的 user_msg 原样上屏那条路)**故意不在这份名单里** ——
  //    W12 复审定案:user_msg 一律不转,理由见下面它上屏那处的注释。
  //
  // ═══════════════════════════════════════════════════════════════════════════
  // 🔴 屏幕上是繁體,而**已签发的 docx 正文仍是简体** —— 这是**已知且被接受**的状态
  // ───────────────────────────────────────────────────────────────────────────
  //   屏幕上(下面这几行转出来的):臨邊無防護   嚴重   《工程暫停令》
  //   docx 正文与标题(后端出的)  :临边无防护   严重   《工程暂停令》
  //       └ agents/supervision/documents.py 的正文模板 + core/doc_no.DOC_TITLE_ZH
  //
  // **监理签的是那份 docx,看的是这块面板。** 方案 §7.1 当初据此判定
  // 「监理面板不许先做」;`bd1b8f9` 越过了那条禁令(流程失误,当时没回应它),
  // 复审抓出后交负责人拍板 —— **2026-08-18 定案「甲:保留繁體面板」**。
  //
  // 定案的含义是**可接受,不是已解决**:
  //   · 依据:面板是操作界面不是文书本身;§4.55 已为规范书名接受过同一形态
  //   · 「文书正文用哪种语言固化」转为 **W11 待决项**,§7.1 的推理原样留着没被推翻
  //   · 附带好处:繁體面板让「香港工地在签简体法律文书」这件事**变得可见** ——
  //     那本来就先于本方案存在,以前只是没人看得见
  //
  // ⚠️ 所以**别顺手把 documents.py 或 DOC_TITLE_ZH 也转了来「对齐」** ——
  //    那两处进 docx、进库、进唯一索引,动它们是 W11 的题,不是观感修正。
  //    同理:`doc.filename` 下面有单独一条红字钉着不许转(它要和盘上那份对得上)。
  // ═══════════════════════════════════════════════════════════════════════════
  const itemText = useHantUI(hazard.item);
  const severityText = useHantUI(hazard.severity);
  const gradeText = useHantUI(hazard.grade);
  const statusText = useHantUI(hazard.status_display || hazardStatusZh(hazard.status));
  const dueText = useHantUI(hazard.due_display || hazard.due_date || "");
  const gradeLabels = useHantUIAll(GRADE_CHOICES);
  // hook 必须无条件调用,所以没举手时喂空串 —— **不是**给 `confirmPrompt` 随便传个动作:
  // 它对认不出的动作会落到最后那个 return(上报主管部门那句),而这句话是人在按下
  // 不可撤销的那一下之前唯一读的东西,拼错了比抛异常还坏(异常至少看得见)。
  const confirmText = useHantUI(armedAction ? confirmPrompt(armedAction, hazard) : "");

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2.5">
      <div className="flex items-start gap-2.5">
        {isPending && (
          // 只有 pending 能进批量确认(D17),所以别的状态连勾选框都不出现 ——
          // 给一个点了没用的勾选框,人会以为系统没反应。
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            disabled={busy}
            aria-label={`選中隱患 ${hazard.hazard_no}`}
            className="mt-1 size-4 shrink-0 cursor-pointer accent-blue-600 pointer-coarse:size-6"
          />
        )}
        {/* 🔴 发现这条隐患的那张现场照片。
            2026-08-17 真人反馈只有三个字:「没有照片」——
            在这之前操作台上一条隐患只有文字,而人要在这儿判**一般 / 严重**
            (定级是签发文书的前置),还要判是不是「识错了」而按下否决 ——
            **否决这个判断完全依赖看照片**(帽子到底戴没戴)。
            让人不看照片就下这两个判断,方向是反的。

            ⚠️ 这是**首次发现**那张,不是复查照片(那张在证据链的复查记录行里)。
            混起来 = 拿发现时的照片当「整改后」的证据。
            ⚠️ 它吃 ARTIFACT_BASE 那条链;断了会变成「预览不出来」的占位框
            (PhotoThumb 的 onError 兜着,不留破图)。点开是大图,走同一个取件端点。 */}
        {photoUrl && (
          <a href={photoUrl} target="_blank" rel="noreferrer" title="點開看大圖">
            <PhotoThumb src={photoUrl} alt={`${itemText} 的現場照片`} />
          </a>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-gray-900">{itemText}</span>
            {/* 🔴 现场那一档念的是 `severity`,**不是 `grade`**(supervision-lib 的
                HazardBrief.severity 那条红线,与后端 `_hazard_line` 同一条规矩):
                needs_grading=1 时 `grade` 是映射表给的默认值(一般),不是有人判过的
                结论 —— 照它念,屏幕上写着「一般隐患」,人会照这句去签文书,然后被
                服务端硬拦③拒掉,却不知道该先去定级。
                `severity` 只有 GET 端点那条路会给;老路(工具返回)没有这个键,
                那时这颗徽章干脆不出现,让下面那颗「监理定级」和「⚠ 需人工定级」说话。 */}
            {hazard.severity && (
              // 🔴 键用原值 `hazard.severity`(SEVERITY_CHIP 的键是简体,与后端
              //    `agents/safety/severity.py` 同源);屏幕上那份用 severityText。
              <Chip tone={SEVERITY_CHIP[hazard.severity] ?? "bg-gray-100 text-gray-600 ring-gray-200"}>
                現場{severityText}
              </Chip>
            )}
            {/* 监理那一档(一般/严重)只在**定过级之后**才显示 —— 它决定了下面出现的是
                「签发通知单」还是「签发暂停令」(服务端硬拦①②),没定级时把默认值摆出来
                就是在给一个还不成立的结论背书。 */}
            {!hazard.needs_grading && hazard.grade && (
              // 同上:键用原值(GRADE_CHIP 的键来自 db/hazards.py 的 GRADES),字用 gradeText。
              <Chip tone={GRADE_CHIP[hazard.grade] ?? "bg-gray-100 text-gray-600 ring-gray-200"}>
                監理定級:{gradeText}
              </Chip>
            )}
            <Chip tone={STATUS_CHIP[hazard.status] ?? "bg-gray-100 text-gray-600 ring-gray-200"}>
              {/* 中文名优先用后端算好的那份;拿不到才退回本地词表(两边同一张表)。
                  🔴 **两条路一起过繁體转换**(statusText),不是只转其中一条 ——
                  只转一条的表现是同一颗徽章会因为「这次是哪条路供的字」而变字形。 */}
              {statusText}
            </Chip>
            {hazard.needs_grading && (
              // 🔴 显眼是刻意的:needs_grading=1 的隐患**任何签发都被服务端硬拦**
              // (Codex#11)。不标出来的话,人会一直点签发、一直被拒,而真正要做的
              // 是先定级 —— 界面必须把这件事说在前面。
              <Chip tone="bg-fuchsia-100 text-fuchsia-800 ring-fuchsia-300">
                ⚠ 需人工定級
              </Chip>
            )}
            {hazard.overdue && (
              <Chip tone="bg-red-100 text-red-800 ring-red-300">已超期</Chip>
            )}
            {/* 未归工地(D6):`project_id === ""` 是**有意义的值**,不是缺失。
                这一条在「按工地筛」的那一屏里永远不出现 —— 标出来,监理才知道
                切回「全部」时多出来的是哪些。有归属的不显示编号:这里拿不到工地名,
                摆一串 P-xxxx 只是噪声。 */}
            {hazard.project_id === "" && (
              <Chip tone="bg-gray-100 text-gray-600 ring-gray-200">未歸工地</Chip>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
            <span className="font-mono text-[11px] break-all text-gray-400 select-all">
              {hazard.hazard_no}
            </span>
            {hasDueInfo && (
              <span className={`text-[11px] ${hazard.overdue ? "text-red-600" : "text-gray-400"}`}>
                {/* 期限的人话由后端算(due_display),前端一行日期换算都不写 ——
                    与下面那个输入框同一条红线(dates.py 用 314 行证明了这件事有多容易算错)。 */}
                {hazard.due_date ? `整改期限 ${dueText}` : "還沒下過整改期限"}
              </span>
            )}
          </div>
        </div>
      </div>

      {hazard.needs_grading && (
        <div className="rounded-lg bg-fuchsia-50 px-2.5 py-2 text-[12px] text-fuchsia-900">
          現場判的是「待定級」(詞表外的隱患項)。不知道不等於不嚴重 ——
          先由人定成一般或嚴重,才能簽文書。
        </div>
      )}

      {needsDue && (
        <div className="flex flex-col gap-1">
          <Label htmlFor={`gyt-due-${hazard.hazard_no}`} className="text-[12px]">
            整改期限(寫原話:{DUE_PHRASE_EXAMPLES})
          </Label>
          {/* 原话原样送后端,**前端一行日期换算都不写** —— agents/schedule/dates.py
              用 314 行证明了中文日期不好算,红线是「只传原话,代码来算」。 */}
          <Input
            id={`gyt-due-${hazard.hazard_no}`}
            value={form.due}
            onChange={(e) => onFormChange({ due: e.target.value })}
            placeholder={`如:${DUE_PHRASE_SAMPLE}`}
            disabled={busy}
          />
        </div>
      )}

      {needsPhoto && (
        <RowPhotoChoice
          hazardNo={hazard.hazard_no}
          rowPhotoId={form.photo}
          shared={sharedPhoto}
          busy={busy}
          onPhotoIdChange={(value) => onFormChange({ photo: value })}
        />
      )}

      {armedAction ? (
        <IssueConfirmBar
          // 这句里插了 `hazard.item`(后端来的违规项名),所以整句过转换,
          // 不是只转 `confirmPrompt` 里那些源码文案。
          prompt={confirmText}
          confirmLabel={CONFIRM_BUTTON_LABEL[armedAction] ?? "確認"}
          armedAt={armedAt}
          onCancel={onDisarm}
          onConfirm={() => onAct(armedAction)}
        />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {actions.map((action) =>
            action === "grade" ? (
              // 定级给两颗按钮而不是一个下拉:少一次点击,手套也点得中。
              //
              // 🔴 **「现在是哪一档」不许渲染成按钮。** 2026-08-16 手工验抓到两件事,
              //    根子都在原来那句 `disabled={busy || hazard.grade === grade}`:
              //
              //    ① 观感:当前级别那颗是 disabled 的,但「严重」恒用 destructive
              //       (实心红)——而这个面板里实心红是**主操作**的样子。
              //       一颗看着是主操作、点下去没反应的按钮,读起来就是系统坏了。
              //    ② 🔴 **功能**:`needs_grading=1` 时 `hazard.grade` 里存的是
              //       **映射表给的默认档(一般)**,不是有人判过的结论。于是那一档
              //       「一般」这颗被判成「当前值」而禁用 —— 监理认定它就是一般隐患,
              //       **却点不下去**,屏幕上唯一能点的是「严重」。
              //       而硬拦②防的正是「一般隐患签了暂停令 = 平白停一片人的工」。
              //
              //    所以判据从「值相等」换成 `hasVerdict`(有没有人判过):
              //      · 判过 → 当前那一档渲成**灰色状态片**(不是按钮),另一档才是按钮;
              //      · 没判过(needs_grading)→ **两颗都是真按钮**,一颗都不锁。
              <div key={action} className="flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-gray-500">
                  {已判级别 ? "改判為" : "定級為"}
                </span>
                {/* 🔴 `grade` 是**简体原值**,一路管着三件事:`已判级别 === grade` 的比较、
                    `GRADE_SEVERE` 的配色判断、以及 `onAct("grade", grade)` **送后端**
                    (`HAZARD_GRADES` 是受控词表,词表外后端回 400)。
                    屏幕上那份繁體是 `gradeLabels[i]`,**按下标**跟这个数组配对 ——
                    别图省事把数组换成繁體的,那三处会一起静默失灵:
                    比较永远不等(当前档渲成按钮)、配色掉回灰、请求被后端 400。 */}
                {GRADE_CHOICES.map((grade, i) =>
                  已判级别 === grade ? (
                    // 状态片:说清「现在就是这一档」,而不是摆一颗点不动的按钮。
                    <span
                      key={grade}
                      title="這是現在的級別,不用再點一次"
                      className="inline-flex items-center gap-1 rounded-md bg-gray-100 px-2 py-1 text-[12px] text-gray-500 ring-1 ring-gray-200 ring-inset"
                    >
                      <Check className="size-3.5" />
                      現在:{gradeLabels[i]}
                    </span>
                  ) : (
                    <Button
                      key={grade}
                      size="sm"
                      variant={grade === GRADE_SEVERE ? "destructive" : "outline"}
                      disabled={busy}
                      onClick={() => onAct("grade", grade)}
                      className="pointer-coarse:min-h-11"
                    >
                      {gradeLabels[i]}
                    </Button>
                  ),
                )}
              </div>
            ) : action === "reinspect" ? (
              // 🔴 **照片没就绪就把这两颗按下去的路堵上**,而不是让 `actionBody` 事后抛。
              //    ① 「合格」会把隐患**销项**,那一下不可撤销,而销项的唯一凭据就是
              //       这张整改后的照片(方案 §5.2 那条红线)—— 不能让人在一个不可逆
              //       动作上先点了再说;
              //    ② 更要命的是被拦下来那句话:`SUPERVISION_MESSAGES.badPhotoId` 写的是
              //       「在聊天记录里那张照片下面能看到」,那是**旧路**的指路话。人刚刚
              //       明明拍了一张,却被指回聊天记录去找编号 —— 真人测试反馈的那句
              //       「照片不能是编号意义不明」说的就是这一幕。
              //    判据用 `复查拦路`(= `reinspectBlocker`),与提交时算编号的
              //    `effectivePhotoId` 同源,两处不许各写一份。
              // 变灰**必须说清差什么** —— 只变灰的话人只知道点不动、不知道要先干嘛,
              // 那正是本仓反复防的「点了没反应」。
              //
              // 🔴 差什么这句话默认在 `title` 里(真人反馈「太多手续复杂」那一轮定的):
              //    照片那一格就在面板顶上、大得多、还是虚线框/转圈/红卡 —— 缺什么是
              //    自明的,一句常驻解释换来的是每条可复查的隐患都重复一遍。
              //    ⚠️ **`already-used` 那一档是例外,必须常驻**:它是这一行独有的事实
              //    (刚拿共用那张给这条登记过复查了),面板顶上那一格一点线索都没有,
              //    而触屏根本没有 hover —— 藏进 title 等于没说,人只会看见一颗死按钮。
              <div key={action} className="flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-gray-500">複查結論</span>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || 复查拦路 !== null}
                  title={复查拦路?.message}
                  onClick={() => onAct("reinspect", undefined, "pass")}
                  className="pointer-coarse:min-h-11"
                >
                  <Check className="mr-1 size-3.5" />
                  合格
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || 复查拦路 !== null}
                  title={复查拦路?.message}
                  onClick={() => onAct("reinspect", undefined, "fail")}
                  className="pointer-coarse:min-h-11"
                >
                  不合格
                </Button>
                {(复查拦路?.reason === "already-used" || 复查拦路?.reason === "uploading") && (
                  // 「正在传」也常驻:那一档的等待是有尽头的,而人盯着屏幕不知道
                  // 自己在等什么最难受(这条从改造前就是这么定的,措辞跟着共用改了指向)。
                  <span className="text-[12px] text-gray-600">{复查拦路.message}</span>
                )}
              </div>
            ) : (
              <Button
                key={action}
                size="sm"
                // 「否决」不能长成 destructive 那颗红按钮:它跟「签发暂停令」并排站,
                // 两颗一样红的话,手最先够到的那颗是哪颗就成了运气问题(而这两颗一颗
                // 停工、一颗删数据)。它走 outline + 垃圾桶图标,红只留给签发那两颗。
                variant={
                  action === "reject"
                    ? "outline"
                    : actionNeedsConfirm(action)
                      ? "destructive"
                      : "default"
                }
                disabled={busy}
                onClick={() => (actionNeedsConfirm(action) ? onArm(action) : onAct(action))}
                className={
                  action === "reject"
                    ? "text-gray-600 pointer-coarse:min-h-11"
                    : "pointer-coarse:min-h-11"
                }
              >
                {action === "reject" ? (
                  <Trash2 className="mr-1 size-3.5" />
                ) : (
                  actionNeedsConfirm(action) && <Gavel className="mr-1 size-3.5" />
                )}
                {ACTION_LABEL[action]}
              </Button>
            ),
          )}
          {actions.length === 0 && (
            <span className="text-[12px] text-gray-400">
              這條已經走完流程,沒有下一步了。
            </span>
          )}
        </div>
      )}

      {failure && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[13px] text-red-700">
          {/* 🔴 后端的人话**原样上屏,一个字都不转**(W12 复审定案:user_msg 一律不转)。
              批量确认失败那条走的是 `result.failed[].reason`,而后端拼这些句子时
              会把隐患描述、项目名这类**用户/现场数据**内插进去 —— 整句过转换器
              在原理上分不出哪一半是系统写的字,而分不出时默认转是危险的那一侧。
              本地兜底那几句(SUPERVISION_MESSAGES.*)源码里已经是繁體。 */}
          {failure}
        </div>
      )}

      {/*
        ── 证据链的展开口(W10)。**整块放在这一行卡片的最后**,这是刻意的。 ──────

        ⚠️ 展开/收起会改变行高,**这本身就是一次重排** —— 而 `list` 头注那条教训写着
        「列表不许在手指已经落下去的时候重排,这里每一颗按钮都是法律动作」。
        两条结论,都不许"顺手优化"回去:

        ① **展开的内容长在这张卡片里、就在这颗按钮的正下方**,所以**上面那些行一格都不动**,
           这一行自己的那几颗动作按钮(签发 / 否决 / 定级)也一格都不动 —— 它们全排在
           展开口**之前**。被推下去的只有它**下面**的行,而人此刻的手指正落在这一行上。
           这与 `list` 那条禁止的重排是两回事:那条禁的是**没人碰它、它自己动**
           (轮询、自动重拉、动作回执整表刷新);这一下是人自己按出来的,
           而且动的是他正盯着看的那一块。取数三态之间还会再变一次高
           (正在读 → 读到了),同理只影响它自己和下面。

        ② **允许多条同时展开,不做手风琴。** 手风琴看着整齐,但它恰恰会犯①禁止的事:
           展开第 5 行时自动收起第 2 行,于是第 3、4、5 行**整体往上跳** ——
           手指正落向第 5 行,而那一块在半路上移走了,底下换成了别人。
           「同时只开一条」省下来的那点屏幕,换的是一次可能签错文书的位移,不划算。
           顺带一个实在的好处:两条隐患的证据链能并排看(「这两条都通知过了吗」)。

        这颗按钮**不跟着 `busy` 禁用**:它只读、不写任何东西;而且动作成功之后面板会把
        这条的缓存作废、展开着的自动重拉(见 `invalidateDetail`),不存在"看着旧数据下手"。
      */}
      <div className="border-t border-gray-100 pt-2">
        <button
          type="button"
          onClick={onToggleDetail}
          aria-expanded={expanded}
          aria-controls={evidenceId}
          className="flex w-full cursor-pointer items-center gap-1.5 rounded-md px-1 py-1 text-left text-[12px] text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-700 pointer-coarse:min-h-11"
        >
          {expanded ? (
            <ChevronDown className="size-3.5 shrink-0" />
          ) : (
            <ChevronRight className="size-3.5 shrink-0" />
          )}
          {/* 措辞按「这一块里有什么」写,不写「详情」「展开」那种什么都没说的词。
              收起时也用同一句 —— 换成「收起」的话,同一颗按钮在两个状态下说的是
              两件不相干的事(一个说内容、一个说动作),而箭头已经把方向讲清楚了。 */}
          <span>已簽文書和複查記錄</span>
        </button>
        {expanded && (
          <div id={evidenceId} className="mt-2">
            <HazardEvidence state={detail} artifactBase={artifactBase} onRetry={onRetryDetail} />
          </div>
        )}
      </div>
    </div>
  );
}

/** 面板取数的三种下场。**三者必须长得不一样**,理由见 `SupervisionPanel` 里那段。 */
type LoadPhase = "loading" | "ready" | "unreadable";

/**
 * 监理处置面板 —— 界面上的常驻操作台(W10 之后它自己去取数,不再等聊天流喂)。
 *
 * ⚠️ 必须 portal 到 body,**不能就地渲染** —— 完整的层叠上下文推演在
 * checkin.tsx 的同一处注释里(输入框外壳那层 `relative z-10` 建立了层叠上下文,
 * 把弹窗关在里面,聊天顶栏反而盖在上面;窗口一矮关闭按钮就钻到顶栏底下,
 * 点不动也没有任何视觉提示)。这里的处境完全相同,别再复现一遍那个坑。
 *
 * `artifactBase` 仍然**由调用方传进来**,本文件一如既往不读 `process.env`
 * (`NEXT_PUBLIC_ARTIFACT_BASE` 那条链见 `SupervisionDocCards` 头注)。W10 之后
 * 常驻入口那条路由 `supervision-entry.tsx` 提供它 —— 它是新链路的根,
 * 与老链路的根 tool-calls.tsx 对称。
 */
export function SupervisionPanel({
  artifactBase,
  onClose,
  initialScope,
}: {
  artifactBase: string;
  onClose: () => void;
  /** 打开时先看哪一档。省略 = 「在办」(与后端缺省同一档)。 */
  initialScope?: HazardScope;
}) {
  const apiBase = useApiBase();
  const projectFilter = useProjectFilter();

  const [scope, setScope] = useState<HazardScope>(initialScope ?? HAZARD_SCOPE_ACTIVE);
  /** 手动重拉的计数器:切筛子之外的重新取数(重试 / 刷新)靠它触发。 */
  const [reloadTick, setReloadTick] = useState(0);
  const [phase, setPhase] = useState<LoadPhase>("loading");
  /** `unreadable` 时屏幕上那句话。一律是人话(后端的 user_msg 或 normalizeError 的固定中文)。 */
  const [loadMessage, setLoadMessage] = useState("");
  /**
   * 上一次拉取的整份结果 —— 只用它读**屏幕上看不到的那些数**
   * (`unassigned` / `truncated` / `total` / `today`)。屏幕上看得到的计数
   * 一律从 `list` 现算(见下面 `onScreen`)。
   *
   * 分工的理由:动作做完之后**不重拉**(见 `list` 那段),所以这份快照会渐渐变旧。
   * 让它只负责「你没看见的那部分有多少」—— 那类数天然就是「截至上次拉取」,
   * 差一条不会误导;而「这一屏有几条待确认」差一条就是错的,那种必须现算。
   */
  const [snapshot, setSnapshot] = useState<HazardListResult | null>(null);

  /**
   * 面板自己那份清单。**每次拉取整份替换,之后只由动作回执就地打补丁。**
   *
   * ⚠️ 动作成功后用 `patchHazard` / `removeHazard` 改这一份,**不许整表重拉** ——
   * 这条是从 W9 那版继承下来的教训,换了数据源之后结论一个字没变:
   * 面板开着的时候列表不许重排。人的手指已经落在某一行的按钮上了,重排就是点到
   * 别人身上,而这里每一颗按钮都是法律动作(签暂停令 = 停一片人的工,
   * 否决 = 把一行从台账里删掉)。
   * (W9 那版的原话是「别改成用 useEffect 持续跟着 props 合并」,讲的是同一件事的
   *  另一种犯法:那时 props 每次渲染都是新引用,跟着合并 = setState → 重渲染 →
   *  新引用 → 再 setState,一个不会停的循环。props 已经没了,但「别让列表自己动」
   *  这条留着。)
   *
   * 唯一会重排的两处都是**用户主动要的**:切筛子、点刷新。那两处会先进 `loading`
   * 把整张列表撤下去,不存在「手指落着而底下换人」的窗口。
   */
  const [list, setList] = useState<HazardBrief[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  /** 本次面板里已经出稿的文书,**只增不减**:一次动作出的三份必须一直看得见,
   *  下一次动作不许把它们冲掉(人还没来得及点开就没了 = 又一次「点不开」)。 */
  const [docs, setDocs] = useState<SupervisionDoc[]>([]);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [armed, setArmed] = useState<{ hazardNo: string; action: DisposalAction; at: number } | null>(
    null,
  );
  /**
   * 批量确认的举手时刻(null = 还没举手)。复用单条那套 `IssueConfirmBar`。
   *
   * 🔴 **为什么批量确认必须问一句** —— 它是这个面板上唯一一扇单向门:
   * 「确认」把隐患推到 `open`,而后端的删除是
   * `DELETE … WHERE status = 'pending'` —— 只有还没确认的才删得掉。
   * 也就是说这一下**亲手拆掉了唯一的逃生口**:一条识别错了的隐患
   * (白色安全帽认成没戴)从此再也删不掉,唯一出路是为一个不存在的隐患
   * 真的签一份法律文书,再拍照登记「复查合格」。
   *
   * 而在 2026-08-21 之前,这颗按钮只有一个 `disabled={busy}`,旁边就是「全選」——
   * 面板上护栏最少的一颗,后果却是不可逆的。三颗有确认条的
   * (suspend/escalate/reject)反而都是能另走流程补救的。
   */
  const [armedBatch, setArmedBatch] = useState<number | null>(null);
  /**
   * 签发人**自己报的名字**,记在 localStorage(键见 `SIGNER_NAME_STORAGE_KEY`)。
   *
   * ⚠️ **不复用打卡那个 `gyt:checkin:worker-name`**:那存的是工友自己的名字,
   * 这里要的是监理的名字。同一台手机上完全可能是两个人 —— 复用等于把签发人
   * 记成别人,而那比不记更坏:一片空白至少诚实。
   *
   * 🔴 **它不是认证身份**(这套系统只有一把共享口令、没有角色),
   * 界面上那行小字必须把这一点说出来,别让人以为系统在验身份。
   */
  const [signerName, setSignerName] = useState("");
  useEffect(() => {
    // 挂载后再读:这个面板是点开才挂载的,不存在 SSR/hydration 顾虑,
    // 但读 localStorage 仍然放进 effect —— 与 checkin.tsx 同一个姿势。
    try {
      setSignerName(window.localStorage.getItem(SIGNER_NAME_STORAGE_KEY) ?? "");
    } catch {
      // 隐私模式下 localStorage 会抛。记不住名字不该让面板打不开。
    }
  }, []);
  const rememberSigner = useCallback((name: string) => {
    setSignerName(name);
    try {
      window.localStorage.setItem(SIGNER_NAME_STORAGE_KEY, name);
    } catch {
      // 同上:记不住就算了,这一次的签发照样带得上名字。
    }
  }, []);

  const [forms, setForms] = useState<Record<string, FormState>>({});
  const [failures, setFailures] = useState<Record<string, string>>({});
  /**
   * 证据链展开着的那些隐患编号。**可以同时展开多条**(不做手风琴),
   * 完整理由在 `HazardRow` 里展开口那段注释 —— 一句话:手风琴会让人手指落向的那一行
   * 在半路上往上跳。
   */
  const [expandedDetails, setExpandedDetails] = useState<string[]>([]);
  /**
   * 已经拉过的详情,键是隐患编号。**收起不清它**(再展开就不用等一次网络),
   * 但**动作成功之后必须把那一条删掉**(见 `invalidateDetail`)。
   * 键不存在 = 没缓存,下面那个对账 effect 会去补一发。
   */
  const [details, setDetails] = useState<Record<string, DetailState>>({});
  /**
   * 每条隐患当前在飞的那一发详情请求。用途有二:
   *   ① 同一条又要拉一次(重试 / 动作后失效)时,把上一发掐掉;
   *   ② **认领**:回来的结果只有在这个表里还挂着自己那个 controller 时才算数 ——
   *      abort 只能掐掉还没回来的,已经进到 `await res.text()` 之后那一发照样会走完,
   *      不认领的话旧结果会盖掉新结果(表现是重试之后又跳回原来那句错)。
   */
  const detailFetchesRef = useRef<Map<string, AbortController>>(new Map());

  /**
   * **整屏共用的**那张复查照片,三态见 `PhotoUpload`(2026-08-16 从「每条一份」改过来)。
   *
   * ⚠️ 它**不是** `forms[编号].photo` 的替身:提交时发哪个编号仍然由
   * `effectivePhotoId(form.photo, sharedPhoto)` 一处算出来 —— 行内填了以行内为准,
   * 没填才回落到这一张。并存两份「提交用编号」的下场是它们迟早漂开:
   * 界面按这一份画绿卡,而提交发的是那一份,两处看着都对。
   * 这里只管「传的过程长什么样」和「传成了拿到哪个编号」。
   *
   * ⚠️ **不在上传成功那一刻把编号写进每一行的 `forms`** —— 那是一次快照,
   * 之后新出现的可复查行(切筛子、刚判了不合格、别的隐患刚签完通知单)就是空的,
   * 而照片明明还摆在屏幕顶上。理由整段在 `effectivePhotoId` 头注。
   */
  const [sharedPhoto, setSharedPhoto] = useState<PhotoUpload | undefined>(undefined);
  /** 共用那一发上传的 controller。认领机制同 `detailFetchesRef`(见它的头注)。 */
  const sharedPhotoFetchRef = useRef<AbortController | null>(null);
  /**
   * 共用那张本地预览图的 objectURL。
   *
   * 🔴 **必须用 ref 记,不能只靠 state 收尾。** revoke 要在卸载时做,而卸载时
   * 清理函数闭包里的 state 是**挂载那一刻**那份(空的)—— 靠 state 收尾等于
   * 一张都不 revoke,面板反复开关就一直往内存里堆图(每张几 MB)。
   *
   * 🔴 revoke 一律在**事件处理里**做(`swapSharedPreview`),不在 setState 的更新函数
   * 里做:更新函数允许被 React 重复调用(严格模式、并发渲染),而 revoke 是一次性
   * 副作用,放进去就可能把还在显示的那张吊销掉 —— 表现是缩略图突然变成碎图,
   * 而且只在某些渲染时序下复现,查起来极费劲。
   */
  const sharedPreviewRef = useRef<string | null>(null);
  /**
   * 每条隐患**上一次登记复查用掉的那个编号**。
   *
   * 🔴 它是「共用照片不清场」的配套闸,完整推演在 supervision-lib 的
   * `reinspectBlocker` 头注。一句话:改成共用之后不能再照行清照片(清了就是
   * 「登记完第一条,后面几条的照片全没了」),但判「不合格」的那一条会回到可复查,
   * 上面还挂着刚才那张 —— 让它再用一次等于拿**上一轮**的照片当这一轮
   * 「整改后」的证据,而屏幕上一切正常。记下用过哪张,那一条就得先换一张新的。
   *
   * 只记最近一次,不记全部历史:它挡的是「刚登记完、照片还挂着,顺手又给同一条
   * 点了一次」这一幕 —— 那是共用带来的唯一新增误用路径。
   */
  const [usedPhotos, setUsedPhotos] = useState<Record<string, string>>({});

  /**
   * 繁體字典**预热** —— 面板一挂载就让它上路,而不是等第一条文本渲染时才拉。
   *
   * 面板打开的下一件事就是渲染一屏后端来的中文(状态、级别、违规项、期限),
   * 不预热的话那些字会先以简体闪一下、字典到位后再变成繁體。差别只有一瞬,
   * 但这一屏底下每颗按钮都是法律动作,字在眼皮底下变形会让人怀疑自己看错了行。
   *
   * 🔴 预热放在**面板**里、不放在常驻入口(supervision-entry.tsx)上:
   * 那颗按钮是首屏就挂着的,在那儿预热 = 每个用户开站就拉 438 KB 字典,
   * 包括从不开这个面板的简体工友。字典必须跟着面板走。
   */
  useEffect(() => {
    ensureHantConverter();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  /** 关面板时把还在飞的详情请求全掐掉 —— 结果已经没人要了。 */
  useEffect(() => {
    const inFlight = detailFetchesRef.current;
    return () => {
      for (const controller of inFlight.values()) controller.abort();
      inFlight.clear();
    };
  }, []);

  /**
   * 关面板时:掐掉还在飞的上传,并把本地预览图 revoke 掉。
   *
   * 不 revoke 的话那张现场照片(几 MB)会一直占着内存,而面板是反复开关的 ——
   * 一个下午下来就是几百 MB,表现是浏览器越用越卡,没人会把它跟这个面板联系起来。
   * (共用之后只剩一张要收,但这一步一样不能省:反复开关照样会堆。)
   */
  useEffect(() => {
    return () => {
      sharedPhotoFetchRef.current?.abort();
      sharedPhotoFetchRef.current = null;
      if (sharedPreviewRef.current) URL.revokeObjectURL(sharedPreviewRef.current);
      sharedPreviewRef.current = null;
    };
  }, []);

  /**
   * 换掉共用那张本地预览图,顺手 revoke 旧的那张(传 null = 只清不换)。
   *
   * 单独抽出来是因为「什么时候该 revoke」有两个来源(换一张、关面板),
   * 写在一处才不会漏 —— 而漏掉不会有任何报错,只是内存慢慢涨。
   * ⚠️ 复查登记成功**不再**是它的来源之一:共用那张登记完照旧留着
   * (见 `usedPhotos` 头注),清掉就是「登记完第一条,后面几条的照片全没了」。
   */
  const swapSharedPreview = useCallback((url: string | null) => {
    const previous = sharedPreviewRef.current;
    if (previous && previous !== url) URL.revokeObjectURL(previous);
    sharedPreviewRef.current = url;
  }, []);

  /**
   * 共用那张照片的**对外形状** —— 交给 lib 那几个判据用(它们不认 `PhotoDraft`
   * 那些浏览器对象,只认「有没有编号 / 现在是哪一档」)。
   *
   * 🔴 `photoId` 只在 `done` 这一档存在,这条由类型钉着(判别联合,见
   * `SharedPhotoState` 头注)—— 「正在传却已经有编号」那种组合根本拼不出来,
   * 而它拼出来的后果是人在照片还没传完时就点得动「合格」,那一下不可撤销。
   */
  const sharedPhotoState = useMemo<SharedPhotoState>(() => {
    if (!sharedPhoto) return { phase: "none" };
    if (sharedPhoto.phase === "uploading") return { phase: "uploading" };
    if (sharedPhoto.phase === "failed") return { phase: "failed" };
    return { phase: "ready", photoId: sharedPhoto.photoId };
  }, [sharedPhoto]);

  /**
   * 拉一屏隐患。**这是整个面板唯一的取数口**(动作回执只打补丁,不重拉)。
   *
   * 触发条件就是依赖数组那四项:开面板(挂载)、切筛子、切工地、点刷新/重试。
   * 每次都先退回 `loading` 把旧列表撤下去 —— 不许「旧清单挂着、底下悄悄换成新的」,
   * 那正是 `list` 头注要防的那种重排。
   *
   * ⚠️ **三种下场必须分开画**,这是本次改动最要紧的一条:
   *   · loading    —— 正在读
   *   · unreadable —— 读不出来(网络断 / 401 / 接口没开通 / 信封形状不对)
   *   · ready 且 0 条 —— 台账里这一档真的没有
   * 后两种在屏幕上长一样的话,监理看到一片空白会直接收工 —— 而真相可能是
   * 后端根本没连上。`parseHazardListEnvelope` 在「hazards 不是数组」时特地回
   * `ok:false` 而不是空清单,为的就是让这里能分开说(它自己的头注写了这条)。
   *
   * ⚠️ 下面第一句 `setPhase("loading")` 会被 eslint 的 `react-hooks/set-state-in-effect`
   * 报一条 **warning**(`pnpm build` 里看得到,不是 error)。**别为了消掉它把这句删了**:
   * 删了的结果是切筛子/点刷新时旧清单原地挂着、新数据回来才悄悄换掉 —— 那正是
   * 上面 `list` 头注禁止的「手指落着而底下换人」。仓里 checkin.tsx 与
   * useMediaQuery.tsx 有同一条 warning,同样是刻意的。
   */
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setPhase("loading");
    // 举手状态和勾选跨清单没有意义:编号可能压根不在新的一屏里。
    // 上一屏的失败原因同理(它说的是对着旧快照做的那次动作)。
    // `forms` / `sharedPhoto` / `usedPhotos` 刻意**不清**:那是人一个字一个字打进去
    // 的期限、和刚爬上去拍回来的那张照片,切一下筛子就没了很气人(照片还得重拍一趟)。
    // `forms` 与 `usedPhotos` 都按隐患编号存,换一屏之后不属于这一屏的那些只是暂时
    // 不显示,不会串行;共用那张本来就是整屏一份,**跨筛子继续管用是它该有的样子**
    // ——「在办」里判完一条,切到「全部」接着判另一条,用的还是同一趟拍的那张照片。
    setArmed(null);
    setSelected([]);
    setFailures({});
    setBanner(null);
    void (async () => {
      try {
        const apiKey = getApiKey();
        const res = await fetch(hazardListUrl(apiBase, { scope, projectId: projectFilter }), {
          headers: apiKey ? { "x-api-key": apiKey } : undefined,
          signal: controller.signal,
        });
        const bodyText = await res.text();
        if (cancelled) return;
        if (!res.ok) {
          setLoadMessage(
            normalizeError(res.status, res.headers.get("content-type"), bodyText).message,
          );
          setPhase("unreadable");
          return;
        }
        // 这一支一个错都不抛(查询档的解析器刻意如此:面板每次打开都走这条路,
        // 抛出去就是整个操作台白屏,而白屏之后连「重试」按钮都没有)。
        const result = parseHazardListEnvelope(bodyText);
        if (!result.ok) {
          // 200 但信封说 ok:false —— 后端那句人话是屏幕上唯一的线索,原样上屏。
          setLoadMessage(result.userMsg || SUPERVISION_MESSAGES.badEnvelope);
          setPhase("unreadable");
          return;
        }
        setSnapshot(result);
        setList(result.hazards);
        // 🔴 「待签发复工令」这一档**证据链默认展开**(2026-08-17 真人反馈)。
        //
        // 那一档的行里只剩一颗「签发工程复工令」,而**签复工令是法律动作,它的依据
        // 就是那张复查合格的照片**。照片折在「已签文书和复查记录」里的话,人是在
        // 看不见证据的情况下点签发的 —— 方向反了。
        //
        // 只对这一档做,别推广到别的状态:其余几档的下一步动作(定级、签通知单、
        // 登记复查)靠的是行里已有的信息,默认展开只会把清单撑长,而清单太长本身
        // 就是这一轮在治的毛病。
        //
        // 用**并集**而不是覆盖:人手动展开的那几条不能因为切一次筛子就被收起来。
        // 代价是每条 resuming 各一发详情请求(对账 effect 去拉)—— 这一档很窄
        //(停过工 + 复查已合格 + 就差一份复工令),真机上通常一两条。
        setExpandedDetails((prev) => {
          const 待签复工 = result.hazards
            .filter((h) => h.status === "resuming")
            .map((h) => h.hazard_no)
            .filter((no) => !prev.includes(no));
          return 待签复工.length > 0 ? [...prev, ...待签复工] : prev;
        });
        setPhase("ready");
      } catch {
        // 走到这里的有两种:真的网络异常,和面板关闭/切筛子时的 abort。
        // 后者组件已经不要这个结果了,靠 cancelled 挡掉 —— 不挡的话切一下筛子
        // 就会闪一句「连不上服务器」,而其实是自己取消的。
        if (cancelled) return;
        setLoadMessage(SUPERVISION_MESSAGES.network);
        setPhase("unreadable");
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [apiBase, scope, projectFilter, reloadTick]);

  /** 重新拉一屏(错误态的「重试」与标题栏的「刷新」共用这一颗)。 */
  const reload = useCallback(() => setReloadTick((n) => n + 1), []);

  /**
   * 把某一条的详情缓存作废,并掐掉它那一发在飞的请求。
   *
   * 🔴 **动作成功之后必须调它。** 不调的下场是:刚签完暂停令,展开证据链还显示
   * 「这条还没签过任何文书」—— 监理据此认为文书没出来,回头再签一份,
   * 同一件事出两份法律文书,而两份的编号、日期都不一样,事后没人说得清哪份作数。
   *
   * 只删缓存、不直接重拉:下面那个对账 effect 会看到「展开着却没缓存」再补一发。
   * 好处是**收着的那些条不会白白打一发网络** —— 它们下次展开时才拉,拿到的还是新的。
   */
  const invalidateDetail = useCallback((hazardNo: string) => {
    detailFetchesRef.current.get(hazardNo)?.abort();
    detailFetchesRef.current.delete(hazardNo);
    setDetails((prev) => {
      if (!(hazardNo in prev)) return prev; // 没缓存就别造新引用,省一次重渲染
      const next = { ...prev };
      delete next[hazardNo];
      return next;
    });
  }, []);

  /**
   * 拉一条隐患的详情 + 证据链。三态与清单那条口径完全一致(loading / unreadable / ready)。
   *
   * 「读不出来」与「真的没有」在这里比清单那侧更要命:清单空了监理最多以为没活干,
   * 而证据链空了他会以为**文书没签出来**。所以 `HazardEvidence` 的两句话在颜色、
   * 图标、措辞上全不一样,而这里负责把两种情况**真的分开报**给它。
   *
   * 认领机制(为什么不是简单的 cancelled 标志):abort 只掐得掉还没回来的那一发,
   * 已经进到 `await res.text()` 之后的照样会走完。所以每一发把自己的 controller
   * 挂进 `detailFetchesRef`,回来时先看那张表里还是不是自己 —— 不是就悄悄退场。
   * 少了这一道,重试之后旧结果会盖掉新结果(屏幕上表现为「点了重试又跳回原来那句错」)。
   */
  const loadDetail = useCallback(
    async (hazardNo: string) => {
      detailFetchesRef.current.get(hazardNo)?.abort();
      const controller = new AbortController();
      detailFetchesRef.current.set(hazardNo, controller);
      /** 这一发还算不算数 —— 见上面「认领机制」。 */
      const mine = () => detailFetchesRef.current.get(hazardNo) === controller;

      setDetails((prev) => ({ ...prev, [hazardNo]: { phase: "loading" } }));
      const land = (state: DetailState) => {
        if (!mine()) return;
        detailFetchesRef.current.delete(hazardNo);
        setDetails((prev) => ({ ...prev, [hazardNo]: state }));
      };

      try {
        const apiKey = getApiKey();
        const res = await fetch(hazardDetailUrl(apiBase, hazardNo), {
          headers: apiKey ? { "x-api-key": apiKey } : undefined,
          signal: controller.signal,
        });
        const bodyText = await res.text();
        if (!res.ok) {
          land({
            phase: "unreadable",
            message: normalizeError(res.status, res.headers.get("content-type"), bodyText).message,
          });
          return;
        }
        const result = parseHazardDetailEnvelope(bodyText);
        if (!result.ok || !result.hazard) {
          // 200 但信封说 ok:false(或者形状认不出)—— 后端那句人话是唯一线索,原样上屏。
          land({ phase: "unreadable", message: result.userMsg || SUPERVISION_MESSAGES.badEnvelope });
          return;
        }
        // 先取出来再用:`land` 是个闭包调用,TS 会因此丢掉上面那个 guard 的收窄
        // (它没法证明这次调用不会改到 result)。
        const 权威 = result.hazard;
        land({ phase: "ready", detail: 权威, userMsg: result.userMsg });
        // 顺手把清单里那一行也校准成权威值。
        //
        // 🔴 动作回执**只带得回 status / grade / needs_grading**(data 形状是冻结的,
        // 见 supervision_api 头注),而签发同时会写下 `due_date` —— 于是 patchHazard
        // 之后那一行还挂着旧的期限字段。2026-08-16 手工验实测:签完暂停令、
        // 库里 due_date=2026-08-19,而屏幕上那行仍写着「还没下过整改期限」。
        // 监理据此以为期限没定上,会去再签一次 —— 又是两份法律文书。
        //
        // 详情端点本来就要拉(动作成功后缓存必须作废),顺带把这几个字段接过来,
        // 比给动作回执加字段更省:少一处契约、少一处能漂的地方。
        // `patchHazard` 找不到编号会原样返回,所以这一行对已经被移出清单的隐患是安全的。
        setList((prev) =>
          patchHazard(prev, hazardNo, {
            status: 权威.status,
            status_display: 权威.status_display,
            grade: 权威.grade,
            severity: 权威.severity,
            needs_grading: 权威.needs_grading,
            due_date: 权威.due_date,
            due_display: 权威.due_display,
            overdue: 权威.overdue,
          }),
        );
      } catch {
        // 两种:真网络异常,和收起/关面板/重试时自己 abort 的。
        // 后者已经不在表里了,`mine()` 会挡掉 —— 不挡的话收一下再展开会闪一句「连不上」。
        land({ phase: "unreadable", message: SUPERVISION_MESSAGES.network });
      }
    },
    [apiBase],
  );

  /**
   * 展开 / 收起某一条的证据链。**收起不清缓存**(再展开不用等网络),
   * 只有动作成功那条路才作废(`invalidateDetail`)。
   */
  const toggleDetail = useCallback((hazardNo: string) => {
    setExpandedDetails((prev) =>
      prev.includes(hazardNo) ? prev.filter((no) => no !== hazardNo) : [...prev, hazardNo],
    );
  }, []);

  /**
   * 对账:展开着却没缓存的,补一发。
   *
   * 做成 effect 而不是在 `toggleDetail` 里直接拉,是因为「该不该拉」有**三个**来源
   * (展开、重试作废后、动作成功作废后),写在一处才不会漏掉某一条路 ——
   * 而漏掉的表现是「展开了一直转圈」,没有任何报错。
   *
   * 不会打转:`loadDetail` 第一件事就是把 `loading` 写进 `details`,键立刻存在,
   * 这个 effect 再跑一遍会跳过它。
   */
  useEffect(() => {
    for (const hazardNo of expandedDetails) {
      if (!(hazardNo in details)) void loadDetail(hazardNo);
    }
  }, [expandedDetails, details, loadDetail]);

  /** 关面板时掐掉所有还在飞的详情请求 —— 组件都没了,回来的结果只会打进空气里。 */
  useEffect(() => {
    const inFlight = detailFetchesRef.current;
    return () => {
      for (const controller of inFlight.values()) controller.abort();
      inFlight.clear();
    };
  }, []);

  /** 重试某一条的详情:作废 + 让对账 effect 去补。 */
  const retryDetail = useCallback(
    (hazardNo: string) => invalidateDetail(hazardNo),
    [invalidateDetail],
  );

  const pending = useMemo(() => pendingHazards(list), [list]);
  /**
   * 屏幕上那一行计数 —— **一律从 `list` 现算**,不读快照。
   * 快照里的 pending/overdue 是「拉取那一刻」的数,而人刚确认完三条,
   * 表头还写着老数字,看着就像点了没生效。
   */
  const onScreenOverdue = useMemo(() => list.filter((h) => h.overdue).length, [list]);
  const selectablePending = useMemo(
    () => selected.filter((no) => pending.some((h) => h.hazard_no === no)),
    [selected, pending],
  );
  /**
   * 这一屏有没有可复查的隐患 —— **那一格照片要不要出现,就看它**。
   *
   * 一条都没有的时候摆一个上传框是纯噪音:人会以为哪儿漏了一步、或者去猜它管什么。
   * 判据用 `reinspectableHazards`(内部就是 `availableActions(h).includes("reinspect")`),
   * 不在这儿另比状态 —— 那张表是服务端四道闸在界面上的唯一投影。
   */
  const reinspectable = useMemo(() => reinspectableHazards(list), [list]);
  /**
   * 「下面 3 条隐患的复查结论都用这张」这句话。**传好之前和传好之后数的不是同一个数。**
   *
   * · 还没传成(没传 / 在传 / 传失败)—— 数的是**这一屏能复查的那几条**,
   *   它是一句承诺:「你拍的这张会管住下面这几条」。这一格只在至少有一条时才渲染,
   *   所以这里不会出现 0;
   * · 传好了 —— 数的是它**此刻真正管着**的那几条。🔴 **不是 `reinspectable.length`**:
   *   填了自己编号的、以及刚拿这张登记过复查的,都已经不归它管了。数大了的方向最坏
   *   —— 监理照着这句以为「那几条我都传过照片了」,而其中一条的证据其实来自别处。
   *   三道筛子在 `hazardsUsingSharedPhoto` 里。
   */
  const sharedCoverage = useMemo(() => {
    if (sharedPhotoState.phase !== "ready") {
      return describeSharedPhotoCoverage(reinspectable.length);
    }
    const covered = hazardsUsingSharedPhoto(
      list,
      // `forms` 里还带着期限那一格,这里只把照片编号那一维抽出来给 lib ——
      // lib 是零依赖纯 TS,不该认识面板的表单形状。
      Object.fromEntries(Object.entries(forms).map(([no, f]) => [no, f.photo])),
      usedPhotos,
      sharedPhotoState,
    );
    return describeSharedPhotoCoverage(covered.length);
  }, [list, reinspectable, forms, usedPhotos, sharedPhotoState]);

  const setForm = useCallback((hazardNo: string, patch: Partial<FormState>) => {
    setForms((prev) => ({ ...prev, [hazardNo]: { ...(prev[hazardNo] ?? EMPTY_FORM), ...patch } }));
  }, []);

  /** 把某条隐患上一次动作的失败原因挂上去 / 摘掉。message 一律是后端的人话,原样上屏。 */
  const setFailure = useCallback((hazardNo: string, message: string | null) => {
    setFailures((prev) => {
      const next = { ...prev };
      if (message) next[hazardNo] = message;
      else delete next[hazardNo];
      return next;
    });
  }, []);

  /**
   * 选了一张复查照片 → **立刻直传**,拿回 32 位编号存进 `sharedPhoto`(整屏一份)。
   *
   * ── 上传时机:选完就传,不等点「合格」──────────────────────────────
   * 三条理由,按分量排:
   *   ① 传失败必须在**点结论之前**就让人知道。等点完「合格」才发现照片没上去,
   *      那一下已经是法律动作,而人以为隐患已经销项了;
   *   ② 「合格 / 不合格」那两颗是不可撤销的动作,点下去到出结果之间越短越好 ——
   *      把几秒的上传塞进那个窗口里,人会以为是签发本身在卡;
   *   ③ 传完才有编号,而有没有编号决定那两颗能不能点(见 `reinspectBlocker`)。
   *      不先传的话,那两颗按钮在点之前无法判断能不能点,只能一律亮着。
   *
   * ── 为什么**不**占面板那个 `busy` 闸 ─────────────────────────────────
   * `busy` 的语义是「同一时刻只允许一个**法律动作**在飞」(签发 / 定级 / 销项)。
   * 上传不是法律动作:它只把一张图存进产物库,一个台账状态都不改,失败了重来一次
   * 也没有任何副作用。占了 `busy` 的代价是实打实的 —— 工地 4G 传一张 3MB 的照片
   * 要好几秒,这几秒里**整个操作台是死的**:别的行连「签发通知单」都点不动,
   * 而那两件事互不相干。
   *
   * 不占就得自己防连点,两道:
   *   · **整屏只一发**(共用之后连键都不用了):再选一张会先 abort 上一发 +
   *     认领机制挡掉迟到的结果,不会出现「传了两张、用的是先回来那张」;
   *   · 传的期间 `sharedPhoto.phase === "uploading"`,`reinspectBlocker` 据此把所有
   *     可复查行的「合格 / 不合格」判成点不动,不存在「传到一半就销项」。
   *     ⚠️ 这一道**不能再靠「选图那一下清空 `forms[编号].photo`」** —— 共用之后
   *     那一格是**行内逃生口**,清它等于把人手填的编号抹掉,而那条路跟这张照片无关。
   * 有法律动作在飞时(`busy`)选图入口是禁用的,那是在 `SharedReinspectPhotoField` 里
   * 按 `busy` 关掉的 —— 与行里那个期限输入框同一条规矩:下一个动作可能就是拿这张
   * 照片去销项,换图换到一半会让人分不清提交的到底是哪张。
   */
  const pickSharedPhoto = useCallback(
    async (file: File) => {
      const draft: PhotoDraft = {
        file,
        previewUrl: URL.createObjectURL(file),
        label: describePhotoFile(file),
      };
      swapSharedPreview(draft.previewUrl);

      // 🔴 **先把旧的那一份状态换掉,再开始传**(下面 `setSharedPhoto` 那两处)。
      //    改成共用之前这里还要清 `forms[编号].photo`,防的是这一幕:
      //    A 张传成功、换成 B 张、B 传失败 —— 编号里还是 A,而屏幕上摆着 B 的缩略图
      //    和一句「这张还没传上去」,人索性点了「合格」,证据链里挂的是 A。
      //    共用之后这一幕由**类型**挡住:编号只存在于 `phase === "done"` 那一档
      //    (`SharedPhotoState` 是判别联合),一进 uploading / failed 就没有编号可用,
      //    根本不需要另外去清一个字段 —— 而 `forms[编号].photo` 现在是行内逃生口,
      //    清它反而会抹掉人手填的东西。
      //
      // 本地预检:大小 / 明摆着不是图。省一次白传 —— 工地 4G 传 12MB 要十几秒,
      // 等完再被 413 拒是最气人的一种失败。**真正说了算的仍是后端**(魔数 + 413),
      // 这两条只认它们有把握的那两类,判据与理由在 supervision-lib 那两个函数头注。
      const problem = photoSizeProblem(file) ?? photoTypeProblem(file);
      if (problem) {
        setSharedPhoto({ phase: "failed", draft, message: problem });
        return;
      }

      sharedPhotoFetchRef.current?.abort();
      const controller = new AbortController();
      sharedPhotoFetchRef.current = controller;
      /** 这一发还算不算数 —— 认领机制,理由原样见 `loadDetail` 里那段。 */
      const mine = () => sharedPhotoFetchRef.current === controller;
      /** 落地:先认领再写状态。 */
      const land = (state: PhotoUpload) => {
        if (!mine()) return;
        sharedPhotoFetchRef.current = null;
        setSharedPhoto(state);
      };

      setSharedPhoto({ phase: "uploading", draft });
      try {
        const apiKey = getApiKey();
        const res = await fetch(supervisionPhotoUrl(apiBase), {
          method: "POST",
          headers: {
            // 直传**原始字节**(不是 multipart、不是 JSON)。报上文件自己的类型只是
            // 礼貌:后端按**魔数**判,浏览器给的 MIME 不可靠(DXF 那条线证过一次)。
            // 拿不到类型就给 application/octet-stream —— 留空的话有的代理会自己补一个,
            // 补成什么不由我们说了算。
            "content-type": file.type || "application/octet-stream",
            ...(apiKey ? { "x-api-key": apiKey } : {}),
          },
          body: file,
          signal: controller.signal,
        });
        const bodyText = await res.text();
        if (!res.ok) {
          // 后端的人话原样上屏(413「照片太大了…」、400「不是照片文件」都写得很具体);
          // 接口还没开通那种 404 由 normalizeError 说成「请管理员确认后端版本」。
          land({
            phase: "failed",
            draft,
            message: normalizeError(res.status, res.headers.get("content-type"), bodyText).message,
          });
          return;
        }
        const result = parsePhotoEnvelope(bodyText);
        if (!result.ok || !result.photoId) {
          // 200 但信封说 ok:false,或者编号形状认不出 —— 两种都不许画成「传好了」,
          // 理由在 parsePhotoEnvelope 头注(绿卡 + 空编号 = 下一步被自己人拦住)。
          land({ phase: "failed", draft, message: result.userMsg || PHOTO_MESSAGES.badEnvelope });
          return;
        }
        land({ phase: "done", draft, photoId: result.photoId });
      } catch {
        // 两种:真网络异常,和换图/关面板时自己 abort 的。后者已经不是当前那一发,
        // `mine()` 会挡掉 —— 不挡的话换一张图就会闪一句「连不上服务器」。
        land({ phase: "failed", draft, message: SUPERVISION_MESSAGES.network });
      }
    },
    [apiBase, swapSharedPreview],
  );

  /**
   * 重传上一次那张(不用重新选图)。
   *
   * 失败之后最常见的下一步就是原地再试一次 —— 逼人重拍的话,工地上那个部位
   * 可能已经不方便再爬上去了。走的还是 `pickSharedPhoto`,所以预检、认领
   * 那几道一样都不少。
   */
  const retrySharedPhoto = useCallback(() => {
    const file = sharedPhoto?.draft.file;
    if (file) void pickSharedPhoto(file);
  }, [sharedPhoto, pickSharedPhoto]);

  /** 批量确认(pending → open)。**这是 D17 那道人工闸的全部实现。** */
  const confirmSelected = useCallback(async () => {
    let body: { hazard_nos: string[] };
    try {
      body = confirmBody(selectablePending);
    } catch (err) {
      setBanner({
        tone: "bad",
        text: err instanceof SupervisionContractError ? err.message : SUPERVISION_MESSAGES.noSelection,
      });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const outcome = await callSupervision(apiBase, "confirm", body);
      if (!outcome.ok) {
        setBanner({ tone: "bad", text: outcome.message });
        return;
      }
      const result = parseConfirmEnvelope(outcome.text);
      // 确认成功的那些,状态按契约推进到 open(端点只回编号,不回每条的新状态 ——
      // pending → open 是契约里唯一的那条边,推得动就一定是它)。
      setList((prev) =>
        result.confirmed.reduce((acc, no) => patchHazard(acc, no, { status: "open" }), prev),
      );
      // 状态变了 → 展开着的那块显示的是旧的(「待确认」)。逐条作废,
      // 对账 effect 会把展开着的那几条补回来,收着的等下次展开再拉。
      for (const no of result.confirmed) invalidateDetail(no);
      // 没确认成的原因逐条挂回那一行:只报「已确认 3 条」而不说另外 2 条怎么了,
      // 人会以为全成了。
      setFailures((prev) => {
        const next = { ...prev };
        for (const no of result.confirmed) delete next[no];
        for (const item of result.failed) next[item.hazard_no] = item.reason;
        return next;
      });
      setSelected((prev) => prev.filter((no) => !result.confirmed.includes(no)));
      setBanner({ tone: "ok", text: result.user_msg || `已確認 ${result.confirmed.length} 條。` });
    } catch (err) {
      setBanner({
        tone: "bad",
        text:
          err instanceof SupervisionContractError && err.message
            ? err.message
            : SUPERVISION_MESSAGES.badEnvelope,
      });
    } finally {
      setBusy(false);
    }
  }, [apiBase, selectablePending, invalidateDetail]);

  /**
   * 举手(只给要二次确认的那两个动作)。**先把必填项验一遍再举手。**
   *
   * 顺序反过来的话会出现这一幕:期限那一格还空着,屏幕上却已经弹出
   * 「暂停令是法律文书…不能撤销」,人硬着头皮点了确认,换来一句「得写明整改期限」。
   * 那样两件事都被削弱了 —— 确认框成了噪声,而真正该改的地方在别处。
   */
  const armAction = useCallback(
    (hazard: HazardBrief, action: DisposalAction) => {
      const form = forms[hazard.hazard_no] ?? EMPTY_FORM;
      try {
        actionBody(action, {
          hazardNo: hazard.hazard_no,
          duePhrase: form.due,
          // 与 `runAction` 走同一个 `effectivePhotoId` —— 两处各写一份的话,
          // 举手时验的是 A 张、真发出去的是 B 张,而中间隔着一句「不能撤销」。
          afterPhotoId: effectivePhotoId(form.photo, sharedPhotoState),
        });
      } catch (err) {
        setFailure(
          hazard.hazard_no,
          err instanceof SupervisionContractError ? err.message : SUPERVISION_MESSAGES.badEnvelope,
        );
        return;
      }
      setFailure(hazard.hazard_no, null);
      setArmed({ hazardNo: hazard.hazard_no, action, at: Date.now() });
    },
    [forms, sharedPhotoState, setFailure],
  );

  /** 单条处置:定级 / 签发 / 复查 / 复工 / 上报。**一次只飞一个请求**(busy 全局)。 */
  const runAction = useCallback(
    async (hazard: HazardBrief, action: DisposalAction, grade?: string, result?: "pass" | "fail") => {
      const form = forms[hazard.hazard_no] ?? EMPTY_FORM;
      /**
       * 这一行这次要发的照片编号。**行内填了以行内为准,没填才回落到共用那张** ——
       * 判据只在 `effectivePhotoId` 一处算,按钮灰不灰(`reinspectBlocker`)读的也是它。
       * 各算一遍的下场是「界面说用 A、请求发的是 B」,而复查合格是销项,不可撤销。
       */
      const photoId = effectivePhotoId(form.photo, sharedPhotoState);
      let body: Record<string, unknown>;
      try {
        body = actionBody(action, {
          hazardNo: hazard.hazard_no,
          duePhrase: form.due,
          afterPhotoId: photoId,
          grade,
          result,
          // 签发人自报的名字。没填就不带这个键(actionBody 自己判),
          // 后端记 NULL —— 别在这儿做成必填,理由见 supervision-lib 的 `issuedBy`。
          issuedBy: signerName,
        });
      } catch (err) {
        // 校验没过就把举手状态一并收掉:让确认条退回按钮,人先去把那一格填对。
        // 留着确认条 + 底下一行红字,看起来像「点了确认但没反应」。
        setArmed(null);
        setFailure(
          hazard.hazard_no,
          err instanceof SupervisionContractError ? err.message : SUPERVISION_MESSAGES.badEnvelope,
        );
        return;
      }
      setBusy(true);
      setArmed(null);
      setBanner(null);
      setFailure(hazard.hazard_no, null);
      try {
        const outcome = await callSupervision(apiBase, ACTION_ENDPOINT[action], body);
        if (!outcome.ok) {
          // 后端的 user_msg 已经是人话(三条硬拦、状态机拒绝那几句都写得很具体),
          // **原样上屏**,不在前端重新包装成「操作失败」。
          setFailure(hazard.hazard_no, outcome.message);
          return;
        }
        const parsed = parseActionEnvelope(outcome.text);
        // 🔴 「否决」是**删除,不是状态流转**:回执里那个 status 是 REJECTED_STATUS,
        //   它故意不在八档词表里、也没有中文名。走下面 patchHazard 那条路的话,
        //   屏幕上会冒出一个英文徽章 deleted(本仓明令不许有英文枚举值上屏),
        //   而且那一行会一直挂着 —— 库里已经没有它了,下一次刷新才发现。
        //   正确做法是把这一行从清单里拿掉(supervision-lib 的 REJECTED_STATUS 头注)。
        if (parsed.status === REJECTED_STATUS) {
          setList((prev) => removeHazard(prev, parsed.hazard_no));
          setSelected((prev) => prev.filter((no) => no !== parsed.hazard_no));
          // 那一行都没了,展开状态与详情缓存跟着收干净 —— 留着的话,同一个编号
          // 万一以后又出现(照片重传是幂等的,D14),会顶着上一条的证据链展开。
          setExpandedDetails((prev) => prev.filter((no) => no !== parsed.hazard_no));
          invalidateDetail(parsed.hazard_no);
          setBanner({
            tone: "ok",
            text: parsed.user_msg || `已把隱患 ${parsed.hazard_no} 從台賬裏刪掉。`,
          });
          return;
        }
        setList((prev) =>
          patchHazard(prev, parsed.hazard_no, {
            // 状态一律以**后端返回的**为准,不按前端的预期改 —— 复查合格落 closed
            // 还是 resuming 由 db 按 was_suspended 挑边,前端猜的话必然错一半。
            status: parsed.status,
            ...(parsed.grade ? { grade: parsed.grade } : {}),
            ...(parsed.needsGrading === null ? {} : { needs_grading: parsed.needsGrading }),
          }),
        );
        if (parsed.documents.length > 0) {
          // 🔴 补上「这份是哪条隐患的」。后端的 documents[] 只有类型和编号 ——
          //    而「本次出的文书」是**跨隐患**的汇总:连着给三条隐患签复工令,
          //    那里就是三张一模一样的「工程复工令」,只有编号不同。
          //    2026-08-17 真人反馈原话:「意义不明,缺加上什么的复工令」。
          //    身份从**这次动作的那条隐患**来(不是从回执里找,回执里没有),
          //    所以只能在这儿补 —— 补完两个调用方共用的那张卡自然就会显示。
          const 带身份 = parsed.documents.map((doc) => ({
            ...doc,
            hazard_no: parsed.hazard_no,
            hazard_item: hazard.item,
          }));
          setDocs((prev) => [...prev, ...带身份]);
        }
        // 🔴 动作之后**立刻重拉这一条的详情**,两件事一次办完:
        //   ① 证据链缓存作废 —— 刚签完暂停令、展开却写着「还没签过任何文书」的话,
        //      监理会认为没签成而再签一份,同一件事两份法律文书,编号和日期都不一样;
        //   ② 把清单那一行校准成权威值(期限、中文状态、超期与否)——
        //      动作回执带不回这些,`loadDetail` 落地时会顺手 patch(见它里头那段)。
        // **所有动作都要**,不只是出文书那几个:定级改了级别、复查加了一条记录、
        // 确认改了状态,展开的那一块与那一行都在显示旧的。
        // 收着的隐患也拉:多一发很便宜(动作本来就稀少且是人一下一下点的),
        // 而少拉的代价是那一行一直挂着过期的期限。
        void loadDetail(parsed.hazard_no);
        // 🔴 复查登记完:**照片留着,但记下这一行用掉了哪个编号。**
        //
        // 改成共用之前这里是「把这一行的照片草稿整个收干净」,防的是:判「不合格」
        // 之后这一行会再次出现「登记复查结论」,而上面还挂着上一次那张照片 ——
        // 第二次复查会拿第一次的照片当整改后的证据交上去,屏幕上一切正常。
        //
        // 共用之后**不能再照行清**:那张照片同时管着别的几条,清掉就是
        // 「登记完第一条,后面几条的照片全没了」,而人手上只有那一张。
        // 两者的共存点就是下面这一行 —— 留着照片、记下用过,那一条要再复查必须
        // 先在上面换一张新拍的(完整推演在 supervision-lib 的 `reinspectBlocker` 头注)。
        // 记的是**真发出去的那个** `photoId`,不是共用那张的编号:行内填了别的编号时
        // 用掉的是它,拿共用那张去记的话那一行会被错误地放行第二次。
        if (action === "reinspect") {
          setUsedPhotos((prev) => ({ ...prev, [hazard.hazard_no]: photoId }));
        }
        // 🔴 只有 `docLine` 这一支要过转换器,另外两支都不许过:
        //   · `parsed.user_msg` —— 后端的人话,**一律不转**(W12 复审定案,理由见
        //     banner 上屏那处:它内插了隐患编号 / 描述 / 项目名这些用户数据);
        //   · `"已完成。"`     —— 本地兜底,源码里就是繁體。
        // 而 `docLine` 是 `describeDocuments` 拿 `DOC_TYPE_ZH` 拼的 —— 那张表是
        // 后端 `core/doc_no.DOC_TITLE_ZH` 的**镜像,必须留简体**(改了就跟后端对不上),
        // 所以它只能在这儿、在拼好之后转一次。不转的表现是这一条兜底路径上
        // 冒出一句「已出稿 1 份:工程暂停令」的简体,**没有任何报错**。
        const docLine = hantSync(describeDocuments(parsed.documents));
        setBanner({
          tone: "ok",
          text: parsed.user_msg || docLine || "已完成。",
        });
      } catch (err) {
        setFailure(
          hazard.hazard_no,
          err instanceof SupervisionContractError && err.message
            ? err.message
            : SUPERVISION_MESSAGES.badEnvelope,
        );
      } finally {
        setBusy(false);
      }
    },
    [apiBase, forms, sharedPhotoState, setFailure, invalidateDetail, loadDetail, signerName],
  );

  // ── 面板这一层要上屏的字(界面恒繁體)──────────────────────────────────────
  //
  // 🔴 **必须排在下面那句 `return null` 之前** —— hooks 不许在 early return 之后调。
  //
  // · scopeLabels —— 四颗筛子按钮的字。原值 `HAZARD_SCOPES` 是**送后端的 `?scope=`
  //   受控词**(`agents/supervision/scoping.SCOPES`),词表外后端直接回 400,
  //   所以 key / aria-pressed / setScope 三处一律还用原值,只有按钮上的字用这一份。
  //   ⚠️ 「在办」「待确认」简繁不同形,「超期」「全部」同形 —— 别以为同形的那两个
  //      不用管,顺序是按下标配对的。
  // · scopeLabel  —— 空清单那句「「在辦」这一档里没有隐患」里那个词,同一个理由。
  //
  // ⚠️ `banner.text` 与 `loadMessage`(后端 user_msg 原样上屏那两条路)**不在这儿转** ——
  //    W12 复审定案:user_msg 一律不转。理由见它们各自上屏那处。
  const scopeLabels = useHantUIAll(HAZARD_SCOPES);
  const scopeLabel = useHantUI(scope);

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="監理確認與處置"
    >
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      {/* 高度上限取 min(92dvh, 100dvh-2rem):理由原样见 checkin.tsx ——
          dvh 因为手机地址栏会伸缩,减 2rem 是外层那圈 p-4,少了它在矮视口上
          居中会把顶部连同关闭按钮推出视口,而外层不滚动。 */}
      <div className="relative z-10 flex max-h-[min(92dvh,calc(100dvh-2rem))] w-full max-w-2xl flex-col gap-3 overflow-y-auto rounded-2xl bg-white p-4 shadow-xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <ClipboardCheck className="size-5 text-gray-700" />
            監理確認與處置
          </div>
          <div className="flex items-center gap-1">
            {/* 刷新会**整表重排**(见 list 头注),所以它必须是人主动点的一颗按钮,
                不能做成自动轮询 —— 手指落在「签发暂停令」上的时候底下换了人,
                这里的代价是真停一片人的工。动作做完后不自动重拉也是同一条理由。 */}
            <button
              type="button"
              onClick={reload}
              disabled={busy || phase === "loading"}
              className="flex size-7 cursor-pointer items-center justify-center rounded text-gray-500 hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:size-11"
              aria-label="重新拉一遍隱患清單"
              title="重新拉一遍(會按最新台賬重排整張清單)"
            >
              <RefreshCcw className={`size-4 ${phase === "loading" ? "animate-spin" : ""}`} />
            </button>
            {/* 触摸目标 44×44(pointer-coarse),判据不按屏宽 —— 手机横过来有 844px 宽,
                按宽度猜会退回 28px,而那时手指并没有变细(同 checkin.tsx)。 */}
            <button
              type="button"
              onClick={onClose}
              className="flex size-7 cursor-pointer items-center justify-center rounded text-gray-500 hover:bg-gray-100 pointer-coarse:size-11"
              aria-label="關閉"
            >
              <X className="size-5" />
            </button>
          </div>
        </div>

        {/* 签发人。**放在筛子上面、面板一打开就看得见** —— 它要在人点签发之前
            就设好,而不是在确认条上临时问(那时候文书已经渲染落盘了,
            见后端 `_issued_by` 的取舍)。
            🔴 那行小字必须说清「系统不验身份」:这套系统只有一把共享口令、
            没有角色,写成「签发人认证」会让人以为它验过。 */}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <label
            htmlFor="gyt-signer-name"
            className="text-[13px] whitespace-nowrap text-gray-700"
          >
            簽發人
          </label>
          <input
            id="gyt-signer-name"
            type="text"
            value={signerName}
            onChange={(e) => rememberSigner(e.target.value)}
            placeholder="你的姓名"
            maxLength={40}
            autoComplete="name"
            className="min-w-0 flex-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-[13px] text-gray-900 placeholder:text-gray-500 focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:outline-none pointer-coarse:min-h-11"
          />
          <span className="w-full text-[12px] text-gray-600">
            記在這台手機上,簽發文書時一起存進台賬。系統不驗身份,只是留個記錄。
          </span>
        </div>

        {/* 四档筛子。**顺序就是 HAZARD_SCOPES 的顺序**(受控词表,后端词表外直接 400)——
            所以这里 map 那个常量,不手写四颗按钮:手写的下场是哪天后端加一档而界面上没有,
            或者拼错一个字然后每次都 400。刻意不做自由输入框,理由同上。 */}
        <div
          role="group"
          aria-label="篩選隱患"
          className="flex flex-wrap items-center gap-1.5"
        >
          {HAZARD_SCOPES.map((s, i) => (
            <button
              key={s}
              type="button"
              // 有动作在飞的时候不许切:切了会把整张清单换掉,而那个请求的回执
              // 还要回来往清单上打补丁,补到一张已经不是它的清单上。
              disabled={busy}
              aria-pressed={s === scope}
              onClick={() => setScope(s)}
              className={`rounded-full px-3 py-1 text-[13px] font-medium ring-1 ring-inset transition-colors disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11 pointer-coarse:px-4 ${
                s === scope
                  ? "bg-gray-900 text-white ring-gray-900"
                  : "bg-white text-gray-600 ring-gray-300 hover:bg-gray-50"
              }`}
            >
              {/* 🔴 上屏的字用 `scopeLabels[i]`(繁體),而 key / aria-pressed /
                  setScope 三处一律用原值 `s` —— 它是送后端的受控词,转了后端 400。 */}
              {scopeLabels[i]}
            </button>
          ))}
          {/* 当前看的是哪个工地。**「全部工地」这一档必须说出来** —— 不说的话,
              监理默认屏幕上就是全部,而实际可能被顶栏那颗工地按钮筛过了。
              切工地在顶栏(ProjectSwitcher),这里只报状态、不给第二个入口:
              两个地方都能改同一件事,人就不知道以哪个为准。 */}
          <span className="ml-auto text-[12px] text-gray-400">
            {projectFilter === null ? "全部工地" : "只看頂欄選中的工地"}
          </span>
        </div>

        {banner && (
          <div
            aria-live="polite"
            className={
              banner.tone === "ok"
                ? "rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[13px] text-emerald-800"
                : "rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[13px] text-red-700"
            }
          >
            {/* 🔴 后端的 user_msg **原样上屏,一个字都不转**(W12 复审定案)。
                后端拼这句时会把隐患编号、隐患描述、项目名内插进去,整句转换
                在原理上分不出哪一半是系统写的字。本地兜底那几句源码里已是繁體;
                `docLine` 那一支是由 `DOC_TYPE_ZH`(受控枚举,必须留简体)拼出来的,
                所以它在**构造处**单独过了一道 hantSync —— 见 setBanner 那里。 */}
            {banner.text}
          </div>
        )}

        {/* 举了手就把整条橙条换成确认条 —— 与单条处置同一个形状(顶掉原按钮,
            不是在旁边多长一个框)。理由见 IssueConfirmBar 头注:一个不可逆的动作
            不能建在一个可能被浏览器悄悄关掉的 window.confirm 上。 */}
        {pending.length > 0 && armedBatch !== null && (
          <IssueConfirmBar
            prompt={confirmBatchPrompt(selectablePending.length)}
            confirmLabel={`確認 ${selectablePending.length} 條`}
            armedAt={armedBatch}
            onCancel={() => setArmedBatch(null)}
            onConfirm={() => {
              setArmedBatch(null);
              void confirmSelected();
            }}
          />
        )}

        {pending.length > 0 && armedBatch === null && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-amber-50 px-3 py-2">
            <div className="text-[13px] text-amber-900">
              待確認 {pending.length} 條 —— 確認之後才進正式流程(簽文書、算整改率、能被升級)。
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  // 判据用 selectablePending(已经滤掉划走/已确认的),不用 selected 的裸长度:
                  // 那里可能留着已经不在清单里的编号,拿它比会出现「显示取消全选、点了却全选上」。
                  setSelected(() =>
                    selectablePending.length === pending.length
                      ? []
                      : pending.map((h) => h.hazard_no),
                  )
                }
                className="pointer-coarse:min-h-11"
              >
                {selectablePending.length === pending.length ? "取消全選" : "全選"}
              </Button>
              <Button
                size="sm"
                disabled={busy || selectablePending.length === 0}
                // 不直接确认,先举手 —— 这一下是单向门,见 armedBatch 那段红字。
                onClick={() => setArmedBatch(Date.now())}
                className="pointer-coarse:min-h-11"
              >
                {busy ? (
                  <LoaderCircle className="mr-1 size-4 animate-spin" />
                ) : (
                  <Check className="mr-1 size-4" />
                )}
                確認選中 {selectablePending.length} 條
              </Button>
            </div>
          </div>
        )}

        {/* 这一屏有什么 —— 计数从 list 现算(见 onScreenOverdue 那段)。
            待确认那个数**不在这里重复**:它就在上面那条橙色确认条上,同一个数写两遍,
            人会先怀疑是不是两个不同的东西。
            `today` 是**后端那一侧的**香港日历日:超期是按它算的,而这台电脑的
            时钟/时区未必一样,写出来才对得上。 */}
        {phase === "ready" && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-gray-500">
            <span>
              這一屏 {list.length} 條
              {onScreenOverdue > 0 ? ` · 已超期 ${onScreenOverdue} 條` : ""}
            </span>
            {snapshot?.today && <span>期限按工地日期 {snapshot.today} 算</span>}
            {/* D11_NOTE —— 复查结论为什么不让模型下。
                2026-08-16 真人反馈「太多手续复杂」之前,这句话挂在**每一条**可复查的
                隐患行上,清单里有几条就重复几遍(实测 3 条)。而它要传达的东西
                一个人只需要知道一次:模型给建议、人下结论。
                复查照片的角度光线取景都变了,「没拍到那个部位」和「问题已消除」
                在模型眼里一样 —— 那是往「误判合格」方向错,而这一侧会死人。
                🔴 **别再挪回行里,也别删。** 删了「为什么系统不替我判」就没人回答;
                挪回行里就又变成每行讲一遍课。 */}
            {/* 判据与那一格照片同源(`reinspectable`,内部就是
                `availableActions(h).includes("reinspect")`)—— 两处各写一份的话,
                会出现「照片那一格在、这句话不在」这种说不清的组合。
                ⚠️ 共用照片之后这句话更要紧了:一张照片管好几条结论,
                「谁来判」这件事只会更容易被当成「系统替我判过了」。 */}
            {reinspectable.length > 0 && (
              <span title="複查照片的角度、光線、取景都變了,模型分不清「問題已消除」和「這張沒拍到那個部位」——那是往「誤判合格」方向錯。">
                複查結論由人來下
              </span>
            )}
          </div>
        )}

        {/* 🔴 截断必须上屏(supervision-lib 的 truncated 那条):不说的话,屏幕上是一张
            看起来完整的清单,监理据此说「就剩这些了」—— 少掉的那些一条提示都不会有。 */}
        {phase === "ready" && snapshot?.truncated && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600" />
            <div className="text-[12px] leading-snug text-amber-900">
              這一檔的隱患太多,只列出了前 {list.length} 條(台賬裏共 {snapshot.total} 條)。
              換一個篩子,或者在頂欄先選一個工地,才看得全。
            </div>
          </div>
        )}

        {/* 🔴 未归属那一批(D6):它**不过上面的筛子** —— 回答的不是「这一屏有几条」,
            而是「有没有一批隐患没人看得见」。后端的 list_hazards 工具也会显式报这一句,
            两处同一个理由:不报的话,那批隐患在界面上永远没人看见。 */}
        {phase === "ready" && (snapshot?.unassigned ?? 0) > 0 && (
          <div className="rounded-lg bg-gray-50 px-3 py-2 text-[12px] leading-snug text-gray-600">
            {projectFilter === null
              ? `上面這些裏,有 ${snapshot?.unassigned} 條還沒歸到任何工地。`
              : `另外還有 ${snapshot?.unassigned} 條隱患沒歸到任何工地,這一屏裏看不到 —— 把頂欄的工地切回「全部」才看得見。`}
          </div>
        )}

        {/* ── 这次复查的照片:整屏一格,排在清单**之前** ──────────────────────
            🔴 **只在这一屏至少有一条能复查时才出现。** 一条都没有的时候摆一个上传框
            是纯噪音:人会以为哪儿漏了一步,或者去猜它到底管什么。判据用 `reinspectable`
            (= `availableActions(h).includes("reinspect")`),与下面每一行「合格/不合格」
            那两颗按钮出不出现是同一张表 —— 分开写就会出现「有框没按钮」或者反过来。

            排在清单之前是刻意的:人的动作顺序就是「先拍照,再一条条下结论」。
            排在后面的话,前几条的按钮是灰的而解释在屏幕更下方,人得先滚下去才知道差什么。

            ⚠️ 它**不在** `phase === "ready"` 之外出现:正在读 / 读不出来的时候
            `list` 是空的,`reinspectable` 自然也空,这一格跟着不出现 —— 不用额外加判据,
            但别把 `list` 换成快照里的数,那份数是「拉取那一刻」的。 */}
        {reinspectable.length > 0 && (
          <SharedReinspectPhotoField
            upload={sharedPhoto}
            busy={busy}
            artifactBase={artifactBase}
            coverage={sharedCoverage}
            onPick={(file) => void pickSharedPhoto(file)}
            onRetry={retrySharedPhoto}
          />
        )}

        <div className="flex flex-col gap-2">
          {/* 🔴 三种下场必须长得不一样(取数那个 effect 的头注写了全部理由):
              正在读 / 读不出来 / 真的一条都没有。后两种糊成一片空白的话,
              监理看见空白就收工,而真相可能是后端根本没连上。 */}
          {phase === "loading" ? (
            <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 px-3 py-8 text-[13px] text-gray-500">
              <LoaderCircle className="size-4 animate-spin" />
              正在讀隱患台賬…
            </div>
          ) : phase === "unreadable" ? (
            <div className="flex flex-col items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-4">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-600" />
                <div className="text-[13px] leading-snug text-red-700">
                  {/* 这一句一律是人话:后端的 user_msg,或 normalizeError 那几句固定中文。
                      **绝不要写成「台账里没有隐患」** —— 那正是这个分支存在的全部意义。
                      🔴 user_msg 那一支**不转繁體**(W12 复审定案),normalizeError 那几句
                      源码里已经是繁體 —— 两支都不需要转换器。 */}
                  隱患清單沒讀出來。{loadMessage}
                </div>
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={reload}
                className="pointer-coarse:min-h-11"
              >
                <RefreshCcw className="mr-1 size-3.5" />
                重試
              </Button>
            </div>
          ) : list.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-300 px-3 py-8 text-center text-[13px] text-gray-500">
              {/* 念的是繁體那份;`scope` 原值仍是送后端的受控词,别在这儿念它。 */}
              「{scopeLabel}」這一檔裏沒有隱患。
              <br />
              換上面的篩子看別的檔;頂欄選了工地的話,也可能是這個工地下面沒有。
            </div>
          ) : (
            list.map((hazard) => (
              <HazardRow
                key={hazard.hazard_no}
                hazard={hazard}
                selected={selected.includes(hazard.hazard_no)}
                busy={busy}
                armedAction={armed?.hazardNo === hazard.hazard_no ? armed.action : null}
                armedAt={armed?.at ?? 0}
                form={forms[hazard.hazard_no] ?? EMPTY_FORM}
                failure={failures[hazard.hazard_no] ?? null}
                artifactBase={artifactBase}
                expanded={expandedDetails.includes(hazard.hazard_no)}
                detail={details[hazard.hazard_no]}
                sharedPhoto={sharedPhotoState}
                usedPhotoId={usedPhotos[hazard.hazard_no] ?? null}
                onToggleSelect={() => setSelected((prev) => toggleSelected(prev, hazard.hazard_no))}
                onFormChange={(patch) => setForm(hazard.hazard_no, patch)}
                onArm={(action) => armAction(hazard, action)}
                onDisarm={() => setArmed(null)}
                onAct={(action, grade, result) => void runAction(hazard, action, grade, result)}
                onToggleDetail={() => toggleDetail(hazard.hazard_no)}
                onRetryDetail={() => retryDetail(hazard.hazard_no)}
              />
            ))
          )}
        </div>

        {docs.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="text-sm font-medium text-gray-700">本次出的文書</div>
            <SupervisionDocCards documents={docs} artifactBase={artifactBase} />
          </div>
        )}

        {/* 「否决」到底做了什么 —— 必须写清楚。
            ⚠️ W10 之前这一段写的是「只是把这条从本清单里划掉,后端还没有删除接口」,
            那时它确实只是本地视图操作。现在 `POST /supervision/reject` 上线了,
            **它是真删**(库里整行没了)。这段话没跟着改的话,人会照着旧说明放心去点
            ——「反正只是划掉」—— 然后一条真实存在的隐患就从台账上消失了。 */}
        <div className="rounded-lg bg-gray-50 px-3 py-2 text-[11px] leading-relaxed text-gray-500">
          「否決」是給**識錯了**的隱患用的(照片裏那頂帽子其實戴着):那一行會從台賬裏
          整行刪掉,找不回來,而且只有還沒確認的隱患能這麼刪。確實是隱患的,請用「確認」。
          <br />
          不確認、也不否決,本身是安全的:待確認的隱患不算整改率、不進超期清單、不會被升級(D17)。
          <br />
          這份清單直接讀的是隱患台賬,和聊天記錄沒有關係 —— 刷新頁面、換台機器進來,
          看到的都是同一份。
        </div>
      </div>
    </div>,
    document.body,
  );
}

/**
 * 「待确认隐患」卡片 —— tool-calls.tsx 在工具结果里发现 `data.hazards` 时渲染它。
 *
 * ── 🔴 它现在**不可达**,是刻意留着的死代码(W10)──────────────────────
 * 判据(工具名在白名单 **且** `data.hazards` 存在)挂在**子 Agent 的工具返回**上,
 * 而 supervisor 的 `output_mode="last_message"` 把那份返回整个丢掉了 ——
 * 实测线程状态 0 条、历史检查点 0 条、`useStream` 也没订阅 `subgraphs`
 * (`docs/W10_界面取不到工具返回_方案.md` §1)。所以这张卡从 W9 上线起
 * **一次都没渲染出来过**,不是没人点。今天的入口是 `SupervisionEntry`。
 *
 * 为什么不删:
 *   ① `output_mode` 是可能变的(它是编排层的成本取舍,不是永久事实);哪天真出现了
 *      工具返回,这张卡就地活过来 —— 而它的价值仍在(隐患在哪张照片上发现的,
 *      就在那条消息底下处置,不用先记住编号)。
 *   ② **静默的死代码比标注过的死代码危险得多**:删了的话,下一个人看到「聊天里
 *      没有隐患卡」会以为本来就没设计过,于是重做一遍;留着而不标注的话,
 *      他会以为这条路是通的,照着改半天没反应 —— W10 之前正是后面这种。
 * 改动它的时候记住:**改完你没法在界面上验它**,今天没有任何输入能让它出现。
 *
 * `failedItems`(D10 / Codex#10 的 `failed_items`)**必须显示**:后端那一侧已经
 * 落了一行 `hazard_ingest_failures`,但工友看不到库 —— 界面不显示的话,一条真实
 * 存在的隐患就这么没了,而屏幕上一切正常(识别回执照常报了这一项)。
 * ⚠️ 这条「必须显示」现在**没有任何地方在履行**(整张卡不可达)—— 那批失败项
 * 目前只在后端日志里。它不属于 W10 的范围,但别以为界面上已经有人在管了。
 *
 * ── 繁體化(W12):这张卡**只做了源码里的静态繁體,没有接运行时转换** ──────────
 * 也就是说 `failedItems`(后端来的违规项名,8 类里 5 类简繁不同形)在这张卡上
 * 仍会显示简体。这是**刻意的取舍**,两条理由:
 *   ① 它长在**聊天流**里,不是点开才挂载的面板。接上 `useHantUI` 等于哪天这条路
 *      一通,每条隐患消息都会触发 438 KB 字典的懒加载 —— 而那条「简体工友一个
 *      字节都不下」是刚实测过的承诺;
 *   ② 它**不可达**,接了也没法在界面上验(本组件头注最后一句就是这个意思),
 *      而接一段验不了的转换逻辑,下一个人只会以为这里已经管好了。
 * 🔴 哪天让这张卡活过来,**这一段要一起重做**:那时 failedItems / 徽章上的字
 * 都得过转换,而底下这颗按钮点开的 `SupervisionPanel` 里已经转了 ——
 * 一半繁體一半简体比全简体更像坏了。
 */
export function HazardIntakeCard({
  hazards,
  failedItems,
  artifactBase,
}: {
  hazards: readonly HazardBrief[];
  failedItems: readonly string[];
  artifactBase: string;
}) {
  const [open, setOpen] = useState(false);
  if (hazards.length === 0 && failedItems.length === 0) return null;

  const pendingCount = pendingHazards(hazards).length;
  const needGrading = hazards.filter((h) => h.needs_grading).length;
  const severe = hazards.filter((h) => h.grade === GRADE_SEVERE).length;

  return (
    <div className="mx-auto w-full max-w-3xl">
      <div className="my-2 flex items-start gap-3 rounded-xl border border-blue-200 bg-blue-50/40 px-4 py-3">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-blue-600" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-medium text-gray-900">隱患台賬</span>
            <span className="text-[13px] text-gray-600">
              {hazards.length > 0 ? `已登記 ${hazards.length} 條` : "本次沒有登記成功的隱患"}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-gray-600">
            {pendingCount > 0 && (
              <Chip tone="bg-amber-50 text-amber-700 ring-amber-200">待確認 {pendingCount} 條</Chip>
            )}
            {severe > 0 && (
              <Chip tone="bg-red-50 text-red-700 ring-red-200">嚴重 {severe} 條</Chip>
            )}
            {needGrading > 0 && (
              <Chip tone="bg-fuchsia-100 text-fuchsia-800 ring-fuchsia-300">
                需人工定級 {needGrading} 條
              </Chip>
            )}
          </div>

          {failedItems.length > 0 && (
            <div className="mt-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-2">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-600" />
              <div className="text-[12px] leading-snug text-red-700">
                有 {failedItems.length} 項沒能記進台賬:{failedItems.join("、")}。
                這幾項不會進整改流程,請人工補記或找管理員查日誌。
              </div>
            </div>
          )}

          {hazards.length > 0 && (
            <div className="mt-2.5">
              <Button
                size="sm"
                onClick={() => setOpen(true)}
                className="pointer-coarse:min-h-11"
              >
                <ClipboardCheck className="mr-1 size-4" />
                {pendingCount > 0 ? `去確認(${pendingCount} 條待確認)` : "打開監理處置"}
              </Button>
            </div>
          )}

          <div className="mt-1.5 text-[11px] text-gray-400">
            自動登記的隱患都是「待確認」:要有人確認過,才會進簽文書 / 算整改率 / 能被升級的正式流程。
          </div>
        </div>
      </div>
      {open && (
        // 面板不再收 `hazards` —— 它自己去 GET /supervision/hazards 拉(W10)。
        // 这里能给的只是「先看哪一档」:这张卡讲的是刚登记的那几条,而它们一律是
        // pending,所以开在「待确认」档上。**它们不一定是屏幕上仅有的几条** ——
        // 面板拉的是整个台账的待确认,比这张卡说的可能多。这是刻意的:
        // 一次确认动作本来就该看见所有待确认的,而不是只看这一条消息带来的。
        <SupervisionPanel
          artifactBase={artifactBase}
          initialScope={HAZARD_SCOPE_PENDING}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
