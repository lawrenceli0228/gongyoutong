import { ContentBlock } from "@langchain/core/messages";
import { toast } from "sonner";

import { compressImageFile } from "@/lib/image-compress";

// --------------------------------------------------------------------------
// 上传出错的三句人话(W12 界面繁體化,2026-08-18 补)
// --------------------------------------------------------------------------
//
// 🔴 上游这三句原文是英文,而界面已经整体恒繁體 —— 港方工友把 .doc 拖进输入框,
//    一整屏繁體里弹出一句 "You have uploaded invalid file type. Please upload a
//    JPEG, PNG, GIF, WEBP image, a PDF, or a DXF drawing (.dxf)."。
//
// ⚠️ **W12 那套繁體守卫抓不到英文残留**:`hant-ui-strings.test.ts` 的判据是
//    `s2hk(v) !== v`,而 s2hk 对英文**恒等** —— 英文串在它眼里永远「已经到位」。
//    这几条是人工扫出来的,不是守卫报出来的。写新文案时别指望它替你把关。
//
// 🔴 为什么摆在这个文件里、而不是摆在真正用它的 use-file-upload.tsx:
//    use-file-upload.tsx **已经** import 本文件的 fileToContentBlock,
//    反过来 import 就成环。本文件是这条链上的叶子,常量放这儿才无环。
//
// 🔴 为什么抽成常量:上游把同一句英文**抄了三遍**(选文件 / 拖进来 / 粘贴,
//    分散在 use-file-upload.tsx 三处),重复那句「已经加过了」也抄了三遍。
//    漏改一处的表现是「同一个错,粘贴时弹英文、拖拽时弹繁體」——
//    没有报错,而且要走三条不同的交互路径才复现得出来。
//
// ⚠️ 句子里**不许出现 MIME 串**(`image/jpeg` 那种)。工地师傅认的是
//    「图片 / PDF / 图纸」,不是 `application/pdf`。本文件那句原文就是直接把
//    `supportedFileTypes.join(", ")` 拼上屏的,一并改掉了。

/** 能传什么 —— 两句「格式不支持」共用的尾巴。 */
const SUPPORTED_HINT =
  "能傳的有:圖片(JPEG、PNG、GIF、WEBP)、PDF、還有 DXF 圖紙(.dxf)。";

/** 选文件 / 拖进来时选了不支持的格式。 */
export const MSG_UNSUPPORTED_PICK = `這種文件傳不了。${SUPPORTED_HINT}`;

/** 粘贴进来的是不支持的格式(上游对粘贴用的是另一句措辞,这个区分保留)。 */
export const MSG_UNSUPPORTED_PASTE = `粘貼進來的這種文件傳不了。${SUPPORTED_HINT}`;

/** 同一条消息里重复加了同一份文件。 */
export function msgDuplicate(files: readonly File[]): string {
  const names = files.map((f) => f.name).join("、");
  return `這些文件已經加過了:${names}。同一條消息裏,一份文件只能傳一次。`;
}

