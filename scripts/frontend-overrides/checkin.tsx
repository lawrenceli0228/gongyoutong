/**
 * 打卡组件(W7 · C 泳道)—— **本仓自有新文件**,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/checkin.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * ── 为什么它不走聊天流 ─────────────────────────────────────────────
 * 打卡的**写入**一个 LLM 都不经过(D15):幂等在对话链里做不到(regenerate /
 * 流重连都会重放)、自拍和坐标不该发给模型、「拍照 → 出凭证」不该有概率成分。
 * 完整推演在 backend/src/gyt/checkin_api.py 的模块 docstring —— 那里同时是
 * **请求契约的唯一真相**,本文件所有收发格式都以它为准,一个字段都不许偏。
 *
 * ── 分层 ──────────────────────────────────────────────────────────
 * 所有可测逻辑都在 @/lib/checkin-lib(scripts/frontend-overrides/checkin-lib.ts,
 * 纯 TS 零依赖,scripts/frontend-tests/ 的 vitest 直接测它)。本文件只留
 * 「碰浏览器」的部分:摄像头、canvas、fetch、storage 对象的注入。
 */

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryState } from "nuqs";
import { Camera, ImageOff, LoaderCircle, RefreshCcw, Smartphone, X } from "lucide-react";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { getApiKey } from "@/lib/api-key";
import {
  advancePairState,
  buildCheckinHeaders,
  buildCheckinPageUrl,
  CHECKIN_MESSAGES,
  CHECKIN_QUERY_KEY,
  CHECKIN_QUERY_OPEN_VALUE,
  CheckinContractError,
  CheckinSource,
  checkinUrl,
  clearEventId,
  formatCheckedAt,
  GEO_ACQUIRE_TIMEOUT_MS,
  GEO_MAX_AGE_MS,
  GeoResult,
  geoResultFromPosition,
  geoStatusFromPositionError,
  HEADER_PAIR,
  loadOrCreateEventId,
  loadSavedIdentity,
  NETWORK_ERROR_STATUS,
  newPairId,
  normalizeError,
  normalizePairId,
  NormalizedError,
  PAIR_POLL_INTERVAL_MS,
  PAIR_QUERY_KEY,
  PairState,
  parsePairEnvelope,
  parseRecentEnvelope,
  parseReceiptEnvelope,
  pairScannedUrl,
  pairStatusUrl,
  Receipt,
  receiptImageUrl,
  recentUrl,
  saveIdentity,
  sha256Hex,
  withSubmitDeadline,
} from "@/lib/checkin-lib";
import { CheckinQrPanel } from "./qrcode";
import { useMediaQuery } from "@/hooks/useMediaQuery";

/**
 * 产物的静态出口。**与 human.tsx / tool-calls.tsx 的 ARTIFACT_BASE 同源,三处要一起改**
 * —— CLAUDE.md 同源清单「产物出口 ARTIFACT_BASE 这条链」的第三个读者就是本文件
 * (前两个读者的注释在 human.tsx:42-63 与 tool-calls.tsx:101-106,坑的全貌在那边:
 * 端口 8788 的三份真相、公网 mixed content、NEXT_PUBLIC_* 是编译期变量)。
 *
 * 凭证图走 `${ARTIFACT_BASE}/by-id/<artifact_id>`,与 human.tsx 的 byIdUrl
 * **完全同一条已验证路径**:同源部署时浏览器自动把登录 Cookie 带上。
 * ⚠️ 不许自开取图端点(v3 的 /checkin/photo 已废):<img> 带不了 X-Api-Key
 * 这种自定义请求头,那条路必然裂图(W7 方案 §1.3)。
 */
const ARTIFACT_BASE =
  process.env.NEXT_PUBLIC_ARTIFACT_BASE || "http://127.0.0.1:8788";

/** canvas 出 JPEG 的质量。后端反正要缩图 + 重编码(W7 §3.3),这里只求
 * 「别把一整张原图端过去」;0.92 在手机上肉眼无损、体积减半。 */
const JPEG_QUALITY = 0.92;

/**
 * 后端地址的解析**照抄 Stream.tsx(:147-180)**:URL 参数 apiUrl 优先,
 * 退 NEXT_PUBLIC_API_URL。本机 dev 是 http://localhost:2024(直连 langgraph);
 * 公网是 `${GYT_PUBLIC_ORIGIN}/api` —— Caddy 的 handle_path /api/* 剥前缀转发,
 * 同源所以登录 Cookie 自动带上。别发明第三种拿地址的路子:frontend/.env 与
 * docker-compose.vps.yml 的 build args 都只喂这两个入口。
 */
function useApiBase(): string {
  const envApiUrl = process.env.NEXT_PUBLIC_API_URL;
  const [apiUrlParam] = useQueryState("apiUrl", { defaultValue: envApiUrl || "" });
  return apiUrlParam || envApiUrl || "";
}

/**
 * 起一次定位。永不 reject —— 拿不到坐标照样能交,状态如实带上;
 * 判定规则全在 checkin-lib 的纯函数里,这里只负责把 navigator 的回调接上。
 *
 * ⚠️ **调用时机是面板一打开,不是按快门**(见下面那个 mount effect)。
 * 2026-08-15 真机实测:本机冷启动首次 fix 要 5344ms,而原来从按快门才起跑、
 * 只给 3 秒 —— 于是**每个人的第一次打卡都必然超时**。三个参数的推演见
 * checkin-lib 里 GEO_ACQUIRE_TIMEOUT_MS 那段。
 */
