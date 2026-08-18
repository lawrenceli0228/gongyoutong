/**
 * 打卡纯函数库 —— checkin.tsx 的所有可测逻辑都沉在这里。
 *
 * ⚠️ 本文件必须保持**纯 TS、零依赖、零环境假设**(不 import react、不 import "@/…"):
 *    scripts/frontend-tests/ 那个独立 vitest 包按相对路径直接 import 它,
 *    多一个依赖那个包就装不动了;多一处 window 假设它就跑不起来了。
 *    (唯一的运行时依赖是 crypto.subtle,而它「可能不存在」本身就是要测的分支。)
 *
 * 安装:scripts/setup-frontend.sh 的 install_new_file 拷到 frontend/src/lib/checkin-lib.ts。
 * frontend/ 不进 git,**别直接改那边** —— 换台机器就没了。
 *
 * 请求契约的唯一真相在 backend/src/gyt/checkin_api.py 的模块 docstring(T1 冻结):
 * 发(本文件 buildCheckinHeaders)、收(checkin_api.py)、存(db/attendance.py)
 * 三处任一改名 = **静默少一个字段**,没有报错 —— CLAUDE.md 同源清单那条。
 * 下面每个「镜像常量」旁边都标了后端对应物,改哪边都要两边一起。
 */

// ---------------------------------------------------------------------------
// 契约常量 —— 逐个镜像 checkin_api.py 顶部的 Final 常量
// ---------------------------------------------------------------------------

/** 镜像 checkin_api.py 的 HEADER_* 六件。全小写:HTTP/2 强制小写头名,
 * 常量长什么样 = 网络上传的长什么样(auth.py:105 的 API_KEY_HEADER 同一约定)。 */
export const HEADER_EVENT_ID = "x-gyt-event-id";
export const HEADER_WORKER = "x-gyt-worker";
export const HEADER_SITE = "x-gyt-site";
export const HEADER_GEO = "x-gyt-geo";
export const HEADER_SOURCE = "x-gyt-source";
export const HEADER_DIGEST = "x-gyt-digest";

/**
 * 扫码配对(W8)用的第七个头,**可省** —— 省略时打卡行为与今天完全一致。
 *
 * 两条不许犯的规矩(契约「三、后端新增」那节):
 *  · 它**不进指纹**:同一次打卡带不带 pair 必须算出同一个 digest,
 *    否则「手机重发一次」会被当成新的一次打卡,幂等层当场失效;
 *  · 它**不是凭据**、不授予任何权限,所有配对端点照样在登录闸和 X-Api-Key 后面。
 */
export const HEADER_PAIR = "x-gyt-pair";

/** 镜像 GEO_SEPARATOR。分号而不是逗号:逗号在 HTTP header 里有「同名 header 合并」
 * 的既有语义,代理把两个同名头合成 `a, b` 之后用逗号切就切错了。 */
export const GEO_SEPARATOR = ";";

/** 镜像 DIGEST_HEX_LEN(sha256 十六进制长度)。定长校验是最便宜的一道格式闸。 */
export const DIGEST_HEX_LEN = 64;

/** 镜像 MAX_NAME_BYTES / MAX_EVENT_ID_LEN —— 协议层的闸,不是业务规则。 */
export const MAX_NAME_BYTES = 256;
export const MAX_EVENT_ID_LEN = 128;

/* ---------------------------------------------------------------------------
 * 定位的三个时间常量 —— 2026-08-15 真机实测后重定,别再合回一个数
 * -------------------------------------------------------------------------
 * 原来只有一个 `GEO_TIMEOUT_MS = 3000`,而且从「按快门」那一刻才起跑。
 * 本机(macOS,无 GPS,靠 WiFi 定位)实测:
 *
 *     冷启动首次 fix(权限已授) 5344ms   ← 3 秒预算在 3001ms 准时超时
 *     系统缓存热了之后             2ms   ← 同样 3 秒参数,秒回
 *     精度                       ±35 米  ← 分辨地盘绰绰有余
 *
 * 也就是说:**每个人的第一次打卡都必然拿不到定位**,而 D11 加定位的理由正是
 * 「工友跑多个地盘,要知道人在哪」—— 首打必空等于这个功能形同虚设。
 *
 * 原来那个 3 秒并没有错,错在它同时兼了两个职:既当「定位本身的预算」,
 * 又当「提交前最多等多久」。这两件事的合理取值差一个数量级,合成一个数
 * 必然顾此失彼。v2 拆成三个:
 */

/** 定位本身的预算。实测冷启动 5.3 秒,给 20 秒留足余量(工地信号比这里差)。
 *
 * 敢给这么长的前提是它**不挡任何人**:定位在面板一打开就起跑,而用户还要
 * 框取景、打姓名 —— 那十几秒本来就是白等的。真正约束提交的是下面那个。 */
export const GEO_ACQUIRE_TIMEOUT_MS = 20_000;

/** 提交那一刻最多再等定位多久。等不到就按 timeout 状态照发。
 *
 * 「不为定位挡住打卡」这条原则由**这个数**保证,而不是由定位预算保证 ——
 * 结构上就没有「定位慢 → 提交卡住」这条路径,不靠调参碰运气。 */
export const GEO_SUBMIT_WAIT_MS = 1_500;

/** 接受多旧的缓存位置(getCurrentPosition 的 maximumAge)。
 *
 * 原来没设 = 默认 0 = **拒绝一切缓存、每次强制重新定位一遍**,这是首打必超时的
 * 第三个原因。打卡要回答的是「你在哪个地盘」不是厘米级,一分钟前的位置完全够用;
 * 实测热缓存命中是 0~2ms。 */
