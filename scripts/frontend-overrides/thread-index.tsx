import { v4 as uuidv4 } from "uuid";
import { ReactNode, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { useStreamContext } from "@/providers/Stream";
import { useState, FormEvent } from "react";
import { Button } from "../ui/button";
import { Checkpoint, Message } from "@langchain/langgraph-sdk";
import { AssistantMessage, AssistantMessageLoading } from "./messages/ai";
import { PreviewCard } from "./messages/tool-calls";
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
  Camera,
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
import { GytTimingRows } from "./GytTimingRows";
import {
  captureCadPreview,
  type CapturedCadPreview,
} from "@/lib/cad-preview-lib";
import { toast } from "sonner";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { useFileUpload } from "@/hooks/use-file-upload";
// 发送前把附件块换成编号块 —— 附件在取件时就直传过了,这里只搬编号(见 handleSubmit)。
import { toWireBlocks } from "@/lib/multimodal-utils";
import { CheckinEntry } from "./checkin";
import { SupervisionEntry } from "./supervision-entry";
import { ReportsEntry } from "./reports-entry";
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
      <div ref={context.contentRef} className={props.contentClassName}>
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
      {/* 上游原文 "Scroll to bottom" —— 恒繁體界面里的一颗英文按钮。
          英文残留那套繁體守卫抓不到(判据 s2hk(v)!==v 对英文恒等),只能人扫。 */}
      <span>回到最新</span>
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
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[12px] bg-[var(--gyt-green)] text-base font-black text-white sm:h-9 sm:w-9 sm:text-lg">
        工
      </span>
      {/* 360px 以下(iPhone SE 一代那种老屏)连字都放不下:顶栏可用 298px,
          而侧栏开关 40 + 品牌 93 + 当前工地 123 + 资料库 40 + 资料归档 38 = 358,
          实测「📂 资料归档」被顶到 right=374、整个掉出屏幕(而且没有横向滚动条,
          屏幕上一点线索都没有)。这一档只留绿色「工」徽标 —— 它本来就是 logo,
          底下那句大标题「有事就问工友通」也还在,认得出是谁家的产品。 */}
      <span
        className={cn(
          "text-lg font-black tracking-tight whitespace-nowrap text-[var(--gyt-ink)] sm:text-xl",
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
  /** 输入框本体 —— 能力卡填例句之后要把光标送进去(2026-08-25 设计审计 D2)。 */
  const composerRef = useRef<HTMLTextAreaElement>(null);
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
  // CAD 子图以 last_message 交回:内部 ToolMessage 在流式期间可见,收工后会从根状态消失。
  // 以本线程第一条用户消息为键立刻缓存成功预览,保证图片不会在最终答复出现时闪退。
  const previewConversationKey =
    messages.find((message) => message.type === "human")?.id ??
    "new-conversation";
  const [capturedCadPreviews, setCapturedCadPreviews] = useState<
    Record<string, CapturedCadPreview[]>
  >({});
  useEffect(() => {
    const incoming = messages
      .map(captureCadPreview)
      .filter((item) => item !== null);
    if (incoming.length === 0) return;
    setCapturedCadPreviews((previous) => {
      const current = previous[previewConversationKey] ?? [];
      const known = new Set(current.map((item) => item.messageId));
      const additions = incoming.filter((item) => !known.has(item.messageId));
      if (additions.length === 0) return previous;
      return {
        ...previous,
        [previewConversationKey]: [...current, ...additions].slice(-5),
      };
    });
  }, [messages, previewConversationKey]);
  const visibleCadPreviews = capturedCadPreviews[previewConversationKey] ?? [];

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
      // 🔴 这是主聊天界面**唯一**的出错提示 —— 上游原文是英文,而界面已经整体恒繁體,
      // 于是港方工友会在一整屏繁體里吃到一句 "An error occurred. Please try again."。
      // (W12 那套繁體守卫的判据是 `s2hk(v) !== v`,**英文恒等**,所以它一条都不覆盖。)
      //
      // ⚠️ `{message}` 是上游框架原样透出来的技术文本(常是英文,可能带类名/路径),
      //    这里**刻意不翻也不删**:它是屏幕上唯一能报给管理员的线索。
      //    人话那一半由标题承担 —— 工友看标题就知道该干什么,底下那行给管理员看。
      toast.error("出錯了,請再試一次", {
        description: (
          <p>
            <strong>出錯原因:</strong> <code>{message}</code>
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

    const textParts =
      input.trim().length > 0 ? [{ type: "text", text: input }] : [];

    // 🔴 **界面上那份和送出去那份,内容不一样,这是刻意的。**
    //
    //   界面(下面的 optimisticValues)—— 用带 base64 的**原块**:工友点完发送要
    //     立刻看见自己那张照片,而编号块渲染不出图。
    //   送出去(newHumanMessage)—— 用 `toWireBlocks` 换成的**编号块**:附件在取件时
    //     就已经直传过了(multimodal-utils.ts),这里只搬编号,**字节一个都不上网**。
    //
    // 为什么非分开不可:base64 一旦随消息发上去,它**第 8 步就进检查点**了,而后端
    // 那道改写在第 9 步 —— 追不回来,每张照片永久留一份拷贝。线上量到 11 条会话
    // 就把 langgraph 内存库顶到 1.1 GB(机器一共 1966 MB),一条零载荷的 404 要 12.7 秒。
    // 完整推演在 backend/src/gyt/upload_api.py 的模块头注。
    //
    // ⚠️ 直传失败时 `toWireBlocks` **原样透传**老的 base64 块,后端那条老路一行没删 ——
    //    所以这条优化只会更好、不会更坏,接口挂了顶多是慢病复发。
    const newHumanMessage: Message = {
      id: uuidv4(),
      type: "human",
      content: [
        ...textParts,
        ...toWireBlocks(contentBlocks),
      ] as Message["content"],
    };
    const previewHumanMessage: Message = {
      ...newHumanMessage,
      content: [...textParts, ...contentBlocks] as Message["content"],
    };

    const toolMessages = ensureToolCallsHaveResponses(stream.messages);

    const context =
      Object.keys(artifactContext).length > 0 ? artifactContext : undefined;

    stream.submit(
      { messages: [...toolMessages, newHumanMessage], context },
      {
        streamMode: ["values"],
        // 🔴 **`streamSubgraphs` 必须是 false(或干脆不写),别再改回 true。**
        //
        // 它原本是上游 agent-chat-ui 的默认值,2026-08-09 那次把整份 thread-index
        // 拷进来当覆盖件时原样带进来的 —— 不是我们要的。2026-08-21 查「聊天框
        // 会把说的话突然收回去」时查到它头上:
        //
        //   · 开着它,子图(safety / schedule / inspection 那些独立编译的 Agent)
        //     的 `values` 事件也会推上来;
        //   · 而 SDK 对**每个** values 事件是**整份替换**主状态,不是合并
        //     (`@langchain/langgraph-sdk` 的 `dist/ui/manager.js:447` 那句裸
        //     `return data`);
        //   · 子图先推一份长的(它自己的内部消息),父节点跑完再推一份短的
        //     (supervisor 的 `output_mode="last_message"` 只回灌最后一条);
        //   · 于是**子 Agent 说的话先出现、再消失**。零报错。
        //
        // 探针实证(不调模型):子图推到 messages=3,父图回来 messages=2,
        // 安全档那句整条没了。这正是 Claude Code 那套子 Agent 的做法所避免的 ——
        // 子 Agent 的内部过程从来不进主对话,所以没有可收回的东西。
        //
        // ⚠️ 关掉它的代价:子图里发的 **custom 事件**也一起收不到了。耗时行原本
        //    靠那条路,所以它已经改走直连接口(`GET /timing`,见 GytTimingRows.tsx)
        //    —— **两件是同一个开关的两头,别只改一头。**
        streamSubgraphs: false,
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
            // 界面上用带图那份(理由见上面 newHumanMessage 那段)。两份 id 相同,
            // 真状态回来时会顶掉它 —— 那时后端已经把编号改写成 `(照片编号:…)`,
            // human.tsx 再把它渲染回真图,与改动前的观感一模一样。
            previewHumanMessage,
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
      // 与 handleSubmit 那处同一条,理由写在那儿(子图 values 会整份替换主状态,
      // 表现是子 Agent 说的话先出现再消失)。🔴 **三处提交路径必须一致** ——
      // 发送 / 重新生成 / 编辑后重发(human.tsx),漏一处的表现是「平时好好的,
      // 一按重新生成话就被收回去」,而且没人会想到是这两个值不一样。
      streamSubgraphs: false,
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
    <div className="flex h-screen w-full overflow-hidden bg-[linear-gradient(180deg,#F4F6F5_0%,#FBFCFB_62%,#FFFFFF_100%)]">
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
          <div className="relative h-full" style={{ width: 300 }}>
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
            {/* onPick(2026-08-25 设计审计 D2):点带例句的卡 → 把例句**填进输入框**
                并聚焦,不自动发送。卡片本身仍然是状态灯不是按钮,完整推演在
                GytStatusCards.tsx 那个 onClick 上方。
                聚焦要 requestAnimationFrame 兜一下:setInput 触发的重渲染这一帧还没提交,
                同步 focus 会落在旧节点上,表现是「字填进去了但光标不在里面」。 */}
            <GytStatusCards
              onPick={(text) => {
                setInput(text);
                requestAnimationFrame(() => {
                  const el = composerRef.current;
                  if (!el) return;
                  el.focus();
                  // 光标落到末尾,方便直接接着改(默认会全选或落在开头)
                  el.setSelectionRange(text.length, text.length);
                });
              }}
            />
          </div>

          <StickToBottom className="relative flex-1 overflow-hidden">
            <StickyToBottomContent
              className={cn(
                "absolute inset-0 overflow-y-scroll px-4 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent",
                // mt-[25vh] 是首页把大标题往下压到视觉中心用的,桌面上好看,
                // 屏幕一矮就是纯亏损:390×844 实测,顶栏 + 2×2 卡片已吃掉 ~330px,
                // 再扣 25vh(211px)后滚动视口只剩 303px,而底部那坨(大标题 + 输入框 +
                // 动作条)要 312px —— sticky bottom-0 塞不下,动作条被顶到 y=873,
                // **整条动作条连同「打卡」按钮落在 844 的屏幕之外**,不滚动根本看不见。
                //
                // 🔴 **第一版把判据写成了宽度(`sm:`),那是错的**(2026-08-20 复发实测):
                //    真正的约束是**高度**。1600×800 的窗口(宽得很,但矮)照样溢出 38px,
                //    而带一张附件的输入框还要再高一截 —— 实测发送键底边:
                //
                //        视口 1180 → 富余 247px      视口 900 → 富余 37px(已贴边)
                //        视口 1000 → 富余 112px      视口 800 → **溢出 38px**
                //
                //    笔记本不最大化、外接显示器开半屏,都落在这一档。宽度断点拦不住。
                //
                // 所以改成按高度**连续退让**,不设断点(断点必然选错,内容高度是变的):
                //
                //        max(1rem, min(25vh, 100vh - 700px))
                //          ↑ 再挤也留 1rem   ↑ 再宽松也不超过原来的 25vh
                //                            ↑ 永远给下面那坨留 700px
                //
                //    H=1180 → 295px(与改前一模一样,桌面观感不变)
                //    H=900  → 200px      H=800 → 100px      H≤716 → 16px
                //    没有悬崖:高度一点点变矮,留白就一点点收,不会某个像素突然跳。
                //    700 这个数 = 实测 mt-4 时发送键底边 654px + ~46px 余量(第二排附件)。
                //    ⚠️ 改这个数要回去重量一遍,别拍脑袋 —— 量法:附一张图,
                //       逐档设 viewport 高度,读发送键的 getBoundingClientRect().bottom。
                // 🔴 **25vh → 12vh(2026-08-25 设计审计 D3)。**
                //    上面那套「按高度连续退让」的机制没动,只把**上限**收了一档。
                //    病状:1440×900 实测,四张卡的底边在 y≈215,而大标题的顶在 y≈510 ——
                //    中间 **~300px 什么都没有**。眼睛从卡片掉进一个坑再爬上来找标题,
                //    首页读起来像两个互不相干的页面拼在一起。
                //
                //    ⚠️ **这个方向是安全的**:上面那段注释担心的是「动作条被顶出屏幕」,
                //       而收小 mt 是把上面那坨往上提,给下面**多**留空间,
                //       新值在任何高度上都 ≤ 旧值。所以那套 700px 的余量算法一个字没改,
                //       它守的边界只会更宽松,不会更紧。
                //
                //    按文件里那条「改这个数要回去重量一遍」的规矩实测(附一张图,
                //    逐档设视口高度,读发送键 getBoundingClientRect().bottom):
                //
                //        视口 1180 → mt 142(原 295)   视口 900 → mt 108(原 200)
                //        视口 1000 → mt 120(原 250)   视口 800 → mt  96(原 100)
                //        视口 ≤716 → mt  16(与原来一样,已经在最低档)
                !chatStarted &&
                  "mt-4 flex flex-col items-stretch sm:mt-[max(1rem,min(12vh,100vh-700px))]",
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
                  {visibleCadPreviews.map((preview) => (
                    <PreviewCard key={preview.messageId} data={preview.data} />
                  ))}
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
                  {/* 「每一步花了多久」(2026-08-20)。数据源是后端经 custom 流推的
                      `{ gyt_timing: {…} }`,在 providers/Stream.tsx 收下、存进
                      @/lib/timing-lib 那个 run 级 store —— **不是**从 messages 里
                      倒推的(耗时消息里根本没有,倒推不出来)。
                      🔴 受 hideToolCalls 控制,与工具调用痕迹同一档:它俩是同一类
                      东西(讲架构有用、给工地师傅看纯属干扰),开关只有一个,
                      漏了这个条件的表现是「关了中间步骤,底下还挂着一坨秒数」。
                      放在消息之后、加载点之前:一轮跑的过程中能看着它一行行长出来。 */}
                  {!hideToolCalls && <GytTimingRows />}
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
                <div className="sticky bottom-0 flex flex-col items-center gap-4 bg-[#FBFCFB] sm:gap-8">
                  {!chatStarted && (
                    <div className="flex flex-col items-center text-center">
                      <h1 className="text-[30px] leading-tight font-black tracking-tight text-[var(--gyt-ink)] sm:text-[44px]">
                        有事就問工友通
                      </h1>
                      <p className="mt-2 text-[15px] text-[var(--gyt-muted)] sm:mt-3 sm:text-[18px]">
                        説一句話、拍張照,或者傳個文件,我來幫你派活
                      </p>
                    </div>
                  )}

                  <ScrollToBottom className="animate-in fade-in-0 zoom-in-95 absolute bottom-full left-1/2 mb-4 -translate-x-1/2" />

                  <div
                    ref={dropRef}
                    className={cn(
                      "relative z-10 mx-auto mb-4 w-full max-w-3xl overflow-hidden rounded-[26px] bg-white shadow-[0_2px_4px_rgba(23,28,26,0.04),0_20px_50px_rgba(23,28,26,0.09)] transition-all sm:mb-8",
                      dragOver
                        ? "border-2 border-dotted border-[var(--gyt-green)]"
                        : "border border-solid border-[var(--gyt-line)]",
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
                        ref={composerRef}
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
                        className="field-sizing-content resize-none border-none bg-transparent p-5 pb-2 text-[17px] text-[var(--gyt-ink-soft)] shadow-none ring-0 outline-none placeholder:text-[var(--gyt-muted)] focus:ring-0 focus:outline-none"
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
                      <div className="flex flex-wrap items-center gap-2 border-t border-[#EDF0EE] bg-white p-3 px-4 sm:flex-nowrap sm:gap-6">
                        <div className="order-last shrink-0 sm:order-none">
                          {/* 开关本身只有 20px 高;整行给 44px 触控高度,Label 的 htmlFor 让整行都能点。 */}
                          <div className="flex items-center space-x-2 pointer-coarse:min-h-11">
                            <Switch
                              id="render-tool-calls"
                              checked={hideToolCalls ?? false}
                              onCheckedChange={setHideToolCalls}
                            />
                            <Label
                              htmlFor="render-tool-calls"
                              className="text-sm whitespace-nowrap text-[var(--gyt-muted)]"
                            >
                              隱藏中間步驟
                            </Label>
                          </div>
                        </div>
                        {/* 🔴 **拍照入口(2026-08-22)。整个产品的头号动作,而它在此之前
                            没有入口** —— 动作条上唯一的上传口写着「上傳圖紙·資料」
                            (工友要拍的是隐患照片,那句话在劝退),而且**没有 capture**:
                            手机上点它弹的是文件选择器,人得自己找到「相机」那一项。
                            对比很扎眼:打卡(checkin.tsx)与复查照片(supervision.tsx)
                            两处都有 capture,而且两处的注释都写着「不能省」。

                            ⚠️ **为什么是第二个 input,而不是给上面那个加 capture**:
                            `capture` 一加,手机浏览器会**直接开相机、不再给文件选择器**
                            (规范说它是"优先用采集设备"的提示,Android Chrome 是直接开)。
                            也就是说加在那个多用途 input 上 = 图纸(.dxf)和 PDF 资料
                            从手机上**再也传不了**。两个口各管一件事是唯一不互相伤害的做法。

                            两个 input 共用同一个 handleFileUpload —— 去重、格式判断、
                            压缩(image-compress.ts)全在那条链上,这里不许分叉。
                            accept 收窄到 image/jpeg:客户端压缩那条链只收 JPEG
                            (image-compress.ts 头注:canvas 只画得出第一帧,
                            动图经过它会变成单帧而服务端那道闸再也不触发)。
                            相机拍出来的本来就是 JPEG,所以这条收窄对用户零影响。 */}
                        <div className="flex shrink-0 items-center gap-2">
                        <Label
                          htmlFor="camera-input"
                          className="flex pointer-coarse:min-h-11 shrink-0 cursor-pointer items-center gap-1.5 rounded-full border border-[var(--gyt-green)] bg-[var(--gyt-green)] px-3 py-2 text-sm font-bold whitespace-nowrap text-white transition hover:bg-[var(--gyt-green-deep)]"
                        >
                          <Camera className="size-4" />
                          <span>拍照</span>
                        </Label>
                        <input
                          id="camera-input"
                          type="file"
                          onChange={handleFileUpload}
                          accept="image/jpeg"
                          capture="environment"
                          className="hidden"
                        />
                        {/* ── 分组线(2026-08-25 设计审计 D12)──────────────────────
                            这条动作条上六颗按钮**做的是两类完全不同的事**,而在此之前
                            它们并排等距、只靠三种样式(实心绿 / 描边 / 纯文字)区分,
                            而那三种样式编码的是「重要程度」,不是「这颗按钮会发生什么」:

                              左边两颗(拍照 / 上傳圖紙·資料)—— **往这条消息上附文件**,
                                                              附完还要按發送
                              右边三颗(打卡 / 隱患 / 記錄)  —— **打开一个独立操作台**,
                                                              不进对话、不产生消息,
                                                              跟你正在打的那句话毫无关系

                            后者是本仓「直连 HTTP」那条路的三个出口(见 CLAUDE.md 架构大图),
                            它们和發送之间**没有任何先后关系** —— 而并排等距恰恰暗示有。
                            一条竖线 + 把两类各自收进一个 flex 组,让间距自己说话:
                            组内 gap-2、组间 gap-6,鼠标扫过去就看得出是两拨东西。

                            ⚠️ 分组还顺手修了手机上的一个乱象:flex-wrap 原本按**单颗按钮**
                               换行,于是「發送」经常和「隱患」「記錄」落在同一行,
                               看着像它们是一组。现在换行以**组**为单位,不会再拆散。
                            ⚠️ 竖线 `hidden sm:block`:窄屏本来就要换行,那时候竖线会
                               卡在行尾变成一根没来由的短杠。窄屏靠分组换行表达,不靠线。 */}
                        {/* pointer-coarse:min-h-11 = 触屏设备上 44px 下限(2026-09-18 FINDING-006:原来按宽度判
                            `sm:min-h-0`,1024px 平板在监理面板拿得到 44px、在这儿拿不到;全站统一按设备判)。 */}
                        <Label
                          htmlFor="file-input"
                          className="flex pointer-coarse:min-h-11 shrink-0 cursor-pointer items-center gap-1.5 rounded-full border border-[var(--gyt-line)] bg-white px-3 py-2 text-sm font-bold whitespace-nowrap text-[var(--gyt-ink-soft)] transition hover:border-[var(--gyt-mint)]"
                        >
                          <Plus className="size-4 text-[var(--gyt-green-deep)]" />
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
                        </div>

                        <span
                          aria-hidden="true"
                          className="hidden h-6 w-px shrink-0 bg-[var(--gyt-line)] sm:block"
                        />

                        <div className="flex shrink-0 items-center gap-2">
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
                        {/* 巡检记录抽屉(2026-08-22):第三个同类的直连操作台。
                            🔴 它补的是「拍照 → 自动出 Word」这条链的**终点** ——
                            文档真的生成了、真的落盘了,而那份 Envelope 被
                            output_mode="last_message" 丢掉,于是 tool-calls.tsx 里
                            那张巡检记录卡一次都没渲染出来过;而 report/prompt.md
                            教模型说「跟管理员说编号就行」,**那个管理员不存在**(TODO-34)。
                            结果是:文件就在服务器上,而谁都拿不到。
                            按钮自带 type="button",刻意**没有徽章**(理由在组件头注:
                            存档不等人干活,红点只会让人去点掉一个不用处理的提醒)。 */}
                        <ReportsEntry />
                        </div>
                        {stream.isLoading ? (
                          <Button
                            key="stop"
                            onClick={() => stream.stop()}
                            className="ml-auto pointer-coarse:min-h-11 shrink-0 rounded-full whitespace-nowrap"
                          >
                            <LoaderCircle className="h-4 w-4 animate-spin" />
                            停止
                          </Button>
                        ) : (
                          <Button
                            type="submit"
                            className="ml-auto pointer-coarse:min-h-11 shrink-0 rounded-full bg-[var(--gyt-green)] px-7 text-[16px] font-black whitespace-nowrap text-white shadow-md transition-all hover:bg-[var(--gyt-green-deep)]"
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
              <button onClick={closeArtifact} className="cursor-pointer">
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
