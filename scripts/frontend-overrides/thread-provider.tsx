/**
 * 线程列表数据源 —— 覆盖 agent-chat-ui 上游的 src/providers/Thread.tsx
 *
 * ⚠️ 这个文件在 scripts/frontend-overrides/ 里,由 scripts/setup-frontend.sh 拷进
 *    frontend/。**别直接改 frontend/ 里那份** —— 那个目录不进 git,换台机器就没了。
 *
 * ⚠️ 别跟 thread-history.tsx 搞混:那个覆盖的是历史侧栏的**渲染**
 *    (src/components/thread/history/index.tsx),这个覆盖的是它的**取数**。
 *    两者靠 extracted.first_message_content 这一个键对接,改名要一起改(见下)。
 *
 * ===========================================================================
 * 为什么要覆盖上游:侧栏只要标题,上游却把每条会话的全文都拖回来
 * ---------------------------------------------------------------------------
 * 上游的 getThreads() 就是一句 client.threads.search({ metadata, limit: 100 }),
 * 不带任何字段裁剪 —— 服务端于是回**整行**:values(完整 messages 数组)、
 * interrupts、config 全都在里面。而侧栏拿它只干一件事:显示第一句用户输入。
 *
 * 2026-08-15 对着线上 https://velactora.com 实测(24 条会话):
 *
 *   POST /api/threads/search   570ms / 177KB   ← 整个页面最慢的一条资源,没有之一
 *   页面冷启动到网络静默       1.46s           ← 上面这条占了 570ms
 *
 * 而**后端一点都不慢**:在容器内直打 /threads/search 是 0.06s(第二次 0.00s),
 * 单条会话的 /threads/{id}/history 也是 0.00s;开发机到 VPS 的网络往返 161ms。
 * 也就是说 570ms 里绝大部分是「把 177KB 搬过太平洋」的传输时间,
 * 177KB / 24 ≈ 每条 7.4KB —— 而侧栏真正要的那句话不到 100 字节。
 *
 * 更要命的是它**线性恶化**:100 条会话就是约 750KB、好几秒。演示当天攒到几十条
 * 是常态(thread-history.tsx 那个删除按钮就是为此加的),这条会越用越慢。
 *
 * 而且它一轮对话要付两次:Stream.tsx 的 onThreadId 里还有一句
 * `getThreads().then(setThreads)` —— 每次新建会话都重拉一遍整份列表。
 *
 * ===========================================================================
 * 怎么修:让服务端只回要的字段(select)+ 只回第一句话(extract)
 * ---------------------------------------------------------------------------
 * LangGraph SDK 的 threads.search() 支持这两个参数,不用自己造轮子。
 * 真实类型定义在(**别凭印象,这两处是查过的**):
 *   frontend/node_modules/@langchain/langgraph-sdk/dist/client.d.ts:425 起
 *     select?: ThreadSelectField[]    —— 只回列出的列
 *     extract?: Record<string,string> —— 按 JSONB 路径取值,结果放进 extracted 字段
 *   .../dist/schema.d.ts:289          —— ThreadSelectField 的九个合法取值
 *   .../dist/client.js:628 起         —— 确认这两个参数真的进了 POST body
 *
 * 服务端那侧也确认过能接(backend/.venv 里装的就是线上镜像同一套,
 * uv.lock 钉死 langgraph-api 0.12.0 / langgraph-runtime-inmem 0.32.0):
 *   langgraph_api/api/threads.py  的 search_threads —— 认 select/extract,
 *     并且在「为了取值而临时加回来的 values 列」用完之后 pop 掉,**不会回到线上**
 *   langgraph_api/utils/extract.py —— 路径语法(最多 10 条、根必须是
 *     values/metadata/config/interrupts 之一、alias 要是合法标识符)
 *   langgraph_runtime_inmem/ops.py 的 Threads.search —— inmem 后端(本项目用的这个)
 *     确实按 select 过滤字段、按 extract 填 extracted
 *
 * 本机实测(灌 57 条仿真会话,每条 ~8.7KB,形状照着线上那 7.4KB 仿的):
 *
 *   上游现状        493,666 字节  13ms   每条 8661 字节
 *   select+extract   19,021 字节   5ms   每条  334 字节   ← 降到 3.9%,省 96.1%
 *
 * 换算到线上那 24 条:177KB → 约 8KB。570ms 里的传输部分基本归零,
 * 剩下的就是那 161ms 往返,已经没什么可压的了。
 *
 * ===========================================================================
 * 三个「为什么不那样做」
 * ---------------------------------------------------------------------------
 * 1. **为什么不减小 limit / 做分页。** 那是拿功能换速度:侧栏会「少几条」,
 *    而少掉的那几条恰恰是要翻出来演的旧会话。字段裁剪之后每条 334 字节,
 *    100 条也才 33KB —— 根因(把全文搬过来)已经没了,再砍条数纯属自伤。
 *    limit 保持上游的 100 不动。
 *
 * 2. **为什么不在客户端裁。** 客户端裁只减渲染、**一个字节的传输量都减不掉**,
 *    而这条慢的就是传输。那是没解决根因。
 *
 * 3. **为什么不加 try/catch 退回老写法。** 想挡的是「前端先上、后端还是老版本
 *    不认识 select/extract」。实测过服务端对不认识的字段的态度:
 *      POST /threads/search {..., "totally_unknown_field": ["x"]} → HTTP 200,
 *      返回体与不带它时**一字节不差**(additionalProperties 没设 false)。
 *    也就是说老后端会**静默忽略**这两个参数、原样回整行 —— 不是报错。
 *    这种情况下 thread-history.tsx 的 getThreadTitle 会自动退回读 values
 *    (它两条路都留着),标题照常显示,只是没提速。
 *    所以退化是平滑的,再包一层重试只会多一条没人走过的代码路径。
 */

