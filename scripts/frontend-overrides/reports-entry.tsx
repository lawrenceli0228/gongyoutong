/**
 * 巡检记录抽屉的**常驻入口**(2026-08-22)—— 本仓自有新文件,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/reports-entry.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 🔴 它为什么存在:文档出了,而界面上没有任何地方能拿到它 ──────────────
 * 「拍照 → 自动出 Word」是本产品的头号卖点,而这条链的**终点一直是断的**:
 *
 *   ① `agents/report/tools.py` 真的生成了 docx、真的落盘了;
 *   ② 那份 Envelope 被 supervisor 的 `output_mode="last_message"` **整个丢掉**
 *      (W10 查清并记录在案的老根因);
 *   ③ 于是 `tool-calls.tsx` 里那张巡检记录卡(W3 就写好了)**一次都没渲染出来过**;
 *   ④ 而 `agents/report/prompt.md` 教模型说「要打印或转发跟管理员说编号就行」——
 *      **那个管理员不存在**。真机验收里工友原话就是被这么打发走的(TODO-34)。
 *
 * 也就是说:系统答「巡检记录出好了,编号 GYT-…」,人照着去找管理员,而没有管理员;
 * 文件就在服务器上,谁都拿不到。
 *
 * 修法照 **W7 打卡面板 / W10 监理操作台**的先例:
 * **操作台不是聊天产物,它有自己的入口和自己的数据源。** 这是第三个同类。
 * 判据同样很实在:刷新一下页面,聊天里渲染出来的东西全没了,而那份 docx 还在盘上。
 *
 * ── 分层 ──────────────────────────────────────────────────────────────
 * 本文件只做两件事:①一颗按钮;②一个列出最近记录的抽屉。
 * 可测的纯逻辑(地址、解析、排版)在 @/lib/reports-lib —— 那一份零依赖,
 * scripts/frontend-tests/ 的 vitest 直接测它的源码。
 *
 * ⚠️ **`HazardIntakeCard` 那种"标注过的死代码"的教训在这儿反过来用**:
 * tool-calls.tsx 里那张巡检记录卡**仍然不可达**(TODO-47),本文件不去动它 ——
 * 少改一个文件,合流时少一处冲突面。但它现在有了替代品,该在那边补一句墓碑,
 * 否则下一个人会以为那张卡就是出口。
 */

import { useCallback, useEffect, useState } from "react";
import { FileText, Download, RefreshCw } from "lucide-react";

import { getApiKey } from "@/lib/api-key";
import {
  type InspectionReport,
  formatReportTime,
  formatSize,
  parseReportsEnvelope,
  reportDownloadUrl,
  reportHeadline,
  reportsUrl,
} from "@/lib/reports-lib";
import { useApiBase } from "./supervision";

/**
 * 产物的静态出口。**这是 `NEXT_PUBLIC_ARTIFACT_BASE` 这条链的第五个读者**
 * (前四个:human.tsx 的历史照片 / 图纸、tool-calls.tsx 的巡检记录卡、
 *  checkin.tsx 的打卡凭证图、supervision-entry.tsx 的监理文书)——
 * CLAUDE.md 同源清单「产物出口 ARTIFACT_BASE 这条链」那一行要把本文件登记进去,
 * 少登记一处下一个改公网地址的人就会漏掉它。
 *
 * 规矩是**每条链路的根各读一次,面板与卡片一律收 props**。本文件是「巡检记录」
 * 这条链路的根,所以在这儿读一次;下面那个抽屉组件收 props。
 *
 * 这条链**断在任何一环都不报错**:站点照开、提问照答,只是点了没反应 ——
 * 而 https 页面拉 http 资源属于 mixed content,浏览器**连请求都不发**,
 * 界面上一点线索都没有。默认值只绑回环(本机 `make serve-artifacts`);
 * 公网由 docker-compose.vps.yml 的 build args 传 `${GYT_PUBLIC_ORIGIN}/artifacts`。
 * ⚠️ NEXT_PUBLIC_* 是**编译期**变量:改了要重建前端镜像。
 */
const ARTIFACT_BASE = process.env.NEXT_PUBLIC_ARTIFACT_BASE || "http://127.0.0.1:8788";

/**
 * 抽屉一次列几份。
 *
 * ⚠️ **别为了"多看几份"把它调大。** 后端那侧每多列一份就要多翻一批产物 sidecar
 * (线上绝大多数产物是照片,`reports_api._MAX_SIDECARS_SCANNED` 那段有实测推演)。
 * 20 份覆盖的是「最近这阵子出的记录」,而那正是工友要转发的那几份;
 * 找更早的靠编号,不靠往下翻。
 */
const PAGE_SIZE = 20;

