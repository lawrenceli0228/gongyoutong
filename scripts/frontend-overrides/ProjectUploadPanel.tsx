/**
 * 项目 / 图纸 / 资料上传面板(W7 CAD/knowledge §4)—— 视觉按「方案 B · 清爽卡片」重做。
 *
 * 这是本项目**新增**的组件(上游 agent-chat-ui 没有),由 scripts/setup-frontend.sh 直接拷进
 * frontend/src/components/thread/ProjectUploadPanel.tsx。它和聊天输入框那个上传按钮是两回事:
 *   · 聊天上传按钮:当场传一张图让 agent 看一眼(临时,不归项目);
 *   · 本面板:把图纸/规范/任务书**正式归档到某个项目**(或全局规范)。
 *
 * (顶栏化改造)归档入口从右下角浮动按钮**上移到顶栏**:一个「当前工地」chip + 一个
 *   「📂 资料归档」按钮(<ArchiveHeaderControls />),对应设计稿方案 B 的顶栏右侧。开合状态与
 *   项目列表经 <ArchiveProvider> 共享 —— thread-index.tsx 里最外层包一层 <ArchiveProvider>,
 *   顶栏渲染 <ArchiveHeaderControls />;抽屉由 Provider 自己挂,无需再手动 <ProjectUploadPanel/>。
 *   chip 显示的是**真实的当前项目名**(首屏静默拉一次 /projects;没有项目就显示「未选工地」),
 *   不写死设计稿里的示例名。
 *
 * 视觉 = 方案 B(浅灰绿底、纯白卡片、大圆角、单一绿 #0E9F6E),信息架构用 ①存到哪个工地 →
 * ②传什么 → ③信息 三步铺开。**业务逻辑与端点契约一字未改**,只重排 UI + 抬升入口。
 *
 * 打的后端端点(见 backend/webapp.py):
 *   GET  /projects · POST /projects · POST /projects/{id}/drawings
 *   POST /docs · POST /projects/{id}/docs
 * 鉴权复用 @/lib/api-key 的 getApiKey();基址走 NEXT_PUBLIC_API_URL。
 *
 * ⚠️ 未在无 Node 环境编译过:首次 apply 后在 WSL2/Node 里 pnpm dev 肉眼过一遍,修 TS/样式。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
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

// --- 共享状态:开合 + 项目列表 + 当前工地 --------------------------------------
type ArchiveContextValue = {
  open: boolean;
  setOpen: (v: boolean) => void;
  projects: Project[];
  projectId: string;
  setProjectId: (v: string) => void;
  currentName?: string;
  reloadProjects: (silent?: boolean) => Promise<void>;
};

const ArchiveContext = createContext<ArchiveContextValue | null>(null);

function useArchive(): ArchiveContextValue {
  const ctx = useContext(ArchiveContext);
  if (!ctx) throw new Error("useArchive 必须在 <ArchiveProvider> 内使用");
  return ctx;
}

/** 归档面板的状态容器:把「开合 + 项目列表 + 当前工地」抬到顶栏与抽屉共用。 */
export function ArchiveProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");

  const reloadProjects = useCallback(async (silent = false) => {
    try {
      const resp = await fetch(`${API_URL}/projects`, { headers: authHeaders() });
      const env = await readEnvelope(resp);
      const list: Project[] = env?.data?.projects ?? [];
      setProjects(list);
      setProjectId((prev) => prev || (list[0]?.id ?? ""));
    } catch {
      if (!silent) toast.error("拉取项目列表失败,后端起了吗?");
    }
  }, []);

  // 首屏静默拉一次(后端没起也不弹错,让顶栏 chip 有内容);打开抽屉时再拉一次(带提示)。
  useEffect(() => {
    void reloadProjects(true);
  }, [reloadProjects]);
  useEffect(() => {
    if (open) void reloadProjects();
  }, [open, reloadProjects]);

  const currentName = projects.find((p) => p.id === projectId)?.name;

  return (
    <ArchiveContext.Provider
      value={{ open, setOpen, projects, projectId, setProjectId, currentName, reloadProjects }}
    >
      {children}
      <ArchiveDrawer />
    </ArchiveContext.Provider>
  );
}

