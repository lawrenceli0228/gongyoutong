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
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";

import { getApiKey } from "@/lib/api-key";
// 后端工具结果经 values 流到达这里,两处消费它(都在 <StreamProvider> 之内,能安全读流):
//   · ProjectSwitchSync —— switch_project 的结果落成「自然语言切换的工地」;
//   · FilePreviewSync   —— open_drawing / open_document / export_drawing_pdf / download_*
//     的结果自动弹预览 / 触发下载(自然语言操作文件的闭环)。
import { useStreamContext } from "@/providers/Stream";
// 界面恒繁體(负责人 2026-08-18 定案),但本文件**一个转换器都不用**,是刻意的:
//   · 静态文案(按钮 / 占位符 / toast 兜底句)——**源码里直接写繁體**,零运行时;
//   · 后端回来的 `user_msg` —— **一律不转**(W12 复审定案,理由见 createProject 那处)。
// 两条都不需要转换器,所以连 438 KB 的字典都不该被这个面板拉起来。
// ⚠️ 本文件导出的顶栏三件(ProjectSwitcher / LibraryButton /「📂 资料归档」)是**常驻**的 ——
//    以后真要加运行时转换,先回答「它在用户没点任何东西的时候会不会被挂上」:
//    会 = 每个用户首屏都拉字典,包括从不开面板的简体工友。那条承诺是实测过的。
import { cn } from "@/lib/utils";

// 记住上次选中的工地(localStorage 键)。刻意不自动默认第一个项目 —— 那是「你在项目2、
// 它却按项目1答」的坑;改成「记住上次选的,没选就问全局」。
const PROJECT_STORAGE_KEY = "gyt_current_project";

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

// --- 文件预览 / 下载(带鉴权头 fetch 成 Blob;绝不 window.open)---------------------
// API Key 在请求头里,裸 URL 拿不到 —— 所以不能直接 window.open / <img src>,
// 一律 fetch 成 Blob 再 createObjectURL:预览用 <iframe>,下载用临时 <a download>,关闭时 revoke。

type DocRef = { scope: string; doc_type: string; filename: string; project_id?: string | null };
type Disposition = "inline" | "attachment";

function drawingFileUrl(
  artifactId: string,
  opts: { format: "original" | "pdf"; disposition: Disposition },
): string {
  const q = new URLSearchParams({ format: opts.format, disposition: opts.disposition });
  return `${API_URL}/files/drawing/${encodeURIComponent(artifactId)}?${q.toString()}`;
}

function docFileUrl(doc: DocRef, disposition: Disposition): string {
  const q = new URLSearchParams({
    scope: doc.scope,
    doc_type: doc.doc_type,
    filename: doc.filename,
    disposition,
  });
  if (doc.project_id) q.set("project_id", doc.project_id);
  return `${API_URL}/files/doc?${q.toString()}`;
}

/** 带鉴权头取一份文件成 Blob;失败弹后端人话(user_msg 不转,与本文件同口径)。 */
async function authFetchBlob(url: string): Promise<Blob | null> {
  try {
    const resp = await fetch(url, { headers: authHeaders() });
    if (!resp.ok) {
      const env = await readEnvelope(resp);
      toast.error(env?.user_msg ?? "打不開這個文件");
      return null;
    }
    return await resp.blob();
  } catch {
    toast.error("取文件失敗,後端起了嗎?");
    return null;
  }
}

/** 下载:Blob → 临时 <a download> → 点一下 → 立即 revoke(不留 objectURL)。 */
function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** 按文件名关键词预判平立剖,让用户确认/改,不猜错也不逼他每次手选。
 *
 *  🔴 下面那几个中文**不是文案,是拿去匹配「用户上传的文件名」的关键词** ——
 *  界面繁體化时一个字都不许动。工地传上来的 .dxf 绝大多数仍叫「三层平面图.dxf」,
 *  关键词改了就永远猜不中:表现是每次都得手选一遍平/立/剖,**没有任何报错**。
 *  (「平面 / 立面 / 剖面 / 剖」四个词简繁同形,扫描器压根不会报它们 ——
 *   看着没改不是漏了。上面 VIEW_OPTIONS 那三个标签同理。) */
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
  // 文件预览弹窗:showPreview 收一个已建好的 Blob URL,closePreview 关闭并 revoke。
  preview: { url: string; title: string } | null;
  showPreview: (blobUrl: string, title: string) => void;
  closePreview: () => void;
};

const ArchiveContext = createContext<ArchiveContextValue | null>(null);

function useArchive(): ArchiveContextValue {
  const ctx = useContext(ArchiveContext);
  // 这句**刻意留简体**:它只在「组件挂到 <ArchiveProvider> 外面」时抛,是开发期不变式,
  // 生产环境会被 Next 的错误边界吞成一句英文,工友一个字都看不到。与本仓
  // 「注释 / 日志一律简体」同口径,已登记进 scripts/frontend-tests/hant-keep-hans.mjs。
  if (!ctx) throw new Error("useArchive 必须在 <ArchiveProvider> 内使用");
  return ctx;
}