export const GEO_MAX_AGE_MS = 60_000;

/** 镜像 db/attendance.py 的 source CHECK 约束:camera=现场取景,fallback=<input> 降级
 * (可能是相册旧图 = 弱凭证,如实上报,见 checkin.tsx 的降级分支注释)。 */
export const CHECKIN_SOURCES = ["camera", "fallback"] as const;
export type CheckinSource = (typeof CHECKIN_SOURCES)[number];

/**
 * 前端**能发**的定位状态,恰好五种。
 *
 * ⚠️ 后端 GEO_STATUSES 是六种 —— 多出的 `absent` 是**服务端**对
 * 「X-GYT-Geo 这个 header 压根没来」的记法,前端永远不发它:
 * 本库的 buildCheckinHeaders 恒带 geo 头,`absent` 在前端根本不可表达。
 * 谁往这个数组里加 "absent",服务端审计里「前端没发」和「前端说不知道」就混了。
 */
export const GEO_SEND_STATUSES = [
  "ok",
  "denied",
  "timeout",
  "unsupported",
  "error",
] as const;
export type GeoSendStatus = (typeof GEO_SEND_STATUSES)[number];

export type GeoResult =
  | { status: "ok"; lat: number; lon: number; accuracyM: number }
  | { status: Exclude<GeoSendStatus, "ok"> };

/** 契约被违反时抛的错。message 一律是工友看得懂的中文 —— 组件层直接上屏。 */
export class CheckinContractError extends Error {}

/**
 * 前端这份固定文案(与后端 attendance/messages.py 是**两个集中点**,D12 换简繁时
 * 两边都要动)。后端 Envelope 的 user_msg 到了前端**原样透传**,不在这里改写。
 */
export const CHECKIN_MESSAGES = Object.freeze({
  network: "連不上服務器。檢查手機信號或 Wi-Fi,再試一次。",
  authFailed: "登錄信息不對或已過期。刷新頁面重新進一次;還不行就找管理員對一下口令。",
  tooLarge: "照片太大,傳不上去。退出重拍一張再試。",
  rateLimited: "打卡太頻繁,歇幾秒再試。",
  notFound: "打卡服務還沒開通(接口不存在)。請管理員確認後端已更新到帶打卡的版本。",
  conflict: "這次打卡的信息和之前那次對不上。",
  serverError: "服務器出錯了,稍等再試;一直這樣就找管理員。",
  badReceipt: "服務器返回的憑證格式不對,先截圖記下時間,找管理員核對台賬。",
  insecureContext:
    "這個頁面不是 https 打開的,瀏覽器不給算照片指紋,打卡發不出去。請用 https(或 localhost)打開本站再試。",
});

// ---------------------------------------------------------------------------
// Base64URL(无 = 填充)—— 与后端 encode_name / decode_name 逐字节一致
// ---------------------------------------------------------------------------
// 手写而不用 btoa:btoa 要先把字节拼成 binary string(逐字节 fromCharCode),
// Node 里还标记 legacy;手写 20 行,浏览器与 Node 行为逐字节一致,
// 直接产出 URL 安全字母表 + 无填充,不用「标准 base64 再替换字符再剥 =」绕三道。

const BASE64URL_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

const BASE64URL_REVERSE: ReadonlyMap<string, number> = new Map(
  [...BASE64URL_ALPHABET].map((ch, idx) => [ch, idx] as const),
);

function bytesToBase64Url(bytes: Uint8Array): string {
  const chars: string[] = [];
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i];
    const hasB1 = i + 1 < bytes.length;
    const hasB2 = i + 2 < bytes.length;
    const b1 = hasB1 ? bytes[i + 1] : 0;
    const b2 = hasB2 ? bytes[i + 2] : 0;
    chars.push(BASE64URL_ALPHABET[b0 >> 2]);
    chars.push(BASE64URL_ALPHABET[((b0 & 0x03) << 4) | (b1 >> 4)]);
    if (hasB1) chars.push(BASE64URL_ALPHABET[((b1 & 0x0f) << 2) | (b2 >> 6)]);
    if (hasB2) chars.push(BASE64URL_ALPHABET[b2 & 0x3f]);
  }
  return chars.join("");
}

function base64UrlToBytes(value: string): Uint8Array {
  // 无填充编码里长度 mod 4 == 1 不可能出现(1 字节编 2 字符、2 字节编 3 字符)
  if (value.length % 4 === 1) {
    throw new CheckinContractError("名稱字段不是合法的 Base64URL");
  }
  const bytes: number[] = [];
  let buffer = 0;
  let bitCount = 0;
  for (const ch of value) {
    const sextet = BASE64URL_REVERSE.get(ch);
    if (sextet === undefined) {
      throw new CheckinContractError("名稱字段不是合法的 Base64URL");
    }
    buffer = (buffer << 6) | sextet;
    bitCount += 6;
    if (bitCount >= 8) {
      bitCount -= 8;
      bytes.push((buffer >> bitCount) & 0xff);
    }
  }
  return new Uint8Array(bytes);
}

/** 控制字符与 DEL。判定与后端 decode_name 的 `ch < " " or ch == "\x7f"` 同一集合。 */
function hasControlChar(text: string): boolean {
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp < 0x20 || cp === 0x7f) return true;
  }
  return false;
}

