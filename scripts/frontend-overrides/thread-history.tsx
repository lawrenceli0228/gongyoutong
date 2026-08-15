/**
 * 线程历史(侧栏 / 移动端抽屉)—— 覆盖 agent-chat-ui 上游的
 * src/components/thread/history/index.tsx
 *
 * ⚠️ 这个文件在 scripts/frontend-overrides/ 里,由 scripts/setup-frontend.sh 拷进
 *    frontend/。**别直接改 frontend/ 里那份** —— 那个目录不进 git,换台机器就没了。
 *
 * ⚠️ 别跟 thread-index.tsx 搞混:那个覆盖的是 src/components/thread/index.tsx
 *    (聊天主体),这个覆盖的是 .../thread/history/index.tsx(左边那列历史)。
 *
 * ⚠️ 这份只管**渲染**;列表的**取数**在另一件覆盖件 thread-provider.tsx
 *    (覆盖 src/providers/Thread.tsx)。两者靠 FIRST_MESSAGE_EXTRACT_KEY
 *    这一个键名对接 —— 见下面 getThreadTitle 的头注。
 *
 * 为什么要覆盖上游:上游的历史列表只能点进去,**没有删除**。一场演示下来会攒出
 * 几十条「测试一下」「aaa」的废线程,下次上台要在里面翻找真正要演的那条;
 * 而且历史里躺着上一轮的错误回答,评委随手点开一条就可能看到已经修掉的老问题。
 *
 * 上游把列表渲染成一个 ThreadList 组件,桌面侧栏和移动端抽屉**共用**它
 * (见文件末尾 ThreadHistory 的两处 <ThreadList />)。所以删除只要做进 ThreadList,
 * 两处就都有了 —— 这也是这次没有去动 ThreadHistory 本体的原因。
 */

import { Button } from "@/components/ui/button";
import { useThreads } from "@/providers/Thread";
import { createClient } from "@/providers/client";
import { getApiKey } from "@/lib/api-key";
import { cn } from "@/lib/utils";
import { Message, Thread } from "@langchain/langgraph-sdk";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { getContentString } from "../utils";
import { useQueryState, parseAsBoolean } from "nuqs";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { PanelRightOpen, PanelRightClose, Trash2, Loader2 } from "lucide-react";
import { useMediaQuery } from "@/hooks/useMediaQuery";

/** 举手确认后多久自动缩回去。超时的默认结果是**不删**,所以这个值往短了取也安全。 */
const CONFIRM_TIMEOUT_MS = 6000;

/** 失败提示在屏幕上停留的时间。比默认的 4 秒长 —— 删除失败是要人做动作的,一晃而过等于没说。 */
const ERROR_TOAST_MS = 8000;

/**
 * 删除结果。刻意做成「四键信封」的前端简化版(参考后端 core/errors.py 的 Envelope):
 * **调用方只看 ok 和 userMsg,永远拿不到原始异常** —— 从类型上就堵死了
 * 「把 error.message 直接弹给用户」这条路(那会漏出 URL、类名、堆栈)。
 * 原始异常只进 console,给开发看。
 */
type DeleteResult = { ok: true } | { ok: false; userMsg: string };

/**
 * 把删除失败翻译成一句人话。
 *
 * 状态码是怎么拿到的:SDK 自带的 AsyncCaller(node_modules/@langchain/langgraph-sdk/
 * dist/utils/async_caller.js)在响应非 2xx 时抛的是它自己的 HTTPError,上面带 status。
 * 但 HTTPError 这个类没从包里导出(client.d.ts 里搜不到),所以只能鸭子类型读 status,
 * 不能 instanceof。
 *
 * 连不上服务器是另一条路:同一个 AsyncCaller 在 onFailedAttempt 里认出
 * "Failed to fetch" / "ECONNREFUSED" 之类,会换成一个 name === "ConnectionError"
 * 的 Error 抛出来(并且**不重试**,所以后端没起时是立刻报错,不会转好几十秒)。
 */