/** 读当前选中的工地项目编号(供聊天提交时经 config.configurable 注入,让规范问答自动限定作用域)。
 *  必须在 <ArchiveProvider> 之内调用;没选项目时为空串。 */
export function useCurrentProjectId(): string {
  return useArchive().projectId;
}

/** 顶栏归档入口:资料库(浏览全部)+ 当前工地 chip +「📂 资料归档」按钮(方案 B 顶栏右侧)。 */
export function ArchiveHeaderControls() {
  const { setOpen, currentName } = useArchive();
  return (
    <div className="flex items-center gap-2.5">
      <LibraryButton />
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-full bg-[#EEF6F2] px-4 py-2 text-[14px] font-bold text-[#0E7A55] transition hover:bg-[#E2F0EA]"
        title="当前工地 · 点开可切换或归档"
      >
        <span className="h-2 w-2 rounded-full bg-[#0E9F6E]" />
        {currentName ?? "未选工地"}
      </button>
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-full bg-[#1B2420] px-4 py-2.5 text-[14px] font-bold text-white transition hover:bg-black"
        title="按项目归档图纸 / 规范 / 任务书"
      >
        📂 资料归档
      </button>
    </div>
  );
}

// --- 抽屉本体 ------------------------------------------------------------------
function ArchiveDrawer() {
  const { open, setOpen, projects, projectId, setProjectId, currentName, reloadProjects } =
    useArchive();

  const [tab, setTab] = useState<"drawing" | "doc">("drawing");
  const [busy, setBusy] = useState(false);
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
        await reloadProjects();
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

  if (!open) return null;

  return (
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
  );
}

// --- 资料库:只读浏览所有项目的图纸 + 规范(GET /library)------------------------
type LibDrawing = {
  drawing_id: number;
  project_id: string;
  project_name: string | null;
  title: string;
  view_type: string;
  floor: string | null;
  rel_path: string | null;
  artifact_id: string;
  created_at: string;
};
type LibDoc = {
  scope: string;
  project_id: string | null;
  project_name: string | null;
  doc_type: string;
  filename: string;
  rel_path: string;
  size_bytes: number;
  modified_at: string;
};
type LibraryData = {
  projects: { id: string; name: string; code: string | null }[];
  drawings: LibDrawing[];
  docs: LibDoc[];
};

const VIEW_LABEL: Record<string, string> = { plan: "平面", elevation: "立面", section: "剖面" };
const DOC_LABEL: Record<string, string> = { regulation: "规范", task_book: "任务书" };

function fmtSize(n: number): string {
  if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
  if (n >= 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}

/** 统一的 DELETE 调用:成功/失败都弹 toast,返回是否成功(供调用方决定要不要刷新)。 */
async function apiDelete(path: string, body?: unknown): Promise<boolean> {
  try {
    const resp = await fetch(`${API_URL}${path}`, {
      method: "DELETE",
      headers: body ? { "Content-Type": "application/json", ...authHeaders() } : authHeaders(),
      body: body ? JSON.stringify(body) : undefined,
    });
    const env = await readEnvelope(resp);
    if (resp.ok) {
      toast.success(env?.user_msg ?? "已删除");
      return true;
    }
    toast.error(env?.user_msg ?? "删除失败");
    return false;
  } catch {
    toast.error("删除失败,后端起了吗?");
    return false;
  }
}

/** 行内两步删除:点「删除」→ 变「确认删除 / 取消」→ 确认后跑 onDelete。不弹浏览器原生框。 */
function RowDelete({
  label = "删除",
  confirmLabel = "确认删除",
  onDelete,
}: {
  label?: string;
  confirmLabel?: string;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  if (busy) return <span className="text-[12px] text-[#B4BDB8]">删除中…</span>;
  if (confirming) {
    return (
      <span className="flex items-center gap-2 text-[12px]">
        <button
          onClick={async () => {
            setBusy(true);
            try {
              await onDelete();
            } finally {
              setBusy(false);
              setConfirming(false);
            }
          }}
          className="font-bold text-[#C0392B] hover:underline"
        >
          {confirmLabel}
        </button>
        <button
          onClick={() => setConfirming(false)}
          className="text-[#8A948F] hover:underline"
        >
          取消
        </button>
      </span>
    );
  }
  return (
    <button
      onClick={() => setConfirming(true)}
      className="text-[12px] font-bold text-[#B4BDB8] transition hover:text-[#C0392B]"
      title={label}
    >
      {label}
    </button>
  );
}

/** 顶栏「📚 资料库」入口 + 从右侧滑出的只读浏览抽屉。自带开合与拉取状态,不依赖归档面板。 */
export function LibraryButton() {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [data, setData] = useState<LibraryData | null>(null);
  const [loading, setLoading] = useState(false);

  // 抽屉走 portal 挂到 document.body:顶栏在 framer-motion 的 transform 子树里,
  // fixed 定位会被 transform 祖先"锚住"而错位/被 overflow-hidden 裁掉,portal 出去才铺满视口。
  useEffect(() => setMounted(true), []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch(`${API_URL}/library`, { headers: authHeaders() });
      const env = await readEnvelope(resp);
      if (resp.ok && env?.data) setData(env.data as LibraryData);
      else toast.error(env?.user_msg ?? "拉取资料库失败");
    } catch {
      toast.error("拉取资料库失败,后端起了吗?");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const drawer = (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/30"
      onClick={() => setOpen(false)}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex h-full w-full max-w-[560px] flex-col bg-[#F7F9F8] shadow-2xl"
      >
        <div className="flex items-center gap-3 border-b border-[#EAEDEB] bg-white px-7 py-5">
          <div className="flex h-[34px] w-[34px] items-center justify-center rounded-[10px] bg-[#EEF6F2] text-lg">
            📚
          </div>
          <div>
            <div className="text-[19px] font-black text-[#1B2420]">资料库</div>
            <div className="text-[13px] text-[#8A948F]">所有项目的图纸与规范</div>
          </div>
          <button
            onClick={() => void load()}
            className="ml-auto rounded-[10px] border border-[#E4E8E6] px-3 py-1.5 text-[13px] font-bold text-[#6B7772] transition hover:border-[#7FCDAE]"
            title="刷新"
          >
            刷新
          </button>
          <button
            onClick={() => setOpen(false)}
            className="text-[22px] leading-none text-[#B4BDB8] hover:text-[#6B7772]"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-5 overflow-y-auto px-7 py-6">
          {loading && (
            <div className="py-10 text-center text-[15px] text-[#8A948F]">加载中…</div>
          )}
          {!loading && data && <LibraryBody data={data} reload={load} />}
        </div>
      </div>
    </div>
  );

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-full border border-[#E4E8E6] bg-white px-4 py-2 text-[14px] font-bold text-[#33403A] transition hover:border-[#7FCDAE]"
        title="查看所有项目的图纸与规范"
      >
        📚 资料库
      </button>
      {open && mounted && createPortal(drawer, document.body)}
    </>
  );
}

function LibraryBody({ data, reload }: { data: LibraryData; reload: () => Promise<void> }) {
  const globalDocs = data.docs.filter((d) => d.scope === "global");
  const empty = data.drawings.length === 0 && data.docs.length === 0;

  if (empty) {
    return (
      <div className="rounded-[16px] border border-dashed border-[#C6D0CB] bg-white px-6 py-12 text-center text-[15px] leading-relaxed text-[#8A948F]">
        还没有任何图纸或资料。
        <br />
        点右上角「📂 资料归档」传第一份吧。
      </div>
    );
  }

  return (
    <>
      {globalDocs.length > 0 && (
        <LibrarySection title="全局规范" hint="所有项目通用" count={globalDocs.length}>
          {globalDocs.map((d) => (
            <DocRow key={d.rel_path} doc={d} reload={reload} />
          ))}
        </LibrarySection>
      )}
      {data.projects.map((p) => {
        const dwgs = data.drawings.filter((x) => x.project_id === p.id);
        const docs = data.docs.filter((x) => x.scope === "project" && x.project_id === p.id);
        return (
          <LibrarySection
            key={p.id}
            title={p.name}
            hint={`编号 ${p.id}`}
            count={dwgs.length + docs.length}
            action={
              <RowDelete
                label="删除项目"
                confirmLabel="确认删除项目"
                onDelete={async () => {
                  if (await apiDelete(`/projects/${encodeURIComponent(p.id)}`)) await reload();
                }}
              />
            }
          >
            {dwgs.length === 0 && docs.length === 0 ? (
              <div className="px-1 py-1.5 text-[13px] text-[#A2ABA6]">（暂无图纸或资料)</div>
            ) : (
              <>
                {dwgs.map((d) => (
                  <DrawingRow key={d.drawing_id} dwg={d} reload={reload} />
                ))}
                {docs.map((d) => (
                  <DocRow key={d.rel_path} doc={d} reload={reload} />
                ))}
              </>
            )}
          </LibrarySection>
        );
      })}
    </>
  );
}

function LibrarySection({
  title,
  hint,
  count,
  action,
  children,
}: {
  title: string;
  hint: string;
  count: number;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="mb-2 flex items-baseline gap-2">
        <div className="text-[15px] font-black text-[#1B2420]">{title}</div>
        <div className="text-[12px] text-[#A2ABA6]">{hint}</div>
        <div className="ml-auto flex items-baseline gap-3">
          {action}
          <div className="rounded-full bg-[#EEF6F2] px-2.5 py-0.5 text-[12px] font-bold text-[#0E7A55]">
            {count}
          </div>
        </div>
      </div>
      <div className="flex flex-col gap-2">{children}</div>
    </section>
  );
}

function DrawingRow({ dwg, reload }: { dwg: LibDrawing; reload: () => Promise<void> }) {
  return (
    <div className="flex items-center gap-3 rounded-[14px] border border-[#EAEDEB] bg-white px-4 py-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-[#F1F4F3] text-lg">
        📐
      </div>
      <div className="min-w-0">
        <div className="truncate text-[15px] font-bold text-[#1B2420]">{dwg.title}</div>
        <div className="text-[12px] text-[#8A948F]">图纸{dwg.floor ? ` · ${dwg.floor}` : ""}</div>
      </div>
      <span className="ml-auto shrink-0 rounded-full bg-[#EEF6F2] px-2.5 py-1 text-[12px] font-bold text-[#0E7A55]">
        {VIEW_LABEL[dwg.view_type] ?? dwg.view_type}
      </span>
      <RowDelete
        onDelete={async () => {
          if (
            await apiDelete(
              `/projects/${encodeURIComponent(dwg.project_id)}/drawings/${dwg.drawing_id}`,
            )
          )
            await reload();
        }}
      />
    </div>
  );
}

function DocRow({ doc, reload }: { doc: LibDoc; reload: () => Promise<void> }) {
  return (
    <div className="flex items-center gap-3 rounded-[14px] border border-[#EAEDEB] bg-white px-4 py-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-[#F1F4F3] text-lg">
        📄
      </div>
      <div className="min-w-0">
        <div className="truncate text-[15px] font-bold text-[#1B2420]">{doc.filename}</div>
        <div className="text-[12px] text-[#8A948F]">
          {DOC_LABEL[doc.doc_type] ?? doc.doc_type} · {fmtSize(doc.size_bytes)}
        </div>
      </div>
      <RowDelete
        onDelete={async () => {
          const base =
            doc.scope === "global"
              ? "/docs"
              : `/projects/${encodeURIComponent(doc.project_id ?? "")}/docs`;
          if (await apiDelete(base, { doc_type: doc.doc_type, filename: doc.filename }))
            await reload();
        }}
      />
    </div>
  );
}
