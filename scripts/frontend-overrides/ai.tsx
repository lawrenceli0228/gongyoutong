/**
 * AI 消息的显示 —— 覆盖 agent-chat-ui 上游的 src/components/thread/messages/ai.tsx
 *
 * ⚠️ 这个文件在 scripts/frontend-overrides/ 里,由 scripts/setup-frontend.sh 拷进
 *    frontend/。**别直接改 frontend/ 里那份** —— 那个目录不进 git,换台机器就没了。
 *
 * 为什么要覆盖上游:supervisor 架构里每个回答天然有**两个嗓子** ——
 * 子 Agent 先把结果说一遍,交回后 supervisor 按汇报规则再复述一遍。
 * 上游把两份都平铺成正文气泡,用户看到的就是同一份任务清单出现两次
 * (2026-08-08 真机截图实锤:「重复内容污染 context」)。
 *
 * 改法:**supervisor 是唯一对用户说话的嗓子**。子 Agent(消息带 name 且不是
 * supervisor)的正文折叠成一行灰字痕迹,点开才看原文 —— 不删证据:
 * 讲多智能体架构时点开看,恰是「活是谁干的」的第一手记录。
 * 与 tool-calls.tsx 的折叠痕迹同一套视觉语言(复用其 Trace 组件)。
 *
 * 上游基线:agent-chat-ui 2026-08 版;除头注、两个 import、subAgentName
 * 判定与 contentString 渲染分支外,其余逐字保持上游原样,方便对 diff 升级。
 */

import { parsePartialJson } from "@langchain/core/output_parsers";
import { useStreamContext } from "@/providers/Stream";
import { AIMessage, Checkpoint, Message } from "@langchain/langgraph-sdk";
import { useStream } from "@langchain/langgraph-sdk/react";
import { getContentString } from "../utils";
import { BranchSwitcher, CommandBar } from "./shared";
import { MarkdownText } from "../markdown-text";
import { LoadExternalComponent } from "@langchain/langgraph-sdk/react-ui";
import { cn } from "@/lib/utils";
import { ToolCalls, ToolResult, Trace, AGENT_NAMES } from "./tool-calls";
import { MessageContentComplex } from "@langchain/core/messages";
import { Fragment } from "react/jsx-runtime";
import { isAgentInboxInterruptSchema } from "@/lib/agent-inbox-interrupt";
import { ThreadView } from "../agent-inbox";
import { useQueryState, parseAsBoolean } from "nuqs";
import { GenericInterruptView } from "./generic-interrupt";
import { useArtifact } from "../artifact";
import { MessageSquareText } from "lucide-react";
import { useMemo } from "react";
// W12:繁體答话。判定归 lang-lib(纯函数、可单测),拉字典归 hant-convert(懒加载)。
import { resolveLang } from "@/lib/lang-lib";
import { useHantText } from "@/lib/hant-convert";

function CustomComponent({
  message,
  thread,
}: {
  message: Message;
  thread: ReturnType<typeof useStreamContext>;
}) {
  const artifact = useArtifact();
  const { values } = useStreamContext();
  const customComponents = values.ui?.filter(
    (ui) => ui.metadata?.message_id === message.id,
  );

  if (!customComponents?.length) return null;
  return (
    <Fragment key={message.id}>
      {customComponents.map((customComponent) => (
        <LoadExternalComponent
          key={customComponent.id}
          stream={thread as unknown as ReturnType<typeof useStream>}
          message={customComponent}
          meta={{ ui: customComponent, artifact }}
        />
      ))}
    </Fragment>
  );
}

/** markdown 表格块的判定:表头行 + 分隔行。 */
const TABLE_BLOCK_RE = /(^|\n)\s*\|[^\n]+\|\s*\n\s*\|[\s:|-]+\|/;

/** 裁掉 supervisor 对台账的复读,保留真正的增量短评。
 *
 * 为什么裁两种形态:提示词摁不住 DeepSeek 的「最终回答要自包含」本能 ——
 * 实测三连:让它照搬表格,它有时只说「见上表」;禁止它重抄,它硬抄;
 * 再禁,它把表格改写成编号清单。所以渲染层同时裁:
 *   ① markdown 表格行(|…|)
 *   ② 带任务号(T\d+)的列表行 —— 它复述任务必带 T 号,这是可靠指纹
 * 「最近要赶的是 T4」这类**非列表**的点评句会保留,那是它真正的增量价值。
 * 收尾:指着被裁内容的冒号改成句号,多余空行合并。 */
