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
 * `backend/src/gyt/supervision_api.py` 的**模块 docstring**。端点的路径、
 * 请求体字段、Envelope 里 `documents` 数组的键,全以那份为准,本文件只是镜像
 * (W10 之后是 **8 个 POST 动作端点 + 2 个 GET 查询端点**)。
 * 状态与级别那两张受控词表的真相在 `backend/src/gyt/db/hazards.py`
 * (`STATUSES` / `GRADES`),中文名的真相在 `supervision_api._STATUS_ZH`,
 * scope 那张在 `agents/supervision/scoping.py` 的 `SCOPES`。
 * 任一处改名 = **前端静默少一个字段 / 界面上冒出一个英文状态词**,没有报错。
 *
 * ── 为什么这一层值得存在(方案 §5.3 Codex#18)────────────────────────
 * 最终用户回复由 supervisor 汇总,而 supervisor 只有提示词约束 —— 它转述的
 * 文书编号是**赌模型配合度**。结构性修法是「把编号的权威显示面放到前端卡片上」:
 * 卡片直读端点返回的 `doc_no` / `artifact_id`,那段话只是陪衬。用户点的是卡片。
 *
 * ── W10:面板为什么从聊天流里搬出来 ───────────────────────────────────
 * 原先处置面板的入口挂在「聊天流里那条工具返回」上,而 supervisor 的
 * `output_mode="last_message"` **会把子 Agent 的工具返回丢掉** —— 于是那个面板
 * 一次都没打开过(不是没人点,是根本没渲染出来)。修法照 W7 打卡面板的先例:
 * 面板改成界面上的**常驻操作台**,清单/详情直读两个 GET 端点,
 * **一个字都不经过聊天流**。
 *
 * 这条改动在本文件里的落点是三件:`hazardListUrl` / `hazardDetailUrl`(自己去取数,
 * 不等模型给)、`parseHazardListEnvelope` / `parseHazardDetailEnvelope`(把端点的
 * Envelope 解析成界面要的形状),以及 `reject` 这个新动作(误报的隐患得能删掉 ——
 * 原先只能靠 `removeHazard` 在本地视图里划掉,刷新一下它又回来了)。
 * 老的 `hazardsFromToolData` / `documentsFromToolData` **一件都不删**:哪条链路的
 * 工具返回会被裁掉、哪条不会,是编排层的事,前端不该赌 —— 聊天里真出现了工具返回,
 * 那两件照旧把卡片渲出来。它们只是不再是唯一入口(以前是,所以面板才会全程没露过面)。
 * 两条路共用下面同一个行解析器 `toHazardBrief`,形状不会分叉。
 */

// ---------------------------------------------------------------------------
// 端点 —— 镜像 supervision_api.SUPERVISION_ROUTES
// ---------------------------------------------------------------------------

/**
 * 八个 **POST 动作**端点的路径段(不含 `/api`:那一段由 Caddy 剥掉,与打卡链同规矩)。
 *
 * ⚠️ 两个 **GET 查询**端点**不在这张表里** —— 它们要拼 query / 路径参数,
 * 各有专门的构造函数(`hazardListUrl` / `hazardDetailUrl`)。想「统一」成一张表的话,
 * 那两个的三态与转义规则就会被 `supervisionUrl` 这种裸拼接吃掉。
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
  // W10 新增:否决一条**待确认**的隐患(自动识别的误报)。它是删除,不是状态流转 ——
  // 所以它不在 db/hazards.py 的状态机里,回执报的 status 也不是八档里的词(见 ActionResult)。
  "reject",
  // 2026-08-21 新增:「这条不是隐患 / 已当场整改」——**不出文书**把 open 关掉。
  // 与 reject 的分工:reject 删的是 pending(还没人确认、没有留档价值),
  // dismiss 关的是 open(行还在、编号还在、照片还在,只是写明了为什么关)。
  "dismiss",
  // 2026-08-22 新增的**两处订正**。它们与上面那批不同类:不签发任何文书、
  // 不改 status,回答的是「当初记错了」而不是「这件事进展到哪一步了」。
  //
  // extend —— 改整改期限(宽限几天)。它补的是一条**断掉的路**:
  //   agents/schedule/tools.py 明写「要宽限几天,得让监理去改这条隐患的整改期限」,
  //   而在这条端点之前监理这边没有这个按钮 —— 工友照着做、监理找不到,
  //   两边都以为是自己没找到。
  "extend",
  // reassign —— 改归属(挪工地)。隐患的 project_id 在登记那一刻由顶栏那个选择器决定,
  //   忘了选就是空串(未归属)。在这条之前**没有任何动作能改它**,于是那条隐患
  //   永远待在未归属那堆里:按工地筛的清单里没有它,而它在库里还是「在办」。
  "reassign",
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

/**
 * `GET /supervision/hazards` 的地址 —— 常驻操作台开机就打这一条(W10)。
 *
 * ── 🔴 `projectId` 是**三态**,不是两态 ──────────────────────────────
 * 它 1:1 映射后端 `db/hazards.py` 的 `list_rows(project_id: str | None)`:
 *
 * | 传什么                    | 拼出来的 query      | 后端拿到      | 意思             |
 * |---------------------------|---------------------|---------------|------------------|
 * | 不传 / `undefined` / `null` | (这个键**不出现**) | `None`        | 不筛工地(全部) |
 * | `""`(空串)              | `project_id=`       | `""`          | **只看未归属**   |
 * | `"P-xxx"`                 | `project_id=P-xxx`  | `"P-xxx"`     | 只看这个工地     |
 *
 * 「键不出现」和「有键无值」是两码事,而 `URLSearchParams` 最容易在这儿写错:
 * `params.set("project_id", projectId ?? "")` 看着人畜无害,实际把「全部工地」
 * 变成了「只看未归属」—— 屏幕上是一张短得多的清单,没有任何报错,而监理会以为
 * 台账里就这么几条。反过来把空串吞掉(`if (projectId) …`)则是「未归属那一堆
 * 永远筛不出来」,D6 那批没人认领的隐患从此看不见。两种写法都会静默出错,
 * 所以下面那一行的判据只能是 `!= null`,不许用真值判断。
 *
 * `scope` 不在这儿拦词表外的词:界面上它是四颗按钮出来的,拦了也拦不住别的调用方,
 * 而真正拦得住的是后端那句 400 人话(`normalizeError` 会原样上屏)。
 * 空 scope 干脆不写这个键 —— 让后端用它自己的缺省(「在办」),别在前端复制一份缺省。
 */
export function hazardListUrl(
  apiBase: string,
  opts: { scope?: string; projectId?: string | null; photoIds?: readonly string[] } = {},
): string {
  const params = new URLSearchParams();
  const scope = (opts.scope ?? "").trim();
  if (scope) params.set("scope", scope);
  // 🔴 三态就靠这一行:只有 undefined / null 才「不写这个键」,空串必须写成有键无值。
  if (opts.projectId != null) params.set("project_id", opts.projectId);
  // 按**照片**看(2026-09-18 FINDING-001,拍完照那張隱患卡):逗號拼、後端按「全部」算。
  // 空數組 = 不寫這個鍵 —— `photo_id=` 空串後端會 400。
  if (opts.photoIds && opts.photoIds.length > 0) params.set("photo_id", opts.photoIds.join(","));
  const query = params.toString();
  const base = `${stripTrailingSlash(apiBase)}/supervision/hazards`;
  return query ? `${base}?${query}` : base;
}

/**
 * `GET /supervision/hazards/{hazard_no}` 的地址 —— 详情 + 证据链。
 *
 * 编号一律 `encodeURIComponent`。今天的编号(`GYT-H-日期-时刻-4hex`)全是安全字符,
 * 转义与不转义拼出来一模一样 —— 但这是「今天」:编号格式归 `core/doc_no.py` 管,
 * 哪天多一段带斜杠或中文的东西,不转义就是路径注入点,而且**不会有任何报错**
 * (`/` 会被当成路径分隔符,打到一个不存在的路由上,404 又被说成「接口还没开通」)。
 * 顺手 trim:从聊天记录里复制编号常带一个尾空格,不 trim 就变成 `%20` 然后 404。
 */
export function hazardDetailUrl(apiBase: string, hazardNo: string): string {
  const encoded = encodeURIComponent(hazardNo.trim());
  return `${stripTrailingSlash(apiBase)}/supervision/hazards/${encoded}`;
}

/**
 * `GET /supervision/ingest-failures` 的地址 —— 「有哪几条隐患**没能**写进台账」(2026-08-22)。
 *
 * `projectId` 三态与 `hazardListUrl` **完全同一套**,所以下面那一行判据也必须是
 * `!= null` 而不是真值判断(理由整段在 `hazardListUrl` 头注上,别在这儿重讲一遍,
 * 但也**别以为这条端点无关紧要就可以省掉三态** —— 塌成两态的表现同样是
 * 「全部工地」悄悄变成「只有未归属」)。
 *
 * ⚠️ 路径是 `/supervision/ingest-failures`,**不在 `/supervision/hazards/` 下面**。
 * 写成 `/supervision/hazards/ingest-failures` 会被详情那条带路径段的路由吞掉,
 * 表现是 404 带一句「台账里没有『ingest-failures』这条隐患」。
 */
export function ingestFailuresUrl(
  apiBase: string,
  opts: { projectId?: string | null } = {},
): string {
  const params = new URLSearchParams();
  if (opts.projectId != null) params.set("project_id", opts.projectId);
  const query = params.toString();
  const base = `${stripTrailingSlash(apiBase)}/supervision/ingest-failures`;
  return query ? `${base}?${query}` : base;
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

/**
 * `POST /supervision/reject` 成功后回执里那个 `status` 的值(W10)。
 *
 * 🔴 它**故意不在 `HAZARD_STATUSES` 里、也不给中文名**:那八档是 db 的状态机,
 * 而这一行已经被删掉了,它没有状态。混进那张表的下场是「已删除」出现在状态徽章的
 * 候选里,而库里永远查不到这么一条 —— 前端凭空多出一档后端不认的状态。
 *
 * 界面上的正确用法是拿它**判断**、不拿它**显示**:
 * `if (result.status === REJECTED_STATUS) setList(removeHazard(list, no))`。
 * 直接把它当状态渲染的话,屏幕上会冒出一个英文词 deleted(违反「不许有英文枚举值」)。
 */
export const REJECTED_STATUS = "deleted";

/** 镜像 db/hazards.py 的 GRADE_NORMAL / GRADE_SEVERE —— 监理口径的二分。 */
export const GRADE_NORMAL = "一般";
export const GRADE_SEVERE = "严重";
export const HAZARD_GRADES = [GRADE_NORMAL, GRADE_SEVERE] as const;
export type HazardGrade = (typeof HAZARD_GRADES)[number];

/**
 * 清单筛子 —— 镜像 `agents/supervision/scoping.py` 的 `SCOPES`
 * (W10 从 `agents/supervision/tools.py` 拆出去的那张表,四个词一个字没变;
 *  在拆完之前它就在 tools.py 里,两处指的是同一张表)。
 *
 * **顺序就是界面上四颗按钮的顺序**,「在办」在最前面 —— 它是缺省档,也是监理
 * 一睁眼要看的那一屏(没销项也没上报的全部)。
 *
 * 🔴 这是**受控词表**,词表外后端直接回 400(它刻意不做模糊匹配:模型/前端自己
 * 发明筛子「严重的」「这周的」时,静默按「全部」处理会让监理以为清单就这么长)。
 * 所以界面上只许从这四个词里选,不许出自由输入框。
 */
export const HAZARD_SCOPE_ACTIVE = "在办";
export const HAZARD_SCOPE_PENDING = "待确认";
/**
 * 2026-09-18 加的第五档(用户反馈:「整改完后的过程应该另有一个待复查清单,不和待确认
 * 混在一起」)。后端 `scoping.REINSPECT_STATUSES` = notified / suspended / reinspect_failed,
 * 与 `reinspectableHazards()` 筛出来的是同一批 —— 那一格共用照片要不要出现、这一档清单
 * 列哪些,判据同源。
 */
export const HAZARD_SCOPE_REINSPECT = "待复查";
export const HAZARD_SCOPE_OVERDUE = "超期";
export const HAZARD_SCOPE_ALL = "全部";
export const HAZARD_SCOPES = [
  HAZARD_SCOPE_ACTIVE,
  HAZARD_SCOPE_PENDING,
  HAZARD_SCOPE_REINSPECT,
  HAZARD_SCOPE_OVERDUE,
  HAZARD_SCOPE_ALL,
] as const;
export type HazardScope = (typeof HAZARD_SCOPES)[number];

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
 * 整改期限那一格 2026-09-18 起是**日期框**(用户反馈:「期限最好是选择日期的形式而不是
 * 由他们自己填,不然格式不一致后面很难统计和展示」)。日期框的值天然是 `YYYY-MM-DD`,
 * 后端 `dates.py` 的 ISO 分支原样认,所以前端仍然**一行日期换算都不写**,只是不再需要
 * 那张「让人照抄的例句表」(`DUE_PHRASE_EXAMPLES`,已删;它当年存在的全部理由是
 * 自由文本框里人不知道该怎么写)。后端的 `due_phrase` 字段名与中文短语解析都留着 ——
 * 对话链、老前端、curl 都还能发「下周三」,只是面板不再让人打字。
 */

/**
 * 前端这份固定文案。后端 Envelope 的 `user_msg` 到了前端**原样透传**,
 * 不在这里改写 —— 三条硬拦、状态机拒绝的那几句都是后端写好的人话,
 * 前端重新包装一层只会把「该去定级」说成「操作失败」。
 */
export const SUPERVISION_MESSAGES = Object.freeze({
  network: "連不上服務器。檢查網絡,再試一次。",
  authFailed: "登錄信息不對或已過期。刷新頁面重新進一次;還不行就找管理員對一下口令。",
  notFound:
    "監理處置接口還沒開通(接口不存在)。請管理員確認後端已更新到帶監理功能的版本。",
  conflict: "這一步現在做不了(狀態可能剛被別人改過),刷新一下再看。",
  rateLimited: "操作太頻繁,歇幾秒再試。",
  serverError: "服務器出錯了,稍等再試;一直這樣就找管理員。",
  badEnvelope:
    "服務器返回的內容格式不對。文書可能已經出了,先別重複點 —— 找管理員查一下台賬。",
  missingDue: "得選一個整改期限(日期)。沒有期限的隱患不會進超期清單,也就永遠沒人來催。",
  badPhotoId: "複查照片編號不對:要 32 位的編號(在聊天記錄裏那張照片下面能看到)。",
  badGrade: "級別只能選「一般」或「嚴重」。",
  noSelection: "先勾選要確認的隱患。",
  // 2026-08-21:不出文书关掉时要写的原因。措辞给两个真实例子 ——
  // 只说「要写原因」的话人会写「不用了」,而那句话事后什么都回答不了。
  missingReason: "關掉之前要寫清為什麼(比如「白色安全帽,現場核過」「已當場整改」)。這句話會留在台賬裏。",
  // 2026-08-22:改期限时要写的原因。**同样带真实例子**,理由与上面那句一字不差。
  // 这一句还多一层:改期是可以被反复用的动作,一屏「順延」什麼都回答不了,
  // 而「這條被展了幾次期」正是事后最该问的那个问题。
  missingExtendReason:
    "改期限之前要寫清為什麼(比如「連續下雨停工三天」「材料到不了」)。這句話會留在台賬裏。",
  // 2026-08-22:改归属时没选工地。⚠️ 它说的是「**沒選**」而不是「不能為空」——
  // 空(未歸屬)是一档合法选择,措辞里不许暗示它非法。
  missingProject: "先選一個工地(要挪回「未歸屬」就選那一檔)。",
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
  return `服務器返回了看不懂的內容(HTTP ${status}),稍後再試。`;
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
 * 隐患摘要 —— 前五个键镜像 `agents/safety/tools.py` 的 `_ingest_hazards` 返回值
 * (`data.hazards[]`),也是 supervision Agent 只读工具返回的同一形状。
 *
 * ── 为什么后面几个是**可选**的(W10)────────────────────────────────
 * 它们只有 `GET /supervision/hazards` 这条新路会给,老路(工具结果,
 * `hazardsFromToolData`)产出的对象里压根没有这几个键。改成必填的话:
 * 老路那边要么当场类型不过,要么被迫编几个假值填进去(`overdue: false`、
 * `due_date: ""`)—— 后者更坏,那是**拿缺省冒充事实**:界面会理直气壮地
 * 显示「没超期」,而真相只是这条路没带这个字段回来。
 * 所以判据是「拿不到就是 undefined」,渲染层据此显示「—」而不是「否」。
 */
export interface HazardBrief {
  hazard_no: string;
  item: string;
  grade: string;
  status: string;
  needs_grading: boolean;
  /**
   * 现场判的那一档(重大/较大/一般/**待定级**)—— safety 直出、后端原样透传,
   * 与 `grade` 不是一回事:`grade` 只有一般/严重两档,`needs_grading=1` 时它是
   * 映射表给的默认值(一般),**不是有人判过的结论**。
   *
   * 🔴 所以界面上那一格念的是 `severity`,不是 `grade`(与后端 `_hazard_line`
   * 同一条规矩)。念错的后果是屏幕上写着「一般隐患」,而它其实还没人定过级 ——
   * 人会照着这句去签文书,然后被服务端硬拦③拒掉,却不知道该去定级。
   */
  severity?: string;
  /** 状态的中文名,后端算好的。前端也有 `hazardStatusZh` 兜底,两边同一张表。 */
  status_display?: string;
  /** 整改期限(香港日历日 `YYYY-MM-DD`)。null = 还没下过期限,不是「没超期」。 */
  due_date?: string | null;
  /** 期限的人话(「明天」「后天」之类),后端算的 —— 前端一行日期换算都不写。 */
  due_display?: string | null;
  /** 超期与否由后端按业务口径算(pending 与未定级的不算超期,D17 + Codex#11)。 */
  overdue?: boolean;
  /** 归属工地。**空串 = 未归属**(D6),不是「缺失」—— 两者在界面上要分开说。 */
  project_id?: string;
  /**
   * 🔴 **发现这条隐患的那张现场照片**(取件编号)。
   *
   * 补它之前,操作台上一条隐患只有文字:监理要在**看不到照片**的情况下判
   * 一般/严重,还要判是不是「识错了」而按下否决 —— 而**否决这个判断完全依赖
   * 看照片**(帽子到底戴没戴),定级又是签发文书的前置。
   * 2026-08-17 真人反馈只有三个字:「没有照片」。
   *
   * ⚠️ **不是复查照片。** 那张在 `SupervisionDoc.photo_id`(证据链里,一次复查一张);
   * 这张是首次发现那张,一条隐患只有一张。两者混起来就是拿发现时的照片当
   * 「整改后」的证据,而那条红线的全部意义就是事后追责时分得清这两张。
   */
  photo_id?: string;
  /**
   * 真停过工没有(2026-09-18,给步骤条用)。**它是「要不要画复工令那一格」的凭据**,
   * 与后端判「复查合格后必须先出复工令」(Codex#6)用的同一个旗子。
   * `grade === 严重` 只说明"将会停工";签发之后级别不能再改,但只有这个旗子证明"真停过"。
   * 老路(工具返回)没有它 —— 那时步骤条退回按级别推。
   */
  was_suspended?: boolean;
  /**
   * 不出文书关掉时写的理由(只有 `dismiss` 会写;`null` = 这条不是那么关掉的)。
   * 步骤条靠它分辨 closed 的两条进路:复查合格销项 vs 识错了关掉 —— 后者只画
   * 「確認 → 關掉」,**不许把签发/复查全打勾**(那是给一条不存在的隐患编证据链)。
   */
  closed_reason?: string | null;
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
 * 取一个「可能是 null 的字符串」字段。**空串一律归成 null**:这几个字段
 * (期限、销项时刻、复查照片编号)在后端就是「有值 / 没有」的二态,
 * 空串与 null 说的是同一件事,前端多一种表示法就多一个 if 会漏。
 *
 * ⚠️ 别拿它去读 `project_id` —— 那个字段的空串是**有意义的值**(未归属),
 * 归成 null 就把「没人认领的一批」变成了「不知道归属」。
 */
function nullableTextOf(rec: Record<string, unknown>, key: string): string | null {
  return textOf(rec, key) || null;
}

/**
 * 一条隐患 → `HazardBrief`;**认不出返回 null,一个错都不抛**(调用方跳过它)。
 *
 * 两条路共用这一个解析器(工具结果 / GET 端点),形状才不会分叉 ——
 * 分叉的表现是「聊天里那张卡能点复查、操作台上那张不能」,而两处看着一模一样。
 *
 * 只有 `hazard_no` 是**不能兜底**的字段:没有编号的条目在界面上什么也做不了
 * (八个动作端点 + 详情端点全靠它定位),所以那种条目直接跳过。其余给保守缺省:
 * 事项空着按「(未写明事项)」显示,状态空着按 pending —— pending 是权限最小的一档
 * (不算整改率、不进超期清单、不能被升级),猜错也不会让人去点一个不该点的按钮。
 *
 * ⚠️ 扩展字段**只在源里真有这个键时才挂上去**(下面那串条件展开),不是无脑
 * 填 undefined:老路那五个键的对象形状因此**一个字节不变**,下游做深比较的地方
 * (单测的 toEqual、React 的 memo 比较)不用跟着改。少一处要同步的地方就少一处会漏。
 */
function toHazardBrief(entry: unknown): HazardBrief | null {
  const rec = asRecord(entry);
  if (!rec) return null;
  const hazardNo = textOf(rec, "hazard_no");
  if (!hazardNo) return null;
  return {
    hazard_no: hazardNo,
    item: textOf(rec, "item") || "(未寫明事項)",
    grade: textOf(rec, "grade"),
    status: textOf(rec, "status") || "pending",
    needs_grading: rec.needs_grading === true,
    ...(typeof rec.severity === "string" ? { severity: rec.severity.trim() } : {}),
    ...(typeof rec.status_display === "string"
      ? { status_display: rec.status_display.trim() }
      : {}),
    ...("due_date" in rec ? { due_date: nullableTextOf(rec, "due_date") } : {}),
    ...("due_display" in rec ? { due_display: nullableTextOf(rec, "due_display") } : {}),
    // overdue 只认真布尔(同 needs_grading 那条规矩):字符串 "false" 是真值,
    // 松一点就等于给每一条隐患都挂上「已超期」的红标。
    ...(typeof rec.overdue === "boolean" ? { overdue: rec.overdue } : {}),
    // project_id 走 typeof 判断而不是 textOf:空串是「未归属」这个**值**,不是缺失。
    ...(typeof rec.project_id === "string" ? { project_id: rec.project_id.trim() } : {}),
    // 发现照片的取件编号。**空/畸形一律当没有** —— 界面据此决定渲不渲缩略图,
    // 塞个半截串进去只会得到一个破图框(而破图与「这条本来就没照片」在屏幕上
    // 长得一样,那正是本仓反复防的那类)。判据与 isPhotoId 同一个正则。
    ...(isPhotoId(textOf(rec, "photo_id")) ? { photo_id: textOf(rec, "photo_id") } : {}),
    // 步骤条要的两个键(2026-09-18)。was_suspended 只认真布尔 —— 库里是 0/1,
    // 后端负责转;真漏转了这里当成没有,**不许把 1 当 true**(同 overdue 那条规矩)。
    ...(typeof rec.was_suspended === "boolean" ? { was_suspended: rec.was_suspended } : {}),
    ...("closed_reason" in rec ? { closed_reason: nullableTextOf(rec, "closed_reason") } : {}),
  };
}

/**
 * 从工具结果的 `data` 里取隐患清单。**认不出就返回空数组,一个错都不抛** ——
 * 这是渲染路径上的函数,抛出去就是整条消息渲染不出来(而它只是一张附加卡片)。
 * 逐条的判据与缺省见 `toHazardBrief`。
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
  return raw.flatMap((entry) => {
    const brief = toHazardBrief(entry);
    return brief ? [brief] : [];
  });
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

/**
 * 改一条隐患的字段,返回**新数组**(不可变;找不到编号就原样返回)。
 *
 * 🔴 **改了 `status` 就必须把 `status_display` 一起丢掉**(除非这次 patch 自己带了新的)。
 *
 * 2026-08-16 手工验当场抓到的:确认一条隐患之后,动作按钮已经变成「签发暂停令」、
 * 展开的证据链也写着「已确认待处置」,而**行首那颗徽章还写着「待确认」**——
 * 因为 `status_display` 是后端**按旧状态**拼好的那句中文,patch 只改了 `status`,
 * 它原封不动留着。渲染那行是 `status_display || hazardStatusZh(status)`,
 * 于是旧标签一直赢。
 *
 * 这比「少显示一格」坏得多:工友点了确认,屏幕上还写着待确认,他会再点一次 ——
 * 而下一颗按钮是签发法律文书。丢掉之后渲染自然回落到 `hazardStatusZh(status)`,
 * 那张词表本来就是后端那份的镜像,不会有第二种说法。
 *
 * 为什么不在这里顺手算一个新的 `status_display`:那等于让这一层替后端做措辞决定。
 * 回落到镜像词表是**同一份真相**,而现造一个是第二份。
 */
export function patchHazard(
  list: readonly HazardBrief[],
  hazardNo: string,
  patch: Partial<HazardBrief>,
): HazardBrief[] {
  const stale = "status" in patch && !("status_display" in patch);
  return list.map((h) => {
    if (h.hazard_no !== hazardNo) return h;
    const next = { ...h, ...patch };
    if (stale) delete next.status_display;
    return next;
  });
}

/** 从清单里划掉一条。否决成功后用它 —— 那一行在库里**是真删了**(POST /supervision/reject)。 */
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
 * 界面上的**单条**处置动作(批量确认 `confirm` 不在这儿,它走勾选那条路)。
 * 与端点**不是一一对应**:`reinspect` 打的是 `reinspect-result`
 * (它一个动作两种结论),所以下面留了一张映射表。
 *
 * 这张表的顺序只是枚举顺序,**不是按钮顺序** —— 按钮顺序由 `availableActions`
 * 逐档给,主要动作排第一(手最先够到的那颗要是对的那颗)。
 */
export const DISPOSAL_ACTIONS = [
  "grade",
  "notice",
  "suspend",
  "reinspect",
  "resume",
  "escalate",
  "reject",
  "dismiss",
  // 2026-08-22 的两处**订正**。放进同一张表是因为它们在界面上就是并排的按钮
  // (同一条隐患行上点开的同一个抽屉),走同一套「填参数 → POST → 解信封」的流程。
  // 但它们与上面那批的**性质不同**,别混着读:
  //   上面那批答的是「这件事进展到哪一步了」,会签发法律文书;
  //   这两颗答的是「当初记错了」,一份文书都不出。
  "extend",
  "reassign",
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
    reject: "reject",
    dismiss: "dismiss",
    extend: "extend",
    reassign: "reassign",
  });

/** 签发那两颗按钮共用的字(理由见 `ACTION_LABEL` 里 notice 那条)。 */
export const ISSUE_LABEL = "簽發處置文書";

/** 「会发一张纸出去」的两颗:文书种类由级别带出,按钮上不让人选。 */
export const ISSUE_ACTIONS: ReadonlySet<DisposalAction> = new Set<DisposalAction>([
  "notice",
  "suspend",
]);

/**
 * 这个级别签发时**会带出哪几份文书**(`DOC_TYPE_ZH` 的键,顺序 = 后端产出顺序)。
 *
 * 镜像 supervision_api 的两条路:`_work_notice` 一份;`_work_suspend` 三份原子产出
 * (通知单 → 暂停令 → 致建设单位报告)。级别认不出 = 一份都不承诺(空数组),
 * 别退到「一般」—— 那等于替人定了级。
 */
export function issuedDocTypes(grade: string): readonly string[] {
  if (grade === GRADE_NORMAL) return ["notice"];
  if (grade === GRADE_SEVERE) return ["notice", "suspension", "owner_report"];
  return [];
}

/**
 * 按钮上的字。用「签发」而不是「生成」—— 这几份是要拿去签字盖章的法律文书。
 *
 * `reject` 那句刻意把**什么时候用它**和**会发生什么**都写在按钮上:
 * 它治的是自动识图的误报(照片里那顶帽子其实戴着),而它是**真删**不是标记 ——
 * 只写「否决」的话,人会以为跟「驳回」一样还能翻出来看。
 */
export const ACTION_LABEL: Readonly<Record<DisposalAction, string>> = Object.freeze({
  grade: "人工定級",
  // 🔴 通知单与暂停令**同一句字**(2026-09-18 用户反馈:「出监理通知单和工程暂停令
  //    应该是根据事件等级自动带出的」)。逻辑上它们本来就是带出的 —— `availableActions`
  //    对一般只给 notice、对严重只给 suspend,后端还有硬拦①② —— 但两颗按钮字不一样,
  //    监理扫过一屏看到两种按钮,读成「要我选」。现在按钮统一,**带出哪几份写在按钮
  //    下面那一行**(`issuedDocTypes`),暂停令那条路照旧要过二次确认(那句话里
  //    仍然写明「三份」「停工」)。
  notice: ISSUE_LABEL,
  suspend: ISSUE_LABEL,
  reinspect: "登記複查結論",
  resume: "簽發工程復工令",
  escalate: "上報主管部門",
  reject: "否決(誤報,刪掉)",
  // 与 reject 只差一个字都不行:那两颗会并排出现在不同状态的隐患上,
  // 而后果完全不同(一个整行删掉、一个留行留理由)。
  dismiss: "關掉(不出文書,要寫原因)",
  // 这两颗都把「不出文书」写在按钮上。写它的理由与 dismiss 那颗一样:
  // 监理点这几颗之前,心里的问题是「这一下会不会又发一张纸出去」。
  extend: "改整改期限(不出文書,要寫原因)",
  reassign: "改工地(不出文書)",
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
 *   · `needs_grading=1` → **签发那几件一件都不给**(Codex#11:未知风险不许按一般隐患
 *     走完闭环。硬拦③排在级别方向那两道之前,这里也一样)。留下的是定级;
 *     pending 时还留一个「否决」,理由见函数体里那段;
 *   · grade=严重 的 open → 只给 `suspend`,不给 `notice`(硬拦①);
 *   · grade=一般 的 open → 只给 `notice`,不给 `suspend`(硬拦②)。
 *
 * `grade` 在 pending / open 都给:`_GRADABLE_STATUSES` 就是这两档,而且
 * 「确实要停工,先把它改定为严重隐患」正是硬拦②那句拒绝话给出的出路。
 *
 * `reject`(W10)**只在 pending 一档**:服务端硬拦「已经确认过的不许删」(409),
 * 别处给出来就是一颗点下去必挨骂的按钮。
 */
/**
 * 这条隐患**有人判过的**级别;没人判过就是 `null`。
 *
 * 🔴 **`hazard.grade` 永远有值,所以它答不了这个问题。** `needs_grading=1` 时那个值是
 * `agents/supervision/grading.py` 的映射表给的**默认档(一般)**,不是结论。
 *
 * 2026-08-16 手工验抓到过把它当结论用的两个后果:
 *   · 界面上「定级为一般」那颗按钮被判成「当前值」而**禁用** —— 监理认定它就是
 *     一般隐患,却点不下去,屏幕上唯一能点的是「严重」。而硬拦②防的正是
 *     「一般隐患签了暂停令 = 平白停一片人的工」;
 *   · 对外念出「这条是一般隐患」,而它真实级别是未知的(`_hazard_line` 那条同源约束)。
 *
 * 后端 `_advise` 与端点 `_require_graded` 认的都是同一个旗子(`needs_grading`),
 * 不是级别字段本身。
 */
export function currentGrade(hazard: HazardBrief): string | null {
  return hazard.needs_grading ? null : hazard.grade;
}

export function availableActions(hazard: HazardBrief): DisposalAction[] {
  if (hazard.needs_grading) {
    // 未定级这一档只剩定级一件事(Codex#11:未知风险不许按一般隐患走完闭环)——
    // 但 **pending 时「否决」要一起给**。
    //
    // 🔴 这一条是 2026-08-16 W10 定的,方向跟直觉相反,别再"顺手收窄"回去:
    //    误报**恰恰最常落在这一档** —— safety 认出一个受控词表外的违规项,就是
    //    needs_grading=1 + pending。逼人先给一个「根本不是隐患」的东西定级才准删,
    //    等于往台账里留一条判过级的假隐患,而定级是要进法律文书的动作。
    //    服务端 `delete_pending` 只看 `status='pending'`,压根不关心 needs_grading;
    //    所以这里给出来仍然**比服务端窄**(非 pending 的 needs_grading 一律不给),
    //    没有破坏本函数头注那条「只许更窄、不许更宽」的红线。
    //
    // 🔴 `reassign`(2026-08-22)在这一档**也要给**,理由与上面那条同构:
    //    「归错了工地」和「定没定级」是两件互不相干的事,而未归属的隐患
    //    恰恰最常落在 pending(工友拍照时没在顶栏选工地)。
    //    逼人先定级才准挪工地,等于让一条本该归到 A 工地的隐患在未归属那堆里再待一轮。
    return hazard.status === "pending" ? ["grade", "reject", "reassign"] : ["grade"];
  }
  switch (hazard.status) {
    case "pending":
      // 确认(pending → open)走批量勾选那条路,不在单条动作里 —— 它是个批量端点。
      // 「否决」跟它是同一个岔路口的另一条:确认 = 这确实是隐患,否决 = 识错了,删掉。
      // `reassign` 排最后:它是订正,不是处置。**pending 这一档尤其要有它** ——
      // 「拍的时候忘了选工地」这件事,发现的时机就在监理翻待确认清单的这一刻。
      return ["grade", "reject", "reassign"];
    case "open":
      // 第三颗 `dismiss`(2026-08-21)是**纠错出口**,排在最后:主要动作在前。
      // 🔴 它非有不可:在它之前 open 只有两条出口而两条都要签发法律文书,
      //    于是一条识别错了的隐患关不掉 —— 唯一的出路是为一个不存在的隐患
      //    真的签一份《监理通知单》,再拍照登记「复查合格」。
      //    而「确认」这一下是单向门(否决只删 pending),确认之后连否决都没了。
      // 第四颗 `reassign`(2026-08-22)排在最末:它是订正,不是处置。
      return hazard.grade === GRADE_SEVERE
        ? ["suspend", "grade", "dismiss", "reassign"]
        : ["notice", "grade", "dismiss", "reassign"];
    case "notified":
    case "suspended":
      // `extend`(2026-08-22)只在这两档 —— 只有它们有一个在跑的期限
      // (镜像后端 `DUE_CHANGEABLE_STATUSES`)。排在复查之后:主要动作在前,
      // 「宽限几天」是例外情况。
      return ["reinspect", "extend"];
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

/**
 * 要不要填整改期限(`due_phrase` 收用户原话,换算全在后端 dates.py)。
 *
 * `extend`(2026-08-22)当然也要 —— 它整个动作就是「换一个期限」。
 * 三处共用同一件后端换算(`_resolve_due`),日期框送的 ISO 串三处都认。
 */
export function actionNeedsDuePhrase(action: DisposalAction): boolean {
  return action === "notice" || action === "suspend" || action === "extend";
}

/**
 * 这一行**日期框里显示的**、也是**请求里发的**那个期限(2026-09-18)。
 *
 * 判据:人改过就用人改的;没改过,签发(notice / suspend)用后端按级别预填的那个
 * (`HazardListResult.dueDefaults`);`extend` **不预填** —— 它整个动作就是「换一个期限」,
 * 预填成默认值等于替监理决定展到哪天,而改期是要写理由、事后并排看的动作。
 *
 * 🔴 三处调用(行内日期框的 `value`、举手时的校验、真发请求)**必须都走这一个函数**。
 *    各算一遍的下场是「屏幕上写着 8/27、发出去的是空串」,后端回一句「得写明期限」,
 *    而那一格明明填着日期 —— 监理会以为是自己按错了。
 *
 * 没有默认值(老后端 / 级别认不出)就是空串,让 `actionBody` 的必填校验说话,不编日期。
 */
export function effectiveDuePhrase(input: {
  typed: string;
  action: DisposalAction;
  grade: string;
  dueDefaults: Readonly<Record<string, string>>;
}): string {
  const typed = input.typed.trim();
  if (typed) return typed;
  if (!ISSUE_ACTIONS.has(input.action)) return "";
  return input.dueDefaults[input.grade] ?? "";
}

/**
 * 要不要写原因。两颗:`dismiss`(把隐患关掉)与 `extend`(改期限)。
 *
 * 判据不是「会不会写库」,是**「这一下事后能不能被问责」**:
 *   · 关掉  —— 事后唯一能回答「这条为什么关的」的地方;
 *   · 改期  —— 它是**可以被反复使用**的动作,每一次单看都合理
 *     (下雨了、材料没到、人手不够),只有把历次理由并排看才看得出问题。
 *     没有理由的留痕等于没有留痕。
 *
 * ⚠️ 两颗的长度下限是**两个常量**(`DISMISS_REASON_MIN_LEN` / `EXTEND_REASON_MIN_LEN`),
 * 眼下取值相同 —— **别合并**。理由见那两个常量各自的注释。
 */
export function actionNeedsReason(action: DisposalAction): boolean {
  return action === "dismiss" || action === "extend";
}

/**
 * 要不要选一个工地。只有 `reassign` 一颗 —— 它整个动作就是「换一个工地」。
 *
 * 🔴 界面上这一格必须是**选择器**(只列真实存在的工地)加一档「未歸屬」,
 * 不能是自由输入框。后端刻意**不校验目标工地是否存在**:`projects` 与
 * `hazards.project_id` 之间本来就没有外键(D6 的取舍),在那儿补一道半套的
 * 完整性校验没有意义。所以**这道闸的正确位置就在界面上**。
 * 做成输入框的话,打错一个字 = 那条隐患挪进一个不存在的工地,
 * 从此两边都筛不到它,而且没有任何报错。
 */
export function actionNeedsProject(action: DisposalAction): boolean {
  return action === "reassign";
}

/** 要不要挂复查照片(方案 §5.2 红线:拿不到照片就没有任何路径能改成 closed)。 */
export function actionNeedsPhoto(action: DisposalAction): boolean {
  return action === "reinspect";
}

// ---------------------------------------------------------------------------
// 動作優先(2026-09-18 設計審查 FINDING-003)
// ---------------------------------------------------------------------------

/**
 * 一條隱患的**主按鈕**:= 步驟條上的當前步。pending 是「確認」(走批量端點、只帶這一條);
 * 其餘是 `availableActions` 的第一顆 —— 那張表本來就是「主要動作在前」排的,所以
 * open 未定級時主按鈕是定級(硬攔③:任何簽發都會被服務端拒),定過級才是簽發;
 * 走完流程的是 null。
 *
 * 為什麼要有這一層:在此之前處置區把**所有可用動作的輸入框一次全擺出來**
 * (`needsDue = actions.some(...)`),一條 open 的隱患點開就是日期框 + 關掉的原因 +
 * 挪工地下拉 + 五顆按鈕 —— 人還沒說要幹什麼,先看到三件事要填。用戶反饋原話:
 * 「太繁瑣、UI 不明確」。現在一條只露一顆主按鈕 + 「更多 ▾」,點了哪個才展開哪一格。
 */
export type PrimaryAction = DisposalAction | "confirm";

export function primaryAction(hazard: HazardBrief): PrimaryAction | null {
  if (hazard.status === "pending") return "confirm";
  return availableActions(hazard)[0] ?? null;
}

/** 「更多 ▾」裏的那幾顆 = 可用動作減去主按鈕,順序不變,**一個都不丟**(功能不減)。 */
export function secondaryActions(hazard: HazardBrief): DisposalAction[] {
  const primary = primaryAction(hazard);
  return availableActions(hazard).filter((a) => a !== primary);
}

/** 選了這個動作之後,那一格要露出哪幾個輸入。與四個 `actionNeeds*` 同源,只是換成一次拿全。 */
export function actionFields(action: DisposalAction): {
  due: boolean;
  reason: boolean;
  photo: boolean;
  project: boolean;
} {
  return {
    due: actionNeedsDuePhrase(action),
    reason: actionNeedsReason(action),
    photo: actionNeedsPhoto(action),
    project: actionNeedsProject(action),
  };
}

/**
 * 要不要二次确认。**判据是「这一下能不能反悔」**,不是「会不会写库」:
 *   · `suspend` —— 一次停掉一片人的工;
 *   · `escalate` —— 对施工单位的正式指控,报到建设主管部门;
 *   · `reject`(W10)—— 那一行从库里**删掉**,连同它的登记时刻、照片关联一起没了。
 * 三者都无法在系统里撤销(本批不做重签,见 `_GRADABLE_STATUSES` 头注),
 * 手滑一下的代价落在真实工地上,所以必须多问一句。
 *
 * `reject` 这一颗还多一层理由:它跟「人工定级」并排长在同一条 pending 隐患上,
 * 两颗按钮隔着一格 —— 手指点偏一格的代价是一条真实存在的隐患从台账上消失,
 * 而屏幕上只会少一行,没有任何提示。
 *
 * `notice` 不问:它是监理日常动作,而且期限那一格本来就要动手打字 ——
 * 每一颗按钮都弹确认框的下场是人闭着眼点「确定」,那时真正该拦的三颗也就废了。
 */
/**
 * **不会产生任何一份文书**的那几颗动作(2026-08-25 设计审计 D8)。
 *
 * 界面拿它决定按钮长相:这几颗走描边,不走实心 —— 实心留给「发一张纸出去」
 * (监理通知单 / 复工令)和「按下去回不来」(暂停令 / 上报 / 关掉)。
 *
 * 🔴 **判据是「出不出纸」,不是「重不重要」。** 重要是主观的,下一个人会有不同的
 * 排序;而「这一下会不会有一份盖章的文书发出去」只有一个答案 —— 而且这几颗的
 * `ACTION_LABEL` 上本来就写着「不出文書」,界面和判据说的是同一句话。
 *
 * ⚠️ `dismiss` **不在这里**:它虽然也不出纸(标签上写着「不出文書,要寫原因」),
 *    但它是**终态** —— `closed` 在 `ALLOWED_TRANSITIONS` 里出边是空集,关掉之后
 *    系统里没有任何一条路能开回来。它归红色那一档,由 `actionNeedsConfirm` 管。
 *    两个集合都收 dismiss 的话,红色会被描边盖掉,而那颗恰恰是最需要红的。
 * ⚠️ `reject` 也不在这里:它整行删数据,界面上单独走 outline + 垃圾桶图标
 *    (理由在 supervision.tsx 那个 variant 三元上方),判据与本集合无关。
 */
export const DOCLESS_ACTIONS: ReadonlySet<DisposalAction> = new Set<DisposalAction>([
  "extend",
  "reassign",
  "reinspect",
]);

export function actionNeedsConfirm(action: DisposalAction): boolean {
  return (
    action === "suspend" ||
    action === "escalate" ||
    action === "reject" ||
    // `dismiss`(2026-08-21):`closed` 是**终态**(ALLOWED_TRANSITIONS 里它的
    // 出边是空集),关掉之后系统里没有任何一条路能把它开回来。
    // 判据仍是那句「这一下能不能反悔」—— 不能,所以要问。
    action === "dismiss"
  );
}

/** 二次确认框里那句话。写清楚**后果**和**不可撤销**,不写「确定吗?」。 */
export function confirmPrompt(action: DisposalAction, hazard: HazardBrief): string {
  if (action === "reject") {
    return (
      // 这句话是拿去弹框的,里面不许写 markdown 记号(** 会原样显示成星号)。
      `要把隱患「${hazard.item}」(${hazard.hazard_no})從台賬裏刪掉嗎?\n\n` +
      "這一條會整行刪掉,不是標記成已處理 —— 刪了就找不回來了。\n" +
      "只有還沒確認的隱患能這麼刪;確實是隱患的,請改用「確認」。"
    );
  }
  if (action === "dismiss") {
    return (
      `要把隱患「${hazard.item}」(${hazard.hazard_no})關掉嗎?\n\n` +
      "不出任何文書,但這一行會留在台賬裏,寫着你填的原因。\n" +
      "🔴 關掉之後就是終點,系統裏沒有任何一條路能把它開回來。\n" +
      "還不確定的話,先別關 —— 它留在「在辦」裏不會催你。"
    );
  }
  if (action === "suspend") {
    return (
      `要為隱患「${hazard.item}」(${hazard.hazard_no})一次簽發三份文書嗎?\n` +
      "《監理通知單》+《工程暫停令》+《致建設單位報告》\n\n" +
      "暫停令是法律文書,簽字蓋章後據以停工。系統裏不能撤銷,簽錯只能另走複查/升級流程。"
    );
  }
  if (action === "escalate") {
    return (
      `要為隱患「${hazard.item}」(${hazard.hazard_no})出具《監理報告》報建設主管部門嗎?\n\n` +
      "這是對施工單位的正式指控,舉證鏈是「通知過 + 期限到了 + 複查過 + 仍未整改」。\n" +
      "系統裏不能撤銷。"
    );
  }
  // ⚠️ **这个兜底 2026-08-22 从「默认返回上报那段话」改成了显式抛。**
  //    原来那份是 `return 上报那段` —— 也就是说任何一个没在上面列出来的动作,
  //    弹出来的都是「要出具《监理报告》报建设主管部门吗?」。
  //    今天它碰巧不触发(`actionNeedsConfirm` 只对四颗返回 true,四颗都在上面),
  //    但那是**两个函数之间的巧合**:哪天有人把 `extend` 加进 actionNeedsConfirm
  //    (它是可以被反复用的动作,有人会想加),监理点「改期限」会看到一句
  //    「要报主管部门吗」—— 而他多半会点确定。
  //    改期与改归属**刻意不需要确认**:判据仍是那句「这一下能不能反悔」,
  //    两者都能再改回来,而且都留痕。
  throw new SupervisionContractError(
    `「${ACTION_LABEL[action]}」還沒寫確認話術 —— 別讓它落到別的動作那句上。`,
  );
}

// ---------------------------------------------------------------------------
// 请求体拼装(校验在这里做,省一次白跑;服务端仍会再校验一遍)
// ---------------------------------------------------------------------------

/** 产物编号:`core/artifacts.py` 是 `uuid4().hex` —— 32 位小写十六进制。 */
const ARTIFACT_ID_PATTERN = /^[0-9a-f]{32}$/;

/**
 * 批量确认的二次确认话术(2026-08-21 补)。
 *
 * ===========================================================================
 * 🔴 为什么批量确认必须问一句 —— 它是这个面板上**唯一一扇单向门**
 * ---------------------------------------------------------------------------
 * 「确认」把隐患从 `pending` 推到 `open`,而这一下**亲手拆掉了唯一的逃生口**:
 * 后端的删除是 `DELETE … WHERE status = 'pending'`(`db/hazards.py`)——
 * 只有还没确认的隐患才删得掉。确认之后,一条识别错了的隐患
 * (比如把白色安全帽认成没戴)就**再也删不掉了**,唯一的出路是
 * 为一个不存在的隐患真的签发一份法律文书,再拍照登记「复查合格」。
 *
 * 而在这之前,`全選 → 確認選中 N 條` 只有一个 `disabled={busy}`,
 * 旁边就是「全選」—— 而 `actionNeedsConfirm` 那三颗(suspend/escalate/reject)
 * 全都有确认条。**护栏最少的那一颗,后果却是不可逆的。**
 *
 * 措辞照 `confirmPrompt` 的规矩:说清楚**后果**和**不可撤销**,不写「确定吗?」,
 * 并且给出**这一刻还能做的事**(先扫一眼、认错的先否决)——
 * 一句只讲坏消息的确认框,人只会闭着眼点「确定」。
 * ⚠️ 里面不许写 markdown 记号(** 会原样显示成星号),同 `confirmPrompt`。
 */
export function confirmBatchPrompt(count: number): string {
  return (
    `要把選中的 ${count} 條隱患都確認下來嗎?\n\n` +
    "確認之後這些隱患就進了正式台賬,不能再「否決」刪掉 ——\n" +
    "只能走「簽發文書 → 複查合格」這條路收尾。\n\n" +
    "先掃一眼有沒有認錯的;認錯的請先逐條否決,再確認剩下的。"
  );
}

/** 签发人名字记在 localStorage 的哪个键。
 *
 * ⚠️ **与打卡那个 `gyt:checkin:worker-name` 是两个键,别复用。**
 * 那个存的是**工友**自己的名字(他在这台手机上打卡用的),而这里要的是
 * **监理**的名字。同一台手机上两者完全可能是不同的人 —— 复用等于把
 * 签发人记成别人,而那比不记更坏:一片空白至少诚实。
 */
export const SIGNER_NAME_STORAGE_KEY = "gyt:supervision:signer-name";

/** 关掉理由的最短字数,**镜像后端 `supervision_api._DISMISS_REASON_MIN_LEN`**。
 * 漂了的表现不算静默(后端会回 400,那句人话原样上屏),但会让人以为是自己按错了。 */
export const DISMISS_REASON_MIN_LEN = 4;

/**
 * 改期理由的最短字数,**镜像后端 `supervision_api._DUE_REASON_MIN_LEN`**(2026-08-22)。
 *
 * ⚠️ **今天它与 `DISMISS_REASON_MIN_LEN` 取值相同,而这是两个常量,不许合并。**
 * 两者挡的是同一种敷衍(「。」「1」),但量的是两件事:那个是「为什么把一条隐患关掉」,
 * 这个是「为什么把期限往后挪」。改期是**可以被反复使用**的动作(一条隐患能一路展到
 * 下个月),哪天有人觉得它该写得更详细,动的是这一个 —— 合并的话会连带把
 * 「关掉」的门槛也抬高,而那条后端注释里明写着「不设更高」。
 * 本仓在「眼下相等就合并」上已经吃过亏(CLAUDE.md 前端覆盖件那两个数)。
 */
export const EXTEND_REASON_MIN_LEN = 4;

/** 一次批量确认的条数上限,镜像 supervision_api._MAX_CONFIRM_BATCH。 */
export const MAX_CONFIRM_BATCH = 200;

/** `POST /supervision/confirm` 的请求体。去重保序 —— 双击会把同一条发两遍。 */
export function confirmBody(hazardNos: readonly string[]): { hazard_nos: string[] } {
  const nos = [...new Set(hazardNos.map((n) => n.trim()).filter(Boolean))];
  if (nos.length === 0) {
    throw new SupervisionContractError(SUPERVISION_MESSAGES.noSelection);
  }
  if (nos.length > MAX_CONFIRM_BATCH) {
    throw new SupervisionContractError(`一次最多確認 ${MAX_CONFIRM_BATCH} 條,分幾次來。`);
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
  /**
   * dismiss / extend 必填:为什么。原样进台账,是事后唯一能回答这个问题的地方。
   *
   * 两个动作共用这一个字段,而**长度下限是两个常量**
   * (`DISMISS_REASON_MIN_LEN` / `EXTEND_REASON_MIN_LEN`)—— 见那两个常量的注释。
   */
  reason?: string;
  /**
   * reassign 必填:挪到哪个工地。
   *
   * 🔴 **空串是合法值(挪回「未歸屬」),所以这里的判据只能是「这个键在不在」。**
   * 写成「值非空才算填了」的话,「挪回未归属」这个动作在结构上就不存在 ——
   * 而它恰恰是最常用的反向操作(拍的时候选错了工地)。
   * 后端 `_work_reassign` 用的是同一条判据(`"project_id" not in body`),两边同源。
   */
  projectId?: string;
  /**
   * 签发人**自己报的名字**(2026-08-21)。所有动作都可以带,后端记进
   * `hazard_docs.issued_by`。
   *
   * 🔴 **不是认证身份**:这套系统只有一把共享口令、没有角色。它能提供的只是
   * 「有个名字总比一片空白强」—— 在它之前,「这份《工程暂停令》是谁签的」
   * 在系统里**无解**。完整推演在后端 `db/hazards.DocDraft.issued_by`。
   *
   * 可以不填。**别在前端做成必填** —— 后端那边到这一步文书已经渲染落盘了,
   * 为一个补充性的审计字段挡住法律文书的签发不划算(理由同后端 `_issued_by`)。
   */
  issuedBy?: string;
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
    throw new SupervisionContractError("沒説是哪條隱患(缺隱患編號)。");
  }
  const body: Record<string, unknown> = { hazard_no: hazardNo };

  // 签发人放在这儿(**所有早返回分支之前**)是刻意的:每个动作各自 return,
  // 塞在任何一个分支里都等于只有那一条路记得下人,而漏掉的那几条不会报错。
  // 空就不发这个键 —— 后端 `_issued_by` 对缺失和空串一视同仁记 NULL,
  // 但少发一个空键让请求体里「有没有报名字」一眼可见。
  const issuedBy = (input.issuedBy ?? "").trim();
  if (issuedBy) {
    body.issued_by = issuedBy;
  }

  if (action === "grade") {
    const grade = (input.grade ?? "").trim();
    if (!(HAZARD_GRADES as readonly string[]).includes(grade)) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.badGrade);
    }
    body.grade = grade;
    return body;
  }

  if (action === "dismiss") {
    const reason = (input.reason ?? "").trim();
    // 长度下限镜像后端 `_DISMISS_REASON_MIN_LEN`(4)。前端先说一句是体验,
    // **不是安全边界** —— 服务端那一份一条都不能省(同本函数头注)。
    if (reason.length < DISMISS_REASON_MIN_LEN) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.missingReason);
    }
    body.reason = reason;
    return body;
  }

  if (action === "extend") {
    // 🔴 **这个分支必须排在下面那个 `actionNeedsDuePhrase` 前面。**
    //    那个分支只塞 due_phrase 就 return,而 extend 是**两样都要**的唯一一个动作
    //    (notice / suspend 只要期限)。排在后面的表现极其阴险:监理认真写的原因
    //    被静默丢掉,后端回一句「改期限要写清为什么」,而那句话就在他刚填过的
    //    输入框旁边 —— 他会以为是自己写得太短,再写一遍,再被拒。
    const duePhrase = (input.duePhrase ?? "").trim();
    if (!duePhrase) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.missingDue);
    }
    const reason = (input.reason ?? "").trim();
    if (reason.length < EXTEND_REASON_MIN_LEN) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.missingExtendReason);
    }
    body.due_phrase = duePhrase;
    body.reason = reason;
    return body;
  }

  if (action === "reassign") {
    // 🔴 判据是 `undefined` 而不是真值 —— 空串是合法值(挪回「未歸屬」)。
    //    写成 `if (!projectId) throw` 的话,「挪回未归属」永远发不出去,
    //    而那正是这个动作最常见的反向用法。
    if (input.projectId === undefined) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.missingProject);
    }
    body.project_id = input.projectId.trim();
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
      throw new SupervisionContractError("複查結論只能是合格或不合格,得由人來下。");
    }
    const photoId = (input.afterPhotoId ?? "").trim().toLowerCase();
    if (!ARTIFACT_ID_PATTERN.test(photoId)) {
      throw new SupervisionContractError(SUPERVISION_MESSAGES.badPhotoId);
    }
    body.result = input.result;
    body.after_photo_id = photoId;
    return body;
  }

  // resume / escalate / reject 只要编号(reject 的「只有 pending 能删」由服务端拦,
  // 前端这边靠 availableActions 不把按钮给出去 —— 两道都不能省)。
  return body;
}

