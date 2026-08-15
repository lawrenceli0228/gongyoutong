/**
 * 监理处置纯函数库 —— supervision.tsx 的所有可测逻辑都沉在这里(W9 · S6 泳道)。
 *
 * ⚠️ 本文件必须保持**纯 TS、零依赖、零环境假设**(不 import react、不 import "@/…"):
 *    scripts/frontend-tests/ 那个独立 vitest 包按相对路径直接 import 它 ——
 *    多一个依赖那个包就装不动,多一处 window 假设它就跑不起来。
 *    分法照 checkin-lib.ts:碰浏览器的(fetch / portal / 摄像头)一律留在 .tsx。
 *
 * 安装:scripts/setup-frontend.sh 的 install_new_file 拷到 frontend/src/lib/supervision-lib.ts。
 * frontend/ 不进 git,**别直接改那边** —— 换台机器就没了。
 *
 * ── 契约的唯一真相 ────────────────────────────────────────────────────
 * `backend/src/gyt/supervision_api.py` 的**模块 docstring**。七个端点的路径、
 * 请求体字段、Envelope 里 `documents` 数组的四个键,全以那份为准,本文件只是镜像。
 * 状态与级别那两张受控词表的真相在 `backend/src/gyt/db/hazards.py`
 * (`STATUSES` / `GRADES`),中文名的真相在 `supervision_api._STATUS_ZH`。
 * 任一处改名 = **前端静默少一个字段 / 界面上冒出一个英文状态词**,没有报错。
 *
 * ── 为什么这一层值得存在(方案 §5.3 Codex#18)────────────────────────
 * 最终用户回复由 supervisor 汇总,而 supervisor 只有提示词约束 —— 它转述的
 * 文书编号是**赌模型配合度**。结构性修法是「把编号的权威显示面放到前端卡片上」:
 * 卡片直读端点返回的 `doc_no` / `artifact_id`,那段话只是陪衬。用户点的是卡片。
 */

// ---------------------------------------------------------------------------
// 端点 —— 镜像 supervision_api.SUPERVISION_ROUTES
// ---------------------------------------------------------------------------

/**
 * 七个端点的路径段(不含 `/api`:那一段由 Caddy 剥掉,与打卡链同规矩)。
 *
 * ⚠️ `reinspect-result` 中间是**连字符**,不是下划线。写错的表现是 404,
 * 而 404 在这里会被 normalizeError 说成「监理接口还没开通」—— 方向全错。
 */
export const SUPERVISION_ENDPOINTS = [
  "confirm",
  "grade",
  "notice",
  "suspend",
  "reinspect-result",
  "resume",
  "escalate",
] as const;
export type SupervisionEndpoint = (typeof SUPERVISION_ENDPOINTS)[number];

function stripTrailingSlash(base: string): string {
  return base.replace(/\/+$/, "");
}

/**
 * 拼端点地址。`apiBase` 的来路见 supervision.tsx 的 useApiBase 注释
 * (本机 dev 直连 :2024;公网是 `${GYT_PUBLIC_ORIGIN}/api`,Caddy 剥前缀转发)。
 */
export function supervisionUrl(apiBase: string, endpoint: SupervisionEndpoint): string {
  return `${stripTrailingSlash(apiBase)}/supervision/${endpoint}`;
}

// ---------------------------------------------------------------------------
// 受控词表:状态、级别、文书类型
// ---------------------------------------------------------------------------

/** 镜像 db/hazards.py 的 STATUSES(八档,顺序即状态机推进顺序)。 */
export const HAZARD_STATUSES = [
  "pending",
  "open",
  "notified",
  "suspended",
  "reinspect_failed",
  "resuming",
  "closed",
  "escalated",
] as const;
export type HazardStatus = (typeof HAZARD_STATUSES)[number];

/**
 * 状态 → 给工地上的人看的中文。**逐字镜像 supervision_api._STATUS_ZH。**
 *
 * 🔴 `suspended` 只能念「已出具暂停令」,**不许念「已责令停工」**
 * (db/hazards.py 的 STATUSES 头注 + supervision_api 那条红线):
 * 它只证明文书出了稿,不证明工地真停了工。对外措辞不许升级。
 */
export const HAZARD_STATUS_ZH: Readonly<Record<HazardStatus, string>> = Object.freeze({
  pending: "待确认",
  open: "已确认待处置",
  notified: "已签发通知单",
  suspended: "已出具暂停令",
  reinspect_failed: "复查不合格",
  resuming: "待签发复工令",
  closed: "已销项",
  escalated: "已上报主管部门",
});

