/**
 * lang-lib.ts(scripts/frontend-overrides/)的单测。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *
 * 锁的都是**静默出错**的东西:
 *   · 用户发言被转 → human.tsx 的 refPattern 匹配不上 → 照片不渲染成图,退回裸 hex,零报错;
 *   · 「显式优先」被改成「永远往回扫」→ 用户点了 English 却仍收到简体,按钮像没反应;
 *   · 目标语是简体时也走转换 → 空转,而且把 opencc 那 438 KB 拉给了不需要的人;
 *   · 判别字集里混进歧义字 → 误判,而误判的表现只是「答话语言偶尔不对」。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  DEFAULT_LANG,
  detectInput,
  HANS_CHARS,
  HANT_CHARS,
  HANT_EXTRA,
  LANGS,
  resolveLang,
  SCRIPT_PAIRS,
  shouldConvert,
  userTypedText,
} from "../frontend-overrides/lang-lib";
// 判「简体侧是不是真的简体」必须用真转换器,不能用字表 —— 理由见下面那条
// 「每个字对的简体侧在 hk 档下必须真的会变」。
import { s2hk } from "./hant-scan.mjs";

// ---------------------------------------------------------------------------
// 判别字集本身的完整性
// ---------------------------------------------------------------------------

describe("判别字集", () => {
  it("每一项恰好两个字(左简右繁)", () => {
    const bad = SCRIPT_PAIRS.filter((p) => [...p].length !== 2);
    expect(bad).toEqual([]);
  });

  it("简体侧与繁體侧完全不相交 —— 相交就说明收进了两边通用的歧义字", () => {
    const overlap = [...HANS_CHARS].filter((c) => HANT_CHARS.has(c));
    expect(overlap).toEqual([]);
  });

  it("两侧都没有重复字 —— 重复会让计数偏向那一边", () => {
    expect(HANS_CHARS.size).toBe(SCRIPT_PAIRS.length);
    // 繁體侧 = 字对右半 + 港台异形(HANT_EXTRA)。加法而不是等号,
    // 因为 hk 档下有些繁體字根本配不成对(见 lang-lib.ts 的 HANT_EXTRA 头注)。
    expect(HANT_CHARS.size).toBe(SCRIPT_PAIRS.length + HANT_EXTRA.length);
    // HANT_EXTRA 不许和字对右半重复 —— 重复了 Set 会吞掉,上面那条就白断言了
    const fromPairs = new Set(SCRIPT_PAIRS.map((p) => p[1]));
    for (const ch of HANT_EXTRA) {
      expect(fromPairs.has(ch), `${ch} 已经在字对右半里了,别在 HANT_EXTRA 再写一遍`).toBe(false);
    }
  });

  it("刻意排除的一简对多繁歧义字不在表里", () => {
    // 这些字**两边都在用**(繁體也写「干活」「后面」),收进来会误判。
    // 名单与 lang-lib.ts 头注那段互指,改一处两处一起。
    for (const ch of ["干", "发", "面", "里", "云", "后", "几", "复"]) {
      expect(HANS_CHARS.has(ch), `${ch} 不该在简体判别集里`).toBe(false);
      expect(HANT_CHARS.has(ch), `${ch} 不该在繁體判别集里`).toBe(false);
    }
  });

  it("HANS_CHARS / HANT_CHARS 是从 SCRIPT_PAIRS 派生的,不是手写的第二份", () => {
    expect([...HANS_CHARS].join("")).toBe(SCRIPT_PAIRS.map((p) => p[0]).join(""));
    expect([...HANT_CHARS].join("")).toBe(
      SCRIPT_PAIRS.map((p) => p[1]).join("") + HANT_EXTRA.join(""),
    );
  });

  /**
   * 🔴 这条是 2026-08-18 补的,补的原因是它当天抓出了一个**已上线的缺陷**。
   *
   * 本项目的转换档是 `cn → hk`(香港),而香港常用字字形表跟台湾不一样。
   * 字对表当初是按「台式繁體」凭印象写的,于是收进了一对**在香港根本不成立**的:
   *
   *     "户戶"   ← hk("户") === "户"  香港的繁體正文里就写「户」(「用户」)
   *
   * 后果:**港人打「用户」被算成一张简体票**,答话可能因此掉回简体。
   * 没有报错,只有偶尔「我明明打繁體它却用简体答」。
   *
   * 判据用真转换器,不用字表 —— 简体侧必须**真的会变**,才谈得上「简体独有」。
   */
  it("🔴 每个字对的简体侧,在 hk 档下必须真的会变(否则它不是简体独有字)", () => {
    const notSimplifiedOnly = SCRIPT_PAIRS.filter((p) => s2hk(p[0]) === p[0]);
    expect(
      notSimplifiedOnly,
      "这些字对的左半在 cn→hk 下原样不变,说明它在香港的繁體正文里也这么写 —— " +
        "收进 HANS_CHARS 会把港人的输入误判成简体。" +
        "要么删掉这一对,要么把繁體侧那个字挪进 HANT_EXTRA(「戶」就是这么处理的)。",
    ).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// detectInput —— 判别向量
// ---------------------------------------------------------------------------

describe("detectInput 判别向量", () => {
  it("繁體工地提问判成繁體", () => {
    expect(detectInput("幫我記一條:明天上午整改臨邊防護")).toBe("zh-Hant");
    expect(detectInput("今天還有哪些任務沒做完?")).toBe("zh-Hant");
    expect(detectInput("鋼筋複檢那條改到週五")).toBe("zh-Hant");
  });

  it("简体工地提问判成简体", () => {
    expect(detectInput("帮我记一条:明天上午整改临边防护")).toBe("zh-Hans");
    expect(detectInput("今天还有哪些任务没做完?")).toBe("zh-Hans");
  });

  it("英文提问判成英文", () => {
    expect(detectInput("What tasks are still open today?")).toBe("en");
    expect(detectInput("Add a task: fix the edge protection")).toBe("en");
  });

  it("🔴 简繁同形的短句判不出来 —— 这是常态,返回 null 交给兜底链", () => {
    // 「好」「收到」在简繁里逐字相同;8 类违规项里也有一半同形。
    for (const s of ["好", "好的", "收到", "未戴安全帽", "消防通道堵塞"]) {
      expect(detectInput(s), `「${s}」应判不出来`).toBeNull();
    }
  });

  it("空串 / 纯数字 / 纯符号 判不出来,且不抛", () => {
    for (const s of ["", "   ", "12345", "!!!???", "T1 T2 T3"]) {
      expect(detectInput(s)).toBeNull();
    }
  });

  it("简繁混排按多数决 —— 粘贴来的文本会这样", () => {
    // 繁體侧 3 个判别字(這/個/務)对简体侧 2 个(这/条)
    expect(detectInput("這個任務 和 这条")).toBe("zh-Hant");
    // 反过来:简体侧 3 个(这/个/务)对繁體侧 2 个(這/條)
    expect(detectInput("这个任务 和 這條")).toBe("zh-Hans");
  });

  it("打平时判不出来 —— 不许偏向任何一边", () => {
    // 繁體侧 2(這/個)vs 简体侧 2(这/条):打平
    expect(detectInput("這個 和 这条")).toBeNull();
  });

  it("汉字优先于拉丁 —— 中文里夹英文缩写不该判成英文", () => {
    expect(detectInput("把 T1 這條銷了")).toBe("zh-Hant");
    expect(detectInput("把 T1 这条销了")).toBe("zh-Hans");
  });

  it("null / undefined 不抛", () => {
    expect(detectInput(undefined as unknown as string)).toBeNull();
    expect(detectInput(null as unknown as string)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// resolveLang —— 三层兜底链,顺序不许反
// ---------------------------------------------------------------------------

describe("resolveLang 兜底链", () => {
  it("🔴 显式选择优先于往回扫 —— 用户点了按钮系统不许当没看见", () => {
    // 全程简体聊了十轮,点了 English,再打「ok」。
    // 只往回扫的话会找到十轮简体,把显式选择覆盖掉。
    const tenSimplified = Array.from({ length: 10 }, () => "帮我记一条任务");
    expect(resolveLang("en", ["ok", ...tenSimplified])).toBe("en");
    expect(resolveLang("zh-Hant", tenSimplified)).toBe("zh-Hant");
  });

  it("没显式选择时,末条判得出来就用末条", () => {
    expect(resolveLang(null, ["今天還有哪些任務?", "帮我记一条"])).toBe("zh-Hant");
  });

  it("🔴 末条判不出来就往回扫 —— 第二句只打「好」要沿用上一句的语言", () => {
    expect(resolveLang(null, ["好", "幫我記一條:整改臨邊防護"])).toBe("zh-Hant");
    expect(resolveLang(null, ["好", "帮我记一条:整改临边防护"])).toBe("zh-Hans");
  });

  it("整条线程都判不出来 → 落 DEFAULT_LANG", () => {
    expect(resolveLang(null, ["好", "收到", "12345"])).toBe(DEFAULT_LANG);
  });

  it("线程为空 → 落 DEFAULT_LANG(首条是纯图片时就是这样)", () => {
    expect(resolveLang(null, [])).toBe(DEFAULT_LANG);
  });

  it("DEFAULT_LANG 是繁體(定案 #1:香港官方语言 + 本地工友为主)", () => {
    expect(DEFAULT_LANG).toBe("zh-Hant");
  });

  it("LANGS 三个值齐全且不重复", () => {
    expect([...LANGS].sort()).toEqual(["en", "zh-Hans", "zh-Hant"]);
  });
});

// ---------------------------------------------------------------------------
// shouldConvert —— 两条硬约束
// ---------------------------------------------------------------------------

describe("shouldConvert 硬约束", () => {
  it("🔴 用户发言恒不转 —— 转了 human.tsx 的照片编号正则就匹配不上", () => {
    // (照片编号:…) 是后端 uploads.py 拼进去的简体;转成「照片編號」之后
    // refPattern 抓不到 → 照片不渲染成图,退回一段裸 hex,而且零报错。
    for (const lang of LANGS) {
      expect(shouldConvert("human", lang), `human + ${lang} 必须为 false`).toBe(false);
    }
  });

  it("🔴 目标语是简体时不转 —— 空转,而且会把 438 KB 的 opencc 拉给不需要的人", () => {
    expect(shouldConvert("ai", "zh-Hans")).toBe(false);
  });

  it("目标语是英文时不转 —— 英文走提示词那条路,不是字形转换(第二批)", () => {
    expect(shouldConvert("ai", "en")).toBe(false);
  });

  it("只有「AI 消息 + 繁體」这一种组合要转", () => {
    expect(shouldConvert("ai", "zh-Hant")).toBe(true);

    const combos = (["human", "ai"] as const).flatMap((role) =>
      LANGS.map((lang) => [role, lang, shouldConvert(role, lang)] as const),
    );
    expect(combos.filter(([, , yes]) => yes)).toEqual([["ai", "zh-Hant", true]]);
  });
});

// ---------------------------------------------------------------------------
// 后端拼进用户消息的东西,判语种时一律不算(2026-08-18 真人试用抓到)
// ---------------------------------------------------------------------------

describe("userTypedText:只留用户自己打的字", () => {
  const REF = "(图纸编号:0123456789abcdef0123456789abcdef)";
  /** 队友 2026-08-18 把 _PDF_HINT 从 2 句扩成 4 句,简体票从 2 涨到 28。 */
  const PDF_HINT =
    "(你传的是 PDF。聊天窗口当场看图暂时只认 DXF。如果这是**图纸**,请用右上角" +
    "「📂 资料归档」面板上传 —— 那里图纸支持 PDF 和 DXF,归档进去后就能查图层/构件、" +
    "出预览、读图上文字。如果 PDF 里其实是现场照片,麻烦先截个图再传)";

  it("🔴 港式短句 + 传图纸,判别不许被翻成简体", () => {
    // 「图」「纸」都在 SCRIPT_PAIRS 里 —— 编号块凭空投两张简体票。
    for (const phrase of ["呢張係咩", "check 下呢張", "這張圖有冇問題"]) {
      expect(detectInput(phrase), `「${phrase}」本身该判繁體`).toBe("zh-Hant");
      expect(
        detectInput(`${phrase}\n${REF}`),
        `「${phrase}」拼上编号块之后判别变了 —— 编号块是后端拼的,不该参与判别`,
      ).toBe("zh-Hant");
    }
  });

  it("🔴 提示语比编号块狠得多 —— 一句 _PDF_HINT 能压过任何短句", () => {
    // 实测 28 张简体票。它跟在编号块**后面**,所以「在编号块处截断」才治得住。
    expect(detectInput(`呢張圖則睇下\n${REF} ${PDF_HINT}`)).toBe("zh-Hant");
    expect(
      detectInput(`呢張圖則睇下\n${REF} (这张图的格式暂时打不开,请转成 JPG 或 PNG 再传一次)`),
    ).toBe("zh-Hant");
  });

  it("🔴 什么都不打只传附件 → 判不出,落默认繁體(而不是被后端那句开场白带成简体)", () => {
    // uploads.py 在用户没打字时**自己编一句**:「看看这张照片。」——「这」「张」都是简体判别字。
    // 判据是「编号块前面没有换行 = 那句不是用户打的」。
    expect(detectInput("看看这张照片。(照片编号:abc)")).toBeNull();
    expect(detectInput("看看这张图纸。(图纸编号:abc)")).toBeNull();
    expect(resolveLang(null, ["看看这张图纸。(图纸编号:abc)"])).toBe(DEFAULT_LANG);
  });

  it("用户自己打的字一个都不许丢", () => {
    expect(userTypedText(`睇下呢張圖\n${REF}`)).toBe("睇下呢張圖\n");
    expect(userTypedText("今天还有哪些任务没做完?")).toBe("今天还有哪些任务没做完?");
    // 全角括号 / 全角冒号,uploads.py 那几条正则都产得出来
    expect(userTypedText("帮我看看\n（照片编号：abc、def）有问题吗")).toBe("帮我看看\n");
  });

  it("🔴 同源:截断用的词必须与 human.tsx 的 refPattern 是同两个词", () => {
    // human.tsx 拿这两个词拼正则去捞编号渲染图片。哪天那边改了词(或后端 uploads.py
    // 改了措辞),这条会红 —— 否则表现只是「偶尔判错语种」,永远查不到这儿。
    const human = readFileSync(
      fileURLToPath(new URL("../frontend-overrides/human.tsx", import.meta.url)),
      "utf8",
    );
    expect(human, "human.tsx 的 refPattern 形参词变了").toMatch(
      /refPattern\s*=\s*\(word:\s*"照片"\s*\|\s*"图纸"\)/,
    );
    for (const word of ["照片", "图纸"]) {
      expect(userTypedText(`前\n(${word}编号:abc)后`), `截不掉「${word}编号」`).toBe("前\n");
    }
  });
});
