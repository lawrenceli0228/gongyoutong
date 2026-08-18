/**
 * 简→繁(香港)转换器 —— **懒加载**,只在真要转的时候才拉字典。
 *
 * 安装:scripts/setup-frontend.sh 的 install_new_file 拷到 frontend/src/lib/hant-convert.tsx。
 * frontend/ 不进 git,**别直接改那边** —— 换台机器就没了。
 *
 * 判定归 lang-lib.ts(纯 TS 零依赖,可单测);本文件只管**拉字典 + 转字**。
 * 方案:docs/W12_三语切换_方案.md §4.6 / §5.1。
 *
 * ===========================================================================
 * 图 1:转换点插在哪 —— 以及为什么这个选择不是口味问题
 * ---------------------------------------------------------------------------
 *   ai.tsx  displayString ──▶ <MarkdownText> ──▶ react-markdown ──▶ td/th 组件
 *      ▲                                                              │
 *      └── 转换插在这里(本文件的 useHantText)                        │
 *                                                                     ▼
 *                                              textOf(children) 取整格文本,
 *                                              拿它当**键**精确查徽章样式
 *
 * **为什么必须在 ai.tsx,而不是 markdown-text.tsx 里面:**
 * `MarkdownText` 有四个消费者,其中两个**不是本仓的** ——
 *   frontend/src/components/thread/agent-inbox/components/state-view.tsx
 *   frontend/src/components/thread/agent-inbox/components/inbox-item-input.tsx
 * 那两个是上游的英文调试界面,显示的是原始 state。放 markdown-text.tsx
 * 会把它们也转了 —— 那是我们不拥有的表面,而且转它是错的。
 *
 * **为什么用户发言不走这条路:** `shouldConvert` 对 human 恒 false
 * (lang-lib.ts 里有推演)—— human.tsx 的 `(照片编号:…)` 正则是简体的,
 * 转了照片就不渲染成图。而且本文件压根不挂在 human 的渲染路径上,是双保险。
 *
 * **为什么徽章不怕:** 那两张表收了简繁两套键(lang-lib.ts 的
 * `TASK_STATUS_WORDS` / `SEVERITY_WORDS` 与它们的 `_HANT` 镜像)。
 * 所以转换点在哪都不影响徽章 —— 约束从「记住别挪」变成「表里有两种写法」,
 * 后者有测试钉着。
 *
 * ===========================================================================
 * 图 2:为什么要懒加载,以及为什么不许裁字典
 * ---------------------------------------------------------------------------
 *   T4 实测(方案 §5.1):opencc-js@1.4.1 的 cn2t 入口 = **438 KB gzipped**,
 *   其中 STPhrases 一个文件占 394 KB。
 *
 *   🔴 **不许把 STPhrases 裁掉换 25 KB 的精简档。** 那个表不是术语表,
 *      是「一简对多繁」的**消歧表**。裁掉之后拿本仓真实语料实测:
 *          签发 → 籤發(抽籤的籤)   而正确是 簽發
 *          复查 → 復查              而正确是 複查
 *          下周三 → 下周三(没转)   而正确是 下週三
 *      「簽發」「複查」正是监理链的两个核心动作,而这个错
 *      **没有任何测试会发现**(转换器没坏,只是选错了字)。
 *
 *   所以整份 lazy `import()`:简体用户一个字节都不下,繁體用户切过来时
 *   拉一次、之后浏览器缓存。300 KB 的预算管的是首屏。
 */

import { useEffect, useMemo, useState } from "react";

import { type Lang, shouldConvert, type MessageRole } from "@/lib/lang-lib";

type Converter = (text: string) => string;

/** 拉好之后缓存在模块级 —— 一个页面生命周期内只拉一次。 */
let cached: Converter | null = null;
/** 正在拉的那个 promise。并发调用共用它,不会重复发请求。 */
let inflight: Promise<Converter> | null = null;

/**
 * 拉字典并造出转换器。幂等、并发安全。
 *
 * 失败时**不抛**:返回恒等函数(原样返回简体)。理由 —— 网差的工地上
 * 「看到简体」远好过「整段答话消失」或者页面白屏。
 * 失败会写一条 console.warn,那是唯一的线索(方案里记着这条)。
 */
