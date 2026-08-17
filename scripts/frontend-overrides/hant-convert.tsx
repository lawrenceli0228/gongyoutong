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
  const [converter, setConverter] = useState<Converter | null>(() => cached);

  useEffect(() => {
    if (!wanted) return;
    if (cached) {
      setConverter(cached);
      return;
    }
    let alive = true;
    void loadHantConverter().then((conv) => {
      if (alive) setConverter(conv);
    });
    return () => {
      alive = false;
    };
  }, [wanted]);

  // useMemo 不是优化,是**别每次重渲染都重跑字典查找**。
  // 量过了(方案 §4 性能那条):一条 200 字答话几毫秒,20 轮历史也在噪声里 ——
  // 所以**不要**再往上加缓存层,那会把结果塞进 state,复杂度不值。
  return useMemo(() => {
    if (!wanted || !converter) return text;
    return converter(text);
  }, [text, wanted, converter]);
}
