/**
 * 上传前把照片缩到服务端反正要缩到的那个尺寸 —— 治「点了发送,先干等十几秒」。
 *
 * ===========================================================================
 * 这件事的定性:**不是新增一步压缩,是把服务端已有的那一步搬到客户端**
 * ---------------------------------------------------------------------------
 * `backend/src/gyt/agents/safety/tools.py` 的 `_prepare_image` 早就在缩了:
 *
 *     if max(width, height) <= max_edge and len(payload) <= limit_bytes:
 *         return payload, MIME_BY_EXT[ext]        # 没超限 → 原样透传,连重编码都不做
 *     image.convert("RGB"); image.thumbnail((max_edge, max_edge), LANCZOS)
 *     for quality in (85, 75, 65, 55): ...        # 还大就逐档降
 *
 * 也就是说 **kimi 从来没见过 4000px 的图**,它收到的一直是 2048。
 * 那 4000px 的原图在网上传了十几秒,到了服务端第一件事就是被缩掉。
 *
 * 所以本文件的目标不是「压得更狠」,而是**让那次必然发生的缩图提前到客户端做**,
 * 于是网上少传十几秒。判据必须与服务端**逐条对齐**(见下面的决策树),
 * 对不齐就会出现「客户端缩一次、服务端再缩一次」的两代有损叠加。
 *
 * ===========================================================================
 * 2026-08-19 线上实测(不是估的)
 * ---------------------------------------------------------------------------
 * 一次「拍照 → 出巡检记录」量到 56.7 秒,后端日志拆开:
 *
 *     12.0s  照片上传 + 建 run     ← run_create_ms,这段后端一行代码没跑
 *      0.6s  排队
 *     44.1s  执行
 *              0.3s  supervisor 派活
 *              1.0s  safety 决定要看图
 *             11.4s  kimi 识图        ← 真正在干活的只有这段
 *             26.9s  safety 组织回答
 *              2.6s  supervisor 汇总
 *
 * 那 12 秒几乎正比于照片大小(2.87MB → 12003ms;3.11MB → 14210ms)。
 * 同一天实测该用户上下行都只有约 1 Mbps(3MB 上传 23.9 秒)。工地 4G 只会更差。
 *
 * 顺带减轻(**不是治好**)的两处,同一批日志里量到的:
 *   · 对话历史 16.0MB / 113 秒 —— 根因是 base64 进了 LangGraph 检查点、每步一份,
 *     压缩只把常数改小,**它仍然随轮数无限涨**。结构修法见 TODOS.md 的 TODO-51。
 *   · 聊天里的缩略图 3.1MB / 25.3 秒 —— human.tsx 直接 GET 原图,服务端不缩图。
 *
 * ===========================================================================
 * 决策树(与 _prepare_image 逐条对齐)
 * ---------------------------------------------------------------------------
 *
 *   File
 *    │
 *    ├─ type !== "image/jpeg" ──────────────────────────► 原件(见下方「只收 JPEG」)
 *    │
 *    ├─ size <= DECODE_FLOOR_BYTES ────────────────────► 原件
 *    │     ⚠️ 这是**纯性能闸,不是策略闸** —— 只为了别给一张 100KB 的图
 *    │        白解码一次(手机上是真实的卡顿)。它没有任何业务含义。
 *    │        副作用:压得很狠的 4000px 小图(200KB 那种)不会被加速,
 *    │        照旧交给服务端缩 —— 结果正确,只是没提速。可接受:
 *    │        200KB 在 1 Mbps 下也就 1.6 秒。
 *    ▼
 *   解码(EXIF 转正,见下方)
 *    │
 *    ├─ !needsWork(w, h, size) ────────────────────────► 原件
 *    │     🔴 **这一条是本文件的核心**,判据逐字镜像服务端那个 if:
 *    │        长边 <= MAX_EDGE 且 体积 <= TARGET_BYTES → 服务端会原样透传,
 *    │        那客户端**也一个字节都不许动**。
 *    │        少了这条的坏法:1920×1080 / 800KB 的手机照片,尺寸压根不用缩,
 *    │        却被白白有损重编码一次 —— 而 _prepare_image 的注释明写着
 *    │        「重编码只会白白损失画质,而判断有没有戴安全帽经不起反复有损压缩」。
 *    ▼
 *   fitWithin → canvas 缩放 → toBlob(JPEG, JPEG_QUALITY)
 *    │
 *    ├─ !shouldKeepCompressed(原, 压) ─────────────────► 原件
 *    │     压完不见得小(已经压过的图重编码常常更大)。
 *    │     这条是最容易被顺手「简化」掉的一条,删了之后的表现是
 *    │     「压了个寂寞,还掉了画质」,没有任何报错。
 *    ▼
 *   压缩后的 File(image/jpeg)
 *
 * ===========================================================================
 * 为什么**只收 image/jpeg**(三个独立理由,任何一个都够)
 * ---------------------------------------------------------------------------
 * ① **JPEG 不可能是动图,所以绕不过服务端那道动图闸。**
 *    `_prepare_image` 有这一条:
 *        if frames > 1: raise ImageRejected(FILE_UNSUPPORTED, "这是一张动图(N 帧)…")
 *    而 canvas 只画得出第一帧。**animated WEBP 和 APNG 一旦经过客户端就变成
 *    frames == 1,服务端这道闸再也不会触发** —— 工友以为系统看了整段,
 *    其实只看了第一帧,而且一句话都不说。GIF 好排除,APNG / animated WEBP 不好认。
 *    收窄到 JPEG 是唯一不用去认动图的办法。
 *
 * ② **mime 不变,查重才不会漏。** `use-file-upload.tsx` 的 isDuplicate 拿
 *    `b.mimeType === file.type` 比。PNG 压成 JPEG 之后 block 是 image/jpeg、
 *    重选的 File 还是 image/png → **判不出重复**,同一张图登记两次产物、
 *    识图跑两遍、钱花两份。JPEG 进 JPEG 出就没这个问题,那两处一个字都不用改。
 *
 * ③ **覆盖面没损失。** 工地照片 = 手机拍 = JPEG(iOS 的 HEIC 由 Safari 在上传时
 *    转成 JPEG,file.type 就是 image/jpeg)。PNG 基本都是截图,而真图纸走的是
 *    DXF 那条上传路、压根不经过这里。PNG/WEBP/GIF 一律原样上传,
 *    **行为与本改动之前逐字节一致,零回归**。
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
 * ===========================================================================
 * 与服务端**唯一**的实质差别:重采样算法
 * ---------------------------------------------------------------------------
 *     今天:  4000×3000 ──Pillow LANCZOS──► 2048×1536 ──JPEG q85──► kimi
 *     改后:  4000×3000 ──浏览器 canvas ──► 2048×1536 ──JPEG q85──► kimi
 *                         ↑ 差别只在这儿
 *
 * 尺寸档位一样、质量档位一样、有损代数一样(都是一次),**只有滤波器不同**。
 * ⚠️ 别把这写成「逐像素一致」—— 不是。LANCZOS 比浏览器默认的双线性锐。
 *    4000→2048 只有 2 倍下采样,属于混叠不明显的区间;
 *    `imageSmoothingQuality = "high"` 再拉近一些,但不完全等价。
 *
 * 另:服务端那个**质量阶梯 (85,75,65,55) 仍然是后手**。这里只压一档 q0.85 ——
 * 2048×1536 的照片在 q85 下约 700KB,要超 4MB 得是病态输入。真超了服务端会再压一次
 * (代价是那种输入上多一代有损),不值得为它在客户端复制一整个循环。
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
 * `scripts/frontend-tests/image-compress.test.ts` 钉的是**上面四个纯函数**
 * 与「非浏览器环境返回原件」这一条。
 * 🔴 **canvas 的真实解码/编码路径没有测试** —— jsdom 不实现 toBlob,
 *    而为它引 node-canvas 是给测试装一个原生依赖,得不偿失。
 *    也就是说 EXIF 转正、铺色、编码质量这几件**只有真机能验**。
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
 * 体积上限。超过它服务端就会重编码,所以客户端也该动手。
 *
 * 🔴 镜像 `config.py` 的 `photo_compress_target_mb`(默认 4.0)。同上,两边一起改。
 */
