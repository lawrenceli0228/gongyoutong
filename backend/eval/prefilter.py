"""从 YOLO 格式的公开数据集里预筛出 Safety 评测集候选图。

用途:把「30 张照片的人工标注」压缩成「30 次快速确认」。
脚本**只做预筛与草稿**,最终标注必须人工逐张过目 —— 理由见下面「为什么不能全自动」。

    python -m eval.prefilter --src <解压目录> [--dry-run]

===========================================================================
为什么不能全自动(读之前先看这段)
---------------------------------------------------------------------------
公开数据集的标注与我们要的**不是一回事**,差三层:

1. **粒度不同。** 它是逐对象包围框(这个人戴了帽、那个人没戴),
   我们要的是整张照片一个判断(violation / compliant / not_site)。
   一张有 5 个工人、其中 1 个没戴帽的照片,在它那儿是若干个框,在我们这儿整张就是违规。

2. **词表不同。** 它有 boots / gloves / goggles 三类,而我们的受控词表(8 项)里没有
   对应的词。这类图既不能算合规(画面里确实有违规),也没法按我们的词表标注 ——
   脚本把它们单独隔离到 ambiguous 桶,不进候选集。
   不隔离的话:你标注时看到有人没戴手套会犹豫,而模型受提示词约束根本不会输出这个词,
   最后是「你判违规、模型判合规」,评测扣的是**词表设计**的分,不是模型的分。

3. **它的标注也可能错。** 别人的数据集没有为我们的评测门槛做过质量保证。
   这 30 张是要用来判定「Safety Agent 达没达到 80%」的尺子,尺子本身必须由我们校准。

===========================================================================
分桶规则(ASCII)
---------------------------------------------------------------------------
    读 labels/<name>.txt(YOLO: class_id cx cy w h,归一化)
        │
        ├─ 含 no-boots / no-gloves / no-goggles ?
        │       └─是─► ambiguous(词表覆盖不到,直接排除,不进候选)
        │
        ├─ 含 no-helmet / no-vest ?
        │       └─是─┬─ 违规框很小 或 框总数很多 ─► hard    (难例:远景/密集)
        │             └─ 否 ──────────────────────► violation(违规,主力)
        │
        ├─ 正向 PPE 框 >= 2 且 一个负类都没有 ────────► compliant(合规对照)
        │
        └─ 其余(空图、只有机械车辆、单个 PPE 等)────► other(不用)

    注:本数据集几乎不标 person 框(5170 张里仅 38 张有),所以「密集/远景」
        一律用**标注框**而非人框来判定 —— 详见 PERSON_CLASS 常量的说明。

    去重:文件名形如 <原名>_jpg.rf.<hash>.jpg,同一原名可能有多份副本
          (增强或重复上传)。按 <原名> 分组,每组只取一张 ——
          评测集里混进近乎相同的图,分数会虚高而你看不出来。

    抽样:按原名排序后等距取样(不是随机),保证**可复现**且覆盖面散开。
===========================================================================
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# --- 与 data.yaml 的 names 顺序严格对应,改数据集就要改这里 ----------------------
CLASS_NAMES: Final[tuple[str, ...]] = (
    "boots",
    "gloves",
    "goggles",
    "helmet",
    "no-boots",
    "no-gloves",
    "no-goggles",
    "no-helmet",
    "no-vest",
    "person",
    "vest",
)

# 能映射到我们受控词表的负类。键是数据集类名,值是 eval/README.md 里的规定用词。
MAPPABLE_NEGATIVES: Final[dict[str, str]] = {
    "no-helmet": "未戴安全帽",
    "no-vest": "未穿反光衣",
}

# 映射不到我们词表的负类 —— 含这些的图一律排除(理由见模块 docstring 第 2 条)。
UNMAPPABLE_NEGATIVES: Final[frozenset[str]] = frozenset({"no-boots", "no-gloves", "no-goggles"})

POSITIVE_PPE: Final[frozenset[str]] = frozenset({"helmet", "vest"})
PERSON_CLASS: Final[str] = "person"
"""⚠️ 这个数据集几乎不用 person 类 —— 5170 张里只有 38 张有 person 框,
617 张违规图里**一张都没有**。标注者是直接在头部/身体上画 helmet / no-helmet,
不单独画人框。所以下面的分桶与难例判定**一律不能依赖 person**,
第一版就是栽在这上面:hard 桶恒为 0,compliant 只剩 14 张。
换数据集时先跑 --dry-run 看分桶数字,别默认 person 一定存在。"""

# --- 难例判定阈值:下面两个数字来自本数据集的实际分布,不是拍脑袋定的 ---------------
HARD_FAR_BOX_AREA: Final[float] = 0.012
"""违规框归一化面积小于此值 = 远景/小目标。