// ---------------------------------------------------------------------------
// 响应解析
// ---------------------------------------------------------------------------

/**
 * `documents` 数组里的一项 —— 前四个键镜像 supervision_api._IssuedDoc.as_payload()。
 *
 * 后面几个是**可选**的:只有 `GET /supervision/hazards/{no}`(证据链)那条路会给,
 * 六个动作端点的回执里没有。可选的理由同 `HazardBrief` —— 拿不到就是 undefined,
 * 不许拿缺省冒充事实。
 */
export interface SupervisionDoc {
  doc_type: string;
  doc_no: string;
  /** 取件编号。**为 null 时绝不渲染下载链接**,见 documentUrl。 */
  artifact_id: string | null;
  /**
   * 文件名。**复查记录行是 `null`** —— 那一行没有文件(`artifact_id` 也是 null)。
   *
   * 🔴 这里原来写的是 `string`,与后端详情端点的真实返回对不上
   * (`GET /supervision/hazards/{no}` 对 reinspect 行回的就是 `"filename": null`,
   *  契约在 supervision_api.py 的模块头注)。
   * 没炸只是因为**两条产出路径都不走类型检查**:动作回执那条来自
   * `parseActionEnvelope`(内部 `as` 断言),详情那条来自 `parseHazardDetailEnvelope`;
   * 而 `scripts/frontend-tests` 那份 tsc **不在 `make test-frontend` 的路径上**
   * (CI 跑的是 vitest)。于是「类型说不可能为 null、运行时天天为 null」并存了一阵子。
   * 2026-08-16 给测试里的工厂补显式返回类型标注时才暴露出来。
   */
  filename: string | null;
  /** 文书类型的中文名,后端算好的;前端也有 `docTypeZh` 兜底,两边同一张表。 */
  doc_type_display?: string;
  /**
   * 🔴 **这一次复查拍的那张照片**,只有 `doc_type=reinspect` 的行才有。
   *
   * **不是**隐患首次发现那张(那张在 `hazards.photo_id` 里,压根不在这个数组)。
   * 混起来的后果是拿发现时的照片当「整改后」的证据摆在证据链里 —— 界面上一切正常,
   * 而那正是复查合格能把隐患销项的凭据。所以这个字段只从复查行读,别处一律没有。
   */
  photo_id?: string | null;
  /** 复查结论 `pass` / `fail`,只有复查行有。中文名走 `result_display`。 */
  result?: string;
  /** 复查结论的中文:pass=合格、fail=不合格。**英文枚举值不许上屏。** */
  result_display?: string;
  /** 这一行是什么时候挂上去的(证据链按它排先后)。 */
  created_at?: string;
  /**
   * 这份文书属于哪条隐患。**后端不给,由前端在收到动作回执时补上。**
   *
   * 🔴 为什么必须有:「本次出的文书」那一块是**跨隐患**的汇总 —— 连着给三条隐患
   * 签复工令,那里就是三张一模一样的「工程复工令」,只有编号不同,而编号对人
   * 不说明任何事。2026-08-17 真人反馈的原话是「意义不明,缺加上什么的复工令」。
   *
   * 证据链那一侧不需要它(整块就挂在某一条隐患下面,上下文自明),所以是可选的 ——
   * 两个调用方共用同一张卡,有就显示、没有就不显示。
   */
  hazard_no?: string;
  /** 那条隐患的违规项(「高空作业未系安全带」)。人认的是这个,不是编号。 */
  hazard_item?: string;
}

