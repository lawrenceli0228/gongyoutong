import { ContentBlock } from "@langchain/core/messages";
import { toast } from "sonner";

// Returns a Promise of a typed multimodal block for images or PDFs
export async function fileToContentBlock(
  file: File,
): Promise<ContentBlock.Multimodal.Data> {
  const supportedImageTypes = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
  ];
  const supportedFileTypes = [...supportedImageTypes, "application/pdf"];

  // GYT 方案 A:DXF 图纸。浏览器给 .dxf 的 MIME 极不可靠(常是空串或
  // application/octet-stream),所以**按文件名 .dxf 后缀认**,不信 file.type。
  const isDxf = file.name.toLowerCase().endsWith(".dxf");

  if (!supportedFileTypes.includes(file.type) && !isDxf) {
    toast.error(
      `Unsupported file type: ${file.type}. Supported types are: ${supportedFileTypes.join(", ")}, .dxf`,
    );
    return Promise.reject(new Error(`Unsupported file type: ${file.type}`));
  }

  const data = await fileToBase64(file);

  // DXF 图纸:发成 file 块(与 PDF 同形),后端 uploads.py 按 filename .dxf 认出来
  // 登记成 DRAWING 产物。mimeType 钉一个规范值,后端主要还是看 filename。
  if (isDxf) {
    return {
      type: "file",
      mimeType: "image/vnd.dxf",
      data,
      metadata: { filename: file.name },
    };
  }

  if (supportedImageTypes.includes(file.type)) {
    return {
      type: "image",
      mimeType: file.type,
      data,
      metadata: { name: file.name },
    };
  }

  // PDF
  return {
    type: "file",
    mimeType: "application/pdf",
    data,
    metadata: { filename: file.name },
  };
}

// Helper to convert File to base64 string
export async function fileToBase64(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result as string;
      // Remove the data:...;base64, prefix
      resolve(result.split(",")[1]);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Type guard for Base64ContentBlock
export function isBase64ContentBlock(
  block: unknown,
): block is ContentBlock.Multimodal.Data {
  if (typeof block !== "object" || block === null || !("type" in block))
    return false;
  // file type (legacy)
  if (
    (block as { type: unknown }).type === "file" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    ((block as { mimeType: string }).mimeType.startsWith("image/") ||
      (block as { mimeType: string }).mimeType === "application/pdf")
  ) {
    return true;
  }
  // image type (new)
  if (
    (block as { type: unknown }).type === "image" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    (block as { mimeType: string }).mimeType.startsWith("image/")
  ) {
    return true;
  }
  // GYT 方案 A:DXF 图纸 file 块。按 mimeType=image/vnd.dxf 或 filename .dxf 认,
  // 否则预览组件会当它「不支持」而不显示这张卡片。
  if ((block as { type: unknown }).type === "file") {
    const mime = (block as { mimeType?: unknown }).mimeType;
    const filename = (block as { metadata?: { filename?: unknown } }).metadata
      ?.filename;
    if (
      (typeof mime === "string" && mime === "image/vnd.dxf") ||
      (typeof filename === "string" && filename.toLowerCase().endsWith(".dxf"))
    ) {
      return true;
    }
  }
  return false;
}
