/**
 * image-compress.ts(scripts/frontend-overrides/)的单测。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run image-compress.test.ts
 *
 * 钉的全是**静默出错**的东西 —— 这条链上没有任何一处会报错,坏了只表现为
 * 「照片好像糊了一点」或者「怎么还是那么慢」:
 *   · 判据与服务端 `_prepare_image` 漂了 → 客户端缩一次、服务端再缩一次,
 *     两代有损叠加,而「有没有戴安全帽」这种判断经不起反复有损压缩;
 *   · `needsWork` 那条被删掉 → 1920×1080 的手机照片本来一个字节都不用动,
 *     却被白白重编码一次,纯亏画质、一点速度都不省;
 *   · 收窄到 JPEG 那条被「顺手放宽」→ animated WEBP / APNG 经 canvas 拍平成 1 帧,
 *     服务端 `frames > 1` 那道闸**再也不会触发**,工友以为系统看了整段,其实只看了第一帧;
 *   · `shouldKeepCompressed` 被当成多余逻辑删掉 → 「压了个寂寞,还掉了画质」;
 *   · `fitWithin` 让短边落到 0 → canvas 抛错 → 整条压缩退回原图,而它本来就是
 *     「失败就退原图」的设计,于是**连一句报错都不会有**,只是永远没提速。
 *
 * ⚠️ 覆盖边界(与被测文件头注最后一节一致):canvas 的真实解码/编码路径
 *    —— EXIF 转正、铺色、编码质量 —— **这里一条都没测**。jsdom 不实现 toBlob,
 *    为它引 node-canvas 是给测试装一个原生依赖,得不偿失。那几件只有真机能验。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  COMPRESSIBLE_TYPE,
  DECODE_FLOOR_BYTES,
  JPEG_QUALITY,
  MAX_EDGE,
  MIN_GAIN_RATIO,
  TARGET_BYTES,
  compressImageFile,
  fitWithin,
  needsWork,
  shouldKeepCompressed,
  shouldTryCompress,
} from "@/lib/image-compress";

/**
 * 造一个只用来喂判据的 File。
 *
 * Node 20+ 自带全局 `File`(本机与 CI 都远高于这条线),所以正常走**真 File** ——
 * 只有真 File 才能证明 `compressImageFile` 早退时还回来的是**同一个对象**
 * 而不是某个看着一样的替身(下面用 `toBe` 断的就是引用相等)。
 *
 * 万一运行时没有全局 File(某些精简发行版会把 undici 那套裁掉),退到一个只带
 * `{name,type,size,lastModified}` 四个字段的最小替身 + `as unknown as File`:
 * 被测的四个纯函数只看 type/size,`compressImageFile` 的两条早退分支也只看这两个,
 * 再多造也是白造。
 */
function makeFile(
  bytes: number,
  type: string = COMPRESSIBLE_TYPE,
  name = "工地照片.jpg",
): File {
  if (typeof File === "function") {
    return new File([new Uint8Array(bytes)], name, { type, lastModified: 0 });
  }
  return { name, type, size: bytes, lastModified: 0 } as unknown as File;
}