/**
 * 巡检记录入口 —— thread-index.tsx 把它放在输入框动作条上。
 *
 * `type="button"` **不能省**:它被塞在聊天输入的 `<form>` 里,裸 button 默认
 * `type=submit`,点一下会把聊天输入连带发出去(与 CheckinEntry / SupervisionEntry 同一条)。
 *
 * ⚠️ **这颗按钮刻意没有徽章。** 监理那颗有徽章是因为「待确认」是**在等人干活**的东西
 * (D17:pending 不催办,没人知道就等于没登记);巡检记录不等人干活,它只是存档 ——
 * 给它一个「又出了 3 份」的红点,只会让工友每次都去点掉一个不需要处理的提醒。
 * 顺带省掉一条 60 秒轮询,和动作条上的一截宽度(那一行已经挤爆过一次)。
 */
export function ReportsEntry() {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);

  return (
    <>
      {/* 触摸目标三件套,与 CheckinEntry / SupervisionEntry 一字不差,理由也一样
          (2026-08-15 在 390×844 的实测:动作条被 flex 挤压,「打卡」只剩 42px 宽、
          两个字竖排):whitespace-nowrap 不许竖排 / shrink-0 不被压 /
          min-h-11 min-w-11(44px)触摸下限,sm: 之后归零,桌面观感不变。
          按钮上只放两个字也是为这条 —— 这一行现在有五件东西了。 */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap sm:min-h-0 sm:min-w-0"
        aria-label="巡檢記錄:下載或轉發已經出好的記錄"
      >
        <FileText className="size-5 text-gray-600" />
        <span className="text-sm text-gray-600">記錄</span>
      </button>
      {open && <ReportsPanel artifactBase={ARTIFACT_BASE} onClose={close} />}
    </>
  );
}

interface ReportsPanelProps {
  /** 产物静态出口。**收 props,自己不读 env** —— 规矩见 ARTIFACT_BASE 那段。 */
  artifactBase: string;
  onClose: () => void;
}

/**
 * 抽屉本体。
 *
 * 三种状态**分开画**,一种都不许合并:
 *   · 正在读     —— 「正在找…」
 *   · 读不出来   —— 「读不出来,再试一次」+ 重试按钮
 *   · 真的没有   —— 「还没有巡检记录,拍张照发给我」
 *
 * 🔴 后两种在界面上长得一模一样,而给人的下一步完全相反:一个该重试,
 * 一个该去拍照。合并成「暂无记录」的表现是 —— 后端挂了的时候,
 * 工友以为自己那份记录丢了,然后重拍一遍(又花一次识图的钱和时间)。
 * `parseReportsEnvelope` 的 `ok` 字段就是为这件事留的。
 */
function ReportsPanel({ artifactBase, onClose }: ReportsPanelProps) {
  const apiBase = useApiBase();
  const [reports, setReports] = useState<InspectionReport[]>([]);
  const [loading, setLoading] = useState(true);
  /** null = 还没读过 / 读成功了;字符串 = 这一屏读不出来,屏幕上原样显示它。 */
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const apiKey = getApiKey();
      const res = await fetch(reportsUrl(apiBase, { limit: PAGE_SIZE }), {
        headers: apiKey ? { "x-api-key": apiKey } : undefined,
      });
      const body = await res.text();
      const result = parseReportsEnvelope(body);
      if (!res.ok || !result.ok) {
        // 后端那句人话优先(它知道具体是什么事);拿不到才退回这句通用的。
        setError(result.userMsg || "這一屏讀不出來,點「再試一次」。");
        setReports([]);
        return;
      }
      setReports(result.reports);
      setTruncated(result.scanTruncated);
    } catch {
      // 网络层异常(压根没有响应)。**不许静默当成"没有记录"** —— 见组件头注。
      setError("連不上服務器。檢查網絡,再試一次。");
      setReports([]);
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label="巡檢記錄"
      onClick={onClose}
    >
      <div
        className="max-h-[80vh] w-full max-w-xl overflow-y-auto rounded-t-[22px] bg-white p-5 shadow-[0_1px_2px_rgba(23,28,26,0.05),0_24px_60px_rgba(23,28,26,0.12)] sm:rounded-[22px]"
        // 点内容区不该关掉抽屉 —— 点遮罩才关。
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mb-4 flex items-center justify-between gap-3">
          <h2 className="text-lg font-black text-[var(--gyt-ink)]">巡檢記錄</h2>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              className="flex min-h-11 items-center gap-1.5 rounded-[10px] border border-[var(--gyt-line)] px-3 text-sm text-[var(--gyt-ink-soft)] disabled:opacity-50 sm:min-h-0 sm:py-1.5"
            >
              <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </button>
            <button
              type="button"
              onClick={onClose}
              className="min-h-11 rounded-[10px] border border-[var(--gyt-line)] px-3 text-sm text-[var(--gyt-ink-soft)] sm:min-h-0 sm:py-1.5"
            >
              關閉
            </button>
          </div>
        </header>

        {loading && reports.length === 0 && (
          <p className="py-8 text-center text-sm text-[var(--gyt-muted)]">正在找…</p>
        )}

        {/* 读不出来 —— 与「真的没有」分开画,理由见组件头注。 */}
        {!loading && error !== null && (
          <div className="rounded-[12px] border border-amber-300 bg-amber-50 p-4">
            <p className="text-sm text-[var(--gyt-ink-soft)]">{error}</p>
            <button
              type="button"
              onClick={() => void load()}
              className="mt-3 min-h-11 rounded-[10px] bg-[var(--gyt-green)] px-4 text-sm font-bold text-white sm:min-h-0 sm:py-2"
            >
              再試一次
            </button>
          </div>
        )}

        {/* 真的一份都没有。这句话要告诉人**下一步做什么**。 */}
        {!loading && error === null && reports.length === 0 && (
          <p className="py-8 text-center text-sm text-[var(--gyt-muted)]">
            還沒有巡檢記錄。
            <br />
            工地上拍張照發給我,我看完就給你出一份。
          </p>
        )}

        {reports.length > 0 && (
          <ul className="flex flex-col gap-2">
            {reports.map((report) => (
              <ReportRow
                key={report.artifactId}
                report={report}
                artifactBase={artifactBase}
              />
            ))}
          </ul>
        )}

        {truncated && reports.length > 0 && (
          // 「只列了最近这些」**必须说**:不说的话人以为更早的记录丢了。
          <p className="mt-3 text-xs text-[var(--gyt-muted)]">
            只列了最近這些。要找更早的記錄,把編號報給我。
          </p>
        )}
      </div>
    </div>
  );
}