/**
 * 「后端 → 前端」切换工地的落地点(自然语言切换的唯一闭环处)。
 *
 * 后端 supervisor 识别到用户要切工地时调 `switch_project` 工具,其 ToolMessage
 * (name=switch_project、content 是 `{ok,data:{project_id,name},...}` 的干净 JSON)
 * 随 values 流到前端。这里监听消息流,认到**本会话新到达**的成功切换,就调 setProjectId
 * 更新 React state + localStorage —— 下一轮 stream.submit 就带上新的 gyt_project_id。
 *
 * 只认「新到达」的切换:挂载时先把已有消息全部记为已处理,这样打开一条历史里切过工地的旧线程
 * 不会把你此刻的选择改掉(那是过去的动作,不该现在重放)。渲染 null,只跑副作用。
 */
function ProjectSwitchSync(): null {
  const stream = useStreamContext();
  const { projectId, setProjectId, reloadProjects } = useArchive();
  const handledRef = useRef<Set<string> | null>(null);

  useEffect(() => {
    const messages = stream.messages ?? [];
    // 首次:把现有消息(可能是加载进来的历史)全标记为已处理,避免回放旧切换。
    if (handledRef.current === null) {
      handledRef.current = new Set(
        messages.map((m) => m.id).filter((id): id is string => Boolean(id)),
      );
      return;
    }
    for (const m of messages) {
      const msg = m as { type?: string; name?: string; content?: unknown; id?: string };
      if (msg.type !== "tool" || msg.name !== "switch_project" || !msg.id) continue;
      if (handledRef.current.has(msg.id)) continue;
      handledRef.current.add(msg.id);
      let env: { ok?: boolean; data?: { project_id?: unknown } } | null = null;
      try {
        env = JSON.parse(typeof msg.content === "string" ? msg.content : "");
      } catch {
        continue;
      }
      // 失败信封 data 为 null;成功切换才有 data.project_id(空串=切到全部/不限项目,也要生效)。
      if (!env?.ok || !env.data || typeof env.data.project_id !== "string") continue;
      const next = env.data.project_id;
      if (next !== projectId) {
        setProjectId(next);
        void reloadProjects(true); // 顺手刷一下项目列表,让顶栏 chip 立刻显示新工地名
      }
    }
  }, [stream.messages, projectId, setProjectId, reloadProjects]);

  return null;
}

/** 归档面板的状态容器:把「开合 + 项目列表 + 当前工地」抬到顶栏与抽屉共用。 */
export function ArchiveProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectIdState] = useState("");

  // 切工地时顺手记进 localStorage;空串 = 未选(问全局)。
  const setProjectId = useCallback((id: string) => {
    setProjectIdState(id);
    try {
      if (id) localStorage.setItem(PROJECT_STORAGE_KEY, id);
      else localStorage.removeItem(PROJECT_STORAGE_KEY);
    } catch {
      /* localStorage 不可用(隐私模式等)就只留内存态 */
    }
  }, []);

  const reloadProjects = useCallback(async (silent = false) => {
    try {
      const resp = await fetch(`${API_URL}/projects`, { headers: authHeaders() });
      const env = await readEnvelope(resp);
      const list: Project[] = env?.data?.projects ?? [];
      setProjects(list);
      // 恢复上次选择(仍在册才用);否则保持「未选」—— **绝不偷偷默认第一个**。
      setProjectIdState((prev) => {
        if (prev && list.some((p) => p.id === prev)) return prev;
        let saved = "";
        try {
          saved = localStorage.getItem(PROJECT_STORAGE_KEY) ?? "";
        } catch {
          saved = "";
        }
        return saved && list.some((p) => p.id === saved) ? saved : "";
      });
    } catch {
      if (!silent) toast.error("拉取項目列表失敗,後端起了嗎?");
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

  const [preview, setPreview] = useState<{ url: string; title: string } | null>(null);
  const showPreview = useCallback((url: string, title: string) => {
    setPreview((prev) => {
      if (prev) URL.revokeObjectURL(prev.url); // 换一份预览先把旧的 objectURL 放掉,别泄漏
      return { url, title };
    });
  }, []);
  const closePreview = useCallback(() => {
    setPreview((prev) => {
      if (prev) URL.revokeObjectURL(prev.url);
      return null;
    });
  }, []);

  return (
    <ArchiveContext.Provider
      value={{
        open,
        setOpen,
        projects,
        projectId,
        setProjectId,
        currentName,
        reloadProjects,
        preview,
        showPreview,
        closePreview,
      }}
    >
      {children}
      <ArchiveDrawer />
      <PreviewModal />
      {/* 后端「打开/预览/下载」工具结果在这里落地成自动预览 / 下载(自然语言操作文件的闭环)。 */}
      <FilePreviewSync />
      {/* 后端 switch_project 的结果在这里落地成前端选择(自然语言切换工地的闭环)。 */}
      <ProjectSwitchSync />
    </ArchiveContext.Provider>
  );
}

/** PDF 预览弹窗:portal 挂到 body,<iframe> 打开 Blob URL;关闭由 closePreview 释放 URL。 */
function PreviewModal() {
  const { preview, closePreview } = useArchive();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  // ESC 关闭:预览是全屏遮罩,给个不用找 ✕ 的退出口。
  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closePreview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview, closePreview]);
  if (!preview || !mounted) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex flex-col bg-black/60 p-3 sm:p-8"
      onClick={closePreview}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="mx-auto flex h-full w-full max-w-[960px] flex-col overflow-hidden rounded-[14px] bg-white shadow-2xl"
      >
        <div className="flex items-center gap-3 border-b border-[#EAEDEB] px-5 py-3">
          {/* title 是用户自己起的图纸/文件名,原样上屏、不转(与顶栏 chip 同一条规矩)。 */}
          <div className="truncate text-[15px] font-bold text-[#1B2420]">{preview.title}</div>
          <button
            onClick={closePreview}
            className="ml-auto text-[22px] leading-none text-[#B4BDB8] hover:text-[#6B7772]"
            title="關閉預覽"
          >
            ✕
          </button>
        </div>
        <iframe src={preview.url} title={preview.title} className="h-full w-full flex-1 border-0" />
      </div>
    </div>,
    document.body,
  );
}

