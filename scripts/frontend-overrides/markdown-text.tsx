"use client";

/**
 * Markdown 渲染 —— 覆盖 agent-chat-ui 上游的 src/components/thread/markdown-text.tsx
 *
 * ⚠️ 这个文件在 scripts/frontend-overrides/ 里,由 scripts/setup-frontend.sh 拷进
 *    frontend/。**别直接改 frontend/ 里那份** —— 那个目录不进 git,换台机器就没了。
 *
 * 为什么要覆盖上游:任务台账的查询结果按提示词约定输出成 markdown 表格
 * (任务号 | 任务 | 期限 | 状态),上游的表格样式是文档风(粗黑表头+满格边框),
 * 放在聊天气泡里像贴了张 Word 表。这里把表格改成现代应用风:
 *   · 圆角卡片容器 + 横向滚动(窄屏不撑破气泡)
 *   · 低调小字表头、斑马纹、行悬停
 *   · 语义徽章:「已逾期」红 /「未完成」琥珀 /「已完成」绿 /「没定期限」灰,
 *     T 号渲染成等宽小徽章 —— 状态一眼可扫,这正是「工作表格」的意义。
 * 徽章按**整格文本精确匹配**触发,不碰其它表格的正常单元格。
 *
 * 上游基线:agent-chat-ui 2026-08 版;除头注、textOf/徽章表与 table/th/td/tr
 * 四个组件外,其余逐字保持上游原样,方便对 diff 升级。
 */

import "./markdown-styles.css";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { FC, memo, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { SyntaxHighlighter } from "@/components/thread/syntax-highlighter";

import { TooltipIconButton } from "@/components/thread/tooltip-icon-button";
import { cn } from "@/lib/utils";
// W12:徽章键收简繁两套。词表的唯一真相在 lang-lib,别在本文件里再抄一份。
import {
  TASK_STATUS_WORDS,
  TASK_STATUS_WORDS_HANT,
  withHantKeys,
} from "@/lib/lang-lib";

import "katex/dist/katex.min.css";

interface CodeHeaderProps {
  language?: string;
  code: string;
}

const useCopyToClipboard = ({
  copiedDuration = 3000,
}: {
  copiedDuration?: number;
} = {}) => {
  const [isCopied, setIsCopied] = useState<boolean>(false);

  const copyToClipboard = (value: string) => {
    if (!value) return;

    navigator.clipboard.writeText(value).then(() => {
      setIsCopied(true);
      setTimeout(() => setIsCopied(false), copiedDuration);
    });
  };

  return { isCopied, copyToClipboard };
};

const CodeHeader: FC<CodeHeaderProps> = ({ language, code }) => {
  const { isCopied, copyToClipboard } = useCopyToClipboard();
  const onCopy = () => {
    if (!code || isCopied) return;
    copyToClipboard(code);
  };

  return (
    <div className="flex items-center justify-between gap-4 rounded-t-lg bg-zinc-900 px-4 py-2 text-sm font-semibold text-white">
      <span className="lowercase [&>span]:text-xs">{language}</span>
      <TooltipIconButton
        tooltip="Copy"
        onClick={onCopy}
      >
        {!isCopied && <CopyIcon />}
        {isCopied && <CheckIcon />}
      </TooltipIconButton>
    </div>
  );
};

/** 把任意 ReactNode 摊平成纯文本,供整格精确匹配用。 */
function textOf(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node === "object" && "props" in (node as any))
    return textOf((node as any).props?.children);
  return "";
}

const CHIP_BASE =
  "inline-flex items-center rounded-full px-2 py-0.5 text-[12px] font-medium ring-1 ring-inset";

/**
 * 状态词 → 徽章样式。只认整格精确匹配,防止误染普通句子里的这些词。
 *
 * **简繁两套键都收**(W12,2026-08-17)。词表与繁體镜像的唯一真相在
 * `@/lib/lang-lib` 的 `TASK_STATUS_WORDS` / `TASK_STATUS_WORDS_HANT`,
 * 那边有推演;`badge-keys.test.ts` 用真转换器钉着「繁體项 === s2hk(简体项)」。
 *
 * 🔴 **收繁體键修的是一个 W12 之前就存在的 bug,不只是给转换让路。**
 * 这四个词是 `agents/schedule/prompt.md:63` 让**模型照抄**写进表格的,
 * 而探针实测模型本来就有 25%-75% 的概率自己吐繁體 —— 所以在 W12 之前,
 * 模型写「沒定期限」时这个徽章就已经静默不上色了。
 *
 * 附带好处:**转换点插在哪都不影响徽章**。约束从「记住别挪转换点」
 * (靠人记)变成「表里有两种写法」(可测)。
 */
const STATUS_CHIPS: Record<string, string> = withHantKeys(
  TASK_STATUS_WORDS,
  TASK_STATUS_WORDS_HANT,
  (word) =>
    ({
      已逾期: `${CHIP_BASE} bg-red-50 text-red-700 ring-red-200`,
      未完成: `${CHIP_BASE} bg-amber-50 text-amber-700 ring-amber-200`,
      已完成: `${CHIP_BASE} bg-emerald-50 text-emerald-700 ring-emerald-200`,
      没定期限: `${CHIP_BASE} bg-gray-100 text-gray-500 ring-gray-200`,
    })[word] ?? CHIP_BASE,
);

const TASK_ID_RE = /^T\d+$/;