/** 读后端源码。读不到就让测试红(不是 skip)—— 读不到本身就说明那边挪窝了。 */
function readBackend(relative: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../../backend/src/gyt/${relative}`, import.meta.url)),
    "utf8",
  );
}

// ---------------------------------------------------------------------------
// 常量:三个数是服务端配置的**镜像**,单独改一个没有任何报错
// ---------------------------------------------------------------------------

describe("常量与服务端同源", () => {
  /**
   * 🔴 这三个数镜像后端:
   *     MAX_EDGE      ← `config.py` 的 `photo_compress_max_edge_px`(默认 2048)
   *     TARGET_BYTES  ← `config.py` 的 `photo_compress_target_mb`(默认 4.0)
   *     JPEG_QUALITY  ← `agents/safety/tools.py` 的 `_JPEG_QUALITY_STEPS` 第一档 85
   *
   * **两边必须一起改。** 漂了的表现:比如有人为省钱把服务端 max_edge 降到 1024,
   * 客户端还在发 2048 → 服务端 `max(w,h) > 1024` 成立 → **再缩一次、再有损编码一次**,
   * 两代有损叠加、识图准确率掉,而日志、界面、报错里一个字都不会有。
   */
  it("🔴 MAX_EDGE / TARGET_BYTES / JPEG_QUALITY 三个数没被单独改过", () => {
    expect(MAX_EDGE).toBe(2048);
    expect(TARGET_BYTES).toBe(4 * 1024 * 1024);
    expect(JPEG_QUALITY).toBe(0.85);
  });

  /**
   * 上面那条只挡「这边被改了」,挡不住「那边被改了」—— 而那边被改同样是两代有损。
   * 所以再从后端源码里**读一遍真值**比对,别手抄。
   */
  it("🔴 与后端源码逐个比对(从 config.py / tools.py 读,不手抄)", () => {
    const config = readBackend("config.py");
    const tools = readBackend("agents/safety/tools.py");

    const maxEdge = /photo_compress_max_edge_px:\s*int\s*=\s*Field\(default=(\d+)/.exec(config);
    const targetMb = /photo_compress_target_mb:\s*float\s*=\s*Field\(default=([\d.]+)/.exec(config);
    const firstQuality = /_JPEG_QUALITY_STEPS[^=]*=\s*\(\s*(\d+)/.exec(tools);

    // 空转闸:读不到就说明后端那几行的写法变了,别让守卫静默放行
    expect(maxEdge, "config.py 里读不到 photo_compress_max_edge_px —— 那边改写法了?").not.toBeNull();
    expect(targetMb, "config.py 里读不到 photo_compress_target_mb —— 那边改写法了?").not.toBeNull();
    expect(firstQuality, "tools.py 里读不到 _JPEG_QUALITY_STEPS —— 那边改写法了?").not.toBeNull();

    expect(Number(maxEdge?.[1]), "服务端 max_edge 变了,MAX_EDGE 要跟着改").toBe(MAX_EDGE);
    expect(
      Number(targetMb?.[1]) * 1024 * 1024,
      "服务端 target_mb 变了,TARGET_BYTES 要跟着改",
    ).toBe(TARGET_BYTES);
    expect(
      Number(firstQuality?.[1]) / 100,
      "服务端质量阶梯的第一档变了,JPEG_QUALITY 要跟着改",
    ).toBe(JPEG_QUALITY);
  });

  /**
   * `needsWork` 是对这个 if 取反。哪天后端把 `<=` 改成 `<`(或者把 and 改成 or),
   * 客户端的边界就整体错开一格 —— 表现是恰好卡在 2048 / 4MB 的图两边各压一次。
   */
  it("🔴 服务端那个「原样透传」的 if 还是原来的形状", () => {
    expect(
      readBackend("agents/safety/tools.py"),
      "_prepare_image 的透传判据变了 —— needsWork 是照它取反写的,要一起改",
    ).toMatch(/if max\(width, height\) <= max_edge and len\(payload\) <= limit_bytes:/);
  });

  it("COMPRESSIBLE_TYPE 是且只是 image/jpeg(收窄的三个理由在头注,别放宽)", () => {
    expect(COMPRESSIBLE_TYPE).toBe("image/jpeg");
  });
});

// ---------------------------------------------------------------------------
// shouldTryCompress —— 解码前的快速分流。只看类型和大小,不解码。
// ---------------------------------------------------------------------------

describe("shouldTryCompress 解码前分流", () => {
  const BIG = DECODE_FLOOR_BYTES * 4; // 远超性能闸,免得把两条判据混在一起看

  it("够大的 JPEG 才进得去", () => {
    expect(shouldTryCompress({ type: "image/jpeg", size: BIG })).toBe(true);
  });

  /**
   * 🔴 **本组最重要的一条。** 收窄到 JPEG 不是图省事,是为了不绕过服务端的动图闸:
   * `_prepare_image` 有 `if frames > 1: raise ImageRejected(...)`,而 canvas 只画得出
   * 第一帧 —— animated WEBP / APNG 一旦经过客户端就变成 frames == 1,
   * **那道闸再也不会触发**,工友以为系统看了整段,其实只看了第一帧,而且一句话都不说。
   * GIF 好排除,APNG / animated WEBP 不好认,收窄到 JPEG 是唯一不用去认动图的办法。
   *
   * 顺带两条也靠它:mime 前后不变,use-file-upload.tsx 的查重才不会漏;
   * 而 DXF 图纸走的是另一条上传路,压根不该经过这里。
   */
  it("🔴 非 JPEG 一律不碰 —— 放宽会让服务端的动图闸永远不触发", () => {
    const notJpeg = [
      "image/png", // 截图,压成 JPEG 会让查重按 mimeType 比时判不出重复
      "image/webp", // 可能是 animated WEBP,canvas 会拍平成 1 帧
      "image/gif", // 同上
      "application/pdf",
      "application/octet-stream", // DXF 图纸的浏览器 MIME 就常是这个
      "", // 认不出类型时浏览器给空串
    ];
    for (const type of notJpeg) {
      expect(shouldTryCompress({ type, size: BIG }), `${type || "(空串)"} 不该被压`).toBe(false);
    }
  });

  it("类型判定大小写不敏感 —— 有些来源给的是大写", () => {
    expect(shouldTryCompress({ type: "IMAGE/JPEG", size: BIG })).toBe(true);
    expect(shouldTryCompress({ type: "Image/Jpeg", size: BIG })).toBe(true);
  });

  /**
   * 这道闸是**纯性能闸,不是策略闸** —— 只为了别给一张 100KB 的图白解码一次
   * (解一张 4000×3000 要 48MB 位图,低端安卓上是真实卡顿)。判据是 `>` 不是 `>=`,
   * 恰好等于就不进。
   */
  it("恰好等于 DECODE_FLOOR_BYTES 不进(判据是 >),多一个字节才进", () => {
    expect(shouldTryCompress({ type: "image/jpeg", size: DECODE_FLOOR_BYTES })).toBe(false);
    expect(shouldTryCompress({ type: "image/jpeg", size: DECODE_FLOOR_BYTES + 1 })).toBe(true);
    expect(shouldTryCompress({ type: "image/jpeg", size: 0 })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// needsWork —— 本文件最值钱的一组,它逐字镜像服务端的透传判据
// ---------------------------------------------------------------------------

describe("needsWork 镜像服务端的透传判据", () => {
  /**
   * 🔴 **这条钉的回归是「白白多压一次」。**
   *
   * 1920×1080 / 800KB 是手机照片里极常见的一档,长边没超 2048、体积没超 4MB ——
   * 服务端 `_prepare_image` 对它是 `return payload`,**连重编码都不做**。
   * 那客户端也一个字节都不许动。判成「要压」的后果不是慢,是**纯亏画质**:
   * tools.py 的注释原话是「重编码只会白白损失画质,而判断有没有戴安全帽
   * 经不起反复有损压缩」,而且一点速度都不省(体积本来就不大)。
   */
  it("🔴 1920×1080 / 800KB 的手机照片不用压 —— 服务端对它是原样透传", () => {
    expect(needsWork(1920, 1080, 800 * 1024)).toBe(false);
  });

  it("长边超过 2048 要压(横图 / 竖图各一条,Math.max 两个方向都要管)", () => {
    expect(needsWork(4000, 3000, 1024)).toBe(true); // 横
    expect(needsWork(3000, 4000, 1024)).toBe(true); // 竖
  });

  it("长边恰好 2048 不用压(判据是 >),2049 才要压", () => {
    expect(needsWork(2048, 1536, 1024)).toBe(false);
    expect(needsWork(1536, 2048, 1024)).toBe(false); // 竖过来同理
    expect(needsWork(2049, 1536, 1024)).toBe(true);
  });

  it("尺寸没超但体积超 4MB 也要压 —— 服务端到这一步会走质量阶梯", () => {
    expect(needsWork(1600, 1200, TARGET_BYTES + 1)).toBe(true);
  });

  it("体积恰好 4MB 不用压(判据同样是 >)", () => {
    expect(needsWork(1600, 1200, TARGET_BYTES)).toBe(false);
  });

  it("两个都超当然要压", () => {
    expect(needsWork(4000, 3000, 6 * 1024 * 1024)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// fitWithin —— 等比缩进框里。返回值要么是 {0,0}(放弃),要么每边都 >= 1 的整数。
// ---------------------------------------------------------------------------

describe("fitWithin 等比缩放", () => {
  it("长边没超上限就原样返回,**不放大**", () => {
    expect(fitWithin(800, 600)).toEqual({ width: 800, height: 600 });
    expect(fitWithin(1, 1)).toEqual({ width: 1, height: 1 });
    expect(fitWithin(2048, 1536)).toEqual({ width: 2048, height: 1536 });
  });

  it("横图 4000×3000 → 2048×1536", () => {
    expect(fitWithin(4000, 3000)).toEqual({ width: 2048, height: 1536 });
  });

  it("竖图 3000×4000 → 1536×2048 —— 缩的是长边,不是宽", () => {
    expect(fitWithin(3000, 4000)).toEqual({ width: 1536, height: 2048 });
  });

  it("正方形 4000×4000 → 2048×2048", () => {
    expect(fitWithin(4000, 4000)).toEqual({ width: 2048, height: 2048 });
  });

  /**
   * 🔴 极端长宽比(工地上真会拍全景条)缩完短边可能不足 1。
   * `canvas.height = 0` 会让整条压缩抛错 → catch 里退回原图 ——
   * 而「退回原图」本来就是设计内的正常行为,所以**不会有任何报错**,
   * 只是这类图永远没提速,没人查得到。
   *
   * ⚠️ 注意 8000×20 在默认 2048 下**还轮不到兜底**(20×0.256 = 5.12 → 5)。
   *    真正让 `Math.max(1, …)` 生效要更极端的比例,下面两条各造了一个。
   */
  it("🔴 极端长宽比的短边一律 >= 1,绝不能是 0", () => {
    expect(fitWithin(8000, 20)).toEqual({ width: 2048, height: 5 });
    expect(fitWithin(20, 8000)).toEqual({ width: 5, height: 2048 }); // 竖过来同理

    // 这两条才真的踩到 Math.max(1, …):不兜底的话 round() 会给出 0
    expect(fitWithin(8000, 1)).toEqual({ width: 2048, height: 1 });
    expect(fitWithin(8000, 20, 100)).toEqual({ width: 100, height: 1 });
  });

  it("宽或高为 0 / 负数 / NaN → 返回 {0,0},让调用方放弃", () => {
    // compressImageFile 拿到 {0,0} 会直接 return 原件,不去碰 canvas
    expect(fitWithin(0, 100)).toEqual({ width: 0, height: 0 });
    expect(fitWithin(100, 0)).toEqual({ width: 0, height: 0 });
    expect(fitWithin(-4000, 3000)).toEqual({ width: 0, height: 0 });
    expect(fitWithin(4000, -3000)).toEqual({ width: 0, height: 0 });
    expect(fitWithin(Number.NaN, 100)).toEqual({ width: 0, height: 0 });
  });

  it("结果一律是整数 —— canvas 的宽高吃小数会被截断,尺寸就对不上了", () => {
    for (const [w, h] of [
      [4001, 2999],
      [3777, 1919],
      [100.6, 50.4], // 没超上限那条路也要 round,不能原样漏出去
      [2731.5, 4096.5],
    ]) {
      const got = fitWithin(w, h);
      expect(Number.isInteger(got.width), `${w}×${h} 的宽不是整数`).toBe(true);
      expect(Number.isInteger(got.height), `${w}×${h} 的高不是整数`).toBe(true);
    }
    expect(fitWithin(100.6, 50.4)).toEqual({ width: 101, height: 50 });
  });

  it("超限时长边恰好落在上限上,比例保持住", () => {
    const got = fitWithin(4001, 2999);
    expect(Math.max(got.width, got.height)).toBe(MAX_EDGE);
    // 比例误差只该来自 round 的半个像素
    expect(Math.abs(got.width / got.height - 4001 / 2999)).toBeLessThan(0.01);
  });

  it("传显式 maxEdge 也生效(默认参数没写死)", () => {
    expect(fitWithin(4000, 2000, 1000)).toEqual({ width: 1000, height: 500 });
    // 上限放大到 8000,4000 长边没超 → 仍然不放大
    expect(fitWithin(4000, 2000, 8000)).toEqual({ width: 4000, height: 2000 });
  });
});

// ---------------------------------------------------------------------------
// shouldKeepCompressed —— 决策树最后一条,最容易被顺手「简化」掉
// ---------------------------------------------------------------------------

describe("shouldKeepCompressed 压出来的值不值得用", () => {
  /**
   * 已经压过的图重编码常常**更大**。删了这条之后的表现是「压了个寂寞,还掉了画质」,
   * 没有任何报错 —— 上传照走,只是又白白有损了一代。
   */
  it("🔴 压完反而更大 → 不要", () => {
    expect(shouldKeepCompressed(1000, 1200)).toBe(false);
    expect(shouldKeepCompressed(1000, 1000)).toBe(false);
  });

  it("只小 5%(在 MIN_GAIN_RATIO 之内)→ 不要,不值得为这点体积掉一档画质", () => {
    expect(shouldKeepCompressed(1000, 950)).toBe(false);
  });

  it("边界:恰好等于 原图 × MIN_GAIN_RATIO → 不要(判据是 <),再小一个字节才要", () => {
    expect(shouldKeepCompressed(1000, 1000 * MIN_GAIN_RATIO)).toBe(false);
    expect(shouldKeepCompressed(1000, 1000 * MIN_GAIN_RATIO - 1)).toBe(true);
  });

  it("小一半 → 要", () => {
    expect(shouldKeepCompressed(1000, 500)).toBe(true);
    // 头注里量到的那档真实数据:3.11MB 的原片压成约 700KB
    expect(shouldKeepCompressed(3.11 * 1024 * 1024, 700 * 1024)).toBe(true);
  });

  it("压缩结果 0 字节 / 负数 → 不要(toBlob 出了怪东西,别拿它去上传)", () => {
    expect(shouldKeepCompressed(1000, 0)).toBe(false);
    expect(shouldKeepCompressed(1000, -5)).toBe(false);
    expect(shouldKeepCompressed(1000, Number.NaN)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// compressImageFile —— 只测早退分支。真正碰 canvas 的那段没有测试(见文件头注)。
// ---------------------------------------------------------------------------

describe("compressImageFile 早退:压不动就原样返回", () => {
  /**
   * 前提自检,不是废话:下面几条断的是「早退时还回同一个对象」,而**走完整条
   * canvas 路失败之后也会还回同一个对象**(头注:失败一律回退原图)。
   * 也就是说环境一旦变成 jsdom,这几条会因为**另一个原因**照绿 —— 变成空转。
   * vitest.config.ts 的默认环境是 node,这条钉着它别被顺手改掉。
   */
  it("前提:测试环境里没有 document(默认环境是 node,别改成 jsdom)", () => {
    expect(typeof document).toBe("undefined");
  });

  it("🔴 非浏览器环境(SSR / Node)原样返回**同一个 File 对象**", async () => {
    const file = makeFile(DECODE_FLOOR_BYTES * 4);
    expect(shouldTryCompress(file), "这张图本该进得了分流,否则这条在测别的东西").toBe(true);

    await expect(compressImageFile(file)).resolves.toBe(file);
  });

  it("非 JPEG 原样返回同一个 File 对象(连解码都不做)", async () => {
    const png = makeFile(DECODE_FLOOR_BYTES * 4, "image/png", "截图.png");
    await expect(compressImageFile(png)).resolves.toBe(png);
  });

  it("小于性能闸的 JPEG 原样返回 —— 不为一张小图白解一次", async () => {
    const small = makeFile(DECODE_FLOOR_BYTES, COMPRESSIBLE_TYPE, "小图.jpg");
    await expect(compressImageFile(small)).resolves.toBe(small);
  });
});
