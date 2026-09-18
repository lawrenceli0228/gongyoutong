/**
 * 拍完照,回答上方那張**能點的隱患卡**(2026-09-18 設計審查 FINDING-001)。
 *
 * 新增組件(上游 agent-chat-ui 沒有),由 scripts/setup-frontend.sh 用 install_new_file
 * 拷進 frontend/src/components/thread/hazard-result-card.tsx,ai.tsx 在**本輪最後一條**
 * 調度中樞回答上方掛一次(判據在 ai.tsx 的 turnPhotoIds / isLastAnswerOfTurn)。
 *
 * ===========================================================================
 * 為什麼非有不可
 * ---------------------------------------------------------------------------
 * 在它之前,工友拍完照看到的是一段文字:「這兩條已經登記成待確認隱患了(用電 GYT-H-…、
 * 堆料 GYT-H-…),等監理確認後進正式流程。」—— 系統其實已經幹完了活(隱患真進了台賬),
 * 對話裏卻**沒有任何可點的東西**;確認 / 否決 / 去處置全在底欄那顆 12px 的「隱患」後面。
 * 以「拍一張照到監理簽發通知單」計是 13 個動作,這張卡把它降到 6 個。
 *
 * ===========================================================================
 * 數據從哪來 —— **不等工具返回,拿照片編號直查台賬**
 * ---------------------------------------------------------------------------
 * supervisor 的 `output_mode="last_message"` 把子 Agent 的工具返回整個丟掉,
 * tool-calls.tsx 那張 HazardIntakeCard 從 W9 起**一次都沒渲染出來過**(CLAUDE.md 記着,
 * 三條取數路徑實測全堵)。而用戶消息裏有後端拼進去的 `(照片编号:<32位hex>)`,那是穩的:
 * 這張卡拿它打 `GET /supervision/hazards?photo_id=a,b`(2026-09-18 加的參數,按照片看、
 * 狀態不限),再在客戶端按 photo_id 篩一遍兜底(老後端不認這個參數時會整張台賬回來)。
 *
 * 取數時機:**這一輪跑完**(stream.isLoading 翻 false)才拉 —— 登記發生在識隱患那一步,
 * 跑着的時候拉到的可能是半截。拉不到 / 一條都沒有 → 整張卡不渲染(回答正文自己會說
 * 「沒看出隱患」,卡上寫「0 條」是噪音)。
 *
 * ===========================================================================
 * 卡上能做什麼 —— 只做**待確認**那一步的兩件事
 * ---------------------------------------------------------------------------
 *   · 確認是隱患 → `POST /supervision/confirm`(與面板的批量確認同一端點,只帶這一條);
 *   · 不是隱患   → `POST /supervision/reject`(真刪,所以要二次確認:先「確定刪掉?」再刪)。
 * 定級 / 簽發 / 複查那些**不搬到這兒** —— 那是監理的事、是法律動作,面板裏有完整的
 * 步驟條與二次確認;卡底「去監理處置 →」一下就到(dispatch OPEN_SUPERVISION_EVENT,
 * SupervisionEntry 收到就開面板)。
 *
 * ⚠️ 繁體:標籤源碼裏就是繁體。後端來的違規項名 / 狀態中文名**跟着答話的語種走**
 *    (`useHantText`,與 ai.tsx 的答話同一條規則),**不走 `useHantUI`** —— 這張卡長在
 *    聊天流裏、常駐,`useHantUI` 會讓每個簡體工友首屏拉 438 KB 字典
 *    (hant-ui-strings.test.ts 那條守衛盯着)。
 * ⚠️ 後端的 user_msg 原樣上屏,一個字不轉(W12 復審定案)。
 */

