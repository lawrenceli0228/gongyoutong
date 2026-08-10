/**
 * 覆盖 frontend/src/lib/api-key.tsx —— 给公网部署加一条「令牌从构建期注入」的兜底。
 *
 * ## 上游原样是什么
 *
 * 上游只读 localStorage:
 *
 *     export function getApiKey(): string | null {
 *       try {
 *         if (typeof window === "undefined") return null;
 *         return window.localStorage.getItem("lg:chat:apiKey") ?? null;
 *       } catch { }
 *       return null;
 *     }
 *
 * 也就是说,令牌只能靠人**手动粘进浏览器的 localStorage**。
 * (Stream.tsx 第 169 行那句注释写着 "use localStorage with env var fallback",
 *  但那个 env 兜底在上游根本没实现 —— 注释是空头支票,这个文件把它补上。)
 *
 * ## 为什么要补
 *
 * 公网部署时后端开了鉴权(auth.py 校验请求头 X-Api-Key,Stream.tsx 第 57 行负责发)。
 * 不补这一条的话,外部测试的人打开页面的流程是:
 * 「先按 F12 → 找到 Application → localStorage → 手动新建一条 lg:chat:apiKey」。
 * 这个流程对工地上的人不成立,对大部分测试的人也不成立 —— 结果是没人测得动。
 *
 * 补上之后,令牌在构建期就烘进浏览器包里,测试的人只需要过 Caddy 那道口令门,
 * 进来就能直接用。
 *
 * ## 令牌会明文躺在浏览器包里 —— 这是刻意的,不是疏忽
 *
 * NEXT_PUBLIC_* 是 Next.js 的编译期变量,值会被直接替换进 JS 产物,
 * 任何人打开 DevTools 都能看到。之所以可以接受:
 *
 *   · 能拿到这个包的人,**已经过了 Caddy 的 basic_auth 口令门**。
 *     对他而言令牌不是秘密,他本来就有权用这个服务。
 *   · 令牌在这套设计里的职责是**纵深第二道**,防的是「Caddy 那层失效」——
 *     路由配错、有人排障时给 backend 加回 ports、机器上多起了一个容器直连内网。
 *     它从来就不负责挡终端用户。
 *
 * 换句话说:真正的门是口令,令牌是门后面的第二把锁。把第二把锁的钥匙
 * 交给已经进门的人,不改变外面那道门的强度。
 *
 * ⚠️ 正因如此,**只有 GYT_ACCESS_TOKEN 这个令牌能这么注入**。
 *    两把模型 API Key(DeepSeek / Moonshot)永远只在后端容器里,
 *    一个字符都不许出现在 NEXT_PUBLIC_* 里。
 *
 * ## 前置依赖(不满足的话这个文件是空转)
 *
 * scripts/setup-frontend.sh 生成的 frontend/Dockerfile 目前只声明了
 * ARG NEXT_PUBLIC_API_URL 和 ARG NEXT_PUBLIC_ASSISTANT_ID,
 * **没有** ARG NEXT_PUBLIC_API_KEY。Docker 对没声明的 build arg 只警告
 * 一句 "unused build arg" 就过去了 —— 构建成功、页面能开、令牌没进包,
 * 表现是每个请求被后端 401,而不是构建失败。
 * 要让本文件真正生效,那个 Dockerfile 模板里必须补上 ARG + ENV。
 */

/** localStorage 里存令牌的键名。上游定的,Stream.tsx 写入时也用这个,别改。 */
const STORAGE_KEY = "lg:chat:apiKey";

/**
 * 读构建期注入的令牌。
 *
 * 这里包一层 try 不是防御性编程的仪式感,是有具体故障要防的:
 * Next.js 只会把**构建时确实存在**的 NEXT_PUBLIC_* 替换成字面量。
 * 万一构建时没给这个变量(比如 Dockerfile 还没补上那条 ARG),
 * `process.env.NEXT_PUBLIC_API_KEY` 这个表达式可能原样留在浏览器包里,
 * 而浏览器环境里根本没有 process 这个全局对象 → ReferenceError。
 *
 * 那种情况下该发生的事是「当作没有令牌」,而不是**整个聊天页白屏**。
 * 少一个令牌顶多是请求被 401,还能看见界面去排查;白屏则什么线索都没有。
 */
function readInjectedApiKey(): string | null {
  try {
    const injected = process.env.NEXT_PUBLIC_API_KEY;
    if (typeof injected !== "string") return null;

    // 顺手挡掉两种「填了等于没填」的情况:
    //   · 空串:build arg 传了但值是空的
    //   · 没替换掉的占位符:有人照着示例文件原样抄了进去
    const trimmed = injected.trim();
    if (!trimmed) return null;
    if (trimmed.startsWith("替换成")) return null;

    return trimmed;
  } catch {
    // 见上面的说明:读不到就当没有,绝不往外抛。
  }

  return null;
}

/**
 * 取后端令牌,供请求头 X-Api-Key 使用。
 *
 * 优先级:localStorage > 构建期注入。
 *
 * localStorage 排前面是为了**本机调试**:公网包里烘的是线上那个令牌,
 * 想拿同一个包连别的后端、或者临时试一个新令牌时,
 * 在 DevTools 里改一条 localStorage 就能覆盖,不用重建镜像(前端构建是分钟级的)。
 * 上游的「在设置表单里填 apiKey」那条路也是写进 localStorage 的,顺带一起兼容了。
 *
 * 返回 null 表示「没有令牌」,调用方(Stream.tsx / Thread.tsx)会照原样不发这个头。
 */
export function getApiKey(): string | null {
  try {
    // 服务端渲染阶段没有 window。这时候也不返回注入的令牌 ——
    // 令牌是给浏览器发请求用的,SSR 阶段拿它没有用处,
    // 而少一处能把它写进服务端渲染 HTML 的可能,就少一处意外。
    if (typeof window === "undefined") return null;

    const stored = window.localStorage.getItem(STORAGE_KEY);
    // 用 trim 过滤掉手工粘贴时常见的首尾空格/换行 —— 那种令牌发出去必然 401,
    // 而且肉眼完全看不出哪里不对。
    if (stored && stored.trim()) return stored.trim();
  } catch {
    // localStorage 在少数场景下会直接抛(隐私模式、被浏览器策略禁用、
    // 或者页面被套在第三方 iframe 里)。这时候不该整页崩掉,
    // 往下走去用构建期注入的那个值就行。
  }

  return readInjectedApiKey();
}