export const TARGET_BYTES = 4 * 1024 * 1024;

/**
 * JPEG 输出质量。
 *
 * 🔴 镜像服务端 `_JPEG_QUALITY_STEPS` 的**第一档 85**(tools.py:186)——
 *    对齐它,客户端压出来的东西才和服务端本来会压出来的是同一档。
 *
 * ⚠️ 与打卡自拍那条(`checkin.tsx` 的 `JPEG_QUALITY = 0.92`)**故意不同,别去对齐**:
 *    那条画的是摄像头帧(本来就只有 640~1280 宽,不缩尺寸),只求别端原图过去;
 *    这条要吃手机相册里 4000px 的原片,而且要跟服务端的档位一致。
 */
export const JPEG_QUALITY = 0.85;

/**
 * 小于这个字节数就连解码都不做。
 *
 * ⚠️ **这是纯性能闸,不是策略闸** —— 见头注决策树里的说明。
 *    它没有任何业务含义,别把它当成「多大才算大照片」的定义:
 *    真正的判据是 `needsWork`,那条才镜像服务端。
 *    解一张 4000×3000 的图要 48MB 位图,低端安卓手机上是真实的卡顿代价,
 *    这道闸只为免掉「为一张 100KB 的图白解一次」。
 */
export const DECODE_FLOOR_BYTES = 256 * 1024;