取值依据:617 张违规图的最大违规框面积,第 10 百分位是 0.01115
(5% 位 0.006 / 中位 0.077 / 75% 位 0.156)。取 0.012 大致框住最小的那 10%,
约 61 张候选 —— 够挑 5 张难例,又不至于把中等距离的也算进来。"""

HARD_CROWD_BOX_COUNT: Final[int] = 7
"""标注框总数达到此值 = 画面里人多、遮挡重,属于难例。

取值依据:违规图的框数分布 —— ≥7 个框约占 13%,≥8 个约占 10%。
取 7 能拿到足量候选。(这里数的是所有框而非人框,原因见 PERSON_CLASS 的说明。)"""

COMPLIANT_MIN_POSITIVE_BOXES: Final[int] = 2
"""合规候选至少要有这么多个正向 PPE 框。

只有 1 个框的图,可能是「地上放着一顶安全帽」而不是「有人正确佩戴」,
人工确认时很难判断。要求 2 个以上,拿到的图证据更足、确认更快。"""

# --- 目标配比(与 eval/README.md 的「建议配比」同源)-----------------------------
TARGET_COUNTS: Final[dict[str, int]] = {"violation": 15, "compliant": 7, "hard": 5}
NOT_SITE_NEEDED: Final[int] = 3
"""非工地干扰项。公开的工地数据集里不可能有,必须另找 —— 脚本只负责提醒。"""

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
"""仓库根目录(本文件在 <root>/backend/eval/prefilter.py,往上两级)。

默认输出路径必须**锚定在仓库根**,不能写成 "data/demo/photos" 这种相对路径 ——
本模块只能以 `python -m eval.prefilter` 从 backend/ 下运行,相对路径会解析成
backend/data/... 和 backend/backend/eval/...。第一次跑就是这么把 27 张照片
和草稿 CSV 写到了两个错地方,而且当时没报任何错(目录是自动新建的)。