/** 状态 → 中文;词表外的原样返回(**不猜、也不抛**:展示函数不该因为后端加了一档就炸)。 */
export function hazardStatusZh(status: string): string {
  return (HAZARD_STATUS_ZH as Record<string, string>)[status] ?? status;
}

/** 镜像 db/hazards.py 的 GRADE_NORMAL / GRADE_SEVERE —— 监理口径的二分。 */
export const GRADE_NORMAL = "一般";
export const GRADE_SEVERE = "严重";
export const HAZARD_GRADES = [GRADE_NORMAL, GRADE_SEVERE] as const;
export type HazardGrade = (typeof HAZARD_GRADES)[number];

/**
 * 文书类型 → 中文名。前五档镜像 `core/doc_no.DOC_TITLE_ZH`,
 * 第六档 `reinspect` 不是文书(`DocKind` 里没有它,见 doc_no.py 头注那两个孤儿),
 * 名字取自 `agents/supervision/documents.py` 的 `_doc_type_zh`
 * (2026-08-16 从 `supervision_api.py` 拆出来的,那边只留编排与鉴权;
 *  两处并行落地时这个指针短暂指错过,别再指回 supervision_api)。
 */
export const DOC_TYPE_ZH: Readonly<Record<string, string>> = Object.freeze({
  notice: "监理通知单",
  suspension: "工程暂停令",
  resumption: "工程复工令",
  owner_report: "致建设单位报告",
  authority_report: "监理报告",
  reinspect: "复查记录",
});

/** 文书类型 → 中文;认不出就原样透出(同 hazardStatusZh 的理由)。 */
export function docTypeZh(docType: string): string {
  return DOC_TYPE_ZH[docType] ?? docType;
}

/**
 * 复查记录那一档 —— 镜像 `hazard_docs.doc_type` 里的 `reinspect`。
 *
 * 它**不是文书**:`DocKind` 里根本没有这一档(doc_no.py 头注那两个孤儿之一),
 * 它的 `artifact_id` 天然为空(复查不出文件,只留一条记录)。
 */
export const REINSPECT_DOC_TYPE = "reinspect";

/**
 * 这一条是不是「能下载的文书」。
 *
 * 🔴 用途是把两种「没有 artifact_id」分开说:
 *   · 复查记录 —— **本来就没有文件**,界面上说「下不了」是在吓人;
 *   · 文书但 artifact_id 空 —— 这是**真出事了**(纸签了却取不了件),必须显眼。
 * 混成一句话的代价是后一种被前一种的噪声淹掉,而它恰恰是要人去查的那一种。
 */
export function isDownloadableDoc(doc: Pick<SupervisionDoc, "doc_type">): boolean {
  return doc.doc_type !== REINSPECT_DOC_TYPE;
}

// ---------------------------------------------------------------------------
// 错误与文案
// ---------------------------------------------------------------------------

/** 契约被违反时抛的错。message 一律是看得懂的中文 —— 组件层直接上屏。 */
export class SupervisionContractError extends Error {}

/**
 * 前端这份固定文案。后端 Envelope 的 `user_msg` 到了前端**原样透传**,
 * 不在这里改写 —— 三条硬拦、状态机拒绝的那几句都是后端写好的人话,
 * 前端重新包装一层只会把「该去定级」说成「操作失败」。
 */
export const SUPERVISION_MESSAGES = Object.freeze({
  network: "连不上服务器。检查网络,再试一次。",
  authFailed: "登录信息不对或已过期。刷新页面重新进一次;还不行就找管理员对一下口令。",
  notFound:
    "监理处置接口还没开通(接口不存在)。请管理员确认后端已更新到带监理功能的版本。",
  conflict: "这一步现在做不了(状态可能刚被别人改过),刷新一下再看。",
  rateLimited: "操作太频繁,歇几秒再试。",
  serverError: "服务器出错了,稍等再试;一直这样就找管理员。",
  badEnvelope:
    "服务器返回的内容格式不对。文书可能已经出了,先别重复点 —— 找管理员查一下台账。",
  missingDue: "得写明整改期限(比如「明天」「3天后」「下周三」「月底」)。",
  badPhotoId: "复查照片编号不对:要 32 位的编号(在聊天记录里那张照片下面能看到)。",
  badGrade: "级别只能选「一般」或「严重」。",
  noSelection: "先勾选要确认的隐患。",
});

