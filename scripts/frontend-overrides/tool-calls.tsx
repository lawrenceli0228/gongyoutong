/**
 * 工具调用的显示 —— 覆盖 agent-chat-ui 上游的 src/components/thread/messages/tool-calls.tsx
 *
 * ⚠️ 这个文件在 scripts/frontend-overrides/ 里,由 scripts/setup-frontend.sh 拷进
 *    frontend/。**别直接改 frontend/ 里那份** —— 那个目录不进 git,换台机器就没了。
 *
 * 为什么要覆盖上游:上游把每次工具调用都渲染成一个带边框的大方块,
 * 里面是原始工具名 + 一长串 UUID + 原始返回值。对开发调试有用,
 * 但工友通是给工地师傅用的,而演示时评委看到的也是这个界面 ——
 * 满屏的 `transfer_back_to_supervisor` 和 `call_00_bWobucJo6Os34E4XT4L79429`
 * 会把「拍张照就知道有什么隐患」这件事本身淹掉。
 *
 * 改成:默认一行中文摘要(「转给 安全巡检员」),点一下才展开原始细节。
 * 细节没有删掉 —— 讲技术架构时展开来看,恰恰是多智能体协作的证据。
 *
 * 除了折叠,本文件还负责**把「有东西可下、可确认」这件事从灰行里捞出来**,
 * 现在一共三张卡(判据各不相同,详见 ToolResult 里那段「三条分支」):
 *   · 巡检记录卡(W3)—— 按工具名认 render_inspection_report;
 *   · 监理文书下载卡(W9)—— 按返回值里的 `documents` 数组渲染 N 张;
 *   · 隐患台账卡(W9)—— 按返回值里的 `hazards` 数组,同时是监理处置面板的入口。
 * 没有这些卡,产物就只是折叠 JSON 里的一个字符串:点不开、下不到,而且不报错。
 */

import { AIMessage, ToolMessage } from "@langchain/langgraph-sdk";
import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ChevronRight,
  ArrowRightLeft,
  Check,
  Wrench,
  FileText,
  ExternalLink,
  Copy,
} from "lucide-react";
import { HazardIntakeCard, SupervisionDocCards } from "../supervision";
import {
  documentsFromToolData,
  failedItemsFromToolData,
  HAZARD_SOURCE_TOOLS,
  hazardsFromToolData,
} from "@/lib/supervision-lib";
// W12:定级徽章键收简繁两套。词表唯一真相在 lang-lib,别在本文件里再抄一份。
import {
  SEVERITY_WORDS,
  SEVERITY_WORDS_HANT,
  withHantKeys,
} from "@/lib/lang-lib";

/** 子 Agent 的中文名。加新 Agent 时往这里补一行,不补也不会坏(会退回显示英文名)。
 *  导出给 ai.tsx 覆盖件用(子 Agent 正文折叠行也要念中文名)。
 *
 *  🔴 **这张表要和后端 `graph.AGENT_REGISTRY` 的 name 对齐,而它没有守卫。**
 *     同一份名单在前端有三份拷贝,另外两份(`timing-lib.ts` 的 `AGENT_LABELS`、
 *     `GytStatusCards.tsx` 的 `AGENT_TO_CARD`)有一条**读源码比对**的测试盯着,
 *     六天里一个字没漂;**只有这份没有守卫,就漂了** —— W7 加 attendance 时漏补,
 *     于是工友问「上周谁来过」,一整屏繁體里冒出「轉給 attendance」。
 *     ⚠️ 补一行只是修今天这次;真正该做的是照 `timing-lib.test.ts` 那套
 *     `readAgentToCard` 加一条读 `graph.py` 的守卫(排在 P3)。 */