/**
 * 压完必须小于原图的这个比例才采用。
 *
 * 不是「小一点就要」:为 3% 的体积去掉一次重编码的画质不划算。
 */
export const MIN_GAIN_RATIO = 0.9;

/** 唯一会被压的 MIME。收窄到它的三个理由在头注,别放宽。 */
export const COMPRESSIBLE_TYPE = "image/jpeg";

/**
 * 解码前的快速分流。**只看类型和大小,不解码。**
 *
 * 注意它不是「该不该压」的最终判据 —— 那是 `needsWork`,要拿到真实尺寸才能判。
 */
export function shouldTryCompress(file: {
  readonly type: string;
  readonly size: number;
}): boolean {
  if (file.type.toLowerCase() !== COMPRESSIBLE_TYPE) return false;
  return file.size > DECODE_FLOOR_BYTES;
}

/**
 * 服务端到底会不会动这张图 —— 镜像 `_prepare_image` 那个 if 的**前两项**:
 *
 *     if max(width, height) <= max_edge and len(payload) <= limit_bytes and upright:
 *         return payload          # 原样透传
 *         ↑ 前两项在这儿取反       ↑ 第三项故意不镜像,见下
 *
 * 服务端不动的,客户端也不许动(否则就是白掉一档画质)。
 *
 * ---------------------------------------------------------------------------
 * 第三项 `upright`(EXIF 方向正不正)**故意不镜像**(2026-08-19,TODO-53)
 * ---------------------------------------------------------------------------
 * 两边各自都保得住「模型看到的是正立的」,而跟它要付真代价:
 *
 *   · 本文件解码时就用 `imageOrientation: "from-image"`,**凡是它重编码过的
 *     都已经转正**,产出还不带方向标记 —— 服务端拿到就是 upright,直接透传。
 *   · 本文件原样放过的(小图 / 非 JPEG / 压完更大),EXIF 还在原文件里,
 *     服务端那条新判据接得住,会替它转正。
 *
 * 要镜像的话,得在**解码前**知道方向 —— 而 `createImageBitmap` 不给,只能自己
 * 解 JPEG 的 APP1 段。为一个服务端已经兜住的场景手写 EXIF 解析器,是拿一段新的、
 * 没测试的字节解析去换一次服务端重编码,不划算。
 *
 * ⚠️ 这条「不镜像」的前提是**服务端真的会转正**。`image-compress.test.ts` 里
 *    有一条专门盯着它(`_UPRIGHT_ORIENTATIONS` + `exif_transpose` 都还在)——
 *    前提没了而这里不知道的话,方向不正的小图两边都不管,原样躺着送进模型,
 *    而界面上还是正的(浏览器按 EXIF 渲染),一句报错都没有。
 */
