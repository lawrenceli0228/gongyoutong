import { describe, expect, it } from "vitest";

import { cadPreviewFileUrl, captureCadPreview } from "../frontend-overrides/cad-preview-lib";

const PNG_ID = "0123456789abcdef0123456789abcdef";

describe("captureCadPreview", () => {
  it("从成功的 render_preview 信封提取预览", () => {
    expect(
      captureCadPreview({
        id: "tool-1",
        type: "tool",
        name: "render_preview",
        content: JSON.stringify({
          ok: true,
          data: { png_id: PNG_ID, format: "pdf", page_count: 3 },
        }),
      }),
    ).toEqual({
      messageId: "tool-1",
      data: { png_id: PNG_ID, format: "pdf", page_count: 3 },
    });
  });

  it.each([
    { id: "tool-2", type: "tool", name: "render_preview", content: "not-json" },
    {
      id: "tool-3",
      type: "tool",
      name: "render_preview",
      content: JSON.stringify({ ok: false, data: { png_id: PNG_ID } }),
    },
    {
      id: "tool-4",
      type: "tool",
      name: "render_preview",
      content: JSON.stringify({
        ok: true,
        data: { png_id: "not-an-artifact-id" },
      }),
    },
    {
      id: "tool-5",
      type: "tool",
      name: "query_dimension",
      content: JSON.stringify({ ok: true, data: { png_id: PNG_ID } }),
    },
  ])("忽略失败、坏信封、坏编号和其他工具 %#", (message) => {
    expect(captureCadPreview(message)).toBeNull();
  });
});

describe("cadPreviewFileUrl", () => {
  it("复用现有后端 2024 文件端点，不再依赖 8788", () => {
    expect(cadPreviewFileUrl("http://localhost:2024/", PNG_ID)).toBe(
      `http://localhost:2024/files/cad-preview/${PNG_ID}`,
    );
  });

  it("拒绝非法产物编号", () => {
    expect(cadPreviewFileUrl("http://localhost:2024", "../preview.png")).toBeNull();
  });
});
