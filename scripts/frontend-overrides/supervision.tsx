/**
 * 监理确认与处置界面(W9 · S6 泳道)—— **本仓自有新文件**,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/supervision.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 它要解决的问题 ────────────────────────────────────────────────────
 * safety 看完照片会把违规项登记成 **pending 隐患**(D17:自动写入,但不算整改率、
 * 不进超期清单、不能被升级)。**必须有人确认**,它才进正式流程 —— 而在这个界面
 * 出现之前,确认这一步在系统里没有任何入口:隐患躺在库里,谁也看不见。
 *
 * 后面那六种文书(通知单/暂停令/致建设单位报告/复工令/监理报告)全部落进 artifacts,
 * 而 tool-calls.tsx 此前只认 `render_inspection_report` 一个工具 —— 于是演示时说
 * 「暂停令已签发」,界面上只有一行灰色折叠:**点不开、下不到,而且不会报错**。
 *
 * ── 为什么写入不走对话链 ──────────────────────────────────────────────
 * 签发《监理通知单》《工程暂停令》是**法律行为**,不能由概率性系统单方面触发
 * (方案 §5.1)。supervision Agent 只能查、只能建议;所有改状态 / 出文书的动作
 * 都是本面板直接打 HTTP 端点 —— 与 W7 打卡「写入不走 LLM」同构,这次理由更硬。
 * **请求/响应契约的唯一真相是 `backend/src/gyt/supervision_api.py` 的模块 docstring**,
 * 本文件一个字段都不许偏。
 *
 * ── 分层 ──────────────────────────────────────────────────────────────
 * 所有可测逻辑都在 @/lib/supervision-lib(scripts/frontend-overrides/supervision-lib.ts,
 * 纯 TS 零依赖,scripts/frontend-tests/ 的 vitest 直接测它)。本文件只留「碰浏览器」
 * 的部分:portal、fetch、表单状态。**判断哪条隐患现在能做什么,一律问
 * `availableActions`,别在 JSX 里就地写 `status === "open" && …`** —— 那些判据要
 * 逐条对齐服务端四道闸,散进 JSX 就没法测、也没法一眼看出哪条漂了。
 *
 * ── 挂在哪 ────────────────────────────────────────────────────────────
 * 入口是 tool-calls.tsx 里那张「待确认隐患」卡片(隐患在哪张照片上发现的,
 * 就在那条消息底下处置),不是输入框动作条。两个原因:①上下文就在眼前,
 * 不用先记住编号再去别处找;②动作条那件(thread-index.tsx)是打给上游的补丁,
 * 本泳道不动它 —— 少改一个文件,合流时少一处冲突面。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryState } from "nuqs";
import {
  AlertTriangle,
  Check,
  ClipboardCheck,
  ExternalLink,
  FileText,
  Gavel,
  LoaderCircle,
  ShieldAlert,
  X,
} from "lucide-react";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { getApiKey } from "@/lib/api-key";
import {
  ACTION_LABEL,
  ACTION_ENDPOINT,
  actionBody,
  actionNeedsConfirm,
  actionNeedsDuePhrase,
  actionNeedsPhoto,
  availableActions,
  confirmBody,
  confirmPrompt,
  describeDocuments,
  DisposalAction,
  documentUrl,
  docTypeZh,
  GRADE_NORMAL,
  GRADE_SEVERE,
  HazardBrief,
  hazardStatusZh,
  isDownloadableDoc,
  mergeHazards,
  NETWORK_ERROR_STATUS,
  normalizeError,
  parseActionEnvelope,
  parseConfirmEnvelope,
  patchHazard,
  pendingHazards,
  removeHazard,
  SupervisionContractError,
  SupervisionDoc,
  SupervisionEndpoint,
  SUPERVISION_MESSAGES,
  supervisionUrl,
  toggleSelected,
} from "@/lib/supervision-lib";

/**
 * 举手到可确认之间的静默期。**与 thread-history.tsx 的 ARM_QUIET_MS 同一个数、
 * 同一条理由**(那边的头注写全了:300ms 是人有意识看清一句话的下限,系统双击
 * 判定通常在 500ms 内,400 卡在中间)。这里挡的东西更贵:双击穿透一次
 * = 平白停掉一片人的工,或者往建设主管部门报一份指控。
 */
const ARM_QUIET_MS = 400;

/** 级别徽章配色。两个取值来自 db/hazards.py 的 GRADES,别自由发挥。 */
const GRADE_CHIP: Record<string, string> = {
  [GRADE_SEVERE]: "bg-red-50 text-red-700 ring-red-200",
  [GRADE_NORMAL]: "bg-sky-50 text-sky-700 ring-sky-200",
};

