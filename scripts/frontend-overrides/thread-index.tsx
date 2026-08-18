import { v4 as uuidv4 } from "uuid";
import { ReactNode, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { useStreamContext } from "@/providers/Stream";
import { useState, FormEvent } from "react";
import { Button } from "../ui/button";
import { Checkpoint, Message } from "@langchain/langgraph-sdk";
import { AssistantMessage, AssistantMessageLoading } from "./messages/ai";
import { HumanMessage } from "./messages/human";
import {
  DO_NOT_RENDER_ID_PREFIX,
  ensureToolCallsHaveResponses,
} from "@/lib/ensure-tool-responses";
import { TooltipIconButton } from "./tooltip-icon-button";
import {
  ArrowDown,
  LoaderCircle,
  PanelRightOpen,
  PanelRightClose,
  SquarePen,
  XIcon,
  Plus,
} from "lucide-react";
import { useQueryState, parseAsBoolean } from "nuqs";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";
import ThreadHistory from "./history";
import {
  ArchiveProvider,
  ArchiveHeaderControls,
  useCurrentProjectId,
} from "./ProjectUploadPanel";
import { GytStatusCards } from "./GytStatusCards";
import { toast } from "sonner";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { useFileUpload } from "@/hooks/use-file-upload";
import { CheckinEntry } from "./checkin";
import { SupervisionEntry } from "./supervision-entry";
import { ContentBlocksPreview } from "./ContentBlocksPreview";
import {
  useArtifactOpen,
  ArtifactContent,
  ArtifactTitle,
  useArtifactContext,
} from "./artifact";

function StickyToBottomContent(props: {
  content: ReactNode;
  footer?: ReactNode;
  className?: string;
  contentClassName?: string;
}) {
  const context = useStickToBottomContext();
  return (
    <div
      ref={context.scrollRef}
      style={{ width: "100%", height: "100%" }}
      className={props.className}
    >
      <div
        ref={context.contentRef}
        className={props.contentClassName}
      >
        {props.content}
      </div>

      {props.footer}
    </div>
  );
}

function ScrollToBottom(props: { className?: string }) {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();

  if (isAtBottom) return null;
  return (
    <Button
      variant="outline"
      className={props.className}
      onClick={() => scrollToBottom()}
    >
      <ArrowDown className="h-4 w-4" />
      <span>Scroll to bottom</span>
    </Button>
  );
}

/**
 * 工友通品牌标识:绿色圆角「工」+ 名称(方案 B「清爽卡片」顶栏)。
 *
 * ⚠️ 手机上这里栽过一次(2026-08-15 用无头浏览器在 390×844 iPhone 视口实测):
 * 顶栏是一条 flex 行,而这颗按钮当时既没有 `shrink-0` 也没有 `whitespace-nowrap`,
 * 右边的「当前工地 / 资料库 / 资料归档」一挤,「工友通」就被压到 **34px 宽 × 84px 高**
 * —— 三个字竖排成一列。注意页面**没有**横向滚动(scrollWidth == innerWidth),
 * 所以不是溢出,是 flex 挤压:查的人若去找 overflow 会找不到东西。
 * 两件缺一不可:`shrink-0` 让它不被压,`whitespace-nowrap` 让它就算被压也不折行。
 */
function GytBrand({
  onClick,
  hideTextOnMobile = false,
}: {
  onClick?: () => void;
  /** 窄屏只留绿色「工」徽标、藏掉「工友通」三个字。
   *  进入对话后的顶栏比首页多一颗「新对话」按钮,390px 下放不下整个字号 ——
   *  藏掉不丢任何功能:徽标本身 onClick 就是「回到新对话」。 */
  hideTextOnMobile?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex shrink-0 cursor-pointer items-center gap-2 sm:gap-2.5"
    >
      {/* 窄屏把徽标与字号各降一档,给右侧那组顶栏入口腾出约 20px;≥640px 原样恢复。 */}
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[11px] bg-[#0E9F6E] text-base font-black text-white sm:h-9 sm:w-9 sm:text-lg">
        工
      </span>
      {/* 360px 以下(iPhone SE 一代那种老屏)连字都放不下:顶栏可用 298px,
          而侧栏开关 40 + 品牌 93 + 当前工地 123 + 资料库 40 + 资料归档 38 = 358,
          实测「📂 资料归档」被顶到 right=374、整个掉出屏幕(而且没有横向滚动条,
          屏幕上一点线索都没有)。这一档只留绿色「工」徽标 —— 它本来就是 logo,
          底下那句大标题「有事就问工友通」也还在,认得出是谁家的产品。 */}
      <span
        className={cn(
          "text-lg font-black tracking-tight whitespace-nowrap text-[#1B2420] sm:text-xl",
          hideTextOnMobile ? "hidden sm:inline" : "max-[359px]:hidden",
        )}
      >
        工友通
      </span>
    </button>
  );
}

/** 外壳:把整棵 Thread 包进 <ArchiveProvider>,让内部能读到「当前工地」并注入提交配置。 */
export function Thread() {
  return (
    <ArchiveProvider>
      <ThreadInner />
    </ArchiveProvider>
  );
}

function ThreadInner() {
  const currentProjectId = useCurrentProjectId();
  const [artifactContext, setArtifactContext] = useArtifactContext();
  const [artifactOpen, closeArtifact] = useArtifactOpen();

  const [threadId, _setThreadId] = useQueryState("threadId");
  const [chatHistoryOpen, setChatHistoryOpen] = useQueryState(
    "chatHistoryOpen",
    parseAsBoolean.withDefault(false),
  );
  const [hideToolCalls, setHideToolCalls] = useQueryState(
    "hideToolCalls",
    parseAsBoolean.withDefault(false),
  );
  const [input, setInput] = useState("");
  const {
    contentBlocks,
    setContentBlocks,
    handleFileUpload,
    dropRef,
    removeBlock,
    resetBlocks: _resetBlocks,
    dragOver,
    handlePaste,
  } = useFileUpload();
  const [firstTokenReceived, setFirstTokenReceived] = useState(false);
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");

  const stream = useStreamContext();
  const messages = stream.messages;
  const isLoading = stream.isLoading;

  const lastError = useRef<string | undefined>(undefined);

  const setThreadId = (id: string | null) => {
    _setThreadId(id);

    // close artifact and reset artifact context
    closeArtifact();
    setArtifactContext({});
  };

  useEffect(() => {
    if (!stream.error) {
      lastError.current = undefined;
      return;
    }
    try {
      const message = (stream.error as any).message;
      if (!message || lastError.current === message) {
        // Message has already been logged. do not modify ref, return early.
        return;
      }

      // Message is defined, and it has not been logged yet. Save it, and send the error
      lastError.current = message;
      toast.error("An error occurred. Please try again.", {
        description: (
          <p>
            <strong>Error:</strong> <code>{message}</code>
          </p>
        ),
        richColors: true,
        closeButton: true,
      });
    } catch {
      // no-op
    }
  }, [stream.error]);

  // TODO: this should be part of the useStream hook
  const prevMessageLength = useRef(0);
  useEffect(() => {
    if (
      messages.length !== prevMessageLength.current &&
      messages?.length &&
      messages[messages.length - 1].type === "ai"
    ) {
      setFirstTokenReceived(true);
    }

    prevMessageLength.current = messages.length;
  }, [messages]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if ((input.trim().length === 0 && contentBlocks.length === 0) || isLoading)
      return;
    setFirstTokenReceived(false);

    const newHumanMessage: Message = {
      id: uuidv4(),
      type: "human",
      content: [
        ...(input.trim().length > 0 ? [{ type: "text", text: input }] : []),
        ...contentBlocks,
      ] as Message["content"],
    };

    const toolMessages = ensureToolCallsHaveResponses(stream.messages);

    const context =
      Object.keys(artifactContext).length > 0 ? artifactContext : undefined;

    stream.submit(
      { messages: [...toolMessages, newHumanMessage], context },
      {
        streamMode: ["values"],
        streamSubgraphs: true,
        streamResumable: true,
        // W7 §3:把界面选中的「当前工地」经 config.configurable 带给后端,
        // 让规范问答自动限定到「全局 + 该项目」——没选项目就不带,后端只查全局。
        config: currentProjectId
          ? { configurable: { gyt_project_id: currentProjectId } }
          : undefined,
        optimisticValues: (prev) => ({
          ...prev,
          context,
          messages: [
            ...(prev.messages ?? []),
            ...toolMessages,
            newHumanMessage,
          ],
        }),
      },
    );

    setInput("");
    setContentBlocks([]);
  };

  const handleRegenerate = (
    parentCheckpoint: Checkpoint | null | undefined,
  ) => {
    // Do this so the loading state is correct
    prevMessageLength.current = prevMessageLength.current - 1;
    setFirstTokenReceived(false);
    stream.submit(undefined, {
      checkpoint: parentCheckpoint,
      streamMode: ["values"],
      streamSubgraphs: true,
      streamResumable: true,
      config: currentProjectId
        ? { configurable: { gyt_project_id: currentProjectId } }
        : undefined,
    });
  };

  const chatStarted = !!threadId || !!messages.length;
  const hasNoAIOrToolMessages = !messages.find(
    (m) => m.type === "ai" || m.type === "tool",
  );

  return (
    <div className="flex h-screen w-full overflow-hidden bg-[#EEF1F0]">
      {/* W7 CAD/knowledge:归档入口已上移到顶栏(<ArchiveHeaderControls />);抽屉由 <ArchiveProvider> 自挂 */}
      <div className="relative hidden lg:flex">
        <motion.div
          className="absolute z-20 h-full overflow-hidden border-r bg-white"
          style={{ width: 300 }}
          animate={
            isLargeScreen
              ? { x: chatHistoryOpen ? 0 : -300 }
              : { x: chatHistoryOpen ? 0 : -300 }
          }
          initial={{ x: -300 }}
          transition={
            isLargeScreen
              ? { type: "spring", stiffness: 300, damping: 30 }
              : { duration: 0 }
          }
        >
          <div
            className="relative h-full"
            style={{ width: 300 }}
          >
            <ThreadHistory />
          </div>
        </motion.div>
      </div>

      <div
        className={cn(
          "grid w-full grid-cols-[1fr_0fr] transition-all duration-500",
          artifactOpen && "grid-cols-[3fr_2fr]",
        )}
      >
        <motion.div
          className={cn(
            "relative flex min-w-0 flex-1 flex-col overflow-hidden",
            !chatStarted && "grid-rows-[1fr]",
          )}
          layout={isLargeScreen}
          animate={{
            marginLeft: chatHistoryOpen ? (isLargeScreen ? 300 : 0) : 0,
            width: chatHistoryOpen
              ? isLargeScreen
                ? "calc(100% - 300px)"
                : "100%"
              : "100%",
          }}
          transition={
            isLargeScreen
              ? { type: "spring", stiffness: 300, damping: 30 }
              : { duration: 0 }
          }
        >
          {!chatStarted && (
            <div className="absolute top-0 left-0 z-10 flex w-full items-center gap-1.5 border-b border-[#EAEDEB] bg-white/80 p-2.5 pl-3 backdrop-blur sm:gap-2">
              {(!chatHistoryOpen || !isLargeScreen) && (
                <Button
                  // 窄屏把左右内边距从 px-4 收到 px-2:这一颗省下的 16px,
                  // 正好是「当前工地」按钮在 390px 下还能显出项目名的余量。
                  className="shrink-0 px-2 hover:bg-black/5 sm:px-4"
                  variant="ghost"
                  onClick={() => setChatHistoryOpen((p) => !p)}
                >
                  {chatHistoryOpen ? (
                    <PanelRightOpen className="size-5" />
                  ) : (
                    <PanelRightClose className="size-5" />
                  )}
                </Button>
              )}
              <GytBrand onClick={() => setThreadId(null)} />
              {/* shrink-0 与 ArchiveHeaderControls 内部一致(理由见那边的注释):
                  这一组里能让步的只有「当前工地」的项目名,它自己 truncate,
                  不需要、也不能靠压缩容器来腾地方。 */}
              <div className="ml-auto shrink-0">
                <ArchiveHeaderControls />
              </div>
            </div>
          )}
          {chatStarted && (
            // 进入对话后的顶栏:比首页多一颗「新对话」,窄屏预算更紧。
            // max-[359px]:flex-wrap —— 360px 以下允许折成两行(这条顶栏在正常流里,
            // 不像首页那条是 absolute 覆盖、折行会顶穿下面靠 pt-16 留的白)。
            <div className="relative z-10 flex items-center justify-between gap-1.5 p-2 max-[359px]:flex-wrap sm:gap-3">
              <div className="relative flex shrink-0 items-center justify-start gap-2">
                <div className="absolute left-0 z-10">
                  {(!chatHistoryOpen || !isLargeScreen) && (
                    <Button
                      // 与首页顶栏同样的窄屏收窄(见上方那颗的注释)。
                      className="px-2 hover:bg-gray-100 sm:px-4"
                      variant="ghost"
                      onClick={() => setChatHistoryOpen((p) => !p)}
                    >
                      {chatHistoryOpen ? (
                        <PanelRightOpen className="size-5" />
                      ) : (
                        <PanelRightClose className="size-5" />
                      )}
                    </Button>
                  )}
                </div>
                <motion.div
                  animate={{
                    marginLeft: !chatHistoryOpen ? 48 : 0,
                  }}
                  transition={{
                    type: "spring",
                    stiffness: 300,
                    damping: 30,
                  }}
                >
                  <GytBrand
                    hideTextOnMobile
                    onClick={() => setThreadId(null)}
                  />
                </motion.div>
              </div>

              {/* shrink-0 同首页顶栏(理由见 ArchiveHeaderControls 里的注释)。 */}
              <div className="flex shrink-0 items-center gap-1.5 sm:gap-3">
                <ArchiveHeaderControls />
                <TooltipIconButton
                  size="lg"
                  className="shrink-0 p-2 sm:p-4"
                  tooltip="新對話"
                  variant="ghost"
                  onClick={() => setThreadId(null)}
                >
                  <SquarePen className="size-5" />
                </TooltipIconButton>
              </div>

              <div className="from-background to-background/0 absolute inset-x-0 top-full h-5 bg-gradient-to-b" />
            </div>
          )}

          {/* W7 首页重设计:4 张能力状态卡片(常驻,派活时对应卡片发亮)。
              首页顶栏是 absolute 覆盖,给卡片留出顶栏高度避免被挡;进入对话后顶栏在流内,无需留白。 */}
          <div className={cn(!chatStarted && "pt-16")}>
            <GytStatusCards />
          </div>

          <StickToBottom className="relative flex-1 overflow-hidden">
            <StickyToBottomContent
              className={cn(
                "absolute inset-0 overflow-y-scroll px-4 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent",
                // mt-[25vh] 是首页把大标题往下压到视觉中心用的,桌面上好看,
                // 手机上是纯亏损:390×844 实测,顶栏 + 2×2 卡片已吃掉 ~330px,
                // 再扣 25vh(211px)后滚动视口只剩 303px,而底部那坨(大标题 + 输入框 +
                // 动作条)要 312px —— sticky bottom-0 塞不下,动作条被顶到 y=873,
                // **整条动作条连同「打卡」按钮落在 844 的屏幕之外**,不滚动根本看不见。
                // 窄屏收到 mt-4,≥640px 原样恢复 25vh。
                !chatStarted && "mt-4 flex flex-col items-stretch sm:mt-[25vh]",
                chatStarted && "grid grid-rows-[1fr_auto]",
              )}
              contentClassName="pt-8 pb-16 max-w-3xl mx-auto flex flex-col gap-4 w-full"
              content={
                <>
                  {messages
                    .filter((m) => !m.id?.startsWith(DO_NOT_RENDER_ID_PREFIX))
                    .map((message, index) =>
                      message.type === "human" ? (
                        <HumanMessage
                          key={message.id || `${message.type}-${index}`}
                          message={message}
                          isLoading={isLoading}
                        />
                      ) : (
                        <AssistantMessage
                          key={message.id || `${message.type}-${index}`}
                          message={message}
                          isLoading={isLoading}
                          handleRegenerate={handleRegenerate}
                        />
                      ),
                    )}
                  {/* Special rendering case where there are no AI/tool messages, but there is an interrupt.
                    We need to render it outside of the messages list, since there are no messages to render */}
                  {hasNoAIOrToolMessages && !!stream.interrupt && (
                    <AssistantMessage
                      key="interrupt-msg"
                      message={undefined}
                      isLoading={isLoading}
                      handleRegenerate={handleRegenerate}
                    />
                  )}
                  {isLoading && !firstTokenReceived && (
                    <AssistantMessageLoading />
                  )}
                </>
              }
              footer={
                // 窄屏把纵向留白与标题字号各收一档 —— 这一坨是 sticky bottom-0,
                // 它有多高,动作条就被顶多高;省下的每一像素都是「打卡按钮在不在屏幕里」。
                // ⚠️ 这里只能用 `//`:footer={…} 是 JSX 属性表达式,里头塞 {/* */} 会被
                // 解析成第二个表达式,直接 Syntax Error(2026-08-15 踩过)。
                <div className="sticky bottom-0 flex flex-col items-center gap-4 bg-[#EEF1F0] sm:gap-8">
                  {!chatStarted && (
                    <div className="flex flex-col items-center text-center">
                      <h1 className="text-[30px] leading-tight font-black tracking-tight text-[#1B2420] sm:text-[44px]">
                        有事就問工友通
                      </h1>
                      <p className="mt-2 text-[15px] text-[#6B7772] sm:mt-3 sm:text-[18px]">
                        説一句話、拍張照,或者傳個文件,我來幫你派活
                      </p>
                    </div>
                  )}

                  <ScrollToBottom className="animate-in fade-in-0 zoom-in-95 absolute bottom-full left-1/2 mb-4 -translate-x-1/2" />

                  <div
                    ref={dropRef}
                    className={cn(
                      "relative z-10 mx-auto mb-4 w-full max-w-3xl overflow-hidden rounded-[22px] bg-white shadow-[0_12px_40px_rgba(27,36,32,0.10)] transition-all sm:mb-8",
                      dragOver
                        ? "border-2 border-dotted border-[#0E9F6E]"
                        : "border border-solid border-[#E4E8E6]",
                    )}
                  >
                    <form
                      onSubmit={handleSubmit}
                      className="mx-auto grid max-w-3xl grid-rows-[1fr_auto] gap-2"
                    >
                      <ContentBlocksPreview
                        blocks={contentBlocks}
                        onRemove={removeBlock}
                      />
                      <textarea
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onPaste={handlePaste}
                        onKeyDown={(e) => {
                          if (
                            e.key === "Enter" &&
                            !e.shiftKey &&
                            !e.metaKey &&
                            !e.nativeEvent.isComposing
                          ) {
                            e.preventDefault();
                            const el = e.target as HTMLElement | undefined;
                            const form = el?.closest("form");
                            form?.requestSubmit();
                          }
                        }}
                        placeholder="對着我説話、拍張照,或問一句…"
                        className="field-sizing-content resize-none border-none bg-transparent p-5 pb-2 text-[17px] text-[#33403A] shadow-none ring-0 outline-none placeholder:text-[#A2ABA6] focus:ring-0 focus:outline-none"
                      />

                      {/* 底部动作条 —— 手机上这里是**打卡功能的唯一入口**,挤爆等于打卡不可用。
                          2026-08-15 在 390×844 实测的病状:gap-6(24px×3=72px)+ 四项内容
                          intrinsic 宽约 400px,远超可用的 358px(390 − px-4),于是每一项都被
                          flex 压到文字竖排 —— 「打卡」按钮只剩 **42px 宽**,手指几乎点不中。

                          解法是**换行而不是隐藏**(窄屏也一件功能都不少):
                          · flex-wrap + order-last —— 只把「隐藏中间步骤」这个纯观感开关挤到第二行,
                            第一行留给 上传 / 打卡 / 发送 三个真动作;
                          · ≥640px 用 sm:flex-nowrap + sm:gap-6 原样退回今天的单行布局,
                            所以 768 / 1280 一个像素都不动。 */}
                      <div className="flex flex-wrap items-center gap-2 border-t border-[#EEF1F0] bg-[#FAFBFB] p-3 px-4 sm:flex-nowrap sm:gap-6">
                        <div className="order-last shrink-0 sm:order-none">
                          <div className="flex items-center space-x-2">
                            <Switch
                              id="render-tool-calls"
                              checked={hideToolCalls ?? false}
                              onCheckedChange={setHideToolCalls}
                            />
                            <Label
                              htmlFor="render-tool-calls"
                              className="text-sm whitespace-nowrap text-[#6B7772]"
                            >
                              隱藏中間步驟
                            </Label>
                          </div>
                        </div>
                        {/* min-h-11 = 44px,触摸目标的通用下限;≥640px 退回原来的高度(sm:min-h-0)。 */}
                        <Label
                          htmlFor="file-input"
                          className="flex min-h-11 shrink-0 cursor-pointer items-center gap-1.5 rounded-[12px] border border-[#E4E8E6] bg-white px-3 py-2 text-sm font-bold whitespace-nowrap text-[#33403A] transition hover:border-[#7FCDAE] sm:min-h-0"
                        >
                          <Plus className="size-4 text-[#0E9F6E]" />
                          <span>上傳圖紙·資料</span>
                        </Label>
                        <input
                          id="file-input"
                          type="file"
                          onChange={handleFileUpload}
                          multiple
                          accept="image/jpeg,image/png,image/gif,image/webp,application/pdf,.dxf,image/vnd.dxf"
                          className="hidden"
                        />
                        {/* 打卡入口(W7 · D15):点开是直连 POST /checkin 的自拍面板,
                            不进对话、不产生消息 —— 所以放在动作条而不是消息区
                            (tool-calls.tsx 只消费 ToolMessage,直连打卡根本不产生它,
                            W7 §1.5)。组件自带 type="button",不会误触本 form 的提交。 */}
                        <CheckinEntry />
                        {/* 监理处置入口(W10):同样是**直连 HTTP 的操作台**,不进对话、
                            不产生消息 —— 所以和打卡并排放在动作条,不放消息区。
                            W9 当初就是放错了地方:它挂在 tool-calls.tsx 那张「隐患台账」卡上,
                            而那张卡的判据是「子 Agent 的工具返回里有 hazards 数组」,
                            supervisor 的 output_mode="last_message" 把那份返回整个丢掉了 ——
                            结果是面板一次都没打开过,而且不报错(方案 docs/W10_界面取不到工具返回_方案.md)。
                            按钮自带 type="button" 与待确认计数徽章,不会误触本 form 的提交。 */}
                        <SupervisionEntry />
                        {stream.isLoading ? (
                          <Button
                            key="stop"
                            onClick={() => stream.stop()}
                            className="ml-auto min-h-11 shrink-0 rounded-[14px] whitespace-nowrap sm:min-h-0"
                          >
                            <LoaderCircle className="h-4 w-4 animate-spin" />
                            停止
                          </Button>
                        ) : (
                          <Button
                            type="submit"
                            className="ml-auto min-h-11 shrink-0 rounded-[14px] bg-[#0E9F6E] px-7 text-[16px] font-black whitespace-nowrap text-white shadow-md transition-all hover:bg-[#0b7f58] sm:min-h-0"
                            disabled={
                              isLoading ||
                              (!input.trim() && contentBlocks.length === 0)
                            }
                          >
                            發送
                          </Button>
                        )}
                      </div>
                    </form>
                  </div>
                </div>
              }
            />
          </StickToBottom>
        </motion.div>
        <div className="relative flex flex-col border-l">
          <div className="absolute inset-0 flex min-w-[30vw] flex-col">
            <div className="grid grid-cols-[1fr_auto] border-b p-4">
              <ArtifactTitle className="truncate overflow-hidden" />
              <button
                onClick={closeArtifact}
                className="cursor-pointer"
              >
                <XIcon className="size-5" />
              </button>
            </div>
            <ArtifactContent className="relative flex-grow" />
          </div>
        </div>
      </div>
    </div>
  );
}
