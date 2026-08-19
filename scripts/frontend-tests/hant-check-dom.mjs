/**
 * 真机验收:把浏览器里**实际渲染出来的字**抓回来,逐条判还有没有简体。
 *
 *     node hant-check-dom.mjs < dom-text.json
 *
 * 输入是一个 JSON 字符串数组(浏览器里 `browse js` 抓出来的可见文本)。
 *
 * ===========================================================================
 * 为什么源码级守卫**不够**,必须再来一道 DOM 级的
 * ---------------------------------------------------------------------------
 * `hant-ui-strings.test.ts` 判的是**源码里的字面量**。屏幕上的字有三个来源,
 * 它只管得住第一个:
 *
 *   ① 源码里的字面量            ← 源码级守卫管得住
 *   ② 后端 Envelope 给的文本    ← 运行时才存在,源码里根本没有这串字
 *      (status_display / user_msg / due_display / doc_type_display)
 *   ③ 上游 agent-chat-ui 自带的、我们没覆盖的组件
 *
 * ②漏了的表现:繁體面板里冒出一句简体回执。
 * ③漏了的表现:某个上游按钮一直是简体(或英文),而覆盖件清单里查不到它 ——
 *   这类只有真机能发现,因为它压根不在 scripts/frontend-overrides/ 里。
 *
 * 所以这道是**结果导向**的:不管那个字从哪来,只要它在屏幕上就得是繁體。
 *
 * ⚠️ 跑这个之前必须确认页面上**没有对话内容**。用户发言与答话不适用这条判据 ——
 *    用户打简体就该显示简体(`shouldConvert` 对 human 恒 false),
 *    答话跟着用户走。混进来会得到一堆假阳性。
 */

import { readFileSync } from "node:fs";

import * as OpenCC from "opencc-js";

const s2hk = OpenCC.Converter({ from: "cn", to: "hk" });
const HAN = /[一-鿿]/;

/**
 * 允许留简体的界面文本 —— 与 `hant-keep-hans.mjs` 是两回事,别混。
 *
 * 那张表管的是**源码里的字面量**(值/键,大多根本不上屏);
 * 这张管的是**真上了屏、但按定案就该是简体**的字。目前只有一类:
 * 工地照片编号那种后端标记,以及从聊天框漏进来的用户内容。
 */
const ALLOW_ON_SCREEN = [
  /照片编号/, // 后端拼的标记,human.tsx 会把它渲染成图;裸露时说明图没渲染出来
  /图纸编号/,
];

const raw = readFileSync(0, "utf8");
/** @type {string[]} */
const texts = JSON.parse(raw);

const offenders = [];
for (const t of texts) {
  const s = (t ?? "").trim();
  if (!s || !HAN.test(s)) continue;
  if (ALLOW_ON_SCREEN.some((re) => re.test(s))) continue;
  const hk = s2hk(s);
  if (hk !== s) offenders.push({ text: s, hk });
}

if (offenders.length === 0) {
  process.stdout.write(`✅ 屏幕上 ${texts.length} 段文本,一条简体都没有\n`);
  process.exit(0);
}

process.stdout.write(`🔴 屏幕上还有 ${offenders.length} 段简体:\n\n`);
for (const o of offenders) {
  process.stdout.write(`  ${o.text}\n  → ${o.hk}\n\n`);
}
process.exit(1);