// 会触发「预览 / 下载」的后端工具名(其 ToolMessage 的 data 带 action + 文件元数据)。
const FILE_ACTION_TOOLS = new Set([
  "open_drawing",
  "export_drawing_pdf",
  "download_drawing",
  "open_document",
  "download_document",
]);

/** 从后端工具结果的 data 里算出该 fetch 哪个 URL、以及预览标题 / 下载文件名。 */
function fileActionTarget(
  action: "preview" | "download",
  data: any,
): { url: string; title: string; downloadName: string } | null {
  if (data?.target === "drawing" && data.artifact_id) {
    const title = data.name ?? "圖紙";
    if (action === "preview") {
      return {
        url: drawingFileUrl(data.artifact_id, { format: "pdf", disposition: "inline" }),
        title,
        downloadName: `${title}.pdf`,
      };
    }
    return {
      url: drawingFileUrl(data.artifact_id, { format: "original", disposition: "attachment" }),
      title,
      downloadName: `${title}.${data.format === "pdf" ? "pdf" : "dxf"}`,
    };
  }
  if (data?.target === "doc" && data.filename) {
    return {
      url: docFileUrl(
        {
          scope: data.scope,
          doc_type: data.doc_type,
          filename: data.filename,
          project_id: data.project_id,
        },
        action === "preview" ? "inline" : "attachment",
      ),
      title: data.filename,
      downloadName: data.filename,
    };
  }
  return null;
}

async function runFileAction(
  action: "preview" | "download",
  data: any,
  showPreview: (u: string, t: string) => void,
): Promise<void> {
  const target = fileActionTarget(action, data);
  if (!target) return;
  const blob = await authFetchBlob(target.url);
  if (!blob) return;
  if (action === "preview") showPreview(URL.createObjectURL(blob), target.title);
  else triggerBlobDownload(blob, target.downloadName);
}

/** 后端「打开/预览/下载」工具结果的落地点:认到本会话**新到达**的成功结果,自动弹预览 / 触发下载。
 *
 *  与 ProjectSwitchSync 同一套「只认新到达」的做法:挂载时先把已有消息记为已处理,
 *  这样打开一条历史线程不会把里面旧的打开动作重放一遍。歧义(needs_disambiguation)或失败
 *  一律不自动动手 —— 那时模型已经在聊天里问清 / 说明了,替用户猜着打开反而添乱。 */
function FilePreviewSync(): null {
  const stream = useStreamContext();
  const { showPreview } = useArchive();
  const handledRef = useRef<Set<string> | null>(null);

  useEffect(() => {
    const messages = stream.messages ?? [];
    if (handledRef.current === null) {
      handledRef.current = new Set(
        messages.map((m) => m.id).filter((id): id is string => Boolean(id)),
      );
      return;
    }
    for (const m of messages) {
      const msg = m as { type?: string; name?: string; content?: unknown; id?: string };
      if (msg.type !== "tool" || !msg.name || !FILE_ACTION_TOOLS.has(msg.name) || !msg.id) continue;
      if (handledRef.current.has(msg.id)) continue;
      handledRef.current.add(msg.id);
      let env: { ok?: boolean; data?: any } | null = null;
      try {
        env = JSON.parse(typeof msg.content === "string" ? msg.content : "");
      } catch {
        continue;
      }
      const data = env?.ok ? env.data : null;
      if (!data || data.needs_disambiguation) continue; // 歧义/失败:模型已在聊天里说明,不自动动手
      const action = data.action;
      if (action !== "preview" && action !== "download") continue;
      void runFileAction(action, data, showPreview);
    }
  }, [stream.messages, showPreview]);

  return null;
}