function describeDeleteFailure(error: unknown): string {
  const status =
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    typeof (error as { status: unknown }).status === "number"
      ? (error as { status: number }).status
      : undefined;

  // 404:后端确实回了「没这条」。但**不能**就此认定「那它本来就没了、悄悄从列表摘掉」——
  // 走代理(langgraph-nextjs-api-passthrough)时路径配错同样是 404,那种情况下线程好端端在。
  // 摘掉 = 界面上「删成功了」而服务器上还在,正是最坏的那种假象。所以只提示,不摘行。
  if (status === 404) {
    return "删不掉:服务器上没找到这条记录。刷新一下页面,看看它是不是已经不在了。";
  }
  if (status === 401 || status === 403) {
    return "删不掉:没有权限。检查一下 API Key 填对了没有。";
  }
  if (status !== undefined && status >= 500) {
    return `删不掉:服务器这边出错了(${status})。稍等一下再试。`;
  }
  if (status !== undefined) {
    return `删不掉:服务器拒绝了这次删除(${status})。`;
  }

  const name = error instanceof Error ? error.name : "";
  if (name === "ConnectionError" || error instanceof TypeError) {
    return "删不掉:连不上服务器。确认后端还开着,再试一次。";
  }
  return "删不掉,原因不清楚。刷新页面再试一次;还是不行就看一眼后端日志。";
}

/**
 * 删线程的唯一出口。发的就是 DELETE {apiUrl}/threads/{thread_id}(成功回 204 空响应)。
 *
 * 为什么这里要自己解析 apiUrl,而不是从 useThreads() 拿:
 * useThreads() 只暴露了 getThreads / threads / setThreads / loading 四样,**没有 apiUrl**
 * (见 src/providers/Thread.tsx 的 ThreadContextType)。所以只能把 Thread provider 里那三行
 * 原样照抄一遍:apiUrl 和 authScheme 走 useQueryState(带 env 兜底)、apiKey 走 getApiKey()。
 * **必须原样**,不能图省事只读 process.env —— 那样 ?apiUrl=... 覆盖时,聊天打到 A 后端、
 * 删除打到 B 后端,点了没反应还查不出所以然。
 *
 * 用 SDK 的 client.threads.delete() 而不是裸 fetch,是为了不再抄一份鉴权头的写法
 * (headers 的名字归 SDK 管,以后它改了这里跟着走)。
 */
function useDeleteThread(): (threadId: string) => Promise<DeleteResult> {
  const envApiUrl: string | undefined = process.env.NEXT_PUBLIC_API_URL;
  const envAuthScheme: string | undefined = process.env.NEXT_PUBLIC_AUTH_SCHEME;

  const [apiUrl] = useQueryState("apiUrl", { defaultValue: envApiUrl || "" });
  const [authScheme] = useQueryState("authScheme", {
    defaultValue: envAuthScheme || "",
  });

  return useCallback(
    async (threadId: string): Promise<DeleteResult> => {
      if (!apiUrl) {
        return { ok: false, userMsg: "删不掉:还没填服务地址。" };
      }
      try {
        const client = createClient(
          apiUrl,
          getApiKey() ?? undefined,
          authScheme || undefined,
        );
        await client.threads.delete(threadId);
        return { ok: true };
      } catch (error: unknown) {
        // 原始异常只落 console,不进界面 —— 界面上只出 describeDeleteFailure 那句话。
        console.error("[历史列表] 删除线程失败", { threadId, error });
        return { ok: false, userMsg: describeDeleteFailure(error) };
      }
    },
    [apiUrl, authScheme],
  );
}

/**
 * 取数那侧(thread-provider.tsx)用 extract 把第一句话单独取出来时,放在返回体
 * 里的键名。
 *
 * ⚠️ 同源:必须与 scripts/frontend-overrides/thread-provider.tsx 的
 *    `FIRST_MESSAGE_EXTRACT` 一字不差。只改一边的症状是**标题全部退化成
 *    32 位 thread_id**(这边取不到值就走最后那条兜底),而请求照发、页面照开、
 *    控制台一声不吭 —— 看起来像后端没返回数据,方向全错。
 */
const FIRST_MESSAGE_EXTRACT_KEY = "first_message_content";

