/**
 * checkin-lib.ts(scripts/frontend-overrides/)的单测。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *
 * 锁的都是**静默出错**的东西:编码漂了 → 姓名变乱码画进水印,一路无报错;
 * geo 状态漂了 → 后端 CHECK 约束打回,报错在数据库层;digest 头没了 →
 * 幂等层①永不命中,每次重发白烧一次水印,而功能看起来完全正常。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildCheckinHeaders,
  buildGeoHeader,
  CHECKIN_MESSAGES,
  CheckinContractError,
  checkinUrl,
  clearEventId,
  decodeNameFromHeader,
  DIGEST_HEX_LEN,
  encodeNameForHeader,
  EVENT_ID_STORAGE_KEY,
  formatCheckedAt,
  GEO_ACQUIRE_TIMEOUT_MS,
  GEO_MAX_AGE_MS,
  GeoResult,
  GEO_SEND_STATUSES,
  GEO_SUBMIT_WAIT_MS,
  geoResultFromPosition,
  geoStatusFromPositionError,
  loadOrCreateEventId,
  loadSavedIdentity,
  MAX_NAME_BYTES,
  NETWORK_ERROR_STATUS,
  newEventId,
  normalizeError,
  parseRecentEnvelope,
  parseReceiptEnvelope,
  Receipt,
  receiptImageUrl,
  recentUrl,
  saveIdentity,
  sha256Hex,
  StorageLike,
  withSubmitDeadline,
} from "../frontend-overrides/checkin-lib";

/**
 * 编解码往返向量,抄自 backend/tests/unit/test_checkin_api.py 的
 * ROUNDTRIP_VECTORS(七条)—— **两边要逐条一致**,那边的注释也指回本文件。
 * 漂了的表现:后端解出来的姓名和工友输入的不是一个东西,而水印照画、
 * 凭证照出、库里照存,一路无报错。
 *
 * 第二列的期望编码是 2026-08-15 用后端同款算法算出来钉死的
 * (python3: base64.urlsafe_b64encode(name.encode("utf-8")).rstrip(b"=")),
 * 所以这份表不止「两边同表」,还把**字节级输出**钉在了这里 ——
 * 哪边偷偷换了实现,这里先红。
 */
const ROUNDTRIP_VECTORS: ReadonlyArray<readonly [name: string, encoded: string]> = [
  ["张三", "5byg5LiJ"],
  ["陳大文", "6Zmz5aSn5paH"], // 繁体 —— 香港现场的常态
  ["李四-B组", "5p2O5ZubLULnu4Q"],
  ["Ada Wong", "QWRhIFdvbmc"],
  ["王𠮶", "546L8KCutg"], // HKSCS 平面 2,UTF-8 四字节
  ["工地A栋 3/F", "5bel5ZywQeagiyAzL0Y"],
  ["名字里有空格 和 中点·", "5ZCN5a2X6YeM5pyJ56m65qC8IOWSjCDkuK3ngrnCtw"],
];

function memoryStorage(): StorageLike & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => void map.set(key, value),
    removeItem: (key) => void map.delete(key),
  };
}

