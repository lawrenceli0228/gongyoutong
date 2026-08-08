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
import { ChevronRight, ArrowRightLeft, Check, Wrench } from "lucide-react";

/** 子 Agent 的中文名。加新 Agent 时往这里补一行,不补也不会坏(会退回显示英文名)。 */
const AGENT_NAMES: Record<string, string> = {
  supervisor: "调度中枢",
  safety: "安全巡检员",
  ping: "连通性自检",
  knowledge: "规范检索",
  schedule: "任务管理",
  cad: "图纸查询",
  report: "报告生成",
};

/** 业务工具的中文名。 */
const TOOL_NAMES: Record<string, string> = {
  analyze_site_photo: "查看现场照片",
  render_inspection_report: "生成巡检记录",
  add_task: "记任务",
  list_tasks: "查任务清单",
  reschedule_task: "改期限",
  finish_task: "任务销项",
  echo: "回声自检",
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

/** 一行可展开的痕迹条。折叠时只有一行灰字,不抢正文的注意力。 */
function Trace({
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

  return (
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
  );
}
