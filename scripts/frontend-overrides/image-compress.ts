/**
 * 上传前把照片压小 —— 治「点了发送先干等十几秒」。
 *
 * ===========================================================================
 * 两条判据,目标不同,别合并(2026-08-19 真人试用打脸之后重写)
 * ---------------------------------------------------------------------------
 * 第一版这里只有一条判据:「服务端会不会动这张图」,逐字镜像
 * `agents/safety/tools.py:_prepare_image` 那个透传 `if`。想法是「客户端只做
 * 服务端本来就要做的事,一步不多」,听起来很干净。
 *
 * 🔴 **上线当天就被一张真图证伪了**:
 *
 *     1448×1086 的 PNG,2,485,074 字节
 *       · 长边 1448 <= 2048   → 服务端不缩
 *       · 体积 2.4MB <= 4MB   → 服务端不重编码
 *       · 格式是 PNG           → 第一版客户端压根不收
 *     结果:客户端不碰、服务端也不碰,2.4MB 原样上网。
 *     而它转成 JPEG q85 只有 312,115 字节 —— **8 倍,一个像素都不用缩**。
 *
 * 错在把两个不同的目标当成了一个:
 *
 *     服务端那两条线(2048px / 4MB)是为**省视觉 token**定的 ——
 *       2.4MB 的图对服务端毫无压力,它当然不动。
 *     客户端要省的是**用户的等待** ——
 *       2.4MB 在工地 4G 上就是十几秒的干等。
 *
 * 所以现在是两条判据,取并集:
 *
 *     needsWork = 长边 > MAX_EDGE(2048)        ← 服务端反正要缩,不如我先缩
 *               || 体积 > UPLOAD_BUDGET(600KB)  ← 传太久,值得重编码一次
 *
 * `UPLOAD_BUDGET_BYTES` 比服务端的 `TARGET_BYTES` 严格得多,所以这是个**超集**:
 * 凡是服务端会动手的,客户端一定先动手了;客户端放过的,服务端也一定放过。
 * (`image-compress.test.ts` 有一条把这个大小关系钉死。)
 *
 * ===========================================================================
 * PNG 也收了,但只在「它其实是张照片」的时候才转成 JPEG
 * ---------------------------------------------------------------------------
 * 第一版只收 JPEG,理由写的是「工地照片 = 手机拍 = JPEG」。**那个前提是错的** ——
 * 真实到手的图有相当一部分是 PNG(转发、截取、某些 App 导出)。
 *
 * 但 PNG 不能无脑转 JPEG:界面截图 / 图纸截图转 JPEG 会把细线和标注数字糊掉。
 * 判别靠的不是猜,是**实测出来的一道很宽的沟**(2026-08-19,同一台机上量的):
 *
 *     照片存成 PNG   1.58 ~ 1.98 字节/像素   →JPEG q85  收益 6.9 ~ 8.0 倍
 *     界面截图        0.13 字节/像素          →JPEG q85  收益 1.4 倍
 *
 * 差一个数量级。所以判据就是「压完够不够小」——
 * `MIN_GAIN_RATIO_PNG = 0.5`(必须小到一半以下才换格式):
 *
 *     照片   压到 0.14 → 远小于 0.5 → 换成 JPEG ✅
 *     截图   压到 0.71 → 大于 0.5   → **保持原 PNG**,线条不受损 ✅
 *
 * 两者之间空着 0.14~0.71 这么大一段,不是卡在边界上的判据。
 * ⚠️ 别把 PNG 的门槛调到跟 JPEG 一样(0.9)—— 那等于把截图也转了,
 *    而截图糊掉的是**标注数字**,工友照着读会读错尺寸。
 *
 * ===========================================================================
 * GIF / WEBP 仍然不收 —— 动图那道闸不能被绕过
 * ---------------------------------------------------------------------------
 * `_prepare_image` 有这一条:
 *     if frames > 1: raise ImageRejected(FILE_UNSUPPORTED, "这是一张动图(N 帧)…")
 * 而 canvas 只画得出第一帧。animated WEBP 一旦经过客户端就变成 frames == 1,
 * 服务端这道闸再也不会触发 —— 工友以为系统看了整段,其实只看了第一帧,不报错。
 *
 * · JPEG 不可能是动图,天然安全。
 * · PNG **可能**是动图(APNG),所以收 PNG 的代价是必须自己认出来 ——
 *   `isAnimatedPng` 扫 PNG 的块流找 `acTL`,认出来就原样放过、交给服务端拒。
 * · WEBP 的动画标志藏在 VP8X 块里,格式分支比 APNG 多;而工地上 WEBP 极少见,
 *   为它再写一个解析器不划算。**不收 = 行为与改动前完全一致,零回归。**
 *
 * ===========================================================================
 * EXIF 方向:这件事上浏览器标准反复过,别照记忆写
 * ---------------------------------------------------------------------------
 * 手机竖着拍的照片,像素其实是横的,靠 EXIF Orientation 标记转正。
 * 一旦画进 canvas,EXIF 就没了 —— 处理错的表现是**照片躺倒**,
 * 而识图模型看一张躺倒的工地照,判断会直接崩,且不会有任何报错。
 *
 * 两条解码路在现代浏览器里都是对的,这里主用第一条、失败退第二条:
 *   · createImageBitmap(blob, { imageOrientation: "from-image" })
 *     ⚠️ 这个选项的**默认值**在标准里改过("none" → "from-image"),
 *        所以这里**显式写死**,不吃默认值。
 *   · <img> + drawImage
 *     CSS `image-orientation` 的初始值自 Chrome 81 / Safari 13.1 / Firefox 26
 *     起就是 from-image,naturalWidth/Height 报的也是转正后的尺寸。
 *
 * 服务端 `_prepare_image` 现在也有 `exif_transpose` 了(TODO-53,同一天修的),
 * 所以客户端放过的图方向也不会错 —— 两边都保得住「模型看到的是正立的」。
 *
 * ===========================================================================
 * 失败一律回退原图
 * ---------------------------------------------------------------------------
 * 压缩是**加速手段**,不是功能。解码失败、canvas 取不到 2d、toBlob 回 null、
 * 浏览器太老 —— 任何一条都直接返回原文件,让上传照常走完。
 * 绝不允许「压不了 = 传不了」:工友在工地传不了照片,这个产品就没用了。
 *
 * ===========================================================================
 * 测试边界(别以为覆盖住了)
 * ---------------------------------------------------------------------------
 * `scripts/frontend-tests/image-compress.test.ts` 钉的是本文件的**纯函数**
 * 与「非浏览器环境返回原件」这一条。
 * 🔴 **canvas 的真实解码/编码路径没有测试** —— jsdom 不实现 `toBlob`,
 *    而为它引 node-canvas 是给测试装一个原生依赖,得不偿失。
 *    也就是说 EXIF 转正、铺白底、编码质量这几件**只有真机能验**。
 */