/**
 * 把 UTF-8 文本编成可放进 header 的 Base64URL(**无 = 填充**)。
 *
 * 与后端 `checkin_api.encode_name` **逐字节一致** —— 共享的测试向量在
 * backend/tests/unit/test_checkin_api.py 的 ROUNDTRIP_VECTORS(七条),
 * scripts/frontend-tests/checkin-lib.test.ts 抄的是同一份,两边注释互指。
 *
 * 控制字符在这里就挡(后端把这道闸放在 decode 侧):发出去也必被后端拒收,
 * 提前失败省一次白跑;对所有合法输入,输出与 encode_name 逐字节相同。
 */
export function encodeNameForHeader(name: string): string {
  if (hasControlChar(name)) {
    throw new CheckinContractError("名稱裏有換行或控制字符,請去掉再試");
  }
  const raw = new TextEncoder().encode(name);
  if (raw.length > MAX_NAME_BYTES) {
    throw new CheckinContractError(
      `名稱太長(${raw.length} 字節,上限 ${MAX_NAME_BYTES}),請寫短一點`,
    );
  }
  return bytesToBase64Url(raw);
}

/**
 * `encodeNameForHeader` 的逆,镜像后端 `decode_name`。发侧其实用不上它 ——
 * 留着是让 vitest 的往返断言有真牙齿(编完解回必须一字不差)。
 *
 * ⚠️ 与后端同一条铁律:**先查长度再解码**。反过来的话,一个超长输入会先被
 * 完整解码一遍才被判超长 —— 拿校验代码本身当放大器。
 */
export function decodeNameFromHeader(value: string): string {
  if (value.length > MAX_NAME_BYTES * 2) {
    // Base64 撑 4/3,×2 是宽松上界(与后端 decode_name 同一判据)
    throw new CheckinContractError("名稱字段過長");
  }
  const raw = base64UrlToBytes(value);
  if (raw.length > MAX_NAME_BYTES) {
    throw new CheckinContractError("名稱字段過長");
  }
  let text: string;
  try {
    // fatal: 非法 UTF-8 必须抛,不许悄悄换成 U+FFFD —— 那等于把乱码当名字收下
    text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
  } catch {
    throw new CheckinContractError("名稱字段不是合法的 UTF-8");
  }
  if (hasControlChar(text)) {
    throw new CheckinContractError("名稱字段含控制字符");
  }
  return text;
}

// ---------------------------------------------------------------------------
// 定位
// ---------------------------------------------------------------------------

/**
 * GeolocationPositionError.code → 发给后端的状态。
 *
 * 三个码是 W3C 规范定死的:1 PERMISSION_DENIED / 2 POSITION_UNAVAILABLE / 3 TIMEOUT。
 * denied 与 timeout 各有明确的审计语义(人拒了 / 信号慢),必须分开;
 * 2(设备拿不到)与任何未知码都归 error —— 对审计来说「拿不到」和「出错了」
 * 区分度不大,而多一个自造状态就多一个后端 CHECK 约束对不上的机会
 * (对不上的表现是整条 INSERT 失败、报错在数据库层,W7 §3.1)。
 */
export function geoStatusFromPositionError(
  code: number,
): Extract<GeoSendStatus, "denied" | "timeout" | "error"> {
  if (code === 1) return "denied";
  if (code === 3) return "timeout";
  return "error";
}

/**
 * 浏览器给的坐标 → GeoResult。
 *
 * 浏览器按规范给的值就该合法;仍验一遍是因为后端把 NaN/越界判成 400 ——
 * 那对工友是一次白跑。这里降级成 error 状态,**照样能交**(W7 §2:
 * 拿不到坐标不挡打卡)。
 */
export function geoResultFromPosition(pos: {
  coords: { latitude: number; longitude: number; accuracy: number };
}): GeoResult {
  const { latitude, longitude, accuracy } = pos.coords;
  const latOk = Number.isFinite(latitude) && Math.abs(latitude) <= 90;
  const lonOk = Number.isFinite(longitude) && Math.abs(longitude) <= 180;
  const accOk = Number.isFinite(accuracy) && accuracy >= 0;
  if (!latOk || !lonOk || !accOk) return { status: "error" };
  return { status: "ok", lat: latitude, lon: longitude, accuracyM: accuracy };
}

/**
 * 提交那一刻给定位设的最后期限:等到就用,等不到就按 timeout 状态照发。
 *
 * **「不为定位挡住打卡」这条原则由本函数保证。** 定位本身的预算(20 秒)给得很松,
 * 因为它在面板一打开就起跑、跑在用户框取景和打字的那段白等时间里;
 * 而提交只肯再等 `waitMs`(默认 1.5 秒)。两个数各司其职:
 * 前者决定「能不能拿到」,后者决定「会不会卡住人」。
 *
 * 合成一个数就是 2026-08-15 之前那个 bug:3 秒既想当预算又想当期限,
 * 结果冷启动要 5.3 秒的机器上,**每个人的第一次打卡都拿不到定位**。
 *
 * 用 Promise.race 而不是给 getCurrentPosition 一个短 timeout:后者会**取消**
 * 那次定位,下一次又得从冷启动重来;race 只是不再等它,浏览器那边照跑,
 * 跑完进系统缓存 —— 于是**第二次打卡是热的**(实测 0~2ms)。
 *
 * `timer` 可注入是为了测试:真 sleep 写出来的用例又慢又飘。
 */
