/**
 * 覆盖件的两个数:`apply_override` 几件、`install_new_file` 几件。
 *
 * ===========================================================================
 * 🔴 为什么要这个文件:同一条纪律**复发了四次**
 * ---------------------------------------------------------------------------
 * `scripts/setup-frontend.sh` 的头注和 `CLAUDE.md`「前端覆盖件」那一节各写着这两个数,
 * 而真值在脚本里那两行 grep。四次实录(都记在那两处的注释里):
 *
 *   · 2026-08-11 之前     文档写「八件」,实际十一件
 *   · W7 合流后           文档写「十一 + 三」,实际已是 12 + 5
 *   · 2026-08-19          脚本自己的头数在「八」上停了两批
 *   · 2026-08-21          **同一次编辑里**头数改对了(十三),而同一节的结论段
 *                         还停在当天上半场的「12 + 13」,两句自相矛盾
 *
 * 每一次的坏法都一样:**零报错**。下一个人照着数,会以为「剩下的不用管」。
 * CLAUDE.md 里那句话写得很直白 ——
 *   「这已经不是纪律问题了 —— 四次复发说明该交给机器。落点见 P3:
 *     `make test-frontend` 里加一句 `grep -c` 与本段两个数字的比对。」
 * 这就是那台机器(2026-08-22 补上)。
 *
 * ===========================================================================
 * 判据
 * ---------------------------------------------------------------------------
 * 真值 = 两条 grep(与脚本头注里写的数法**逐字相同**)。
 * 被核对的是**中文数字**,因为那两处写的就是中文(「十三件」)——
 * 拿阿拉伯数字去比会一条都匹配不上,然后这道闸静默变成永远通过。
 *
 * ⚠️ **它只保证数对得上,保证不了别的。** 两个数相等的时候尤其别合并 ——
 * 它们量的是两件事(打补丁 vs 装新文件),这条测试拿它们分别比对,
 * 合并成一个数之后这里会红,那是**刻意的**。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SETUP_SH = fileURLToPath(new URL("../setup-frontend.sh", import.meta.url));
const CLAUDE_MD = fileURLToPath(new URL("../../CLAUDE.md", import.meta.url));

/**
 * 阿拉伯数字 → 中文数字。只覆盖到 99 —— 覆盖件到不了那个量级,
 * 真到了的话这里会抛,而那本身就是个该有人看一眼的信号。
 */
function toChinese(n: number): string {
  const digits = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"];
  if (n < 10) return digits[n];
  if (n > 99) throw new Error(`覆盖件到 ${n} 件了?这个转换只写到 99,回去看一眼`);
  const tens = Math.floor(n / 10);
  const ones = n % 10;
  const head = tens === 1 ? "十" : `${digits[tens]}十`;
  return ones === 0 ? head : `${head}${digits[ones]}`;
}

/**
 * 数脚本里真正的调用次数。
 *
 * 判据与 `setup-frontend.sh` 头注里写的数法**逐字相同**
 * (`grep -cE '^\s*apply_override '`)—— 行首可有缩进、函数名后必须有空格。
 * 后面那个空格不能省:没有它的话 `apply_override_foo` 这种名字也会被数进来。
 */
function countCalls(source: string, fn: "apply_override" | "install_new_file"): number {
  const pattern = new RegExp(String.raw`^\s*${fn} `, "gm");
  return (source.match(pattern) ?? []).length;
}

describe("覆盖件件数:脚本 / CLAUDE.md 与真值对得上", () => {
  const setup = readFileSync(SETUP_SH, "utf8");
  const applyCount = countCalls(setup, "apply_override");
  const installCount = countCalls(setup, "install_new_file");

  it("真值本身是合理的(两个数都非零)", () => {
    // 这一条防的是**这道闸自己坏掉**:正则写错时两个数会双双变成 0,
    // 而 0 === 0 会让下面几条全绿 —— 一道永远通过的闸比没有闸更坏。
    expect(applyCount).toBeGreaterThan(0);
    expect(installCount).toBeGreaterThan(0);
  });

  it("setup-frontend.sh 头注里的两个中文数字对得上", () => {
    // 那句话在脚本里是**跨行的注释**:
    //   # … apply_override **十三件** + install_new_file
    //   #    **十五件**,是**两个数**…
    // 先把「换行 + `#` + 缩进」压成一个空格再匹配。直接上 /s 修饰符的话
    // `.+?` 会跨过换行把注释前缀一起吃进捕获组,而**那时它照样匹配成功** ——
    // 只是捕获到的东西对不上,于是变成一条报错文案完全指错方向的红灯。
    const flat = setup.replace(/\n#\s*/g, " ");
    const 头注 = /apply_override \*\*([^*]+?)件\*\* \+ install_new_file \*\*([^*]+?)件\*\*/.exec(
      flat,
    );
    expect(
      头注,
      "没在 setup-frontend.sh 里找到那句「apply_override N件 + install_new_file N件」——\n" +
        "它是这道闸认路的锚,改措辞要连这条测试一起改",
    ).not.toBeNull();
    expect(头注![1]).toBe(toChinese(applyCount));
    expect(头注![2]).toBe(toChinese(installCount));
  });

  it("CLAUDE.md「前端覆盖件」那一节的两个数对得上", () => {
    const md = readFileSync(CLAUDE_MD, "utf8");
    // CLAUDE.md 的写法是 `- **`apply_override` 十三件** ——`:
    // 星号包住的是**整段**(名字 + 数字),不是只包数字。
    const apply = /\*\*`apply_override` ([^*]+?)件\*\*/.exec(md);
    const install = /\*\*`install_new_file` ([^*]+?)件\*\*/.exec(md);
    expect(
      apply,
      "CLAUDE.md 里没找到「**`apply_override` N件**」—— 那一节的措辞改了就要连这条测试一起改",
    ).not.toBeNull();
    expect(install, "CLAUDE.md 里没找到「**`install_new_file` N件**」").not.toBeNull();
    expect(apply![1]).toBe(toChinese(applyCount));
    expect(install![1]).toBe(toChinese(installCount));
  });

  it("每一件覆盖件在 scripts/frontend-overrides/ 里都真的存在", () => {
    // 这一条与件数无关,但成因同源:注册了一个不存在的文件,
    // `apply_override` 只 warning 跳过、`install_new_file` 会 die ——
    // 前者**完全静默**,表现是那件覆盖件从来没生效过。
    const names = [...setup.matchAll(/^\s*(?:apply_override|install_new_file) "([^"]+)"/gm)].map(
      (m) => m[1],
    );
    expect(names.length).toBe(applyCount + installCount);
    for (const name of names) {
      const path = fileURLToPath(new URL(`../frontend-overrides/${name}`, import.meta.url));
      expect(() => readFileSync(path, "utf8"), `注册了但文件不在:${name}`).not.toThrow();
    }
  });
});
