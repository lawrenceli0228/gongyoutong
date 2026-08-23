/** CAD 预览工具结果的纯解析器。保持零依赖,供聊天渲染与独立 vitest 共用。 */

export type CadPreviewData = {
  png_id: string;
  format?: string;
  page_count?: number;
};

export type CapturedCadPreview = {
  messageId: string;
  data: CadPreviewData;
};

const ARTIFACT_ID_RE = /^[0-9a-f]{32}$/;

/**
 * CAD 预览复用现有 LangGraph HTTP 服务，不再把本地 8788 变成必需进程。
 * apiBase 由 NEXT_PUBLIC_API_URL 传入；函数保持零依赖，方便独立 vitest 锁住地址契约。
 */
export function cadPreviewFileUrl(apiBase: string, pngId: string): string | null {
  if (!ARTIFACT_ID_RE.test(pngId)) return null;
  const base = String(apiBase || "http://localhost:2024").replace(/\/+$/, "");
  return `${base}/files/cad-preview/${pngId}`;
}

/**
 * 子 Agent 以 output_mode=last_message 交回后,内部 ToolMessage 不会留在最终线程状态；
 * 但流式运行期间消息会短暂到达前端。这里严格认成功信封并提取 32 位 PNG 产物编号,
 * 由调用方立即缓存,避免工具结果在收工后消失时预览也跟着消失。
 */
export function captureCadPreview(message: unknown): CapturedCadPreview | null {
  const msg = message as {
    id?: unknown;
    type?: unknown;
    name?: unknown;
    content?: unknown;
  };
  if (
    msg?.type !== "tool" ||
    msg.name !== "render_preview" ||
    typeof msg.id !== "string" ||
    typeof msg.content !== "string"
  ) {
    return null;
  }

  let envelope: any;
  try {
    envelope = JSON.parse(msg.content);
  } catch {
    return null;
  }
  const pngId = envelope?.ok === true ? envelope?.data?.png_id : null;
  if (typeof pngId !== "string" || !ARTIFACT_ID_RE.test(pngId)) return null;

  return {
    messageId: msg.id,
    data: {
      png_id: pngId,
      ...(typeof envelope.data.format === "string" && {
        format: envelope.data.format,
      }),
      ...(typeof envelope.data.page_count === "number" && {
        page_count: envelope.data.page_count,
      }),
    },
  };
}
