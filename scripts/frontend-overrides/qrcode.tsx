/**
 * 电脑端二维码面板 —— **本仓自有新文件**,上游 agent-chat-ui 没有对应物。
 * 由 scripts/setup-frontend.sh 的 install_new_file 装到
 * frontend/src/components/thread/qrcode.tsx;**别直接改 frontend/ 里那份**,
 * 那个目录不进 git,换台机器就没了。
 *
 * 为什么存在:打卡要自拍 + 定位,这两样在工位电脑上都不对劲(桌面摄像头拍的是
 * 天花板,定位是机房)。宽屏时给一张「本站网址」的二维码,让人掏出手机扫码,
 * 在手机上打开同一个站打卡。
 *
 * D7 定死:**二维码只是本站 URL,不带 token、不带姓名、不带任何 query**。
 * 两个理由,任何一个单独都够:
 *   · 带 token = 把门钥匙印在屏幕上,谁路过拍张照都进得来;
 *   · scripts/serve_login.py 登录后的重定向写死 `/`(:350 附近),
 *     带 query 也活不过登录跳转,等于白带。
 *
 * 库:qrcode.react(setup-frontend.sh 用 --save-exact 钉死版本装的,常量
 * QRCODE_REACT_VERSION 在那边)。选它:React 组件直出 SVG —— 无 canvas、
 * 无 DOM 副作用、SSR 安全、高分屏不糊;纠错级别 M(约 15%)对一条短 URL 绰绰有余,
 * 级别抬高只会让码更密、隔着桌子更难扫。
 */

import { QRCodeSVG } from "qrcode.react";
import { Smartphone } from "lucide-react";

/** 二维码边长(px)。太小隔着桌面扫不上,太大喧宾夺主 —— 168 约等于名片宽。 */
const QR_SIZE_PX = 168;

export function CheckinQrPanel({ url }: { url: string }) {
  // SSR/预渲染阶段拿不到 window.location,调用方会先传空串 —— 渲染空,
  // 客户端挂载后补上。这里不自己去摸 window:保持无副作用,谁传谁负责。
  if (!url) return null;

  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-gray-200 bg-gray-50 p-4">
      <div className="rounded-lg bg-white p-2">
        <QRCodeSVG
          value={url}
          size={QR_SIZE_PX}
          level="M"
          marginSize={2}
        />
      </div>
      <div className="flex items-center gap-1.5 text-sm text-gray-600">
        <Smartphone className="h-4 w-4 shrink-0" />
        用手机扫码打开本站打卡
      </div>
      {/* 网址明文放在码下面:扫不动时(镜面反光、贴膜)还能照着手打 */}
      <div className="max-w-[13rem] text-center font-mono text-[11px] break-all text-gray-400 select-all">
        {url}
      </div>
    </div>
  );
}
