/**
 * 逐条比对「这一批繁體化改动」是不是**工具转出来的** —— 治手打错形。
 *
 *     node hant-verify-diff.mjs <base-ref>        # 例:node hant-verify-diff.mjs main
 *
 * ===========================================================================
 * 为什么单靠 hant-ui-strings.test.ts 不够
 * ---------------------------------------------------------------------------
 * 那条守卫的判据是 `s2hk(v) === v`,它抓的是「**还是简体**」。
 * 抓不住「转成了错的繁體」:
 *
 *     s2hk("籤發") === "籤發"     ← 全是繁體字形,简→繁转换器不碰,守卫放行
 *                                   而正确写法是「簽發」
 *
 * 而「復」本身是合法繁體字(「工程復工令」就该写「復」),所以也**不能靠黑名单**。
 * 唯一可靠的判据是**溯源**:新值必须恰好等于 `s2hk(旧值)`。
 *
 * 这就是本文件干的事 —— 拿 base ref 的同一个文件当旧值,逐条配对。
 *
 * ===========================================================================
 * 配对逻辑
 * ---------------------------------------------------------------------------
 *     removed = 旧有新无        added = 新有旧无
 *     对每个 removed r:若 s2hk(r) ∈ added → 配上,双方出列(这是一次合规转换)
 *     剩下的全部报出来:
 *       · removed 没配上 → 这条被改成了**别的东西**(手打?顺手改文案?)或被删了
 *       · added  没来源 → 凭空多出来的串(新写的文案 → 正常;手打的繁體 → 要查)
 *
 * ⚠️ 剩菜不等于错。新写一句文案就会出现在「added 没来源」里。
 *    这件工具的作用是把「几百条改动」压成「十几条要人看一眼的」,不是自动判罪。
 */

import { execFileSync } from "node:child_process";
import { readdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { basename, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

import { scanFile, s2hk } from "./hant-scan.mjs";
import { TOOL_MISCONVERT } from "./hant-keep-hans.mjs";

/**
 * 把 `s2hk` 的产出再过一遍「工具选错字」登记表。
 *
 * 不这么做的话,溯源器会把**正确的人工修正**报成剩菜:
 * `s2hk("…签错…")` 给的是「籤錯」,而盘上写的是「簽錯」(签字的签,才对)。
 * 报成剩菜的下场不是没人管,而是有人照工具回改 —— 那正好把对的改错。
 */
function expectedHant(oldValue) {
  let want = s2hk(oldValue);
  for (const fix of Object.values(TOOL_MISCONVERT)) {
    if (want.includes(fix.wrong)) want = want.replaceAll(fix.wrong, fix.right);
  }
  return want;
}

const OVERRIDES_REL = "scripts/frontend-overrides";
const OVERRIDES_DIR = fileURLToPath(
  new URL("../frontend-overrides", import.meta.url),
);
const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));

const baseRef = process.argv[2];
if (!baseRef) {
  process.stderr.write("用法:node hant-verify-diff.mjs <base-ref>\n");
  process.exit(2);
}

/** 把 base ref 里的那份取出来落到临时文件,好让 AST 扫描器照常吃。 */
function scanAtRef(ref, relPath, tmp) {
  let text;
  try {
    text = execFileSync("git", ["show", `${ref}:${relPath}`], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
    });
  } catch {
    return null; // base 里没有这个文件 = 本批新增
  }
  const tmpFile = join(tmp, basename(relPath));
  writeFileSync(tmpFile, text, "utf8");
  return scanFile(tmpFile);
}

const tmp = mkdtempSync(join(tmpdir(), "gyt-hant-"));
const files = readdirSync(OVERRIDES_DIR)
  .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
  .sort();

let pairs = 0;
const orphanRemoved = [];
const orphanAdded = [];

for (const f of files) {
  const rel = `${OVERRIDES_REL}/${f}`;
  const before = scanAtRef(baseRef, rel, tmp);
  if (before === null) continue; // 新文件,没有旧值可比
  const after = scanFile(join(OVERRIDES_DIR, f));

  const oldVals = new Set(before.map((r) => r.value));
  const newVals = new Set(after.map((r) => r.value));

  const removed = [...oldVals].filter((v) => !newVals.has(v));
  const added = new Set([...newVals].filter((v) => !oldVals.has(v)));

  for (const r of removed) {
    const want = expectedHant(r);
    if (added.has(want)) {
      added.delete(want);
      pairs += 1;
    } else {
      orphanRemoved.push({ file: f, value: r, want });
    }
  }
  for (const a of added) orphanAdded.push({ file: f, value: a });
}

process.stdout.write(`基线 ${baseRef}\n\n`);
process.stdout.write(`✅ 配上的转换(新值 === s2hk(旧值)):${pairs} 条\n`);
process.stdout.write(`⚠️  旧串没配上:${orphanRemoved.length} 条\n`);
process.stdout.write(`⚠️  新串没来源:${orphanAdded.length} 条\n`);

if (orphanRemoved.length) {
  process.stdout.write(`\n── 旧串没配上(被改成了别的东西,或被删了)──\n`);
  for (const o of orphanRemoved) {
    process.stdout.write(`  ${o.file}\n    旧:${o.value}\n    应:${o.want}\n`);
  }
}
if (orphanAdded.length) {
  process.stdout.write(`\n── 新串没来源(新写的文案?还是手打的繁體?)──\n`);
  for (const o of orphanAdded) process.stdout.write(`  ${o.file}\t${o.value}\n`);
}

// 退出码只表示「有没有要人看的剩菜」,不表示对错 —— 见头注最后一段。
process.exit(orphanRemoved.length + orphanAdded.length > 0 ? 1 : 0);