/** 读当前选中的工地项目编号(供聊天提交时经 config.configurable 注入,让规范问答自动限定作用域)。
 *  必须在 <ArchiveProvider> 之内调用;没选项目时为空串。 */
export function useCurrentProjectId(): string {
  return useArchive().projectId;
}

/** 顶栏醒目的「当前工地」切换条:一眼看清现在针对哪个项目,点开可切换 / 选「全部」/ 去新建。
 *  这是「你在项目2、它却按项目1答」的正解 —— 把默默默认的小 chip 换成显眼、可切、不偷偷默认。 */
function ProjectSwitcher() {
  const { projects, projectId, setProjectId, currentName, setOpen } = useArchive();
  const [menuOpen, setMenuOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [menuOpen]);

  const scoped = !!projectId;
  const rowCls = (active: boolean) =>
    cn(
      "flex w-full items-center gap-2 px-3 py-2 text-left text-[14px] transition hover:bg-[#F7F9F8]",
      active ? "font-bold text-[#1B2420]" : "text-[#6B7772]",
    );

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setMenuOpen((v) => !v)}
        className={cn(
          // 窄屏收内边距 + 不许换行。这颗是顶栏里唯一宽度会随内容变的元素,
          // 也是唯一一个「让步点」—— 但让步靠的是下面那个 span 的 max-w + truncate
          // (给出确定的上限),**不是**靠 flex 压缩:整组按钮都是 shrink-0,
          // 谁都压不动(理由见 ArchiveHeaderControls 的注释)。
          "flex items-center gap-1.5 rounded-full border px-2.5 py-2 text-[14px] font-bold whitespace-nowrap transition sm:gap-2 sm:px-4",
          scoped
            ? "border-[#0E9F6E] bg-[#EEF6F2] text-[#0E7A55]"
            : "border-dashed border-[#C6D0CB] bg-white text-[#8A948F] hover:border-[#7FCDAE]",
        )}
        title="當前工地 —— 問答 / 看圖 / 歸檔都按它走"
      >
        <span
          className={cn(
            "h-2 w-2 shrink-0 rounded-full",
            scoped ? "bg-[#0E9F6E]" : "bg-[#C6D0CB]",
          )}
        />
        {/* 窄屏把项目名的上限从 180px 压到 72px、360px 以下再压到 52px(超出照旧 truncate 出「…」)。
            72 不是随手写的:390px 顶栏的可用内容宽是 368px(390 − pl-3 − p-2.5),
            侧栏开关 40 + 品牌 93 + 本颗 123 + 资料库 40 + 资料归档 38 + 四道 gap-1.5 = 358,
            余 10px。上限再放宽到 88 就会顶到 380、把「资料归档」挤出屏幕右缘(实测过)。
            「在哪个工地」比完整名字更要紧 —— 点开下拉能看到全名,hover 还有 title。
            ≥640px 恢复 180px。 */}
        {/* currentName 是**用户自己起的项目名**(经后端存的原文),不许转 ——
            工地名转了会跟台账 / 回执里的写法对不上,而且那是人家填的字。
            只有兜底那句是我们的文案,写繁體。 */}
        <span className="max-w-[72px] truncate max-[359px]:max-w-[52px] sm:max-w-[180px]">
          🏗 {currentName ?? "全部工地(未選)"}
        </span>
        <span className="shrink-0 text-[#9AA5A0]">▾</span>
      </button>

      {menuOpen && (
        <div className="absolute right-0 z-50 mt-2 w-[248px] overflow-hidden rounded-[14px] border border-[#E4E8E6] bg-white shadow-[0_12px_40px_rgba(27,36,32,0.16)]">
          <div className="px-3 pt-2.5 pb-1 text-[12px] font-bold text-[#9AA5A0]">切換當前工地</div>
          <button
            onClick={() => {
              setProjectId("");
              setMenuOpen(false);
            }}
            className={rowCls(!scoped)}
          >
            <span className="h-2 w-2 shrink-0 rounded-full bg-[#C6D0CB]" />
            全部工地(不限項目)
          </button>
          {projects.map((p) => (
            <button
              key={p.id}
              onClick={() => {
                setProjectId(p.id);
                setMenuOpen(false);
              }}
              className={rowCls(p.id === projectId)}
            >
              <span
                className={cn(
                  "h-2 w-2 shrink-0 rounded-full",
                  p.id === projectId ? "bg-[#0E9F6E]" : "bg-[#D5DBD8]",
                )}
              />
              <span className="truncate">{p.name}</span>
              <span className="ml-auto shrink-0 text-[12px] text-[#B4BDB8]">{p.id}</span>
            </button>
          ))}
          <button
            onClick={() => {
              setMenuOpen(false);
              setOpen(true);
            }}
            className="flex w-full items-center gap-2 border-t border-[#EEF1F0] px-3 py-2.5 text-[14px] font-bold text-[#0E7A55] transition hover:bg-[#F7F9F8]"
          >
            ＋ 新建 / 管理工地
          </button>
        </div>
      )}
    </div>
  );
}