interface ReportRowProps {
  report: InspectionReport;
  artifactBase: string;
}

/**
 * 列表里的一行 = 编号 + 时间 + 大小 + 一颗下载按钮。
 *
 * ⚠️ **整行是一个 `<a download>`,不是 button + JS 下载。** 原生下载有三个好处:
 * 长按能「在新標籤打開 / 拷貝連結」(工友要把链接发到群里)、失败时浏览器自己会说话、
 * 而且不占任何 JS 状态。用 fetch + blob 的做法在这里全是坏处。
 *
 * 🔴 `download` 属性给的是**文件名**。不给的话,`/by-id/<32位hex>` 这条路径
 * 下载下来的文件就叫那串 hex —— 工友手机里躺着一个 `a3f9…c1.docx`,
 * 而他要找的是「上周那份临边防护的记录」。
 */
function ReportRow({ report, artifactBase }: ReportRowProps) {
  const headline = reportHeadline(report);
  const when = formatReportTime(report.reportNo);
  const size = formatSize(report.sizeBytes);

  return (
    <li>
      <a
        href={reportDownloadUrl(artifactBase, report.artifactId)}
        download={report.filename || undefined}
        className="flex min-h-14 items-center gap-3 rounded-[16px] border border-transparent bg-[#F9FAFA] px-4 py-3 transition hover:border-[var(--gyt-mint)]"
      >
        <FileText className="size-5 shrink-0 text-[var(--gyt-green)]" />
        <span className="min-w-0 flex-1">
          {/* 🔴 **主行是名字,不是编号(2026-08-25 设计审计 D9)。**
              改之前这里放的是 `reportTitle()`,也就是 19 位的
              `GYT-20260825-083033`;而**编号里那串数字就是下面那行的时间** ——
              主行是副行的机器格式复读,九行除编号外完全一样(同图标、同 37 KB),
              班组长要找「今早那份」只能一个个下载来看。
              ⚠️ tabular-nums 一起去掉了:那是给编号排列用的,而名字是中文,
                 等宽数字在这儿只会让字距变怪。 */}
          <span className="block truncate text-sm font-bold text-[var(--gyt-ink-soft)]">
            {headline}
          </span>
          <span className="block truncate text-xs text-[var(--gyt-muted)]">
            {/* 编号降到副行,和时间、大小并排 —— **没有删掉**:
                要拿编号去对账 / 报给监理的人照样一眼看得到。
                tabular-nums 跟着编号搬到这一行来了。
                三样之间那个「·」只在两边都有东西时才出现 —— 任何一样缺了,
                屏幕上不许留一个孤零零的分隔符(那看着像出了错)。 */}
            <span className="tabular-nums">{report.reportNo ?? ""}</span>
            {[report.reportNo ?? "", when, size].filter(Boolean).length > 1 && (
              <span aria-hidden="true">{report.reportNo ? " · " : ""}</span>
            )}
            {[when, size].filter(Boolean).join(" · ")}
          </span>
        </span>
        <Download className="size-4 shrink-0 text-[var(--gyt-muted)]" />
      </a>
    </li>
  );
}