export function withSubmitDeadline(
  pending: Promise<GeoResult>,
  waitMs: number = GEO_SUBMIT_WAIT_MS,
  timer: (fn: () => void, ms: number) => unknown = setTimeout,
): Promise<GeoResult> {
  return Promise.race([
    pending,
    new Promise<GeoResult>((resolve) => {
      timer(() => resolve({ status: "timeout" }), waitMs);
    }),
  ]);
}

/**
 * 拼 X-GYT-Geo 的值。契约(checkin_api.py docstring「定位」节):
 *
 *     ok;22.302711;114.177216;12.5   ← 状态;纬度;经度;精度(米),恰好四段
 *     denied                          ← 非 ok 只有状态一段
 *
 * 合成一个 header 而不是拆四个,是让「经纬度必须成对」由格式本身锁死。
 * 数字用 String() 序列化:后端 float() 认得 JS 的十进制与指数计法,
 * NaN/Infinity 被上游 geoResultFromPosition 的 finite 闸挡掉,这里再兜一道 ——
 * 兜的是「有人绕过 geoResultFromPosition 手搓 GeoResult」的程序错误。
 * 指纹比对用的是这串的**原始字节**(两端看到同一份),所以序列化怎么写都不会漂,
 * 只要写完不再改。
 */
export function buildGeoHeader(result: GeoResult): string {
  if (result.status !== "ok") return result.status;
  const numbers = [result.lat, result.lon, result.accuracyM];
  if (numbers.some((n) => !Number.isFinite(n))) {
    throw new CheckinContractError("座標不是有限數字 —— 這是程序錯誤,不是你的問題");
  }
  return ["ok", ...numbers.map(String)].join(GEO_SEPARATOR);
}

// ---------------------------------------------------------------------------
// 照片指纹(X-GYT-Digest)
// ---------------------------------------------------------------------------

/**
 * 照片字节的 sha256,64 位小写十六进制 —— X-GYT-Digest 的值。
 *
 * 客户端只算**照片**的哈希,不算完整指纹:完整指纹的其余输入(姓名/地盤/坐标)
 * 本来就全在 header 里,服务端自己拼(checkin_api.py「X-GYT-Digest 不可信」那节)。
 * 这样前后端不用各写一份规范化逻辑,也就不会漂。
 *
 * ⚠️ crypto.subtle **只在安全上下文存在**(https 与 localhost/127.0.0.1;
 * `http://<局域网IP>:3000` 没有)。不可用时**必须抛**、绝不静默省掉这个 header:
 * 省掉的表现是幂等层①永不命中、每次重发都白烧一次水印,而功能看起来完全正常
 * (checkin_api.py docstring 明文写了这条)。组件层拿 message 直接上屏。
 */
export async function sha256Hex(bytes: ArrayBuffer | Uint8Array): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new CheckinContractError(CHECKIN_MESSAGES.insecureContext);
  }
  const digest = await subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// ---------------------------------------------------------------------------
// 请求头拼装
// ---------------------------------------------------------------------------

export interface CheckinHeaderInput {
  eventId: string;
  worker: string;
  /** 地盤名,可省(契约:X-GYT-Site 可省略;后端指纹里 None ≡ 空串,这里选省略)。 */
  site?: string;
  geo: GeoResult;
  source: CheckinSource;
  digestHex: string;
  /** 扫码配对串(W8),可省。**格式不对当没有,绝不抛** —— 理由见函数体里那段。 */
  pairId?: string | null;
}

/**
 * 拼 POST /checkin 的六个业务 header(名字全小写)。
 *
 * **X-Api-Key 刻意不在这里** —— 令牌怎么来(getApiKey():localStorage 优先、
 * 构建期注入兜底)是组件层的事;本函数保持纯,测试才不用碰 window。
 * Content-Type: image/jpeg 同理由组件层给(它跟 body 是一对)。
 */
export function buildCheckinHeaders(
  input: CheckinHeaderInput,
): Record<string, string> {
  const eventId = input.eventId.trim();
  if (!eventId || eventId.length > MAX_EVENT_ID_LEN) {
    throw new CheckinContractError("打卡編號不對 —— 這是程序錯誤,刷新頁面再試");
  }
  // header 值必须 ASCII;顺带把空格/控制字符挡掉。newEventId() 只产出这个字符集。
  if (!/^[A-Za-z0-9_-]+$/.test(eventId)) {
    throw new CheckinContractError("打卡編號含非法字符 —— 這是程序錯誤,刷新頁面再試");
  }
  const worker = input.worker.trim();
  if (!worker) {
    throw new CheckinContractError("請先填姓名再打卡");
  }
  // 镜像后端 validate_digest_hex:定长 + 纯十六进制,归一化成小写
  const digest = input.digestHex.trim().toLowerCase();
  if (digest.length !== DIGEST_HEX_LEN || !/^[0-9a-f]+$/.test(digest)) {
    throw new CheckinContractError("照片指紋不對 —— 這是程序錯誤,重拍一張再試");
  }
  if (!CHECKIN_SOURCES.includes(input.source)) {
    throw new CheckinContractError("拍照來源不對 —— 這是程序錯誤");
  }
  const site = (input.site ?? "").trim();
  // 配对头:可省,**而且格式不对就当没有,这里一个错都不抛**。
  // 上面每一条校验抛错的后果是「打不了卡」,对指纹/姓名/编号那是对的 ——
  // 它们错了这次打卡本来就记不成账。配对不一样:它只决定「电脑那边跟不跟着变」,
  // 为它挡下一次打卡是把主次颠倒了(W8 契约第五节:配对是锦上添花,
  // 断了只是电脑不联动,手机照样得能把卡打出去)。
  const pairId = normalizePairId(input.pairId);
  const base: Record<string, string> = {
    [HEADER_EVENT_ID]: eventId,
    [HEADER_WORKER]: encodeNameForHeader(worker),
    [HEADER_GEO]: buildGeoHeader(input.geo),
    [HEADER_SOURCE]: input.source,
    [HEADER_DIGEST]: digest,
    ...(pairId ? { [HEADER_PAIR]: pairId } : {}),
  };
  // 空地盤省略 header 而不是发空串:后端指纹里两者等价
  // (test_checkin_api.py 的 test_地盤名为_None_与空串等价),少发一条是一条。
  return site ? { ...base, [HEADER_SITE]: encodeNameForHeader(site) } : base;
}