/**
 * 列表项的标题:第一条用户消息的正文,取不到就退回 thread_id。
 *
 * 两条取值路径都得留着,不是冗余:
 *
 * 1. `extracted[FIRST_MESSAGE_EXTRACT_KEY]` —— 正常路径。thread-provider.tsx 用
 *    threads.search 的 select + extract 让服务端**只回这一句**,不再回整份
 *    messages(2026-08-15 线上实测:那一条请求 570ms / 177KB,是整页最慢的资源;
 *    裁完本机实测 493,666 → 19,021 字节,省 96.1%。完整数据与出处见 provider 那份头注)。
 *
 * 2. `t.values.messages[0].content` —— 上游原逻辑,**留作退化路径**。
 *    实测过服务端对不认识的字段是 HTTP 200 静默忽略(不是报错),所以万一前端先上、
 *    后端还是不认 select/extract 的老版本,回来的就是没裁过的整行 —— 这时候
 *    extracted 不存在,靠这条把标题接住。少了它那种情况下**满屏都是 thread_id**。
 *
 * 顺序不能反:extract 生效时 values 压根不在返回体里,先看 extracted 才是常态路径。
 */
function getThreadTitle(t: Thread): string {
  // extracted 的值类型是 unknown(SDK 只保证 Record<string, unknown>),
  // 得自己收窄:字符串直接用;多模态数组交给 getContentString 挑出 text 块
  // (真出现过 —— 首句带图而 ingest_uploads 还没来得及改写就落了盘)。
  const extracted = t.extracted?.[FIRST_MESSAGE_EXTRACT_KEY];
  if (typeof extracted === "string" && extracted.length > 0) {
    return extracted;
  }
  if (Array.isArray(extracted)) {
    const text = getContentString(extracted as Message["content"]);
    if (text.length > 0) return text;
  }

  if (
    typeof t.values === "object" &&
    t.values &&
    "messages" in t.values &&
    Array.isArray(t.values.messages) &&
    t.values.messages?.length > 0
  ) {
    return getContentString(t.values.messages[0].content);
  }
  return t.thread_id;
}

/**
 * 举手确认条 —— 顶掉原来那一行,不是弹窗。
 *
 * 为什么不用 window.confirm:它阻塞主线程,文案会被浏览器加一层「localhost:3000 显示」的壳,
 * 而且用户勾了「阻止此页面再次弹出对话框」之后**会被静默跳过**(直接返回 false)。
 * 一个不可逆操作的关卡,不能建在一个可能被浏览器悄悄关掉的东西上。
 *
 * 为什么不用 sonner 的 toast 二次确认(文案空间最舒服的那个方案):移动端历史是 Radix Dialog
 * 抽屉(ui/sheet.tsx 用的就是 @radix-ui/react-dialog),它打开时 DismissableLayer 会把
 * document.body.style.pointerEvents 置成 "none"(见 @radix-ui/react-dismissable-layer
 * 的 dist/index.mjs),只有弹层自己是 auto。toast 渲染在抽屉外面(Toaster 挂在 app/page.tsx),
 * 于是**桌面点得动、移动端点不动**。只在一半设备上生效的确认,等于没有确认。
 *
 * 行内确认不进 portal、不受 modal 影响,桌面和移动端是同一套代码同一个行为。
 * 代价:280px 一行放不下线程标题,确认条上只有警告和两个按钮 —— 靠「就是你刚点的那一行原地变红」
 * 来指认对象。高度和原来那行一样(h-9),不会把下面的列表顶得跳一下。
 */
/** 举手到可确认之间的静默期。低于这个间隔的「确认」一律当成误触丢掉。
 *
 * 300ms 是人**有意识**看清一句话再点第二下的下限,而系统双击判定通常在 500ms 内 ——
 * 取 400ms 卡在中间:挡得住双击,又不会让真想删的人觉得按钮卡住。
 * (这个数是取舍不是实测值,改它之前先想清楚要挡的是哪种手势。)
 */
const ARM_QUIET_MS = 400;