export interface ActionResult {
  hazard_no: string;
  /**
   * 动作完成后**库里的真实状态**(不是前端猜的)。
   *
   * ⚠️ `reject` 回的是 `REJECTED_STATUS`(见上面那条:唯一一个不在八档词表里的值)。
   * 别拿它去点亮状态徽章 —— `reject` 成功之后该做的是把这条 `removeHazard` 掉。
   */
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

/**
 * 证据链那几个只有详情端点才给的键。同 `toHazardBrief` 的规矩:
 * **源里没有这个键就不挂上去**,不填 undefined —— 动作端点回执的四键形状因此不变。
 *
 * 严格版(`toDoc`)与宽松版(`documentsFromToolData`)共用这一份:两条路的
 * **严格度**不同(见 documentsFromToolData 的注释),但同一个字段该怎么读是一样的,
 * 抄两份迟早分叉成「详情页显示复查结论、动作回执不显示」这种说不清的差别。
 */
function docExtras(rec: Record<string, unknown>): Partial<SupervisionDoc> {
  return {
    ...(typeof rec.doc_type_display === "string"
      ? { doc_type_display: rec.doc_type_display.trim() }
      : {}),
    ...("photo_id" in rec ? { photo_id: nullableTextOf(rec, "photo_id") } : {}),
    ...(typeof rec.result === "string" ? { result: rec.result.trim() } : {}),
    ...(typeof rec.result_display === "string"
      ? { result_display: rec.result_display.trim() }
      : {}),
    ...(typeof rec.created_at === "string" ? { created_at: rec.created_at.trim() } : {}),
  };
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
    ...docExtras(rec),
  };
}

