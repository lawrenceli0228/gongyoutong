import React from "react";
import { File, X as XIcon } from "lucide-react";
import { ContentBlock } from "@langchain/core/messages";
import { cn } from "@/lib/utils";
import Image from "next/image";
export interface MultimodalPreviewProps {
  block: ContentBlock.Multimodal.Data;
  removable?: boolean;
  onRemove?: () => void;
  className?: string;
  size?: "sm" | "md" | "lg";
}

// --------------------------------------------------------------------------
// 🔴 这个文件里的英文残留(W12,2026-08-18 人工扫出来的)
// --------------------------------------------------------------------------
// 界面已经整体恒繁體,但本文件曾有 8 条英文上屏:4 个 aria-label(读屏用户**听到**
// 的就是 "Remove image")、1 条 JSX 正文("Unsupported file type")、
// 3 条缺文件名时的兜底显示("uploaded image" / "PDF file" / "DXF drawing")。
//
// ⚠️ W12 那套繁體守卫**一条都报不出来**:`hant-ui-strings.test.ts` 的判据是
//    `s2hk(v) !== v`,而 s2hk 对英文恒等 —— 英文在它眼里永远「已经到位」。
//    英文残留只能靠人扫,写新文案时别指望守卫。
//
// ⚠️ aria-label 是这里最容易被跳过的一档:它不上屏,肉眼过一遍页面看不见,
//    只有读屏用户会撞上 —— 一个全繁體的界面里突然念一句英文。

export const MultimodalPreview: React.FC<MultimodalPreviewProps> = ({
  block,
  removable = false,
  onRemove,
  className,
  size = "md",
}) => {
  // Image block
  if (
    block.type === "image" &&
    typeof block.mimeType === "string" &&
    block.mimeType.startsWith("image/")
  ) {
    const url = `data:${block.mimeType};base64,${block.data}`;
    let imgClass: string = "rounded-md object-cover h-16 w-16 text-lg";
    if (size === "sm") imgClass = "rounded-md object-cover h-10 w-10 text-base";
    if (size === "lg") imgClass = "rounded-md object-cover h-24 w-24 text-xl";
    return (
      <div className={cn("relative inline-block", className)}>
        <Image
          src={url}
          alt={String(block.metadata?.name || "上傳的圖片")}
          className={imgClass}
          width={size === "sm" ? 16 : size === "md" ? 32 : 48}
          height={size === "sm" ? 16 : size === "md" ? 32 : 48}
        />
        {removable && (
          <button
            type="button"
            className="absolute top-1 right-1 z-10 rounded-full bg-gray-500 text-white hover:bg-gray-700"
            onClick={onRemove}
            aria-label="移除這張圖片"
          >
            <XIcon className="h-4 w-4" />
          </button>
        )}
      </div>
    );
  }

  // PDF block
  if (block.type === "file" && block.mimeType === "application/pdf") {
    const filename =
      block.metadata?.filename || block.metadata?.name || "PDF 文件";
    return (
      <div
        className={cn(
          "relative flex items-start gap-2 rounded-md border bg-gray-100 px-3 py-2",
          className,
        )}
      >
        <div className="flex flex-shrink-0 flex-col items-start justify-start">
          <File
            className={cn(
              "text-teal-700",
              size === "sm" ? "h-5 w-5" : "h-7 w-7",
            )}
          />
        </div>
        <span
          className={cn("min-w-0 flex-1 text-sm break-all text-gray-800")}
          style={{ wordBreak: "break-all", whiteSpace: "pre-wrap" }}
        >
          {String(filename)}
        </span>
        {removable && (
          <button
            type="button"
            className="ml-2 self-start rounded-full bg-gray-200 p-1 text-teal-700 hover:bg-gray-300"
            onClick={onRemove}
            aria-label="移除這份 PDF"
          >
            <XIcon className="h-4 w-4" />
          </button>
        )}
      </div>
    );
  }

  // GYT 方案 A:DXF 图纸块。按 mimeType=image/vnd.dxf 或 filename .dxf 认,
  // 渲染成一张「图纸」小卡片(否则会落到最下面那张「这种文件传不了」的兜底卡)。
  if (
    block.type === "file" &&
    (block.mimeType === "image/vnd.dxf" ||
      String(block.metadata?.filename || "")
        .toLowerCase()
        .endsWith(".dxf"))
  ) {
    const filename = block.metadata?.filename || "未命名";
    return (
      <div
        className={cn(
          "relative flex items-start gap-2 rounded-md border bg-gray-100 px-3 py-2",
          className,
        )}
      >
        <div className="flex flex-shrink-0 flex-col items-start justify-start">
          <File
            className={cn(
              "text-blue-700",
              size === "sm" ? "h-5 w-5" : "h-7 w-7",
            )}
          />
        </div>
        <span
          className={cn("min-w-0 flex-1 text-sm break-all text-gray-800")}
          style={{ wordBreak: "break-all", whiteSpace: "pre-wrap" }}
        >
          {`圖紙 ${String(filename)}`}
        </span>
        {removable && (
          <button
            type="button"
            className="ml-2 self-start rounded-full bg-gray-200 p-1 text-blue-700 hover:bg-gray-300"
            onClick={onRemove}
            aria-label="移除這份圖紙"
          >
            <XIcon className="h-4 w-4" />
          </button>
        )}
      </div>
    );
  }

  // Fallback for unknown types
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-md border bg-gray-100 px-3 py-2 text-gray-500",
        className,
      )}
    >
      <File className="h-5 w-5 flex-shrink-0" />
      <span className="truncate text-xs">這種文件傳不了</span>
      {removable && (
        <button
          type="button"
          className="ml-2 rounded-full bg-gray-200 p-1 text-gray-500 hover:bg-gray-300"
          onClick={onRemove}
          aria-label="移除這份文件"
        >
          <XIcon className="h-4 w-4" />
        </button>
      )}
    </div>
  );
};
