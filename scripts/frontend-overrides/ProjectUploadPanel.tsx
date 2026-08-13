/**
 * 项目 / 图纸 / 资料上传面板(W7 CAD/knowledge §4)。
 *
 * 这是本项目**新增**的组件(上游 agent-chat-ui 没有),由 scripts/setup-frontend.sh 直接拷进
 * frontend/src/components/thread/ProjectUploadPanel.tsx,并在 thread-index.tsx 里挂一次
 * <ProjectUploadPanel />。它渲染一个右下角浮动按钮 + 弹窗,和聊天输入框那个上传按钮是两回事:
 *   · 聊天上传按钮:当场传一张图让 agent 看一眼(临时,不归项目);
 *   · 本面板:把图纸/规范/任务书**正式归档到某个项目**(或全局规范),走后端 webapp.py 的端点。
 *
 * 打的后端端点(见 backend/webapp.py):
 *   GET  /projects                       列项目
 *   POST /projects                       建项目(JSON)
 *   POST /projects/{id}/drawings         传 DXF 图纸(multipart:file/view_type/floor?/title?)
 *   POST /docs                           传全局规范(multipart:file/doc_type=regulation)
 *   POST /projects/{id}/docs             传项目规范/任务书(multipart:file/doc_type)
 *
 * 鉴权:复用 @/lib/api-key 的 getApiKey()(公网部署时令牌构建期注入;本机无令牌则不发头,
 * 与后端 enable_custom_route_auth 的「没配令牌就放行」一致)。基址走 NEXT_PUBLIC_API_URL。
 *
 * ⚠️ 未在无 Node 环境跑过(方案 §4 说明):首次 apply 后请在 WSL2/Node 里 pnpm dev 肉眼过一遍,
 *    尤其是 import 路径与 Tailwind 类;有 TS/样式问题就地调,逻辑与端点契约已对齐后端。
 */

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { getApiKey } from "@/lib/api-key";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:2024";

type Project = { id: string; name: string; code: string | null };
type ViewType = "plan" | "elevation" | "section";
type DocScope = "global" | "project";
type DocType = "regulation" | "task_book";

const VIEW_OPTIONS: { value: ViewType; label: string }[] = [
  { value: "plan", label: "平面图" },
  { value: "elevation", label: "立面图" },
  { value: "section", label: "剖面图" },
];

function authHeaders(): Record<string, string> {
  const key = getApiKey();
  return key ? { "X-Api-Key": key } : {};
}

