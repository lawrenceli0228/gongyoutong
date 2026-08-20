/**
 * image-compress.ts(scripts/frontend-overrides/)的单测。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run image-compress.test.ts
 *
 * 钉的全是**静默出错**的东西 —— 这条链上没有任何一处会报错,坏了只表现为
 * 「照片好像糊了一点」或者「怎么还是那么慢」:
 *   · `UPLOAD_BUDGET_BYTES` 被「对齐」到服务端的 4MB → 客户端放过、服务端也放过,
 *     2.4MB 的图原样上网(2026-08-19 上线当天真图打脸的那条,见被测文件头注第一节);
 *   · 反过来把预算调到比 `TARGET_BYTES` 还大 → 超集关系断掉 → 有一段图客户端压一次、
 *     服务端再压一次,**两代有损叠加**,而「有没有戴安全帽」这种判断经不起反复有损压缩;
 *   · `DECODE_FLOOR_BYTES` 涨到预算线之上 → 预算线里有一段永远够不着,那批图永远没提速;
 *   · PNG 的 `MIN_GAIN_RATIO_PNG` 被「统一」到 0.9 → 界面截图 / 图纸截图也被转成 JPEG,
 *     **糊掉的是标注数字**,工友照着读会读错尺寸;
 *   · 压缩类型被「补全」上 GIF / WEBP → animated WEBP 经 canvas 拍平成 1 帧,
 *     服务端 `frames > 1` 那道闸**再也不会触发**,工友以为系统看了整段,其实只看了第一帧;
 *   · `isAnimatedPng` 认不出 APNG → 同一条坏法,只是入口换成了 PNG;
 *   · `shouldKeepCompressed` 被当成多余逻辑删掉 → 「压了个寂寞,还掉了画质」;
 *   · `fitWithin` 让短边落到 0 → canvas 抛错 → 整条压缩退回原图,而它本来就是
 *     「失败就退原图」的设计,于是**连一句报错都不会有**,只是永远没提速。
 *
 * ⚠️ 覆盖边界(与被测文件头注最后一节一致):canvas 的真实解码/编码路径
 *    —— EXIF 转正、铺白底、编码质量 —— **这里一条都没测**。jsdom 不实现 toBlob,
 *    为它引 node-canvas 是给测试装一个原生依赖,得不偿失。那几件只有真机能验。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  COMPRESSIBLE_TYPES,
  DECODE_FLOOR_BYTES,
  JPEG_QUALITY,
  MAX_EDGE,
  MIN_GAIN_RATIO_JPEG,
  MIN_GAIN_RATIO_PNG,
  TARGET_BYTES,
  UPLOAD_BUDGET_BYTES,
  compressImageFile,
  fitWithin,
  isAnimatedPng,
  minGainRatio,
  needsWork,
  shouldKeepCompressed,
  shouldTryCompress,
} from "@/lib/image-compress";

/** 两个受支持的输入格式。写成常量是为了让「按输入格式分档」那几条一眼看出在测哪一档。 */
const JPEG = "image/jpeg";
const PNG = "image/png";

/**
 * 造一个只用来喂判据的 File。
 *
 * Node 20+ 自带全局 `File`(本机与 CI 都远高于这条线),所以正常走**真 File** ——
 * 只有真 File 才能证明 `compressImageFile` 早退时还回来的是**同一个对象**
 * 而不是某个看着一样的替身(下面用 `toBe` 断的就是引用相等)。
 *
 * 万一运行时没有全局 File(某些精简发行版会把 undici 那套裁掉),退到一个只带
 * `{name,type,size,lastModified}` 四个字段的最小替身 + `as unknown as File`:
 * 被测的纯函数只看 type/size,`compressImageFile` 的两条早退分支也只看这两个,
 * 再多造也是白造。
 */
