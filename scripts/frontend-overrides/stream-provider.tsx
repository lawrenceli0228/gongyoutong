/**
 * 覆盖上游 `src/providers/Stream.tsx`。**唯一的改动是线程历史怎么取。**
 *
 * ⚠️ 这件覆盖件 2026-08-20 加过一次(接耗时的 custom 事件)、08-21 删过一次
 *    (耗时改走直连接口),现在因为**另一个**理由回来了。别把它当成那件事的复活 ——
 *    耗时行**不**经过这里,它自己轮询 `GET /timing`(见 GytTimingRows.tsx)。
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * 🔴 为什么非改不可:打开一条历史会话要等好几秒,而且是纯白板
 * ───────────────────────────────────────────────────────────────────────────
 * 上游这行写的是 `fetchStateHistory: true`。SDK 拿到 `true` 之后:
 *
 *     // @langchain/langgraph-sdk/dist/react/stream.lgp.js:26
 *     const limit = typeof options?.limit === "number" ? options.limit : 10
 *
 * `true` 不是 number,**落到 10** —— 每次打开会话都拉 **10 份完整状态快照**,
 * 每份都带整条线程的完整 messages。而**照片的 base64 会永久留在某个检查点里**
 * (`core/uploads.py` 把它换成「照片编号」是在 step 9,而那条带 base64 的消息
 * step 8 就已经落盘了,`RemoveMessage` 追不回来)。
 *
 * 解开本机真实检查点量出来的数(不是估算):
 *
 *     fetchStateHistory: true → history(limit=10)   7,501,333 B
 *     实际渲染只要的 head 状态                          8,248 B      ← 909 倍
 *
 * 可自证伪的预测:**传过照片的会话慢,纯文字的会话不慢。**
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * 改法:先出字,后补历史 —— 两阶段
 * ───────────────────────────────────────────────────────────────────────────
 *   阶段一  `threads.getState()`  一份 head(约 8 KB)→ 消息立刻上屏
 *   阶段二  `threads.getHistory()` 十份(那 7.5 MB)→ 后台补,只为分支功能
 *
 * 🔴 **阶段二不许省。** `ai.tsx` 与 `human.tsx` 的「编辑重发 / 重新生成」读的是
 *    `meta?.firstSeenState?.parent_checkpoint` —— 那是从 history 里逐条找出来的
 *    分叉点。只留一份快照时**每条消息的 `firstSeenState` 都会指向 head**,
 *    编辑一条老消息会从**错误的检查点**分叉,而且**不报任何错**。
 *    (所以也别把 `fetchStateHistory` 直接改成 `{limit: 1}` —— 那正是这个坑。)
 *
 * 走 `options.thread` 这个**公开选项**(`dist/ui/types.d.ts` 的 `UseStreamThread`):
 * 传了它,SDK 内建的取数就切成 passthrough(`stream.lgp.js:179-183`),历史完全由
 * 我们说了算,而 LGP 传输、分支树、`isThreadLoading` 那些一样都不丢。
 *
 * ⚠️ 这**不减少字节**,只是把它挪出关键路径:7.5 MB 照样要下,只是不再挡着首屏。
 *    真正的根治是别让 base64 进 state(TODOS.md 的 TODO-51),那是另一件事。
 */

import React, {
  createContext,
  useContext,
  ReactNode,
  useCallback,
  useState,
  useEffect,
  useMemo,
  useRef,
} from "react";
import { useStream } from "@langchain/langgraph-sdk/react";
import { type Message, type ThreadState } from "@langchain/langgraph-sdk";
import {
  uiMessageReducer,
  isUIMessage,
  isRemoveUIMessage,
  type UIMessage,
  type RemoveUIMessage,
} from "@langchain/langgraph-sdk/react-ui";
import { useQueryState } from "nuqs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { LangGraphLogoSVG } from "@/components/icons/langgraph";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ArrowRight } from "lucide-react";
import { PasswordInput } from "@/components/ui/password-input";
import { getApiKey } from "@/lib/api-key";
import { createClient } from "./client";
import { useThreads } from "./Thread";
import { toast } from "sonner";

export type StateType = { messages: Message[]; ui?: UIMessage[] };

const useTypedStream = useStream<
  StateType,
  {
    UpdateType: {
      messages?: Message[] | Message | string;
      ui?: (UIMessage | RemoveUIMessage)[] | UIMessage | RemoveUIMessage;
      context?: Record<string, unknown>;
    };
    CustomEventType: UIMessage | RemoveUIMessage;
  }
>;