function stripLedgerEcho(md: string): string {
  const kept = md
    .split("\n")
    .filter(
      (line) =>
        !/^\s*\|.*\|\s*$/.test(line) &&
        !/^\s*(\d+[.、]|[-*•])\s.*T\d+/.test(line),
    );
  return kept
    .join("\n")
    .replace(/[:：]\s*(\n|$)/g, "。$1")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function parseAnthropicStreamedToolCalls(
  content: MessageContentComplex[],
): AIMessage["tool_calls"] {
  const toolCallContents = content.filter((c) => c.type === "tool_use" && c.id);

  return toolCallContents.map((tc) => {
    const toolCall = tc as Record<string, any>;
    let json: Record<string, any> = {};
    if (toolCall?.input) {
      try {
        json = parsePartialJson(toolCall.input) ?? {};
      } catch {
        // Pass
      }
    }
    return {
      name: toolCall.name ?? "",
      id: toolCall.id ?? "",
      args: json,
      type: "tool_call",
    };
  });
}

interface InterruptProps {
  interrupt?: unknown;
  isLastMessage: boolean;
  hasNoAIOrToolMessages: boolean;
}

function Interrupt({
  interrupt,
  isLastMessage,
  hasNoAIOrToolMessages,
}: InterruptProps) {
  const fallbackValue = Array.isArray(interrupt)
    ? (interrupt as Record<string, any>[])
    : (((interrupt as { value?: unknown } | undefined)?.value ??
        interrupt) as Record<string, any>);

  return (
    <>
      {isAgentInboxInterruptSchema(interrupt) &&
        (isLastMessage || hasNoAIOrToolMessages) && (
          <ThreadView interrupt={interrupt} />
        )}
      {interrupt &&
      !isAgentInboxInterruptSchema(interrupt) &&
      (isLastMessage || hasNoAIOrToolMessages) ? (
        <GenericInterruptView interrupt={fallbackValue} />
      ) : null}
    </>
  );
}

export function AssistantMessage({
  message,
  isLoading,
  handleRegenerate,
}: {
  message: Message | undefined;
  isLoading: boolean;
  handleRegenerate: (parentCheckpoint: Checkpoint | null | undefined) => void;
}) {
  const content = message?.content ?? [];
  const contentString = getContentString(content);
  const [hideToolCalls] = useQueryState(
    "hideToolCalls",
    parseAsBoolean.withDefault(false),
  );

  const thread = useStreamContext();
  const isLastMessage =
    thread.messages[thread.messages.length - 1].id === message?.id;
  const hasNoAIOrToolMessages = !thread.messages.find(
    (m) => m.type === "ai" || m.type === "tool",
  );
  const meta = message ? thread.getMessagesMetadata(message) : undefined;
  const threadInterrupt = thread.interrupt;

  const parentCheckpoint = meta?.firstSeenState?.parent_checkpoint;
  const anthropicStreamedToolCalls = Array.isArray(content)
    ? parseAnthropicStreamedToolCalls(content)
    : undefined;

  const hasToolCalls =
    message &&
    "tool_calls" in message &&
    message.tool_calls &&
    message.tool_calls.length > 0;
  const toolCallsHaveContents =
    hasToolCalls &&
    message.tool_calls?.some(
      (tc) => tc.args && Object.keys(tc.args).length > 0,
    );
  const hasAnthropicToolCalls = !!anthropicStreamedToolCalls?.length;
  const isToolResult = message?.type === "tool";

  // 子 Agent 判定:langgraph_supervisor 给每条 AI 消息都标了 name
  // (真机核实过线程状态:schedule / safety / supervisor 一个不落)。
  // 只有 supervisor 的正文用大字排版;其余嗓子一律折叠。
  const subAgentName =
    message?.type === "ai" &&
    "name" in message &&
    message.name &&
    message.name !== "supervisor"
      ? message.name
      : undefined;

  // 例外:带 markdown 表格的子 Agent 消息**不折叠**。
  // 教训(2026-08-08 真机,两个方向都踩过):表格折叠后靠 supervisor 重抄 ——
  // 它有时不抄(只说「见上表」,用户什么都看不到),有时又违令硬抄(表格出现两次)。
  // 提示词两头都摁不住,所以结构性定死:表格谁产谁展示,supervisor 消息里的表格
  // 在渲染层裁掉(见下面 turnHasVisibleAgentTable)。表格恰好出现一次,不赌模型。
  const hasMarkdownTable = TABLE_BLOCK_RE.test(contentString);

  // supervisor 消息的复读剔重:本轮(上一条 human 之后)已有子 Agent 的表格
  // 展示在上面时,supervisor 正文里的表格行与带 T 号的清单行全部裁掉,只留短评。
  const turnHasVisibleAgentTable = (() => {
    if (subAgentName || message?.type !== "ai") return false;
    const idx = thread.messages.findIndex((m) => m.id === message.id);
    for (let i = idx - 1; i >= 0; i--) {
      const m = thread.messages[i];
      if (m.type === "human") break;
      if (
        m.type === "ai" &&
        "name" in m &&
        m.name &&
        m.name !== "supervisor" &&
        TABLE_BLOCK_RE.test(getContentString(m.content ?? []))
      ) {
        return true;
      }
    }
    return false;
  })();

  const strippedString = turnHasVisibleAgentTable
    ? stripLedgerEcho(contentString)
    : contentString;

  // ===========================================================================
  // W12 第一批:按用户输入的语言答话(目前只做繁體)
  // ---------------------------------------------------------------------------
  // 为什么转换在**这里**,而不在 markdown-text.tsx 里面:
  //   `MarkdownText` 有四个消费者,其中两个是**上游的英文调试界面**
  //   (agent-inbox 的 state-view / inbox-item-input)。放进 markdown-text
  //   会把它们也转了 —— 那是我们不拥有的表面,而且转它是错的。
  //
  // 为什么繁體要靠转换而不是靠提示词:探针 155 次真调用实测,
  //   靠提示词让模型换繁體在**需要调工具的 Agent** 上只有 25%-60%
  //   (门槛 80%),五个档位试遍都不够 —— CLAUDE.md「提示词只是概率性生效,
  //   结构件才兜得住」的第四次应验。方案 §4.5。
  //
  // 语种怎么定:显式选择(第二批才有切换器,现在恒 null)→ 往回扫本线程的
  //   用户发言 → 落 DEFAULT_LANG。「第二句只打好」靠往回扫那一层。
  //   用户发言本身**永不转换**(shouldConvert 对 human 恒 false)——
  //   human.tsx 的 `(照片编号:…)` 正则是简体的,转了照片就不渲染成图。
  // ===========================================================================
  const userTextsNewestFirst = useMemo(
    () =>
      thread.messages
        .filter((m) => m.type === "human")
        .map((m) => getContentString(m.content))
        .reverse(),
    [thread.messages],
  );
  const lang = resolveLang(null, userTextsNewestFirst);
  const displayString = useHantText(strippedString, "ai", lang);

  // 「Transferring back to supervisor」是 langgraph_supervisor 注入的**收工信号**,
  // 后端必须保留(关掉会让 supervisor 复转直至熔断,graph.py 有血泪注释)。
  // 按**内容**兜底判定而不是只看 name:流式期间消息可能还没带 name,
  // 只按 name 折叠的话,这句英文会先裸奔一会儿(真机截图踩过)。
  const isBackHandoff = /^Transferring back to /.test(contentString.trim());

  if (isToolResult && hideToolCalls) {
    return null;
  }

  return (
    <div className="group mr-auto flex w-full items-start gap-2">
      <div className="flex w-full flex-col gap-2">
        {isToolResult ? (
          <>
            <ToolResult message={message} />
            <Interrupt
              interrupt={threadInterrupt}
              isLastMessage={isLastMessage}
              hasNoAIOrToolMessages={hasNoAIOrToolMessages}
            />
          </>
        ) : (
          <>
            {((subAgentName || isBackHandoff) && !hasMarkdownTable
              ? contentString
              : displayString
            ).length > 0 &&
              ((subAgentName || isBackHandoff) && !hasMarkdownTable ? (
                <Trace
                  label={
                    isBackHandoff
                      ? "交回调度中枢"
                      : `${AGENT_NAMES[subAgentName ?? ""] ?? subAgentName} · 已把结果交给调度中枢`
                  }
                  icon={
                    <MessageSquareText className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                  }
                >
                  <div className="py-1 whitespace-pre-wrap break-words text-gray-600">
                    {contentString}
                  </div>
                </Trace>
              ) : (
                <div className="py-1">
                  <MarkdownText>{displayString}</MarkdownText>
                </div>
              ))}

            {!hideToolCalls && (
              <>
                {(hasToolCalls && toolCallsHaveContents && (
                  <ToolCalls toolCalls={message.tool_calls} />
                )) ||
                  (hasAnthropicToolCalls && (
                    <ToolCalls toolCalls={anthropicStreamedToolCalls} />
                  )) ||
                  (hasToolCalls && (
                    <ToolCalls toolCalls={message.tool_calls} />
                  ))}
              </>
            )}

            {message && (
              <CustomComponent
                message={message}
                thread={thread}
              />
            )}
            <Interrupt
              interrupt={threadInterrupt}
              isLastMessage={isLastMessage}
              hasNoAIOrToolMessages={hasNoAIOrToolMessages}
            />
            <div
              className={cn(
                "mr-auto flex items-center gap-2 transition-opacity",
                "opacity-0 group-focus-within:opacity-100 group-hover:opacity-100",
              )}
            >
              <BranchSwitcher
                branch={meta?.branch}
                branchOptions={meta?.branchOptions}
                onSelect={(branch) => thread.setBranch(branch)}
                isLoading={isLoading}
              />
              <CommandBar
                content={contentString}
                isLoading={isLoading}
                isAiMessage={true}
                handleRegenerate={() => handleRegenerate(parentCheckpoint)}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function AssistantMessageLoading() {
  return (
    <div className="mr-auto flex items-start gap-2">
      <div className="bg-muted flex h-8 items-center gap-1 rounded-2xl px-4 py-2">
        <div className="bg-foreground/50 h-1.5 w-1.5 animate-[pulse_1.5s_ease-in-out_infinite] rounded-full"></div>
        <div className="bg-foreground/50 h-1.5 w-1.5 animate-[pulse_1.5s_ease-in-out_0.5s_infinite] rounded-full"></div>
        <div className="bg-foreground/50 h-1.5 w-1.5 animate-[pulse_1.5s_ease-in-out_1s_infinite] rounded-full"></div>
      </div>
    </div>
  );
}