export const AGENT_NAMES: Record<string, string> = {
  supervisor: "調度中樞",
  safety: "安全巡檢員",
  // inspection 是「巡检出记录」英雄链(safety ─硬边→ report)编译成的子图。
  // 漏了这一行的代价很具体:它是演示第一跳,界面上会显示英文「转给 inspection」。
  inspection: "巡檢出記錄",
  knowledge: "規範檢索",
  schedule: "任務管理",
  cad: "圖紙查詢",
  report: "報告生成",
  // W7 的 attendance Agent。它**只管查** —— 打卡写入走 checkin_api.py 直连接口,
  // 不经任何 Agent(D15)。所以这里只会出现「转给考勤」这一种交接痕迹。
  // ⚠️ 这一行 2026-08-15 W7 落地时漏了,一直到 08-21 才补 —— 六天里界面上
  //    一直显示的是英文 agent 名,零报错。别再漏第二次。
  attendance: "考勤查詢",
  // W9 的 supervision Agent(S5 泳道落地)。它**只查、只建议** —— 改状态和出文书
  // 全部走 HTTP 端点,不经 LLM(方案 §5.1:法律行为不能由概率性系统单方面触发)。
  // 这一行现在补上,是为了 S5 一挂进 AGENT_REGISTRY,界面上不会冒出「转给 supervision」。
  supervision: "監理處置",
  // 墓碑:`ping` 曾经在这儿。2026-08-08 它已从 AGENT_REGISTRY **摘除**
  // (理由见 graph.py 该常量顶部的三连实锤),`transfer_to_ping` 这条路不存在,
  // 留着只会让下一个人以为它还能被路由到。要复活先改后端注册表。
};

/** 业务工具的中文名。
 *
 * 漏一条不会崩(summarize 会退回显示英文函数名),但界面上就会冒出
 * `query_dimension` 这种东西 —— 这正是覆盖上游的初衷,别让它漏回来。
 *
 * 加新工具时的对齐方法:把 `backend/src/gyt/agents/` 下所有 `@tool("名字", ...)`
 * grep 一遍,逐条对照这张表补齐。名字要按工具的**真实行为**取,不能望文生义 ——
 * 下面这几条就是照着各 tools.py 里给模型看的 description 定的,不是猜的:
 *   · query_dimension  只读图上**已经标注**的尺寸,图上没标它如实说做不了,
 *                      绝不替人估算 —— 所以叫「查标注尺寸」而不是「量尺寸」。
 *   · list_components  数的是块 / 图元的数量与分布,不是"列一份构件表"。
 *   · render_preview   出的是整张图的 PNG 预览,图元太多会主动跳过不渲染。
 *   · search_regulation 查的是规范条文原文,带文件名 + 页码出处,查不到就说查不到。
 */
const TOOL_NAMES: Record<string, string> = {
  // safety / report(英雄链)
  analyze_site_photo: "查看現場照片",
  render_inspection_report: "生成巡檢記錄",
  // schedule(任务台账)
  add_task: "記任務",
  list_tasks: "查任務清單",
  reschedule_task: "改期限",
  finish_task: "任務銷項",
  // cad(看图纸)
  list_drawings: "查圖紙清單",
  parse_drawing: "看圖紙概覽",
  query_dimension: "查標註尺寸",
  list_components: "數圖上構件",
  layer_stats: "查圖層清單",
  render_preview: "出圖紙預覽",
  // knowledge(规范检索)
  search_regulation: "查規範條文",
  // supervision(监理处置 —— **只读那三件**,方案 §5.1。写入不走工具:
  // 签发通知单/暂停令是法律行为,由界面直连 HTTP 端点触发,LLM 一步都不经过)
  list_hazards: "查隱患清單",
  get_hazard: "查隱患詳情",
  suggest_disposal: "看該怎麼處置",
  // attendance(考勤 —— **同款只读形态**,W7/D15。打卡写入走 checkin_api.py 直连接口)
  // ⚠️ 与上面的 AGENT_NAMES 同一批漏登记,补于 2026-08-21。
  list_attendance_days: "查誰來過",
  list_attendance_detail: "查打卡明細",
  // ping(链路自检,不是业务工具)。它的 Agent 早已摘除(见 AGENT_NAMES 的墓碑),
  // 但 echo 这个工具名留着无害 —— 真跑起来它不会出现,出现了也念得出中文。
  echo: "回聲自檢",
};