import { useCallback, useEffect, useState } from "react";
import { Check, ChevronRight, ShieldAlert, Trash2 } from "lucide-react";
import { useStreamContext } from "@/providers/Stream";
import { getApiKey } from "@/lib/api-key";
import { useHantText } from "@/lib/hant-convert";
import type { Lang } from "@/lib/lang-lib";
import {
  actionBody,
  confirmBody,
  HAZARD_SCOPE_ALL,
  hazardListUrl,
  hazardsForPhotos,
  hazardStatusZh,
  normalizeError,
  parseActionEnvelope,
  parseConfirmEnvelope,
  parseHazardListEnvelope,
  patchHazard,
  removeHazard,
  requestOpenSupervision,
  REJECTED_STATUS,
  SUPERVISION_MESSAGES,
  SupervisionContractError,
  type HazardBrief,
} from "@/lib/supervision-lib";
import { callSupervision, useApiBase } from "./supervision";

/** 級別徽章配色:嚴重紅、一般灰;沒定級時念現場那一檔(較大琥珀 / 重大紅)。鍵是後端簡體原值。 */
const GRADE_CHIP: Record<string, string> = {
  严重: "bg-red-50 text-red-700 ring-red-200",
  一般: "bg-gray-100 text-gray-600 ring-gray-200",
  重大: "bg-red-50 text-red-700 ring-red-200",
  较大: "bg-amber-50 text-amber-700 ring-amber-200",
};

/** 這條隱患卡上念哪一檔:定過級念定級,沒定過念現場檔(currentGrade 那條紅線:默認檔不是結論)。 */
function chipValue(h: HazardBrief): string {
  if (!h.needs_grading && h.grade) return h.grade;
  return h.severity || "";
}

type Phase = "idle" | "ready";

/**
 * 卡片出現時的動效(2026-09-18 用戶反饋:「隱患沒有動畫提示」)。
 * 這張卡是整條流程裏**最該被看見的一下** —— 答案正文是一段字,卡從中間滑上來、
 * 邊框亮兩下綠光,眼睛才會先落到它上面。只做兩下、1.2 秒一下,不常駐閃:常駐閃是噪音。
 * 關鍵幀內聯在組件裏、`gyt-` 前綴(與 GytStatusCards 的 FlowStyle 同一套紀律:globals.css
 * 是上游文件,不動);`prefers-reduced-motion` 一律關。
 */
function CardMotionStyle() {
  return (
    <style>{`
      @keyframes gyt-hazard-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
      @keyframes gyt-hazard-halo { 0%, 100% { box-shadow: 0 0 0 0 rgba(22,128,92,0); } 40% { box-shadow: 0 0 0 6px rgba(22,128,92,.22); } }
      .gyt-hazard-card { animation: gyt-hazard-in .35s ease-out both, gyt-hazard-halo 1.2s ease-out .35s 2; }
      @media (prefers-reduced-motion: reduce) { .gyt-hazard-card { animation: none; } }
    `}</style>
  );
}

/** 多個串一次過 useHantText(hook 不能放進 map 裏):用 \u0001 拼、轉完再拆,字典是逐字換的,分隔符不動。 */
const SEP = "\u0001";