export interface NormalizedError {
  ok: false;
  /** 看得懂的中文,组件直接上屏。 */
  message: string;
  /** HTTP 状态码;0 = 网络层异常(压根没有响应)。 */
  status: number;
  /** Envelope 带 error_code 时透传,其余为 null。只进日志/调试,不上屏。 */
  errorCode: string | null;
}

/** 网络层异常没有响应,约定 status 传 0(fetch reject 时组件层这么调)。 */
export const NETWORK_ERROR_STATUS = 0;

function messageForStatus(status: number): string {
  if (status === 401 || status === 403) return SUPERVISION_MESSAGES.authFailed;
  if (status === 404) return SUPERVISION_MESSAGES.notFound;
  if (status === 409) return SUPERVISION_MESSAGES.conflict;
  if (status === 429) return SUPERVISION_MESSAGES.rateLimited;
  if (status >= 500) return SUPERVISION_MESSAGES.serverError;
  return `服务器返回了看不懂的内容(HTTP ${status}),稍后再试。`;
}

function tryParseJsonObject(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // 不是 JSON,让调用方走非 JSON 分支
  }
  return null;
}

/**
 * 把四种错误形状压成一个 `{ok:false, message}`(判据与 checkin-lib.normalizeError 同款,
 * 两处各自持有自己的文案 —— 打卡说「打卡太频繁」,监理说「操作太频繁」):
 *
 *   ① 进了 handler:Envelope 四键 —— `user_msg` 已经是人话,**原样用**
 *   ② langgraph 鉴权中间件 / Caddy 未登录:`{"detail": …}` —— 英文内部话,不上屏
 *   ③ Caddy 的错误页:连 JSON 都不是 —— 按状态码给固定中文
 *   ④ 网络层异常:没有响应,约定 status=0
 *
 * ⚠️ **Envelope 分支必须优先**。404 有两种来源:接口不存在(要提醒管理员升级后端)
 * 与「没找到隐患编号」(后端带着 user_msg 说清了)。先看 user_msg 才不会把后者
 * 说成「监理接口还没开通」,让人跑去查部署 —— 而真相只是编号打错了一位。
 */
export function normalizeError(
  status: number,
  contentType: string | null,
  bodyText: string,
): NormalizedError {
  if (status === NETWORK_ERROR_STATUS) {
    return { ok: false, message: SUPERVISION_MESSAGES.network, status, errorCode: null };
  }
  const claimsJson = (contentType ?? "").toLowerCase().includes("json");
  const looksJson = bodyText.trimStart().startsWith("{");
  const parsed = claimsJson || looksJson ? tryParseJsonObject(bodyText) : null;
  if (parsed) {
    const userMsg = parsed.user_msg;
    if (typeof userMsg === "string" && userMsg.trim()) {
      const errorCode = parsed.error_code;
      return {
        ok: false,
        message: userMsg.trim(),
        status,
        errorCode: typeof errorCode === "string" ? errorCode : null,
      };
    }
  }
  return { ok: false, message: messageForStatus(status), status, errorCode: null };
}

// ---------------------------------------------------------------------------
// 隐患 —— 从工具结果里捞出来的那一份
// ---------------------------------------------------------------------------

/**
 * 隐患摘要 —— 镜像 `agents/safety/tools.py` 的 `_ingest_hazards` 返回的那五个键
 * (`data.hazards[]`),也是 W9 的 supervision Agent 只读工具会返回的同一形状。
 */
export interface HazardBrief {
  hazard_no: string;
  item: string;
  grade: string;
  status: string;
  needs_grading: boolean;
}

/**
 * 哪些工具的返回值里可能带隐患清单。
 *
 * `analyze_site_photo` 是**现在就有的**那条(S3 已落地:识别 + 登记成 pending);
 * 后三个是 S5 的 supervision Agent 只读工具(方案 §5.1),**以
 * `backend/src/gyt/agents/supervision/tools.py` 的 `@tool` 名字为准**。
 * 名字对不上不会崩 —— 只是那条工具的结果不生成隐患卡片,和 W9 之前一样。
 */
export const HAZARD_SOURCE_TOOLS = [
  "analyze_site_photo",
  "list_hazards",
  "get_hazard",
  "suggest_disposal",
] as const;

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function textOf(rec: Record<string, unknown>, key: string): string {
  const value = rec[key];
  return typeof value === "string" ? value.trim() : "";
}