/**
 * 压缩后长边的像素上限。
 *
 * 🔴 **镜像服务端 `config.py` 的 `photo_compress_max_edge_px`(默认 2048)。**
 *    漂了的坏法:有人为省钱把服务端降到 1024,客户端还在发 2048 →
 *    服务端 `max(w,h) > 1024` → **再缩一次、再有损编码一次**,
 *    两代有损叠加、识图准确率掉,而**没有任何报错**。
 *    改这个数要回去改 config.py(反之亦然),CLAUDE.md 的同源清单有登记。
 */
export const MAX_EDGE = 2048;

/**
 * 服务端的体积线。**这个常量不参与 `needsWork` 的判断**,只用来:
 *   ① 给测试钉住「UPLOAD_BUDGET_BYTES 必须比它小」这个超集关系;
 *   ② 让读代码的人一眼看到两条线的差距(4MB vs 600KB)。
 *
 * 🔴 镜像 `config.py` 的 `photo_compress_target_mb`(默认 4.0)。
 */
export const TARGET_BYTES = 4 * 1024 * 1024;

/**
 * 上传预算 —— **客户端自己的线,和服务端无关**。
 *
 * 超过它就值得重编码一次,哪怕服务端根本不会动这张图。
 * 600KB 的来历:1 Mbps 上行(2026-08-19 实测工地环境的量级)下,
 * base64 膨胀 4/3 后约 6 秒。再往上,工友就会开始觉得「点了没反应」。
 *
 * ⚠️ 调大它 = 更多图被原样上传(等待变长);
 *    调小它 = 更多图被重编码一次(画质少一档、手机上多一次解码开销)。
 *    别去和服务端的 4MB 对齐 —— 那两条线的目标不是一回事,见头注第一节。
 */