export function HazardResultCard({ photoIds, lang }: { photoIds: readonly string[]; lang: Lang }) {
  const apiBase = useApiBase();
  const stream = useStreamContext();
  const isLoading = stream.isLoading;

  const [phase, setPhase] = useState<Phase>("idle");
  const [list, setList] = useState<HazardBrief[]>([]);
  const [busy, setBusy] = useState(false);
  const [armedReject, setArmedReject] = useState<string | null>(null);
  const [failures, setFailures] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  // 這一輪跑完才拉;照片編號變了(編輯重發)也重拉。拉不到一律安靜:這張卡是錦上添花,
  // 回答正文還在,不能因為它讓整條消息炸掉。
  useEffect(() => {
    if (isLoading || !apiBase || photoIds.length === 0) return;
    const controller = new AbortController();
    let cancelled = false;
    // 不在 effect 裏同步 setState(react-hooks 那條規則):拉到之前 phase 留在 idle,渲染 null。
    (async () => {
      try {
        const apiKey = getApiKey();
        const res = await fetch(hazardListUrl(apiBase, { photoIds }), {
          headers: apiKey ? { "x-api-key": apiKey } : undefined,
          signal: controller.signal,
        });
        if (cancelled) return;
        if (!res.ok) {
          setList([]);
          setPhase("ready");
          return;
        }
        const parsed = parseHazardListEnvelope(await res.text());
        if (cancelled) return;
        // 後端認得 `?photo_id` 時,沒傳 scope 它會回「全部」;老後端不認這個參數,回的是缺省「在办」
        // 且整張台賬 —— 那時才在客戶端按 photo_id 兜底篩。
        // 🔴 認得的時候**不許再按 photo_id 篩一遍**:同一張照片重傳會登記成新編號,而隱患那一行
        //    冪等命中、photo_id 停在第一次那個,後端是按內容指紋(sha256)匹配到的 ——
        //    客戶端再按 id 篩就把它篩掉了,卡空着而隱患明明在(2026-09-18 實測到)。
        const honored = parsed.ok && parsed.scope === HAZARD_SCOPE_ALL;
        setList(!parsed.ok ? [] : honored ? parsed.hazards : hazardsForPhotos(parsed.hazards, photoIds));
        setPhase("ready");
      } catch {
        if (!cancelled) {
          setList([]);
          setPhase("ready");
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // photoIds 是 ai.tsx 每次渲染現算的數組;按內容比,別按引用(引用每次都新)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoading, apiBase, photoIds.join(",")]);

  const setFailure = useCallback((no: string, text: string | null) => {
    setFailures((prev) => {
      const next = { ...prev };
      if (text === null) delete next[no];
      else next[no] = text;
      return next;
    });
  }, []);

  const confirmOne = useCallback(
    async (h: HazardBrief) => {
      setBusy(true);
      setFailure(h.hazard_no, null);
      try {
        const outcome = await callSupervision(apiBase, "confirm", confirmBody([h.hazard_no]));
        if (!outcome.ok) {
          setFailure(h.hazard_no, outcome.message);
          return;
        }
        const result = parseConfirmEnvelope(outcome.text);
        if (result.confirmed.includes(h.hazard_no)) {
          setList((prev) => patchHazard(prev, h.hazard_no, { status: "open" }));
        } else {
          const why = result.failed.find((f) => f.hazard_no === h.hazard_no)?.reason;
          setFailure(h.hazard_no, why || result.user_msg || SUPERVISION_MESSAGES.badEnvelope);
        }
      } catch (err) {
        setFailure(
          h.hazard_no,
          err instanceof SupervisionContractError && err.message
            ? err.message
            : SUPERVISION_MESSAGES.badEnvelope,
        );
      } finally {
        setBusy(false);
      }
    },
    [apiBase, setFailure],
  );

  const rejectOne = useCallback(
    async (h: HazardBrief) => {
      setBusy(true);
      setArmedReject(null);
      setFailure(h.hazard_no, null);
      try {
        const outcome = await callSupervision(
          apiBase,
          "reject",
          actionBody("reject", { hazardNo: h.hazard_no }),
        );
        if (!outcome.ok) {
          setFailure(h.hazard_no, outcome.message);
          return;
        }
        const parsed = parseActionEnvelope(outcome.text);
        if (parsed.status === REJECTED_STATUS) {
          setList((prev) => removeHazard(prev, h.hazard_no));
          setNotice(parsed.user_msg || "已從台賬裏刪掉。");
        } else {
          setFailure(h.hazard_no, parsed.user_msg || SUPERVISION_MESSAGES.badEnvelope);
        }
      } catch (err) {
        setFailure(
          h.hazard_no,
          err instanceof SupervisionContractError && err.message
            ? err.message
            : normalizeError(500, null, "").message,
        );
      } finally {
        setBusy(false);
      }
    },
    [apiBase, setFailure],
  );

  // hook 要無條件調用,所以先算好要轉的那幾份,再決定渲不渲染。
  const itemTexts = useHantText(list.map((h) => h.item).join(SEP), "ai", lang).split(SEP);
  const chipTexts = useHantText(list.map(chipValue).join(SEP), "ai", lang).split(SEP);
  const statusTexts = useHantText(
    list.map((h) => h.status_display || hazardStatusZh(h.status)).join(SEP),
    "ai",
    lang,
  ).split(SEP);
  const noticeText = useHantText(notice ?? "", "ai", lang);

  if (phase !== "ready" || (list.length === 0 && !notice)) return null;

  const pendingCount = list.filter((h) => h.status === "pending").length;

  return (
    <div className="gyt-hazard-card mb-2 overflow-hidden rounded-xl border border-[var(--gyt-line)] bg-white text-[14px]">
      <CardMotionStyle />
      <div className="flex items-center gap-2 border-b border-[var(--gyt-line)] px-3 py-2 font-bold">
        <ShieldAlert className="size-4 text-[var(--gyt-green-deep)]" />
        <span>看到 {list.length} 處隱患</span>
        <span className="ml-auto text-[12px] font-medium text-[var(--gyt-muted)]">
          {pendingCount > 0 ? `已登記 · ${pendingCount} 條待確認` : "已登記"}
        </span>
      </div>
      {list.map((h, i) => {
        const chip = chipValue(h);
        const isPending = h.status === "pending";
        const armed = armedReject === h.hazard_no;
        const failure = failures[h.hazard_no];
        return (
          <div
            key={h.hazard_no}
            className="grid grid-cols-[auto_1fr] items-start gap-x-3 gap-y-1 border-b border-[var(--gyt-line)] px-3 py-2.5 last:border-b-0"
          >
            {/* 鍵用後端簡體原值,字用轉過的那份 */}
            <span
              className={`mt-0.5 inline-flex rounded-md px-1.5 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${
                GRADE_CHIP[chip] ?? "bg-gray-100 text-gray-600 ring-gray-200"
              }`}
            >
              {chipTexts[i] || "—"}
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className="font-bold text-[var(--gyt-ink)]">{itemTexts[i]}</span>
                {!isPending && (
                  <span className="rounded-md bg-[#E3EFE9] px-1.5 py-0.5 text-[11px] font-semibold text-[var(--gyt-green-deep)]">
                    {statusTexts[i]}
                  </span>
                )}
              </div>
              <div className="font-mono text-[11px] text-gray-400 select-all">{h.hazard_no}</div>
              {isPending && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {armed ? (
                    <>
                      <span className="text-[12px] text-red-700">
                        否決 = 識錯了,這一行從台賬刪掉,找不回來。
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void rejectOne(h)}
                        className="inline-flex min-h-11 items-center gap-1 rounded-lg bg-red-600 px-3 text-[13px] font-semibold text-white disabled:opacity-50"
                      >
                        <Trash2 className="size-3.5" />
                        確定刪掉
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setArmedReject(null)}
                        className="min-h-11 rounded-lg border border-[var(--gyt-line)] px-3 text-[13px] text-[var(--gyt-ink-soft)]"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void confirmOne(h)}
                        className="inline-flex min-h-11 items-center gap-1 rounded-lg bg-[var(--gyt-green)] px-3 text-[13px] font-semibold text-white transition hover:bg-[var(--gyt-green-deep)] disabled:opacity-50"
                      >
                        <Check className="size-3.5" />
                        確認是隱患
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setArmedReject(h.hazard_no)}
                        className="min-h-11 rounded-lg border border-[var(--gyt-line)] px-3 text-[13px] text-[var(--gyt-ink-soft)] hover:border-[var(--gyt-mint)] disabled:opacity-50"
                      >
                        不是隱患
                      </button>
                    </>
                  )}
                </div>
              )}
              {failure && (
                // 後端的 user_msg 原樣上屏,一個字不轉(W12 復審定案)
                <div className="mt-1.5 rounded-md bg-red-50 px-2 py-1 text-[12px] text-red-700">{failure}</div>
              )}
            </div>
          </div>
        );
      })}
      {notice && list.length === 0 && (
        <div className="px-3 py-2 text-[12px] text-[var(--gyt-muted)]">{noticeText}</div>
      )}
      <button
        type="button"
        onClick={requestOpenSupervision}
        className="flex min-h-11 w-full items-center justify-between bg-[#F6FAF8] px-3 text-[13px] font-semibold text-[var(--gyt-green-deep)] transition hover:bg-[#EEF3F1]"
      >
        <span>去監理處置(定級 / 簽發文書 / 複查)</span>
        <ChevronRight className="size-4" />
      </button>
    </div>
  );
}