/**
 * 从工具结果的 `data` 里取隐患清单。**认不出就返回空数组,一个错都不抛** ——
 * 这是渲染路径上的函数,抛出去就是整条消息渲染不出来(而它只是一张附加卡片)。
 *
 * 只有 `hazard_no` 是**不能兜底**的字段:没有编号的条目在界面上什么也做不了
 * (七个端点全靠它定位),所以那种条目直接跳过。其余字段给保守缺省:
 * 事项空着按「(未写明事项)」显示,状态空着按 pending —— pending 是权限最小的一档
 * (不算整改率、不进超期清单、不能被升级),猜错也不会让人去点一个不该点的按钮。
 */
export function hazardsFromToolData(data: unknown): HazardBrief[] {
  const rec = asRecord(data);
  if (!rec) return [];
  // 两种形状都要认(后端两条真实路径):
  //   · `data.hazards` 是数组 —— analyze_site_photo(登记结果)与 list_hazards(清单);
  //   · `data` 自己就是一条 —— get_hazard 把 `_hazard_item` 直接 ** 展开进 data。
  // 认第二种的用处很实在:它是**处置更早那条隐患的唯一入口** —— 对话里问一句
  // 「GYT-H-… 复查了吗」,回复底下就会出现同一张卡、点开就是同一个面板。
  const raw = Array.isArray(rec.hazards) ? rec.hazards : [rec];
  const out: HazardBrief[] = [];
  for (const entry of raw) {
    const item = asRecord(entry);
    if (!item) continue;
    const hazardNo = textOf(item, "hazard_no");
    if (!hazardNo) continue;
    out.push({
      hazard_no: hazardNo,
      item: textOf(item, "item") || "(未写明事项)",
      grade: textOf(item, "grade"),
      status: textOf(item, "status") || "pending",
      needs_grading: item.needs_grading === true,
    });
  }
  return out;
}

/**
 * 登记失败的违规项(D10 / Codex#10:识别照常返回、失败项进 `failed_items`)。
 *
 * **界面上必须看得见。** 后端那一侧已经落了一行 `hazard_ingest_failures`,
 * 但工友看不到库;界面不显示的话,一条真实存在的隐患就这么没了 —— 而屏幕上
 * 一切正常(照片识别的回执照常报了这一项)。
 */
export function failedItemsFromToolData(data: unknown): string[] {
  const raw = asRecord(data)?.failed_items;
  if (!Array.isArray(raw)) return [];
  return raw.map((x) => (typeof x === "string" ? x.trim() : "")).filter(Boolean);
}

/**
 * 合并两份隐患清单:**按 `hazard_no` 去重,后来的赢**(状态更新过)。
 *
 * 顺序保持 `prev` 的次序、新的追加在后 —— 面板开着的时候按钮不许跳位:
 * 手指已经落下去了,列表重排就是点到别人身上(而这里每一颗按钮都是法律动作)。
 */
export function mergeHazards(
  prev: readonly HazardBrief[],
  incoming: readonly HazardBrief[],
): HazardBrief[] {
  const byNo = new Map<string, HazardBrief>();
  for (const h of incoming) byNo.set(h.hazard_no, h);
  const merged = prev.map((h) => byNo.get(h.hazard_no) ?? h);
  const seen = new Set(prev.map((h) => h.hazard_no));
  for (const h of incoming) {
    if (!seen.has(h.hazard_no)) merged.push(h);
  }
  return merged;
}

/** 改一条隐患的字段,返回**新数组**(不可变;找不到编号就原样返回)。 */
export function patchHazard(
  list: readonly HazardBrief[],
  hazardNo: string,
  patch: Partial<HazardBrief>,
): HazardBrief[] {
  return list.map((h) => (h.hazard_no === hazardNo ? { ...h, ...patch } : h));
}

/** 从清单里划掉一条(「否决」只是本地视图操作,见 supervision.tsx 里那段说明)。 */
export function removeHazard(list: readonly HazardBrief[], hazardNo: string): HazardBrief[] {
  return list.filter((h) => h.hazard_no !== hazardNo);
}

/** 待确认的那些(D17:只有 pending 能进 `POST /supervision/confirm`)。 */
export function pendingHazards(list: readonly HazardBrief[]): HazardBrief[] {
  return list.filter((h) => h.status === "pending");
}