function DeleteConfirmBar({
  onCancel,
  onConfirm,
  armedAt,
}: {
  onCancel: () => void;
  onConfirm: () => void;
  /** 举手那一刻的时间戳,用来卡静默期。 */
  armedAt: number;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // 焦点落在「取消」而不是「删除」:键盘用户敲一下回车的默认结果必须是不删。
  // preventScroll 是因为列表本身是滚动容器,聚焦时不希望它自己跳位置。
  useEffect(() => {
    cancelRef.current?.focus({ preventScroll: true });
  }, []);

  return (
    <div
      role="group"
      aria-label="确认删除这条对话记录"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }}
      className="flex h-9 w-[280px] items-center justify-between gap-1 rounded-md border border-red-300 bg-red-50 pr-1 pl-2.5"
    >
      <span className="truncate text-xs text-red-700">删了找不回来,真删?</span>
      {/* ⚠️ 顺序是「删除 / 取消」,取消在**最右** —— 这是刻意的,别按习惯调回去。
          2026-08-11 对抗复核拿跑着的编译产物里的真实 CSS 值算过几何:
          确认条 w-[280px] 从 x=4 起、pr-1 + 1px 边框 → 内容右缘 279;
          垃圾桶按钮 absolute right-2 size-7 → 占 [264, 292],图标中心 x=278。
          **重叠区 [264, 279] 有 15px,而且与按钮文字宽度无关**(光 px-2 内边距就 16px)。
          移动端更糟:375px 屏上抽屉行宽 281px,垃圾桶被整个盖住。
          于是「对着垃圾桶图标正中双击」时,第二下必然落在最右那个按钮上。
          把取消放最右,双击的默认结果就从「删掉了」变成「撤销举手」。
          加上下面的静默期,两道一起才稳 —— 单靠任一条都能被更快的手绕过。 */}
      <div className="flex shrink-0 items-center gap-1">
        <button
          type="button"
          onClick={() => {
            // 静默期:举手后 400ms 内的「确认」当误触丢掉,一声不吭。
            // 不弹提示是刻意的 —— 真误触的人根本没看见这个条,给他弹个框只会更懵;
            // 而真想删的人稍等半秒再点就过了,不会觉得坏了。
            if (Date.now() - armedAt < ARM_QUIET_MS) return;
            onConfirm();
          }}
          className="rounded bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-700 focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:outline-none"
        >
          删除
        </button>
        <button
          ref={cancelRef}
          type="button"
          onClick={onCancel}
          className="rounded px-2 py-1 text-xs text-gray-600 hover:bg-white hover:text-gray-900 focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:outline-none"
        >
          取消
        </button>
      </div>
    </div>
  );
}

/**
 * 一行线程。纯展示件:自己不发请求、不改列表,状态全由 ThreadList 拿着
 * (这样才能保证「同时只有一行处于举手状态」)。
 */
