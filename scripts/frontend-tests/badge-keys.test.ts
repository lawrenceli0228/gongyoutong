/**
 * 徽章受控词的繁體镜像 —— 拿**真转换器**钉住,不许手写漂了。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *
 * 为什么这条测试非有不可:
 *
 * `markdown-text.tsx` 的 STATUS_CHIPS 与 `tool-calls.tsx` 的 SEVERITY_CHIP
 * 都是**整格精确匹配**的表。W12 之后它们收简繁两套键,而繁體那一套是
 * lang-lib.ts 里的字面量 —— 一旦跟真转换器的输出不一致,表现是
 * **徽章静默不上色**:界面还在、文字还对、只是那一格没有颜色。
 * 没有别的东西会报错。
 *
 * ⚠️ 这里刻意用 `opencc-js/cn2t` 那个**全量**入口,和产品代码
 *    (hant-convert.tsx 的 `import("opencc-js/cn2t")`)是同一个 ——
 *    换成 core + 单字表的精简档会让本测试与线上转换器不是同一套判据,
 *    那时测试绿而线上错(实测精简档把「签发」转成「籤發」)。
 */

import { describe, expect, it } from "vitest";
// opencc-js 的 exports map 给 ./cn2t 也挂了 types(指向 types/full.d.ts),
// 所以这里不需要 @ts-expect-error —— 加了反而会被 tsc 判成多余指令。
import OpenCC from "opencc-js/cn2t";

import {
  SEVERITY_WORDS,
  SEVERITY_WORDS_HANT,
  TASK_STATUS_WORDS,
  TASK_STATUS_WORDS_HANT,
  withHantKeys,
} from "../frontend-overrides/lang-lib";

/** 与 hant-convert.tsx 里那句 Converter({ from: "cn", to: "hk" }) 完全一致。 */
const s2hk = (OpenCC as any).Converter({ from: "cn", to: "hk" }) as (
  s: string,
) => string;

describe("徽章受控词的繁體镜像", () => {
  it("状态四词:繁體项逐个等于 s2hk(简体项)", () => {
    expect(TASK_STATUS_WORDS_HANT).toEqual(TASK_STATUS_WORDS.map(s2hk));
  });

  it("定级四词:繁體项逐个等于 s2hk(简体项)", () => {
    expect(SEVERITY_WORDS_HANT).toEqual(SEVERITY_WORDS.map(s2hk));
  });

  it("两组等长 —— withHantKeys 按下标配对,长度不等会静默丢键", () => {
    expect(TASK_STATUS_WORDS_HANT.length).toBe(TASK_STATUS_WORDS.length);
    expect(SEVERITY_WORDS_HANT.length).toBe(SEVERITY_WORDS.length);
  });

  it("实际变形的就那三个 —— 其余同形(留档,变了要知道为什么)", () => {
    const changed = [...TASK_STATUS_WORDS, ...SEVERITY_WORDS].filter(
      (w) => s2hk(w) !== w,
    );
    expect(changed).toEqual(["没定期限", "较大", "待定级"]);
  });
});

describe("withHantKeys 加宽后的表", () => {
  const chips = withHantKeys(
    TASK_STATUS_WORDS,
    TASK_STATUS_WORDS_HANT,
    (w) => `style-of-${w}`,
  );

  it("简体键与繁體键都查得到,且指向同一个样式", () => {
    expect(chips["没定期限"]).toBe("style-of-没定期限");
    expect(chips["沒定期限"]).toBe("style-of-没定期限");
  });

  it("🔴 模型自己吐繁體时徽章仍上色 —— 这是 W12 之前就有的 bug", () => {
    // 探针实测模型有 25%-75% 的概率自发用繁體(方案 §4.5),
    // 所以「只认简体键」在 W12 之前就已经会静默失配了。
    for (const w of TASK_STATUS_WORDS_HANT) {
      expect(chips[w], `繁體「${w}」查不到样式`).toBeTruthy();
    }
    for (const w of TASK_STATUS_WORDS) {
      expect(chips[w], `简体「${w}」查不到样式`).toBeTruthy();
    }
  });

  it("表外的词查不到 —— 整格精确匹配的语义不许被加宽破坏", () => {
    expect(chips["随便一句话"]).toBeUndefined();
    expect(chips["已完成了"]).toBeUndefined();
  });

  it("同形词不会因为写两次而串样式", () => {
    // 「已完成」简繁同形 → withHantKeys 会写同一个键两次,值相同,无害。
    expect(chips["已完成"]).toBe("style-of-已完成");
  });
});

describe("转换器本身的行为(锁住产品依赖的那几条)", () => {
  it("🔴 全量档才转得对「簽發」「複查」—— 精简档会转错,不许换", () => {
    expect(s2hk("签发")).toBe("簽發");
    expect(s2hk("复查")).toBe("複查");
    expect(s2hk("复检")).toBe("複檢");
    expect(s2hk("下周三")).toBe("下週三");
  });

  it("8 类违规项转得对(它们出现在答话正文里)", () => {
    expect(s2hk("临边无防护")).toBe("臨邊無防護");
    expect(s2hk("材料堆放混乱")).toBe("材料堆放混亂");
    expect(s2hk("用电隐患")).toBe("用電隱患");
    expect(s2hk("动火作业无监护")).toBe("動火作業無監護");
    // 同形的两个:转完不变
    expect(s2hk("未戴安全帽")).toBe("未戴安全帽");
    expect(s2hk("消防通道堵塞")).toBe("消防通道堵塞");
  });

  it("编号 / T 号 / 英文原样不动", () => {
    expect(s2hk("GYT-20260817-123456")).toBe("GYT-20260817-123456");
    expect(s2hk("T1 T2 T3")).toBe("T1 T2 T3");
    expect(s2hk("transfer_to_schedule")).toBe("transfer_to_schedule");
  });

  it("不做**术语**替换 —— 那一层归 W11,现在只转字形", () => {
    // to.hk 不含地区词汇表(那是 to.hkp),所以「鼠标」只转字形不换词。
    expect(s2hk("鼠标")).toBe("鼠標");
    expect(s2hk("软件")).toBe("軟件");
  });
});