/**
 * 解析**单条**动作端点的 200 响应 —— 除 `confirm` 之外的七个(它是批量端点,
 * data 形状不同,另有 `parseConfirmEnvelope`)。`reject` 也走这一份:
 * 它的 `documents` 是空数组、`status` 是 `REJECTED_STATUS`,判据一个字都不用改。
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
// 两个 GET 查询端点的解析(W10)—— 宽松那一档,**一个错都不许抛**
// ---------------------------------------------------------------------------
//
// 为什么与上面那两个动作解析器反着来:动作那条路是「人刚点了签发」,契约破了必须
// 响亮地失败;查询这条路是**面板每次打开、每次刷新都在走**,抛出去就是整个操作台白屏
// —— 而白屏之后连「重试」按钮都没有,监理只能刷浏览器。所以这里一律给保守缺省,
// 认不出的条目跳过,`ok` 字段留给调用方决定要不要提示。
//
// ⚠️ 保守缺省的方向是「少说」不是「乱说」:计数拿不到就报 0(而不是编一个),
// `truncated` 拿不到就当没截断。唯一例外是 `total` —— 见下面那条注释。

/** `GET /supervision/hazards` 解析出来的一屏。 */
export interface HazardListResult {
  /** 信封是不是 `ok:true` 且形状认得出。false 时下面全是缺省值。 */
  ok: boolean;
  /** 后端回声的筛子。缺了按「在办」算 —— 与后端缺省同一档。 */
  scope: string;
  /**
   * 归属回声的**三态**,与 `hazardListUrl` 的入参一一对应:
   * `null` = 全部工地 / `""` = 只看未归属 / `"P-x"` = 某个工地。
   *
   * 它只是回声:界面上显示「现在看的是哪一批」该信自己发出去的那份状态,
   * 这个字段用来**核对**(对不上说明请求和响应不是一回事,通常是 URL 拼错了)。
   */
  projectId: string | null;
  /** 后端那一侧的「今天」(香港日历日)。超期是按它算的,别用浏览器的本地日期。 */
  today: string;
  hazards: HazardBrief[];
  /** 台账里一共几条(过了 scope 筛子之后)。 */
  total: number;
  /** 待确认几条 —— 它是「有活要干」的红点。 */
  pending: number;
  /** 超期几条。 */
  overdue: number;
  /**
   * 未归属**一共**几条。🔴 **它不过 scope 筛子** —— 它回答的不是「这一屏里有几条」,
   * 而是「有没有一批隐患没人看得见」(D6:未归属是空串不是 NULL,不选工地就永远筛不到)。
   * 界面上要单独说这一句,混进上面那三个计数里就等于没说。
   */
  unassigned: number;
  /**
   * 条数超过后端上限(`config.supervision_list_max_rows`,当前 50)被截断了。
   *
   * 🔴 **界面必须显示这件事。** 不显示的话,屏幕上是一张看起来完整的清单,
   * 而监理据此说「就剩这些了」—— 少掉的那些一条都不会有任何提示。
   */
  truncated: boolean;
  /**
   * 签发时按级别预填的整改期限:`{级别: "YYYY-MM-DD"}`(2026-09-18)。后端按工地日历日
   * 算好给的,前端塞进日期框、监理不改就原样当 `due_phrase` 送回 —— 所以这里
   * **一行日期换算都没有**,天数(一般 7 / 严重 1)也不镜像,只在 `config.py` 有一份。
   * 老后端没这个键 = 空表,日期框空着、必填校验说话,不编日期。
   */
  dueDefaults: Readonly<Record<string, string>>;
  /** 后端写好的人话,原样透传(不重新包装)。 */
  userMsg: string;
}

