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
import { useQueryState } from "nuqs";
import { Camera, ImageOff, LoaderCircle, RefreshCcw, X } from "lucide-react";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { getApiKey } from "@/lib/api-key";
import {
  buildCheckinHeaders,
  CHECKIN_MESSAGES,
  CheckinSource,
  checkinUrl,
  clearEventId,
  formatCheckedAt,
  GEO_ACQUIRE_TIMEOUT_MS,
  GEO_MAX_AGE_MS,
  GeoResult,
  geoResultFromPosition,
  geoStatusFromPositionError,
  loadOrCreateEventId,
  loadSavedIdentity,
  NETWORK_ERROR_STATUS,
  normalizeError,
  NormalizedError,
  parseRecentEnvelope,
  parseReceiptEnvelope,
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

function CheckinDialog({ onClose }: { onClose: () => void }) {
  const apiBase = useApiBase();
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");

  // ⚠️ 本组件**只在点击入口后挂载**(CheckinEntry 的 open && …),永远不进
  // SSR/hydration —— 所以下面敢在首次渲染就直接读浏览器状态(localStorage /
  // navigator / location)。换成「挂载后 useEffect 再 set」反而会先闪一帧降级
  // 界面;typeof 守卫仍留着,纯防有人日后把它挪进服务端渲染树。

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
  useEffect(() => {
    if (!hasCamera || photo || receipt) return;
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
  }, [hasCamera, photo, receipt, stopStream]);

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
      setRecentError(
        err instanceof Error && err.message ? err.message : CHECKIN_MESSAGES.network,
      );
    }
  }, [apiBase]);
  useEffect(() => {
    void refreshRecent();
  }, [refreshRecent]);

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
  }, [apiBase, photo, refreshRecent, siteName, workerName]);

  /** 409 之后的出路:同 event_id 配上了不同内容(换了照片/改了名字再交),
   * 这是幂等层①在正确工作。换新 event_id = 当成新的一次打卡重新记账。 */
  const startFreshEvent = useCallback(() => {
    clearEventId(window.sessionStorage);
    setProblem(null);
  }, []);

  const startAnother = useCallback(() => {
    setReceipt(null);
    setProblem(null);
  }, []);

  // 二维码只在宽屏给:窄屏 = 多半已经在手机上,再给码是让人拿手机扫手机。
  // D7:本站地址,不带 token、不带 query(带 query 也活不过 serve_login 的
  // 登录跳转,它写死重定向 /)。
  const siteUrl = typeof window === "undefined" ? "" : window.location.origin;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const showCameraView = hasCamera && cameraState !== "failed";

  return (
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
      <div className="relative z-10 flex max-h-[92vh] w-full max-w-md flex-col gap-4 overflow-y-auto rounded-2xl bg-white p-4 shadow-xl lg:max-w-3xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <Camera className="size-5 text-gray-700" />
            工地打卡
          </div>
          <button
            type="button"
            onClick={onClose}
            className="cursor-pointer rounded p-1 text-gray-500 hover:bg-gray-100"
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

          {isLargeScreen && (
            <div className="shrink-0 lg:w-60">
              <CheckinQrPanel url={siteUrl} />
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
    </div>
  );
}

/**
 * 打卡入口 —— thread-index.tsx 把它放在输入框动作条(「上传 图纸/PDF/图片」旁),
 * 风格与那颗 Label 按钮对齐。type="button" 不能省:它被塞在聊天输入的 <form>
 * 里,裸 button 默认 type=submit,点一下会把聊天输入一起发出去。
 */
export function CheckinEntry() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex cursor-pointer items-center gap-2"
      >
        <Camera className="size-5 text-gray-600" />
        <span className="text-sm text-gray-600">打卡</span>
      </button>
      {open && <CheckinDialog onClose={() => setOpen(false)} />}
    </>
  );
}