export const UPLOAD_BUDGET_BYTES = 600 * 1024;

/**
 * JPEG 输出质量。
 *
 * 🔴 镜像服务端 `_JPEG_QUALITY_STEPS` 的**第一档 85**(tools.py)——
 *    对齐它,客户端压出来的东西才和服务端本来会压出来的是同一档。
 *
 * ⚠️ 与打卡自拍那条(`checkin.tsx` 的 `JPEG_QUALITY = 0.92`)**故意不同,别去对齐**。
 */
export const JPEG_QUALITY = 0.85;

/**
 * 小于这个字节数就连解码都不做。
 *
 * ⚠️ **这是纯性能闸,不是策略闸**。它没有任何业务含义,别把它当成
 *    「多大才算大照片」的定义 —— 真正的判据是 `needsWork`。
 *    解一张 4000×3000 的图要 48MB 位图,低端安卓手机上是真实的卡顿代价,
 *    这道闸只为免掉「为一张 100KB 的图白解一次」。
 *    它必须**小于** UPLOAD_BUDGET_BYTES,否则预算线里有一段永远够不着。
 */
export const DECODE_FLOOR_BYTES = 256 * 1024;

/**
 * JPEG 重编码后要小到原图的这个比例才采用。
 *
 * 同格式重编码,画质代价小,所以门槛松:小一点就值。
 * 不是「小 1 个字节就要」:为 3% 的体积白掉一档画质不划算。
 */
export const MIN_GAIN_RATIO_JPEG = 0.9;

/**
 * PNG 换成 JPEG 后要小到原图的这个比例才采用。
 *
 * 🔴 **比 JPEG 那档严格得多,这是刻意的** —— 见头注「PNG 也收了」那一节:
 *    换格式意味着不可逆地丢掉线条锐度,只有「它其实是张照片」时才划算。
 *    实测照片压到 0.14、截图压到 0.71,0.5 落在中间那道宽沟里。
 */
export const MIN_GAIN_RATIO_PNG = 0.5;

/**
 * 会被压的 MIME。
 *
 * 🔴 **GIF / WEBP 不在里面,是刻意的**(动图闸,见头注)。
 *    哪天有人「补全」它们,压完的动图会变成静态第一帧,而且一声不吭。
 */
export const COMPRESSIBLE_TYPES: readonly string[] = ["image/jpeg", "image/png"];

/**
 * 解码前的快速分流。**只看类型和大小,不解码。**
 *
 * 注意它不是「该不该压」的最终判据 —— 那是 `needsWork`,要拿到真实尺寸才能判。
 */
export function shouldTryCompress(file: {
  readonly type: string;
  readonly size: number;
}): boolean {
  if (!COMPRESSIBLE_TYPES.includes(file.type.toLowerCase())) return false;
  return file.size > DECODE_FLOOR_BYTES;
}

/**
 * 采用压缩件的门槛 —— 按**输入格式**取,不是按输出格式。
 * PNG 那档严得多的理由在头注。
 */
export function minGainRatio(inputType: string): number {
  return inputType.toLowerCase() === "image/png"
    ? MIN_GAIN_RATIO_PNG
    : MIN_GAIN_RATIO_JPEG;
}

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
/** PNG 块头固定 8 字节(4 长度 + 4 类型),块尾还有 4 字节 CRC。 */
const PNG_CHUNK_HEADER = 8;
const PNG_CHUNK_CRC = 4;

/**
 * 这份 PNG 是不是动图(APNG)。
 *
 * 判据来自 APNG 规范:动画控制块 `acTL` **必须出现在第一个 `IDAT` 之前**。
 * 所以顺着块流走,先撞到 `acTL` 就是动图,先撞到 `IDAT` 就不是,可以收工 ——
 * 不用读完整个文件(`acTL` 总在很靠前的位置)。
 *
 * PNG 的块布局:
 *
 *     [8 字节签名][长度(4,大端)][类型(4,ASCII)][数据(长度)][CRC(4)] …
 *
 * ⚠️ 任何解析异常(签名不对、长度越界、截断)一律当**不是动图**处理:
 *    这个函数只用来「要不要放过这张图」,判错成动图的代价是白白不压;
 *    而真动图漏判的代价是服务端那道闸被绕过。所以宁可保守 —— 但真正兜底的
 *    仍然是服务端那条 `frames > 1`,这里只是别把它绕过去。
 */
