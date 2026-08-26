/**
 * 根布局覆盖件(上游 `src/app/layout.tsx`)—— 2026-08-25 设计审计后加的第一件。
 *
 * 上游那份三十行里有三处在生产上是错的,而**三处都零报错**:
 *
 *   ① `title: "Agent Chat"` / `description: "Agent Chat UX by LangChain"`
 *      → 浏览器标签页、收藏夹、分享出去的链接预览,写的全是**上游的名字**。
 *        线上实测过:`document.title === "Agent Chat"`。客户把站收藏起来,
 *        书签名字叫 Agent Chat。
 *
 *   ② `<html lang="en">`
 *      → 一个整屏繁體中文的界面对外声明自己是英文。后果不只是「标签写错了」:
 *        `lang` 是浏览器挑 CJK 字形、断行、以及读屏软件选语音的依据之一。
 *        登录页(`scripts/login-page.html:69`)早就是 `lang="zh-HK"` 了 —— App 没跟上。
 *
 *   ③ `Inter({ subsets: ["latin"] })` 直接挂在 `<body>` 上
 *      → 🔴 **这条最贵。** Inter 只订了拉丁字集,一个汉字都不归它管;
 *        于是整个界面的每一个汉字都落进浏览器兜底,而兜底是什么取决于操作系统。
 *        Windows 上那是 Microsoft YaHei —— **简体字形表**。
 *        拿它渲染繁體会落到大陆规范字形(骨/者/直/戶 这类笔形与港标不同),
 *        页面照开、字也认得出来,只是每个字都「长得不太对」,
 *        港方一眼看得出、又说不上哪儿怪。
 *
 *        这正是 `login-page.html:112` 那段注释亲手记过的坑。**登录页修了,App 没修** ——
 *        于是同一个用户在同一次会话里,登录页看到的是港标字形,按下「進 入」之后
 *        字形就换了一套,而中间没有任何提示。
 *
 * ⚠️ **Inter 没有被删掉,是被降到了第一档之后当拉丁字体用。** 这是有意的:
 *    界面上大量出现 `GYT-20260825-083033` 这种编号和 `37 KB` 这种数字,
 *    Inter 的等宽数字比系统默认好读得多。CSS 的字体回退是**逐字符**的 ——
 *    拉丁字符命中 Inter,汉字在 Inter 里查不到就往后走,落到 PingFang HK。
 *    两件事各得其所,不冲突。
 *
 * 🔴 **字体表与 `scripts/login-page.html` 那份是同源的,改一处要改两处。**
 *    那边的完整推演在它自己的注释里(为什么 `-apple-system` 排第一却不会抢走汉字:
 *    SF 本身没有 CJK 字形,逐字符回退时会被跳过;末尾留 SC 一档是兜底,
 *    装不上港标字体的机器有字总比方框强)。
 *
 * ⚠️ 用 inline `style` 而不是 className,是因为 Tailwind preflight 会在 `body` 上
 *    压一条 `font-family`;行内样式赢得过它,而多一个自定义 class 要跟框架抢优先级。
 */

import type { Metadata } from "next";
import "./globals.css";
import { Inter, JetBrains_Mono } from "next/font/google";
import React from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";

const inter = Inter({
  subsets: ["latin"],
  preload: true,
  display: "swap",
  // 变量形式:只定义 `--gyt-font-latin`,不直接接管 body。
  // 用 inter.className 的话它会把整条 font-family 写死成「Inter + 系统兜底」,
  // 那正是原来的病 —— 汉字挂不上任何一个指定的字体。
  variable: "--gyt-font-latin",
});

/**
 * 等宽字体(前端提升方案 1b「流」)。界面上大量的编号 `GYT-H-0312`、耗时 `6.83s`、
 * 文件大小 `42.6 KB` 都用等宽 —— 方案 dc.html 用的是 JetBrains Mono。
 *
 * 🔴 **只订 latin**:这些字符全是 ASCII,汉字永远走不到这条 `font-mono` 上
 *    (CSS 逐字符回退,汉字在 JetBrains Mono 里查不到会落回上面的 CJK 表)。
 *    latin 子集 ~30 KB woff2,守得住首屏预算;整套 CJK 等宽有好几 MB,绝不自托管。
 * ⚠️ 只定义 `--gyt-font-mono`,下面在 <body> 上把 Tailwind 的 `--font-mono` 指过来 ——
 *    这样全站 `font-mono` 工具类(编号 / 数字 / 代码)一次性换成它,不必逐处改 className。
 */
const jbMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
  variable: "--gyt-font-mono",
});

/**
 * 港标繁體优先。**与 `scripts/login-page.html` 的 body 字体表逐字段一致**,
 * 只在最前面多一档 `var(--gyt-font-latin)`(= Inter,只吃拉丁)。
 */
const FONT_STACK = [
  "var(--gyt-font-latin)",
  "-apple-system",
  "BlinkMacSystemFont",
  '"PingFang HK"',
  '"PingFang TC"',
  '"Hiragino Sans CNS"',
  '"Noto Sans CJK HK"',
  '"Noto Sans CJK TC"',
  '"Source Han Sans HK"',
  '"Microsoft JhengHei"',
  '"PingFang SC"',
  '"Noto Sans CJK SC"',
  "sans-serif",
].join(",");

export const metadata: Metadata = {
  // 界面恒繁體(2026-08-18 定案)。「工友通」三个字简繁同形,所以看着没改、不是漏了。
  title: "工友通",
  description: "建築工地多智能體助手",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-HK">
      <body
        className={`${inter.variable} ${jbMono.variable}`}
        // `--font-mono` 是 Tailwind v4 `font-mono` 工具类读的令牌(globals.css 没自定义,
        // 走的是框架默认);在 body 上把它指向 JetBrains Mono,自定义属性会向下继承,
        // 全站 `font-mono` 一次性生效。等宽只吃 ASCII,末尾照留系统兜底。
        style={
          {
            fontFamily: FONT_STACK,
            "--font-mono":
              "var(--gyt-font-mono), ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
          } as React.CSSProperties
        }
      >
        <NuqsAdapter>{children}</NuqsAdapter>
      </body>
    </html>
  );
}