async function readEnvelope(resp: Response): Promise<{ user_msg?: string; data?: any } | null> {
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

/** 按文件名关键词预判平立剖,让用户确认/改,不猜错也不逼他每次手选。 */
function guessViewType(filename: string): ViewType | "" {
  const n = filename.toLowerCase();
  if (n.includes("平面") || n.includes("plan")) return "plan";
  if (n.includes("立面") || n.includes("elev")) return "elevation";
  if (n.includes("剖面") || n.includes("剖") || n.includes("section")) return "section";
  return "";
}

export function ProjectUploadPanel() {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"drawing" | "doc">("drawing");
  const [busy, setBusy] = useState(false);

  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");

  const [newName, setNewName] = useState("");
  const [newCode, setNewCode] = useState("");

  const [dwgFile, setDwgFile] = useState<File | null>(null);
  const [viewType, setViewType] = useState<ViewType | "">("");
  const [floor, setFloor] = useState("");
  const [title, setTitle] = useState("");

  const [docScope, setDocScope] = useState<DocScope>("global");
  const [docType, setDocType] = useState<DocType>("regulation");
  const [docFile, setDocFile] = useState<File | null>(null);

  const loadProjects = useCallback(async () => {
    try {
      const resp = await fetch(`${API_URL}/projects`, { headers: authHeaders() });
      const env = await readEnvelope(resp);
      const list: Project[] = env?.data?.projects ?? [];
      setProjects(list);
      setProjectId((prev) => prev || (list[0]?.id ?? ""));
    } catch {
      toast.error("拉取项目列表失败,后端起了吗?");
    }
  }, []);

  useEffect(() => {
    if (open) loadProjects();
  }, [open, loadProjects]);

  // 全局作用域只收规范:切到全局就把类型钉死成规范。
  useEffect(() => {
    if (docScope === "global") setDocType("regulation");
  }, [docScope]);

  async function createProject() {
    if (!newName.trim()) {
      toast.error("项目名不能为空");
      return;
    }
    setBusy(true);
    try {
      const resp = await fetch(`${API_URL}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: newName.trim(), code: newCode.trim() || undefined }),
      });
      const env = await readEnvelope(resp);
      if (resp.ok) {
        toast.success(env?.user_msg ?? "项目建好了");
        setNewName("");
        setNewCode("");
        await loadProjects();
        if (env?.data?.id) setProjectId(env.data.id);
      } else {
        toast.error(env?.user_msg ?? "建项目失败");
      }
    } finally {
      setBusy(false);
    }
  }

  async function uploadDrawing() {
    if (!dwgFile) return toast.error("请选一张 .dxf 图纸");
    if (!projectId) return toast.error("先选或建一个项目");
    if (!viewType) return toast.error("请选平面 / 立面 / 剖面");
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", dwgFile);
      fd.append("view_type", viewType);
      if (floor.trim()) fd.append("floor", floor.trim());
      fd.append("title", title.trim() || dwgFile.name.replace(/\.dxf$/i, ""));
      const resp = await fetch(
        `${API_URL}/projects/${encodeURIComponent(projectId)}/drawings`,
        { method: "POST", headers: authHeaders(), body: fd },
      );
      const env = await readEnvelope(resp);
      if (resp.ok) {
        toast.success(env?.user_msg ?? "图纸上传成功");
        setDwgFile(null);
        setTitle("");
        setFloor("");
      } else {
        toast.error(env?.user_msg ?? "图纸上传失败");
      }
    } finally {
      setBusy(false);
    }
  }

  async function uploadDoc() {
    if (!docFile) return toast.error("请选一份 PDF");
    if (docScope === "project" && !projectId) return toast.error("项目资料要先选项目");
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", docFile);
      fd.append("doc_type", docType);
      const url =
        docScope === "global"
          ? `${API_URL}/docs`
          : `${API_URL}/projects/${encodeURIComponent(projectId)}/docs`;
      const resp = await fetch(url, { method: "POST", headers: authHeaders(), body: fd });
      const env = await readEnvelope(resp);
      if (resp.ok) {
        toast.success(env?.user_msg ?? "资料上传成功");
        setDocFile(null);
      } else {
        toast.error(env?.user_msg ?? "资料上传失败");
      }
    } finally {
      setBusy(false);
    }
  }

  const inputCls =
    "w-full rounded border border-gray-300 px-2 py-1 text-sm focus:border-gray-500 focus:outline-none";
  const btnCls =
    "rounded bg-black px-3 py-1.5 text-sm text-white hover:bg-gray-800 disabled:opacity-50";

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-4 left-4 z-40 rounded-full bg-black px-4 py-2 text-sm text-white shadow-lg hover:bg-gray-800"
        title="按项目上传图纸 / 规范 / 任务书"
      >
        📁 图纸 / 资料
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => setOpen(false)}
        >
          <div
            className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg bg-white p-5 text-gray-900 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">图纸 / 资料管理</h2>
              <button onClick={() => setOpen(false)} className="text-gray-400 hover:text-gray-700">
                ✕
              </button>
            </div>

            {/* 项目下拉 + 新建 */}
            <div className="mb-4 rounded border border-gray-200 p-3">
              <label className="mb-1 block text-xs text-gray-500">项目</label>
              <select
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
                className={inputCls}
              >
                <option value="">（未选 / 全局）</option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}（{p.id}）
                  </option>
                ))}
              </select>
              <div className="mt-2 flex gap-2">
                <input
                  className={inputCls}
                  placeholder="新建项目名，如 幸福小区A3栋"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                />
                <input
                  className="w-24 rounded border border-gray-300 px-2 py-1 text-sm"
                  placeholder="短码"
                  value={newCode}
                  onChange={(e) => setNewCode(e.target.value)}
                />
                <button className={btnCls} disabled={busy} onClick={createProject}>
                  新建
                </button>
              </div>
            </div>

            {/* 页签 */}
            <div className="mb-3 flex gap-2 border-b border-gray-200">
              {(["drawing", "doc"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={
                    "px-3 py-1.5 text-sm " +
                    (tab === t ? "border-b-2 border-black font-medium" : "text-gray-500")
                  }
                >
                  {t === "drawing" ? "图纸（DXF）" : "规范 / 任务书（PDF）"}
                </button>
              ))}
            </div>

            {tab === "drawing" ? (
              <div className="space-y-2">
                <input
                  type="file"
                  accept=".dxf"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null;
                    setDwgFile(f);
                    if (f) {
                      setViewType(guessViewType(f.name));
                      setTitle((prev) => prev || f.name.replace(/\.dxf$/i, ""));
                    }
                  }}
                  className="text-sm"
                />
                <div className="flex gap-2">
                  {VIEW_OPTIONS.map((o) => (
                    <label key={o.value} className="flex items-center gap-1 text-sm">
                      <input
                        type="radio"
                        name="view_type"
                        checked={viewType === o.value}
                        onChange={() => setViewType(o.value)}
                      />
                      {o.label}
                    </label>
                  ))}
                </div>
                <input
                  className={inputCls}
                  placeholder="图纸名（留空=文件名）"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
                <input
                  className={inputCls}
                  placeholder="楼层（可选，如 1F / 标准层）"
                  value={floor}
                  onChange={(e) => setFloor(e.target.value)}
                />
                <button className={btnCls} disabled={busy} onClick={uploadDrawing}>
                  {busy ? "上传中…" : "上传图纸"}
                </button>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex gap-3 text-sm">
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="scope"
                      checked={docScope === "global"}
                      onChange={() => setDocScope("global")}
                    />
                    全局规范（所有项目通用）
                  </label>
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="scope"
                      checked={docScope === "project"}
                      onChange={() => setDocScope("project")}
                    />
                    本项目
                  </label>
                </div>
                {docScope === "project" && (
                  <div className="flex gap-3 text-sm">
                    <label className="flex items-center gap-1">
                      <input
                        type="radio"
                        name="doc_type"
                        checked={docType === "regulation"}
                        onChange={() => setDocType("regulation")}
                      />
                      规范
                    </label>
                    <label className="flex items-center gap-1">
                      <input
                        type="radio"
                        name="doc_type"
                        checked={docType === "task_book"}
                        onChange={() => setDocType("task_book")}
                      />
                      任务书
                    </label>
                  </div>
                )}
                <input
                  type="file"
                  accept=".pdf"
                  onChange={(e) => setDocFile(e.target.files?.[0] ?? null)}
                  className="text-sm"
                />
                <button className={btnCls} disabled={busy} onClick={uploadDoc}>
                  {busy ? "上传中…" : "上传资料"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