/** 状态徽章配色:能动的暖色、收尾的灰、出事的红。认不出的状态退回灰色,不炸。 */
const STATUS_CHIP: Record<string, string> = {
  pending: "bg-amber-50 text-amber-700 ring-amber-200",
  open: "bg-blue-50 text-blue-700 ring-blue-200",
  notified: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  suspended: "bg-red-50 text-red-700 ring-red-200",
  reinspect_failed: "bg-orange-50 text-orange-700 ring-orange-200",
  resuming: "bg-teal-50 text-teal-700 ring-teal-200",
  closed: "bg-gray-100 text-gray-500 ring-gray-200",
  escalated: "bg-purple-50 text-purple-700 ring-purple-200",
};

/**
 * 后端地址的解析**照抄 checkin.tsx 的 useApiBase**(它又照抄 Stream.tsx:147-180):
 * URL 参数 apiUrl 优先,退 NEXT_PUBLIC_API_URL。本机 dev 是 http://localhost:2024;
 * 公网是 `${GYT_PUBLIC_ORIGIN}/api` —— Caddy 的 handle_path /api/* 剥前缀转发,
 * 同源所以登录 Cookie 自动带上。别发明第三种拿地址的路子。
 */
function useApiBase(): string {
  const envApiUrl = process.env.NEXT_PUBLIC_API_URL;
  const [apiUrlParam] = useQueryState("apiUrl", { defaultValue: envApiUrl || "" });
  return apiUrlParam || envApiUrl || "";
}

type CallOutcome =
  | { ok: true; text: string }
  | { ok: false; message: string };

/**
 * 打一次监理端点。**所有写入都从这一个出口走** —— 散着写 fetch 的下场是
 * 某一处漏了 x-api-key(公网上表现为每次都 401)或者漏了错误归一化
 * (于是屏幕上出现一行 "Failed to fetch",而这是给工地上的人看的界面)。
 */