/** 巡检记录工具名。它的返回值要单独渲染成卡片,不能只折进灰行,理由见 ARTIFACT_BASE。
 *
 * ⚠️ **这个常量只服务巡检记录那一张卡,别再往它身上加第二种文书**(W9 之前它是
 * 「工具名 → 卡片」的唯一判据,于是监理那六种文书在界面上一个出口都没有:
 * 点不开、下不到,而且不会报错 —— 方案 §6.5 点名的就是这一行)。
 * 新的文书走的是另一条判据:**看返回值里有没有 `documents` 数组**,与本常量无关,
 * 两条分支互不影响 —— 详见下面 ToolResult 里那段「三条分支」的注释。 */
const REPORT_TOOL_NAME = "render_inspection_report";

/**
 * 巡检记录的静态出口。
 *
 * 为什么需要这一层:docx 落在 `<仓库根>/data/artifacts/<日期>/<32位id>.docx`
 * (= `Settings.artifacts_dir`,容器里是 `/app/data/artifacts`,挂的是同一个目录),
 * 而 `langgraph.json` 只声明了图 —— **没有任何 HTTP 端点能把文件给出去**。
 * 于是「拍照自动出 Word」这个卖点,在界面上此前的最终形态是折叠 JSON 里的
 * 一个 path 字符串:硬边、数据保真(不经 LLM 转抄)、免责落款那一整套设计,
 * 评委看不到那份文档就等于全白做。
 *
 * 起法(仓库根):`make serve-artifacts`
 * ⚠️ 端口号有两处(本文件与 Makefile 的 ARTIFACTS_PORT),要改一起改。
 *
 * **本机开发**默认 `http://127.0.0.1:8788`(只绑回环,照片和巡检记录不出本机)。
 * **公网部署**必须换成同源路径,否则 127.0.0.1 指的是测试者自己的电脑(下载链接是死的),
 * 而且 https 页面拉 http 资源会被浏览器按 mixed content 直接拦掉。
 * 由 docker-compose.vps.yml 的 build args 传 `${GYT_PUBLIC_ORIGIN}/artifacts`。
 * **与 human.tsx、checkin.tsx(W7 打卡凭证图)的 ARTIFACT_BASE 同源,三处要改一起改。**
 *
 * ⚠️ NEXT_PUBLIC_* 是**编译期**变量:改了要重建前端镜像。
 */
const ARTIFACT_BASE =
  process.env.NEXT_PUBLIC_ARTIFACT_BASE || "http://127.0.0.1:8788";

/** 把信封里的绝对路径换成静态服务的 URL。
 *
 * 取最后两段(`<日期目录>/<文件名>`),因为静态服务的根就是 artifacts_dir。
 * 不自己按当天日期拼 —— 落盘目录名走的是 UTC,晚上演示时会落在"明天"那个文件夹里。 */
function artifactUrl(path: string): string | null {
  const segs = String(path || "")
    .split(/[/\\]/)
    .filter(Boolean);
  if (segs.length < 2) return null;
  return `${ARTIFACT_BASE}/${segs.slice(-2).map(encodeURIComponent).join("/")}`;
}

/**
 * 定级徽章配色。四个取值来自 `agents/safety/severity.py:36-39`,别自由发挥。
 *
 * **简繁两套键都收**(W12,2026-08-17)。词表与繁體镜像的唯一真相在
 * `@/lib/lang-lib` 的 `SEVERITY_WORDS` / `SEVERITY_WORDS_HANT`;
 * `badge-keys.test.ts` 用真转换器钉着「繁體项 === s2hk(简体项)」。
 * 变形两处:较→較、待定级→待定級,另两个同形。
 *
 * 理由与 markdown-text.tsx 的 STATUS_CHIPS 同源:后端常量虽是简体,
 * 但**经模型转述**才到界面,而模型本来就有 25%-75% 的概率吐繁體 ——
 * 所以「只认简体键」这个 bug 在 W12 之前就存在。
 *
 * 🔴 **下面那张内表的四个键必须留简体,界面繁體化那一批不许顺手转它。**
 * 看 `withHantKeys` 的实现:它只拿**简体词**去调 styleOf(繁體那一套键是它自己
 * 在外面配上同一份样式的)。把内表的键改成繁體 → styleOf 一个都查不到 →
 * 全落到 `??` 后面那个灰底默认色,「较大」「待定级」两档静默掉色,
 * 而徽章照旧显示、控制台干净、测试全绿。
 * 另外这四个键是**标识符不是字符串字面量**,hant-scan.mjs 走 AST 扫不到它们 ——
 * 守卫不会替你拦这一手,只能靠这段注释。
 */