export function needsWork(
  width: number,
  height: number,
  bytes: number,
): boolean {
  return Math.max(width, height) > MAX_EDGE || bytes > TARGET_BYTES;
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

/** 压出来的这份值不值得用。见头注决策树最后一条。 */
export function shouldKeepCompressed(
  originalBytes: number,
  compressedBytes: number,
): boolean {
  if (!(compressedBytes > 0)) return false;
  return compressedBytes < originalBytes * MIN_GAIN_RATIO;
}

// ===========================================================================
// 下面开始碰浏览器。上面全是纯函数,frontend-tests 里逐条钉着;
// 下面这段**没有测试**(jsdom 不实现 canvas),只有真机能验。
// ===========================================================================

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
      //    写成中文会被 hant-ui-strings.test.ts 判成「界面串还是简体」而红,
      //    而登记进 hant-keep-hans.mjs 是错的:那张表管的是**真上屏**的例外。
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
      (blob) => (blob ? resolve(blob) : reject(new Error("canvas toBlob failed"))),
      type,
      quality,
    );
  });
}

/**
 * 把一张照片缩到服务端反正要缩到的尺寸。
 * **任何情况下都会返回一个能上传的 File** —— 压不动、压坏了,返回的就是原件。
 *
 * 调用点只有 `multimodal-utils.ts` 的 `fileToContentBlock`,那是选文件 /
 * 拖拽 / 粘贴三条路唯一的汇合处(上游把校验抄了三遍,但这一步没有)。
 */
export async function compressImageFile(file: File): Promise<File> {
  if (!shouldTryCompress(file)) return file;
  if (typeof document === "undefined") return file; // SSR / 非浏览器环境

  let release: (() => void) | null = null;
  try {
    const decoded = await decode(file);
    release = decoded.done;

    // 拿到真实尺寸之后才是正经判据 —— 服务端不动的,这里也不动。
    if (!needsWork(decoded.width, decoded.height, file.size)) return file;

    const { width, height } = fitWithin(decoded.width, decoded.height);
    if (width === 0 || height === 0) return file;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;

    // 拉近与服务端 LANCZOS 的差距(见头注「唯一的实质差别」)。
    // Chrome 在 "high" 下会换更好的滤波器;不认这个属性的浏览器忽略它,无害。
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(decoded.source, 0, 0, width, height);

    // 输出恒为 JPEG:入参只可能是 JPEG(shouldTryCompress 卡死了),
    // 所以 mimeType 前后不变 —— use-file-upload.tsx 的查重逻辑因此不用动。
    const blob = await toBlob(canvas, COMPRESSIBLE_TYPE, JPEG_QUALITY);
    if (!shouldKeepCompressed(file.size, blob.size)) return file;

    // 文件名保持原样。后端不看它(照片的落盘扩展名是按 mimeType 查
    // EXT_BY_MIME 得来的,见 core/uploads.py),而界面上那张预览卡片和
    // 「这份已经加过了」的提示都是拿它给人看的。
    return new File([blob], file.name, {
      type: COMPRESSIBLE_TYPE,
      lastModified: file.lastModified,
    });
  } catch {
    // 见头注「失败一律回退原图」:压缩坏了绝不能把上传带崩。
    return file;
  } finally {
    release?.();
  }
}