/** 后端给的默认期限只认 ISO 日期串;别的形态(「明天」/ 数字)一律丢,不猜。 */
const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function dueDefaultsOf(rec: Record<string, unknown>): Readonly<Record<string, string>> {
  const raw = asRecord(rec.due_defaults);
  if (!raw) return {};
  const out: Record<string, string> = {};
  for (const [grade, value] of Object.entries(raw)) {
    // 一档畸形只丢那一档:一档坏了不该连累另一档预填不出来
    if (typeof value === "string" && ISO_DATE_PATTERN.test(value)) out[grade] = value;
  }
  return out;
}

/** 数一个计数字段;不是有限的非负数就用兜底值(**不编数**)。 */
function countOf(rec: Record<string, unknown>, key: string, fallback: number): number {
  const value = rec[key];
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return fallback;
  return Math.trunc(value);
}

/**
 * 读不出清单时的那一份:全是缺省值、`ok:false`,只把后端那句人话留着。
 *
 * 做成函数而不是模块级常量 —— 它里面有个 `hazards` 数组,调用方会拿去 setState、
 * 拿去 `mergeHazards`。共享同一份的话两块面板会互相串,而这种 bug 查起来极费劲。
 */
function unreadableList(userMsg: string): HazardListResult {
  return {
    ok: false,
    scope: HAZARD_SCOPE_ACTIVE,
    projectId: null,
    today: "",
    hazards: [],
    total: 0,
    pending: 0,
    overdue: 0,
    unassigned: 0,
    truncated: false,
    dueDefaults: {},
    userMsg,
  };
}

/**
 * 解析 `GET /supervision/hazards` 的响应体。**一个错都不抛**(理由见本节开头)。
 *
 * `ok:false` / 不是 JSON / `data` 不是对象 / **`hazards` 不是数组** —— 一律退化成
 * 「空清单 + `ok:false`」,而 `userMsg` **照旧解析出来**:非 200 的那几种错误
 * (401/404/429/500)由 `normalizeError` 说话,但 200 带 `ok:false` 这种
 * 「进了 handler 又没成」的情形,后端写好的那句人话是屏幕上唯一的线索。
 *
 * 🔴 `hazards` 不是数组时**不许回 `ok:true` + 空清单**:那样屏幕上写的是
 * 「台账里没有隐患」,而真相是「这一屏读不出来」—— 两句话在界面上长得一模一样,
 * 而前一句会让监理直接收工。`ok:false` 才能让面板说「读不出来,刷新试试」。
 * (同一件事在动作那条路上是直接抛,见 `parseActionEnvelope` 的 documents 判据:
 *  两条路的代价不对称,所以一个抛、一个回 false,但都不许静默当成「就是空的」。)
 */
export function parseHazardListEnvelope(bodyText: string): HazardListResult {
  const parsed = tryParseJsonObject(bodyText);
  // 连 JSON 都不是的时候 parsed 是 null,`?? {}` 让下面这行不必再分一次支。
  const userMsg = textOf(parsed ?? {}, "user_msg");
  const data = parsed && parsed.ok === true ? asRecord(parsed.data) : null;
  if (!data || !Array.isArray(data.hazards)) return unreadableList(userMsg);
  const hazards = data.hazards.flatMap((entry) => {
    const brief = toHazardBrief(entry);
    return brief ? [brief] : [];
  });
  return {
    ok: true,
    scope: textOf(data, "scope") || HAZARD_SCOPE_ACTIVE,
    // 🔴 三态回声,判据只能是 typeof:空串是「只看未归属」这个**值**,
    //    用 `textOf(...) || null` 会把它压成 null,于是界面显示「全部工地」。
    projectId: typeof data.project_id === "string" ? data.project_id.trim() : null,
    today: textOf(data, "today"),
    hazards,
    // total 的兜底是**手上真有的行数**,不是 0:清单里明明列着 12 条而表头写「共 0 条」
    // 是自相矛盾,人会以为界面坏了。它是个诚实的下界(截断时本来就比真值小)。
    total: countOf(data, "total", hazards.length),
    // 这三个的兜底是 0 —— 「不知道有几条」只能说 0,凭空编一个数就是吓唬人 / 骗人。
    pending: countOf(data, "pending", 0),
    overdue: countOf(data, "overdue", 0),
    unassigned: countOf(data, "unassigned", 0),
    truncated: data.truncated === true,
    dueDefaults: dueDefaultsOf(data),
    userMsg,
  };
}

/** 一次改期留痕(2026-08-22)。镜像后端 `supervision_api._due_change_payload`。 */
export interface DueChange {
  /** 从哪天挪走。理论上非空(只有 notified/suspended 能改期,那两档期限必填)。 */
  oldDue: string | null;
  /** 挪到哪天。 */
  newDue: string;
  /** 为什么。**必填**(后端拦),原样进台账。 */
  reason: string;
  /** 谁改的。**自报的名字,不是认证身份** —— 同 `issuedBy`,别拿它做权限判断。 */
  changedBy: string | null;
  /** 什么时候改的。 */
  createdAt: string;
}

function toDueChange(entry: unknown): DueChange | null {
  const rec = asRecord(entry);
  if (!rec) return null;
  const newDue = textOf(rec, "new_due");
  // 没有新期限的留痕是没有意义的一行 —— 跳过而不是渲染成一行空白。
  if (!newDue) return null;
  return {
    oldDue: nullableTextOf(rec, "old_due"),
    newDue,
    reason: textOf(rec, "reason"),
    changedBy: nullableTextOf(rec, "changed_by"),
    createdAt: textOf(rec, "created_at"),
  };
}

/** 一条隐患的详情 = 清单里那一行 + 证据链。 */
export interface HazardDetail extends HazardBrief {
  /** 证据链:五种文书 + 复查记录行,后端按时间排好。空数组 = 还没签过任何东西。 */
  documents: SupervisionDoc[];
  /**
   * 历次改期留痕(2026-08-22)。**与 `documents` 平级,不是它的一部分。**
   *
   * 🔴 别把它并进 `documents` 去省一个字段:那个数组里每一项都有编号、都能下载,
   * 而这几行两样都没有。并进去的表现是证据链表里多出几行点不开的东西,
   * 而「一共签了几份文书」这个数会跟着错 —— 那个数是要拿去举证的。
   */
  dueChanges: DueChange[];
  /** 复查过没有(哪怕不合格也算复查过)—— 升级上报的举证链要这一条。 */
  reinspected: boolean;
  /** 首次发现的时刻(带时区的 ISO 串,香港时间)。 */
  foundAt: string;
  /** 销项时刻;null = 还没销项。 */
  closedAt: string | null;
}

export interface HazardDetailResult {
  ok: boolean;
  /** 认不出就是 null(含 404 那种)。调用方据此显示 `userMsg`,别渲染半张空卡。 */
  hazard: HazardDetail | null;
  userMsg: string;
}

/**
 * 解析 `GET /supervision/hazards/{hazard_no}` 的响应体。**一个错都不抛。**
 *
 * 404(编号查不到)的形状是 `{ok:false, data:null, user_msg:"没找到隐患…"}` ——
 * 这里回 `{ok:false, hazard:null, userMsg:"没找到隐患…"}`,那句人话得留着:
 * 它把人带回去核对编号,而按状态码说话会变成「监理接口还没开通」,方向全错
 * (同 `normalizeError` 里那条「Envelope 分支必须优先」)。
 *
 * `hazard_no` 认不出时即便信封说 `ok:true` 也当失败:没有编号的详情页上,
 * 每一颗按钮都不知道该打给谁 —— 与其画一张点不动的卡,不如老实说这条读不出来。
 */
export function parseHazardDetailEnvelope(bodyText: string): HazardDetailResult {
  const parsed = tryParseJsonObject(bodyText);
  // 连 JSON 都不是的时候 parsed 是 null,`?? {}` 让下面这行不必再分一次支。
  const userMsg = textOf(parsed ?? {}, "user_msg");
  const data = parsed && parsed.ok === true ? asRecord(parsed.data) : null;
  const brief = data ? toHazardBrief(data) : null;
  if (!data || !brief) return { ok: false, hazard: null, userMsg };
  return {
    ok: true,
    hazard: {
      ...brief,
      // 复用宽松版:证据链里认不出的行跳过,不因为一行坏数据丢掉整条隐患。
      documents: documentsFromToolData(data),
      reinspected: data.reinspected === true,
      // 同 documents 的规矩:认不出的行跳过,不因为一行坏数据丢掉整条隐患。
      // 键不在(老后端)时是空数组 —— 界面上那一段整块不渲染,不报错。
      dueChanges: Array.isArray(data.due_changes)
        ? data.due_changes.flatMap((entry) => {
            const change = toDueChange(entry);
            return change ? [change] : [];
          })
        : [],
      foundAt: textOf(data, "found_at"),
      closedAt: nullableTextOf(data, "closed_at"),
    },
    userMsg,
  };
}

// ---------------------------------------------------------------------------
// 登记失败清单(2026-08-22)—— D10 那条一直没通的出口
// ---------------------------------------------------------------------------

/**
 * 一条「没能写进台账」的记录。镜像 `GET /supervision/ingest-failures` 的一行。
 *
 * ⚠️ **它没有 `reason`,那是刻意的。** 后端那一列存的是异常类型与约束名
 * (`IngestFailureRow` 头注:「给排查的人看的内部细节,**不进人话**」),
 * 端点根本不发它。哪天有人觉得"多给一个字段没坏处"而在后端补上,
 * 界面上就会出现「UNIQUE constraint failed: hazards.project_id」这种东西。
 *
 * ⚠️ 它也**没有隐患编号**——那条隐患压根没登记成功,不存在编号。
 * 所以这几行**点不开、没有任何动作**:监理能做的只有一件事,让人回去重拍。
 */
export interface IngestFailure {
  /** 那张照片的 32 位产物编号。拼 `<ARTIFACT_BASE>/by-id/<photoId>` 能看到原图。 */
  photoId: string;
  /** 哪个违规项没进去。 */
  item: string;
  /** 哪个工地。空串 = 未归属(D6)。 */
  projectId: string;
  /** 什么时候失败的。 */
  createdAt: string;
}

export interface IngestFailureResult {
  ok: boolean;
  failures: IngestFailure[];
  /** 本次列出来几条(不是磁盘上一共几条)。 */
  total: number;
  truncated: boolean;
  userMsg: string;
}