/** 造一份合法凭证,单测里按需覆盖字段。 */
function receiptFixture(overrides: Partial<Receipt> = {}): Receipt {
  return {
    receipt_no: "GYT-A-20260815-083000-1a2b",
    worker_name: "张三",
    site_name: "A栋",
    checked_at: "2026-08-15T08:30:00+08:00",
    work_date: "2026-08-15",
    geo_status: "ok",
    source: "camera",
    artifact_id: "0123456789abcdef0123456789abcdef",
    photo_purged_at: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("名称编解码(与后端 encode_name 逐字节一致)", () => {
  it.each(ROUNDTRIP_VECTORS)("编码钉死:%s", (name, encoded) => {
    expect(encodeNameForHeader(name)).toBe(encoded);
  });

  it.each(ROUNDTRIP_VECTORS)("往返之后原样不变:%s", (name) => {
    expect(decodeNameFromHeader(encodeNameForHeader(name))).toBe(name);
  });

  it.each(ROUNDTRIP_VECTORS)("编出来的全是 header 安全字符:%s", (name) => {
    const value = encodeNameForHeader(name);
    // ASCII 且只含 Base64URL 字母表 —— 别的字符会被中间件截断或拆分
    expect(/^[A-Za-z0-9\-_]*$/.test(value)).toBe(true);
  });

  it("不带 = 填充(后端 encode_name 刻意剥掉,解码侧补回)", () => {
    expect(encodeNameForHeader("张")).not.toContain("=");
    expect(decodeNameFromHeader(encodeNameForHeader("张"))).toBe("张");
  });

  it("空串安全往返(地盤名可省略,空串不能变 null 也不能抛)", () => {
    expect(encodeNameForHeader("")).toBe("");
    expect(decodeNameFromHeader("")).toBe("");
  });

  it("超长姓名在编码侧就被拒", () => {
    expect(() => encodeNameForHeader("张".repeat(MAX_NAME_BYTES))).toThrow(
      CheckinContractError,
    );
  });

  it("超长输入在解码侧先查长度再解码(不给放大器机会)", () => {
    expect(() => decodeNameFromHeader("A".repeat(1_000_000))).toThrow(
      CheckinContractError,
    );
  });

  it("控制字符与换行在编码侧就被拒(后端 decode 也会拒,这边提前失败)", () => {
    for (const bad of ["张\n三", "张\r\n三", "张\x00三", "张\x1f三", "张\x7f"]) {
      expect(() => encodeNameForHeader(bad)).toThrow(CheckinContractError);
    }
  });

  it("非法 Base64URL / 非 UTF-8 收敛成契约异常", () => {
    expect(() => decodeNameFromHeader("这不是base64!!!")).toThrow(
      CheckinContractError,
    );
    // "__79" 是 0xff 0xfe 0xfd 的合法 Base64URL,但那三个字节不是合法 UTF-8
    expect(() => decodeNameFromHeader("__79")).toThrow(CheckinContractError);
    // "/" 属于标准 base64 字母表,不在 URL 安全表里 —— 也要收敛成契约异常
    expect(() => decodeNameFromHeader("//79")).toThrow(CheckinContractError);
    // 无填充编码里长度 mod 4 == 1 不可能出现
    expect(() => decodeNameFromHeader("AAAAA")).toThrow(CheckinContractError);
  });
});

describe("定位的三个时间常量与提交期限(2026-08-15 首打必超时那个 bug 的回归)", () => {
  // 背景:原来只有一个 GEO_TIMEOUT_MS=3000,既当定位预算又当提交期限,
  // 而真机实测冷启动首次 fix 要 5344ms —— 于是每个人的第一次打卡都拿不到定位。
  // 下面几条锁的是「别再合回一个数」。

  it("定位预算必须显著大于实测的冷启动耗时", () => {
    // 实测 5344ms(macOS/WiFi 定位,权限已授)。工地信号只会更差,
    // 所以这里要的不是「刚好够」,是「有数量级余量」。
    expect(GEO_ACQUIRE_TIMEOUT_MS).toBeGreaterThan(5344 * 2);
  });

  it("提交期限必须远小于定位预算 —— 两个数各司其职,不许合并", () => {
    // 前者决定「会不会卡住人」,后者决定「能不能拿到」。
    // 合成一个数就是那个 bug 本身。
    expect(GEO_SUBMIT_WAIT_MS).toBeLessThan(GEO_ACQUIRE_TIMEOUT_MS / 5);
  });

  it("提交期限要短到工友感觉不出来", () => {
    expect(GEO_SUBMIT_WAIT_MS).toBeLessThanOrEqual(2000);
  });

  it("必须接受缓存位置 —— 默认 0 会强制每次重新定位,是首打必空的第三个原因", () => {
    expect(GEO_MAX_AGE_MS).toBeGreaterThan(0);
  });

  it("定位赶在期限内到 → 用真结果", async () => {
    const pending = Promise.resolve<GeoResult>({
      status: "ok",
      lat: 22.3,
      lon: 114.1,
      accuracyM: 35,
    });
    // 期限给 0 也不该抢跑:已 resolve 的 promise 在同一轮微任务里就赢了
    await expect(withSubmitDeadline(pending, 0)).resolves.toEqual({
      status: "ok",
      lat: 22.3,
      lon: 114.1,
      accuracyM: 35,
    });
  });

  it("定位没赶上 → 按 timeout 照发,绝不卡住提交", async () => {
    // 永不 settle 的 promise = 最坏情况(定位彻底没响应)
    const never = new Promise<GeoResult>(() => {});
    const fired: number[] = [];
    const fakeTimer = (fn: () => void, ms: number) => {
      fired.push(ms);
      fn(); // 立刻触发,不真等 —— 真 sleep 的用例又慢又飘
      return 0;
    };
    await expect(withSubmitDeadline(never, 1500, fakeTimer)).resolves.toEqual({
      status: "timeout",
    });
    expect(fired).toEqual([1500]);
  });

  it("期限缺省值就是 GEO_SUBMIT_WAIT_MS", async () => {
    const never = new Promise<GeoResult>(() => {});
    let seen = -1;
    const fakeTimer = (fn: () => void, ms: number) => {
      seen = ms;
      fn();
      return 0;
    };
    await withSubmitDeadline(never, undefined, fakeTimer);
    expect(seen).toBe(GEO_SUBMIT_WAIT_MS);
  });
});

describe("定位(六种形态:ok 四段,四种失败单段,absent 前端不可表达)", () => {
  it("ok 恰好四段,分隔符是分号 —— 与 checkin_api.py docstring 的示例逐字一致", () => {
    expect(
      buildGeoHeader({ status: "ok", lat: 22.302711, lon: 114.177216, accuracyM: 12.5 }),
    ).toBe("ok;22.302711;114.177216;12.5");
  });

  it.each([["denied"], ["timeout"], ["unsupported"], ["error"]] as const)(
    "非 ok 只有状态一段:%s",
    (status) => {
      expect(buildGeoHeader({ status })).toBe(status);
    },
  );

  it("absent 不在前端可发状态里 —— 它是服务端对「header 缺失」的记法", () => {
    expect(GEO_SEND_STATUSES).not.toContain("absent");
  });

  it("非有限数字被兜住(绕过 geoResultFromPosition 手搓的程序错误)", () => {
    expect(() =>
      buildGeoHeader({ status: "ok", lat: Number.NaN, lon: 114, accuracyM: 5 }),
    ).toThrow(CheckinContractError);
  });

  it("GeolocationPositionError 的码映射:1→denied / 3→timeout / 2 与未知→error", () => {
    expect(geoStatusFromPositionError(1)).toBe("denied");
    expect(geoStatusFromPositionError(3)).toBe("timeout");
    expect(geoStatusFromPositionError(2)).toBe("error");
    expect(geoStatusFromPositionError(0)).toBe("error");
    expect(geoStatusFromPositionError(99)).toBe("error");
  });

  it("浏览器坐标合法 → ok;NaN/越界 → 降级 error(照样能交,不挡打卡)", () => {
    expect(
      geoResultFromPosition({
        coords: { latitude: 22.3, longitude: 114.1, accuracy: 30 },
      }),
    ).toEqual({ status: "ok", lat: 22.3, lon: 114.1, accuracyM: 30 });
    expect(
      geoResultFromPosition({
        coords: { latitude: Number.NaN, longitude: 114.1, accuracy: 30 },
      }),
    ).toEqual({ status: "error" });
    expect(
      geoResultFromPosition({
        coords: { latitude: 91, longitude: 114.1, accuracy: 30 },
      }),
    ).toEqual({ status: "error" });
    expect(
      geoResultFromPosition({
        coords: { latitude: 22.3, longitude: 181, accuracy: 30 },
      }),
    ).toEqual({ status: "error" });
    expect(
      geoResultFromPosition({
        coords: { latitude: 22.3, longitude: 114.1, accuracy: -1 },
      }),
    ).toEqual({ status: "error" });
  });
});

describe("照片指纹 sha256Hex", () => {
  it("空字节 → 标准 sha256(与后端 test_照片哈希是标准_sha256 同一向量)", async () => {
    await expect(sha256Hex(new Uint8Array(0))).resolves.toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });

  it("abc → 标准 sha256(FIPS 180 的公开向量)", async () => {
    await expect(sha256Hex(new TextEncoder().encode("abc"))).resolves.toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });

  it("crypto 缺失(非安全上下文)必须抛中文错误,绝不静默省掉 digest 头", async () => {
    vi.stubGlobal("crypto", undefined);
    await expect(sha256Hex(new Uint8Array([1]))).rejects.toThrow(/https/);
  });
});

describe("buildCheckinHeaders(发侧契约:头名全小写,X-Api-Key 由组件层并入)", () => {
  const okInput = {
    eventId: "11111111-2222-4333-8444-555555555555",
    worker: "张三",
    site: "A栋",
    geo: { status: "ok", lat: 22.302711, lon: 114.177216, accuracyM: 12.5 } as const,
    source: "camera" as const,
    digestHex: "a".repeat(DIGEST_HEX_LEN),
  };

  it("完整形状:六个头,名字全小写,值与各纯函数一致", () => {
    expect(buildCheckinHeaders(okInput)).toEqual({
      "x-gyt-event-id": "11111111-2222-4333-8444-555555555555",
      "x-gyt-worker": "5byg5LiJ",
      "x-gyt-site": "Qeagiw",
      "x-gyt-geo": "ok;22.302711;114.177216;12.5",
      "x-gyt-source": "camera",
      "x-gyt-digest": "a".repeat(DIGEST_HEX_LEN),
    });
  });

  it("地盤为空/缺省时省略 x-gyt-site(后端指纹里省略 ≡ 空串)", () => {
    const withoutSite = buildCheckinHeaders({ ...okInput, site: undefined });
    expect(withoutSite).not.toHaveProperty("x-gyt-site");
    const blankSite = buildCheckinHeaders({ ...okInput, site: "   " });
    expect(blankSite).not.toHaveProperty("x-gyt-site");
  });

  it("digest 归一化成小写(镜像后端 validate_digest_hex)", () => {
    const headers = buildCheckinHeaders({
      ...okInput,
      digestHex: "A".repeat(DIGEST_HEX_LEN),
    });
    expect(headers["x-gyt-digest"]).toBe("a".repeat(DIGEST_HEX_LEN));
  });

  it("不像样的输入逐个被拒:空姓名 / 坏 digest / 坏 source / 坏 eventId", () => {
    expect(() => buildCheckinHeaders({ ...okInput, worker: "  " })).toThrow(
      CheckinContractError,
    );
    expect(() =>
      buildCheckinHeaders({ ...okInput, digestHex: "a".repeat(DIGEST_HEX_LEN - 1) }),
    ).toThrow(CheckinContractError);
    expect(() =>
      buildCheckinHeaders({ ...okInput, digestHex: "g".repeat(DIGEST_HEX_LEN) }),
    ).toThrow(CheckinContractError);
    expect(() =>
      buildCheckinHeaders({ ...okInput, source: "album" as unknown as "camera" }),
    ).toThrow(CheckinContractError);
    expect(() => buildCheckinHeaders({ ...okInput, eventId: "" })).toThrow(
      CheckinContractError,
    );
    expect(() => buildCheckinHeaders({ ...okInput, eventId: "有 空格" })).toThrow(
      CheckinContractError,
    );
    expect(() => buildCheckinHeaders({ ...okInput, eventId: "x".repeat(129) })).toThrow(
      CheckinContractError,
    );
  });
});

describe("event_id 的存取清(sessionStorage 语义,存储可注入)", () => {
  it("newEventId:UUID 形状、header 安全、彼此不同", () => {
    const ids = new Set(Array.from({ length: 8 }, () => newEventId()));
    expect(ids.size).toBe(8);
    for (const id of ids) {
      expect(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)).toBe(
        true,
      );
    }
  });

  it("loadOrCreateEventId:首次创建并落存储,之后复用同一个(刷新防重复记账)", () => {
    const storage = memoryStorage();
    const first = loadOrCreateEventId(storage);
    expect(storage.map.get(EVENT_ID_STORAGE_KEY)).toBe(first);
    expect(loadOrCreateEventId(storage)).toBe(first);
  });

  it("clearEventId 之后是新的一次打卡", () => {
    const storage = memoryStorage();
    const first = loadOrCreateEventId(storage);
    clearEventId(storage);
    expect(storage.map.has(EVENT_ID_STORAGE_KEY)).toBe(false);
    expect(loadOrCreateEventId(storage)).not.toBe(first);
  });

  it("存储抛错(隐私模式)时降级为内存 ID,打卡不被存储问题挡住", () => {
    const broken: StorageLike = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    };
    const id = loadOrCreateEventId(broken);
    expect(id.length).toBeGreaterThan(0);
    expect(() => clearEventId(broken)).not.toThrow();
  });
});