function acquireGeo(): Promise<GeoResult> {
  if (typeof navigator === "undefined" || !navigator.geolocation) {
    return Promise.resolve({ status: "unsupported" });
  }
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(geoResultFromPosition(pos)),
      (err) => resolve({ status: geoStatusFromPositionError(err?.code ?? -1) }),
      {
        timeout: GEO_ACQUIRE_TIMEOUT_MS,
        enableHighAccuracy: true,
        // 不设的话默认 0 = 拒绝一切缓存、每次强制重新定位。实测热缓存命中 0~2ms,
        // 而打卡要的是「在哪个地盘」不是厘米级 —— 一分钟前的位置完全够用。
        maximumAge: GEO_MAX_AGE_MS,
      },
    );
  });
}

/**
 * 生成本次面板的配对串;**失败返回 null,绝不往外抛**(W8)。
 *
 * 浏览器太老拿不到 crypto.getRandomValues 时,代价只是「电脑那边不联动」——
 * 而这行代码跑在渲染路径上,抛出去就是整个打卡面板白屏 = 连卡都打不了。
 * 配对是锦上添花,不许升级成打卡的前置条件。
 */
function tryNewPairId(): string | null {
  try {
    return newPairId();
  } catch {
    return null;
  }
}

type PhotoDraft = {
  blob: Blob;
  /** camera=现场取景截帧;fallback=<input> 选的(可能是相册旧图 = 弱凭证)。 */
  source: CheckinSource;
  previewUrl: string;
};

type Problem = {
  message: string;
  /** 409 时为 true:给「换新的一次打卡」按钮(生成新 event_id)。 */
  conflict: boolean;
};

/**
 * 把一次打卡发出去。**raw body,不是 multipart** —— 四条理由在 checkin_api.py
 * docstring「为什么是 raw body」:multipart 依赖不在后端、超 1MB 会 spool 落
 * /tmp、幂等短路不了、大小检查只能事后。元数据全在 header(buildCheckinHeaders),
 * body 就是 JPEG 原始字节。
 */
async function postCheckin(args: {
  apiBase: string;
  blob: Blob;
  source: CheckinSource;
  worker: string;
  site: string;
  geo: GeoResult;
  eventId: string;
  /** 扫码配对串(W8),没扫码进来就是 null —— 那时行为与配对上线前完全一致。 */
  pairId: string | null;
}): Promise<{ kind: "ok"; receipt: Receipt } | { kind: "fail"; failure: NormalizedError }> {
  const bytes = await args.blob.arrayBuffer();
  // 客户端只算照片 sha256(完整指纹由服务端拼,见 checkin_api.py「X-GYT-Digest
  // 不可信」节)。非安全上下文下这里会抛中文错误 —— 绝不静默省掉 digest 头。
  const digestHex = await sha256Hex(bytes);
  const apiKey = getApiKey();
  const headers: Record<string, string> = {
    ...buildCheckinHeaders({
      eventId: args.eventId,
      worker: args.worker,
      site: args.site,
      geo: args.geo,
      source: args.source,
      digestHex,
      // 配对头由 buildCheckinHeaders 决定带不带(坏了就当没有,不抛)。
      // ⚠️ 它**不进指纹**:后端 build_digest 的输入一个字节都不动 ——
      // 否则同一次打卡带不带 pair 会算出两个 digest,幂等层当场失效。
      pairId: args.pairId,
    }),
    "content-type": "image/jpeg",
    // 鉴权头与 Stream.tsx(:57)同一来源:getApiKey() 读 localStorage,
    // 退构建期注入(api-key.tsx 覆盖件)。没有令牌就不发这个头 —— 本机
    // 未开鉴权时后端也不看它。
    ...(apiKey ? { "x-api-key": apiKey } : {}),
  };
  let res: Response;
  try {
    res = await fetch(checkinUrl(args.apiBase), { method: "POST", headers, body: bytes });
  } catch {
    return { kind: "fail", failure: normalizeError(NETWORK_ERROR_STATUS, null, "") };
  }
  const bodyText = await res.text();
  if (!res.ok) {
    return {
      kind: "fail",
      failure: normalizeError(res.status, res.headers.get("content-type"), bodyText),
    };
  }
  // 重发命中(同键同指纹)也是 200 + 原凭证,对这里两者无区别 —— 契约刻意如此
  return { kind: "ok", receipt: parseReceiptEnvelope(bodyText) };
}

/** 凭证缩略图。artifact_id 为 null 时**只出这行字、绝不渲染 <img>**(上线闸③):
 * 裂图标和「图真没了」长得一模一样,分不开等于没告诉工友。 */
function ReceiptThumb({ receipt, large }: { receipt: Receipt; large?: boolean }) {
  const [broken, setBroken] = useState(false);
  const url = receiptImageUrl(receipt, ARTIFACT_BASE);

  if (url === null) {
    return (
      <div className="rounded-lg bg-gray-100 px-3 py-2 text-[12px] text-gray-500">
        凭证图已过期清理
      </div>
    );
  }
  if (broken) {
    // 打不开 ≠ 被清理。措辞与 human.tsx 的 ArtifactPhoto 同一口径:
    // 服务没起(make serve-artifacts)或文件不在这台机器上,别写死单一原因。
    return (
      <div className="flex items-start gap-2 rounded-lg bg-gray-100 px-3 py-2">
        <ImageOff className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" />
        <div className="text-[12px] leading-snug text-gray-500">
          凭证图暂时打不开(取件服务没起,或图在别的机器上)
        </div>
      </div>
    );
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title="点开看大图"
    >
      {/* 原生 <img> 而不是 next/image,理由同 human.tsx:213 那段注释 */}
      <img
        src={url}
        alt={`打卡凭证 ${receipt.receipt_no}`}
        onError={() => setBroken(true)}
        className={
          large
            ? "max-h-48 rounded-lg border border-gray-200 object-contain"
            : "h-12 w-12 rounded-lg border border-gray-200 object-cover"
        }
      />
    </a>
  );
}

