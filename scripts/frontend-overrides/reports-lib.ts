/**
 * 巡检记录抽屉的纯逻辑(2026-08-22)——「拍照 → 自动出 Word」这条链**唯一的出口**。
 *
 * ===========================================================================
 * 🔴 它补的是什么:文档出了,而界面上没有任何地方能拿到它
 * ---------------------------------------------------------------------------
 * 这条链在此之前是断的,而且断得很安静:
 *
 *   ① `agents/report/tools.py` 真的生成了 docx、真的登记成产物、真的落在磁盘上;
 *   ② 那份 Envelope 里有 report_no / filename / artifact_id,
 *      而 supervisor 的 `output_mode="last_message"` **把子 Agent 的工具返回整个丢掉**
 *      —— W10 已经查清并记录在案的老根因;
 *   ③ 于是 `tool-calls.tsx` 里那张巡检记录卡(W3 就写好了)**一次都没渲染出来过**;
 *   ④ 而 `agents/report/prompt.md` 教模型说「要打印或转发跟管理员说编号就行」——
 *      **那个管理员不存在**(真机验收里工友就是这么被打发走的,TODO-34)。
 *
 * 形状照 W7 打卡面板、W10 监理操作台的先例:
 * **操作台不是聊天产物,它有自己的入口和自己的数据源。**
 *
 * ===========================================================================
 * 约束
 * ---------------------------------------------------------------------------
 * ⚠️ **本文件零依赖、零环境假设**(不碰 window / document / fetch)——
 *    与 checkin-lib.ts / supervision-lib.ts / timing-lib.ts 同一条约束。
 *    `scripts/frontend-tests/` 那个独立 vitest 包直接 import 它的源码,
 *    默认环境是 node;碰了 window 的代码会在那儿悄悄跑通然后在 SSR 上炸。
 *
 * ⚠️ **这里不下载文件,只拼地址。** 正文由 `scripts/serve_artifacts.py`(本机 :8788)
 *    或公网那侧 `Caddyfile` 的 `handle_path /artifacts/*` 递出去 ——
 *    与照片、监理文书同一条出口(`ARTIFACT_BASE` 那条链,CLAUDE.md 同源清单里
 *    登记过它的每一环)。在这儿另开一条 = 那张表要多记一处。
 */

// ---------------------------------------------------------------------------
// 地址
// ---------------------------------------------------------------------------

function stripTrailingSlash(base: string): string {
  return base.replace(/\/+$/, "");
}

/**
 * `GET /reports` 的地址。`apiBase` 的来路与监理面板同源
 * (本机 dev 直连 :2024;公网是 `${GYT_PUBLIC_ORIGIN}/api`,Caddy 剥前缀转发)。
 *
 * `limit` 不传就不写这个键 —— 让后端用它自己的缺省(20),
 * 别在前端复制一份缺省(两份缺省漂开的表现是"我明明改了后端却没变")。
 */
export function reportsUrl(apiBase: string, opts: { limit?: number } = {}): string {
  const base = `${stripTrailingSlash(apiBase)}/reports`;
  const limit = opts.limit;
  if (limit == null || !Number.isFinite(limit) || limit <= 0) return base;
  return `${base}?limit=${Math.trunc(limit)}`;
}

/**
 * 一份巡检记录的下载地址。
 *
 * 🔴 **走 `/by-id/` 而不是拼日期目录。** 产物在盘上是
 * `data/artifacts/<UTC日期 YYYYMMDD>/<32位hex>.<扩展名>`,带**日期段** ——
 * 而前端既拿不到它、也不能按本地当天日期去猜:晚上演示时 UTC 已经是"明天",
 * 猜必错。`serve_artifacts.py` 的 `/by-id/` 就是为这件事加的(那边有整段原委)。
 *
 * 编号一律 `encodeURIComponent`:今天的 artifact_id 是 32 位小写 hex、
 * 转不转义一模一样,但那是「今天」—— 而这个值来自网络响应,不是本地常量。
 */
export function reportDownloadUrl(artifactBase: string, artifactId: string): string {
  const id = encodeURIComponent(artifactId.trim());
  return `${stripTrailingSlash(artifactBase)}/by-id/${id}`;
}