/** 勾选/取消一条,返回**新数组**。顺序保持点选顺序,不排序 —— 请求体的顺序即回执顺序。 */
export function toggleSelected(selected: readonly string[], hazardNo: string): string[] {
  return selected.includes(hazardNo)
    ? selected.filter((no) => no !== hazardNo)
    : [...selected, hazardNo];
}

// ---------------------------------------------------------------------------
// 处置动作:哪条隐患现在能做什么
// ---------------------------------------------------------------------------

/**
 * 界面上的处置动作。与端点**不是一一对应**:`reinspect` 打的是
 * `reinspect-result`(它一个动作两种结论),所以下面留了一张映射表。
 */
export const DISPOSAL_ACTIONS = [
  "grade",
  "notice",
  "suspend",
  "reinspect",
  "resume",
  "escalate",
] as const;
export type DisposalAction = (typeof DISPOSAL_ACTIONS)[number];

/** 动作 → 端点。 */
export const ACTION_ENDPOINT: Readonly<Record<DisposalAction, SupervisionEndpoint>> =
  Object.freeze({
    grade: "grade",
    notice: "notice",
    suspend: "suspend",
    reinspect: "reinspect-result",
    resume: "resume",
    escalate: "escalate",
  });

/** 按钮上的字。用「签发」而不是「生成」—— 这几份是要拿去签字盖章的法律文书。 */
export const ACTION_LABEL: Readonly<Record<DisposalAction, string>> = Object.freeze({
  grade: "人工定级",
  notice: "签发监理通知单",
  suspend: "签发暂停令(三份)",
  reinspect: "登记复查结论",
  resume: "签发工程复工令",
  escalate: "上报主管部门",
});

/**
 * 这条隐患现在能做哪几件事。**判据逐条对应 supervision_api 的四道闸**
 * (`_require_graded` / `_refuse_severe_notice` / `_refuse_normal_suspend` /
 * `_require_status` 那几个 `_*_FROM` 集合),顺序 = 主要动作在前。
 *
 * 🔴 这里只是「别让人点一个必被拒的按钮」的前置判断,**真正说了算的是服务端**
 * (它还要过一遍并发窗口:先读的快照说可以、写的时候 rowcount=0 就是 409)。
 * 所以这个表**只许比服务端窄,永远不许更宽** —— 宽了就是给人一颗点下去必挨骂的
 * 按钮,而工友会以为是系统坏了。
 *
 * 三条硬拦在这里的投影:
 *   · `needs_grading=1` → **只剩定级一件事**(Codex#11:未知风险不许按一般隐患
 *     走完闭环。硬拦③排在级别方向那两道之前,这里也一样);
 *   · grade=严重 的 open → 只给 `suspend`,不给 `notice`(硬拦①);
 *   · grade=一般 的 open → 只给 `notice`,不给 `suspend`(硬拦②)。
 *
 * `grade` 在 pending / open 都给:`_GRADABLE_STATUSES` 就是这两档,而且
 * 「确实要停工,先把它改定为严重隐患」正是硬拦②那句拒绝话给出的出路。
 */
export function availableActions(hazard: HazardBrief): DisposalAction[] {
  if (hazard.needs_grading) return ["grade"];
  switch (hazard.status) {
    case "pending":
      // 确认(pending → open)走批量勾选那条路,不在单条动作里 —— 它是个批量端点。
      return ["grade"];
    case "open":
      return hazard.grade === GRADE_SEVERE ? ["suspend", "grade"] : ["notice", "grade"];
    case "notified":
    case "suspended":
      return ["reinspect"];
    case "reinspect_failed":
      // 再复查一次,或者升级上报 —— 举证链要求「通知过 + 期限到了 + 复查过 + 没改」,
      // 所以 escalate 只从这一档进(_ESCALATE_FROM)。
      return ["reinspect", "escalate"];
    case "resuming":
      return ["resume"];
    default:
      // closed / escalated / 词表外:没有下一步。
      return [];
  }
}

/** 要不要填整改期限(`due_phrase` 收用户原话,换算全在后端 dates.py)。 */
export function actionNeedsDuePhrase(action: DisposalAction): boolean {
  return action === "notice" || action === "suspend";
}

/** 要不要挂复查照片(方案 §5.2 红线:拿不到照片就没有任何路径能改成 closed)。 */
export function actionNeedsPhoto(action: DisposalAction): boolean {
  return action === "reinspect";
}