function toIngestFailure(entry: unknown): IngestFailure | null {
  const rec = asRecord(entry);
  if (!rec) return null;
  const photoId = textOf(rec, "photo_id");
  const item = textOf(rec, "item");
  // 两样缺一样这行就没意义了:没照片编号看不到原图,没违规项不知道要重拍什么。
  if (!photoId || !item) return null;
  return {
    photoId,
    item,
    // 🔴 空串是「未归属」这个**值**,不是"没读到" —— 所以用 textOf 而不是
    //    nullableTextOf。压成 null 的话界面会显示成「不知道哪个工地」,
    //    而真相是「这条明确不属于任何工地」,两句话给人的下一步完全不同。
    projectId: textOf(rec, "project_id"),
    createdAt: textOf(rec, "created_at"),
  };
}

/**
 * 解析 `GET /supervision/ingest-failures` 的响应体。**一个错都不抛**(同清单那条)。
 *
 * 🔴 读不出时回 `ok:false` 而不是「ok:true + 空清单」,理由与 `parseHazardListEnvelope`
 * 那段红字一字不差:「没有登记失败」和「这一屏读不出来」在界面上长得一模一样,
 * 而前一句会让监理放心收工 —— 可这条清单存在的全部意义就是告诉他有东西漏了。
 */
export function parseIngestFailureEnvelope(bodyText: string): IngestFailureResult {
  const parsed = tryParseJsonObject(bodyText);
  const userMsg = textOf(parsed ?? {}, "user_msg");
  const data = parsed && parsed.ok === true ? asRecord(parsed.data) : null;
  if (!data || !Array.isArray(data.failures)) {
    return { ok: false, failures: [], total: 0, truncated: false, userMsg };
  }
  const failures = data.failures.flatMap((entry) => {
    const row = toIngestFailure(entry);
    return row ? [row] : [];
  });
  return {
    ok: true,
    failures,
    // 兜底是手上真有的行数,不是 0 —— 列着 3 行而表头写「共 0 条」是自相矛盾。
    total: countOf(data, "total", failures.length),
    truncated: data.truncated === true,
    userMsg,
  };
}

// ---------------------------------------------------------------------------
// 证据链的排版素材(W10 · 详情面板)
// ---------------------------------------------------------------------------

/** 证据链里的一行:那条记录本身 + 它是**第几次复查**。 */
export interface EvidenceRow {
  doc: SupervisionDoc;
  /**
   * 复查行是第几次复查(从 1 起数);**文书行恒为 null**。
   *
   * 为什么要这个序号:复查可以反复做(不合格 → 再整改 → 再复查),证据链里就会有
   * 好几条长得一模一样的复查记录。不编号的话屏幕上是三行「复查:不合格」,
   * 人分不出哪一行是最近那次 —— 而「最近一次复查什么结论」正是监理翻这一页最想知道的。
   */
  reinspectionNo: number | null;
}

/**
 * 给证据链的每一行标上「第几次复查」。**顺序原样保留,不排序、不分组。**
 *
 * 🔴 **不许按类型分成「文书一堆、复查一堆」再渲染。** 后端 `docs_of` 是按挂进台账的
 * 先后给的,而证据链的意义就是这个先后:上报主管部门那条举证链是
 * 「通知过 + 期限到了 + 复查过 + 仍未整改」—— 按类型分组等于把时间轴拆了,
 * 剩下的只是两张清单,答不了「先通知还是先复查」这种事后会被问到的问题。
 *
 * 判据用 `isDownloadableDoc`(**别自己比 doc_type**):它按的是「这一档本来有没有文件」,
 * 所以一份 artifact_id 为空的通知单仍然算文书行 —— 那是**事故**(纸签了取不了件),
 * 要走文书那张卡去显眼地说,不能被误编进复查序号里。
 *
 * ⚠️ 传进来的必须是**整条**证据链。喂半截进来序号就是错的(第 2 次会被数成第 1 次),
 * 而屏幕上看不出任何异常 —— 今天唯一的来源是详情端点的 `documents`,它本来就是整条。
 */
export function evidenceRows(docs: readonly SupervisionDoc[]): EvidenceRow[] {
  let seen = 0;
  return docs.map((doc) => {
    if (isDownloadableDoc(doc)) return { doc, reinspectionNo: null };
    seen += 1;
    return { doc, reinspectionNo: seen };
  });
}

/** 后端时刻串的形状:`2026-08-16T09:50:08+08:00`。只取到分,秒对监理没有意义。 */
const ISO_MOMENT_PATTERN = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/;

/**
 * 把后端给的时刻串排成「2026-08-16 09:50」。**纯截字符串,一点换算都不做。**
 *
 * 🔴 **绝对不许写成 `new Date(iso).toLocaleString()`。** 那会按**浏览器所在时区**
 * 重排:监理的笔记本设成 UTC 时,一条 09:50 发现的隐患在屏幕上就成了 01:50 ——
 * 而这是要拿去追责的时刻,差八小时能把「下班后违规作业」说成「上班前」。
 * 后端给的串里时区偏移已经写死(全仓唯一时间权威是 `attendance/receipt.py` 的
 * 香港时间快照),照抄前面那截就是原样保留。
 * 这与「日期换算一律交后端 `dates.py`」是同一条红线的两半:那半禁止**算**,
 * 这半禁止**转**。
 *
 * 认不出形状返回空串,调用方据此干脆不显示这一格 —— 宁可少一格,
 * 也不许把半截串或者 `Invalid Date` 摆到屏幕上。
 */