function ThreadRow({
  title,
  isArmed,
  armedAt,
  isDeleting,
  canHover,
  onOpen,
  onArm,
  onCancel,
  onConfirm,
}: {
  title: string;
  isArmed: boolean;
  /** 举手那一刻的时间戳,透传给确认条卡静默期。 */
  armedAt: number;
  isDeleting: boolean;
  canHover: boolean;
  onOpen: () => void;
  onArm: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="group relative w-full px-1">
      {isArmed ? (
        <DeleteConfirmBar
          armedAt={armedAt}
          onCancel={onCancel}
          onConfirm={onConfirm}
        />
      ) : (
        <>
          {/*
            className 与上游一模一样,一个字没改;给删除按钮腾位置是靠下面那个 <p> 的
            max-w,不是靠给 Button 加 pr-*。
            为什么绕这一下:Button 自带 px-4,再叠一个 pr-10 就成了同组冲突,
            最终哪个生效取决于 Tailwind 生成 CSS 时 px-* 和 pr-* 谁排在后面 ——
            那是框架内部的排序规则,升一次版就可能反过来,反过来的表现是
            「标题压在垃圾桶图标底下」。max-w 没有任何东西跟它抢,排序怎么变都不影响。
          */}
          <Button
            variant="ghost"
            className="w-[280px] items-start justify-start text-left font-normal"
            onClick={(e) => {
              e.preventDefault();
              onOpen();
            }}
          >
            {/*
              13rem = 208px。这一行的排布(单位 px,从行容器左边缘算起):
              行宽 300(侧栏 w-[300px])→ px-1 让 Button 从 4 起、宽 280、到 284;
              Button 的 px-4 让正文从 20 起,截断到 20+208 = 228 为止;
              删除按钮 right-2 + size-7,占 264~292。中间留 36px,标题绝不会被压住。
              280 和 300 这两个数是上游写死的,改了它们记得回来重算这里。
            */}
            <p className="max-w-[13rem] truncate text-ellipsis">{title}</p>
          </Button>

          {/*
            删除按钮是绝对定位的**兄弟**元素,不是套在上面那个 Button 里面 ——
            按钮套按钮是非法 HTML,而且点删除会连带触发打开线程。
          */}
          <button
            type="button"
            aria-label={`删除这条对话记录:${title}`}
            title="删除这条对话记录"
            disabled={isDeleting}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onArm();
            }}
            className={cn(
              "absolute top-1/2 right-2 flex size-7 -translate-y-1/2 items-center justify-center rounded-md",
              // 这里只能写一个 transition:transition-opacity 和 transition-colors 都是
              // transition-property 这一组的,cn() 底下的 tailwind-merge 会按同组冲突处理、
              // 只留后写的那个 —— 两个都写等于其中一个白写(悬停显形就不带淡入了)。
              // 通配的 transition 一次覆盖 opacity + color + background-color。
              "text-gray-400 transition hover:bg-red-100 hover:text-red-600",
              "focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:outline-none",
              // 悬停才显形,免得一列垃圾桶图标在旁边勾着人点。
              // 但**触屏没有 hover**,那种设备上必须常驻,否则永远点不到 ——
              // 所以这里用 JS 判断 (hover: hover) 而不是 CSS 的 group-hover:
              // 纯 CSS 写法要靠两条同在 @media (hover:hover) 里的规则的先后顺序,
              // 那个顺序由 Tailwind 的变体排序决定,升级一次就可能翻车。
              // useMediaQuery 首帧返回 false(effect 还没跑),也就是**先常驻再隐藏**:
              // 桌面端首屏会闪一下,但坏的方向是「多显示了一瞬」,不是「触屏设备上点不到」。
              canHover ? "opacity-0 group-hover:opacity-100" : "opacity-70",
              isDeleting && "opacity-100",
            )}
          >
            {isDeleting ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Trash2 className="size-4" />
            )}
          </button>
        </>
      )}
    </div>
  );
}

function ThreadList({
  threads,
  onThreadClick,
}: {
  threads: Thread[];
  onThreadClick?: (threadId: string) => void;
}) {
  const [threadId, setThreadId] = useQueryState("threadId");
  const { setThreads } = useThreads();
  const deleteThread = useDeleteThread();

  // 「正在举手等确认」和「正在删」都提到列表这一层:
  // 举手的只能有一条(点第二条时第一条自动缩回去),避免一屏红条。
  // 记的是 {id, 举手时刻} 而不是只记 id —— 时刻要传给确认条卡静默期,
  // 挡「对着垃圾桶双击」那条误删路径(几何推演见 DeleteConfirmBar 的注释)。
  const [armed, setArmed] = useState<{ id: string; at: number } | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // (hover: hover) 而不是屏幕宽度 —— 判据是「这台设备有没有鼠标」,
  // 窄窗口的桌面浏览器照样能 hover,平板横屏再宽也不能。
  const canHover = useMediaQuery("(hover: hover)");

  // 举手了又不理它,自己缩回去。超时的结果是不删,所以这是安全方向的兜底。
  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(null), CONFIRM_TIMEOUT_MS);
    return () => clearTimeout(timer);
    // 只依赖 id:同一条重复举手不该把倒计时重置成新的一轮。
  }, [armed?.id]);

  const handleConfirmDelete = useCallback(
    async (target: Thread) => {
      const targetId = target.thread_id;
      setArmed(null);
      setDeletingId(targetId);

      const result = await deleteThread(targetId);
      setDeletingId(null);

      if (!result.ok) {
        // 失败就只出提示,**行留在列表里**。把行摘掉会让人以为删干净了,
        // 而服务器上那条还在 —— 假装删成功比删失败更坏。
        toast.error(result.userMsg, {
          richColors: true,
          duration: ERROR_TOAST_MS,
          closeButton: true,
        });
        return;
      }

      setThreads((prev) => prev.filter((t) => t.thread_id !== targetId));

      // 删的正好是当前开着的那条:必须把主界面也拉回新会话。
      // 不拉的话界面还停在一条服务器上已经没有的线程上,下一句话发出去会打到一个
      // 不存在的 thread_id,报出来的错前端没人接。
      if (targetId === threadId) {
        setThreadId(null);
      }

      toast.success("这条对话记录已经删掉了", { duration: 3000 });
    },
    [deleteThread, setThreads, threadId, setThreadId],
  );

  return (
    <div className="flex h-full w-full flex-col items-start justify-start gap-2 overflow-y-scroll [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent">
      {threads.map((t) => (
        <ThreadRow
          key={t.thread_id}
          title={getThreadTitle(t)}
          isArmed={armed?.id === t.thread_id}
          armedAt={armed?.at ?? 0}
          isDeleting={deletingId === t.thread_id}
          canHover={canHover}
          onOpen={() => {
            onThreadClick?.(t.thread_id);
            if (t.thread_id === threadId) return;
            setThreadId(t.thread_id);
          }}
          onArm={() => setArmed({ id: t.thread_id, at: Date.now() })}
          onCancel={() => setArmed(null)}
          onConfirm={() => void handleConfirmDelete(t)}
        />
      ))}
    </div>
  );
}