async function callSupervision(
  apiBase: string,
  endpoint: SupervisionEndpoint,
  body: Record<string, unknown>,
): Promise<CallOutcome> {
  const apiKey = getApiKey();
  let res: Response;
  try {
    res = await fetch(supervisionUrl(apiBase, endpoint), {
      method: "POST",
      headers: {
        "content-type": "application/json",
        // 令牌与 Stream.tsx 同一来源(getApiKey():localStorage 优先、构建期注入兜底)。
        // 没有令牌就不发这个头 —— 本机未开鉴权时后端也不看它。
        ...(apiKey ? { "x-api-key": apiKey } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, message: normalizeError(NETWORK_ERROR_STATUS, null, "").message };
  }
  const text = await res.text();
  if (!res.ok) {
    return {
      ok: false,
      message: normalizeError(res.status, res.headers.get("content-type"), text).message,
    };
  }
  return { ok: true, text };
}

/** 小徽章。ring-inset 让它在浅底上也有边界,不至于糊成一片。 */
function Chip({ tone, children }: { tone: string; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[12px] font-medium ring-1 ring-inset ${tone}`}
    >
      {children}
    </span>
  );
}

/**
 * 文书下载卡 —— 一份一张,**按 `documents` 数组渲染 N 张**(契约冻结形状的用处:
 * 「N 张卡」与「N 份文书」天然对齐,三份里少出一份当场看得出来)。
 *
 * `artifactBase` 由调用方传进来,本文件**不读 `process.env`**:
 * `NEXT_PUBLIC_ARTIFACT_BASE` 那条链(CLAUDE.md 同源清单)已经有三个读者
 * (human.tsx / tool-calls.tsx / checkin.tsx),而这条链**断在任何一环都不报错** ——
 * 少一个读者就少一处能断的地方。监理这条复用 tool-calls.tsx 手里那一份。
 */
export function SupervisionDocCards({
  documents,
  artifactBase,
}: {
  documents: readonly SupervisionDoc[];
  artifactBase: string;
}) {
  if (documents.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      {documents.map((doc) => {
        const url = documentUrl(doc, artifactBase);
        if (!isDownloadableDoc(doc)) {
          // 复查记录**不是文书**:它本来就没有文件(DocKind 里没有这一档)。
          // 跟文书长一个样、再挂一句「下不了」的话,真正出事的那种
          //(文书签了却取不了件)会被这种噪声淹掉 —— 而那一种恰恰要人去查。
          return (
            <div
              key={doc.doc_no}
              className="flex items-center gap-2 rounded-lg bg-gray-50 px-3 py-2 text-[12px] text-gray-500"
            >
              <ClipboardCheck className="size-4 shrink-0 text-gray-400" />
              <span>{docTypeZh(doc.doc_type)}</span>
              <span className="font-mono break-all select-all">{doc.doc_no}</span>
              <span className="text-gray-400">(记录,没有文件)</span>
            </div>
          );
        }
        return (
          <div
            key={doc.doc_no}
            className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50/40 px-3 py-2.5"
          >
            <FileText className="mt-0.5 h-5 w-5 shrink-0 text-amber-700" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="font-medium text-gray-900">{docTypeZh(doc.doc_type)}</span>
                {/* 编号 select-all:监理要在电话里报它、上报时按它排证据链,
                    一点就能整串复制(同 checkin.tsx 凭证卡片的做法)。 */}
                <span className="font-mono text-[13px] break-all text-gray-600 select-all">
                  {doc.doc_no}
                </span>
              </div>
              {/* D15 的定位:AI 出稿、总监理工程师签字生效。这句话在每份文书正文里
                  也印着(docgen 的 SUPERVISION_DISCLAIMER),界面上再说一遍是因为
                  「已签发」三个字在屏幕上太容易被读成「已经生效」。 */}
              <div className="mt-1 text-[12px] text-gray-500">
                这份是出稿,要总监理工程师签字盖章后才是正式文件。
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {url ? (
                  <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    // title 给文件名:落到磁盘上叫什么,点之前就知道
                    // (取件端点按编号给文件,浏览器另存时用的就是这个名字)。
                    title={doc.filename}
                    // pointer-coarse:戴手套的手指按不中 28px 的链接(同 checkin.tsx 那颗关闭按钮)
                    className="inline-flex items-center gap-1.5 rounded-md bg-amber-600 px-2.5 py-1 text-[13px] font-medium text-white transition-colors hover:bg-amber-700 pointer-coarse:min-h-11 pointer-coarse:px-4"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                    打开文书
                  </a>
                ) : (
                  // artifact_id 为空 = 文书签发了(编号已进台账)但取不了件。
                  // **绝不渲染死链接**:点了没反应和「文件真没了」在界面上分不开
                  // (同 checkin-lib.receiptImageUrl 那条规矩)。
                  <span className="text-[12px] text-red-600">
                    这份文书没有取件编号,下不了 —— 编号已进台账,找管理员按编号取。
                  </span>
                )}
              </div>
            </div>
          </div>
        );
      })}
      {/* 只在真有东西可下的时候说这句 —— 一堆复查记录底下挂一行「打不开?」
          等于凭空制造一个不存在的问题。 */}
      {documents.some(isDownloadableDoc) && (
        <div className="text-[11px] text-gray-400">
          打不开?本机要先在仓库根执行 <code className="font-mono">make serve-artifacts</code>
        </div>
      )}
    </div>
  );
}

/** 举手确认条 —— 顶掉原来那颗按钮,不是 window.confirm。
 *
 * **不用 window.confirm 的理由原样见 thread-history.tsx 的 DeleteConfirmBar 头注**:
 * 它阻塞主线程、文案被浏览器套壳,而且用户勾了「阻止此页面再次弹出对话框」之后
 * **会被静默跳过**(直接返回 false)。一个不可逆的法律动作,不能建在一个可能被
 * 浏览器悄悄关掉的东西上。
 *
 * 「取消」放最右、并且拿焦点:双击穿透时第二下落在取消上,键盘敲回车的默认结果是不签。
 */
function IssueConfirmBar({
  prompt,
  armedAt,
  onCancel,
  onConfirm,
}: {
  prompt: string;
  armedAt: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus({ preventScroll: true });
  }, []);

  return (
    <div
      role="group"
      aria-label="确认签发"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }}
      className="flex flex-col gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2"
    >
      <div className="text-[13px] whitespace-pre-line text-red-800">{prompt}</div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => {
            // 静默期:举手后 400ms 内的「确认」当误触丢掉,一声不吭
            // (真误触的人根本没看见这个条,弹提示只会更懵;真要签的人稍等半秒就过了)。
            if (Date.now() - armedAt < ARM_QUIET_MS) return;
            onConfirm();
          }}
          className="rounded-md bg-red-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-red-700 focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:outline-none pointer-coarse:min-h-11 pointer-coarse:px-4"
        >
          确认签发
        </button>
        <button
          ref={cancelRef}
          type="button"
          onClick={onCancel}
          className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[13px] text-gray-700 hover:bg-gray-50 focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:outline-none pointer-coarse:min-h-11 pointer-coarse:px-4"
        >
          取消
        </button>
      </div>
    </div>
  );
}

type FormState = { due: string; photo: string };

const EMPTY_FORM: FormState = { due: "", photo: "" };

/** 一条隐患的处置区。纯展示 + 回调,自己不发请求 —— 请求全在面板那一层,
 *  这样「同一时刻只有一个动作在飞」才好保证(法律文书不能并发点两下)。 */
function HazardRow({
  hazard,
  selected,
  busy,
  armedAction,
  armedAt,
  form,
  failure,
  onToggleSelect,
  onFormChange,
  onArm,
  onDisarm,
  onAct,
  onDismiss,
}: {
  hazard: HazardBrief;
  selected: boolean;
  /** 整个面板有请求在飞时为 true:此时所有按钮禁用。 */
  busy: boolean;
  armedAction: DisposalAction | null;
  armedAt: number;
  form: FormState;
  /** 这条隐患上一次动作的失败原因(后端的人话,原样上屏)。 */
  failure: string | null;
  onToggleSelect: () => void;
  onFormChange: (patch: Partial<FormState>) => void;
  onArm: (action: DisposalAction) => void;
  onDisarm: () => void;
  onAct: (action: DisposalAction, grade?: string, result?: "pass" | "fail") => void;
  onDismiss: () => void;
}) {
  const actions = availableActions(hazard);
  const isPending = hazard.status === "pending";
  const needsDue = actions.some(actionNeedsDuePhrase);
  const needsPhoto = actions.some(actionNeedsPhoto);

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2.5">
      <div className="flex items-start gap-2.5">
        {isPending && (
          // 只有 pending 能进批量确认(D17),所以别的状态连勾选框都不出现 ——
          // 给一个点了没用的勾选框,人会以为系统没反应。
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            disabled={busy}
            aria-label={`选中隐患 ${hazard.hazard_no}`}
            className="mt-1 size-4 shrink-0 cursor-pointer accent-blue-600 pointer-coarse:size-6"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-gray-900">{hazard.item}</span>
            {hazard.grade && (
              <Chip tone={GRADE_CHIP[hazard.grade] ?? "bg-gray-100 text-gray-600 ring-gray-200"}>
                {hazard.grade}隐患
              </Chip>
            )}
            <Chip tone={STATUS_CHIP[hazard.status] ?? "bg-gray-100 text-gray-600 ring-gray-200"}>
              {hazardStatusZh(hazard.status)}
            </Chip>
            {hazard.needs_grading && (
              // 🔴 显眼是刻意的:needs_grading=1 的隐患**任何签发都被服务端硬拦**
              // (Codex#11)。不标出来的话,人会一直点签发、一直被拒,而真正要做的
              // 是先定级 —— 界面必须把这件事说在前面。
              <Chip tone="bg-fuchsia-100 text-fuchsia-800 ring-fuchsia-300">
                ⚠ 需人工定级
              </Chip>
            )}
          </div>
          <div className="mt-0.5 font-mono text-[11px] break-all text-gray-400 select-all">
            {hazard.hazard_no}
          </div>
        </div>
        {isPending && (
          // 「否决」:见面板底部那段说明 —— 后端**没有**删除/标误报的端点,
          // 这里只是把它从本次清单里划掉。不确认本身就是安全的(pending 不算数)。
          <button
            type="button"
            onClick={onDismiss}
            disabled={busy}
            className="shrink-0 rounded px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100 hover:text-gray-700 pointer-coarse:min-h-11"
          >
            否决
          </button>
        )}
      </div>

      {hazard.needs_grading && (
        <div className="rounded-lg bg-fuchsia-50 px-2.5 py-2 text-[12px] text-fuchsia-900">
          现场判的是「待定级」(词表外的隐患项)。不知道不等于不严重 ——
          先由人定成一般或严重,才能签文书。
        </div>
      )}

      {needsDue && (
        <div className="flex flex-col gap-1">
          <Label htmlFor={`gyt-due-${hazard.hazard_no}`} className="text-[12px]">
            整改期限(写原话:明天 / 3天后 / 下周三 / 月底)
          </Label>
          {/* 原话原样送后端,**前端一行日期换算都不写** —— agents/schedule/dates.py
              用 314 行证明了中文日期不好算,红线是「只传原话,代码来算」。 */}
          <Input
            id={`gyt-due-${hazard.hazard_no}`}
            value={form.due}
            onChange={(e) => onFormChange({ due: e.target.value })}
            placeholder="如:下周三"
            disabled={busy}
          />
        </div>
      )}

      {needsPhoto && (
        <div className="flex flex-col gap-1">
          <Label htmlFor={`gyt-photo-${hazard.hazard_no}`} className="text-[12px]">
            整改后照片的编号(32 位,在聊天里那张图下面)
          </Label>
          <Input
            id={`gyt-photo-${hazard.hazard_no}`}
            value={form.photo}
            onChange={(e) => onFormChange({ photo: e.target.value })}
            placeholder="如:0123456789abcdef0123456789abcdef"
            disabled={busy}
            className="font-mono text-[12px]"
          />
          {/* D11:模型给建议、人下结论。复查照片角度光线取景都变了,
              「没拍到那个部位」和「问题已消除」在模型眼里一样 —— 那是往
              「误判合格」方向错,而这一侧会死人。 */}
          <div className="text-[11px] text-gray-400">
            复查结论由人来下:模型分不清「问题已消除」和「这张没拍到那个部位」。
          </div>
        </div>
      )}

      {armedAction ? (
        <IssueConfirmBar
          prompt={confirmPrompt(armedAction, hazard)}
          armedAt={armedAt}
          onCancel={onDisarm}
          onConfirm={() => onAct(armedAction)}
        />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {actions.map((action) =>
            action === "grade" ? (
              // 定级给两颗按钮而不是一个下拉:少一次点击,手套也点得中。
              <div key={action} className="flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-gray-500">定级为</span>
                {[GRADE_NORMAL, GRADE_SEVERE].map((grade) => (
                  <Button
                    key={grade}
                    size="sm"
                    variant={grade === GRADE_SEVERE ? "destructive" : "outline"}
                    disabled={busy || hazard.grade === grade}
                    onClick={() => onAct("grade", grade)}
                    className="pointer-coarse:min-h-11"
                  >
                    {grade}
                  </Button>
                ))}
              </div>
            ) : action === "reinspect" ? (
              <div key={action} className="flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-gray-500">复查结论</span>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => onAct("reinspect", undefined, "pass")}
                  className="pointer-coarse:min-h-11"
                >
                  <Check className="mr-1 size-3.5" />
                  合格
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => onAct("reinspect", undefined, "fail")}
                  className="pointer-coarse:min-h-11"
                >
                  不合格
                </Button>
              </div>
            ) : (
              <Button
                key={action}
                size="sm"
                variant={actionNeedsConfirm(action) ? "destructive" : "default"}
                disabled={busy}
                onClick={() => (actionNeedsConfirm(action) ? onArm(action) : onAct(action))}
                className="pointer-coarse:min-h-11"
              >
                {actionNeedsConfirm(action) && <Gavel className="mr-1 size-3.5" />}
                {ACTION_LABEL[action]}
              </Button>
            ),
          )}
          {actions.length === 0 && (
            <span className="text-[12px] text-gray-400">
              这条已经走完流程,没有下一步了。
            </span>
          )}
        </div>
      )}

      {failure && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[13px] text-red-700">
          {failure}
        </div>
      )}
    </div>
  );
}

/**
 * 监理处置面板。
 *
 * ⚠️ 必须 portal 到 body,**不能就地渲染** —— 完整的层叠上下文推演在
 * checkin.tsx 的同一处注释里(输入框外壳那层 `relative z-10` 建立了层叠上下文,
 * 把弹窗关在里面,聊天顶栏反而盖在上面;窗口一矮关闭按钮就钻到顶栏底下,
 * 点不动也没有任何视觉提示)。这里的处境完全相同,别再复现一遍那个坑。
 */
function SupervisionPanel({
  hazards,
  artifactBase,
  onClose,
}: {
  hazards: readonly HazardBrief[];
  artifactBase: string;
  onClose: () => void;
}) {
  const apiBase = useApiBase();

  /**
   * 面板自己那份清单。**只在挂载时从 props 取一次**(mergeHazards 顺便按编号去重)。
   *
   * ⚠️ 别改成「用 useEffect 持续跟着 props 合并」:调用方每次渲染都会现解析出一个
   * 新数组,而 mergeHazards 每次返回新引用 —— setState → 重渲染 → 新引用 → 再 setState,
   * 一个不会停的循环。而且面板本来就是每次打开重新挂载({open && <SupervisionPanel/>}),
   * 拿到的就是打开那一刻的清单,不需要跟。
   * 附带的好处:面板开着的时候列表**不会重排** —— 手指已经落下去了,重排就是点到
   * 别人身上,而这里每一颗按钮都是法律动作。
   */
  const [list, setList] = useState<HazardBrief[]>(() => mergeHazards([], hazards));
  const [selected, setSelected] = useState<string[]>([]);
  /** 本次面板里已经出稿的文书,**只增不减**:一次动作出的三份必须一直看得见,
   *  下一次动作不许把它们冲掉(人还没来得及点开就没了 = 又一次「点不开」)。 */
  const [docs, setDocs] = useState<SupervisionDoc[]>([]);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [armed, setArmed] = useState<{ hazardNo: string; action: DisposalAction; at: number } | null>(
    null,
  );
  const [forms, setForms] = useState<Record<string, FormState>>({});
  const [failures, setFailures] = useState<Record<string, string>>({});

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const pending = useMemo(() => pendingHazards(list), [list]);
  const selectablePending = useMemo(
    () => selected.filter((no) => pending.some((h) => h.hazard_no === no)),
    [selected, pending],
  );

  const setForm = useCallback((hazardNo: string, patch: Partial<FormState>) => {
    setForms((prev) => ({ ...prev, [hazardNo]: { ...(prev[hazardNo] ?? EMPTY_FORM), ...patch } }));
  }, []);

  /** 把某条隐患上一次动作的失败原因挂上去 / 摘掉。message 一律是后端的人话,原样上屏。 */
  const setFailure = useCallback((hazardNo: string, message: string | null) => {
    setFailures((prev) => {
      const next = { ...prev };
      if (message) next[hazardNo] = message;
      else delete next[hazardNo];
      return next;
    });
  }, []);

  /** 批量确认(pending → open)。**这是 D17 那道人工闸的全部实现。** */
  const confirmSelected = useCallback(async () => {
    let body: { hazard_nos: string[] };
    try {
      body = confirmBody(selectablePending);
    } catch (err) {
      setBanner({
        tone: "bad",
        text: err instanceof SupervisionContractError ? err.message : SUPERVISION_MESSAGES.noSelection,
      });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const outcome = await callSupervision(apiBase, "confirm", body);
      if (!outcome.ok) {
        setBanner({ tone: "bad", text: outcome.message });
        return;
      }
      const result = parseConfirmEnvelope(outcome.text);
      // 确认成功的那些,状态按契约推进到 open(端点只回编号,不回每条的新状态 ——
      // pending → open 是契约里唯一的那条边,推得动就一定是它)。
      setList((prev) =>
        result.confirmed.reduce((acc, no) => patchHazard(acc, no, { status: "open" }), prev),
      );
      // 没确认成的原因逐条挂回那一行:只报「已确认 3 条」而不说另外 2 条怎么了,
      // 人会以为全成了。
      setFailures((prev) => {
        const next = { ...prev };
        for (const no of result.confirmed) delete next[no];
        for (const item of result.failed) next[item.hazard_no] = item.reason;
        return next;
      });
      setSelected((prev) => prev.filter((no) => !result.confirmed.includes(no)));
      setBanner({ tone: "ok", text: result.user_msg || `已确认 ${result.confirmed.length} 条。` });
    } catch (err) {
      setBanner({
        tone: "bad",
        text:
          err instanceof SupervisionContractError && err.message
            ? err.message
            : SUPERVISION_MESSAGES.badEnvelope,
      });
    } finally {
      setBusy(false);
    }
  }, [apiBase, selectablePending]);

  /**
   * 举手(只给要二次确认的那两个动作)。**先把必填项验一遍再举手。**
   *
   * 顺序反过来的话会出现这一幕:期限那一格还空着,屏幕上却已经弹出
   * 「暂停令是法律文书…不能撤销」,人硬着头皮点了确认,换来一句「得写明整改期限」。
   * 那样两件事都被削弱了 —— 确认框成了噪声,而真正该改的地方在别处。
   */
  const armAction = useCallback(
    (hazard: HazardBrief, action: DisposalAction) => {
      const form = forms[hazard.hazard_no] ?? EMPTY_FORM;
      try {
        actionBody(action, {
          hazardNo: hazard.hazard_no,
          duePhrase: form.due,
          afterPhotoId: form.photo,
        });
      } catch (err) {
        setFailure(
          hazard.hazard_no,
          err instanceof SupervisionContractError ? err.message : SUPERVISION_MESSAGES.badEnvelope,
        );
        return;
      }
      setFailure(hazard.hazard_no, null);
      setArmed({ hazardNo: hazard.hazard_no, action, at: Date.now() });
    },
    [forms, setFailure],
  );

  /** 单条处置:定级 / 签发 / 复查 / 复工 / 上报。**一次只飞一个请求**(busy 全局)。 */
  const runAction = useCallback(
    async (hazard: HazardBrief, action: DisposalAction, grade?: string, result?: "pass" | "fail") => {
      const form = forms[hazard.hazard_no] ?? EMPTY_FORM;
      let body: Record<string, unknown>;
      try {
        body = actionBody(action, {
          hazardNo: hazard.hazard_no,
          duePhrase: form.due,
          afterPhotoId: form.photo,
          grade,
          result,
        });
      } catch (err) {
        // 校验没过就把举手状态一并收掉:让确认条退回按钮,人先去把那一格填对。
        // 留着确认条 + 底下一行红字,看起来像「点了确认但没反应」。
        setArmed(null);
        setFailure(
          hazard.hazard_no,
          err instanceof SupervisionContractError ? err.message : SUPERVISION_MESSAGES.badEnvelope,
        );
        return;
      }
      setBusy(true);
      setArmed(null);
      setBanner(null);
      setFailure(hazard.hazard_no, null);
      try {
        const outcome = await callSupervision(apiBase, ACTION_ENDPOINT[action], body);
        if (!outcome.ok) {
          // 后端的 user_msg 已经是人话(三条硬拦、状态机拒绝那几句都写得很具体),
          // **原样上屏**,不在前端重新包装成「操作失败」。
          setFailure(hazard.hazard_no, outcome.message);
          return;
        }
        const parsed = parseActionEnvelope(outcome.text);
        setList((prev) =>
          patchHazard(prev, parsed.hazard_no, {
            // 状态一律以**后端返回的**为准,不按前端的预期改 —— 复查合格落 closed
            // 还是 resuming 由 db 按 was_suspended 挑边,前端猜的话必然错一半。
            status: parsed.status,
            ...(parsed.grade ? { grade: parsed.grade } : {}),
            ...(parsed.needsGrading === null ? {} : { needs_grading: parsed.needsGrading }),
          }),
        );
        if (parsed.documents.length > 0) {
          setDocs((prev) => [...prev, ...parsed.documents]);
        }
        const docLine = describeDocuments(parsed.documents);
        setBanner({
          tone: "ok",
          text: parsed.user_msg || docLine || "已完成。",
        });
      } catch (err) {
        setFailure(
          hazard.hazard_no,
          err instanceof SupervisionContractError && err.message
            ? err.message
            : SUPERVISION_MESSAGES.badEnvelope,
        );
      } finally {
        setBusy(false);
      }
    },
    [apiBase, forms, setFailure],
  );

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="监理确认与处置"
    >
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      {/* 高度上限取 min(92dvh, 100dvh-2rem):理由原样见 checkin.tsx ——
          dvh 因为手机地址栏会伸缩,减 2rem 是外层那圈 p-4,少了它在矮视口上
          居中会把顶部连同关闭按钮推出视口,而外层不滚动。 */}
      <div className="relative z-10 flex max-h-[min(92dvh,calc(100dvh-2rem))] w-full max-w-2xl flex-col gap-3 overflow-y-auto rounded-2xl bg-white p-4 shadow-xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <ClipboardCheck className="size-5 text-gray-700" />
            监理确认与处置
          </div>
          {/* 触摸目标 44×44(pointer-coarse),判据不按屏宽 —— 手机横过来有 844px 宽,
              按宽度猜会退回 28px,而那时手指并没有变细(同 checkin.tsx)。 */}
          <button
            type="button"
            onClick={onClose}
            className="flex size-7 cursor-pointer items-center justify-center rounded text-gray-500 hover:bg-gray-100 pointer-coarse:size-11"
            aria-label="关闭"
          >
            <X className="size-5" />
          </button>
        </div>

        {banner && (
          <div
            aria-live="polite"
            className={
              banner.tone === "ok"
                ? "rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[13px] text-emerald-800"
                : "rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[13px] text-red-700"
            }
          >
            {banner.text}
          </div>
        )}

        {pending.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-amber-50 px-3 py-2">
            <div className="text-[13px] text-amber-900">
              待确认 {pending.length} 条 —— 确认之后才进正式流程(签文书、算整改率、能被升级)。
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  // 判据用 selectablePending(已经滤掉划走/已确认的),不用 selected 的裸长度:
                  // 那里可能留着已经不在清单里的编号,拿它比会出现「显示取消全选、点了却全选上」。
                  setSelected(() =>
                    selectablePending.length === pending.length
                      ? []
                      : pending.map((h) => h.hazard_no),
                  )
                }
                className="pointer-coarse:min-h-11"
              >
                {selectablePending.length === pending.length ? "取消全选" : "全选"}
              </Button>
              <Button
                size="sm"
                disabled={busy || selectablePending.length === 0}
                onClick={() => void confirmSelected()}
                className="pointer-coarse:min-h-11"
              >
                {busy ? (
                  <LoaderCircle className="mr-1 size-4 animate-spin" />
                ) : (
                  <Check className="mr-1 size-4" />
                )}
                确认选中 {selectablePending.length} 条
              </Button>
            </div>
          </div>
        )}

        <div className="flex flex-col gap-2">
          {list.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-300 px-3 py-8 text-center text-[13px] text-gray-500">
              这次没有登记到隐患。
            </div>
          ) : (
            list.map((hazard) => (
              <HazardRow
                key={hazard.hazard_no}
                hazard={hazard}
                selected={selected.includes(hazard.hazard_no)}
                busy={busy}
                armedAction={armed?.hazardNo === hazard.hazard_no ? armed.action : null}
                armedAt={armed?.at ?? 0}
                form={forms[hazard.hazard_no] ?? EMPTY_FORM}
                failure={failures[hazard.hazard_no] ?? null}
                onToggleSelect={() => setSelected((prev) => toggleSelected(prev, hazard.hazard_no))}
                onFormChange={(patch) => setForm(hazard.hazard_no, patch)}
                onArm={(action) => armAction(hazard, action)}
                onDisarm={() => setArmed(null)}
                onAct={(action, grade, result) => void runAction(hazard, action, grade, result)}
                onDismiss={() => {
                  setList((prev) => removeHazard(prev, hazard.hazard_no));
                  setSelected((prev) => prev.filter((no) => no !== hazard.hazard_no));
                }}
              />
            ))
          )}
        </div>

        {docs.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="text-sm font-medium text-gray-700">本次出的文书</div>
            <SupervisionDocCards documents={docs} artifactBase={artifactBase} />
          </div>
        )}

        {/* 「否决」到底做了什么 —— 必须写清楚,否则人会以为库里那条被删了。
            后端七个端点里**没有**删除或标误报的那一条(方案 §4.2 画了这条路,
            S4 没做),所以这里只是把它从本次清单里划掉。不确认本身就是安全的:
            pending 不算整改率、不进超期清单、不能被升级(D17)。 */}
        <div className="rounded-lg bg-gray-50 px-3 py-2 text-[11px] leading-relaxed text-gray-500">
          「否决」只是把这条从本清单里划掉 —— 后端还没有删除/标误报的接口,那条隐患
          会以「待确认」留在台账里。而待确认的隐患不算整改率、不进超期清单、不会被升级,
          所以不确认本身就是安全的。
          <br />
          这份清单来自你刚才那条消息(照片识别的登记结果,或者监理助手查出来的隐患)。
          要处置更早的隐患,在对话里问一句「查一下待办的隐患」,它的回复底下会出现同样的卡片。
        </div>
      </div>
    </div>,
    document.body,
  );
}

/**
 * 「待确认隐患」卡片 —— tool-calls.tsx 在工具结果里发现 `data.hazards` 时渲染它。
 *
 * 它同时是**监理面板唯一的入口**:隐患在哪张照片上发现的,就在那条消息底下处置。
 *
 * `failedItems`(D10 / Codex#10 的 `failed_items`)**必须显示**:后端那一侧已经
 * 落了一行 `hazard_ingest_failures`,但工友看不到库 —— 界面不显示的话,一条真实
 * 存在的隐患就这么没了,而屏幕上一切正常(识别回执照常报了这一项)。
 */
export function HazardIntakeCard({
  hazards,
  failedItems,
  artifactBase,
}: {
  hazards: readonly HazardBrief[];
  failedItems: readonly string[];
  artifactBase: string;
}) {
  const [open, setOpen] = useState(false);
  if (hazards.length === 0 && failedItems.length === 0) return null;

  const pendingCount = pendingHazards(hazards).length;
  const needGrading = hazards.filter((h) => h.needs_grading).length;
  const severe = hazards.filter((h) => h.grade === GRADE_SEVERE).length;

  return (
    <div className="mx-auto w-full max-w-3xl">
      <div className="my-2 flex items-start gap-3 rounded-xl border border-blue-200 bg-blue-50/40 px-4 py-3">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-blue-600" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-medium text-gray-900">隐患台账</span>
            <span className="text-[13px] text-gray-600">
              {hazards.length > 0 ? `已登记 ${hazards.length} 条` : "本次没有登记成功的隐患"}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-gray-600">
            {pendingCount > 0 && (
              <Chip tone="bg-amber-50 text-amber-700 ring-amber-200">待确认 {pendingCount} 条</Chip>
            )}
            {severe > 0 && (
              <Chip tone="bg-red-50 text-red-700 ring-red-200">严重 {severe} 条</Chip>
            )}
            {needGrading > 0 && (
              <Chip tone="bg-fuchsia-100 text-fuchsia-800 ring-fuchsia-300">
                需人工定级 {needGrading} 条
              </Chip>
            )}
          </div>

          {failedItems.length > 0 && (
            <div className="mt-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-2">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-600" />
              <div className="text-[12px] leading-snug text-red-700">
                有 {failedItems.length} 项没能记进台账:{failedItems.join("、")}。
                这几项不会进整改流程,请人工补记或找管理员查日志。
              </div>
            </div>
          )}

          {hazards.length > 0 && (
            <div className="mt-2.5">
              <Button
                size="sm"
                onClick={() => setOpen(true)}
                className="pointer-coarse:min-h-11"
              >
                <ClipboardCheck className="mr-1 size-4" />
                {pendingCount > 0 ? `去确认(${pendingCount} 条待确认)` : "打开监理处置"}
              </Button>
            </div>
          )}

          <div className="mt-1.5 text-[11px] text-gray-400">
            自动登记的隐患都是「待确认」:要有人确认过,才会进签文书 / 算整改率 / 能被升级的正式流程。
          </div>
        </div>
      </div>
      {open && (
        <SupervisionPanel
          hazards={hazards}
          artifactBase={artifactBase}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