export function isAnimatedPng(bytes: Uint8Array): boolean {
  if (bytes.length < PNG_SIGNATURE.length) return false;
  for (let i = 0; i < PNG_SIGNATURE.length; i += 1) {
    if (bytes[i] !== PNG_SIGNATURE[i]) return false; // 不是 PNG,不归这里管
  }

  let at = PNG_SIGNATURE.length;
  while (at + PNG_CHUNK_HEADER <= bytes.length) {
    const length =
      // 大端 32 位。用 >>> 0 收成无符号,否则 length 超过 2^31 会变负数,
      // 下面的越界判断就会失效。
      ((bytes[at] << 24) |
        (bytes[at + 1] << 16) |
        (bytes[at + 2] << 8) |
        bytes[at + 3]) >>>
      0;
    const type = String.fromCharCode(
      bytes[at + 4],
      bytes[at + 5],
      bytes[at + 6],
      bytes[at + 7],
    );
    if (type === "acTL") return true;
    if (type === "IDAT") return false; // acTL 必须在它之前,没撞上就不是动图
    at += PNG_CHUNK_HEADER + length + PNG_CHUNK_CRC;
    if (!Number.isFinite(at) || at <= 0) return false; // 长度是垃圾值,别死循环
  }
  return false; // 读到头也没见 acTL / IDAT —— 截断或畸形,当静态图
}

/**
 * 这张图该不该动手。取两条判据的并集,理由在头注第一节。
 *
 *     长边 > MAX_EDGE          ← 服务端反正要缩(镜像 _prepare_image)
 *  || 体积 > UPLOAD_BUDGET     ← 传太久,值得重编码一次(客户端自己的线)
 */
export function needsWork(
  width: number,
  height: number,
  bytes: number,
): boolean {
  return Math.max(width, height) > MAX_EDGE || bytes > UPLOAD_BUDGET_BYTES;
}

/**
 * 等比缩进 maxEdge 见方的框里。长边没超上限就原样返回(**不放大**)。
 *
 * 返回值一律是 >= 1 的整数:canvas 的宽高吃小数会被截断,吃 0 会直接抛错。
 */
export function fitWithin(
  width: number,
  height: number,
  maxEdge: number = MAX_EDGE,
): { width: number; height: number } {
  if (!(width > 0) || !(height > 0)) return { width: 0, height: 0 };
  const longEdge = Math.max(width, height);
  if (longEdge <= maxEdge) {
    return { width: Math.round(width), height: Math.round(height) };
  }
  const scale = maxEdge / longEdge;
  return {
    // 极端长宽比(比如 8000×20 的全景条)缩完短边会不足 1 —— 兜到 1,
    // 否则 canvas.height = 0 会让整条压缩抛错、白白退回原图。
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

/** 压出来的这份值不值得用。门槛按**输入格式**取,见 `minGainRatio`。 */
export function shouldKeepCompressed(
  originalBytes: number,
  compressedBytes: number,
  inputType: string = "image/jpeg",
): boolean {
  if (!(compressedBytes > 0)) return false;
  return compressedBytes < originalBytes * minGainRatio(inputType);
}

// ===========================================================================
// 下面开始碰浏览器。上面全是纯函数,frontend-tests 里逐条钉着;
// 下面这段**没有测试**(jsdom 不实现 canvas),只有真机能验。
// ===========================================================================

/** APNG 的 acTL 总在很靠前的位置,读个开头就够,别把整张图读进内存。 */
const APNG_PROBE_BYTES = 64 * 1024;

async function isAnimatedPngFile(file: Blob): Promise<boolean> {
  try {
    const head = await file.slice(0, APNG_PROBE_BYTES).arrayBuffer();
    return isAnimatedPng(new Uint8Array(head));
  } catch {
    return false; // 读不出来就当静态图,兜底仍在服务端
  }
}

interface Decoded {
  readonly source: CanvasImageSource;
  readonly width: number;
  readonly height: number;
  readonly done: () => void;
}

/** 解码成能画进 canvas 的东西。两条路都在头注「EXIF 方向」里解释过。 */
async function decode(file: Blob): Promise<Decoded> {
  if (typeof createImageBitmap === "function") {
    try {
      // imageOrientation 显式写死,不吃默认值 —— 标准改过(见头注)。
      const bitmap = await createImageBitmap(file, {
        imageOrientation: "from-image",
      });
      return {
        source: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        done: () => bitmap.close(),
      };
    } catch {
      // 落到 <img> 那条。老浏览器可能不认 imageOrientation 这个字典键。
    }
  }

  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      // ⚠️ 这句**刻意留英文**,与 multimodal-utils.ts 里那句拒绝理由同口径:
      //    它只进 catch 和控制台,一个工友都看不到 —— 属于本仓
      //    「注释 / 日志 / 开发者报错」那一档,不进翻译范围。
      el.onerror = () => reject(new Error("image decode failed"));
      el.src = url;
    });
    return {
      source: img,
      width: img.naturalWidth,
      height: img.naturalHeight,
      done: () => URL.revokeObjectURL(url),
    };
  } catch (err) {
    URL.revokeObjectURL(url);
    throw err;
  }
}