const SEVERITY_CHIP: Record<string, string> = withHantKeys(
  SEVERITY_WORDS,
  SEVERITY_WORDS_HANT,
  (word) =>
    ({
      重大: "bg-red-50 text-red-700 ring-red-200",
      较大: "bg-amber-50 text-amber-700 ring-amber-200",
      一般: "bg-sky-50 text-sky-700 ring-sky-200",
      待定级: "bg-gray-100 text-gray-500 ring-gray-200",
    })[word] ?? "bg-gray-100 text-gray-500 ring-gray-200",
);

type ReportData = {
  report_no?: string;
  filename?: string;
  path?: string;
  violations?: string[];
  max_severity?: string;
  label?: string;
};

type Summary = { icon: "handoff" | "tool"; text: string };

/**
 * 把工具名翻成一句人话。
 *
 * 交接工具由 langgraph_supervisor 按 `transfer_to_<agent>` /
 * `transfer_back_to_supervisor` 的模式自动生成,所以这里按模式解析而不是硬编码 ——
 * 以后加 Agent 不用回来改这个函数。
 */
function summarize(name: string): Summary {
  const back = name.match(/^transfer_back_to_(.+)$/);
  if (back) {
    const who = AGENT_NAMES[back[1]] ?? back[1];
    return { icon: "handoff", text: `交回 ${who}` };
  }
  const to = name.match(/^transfer_to_(.+)$/);
  if (to) {
    const who = AGENT_NAMES[to[1]] ?? to[1];
    return { icon: "handoff", text: `轉給 ${who}` };
  }
  return { icon: "tool", text: TOOL_NAMES[name] ?? name };
}

function isComplexValue(value: any): boolean {
  return Array.isArray(value) || (typeof value === "object" && value !== null);
}

/** 一行可展开的痕迹条。折叠时只有一行灰字,不抢正文的注意力。
 *  导出给 ai.tsx 覆盖件复用:子 Agent 的原始汇报也折叠成这种行。 */