/** 顶栏一组入口:当前工地切换 + 资料库(浏览全部)+「📂 资料归档」。 */
export function ArchiveHeaderControls() {
  const { setOpen } = useArchive();
  return (
    // 为什么加这些断点:2026-08-15 在 390×844(iPhone)视口实测,这一组三颗按钮
    // 的 intrinsic 宽度远超顶栏剩余空间,于是被 flex 压成竖排 ——「资料库」52×102、
    // 「资料归档」50×125(文字一列一个字)。页面并没有横向滚动,所以不是溢出是挤压。
    // gap 与内边距窄屏收一档,≥640px(sm)全部原样恢复。
    //
    // ⚠️ shrink-0 不能少,而且**不能换成 min-w-0**:这一组的三颗子按钮自己都是
    // shrink-0(不然就竖排),所以本容器一旦被允许压到比内容窄,子按钮会溢出到
    // 容器外,而外面的兄弟(「新对话」那颗)是按**容器的盒子**排的 —— 结果两颗
    // 按钮直接叠在一起。改造过程中就这么错过一次:进入对话后的顶栏里,
    // 「新对话」[349→381] 压在「资料归档」[330→368] 上,重叠 19px。
    <div className="flex shrink-0 items-center gap-1.5 sm:gap-2.5">
      <ProjectSwitcher />
      <LibraryButton />
      <button
        onClick={() => setOpen(true)}
        className="flex shrink-0 items-center gap-2 rounded-full bg-[#1B2420] px-2.5 py-2.5 text-[14px] font-bold whitespace-nowrap text-white transition hover:bg-black sm:px-4"
        title="按項目歸檔圖紙 / 規範 / 任務書"
      >
        {/* 窄屏只留 📂 图标(顶栏放不下三颗带字的按钮),≥640px 恢复「📂 资料归档」。
            功能一件不少:图标本身就是按钮,点开的还是同一个归档抽屉。
            ⚠️ 为什么是「两个 span 各写一份」而不是「📂 + <span>资料归档</span>」:
            本 button 是 flex 且带 gap-2,后者会把图标和文字变成**两个 flex item**,
            桌面端凭空多出 8px、整组按钮左移 —— 2026-08-15 像素比对抓到过(1280 视口
            有 0.487% 像素变化,全在这一片)。display:none 的那份不参与 flex 布局,
            所以这种写法在任何断点下都只有一个 flex item,桌面端与改造前逐像素一致。 */}
        <span className="sm:hidden">📂</span>
        <span className="hidden sm:inline">📂 資料歸檔</span>
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
      toast.error("項目名不能為空");
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
        // 🔴 `env.user_msg` **一律不过繁體转换器**(W12 复审定案)。
        //
        // 这个文件里的 user_msg 内插的是**用户自己起的项目名 / 自己传的文件名**
        // (webapp.py:180/235/344 那几句 `f"项目「{name}」建好了"`):
        //     转换器会把「恒昌 3 期」写成「恆昌 3 期」、把文件名里的字一起换掉,
        //     于是回执里的名字跟他刚才亲手打进去的那个对不上 —— 而这**一行报错都没有**。
        // 整句转换在原理上分不出「系统写的字」和「内插的用户数据」,分不出的时候
        // 默认转是危险的那一侧,所以整条 user_msg 通道都不转,与后端同一口径。
        //
        // 兜底句是我们自己的文案,源码里就写成繁體 —— 它本来就不需要转。
        // 守卫在 scripts/frontend-tests/hant-ui-strings.test.ts(实参里出现
        // user_msg / userMsg 就报),别再包回去。
        toast.success(env?.user_msg ?? "項目建好了");
        setNewName("");
        setNewCode("");
        setShowNew(false);
        await reloadProjects();
        if (env?.data?.id) setProjectId(env.data.id);
      } else {
        toast.error(env?.user_msg ?? "建項目失敗");
      }
    } finally {
      setBusy(false);
    }
  }

  async function uploadDrawing() {
    if (!dwgFile) return toast.error("請選一張 .dxf 或 .pdf 圖紙");
    if (!projectId) return toast.error("先選或建一個項目");
    if (!viewType) return toast.error("請選平面 / 立面 / 剖面");
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", dwgFile);
      fd.append("view_type", viewType);
      if (floor.trim()) fd.append("floor", floor.trim());
      fd.append("title", title.trim() || dwgFile.name.replace(/\.(dxf|pdf)$/i, ""));
      const resp = await fetch(
        `${API_URL}/projects/${encodeURIComponent(projectId)}/drawings`,
        { method: "POST", headers: authHeaders(), body: fd },
      );
      const env = await readEnvelope(resp);
      if (resp.ok) {
        toast.success(env?.user_msg ?? "圖紙上傳成功");
        setDwgFile(null);
        setTitle("");
        setFloor("");
        setViewType("");
      } else {
        toast.error(env?.user_msg ?? "圖紙上傳失敗");
      }
    } finally {
      setBusy(false);
    }
  }

  async function uploadDoc() {
    if (!docFile) return toast.error("請選一份 PDF");
    if (docScope === "project" && !projectId) return toast.error("項目資料要先選項目");
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
        toast.success(env?.user_msg ?? "資料上傳成功");
        setDocFile(null);
      } else {
        toast.error(env?.user_msg ?? "資料上傳失敗");
      }
    } finally {
      setBusy(false);
    }
  }

  function pickDrawing(f: File | null) {
    setDwgFile(f);
    if (f) {
      setViewType(guessViewType(f.name));
      setTitle((prev) => prev || f.name.replace(/\.(dxf|pdf)$/i, ""));
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
            <div className="text-[19px] font-black text-[#1B2420]">資料歸檔</div>
            <div className="text-[13px] text-[#8A948F]">把圖紙、規範正式存進項目</div>
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
            <div className={stepLabel}>① 存到哪個工地</div>
            <div className="flex gap-2.5">
              <select
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
                className={fieldCls + " font-bold text-[#1B2420]"}
              >
                <option value="">（未選 / 全局）</option>
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
                  placeholder="新建項目名，如 幸福小區A3棟"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                />
                <input
                  className="w-[90px] rounded-[14px] border border-[#E4E8E6] bg-white px-3 py-3.5 text-[15px]"
                  placeholder="短碼"
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
            <div className={stepLabel}>② 傳什麼</div>
            <div className="grid grid-cols-2 gap-2.5">
              <button onClick={() => setTab("drawing")} className={bigChoice(tab === "drawing")}>
                <div
                  className={
                    "text-[18px] font-black " +
                    (tab === "drawing" ? "text-[#0E7A55]" : "text-[#6B7772]")
                  }
                >
                  📐 圖紙
                </div>
                <div
                  className={
                    "mt-1 text-[13px] font-bold " +
                    (tab === "drawing" ? "text-[#5FAE8E]" : "text-[#A2ABA6]")
                  }
                >
                  DXF / PDF 文件
                </div>
              </button>
              <button onClick={() => setTab("doc")} className={bigChoice(tab === "doc")}>
                <div
                  className={
                    "text-[18px] font-black " +
                    (tab === "doc" ? "text-[#0E7A55]" : "text-[#6B7772]")
                  }
                >
                  📄 資料
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
              <div className={stepLabel + " mb-0"}>③ 圖紙信息</div>
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
                  accept=".dxf,.pdf"
                  className="hidden"
                  onChange={(e) => pickDrawing(e.target.files?.[0] ?? null)}
                />
                {dwgFile ? (
                  <span className="font-bold text-[#1B2420]">{dwgFile.name}</span>
                ) : (
                  <>
                    拖入 .dxf / .pdf,或 <span className="font-bold text-[#0E9F6E]">點擊選擇</span>
                  </>
                )}
              </label>

              <div className="text-[14px] font-semibold text-[#6B7772]">這是哪種圖?</div>
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
                  placeholder="圖名(留空=文件名)"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
                <input
                  className="w-[130px] rounded-[14px] border border-[#E4E8E6] bg-white px-4 py-3.5 text-[15px] placeholder:text-[#A2ABA6]"
                  placeholder="樓層·可選"
                  value={floor}
                  onChange={(e) => setFloor(e.target.value)}
                />
              </div>

              <button onClick={uploadDrawing} disabled={busy} className={archiveBtn}>
                {/* currentName 是用户自己起的项目名,原样上屏、不转(理由见顶栏那颗 chip)。 */}
                {busy ? "上傳中…" : `歸檔到 ${currentName ?? "…先選項目"}`}
              </button>
            </section>
          ) : (
            <section className="flex flex-col gap-3">
              <div className={stepLabel + " mb-0"}>③ 資料信息</div>

              <div className="text-[14px] font-semibold text-[#6B7772]">作用域</div>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setDocScope("global")}
                  className={bigChoice(docScope === "global")}
                >
                  <div className="text-[16px] font-black text-[#1B2420]">全局規範</div>
                  <div className="mt-1 text-[12px] font-bold text-[#8A948F]">所有項目通用</div>
                </button>
                <button
                  onClick={() => setDocScope("project")}
                  className={bigChoice(docScope === "project")}
                >
                  <div className="text-[16px] font-black text-[#1B2420]">本項目</div>
                  <div className="mt-1 text-[12px] font-bold text-[#8A948F]">
                    {currentName ?? "先選項目"}
                  </div>
                </button>
              </div>

              {/* 这两颗按钮的中文只是标签 —— 送后端的是 `doc_type=regulation / task_book`
                  (英文枚举,见 uploadDoc 里那句 fd.append),所以转繁體不影响任何请求。 */}
              {docScope === "project" && (
                <div className="grid grid-cols-2 gap-2">
                  <button
                    onClick={() => setDocType("regulation")}
                    className={toggle(docType === "regulation")}
                  >
                    規範
                  </button>
                  <button
                    onClick={() => setDocType("task_book")}
                    className={toggle(docType === "task_book")}
                  >
                    任務書
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
                    拖入 .pdf,或 <span className="font-bold text-[#0E9F6E]">點擊選擇</span>
                  </>
                )}
              </label>

              <button onClick={uploadDoc} disabled={busy} className={archiveBtn}>
                {busy ? "上傳中…" : docScope === "global" ? "歸檔到 全局規範" : "歸檔到本項目"}
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
  ext: string; // ".dxf" / ".pdf":决定预览走「DXF 转 PDF」还是「PDF 原样」,以及下载原文件的后缀
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
  chunks: number; // 向量块数:0 = 入库中/无文字层,>0 = 已入库、可检索
  status: string; // indexed / ingesting / unsearchable(见后端 /library 的 _doc_status)
};
type LibraryData = {
  projects: { id: string; name: string; code: string | null }[];
  drawings: LibDrawing[];
  docs: LibDoc[];
};