/** canvas.toBlob 是回调式的,包成 Promise;出不来就抛。 */
function toBlob(
  canvas: HTMLCanvasElement,
  type: string,
  quality: number,
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      // 报错文案留英文,理由同 decode() 里那条(只进 catch,不上屏)。
      (blob) =>
        blob ? resolve(blob) : reject(new Error("canvas toBlob failed")),
      type,
      quality,
    );
  });
}

/**
 * 把一张照片压小。
 * **任何情况下都会返回一个能上传的 File** —— 压不动、压坏了,返回的就是原件。
 *
 * 调用点只有 `multimodal-utils.ts` 的 `fileToContentBlock`,那是选文件 /
 * 拖拽 / 粘贴三条路唯一的汇合处(上游把校验抄了三遍,但这一步没有)。
 */
export async function compressImageFile(file: File): Promise<File> {
  if (!shouldTryCompress(file)) return file;
  if (typeof document === "undefined") return file; // SSR / 非浏览器环境

  // 动图原样放过 —— 交给服务端那句「这是一张动图,看不了」,别在这儿悄悄拍平。
  if (file.type.toLowerCase() === "image/png" && (await isAnimatedPngFile(file))) {
    return file;
  }

  let release: (() => void) | null = null;
  try {
    const decoded = await decode(file);
    release = decoded.done;

    if (!needsWork(decoded.width, decoded.height, file.size)) return file;

    const { width, height } = fitWithin(decoded.width, decoded.height);
    if (width === 0 || height === 0) return file;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;

    // 拉近与服务端 LANCZOS 的差距。Chrome 在 "high" 下会换更好的滤波器;
    // 不认这个属性的浏览器忽略它,无害。
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";

    // JPEG 没有透明通道。不先铺白,PNG 的透明区会变成**黑块** ——
    // 而工地照片里那种黑块看着像烧焦,识图模型会当成异常报上来。
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(decoded.source, 0, 0, width, height);

    const blob = await toBlob(canvas, "image/jpeg", JPEG_QUALITY);
    if (!shouldKeepCompressed(file.size, blob.size, file.type)) return file;

    // 文件名保持原样。后端不看它(照片的落盘扩展名是按 mimeType 查
    // EXT_BY_MIME 得来的,见 core/uploads.py),而界面上那张预览卡片和
    // 「这份已经加过了」的提示都是拿它给人看的 —— 把 工地.png 改成 工地.jpg
    // 只会让人以为自己传错了文件。
    //
    // ⚠️ 但 **mimeType 变了**(PNG 进 JPEG 出),所以 `use-file-upload.tsx`
    //    的查重不能再拿 mime 比 —— 那边已经改成只比文件名,注释互指。
    return new File([blob], file.name, {
      type: "image/jpeg",
      lastModified: file.lastModified,
    });
  } catch {
    // 见头注「失败一律回退原图」:压缩坏了绝不能把上传带崩。
    return file;
  } finally {
    release?.();
  }
}
