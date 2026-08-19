/**
 * 界面文案繁體化的守卫 —— 「默认繁體、例外登记」。
 *
 * 判据只有一条:覆盖件里每个含汉字的字面量 / JSX 文本,要么 `s2hk(v) === v`
 * (已是繁體,或简繁同形),要么在 `hant-keep-hans.mjs` 里登记了理由。
 *
 * ===========================================================================
 * 这条守卫替掉的是什么
 * ---------------------------------------------------------------------------
 * 替掉的是「转的时候小心点」。界面繁體化会一次性动几百个串,而漏掉一个的表现
 * 是**繁體界面里冒出一个简体词** —— 没有报错、测试全绿、只有香港工友会觉得别扭
 * 但说不上来哪里别扭。这种缺陷靠 review 是抓不住的。
 *
 * ⚠️ 扫描走 TypeScript 的 AST(`hant-scan.mjs`),不走正则。本仓注释密度极高
 *    且全是中文,正则分不清注释和文案;而注释按本仓约定**就该是简体**。
 *
 * ===========================================================================
 * 🔴 这条守卫**抓不住什么** —— 写在这儿免得下一个人高估它
 * ---------------------------------------------------------------------------
 * 它抓的是「**还是简体**」。它抓不住「**转成了错的繁體**」:
 *
 *     s2hk("籤發") === "籤發"      ← 原样返回,守卫放行
 *
 * 因为「籤發」已经全是繁體字形,简→繁转换器根本不碰它 —— 而正确写法是「簽發」。
 * (本文件第一版的 docstring 就吹过「天然覆盖一简对多繁」,是过头话,当场被
 *  自己的用例证伪。留着这段是为了别让人再吹一次。)
 *
 * 手打错形由**另一件**兜:`node hant-verify-diff.mjs <base-ref>` ——
 * 它逐条比对本分支改过的中文串,要求 `新值 === s2hk(旧值)`,
 * 也就是**只认工具转出来的结果**。改完批量转换必须跑它,见 W12 方案 §5.3。
 */

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  findConversionCallArgs,
  findRuntimeConversionCalls,
  scanAll,
  s2hk,
} from "./hant-scan.mjs";
import {
  FILE_EXEMPT,
  KEEP_HANS,
  TOOL_MISCONVERT,
  isExempt,
} from "./hant-keep-hans.mjs";