function makeFile(bytes: number, type: string = JPEG, name = "工地照片.jpg"): File {
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
// 常量:几个数是服务端配置的**镜像**,单独改一个没有任何报错
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
   *
   * ⚠️ `TARGET_BYTES` 从 2026-08-19 起**不再参与 `needsWork`**(客户端改用自己的
   *    `UPLOAD_BUDGET_BYTES`,见下一组)。它仍然必须与服务端对齐 —— 因为下一组那条
   *    超集断言就是拿它当尺子量的,它漂了,那条守卫量的就是错的东西。
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
   * ===========================================================================
   * ⚠️ 这条守卫的含义在 2026-08-19 变过一次,**别再当成「逐字镜像」来读**
   * ---------------------------------------------------------------------------
   * 第一版 `needsWork` 是对这个 if 的前两项**逐字取反**写的,那时候两边的体积线
   * 是同一个数(4MB)。现在不是了:
   *
   *     长边那一项   仍然镜像 —— 客户端也用 MAX_EDGE = max_edge = 2048
   *     体积那一项   **不再镜像** —— 客户端用自己的 UPLOAD_BUDGET_BYTES(600KB)
   *
   * 理由在被测文件头注第一节:服务端那条 4MB 是为省**视觉 token**定的,
   * 客户端那条 600KB 是为省**工友的等待**定的,两个目标不是一回事。
   * 硬把两条对齐,就是 2026-08-19 那张 1448×1086 / 2.4MB 的图两边都不管的那个 bug。
   *
   * 那这条守卫现在还守什么?两件:
   *   ① **长边那一项**的形状 —— 后端把 `<=` 改成 `<`(或 and 改成 or),
   *      客户端在 2048 那个边界上就整体错开一格,表现是恰好卡在 2048 的图两边各缩一次;
   *   ② **服务端的行为没变** —— 客户端敢在 600KB~4MB 这段自己动手、且认为
   *      「动完服务端一定放过」,前提就是服务端这个透传 if 还在、还是这个形状。
   *
   * ===========================================================================
   * 第三项 `and upright` 客户端**故意不镜像**(TODO-53 修复引入,2026-08-19)
   * ---------------------------------------------------------------------------
   * 服务端多了一条「EXIF 方向不正的也要走重编码」。客户端不跟,理由是两边
   * 各自都能保证「模型看到的是正立的」,而跟了要付真代价:
   *
   *   · 客户端解码时就用 imageOrientation: "from-image",**凡是它重编码过的
   *     都已经转正**,而且产出不带方向标记 —— 服务端拿到就是 upright。
   *   · 客户端原样放过的(小图 / 动图 / 压完不划算的),EXIF 还在原文件里,
   *     服务端那条新判据接得住。
   *
   * 要镜像的话,客户端得在**解码前**知道方向 —— 而 createImageBitmap 不给,
   * 只能自己解 JPEG 的 APP1 段。为一个服务端已经兜住的场景手写 EXIF 解析器,
   * 是拿一个新的、没测试的字节解析去换一次服务端重编码,不划算。
   *
   * ⚠️ 所以这条断言只钉**前两项的形状**。第三项单独钉在下一条里 —— 拆开是为了
   *    「后端改了边界」和「后端改了方向策略」两种漂移能分别报出来,
   *    合成一条正则的话,报错只会说「形状变了」,人还得自己去 diff。
   */
  it("🔴 服务端那个「原样透传」的 if 还是原来的形状(长边那一项客户端仍然镜像)", () => {
    expect(
      readBackend("agents/safety/tools.py"),
      "_prepare_image 的透传判据变了 —— needsWork 的长边那一项照它写的,要一起改;" +
        "体积那一项客户端**故意不跟**(用自己的 UPLOAD_BUDGET_BYTES),别顺手改回去",
    ).toMatch(/if max\(width, height\) <= max_edge and len\(payload\) <= limit_bytes and upright:/);
  });

  /**
   * 上一条的第三项单独钉在这儿。守的是**客户端那条「不镜像」的前提还成不成立**:
   * 客户端敢放过一张方向不正的小图,唯一的依据就是「服务端会转正它」。
   *
   * 哪天有人把 `upright` 从判据里拿掉(比如觉得多一次重编码不值),
   * 那条前提当场失效,而表现是**静默的** —— 方向不正的小图既没被客户端转、
   * 也没被服务端转,原样送进模型,躺倒。界面上还是正的(浏览器按 EXIF 渲染),
   * 一句报错都没有,正是 TODO-53 那个 bug 复活。
   */
  it("🔴 服务端仍然把「方向不正」算进要动手的条件(客户端不镜像它的前提)", () => {
    const src = readBackend("agents/safety/tools.py");
    expect(src, "_UPRIGHT_ORIENTATIONS 没了 —— 方向策略变了,回去读 image-compress.ts 头注").toMatch(
      /_UPRIGHT_ORIENTATIONS/,
    );
    expect(src, "upright 不再由 EXIF 方向算出来了,客户端「不镜像」的前提可能已失效").toMatch(
      /upright = orientation in _UPRIGHT_ORIENTATIONS/,
    );
    expect(src, "重编码分支里的 exif_transpose 没了 —— 方向不正的图会原样躺着喂给模型").toMatch(
      /ImageOps\.exif_transpose\(image\)/,
    );
  });

  /**
   * 🔴 **GIF / WEBP 不在表里是刻意的,不是漏了。**
   *
   * `_prepare_image` 有 `if frames > 1: raise ImageRejected(FILE_UNSUPPORTED, …)`,
   * 而 canvas 只画得出第一帧 —— animated WEBP 一旦经过客户端就变成 frames == 1,
   * 服务端那道闸**再也不会触发**,工友以为系统看了整段,其实只看了第一帧,一声不吭。
   *
   * PNG 收进来是有代价的(APNG 也是动图),代价由 `isAnimatedPng` 付,单独一组钉着。
   * WEBP 的动画标志藏在 VP8X 块里、分支比 APNG 多,而工地上 WEBP 极少见 ——
   * 不收 = 行为与改动前完全一致,零回归。
   */
  it("🔴 COMPRESSIBLE_TYPES 恰好是 JPEG + PNG 两项 —— 别「补全」GIF / WEBP", () => {
    expect([...COMPRESSIBLE_TYPES]).toEqual([JPEG, PNG]);
    for (const t of ["image/gif", "image/webp", "image/avif", "image/heic"]) {
      expect(COMPRESSIBLE_TYPES.includes(t), `${t} 不该被收进来`).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// 🔴 本文件最值钱的一组:两条判据的大小关系。全组都是常量比较,没有一行业务逻辑,
//    但它守的是整条链的**结构前提** —— 关系一旦反过来,所有单点用例还是绿的。
// ---------------------------------------------------------------------------

describe("🔴 两条判据的超集关系", () => {
  /**
   * 🔴 **这条是全文件的地基。**
   *
   * 客户端判据 = 长边 > 2048 || 体积 > 600KB
   * 服务端判据 = 长边 > 2048 || 体积 > 4MB(|| 方向不正)
   *
   * 只要 `UPLOAD_BUDGET_BYTES < TARGET_BYTES`,客户端就是服务端的**超集**:
   *
   *     凡是服务端会动手的,客户端一定先动手了  → 服务端拿到的已经是缩过/压过的,
   *                                              它自己那两条不再成立,直接透传;
   *     客户端放过的,服务端也一定放过          → 没人碰,原图上模型。
   *
   * 两种情况都只有**一代有损**。
   *
   * 反过来(预算 >= 4MB)会开出一段两边都要动手的区间:客户端压一次(q85)、
   * 服务端再压一次(q85),**两代有损叠加**。tools.py 的注释原话是
   * 「重编码只会白白损失画质,而判断有没有戴安全帽经不起反复有损压缩」。
   * 而这件事**没有任何报错**,只表现为识图准确率慢慢往下掉。
   */
  it("🔴 UPLOAD_BUDGET_BYTES < TARGET_BYTES —— 客户端必须是服务端的超集", () => {
    expect(
      UPLOAD_BUDGET_BYTES,
      "客户端预算涨到服务端那条线之上了 —— 中间那段图会被客户端和服务端各压一次,两代有损叠加",
    ).toBeLessThan(TARGET_BYTES);
  });

  /**
   * 🔴 `DECODE_FLOOR_BYTES` 是**纯性能闸**(别为一张 100KB 的小图白解一次码),
   * 不是策略闸。它必须落在预算线**下面**,否则中间那段
   * `[UPLOAD_BUDGET_BYTES, DECODE_FLOOR_BYTES]` 就是一段**永远够不着的预算**:
   * 那批图明明超了预算该压,却在解码前就被性能闸挡掉了。
   *
   * 表现同样是静默的 —— 没有报错,只是那一档大小的图永远没提速,
   * 而查的人会去看 `needsWork`(它写得好好的),想不到是上一道闸把它们拦在门外。
   */
  it("🔴 DECODE_FLOOR_BYTES < UPLOAD_BUDGET_BYTES —— 否则预算线里有一段永远够不着", () => {
    expect(
      DECODE_FLOOR_BYTES,
      "解码闸抬到预算线之上了 —— 这两条线之间的图该压却连解码都轮不到,且零报错",
    ).toBeLessThan(UPLOAD_BUDGET_BYTES);
  });

  it("三条线的实际取值(改任何一个都要回去重读被测文件头注)", () => {
    expect(DECODE_FLOOR_BYTES).toBe(256 * 1024);
    expect(UPLOAD_BUDGET_BYTES).toBe(600 * 1024);
    expect(TARGET_BYTES).toBe(4 * 1024 * 1024);
  });
});

// ---------------------------------------------------------------------------
// shouldTryCompress —— 解码前的快速分流。只看类型和大小,不解码。
// ---------------------------------------------------------------------------

describe("shouldTryCompress 解码前分流", () => {
  const BIG = DECODE_FLOOR_BYTES * 4; // 远超性能闸,免得把两条判据混在一起看

  /**
   * 🔴 **PNG 从 2026-08-19 起收了,这条以前是 false。**
   *
   * 第一版收窄到 JPEG,理由写的是「工地照片 = 手机拍 = JPEG」。**那个前提是错的** ——
   * 真实到手的图有相当一部分是 PNG(转发、截取、某些 App 导出),而 PNG 恰恰是
   * 收益最大的一档:照片存成 PNG 转 JPEG q85 实测 6.9~8.0 倍。
   *
   * 收 PNG 的代价是 APNG(PNG 也可能是动图),由 `isAnimatedPng` 付,单独一组钉着。
   */
  it("🔴 够大的 PNG 现在也进得去(以前是 false)", () => {
    expect(shouldTryCompress({ type: PNG, size: BIG })).toBe(true);
  });

  it("够大的 JPEG 进得去", () => {
    expect(shouldTryCompress({ type: JPEG, size: BIG })).toBe(true);
  });

  /**
   * 🔴 **本组最重要的一条,而且它在「PNG 收了」之后更容易被顺手放宽。**
   *
   * `_prepare_image` 有 `if frames > 1: raise ImageRejected(...)`,而 canvas 只画得出
   * 第一帧 —— animated WEBP / GIF 一旦经过客户端就变成 frames == 1,
   * **那道闸再也不会触发**,工友以为系统看了整段,其实只看了第一帧,而且一句话都不说。
   *
   * 「PNG 都收了,GIF/WEBP 为什么不收」的回答不是「懒」,是**代价不对称**:
   * APNG 的动画标志(`acTL`)是一个块类型,顺着块流扫几十字节就认得出;
   * WEBP 的藏在 VP8X 里、分支多得多,而工地上 WEBP 极少见 —— 不收 = 零回归。
   *
   * 顺带一条也靠它:DXF 图纸走的是另一条上传路(MIME 常是 octet-stream),
   * 压根不该经过这里。
   */
  it("🔴 GIF / WEBP 仍然一律不碰 —— 放宽会让服务端的动图闸永远不触发", () => {
    const rejected = [
      "image/webp", // 可能是 animated WEBP,canvas 会拍平成 1 帧
      "image/gif", // 同上
      "image/avif", // 同上,而且解码支持面更窄
      "application/pdf",
      "application/octet-stream", // DXF 图纸的浏览器 MIME 就常是这个
      "", // 认不出类型时浏览器给空串
    ];
    for (const type of rejected) {
      expect(shouldTryCompress({ type, size: BIG }), `${type || "(空串)"} 不该被压`).toBe(false);
    }
  });

  it("类型判定大小写不敏感 —— 有些来源给的是大写", () => {
    expect(shouldTryCompress({ type: "IMAGE/JPEG", size: BIG })).toBe(true);
    expect(shouldTryCompress({ type: "Image/Jpeg", size: BIG })).toBe(true);
    expect(shouldTryCompress({ type: "IMAGE/PNG", size: BIG })).toBe(true);
    expect(shouldTryCompress({ type: "Image/Png", size: BIG })).toBe(true);
    // 大小写不敏感只该放宽大小写,不该顺手放宽类型
    expect(shouldTryCompress({ type: "IMAGE/WEBP", size: BIG })).toBe(false);
  });

  /**
   * 这道闸是**纯性能闸,不是策略闸** —— 只为了别给一张 100KB 的图白解码一次
   * (解一张 4000×3000 要 48MB 位图,低端安卓上是真实卡顿)。判据是 `>` 不是 `>=`,
   * 恰好等于就不进。
   */
  it("恰好等于 DECODE_FLOOR_BYTES 不进(判据是 >),多一个字节才进", () => {
    expect(shouldTryCompress({ type: JPEG, size: DECODE_FLOOR_BYTES })).toBe(false);
    expect(shouldTryCompress({ type: JPEG, size: DECODE_FLOOR_BYTES + 1 })).toBe(true);
    expect(shouldTryCompress({ type: PNG, size: DECODE_FLOOR_BYTES })).toBe(false);
    expect(shouldTryCompress({ type: JPEG, size: 0 })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// needsWork —— 两条判据取并集(长边镜像服务端 + 体积用客户端自己的预算)
// ---------------------------------------------------------------------------

describe("needsWork 两条判据取并集", () => {
  /**
   * 🔴 **这条是 2026-08-19 上线当天被一张真图证伪的那一条,本组第一等重要。**
   *
   * 真图:1448×1086 的 PNG,2,485,074 字节。按**旧判据**(照服务端那两条线写)——
   *
   *     长边 1448 <= 2048   → 服务端不缩 → 客户端也不缩
   *     体积 2.4MB <= 4MB   → 服务端不重编码 → 客户端也不重编码
   *
   * 结果客户端不碰、服务端也不碰,2.4MB 原样上网,工地 4G 上就是十几秒干等。
   * 而它转成 JPEG q85 只有 312,115 字节 —— **8 倍,一个像素都不用缩**。
   *
   * 下面先把「旧判据确实会放过它」这件事断出来(否则这条用例就是在测别的东西),
   * 再断新判据必须 true。这两半合起来才是完整的回归钉。
   */
  it("🔴 1448×1086 / 2,485,074 字节:旧判据放过它,新判据必须动手", () => {
    const WIDTH = 1448;
    const HEIGHT = 1086;
    const BYTES = 2_485_074;

    // 前提自检:这张图对**服务端**那两条线确实都不成立 —— 也就是旧判据会返回 false。
    // 这两句一旦不成立,下面那句 true 可能是靠长边混过去的,用例就空转了。
    expect(Math.max(WIDTH, HEIGHT), "长边超了 2048 的话,这条用例测的就不是体积那一项了").toBeLessThanOrEqual(
      MAX_EDGE,
    );
    expect(BYTES, "体积超了服务端 4MB 的话,旧判据本来就会 true,这条用例就没意义了").toBeLessThanOrEqual(
      TARGET_BYTES,
    );

    expect(
      needsWork(WIDTH, HEIGHT, BYTES),
      "客户端又不管这张图了 —— 服务端也不管,2.4MB 原样上网。" +
        "多半是有人把 needsWork 的体积项改回 TARGET_BYTES 了",
    ).toBe(true);
  });

  it("🔴 小图小体积仍然一个字节都不动 —— 重编码只会白亏一档画质", () => {
    // 1000×800 / 300KB:长边没超 2048,体积没超 600KB。
    // 判成「要压」的后果不是慢,是纯亏画质,而且一点速度都不省(体积本来就不大)。
    expect(needsWork(1000, 800, 300 * 1024)).toBe(false);
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

  it("尺寸没超但体积超预算也要压 —— 传太久,值得重编码一次", () => {
    expect(needsWork(1600, 1200, UPLOAD_BUDGET_BYTES + 1)).toBe(true);
  });

  /**
   * 判据是 `>`,恰好等于预算不动手。
   *
   * ⚠️ 这条**挡不住**「体积项被改回 TARGET_BYTES」那种退化(614400 也 <= 4MB,
   *    改回去它照样绿)。真正挡那件事的是本组第一条和上一条,别把这条当成替身。
   */
  it("体积恰好等于 UPLOAD_BUDGET_BYTES 不用压(判据同样是 >)", () => {
    expect(needsWork(1600, 1200, UPLOAD_BUDGET_BYTES)).toBe(false);
  });

  it("两条都超当然要压", () => {
    expect(needsWork(4000, 3000, 6 * 1024 * 1024)).toBe(true);
  });

  /**
   * 超集关系在函数层面再钉一遍:凡是服务端会动手的体积,客户端一定先动手。
   * 上面那组断的是两个常量的大小关系,这条断的是**它真的传导到了判据里** ——
   * 有人把 `needsWork` 改成 `bytes > TARGET_BYTES` 的话,常量关系还是对的,
   * 只有这条会红。
   */
  it("🔴 服务端会动手的体积,客户端一定先动手(超集关系传导到了判据里)", () => {
    for (const bytes of [TARGET_BYTES + 1, TARGET_BYTES * 2, 10 * 1024 * 1024]) {
      expect(needsWork(800, 600, bytes), `${bytes} 字节服务端会重编码,客户端却放过了`).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// minGainRatio —— 采用门槛按**输入格式**分档
// ---------------------------------------------------------------------------

describe("minGainRatio 按输入格式分档", () => {
  it("PNG 走严档、JPEG 走松档", () => {
    expect(minGainRatio(PNG)).toBe(MIN_GAIN_RATIO_PNG);
    expect(minGainRatio(JPEG)).toBe(MIN_GAIN_RATIO_JPEG);
  });

  it("大小写不敏感 —— 有些来源给的是大写", () => {
    expect(minGainRatio("IMAGE/PNG")).toBe(MIN_GAIN_RATIO_PNG);
    expect(minGainRatio("Image/Png")).toBe(MIN_GAIN_RATIO_PNG);
    expect(minGainRatio("IMAGE/JPEG")).toBe(MIN_GAIN_RATIO_JPEG);
  });

  /**
   * 认不出的类型走 JPEG 那档(松档)。这是安全的方向:松档只影响「要不要采用压缩件」,
   * 而能走到这一步的类型早被 `shouldTryCompress` 筛过了,不可能真是个 WEBP。
   */
  it("未知 / 空类型走 JPEG 那档,不抛", () => {
    for (const t of ["image/webp", "application/octet-stream", ""]) {
      expect(minGainRatio(t)).toBe(MIN_GAIN_RATIO_JPEG);
    }
  });

  /**
   * 🔴 **这条钉的是「别把两档统一了」。**
   *
   * PNG 换 JPEG 是**换格式**,不可逆地丢掉线条锐度,所以门槛必须严得多。
   * 判据不是猜的,是 2026-08-19 同一台机上量出来的一道**很宽的沟**:
   *
   *     照片存成 PNG   1.58 ~ 1.98 字节/像素   → JPEG q85 收益 6.9 ~ 8.0 倍(压到 0.14)
   *     界面 / 图纸截图 0.13 字节/像素          → JPEG q85 收益 1.4 倍  (压到 0.71)
   *
   * 0.5 落在 0.14 和 0.71 中间那道宽沟里,不是卡在边界上的判据。
   *
   * ⚠️ 把 PNG 这档调到跟 JPEG 一样的 0.9,等于**把截图也转了** ——
   *    而截图糊掉的是**标注数字**,工友照着读会读错尺寸。
   *    没有报错,只有「这张图上的数字怎么看不清」。
   */
  it("🔴 MIN_GAIN_RATIO_PNG < MIN_GAIN_RATIO_JPEG —— 两档不许统一", () => {
    expect(
      MIN_GAIN_RATIO_PNG,
      "PNG 的门槛被放宽到 JPEG 那档了 —— 界面截图 / 图纸截图会被转成 JPEG,标注数字糊掉",
    ).toBeLessThan(MIN_GAIN_RATIO_JPEG);
  });

  it("两档的实际取值(改了要回去重读那道「宽沟」的实测数)", () => {
    expect(MIN_GAIN_RATIO_PNG).toBe(0.5);
    expect(MIN_GAIN_RATIO_JPEG).toBe(0.9);
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
   * 🔴 **PNG 那两条是本组的核心,它们分开的是「照片」和「截图」。**
   *
   * 门槛按**输入格式**取(不是输出格式 —— 输出恒为 JPEG,按它取就没有分档可言了)。
   * 两条用的都是被测文件头注里那两个实测比值,不是随手编的数。
   */
  it("🔴 PNG 压到 0.14(它其实是张照片)→ 换成 JPEG", () => {
    // 头注里那张真图:2,485,074 → 312,115,比值 0.126
    expect(shouldKeepCompressed(2_485_074, 312_115, PNG)).toBe(true);
  });

  it("🔴 PNG 只压到 0.71(它是张截图)→ 保持原 PNG,别把标注数字糊掉", () => {
    expect(shouldKeepCompressed(1000, 710, PNG)).toBe(false);
  });

  it("同一个 0.71,JPEG 输入就要 —— 同格式重编码画质代价小,门槛松", () => {
    expect(shouldKeepCompressed(1000, 710, JPEG)).toBe(true);
  });

  it("不传第三参 → 走 JPEG 那档(默认值,调用方漏传时是安全的那一侧)", () => {
    expect(shouldKeepCompressed(1000, 710)).toBe(true);
    expect(shouldKeepCompressed(1000, 950)).toBe(false); // 只小 5%,不值得掉一档画质
  });

  it("边界:恰好等于 原图 × 门槛 → 不要(判据是 <),再小一个字节才要", () => {
    expect(shouldKeepCompressed(1000, 1000 * MIN_GAIN_RATIO_JPEG, JPEG)).toBe(false);
    expect(shouldKeepCompressed(1000, 1000 * MIN_GAIN_RATIO_JPEG - 1, JPEG)).toBe(true);
    expect(shouldKeepCompressed(1000, 1000 * MIN_GAIN_RATIO_PNG, PNG)).toBe(false);
    expect(shouldKeepCompressed(1000, 1000 * MIN_GAIN_RATIO_PNG - 1, PNG)).toBe(true);
  });

  /**
   * 已经压过的图重编码常常**更大**。删了这条之后的表现是「压了个寂寞,还掉了画质」,
   * 没有任何报错 —— 上传照走,只是又白白有损了一代。
   */
  it("🔴 压完反而更大 / 一样大 → 不要", () => {
    expect(shouldKeepCompressed(1000, 1200)).toBe(false);
    expect(shouldKeepCompressed(1000, 1000)).toBe(false);
    expect(shouldKeepCompressed(1000, 1200, PNG)).toBe(false);
  });

  it("压缩结果 0 字节 / 负数 / NaN → 不要(toBlob 出了怪东西,别拿它去上传)", () => {
    for (const type of [JPEG, PNG]) {
      expect(shouldKeepCompressed(1000, 0, type)).toBe(false);
      expect(shouldKeepCompressed(1000, -5, type)).toBe(false);
      expect(shouldKeepCompressed(1000, Number.NaN, type)).toBe(false);
    }
    // 不传第三参那条路也要走一遍
    expect(shouldKeepCompressed(1000, 0)).toBe(false);
  });

  it("小一半 → 要(两档都过)", () => {
    expect(shouldKeepCompressed(1000, 400, JPEG)).toBe(true);
    expect(shouldKeepCompressed(1000, 400, PNG)).toBe(true);
    // 头注里量到的那档真实数据:3.11MB 的原片压成约 700KB
    expect(shouldKeepCompressed(3.11 * 1024 * 1024, 700 * 1024)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// isAnimatedPng —— 收 PNG 的代价就是这一件。自己造字节,不引任何图片依赖。
// ---------------------------------------------------------------------------

/**
 * PNG 的块布局(被测文件头注里那张图):
 *
 *     [8 字节签名][长度(4,大端)][类型(4,ASCII)][数据(长度)][CRC(4)] …
 */
const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/**
 * 拼一个 PNG 块。
 *
 * @param type        四个 ASCII 字母的块类型(IHDR / acTL / IDAT / …)
 * @param dataLength  真正写进去多少字节的数据(内容全 0,解析器不看内容)
 * @param declared    写进长度字段的数,默认等于 dataLength。
 *                    传一个和实际对不上的数,就能造出「长度是垃圾值」那类畸形块 ——
 *                    下面钉 `>>> 0` 和 `at <= 0` 兜底的那两条就靠它。
 */
function chunk(type: string, dataLength = 0, declared = dataLength): number[] {
  return [
    (declared >>> 24) & 0xff,
    (declared >>> 16) & 0xff,
    (declared >>> 8) & 0xff,
    declared & 0xff,
    ...[...type].map((c) => c.charCodeAt(0)),
    ...new Array<number>(dataLength).fill(0), // 数据段
    0, 0, 0, 0, // CRC:解析器不校验,填 0 就行
  ];
}

/** 签名 + 若干块,拼成一份能喂给 isAnimatedPng 的字节流。 */
function png(...chunks: number[][]): Uint8Array {
  return new Uint8Array([...PNG_SIG, ...chunks.flat()]);
}

describe("isAnimatedPng 认出 APNG", () => {
  /**
   * 🔴 **这一组守的是「PNG 收进 COMPRESSIBLE_TYPES」的代价。**
   *
   * 认不出 APNG 的后果:canvas 把它拍平成第一帧 → 服务端 `frames > 1` 那道闸
   * 再也不触发 → 工友以为系统看了整段,其实只看了第一帧,**一句报错都没有**。
   * 这正是当初收窄到 JPEG 想避开的那件事,只是入口从 WEBP 换成了 PNG。
   */
  it("🔴 acTL 出现在 IDAT 之前 → 是动图", () => {
    // IHDR 固定 13 字节数据,acTL 固定 8 字节(帧数 + 播放次数)—— 照 APNG 规范造
    expect(isAnimatedPng(png(chunk("IHDR", 13), chunk("acTL", 8), chunk("IDAT", 4)))).toBe(true);
  });

  it("只有 IHDR + IDAT → 普通静态 PNG", () => {
    expect(isAnimatedPng(png(chunk("IHDR", 13), chunk("IDAT", 4)))).toBe(false);
  });

  /**
   * APNG 规范要求 `acTL` **必须**在第一个 `IDAT` 之前。所以撞上 IDAT 就可以收工 ——
   * 后面再出现 acTL 的文件不是合法 APNG,不该为它把整份文件读完
   * (一张 20MB 的 PNG 全读进内存,低端手机上是真实代价)。
   */
  it("IDAT 之后才出现 acTL → 不是合法 APNG,提前收工", () => {
    expect(isAnimatedPng(png(chunk("IHDR", 13), chunk("IDAT", 4), chunk("acTL", 8)))).toBe(false);
  });

  it("acTL 夹在别的辅助块中间也认得出(不是只看第一个块)", () => {
    expect(
      isAnimatedPng(
        png(chunk("IHDR", 13), chunk("gAMA", 4), chunk("pHYs", 9), chunk("acTL", 8), chunk("IDAT", 4)),
      ),
    ).toBe(true);
  });

  it("不是 PNG 签名(比如 JPEG)→ false,不归这里管", () => {
    // JPEG 的 SOI + APP0
    expect(isAnimatedPng(new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46]))).toBe(
      false,
    );
    // 签名只差最后一个字节 —— 逐字节比对,不是只看前两个
    expect(isAnimatedPng(new Uint8Array([...PNG_SIG.slice(0, 7), 0x00, ...chunk("acTL", 8)]))).toBe(
      false,
    );
  });

  /**
   * 空 / 太短 / 截断在块中间 —— 一律 false,而且**绝不许抛**。
   *
   * 抛了的后果:`isAnimatedPngFile` 那层虽有 try,但这个纯函数是
   * `compressImageFile` 之外唯一被单测覆盖的入口,抛出去就等于把一个
   * 「会炸的解析器」放进了上传主路径。
   */
  it("空数组 / 只有签名 / 截断在块头中间 → false,且不抛", () => {
    expect(() => isAnimatedPng(new Uint8Array())).not.toThrow();
    expect(isAnimatedPng(new Uint8Array())).toBe(false);
    expect(isAnimatedPng(new Uint8Array(PNG_SIG))).toBe(false); // 只有签名
    expect(isAnimatedPng(new Uint8Array(PNG_SIG.slice(0, 7)))).toBe(false); // 连签名都不全
    // 块头只读到 6 字节(不足 8),循环压根进不去
    expect(isAnimatedPng(new Uint8Array([...PNG_SIG, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48]))).toBe(false);
    // 长度字段说有 100 字节数据,实际一个都没有 —— 跳过去就越界了
    expect(isAnimatedPng(png(chunk("IHDR", 0, 100)))).toBe(false);
  });

  /**
   * 🔴 **这条钉的是 `>>> 0` 和 `at <= 0` 那两句兜底,拿掉它们会把浏览器卡死。**
   *
   * 长度字段是大端 32 位**无符号**数,而 JS 的位运算产出的是**有符号** 32 位:
   *
   *     0xFFFFFFF4 不收成无符号 → -12
   *     at += 8(块头) + (-12)(长度) + 4(CRC) = 0   → **at 原地不动,死循环**
   *
   * 死循环发生在**上传主路径**上(选文件 → fileToContentBlock → compressImageFile),
   * 表现是工友点了发送之后整个页面卡死,而且没有任何报错可查。
   *
   * 0xFFFFFFFF 那条是同一件事的另一半:收成无符号之后 at 会一步跳出文件,
   * 循环条件不满足、正常收工 —— 而不是拿一个负数继续往下读。
   *
   * ⚠️ 用 timeout 兜住:兜底被拿掉的话这条不是「红」,是**永远跑不完** ——
   *    没有超时的话它会把整个 vitest 进程挂在这儿,CI 上表现成「卡住了」而不是「测试失败」。
   */
  it(
    "🔴 块长度是垃圾值 → false,且绝不死循环",
    { timeout: 2000 },
    () => {
      // 0xFFFFFFF4:不收成无符号的话 at 增量恰好是 0 —— 最狠的那个死循环输入
      expect(isAnimatedPng(png(chunk("IHDR", 0, 0xfffffff4), chunk("acTL", 8)))).toBe(false);
      // 0xFFFFFFFF:收成无符号后一步跳出文件尾,正常收工
      expect(isAnimatedPng(png(chunk("IHDR", 0, 0xffffffff), chunk("acTL", 8)))).toBe(false);
      // 几个别的边界值,都不许挂
      for (const declared of [0x80000000, 0xfffffff0, 0xfffffffc, 0x7fffffff]) {
        expect(isAnimatedPng(png(chunk("IHDR", 0, declared), chunk("IDAT", 4)))).toBe(false);
      }
    },
  );

  /**
   * 垃圾长度**排在 acTL 后面**时仍然要认出动图 —— 先撞上 acTL 就该收工,
   * 不该被后面的畸形块带跑。这条防的是有人把「先扫一遍全部块再判断」当成优化。
   */
  it("acTL 在前、畸形块在后 → 仍然认出是动图", () => {
    expect(isAnimatedPng(png(chunk("acTL", 8), chunk("IHDR", 0, 0xfffffff4)))).toBe(true);
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

  /**
   * PNG 现在**进得了分流**了(shouldTryCompress 收它),所以它比 JPEG 多走一步:
   * 过了第一道闸,栽在 `typeof document === "undefined"` 上。
   * 这条钉的是那一步也老实还回同一个对象 —— 而不是在 node 下去摸 canvas 抛出去。
   */
  it("🔴 PNG 在 node 下也原样返回同一个对象(它已经过得了分流那一关)", async () => {
    const png = makeFile(DECODE_FLOOR_BYTES * 4, PNG, "截图.png");
    expect(shouldTryCompress(png), "PNG 该进得了分流,否则这条测的是上一道闸").toBe(true);

    await expect(compressImageFile(png)).resolves.toBe(png);
  });

  it("不在压缩类型表里的(GIF / WEBP)原样返回同一个对象(连解码都不做)", async () => {
    const gif = makeFile(DECODE_FLOOR_BYTES * 4, "image/gif", "动图.gif");
    const webp = makeFile(DECODE_FLOOR_BYTES * 4, "image/webp", "图.webp");
    await expect(compressImageFile(gif)).resolves.toBe(gif);
    await expect(compressImageFile(webp)).resolves.toBe(webp);
  });

  it("小于性能闸的图原样返回 —— 不为一张小图白解一次", async () => {
    const small = makeFile(DECODE_FLOOR_BYTES, JPEG, "小图.jpg");
    await expect(compressImageFile(small)).resolves.toBe(small);
    const smallPng = makeFile(DECODE_FLOOR_BYTES, PNG, "小截图.png");
    await expect(compressImageFile(smallPng)).resolves.toBe(smallPng);
  });
});
