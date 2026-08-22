import { useState, useRef, useEffect, ChangeEvent } from "react";
import { toast } from "sonner";
import { ContentBlock } from "@langchain/core/messages";
import {
  MSG_UNSUPPORTED_PASTE,
  MSG_UNSUPPORTED_PICK,
  fileToContentBlock,
  msgDuplicate,
} from "@/lib/multimodal-utils";

export const SUPPORTED_FILE_TYPES = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "application/pdf",
];

// GYT 方案 A:除图片/PDF 外,再收 DXF 图纸。DXF 的浏览器 MIME 不可靠(常是空串或
// application/octet-stream),所以**按文件名 .dxf 后缀认**,不信 file.type。
export function isDxfFile(file: File): boolean {
  return file.name.toLowerCase().endsWith(".dxf");
}

export function isSupportedFile(file: File): boolean {
  return SUPPORTED_FILE_TYPES.includes(file.type) || isDxfFile(file);
}

/**
 * 相机拍出来的那一张 → 换一个当场唯一的文件名(2026-08-22,随「拍照」入口一起加)。
 *
 * ── 🔴 不改名会怎样 ─────────────────────────────────────────────────
 * 下面 `isDuplicate` 的图片分支**只比文件名**(那儿有整段推演,别去改它 ——
 * 比 mime 会因为客户端压缩而必然判不出重复)。而 **iOS Safari 的
 * `<input capture>` 给每一张现场拍的照片的名字都是 `image.jpg`**,
 * 一个字都不带变。
 *
 * 于是工友在同一条消息里拍两处隐患:第一张进去了,第二张被判成重复、
 * 弹一句「这几个文件刚才已经加过了」—— 而他明明刚拍了一张全新的照片。
 * 他会以为系统坏了,或者以为第一张没拍上,然后重来一遍。
 *
 * ── 为什么改名是对的解法 ────────────────────────────────────────────
 * 判据不是"绕过查重",而是一条**真的成立的事实**:相机不会把同一张照片递第二次。
 * 每按一次快门就是一张新照片,哪怕它和上一张长得一模一样(拍虚了重拍),
 * 那也是工友**有意**要再传一张。查重防的是"同一个文件被选了两遍",
 * 而相机这条路上根本不存在"再选一遍同一个文件"这个动作。
 *
 * 时间戳 + 随机尾:同一秒里连按两下也不会撞(手机上真做得到)。
 * 保留原扩展名 —— `image-compress.ts` 与后端的类型判断都不看它,但工友手机里
 * 那份下载下来的文件要带 `.jpg` 才点得开。
 */
function renameCameraCapture(file: File): File {
  const dot = file.name.lastIndexOf(".");
  const ext = dot > 0 ? file.name.slice(dot) : ".jpg";
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  return new File([file], `現場照片-${stamp}${ext}`, {
    type: file.type,
    lastModified: file.lastModified,
  });
}

/**
 * 这一发是不是从**相机**来的。
 *
 * 判据是 input 上有没有 `capture` 属性 —— 不是 id、不是 name。
 * 用 id 的话,哪天有人改了 `camera-input` 这个 id(或者再加一个相机入口),
 * 这里会**静默失效**:改名不再发生,而 iOS 上第二张照片重新开始被判重复,
 * 没有任何东西会说话。`capture` 是那个入口之所以是相机的**原因本身**,
 * 跟着它走的话,只要那个属性还在,这条就还成立。
 */
function isCameraInput(input: HTMLInputElement): boolean {
  return input.hasAttribute("capture");
}

interface UseFileUploadOptions {
  initialBlocks?: ContentBlock.Multimodal.Data[];
}

