/**
 * 首页覆盖件(上游 `src/app/page.tsx`)—— 2026-08-25 设计审计。
 *
 * 唯一的改动:把 Suspense 的 fallback 从上游那行
 *
 *     <div>Loading (layout)...</div>
 *
 * 换成一块像这个产品的加载态。
 *
 * 🔴 为什么这一行值得一件覆盖件:**它是登录之后用户看到的第一屏**。
 *    登录页是很讲究的(工地胶带、安全橙、硬投影、港标字体),按下「進 入」之后
 *    屏幕左上角蹦出一行浏览器默认字的英文 —— 没有样式、没有品牌、没有布局,
 *    像是页面崩了。线上实测那一屏能停留一到几秒(取决于线程历史多大)。
 *
 *    这不是"不好看"的问题:加载态是**唯一**能告诉用户「系统在动、别刷新」的东西。
 *    一行左上角的英文既不说明在等什么,也不像是这个系统该有的样子,
 *    工友的合理反应是刷新 —— 而刷新会让刚才那几秒重来一遍。
 *
 * ⚠️ **除了 fallback 那一处,其余与上游逐字保持一致。** 这是 Provider 的嵌套顺序
 *    (Thread → Stream → Artifact),上游动过一次就会连着改这里;
 *    保持逐字一致是为了下次 `git diff` 上游时,差异一眼就只有那一块。
 *
 * ⚠️ 文案是**繁體**(界面恒繁體,2026-08-18 定案),且**源码里就写成繁體** ——
 *    这是首屏,挂 useHantUI 等于让每个用户首屏拉 438 KB 字典,
 *    正是 hant-convert.tsx「图 3」那条红线禁止的事。
 */

"use client";

import { Thread } from "@/components/thread";
import { StreamProvider } from "@/providers/Stream";
import { ThreadProvider } from "@/providers/Thread";
import { ArtifactProvider } from "@/components/thread/artifact";
import { Toaster } from "@/components/ui/sonner";
import React from "react";

/**
 * 加载态。刻意长得像首页**已经加载完**的样子:同一个底色、同一颗绿色 logo、
 * 同样的居中构图 —— 这样从加载态切到真界面时，视觉上是「内容长出来」，
 * 不是「换了一个页面」。
 *
 * 零依赖:不 import 任何组件。它要在 Suspense 边界**之外**能渲染,
 * 而边界之内的东西(Thread / 三个 Provider)正是还没就绪的那些。
 */
function BootScreen() {
  return (
    <div
      className="flex h-screen w-full flex-col items-center justify-center gap-5 bg-[#EEF1F0]"
      role="status"
      aria-live="polite"
    >
      {/* logo 方块 —— 与顶栏那颗同色同圆角,只是大一号。 */}
      <div className="flex h-14 w-14 items-center justify-center rounded-[16px] bg-[var(--gyt-green)] text-[26px] font-black text-white shadow-[0_10px_30px_rgba(14,159,110,0.28)]">
        工
      </div>

      <div className="flex flex-col items-center gap-2">
        <div className="text-[17px] font-black tracking-tight text-[var(--gyt-ink)]">
          工友通
        </div>
        {/* 说人话,并且**说清在等什么** —— 「載入中」三个字回答不了「还要多久 / 卡住了吗」。
            对比度:#5F6B66 在 #EEF1F0 上是 4.73:1,过 AA(与 GytStatusCards 同一个灰)。 */}
        <div className="text-[13px] font-medium text-[var(--gyt-muted)]">
          正在打開你的對話記錄…
        </div>
      </div>

      {/* 不定长进度条。用 transform 动画(合成器友好),不动 width。
          `motion-reduce:animate-none` 是必须的:开了「减少动态效果」的用户
          看到的应该是一根静止的浅色条,不是一根永远在跑的。 */}
      <div className="h-1 w-[132px] overflow-hidden rounded-full bg-[#DCE3E0]">
        <span className="block h-full w-1/3 rounded-full bg-[var(--gyt-green)] motion-safe:animate-[gyt-boot_1.1s_ease-in-out_infinite] motion-reduce:w-full motion-reduce:opacity-40" />
      </div>

      {/* keyframes 就地定义:globals.css 是上游的,不为一处加载态去覆盖它。 */}
      <style>{`
        @keyframes gyt-boot {
          0%   { transform: translateX(-100%); }
          100% { transform: translateX(300%); }
        }
      `}</style>
    </div>
  );
}

export default function DemoPage(): React.ReactNode {
  return (
    <React.Suspense fallback={<BootScreen />}>
      <Toaster />
      <ThreadProvider>
        <StreamProvider>
          <ArtifactProvider>
            <Thread />
          </ArtifactProvider>
        </StreamProvider>
      </ThreadProvider>
    </React.Suspense>
  );
}