type StreamContextType = ReturnType<typeof useTypedStream>;
const StreamContext = createContext<StreamContextType | undefined>(undefined);

async function sleep(ms = 4000) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function checkGraphStatus(
  apiUrl: string,
  apiKey: string | null,
  authScheme?: string,
): Promise<boolean> {
  try {
    const headers = new Headers();
    if (apiKey) headers.set("X-Api-Key", apiKey);
    if (authScheme) headers.set("X-Auth-Scheme", authScheme);

    const res = await fetch(`${apiUrl}/info`, {
      headers,
    });

    return res.ok;
  } catch (e) {
    console.error(e);
    return false;
  }
}

/** 后台补历史的延迟。首屏那几帧让给渲染,别和它抢主线程。 */
const HISTORY_BACKFILL_MS = 300;

/** 与 SDK 内建取数同一个数(`stream.lgp.js:26` 的那个 `: 10`)—— 分支树要靠它才完整。 */
const HISTORY_LIMIT = 10;

/**
 * 线程历史的两阶段取法。返回值形状就是 SDK 的 `UseStreamThread`。
 *
 * 阶段一 `getState()` 只取 head(约 8 KB),消息立刻上屏;
 * 阶段二 `getHistory()` 后台补齐,分支功能靠它(完整理由见文件头注)。
 *
 * 🔴 **`data[0]` 必须是 head。** SDK 把 `history.data` 交给 `getBranchContext`,
 *    取的是 `flatHistory.at(-1)` 当 `threadHead`,再从它的 `values` 里读消息
 *    (`stream.lgp.js:196` / `:238`)。`getHistory` 本来就是新的在前,而阶段一
 *    只有一份、顺序无从谈起 —— 两种情况都成立,但这条前提别在改动里弄丢:
 *    弄丢的表现是**消息一条都不显示**,而控制台干净。
 *
 * ⚠️ 阶段二的失败**不上屏**:那时消息已经在了,弹一个工友看不懂的红框只会吓人。
 *    代价是分支功能静默不可用 —— 取舍写在这儿,别当成漏了处理。
 */