export function useFileUpload({
  initialBlocks = [],
}: UseFileUploadOptions = {}) {
  const [contentBlocks, setContentBlocks] =
    useState<ContentBlock.Multimodal.Data[]>(initialBlocks);
  const dropRef = useRef<HTMLDivElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);

  const isDuplicate = (file: File, blocks: ContentBlock.Multimodal.Data[]) => {
    if (isDxfFile(file)) {
      return blocks.some(
        (b) => b.type === "file" && b.metadata?.filename === file.name,
      );
    }
    if (file.type === "application/pdf") {
      return blocks.some(
        (b) =>
          b.type === "file" &&
          b.mimeType === "application/pdf" &&
          b.metadata?.filename === file.name,
      );
    }
    if (SUPPORTED_FILE_TYPES.includes(file.type)) {
      // ----------------------------------------------------------------------
      // 图片这一支**只比文件名,不比 mimeType**(上游原文还比着 mime,这里删掉了)
      // ----------------------------------------------------------------------
      //
      // 🔴 **为什么不能比 mime**:转块前会先过 `image-compress.ts` 压一道,而它
      //    **收 PNG、出 JPEG**。于是 block 里的 `mimeType` 是 `image/jpeg`
      //    (`multimodal-utils.ts` 的 `fileToContentBlock` 刻意读的是压缩件的
      //    `upload.type`,理由在那儿的注释里),而工友重新选中的那份 File
      //    仍然是 `image/png` —— 拿 mime 比**必然判不出重复**。
      //    (`image-compress.ts` 头注末尾那条注释指回这里,两头互指。)
      //
      // 🔴 **漏判的后果不是「多一张预览卡」这么轻**:同一张图会被后端登记成
      //    **两份产物**、拿到两个照片编号,识图就照着跑两遍 —— 二十来秒的活干两遍、
      //    钱花两份,而界面上一声不吭,没有任何报错。
      //
      // ⚠️ **只比文件名的代价**:同一条消息里两个同名但不同格式的图片
      //    (`工地.png` 和 `工地.jpg`)会被判成重复,后加的那张进不来。
      //    **这是接受的取舍** —— 同一条消息里的同名文件本来就该算重复(界面上那两张
      //    预览卡只显示文件名,工友自己也分不清谁是谁),而且这种情况极罕见。
      //
      // ⚠️ **这段逻辑上游抄了两份**:这份给「选文件 / 拖进来」用,`handlePaste`
      //    里(本文件下方)还重新定义了一份给「粘贴」用。**两份必须一起改** ——
      //    漏一份的表现是「选文件查得出重复、粘贴查不出」,没有报错,而且要走两条
      //    不同的交互路径才复现得出来。
      //
      // ⚠️ 上面 PDF 那一支的 `b.mimeType === "application/pdf"` 是**字面量**比较,
      //    不是拿 `file.type` 比;PDF 和 DXF 都不经过压缩、mime 前后不变,别顺手删。
      return blocks.some(
        (b) => b.type === "image" && b.metadata?.name === file.name,
      );
    }
    return false;
  };

  const handleFileUpload = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    // 相机那一路先改名再进查重 —— 理由整段在 `renameCameraCapture` 上面。
    // ⚠️ 顺序不能反:改名必须发生在 `isDuplicate` 之前,而不是转块之后。
    const fromCamera = isCameraInput(e.target);
    const fileArray = Array.from(files).map((file) =>
      fromCamera ? renameCameraCapture(file) : file,
    );
    const validFiles = fileArray.filter((file) =>
      isSupportedFile(file),
    );
    const invalidFiles = fileArray.filter(
      (file) => !isSupportedFile(file),
    );
    const duplicateFiles = validFiles.filter((file) =>
      isDuplicate(file, contentBlocks),
    );
    const uniqueFiles = validFiles.filter(
      (file) => !isDuplicate(file, contentBlocks),
    );

    if (invalidFiles.length > 0) {
      toast.error(MSG_UNSUPPORTED_PICK);
    }
    if (duplicateFiles.length > 0) {
      toast.error(msgDuplicate(duplicateFiles));
    }

    const newBlocks = uniqueFiles.length
      ? await Promise.all(uniqueFiles.map(fileToContentBlock))
      : [];
    setContentBlocks((prev) => [...prev, ...newBlocks]);
    e.target.value = "";
  };

  // Drag and drop handlers
  useEffect(() => {
    if (!dropRef.current) return;

    // Global drag events with counter for robust dragOver state
    const handleWindowDragEnter = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        dragCounter.current += 1;
        setDragOver(true);
      }
    };
    const handleWindowDragLeave = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        dragCounter.current -= 1;
        if (dragCounter.current <= 0) {
          setDragOver(false);
          dragCounter.current = 0;
        }
      }
    };
    const handleWindowDrop = async (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounter.current = 0;
      setDragOver(false);

      if (!e.dataTransfer) return;

      const files = Array.from(e.dataTransfer.files);
      const validFiles = files.filter((file) =>
        isSupportedFile(file),
      );
      const invalidFiles = files.filter(
        (file) => !isSupportedFile(file),
      );
      const duplicateFiles = validFiles.filter((file) =>
        isDuplicate(file, contentBlocks),
      );
      const uniqueFiles = validFiles.filter(
        (file) => !isDuplicate(file, contentBlocks),
      );

      if (invalidFiles.length > 0) {
        toast.error(MSG_UNSUPPORTED_PICK);
      }
      if (duplicateFiles.length > 0) {
        toast.error(msgDuplicate(duplicateFiles));
      }

      const newBlocks = uniqueFiles.length
        ? await Promise.all(uniqueFiles.map(fileToContentBlock))
        : [];
      setContentBlocks((prev) => [...prev, ...newBlocks]);
    };
    const handleWindowDragEnd = (e: DragEvent) => {
      dragCounter.current = 0;
      setDragOver(false);
    };
    window.addEventListener("dragenter", handleWindowDragEnter);
    window.addEventListener("dragleave", handleWindowDragLeave);
    window.addEventListener("drop", handleWindowDrop);
    window.addEventListener("dragend", handleWindowDragEnd);

    // Prevent default browser behavior for dragover globally
    const handleWindowDragOver = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("dragover", handleWindowDragOver);

    // Remove element-specific drop event (handled globally)
    const handleDragOver = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(true);
    };
    const handleDragEnter = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(true);
    };
    const handleDragLeave = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);
    };
    const element = dropRef.current;
    element.addEventListener("dragover", handleDragOver);
    element.addEventListener("dragenter", handleDragEnter);
    element.addEventListener("dragleave", handleDragLeave);

    return () => {
      element.removeEventListener("dragover", handleDragOver);
      element.removeEventListener("dragenter", handleDragEnter);
      element.removeEventListener("dragleave", handleDragLeave);
      window.removeEventListener("dragenter", handleWindowDragEnter);
      window.removeEventListener("dragleave", handleWindowDragLeave);
      window.removeEventListener("drop", handleWindowDrop);
      window.removeEventListener("dragend", handleWindowDragEnd);
      window.removeEventListener("dragover", handleWindowDragOver);
      dragCounter.current = 0;
    };
  }, [contentBlocks]);

  const removeBlock = (idx: number) => {
    setContentBlocks((prev) => prev.filter((_, i) => i !== idx));
  };

  const resetBlocks = () => setContentBlocks([]);

  /**
   * Handle paste event for files (images, PDFs)
   * Can be used as onPaste={handlePaste} on a textarea or input
   */
  const handlePaste = async (
    e: React.ClipboardEvent<HTMLTextAreaElement | HTMLInputElement>,
  ) => {
    const items = e.clipboardData.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length === 0) {
      return;
    }
    e.preventDefault();
    const validFiles = files.filter((file) =>
      isSupportedFile(file),
    );
    const invalidFiles = files.filter(
      (file) => !isSupportedFile(file),
    );
    const isDuplicate = (file: File) => {
      if (isDxfFile(file)) {
        return contentBlocks.some(
          (b) => b.type === "file" && b.metadata?.filename === file.name,
        );
      }
      if (file.type === "application/pdf") {
        return contentBlocks.some(
          (b) =>
            b.type === "file" &&
            b.mimeType === "application/pdf" &&
            b.metadata?.filename === file.name,
        );
      }
      if (SUPPORTED_FILE_TYPES.includes(file.type)) {
        // 🔴 图片这一支**只比文件名,不比 mimeType** —— 与本文件上方 hook 体里那份
        //    `isDuplicate` 的图片分支**必须逐字一致**(上游把这段抄了两份:那份给
        //    选文件 / 拖拽用,这份给粘贴用)。**两份要一起改**,漏一份的表现是
        //    「选文件查得出重复、粘贴查不出」,没有报错,要走两条不同交互才复现。
        //
        //    为什么不能比 mime:`image-compress.ts` 收 PNG、出 JPEG,block 里躺着
        //    `image/jpeg` 而重新粘进来的 File 还是 `image/png`,拿 mime 比必然漏判。
        //    漏判 = 同一张图登记两份产物、识图跑两遍、钱花两份,且一声不吭。
        //    完整推演(含「同名不同格式会被判成重复」这个**接受的取舍**)写在上面
        //    那份里,别只看这几行。
        return contentBlocks.some(
          (b) => b.type === "image" && b.metadata?.name === file.name,
        );
      }
      return false;
    };
    const duplicateFiles = validFiles.filter(isDuplicate);
    const uniqueFiles = validFiles.filter((file) => !isDuplicate(file));
    if (invalidFiles.length > 0) {
      toast.error(MSG_UNSUPPORTED_PASTE);
    }
    if (duplicateFiles.length > 0) {
      toast.error(msgDuplicate(duplicateFiles));
    }
    if (uniqueFiles.length > 0) {
      const newBlocks = await Promise.all(uniqueFiles.map(fileToContentBlock));
      setContentBlocks((prev) => [...prev, ...newBlocks]);
    }
  };

  return {
    contentBlocks,
    setContentBlocks,
    handleFileUpload,
    dropRef,
    removeBlock,
    resetBlocks,
    dragOver,
    handlePaste,
  };
}