/** 打卡成功后的凭证卡片:编号大字、姓名/地盤/时间、缩略图(或「已过期清理」)。 */
function ReceiptCard({ receipt }: { receipt: Receipt }) {
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-4">
      <div className="text-sm font-medium text-emerald-700">打卡成功</div>
      {/* 编号是取件/对账凭证,select-all 让人一点就能整串复制(同 human.tsx 图注) */}
      <div className="font-mono text-lg font-semibold tracking-tight break-all text-gray-900 select-all">
        {receipt.receipt_no}
      </div>
      <div className="text-sm text-gray-700">
        {receipt.worker_name}
        {receipt.site_name ? ` · ${receipt.site_name}` : ""}
        {` · ${formatCheckedAt(receipt.checked_at)}`}
      </div>
      <ReceiptThumb
        receipt={receipt}
        large
      />
      {receipt.source === "fallback" && (
        // D6:降级路径可能选到相册旧图,凭证力弱 —— 如实标注,不装现场即拍
        <div className="text-[11px] text-amber-600">
          相册上传(未必是现场即拍),凭证力较弱
        </div>
      )}
    </div>
  );
}

/** 「最近打卡」的一行。 */
function RecentRow({ receipt }: { receipt: Receipt }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-gray-200 bg-white px-3 py-2">
      <ReceiptThumb receipt={receipt} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm text-gray-800">
          {receipt.worker_name}
          {receipt.site_name ? ` · ${receipt.site_name}` : ""}
        </div>
        <div className="text-[11px] text-gray-400">
          {formatCheckedAt(receipt.checked_at)}
        </div>
      </div>
      <div className="max-w-[9rem] shrink-0 text-right font-mono text-[10px] break-all text-gray-400 select-all">
        {receipt.receipt_no}
      </div>
    </div>
  );
}

/**
 * 手机接管之后,电脑这半边(原来放取景框的位置)显示什么(W8)。
 *
 * 存在的理由是「别让人以为坏了」:取景框是被**主动**关掉的(见下面那个
 * getUserMedia effect 的 pairTookOver 分支),不说一句的话,屏幕上就只是
 * 突然空了一块。
 *
 * ⚠️ 这里**刻意不重复** PAIR_MESSAGES 那句话 —— 那句的归属地是右边的二维码面板
 * (契约第五节那张表说的就是那儿)。两块同时在屏幕上(本组件只在
 * pairTookOver 时出现,而 pairTookOver 蕴含二维码面板也在),两处说同一句话
 * 看着就像界面出了 bug。分工:右边说**手机上该干什么**,这边说**这台电脑怎么了**。
 * (2026-08-15 playwright 实测第一版就是原样抄了一遍,截出来两句话叠在一起。)
 */
function PairTakeoverNotice({ state }: { state: PairState }) {
  return (
    <div
      className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-gray-300 bg-gray-50 px-4 py-10 text-center"
      aria-live="polite"
    >
      <Smartphone className="size-6 text-gray-400" />
      <div className="text-sm text-gray-700">
        {state === "done" ? "这次打卡在手机上完成了。" : "这台电脑的取景已经关了。"}
      </div>
      <div className="text-[11px] text-gray-400">
        {state === "done"
          ? "凭证在手机上;这台电脑没取到凭证图。"
          : "手机那边拍完,这里会自动出凭证。"}
      </div>
    </div>
  );
}