// ---------------------------------------------------------------------------
// event_id:幂等键的客户端半边
// ---------------------------------------------------------------------------

/** 注入用的最小存储接口 —— window.sessionStorage / localStorage 都天然满足,
 * 测试用 Map 包一个就行,不用碰 jsdom。 */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** sessionStorage 键名。gyt 前缀,别跟上游的 lg:chat:* 混在一个命名空间里。 */
export const EVENT_ID_STORAGE_KEY = "gyt:checkin:event-id";

/**
 * 生成新的幂等键(UUID v4 格式,36 字符,< MAX_EVENT_ID_LEN)。
 *
 * 两级兜底不是防古董浏览器:crypto.randomUUID 和 subtle 一样**只在安全上下文有**,
 * 而 getRandomValues 到处都有 —— 第二级防的是「局域网 http 联调」这个真会发生的场景
 * (那时 sha256Hex 会抛、打不了卡,但 event_id 的生成不该先炸)。
 */
export function newEventId(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  if (c?.getRandomValues) {
    const bytes = c.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
    const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
    return [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20),
    ].join("-");
  }
  throw new CheckinContractError("這台瀏覽器太老,生成不了打卡編號,換個瀏覽器試試");
}

/**
 * 取(或创建)本次打卡的 event_id,落 **sessionStorage**。
 *
 * 为什么必须落存储(W7 §3.2 / checkin_api.py docstring「客户端也要负责」):
 * 只在 React 生命周期里复用是不够的 —— 刷新、崩溃、App 被杀之后会生成新 ID,
 * 用户再提交就是**重复记账**。落了 sessionStorage,重发带同一个 ID,
 * 幂等层①在读 body 之前就还他原凭证。**提交成功后才 clearEventId。**
 *
 * 存储抛错(隐私模式等)时降级为「本次内存里的 ID」:打卡这个动作不能被
 * 存储问题挡住;代价是刷新后那层防重复的保险没了 —— 如实接受,不装没事。
 */
export function loadOrCreateEventId(storage: StorageLike): string {
  try {
    const existing = storage.getItem(EVENT_ID_STORAGE_KEY);
    if (existing && existing.trim()) return existing.trim();
  } catch {
    // 读失败按「没有」处理,走下面的新建
  }
  const fresh = newEventId();
  try {
    storage.setItem(EVENT_ID_STORAGE_KEY, fresh);
  } catch {
    // 写不进去就只活在内存里,见函数头注的取舍
  }
  return fresh;
}

/** 提交**成功**后清掉幂等键 —— 下一次打卡才是新的一次。失败时绝不清:
 * 重试要靠同一个 ID 防重复记账。 */
export function clearEventId(storage: StorageLike): void {
  try {
    storage.removeItem(EVENT_ID_STORAGE_KEY);
  } catch {
    // 清不掉的后果只是下次复用旧 ID 再拿回原凭证,无害
  }
}

// ---------------------------------------------------------------------------
// 姓名/地盤:localStorage 记住上次
// ---------------------------------------------------------------------------

export const WORKER_NAME_STORAGE_KEY = "gyt:checkin:worker-name";
export const SITE_NAME_STORAGE_KEY = "gyt:checkin:site-name";

/**
 * 读上次填过的姓名/地盤。这就是 TODOS.md 的 TODO-35 说的「身份只是个字符串」:
 * 谁都能填任何名字,没有实名、没有账号 —— 记住只是省打字,**不是登录态**,
 * 别在它上面盖任何权限逻辑。
 */
export function loadSavedIdentity(storage: StorageLike): {
  worker: string;
  site: string;
} {
  try {
    return {
      worker: (storage.getItem(WORKER_NAME_STORAGE_KEY) ?? "").trim(),
      site: (storage.getItem(SITE_NAME_STORAGE_KEY) ?? "").trim(),
    };
  } catch {
    return { worker: "", site: "" };
  }
}

/** 提交成功后记住本次身份(失败不记 —— 没打成的名字不值得记)。 */
export function saveIdentity(
  storage: StorageLike,
  worker: string,
  site: string,
): void {
  try {
    storage.setItem(WORKER_NAME_STORAGE_KEY, worker.trim());
    storage.setItem(SITE_NAME_STORAGE_KEY, site.trim());
  } catch {
    // 记不住只是下次要重新打字,不值得报错
  }
}

// ---------------------------------------------------------------------------
// 错误归一化(W7 §3.11:三层来源,四种形状)
// ---------------------------------------------------------------------------

export interface NormalizedError {
  ok: false;
  /** 工友看得懂的中文,组件直接上屏。 */
  message: string;
  /** HTTP 状态码;0 = 网络层异常(压根没有响应)。组件用它认 409。 */
  status: number;
  /** Envelope 带 error_code 时透传,其余为 null。只进日志/调试,不上屏。 */
  errorCode: string | null;
}

