/**
 * 附件直传契约的**跨语言镜像守卫**(2026-08-21)。
 *
 * ⚠️ 这个文件**不测行为,只对账**。被守的两样东西住在
 * `scripts/frontend-overrides/multimodal-utils.ts` 里,而那份**进不了这个测试包** ——
 * 它 import 了 `sonner` 和 `@/lib/image-compress`,而本包是零依赖、按相对路径直读的。
 * 所以这里退一步:**把两边的源码当文本读进来比对**,与 `image-compress.test.ts`
 * 里那条「直接读后端源码比真值」是同一种做法、同一个理由 ——
 * 只断自己这边的字面量,挡得住这边被改,**挡不住那边被改**,而后者一样会静默出错。
 *
 * ===========================================================================
 * 漂了会怎么坏
 * ---------------------------------------------------------------------------
 * 前端在取件时把附件 POST 到 `/attachments` 换一个 32 位编号,发送时消息里只带
 *
 *     {"type": "gyt_attachment", "outcome": "photo", "artifact_id": "<32位hex>"}
 *
 * 后端 `core/uploads.py` 的 `_rewrite` 按 `type` 认它、按 `outcome` 分六个桶。
 *
 * 🔴 **两边任一处改了名字,表现都不是报错**:后端认不出这个块 → 当成没有附件 →
 *    消息里一个字都不提那张照片 → **工友以为传上去了**,而模型压根不知道有图。
 *    前端那一半更隐蔽:块照发、后端照收,只是**退回了老路**(base64 进消息),
 *    于是这次优化想治的毛病悄悄复发 —— 界面、日志、测试全都不会说话。
 *
 * 所以这条守卫盯的是**名字**,不是行为。
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** 读后端源码。读不到就让测试红(不是 skip)—— 读不到本身就说明那边挪窝了。 */
function readBackend(relative: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../../backend/src/gyt/${relative}`, import.meta.url)),
    "utf8",
  );
}

/** 读前端覆盖件源码。同上:读不到就红。 */
function readOverride(name: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../frontend-overrides/${name}`, import.meta.url)),
    "utf8",
  );
}

const uploadsPy = readBackend("core/uploads.py");
const multimodalTs = readOverride("multimodal-utils.ts");

/**
 * 从 `uploads.py` 里抠出 `ATTACHMENT_OUTCOMES` 那个元组里的字符串。
 *
 * 抠不出来就抛,**不是回空数组** —— 回空数组的话下面每条断言都会「通过」
 * (空集合是任何集合的子集),那正是守卫最不该有的失败方式。
 */
function backendOutcomes(): string[] {
  // ⚠️ 别写成 `Final\[[^\]]*\]` —— 真声明是 `Final[tuple[str, ...]]`,**方括号是嵌套的**,
  //    那种写法在第一个 `]` 就停,后面对不上。用 `[^=]*` 跳到等号,与嵌套无关。
  const block = /ATTACHMENT_OUTCOMES[^=]*=\s*\(([\s\S]*?)\)/.exec(uploadsPy);
  if (!block) {
    throw new Error("uploads.py 里读不到 ATTACHMENT_OUTCOMES —— 那边改写法了?");
  }
  // 元组里写的是常量名(OUTCOME_PHOTO 那些),得再各自查一次它们的值。
  const names = block[1].match(/OUTCOME_[A-Z_]+/g) ?? [];
  if (names.length === 0) {
    throw new Error("ATTACHMENT_OUTCOMES 里一个 OUTCOME_* 都没读到 —— 那边改写法了?");
  }
  return names.map((name) => {
    const hit = new RegExp(`^${name}:\\s*Final\\[str\\]\\s*=\\s*"([^"]+)"`, "m").exec(uploadsPy);
    if (!hit) throw new Error(`uploads.py 里读不到常量 ${name} 的值`);
    return hit[1];
  });
}

function backendBlockType(): string {
  const hit = /ATTACHMENT_BLOCK_TYPE:\s*Final\[str\]\s*=\s*"([^"]+)"/.exec(uploadsPy);
  if (!hit) throw new Error("uploads.py 里读不到 ATTACHMENT_BLOCK_TYPE —— 那边改写法了?");
  return hit[1];
}

describe("🔴 附件直传:前端发的块与后端认的块同名", () => {
  it("块的 type 两边一致", () => {
    // Arrange
    const expected = backendBlockType();

    // Act:前端那份写在 toWireBlocks 里,是个字面量
    const present = multimodalTs.includes(`type: "${expected}"`);

    // Assert
    expect(
      present,
      `后端认的块 type 是 "${expected}",而 multimodal-utils.ts 里没有这个字面量 —— ` +
        "两边漂了的话后端会当成「这条消息没有附件」,工友以为传上去了而模型看不到图",
    ).toBe(true);
  });

  it("端点路径两边一致", () => {
    // Arrange:后端那条路由写在 upload_api.py 的 UPLOAD_ROUTES 里
    const uploadApiPy = readBackend("upload_api.py");
    const hit = /Route\(\s*"([^"]+)"/.exec(uploadApiPy);
    expect(hit, "upload_api.py 里读不到 Route(...) —— 那边改写法了?").not.toBeNull();
    const path = hit![1];

    // Act & Assert
    expect(
      multimodalTs.includes(`ATTACHMENT_PATH = "${path}"`),
      `后端路由是 ${path},前端 ATTACHMENT_PATH 对不上 —— 前端会拿到 404 然后` +
        "**安静退回老路**(base64 进消息),这次优化白做而没有任何东西会说话",
    ).toBe(true);
  });

  it("六个 outcome 后端有、前端不许自己另造一套", () => {
    // Arrange
    const outcomes = backendOutcomes();

    // Assert:先钉住数量与内容 —— 加了一个而不管 _rewrite,那种附件会被静默
    //        归进「格式不支持」(用户传了张好照片,却被告知请转成 JPG)
    expect(new Set(outcomes)).toEqual(
      new Set([
        "photo",
        "drawing",
        "unsupported",
        "pdf",
        "photo_too_large",
        "drawing_too_large",
      ]),
    );

    // 🔴 前端**刻意不复制这张表**:它拿到什么 outcome 就原样转发什么,
    //    人话一律由后端 _rewrite 拼(理由在 upload_api.py 模块头注)。
    //    所以这里反过来验:前端源码里**不许**出现这些值的字面量 ——
    //    一旦出现,就是有人开始在前端做分支判断了,那条「人话只有一份真相」
    //    的规矩当场破掉,而且两边的判据会各自漂。
    for (const outcome of outcomes) {
      expect(
        multimodalTs.includes(`"${outcome}"`),
        `multimodal-utils.ts 里出现了 outcome 字面量 "${outcome}" —— ` +
          "前端不该认识这些值,它只负责原样转发(见 upload_api.py 模块头注)",
      ).toBe(false);
    }
  });

  it("metadata 上那两个键是前端自己的,后端不认识它们", () => {
    // 编号是挂在块的 metadata 上带到 handleSubmit 的,**不进网络**:
    // toWireBlocks 会把它们摘出来拼成编号块。所以后端源码里不该有这两个名字 ——
    // 有的话说明有人把前端的中转结构当成了契约的一部分。
    for (const key of ["gyt_outcome", "gyt_artifact_id"]) {
      expect(
        multimodalTs.includes(`"${key}"`),
        `multimodal-utils.ts 里应当定义 ${key}`,
      ).toBe(true);
      expect(
        uploadsPy.includes(key),
        `uploads.py 里出现了 ${key} —— 那是前端块内部的中转键,不该是跨泳道契约`,
      ).toBe(false);
    }
  });
});