export async function loadHantConverter(): Promise<Converter> {
  if (cached) return cached;
  if (inflight) return inflight;

  inflight = (async () => {
    try {
      // 走 cn2t 那个入口:简体 → 繁體。**别换成 core + 单字表**,理由见图 2。
      const mod = await import("opencc-js/cn2t");
      const OpenCC = (mod as any).default ?? mod;
      const conv = OpenCC.Converter({ from: "cn", to: "hk" }) as Converter;
      cached = conv;
      return conv;
    } catch (err) {
      console.warn("[gyt] 繁體字典没拉到,答话保持简体显示", err);
      const identity: Converter = (t) => t;
      cached = identity;
      return identity;
    } finally {
      inflight = null;
    }
  })();

  return inflight;
}

/**
 * 按语种把一段答话转成繁體;不需要转就**原样返回同一个引用**。
 *
 * 「原样返回同一个引用」不是优化,是契约:调用方靠它判断「这轮没动过」,
 * 而且简体路径必须**一个字节的字典都不下载**。
 *
 * 字典还没到位时先返回原文(简体),到位后 React 重渲染换成繁體 ——
 * 一次性、只在切到繁體的第一条消息上可见。
 */
export function useHantText(text: string, role: MessageRole, lang: Lang): string {
  const wanted = shouldConvert(role, lang);

  // 🔴 **转换器不许进 useState。** 2026-08-18 真机崩过一次,原因值得写下来:
  //
  //    setConverter(conv)   // conv 是个函数
  //
  // React 把「传给 setState 的函数」当成 **updater**(setState(prev => next)),
  // 于是它去调 conv(上一个 state) = conv(null) → opencc 里 null.length → TypeError,
  // 整页崩成 "Application error: a client-side exception has occurred"。
  // 堆栈里的 basicStateReducer 就是那一层。
  //
  // 打补丁的写法是 setConverter(() => conv)。**这里不那么修** —— 转换器本来就是
  // 模块级单例,塞进 state 是多余的。改成「模块级存转换器 + state 只存一个计数器」,
  // 那个坑就**结构上不可能再踩**(state 里永远不是函数)。
  // conversion-seam.test.ts 有一条断言钉着「不许把转换器塞进 state」。
  const [, bumpVersion] = useState(0);

  useEffect(() => {
    if (!wanted || cached) return;
    let alive = true;
    void loadHantConverter().then(() => {
      // 只是催一次重渲染;转换器本身在模块级的 cached 里。
      if (alive) bumpVersion((n) => n + 1);
    });
    return () => {
      alive = false;
    };
  }, [wanted]);

  // useMemo 不是优化,是**别每次重渲染都重跑字典查找**。
  // 量过了(方案 §4 性能那条):一条 200 字答话几毫秒,20 轮历史也在噪声里 ——
  // 所以**不要**再往上加缓存层。
  //
  // 依赖里放 `cached` 而不是某个 state:字典拉到之后 cached 从 null 变成函数,
  // 引用变了 → useMemo 重算。bumpVersion 负责把这次重渲染触发出来。
  return useMemo(() => {
    if (!wanted || !cached) return text;
    return cached(text);
  }, [text, wanted, cached]);
}

// ---------------------------------------------------------------------------
// 界面侧:**恒繁體**,与答话的「跟着用户走」是两条独立的规则
// ---------------------------------------------------------------------------