describe("姓名/地盤记忆(TODO-35:身份只是字符串,记住只是省打字)", () => {
  it("存了能读回,读不到给空串", () => {
    const storage = memoryStorage();
    expect(loadSavedIdentity(storage)).toEqual({ worker: "", site: "" });
    saveIdentity(storage, " 张三 ", " A栋 ");
    expect(loadSavedIdentity(storage)).toEqual({ worker: "张三", site: "A栋" });
  });
});

describe("normalizeError(W7 §3.11 的四种形状 → 一个中文 message)", () => {
  it("形状①后端 Envelope:user_msg 原样透传,error_code 带出", () => {
    const normalized = normalizeError(
      409,
      "application/json",
      JSON.stringify({
        ok: false,
        data: null,
        user_msg: "这次打卡的信息和之前那次对不上",
        error_code: "CONFLICT",
      }),
    );
    expect(normalized).toEqual({
      ok: false,
      message: "这次打卡的信息和之前那次对不上",
      status: 409,
      errorCode: "CONFLICT",
    });
  });

  it("形状② langgraph 的 {detail:…}:英文不上屏,按状态码给固定中文", () => {
    const normalized = normalizeError(
      401,
      "application/json",
      JSON.stringify({ detail: "Invalid API key" }),
    );
    expect(normalized.message).toBe(CHECKIN_MESSAGES.authFailed);
    expect(normalized.message).not.toContain("Invalid");
    expect(normalized.status).toBe(401);
  });

  it("形状③ Caddy 的 413 错误页(非 JSON):给「照片太大」,别让人去翻 uploads.py", () => {
    const normalized = normalizeError(
      413,
      "text/html",
      "<html><body>413 Request Entity Too Large</body></html>",
    );
    expect(normalized.message).toBe(CHECKIN_MESSAGES.tooLarge);
    expect(normalized.message).toContain("照片太大");
  });

  it("形状④ 网络层异常(status=0,压根没有响应)", () => {
    const normalized = normalizeError(NETWORK_ERROR_STATUS, null, "");
    expect(normalized.message).toBe(CHECKIN_MESSAGES.network);
    expect(normalized.status).toBe(0);
  });

  it("content-type 撒谎也不崩:说是 JSON 的 HTML / 没说是 JSON 的 Envelope", () => {
    const htmlAsJson = normalizeError(500, "application/json", "<html>oops</html>");
    expect(htmlAsJson.message).toBe(CHECKIN_MESSAGES.serverError);
    const envelopeAsText = normalizeError(
      429,
      "text/plain",
      JSON.stringify({ ok: false, data: null, user_msg: "歇一歇", error_code: "RATE_LIMITED" }),
    );
    expect(envelopeAsText.message).toBe("歇一歇");
  });

  it("零散状态码的固定话:404(后端还没部署)/ 429 / 5xx", () => {
    expect(normalizeError(404, "text/plain", "not found").message).toBe(
      CHECKIN_MESSAGES.notFound,
    );
    expect(normalizeError(429, "text/plain", "").message).toBe(
      CHECKIN_MESSAGES.rateLimited,
    );
    expect(normalizeError(502, "text/html", "bad gateway").message).toBe(
      CHECKIN_MESSAGES.serverError,
    );
  });
});