/**
 * 要不要二次确认。**判据是「这一下是不是法律行为」**,不是「会不会写库」:
 *   · `suspend` —— 一次停掉一片人的工;
 *   · `escalate` —— 对施工单位的正式指控,报到建设主管部门。
 * 两者都无法在系统里撤销(本批不做重签,见 `_GRADABLE_STATUSES` 头注),
 * 手滑一下的代价落在真实工地上,所以必须多问一句。
 *
 * `notice` 不问:它是监理日常动作,而且期限那一格本来就要动手打字 ——
 * 每一颗按钮都弹确认框的下场是人闭着眼点「确定」,那时真正该拦的两颗也就废了。
 */
export function actionNeedsConfirm(action: DisposalAction): boolean {
  return action === "suspend" || action === "escalate";
}

/** 二次确认框里那句话。写清楚**后果**和**不可撤销**,不写「确定吗?」。 */
export function confirmPrompt(action: DisposalAction, hazard: HazardBrief): string {
  if (action === "suspend") {
    return (
      `要为隐患「${hazard.item}」(${hazard.hazard_no})一次签发三份文书吗?\n` +
      "《监理通知单》+《工程暂停令》+《致建设单位报告》\n\n" +
      "暂停令是法律文书,签字盖章后据以停工。系统里不能撤销,签错只能另走复查/升级流程。"
    );
  }
  return (
    `要为隐患「${hazard.item}」(${hazard.hazard_no})出具《监理报告》报建设主管部门吗?\n\n` +
    "这是对施工单位的正式指控,举证链是「通知过 + 期限到了 + 复查过 + 仍未整改」。\n" +
    "系统里不能撤销。"
  );
}

// ---------------------------------------------------------------------------
// 请求体拼装(校验在这里做,省一次白跑;服务端仍会再校验一遍)
// ---------------------------------------------------------------------------

/** 产物编号:`core/artifacts.py` 是 `uuid4().hex` —— 32 位小写十六进制。 */
const ARTIFACT_ID_PATTERN = /^[0-9a-f]{32}$/;

/** 一次批量确认的条数上限,镜像 supervision_api._MAX_CONFIRM_BATCH。 */
export const MAX_CONFIRM_BATCH = 200;

/** `POST /supervision/confirm` 的请求体。去重保序 —— 双击会把同一条发两遍。 */
export function confirmBody(hazardNos: readonly string[]): { hazard_nos: string[] } {
  const nos = [...new Set(hazardNos.map((n) => n.trim()).filter(Boolean))];
  if (nos.length === 0) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.noSelection);
  }
  if (nos.length > MAX_CONFIRM_BATCH) {
    throw new SupervisionContractError(`一次最多确认 ${MAX_CONFIRM_BATCH} 条,分几次来。`);
  }
  return { hazard_nos: nos };
}

export interface ActionInput {
  hazardNo: string;
  /** notice / suspend 必填:**用户原话**,换算交给后端 dates.py(红线:模型和前端都不许自己算日期)。 */
  duePhrase?: string;
  /** grade 必填。 */
  grade?: string;
  /** reinspect 必填。 */
  result?: "pass" | "fail";
  /** reinspect 必填:整改后那张现场照片的 32 位编号。 */
  afterPhotoId?: string;
}

/**
 * 按动作拼请求体,**必填项缺了就抛中文**(组件层直接上屏)。
 *
 * 为什么前端也校验一遍:少一次白跑,而且「期限没填」这种事在按钮旁边说比等
 * 服务端 400 回来再说快得多。服务端那一份**一条都不能省** —— 这里的校验只是
 * 体验,不是安全边界(方案 §5.1:硬拦全在服务端代码里,不靠提示词、也不靠前端)。
 */
export function actionBody(action: DisposalAction, input: ActionInput): Record<string, unknown> {
  const hazardNo = input.hazardNo.trim();
  if (!hazardNo) {
    throw new SupervisionContractError("没说是哪条隐患(缺隐患编号)。");
  }
  const body: Record<string, unknown> = { hazard_no: hazardNo };

  if (action === "grade") {
    const grade = (input.grade ?? "").trim();
    if (!(HAZARD_GRADES as readonly string[]).includes(grade)) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.badGrade);
    }
    body.grade = grade;
    return body;
  }

  if (actionNeedsDuePhrase(action)) {
    const duePhrase = (input.duePhrase ?? "").trim();
    if (!duePhrase) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.missingDue);
    }
    // 原话原样送 —— 不许在前端把「下周三」换算成日期(dates.py 用 314 行
    // 证明了这件事有多容易算错,红线是「只传原话,代码来算」,前端也在红线内)。
    body.due_phrase = duePhrase;
    return body;
  }

  if (actionNeedsPhoto(action)) {
    if (input.result !== "pass" && input.result !== "fail") {
      throw new SupervisionContractError("复查结论只能是合格或不合格,得由人来下。");
    }
    const photoId = (input.afterPhotoId ?? "").trim().toLowerCase();
    if (!ARTIFACT_ID_PATTERN.test(photoId)) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.badPhotoId);
    }
    body.result = input.result;
    body.after_photo_id = photoId;
    return body;
  }

  // resume / escalate 只要编号
  return body;
}