import { validate } from "uuid";
import { getApiKey } from "@/lib/api-key";
import { Thread } from "@langchain/langgraph-sdk";
import { useQueryState } from "nuqs";
import {
  createContext,
  useContext,
  ReactNode,
  useCallback,
  useState,
  Dispatch,
  SetStateAction,
} from "react";
import { createClient } from "./client";

/**
 * 侧栏真正用得上的列。**不含 values / interrupts / config** —— 那三样加起来
 * 就是上面说的 7.4KB,而侧栏一个都不读(agent-inbox 里那个 thread.values 来自
 * useStreamContext(),是当前打开的那条会话,跟这份列表没关系,核过)。
 *
 * 合法取值只有九个,见 schema.d.ts:289 与后端 langgraph_api/schema.py 的
 * THREAD_FIELDS;写错一个直接 422(实测:`"nope" is not one of ...`),
 * 不是静默忽略 —— 这里写错会让侧栏整个空掉。
 *
 * 为什么留着 created_at/updated_at/status/metadata 而不是只要 thread_id:
 * 它们合计只占每条 334 字节里的一小半,砍掉省不下什么,却会让 Thread 对象
 * 残缺到没法给别的地方复用(SDK 的 Thread 类型把它们标成必有)。
 *
 * as const 是必需的:ThreadSelectField 这个类型**没从包根导出**
 * (dist/index.d.ts 的导出清单里只有 Thread/ThreadState/ThreadStatus/ThreadTask),
 * import 不到,只能靠字面量推断出同样的联合类型喂给 search()。
 */
const THREAD_LIST_SELECT = [
  "thread_id",
  "created_at",
  "updated_at",
  "status",
  "metadata",
] as const;

/**
 * 侧栏标题那句话的取值路径与它在返回体里的键名。
 *
 * ⚠️ 同源:这个键名与 thread-history.tsx 的 `FIRST_MESSAGE_EXTRACT_KEY` 必须一字不差。
 *    只改一边的症状是**标题全部退化成 32 位 thread_id**(取不到值就走兜底),
 *    而请求照发、页面照开、控制台一声不吭 —— 看起来像后端没返回数据。
 *
 * 路径语法见 langgraph_api/utils/extract.py 的 extract_path_value:
 * 点号进字段、方括号进下标,末段用 dict 取或 getattr 取(inmem 后端里
 * messages 存的是 Message 对象不是 dict,靠后者兜住)。
 *
 * 两个边界实测过(本机灌数据打真接口):
 *   · 建了但从没跑过的会话 → extracted.first_message_content 为 null,
 *     getThreadTitle 兜底成 thread_id,与上游行为一致;
 *   · 首句是多模态数组(万一 ingest_uploads 还没改写就落了盘)→ 原样回那个数组,
 *     里面可能带 base64。这**不是退步**:今天这条数组连同整条 messages 一起在回,
 *     裁完至少只剩这一条了;getContentString 只挑 type==="text" 的块,标题照样对。
 */
const FIRST_MESSAGE_EXTRACT = {
  first_message_content: "values.messages[0].content",
} as const;

interface ThreadContextType {
  getThreads: () => Promise<Thread[]>;
  threads: Thread[];
  setThreads: Dispatch<SetStateAction<Thread[]>>;
  threadsLoading: boolean;
  setThreadsLoading: Dispatch<SetStateAction<boolean>>;
}

const ThreadContext = createContext<ThreadContextType | undefined>(undefined);

function getThreadSearchMetadata(
  assistantId: string,
): { graph_id: string } | { assistant_id: string } {
  if (validate(assistantId)) {
    return { assistant_id: assistantId };
  } else {
    return { graph_id: assistantId };
  }
}

export function ThreadProvider({ children }: { children: ReactNode }) {
  const envApiUrl: string | undefined = process.env.NEXT_PUBLIC_API_URL;
  const envAssistantId: string | undefined =
    process.env.NEXT_PUBLIC_ASSISTANT_ID;
  const envAuthScheme: string | undefined = process.env.NEXT_PUBLIC_AUTH_SCHEME;

  const [apiUrl] = useQueryState("apiUrl", {
    defaultValue: envApiUrl || "",
  });
  const [assistantId] = useQueryState("assistantId");
  const [authScheme] = useQueryState("authScheme", {
    defaultValue: envAuthScheme || "",
  });
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(false);

  const getThreads = useCallback(async (): Promise<Thread[]> => {
    const resolvedAssistantId = assistantId || envAssistantId;
    if (!apiUrl || !resolvedAssistantId) return [];
    const client = createClient(
      apiUrl,
      getApiKey() ?? undefined,
      authScheme || undefined,
    );

    const threads = await client.threads.search({
      metadata: {
        ...getThreadSearchMetadata(resolvedAssistantId),
      },
      limit: 100,
      // 这两行就是本覆盖件的全部改动 —— 上游没有它们,于是每条会话把全文都搬回来。
      // 展开成新数组而不是直接传那个 readonly 常量:search() 要的是可变数组类型。
      select: [...THREAD_LIST_SELECT],
      extract: { ...FIRST_MESSAGE_EXTRACT },
    });

    return threads;
  }, [apiUrl, assistantId, authScheme, envAssistantId]);

  const value = {
    getThreads,
    threads,
    setThreads,
    threadsLoading,
    setThreadsLoading,
  };

  return (
    <ThreadContext.Provider value={value}>{children}</ThreadContext.Provider>
  );
}

export function useThreads() {
  const context = useContext(ThreadContext);
  if (context === undefined) {
    throw new Error("useThreads must be used within a ThreadProvider");
  }
  return context;
}