describe("凭证与地址", () => {
  it("receiptImageUrl:artifact_id 为 null → null(组件渲染「凭证图已过期清理」文字,上线闸③)", () => {
    expect(receiptImageUrl(receiptFixture({ artifact_id: null }), "http://127.0.0.1:8788")).toBe(
      null,
    );
  });

  it("receiptImageUrl:非空走 /by-id/,与 human.tsx 的 byIdUrl 同一条路;尾斜杠被吃掉", () => {
    const receipt = receiptFixture();
    expect(receiptImageUrl(receipt, "http://127.0.0.1:8788")).toBe(
      "http://127.0.0.1:8788/by-id/0123456789abcdef0123456789abcdef",
    );
    expect(receiptImageUrl(receipt, "https://example.com/artifacts/")).toBe(
      "https://example.com/artifacts/by-id/0123456789abcdef0123456789abcdef",
    );
  });

  it("checkinUrl / recentUrl:公网同源 /api 与本机直连两种 base 都拼得对", () => {
    expect(checkinUrl("http://localhost:2024")).toBe("http://localhost:2024/checkin");
    expect(checkinUrl("https://example.com/api/")).toBe("https://example.com/api/checkin");
    expect(recentUrl("https://example.com/api")).toBe(
      "https://example.com/api/checkin/recent",
    );
  });

  it("parseReceiptEnvelope:合法 Envelope 解出凭证;ok!=true / data 缺字段 → 中文契约错误", () => {
    const receipt = receiptFixture();
    expect(
      parseReceiptEnvelope(
        JSON.stringify({ ok: true, data: receipt, user_msg: null, error_code: null }),
      ),
    ).toEqual(receipt);
    expect(() => parseReceiptEnvelope(JSON.stringify({ ok: false, data: null }))).toThrow(
      CheckinContractError,
    );
    expect(() =>
      parseReceiptEnvelope(JSON.stringify({ ok: true, data: { worker_name: "张三" } })),
    ).toThrow(CheckinContractError);
    expect(() => parseReceiptEnvelope("<html>登录页</html>")).toThrow(CheckinContractError);
  });

  it("parseReceiptEnvelope:artifact_id 缺失/null 归一成 null(清理后的旧记录)", () => {
    const purged = receiptFixture({ artifact_id: null, photo_purged_at: "2026-09-01T00:00:00+08:00" });
    const parsed = parseReceiptEnvelope(JSON.stringify({ ok: true, data: purged }));
    expect(parsed.artifact_id).toBe(null);
    expect(receiptImageUrl(parsed, "http://127.0.0.1:8788")).toBe(null);
  });

  it("parseRecentEnvelope:records 数组逐条解出;形状不对 → 契约错误", () => {
    const rows = [receiptFixture(), receiptFixture({ receipt_no: "GYT-A-20260815-083001-ffff" })];
    expect(
      parseRecentEnvelope(JSON.stringify({ ok: true, data: { records: rows } })),
    ).toEqual(rows);
    expect(() =>
      parseRecentEnvelope(JSON.stringify({ ok: true, data: { records: "不是数组" } })),
    ).toThrow(CheckinContractError);
    expect(() => parseRecentEnvelope(JSON.stringify({ ok: true, data: null }))).toThrow(
      CheckinContractError,
    );
  });

  it("formatCheckedAt:香港钟表时间原样展示,不过 Date() 本地化;认不出的原样返回", () => {
    expect(formatCheckedAt("2026-08-15T08:30:00+08:00")).toBe("2026-08-15 08:30:00");
    expect(formatCheckedAt("看不懂的时间")).toBe("看不懂的时间");
  });
});