function useLazyThreadHistory(
  apiUrl: string,
  apiKey: string | null,
  authScheme: string | undefined,
  threadId: string | null,
) {
  const [data, setData] = useState<ThreadState<StateType>[] | undefined>(undefined);
  const [error, setError] = useState<unknown>(undefined);
  const [isLoading, setIsLoading] = useState(false);

  const client = useMemo(
    () => createClient(apiUrl, apiKey ?? undefined, authScheme),
    [apiUrl, apiKey, authScheme],
  );

  /**
   * 取完整历史。SDK 在每轮 run 结束后会调 `mutate` 拿新的 head
   * (`stream.lgp.js:380`:`(await history.mutate(id))?.at(0)`)——
   * 所以这里**必须回整份数组**,而且 `at(0)` 得是新的那一份。
   */
  const fetchFull = useCallback(
    async (id: string): Promise<ThreadState<StateType>[] | undefined> => {
      try {
        const full = (await client.threads.getHistory(id, {
          limit: HISTORY_LIMIT,
        })) as ThreadState<StateType>[];
        setData(full);
        setError(undefined);
        return full;
      } catch (e) {
        // 只记不弹:见函数头注最后那条取舍。
        console.error("补线程历史失败(分支功能会不可用):", e);
        setError(e);
        return undefined;
      }
    },
    [client],
  );

  // 🔴 用 ref 存最新的 threadId,给 `mutate` 用。`mutate` 的参数是可选的
  //    (SDK 有时不传),不能只靠它;而把 threadId 放进 useCallback 的依赖里
  //    会让 mutate 的引用每次换会话都变 —— SDK 把它当依赖用,引用抖动会多跑几轮。
  const threadIdRef = useRef<string | null>(threadId);
  threadIdRef.current = threadId;

  useEffect(() => {
    if (!threadId) {
      // 新会话还没建线程:清干净,别让上一条的历史挂在这条底下。
      setData(undefined);
      setError(undefined);
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setData(undefined);
    setError(undefined);
    setIsLoading(true);

    void (async () => {
      // ── 阶段一:head ────────────────────────────────────────────────
      try {
        const head = (await client.threads.getState(threadId)) as ThreadState<StateType>;
        if (cancelled) return;
        setData([head]);
      } catch (e) {
        if (cancelled) return;
        // 阶段一失败 = 这条会话**一个字都出不来**,这个要如实记下来给上层看
        // (`isThreadLoading` 与 `error` 都是 SDK 暴露给界面的)。
        console.error("取线程 head 失败:", e);
        setError(e);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
      if (cancelled) return;
      // ── 阶段二:完整历史(后台)──────────────────────────────────────
      timer = setTimeout(() => {
        if (!cancelled) void fetchFull(threadId);
      }, HISTORY_BACKFILL_MS);
    })();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [client, threadId, fetchFull]);

  const mutate = useCallback(
    async (mutateId?: string) => {
      const id = mutateId ?? threadIdRef.current;
      if (!id) return undefined;
      return fetchFull(id);
    },
    [fetchFull],
  );

  return useMemo(
    () => ({ data, error, isLoading, mutate }),
    [data, error, isLoading, mutate],
  );
}

const StreamSession = ({
  children,
  apiKey,
  apiUrl,
  assistantId,
  authScheme,
}: {
  children: ReactNode;
  apiKey: string | null;
  apiUrl: string;
  assistantId: string;
  authScheme?: string;
}) => {
  const [threadId, setThreadId] = useQueryState("threadId");
  const { getThreads, setThreads } = useThreads();
  // 线程历史自己管:先出字、后补历史(完整推演见文件头注)。
  const threadHistory = useLazyThreadHistory(apiUrl, apiKey, authScheme, threadId ?? null);
  const streamValue = useTypedStream({
    apiUrl,
    apiKey: apiKey ?? undefined,
    assistantId,
    ...(authScheme && {
      defaultHeaders: {
        "X-Auth-Scheme": authScheme,
      },
    }),
    threadId: threadId ?? null,
    // 🔴 **不是 `fetchStateHistory: true`**(上游那行)。那个 `true` 会被 SDK
    //    读成 `limit = 10` —— 一次拉十份完整快照,本机实测 7,501,333 字节,
    //    而首屏真正要的 head 只有 8,248 字节。给了 `thread` 之后 SDK 内建取数
    //    切成 passthrough(`stream.lgp.js:179-183`),历史由上面那个 hook 两阶段供。
    //    ⚠️ 也**别改成 `{limit: 1}`** —— 那样每条消息的 `firstSeenState` 都指向 head,
    //       编辑老消息会从错的检查点分叉且不报错。理由整段在文件头注。
    thread: threadHistory,
    onCustomEvent: (event, options) => {
      if (isUIMessage(event) || isRemoveUIMessage(event)) {
        options.mutate((prev) => {
          const ui = uiMessageReducer(prev.ui ?? [], event);
          return { ...prev, ui };
        });
      }
    },
    onThreadId: (id) => {
      setThreadId(id);
      // Refetch threads list when thread ID changes.
      // Wait for some seconds before fetching so we're able to get the new thread that was created.
      sleep().then(() => getThreads().then(setThreads).catch(console.error));
    },
  });

  useEffect(() => {
    checkGraphStatus(apiUrl, apiKey, authScheme).then((ok) => {
      if (!ok) {
        toast.error("Failed to connect to LangGraph server", {
          description: () => (
            <p>
              Please ensure your graph is running at <code>{apiUrl}</code> and
              your API key is correctly set (if connecting to a deployed graph).
            </p>
          ),
          duration: 10000,
          richColors: true,
          closeButton: true,
        });
      }
    });
  }, [apiKey, apiUrl, authScheme]);

  return (
    <StreamContext.Provider value={streamValue}>
      {children}
    </StreamContext.Provider>
  );
};

// Default values for the form
const DEFAULT_API_URL = "http://localhost:2024";
const DEFAULT_ASSISTANT_ID = "agent";
const AGENT_BUILDER_AUTH_SCHEME = "langsmith-api-key";

export const StreamProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  // Get environment variables
  const envApiUrl: string | undefined = process.env.NEXT_PUBLIC_API_URL;
  const envAssistantId: string | undefined =
    process.env.NEXT_PUBLIC_ASSISTANT_ID;
  const envAuthScheme: string | undefined = process.env.NEXT_PUBLIC_AUTH_SCHEME;

  // Use URL params with env var fallbacks
  const [apiUrl, setApiUrl] = useQueryState("apiUrl", {
    defaultValue: envApiUrl || "",
  });
  const [assistantId, setAssistantId] = useQueryState("assistantId", {
    defaultValue: envAssistantId || "",
  });
  const [authScheme, setAuthScheme] = useQueryState("authScheme", {
    defaultValue: envAuthScheme || "",
  });
  const [isAgentBuilder, setIsAgentBuilder] = useState(
    () =>
      (authScheme || envAuthScheme || "").toLowerCase() ===
      AGENT_BUILDER_AUTH_SCHEME,
  );

  // For API key, use localStorage with env var fallback
  const [apiKey, _setApiKey] = useState(() => {
    const storedKey = getApiKey();
    return storedKey || "";
  });

  const setApiKey = (key: string) => {
    window.localStorage.setItem("lg:chat:apiKey", key);
    _setApiKey(key);
  };

  // Determine final values to use, prioritizing URL params then env vars
  const finalApiUrl = apiUrl || envApiUrl;
  const finalAssistantId = assistantId || envAssistantId;
  const finalAuthScheme = authScheme || envAuthScheme || "";

  // Show the form if we: don't have an API URL, or don't have an assistant ID
  if (!finalApiUrl || !finalAssistantId) {
    return (
      <div className="flex min-h-screen w-full items-center justify-center p-4">
        <div className="animate-in fade-in-0 zoom-in-95 bg-background flex max-w-3xl flex-col rounded-lg border shadow-lg">
          <div className="mt-14 flex flex-col gap-2 border-b p-6">
            <div className="flex flex-col items-start gap-2">
              <LangGraphLogoSVG className="h-7" />
              <h1 className="text-xl font-semibold tracking-tight">
                Agent Chat
              </h1>
            </div>
            <p className="text-muted-foreground">
              Welcome to Agent Chat! Before you get started, you need to enter
              the URL of the deployment and the assistant / graph ID.
            </p>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();

              const form = e.target as HTMLFormElement;
              const formData = new FormData(form);
              const apiUrl = formData.get("apiUrl") as string;
              const assistantId = formData.get("assistantId") as string;
              const apiKey = formData.get("apiKey") as string;

              setApiUrl(apiUrl);
              setApiKey(apiKey);
              setAssistantId(assistantId);
              setAuthScheme(isAgentBuilder ? AGENT_BUILDER_AUTH_SCHEME : "");

              form.reset();
            }}
            className="bg-muted/50 flex flex-col gap-6 p-6"
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="apiUrl">
                Deployment URL<span className="text-rose-500">*</span>
              </Label>
              <p className="text-muted-foreground text-sm">
                This is the URL of your LangGraph deployment. Can be a local, or
                production deployment.
              </p>
              <Input
                id="apiUrl"
                name="apiUrl"
                className="bg-background"
                defaultValue={apiUrl || DEFAULT_API_URL}
                required
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="assistantId">
                Assistant / Graph ID<span className="text-rose-500">*</span>
              </Label>
              <p className="text-muted-foreground text-sm">
                This is the ID of the graph (can be the graph name), or
                assistant to fetch threads from, and invoke when actions are
                taken.
              </p>
              <Input
                id="assistantId"
                name="assistantId"
                className="bg-background"
                defaultValue={assistantId || DEFAULT_ASSISTANT_ID}
                required
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="apiKey">LangSmith API Key</Label>
              <p className="text-muted-foreground text-sm">
                This is <strong>NOT</strong> required if using a local LangGraph
                server. This value is stored in your browser's local storage and
                is only used to authenticate requests sent to your LangGraph
                server.
              </p>
              <PasswordInput
                id="apiKey"
                name="apiKey"
                defaultValue={apiKey ?? ""}
                className="bg-background"
                placeholder="lsv2_pt_..."
              />
            </div>

            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-4">
                <div className="flex flex-col gap-1">
                  <Label htmlFor="agentBuilderEnabled">
                    Built with Agent Builder
                  </Label>
                  <p className="text-muted-foreground text-sm">
                    Enable this for Agent Builder deployments.
                  </p>
                </div>
                <Switch
                  id="agentBuilderEnabled"
                  checked={isAgentBuilder}
                  onCheckedChange={setIsAgentBuilder}
                />
              </div>
            </div>

            <div className="mt-2 flex justify-end">
              <Button
                type="submit"
                size="lg"
              >
                Continue
                <ArrowRight className="size-5" />
              </Button>
            </div>
          </form>
        </div>
      </div>
    );
  }

  return (
    <StreamSession
      apiKey={apiKey}
      apiUrl={finalApiUrl}
      assistantId={finalAssistantId}
      authScheme={finalAuthScheme || undefined}
    >
      {children}
    </StreamSession>
  );
};

// Create a custom hook to use the context
export const useStreamContext = (): StreamContextType => {
  const context = useContext(StreamContext);
  if (context === undefined) {
    throw new Error("useStreamContext must be used within a StreamProvider");
  }
  return context;
};

export default StreamContext;