// 两张标签表:**键是后端回来的枚举值(英文),不许动**;值只是上屏的字,写繁體。
// (VIEW_LABEL 的三个值简繁同形,所以看着没改,不是漏了。)
const VIEW_LABEL: Record<string, string> = { plan: "平面", elevation: "立面", section: "剖面" };
const DOC_LABEL: Record<string, string> = { regulation: "規範", task_book: "任務書" };

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
      // 同样是后端来的人话,**不转**;兜底句已是繁體(理由见 createProject 那处)。
      // 这里的 user_msg 内插的是用户自己起的项目名 / 自己传的文件名。
      toast.success(env?.user_msg ?? "已刪除");
      return true;
    }
    toast.error(env?.user_msg ?? "刪除失敗");
    return false;
  } catch {
    toast.error("刪除失敗,後端起了嗎?");
    return false;
  }
}

/** 行内两步删除:点「删除」→ 变「确认删除 / 取消」→ 确认后跑 onDelete。不弹浏览器原生框。 */
function RowDelete({
  label = "刪除",
  confirmLabel = "確認刪除",
  onDelete,
}: {
  label?: string;
  confirmLabel?: string;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  if (busy) return <span className="text-[12px] text-[#B4BDB8]">刪除中…</span>;
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

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const resp = await fetch(`${API_URL}/library`, { headers: authHeaders() });
      const env = await readEnvelope(resp);
      if (resp.ok && env?.data) setData(env.data as LibraryData);
      else if (!silent) toast.error(env?.user_msg ?? "拉取資料庫失敗");
    } catch {
      if (!silent) toast.error("拉取資料庫失敗,後端起了嗎?");
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  // 有文档还在入库(chunks===0)时,每 4s 静默刷一次,直到都入完 —— 这就是那条「进度」。
  const anyIngesting = !!data?.docs.some((d) => d.status === "ingesting");
  useEffect(() => {
    if (!open || !anyIngesting) return;
    const timer = setInterval(() => void load(true), 4000);
    return () => clearInterval(timer);
  }, [open, anyIngesting, load]);

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
            <div className="text-[19px] font-black text-[#1B2420]">資料庫</div>
            <div className="text-[13px] text-[#8A948F]">所有項目的圖紙與規範</div>
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
            <div className="py-10 text-center text-[15px] text-[#8A948F]">加載中…</div>
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
        className="flex shrink-0 items-center gap-2 rounded-full border border-[#E4E8E6] bg-white px-2.5 py-2 text-[14px] font-bold whitespace-nowrap text-[#33403A] transition hover:border-[#7FCDAE] sm:px-4"
        title="查看所有項目的圖紙與規範"
      >
        {/* 与「资料归档」同一处理(含那条 flex gap 的坑,见那边的注释):
            窄屏只留 📚 图标,≥640px 恢复带字的原样。
            实测(2026-08-15,390×844)不这么做时这颗是 52×102 ——「资料库」三字竖排两行。 */}
        <span className="sm:hidden">📚</span>
        <span className="hidden sm:inline">📚 資料庫</span>
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
        還沒有任何圖紙或資料。
        <br />
        點右上角「📂 資料歸檔」傳第一份吧。
      </div>
    );
  }

  return (
    <>
      {globalDocs.length > 0 && (
        <LibrarySection title="全局規範" hint="所有項目通用" count={globalDocs.length}>
          {globalDocs.map((d) => (
            <DocRow key={d.rel_path} doc={d} reload={reload} />
          ))}
        </LibrarySection>
      )}
      {data.projects.map((p) => {
        const dwgs = data.drawings.filter((x) => x.project_id === p.id);
        const docs = data.docs.filter((x) => x.scope === "project" && x.project_id === p.id);
        return (
          // title 是用户自己起的项目名,原样上屏、不转(与顶栏 chip 同一条规矩)。
          <LibrarySection
            key={p.id}
            title={p.name}
            hint={`編號 ${p.id}`}
            count={dwgs.length + docs.length}
            action={
              <RowDelete
                label="刪除項目"
                confirmLabel="確認刪除項目"
                onDelete={async () => {
                  if (await apiDelete(`/projects/${encodeURIComponent(p.id)}`)) await reload();
                }}
              />
            }
          >
            {dwgs.length === 0 && docs.length === 0 ? (
              <div className="px-1 py-1.5 text-[13px] text-[#A2ABA6]">（暫無圖紙或資料)</div>
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

/** 行内小操作按钮:点一下跑异步动作,期间禁用防连点。label 是繁體静态文案。 */
function ActionBtn({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await onClick();
        } finally {
          setBusy(false);
        }
      }}
      className="shrink-0 rounded-[8px] border border-[#E4E8E6] px-2.5 py-1 text-[12px] font-bold text-[#33403A] transition hover:border-[#7FCDAE] disabled:opacity-50"
    >
      {busy ? "…" : label}
    </button>
  );
}

function DrawingRow({ dwg, reload }: { dwg: LibDrawing; reload: () => Promise<void> }) {
  const { showPreview } = useArchive();
  const isPdf = dwg.ext === ".pdf";

  async function preview() {
    // DXF 转 PDF 预览、PDF 原样预览,后端都走 format=pdf(见 /files/drawing)。
    const blob = await authFetchBlob(
      drawingFileUrl(dwg.artifact_id, { format: "pdf", disposition: "inline" }),
    );
    if (blob) showPreview(URL.createObjectURL(blob), dwg.title);
  }
  async function download(format: "original" | "pdf") {
    const blob = await authFetchBlob(
      drawingFileUrl(dwg.artifact_id, { format, disposition: "attachment" }),
    );
    if (!blob) return;
    const suffix = format === "pdf" ? "pdf" : isPdf ? "pdf" : "dxf";
    triggerBlobDownload(blob, `${dwg.title}.${suffix}`);
  }

  return (
    <div className="flex items-center gap-2 rounded-[14px] border border-[#EAEDEB] bg-white px-4 py-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-[#F1F4F3] text-lg">
        📐
      </div>
      <div className="min-w-0">
        <div className="truncate text-[15px] font-bold text-[#1B2420]">{dwg.title}</div>
        <div className="text-[12px] text-[#8A948F]">圖紙{dwg.floor ? ` · ${dwg.floor}` : ""}</div>
      </div>
      <span className="ml-auto shrink-0 rounded-full bg-[#EEF6F2] px-2.5 py-1 text-[12px] font-bold text-[#0E7A55]">
        {VIEW_LABEL[dwg.view_type] ?? dwg.view_type}
      </span>
      <ActionBtn label="預覽" onClick={preview} />
      {/* DXF:下载原 DXF + 下载生成的 PDF;PDF 图纸:只有一份原文件。 */}
      {isPdf ? (
        <ActionBtn label="下載" onClick={() => download("original")} />
      ) : (
        <>
          <ActionBtn label="DXF" onClick={() => download("original")} />
          <ActionBtn label="PDF" onClick={() => download("pdf")} />
        </>
      )}
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

/** 一份文档的检索状态徽标:已可检索 / 处理中 / 可预览但不可检索(OCR 待支援)。 */
function DocStatus({ doc }: { doc: LibDoc }) {
  if (doc.status === "indexed" || (doc.status !== "unsearchable" && doc.chunks > 0)) {
    return <span className="text-[#5FAE8E]">已入庫 {doc.chunks} 段</span>;
  }
  if (doc.status === "unsearchable") {
    // 无文字层(扫描件/转曲):归了档、能预览,但暂不可检索。不伪造 OCR,如实说。
    return <span className="font-bold text-[#C2892B]">可預覽 · 暫不可檢索(OCR 待支援)</span>;
  }
  return <span className="animate-pulse font-bold text-[#C2892B]">入庫中…</span>;
}

function DocRow({ doc, reload }: { doc: LibDoc; reload: () => Promise<void> }) {
  const { showPreview } = useArchive();
  const ref: DocRef = {
    scope: doc.scope,
    doc_type: doc.doc_type,
    filename: doc.filename,
    project_id: doc.project_id,
  };

  async function preview() {
    const blob = await authFetchBlob(docFileUrl(ref, "inline"));
    if (blob) showPreview(URL.createObjectURL(blob), doc.filename);
  }
  async function download() {
    const blob = await authFetchBlob(docFileUrl(ref, "attachment"));
    if (blob) triggerBlobDownload(blob, doc.filename);
  }

  return (
    <div className="flex items-center gap-2 rounded-[14px] border border-[#EAEDEB] bg-white px-4 py-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-[#F1F4F3] text-lg">
        📄
      </div>
      <div className="min-w-0">
        <div className="truncate text-[15px] font-bold text-[#1B2420]">{doc.filename}</div>
        <div className="text-[12px] text-[#8A948F]">
          {DOC_LABEL[doc.doc_type] ?? doc.doc_type} · {fmtSize(doc.size_bytes)} · <DocStatus doc={doc} />
        </div>
      </div>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <ActionBtn label="預覽" onClick={preview} />
        <ActionBtn label="下載" onClick={download} />
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
    </div>
  );
}
