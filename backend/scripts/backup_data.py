"""把「丢了就再也回不来」的那几样备出来,并**当场验证备份能打开**。

为什么需要它(2026-08-21 五路复查里最硬的一条):
    ``docker-compose.vps.yml`` 七个服务,**没有一个是备份**。唯一的「备份」是
    ``docs/W7_上线实录与部署踩坑.md`` 里一条要人在部署前手动跑的 rsync ——
    而那条 rsync **已经删掉过一次对话历史、改过 data/ 属主**(§2.3 / §2.5)。
    也就是说:防数据丢失的机制,本身就是已经出过事的那一个,而且要人记得跑。

    库里装着**已签发的《工程暂停令》/《复工令》记录** —— 那是法律文书的台账。

===============================================================================
备什么、不备什么(这个取舍是刻意的,别顺手扩大)
-------------------------------------------------------------------------------
备:
  · ``gyt.sqlite3``   88 KB —— 隐患台账、文书记录、任务、考勤、项目。**核心**
  · ``langgraph/``    对话历史(2026-08-21 清空过一次,现在 60 KB;会长)

不备(**并且这不是遗漏,是取舍**):
  · ``artifacts/``  122 MB —— 巡检记录 docx、监理文书、考勤凭证图、工地照片。
        它同样不可再生,但每天全量拷一份 × 保留 N 天,在一块只剩 9 GB 的盘上
        是不负责任的。它的正确解法是**同步到机器外面**(对象存储 / 另一台机),
        不是在同一块盘上多放几份 —— 盘坏了两份一起没。
        ⚠️ 在那之前,artifacts 只有部署手册里那条手动 rsync 兜着。**这是已知缺口。**
  · ``chroma/`` ``cache/`` ``cad_index/`` —— 生成物,能重建
  · ``projects/`` ``global/`` —— 用户传的图纸与规范,原件在传的人手里
  · ``demo/`` —— 演示素材,在 git 里

===============================================================================
🔴 为什么不能 ``cp`` 或 rsync 单个库文件
-------------------------------------------------------------------------------
2026-08-21 起 SQLite 开了 **WAL**(``core/sqlite_util.py``),于是库由**三个**文件
组成:``gyt.sqlite3`` / ``-wal`` / ``-shm``。拷贝其中一个拿到的是不完整状态,
而且**不会报错** —— 恢复的时候才发现少了最近的写。

这里走 ``sqlite3.Connection.backup()``:它是 SQLite 的在线备份 API,
边写边备也能拿到一致快照,不需要停服务。

===============================================================================
备完必须验
-------------------------------------------------------------------------------
一个打不开的备份比没有备份更坏 —— 它让人以为自己有退路。所以每次备完当场:
  ① ``PRAGMA integrity_check`` 必须回 ok
  ② 关键表必须都在,而且行数与源库一致
两条有一条不过就**删掉这份半截备份并以非零码退出** —— 留着它会被下一次轮转
当成一份好备份留下来。

用法::

    python -m scripts.backup_data                    # 备到 --backup-dir(默认 /backups)
    python -m scripts.backup_data --keep 30          # 多留几份
    python -m scripts.backup_data --dry-run          # 只说要干什么,不动手

===============================================================================
退出码(``docker-compose.vps.yml`` 的守护循环靠它分流,改之前先看那边)
-------------------------------------------------------------------------------
  0  备好了
  1  **源库还不在** —— 全新部署上后端还没跑过,属于「等一会儿再来」,不是故障。
     守护循环遇到 1 只记一句然后接着睡;当成故障退出的话,一台干净的机器
     会因为「还没人用过系统」而反复重启备份容器,而那是完全正常的状态。
  2  **备份没通过验证** —— 这是真故障。半截备份已经被删掉,首轮遇到 2 就让
     容器退出,靠 ``docker compose ps`` 里的 Restarting 让人看见
     (backend 那条注释里已经在用这个信号)。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

# 这个脚本在容器里用 `python -m scripts.backup_data` 跑,工作目录是 /app,
# 而 gyt 包装在 /app/src 下 —— 与 backend/scripts/ 里其它脚本同一个姿势。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gyt.config import get_settings  # noqa: E402

_log = logging.getLogger("backup")

#: 备份落在哪儿。**必须在 data_dir 外面** —— 放里面的话下一次备份会把上一次
#: 也装进去,几轮之后每份备份都套着前面所有份(而且 tar 会越来越大,没人会注意)。
_DEFAULT_BACKUP_DIR = Path("/backups")

#: 默认留多少份。库只有几十 KB,14 份日备加起来一两 MB。
#: 真正会占地方的是对话历史,那一项另有体积上限兜着(见 ``_archive_dir``)。
_DEFAULT_KEEP = 14

#: 对话历史超过这么大就跳过不备。为什么必须有这个数,见 ``_archive_dir`` 的红字。
#: 50 × 14 = 700 MB 是最坏情况;而线上那块盘只剩 9 GB,这个上限是按它定的。
_DEFAULT_MAX_HISTORY_MB = 50.0

#: 核心台账表 —— **只用来提醒,不用来判成败**。
#:
#: 🔴 别把它改成硬判据(我第一版就是,当场发现是错的):各域的表是
#: ``open_db`` 进场时按需建的,**新部署上某个域还没被人用过,它的表就不存在**。
#: 拿这张清单当判据的话,一台干净的机器第一次备份就会「验证失败」并自删 ——
#: 而那是完全正常的状态。假警报会训练人忽略这里的报错,比不报还坏。
#:
#: 真正的判据是**源库有什么、备份就得有什么**(见 ``_verify_sqlite``),
#: 那条不依赖任何硬编码清单,加新域也不用回来改。
_CORE_TABLES = (
    "tasks",
    "attendance",
    "projects",
    "drawings",
    "hazards",
    "hazard_docs",
    "hazard_ingest_failures",
)


def _stamp() -> str:
    """备份目录名。用 UTC 是刻意的:排序即时序,不受宿主时区改动影响。

    ⚠️ 和 ``attendance/receipt.py`` 那个「香港时间权威」是两回事 —— 那个是给人看的
    业务时刻,这个是运维用的文件名。别把两者统一。
    """
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")


def _backup_sqlite(src: Path, dst: Path) -> None:
    """在线备份:边写边备也能拿到一致快照,不用停服务(见模块头注)。"""
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def _tables_of(conn: sqlite3.Connection) -> set[str]:
    """库里的业务表名。``sqlite_%`` 那几张是 sqlite 自己的账本,不算。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _verify_sqlite(src: Path, dst: Path) -> tuple[list[str], list[str]]:
    """验证备份。返回 ``(致命问题, 提醒)`` —— 只有前者会让这次备份判负。

    判据是**源库有什么、备份就得有什么**,不依赖任何硬编码表名:
      ① ``PRAGMA integrity_check`` 回 ok
      ② 源库的每一张业务表,备份里都得有
      ③ 每张表的行数,备份**不许少于**源库

    ③ 只查少不查多是刻意的:备份是在线做的,源库这会儿可能又被写了几行,
    备份少一行属于正常竞态;而备份**比源少**才是真备漏了。
    """
    fatal: list[str] = []
    notes: list[str] = []
    try:
        with sqlite3.connect(dst) as conn, sqlite3.connect(src) as origin:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                fatal.append(f"完整性检查没过:{integrity}")

            src_tables = _tables_of(origin)
            dst_tables = _tables_of(conn)

            missing = sorted(src_tables - dst_tables)
            if missing:
                fatal.append(f"源库有而备份里没有的表:{', '.join(missing)}")

            for table in sorted(src_tables & dst_tables):
                # 表名来自 sqlite_master、不是外部输入,拼进 SQL 不违反「全参数化」
                # 那条红线(它管的是**运行期的值**)。表名也没法用占位符。
                got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                want = origin.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                if got < want:
                    fatal.append(f"{table} 只备到 {got} 行,源库有 {want} 行")

            # 提醒档:核心台账表还没建起来。新部署上完全正常(那个域还没被人用过),
            # 所以**不判负** —— 但要说一声,免得有人拿着一份「没有隐患表」的备份
            # 以为自己备到了隐患台账。
            absent = [t for t in _CORE_TABLES if t not in src_tables]
            if absent:
                notes.append(f"源库里还没有这几张表(对应的功能还没被用过):{', '.join(absent)}")
    except sqlite3.Error as exc:
        fatal.append(f"备份打不开:{exc}")
    return fatal, notes