const defaultComponents: any = {
  h1: ({ className, ...props }: { className?: string }) => (
    <h1
      className={cn(
        "mb-8 scroll-m-20 text-4xl font-extrabold tracking-tight last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h2: ({ className, ...props }: { className?: string }) => (
    <h2
      className={cn(
        "mt-8 mb-4 scroll-m-20 text-3xl font-semibold tracking-tight first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h3: ({ className, ...props }: { className?: string }) => (
    <h3
      className={cn(
        "mt-6 mb-4 scroll-m-20 text-2xl font-semibold tracking-tight first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h4: ({ className, ...props }: { className?: string }) => (
    <h4
      className={cn(
        "mt-6 mb-4 scroll-m-20 text-xl font-semibold tracking-tight first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h5: ({ className, ...props }: { className?: string }) => (
    <h5
      className={cn(
        "my-4 text-lg font-semibold first:mt-0 last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  h6: ({ className, ...props }: { className?: string }) => (
    <h6
      className={cn("my-4 font-semibold first:mt-0 last:mb-0", className)}
      {...props}
    />
  ),
  p: ({ className, ...props }: { className?: string }) => (
    <p
      className={cn("mt-5 mb-5 leading-7 first:mt-0 last:mb-0", className)}
      {...props}
    />
  ),
  a: ({ className, ...props }: { className?: string }) => (
    <a
      className={cn(
        "text-primary font-medium underline underline-offset-4",
        className,
      )}
      {...props}
    />
  ),
  blockquote: ({ className, ...props }: { className?: string }) => (
    <blockquote
      className={cn("border-l-2 pl-6 italic", className)}
      {...props}
    />
  ),
  ul: ({ className, ...props }: { className?: string }) => (
    <ul
      className={cn("my-5 ml-6 list-disc [&>li]:mt-2", className)}
      {...props}
    />
  ),
  ol: ({ className, ...props }: { className?: string }) => (
    <ol
      className={cn("my-5 ml-6 list-decimal [&>li]:mt-2", className)}
      {...props}
    />
  ),
  hr: ({ className, ...props }: { className?: string }) => (
    <hr
      className={cn("my-5 border-b", className)}
      {...props}
    />
  ),
  table: ({ className, ...props }: { className?: string }) => (
    <div className="my-4 overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
      <table
        className={cn(
          "w-full border-collapse text-sm",
          "[&_tbody_tr:nth-child(even)]:bg-gray-50/60 [&_tbody_tr:hover]:bg-orange-50/40",
          "[&_tbody_tr:last-child_td]:border-b-0",
          className,
        )}
        {...props}
      />
    </div>
  ),
  th: ({ className, ...props }: { className?: string }) => (
    <th
      className={cn(
        "border-b border-gray-200 bg-gray-50 px-4 py-2.5 text-left text-[12px] font-medium tracking-wider whitespace-nowrap text-gray-500",
        "[&[align=center]]:text-center [&[align=right]]:text-right",
        className,
      )}
      {...props}
    />
  ),
  td: ({
    className,
    children,
    ...props
  }: {
    className?: string;
    children?: React.ReactNode;
  }) => {
    const text = textOf(children).trim();
    const chip = STATUS_CHIPS[text];
    const isTaskId = TASK_ID_RE.test(text);
    return (
      <td
        className={cn(
          "border-b border-gray-100 px-4 py-2.5 text-left align-middle text-gray-700",
          "[&[align=center]]:text-center [&[align=right]]:text-right",
          className,
        )}
        {...props}
      >
        {chip ? (
          <span className={chip}>{text}</span>
        ) : isTaskId ? (
          <span className="rounded-md bg-gray-100 px-1.5 py-0.5 font-mono text-[12px] font-medium text-gray-700">
            {text}
          </span>
        ) : (
          children
        )}
      </td>
    );
  },
  tr: ({ className, ...props }: { className?: string }) => (
    <tr
      className={cn("m-0 p-0", className)}
      {...props}
    />
  ),
  sup: ({ className, ...props }: { className?: string }) => (
    <sup
      className={cn("[&>a]:text-xs [&>a]:no-underline", className)}
      {...props}
    />
  ),
  pre: ({ className, ...props }: { className?: string }) => (
    <pre
      className={cn(
        "max-w-4xl overflow-x-auto rounded-lg bg-black text-white",
        className,
      )}
      {...props}
    />
  ),
  code: ({
    className,
    children,
    ...props
  }: {
    className?: string;
    children: React.ReactNode;
  }) => {
    const match = /language-(\w+)/.exec(className || "");

    if (match) {
      const language = match[1];
      const code = String(children).replace(/\n$/, "");

      return (
        <>
          <CodeHeader
            language={language}
            code={code}
          />
          <SyntaxHighlighter
            language={language}
            className={className}
          >
            {code}
          </SyntaxHighlighter>
        </>
      );
    }

    return (
      <code
        className={cn("rounded font-semibold", className)}
        {...props}
      >
        {children}
      </code>
    );
  },
};

const MarkdownTextImpl: FC<{ children: string }> = ({ children }) => {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        // 🔴 `singleTilde: false` 不能省(2026-09-21 线上体验测试抓到)。
        //    GFM 默认**单个** `~` 就构成删除线,而中文图纸/规范里 `~` 表范围是家常便饭
        //    (`①~㉑轴`、`第5~8層`、`3.300~6.600`)。一句话里出现两个 `~`,中间那段就被
        //    整体划掉,**而且两个波浪号本身消失** —— 「①~㉑軸」当场读成「①㉑軸」。
        //    实测那次:cad 答立面标高,原文「左側立面(㉑~①)標高:… 右側立面(①~㉑)標高:…」
        //    渲染成 `左側立面(㉑<del>①)標高:… 右側立面(①</del>㉑)標高:…`,
        //    两行标高全被划了删除线,工友看到的是「这答案作废了?」。
        //    关掉之后 `~~真删除线~~`(两个波浪)照常工作 —— 我们要的正是这个口径。
        remarkPlugins={[[remarkGfm, { singleTilde: false }], remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={defaultComponents}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
};

export const MarkdownText = memo(MarkdownTextImpl);