/** 网络层异常没有响应,约定 status 传 0(fetch reject 时组件层这么调)。 */
export const NETWORK_ERROR_STATUS = 0;

function messageForStatus(status: number): string {
  if (status === 401 || status === 403) return CHECKIN_MESSAGES.authFailed;
  if (status === 404) return CHECKIN_MESSAGES.notFound;
  if (status === 409) return CHECKIN_MESSAGES.conflict;
  if (status === 413) return CHECKIN_MESSAGES.tooLarge;
  if (status === 429) return CHECKIN_MESSAGES.rateLimited;
  if (status >= 500) return CHECKIN_MESSAGES.serverError;
  return `服務器返回了看不懂的內容(HTTP ${status}),稍後再試。`;
}

function tryParseJsonObject(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // 不是 JSON,让调用方走非 JSON 分支
  }
  return null;
}

/**
 * 把四种错误形状压成一个 `{ok:false, message}`(W7 §3.11 那张表):
 *
 *   ① 进了 handler:Envelope 四键 {ok,data,user_msg,error_code}
 *      —— user_msg 已经是人话,**原样用**,error_code 透传
 *   ② langgraph 鉴权中间件 / Caddy 对 /api/* 的未登录:{"detail": …}
 *      —— detail 是英文/内部话,不上屏,按状态码给固定中文
 *   ③ Caddy 的 413 等错误页:连 JSON 都不是(HTML/纯文本)
 *      —— 按状态码给固定中文(413 = 照片太大;别让人去翻 uploads.py,方向全错)
 *   ④ 网络层异常:没有响应,约定 status=0
 *
 * **前端不能假设所有错误都是 Envelope** —— checkin_api.py 响应契约那节明文。
 * content-type 撒谎时(说 JSON 不是 / 是 JSON 没说)以「解析得动吗」为准。
 */
export function normalizeError(
  status: number,
  contentType: string | null,
  bodyText: string,
): NormalizedError {
  if (status === NETWORK_ERROR_STATUS) {
    return { ok: false, message: CHECKIN_MESSAGES.network, status, errorCode: null };
  }
  const claimsJson = (contentType ?? "").toLowerCase().includes("json");
  const looksJson = bodyText.trimStart().startsWith("{");
  const parsed = claimsJson || looksJson ? tryParseJsonObject(bodyText) : null;
  if (parsed) {
    const userMsg = parsed.user_msg;
    if (typeof userMsg === "string" && userMsg.trim()) {
      const errorCode = parsed.error_code;
      return {
        ok: false,
        message: userMsg.trim(),
        status,
        errorCode: typeof errorCode === "string" ? errorCode : null,
      };
    }
    // {"detail": …} 或别的不认识的 JSON:一律按状态码给固定话
  }
  return { ok: false, message: messageForStatus(status), status, errorCode: null };
}

// ---------------------------------------------------------------------------
// 响应解析(成功侧)与凭证
// ---------------------------------------------------------------------------

/**
 * 凭证对象 —— checkin_api.py 响应契约「凭证对象」那节的镜像,
 * POST 200 与 GET /checkin/recent 共用同一形状。
 * geo_status / source 故意留成 string:前端只展示不分支,后端加枚举值不用动这边。
 */
export interface Receipt {
  receipt_no: string;
  worker_name: string;
  site_name: string | null;
  checked_at: string;
  work_date: string;
  geo_status: string;
  source: string;
  /** 为 null = 图被清理器清掉了(photo_purged_at 非空那条路),见 receiptImageUrl。 */
  artifact_id: string | null;
  photo_purged_at: string | null;
}

function stripTrailingSlash(base: string): string {
  return base.replace(/\/+$/, "");
}

/** POST /checkin 的地址。apiBase 的来路见 checkin.tsx 的 useApiBase 注释。 */
export function checkinUrl(apiBase: string): string {
  return `${stripTrailingSlash(apiBase)}/checkin`;
}

/** GET /checkin/recent 的地址。limit 不传 —— 缺省与上限都归服务端 clamp。 */
export function recentUrl(apiBase: string): string {
  return `${stripTrailingSlash(apiBase)}/checkin/recent`;
}

/**
 * 凭证图地址;**artifact_id 为 null 时返回 null,组件据此渲染
 * 「凭证图已过期清理」的文字,绝不渲染 <img>**(W7 §3.7,上线闸③)——
 * 裂图标和「图真没了」在界面上长得一模一样,分不开就等于没告诉工友。
 *
 * 地址形状 `${base}/by-id/<artifact_id>` 与 human.tsx 的 byIdUrl 完全同一条
 * 已验证路径(base 的同源清单见 checkin.tsx 的 ARTIFACT_BASE 注释)。
 */
export function receiptImageUrl(
  receipt: Pick<Receipt, "artifact_id">,
  base: string,
): string | null {
  if (!receipt.artifact_id) return null;
  return `${stripTrailingSlash(base)}/by-id/${receipt.artifact_id}`;
}

function toReceipt(raw: unknown): Receipt {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new CheckinContractError(CHECKIN_MESSAGES.badReceipt);
  }
  const rec = raw as Record<string, unknown>;
  const requireString = (key: string): string => {
    const value = rec[key];
    if (typeof value !== "string" || !value) {
      throw new CheckinContractError(CHECKIN_MESSAGES.badReceipt);
    }
    return value;
  };
  const optionalString = (key: string): string | null => {
    const value = rec[key];
    return typeof value === "string" && value ? value : null;
  };
  return {
    receipt_no: requireString("receipt_no"),
    worker_name: requireString("worker_name"),
    site_name: optionalString("site_name"),
    checked_at: requireString("checked_at"),
    work_date: requireString("work_date"),
    geo_status: typeof rec.geo_status === "string" ? rec.geo_status : "error",
    source: typeof rec.source === "string" ? rec.source : "fallback",
    artifact_id: optionalString("artifact_id"),
    photo_purged_at: optionalString("photo_purged_at"),
  };
}