function CheckinDialog({ onClose }: { onClose: () => void }) {
  const apiBase = useApiBase();
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");

  // ── 扫码配对(W8)────────────────────────────────────────────────────
  // 一台设备只可能是两个角色之一,判据就是**地址栏里有没有 pair**:
  //   有  = 是被扫的那台(手机):落地上报一次「已扫码」,提交时带 X-GYT-Pair;
  //   没有 = 是显码的那台(电脑):自己生成 pair、轮询、按状态收摄像头。
  // 用 nuqs 读 query(与上面 useApiBase 的 apiUrl 同一套):Next 的客户端路由
  // 跳转不重挂组件,自己读 location.search 会读到过期值。
  const [pairParam] = useQueryState(PAIR_QUERY_KEY);
  const scannedPairId = normalizePairId(pairParam);

  // 电脑侧自己的配对串。放 state 而不是 ref/常量:「再打一次」要换一个新的
  // (旧串已经是 done,不换的话新一轮打卡在电脑上永远不显示)。
  const [hostPairId, setHostPairId] = useState<string | null>(() => tryNewPairId());
  const [pairState, setPairState] = useState<PairState>("waiting");

  // 二维码只在宽屏给:窄屏 = 多半已经在手机上,再给码是让人拿手机扫手机。
  // 被扫的那台不给码 —— 它自己就是「手机」,再显一个码只会让人对着自己扫。
  const showQrPanel = isLargeScreen && !scannedPairId;
  /** 轮询的开关:显着码才值得问。没显码的那台问的是一个没人会扫的串,纯浪费
   * ——而且配对桶是**全站共用一只**(后端 PAIR_BUCKET_IDENTITY 是固定串)。 */
  const isPairHost = showQrPanel && hostPairId !== null;
  /**
   * 手机已经接手:停本机取景,界面换成等待/完成。
   *
   * ⚠️ **判据里不许再 AND 上 isPairHost(或任何含 isLargeScreen 的东西)。**
   * useMediaQuery 是**活订阅**、可来回翻,而 pairState 是单调的 —— 两者一 AND,
   * 就出现这条路径:手机扫上了(取景已关)→ 有人拖窄窗口 / 拔掉投影 →
   * isLargeScreen 变 false → 取景**自己又亮起来**、拍照表单也回来了,
   * 于是同一个人可以在电脑上再打一次(新的 event_id,幂等层拦不住,库里两条)。
   * 而这恰恰就是这个功能瞄准的场景:讲台上一边投影一边掏手机。
   * pairState 只可能被本机的轮询推进(轮询只在显码那台跑),所以单看它足够安全:
   * 手机侧、以及从没显过码的机器,它恒为 waiting。
   */
  const pairTookOver = pairState !== "waiting";

  // ⚠️ 本组件永远不进 SSR/hydration —— 所以下面敢在首次渲染就直接读浏览器状态
  // (localStorage / navigator / location)。换成「挂载后 useEffect 再 set」
  // 反而会先闪一帧降级界面;typeof 守卫仍留着,纯防有人日后把它挪进服务端渲染树。
  //
  // W8 之后它多了一个挂载入口(地址栏 ?checkin=1,见 CheckinEntry),前提靠
  // 那边的 mounted 开关继续成立 —— **别把那道开关当多余的样板删掉**:
  // 服务端同样看得见 ?checkin=1,删了它这个组件就会在服务端渲染一遍,
  // 而它在那边只会返回 null(没有 document),客户端却渲染出 portal 弹窗,
  // 结果是控制台一条 hydration 报错(2026-08-15 实测过,原文抄在那边)。

  // ── 身份:localStorage 记住上次(键名 gyt 前缀)。这就是 TODO-35 说的
  // 「身份只是个字符串」:没有实名、没有账号,记住只是省打字,不是登录态。
  const [savedIdentity] = useState(() =>
    typeof window === "undefined"
      ? { worker: "", site: "" }
      : loadSavedIdentity(window.localStorage),
  );
  const [workerName, setWorkerName] = useState(savedIdentity.worker);
  const [siteName, setSiteName] = useState(savedIdentity.site);

  // ── 取像:特性检测,**不许嗅 UA**(W7 §1.9)。WhatsApp / Facebook / Instagram
  // 的内置浏览器全是 WKWebView,iOS 14.3 起「符合条件的」WKWebView 也可能有
  // getUserMedia —— 有没有取决于宿主 App,UA 白名单在这个现实下必然漏。
  // navigator.mediaDevices 在非安全上下文(http://<局域网IP>)同样是 undefined,
  // 检测顺带把那种情况归进降级线。
  const hasCamera =
    typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
  const [cameraState, setCameraState] = useState<"idle" | "live" | "failed">("idle");
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);

  const [photo, setPhoto] = useState<PhotoDraft | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const geoPromiseRef = useRef<Promise<GeoResult> | null>(null);

  const stopStream = useCallback(() => {
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    mediaStreamRef.current = null;
  }, []);

  // 定位在**面板一打开**就起跑,不等按快门。
  //
  // 为什么这是关键的一步(2026-08-15 真机实测后加的):本机冷启动首次 fix 要
  // 5344ms,而用户从打开面板到按快门要框取景、打姓名 —— 十几秒起步,那段时间
  // 本来就是白等的。让定位跑在那里面,按快门时结果早已就绪(实测再取 0~2ms)。
  //
  // 空依赖数组是刻意的:一次面板生命周期只起一次。重拍不重新定位 ——
  // maximumAge 已经声明了「一分钟内的位置都算数」,面板开着的那点时间里
  // 人不会换地盘,再请求一次只是白费电。
  useEffect(() => {
    geoPromiseRef.current = acquireGeo();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 起前置摄像头。photo 进依赖是刻意的:重拍(photo 清空)时自动重启取景;
  // 拍完即停流(下面 capture 里),预览期间摄像头灯灭 —— 别让人觉得一直被拍。
  //
  // pairTookOver 也进依赖(W8):手机一扫上,这台电脑的取景**必须立刻停** ——
  // 演示时投影上一直挂着主讲人的脸,而他正低头看手机。停的方式是让本 effect
  // 提前 return,**由 cleanup 里那句 stopStream 去关**,不另写一处关流逻辑:
  // 多一个入口就多一次「关了流但 mediaStreamRef 没清」的机会。
  useEffect(() => {
    if (!hasCamera || photo || receipt || pairTookOver) return;
    let cancelled = false;
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "user" }, audio: false })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        mediaStreamRef.current = stream;
        if (videoRef.current) videoRef.current.srcObject = stream;
        setCameraState("live");
      })
      .catch(() => {
        // 特性存在 ≠ 一定给用(权限被拒 / 宿主 App 没申请相机权限)。
        // 降到 <input capture> 那条线,source=fallback 如实上报。
        if (!cancelled) setCameraState("failed");
      });
    return () => {
      cancelled = true;
      stopStream();
    };
  }, [hasCamera, photo, receipt, pairTookOver, stopStream]);

  // 预览 URL 是 createObjectURL 出来的,换图/关面板都要 revoke,不然内存里堆图
  useEffect(() => {
    return () => {
      if (photo) URL.revokeObjectURL(photo.previewUrl);
    };
  }, [photo]);

  const takePhoto = useCallback((blob: Blob, source: CheckinSource) => {
    // 定位**不在这里起跑** —— 它在面板打开时就跑上了(上面那个 mount effect)。
    // 这里只兜一种情况:effect 因故没跑成(理论上不会,留着是防御)。
    geoPromiseRef.current ??= acquireGeo();
    setPhoto({ blob, source, previewUrl: URL.createObjectURL(blob) });
    setProblem(null);
  }, []);

  const capture = useCallback(() => {
    const video = videoRef.current;
    if (!video || video.videoWidth === 0) {
      setProblem({ message: "摄像头还没就绪,等一秒再拍。", conflict: false });
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      setProblem({ message: "这台浏览器截不了图,请用下面的选图方式。", conflict: false });
      return;
    }
    // 预览是镜像的(自拍习惯),成图**不**镜像 —— 水印要的是真实场景
    ctx.drawImage(video, 0, 0);
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          setProblem({ message: "出图失败,再拍一次。", conflict: false });
          return;
        }
        takePhoto(blob, "camera");
        stopStream();
        setCameraState("idle");
      },
      "image/jpeg",
      JPEG_QUALITY,
    );
  }, [stopStream, takePhoto]);

  const onFilePicked = useCallback(
    (event: FormEvent<HTMLInputElement>) => {
      const file = event.currentTarget.files?.[0];
      if (!file) return;
      // accept 只是提示,浏览器不强制;后端还有 JPEG 魔数闸,这里先把明显不对的挡下
      if (file.type && file.type !== "image/jpeg") {
        setProblem({ message: "只收 JPEG 照片(手机相机拍出来的就是)。", conflict: false });
        return;
      }
      takePhoto(file, "fallback");
    },
    [takePhoto],
  );

  const retake = useCallback(() => {
    setPhoto(null);
    setProblem(null);
  }, []);

  // ── 最近打卡(GET /checkin/recent):凭证的持久化出口 —— 刷新页面、换设备
  // 之后凭证还在,不再只活在组件瞬时状态里(W7 §3.7 点名的真缺口)。
  const [recent, setRecent] = useState<Receipt[] | null>(null);
  const [recentError, setRecentError] = useState<string | null>(null);
  const refreshRecent = useCallback(async () => {
    try {
      const apiKey = getApiKey();
      const res = await fetch(recentUrl(apiBase), {
        headers: apiKey ? { "x-api-key": apiKey } : undefined,
      });
      const bodyText = await res.text();
      if (!res.ok) {
        setRecentError(
          normalizeError(res.status, res.headers.get("content-type"), bodyText).message,
        );
        return;
      }
      setRecent(parseRecentEnvelope(bodyText));
      setRecentError(null);
    } catch (err) {
      // ⚠️ 只有**我们自己抛的**中文契约错误才照原样上屏。
      // 原来这里是 `err instanceof Error ? err.message : …`,而 fetch 连不上时抛的是
      // TypeError("Failed to fetch") —— 于是后端一没起,工地师傅的手机上就明晃晃
      // 一行英文(2026-08-15 W8 做浏览器验证时当场截到,面板底下写着「最近打卡
      // Failed to fetch」)。本仓的规矩是面向用户的字符串一律说人话,
      // 浏览器/运行时抛的原文一个字都不许上屏。
      setRecentError(
        err instanceof CheckinContractError && err.message
          ? err.message
          : CHECKIN_MESSAGES.network,
      );
    }
  }, [apiBase]);
  useEffect(() => {
    void refreshRecent();
  }, [refreshRecent]);

  // ── 配对①(手机侧):面板一打开就上报一次「已扫码」──────────────────
  const scanReportedRef = useRef(false);
  useEffect(() => {
    if (!scannedPairId || scanReportedRef.current) return;
    // **先置位再发**。用 ref 不用 state:StrictMode 下 effect 会连着跑两次,
    // 而 setState 式的守卫要等下一次 render 才生效,拦不住紧挨着的第二次 ——
    // 表现是每次开面板都上报两遍(后端幂等,但那是拿别人的幂等擦自己的屁股)。
    scanReportedRef.current = true;
    const apiKey = getApiKey();
    // **失败一律静默,而且不重试**:上报不成只是电脑那边不联动,手机照样拍照打卡。
    // 这时候在手机上弹一句红字,工友只会以为卡打不了(W8 契约第五节那张表:
    // 「轮询失败 → 不显示任何错误」,上报同理)。
    // 不接 AbortController:这是个没有响应体、不写任何 state 的一次性 POST,
    // 面板关了它跑完就完,没有「组件卸载后 setState」那类隐患。
    void fetch(pairScannedUrl(apiBase), {
      method: "POST",
      headers: {
        ...(apiKey ? { "x-api-key": apiKey } : {}),
        [HEADER_PAIR]: scannedPairId,
      },
    }).catch(() => {
      // 连不上/后端还没上这个接口 —— 都不关工友的事
    });
  }, [apiBase, scannedPairId]);

  // ── 配对②(电脑侧):每 2 秒问一次「有人扫了吗、打完了吗」─────────────
  //
  // 为什么是轮询不是 SSE:Caddyfile 只给 handle_path /api/* 配了 flush_interval -1,
  // @checkin 那条专用路由没有 —— SSE 会被缓冲住,表现是「事件全都晚到或不到」
  // 且不报错(W8 契约开头)。人拍一张照十几秒,2 秒一轮绰绰有余。
  //
  // pairState 进依赖是刻意的:状态一变就重建这个循环 —— done 之后
  // 上面那个 early return 会**彻底停掉轮询**,不留一个空转的 setInterval。
  useEffect(() => {
    if (!isPairHost || !hostPairId || pairState === "done") return;
    const controller = new AbortController();
    let stopped = false;
    let inFlight = false;
    const poll = async () => {
      // 上一发还没回来就跳过这一拍。信号一差(工地常态),没有这道闸请求会**叠**着发:
      // 后端那只配对桶是全站共用的固定串、每分钟 60,而 2 秒一发本来就只留了一倍余量
      // —— 叠起来先把自己挤成 429,换来的还只是同一个问题问两遍。
      if (inFlight) return;
      inFlight = true;
      try {
        const apiKey = getApiKey();
        const res = await fetch(pairStatusUrl(apiBase, hostPairId), {
          headers: apiKey ? { "x-api-key": apiKey } : undefined,
          signal: controller.signal,
        });
        // 非 200 一律当「这一轮没消息」:404(后端还没上这条路由)、401、
        // 429 全都不上屏 —— 屏幕上那句「用手机扫这个码」本身没有错,
        // 在它旁边挂一行红字只会让人不敢扫。
        if (!res.ok) return;
        const snapshot = parsePairEnvelope(await res.text());
        if (!snapshot || stopped) return;
        // 只前进不后退:乱序到达的旧响应、以及 TTL 过期后后端回的 waiting,
        // 都不许把「✅ 已打卡成功」打回「请扫码」(advancePairState 头注)。
        setPairState((prev) => advancePairState(prev, snapshot.state));
        if (snapshot.state === "done" && snapshot.receipt) {
          // 直接喂给本组件既有的 receipt 状态 —— 凭证卡片、「再打一次」那一套
          // 原样复用,不为配对另写一份凭证渲染。
          setReceipt(snapshot.receipt);
          void refreshRecent();
        }
      } catch {
        // 网络异常、后端没起、面板关闭时的 abort —— 全部静默,下一轮再说
      } finally {
        // 必须放 finally:早退(!res.ok)和异常都得把闸放开,否则一次失败之后
        // 这个循环再也不发第二发,而界面上一点征兆都没有
        inFlight = false;
      }
    };
    void poll(); // 先来一次,别让人对着二维码干等两秒
    const timer = setInterval(() => void poll(), PAIR_POLL_INTERVAL_MS);
    // **面板一关就把两样都收掉**:定时器不清 = 关了面板还在打接口;
    // 请求不 abort = 关掉的瞬间那一发还在路上,回来时组件已经没了。
    return () => {
      stopped = true;
      clearInterval(timer);
      controller.abort();
    };
  }, [apiBase, hostPairId, isPairHost, pairState, refreshRecent]);

  const submit = useCallback(async () => {
    if (!photo) {
      setProblem({ message: "先拍一张再打卡。", conflict: false });
      return;
    }
    if (!workerName.trim()) {
      setProblem({ message: "先填姓名再打卡。", conflict: false });
      return;
    }
    setSubmitting(true);
    setProblem(null);
    try {
      // event_id 落 sessionStorage:刷新/崩溃后复用同一 ID,重发被幂等层①
      // 还回原凭证而不是记两笔账(checkin_api.py「客户端也要负责」节)。
      const eventId = loadOrCreateEventId(window.sessionStorage);
      // 定位早就在跑了(面板打开那一刻),这里只给它最后 1.5 秒。等不到就按
      // timeout 状态照发 ——「不为定位挡住打卡」这条原则由 withSubmitDeadline
      // 结构性保证,不靠把定位预算调短来碰运气(调短的后果就是首打必空)。
      const geo = await withSubmitDeadline(geoPromiseRef.current ?? acquireGeo());
      const outcome = await postCheckin({
        apiBase,
        blob: photo.blob,
        source: photo.source,
        worker: workerName,
        site: siteName,
        geo,
        eventId,
        // 扫码进来的才有;没有就是普通打卡,与配对上线前一模一样
        pairId: scannedPairId,
      });
      if (outcome.kind === "fail") {
        setProblem({
          message: outcome.failure.message,
          conflict: outcome.failure.status === 409,
        });
        return;
      }
      // **成功才清幂等键**;失败留着,重试要靠它防重复记账
      clearEventId(window.sessionStorage);
      saveIdentity(window.localStorage, workerName, siteName);
      setReceipt(outcome.receipt);
      setPhoto(null);
      void refreshRecent();
    } catch (err) {
      // sha256Hex(非 https)/ buildCheckinHeaders / parseReceiptEnvelope 的中文都到这
      setProblem({
        message: err instanceof Error && err.message ? err.message : "出了点问题,再试一次。",
        conflict: false,
      });
    } finally {
      setSubmitting(false);
    }
  }, [apiBase, photo, refreshRecent, scannedPairId, siteName, workerName]);

  /** 409 之后的出路:同 event_id 配上了不同内容(换了照片/改了名字再交),
   * 这是幂等层①在正确工作。换新 event_id = 当成新的一次打卡重新记账。 */
  const startFreshEvent = useCallback(() => {
    clearEventId(window.sessionStorage);
    setProblem(null);
  }, []);

  const startAnother = useCallback(() => {
    setReceipt(null);
    setProblem(null);
    // 配对也要一并重来:旧串已经是 done(后端不许回退),留着它下一次打卡
    // 在电脑上永远不显示 —— 换一个新串 = 换一张新码 = 干净的一轮。
    setPairState("waiting");
    setHostPairId(tryNewPairId());
    // 取景刚才被配对停掉了,cameraState 还停在 "live"。不退回 idle 的话,
    // 新流还没起来的那零点几秒里按钮已经写着「拍照」——点下去只会撞上
    // capture() 里那句「摄像头还没就绪」。在这里退(用户点击的路径上),
    // 而不是另开一个 effect 盯着 pairTookOver:effect 里同步 setState 会多一轮
    // 级联渲染,而这件事本来就只在「再打一次」这一个出口上发生。
    setCameraState("idle");
  }, []);

  // 二维码里编的地址(W8 契约第二节):`<origin>/?checkin=1&pair=<32位hex>`。
  // checkin=1 负责开面板、pair 负责联动,**缺一不可** —— 只带 pair 的话手机
  // 落在首页,电脑永远停在 waiting,而且一声不吭。
  // 码里为什么可以带 query、又为什么仍然不带 token:见 qrcode.tsx 头注的 D7。
  const qrUrl =
    typeof window === "undefined"
      ? ""
      : buildCheckinPageUrl(window.location.origin, hostPairId);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const showCameraView = hasCamera && cameraState !== "failed" && !pairTookOver;

  // ⚠️ 必须 portal 到 body,**不能就地渲染**。就地渲染时这个对话框的祖先链是:
  //   弹窗(fixed z-50) → 动作条 → <form> → 输入框外壳(relative z-10) → …
  //     → 聊天滚动区(absolute inset-0) → 内容区(relative,z:auto)
  //   输入框外壳那层 `relative z-10` **建立了层叠上下文**,把弹窗关在里面 ——
  //   `z-50` 是它在那个盒子内部的名次,出不了盒子。而 thread-index.tsx 那条聊天
  //   顶栏(`absolute top-0 left-0 z-10`)挂在更外面一层,跟整棵 z:auto 的子树比,
  //   **顶栏赢**。于是顶栏那 62px 高的条子盖在弹窗上面。
  //
  //   症状很阴:窗口够高时卡片被居中推低、关闭按钮落在 62px 以下,看着一切正常;
  //   窗口一矮(实测 ≤700px)卡片上移,X 的中心到了 y=58,**正好钻进顶栏底下,
  //   点不动也没有任何视觉提示**(顶栏是半透明 backdrop-blur,X 还看得见)。
  //   加大 z-index 没用 —— 嵌套层叠上下文里再大的数也出不去。
  //   portal 到 body 让它跟顶栏同台竞争,z-50 才真的是 50。
  //   顺带治好一个隐患:姓名/地盤输入框本来在聊天 <form> 里,按回车会触发聊天发送;
  //   portal 之后它们不在那个 form 的 DOM 子树里了,回车不再误发。
  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="工地打卡"
    >
      {/* 点蒙层关掉。提交中途关掉也安全:event_id 还在 sessionStorage,
          重开重交会被幂等层①还回同一条,不会记两笔账 —— 这正是它存在的意义 */}
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
      />
      {/* 高度上限取 min(两者):
          · 92dvh —— 正常屏上留一圈透气的边,和以前一样;用 dvh 不用 vh,是因为手机
            浏览器的地址栏会伸缩,vh 量的是「地址栏收起时」的最大高度,地址栏露着的
            时候卡片就比可见区域高;
          · calc(100dvh-2rem) —— 减掉外层那圈 p-4(上下各 16px)。少了这一项,
            视口矮到 400px 以下时(手机横过来就是 390px)92dvh+32px 会超出视口,
            外层 items-center 居中把超出的部分**上下均分**,顶部连同关闭按钮被推到
            视口外面,而外层不滚动 —— 又是一次「看得见点不着」。 */}
      <div className="relative z-10 flex max-h-[min(92dvh,calc(100dvh-2rem))] w-full max-w-md flex-col gap-4 overflow-y-auto rounded-2xl bg-white p-4 shadow-xl lg:max-w-3xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <Camera className="size-5 text-gray-700" />
            工地打卡
          </div>
          {/* 触摸设备 44×44:原来是 p-1 撑出来的 28×28,约 4.8mm,比 login-page.html
              自己定的 --tap 触摸地板(56px,注释写明是按「戴手套的手」定的)矮一半。
              图标尺寸不变,只把可点区域撑开。
              判据用 `pointer-coarse`(Tailwind v4 的 `(pointer: coarse)`)而不是 `sm:`:
              按宽度猜触摸设备是错的 —— 手机横过来有 844px 宽,会命中 sm: 退回 28×28,
              而那时手指并没有变细。鼠标设备保持原来的紧凑观感。 */}
          <button
            type="button"
            onClick={onClose}
            className="flex size-7 cursor-pointer items-center justify-center rounded text-gray-500 hover:bg-gray-100 pointer-coarse:size-11"
            aria-label="关闭"
          >
            <X className="size-5" />
          </button>
        </div>

        <div className="flex flex-col gap-4 lg:flex-row">
          <div className="flex min-w-0 flex-1 flex-col gap-3">
            {receipt ? (
              <>
                <ReceiptCard receipt={receipt} />
                <Button
                  variant="outline"
                  onClick={startAnother}
                >
                  再打一次
                </Button>
              </>
            ) : pairTookOver ? (
              // 手机接手之后,这半边不再收任何输入:姓名/地盤是手机上填的,
              // 取景已经关了。留一块说明,而不是留一片空白或一个点不动的按钮。
              <PairTakeoverNotice state={pairState} />
            ) : (
              <>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="gyt-checkin-worker">姓名</Label>
                    <Input
                      id="gyt-checkin-worker"
                      value={workerName}
                      onChange={(e) => setWorkerName(e.target.value)}
                      placeholder="如:陳大文"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="gyt-checkin-site">地盤(可不填)</Label>
                    <Input
                      id="gyt-checkin-site"
                      value={siteName}
                      onChange={(e) => setSiteName(e.target.value)}
                      placeholder="如:观塘A栋"
                    />
                  </div>
                </div>

                {photo ? (
                  <div className="flex flex-col gap-2">
                    {/* 预览用本地 objectURL,不镜像 —— 这就是要交上去的那张 */}
                    <img
                      src={photo.previewUrl}
                      alt="待提交的打卡照片"
                      className="max-h-64 w-full rounded-xl border border-gray-200 object-contain"
                    />
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        onClick={retake}
                        disabled={submitting}
                      >
                        <RefreshCcw className="mr-1 size-4" />
                        重拍
                      </Button>
                      <Button
                        className="flex-1"
                        onClick={() => void submit()}
                        disabled={submitting}
                      >
                        {submitting ? (
                          <>
                            <LoaderCircle className="mr-1 size-4 animate-spin" />
                            正在打卡…
                          </>
                        ) : (
                          "打卡"
                        )}
                      </Button>
                    </div>
                  </div>
                ) : showCameraView ? (
                  <div className="flex flex-col gap-2">
                    {/* playsInline + muted 是 iOS 自动播放的前提;预览镜像(-scale-x-100)
                        符合自拍习惯,capture 画到 canvas 的是未镜像的原始帧 */}
                    <video
                      ref={videoRef}
                      autoPlay
                      playsInline
                      muted
                      className="aspect-[3/4] w-full -scale-x-100 rounded-xl border border-gray-200 bg-gray-900 object-cover"
                    />
                    <Button
                      onClick={capture}
                      disabled={cameraState !== "live"}
                    >
                      <Camera className="mr-1 size-4" />
                      {cameraState === "live" ? "拍照" : "正在起摄像头…"}
                    </Button>
                  </div>
                ) : (
                  <div className="flex flex-col gap-2">
                    {/* 降级线(D6):没有 getUserMedia(内置浏览器/权限被拒)就走
                        <input capture>。capture="user" 的支持因浏览器与设备而异
                        (W7 §1.9,不能断言谁一律忽略):支持的直接调起前置相机,
                        不支持的落到相册选图 —— 那可能是旧图 = 弱凭证,
                        source=fallback 如实上报,后端与凭证卡片都会标出来。 */}
                    <input
                      id="gyt-checkin-file"
                      type="file"
                      accept="image/jpeg,image/jpg"
                      capture="user"
                      onChange={onFilePicked}
                      className="hidden"
                    />
                    <Label
                      htmlFor="gyt-checkin-file"
                      className="flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-gray-300 bg-gray-50 px-4 py-8 text-sm text-gray-600 hover:bg-gray-100"
                    >
                      <Camera className="size-5" />
                      拍一张自拍(或从相册选 JPEG)
                    </Label>
                    {hasCamera && cameraState === "failed" && (
                      <div className="text-[11px] text-gray-400">
                        取景起不来(多半是相机权限没给),已换成选图方式。
                      </div>
                    )}
                  </div>
                )}

                {problem && (
                  <div className="flex flex-col gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2">
                    <div className="text-[13px] text-red-700">{problem.message}</div>
                    {problem.conflict && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={startFreshEvent}
                      >
                        换新的一次打卡
                      </Button>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {showQrPanel && (
            <div className="shrink-0 lg:w-60">
              <CheckinQrPanel
                url={qrUrl}
                state={pairState}
              />
            </div>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <div className="text-sm font-medium text-gray-700">最近打卡</div>
          {recentError ? (
            <div className="text-[12px] text-gray-400">{recentError}</div>
          ) : recent === null ? (
            <div className="text-[12px] text-gray-400">正在取…</div>
          ) : recent.length === 0 ? (
            <div className="text-[12px] text-gray-400">还没有人打过卡。</div>
          ) : (
            <div className="flex flex-col gap-1.5">
              {recent.map((row) => (
                <RecentRow
                  key={row.receipt_no}
                  receipt={row}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/**
 * 打卡入口 —— thread-index.tsx 把它放在输入框动作条(「上传 图纸/PDF/图片」旁),
 * 风格与那颗 Label 按钮对齐。type="button" 不能省:它被塞在聊天输入的 <form>
 * 里,裸 button 默认 type=submit,点一下会把聊天输入一起发出去。
 */
export function CheckinEntry() {
  // ── 面板的开关有两个来源(W8)────────────────────────────────────────
  //   ① 手点这颗按钮;
  //   ② 地址栏里的 ?checkin=1 —— 手机扫码落地(经登录页带 next 跳回来)之后
  //      面板**自动打开**。少了这一条,扫码的人落在首页,还得自己在动作条里
  //      找那颗「打卡」按钮:工地上找不着就等于没有。
  // 用 nuqs 而不是自己读 location.search:Next 的客户端路由跳转不重挂组件,
  // 手读 search 会读到过期值(与本文件 useApiBase 读 apiUrl 同一套)。
  const [checkinParam, setCheckinParam] = useQueryState(CHECKIN_QUERY_KEY);
  const [manualOpen, setManualOpen] = useState(false);

  // ⚠️ URL 那条路**必须等挂载之后**才算数,少了 mounted 这一道就是 hydration 报错。
  // 2026-08-15 用 playwright 实测到的原话:
  //   "Hydration failed because the server rendered HTML didn't match the client"
  //   diff 显示服务端那一格是发送按钮、客户端多出了 role="dialog" 那个 div。
  // 成因:带 ?checkin=1 直接进站时,服务端也看得见这个 query,于是 CheckinDialog
  // 在服务端就渲染了一遍 —— 它里头 `typeof document === "undefined"` 时返回 null,
  // 而客户端返回的是 createPortal 出来的弹窗,两边对不上。
  // (React 会自己重建这棵子树,界面看着正常,只有控制台里那一条 —— 典型的
  //  「能用但一直在报错」,下一个人查别的问题时会被它带偏。)
  // 顺带:mounted 也守住了 CheckinDialog 头注那条前提「本组件永远不进
  // SSR/hydration」—— 它敢在首次渲染直接读 localStorage/navigator,靠的就是这条。
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const open = manualOpen || (mounted && checkinParam === CHECKIN_QUERY_OPEN_VALUE);

  const close = useCallback(() => {
    setManualOpen(false);
    // URL 上那个 checkin=1 必须一起擦掉,否则「关」只活到下一次 render ——
    // open 是「手点开 || URL 说开」,URL 还说开就永远关不掉,点 X 没反应。
    // nuqs 默认走 replace,不往后退栈里塞记录(否则退一步又把面板退开了)。
    void setCheckinParam(null);
  }, [setCheckinParam]);

  return (
    <>
      {/* ⚠️ 打卡是给工地上拿手机的人用的,这颗按钮在手机上必须点得中。
          2026-08-15 在 390×844 实测:动作条被 flex 挤压,这颗只剩 **42px 宽 × 40px 高**,
          「打/卡」两个字竖排 —— 目标比指尖还小。三件套的分工:
          · whitespace-nowrap —— 不许再竖排(治本,与被挤多窄无关);
          · shrink-0 —— 不被兄弟元素压;
          · min-h-11 / min-w-11(44px)—— 触摸目标的通用下限,只在窄屏生效,
            sm: 之后归零,桌面端保持今天这颗「无边框图标+字」的观感不变。 */}
      <button
        type="button"
        onClick={() => setManualOpen(true)}
        className="flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap sm:min-h-0 sm:min-w-0"
      >
        <Camera className="size-5 text-gray-600" />
        <span className="text-sm text-gray-600">打卡</span>
      </button>
      {open && <CheckinDialog onClose={close} />}
    </>
  );
}
