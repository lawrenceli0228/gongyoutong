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

/** 子 Agent 的中文名。加新 Agent 时往这里补一行,不补也不会坏(会退回显示英文名)。
 *  导出给 ai.tsx 覆盖件用(子 Agent 正文折叠行也要念中文名)。 */
export const AGENT_NAMES: Record<string, string> = {
  supervisor: "调度中枢",
  safety: "安全巡检员",
  // inspection 是「巡检出记录」英雄链(safety ─硬边→ report)编译成的子图。
  // 漏了这一行的代价很具体:它是演示第一跳,界面上会显示英文「转给 inspection」。
  inspection: "巡检出记录",
  ping: "连通性自检",
  knowledge: "规范检索",
  schedule: "任务管理",
  cad: "图纸查询",
  report: "报告生成",
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
  analyze_site_photo: "查看现场照片",
  render_inspection_report: "生成巡检记录",
  // schedule(任务台账)
  add_task: "记任务",
  list_tasks: "查任务清单",
  reschedule_task: "改期限",
  finish_task: "任务销项",
  // cad(看图纸)
  list_drawings: "查图纸清单",
  parse_drawing: "看图纸概览",
  query_dimension: "查标注尺寸",
  list_components: "数图上构件",
  layer_stats: "查图层清单",
  render_preview: "出图纸预览",
  // knowledge(规范检索)
  search_regulation: "查规范条文",
  // ping(链路自检,不是业务工具)
  echo: "回声自检",
};

/** 巡检记录工具名。它的返回值要单独渲染成卡片,不能只折进灰行,理由见 ARTIFACT_BASE。 */
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
 * **与 human.tsx 的 ARTIFACT_BASE 同源,要改一起改。**
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

/** 定级徽章配色。四个取值来自 agents/safety/severity.py,别自由发挥。 */
const SEVERITY_CHIP: Record<string, string> = {
  重大: "bg-red-50 text-red-700 ring-red-200",
  较大: "bg-amber-50 text-amber-700 ring-amber-200",
  一般: "bg-sky-50 text-sky-700 ring-sky-200",
  待定级: "bg-gray-100 text-gray-500 ring-gray-200",
};

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
    return { icon: "handoff", text: `转给 ${who}` };
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
                <div className="text-gray-400">（无参数）</div>
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
            <span className="font-medium text-gray-900">巡检记录</span>
            <span className="font-mono text-[13px] text-gray-600">
              {data.report_no}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-gray-600">
            <span>
              {count > 0 ? `共 ${count} 处隐患` : "未发现受控清单内的隐患"}
            </span>
            {chip && (
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-[12px] font-medium ring-1 ring-inset ${chip}`}
              >
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
                打开文档
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
              {copied ? "已复制" : "复制路径"}
            </button>
          </div>
          <div className="mt-1.5 text-[11px] text-gray-400">
            打不开?先在仓库根执行{" "}
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
  const done = /^transfer(_back)?_to_/.test(name) ? "已接手" : "已返回结果";

  // 巡检记录额外出一张卡片。灰行照旧保留 —— 展开原始信封正是多智能体协作的证据。
  const report: ReportData | null =
    isJson && name === REPORT_TOOL_NAME && parsed?.ok === true && parsed?.data
      ? (parsed.data as ReportData)
      : null;

  return (
    <>
      {report && <ReportCard data={report} />}
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
