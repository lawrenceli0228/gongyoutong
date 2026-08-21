"""考勤凭证图清理器:留存到期删图 + 孤儿清扫。**默认演练,``--apply`` 才动手。**

「演练先行」照搬 scripts/migrate_data_dir.py 的惯例(那边的头注同此互指):
删除不可逆,先把「会删什么」原样打出来给人看,确认无误再加 ``--apply``。

跑法:
    python -m gyt.attendance.cleanup            # 演练:只打印,不动任何东西
    python -m gyt.attendance.cleanup --apply    # 真删

两段逻辑,**顺序不许换**:

  ① **留存到期**(W7 §3.9):``work_date`` 早于「今天(香港)- 留存天数」的行,
     删图、行保留(``artifact_id`` 置 NULL、``photo_purged_at`` 记时间,
     界面据此显示「凭证图已过期清理」)。
     **先删文件、后置空行**:反过来若置空成功而删文件失败,那张图从此没有任何
     引用 —— 不止是孤儿(孤儿下一轮还能扫到),更糟的是**审计已经说「已清理」
     而文件还躺在盘上**,PDPO 层面这是假话。先删文件的残局只是「删了图、
     行还挂着 id」→ 死链,而本脚本重跑幂等,下一轮就把行补上。

  ② **孤儿清扫**:磁盘上 kind=ATTENDANCE 的 sidecar 有、库里没有任何行引用的图。
     正常来源是 checkin_api 的写入顺序(先落图后写库,§3.3):register 成功、
     INSERT 失败,图就成了孤儿(无害,但只进不出会吃满盘)。
     放在 ① 之后:① 会把到期行的 ``artifact_id`` 置 NULL,之后再取「在用集合」
     拿到的才是清理后的真相;反过来会把刚清完的 id 当在用,孤儿要多等一轮
     (不是错误,但没必要)。
     **老化窗口**(``attendance_cleanup_min_age_h``,W7 §3.9 / 上线方案 §3.9):
     登记时间在窗口内的孤儿**不许删** —— register 已成功而 INSERT 还没落库的
     正常请求,在这一瞬间看起来就是孤儿,删了它等于把正在进行的打卡搞成死链。

红线(CLAUDE.md「SQL 列表查询一次取回」):在用集合由
``list_active_artifact_ids()`` **一次 SELECT** 取回建成 set,目录遍历只做
``in`` 判断 —— 不许在循环里逐个回库查。清理器跑在定时任务里,慢了没人看见,
会一直慢下去。

===============================================================================
谁在调它(2026-08-21 之前:**没有人**)
-------------------------------------------------------------------------------
🔴 上面那句「清理器跑在定时任务里」从 W7 写下来的那天起就是**愿望,不是事实**。
2026-08-21 复查时上机器查过:``crontab -l`` 是 ``no crontab for root``,
``systemctl list-timers`` 里也没有任何相关条目。**这个模块从来没被自动调起来过。**

同时登录页那份 PDPO(第486章)个人资料收集声明白纸黑字写着
「憑證照片保存 90 天後自動刪除」—— 也就是说,那是一句对使用者的承诺,
而它背后一直是空的。(当时最老的凭证图 6 天,到期日在 11 月 13 日,
所以还没有真的违反,但机制必须在那之前就位。)

现在它由 ``docker-compose.vps.yml`` 的 **housekeeping 服务**每天调一次,
带 ``--apply``。三条跟着这个落点来的约束:
  · 顺序是「先备份、再删图」—— 反过来的话,删完还没来得及备就崩,
    那批图连同台账行一起没了;
  · ``--apply`` 不是可选项。不带它只演练、一个字节都不删,而日志看着完全正常;
  · 退出码 1(有该删没删掉的)只记一句、不让容器退出 —— 单张删不掉不该把
    整个定时活儿停掉,下一轮还会再试。

⚠️ **别再回去开宿主 cron**:宿主配置不进 git、review 不到、换机器不跟着走,
那正是这个模块从 W7 到今天一直没人调的成因本身。

非 ATTENDANCE 的产物(巡检照片、图纸、报告)**一律不碰**,连候选都不进 ——
留存策略只对考勤图定义过(§3.9),别的业务没授权任何人删它们的文件。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gyt.attendance.receipt import TimeSnapshot, make_snapshot
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.db import attendance as attendance_db

_SIDECAR_GLOB = "*/*.json"
"""sidecar 的目录形状:``<artifacts_dir>/<YYYYMMDD>/<32位hex>.json``。
形状与下面读的字段(``kind`` / ``created_at``,取值如 "ATTENDANCE"、UTC ISO)
都以 core/artifacts.py 的 ``register()`` 写入的 sidecar 为准 —— 那边改存法,
这里要跟着改。"""


def _registered_at(meta: dict[str, Any], sidecar: Path) -> datetime:
    """孤儿的「登记时间」:优先 sidecar 里的 ``created_at``,取不到才退回文件 mtime。

    为什么优先 created_at:它是 register() 写死在元数据里的**业务事实**(UTC ISO);
    而 mtime 是文件系统属性,备份还原、跨盘复制、docker 卷迁移都可能把它刷成
    「现在」。mtime 只当 created_at 缺失/解析不了时的兜底 —— 兜底方向是安全的:
    mtime 被刷新只会让孤儿显得更年轻、**更不会**被删(保守),不会误删。
    """
    raw = meta.get("created_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed
    return datetime.fromtimestamp(sidecar.stat().st_mtime, tz=UTC)


def _purge_expired(*, apply: bool, snap: TimeSnapshot) -> tuple[int, int, int]:
    """段① 留存到期。返回 (候选, 删了, 删失败)。

    cutoff 用 ``work_date < cutoff`` 的语义(db 层如此),即恰好满留存天数的
    当天**还留着**,多留不多删 —— 留存是下限承诺,不是精确到时刻的删除义务。
    """
    settings = get_settings()
    cutoff = (snap.stamp.date() - timedelta(days=settings.attendance_retention_days)).isoformat()
    candidates = attendance_db.list_expired_photos(cutoff)
    print(
        f"留存到期(work_date < {cutoff},留存 {settings.attendance_retention_days} 天):"
        f"候选 {len(candidates)} 张"
    )
    if not apply:
        for cand in candidates:
            print(f"  [将删] 行 {cand.row_id} 的凭证图 {cand.artifact_id}")
        return len(candidates), 0, 0

    purged_rows: list[int] = []
    failed = 0
    for cand in candidates:
        try:
            # 返回值**故意不看**:True(真删了)与 False(本来就没有)对我们是
            # 同一件事 —— 目标都是「这个 id 不再指向任何文件」,达成即可置空。
            # 上一轮删了一半(删图成功、置空失败)的行,重跑走的正是 False 这条路。
            # 2026-08-15 合流:artifacts.delete 统一成队友那版(6 个调用方 vs 我 2 个),
            # 不再有 missing_ok 参数、也不再抛 ArtifactNotFound,只剩 OSError 要防。
            artifacts.delete(cand.artifact_id)
        except OSError as exc:
            # 删不掉就**不置空**:行上的 id 留着,下一轮还会再试。
            # 顺序铁律(先文件后行)就是为了保住这个重试机会。
            print(f"  [失败] 行 {cand.row_id} 的图 {cand.artifact_id} 删不掉:{exc}")
            failed += 1
            continue
        purged_rows.append(cand.row_id)
    touched = attendance_db.mark_photos_purged(purged_rows, snap.checked_at)
    print(f"  已删图 {len(purged_rows)} 张,行置空 {touched} 条,删失败 {failed} 张")
    return len(candidates), len(purged_rows), failed


def _sweep_orphans(*, apply: bool, snap: TimeSnapshot) -> tuple[int, int, int]:
    """段② 孤儿清扫。返回 (候选, 删了, 删失败)。

    「窗口内放过」不算候选也不算失败 —— 那是正在进行的打卡的正常长相,
    出现频率与业务量同阶,算进失败会让定时任务天天报红、真失败反而没人看。
    """
    settings = get_settings()
    active = attendance_db.list_active_artifact_ids()  # 一次取回建集合(红线,见头注)
    min_age = timedelta(hours=settings.attendance_cleanup_min_age_h)
    candidates = 0
    deleted = 0
    failed = 0
    spared = 0
    attendance_kind = artifacts.ArtifactKind.ATTENDANCE.value
    for sidecar in sorted(settings.artifacts_dir.glob(_SIDECAR_GLOB)):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 读不出 kind 就不知道是谁家的产物 —— 一律不碰(宁可漏清,不可错删)。
            continue
        if not isinstance(meta, dict) or meta.get("kind") != attendance_kind:
            continue  # 非考勤产物永不进候选,连数都不数(头注末段)
        artifact_id = sidecar.stem
        if artifact_id in active:
            continue
        age = snap.stamp - _registered_at(meta, sidecar)
        if age < min_age:
            # 老化窗口:register 成功但 INSERT 还没落库的正常打卡,此刻就长这样。
            spared += 1
            print(
                f"  [放过] 孤儿 {artifact_id} 登记才 {age.total_seconds() / 3600:.2f} 小时"
                f"(窗口 {settings.attendance_cleanup_min_age_h} 小时)"
            )
            continue
        candidates += 1
        if not apply:
            print(f"  [将删] 孤儿凭证图 {artifact_id}(已 {age.total_seconds() / 3600:.1f} 小时)")
            continue
        try:
            artifacts.delete(artifact_id)  # 返回值不看,理由同上面那处
        except OSError as exc:
            print(f"  [失败] 孤儿 {artifact_id} 删不掉:{exc}")
            failed += 1
            continue
        deleted += 1
    print(
        f"孤儿清扫:候选 {candidates} 张,删了 {deleted} 张,删失败 {failed} 张,窗口内放过 {spared} 张"
    )
    return candidates, deleted, failed


def main(argv: Sequence[str] | None = None) -> int:
    """入口。``argv`` 显式可注入是给测试的(migrate_data_dir 没这个是它只跑一次)。"""
    parser = argparse.ArgumentParser(
        description="清理考勤凭证图:留存到期 + 孤儿。默认演练,--apply 才动手。"
    )
    parser.add_argument("--apply", action="store_true", help="真的执行(默认只演练)")
    args = parser.parse_args(argv)

    # 单一时间快照(receipt.py 是打卡链唯一的时间权威):cutoff、purged_at、
    # 孤儿年龄判定全部同源,跨秒/跨午夜时三处不会互相对不上。
    snap = make_snapshot()
    mode = "真删" if args.apply else "演练"
    print(f"[{mode}] 考勤凭证图清理 @ {snap.checked_at}")

    _, expired_deleted, expired_failed = _purge_expired(apply=args.apply, snap=snap)
    _, orphan_deleted, orphan_failed = _sweep_orphans(apply=args.apply, snap=snap)

    if not args.apply:
        print("\n以上是演练,什么都没动。确认无误后加 --apply 真正执行。")
        return 0
    print(f"\n[完成] 共删 {expired_deleted + orphan_deleted} 张。")
    failures = expired_failed + orphan_failed
    if failures:
        # 有删不掉的就报非零 —— 定时任务的失败必须能被看见,别静默绿灯。
        print(f"[注意] 有 {failures} 张该删没删掉,详见上方逐条输出。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