后来同一类坑在 ``Settings.data_dir`` 上又犯了一次(默认值曾是 ``Path("data")``,
于是 `make dev` 写 backend/data/、容器写 /app/data,两份数据互相看不见)。
那边现在已经改成按 config.py 的 ``__file__`` 推导仓库根 —— 所以**别再把这段读成
"演示照片应该去 backend/data 找"**:默认布局下 <仓库根>/data/demo 才是那一份,
也正是本文件下面 ``--out-photos`` 默认值指的地方。
"""

PHOTO_STEM: Final[str] = "photo_{:02d}"
ROBOFLOW_NAME_SEP: Final[str] = "_jpg.rf."
"""Roboflow 导出时的重命名分隔符:<原名>_jpg.rf.<hash>.jpg。前半段才是原图身份。"""


@dataclass
class ImageFacts:
    """一张图从标注里读出来的事实(不含任何判断)。"""

    path: Path
    label_path: Path
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    max_violation_area: float = 0.0
    """本图中最大的那个「可映射违规框」的归一化面积。越小说明目标越远越小 = 越难。"""

    @property
    def box_count(self) -> int:
        """标注框总数。用来判断画面是否密集(不用人数,因为本数据集不标 person)。"""
        return sum(self.counts.values())

    @property
    def positive_count(self) -> int:
        """正向 PPE 框数(helmet + vest)。合规候选靠它衡量证据是否充足。"""
        return sum(self.counts.get(c, 0) for c in POSITIVE_PPE)

    @property
    def origin(self) -> str:
        """去掉 Roboflow 哈希后的原图名,用来识别同一张图的多个副本。"""
        name = self.path.name
        return name.split(ROBOFLOW_NAME_SEP)[0] if ROBOFLOW_NAME_SEP in name else self.path.stem

    def has(self, *class_names: str) -> bool:
        return any(self.counts.get(c, 0) > 0 for c in class_names)

    def mapped_violations(self) -> list[str]:
        """按我们的受控词表列出违规项(顺序稳定,便于 diff)。"""
        return [zh for cls, zh in MAPPABLE_NEGATIVES.items() if self.counts.get(cls, 0) > 0]


def read_facts(image_path: Path, label_path: Path) -> ImageFacts | None:
    """读一个 YOLO 标注文件。读不了就返回 None(跳过,不让个别坏文件中断整批)。"""
    facts = ImageFacts(path=image_path, label_path=label_path)
    try:
        lines = label_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 5:  # noqa: PLR2004 —— YOLO 一行固定 5 列,不足就是坏行
            continue
        try:
            cid = int(parts[0])
            width, height = float(parts[3]), float(parts[4])
        except ValueError:
            continue
        if not 0 <= cid < len(CLASS_NAMES):
            continue
        name = CLASS_NAMES[cid]
        facts.counts[name] += 1
        if name in MAPPABLE_NEGATIVES:
            facts.max_violation_area = max(facts.max_violation_area, width * height)
    return facts


def bucket_of(f: ImageFacts) -> str:
    """把一张图归到某个桶(规则见模块 docstring 的 ASCII 图)。"""
    if f.has(*UNMAPPABLE_NEGATIVES):
        return "ambiguous"
    if f.mapped_violations():
        crowded = f.box_count >= HARD_CROWD_BOX_COUNT
        far = 0.0 < f.max_violation_area < HARD_FAR_BOX_AREA
        return "hard" if (crowded or far) else "violation"
    if f.positive_count >= COMPLIANT_MIN_POSITIVE_BOXES:
        return "compliant"
    return "other"


def dedupe(items: list[ImageFacts]) -> list[ImageFacts]:
    """同一原图的多个副本只留一张(留标注框最多的那张,信息量大、便于人工判断)。"""
    best: dict[str, ImageFacts] = {}
    for f in items:
        cur = best.get(f.origin)
        if cur is None or sum(f.counts.values()) > sum(cur.counts.values()):
            best[f.origin] = f
    return sorted(best.values(), key=lambda x: x.origin)


def sample_evenly(items: list[ImageFacts], want: int) -> list[ImageFacts]:
    """等距取样(不是随机)—— 同一份数据集跑两次结果一致,评测集可复现。"""
    if want <= 0 or not items:
        return []
    if len(items) <= want:
        return items
    step = len(items) / want
    return [items[int(i * step)] for i in range(want)]


def sample_balanced(items: list[ImageFacts], want: int) -> list[ImageFacts]:
    """按**违规类型**分层后均分名额,组内再等距取样。

    为什么不能直接等距取样(第一版就是这么错的):
        候选池本身是偏的 —— 违规桶里「未穿反光衣」345 张、「未戴安全帽」66 张。
        等距取样会按池子比例照搬这个偏斜,15 张里 13 张测反光衣、只有 5 张测安全帽。
        可**安全帽恰恰是工地最核心、演示最要讲的那一项** —— 尺子在最该精确的地方最粗,
        而且这个偏斜完全是数据集采集偏好带来的,跟我们要测什么毫无关系。

        分层取样 --> 每种违规组合尽量拿等份名额
            未戴安全帽      ┐
            未穿反光衣      ├─ 各占约 1/3 --> 两项被测到的图片数基本持平
            两者都有        ┘

    名额分配:先均分,余数给样本最多的组;某组不够就把剩余名额退回其它组。
    """
    if want <= 0 or not items:
        return []
    groups: dict[str, list[ImageFacts]] = defaultdict(list)
    for f in items:
        groups[";".join(f.mapped_violations())].append(f)
    # 组内先排好序,保证结果可复现
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    quotas = {name: want // len(ordered) for name, _ in ordered}
    for name, _ in ordered[: want % len(ordered)]:  # 余数给样本最多的几组
        quotas[name] += 1

    picked: list[ImageFacts] = []
    shortfall = 0
    for name, pool in ordered:  # 第一轮:各取各的名额,不够的记账
        take = sample_evenly(pool, quotas[name])
        picked.extend(take)
        shortfall += quotas[name] - len(take)
    if shortfall:  # 第二轮:把空出来的名额补给还有余量的组
        for _name, pool in ordered:
            if shortfall <= 0:
                break
            rest = [f for f in pool if f not in picked]
            extra = sample_evenly(rest, shortfall)
            picked.extend(extra)
            shortfall -= len(extra)
    return sorted(picked, key=lambda x: x.origin)


def collect(src: Path) -> list[ImageFacts]:
    """遍历 train/valid/test 三个 split,读出每张图的事实。"""
    found: list[ImageFacts] = []
    for split in ("train", "valid", "test"):
        img_dir, lbl_dir = src / split / "images", src / split / "labels"
        if not img_dir.is_dir() or not lbl_dir.is_dir():
            continue
        for img in sorted(img_dir.iterdir()):
            if img.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            lbl = lbl_dir / f"{img.stem}.txt"
            if not lbl.is_file():
                continue
            facts = read_facts(img, lbl)
            if facts is not None:
                found.append(facts)
    return found


def build_rows(picked: dict[str, list[ImageFacts]]) -> list[dict[str, str]]:
    """生成 safety.csv 草稿行。**note 一律标「待人工确认」,不许伪装成已完成的标注。**"""
    rows: list[dict[str, str]] = []
    index = 1
    for bucket in ("violation", "compliant", "hard"):
        for f in picked.get(bucket, []):
            label = "compliant" if bucket == "compliant" else "violation"
            violations = ";".join(f.mapped_violations())
            detail = ", ".join(f"{k}×{v}" for k, v in sorted(f.counts.items()) if v)
            rows.append(
                {
                    "id": f"S{index:02d}",
                    "type": bucket,
                    "image": f"{PHOTO_STEM.format(index)}.jpg",
                    "label": label,
                    "violations": violations,
                    "note": f"待人工确认 | 源标注: {detail} | 原名: {f.origin}",
                }
            )
            index += 1
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="从 YOLO 数据集预筛 Safety 评测集候选图")
    parser.add_argument("--src", required=True, type=Path, help="解压后的数据集根目录")
    parser.add_argument(
        "--out-photos", type=Path, default=REPO_ROOT / "data/demo/photos", help="照片拷贝目标目录"
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=REPO_ROOT / "backend/eval/datasets/safety.draft.csv",
        help="草稿 CSV 路径(刻意不直接覆盖 safety.csv)",
    )
    parser.add_argument("--dry-run", action="store_true", help="只看统计,不拷贝也不写文件")
    args = parser.parse_args()

    if not args.src.is_dir():
        print(f"[错误] 找不到数据集目录:{args.src}", file=sys.stderr)
        return 2

    print(f"扫描 {args.src} …")
    facts = collect(args.src)
    if not facts:
        print(
            "[错误] 没读到任何 图片+标注 配对。检查目录结构是否为 <split>/images 与 <split>/labels",
            file=sys.stderr,
        )
        return 2

    buckets: dict[str, list[ImageFacts]] = defaultdict(list)
    for f in facts:
        buckets[bucket_of(f)].append(f)

    print(f"\n共 {len(facts)} 张(含副本)。分桶:")
    for name in ("violation", "compliant", "hard", "ambiguous", "other"):
        items = buckets.get(name, [])
        uniq = dedupe(items)
        print(f"  {name:<10} {len(items):>5} 张 → 去重后 {len(uniq):>4} 张")

    picked: dict[str, list[ImageFacts]] = {}
    for name, want in TARGET_COUNTS.items():
        uniq = dedupe(buckets.get(name, []))
        # violation / hard 两桶按违规类型分层,避免照搬数据集自带的偏斜(见 sample_balanced);
        # compliant 桶没有违规类型可分层,等距取样即可。
        got = sample_evenly(uniq, want) if name == "compliant" else sample_balanced(uniq, want)
        picked[name] = got
        flag = "" if len(got) >= want else f"  ⚠️ 不足,只有 {len(got)} 张"
        print(f"\n{name}:目标 {want} 张,取到 {len(got)} 张{flag}")

    rows = build_rows(picked)
    print(f"\n合计选出 {len(rows)} 张。还差 {NOT_SITE_NEEDED} 张**非工地干扰项**,")
    print("  这类图公开的工地数据集里没有,要自己找(办公室 / 街景 / 室内),")
    print("  但它们是必需的 —— 没有干扰项,模型把任何画面都当工地也能拿高分。")

    if args.dry_run:
        print("\n[dry-run] 未拷贝文件、未写 CSV。")
        return 0

    args.out_photos.mkdir(parents=True, exist_ok=True)
    ordered = [x for b in ("violation", "compliant", "hard") for x in picked.get(b, [])]
    for row, f in zip(rows, ordered, strict=True):
        shutil.copy2(f.path, args.out_photos / row["image"])
    print(f"\n已拷贝 {len(rows)} 张到 {args.out_photos}")

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_csv.open("w", encoding="utf-8", newline="") as fp:
        columns = ["id", "type", "image", "label", "violations", "note"]
        writer = csv.DictWriter(fp, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"已写草稿 {args.out_csv}")
    print("\n下一步(必须做,不能跳):")
    print("  1. 逐张看 data/demo/photos/,确认或改掉草稿里的 label 与 violations")
    print("  2. 违规项只能用 eval/README.md 里那 8 个受控词")
    print("  3. 补 3 张非工地干扰项(label 填 not_site,violations 留空)")
    print("  4. 确认无误后改名覆盖 backend/eval/datasets/safety.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