export function Trace({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mx-auto w-full max-w-3xl">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="group flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-[13px] text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-700 focus-visible:ring-2 focus-visible:ring-gray-300 focus-visible:outline-none"
      >
        <ChevronRight
          className={`h-3.5 w-3.5 shrink-0 text-gray-400 transition-transform duration-150 ${
            open ? "rotate-90" : ""
          }`}
        />
        {icon}
        <span className="truncate">{label}</span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <div className="mt-1 ml-[26px] border-l border-gray-200 pl-3 text-[13px]">
              {children}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export function ToolCalls({
  toolCalls,
}: {
  toolCalls: AIMessage["tool_calls"];
}) {
  if (!toolCalls || toolCalls.length === 0) return null;

  return (
    <div className="mx-auto grid max-w-3xl gap-0.5">
      {toolCalls.map((tc, idx) => {
        const args = (tc.args ?? {}) as Record<string, any>;
        const entries = Object.entries(args);
        const { icon, text } = summarize(tc.name);
        return (
          <Trace
            key={idx}
            label={text}
            icon={
              icon === "handoff" ? (
                <ArrowRightLeft className="h-3.5 w-3.5 shrink-0 text-gray-400" />
              ) : (
                <Wrench className="h-3.5 w-3.5 shrink-0 text-gray-400" />
              )
            }
          >
            <div className="space-y-1 py-1 text-gray-500">
              <div className="font-mono text-[12px] text-gray-400">
                {tc.name}
              </div>
              {entries.length > 0 ? (
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                  {entries.map(([key, value]) => (
                    <div
                      key={key}
                      className="col-span-2 grid grid-cols-subgrid items-baseline"
                    >
                      <dt className="font-mono text-[12px] text-gray-500">
                        {key}
                      </dt>
                      <dd className="break-all text-gray-600">
                        {isComplexValue(value) ? (
                          <code className="font-mono text-[12px]">
                            {JSON.stringify(value)}
                          </code>
                        ) : (
                          String(value)
                        )}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <div className="text-gray-400">（無參數）</div>
              )}
            </div>
          </Trace>
        );
      })}
    </div>
  );
}

/**
 * 巡检记录卡片 —— 「拍照 → 自动出 Word」这条链唯一看得见的产出。
 *
 * 为什么渲染在这里而不是让模型报链接:report 的正文会被 ai.tsx 折叠,
 * 链接要靠 supervisor 转述才能露出来 —— 那是在**赌模型配合度**,
 * 而本项目已经在表格那件事上赌输过两次(见 ai.tsx 顶部的案情)。
 * 渲染层是确定的:谁产的谁展示。
 */
function ReportCard({ data }: { data: ReportData }) {
  const [copied, setCopied] = useState(false);
  const url = artifactUrl(data.path ?? "");
  const count = data.violations?.length ?? 0;
  const severity = data.max_severity ?? "";
  const chip = SEVERITY_CHIP[severity];

  const copyPath = () => {
    if (!data.path) return;
    void navigator.clipboard.writeText(data.path).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="mx-auto w-full max-w-3xl">
      <div className="my-2 flex items-start gap-3 rounded-xl border border-orange-200 bg-orange-50/40 px-4 py-3">
        <FileText className="mt-0.5 h-5 w-5 shrink-0 text-orange-600" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-medium text-gray-900">巡檢記錄</span>
            <span className="font-mono text-[13px] text-gray-600">
              {data.report_no}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-gray-600">
            <span>
              {count > 0 ? `共 ${count} 處隱患` : "未發現受控清單內的隱患"}
            </span>
            {chip && (
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-[12px] font-medium ring-1 ring-inset ${chip}`}
              >
                {/* 「最高」简繁同形;`severity` 是**模型转述**过来的原文,简繁都可能,
                    这里**刻意不转**:运行时转换器要拉 438 KB 字典,按 W12 方案 §5.2
                    只许挂在点开才加载的面板上,聊天主界面是常驻的,铺上去等于
                    每个从不看面板的简体工友首屏都白下一份。
                    功能上不吃亏 —— SEVERITY_CHIP 简繁两套键都收,上色不受影响,
                    代价只是这两个字偶尔跟着模型显示成简体。 */}
                最高 {severity}
              </span>
            )}
          </div>
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            {url && (
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 rounded-md bg-orange-600 px-2.5 py-1 text-[13px] font-medium text-white transition-colors hover:bg-orange-700"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                打開文檔
              </a>
            )}
            <button
              type="button"
              onClick={copyPath}
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1 text-[13px] text-gray-700 transition-colors hover:bg-gray-50"
            >
              {copied ? (
                <Check className="h-3.5 w-3.5 text-emerald-600" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )}
              {copied ? "已複製" : "複製路徑"}
            </button>
          </div>
          <div className="mt-1.5 text-[11px] text-gray-400">
            打不開?先在倉庫根執行{" "}
            <code className="font-mono">make serve-artifacts</code>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ToolResult({ message }: { message: ToolMessage }) {
  let parsed: any;
  let isJson = false;
  try {
    parsed = JSON.parse(message.content as string);
    isJson = isComplexValue(parsed);
  } catch {
    isJson = false;
  }

  const name = message.name ?? "";
  const { text } = summarize(name);
  // 交接类的结果没有信息量(永远是 "Successfully transferred to X"),
  // 摘要就写「已接手」;业务工具则说「返回结果」。
  const done = /^transfer(_back)?_to_/.test(name) ? "已接手" : "已返回結果";

  /**
   * ── 三条互不影响的卡片分支 ────────────────────────────────────────────
   *
   * ① 巡检记录:判据是**工具名** === render_inspection_report(W3 就有的那条,
   *    一个字节都没动 —— 它是已上线的演示主链);
   * ② 监理文书:判据是**返回值里有 `documents` 数组**(W9,契约 Codex#16 冻结的形状)。
   *    它按数组渲染 **N 张**下载卡 —— 「N 张卡」与「N 份文书」天然对齐,
   *    三份里少出一份当场看得出来。定死形状就是为了避免静默丢件;
   * ③ 隐患台账:判据是**返回值里有 `hazards` 数组**且工具在 HAZARD_SOURCE_TOOLS 里
   *    (现在只有 analyze_site_photo 会带,S5 的 supervision 只读工具随后)。
   *    这张卡是**监理处置面板唯一的入口**。
   *
   * 🔴 为什么 ② 不会误伤 ①:`render_inspection_report` 的信封里
   *    只有 report_no / filename / path / violations / max_severity / label
   *    (见 agents/report/tools.py),**没有 `documents` 这个键** —— 分支 ② 对它
   *    结构上就不可能命中。③ 同理:它还额外要求工具名在白名单里。
   *    三条各判各的、各渲各的卡,谁都不吃掉谁,灰行也照旧保留(展开原始信封
   *    正是多智能体协作的证据)。
   *
   * 🔴 这里用的两个解析函数**都不抛异常**(supervision-lib 里那条注释:渲染路径上
   *    抛出去 = 整条消息白屏,而它们只负责一张附加卡片)。别换成 parseActionEnvelope
   *    那个严格版 —— 那一份是给「人刚点了签发」的 HTTP 路径用的。
   */
  const envelopeData = isJson && parsed?.ok === true ? parsed.data : null;

  const report: ReportData | null =
    isJson && name === REPORT_TOOL_NAME && parsed?.ok === true && parsed?.data
      ? (parsed.data as ReportData)
      : null;

  const documents = documentsFromToolData(envelopeData);
  const hazards = (HAZARD_SOURCE_TOOLS as readonly string[]).includes(name)
    ? hazardsFromToolData(envelopeData)
    : [];
  const failedItems = (HAZARD_SOURCE_TOOLS as readonly string[]).includes(name)
    ? failedItemsFromToolData(envelopeData)
    : [];

  return (
    <>
      {report && <ReportCard data={report} />}
      {documents.length > 0 && (
        <div className="mx-auto w-full max-w-3xl">
          {/* artifactBase 由这里传下去,supervision.tsx **不自己读 process.env** ——
              NEXT_PUBLIC_ARTIFACT_BASE 那条链(CLAUDE.md 同源清单)断在任何一环
              都不报错,少一个读者就少一处能断的地方。 */}
          <SupervisionDocCards
            documents={documents}
            artifactBase={ARTIFACT_BASE}
          />
        </div>
      )}
      {(hazards.length > 0 || failedItems.length > 0) && (
        <HazardIntakeCard
          hazards={hazards}
          failedItems={failedItems}
          artifactBase={ARTIFACT_BASE}
        />
      )}
      <Trace
        label={`${text} · ${done}`}
        icon={<Check className="h-3.5 w-3.5 shrink-0 text-emerald-500" />}
      >
        <div className="py-1">
          {isJson ? (
            <pre className="overflow-x-auto font-mono text-[12px] whitespace-pre-wrap text-gray-600">
              {JSON.stringify(parsed, null, 2)}
            </pre>
          ) : (
            <div className="break-words whitespace-pre-wrap text-gray-600">
              {String(message.content)}
            </div>
          )}
        </div>
      </Trace>
    </>
  );
}
