/**
 * 电脑端二维码面板 —— **本仓自有新文件**,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/qrcode.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * 为什么存在:打卡要自拍 + 定位,这两样在工位电脑上都不对劲(桌面摄像头拍的是
 * 天花板,定位是机房)。宽屏时给一张二维码,让人掏出手机扫码,在手机上打卡。
 *
 * ── D7:码里放什么(2026-08-15 W8 修订)────────────────────────────────
 * **二维码里不带 token、不带姓名 —— 这条永远成立。** 带 token = 把门钥匙印在
 * 屏幕上,谁路过拍张照都进得来;而配对串(pair)不是凭据、不授予任何权限,
 * 所有配对端点照样在登录闸和 X-Api-Key 后面,它只是「哪台电脑在等哪次打卡」的关联号。
 *
 * D7 原来还有第二条理由「带 query 也活不过登录跳转,等于白带」——
 * 那是因为 scripts/serve_login.py 登录后写死重定向 `/`。**W8 已经把它改成带
 * `next` 回原地址,这条理由不再成立**,所以码里现在带 `?checkin=1&pair=…`
 * (URL 由 checkin-lib 的 buildCheckinPageUrl 拼,形状是契约第二节钉死的)。
 * 哪天有人把登录跳转改回写死 `/`,表现是:手机扫码 → 登录 → 落在首页、面板不开,
 * 电脑永远停在 waiting,一声不吭。
 *
 * ── 配对状态 ──────────────────────────────────────────────────────────
 * 本组件**只管显示**,状态由 checkin.tsx 轮询得来。done 时这儿只留一句
 * 「✅ 已打卡成功」——**凭证卡片不在这儿**:checkin.tsx 把轮询拿到的凭证喂进它
 * 自己既有的 receipt 状态,于是左边主栏原样渲染出 ReceiptCard(那边地方大,
 * 而这一栏只有 240px),「再打一次」那套也一并复用,不为配对另写一份凭证渲染。
 * 顺带:本组件因此不需要反向 import checkin.tsx —— 那会是循环依赖
 * (checkin.tsx 本来就要 import 本文件)。
 *
 * 库:qrcode.react(setup-frontend.sh 用 --save-exact 钉死版本装的,常量
 * QRCODE_REACT_VERSION 在那边)。选它:React 组件直出 SVG —— 无 canvas、
 * 无 DOM 副作用、SSR 安全、高分屏不糊;纠错级别 M(约 15%)对一条短 URL 绰绰有余,
 * 级别抬高只会让码更密、隔着桌子更难扫。
 */

import { QRCodeSVG } from "qrcode.react";
import { LoaderCircle, Smartphone } from "lucide-react";
import { PAIR_MESSAGES, PairState } from "@/lib/checkin-lib";

/** 二维码边长(px)。太小隔着桌面扫不上,太大喧宾夺主 —— 168 约等于名片宽。 */
const QR_SIZE_PX = 168;

export function CheckinQrPanel({
  url,
  state,
}: {
  url: string;
  /** 配对状态;没开配对(生成配对串失败等)时传 "waiting",面板退回纯二维码。 */
  state: PairState;
}) {
  // SSR/预渲染阶段拿不到 window.location,调用方会先传空串 —— 渲染空,
  // 客户端挂载后补上。这里不自己去摸 window:保持无副作用,谁传谁负责。
  if (!url) return null;

  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-gray-200 bg-gray-50 p-4">
      {state === "waiting" && (
        <>
          <div className="rounded-lg bg-white p-2">
            <QRCodeSVG
              value={url}
              size={QR_SIZE_PX}
              level="M"
              marginSize={2}
            />
          </div>
          {/* aria-live:状态是被轮询悄悄换掉的,不播报的话读屏用户永远停在第一句 */}
          <div
            className="flex items-center gap-1.5 text-center text-sm text-gray-600"
            aria-live="polite"
          >
            <Smartphone className="h-4 w-4 shrink-0" />
            {PAIR_MESSAGES.waiting}
          </div>
          {/* 网址明文放在码下面:扫不动时(镜面反光、贴膜)还能照着手打。
              带 query 的地址比原来长,break-all + 小字号让它老实待在这一栏里 */}
          <div className="max-w-[13rem] text-center font-mono text-[11px] break-all text-gray-400 select-all">
            {url}
          </div>
        </>
      )}

      {state === "scanned" && (
        <div
          className="flex flex-col items-center gap-2 py-6 text-center"
          aria-live="polite"
        >
          <LoaderCircle className="h-6 w-6 animate-spin text-gray-400" />
          <div className="text-sm text-gray-700">{PAIR_MESSAGES.scanned}</div>
          {/* 「这台电脑怎么了」由左边那块 PairTakeoverNotice 说(checkin.tsx),
              这儿只说手机上该干什么 —— 两块同时在屏幕上,说同一句话像出了 bug */}
        </div>
      )}

      {/* 只有这一句。凭证卡片在左边主栏(见头注「配对状态」那段);
          万一凭证没跟着回来,左边那块会如实说「这台电脑没取到凭证图」——
          编号和照片本来就在手机上、库里也记着,这儿没有就是没有,不编。 */}
      {state === "done" && (
        <div
          className="py-6 text-sm font-medium text-emerald-700"
          aria-live="polite"
        >
          {PAIR_MESSAGES.done}
        </div>
      )}
    </div>
  );
}
