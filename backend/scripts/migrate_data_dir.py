"""一次性搬迁:把 `backend/data/` 并进仓库根的 `data/`。

===========================================================================
为什么会有两份数据(这个脚本存在的理由)
---------------------------------------------------------------------------
    `Settings.data_dir` 原来的默认值是 `Path("data")` —— **相对路径,按进程
    工作目录解析**。而两条启动路径的工作目录根本不一样:

        make dev        -> 在 backend/ 下跑 -> 数据落 backend/data/
        docker compose  -> GYT_DATA_DIR=/app/data,挂的是仓库根 ./data

    于是 LLM 缓存、SQLite 台账、Chroma 向量库、产物注册表,四样东西各有两份,
    互相看不见。最疼的是**视觉缓存**:演示前焐热那 22 分钟只焐热了其中一边,
    换条路启动就全部 miss、当场重新烧钱重跑(见 TODOS.md 的 TODO-11 演示铁律)。

    config.py 的默认值已经改成「按本文件位置推导仓库根」,两条路径从此同源。
    这个脚本负责把历史遗留的那份搬过去 —— **跑一次就够,跑完可以删掉本文件**。

同名冲突怎么办:
    缓存文件名是**请求键**的哈希,不是响应内容的哈希。同一个键两边内容不同,
    意味着模型对同一个请求给过两次不同的采样结果 —— **两份都是有效缓存**,
    留哪个都不影响正确性。规则取「mtime 更新的那份」,并把每一次覆盖都打印出来,
    让人能看见自己丢了什么,而不是静默替换。

跑法(默认只演练不动手):
    cd backend && python scripts/migrate_data_dir.py            # 演练,看清楚要搬什么
    cd backend && python scripts/migrate_data_dir.py --apply    # 真搬(复制,不删源)
    cd backend && python scripts/migrate_data_dir.py --apply --remove-source

    刻意默认「复制而不是移动」:搬错了还能回头。确认新目录一切正常之后,
    再用 --remove-source 或自己手动删掉 backend/data/。
===========================================================================
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 本文件是 <仓库根>/backend/scripts/migrate_data_dir.py:
#   parents[0]=scripts  [1]=backend  [2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_DIR = _REPO_ROOT / "backend" / "data"
_TARGET_DIR = _REPO_ROOT / "data"

# macOS Finder 到处撒的目录元数据,没有任何搬迁价值
_JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db"})


def _iter_files(root: Path) -> list[Path]:
    """列出目录下所有值得搬的文件(相对路径),跳过系统垃圾文件。"""
    if not root.is_dir():
        return []
    return sorted(
        p.relative_to(root) for p in root.rglob("*") if p.is_file() and p.name not in _JUNK_NAMES
    )


def _mb(paths: list[Path], root: Path) -> float:
    return sum((root / p).stat().st_size for p in paths) / 1e6


def _plan(legacy: Path, target: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """把搬迁分成三堆:新增、同名同内容(跳过)、同名不同内容(要覆盖)。"""
    legacy_files = _iter_files(legacy)
    fresh: list[Path] = []
    identical: list[Path] = []
    clashing: list[Path] = []
    for rel in legacy_files:
        dst = target / rel
        if not dst.exists():
            fresh.append(rel)
            continue
        src = legacy / rel
        if src.read_bytes() == dst.read_bytes():
            identical.append(rel)
        else:
            clashing.append(rel)
    return fresh, identical, clashing


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)  # copy2 保留 mtime,缓存的新旧关系不能在搬迁中丢掉


def main() -> int:
    parser = argparse.ArgumentParser(description="把 backend/data 并进仓库根 data")
    parser.add_argument("--apply", action="store_true", help="真的执行(默认只演练)")
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="搬完删掉 backend/data(只在 --apply 且全部核对通过时生效)",
    )
    args = parser.parse_args()

    if not _LEGACY_DIR.is_dir():
        print(f"没有 {_LEGACY_DIR} —— 无需搬迁,这台机器本来就是干净的。")
        return 0

    fresh, identical, clashing = _plan(_LEGACY_DIR, _TARGET_DIR)
    if not fresh and not clashing:
        print(f"{_LEGACY_DIR} 里的 {len(identical)} 个文件目标处全都已有且内容一致,无需搬迁。")
        return 0

    print(f"源:  {_LEGACY_DIR}")
    print(f"目标:{_TARGET_DIR}")
    print(f"  新增        {len(fresh):>5} 个  {_mb(fresh, _LEGACY_DIR):.1f} MB")
    print(f"  已有且相同  {len(identical):>5} 个  (跳过)")
    print(f"  同名不同内容{len(clashing):>5} 个  (按 mtime 取新)")
    for rel in clashing:
        src, dst = _LEGACY_DIR / rel, _TARGET_DIR / rel
        winner = "源" if src.stat().st_mtime >= dst.stat().st_mtime else "目标"
        print(f"      {rel}  ->  留{winner}那份")

    if not args.apply:
        print("\n以上是演练。确认无误后加 --apply 真正执行。")
        return 0

    copied = 0
    for rel in fresh:
        _copy(_LEGACY_DIR / rel, _TARGET_DIR / rel)
        copied += 1
    for rel in clashing:
        src, dst = _LEGACY_DIR / rel, _TARGET_DIR / rel
        if src.stat().st_mtime >= dst.stat().st_mtime:
            _copy(src, dst)
            copied += 1

    # 核对:每个源文件在目标处都必须存在。核不过就不许删源。
    missing = [rel for rel in _iter_files(_LEGACY_DIR) if not (_TARGET_DIR / rel).exists()]
    if missing:
        print(
            f"\n[错误] 搬完还有 {len(missing)} 个文件没落到目标,源目录一律不动。",
            file=sys.stderr,
        )
        for rel in missing[:10]:
            print(f"        {rel}", file=sys.stderr)
        return 1

    print(f"\n[完成] 复制 {copied} 个文件,全部核对通过。")
    if args.remove_source:
        shutil.rmtree(_LEGACY_DIR)
        print(f"[完成] 已删除 {_LEGACY_DIR}。")
    else:
        print(f"[提示] 源目录留着没动。确认新数据一切正常后再删:rm -rf {_LEGACY_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