// ---------------------------------------------------------------------------
// 响应解析 —— **一个错都不抛**
// ---------------------------------------------------------------------------
//
// 理由与 supervision-lib 的清单解析同源:这是个附属抽屉,读不出来不该把整个
// 界面掀翻。但**不许把"读不出来"说成"没有记录"** —— 见 `parseReportsEnvelope`。

/** 一份巡检记录。镜像 `reports_api._report_payload` 的五个键。 */
export interface InspectionReport {
  /** 32 位十六进制。拼下载地址就靠它。 */
  artifactId: string;
  /**
   * `GYT-八位日期-六位时刻`。**抠不出来就是 null** —— 后端刻意不编一个。
   *
   * 🔴 界面上这一格空着是对的。一个长得像编号、却对不上任何文档的串,
   * 比一格空白坏得多:它会被人报给别人、写进留档,而事后谁也查不到那份文件。
   */
  reportNo: string | null;
  /** 原始文件名(`巡检记录_GYT-….docx`)。下载时显示的名字。 */
  filename: string;
  /** 文件大小;读不出就是 null(**不给 0** —— 0 会被读成"空文件")。 */
  sizeBytes: number | null;
  /** 登记时刻(UTC ISO)。 */
  createdAt: string;
  /**
   * 这份记录**叫什么**(2026-08-25 设计审计 D9)。
   *
   * 用户在出记录时说了「标题写『海之子驗收測試』」才有;没说就是 `null`。
   * 🔴 **`null` 不许在这一层顶成默认文种** —— 顶了之后调用方就分不清
   * 「用户真起了这个名」和「我们编的」。默认文种只在渲染那一刻兜底
   * (`reportTitle`),那里兜是显示逻辑,在这里兜是**篡改数据**。
   * ⚠️ 2026-08-25 之前生成的记录一律没有这个键(上线时线上现存 9 份全是),
   *    所以 `null` 是常态不是异常,别把它当错误处理。
   */
  title: string | null;
}