export function formatHkMoment(iso: string): string {
  const matched = ISO_MOMENT_PATTERN.exec(iso.trim());
  return matched ? `${matched[1]} ${matched[2]}` : "";
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
 * `NEXT_PUBLIC_ARTIFACT_BASE` 那条链(CLAUDE.md 同源清单)现在有**四个**读者
 * (human.tsx / tool-calls.tsx / checkin.tsx / **supervision-entry.tsx**),
 * 链断在任何一环都不报错 —— 而且 https 页面拉 http 资源属于 mixed content,
 * 浏览器**连请求都不发**,界面上一点线索都没有。
 *
 * 少一个读者就少一处能断的地方,所以本文件与面板一律收 props、自己不读。
 * 第四个读者是 2026-08-16(W10)加的:处置面板改成常驻操作台之后多出一条链路,
 * 而**那条链路的根不再是 tool-calls.tsx**(它挂的卡片压根不可达,见 W10 §1)。
 * 规矩因此定成:**每条链路的根各读一次,面板与卡片一律收 props** ——
 * 两条链路对称,谁也不用去 import 另一条的私有常量。
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
      // ⚠️ 复查记录行后端给的 filename 是 null,于是这里兜底拼出一个
      //「复查记录_….docx」—— 它**不是真文件名,那一行根本没有文件**。
      // 渲染前一律先过 `isDownloadableDoc`,别照着这个名字去画一个下载按钮。
      filename: textOf(rec, "filename") || `${docTypeZh(docType)}_${docNo}.docx`,
      ...docExtras(rec),
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

// ---------------------------------------------------------------------------
// 复查照片直传(2026-08-16)—— 端点、本地预检、回执解析
// ---------------------------------------------------------------------------
//
// ── 为什么加这一节 ────────────────────────────────────────────────────────
// 真人测试的反馈原话:**「照片不能是编号意义不明」**。
// 在这之前,「登记复查结论」那一格是个文本框,标签写着
//   「整改后照片的编号(32 位,在聊天里那张图下面)」
// —— 做复查的人手机里刚拍完那张照片,却要他退出去翻聊天记录、找到那张图、
// 把图底下那串 hex 抄回来。工地上这不成立:戴着手套、太阳底下看屏幕、
// 一只手还扶着梯子。**编号是给机器用的,不该出现在人手上的主路径上。**
//
// 修法:先把照片直传成产物拿到编号,再拿这个编号去打 `reinspect-result`。
//
// 🔴 **两步,不是一步。** `reinspect-result` 是法律动作端点(复查合格会把隐患销项),
// 它的请求体形状一个字都不动。把文件塞进那个端点的下场是它同时管上传和销项:
// 「照片太大」和「这条状态不对,签不了」会糊成同一句话回来,而人手上那张照片
// 到底传上去没有,屏幕上再也说不清 —— 而销项是不可撤销的。
//
// ── 这一节的东西为什么值得下沉到 lib ──────────────────────────────────────
// 它们全是**判据**(多大算超限、什么算编号、什么样的回执算数),而判据一旦漂开
// 就是静默出错:前端拦掉一张后端本来收得下的照片、或者放行一个后端不认的编号,
// 两种都不报错。放这里才有测试钉着。碰浏览器的部分(FileReader、objectURL、
// fetch)一律留在 supervision.tsx,这条分法与 checkin-lib.ts 一致。

/**
 * `POST /supervision/photo` 的地址 —— 复查照片直传。
 * 请求体是**原始图片字节**(不是 multipart、也不是 JSON)。
 *
 * ⚠️ 它**故意不进 `SUPERVISION_ENDPOINTS` 那张表**,理由与两个 GET 端点同款:
 * 那张表是给 `supervisionUrl` 拼「八个 POST 动作端点」用的,而那八个共享同一套
 * 调用形状 —— JSON 请求体、`ActionResult` 回执、会改台账状态。这一条一样都不占:
 * 它发的是字节流,它不改任何台账状态,回执形状也不同。
 *
 * 混进那张表的具体下场:`supervision.tsx` 里那个统一出口 `callSupervision`
 * 会给它套上 `content-type: application/json` 并 `JSON.stringify(body)` ——
 * 一张 JPEG 会被序列化成 `{}` 发出去,后端按魔数一看不是图片,回一句
 * 「传上来的不是照片」。人明明拍了照,屏幕上却说没收到照片,而线索一条都没有。
 */
export function supervisionPhotoUrl(apiBase: string): string {
  return `${stripTrailingSlash(apiBase)}/supervision/photo`;
}

/**
 * 照片大小上限,镜像后端 `config.photo_max_mb`(当前 10)。
 *
 * 🔴 **1MB = 1024×1024,不是 1000×1000** —— 后端 `checkin_api._BYTES_PER_MB`
 * 就是这么算的。两边算法不一致的后果只在边界上出现,而且是**单向**的:
 * 按 1000×1000 算的话,一张 10.4MB 的照片在前端被判超限(10.4 > 10),
 * 后端却算它 9.92MiB、本来收得下 —— 于是前端拦掉了一张合法照片,
 * 而界面上除了「太大了」没有任何出路,人只能一张张试。
 *
 * 🔴 这是**第二道**闸不是唯一那道:真正说了算的是后端那句 413。这里拦一下只为
 * 省一次白传 —— 工地 4G 传 12MB 要十几秒,让人等完再被拒是最气人的一种失败。
 */
export const PHOTO_MAX_MB = 10;
export const PHOTO_MAX_BYTES = PHOTO_MAX_MB * 1024 * 1024;

/**
 * 复查照片这条链上的固定文案。
 *
 * 🔴 **另起一份,不并进 `SUPERVISION_MESSAGES`。** 那份里的 `badPhotoId` 写的是
 * 「要 32 位的编号(在聊天记录里那张照片下面能看到)」—— 那是**旧路**的指路话,
 * 今天它只服务折叠起来的手填编号那一格。主路径现在是「拍一张」,两句话指向
 * 两个不同的地方,合成一句必然有一半人被指错方向;而「被指错方向」正是真人测试
 * 反馈的那句「编号意义不明」的全部内容。旧那句一个字不动。
 *
 * 与全仓一样:这些话是给工地上的人看的,不许出现英文枚举值、类名、内部路径。
 */
export const PHOTO_MESSAGES = Object.freeze({
  /** 0 字节。多半是选图时文件还没从 iCloud/网盘下下来。 */
  emptyFile: "這個文件是空的(0 字節),沒法當照片用。換一張再試。",
  /** MIME 明摆着不是图片(选到了 PDF、视频之类)。 */
  notAnImage: "選中的不是照片文件。用「拍照 / 選圖」重新拍一張,或者從相冊裏挑一張照片。",
  /**
   * HEIC / HEIF。**必须单独说一句**:它是真照片,人看不出有什么不对
   * (iPhone 设置成「保留原片」时从相册选图就是这个格式),而后端的魔数闸
   * 只认 JPEG/PNG/WebP、一定拒。只说「格式不对」的话,人会一张接一张地试相册,
   * 每张都被拒 —— 所以这句必须给出唯一那条走得通的路:**现拍一张**。
   */
  heicNotSupported:
    "這張是 iPhone 的 HEIC 格式,系統這邊打不開。用「拍照 / 選圖」現拍一張就行,現拍出來的格式沒問題。",
  /**
   * 传上去了、也回了 200,但回执里没有能用的照片编号。
   *
   * 话要说清「先别用这张」:不说的话,人看到「传成功了」的直觉是接着点复查结论,
   * 而那时手上根本没有编号 —— 下一步会被自己人拦住,而拦的那句话指向聊天记录。
   */
  badEnvelope: "照片好像傳上去了,但服務器沒回編號 —— 這張先別用,重傳一次。",
  /** 折叠的手填编号那一格里打了半截东西。 */
  badManualId: "這不像照片編號:要 32 位,只有數字和 a 到 f 這幾個字母。",
});

/**
 * 这串是不是一个产物编号(32 位小写十六进制)。
 *
 * 🔴 与 `actionBody` 里那道校验**共用同一个 `ARTIFACT_ID_PATTERN`**,不许各写一份。
 * 两个调用方问的是同一个问题的两半:界面拿它决定「复查结论那两颗按钮能不能点」,
 * `actionBody` 拿它决定「这次请求发不发」。两份正则一旦漂开,表现是按钮亮着、
 * 点下去被自己人拦住,而拦下来那句话还把人指回聊天记录 —— 正是这次要修掉的东西。
 *
 * 归一化(trim + 转小写)也必须与 `actionBody` 一致:那边收到 `"ABC…"` 会先转小写
 * 再比,这边如果直接比就会判它不合法,于是一个后端认得的编号在界面上点不动。
 */
export function isPhotoId(value: string): boolean {
  return ARTIFACT_ID_PATTERN.test(value.trim().toLowerCase());
}

/** 「2.3 MB」这种人话。**不做本地化数字**,工地上没人关心千分位。 */
function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return "大小不明";
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${Math.round(size / 1024)} KB`;
  return `${Math.trunc(size)} 字節`;
}

/** 文件名显示上限(字符数)。超了掐中间 —— 理由见 `describePhotoFile`。 */
const FILE_NAME_HEAD = 16;
const FILE_NAME_TAIL = 10;

/**
 * 长文件名掐中间。**掐中间,不掐尾巴。**
 *
 * 尾巴是扩展名,而「这张到底是不是 .heic」正是这一行要回答的问题之一 ——
 * 掐掉尾巴的话,`IMG_20260816_143052_整改后临边防护.heic` 会显示成
 * `IMG_20260816_1430…`,人看不出格式,传上去被拒了也不知道为什么。
 */
function shortenFileName(name: string): string {
  const trimmed = name.trim();
  if (trimmed.length <= FILE_NAME_HEAD + FILE_NAME_TAIL + 1) return trimmed;
  return `${trimmed.slice(0, FILE_NAME_HEAD)}…${trimmed.slice(-FILE_NAME_TAIL)}`;
}

/**
 * 「整改后.jpg · 2.3 MB」—— 选完之后摆在缩略图旁边那一行。
 *
 * 为什么**大小**要显示出来:超限是这条链上最常见的失败,而它在点上传之前就看得见。
 * 显示了,人自己会换一张;不显示的话,他只能等十几秒换来一句「太大了」。
 *
 * 为什么**文件名**要显示出来:工地上戴手套点屏,从相册里选错一张是常事,
 * 而两张现场照片缩了图看着都差不多 —— 文件名是唯一能一眼分清的东西。
 *
 * 没有文件名时说「(没有文件名)」而不是留空:留空的话那一行只剩一个「· 2.3 MB」,
 * 看着像界面坏了。
 */
export function describePhotoFile(file: { name: string; size: number }): string {
  return `${shortenFileName(file.name) || "(沒有文件名)"} · ${formatBytes(file.size)}`;
}

/**
 * 大小上的毛病;没毛病返回 null。
 *
 * 超限那句**必须带上具体多大和上限是多少** —— 与后端 `messages.photo_too_large`
 * 同一条规矩:「太大」这三个字答不了「那多大才行」,人只能一张张试。
 */
export function photoSizeProblem(file: { size: number }): string | null {
  if (!Number.isFinite(file.size) || file.size <= 0) return PHOTO_MESSAGES.emptyFile;
  if (file.size > PHOTO_MAX_BYTES) {
    return `照片太大了(${formatBytes(file.size)}),上限 ${PHOTO_MAX_MB}MB。換一張小一點的再傳。`;
  }
  return null;
}

/**
 * 挑明两种「不用传也知道不行」的文件;其余一律放行。
 *
 * 🔴 **保守是刻意的。** 浏览器给的 MIME 不可靠(DXF 那条线已经证过一次:
 * 同一个 .dxf 在不同浏览器里报三种类型,最后只能按后缀认)。前端把判据抄严一点的
 * 代价是**拦掉一张后端本来收得下的照片,而且界面上没有任何出路** —— 人只会以为
 * 这个功能坏了。真正说了算的是后端那道魔数闸,它看的是文件头字节,不是这个字符串。
 *
 * 所以这里只认两类:
 *   ① 压根不是图(`application/pdf`、`video/mp4`……)—— 十有八九是点错了文件;
 *   ② HEIC / HEIF —— 它是**真照片**,但后端只认 JPEG/PNG/WebP,必拒(见
 *      `PHOTO_MESSAGES.heicNotSupported` 那条:这一类必须单独给出路)。
 * 别的 `image/*`(包括没见过的)一律放行,让后端去判。
 *
 * 拿不到类型(`file.type === ""`)也放行:部分安卓浏览器和相机回调就是空的,
 * 按「空 = 不是图」拦的话,那些机器上整条拍照路径直接断掉,而且毫无提示。
 */
export function photoTypeProblem(file: { type: string }): string | null {
  const type = file.type.trim().toLowerCase();
  if (!type) return null;
  if (type.startsWith("image/heic") || type.startsWith("image/heif")) {
    return PHOTO_MESSAGES.heicNotSupported;
  }
  if (!type.startsWith("image/")) return PHOTO_MESSAGES.notAnImage;
  return null;
}

/** `POST /supervision/photo` 的 200 回执解析结果。 */
export interface PhotoUploadResult {
  /** 信封是 `ok:true` 且**编号认得出**才为 true。 */
  ok: boolean;
  /** 32 位产物编号。**唯一不能兜底的字段** —— 没有它这张照片挂不上复查记录。 */
  photoId: string | null;
  /** 后端给的文件名(如「复查照片.jpg」),只用来显示;拿不到就空串。 */
  filename: string;
  /** 后端那句人话,原样透传(不重新包装)。 */
  userMsg: string;
}

/**
 * 解析 `POST /supervision/photo` 的响应体。**一个错都不抛。**
 *
 * 🔴 与 `parseActionEnvelope` 反着来,判据是「这一下能不能给人一条出路」:
 *   · 动作那条路契约破了必须响亮地失败 —— 三份文书出了两份,不能被静默当成
 *     「这次就出两份」;
 *   · 这一条的正确处置是**摆一张「传失败 + 重试」的卡**,那需要一个能渲染的
 *     失败值,不是一个异常。抛的话调用点必须记得 try/catch,而漏掉的表现是
 *     整个面板白屏 —— 人手上那张刚拍的照片就这么没了,连重试按钮都没有。
 *
 * 🔴 `photo_id` **认不出就一律 `ok:false`,而且要按 `isPhotoId` 验形状**,
 * 不是「有个字符串就算数」。硬回 `ok:true` 的下场很具体:界面画一张绿色的
 * 「照片已上传」卡,而 `form.photo` 里是个半截串 —— 人接着点「合格」,
 * 被 `actionBody` 自己人拦住,而拦下来那句话是「在聊天记录里那张照片下面能看到」。
 * 他刚刚明明拍了一张。
 *
 * `userMsg` **照旧解析出来**(哪怕 `ok:false`):200 带 `ok:false` 这种
 * 「进了 handler 又没成」的情形,后端写好的那句人话是屏幕上唯一的线索
 * —— 同 `parseHazardListEnvelope` 的那条规矩。
 */
export function parsePhotoEnvelope(bodyText: string): PhotoUploadResult {
  const parsed = tryParseJsonObject(bodyText);
  // 连 JSON 都不是的时候 parsed 是 null,`?? {}` 让下面这行不必再分一次支。
  const userMsg = textOf(parsed ?? {}, "user_msg");
  const data = parsed && parsed.ok === true ? asRecord(parsed.data) : null;
  const photoId = data ? textOf(data, "photo_id").toLowerCase() : "";
  if (!data || !isPhotoId(photoId)) {
    return { ok: false, photoId: null, filename: "", userMsg };
  }
  return { ok: true, photoId, filename: textOf(data, "filename"), userMsg };
}

// ---------------------------------------------------------------------------
// 一张照片、多条隐患共用(2026-08-16)—— 真人反馈「上传照片的地方太多了」
// ---------------------------------------------------------------------------
//
// ── 事实先摆出来 ──────────────────────────────────────────────────────────
// 隐患**本来就是从同一张照片里认出来的**:`analyze_site_photo` 看一张工地照片、
// 一次登记好几条(库里现成的例子:同一个照片指纹下面挂着「临边无防护」和
// 「未穿反光衣」两条)。整改完监理去现场拍**一张**,那一张同样覆盖这几条。
//
// 而改之前,清单里**每一条**可复查的隐患各有一个大虚线上传框 —— 三条隐患三个框,
// 屏幕被撑得老长,而人手上只有一张照片。真人原话:「一般都是在一张照片里」。
// 所以正确形状是:**一张照片、多条隐患共用;而结论(合格/不合格)仍然一条一条下。**
//
// ── 🔴 共用会不会让「一次把好几条标成合格」变容易 ────────────────────────
// 会容易一点,但**这不构成新风险**,两条理由:
//   ① 结论仍然**逐条点**。界面上没有、也永远不许有「选中的都合格」那种批量结论 ——
//      D11 讲的是**谁来判**(模型给建议、人下结论),不是**判之前要点几次上传**。
//      误判合格会死人,拦它的是「每条各点一次、每条各自弹自己的后果」,
//      不是「每条各传一次照片」;
//   ② 原先那道「每行各传一次」的摩擦**本来也拦不住谁** —— 想批量放行的人把同一个
//      文件传三遍就是了。而且传三遍拿到的是**三个不同的编号**
//      (`POST /supervision/photo` 明说不做去重、不做幂等),证据链上反而**更难**
//      看出这是同一张照片;共用之后三条挂的是同一个编号,事后一眼就知道同源。
// 一句话:改之前那道摩擦买到的是**麻烦**,不是**安全**。
// 下一个人看到「共用照片」别当成图省事 —— 上面这段推演才是它的理由。
//
// ── 这一节为什么在 lib 里 ─────────────────────────────────────────────────
// 全是**判据**:哪几条用这张、某一行用的是不是这张、这一行现在能不能下结论。
// 判据漂开就是静默出错 —— 界面上标着「用共用那张」而提交发的是另一个编号,
// 两处看着都对,而错的那一侧是**销项**(不可撤销,证据链里挂的是无关的一张照片)。
// 碰浏览器的部分(objectURL、fetch、file input)一律留在 supervision.tsx。

/**
 * 面板上那张共用照片的状态 —— **判别联合,不是「编号 + 一堆布尔」**。
 *
 * 🔴 做成联合是刻意的:编号只在 `ready` 这一档存在,其余三档**根本没有编号**。
 * 写成 `{photoId: string | null; uploading: boolean; failed: boolean}` 的话,
 * 「正在传但 photoId 有值」这种自相矛盾的组合是拼得出来的,而它拼出来之后
 * 界面会让人在照片还没传完时就点「合格」—— 那一下不可撤销。
 *
 * 四档在屏幕上要说四句不同的话(见 `reinspectBlocker`):没传 / 在传 / 传失败 /
 * 传好了。糊成一句「先拍一张照片」的下场是传失败的人以为自己忘了点,再点一次重来。
 */
export type SharedPhotoState =
  | { phase: "none" }
  | { phase: "uploading" }
  | { phase: "failed" }
  | { phase: "ready"; photoId: string };

/** 一行的复查照片是从哪儿来的 —— 界面据此标注(见 `SHARED_PHOTO_MESSAGES`)。 */
export type RowPhotoSource = "none" | "shared" | "own";

/**
 * 复查结论那两颗按钮点不动时的**原因分类**。
 *
 * 界面按它挑呈现方式,不是按它挑措辞(措辞在 `message` 里):
 * 前三档(没传/在传/传失败)在面板顶上那一格里本来就有大幅提示(虚线框 / 转圈 /
 * 红卡),行里再重复一遍就是每条隐患讲一遍课 —— 那正是这次要砍的东西,所以走 title;
 * 而 `already-used` 是**这一行独有的事实**,面板那一格上一点线索都没有,
 * 必须在行里常驻显示(触屏没有 hover,藏进 title 等于没说)。
 */
export type ReinspectBlockReason =
  | "no-photo"
  | "uploading"
  | "upload-failed"
  | "bad-id"
  | "already-used";

export interface ReinspectBlock {
  reason: ReinspectBlockReason;
  /** 给工地上的人看的那句话,组件直接上屏。 */
  message: string;
}

/**
 * 共用照片这条链上的固定文案。
 *
 * 🔴 **另起一份,不并进 `PHOTO_MESSAGES`。** 那一份里的话都指向「这一行自己那一格」
 * (「先拍一张整改后的照片」),而这一份的话必须把人指到**面板顶上那一格** ——
 * 共用之后照片不在行里了,还说「先拍一张」的人会在自己这一行上下找那个框。
 * 指错方向正是上一轮真人反馈(「照片不能是编号意义不明」)的全部内容,别重演。
 */
export const SHARED_PHOTO_MESSAGES = Object.freeze({
  /**
   * 一张都还没传。
   *
   * ⚠️ 它**取代了**「先拍一张整改后的照片,才能下复查结论。」那句(原
   * `PHOTO_MESSAGES.noPhoto`,2026-08-17 一并删掉了)—— 那是「每行一个上传格」
   * 时代的话,**没说清在哪儿拍**:改成共用之后照片那一格在面板顶上,
   * 而人此刻的眼睛在第 5 行那颗灰按钮上,照着那句话会在原地找不到东西可点。
   * 所以这一句的要害是**「在上面那一格」这五个字**,改措辞时别把方位丢了。
   */
  waiting: "先在上面那一格拍一張這次複查的照片,才能下結論。",
  /** 正在传(还没有编号)。**等待是有尽头的,得说出来**,不然人不知道自己在等什么。 */
  uploading: "上面那張照片還在傳,傳完就能下結論。",
  /**
   * 传失败。🔴 **绝不许说成「还没传」** —— 那样人以为自己忘了点,再点一次重来一遍,
   * 而真正的原因(太大 / 不是照片 / 接口没开通)一次都没被看见。
   */
  failed: "上面那張照片沒傳上去。先在上面重傳一次,或者換一張。",
  /**
   * 这一行刚拿这张登记过复查了。
   *
   * 🔴 这句话是「共用照片不清场」的配套闸,完整推演见 `reinspectBlocker` 头注:
   * 判「不合格」的那一条会回到可复查,而它上面还挂着刚才那张 —— 让它再用一次
   * 等于拿**上一轮**的照片当这一轮「整改后」的证据,而屏幕上一切正常。
   */
  alreadyUsed: "這一條剛用上面那張登記過複查了。要再複查一次,先在上面換一張新拍的照片。",
  /** 这一行用的是共用那张(常态,说得轻)。 */
  usesShared: "複查照片:用上面那張共用的",
  /** 🔴 这一行用的**不是**共用那张 —— 必须显眼,理由见 `photoSourceOf` 头注。 */
  usesOwn: "這一條用的不是上面那張共用的,是下面填的這個編號。",
  /** 上面还没传共用照片,而这一行自己填了编号 —— 没有「共用那张」可对照,话就别提它。 */
  usesOwnAlone: "這一條用下面填的這個編號的照片。",
  /** 共用那张现在一条隐患都没管着。 */
  coversNothing: "這張暫時沒有隱患在用 —— 下面每條要麼填了自己的編號,要麼剛用它登記過複查。",
});

/**
 * 这一屏里**要用共用照片**的那几条 = 现在能登记复查结论的那几条。
 *
 * 判据用现成的 `availableActions(h).includes("reinspect")`,**不自己比状态** ——
 * 那张表是服务端四道闸在界面上的唯一投影,在这里另写一份 `status === "notified" || …`
 * 的话,哪天状态机加一档,两处就会各说各话:面板顶上那一格不出现,而行里
 * 「合格/不合格」两颗按钮好端端亮着,人点下去才发现根本没地方传照片。
 *
 * 它同时是「那一格要不要出现」的判据:一条可复查的都没有时摆一个上传框是纯噪音。
 */
export function reinspectableHazards(list: readonly HazardBrief[]): HazardBrief[] {
  return list.filter((h) => availableActions(h).includes("reinspect"));
}

/** 编号归一化。**必须与 `actionBody` 一致**(它 trim + 转小写之后再比正则)——
 *  两边不一致的表现是:同一个编号在这儿被判成「用的是别的照片」,发出去的却是同一张。 */
function normalizePhotoId(value: string): string {
  return value.trim().toLowerCase();
}

/**
 * 这一行的复查照片是从哪儿来的。
 *
 * 🔴 **行内填了就以行内为准,一个字符都不许回落到共用那张。** 这条不是偏好,
 * 是防一幕具体的事故:折叠的手填格里打了半截编号(比如少一位),若这时悄悄
 * 回落成共用那张,屏幕上写着「照片编号不对」而按钮却是亮的、点下去销的项挂着
 * 另一张照片 —— 两样东西看着都对,没有任何报错。所以半截串也算 `own`,
 * 由 `reinspectBlocker` 明说「这不像照片编号」,而不是替人做主。
 *
 * 手填的编号**恰好等于**共用那张时算 `shared`:那本来就是同一张照片,
 * 标成「用的不是共用那张」是撒谎,而这一行标注的全部意义就是让人分清哪条不是同源。
 */
export function photoSourceOf(rowPhotoId: string, shared: SharedPhotoState): RowPhotoSource {
  const own = normalizePhotoId(rowPhotoId);
  if (!own) return shared.phase === "ready" ? "shared" : "none";
  if (shared.phase === "ready" && own === normalizePhotoId(shared.photoId)) return "shared";
  return "own";
}

/**
 * 这一行**提交时真正会发出去**的那个照片编号(拿不到就是空串)。
 *
 * 🔴 它是这一层唯一的真相:按钮灰不灰(`reinspectBlocker`)、请求体里填哪个
 * (`actionBody` 的 `afterPhotoId`),两处读的必须是同一个函数的返回值。
 * 各算一遍的下场就是「界面按这一份画、提交发的是那一份」——
 * 而这一侧错了是**销项**:隐患关掉,证据链里挂着一张跟这次复查无关的照片。
 *
 * ⚠️ 这**不是**在 `forms[编号].photo` 之外另开一条并行状态:行内那一格仍然是
 * 唯一的行内真相,共用那张只是它**为空时的回落**,而回落只在这一个函数里算。
 * (为什么不在上传成功那一刻把编号写进每一行的 `forms`:那是一次**快照** ——
 *  之后切筛子、判了不合格、或者别的隐患刚走到可复查,新出现的那些行里就是空的,
 *  而共用那张明明就摆在屏幕顶上。表现是按钮灰着、旁边写「先去拍一张」,
 *  照片却已经传好了。回落是算出来的,不会漏掉后来的行。)
 */
export function effectivePhotoId(rowPhotoId: string, shared: SharedPhotoState): string {
  const own = normalizePhotoId(rowPhotoId);
  if (own) return own;
  return shared.phase === "ready" ? normalizePhotoId(shared.photoId) : "";
}

export interface ReinspectPhotoContext {
  /** 行内手填的那个编号(折叠的「用已有编号」那一格);空 = 用共用那张。 */
  rowPhotoId: string;
  /** 面板顶上那张共用照片现在什么样。 */
  shared: SharedPhotoState;
  /**
   * 这一行**上一次登记复查用掉的**那个编号;null = 这一轮还没登记过。
   *
   * 只记最近一次,不记全部历史 —— 它挡的是「刚登记完、照片还挂在上面,
   * 顺手又给同一条点了一次」这一幕,那是共用之后唯一新增的误用路径。
   */
  usedPhotoId: string | null;
}

/**
 * 这一行现在能不能下复查结论。**返回 null = 能;返回一句人话 = 不能,这就是原因。**
 *
 * 只变灰不说原因是本仓反复防的「点了没反应」:人只知道点不动,不知道要先干嘛。
 *
 * ── 🔴 `already-used` 这一档为什么存在(共用照片带来的唯一新账)──────────
 * 改成共用之前,复查登记成功会把**那一行**的照片草稿整个收掉(旧 `clearPhoto`),
 * 理由是:判「不合格」的隐患会回到可复查,而上面还挂着刚才那张照片 ——
 * 第二次复查会拿第一次的照片当「整改后」的证据交上去,屏幕上一切正常。
 *
 * 共用之后**不能再照行清**:清掉就是「登记完第一条,后面几条的照片全没了」,
 * 而人手上只有那一张、也不该被逼着重传三遍(这次改造要修的正是这个)。
 * 两者的共存点是:**留着照片,但记下这一行已经用过它**。于是
 *   · 别的行照用不误(共用的意义);
 *   · 判了不合格的那一条**仍然能再复查**,只是必须先换一张新拍的 —— 而那正是
 *     现实里该发生的事:第二次复查本来就发生在下一次整改之后,不可能还是同一张;
 *   · 出路是明的、就在屏幕顶上(换一张 = 新编号,这一条当场解锁),
 *     不是把人锁死在一个灰按钮前面。
 * `POST /supervision/photo` **不做去重也不做幂等**(它自己的头注写明),所以哪怕
 * 重传的是同一个文件,拿到的也是新编号 —— 这条出路不会被幂等堵死。
 *
 * 判据顺序不许换:先「有没有」、再「像不像编号」、最后「是不是刚用过」。
 * 反过来的话,一个空编号会先撞上「刚用过」那句,而屏幕上说的是一件没发生的事。
 */
export function reinspectBlocker(ctx: ReinspectPhotoContext): ReinspectBlock | null {
  const photoId = effectivePhotoId(ctx.rowPhotoId, ctx.shared);
  if (!photoId) {
    if (ctx.shared.phase === "uploading") {
      return { reason: "uploading", message: SHARED_PHOTO_MESSAGES.uploading };
    }
    if (ctx.shared.phase === "failed") {
      return { reason: "upload-failed", message: SHARED_PHOTO_MESSAGES.failed };
    }
    return { reason: "no-photo", message: SHARED_PHOTO_MESSAGES.waiting };
  }
  // 只有手填那条路走得到这里(共用那张的编号在 `parsePhotoEnvelope` 就验过形状了)。
  if (!isPhotoId(photoId)) {
    return { reason: "bad-id", message: PHOTO_MESSAGES.badManualId };
  }
  if (ctx.usedPhotoId && normalizePhotoId(ctx.usedPhotoId) === photoId) {
    return { reason: "already-used", message: SHARED_PHOTO_MESSAGES.alreadyUsed };
  }
  return null;
}

/**
 * 共用那张**此刻真正管着**的那几条 —— 面板上那句「下面 N 条都用这张」按它数。
 *
 * 三道筛子缺一不可,少一道那个数就是句空话:
 *   · 现在能登记复查结论(`reinspectableHazards`);
 *   · 用的确实是共用那张(行内填了别的编号的不算);
 *   · 现在真点得动(刚拿这张登记过复查的那一条已经不归它管了)。
 * 数错的代价不是排版难看:监理照着「下面 3 条都用这张」去点,发现只点得动 2 条,
 * 而第 3 条为什么点不动屏幕上没说 —— 于是他会以为系统坏了。
 */
export function hazardsUsingSharedPhoto(
  list: readonly HazardBrief[],
  rowPhotoIds: Readonly<Record<string, string>>,
  usedPhotoIds: Readonly<Record<string, string>>,
  shared: SharedPhotoState,
): HazardBrief[] {
  if (shared.phase !== "ready") return [];
  return reinspectableHazards(list).filter((h) => {
    const rowPhotoId = rowPhotoIds[h.hazard_no] ?? "";
    if (photoSourceOf(rowPhotoId, shared) !== "shared") return false;
    const usedPhotoId = usedPhotoIds[h.hazard_no] ?? null;
    return reinspectBlocker({ rowPhotoId, shared, usedPhotoId }) === null;
  });
}

/**
 * 「下面 3 条隐患的复查结论都用这张」—— 传好之后那张卡上那一句。
 *
 * 这句话是这次改造的**要害**:共用之后照片和结论不在同一行了,不说清它管着哪几条,
 * 人就得自己数,而数错的方向是「以为它管的比实际多」——「那三条我都传过照片了」,
 * 然后有一条的证据其实来自别处。所以 0 条那一档也要说话(`coversNothing`),
 * 不许静默留白。
 */
export function describeSharedPhotoCoverage(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return SHARED_PHOTO_MESSAGES.coversNothing;
  // 1 条时不说「都」——「都」在中文里预设了复数,一条时读起来像界面算错了。
  if (count === 1) return "下面這 1 條隱患的複查結論用這張。";
  return `下面 ${count} 條隱患的複查結論都用這張。`;
}

// ---------------------------------------------------------------------------
// 每条隐患下面的步骤条(2026-09-18 用户反馈:「处置流程要有逻辑地体现在每个事件下面」)
// ---------------------------------------------------------------------------

export type StepState = "done" | "current" | "todo";

export interface DisposalStep {
  /** 稳定的键,渲染当 key、测试当锚;不上屏。 */
  key: "confirm" | "grade" | "issue" | "rectify" | "resume" | "close" | "escalate" | "dismiss";
  /** 上屏的字。源码里就是繁體(界面恒繁體);它**不含**后端来的字。 */
  label: string;
  state: StepState;
  /**
   * 一小截备注(级别 / 期限 / 关掉的理由 / 带出哪种文书)。**可能含后端原值**
   * (简体的级别名、`due_display`、`closed_reason`),渲染处要过一道 `useHantUI` ——
   * 与 `confirmText` 同一条规矩:整句转,不只转其中一半。
   */
  note?: string;
}

/**
 * 一条隐患**走到哪一步了**,按状态机(`db/hazards.py` 的 ASCII 图)画成一条线。
 *
 * 判据全在这一个纯函数里,渲染层只管把 done / current / todo 画成三种样子。
 * 三条红线,每条对应一种「画错比不画更坏」:
 *
 *   · **未定级时不按默认档画路。** `needs_grading=1` 时 `grade` 是映射表给的默认档
 *     (`currentGrade` 那条红线),按它画就会给一条其实很严重的隐患画一条没有复工令的路。
 *   · **不出文书关掉的只画「確認 → 關掉」。** closed 有两条进路(复查合格 / dismiss),
 *     只有 dismiss 写 `closed_reason`;把签发、复查全打勾,等于给一条识错了的"隐患"
 *     编一整条证据链,而这条线就长在证据链的展开口上面。
 *   · **认不出的状态一格都不画。** 状态机加了档而这里没跟上,宁可空着 —— 空着人会问,
 *     画错人会信。
 *
 * 复工令那一格出不出:`was_suspended`(后端给的、真停过的凭据)**或**已判级别是严重
 * (还没签、将会停工)。前者是事实,后者是路线 —— 老路(工具返回)没有前者时退到后者。
 * 上报那条路(escalated)是从复查不合格分出去的终点,复工令没发生过,所以那一档不画它。
 */
export function disposalSteps(h: HazardBrief): DisposalStep[] {
  const status = h.status;
  if (!(HAZARD_STATUSES as readonly string[]).includes(status)) return [];

  // 不出文书关掉的:两格,全 done。
  if (status === "closed" && typeof h.closed_reason === "string" && h.closed_reason !== "") {
    return [
      { key: "confirm", label: "確認是隱患", state: "done" },
      { key: "dismiss", label: "關掉(不出文書)", state: "done", note: h.closed_reason },
    ];
  }

  const graded = currentGrade(h);
  const isPending = status === "pending";
  const isOpen = status === "open";
  const issued = !isPending && !isOpen; // notified / suspended / reinspect_failed / resuming / closed / escalated
  const rectifying = status === "notified" || status === "suspended" || status === "reinspect_failed";
  const stopsWork = h.was_suspended === true || graded === GRADE_SEVERE;

  const steps: DisposalStep[] = [
    { key: "confirm", label: "確認是隱患", state: isPending ? "current" : "done" },
    {
      key: "grade",
      label: "定級",
      // pending 时永远是 todo(先确认再定级);open 时看有没有人判过。
      state: isPending ? "todo" : graded === null ? "current" : "done",
      ...(graded !== null ? { note: graded } : {}),
    },
    {
      key: "issue",
      label: "簽發文書",
      state: issued ? "done" : isOpen && graded !== null ? "current" : "todo",
      // 带出哪种:一般一份通知单、严重三份;没定级不承诺
      ...(graded === GRADE_NORMAL
        ? { note: "監理通知單" }
        : graded === GRADE_SEVERE
          ? { note: "暫停令等三份" }
          : {}),
    },
    {
      key: "rectify",
      label: "整改・複查",
      state: rectifying ? "current" : issued ? "done" : "todo",
      ...rectifyNote(h),
    },
  ];
  if (status === "escalated") {
    steps.push({ key: "escalate", label: "上報主管部門", state: "done" });
    return steps;
  }
  if (stopsWork) {
    steps.push({
      key: "resume",
      label: "簽發復工令",
      state: status === "resuming" ? "current" : status === "closed" ? "done" : "todo",
    });
  }
  steps.push({ key: "close", label: "銷項", state: status === "closed" ? "done" : "todo" });
  return steps;
}

/** 整改・複查那一格的备注:期限 / 已超期 / 上次复查不合格。老路没有期限字段时干脆没备注。 */
function rectifyNote(h: HazardBrief): { note?: string } {
  const parts: string[] = [];
  if (h.status === "reinspect_failed") parts.push("上次複查不合格");
  if (h.due_display) parts.push(h.overdue ? `已超期(期限 ${h.due_display})` : `期限 ${h.due_display}`);
  else if (h.overdue) parts.push("已超期");
  return parts.length ? { note: parts.join("・") } : {};
}

// ---------------------------------------------------------------------------
// 拍完照的隱患卡(2026-09-18 設計審查 FINDING-001)
// ---------------------------------------------------------------------------

/**
 * 從用戶消息裏抠出照片編號。認的是後端 `core/uploads.py` 拼進消息的那一段
 * `(照片编号:a、b)`(頓號分隔、半角或全角括號),與 human.tsx 的 `refPattern` 同一個判據。
 * 圖紙編號**不算**(那是 cad 的事)。去重、保持出現順序。
 *
 * 這是隱患卡的**唯一數據入口**:supervisor 的 `output_mode="last_message"` 把子 Agent 的
 * 工具返回整個丟掉(CLAUDE.md 記着那張 HazardIntakeCard 一次都沒渲染出來過),
 * 而用戶消息裏的編號是穩的 —— 卡拿它直查台賬。
 */
export function photoIdsInText(text: string): string[] {
  const re = /[(（]照片编号[:：]\s*([0-9a-f]{32}(?:、[0-9a-f]{32})*)[)）]/g;
  const out: string[] = [];
  for (const m of text.matchAll(re)) {
    for (const id of m[1].split("、")) if (!out.includes(id)) out.push(id);
  }
  return out;
}

/** 客戶端再按 photo_id 篩一遍 —— 老後端不認 `?photo_id` 時會把整張台賬回來,不能照單全收。 */
export function hazardsForPhotos(
  list: readonly HazardBrief[],
  photoIds: readonly string[],
): HazardBrief[] {
  if (photoIds.length === 0) return [];
  return list.filter((h) => !!h.photo_id && photoIds.includes(h.photo_id));
}

/**
 * 卡上「去監理處置 →」與動作條上那顆入口按鈕之間的約定:卡 dispatch 這個 DOM 事件,
 * SupervisionEntry 收到就把面板打開。用事件而不是把 open 狀態提到頂層:兩件東西
 * 各在對話流裏一個、動作條裏一個,中間隔着上游的 thread-index,提狀態要穿三層。
 */
export const OPEN_SUPERVISION_EVENT = "gyt:open-supervision";

export function requestOpenSupervision(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(OPEN_SUPERVISION_EVENT));
}