def _dir_size_mb(path: Path) -> float:
    """目录里所有文件加起来多少 MB。"""
    if not path.is_dir():
        return 0.0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1024 / 1024


def _archive_dir(src: Path, dst: Path, *, max_mb: float) -> tuple[int, str | None]:
    """把一个目录打成 .tar.gz。返回 ``(文件数, 跳过的理由或 None)``。

    🔴 **必须有体积上限。** 对话历史那个目录是会长的,而且能长得很吓人:
    2026-08-21 线上实测,11 条带照片的会话就把它顶到 **1.1 GB**
    (langgraph 的内存库每 10 秒无条件全量 pickle 一次,base64 全躺在里面)。
    那天之后照片改走直传、不再进检查点,而且清空过一次,现在只有 60 KB ——
    但「现在小」不等于「以后一直小」,而这块盘只剩 9 GB。

    没有上限的话,失败方式很难看:备份把盘吃满 → 后端写库失败 → 而这一切
    起因于那个本来用来防数据丢失的东西。所以宁可**跳过并大声说**,
    也不要悄悄把盘填满。
    """
    if not src.is_dir():
        return 0, None
    size_mb = _dir_size_mb(src)
    if size_mb > max_mb:
        return 0, (
            f"对话历史有 {size_mb:.0f} MB,超过 {max_mb:.0f} MB 的上限,这次没备。"
            f"台账已经备好了(那才是要紧的);历史要留就先查查 {src} 为什么这么大。"
        )
    count = 0
    with tarfile.open(dst, "w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(src)))
                count += 1
    return count, None


def _prune(backup_dir: Path, keep: int, *, dry_run: bool) -> list[str]:
    """只留最近 ``keep`` 份,删掉更老的。返回删掉(或将要删掉)的名字。

    ⚠️ 只认自己建的那种目录名(``_stamp()`` 的形状),别的一律不碰 ——
    这个目录万一被人拿来放别的东西,不该被这里删掉。
    """
    if not backup_dir.is_dir():
        return []  # 第一次跑(尤其是 --dry-run):目录还没建,没有可轮转的
    kept = sorted(
        (p for p in backup_dir.iterdir() if p.is_dir() and _looks_like_snapshot(p.name)),
        reverse=True,
    )
    doomed = kept[keep:]
    for path in doomed:
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    return [p.name for p in doomed]


def _looks_like_snapshot(name: str) -> bool:
    """``20260821-193012Z`` 这个形状。判据写严一点,免得误删别人的目录。"""
    return (
        len(name) == 16
        and name[8] == "-"
        and name.endswith("Z")
        and name[:8].isdigit()
        and name[9:15].isdigit()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份工友通的台账与对话历史")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=_DEFAULT_BACKUP_DIR,
        help=f"备份落在哪儿(默认 {_DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument("--keep", type=int, default=_DEFAULT_KEEP, help="保留几份")
    parser.add_argument(
        "--max-history-mb",
        type=float,
        default=_DEFAULT_MAX_HISTORY_MB,
        help=(
            f"对话历史超过这么大就跳过不备"
            f"(默认 {_DEFAULT_MAX_HISTORY_MB:.0f} MB,理由见 _archive_dir)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="只说要干什么,不动手")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = get_settings()
    src_db = settings.sqlite_path
    if not src_db.exists():
        _log.error("库文件不在:%s —— 后端还没跑过?先别把这当成备份失败。", src_db)
        return 1

    backup_dir: Path = args.backup_dir
    snapshot = backup_dir / _stamp()

    if args.dry_run:
        _log.info("[演练] 会备到 %s", snapshot)
        _log.info("[演练]   %s -> gyt.sqlite3", src_db)
        _log.info("[演练]   %s -> langgraph.tar.gz", settings.data_dir / "langgraph")
        _log.info("[演练] 会删掉:%s", _prune(backup_dir, args.keep, dry_run=True) or "(没有)")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)

    dst_db = snapshot / "gyt.sqlite3"
    _backup_sqlite(src_db, dst_db)

    fatal, notes = _verify_sqlite(src_db, dst_db)
    for line in notes:
        _log.warning("%s", line)
    if fatal:
        # 半截备份必须删掉:留着它会被下一轮轮转当成一份好备份保留下来,
        # 而恢复的人要到最需要它的那一刻才发现它是坏的。
        shutil.rmtree(snapshot, ignore_errors=True)
        for line in fatal:
            _log.error("备份没通过验证:%s", line)
        _log.error("这份半截备份已删掉 —— 一个打不开的备份比没有备份更坏。")
        return 2

    files, skipped = _archive_dir(
        settings.data_dir / "langgraph",
        snapshot / "langgraph.tar.gz",
        max_mb=args.max_history_mb,
    )
    if skipped:
        _log.warning("%s", skipped)

    size_mb = _dir_size_mb(snapshot)
    dropped = _prune(backup_dir, args.keep, dry_run=False)

    _log.info(
        "备好了:%s(台账表齐、行数对得上;对话历史 %d 个文件;这份 %.1f MB,备份目录合计 %.1f MB)",
        snapshot.name,
        files,
        size_mb,
        _dir_size_mb(backup_dir),
    )
    if dropped:
        _log.info("轮转掉 %d 份老的:%s", len(dropped), ", ".join(dropped))
    _log.info("⚠️ artifacts/ 不在这份备份里(理由见本脚本头注),它仍然只有手动 rsync 兜着。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