// Returns a Promise of a typed multimodal block for images or PDFs
export async function fileToContentBlock(
  file: File,
): Promise<ContentBlock.Multimodal.Data> {
  const supportedImageTypes = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
  ];
  const supportedFileTypes = [...supportedImageTypes, "application/pdf"];

  // GYT 方案 A:DXF 图纸。浏览器给 .dxf 的 MIME 极不可靠(常是空串或
  // application/octet-stream),所以**按文件名 .dxf 后缀认**,不信 file.type。
  const isDxf = file.name.toLowerCase().endsWith(".dxf");

  if (!supportedFileTypes.includes(file.type) && !isDxf) {
    // 🔴 上屏那句用 use-file-upload.tsx 的同一句人话(那边是唯一真相)。
    //    上游原文是 `Unsupported file type: ${file.type}. Supported types are:
    //    image/jpeg, image/png, …` —— 两个毛病:整句英文,而且把 **MIME 串**
    //    直接甩给工友看。恒繁體界面里这一句尤其扎眼,而 W12 那套繁體守卫的判据
    //    是 `s2hk(v) !== v`,**英文恒等**,一条都不会报。
    toast.error(MSG_UNSUPPORTED_PICK);
    // ⚠️ 下面这句**刻意留英文**:它是 Promise 的拒绝理由,只进控制台 / 上游错误链,
    //    一个工友都看不到。带上 file.type 是为了排查时知道浏览器报的是哪个 MIME。
    //    与本仓「注释 / 日志 / 开发者报错」那一档同口径,不进翻译范围。
    return Promise.reject(new Error(`Unsupported file type: ${file.type}`));
  }

  const isImage = supportedImageTypes.includes(file.type);

  // ------------------------------------------------------------------------
  // 上传前把照片缩到服务端反正要缩到的尺寸(image-compress.ts,理由在它的头注)。
  //
  // 🔴 **摆在这里、而不是摆在 use-file-upload.tsx**:那边把「校验 → 查重 →
  //    转块」抄了三遍(选文件 / 拖进来 / 粘贴),而这个函数是三条路唯一的汇合处。
  //    放那边要改三处,漏一处的表现是「拖进来的照片快、粘贴进来的还是慢十几秒」,
  //    没有报错,而且要走三条不同交互才复现得出来。
  //
  // 🔴 **DXF 图纸和 PDF 一个字节都不许动** —— 图纸要的是尺寸/标高精确到毫米,
  //    PDF 里可能有可选中的文字层,任何重编码都是毁内容。所以这里用 isImage 卡住。
  //    (compressImageFile 自己也只认 image/jpeg,这是第二道;两道都留着,
  //     因为哪天有人放宽那边的白名单,这一道还挡着图纸。)
  // ------------------------------------------------------------------------
  const upload = isImage ? await compressImageFile(file) : file;

  const data = await fileToBase64(upload);

  // DXF 图纸:发成 file 块(与 PDF 同形),后端 uploads.py 按 filename .dxf 认出来
  // 登记成 DRAWING 产物。mimeType 钉一个规范值,后端主要还是看 filename。
  if (isDxf) {
    return {
      type: "file",
      mimeType: "image/vnd.dxf",
      data,
      metadata: { filename: file.name },
    };
  }

  if (isImage) {
    return {
      type: "image",
      // ⚠️ 读 **upload.type**,不是 file.type —— 这里必须报**真实字节**的类型:
      //    后端照片的落盘扩展名是拿 mimeType 查 EXT_BY_MIME 得来的
      //    (core/uploads.py,不看文件名),说谎就会出现「.png 后缀装着 jpeg 字节」。
      //    今天两者恒等(压缩只收 JPEG、只出 JPEG),写 upload.type 是为了
      //    哪天有人放宽 COMPRESSIBLE_TYPE 时这里不会静默出错。
      mimeType: upload.type,
      data,
      // 文件名一律用原件的:界面上那张预览卡和「已经加过了」的提示拿它给人看,
      // 后端不看它。改成 .jpg 只会让人以为自己传错了文件。
      metadata: { name: file.name },
    };
  }

  // PDF
  return {
    type: "file",
    mimeType: "application/pdf",
    data,
    metadata: { filename: file.name },
  };
}

// Helper to convert File to base64 string
export async function fileToBase64(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result as string;
      // Remove the data:...;base64, prefix
      resolve(result.split(",")[1]);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Type guard for Base64ContentBlock
export function isBase64ContentBlock(
  block: unknown,
): block is ContentBlock.Multimodal.Data {
  if (typeof block !== "object" || block === null || !("type" in block))
    return false;
  // file type (legacy)
  if (
    (block as { type: unknown }).type === "file" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    ((block as { mimeType: string }).mimeType.startsWith("image/") ||
      (block as { mimeType: string }).mimeType === "application/pdf")
  ) {
    return true;
  }
  // image type (new)
  if (
    (block as { type: unknown }).type === "image" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    (block as { mimeType: string }).mimeType.startsWith("image/")
  ) {
    return true;
  }
  // GYT 方案 A:DXF 图纸 file 块。按 mimeType=image/vnd.dxf 或 filename .dxf 认,
  // 否则预览组件会当它「不支持」而不显示这张卡片。
  if ((block as { type: unknown }).type === "file") {
    const mime = (block as { mimeType?: unknown }).mimeType;
    const filename = (block as { metadata?: { filename?: unknown } }).metadata
      ?.filename;
    if (
      (typeof mime === "string" && mime === "image/vnd.dxf") ||
      (typeof filename === "string" && filename.toLowerCase().endsWith(".dxf"))
    ) {
      return true;
    }
  }
  return false;
}