/** POST /checkin 的 200 响应 → Receipt。形状不对就抛(中文),组件层上屏。 */
export function parseReceiptEnvelope(bodyText: string): Receipt {
  const parsed = tryParseJsonObject(bodyText);
  if (!parsed || parsed.ok !== true) {
    throw new CheckinContractError(CHECKIN_MESSAGES.badReceipt);
  }
  return toReceipt(parsed.data);
}

/** GET /checkin/recent 的 200 响应 → Receipt[](写入序倒排,由服务端保证)。 */
export function parseRecentEnvelope(bodyText: string): Receipt[] {
  const parsed = tryParseJsonObject(bodyText);
  if (!parsed || parsed.ok !== true) {
    throw new CheckinContractError(CHECKIN_MESSAGES.badReceipt);
  }
  const data = parsed.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new CheckinContractError(CHECKIN_MESSAGES.badReceipt);
  }
  const records = (data as Record<string, unknown>).records;
  if (!Array.isArray(records)) {
    throw new CheckinContractError(CHECKIN_MESSAGES.badReceipt);
  }
  return records.map(toReceipt);
}

/**
 * "2026-08-15T08:30:00+08:00" → "2026-08-15 08:30:00"(展示用)。
 *
 * 刻意**不过 Date()**:checked_at 带的是香港时间(+08:00,后端 receipt.py
 * 显式钉死 Asia/Hong_Kong),凭证上要显示的就是打卡那一刻的香港钟表时间;
 * 过 Date 再本地化,人在别的时区翻历史会看到漂过的时间,对不上凭证编号里的时刻。
 * 认不出的格式原样返回 —— 展示函数不该因为格式变了就抛。
 */
export function formatCheckedAt(checkedAt: string): string {
  const match = checkedAt.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : checkedAt;
}

// ---------------------------------------------------------------------------
// 扫码配对(W8)—— 电脑显码 → 手机打卡 → 电脑跟着变
// ---------------------------------------------------------------------------
//
// 要治的病:电脑上打开打卡面板会显示二维码,人扫走之后,**这台电脑什么都不知道**
// —— 摄像头还开着取景(投影时全场看的是主讲人的脸),界面还停在「请拍照」。
//
// 为什么是轮询不是 SSE(契约开头那节,别再翻案):Caddyfile 里只有
// `handle_path /api/*` 那条带 `flush_interval -1`,`@checkin` 那条专用路由没有 ——
// SSE 会被 Caddy 缓冲住,表现是「事件全都晚到或不到」,且不报错。
// 人拍一张照要十几秒,2 秒一轮绰绰有余,而且穿得过任何代理。

/** 配对串长度:32 位十六进制 = 128 bit。 */
export const PAIR_HEX_LEN = 32;

/** 轮询间隔(契约第五节钉死 2 秒)。后端的配对桶按 60/分钟配,2 秒一次 = 30/分钟。 */
export const PAIR_POLL_INTERVAL_MS = 2_000;

/** 二维码 URL 上那两个 query 键。**缺一不可**:
 * checkin 负责开面板、pair 负责联动,只带后者 = 手机落在首页、电脑永远等 waiting。
 * 常量而不是字面量,是因为读的人(checkin.tsx 的 useQueryState)和
 * 写的人(下面 buildCheckinPageUrl)是两处,拼错任何一处都没有报错。 */
export const CHECKIN_QUERY_KEY = "checkin";
export const CHECKIN_QUERY_OPEN_VALUE = "1";
export const PAIR_QUERY_KEY = "pair";

const PAIR_ID_PATTERN = /^[0-9a-f]{32}$/;

/**
 * 归一化并校验配对串:合法返回小写形式,**任何不合法一律返回 null**(不抛)。
 *
 * 归一化的用处:这串是从 URL query 里读回来的,中间经过二维码、扫码 App、
 * 登录跳转好几手,顺手把空白和大小写吃掉,比在每个调用点各写一遍稳。
 */
export function normalizePairId(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim().toLowerCase();
  return PAIR_ID_PATTERN.test(trimmed) ? trimmed : null;
}

/**
 * 生成一个新的配对串(32 位小写十六进制)。
 *
 * **必须 crypto.getRandomValues,不许 Math.random。** 理由不是「防猜」——
 * 它不是凭据、猜中也进不去(所有配对端点照样在登录闸和 X-Api-Key 后面);
 * 理由是**撞号**:两台电脑同时开面板要是撞上同一个串,A 的屏幕会显示 B 的打卡凭证,
 * 而凭证上有姓名、时间和那张自拍。Math.random 在同一毫秒开两个标签页并非撞不上,
 * getRandomValues 是 CSPRNG、128 bit,撞号这件事可以不用再想。
 *
 * getRandomValues 到处都有(不像 crypto.subtle / randomUUID 只在安全上下文),
 * 所以这里不做二级兜底:真没有就是浏览器太老,如实抛。
 */
