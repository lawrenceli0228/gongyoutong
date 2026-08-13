/**
 * 项目 / 图纸 / 资料上传面板(W7 CAD/knowledge §4)—— 视觉按「方案 B · 清爽卡片」重做。
 *
 * 这是本项目**新增**的组件(上游 agent-chat-ui 没有),由 scripts/setup-frontend.sh 直接拷进
 * frontend/src/components/thread/ProjectUploadPanel.tsx,并在 thread-index.tsx 里挂一次
 * <ProjectUploadPanel />。它渲染一个浮动触发按钮 + **从右侧滑出的「资料归档」抽屉**,和聊天输入框
 * 那个上传按钮是两回事:
 *   · 聊天上传按钮:当场传一张图让 agent 看一眼(临时,不归项目);
 *   · 本面板:把图纸/规范/任务书**正式归档到某个项目**(或全局规范)。
 *
 * 视觉 = 方案 B(浅灰绿底、纯白卡片、大圆角、单一绿 #0E9F6E),信息架构用 ①存到哪个工地 →
 * ②传什么 → ③信息 三步铺开,不再挤成一坨。**业务逻辑与端点契约一字未改**,只重排 UI。
 *
 * 打的后端端点(见 backend/webapp.py):
 *   GET  /projects · POST /projects · POST /projects/{id}/drawings
 *   POST /docs · POST /projects/{id}/docs
 * 鉴权复用 @/lib/api-key 的 getApiKey();基址走 NEXT_PUBLIC_API_URL。
 *
 * ⚠️ 未在无 Node 环境编译过:首次 apply 后在 WSL2/Node 里 pnpm dev 肉眼过一遍,修 TS/样式。
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
  { value: "plan", label: "平面" },
  { value: "elevation", label: "立面" },
  { value: "section", label: "剖面" },
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
  const [showNew, setShowNew] = useState(false);
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
        setShowNew(false);
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
        setViewType("");
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

  function pickDrawing(f: File | null) {
    setDwgFile(f);
    if (f) {
      setViewType(guessViewType(f.name));
      setTitle((prev) => prev || f.name.replace(/\.dxf$/i, ""));
    }
  }

  const currentName = projects.find((p) => p.id === projectId)?.name;

  // --- 样式片段(方案 B 调性)---------------------------------------------------
  const stepLabel = "mb-2.5 text-[13px] font-bold text-[#0E9F6E]";
  const fieldCls =
    "w-full rounded-[14px] border border-[#E4E8E6] bg-white px-4 py-3.5 text-[15px] text-[#33403A] placeholder:text-[#A2ABA6] focus:border-[#0E9F6E] focus:outline-none";
  const toggle = (active: boolean) =>
    "rounded-[12px] py-3.5 text-center text-[16px] font-bold transition " +
    (active
      ? "bg-[#0E9F6E] text-white"
      : "border border-[#E4E8E6] bg-white text-[#6B7772] hover:border-[#7FCDAE]");
  const bigChoice = (active: boolean) =>
    "rounded-[14px] p-4 text-center transition " +
    (active
      ? "border-2 border-[#0E9F6E] bg-[#EEF6F2]"
      : "border border-[#E4E8E6] bg-white hover:border-[#7FCDAE]");
  const archiveBtn =
    "rounded-[14px] bg-[#0E9F6E] py-4 text-[19px] font-black text-white transition hover:bg-[#0b7f58] disabled:opacity-50";

  return (
    <>
      {/* 触发按钮(暂放右下角浮动;等首页那块落地后挪进顶栏) */}
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-5 right-5 z-40 flex items-center gap-2 rounded-full bg-[#1B2420] px-5 py-3 text-[15px] font-bold text-white shadow-lg transition hover:bg-black"
        title="按项目归档图纸 / 规范 / 任务书"
      >
        📂 资料归档
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex justify-end bg-black/30"
          onClick={() => setOpen(false)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="flex h-full w-full max-w-[490px] flex-col bg-[#F7F9F8] shadow-2xl"
          >
            {/* 抽屉头 */}
            <div className="flex items-center gap-3 border-b border-[#EAEDEB] bg-white px-7 py-5">
              <div className="flex h-[34px] w-[34px] items-center justify-center rounded-[10px] bg-[#EEF6F2] text-lg">
                📂
              </div>
              <div>
                <div className="text-[19px] font-black text-[#1B2420]">资料归档</div>
                <div className="text-[13px] text-[#8A948F]">把图纸、规范正式存进项目</div>
              </div>
              <button
                onClick={() => setOpen(false)}
                className="ml-auto text-[22px] leading-none text-[#B4BDB8] hover:text-[#6B7772]"
              >
                ✕
              </button>
            </div>

            {/* 抽屉正文 */}
            <div className="flex flex-col gap-6 overflow-y-auto px-7 py-6">
              {/* ① 存到哪个工地 */}
              <section>
                <div className={stepLabel}>① 存到哪个工地</div>
                <div className="flex gap-2.5">
                  <select
                    value={projectId}
                    onChange={(e) => setProjectId(e.target.value)}
                    className={fieldCls + " font-bold text-[#1B2420]"}
                  >
                    <option value="">（未选 / 全局）</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}（{p.id}）
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={() => setShowNew((v) => !v)}
                    className="whitespace-nowrap rounded-[14px] border border-dashed border-[#7FCDAE] bg-[#EEF6F2] px-5 py-3.5 text-[16px] font-bold text-[#0E7A55]"
                  >
                    ＋ 新建
                  </button>
                </div>
                {showNew && (
                  <div className="mt-2.5 flex gap-2">
                    <input
                      className={fieldCls}
                      placeholder="新建项目名，如 幸福小区A3栋"
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                    />
                    <input
                      className="w-[90px] rounded-[14px] border border-[#E4E8E6] bg-white px-3 py-3.5 text-[15px]"
                      placeholder="短码"
                      value={newCode}
                      onChange={(e) => setNewCode(e.target.value)}
                    />
                    <button
                      onClick={createProject}
                      disabled={busy}
                      className="rounded-[14px] bg-[#0E9F6E] px-4 text-[15px] font-bold text-white disabled:opacity-50"
                    >
                      建
                    </button>
                  </div>
                )}
              </section>

              {/* ② 传什么 */}
              <section>
                <div className={stepLabel}>② 传什么</div>
                <div className="grid grid-cols-2 gap-2.5">
                  <button onClick={() => setTab("drawing")} className={bigChoice(tab === "drawing")}>
                    <div
                      className={
                        "text-[18px] font-black " +
                        (tab === "drawing" ? "text-[#0E7A55]" : "text-[#6B7772]")
                      }
                    >
                      📐 图纸
                    </div>
                    <div
                      className={
                        "mt-1 text-[13px] font-bold " +
                        (tab === "drawing" ? "text-[#5FAE8E]" : "text-[#A2ABA6]")
                      }
                    >
                      DXF 文件
                    </div>
                  </button>
                  <button onClick={() => setTab("doc")} className={bigChoice(tab === "doc")}>
                    <div
                      className={
                        "text-[18px] font-black " +
                        (tab === "doc" ? "text-[#0E7A55]" : "text-[#6B7772]")
                      }
                    >
                      📄 资料
                    </div>
                    <div
                      className={
                        "mt-1 text-[13px] font-bold " +
                        (tab === "doc" ? "text-[#5FAE8E]" : "text-[#A2ABA6]")
                      }
                    >
                      PDF 文件
                    </div>
                  </button>
                </div>
              </section>

              {/* ③ 信息 —— 随②切换 */}
              {tab === "drawing" ? (
                <section className="flex flex-col gap-3">
                  <div className={stepLabel + " mb-0"}>③ 图纸信息</div>
                  <label
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                      e.preventDefault();
                      pickDrawing(e.dataTransfer.files?.[0] ?? null);
                    }}
                    className="block cursor-pointer rounded-[16px] border-2 border-dashed border-[#C6D0CB] bg-white px-6 py-6 text-center text-[16px] text-[#8A948F]"
                  >
                    <input
                      type="file"
                      accept=".dxf"
                      className="hidden"
                      onChange={(e) => pickDrawing(e.target.files?.[0] ?? null)}
                    />
                    {dwgFile ? (
                      <span className="font-bold text-[#1B2420]">{dwgFile.name}</span>
                    ) : (
                      <>
                        拖入 .dxf,或 <span className="font-bold text-[#0E9F6E]">点击选择</span>
                      </>
                    )}
                  </label>

                  <div className="text-[14px] font-semibold text-[#6B7772]">这是哪种图?</div>
                  <div className="grid grid-cols-3 gap-2">
                    {VIEW_OPTIONS.map((o) => (
                      <button
                        key={o.value}
                        onClick={() => setViewType(o.value)}
                        className={toggle(viewType === o.value)}
                      >
                        {o.label}
                      </button>
                    ))}
                  </div>

                  <div className="flex gap-2.5">
                    <input
                      className={fieldCls}
                      placeholder="图名(留空=文件名)"
                      value={title}
                      onChange={(e) => setTitle(e.target.value)}
                    />
                    <input
                      className="w-[130px] rounded-[14px] border border-[#E4E8E6] bg-white px-4 py-3.5 text-[15px] placeholder:text-[#A2ABA6]"
                      placeholder="楼层·可选"
                      value={floor}
                      onChange={(e) => setFloor(e.target.value)}
                    />
                  </div>

                  <button onClick={uploadDrawing} disabled={busy} className={archiveBtn}>
                    {busy ? "上传中…" : `归档到 ${currentName ?? "…先选项目"}`}
                  </button>
                </section>
              ) : (
                <section className="flex flex-col gap-3">
                  <div className={stepLabel + " mb-0"}>③ 资料信息</div>

                  <div className="text-[14px] font-semibold text-[#6B7772]">作用域</div>
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => setDocScope("global")}
                      className={bigChoice(docScope === "global")}
                    >
                      <div className="text-[16px] font-black text-[#1B2420]">全局规范</div>
                      <div className="mt-1 text-[12px] font-bold text-[#8A948F]">所有项目通用</div>
                    </button>
                    <button
                      onClick={() => setDocScope("project")}
                      className={bigChoice(docScope === "project")}
                    >
                      <div className="text-[16px] font-black text-[#1B2420]">本项目</div>
                      <div className="mt-1 text-[12px] font-bold text-[#8A948F]">
                        {currentName ?? "先选项目"}
                      </div>
                    </button>
                  </div>

                  {docScope === "project" && (
                    <div className="grid grid-cols-2 gap-2">
                      <button
                        onClick={() => setDocType("regulation")}
                        className={toggle(docType === "regulation")}
                      >
                        规范
                      </button>
                      <button
                        onClick={() => setDocType("task_book")}
                        className={toggle(docType === "task_book")}
                      >
                        任务书
                      </button>
                    </div>
                  )}

                  <label
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                      e.preventDefault();
                      setDocFile(e.dataTransfer.files?.[0] ?? null);
                    }}
                    className="block cursor-pointer rounded-[16px] border-2 border-dashed border-[#C6D0CB] bg-white px-6 py-6 text-center text-[16px] text-[#8A948F]"
                  >
                    <input
                      type="file"
                      accept=".pdf"
                      className="hidden"
                      onChange={(e) => setDocFile(e.target.files?.[0] ?? null)}
                    />
                    {docFile ? (
                      <span className="font-bold text-[#1B2420]">{docFile.name}</span>
                    ) : (
                      <>
                        拖入 .pdf,或 <span className="font-bold text-[#0E9F6E]">点击选择</span>
                      </>
                    )}
                  </label>

                  <button onClick={uploadDoc} disabled={busy} className={archiveBtn}>
                    {busy ? "上传中…" : docScope === "global" ? "归档到 全局规范" : "归档到本项目"}
                  </button>
                </section>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
