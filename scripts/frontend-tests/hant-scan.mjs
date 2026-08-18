/**
 * 覆盖件里的中文串扫描器 —— 界面繁體化(W12 第一批之二)的地基。
 *
 * 两个消费者共用这一份:
 *   ① CLI   `node hant-scan.mjs`          —— 出清册,给人看、给 subagent 分工
 *   ② 守卫  `hant-ui-strings.test.ts`      —— 钉住「所有简体串必须登记」
 *
 * ===========================================================================
 * 🔴 为什么用 TypeScript 的 AST 而不是正则
 * ---------------------------------------------------------------------------
 * 本仓已经栽过一次**假守卫**:knowledge 那条断言写成
 * `any(re.search(shape, body))`,结果提示词里的中文**示例**自己把断言满足了,
 * 把模板真翻成英文测试照绿(见 test_knowledge_tools.py 那段实录)。
 *
 * 中文串这件事上正则会以同样的方式骗人:本仓注释密度极高且全是中文,
 * 正则分不清「注释里的中文」和「会上屏的中文」。分不清就只有两个下场 ——
 * 要么把注释也算进来(守卫永远红,最后被人删掉),
 * 要么粗暴剥注释(剥漏一种写法就静默漏扫)。
 *
 * `ts.createSourceFile` 把这件事变成事实:字符串字面量、模板串、JSX 文本
 * 是 AST 节点,注释根本不在其中。
 *
 * ===========================================================================
 * 判据只有一条:`s2hk(s) === s`
 * ---------------------------------------------------------------------------
 * 「这个串是不是已经繁體」不拿字表判,拿**真转换器**判 —— 转完等于自己就是
 * 已经到位(繁體,或者简繁同形如「超期」「全部」「重大」)。
 *
 * 这样做的好处是它天然覆盖一简对多繁:手打的「復查」会被判成不到位
 * (真值是「複查」),而拿 HANS_CHARS 那种字表判根本看不出来 ——
 * 「復」本来就是繁體字形。
 */

import { readFileSync, readdirSync } from "node:fs";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import * as OpenCC from "opencc-js";

/** 简 → 繁(香港标准)。与 hant-convert.tsx 里跑在浏览器上的那一档同参。 */
export const s2hk = OpenCC.Converter({ from: "cn", to: "hk" });

const HAN = /[一-鿿]/;

const OVERRIDES_DIR = fileURLToPath(
  new URL("../frontend-overrides", import.meta.url),
);

/**
 * 节点在语法树上的位置 —— 决定它「会不会上屏」。
 *
 * 这里只做**事实描述**,不做判决:工具报事实,该不该转由人和登记表定。
 * 自动判「这条是标签还是键」一定会错,而错的方向是静默的。
 */
function describeContext(node) {
  const p = node.parent;
  if (!p) return "toplevel";

  if (ts.isJsxText(node)) return "jsx-text";

  // <Foo title="中文"> / <Foo aria-label="中文">
  if (ts.isJsxAttribute(p) && p.name) return `jsx-attr:${p.name.getText()}`;
  if (ts.isJsxExpression(p)) return "jsx-expr";

  // { key: "中文" } —— 值;{ "中文": … } —— 键
  if (ts.isPropertyAssignment(p)) {
    return p.name === node ? "object-key" : `object-value:${p.name.getText()}`;
  }

  // ["中文", …] 常量数组:受控词表最爱这么写
  if (ts.isArrayLiteralExpression(p)) return "array-item";

  // foo === "中文" / "中文" === foo
  if (ts.isBinaryExpression(p)) return `compare:${p.operatorToken.getText()}`;

  // s.includes("中文") / new RegExp("中文") / params.set("scope", "中文")
  if (ts.isCallExpression(p)) {
    const callee = p.expression.getText();
    const idx = p.arguments.indexOf(node);
    return `call:${callee}#${idx}`;
  }

  if (ts.isVariableDeclaration(p)) return `const:${p.name.getText()}`;
  if (ts.isReturnStatement(p)) return "return";
  if (ts.isTemplateSpan(p) || ts.isTemplateExpression(p)) return "template";
  if (ts.isAsExpression(p)) return describeContext(p);
  if (ts.isParenthesizedExpression(p)) return describeContext(p);

  return ts.SyntaxKind[p.kind];
}

/** 扫一个文件,返回所有含汉字的字面量 / JSX 文本。注释不在 AST 里,天然不入选。 */
export function scanFile(filePath) {
  const text = readFileSync(filePath, "utf8");
  const sf = ts.createSourceFile(
    filePath,
    text,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

  const out = [];
  const visit = (node) => {
    let raw = null;
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node)
    ) {
      raw = node.text;
    } else if (ts.isJsxText(node)) {
      raw = node.text;
    }

    if (raw != null && HAN.test(raw)) {
      const value = ts.isJsxText(node) ? raw.trim() : raw;
      if (value) {
        const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
        const hk = s2hk(value);
        out.push({
          file: basename(filePath),
          line: line + 1,
          value,
          hk,
          changed: hk !== value,
          context: describeContext(node),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return out;
}

/** 扫整个 frontend-overrides/。 */
export function scanAll(dir = OVERRIDES_DIR) {
  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
    .sort();
  return files.flatMap((f) => scanFile(join(dir, f)));
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

const isMain = process.argv[1] && import.meta.url.endsWith(basename(process.argv[1]));
if (isMain) {
  const rows = scanAll();
  const changed = rows.filter((r) => r.changed);
  const mode = process.argv[2] ?? "summary";

  if (mode === "json") {
    process.stdout.write(JSON.stringify(changed, null, 1));
  } else if (mode === "list") {
    for (const r of changed) {
      process.stdout.write(
        `${r.file}:${r.line}\t[${r.context}]\t${r.value}\t→\t${r.hk}\n`,
      );
    }
  } else {
    const byFile = new Map();
    for (const r of changed) byFile.set(r.file, (byFile.get(r.file) ?? 0) + 1);
    process.stdout.write(
      `含汉字的串共 ${rows.length} 条,其中 ${changed.length} 条转换后会变形(= 还是简体):\n\n`,
    );
    for (const [f, n] of [...byFile].sort((a, b) => b[1] - a[1])) {
      process.stdout.write(`${String(n).padStart(4)}  ${f}\n`);
    }
    const byCtx = new Map();
    for (const r of changed) {
      const k = r.context.split(":")[0];
      byCtx.set(k, (byCtx.get(k) ?? 0) + 1);
    }
    process.stdout.write(`\n按语法位置:\n`);
    for (const [c, n] of [...byCtx].sort((a, b) => b[1] - a[1])) {
      process.stdout.write(`${String(n).padStart(4)}  ${c}\n`);
    }
  }
}