// ---------------------------------------------------------------------------
// 响应解析
// ---------------------------------------------------------------------------

/** `documents` 数组里的一项 —— 镜像 supervision_api._IssuedDoc.as_payload()。 */
export interface SupervisionDoc {
  doc_type: string;
  doc_no: string;
  /** 取件编号。**为 null 时绝不渲染下载链接**,见 documentUrl。 */
  artifact_id: string | null;
  filename: string;
}

export interface ActionResult {
  hazard_no: string;
  /** 动作完成后**库里的真实状态**(不是前端猜的)。 */
  status: string;
  /** 顺序固定,一份一张卡。`grade` / `reinspect-result` 不出文书,恒为空数组。 */
  documents: SupervisionDoc[];
  user_msg: string;
  /** 只有 `grade` 端点带。 */
  grade: string | null;
  /** 只有 `grade` 端点带。 */
  needsGrading: boolean | null;
  /** 只有 `reinspect-result` 端点带。 */
  result: string | null;
}

function toDoc(raw: unknown): SupervisionDoc {
  const rec = asRecord(raw);
  if (!rec) throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  const docType = textOf(rec, "doc_type");
  const docNo = textOf(rec, "doc_no");
  if (!docType || !docNo) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  }
  const artifactId = textOf(rec, "artifact_id");
  return {
    doc_type: docType,
    doc_no: docNo,
    // 空 artifact_id 保留成 null 而不是丢掉整条:文书**确实签发了**(编号已进台账),
    // 只是取不了件。丢掉的话界面上少一张卡,而工友以为那份文书没出 —— 反了。
    artifact_id: artifactId || null,
    filename: textOf(rec, "filename") || `${docTypeZh(docType)}_${docNo}.docx`,
  };
}

/**
 * 解析六个单条端点的 200 响应(confirm 另有一份,它的 data 形状不同)。
 *
 * 🔴 `documents` **必须是数组,缺了就抛**。这正是 Codex#16 要冻结形状的理由:
 * 三份文书里少出一份,若形状是三个独立的键,前端少渲一张卡没有任何异常;
 * 是数组的话「N 张卡」与「N 份文书」天然对齐 —— 而「连数组都没有」属于契约破了,
 * 必须响亮地失败,不能悄悄当成「这次没出文书」。
 */
export function parseActionEnvelope(bodyText: string): ActionResult {
  const parsed = tryParseJsonObject(bodyText);
  if (!parsed || parsed.ok !== true) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  }
  const data = asRecord(parsed.data);
  if (!data) throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  const hazardNo = textOf(data, "hazard_no");
  const status = textOf(data, "status");
  if (!hazardNo || !status) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  }
  if (!Array.isArray(data.documents)) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  }
  const userMsg = parsed.user_msg;
  return {
    hazard_no: hazardNo,
    status,
    documents: data.documents.map(toDoc),
    user_msg: typeof userMsg === "string" ? userMsg.trim() : "",
    grade: textOf(data, "grade") || null,
    needsGrading: typeof data.needs_grading === "boolean" ? data.needs_grading : null,
    result: textOf(data, "result") || null,
  };
}

export interface ConfirmFailure {
  hazard_no: string;
  reason: string;
}

export interface ConfirmResult {
  confirmed: string[];
  failed: ConfirmFailure[];
  user_msg: string;
}

/**
 * 解析 `POST /supervision/confirm` 的 200 响应。
 *
 * 它是**批量**动作,`data` 形状与其余六个不同(契约里写明的两处例外之一):
 * `{"confirmed": [...], "failed": [{hazard_no, reason}]}`。
 * 部分成功是常态(有人抢先确认过),`failed` 必须显示出来 ——
 * 只报「已确认 3 条」而不说另外 2 条为什么没成,人会以为全成了。
 */