/**
 * 图 3:界面为什么走另一条路,以及**哪些字不走这条路**
 * ---------------------------------------------------------------------------
 * 负责人 2026-08-18 定案:**界面恒繁體**(中建国际在港,现场语言是繁體),
 * 答话仍跟着用户打的字走。两条规则互不影响,所以这里没有 `lang` 参数。
 *
 * ```
 *   静态文案(按钮/占位符/提示句)  ──▶ **源码里就写成繁體**,不经本文件
 *        └ 理由:走运行时的话,首屏就要拉 438 KB 字典,
 *          「简体用户一个字节都不下」当场作废(§5.2 刚实测过)
 *
 *   后端给的**枚举 / 生成值**        ──▶ 本文件的 useHantUI / hantSync
 *   (status_display / due_display /    └ 这些字运行时才存在,源码里没法转;
 *    doc_type_display / result_display)   而它们整句都是系统写的,转全句是安全的
 *
 *   本地兜底表(HAZARD_STATUS_ZH /   ──▶ 也走 useHantUI
 *    DOC_TYPE_ZH / GRADE_* / SCOPES)   └ 它们同时是**送后端的值**或**后端返回值的镜像**,
 *                                         源码里转了会 400 / 匹配不上(登记在
 *                                         scripts/frontend-tests/hant-keep-hans.mjs)
 *
 *   🔴 后端 Envelope 的 `user_msg`   ──▶ **一律不转**,原样上屏
 *        └ 2026-08-18 复审定案。它**不是纯系统文案,内插了用户数据**:
 *          工友姓名、用户自己起的项目名与文件名、没折过的用户原话。实例 ——
 *              attendance/messages.py 的 missing_glyphs():
 *                「「𠮶」这几个字画不进凭证」   而转换器把 𠮶 转成 嗰
 *              → 工友「王𠮶」被告知他名字里根本没有的一个字。这句话存在的
 *                全部意义就是点名是哪个字,点错了他只能反复重试反复失败。
 *          整句转换在原理上分不出「系统写的字」和「内插的用户数据」,而分不出时
 *          **默认转是危险的那一侧** —— 与本仓「后端一律不转」同一口径。
 *          守卫:hant-ui-strings.test.ts 扫这三个函数的实参,出现 user_msg / userMsg 就报。
 * ```
 *
 * 🔴 **别把这个 hook 铺到常驻界面上。** 面板是点开才挂载的,字典跟着面板走;
 *    铺到主页面 = 每个用户首屏都拉字典,包括从不开面板的简体工友。
 */

/** 预热:面板挂载时叫一次,让字典跟面板一起在路上,而不是等第一条文本渲染。 */
export function ensureHantConverter(): void {
  if (!cached) void loadHantConverter();
}

/**
 * 尽力而为的同步转换 —— 给**命令式**场景用(toast、window.confirm、拼文件名)。
 *
 * 字典还没到位时原样返回简体。这是刻意的:命令式调用没有「等一下再重渲染」
 * 这回事,宁可这一次显示简体,也不能让一句提示语消失或者卡住。
 * 面板挂载时的 `ensureHantConverter()` 让这种情况基本只出现在开面板后的头一瞬。
 */
export function hantSync<T extends string | null | undefined>(text: T): T {
  if (!text) return text;
  if (!cached) {
    void loadHantConverter();
    return text;
  }
  return cached(text) as T;
}

/**
 * 界面文本 → 繁體。空值原样返回(后端字段常是 null,别在每个调用点写三元)。
 *
 * ⚠️ 是 hook,**必须无条件调用** —— 不许写在 if 里、不许写在 map 回调里。
 *    要转一个数组,用 `useHantUIAll`。
 */
export function useHantUI<T extends string | null | undefined>(text: T): T {
  const [, bumpVersion] = useState(0);

  // 与 useHantText 同一个理由:转换器是模块级单例,**不许进 state**
  // (进了 React 会把它当 updater 调掉,2026-08-18 真机崩过一次)。
  useEffect(() => {
    if (cached) return;
    let alive = true;
    void loadHantConverter().then(() => {
      if (alive) bumpVersion((n) => n + 1);
    });
    return () => {
      alive = false;
    };
  }, []);

  return useMemo(() => {
    if (!text || !cached) return text;
    return cached(text) as T;
  }, [text, cached]);
}

/**
 * 一次转一组 —— 给「按钮清单」这种场景(四颗筛子、两档定级)。
 *
 * 存在的理由是 hooks 规则:`items.map((s) => useHantUI(s))` 是违法的
 * (回调里调 hook,数量还会随数据变),而这正是最容易写出来的那一版。
 */
export function useHantUIAll(items: readonly string[]): string[] {
  const [, bumpVersion] = useState(0);

  useEffect(() => {
    if (cached) return;
    let alive = true;
    void loadHantConverter().then(() => {
      if (alive) bumpVersion((n) => n + 1);
    });
    return () => {
      alive = false;
    };
  }, []);

  return useMemo(() => {
    if (!cached) return [...items];
    return items.map((s) => cached!(s));
  }, [items, cached]);
}