function ThreadHistoryLoading() {
  return (
    <div className="flex h-full w-full flex-col items-start justify-start gap-2 overflow-y-scroll [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent">
      {Array.from({ length: 30 }).map((_, i) => (
        <Skeleton
          key={`skeleton-${i}`}
          className="h-10 w-[280px]"
        />
      ))}
    </div>
  );
}

export default function ThreadHistory() {
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");
  const [chatHistoryOpen, setChatHistoryOpen] = useQueryState(
    "chatHistoryOpen",
    parseAsBoolean.withDefault(false),
  );

  const { getThreads, threads, setThreads, threadsLoading, setThreadsLoading } =
    useThreads();

  useEffect(() => {
    if (typeof window === "undefined") return;
    setThreadsLoading(true);
    getThreads()
      .then(setThreads)
      .catch(console.error)
      .finally(() => setThreadsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      {/* 桌面:左侧常驻栏 */}
      <div className="shadow-inner-right hidden h-screen w-[300px] shrink-0 flex-col items-start justify-start gap-6 border-r-[1px] border-slate-300 lg:flex">
        <div className="flex w-full items-center justify-between px-4 pt-1.5">
          <Button
            className="hover:bg-gray-100"
            variant="ghost"
            onClick={() => setChatHistoryOpen((p) => !p)}
          >
            {chatHistoryOpen ? (
              <PanelRightOpen className="size-5" />
            ) : (
              <PanelRightClose className="size-5" />
            )}
          </Button>
          <h1 className="text-xl font-semibold tracking-tight">
            Thread History
          </h1>
        </div>
        {threadsLoading ? (
          <ThreadHistoryLoading />
        ) : (
          <ThreadList threads={threads} />
        )}
      </div>

      {/* 移动端:抽屉。这里用的是同一个 ThreadList,所以删除按钮两处都有 */}
      <div className="lg:hidden">
        <Sheet
          open={!!chatHistoryOpen && !isLargeScreen}
          onOpenChange={(open) => {
            if (isLargeScreen) return;
            setChatHistoryOpen(open);
          }}
        >
          <SheetContent
            side="left"
            className="flex lg:hidden"
          >
            <SheetHeader>
              <SheetTitle>Thread History</SheetTitle>
            </SheetHeader>
            <ThreadList
              threads={threads}
              // 点标题才收起抽屉;点删除不收 —— 一般是要连着清好几条的。
              onThreadClick={() => setChatHistoryOpen((o) => !o)}
            />
          </SheetContent>
        </Sheet>
      </div>
    </>
  );
}