export interface ReportsResult {
  ok: boolean;
  reports: InspectionReport[];
  total: number;
  /** 后端扫到上限还没扫完 —— 界面要说「只列了最近这些」,不许说「就这么多」。 */
  scanTruncated: boolean;
  userMsg: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function textOf(rec: Record<string, unknown>, key: string): string {
  const value = rec[key];
  return typeof value === "string" ? value.trim() : "";
}

function countOf(rec: Record<string, unknown>, key: string, fallback: number): number {
  const value = rec[key];
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return fallback;
  return Math.trunc(value);
}

function toReport(entry: unknown): InspectionReport | null {
  const rec = asRecord(entry);
  if (!rec) return null;
  const artifactId = textOf(rec, "artifact_id");
  // 没有产物编号的那一行**点不开** —— 渲染出来只是一行骗人的东西。
  if (!artifactId) return null;
  const reportNo = textOf(rec, "report_no");
  const size = rec.size_bytes;
  return {
    artifactId,
    // 🔴 空串压成 null,与后端那个「抠不出就给 null」对齐:
    //    界面上判 `reportNo == null` 就够,不用再判空串。
    reportNo: reportNo || null,
    filename: textOf(rec, "filename"),
    // 读不出给 null 不给 0 —— 与耗时那条 token 数同一条规矩:
    // 0 会被读成「这是个空文件」,而真相是「不知道多大」。
    sizeBytes: typeof size === "number" && Number.isFinite(size) && size >= 0 ? size : null,
    createdAt: textOf(rec, "created_at"),
    // 同 reportNo:空串压成 null,让调用方只判一种「没有」。
    title: textOf(rec, "title") || null,
  };
}

/**
 * 解析 `GET /reports` 的响应体。**一个错都不抛。**
 *
 * 🔴 读不出时回 `ok:false` 而不是「ok:true + 空清单」。判据与
 * `supervision-lib.parseHazardListEnvelope` 那段红字一字不差:
 * 「还没有巡检记录」和「这一屏读不出来」在界面上长得一模一样,
 * 而前一句会让人以为自己那份记录丢了 —— 而这个抽屉存在的全部意义
 * 就是把那份文件交到他手上。
 */
export function parseReportsEnvelope(bodyText: string): ReportsResult {
  let parsed: Record<string, unknown> | null = null;
  try {
    parsed = asRecord(JSON.parse(bodyText));
  } catch {
    parsed = null;
  }
  const userMsg = textOf(parsed ?? {}, "user_msg");
  const data = parsed && parsed.ok === true ? asRecord(parsed.data) : null;
  if (!data || !Array.isArray(data.reports)) {
    return { ok: false, reports: [], total: 0, scanTruncated: false, userMsg };
  }
  const reports = data.reports.flatMap((entry) => {
    const row = toReport(entry);
    return row ? [row] : [];
  });
  return {
    ok: true,
    reports,
    // 兜底是手上真有的行数,不是 0 —— 列着 3 行而表头写「共 0 份」是自相矛盾。
    total: countOf(data, "total", reports.length),
    scanTruncated: data.scan_truncated === true,
    userMsg,
  };
}

// ---------------------------------------------------------------------------
// 排版
// ---------------------------------------------------------------------------

const BYTES_PER_KB = 1024;
const KB_PER_MB = 1024;

/**
 * 文件大小 → 人话。`null` 给空串(**不给「0 B」**:那是在编一个数)。
 *
 * 只到 MB 就够 —— 一份巡检记录是几十 KB 的 docx,出现 GB 说明别的地方错了,
 * 那时候屏幕上写「1234 MB」反而是个有用的信号。
 */
export function formatSize(bytes: number | null): string {
  if (bytes == null || bytes < 0) return "";
  if (bytes < BYTES_PER_KB) return `${bytes} B`;
  const kb = bytes / BYTES_PER_KB;
  if (kb < KB_PER_MB) return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
  return `${(kb / KB_PER_MB).toFixed(1)} MB`;
}

/**
 * 巡检记录编号 → 「8月22日 15:30」。抠不出就返回空串,由调用方退回 `createdAt`。
 *
 * ⚠️ **为什么从编号里解而不是直接用 `createdAt`**:编号里那个时刻是**香港时间**
 * (`core/doc_no.py` 从 `attendance/receipt.py` 那个唯一时间权威取),
 * 而 sidecar 的 `created_at` 是 **UTC**(`artifacts.register` 用 `datetime.now(UTC)`)。
 * 两者差 8 小时。给工地师傅看的必须是他手表上那个时间 ——
 * 直接渲染 createdAt 的表现是「明明下午三点出的记录,列表上写着上午七点」,
 * 而没有任何报错。
 *
 * 🔴 **这里不做时区换算**,只是把编号里已经是港时的那串拆开念。
 * 在前端算时区就是在复制一份时间权威,而本仓的规矩是时间权威只有一个。
 */
export function formatReportTime(reportNo: string | null): string {
  if (!reportNo) return "";
  const matched = /^GYT-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}$/.exec(reportNo.trim());
  if (!matched) return "";
  const [, , month, day, hour, minute] = matched;
  return `${Number(month)}月${Number(day)}日 ${hour}:${minute}`;
}

/**
 * 列表里那一行的主标题。**编号在前,文件名兜底。**
 *
 * 编号是拿去跟人对账的那个东西(「你把 GYT-… 那份发我」),文件名只是它的包装。
 * 抠不出编号时退回文件名 —— 总比一行空白强,而且那一行照样点得开。
 */
export function reportTitle(report: InspectionReport): string {
  return report.reportNo ?? report.filename ?? "";
}

/** 没起过名的记录在界面上显示的名字。**繁體**(界面恒繁體)。 */
export const DEFAULT_REPORT_LABEL = "工地安全巡檢記錄";

/**
 * 抽屉里那一行的**粗体主行**(2026-08-25 设计审计 D9)。
 *
 * 🔴 改这个函数之前先看病状:在它之前主行是 19 位机器编号
 * `GYT-20260825-083033`,而**编号里那串数字就是副行那个时间** ——
 * 主行等于副行的机器格式复读。九行除编号外完全相同(同图标、同 37 KB、
 * 无项目名、无隐患数),班组长要找「今早那份」只能一个个下载来看。
 *
 * 编号是给系统对账的,名字才是给人认的。所以:
 *   有 title → 用它;
 *   没有     → 退回默认文种「工地安全巡檢記錄」,**不是**退回编号
 *              (退回编号就等于什么都没改)。
 * 编号仍然显示,降到副行,和时间并排 —— 要报编号的人照样拿得到。
 */
export function reportHeadline(report: InspectionReport): string {
  return report.title ?? DEFAULT_REPORT_LABEL;
}