describe("界面文案:默认繁體、例外登记", () => {
  it("覆盖件里不许有没登记的简体串", () => {
    const offenders = scanAll()
      .filter((r) => r.changed)
      .filter((r) => !isExempt(r.file, r.value, r.context));

    const report = offenders
      .slice(0, 40)
      .map((r) => `  ${r.file}:${r.line} [${r.context}]  ${r.value}  →  ${r.hk}`)
      .join("\n");

    expect(
      offenders.length,
      offenders.length === 0
        ? ""
        : `还有 ${offenders.length} 条简体串没处理。每条二选一:\n` +
          `  · 只是给人看的字 → 改成右边那个繁體形(**用工具转,不许手打**:\n` +
          `      cd scripts/frontend-tests && node hant-scan.mjs list)\n` +
          `  · 是送后端的值 / 跟后端返回值比的 / 当对象键的 / 匹配后端拼串的\n` +
          `    → 写进 hant-keep-hans.mjs 并说明理由,另起一张标签表上屏\n\n` +
          report +
          (offenders.length > 40 ? `\n  …还有 ${offenders.length - 40} 条` : ""),
    ).toBe(0);
  });

  it("登记表里的每条豁免都得说得出理由,而且要卡住语法位置", () => {
    for (const [file, why] of Object.entries(FILE_EXEMPT)) {
      expect(why.length, `${file} 的整份豁免没写理由`).toBeGreaterThan(20);
    }
    for (const [file, entries] of Object.entries(KEEP_HANS)) {
      for (const [value, entry] of Object.entries(entries)) {
        const why = typeof entry === "string" ? entry : entry.why;
        expect(why?.length, `${file} 的「${value}」没写理由`).toBeGreaterThan(20);

        // 老格式(纯字符串)= 整个文件里所有同名串一起豁免。它的粒度缺陷已经
        // 实测出过事(见 hant-keep-hans.mjs 头注里 human.tsx「图纸」那段),
        // 所以只在**这个串在本文件里只出现在一种语法位置**时才允许。
        if (typeof entry === "string") {
          const positions = new Set(
            scanAll()
              .filter((r) => r.file === file && r.value === value)
              .map((r) => r.context),
          );
          expect(
            positions.size,
            `${file} 的「${value}」用的是整串豁免,但它在这个文件里出现在 ` +
              `${positions.size} 种语法位置(${[...positions].join(" / ")})—— ` +
              `整串豁免会把其中的**显示标签**一起盖住,守卫永远不会报它。` +
              `改成 { why, contexts:[…] } 卡住位置。`,
          ).toBeLessThanOrEqual(1);
        } else {
          expect(
            entry.contexts?.length,
            `${file} 的「${value}」写成了对象格式却没给 contexts`,
          ).toBeGreaterThan(0);
        }
      }
    }
  });

  /**
   * 🔴 这条盯的是另一个方向的错,前两条都盯不住它。
   *
   * 守卫①盯「还是简体」—— 一个串被转成繁體之后它就不管了。
   * 溯源器盯「新值 === s2hk(旧值)」—— 转换合规它也放行。
   * 于是有一类错**两边都漏**:
   *
   *     takeRefs(raw, "图纸")   →   takeRefs(raw, "圖紙")
   *
   * 转得完全合规,守卫看它是繁體也满意 —— 而它是拿去匹配后端拼进消息的
   * 简体标记的,匹配不上,图片退回一段裸 hex,**页面照开、控制台干净**。
   *
   * 判据:中文串出现在「参与匹配 / 传参 / 当键」的语法位置,却没在登记表里
   * = 它要么该留简体并登记,要么这个位置压根不该有中文字面量。
   */
  it("参与匹配/传参的位置上,不许有没登记的中文串", () => {
    const RISKY = [
      /^call:.*\.(includes|startsWith|endsWith|indexOf|lastIndexOf|match|test|split|replace|replaceAll)#/,
      /^call:.*\.(set|append|has|get|delete)#/, // URLSearchParams / Map / Headers
      /^compare:(===|!==|==|!=)$/,
      /^object-key$/,
      /^LiteralType$/, // `word: "图纸" | "照片"` 这种类型位置 —— 它约束的是实参
    ];

    const flagged = scanAll()
      .filter((r) => RISKY.some((re) => re.test(r.context)))
      .filter((r) => !isExempt(r.file, r.value, r.context));

    expect(
      flagged.map((r) => `${r.file}:${r.line} [${r.context}] ${r.value}`),
      "这些中文串在**参与匹配或传参**,却没登记豁免。二选一:\n" +
        "  · 它确实是「值」(要送后端 / 要跟后端返回值比 / 要匹配后端拼的串)\n" +
        "    → 把它改回简体,并写进 hant-keep-hans.mjs 说明理由\n" +
        "  · 它其实只是个标签,那这个位置不该拿它做匹配 —— 去改代码结构\n" +
        "🔴 最常见的成因:批量繁體化时顺手把它一起转了(转得很合规,所以没人拦)。",
    ).toEqual([]);
  });

  /**
   * 🔴 登记表里不许出现重复的文件名键。
   *
   * JS 对象字面量的重复键**不报错也不警告**,后写的静默覆盖先写的。
   * 2026-08-18 当天就中招:两个人各自往 `KEEP_HANS` 里加了 `"supervision.tsx"`,
   * 于是先写的那一整组(四个徽章键)被整块吞掉,而**唯一的症状**是守卫报
   * 「这四个串没登记」—— 人会以为自己忘了写,回去再写一遍,再被吞一次。
   *
   * 运行时查不出来(读到的对象已经合并完了),所以只能扫**源码文本**。
   */
  it("🔴 登记表里不许有重复的文件名键(JS 会静默覆盖)", () => {
    const src = readFileSync(
      fileURLToPath(new URL("./hant-keep-hans.mjs", import.meta.url)),
      "utf8",
    );
    // 只看 KEEP_HANS 那个对象:顶层键长这样 —— 行首两空格 + 引号 + 文件名 + 引号 + 冒号
    const body = src.split("export const KEEP_HANS")[1] ?? "";
    const keys = [...body.matchAll(/^ {2}"([^"]+\.tsx?)":/gm)].map((m) => m[1]);
    const dups = keys.filter((k, i) => keys.indexOf(k) !== i);
    expect(
      [...new Set(dups)],
      "KEEP_HANS 里有重复的文件名键 —— 后写的会**静默覆盖**先写的一整组。合并成一个键。",
    ).toEqual([]);
  });

  it("登记表里不许有已经用不上的条目(串改了没人回来删)", () => {
    const seen = new Set(scanAll().map((r) => `${r.file}\u001f${r.value}`));
    const stale: string[] = [];
    for (const [file, entries] of Object.entries(KEEP_HANS)) {
      for (const value of Object.keys(entries)) {
        if (!seen.has(`${file}\u001f${value}`)) stale.push(`${file}:「${value}」`);
      }
    }
    // 留着过期豁免的坏处不是脏,是**它会悄悄替一个新串背书** ——
    // 哪天有人在同一个文件里写了同样的字,守卫就自动放行了。
    expect(stale, `登记表里有源码里已经不存在的串,删掉:\n  ${stale.join("\n  ")}`).toEqual([]);
  });

  it("判据本身可信:简体判成待处理,同形判成已到位", () => {
    // 一简对多繁 —— 转换器选得对(§5.1 实测过,裁子集就会选错)
    expect(s2hk("签发")).toBe("簽發");
    expect(s2hk("复查")).toBe("複查");
    expect(s2hk("下周三")).toBe("下週三");
    // 简繁同形的不该被误判成待处理,否则登记表会被无意义的条目撑爆
    expect(s2hk("超期")).toBe("超期");
    expect(s2hk("全部")).toBe("全部");
    expect(s2hk("重大")).toBe("重大");
  });

  /**
   * 🔴 拦「照工具回改」。
   *
   * `hant-verify-diff.mjs` 的判据是「新值 === s2hk(旧值)」,而 opencc 在少数词上
   * 会选错字(见 TOOL_MISCONVERT 的头注)。于是溯源器会把**正确的人工修正**
   * 报成剩菜,而看到剩菜最省事的做法恰恰是照工具回改 ——
   * 一改就把「簽錯」(签字的签)改回「籤錯」(抽签的籤),意思全变,而且零报错。
   *
   * 这条测试盯的就是那一手:登记过的正确写法必须还在源码里,错误写法一个都不许有。
   */
  it("🔴 转换器选错字的那几个词,人工修正必须还在(别照工具回改)", () => {
    // 干草堆 = 覆盖件里的中文串 + **登录页的可见文字**。
    //
    // 加登录页那一半的理由:opencc 选错字的第三个实例就长在那儿
    // (PICS 声明里的「保存 90 天后自动删除」)。只扫覆盖件的话,
    // 那条**登记不进来** —— 登记了「正确写法必须还在」当场找不到、守卫直接红,
    // 于是它就只能停在注释里,而注释拦不住「照工具回改」那一手。
    //
    // 🔴 登录页必须**剥掉注释**再进干草堆:那段坑本身就写在注释里
    // (原文「保存 90 天后自动删除」原样引着),不剥就自己把自己判成违规。
    const loginText = readFileSync(
      fileURLToPath(new URL("../login-page.html", import.meta.url)),
      "utf8",
    ).replace(/<!--[\s\S]*?-->/g, "\n");

    const haystacks: { where: string; text: string }[] = [
      ...scanAll().map((r) => ({ where: `${r.file}:${r.line}`, text: r.value })),
      { where: "login-page.html", text: loginText },
    ];

    for (const [hans, fix] of Object.entries(TOOL_MISCONVERT)) {
      // 判据本身要成立:工具确实会把它转错
      expect(s2hk(hans), `TOOL_MISCONVERT 里的「${hans}」现在工具转对了 —— 这条可以删了`).toBe(
        fix.wrong,
      );

      const hasRight = haystacks.some((h) => h.text.includes(fix.right));
      const wrongHits = haystacks.filter((h) => h.text.includes(fix.wrong));

      expect(
        hasRight,
        `源码里找不到「${fix.right}」了 —— 人工修正被改掉了?${fix.why}`,
      ).toBe(true);
      expect(
        wrongHits.map((h) => `${h.where}`),
        `源码里出现了工具的错误产出「${fix.wrong}」。${fix.why}`,
      ).toEqual([]);
    }
  });

  /**
   * 🔴 运行时转换只许出现在**点开才挂载**的面板里。
   *
   * 这条守的是一个**实测过的承诺**(方案 §5.2):简体用户整个会话里
   * 一个字节的字典都不下载 —— 两条整页导航的网络 trace 只差那一个
   * 1,145,010 B 的 chunk。
   *
   * 挂到常驻界面上(动作条、状态卡、线程列表、聊天流)就等于**首屏拉字典**,
   * 而且这个代价落在**从不开面板的简体工友**头上,他连繁體界面都用不着。
   *
   * ⚠️ 它不会报错、不会变慢到看得出来 —— 只有翻网络面板才看得见。
   *    所以只能靠这条守卫,靠 review 是守不住的。
   */
  it("🔴 运行时转换不许出现在常驻界面(首屏拉 438 KB 字典)", () => {
    // 白名单 = 点开才挂载的面板。往这里加文件前先回答一个问题:
    // **它在用户没点任何东西的时候会不会被挂上?** 会 = 不许加。
    const LAZY_PANELS = new Set([
      "supervision.tsx", // 监理处置面板(动作条按钮点开)
      "checkin.tsx", // 打卡对话框(动作条按钮点开)
      "ProjectUploadPanel.tsx", // 资料库 / 归档抽屉(点开)
      "hant-convert.tsx", // 出口本身
    ]);

    const dir = fileURLToPath(new URL("../frontend-overrides", import.meta.url));
    const offenders: string[] = [];
    for (const f of readdirSync(dir)) {
      if (!f.endsWith(".ts") && !f.endsWith(".tsx")) continue;
      if (LAZY_PANELS.has(f)) continue;
      const calls = findRuntimeConversionCalls(join(dir, f));
      if (calls.length) offenders.push(`${f} → ${calls.join(", ")}`);
    }

    expect(
      offenders,
      "这些常驻界面的文件调用了运行时转换 —— 后果是**每个用户首屏都拉 438 KB 字典**," +
        "包括从不开面板的简体工友。没有报错、没有明显变慢,只有网络面板看得见。\n" +
        "正确做法:常驻界面的中文**在源码里就写成繁體**(静态,零运行时);" +
        "只有「后端给的文本」和「必须留简体的值」才走 useHantUI,而那些只出现在面板里。",
    ).toEqual([]);
  });

  /**
   * 🔴 后端 Envelope 的 `user_msg` **一律不许过转换器**(2026-08-18 复审定案)。
   *
   * ── 这条守的是什么 ──────────────────────────────────────────────────────
   * `user_msg` 看着像「后端给的显示文本」,和 `status_display` 排在一起,
   * 于是界面繁體化那一批把它整句包进了转换器。**但它不是纯系统文案 ——
   * 它内插了用户自己的数据**,而整句转换在原理上分不出哪一半是系统写的字:
   *
   *   attendance/messages.py 的 `missing_glyphs()`(docstring 原话:
   *   「字符原样回显、不做任何转换(它们多半正是姓名里的字)」):
   *       实测转换器:𠮶 → 嗰    恒 → 恆    㛿 → 𡠹
   *       工友「王𠮶」打卡 → 屏幕告诉他「「嗰」这几个字画不进凭证」
   *       —— 他名字里根本没有这个字。这句话存在的全部意义就是**点名是哪个字**,
   *       点错了他只能反复重试反复失败。**零报错。**
   *
   *   webapp.py:180/235/344 的 `f"项目「{name}」建好了"` / `f"图纸「{title}」上传成功"`
   *       —— 内插的是用户自己起的项目名和文件名,转了就跟他刚打进去的对不上。
   *
   *   schedule/dates.py:223 的兜底报错引的是**没折过的用户原话**(头注写明),
   *       经 supervision_api._resolve_due 透出。
   *
   * 分不出的时候**默认转是危险的那一侧**,所以整条通道都不转 ——
   * 与本仓「后端一律不转」同一口径。
   *
   * ── 判据 ────────────────────────────────────────────────────────────────
   * 转换函数(`useHantUI` / `useHantUIAll` / `hantSync`)的**实参子树**里出现
   * `user_msg` / `userMsg` 这种标识符就报。走 AST 取标识符,不扫源码原文 ——
   * 本仓注释密度极高,而解释「这儿为什么不转 user_msg」的注释恰恰会出现在
   * 转换点旁边,grep 会把它算进去(2026-08-18 已经误报过一次)。
   *
   * ── 🔴 它抓不住什么 ─────────────────────────────────────────────────────
   * 判据落在**名字**上,所以换个名字就绕过去了:
   *     const text = env?.user_msg ?? "…";  useHantUI(text)   ← 看不见
   * 这不是缺陷登记,是判据的边界。它治的是「顺手把 user_msg 包进去」这一手
   * (那是实际发生过的那一次),不是「有人存心绕」。真要更严得做数据流分析,
   * 而那件事的成本远高于它挡住的风险。
   */
  it("🔴 转换器的实参里不许出现 user_msg / userMsg(会把工友姓名里的字改掉)", () => {
    const dir = fileURLToPath(new URL("../frontend-overrides", import.meta.url));
    const USER_MSG_IDENT = /user_?msg/i;

    const offenders = readdirSync(dir)
      .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
      .sort()
      .flatMap((f) => findConversionCallArgs(join(dir, f)))
      .filter((c) => c.names.some((n: string) => USER_MSG_IDENT.test(n)));

    expect(
      offenders.map((c) => `${c.file}:${c.line} ${c.fn}(${c.arg})`),
      "这些地方把后端 Envelope 的 `user_msg` 过了繁體转换器。**会怎么坏**:\n" +
        "  · user_msg 里内插了**用户自己的数据** —— 工友姓名、他起的项目名、他传的文件名、\n" +
        "    他自己打的那句原话。转换器把「王𠮶」的𠮶改成嗰、把「恒昌」改成「恆昌」;\n" +
        "  · 最狠的一句是 attendance 的「「𠮶」这几个字画不进凭证」——\n" +
        "    它存在的全部意义就是点名是哪个字,点错了工友只能反复重试反复失败;\n" +
        "  · **全程零报错**,页面照开、测试照绿,只有那位师傅一个人卡在那儿。\n" +
        "正确做法:user_msg 原样上屏,一个字都不转(与本仓「后端一律不转」同口径)。\n" +
        "本地兜底句在源码里写成繁體即可;真要转的只有**枚举 / 生成值**\n" +
        "(status_display / due_display / doc_type_display / docTypeZh(…) 这一类,整句都是系统写的)。",
    ).toEqual([]);
  });

  /**
   * 🔴 登录页(`scripts/login-page.html`)—— 判据同上,工具**退让成正则**。
   *
   * ===========================================================================
   * 为什么它一直在守卫外面
   * ---------------------------------------------------------------------------
   * `hant-scan.mjs` 的 `scanAll()` 写死扫 `../frontend-overrides` 且只收
   * `.ts` / `.tsx`。登录页是 `.html`,躺在 `scripts/` 下面 —— 于是 W12 那次
   * 界面繁體化把覆盖件整个转了,**整页漏掉了它**。不是判过出局,是方案全文
   * grep 不到 "login-page",没人想起来。
   *
   * 代价比覆盖件那边还大:登录页是**港方用户看到的第一屏**,而且是全站唯一
   * 不需要会话就能访问的页面 —— 一屏简体,后面全繁體。
   *
   * ===========================================================================
   * 🔴 这条是**有意的退让**:html 里没有 TS 的 AST 可用
   * ---------------------------------------------------------------------------
   * `hant-scan.mjs` 的头注花了一整段讲「为什么用 AST 不用正则」——
   * 核心是本仓注释密度极高且全是中文,正则分不清注释和文案。那条论证在这里
   * **依然成立**,只是没有第二条路:引一个 HTML parser 等于给这个零依赖的
   * 测试包加一个依赖,为一个 300 行的静态页不值。
   *
   * 于是靠「先剥掉三种一定不是文案的东西」把正则的适用面收窄:
   *   · `<!-- … -->`  HTML 注释   —— 本仓约定注释一律**简体**,不剥就永远红
   *   · `<script>…`   脚本        —— 里面的 JS 注释同样是简体
   *   · `<style>…`    样式        —— 里面的 CSS 注释同样是简体
   * 剩下的才判 `s2hk(v) !== v`。
   *
   * ===========================================================================
   * 🔴 因此它**抓不住**这些 —— 写在这儿免得下一个人高估它
   * ---------------------------------------------------------------------------
   * ① **`<script>` 里的字符串**。本页真有四条上屏文案在那儿:
   *        peek.textContent = showing ? '顯示' : '隱藏';
   *        peek.setAttribute('aria-label', showing ? '顯示口令' : '隱藏口令');
   *    它们是「点一下显示口令」那颗按钮**切换后**的文字,首屏看不到,
   *    人工过页面最容易漏的就是这种。想扫它就得扫脚本里的引号串,而脚本里
   *    还有一堆简体 JS 注释 —— 正则分不开,硬扫的下场是守卫永远红、最后被删。
   *    **改那两行的人只能靠自己**(login-page.html 头注里也钉了一句)。
   * ② **属性值**只扫下面 `DISPLAY_ATTRS` 那几个会上屏 / 会被读屏念出来的。
   *    别的属性(`data-*`、`content=`)里的中文一律看不见。
   *    往页面上加新的显示属性 = 回来往那张表里加一个。
   * ③ **转成了错的繁體**。与覆盖件那条守卫同一个盲区:`s2hk("籤發") === "籤發"`。
   *    而登录页**不在** `hant-verify-diff.mjs` 的溯源范围里(那件工具也只比
   *    `.ts/.tsx`),所以这一档在这里是**彻底没人守**的。
   *    已知踩过一次:整句转「凭证照片保存 90 天后自动删除」会得到
   *    「90 天**后**自動刪除」——「天后」在 opencc 词组表里是神明那个词。
   * ④ **英文残留**。判据是 `s2hk(v) !== v`,而 s2hk 对英文**恒等** ——
   *    一整句 "An error occurred. Please try again." 在它眼里永远「已经到位」。
   *    这条对覆盖件那几条守卫同样成立,不是登录页特有的。
   */
  it("🔴 登录页的可见文字必须是繁體(正则扫,退让与盲区见注释)", () => {
    const html = readFileSync(
      fileURLToPath(new URL("../login-page.html", import.meta.url)),
      "utf8",
    );

    // 先剥掉三种「一定是简体、一定不是文案」的区域,理由见上面头注。
    const stripped = html
      .replace(/<!--[\s\S]*?-->/g, "\n")
      .replace(/<script\b[\s\S]*?<\/script>/gi, "\n")
      .replace(/<style\b[\s\S]*?<\/style>/gi, "\n");

    /** 会上屏、或者会被读屏念出来的属性。加新的显示属性要回来加一个。 */
    const DISPLAY_ATTRS = /\b(?:placeholder|aria-label|title|alt)\s*=\s*"([^"]*)"/gi;

    const chunks: { where: string; value: string }[] = [];
    // ① 文本节点:标签整体换成换行,剩下的按行切
    for (const raw of stripped.replace(/<[^>]*>/g, "\n").split("\n")) {
      const value = raw.trim();
      if (value) chunks.push({ where: "文本", value });
    }
    // ② 显示类属性值
    for (const m of stripped.matchAll(DISPLAY_ATTRS)) {
      const value = m[1].trim();
      if (value) chunks.push({ where: "属性", value });
    }

    const HAN = /[一-鿿]/;
    const withHan = chunks.filter((c) => HAN.test(c.value));

    // ======================================================================
    // 🔴 两道「防假绿灯」的闸 —— 上面那几条 replace 写坏了,守卫会**静默空转**
    // ----------------------------------------------------------------------
    // 正则守卫最危险的坏法不是误报,是**少扫**:剥掉的比该剥的多,剩下的当然
    // 「没有简体串」,守卫从此永远绿而没人会发现。本仓在 knowledge 那条断言上
    // 栽过同一种跟头(提示词里的中文示例自己把断言满足了,模板真翻成英文测试照绿)。
    //
    // 这两道闸的数是**实测**出来的,不是拍的(2026-08-18,页面 = 当时那一版):
    //     正常                              30 段
    //     把剥注释改成贪婪 `[\s\S]*-->`     15 段  ← 从第一个 <!-- 吃到最后一个 -->,
    //                                              整个登录表单连同页头全没了,
    //                                              只剩 PICS 那一块
    //     完全不剥                          97 段(会把简体注释全算进来 → 永远红)
    //
    // 所以下限取 24(实测 30,留 6 段余量给正常的文案增删)。
    // ⚠️ 第一版这里写的是 15 —— 正好等于「贪婪」那一档的实测值,`>=` 让它擦边过了,
    //    变异验证当场证伪。留着这段是记住:**下限闸的数必须比已知坏法的产出高**,
    //    拍一个「看着够小」的数等于没设闸。
    expect(
      withHan.length,
      "登录页扫到的中文段数掉下来了 —— 大概率是上面那几条 replace 写坏了(守卫空转)," +
        "而不是页面真的没中文了。先去看剥注释 / 剥 script / 剥 style 那三行。",
    ).toBeGreaterThanOrEqual(24);

    // 第二道:**两条取数路径都得有产出**。它与条数无关,所以页面文案怎么增删都不会误报,
    // 而上面那种「整块被吃掉」的坏法它照样抓得住(贪婪那一档把两个属性值也一起吃了)。
    // 谁哪天把 DISPLAY_ATTRS 写错、或者把标签剥离那行搞成剥掉全部内容,这条会说话。
    for (const path of ["文本", "属性"] as const) {
      expect(
        withHan.filter((c) => c.where === path).length,
        `「${path}」这条取数路径一段中文都没扫到 —— 它坏了,不是页面没有。` +
          `文本走的是「标签换行 + 按行切」,属性走的是 DISPLAY_ATTRS 那条正则。`,
      ).toBeGreaterThan(0);
    }

    const offenders = withHan
      .filter((c) => s2hk(c.value) !== c.value)
      .map((c) => `  [${c.where}] ${c.value}\n        → ${s2hk(c.value)}`);

    expect(
      offenders,
      `登录页还有 ${offenders.length} 段简体。它是港方用户看到的**第一屏** ——\n` +
        "一屏简体、后面全繁體,比哪儿都扎眼。\n" +
        "改法:**不许手打繁體**(一简对多繁会静默打错字),用工具转:\n" +
        "  cd scripts/frontend-tests && node -e \"import('opencc-js').then(m=>{\n" +
        "    const c=m.Converter({from:'cn',to:'hk'});console.log(c(process.argv[1]))})\" '简体句子'\n" +
        "⚠️ 只改给人看的字。表单字段名 / name / id / action 一个字节都不许动 ——\n" +
        "   serve_login.py 的 _read_page 有启动期硬校验,动了服务直接起不来。\n" +
        "⚠️ PICS 那六条是 PDPO 第486章下的法律文本:**只转字形,不许改措辞、不许增删**。\n",
    ).toEqual([]);
  });

  it("🔴 钉住这条守卫的**盲区**:手打的错形它放行", () => {
    // 「籤發」全是繁體字形,简→繁转换器不碰 → 守卫看不出来。
    // 这不是缺陷登记,是**判据的边界**:守卫说「没有简体串了」,
    // 不等于说「繁體都对」。哪天有人想删掉 hant-verify-diff.mjs,
    // 先回来看这条 —— 那件工具治的就是这里。
    expect(s2hk("籤發")).toBe("籤發");
    expect(s2hk("復查")).toBe("復查");
    // 而「復」本身是合法繁體字(復工令就该写「復」),所以**不能靠黑名单**兜。
    expect(s2hk("工程复工令")).toBe("工程復工令");
  });
});