export function newPairId(): string {
  if (!globalThis.crypto?.getRandomValues) {
    throw new CheckinContractError("這台瀏覽器生成不了配對碼,換個瀏覽器再試");
  }
  // 存下来再调会丢 this(浏览器里是 "Illegal invocation"),所以照原样从 crypto 上调
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(PAIR_HEX_LEN / 2));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 配对状态机,**只有这三个、且只能单向前进**(契约第三节)。 */
export const PAIR_STATES = ["waiting", "scanned", "done"] as const;
export type PairState = (typeof PAIR_STATES)[number];

const PAIR_STATE_RANK: Readonly<Record<PairState, number>> = Object.freeze({
  waiting: 0,
  scanned: 1,
  done: 2,
});

/**
 * 合并一次轮询结果:**只前进,不后退**。
 *
 * 两个来源都会「倒着说话」,而且都不报错:
 *  · 轮询两秒一发,网络一抖就会**后发先回** —— done 已经收到了,上一轮的
 *    waiting 才姗姗来迟;
 *  · 后端对**未知/过期**的 id 一律回 waiting(契约刻意不区分,免得变成探测接口),
 *    所以面板开着超过 TTL(10 分钟)之后,每一轮都回 waiting。
 *
 * 两者都会把屏幕上的「✅ 已打卡成功」打回「用手机扫这个码」—— 而人已经收工走了,
 * 留在屏幕上的是一句错话。只前进不后退之后,这两种情况都只是「不再更新」。
 */
export function advancePairState(current: PairState, incoming: PairState): PairState {
  return PAIR_STATE_RANK[incoming] > PAIR_STATE_RANK[current] ? incoming : current;
}

/**
 * 配对面板上给人看的三句话(契约第五节那张表,**一字不差**)。
 *
 * 第四种情形「轮询失败」在这里**故意没有对应文案**:配对断了不显示任何错误,
 * 静默重试就是了 —— 电脑那边不联动不影响任何人打卡,给工友看一行红字
 * 只会让他以为卡没打成。
 */
export const PAIR_MESSAGES: Readonly<Record<PairState, string>> = Object.freeze({
  waiting: "用手機掃這個碼,在手機上拍照打卡",
  scanned: "手機已經掃上了 —— 請在手機上拍一張自拍",
  done: "✅ 已打卡成功",
});

/**
 * 二维码里编的地址:`<origin>/?checkin=1&pair=<32位hex>`(契约第二节)。
 *
 * D7 仍然成立 —— 码里**没有 token、没有姓名**,pair 只是「哪台电脑在等哪次打卡」
 * 的关联号。变的只是「能不能带 query」:以前带了也活不过登录跳转
 * (serve_login 写死重定向 `/`),W8 把登录页改成带 `next` 回原地址,query 才活了。
 *
 * **配对串不合法时退化成只带 checkin=1,绝不抛**:这个返回值是在渲染期用的,
 * 抛出去就是整个打卡面板白屏 = 连卡都打不了。退化之后手机照样能扫开面板打卡,
 * 只是电脑那边不跟着变 —— 这正是「配对失败不影响打卡」该有的样子。
 */
export function buildCheckinPageUrl(origin: string, pairId: string | null): string {
  const base = `${stripTrailingSlash(origin)}/?${CHECKIN_QUERY_KEY}=${CHECKIN_QUERY_OPEN_VALUE}`;
  const pair = normalizePairId(pairId);
  return pair ? `${base}&${PAIR_QUERY_KEY}=${pair}` : base;
}

/** GET /checkin/pair?id=… 的地址。id 是校验过的 32 位十六进制,不需要转义。 */
export function pairStatusUrl(apiBase: string, pairId: string): string {
  return `${stripTrailingSlash(apiBase)}/checkin/pair?id=${pairId}`;
}

/** POST /checkin/pair/scanned 的地址(pair 走 X-GYT-Pair 头,不进 query)。 */
export function pairScannedUrl(apiBase: string): string {
  return `${stripTrailingSlash(apiBase)}/checkin/pair/scanned`;
}

export interface PairSnapshot {
  state: PairState;
  /** 只有 done 才可能非空;形状与 POST /checkin 成功时那个 receipt 完全一致。 */
  receipt: Receipt | null;
}

/**
 * 解析 GET /checkin/pair 的响应。**看不懂就返回 null,一个错都不抛。**
 *
 * 为什么不抛:轮询的失败一律静默(契约第五节)。抛的话组件里每一处都要
 * try/catch 才不会把红叉甩到打卡界面上,少写一处就是一次「卡明明打成了,
 * 电脑上却弹个错」。null 的语义是「这一轮没有可信的新消息」——
 * 调用方原地不动、两秒后再说,状态机的单向前进保证了原地不动永远是安全的。
 *
 * done 但凭证形状不对时**保留 done、凭证给 null**:电脑这边最要紧的是
 * 「别再让人对着自己的摄像头等」,凭证渲染不出来是次要的 ——
 * 那份凭证本来就在手机上,库里也记着。
 */
export function parsePairEnvelope(bodyText: string): PairSnapshot | null {
  const parsed = tryParseJsonObject(bodyText);
  if (!parsed || parsed.ok !== true) return null;
  const data = parsed.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const state = (data as Record<string, unknown>).state;
  if (typeof state !== "string") return null;
  if (!(PAIR_STATES as readonly string[]).includes(state)) return null;
  const rawReceipt = (data as Record<string, unknown>).receipt;
  let receipt: Receipt | null = null;
  if (state === "done" && rawReceipt) {
    try {
      receipt = toReceipt(rawReceipt);
    } catch {
      receipt = null;
    }
  }
  return { state: state as PairState, receipt };
}