export function parseConfirmEnvelope(bodyText: string): ConfirmResult {
  const parsed = tryParseJsonObject(bodyText);
  if (!parsed || parsed.ok !== true) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  }
  const data = asRecord(parsed.data);
  if (!data || !Array.isArray(data.confirmed)) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.badEnvelope);
  }
  const rawFailed = Array.isArray(data.failed) ? data.failed : [];
  const userMsg = parsed.user_msg;
  return {
    confirmed: data.confirmed
      .map((x) => (typeof x === "string" ? x.trim() : ""))
      .filter(Boolean),
    failed: rawFailed.flatMap((entry) => {
      const rec = asRecord(entry);
      if (!rec) return [];
      const hazardNo = textOf(rec, "hazard_no");
      if (!hazardNo) return [];
      return [{ hazard_no: hazardNo, reason: textOf(rec, "reason") }];
    }),
    user_msg: typeof userMsg === "string" ? userMsg.trim() : "",
  };
}

// ---------------------------------------------------------------------------
// 文书取件地址
// ---------------------------------------------------------------------------

/**
 * 文书的下载地址:`${base}/by-id/<artifact_id>`。
 *
 * **`artifact_id` 为空时返回 null,组件据此渲染一行说明、绝不渲染死链接** ——
 * 与 checkin-lib.receiptImageUrl 同一条规矩:点了没反应和「文件真没了」在界面上
 * 长得一模一样,分不开就等于没告诉人。
 *
 * `base` **由调用方传进来,本文件不读 `process.env`**:
 * `NEXT_PUBLIC_ARTIFACT_BASE` 那条链(CLAUDE.md 同源清单)现在有三个读者
 * (human.tsx / tool-calls.tsx / checkin.tsx),链断在任何一环都不报错 ——
 * 而且 https 页面拉 http 资源属于 mixed content,浏览器**连请求都不发**。
 * 少一个读者就少一处能断的地方,所以监理这条链复用 tool-calls.tsx 手里那一份。
 */
export function documentUrl(
  doc: Pick<SupervisionDoc, "artifact_id">,
  artifactBase: string,
): string | null {
  if (!doc.artifact_id) return null;
  return `${stripTrailingSlash(artifactBase)}/by-id/${doc.artifact_id}`;
}

/**
 * 从**工具结果**的 `data` 里捞文书清单。**认不出就跳过,一个错都不抛。**
 *
 * ⚠️ 与上面的 `parseActionEnvelope` 是**两种严格度,刻意的**:
 *   · `parseActionEnvelope` 走的是 HTTP 动作那条路 —— 人刚点了「签发」,契约破了
 *     必须响亮地失败,否则「三份出了两份」会被静默当成「这次就出两份」;
 *   · 本函数走的是**渲染**那条路 —— 它在 tool-calls.tsx 的 ToolResult 里被调用,
 *     抛出去就是整条消息渲染不出来(白屏一片),而它只负责一张附加卡片。
 * 两条路的代价不对称,所以严格度也不对称。别为了「统一」把哪一边改成另一边。
 */
export function documentsFromToolData(data: unknown): SupervisionDoc[] {
  const raw = asRecord(data)?.documents;
  if (!Array.isArray(raw)) return [];
  const out: SupervisionDoc[] = [];
  for (const entry of raw) {
    const rec = asRecord(entry);
    if (!rec) continue;
    const docType = textOf(rec, "doc_type");
    const docNo = textOf(rec, "doc_no");
    // 这两个是身份:没有类型说不出这是什么纸,没有编号就无从对台账。缺任一条跳过。
    if (!docType || !docNo) continue;
    const artifactId = textOf(rec, "artifact_id");
    out.push({
      doc_type: docType,
      doc_no: docNo,
      artifact_id: artifactId || null,
      filename: textOf(rec, "filename") || `${docTypeZh(docType)}_${docNo}.docx`,
    });
  }
  return out;
}

/**
 * 一次动作出了几份文书,说成一句话。
 * 出 0 份时返回空串 —— 让调用方自己决定说什么(定级和复查本来就不出文书)。
 */
export function describeDocuments(docs: readonly SupervisionDoc[]): string {
  if (docs.length === 0) return "";
  return `已出稿 ${docs.length} 份:${docs.map((d) => docTypeZh(d.doc_type)).join("、")}`;
}
