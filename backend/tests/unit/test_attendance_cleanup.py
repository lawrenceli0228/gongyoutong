"""考勤凭证图清理器(attendance/cleanup.py)的单元测试。

**全走真存储层 + 真 artifacts**(conftest 已把数据目录隔离到 tmp),不打桩 ——
清理器删的是真文件、置空的是真行,mock 掉任何一层都等于没测:
它最要命的失败模式恰恰是「各层单独都对、拼起来删错东西」。

盯死的五件事(W7 §3.9):
  · 到期的删且行置空,未到期的原样;
  · 老化窗口 —— register 成功但 INSERT 未落库的正常请求不许误删;
  · 非 ATTENDANCE 的孤儿永不碰(别的业务没授权任何人删它们的文件);
  · 演练模式什么都不动(与 migrate_data_dir.py 同惯例);
  · 重复跑幂等。
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gyt.attendance import cleanup
from gyt.attendance.receipt import make_snapshot
from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.db import attendance as attendance_db
from gyt.db.attendance import CheckinDraft

PAYLOAD = b"\xff\xd8\xff" + "假的凭证图字节".encode()


def _register(kind: ArtifactKind = ArtifactKind.ATTENDANCE) -> str:
    return artifacts.register(PAYLOAD, kind=kind, original_name="checkin.jpg")


def _insert_row(*, work_date: str, artifact_id: str | None, tag: str) -> None:
    """插一条最小合法打卡行。时间字段全用 work_date 拼 —— 清理只看 work_date。"""
    attendance_db.insert_checkin(
        CheckinDraft(
            event_id=f"evt-{tag}",
            req_digest="0" * 64,
            worker_name="张三",
            site_name="测试地盤",
            checked_at=f"{work_date}T08:00:00+08:00",
            work_date=work_date,
            lat=None,
            lon=None,
            accuracy_m=None,
            geo_status="absent",
            source="camera",
            receipt_no=f"GYT-A-TEST-{tag}",
            artifact_id=artifact_id,
            created_at=f"{work_date}T08:00:00+08:00",
        )
    )


def _row(tag: str):
    row = attendance_db.find_by_event_id(f"evt-{tag}")
    assert row is not None
    return row


def _dates() -> tuple[str, str]:
    """(明确到期的日期, 今天)。到期日期取留存天数再往前 5 天,离边界远远的 ——
    这里测的是「到期会删」,边界语义(恰满当天不删)由 db 层的 < 号背着。"""
    snap = make_snapshot()
    retention = get_settings().attendance_retention_days
    expired = (snap.stamp.date() - timedelta(days=retention + 5)).isoformat()
    return expired, snap.work_date


def _sidecar_of(artifact_id: str) -> Path:
    matches = list(get_settings().artifacts_dir.glob(f"*/{artifact_id}.json"))
    assert len(matches) == 1, f"应当有且只有一份 sidecar,实际 {matches}"
    return matches[0]


def _age_sidecar(artifact_id: str, *, hours: float) -> None:
    """把 sidecar 的 created_at 往回拨,模拟「登记已久」的孤儿。"""
    sidecar = _sidecar_of(artifact_id)
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    meta["created_at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


# --- 段①:留存到期 -----------------------------------------------------------


def test_到期的删图且行置空_未到期的原样():
    expired_date, today = _dates()
    expired_art = _register()
    fresh_art = _register()
    _insert_row(work_date=expired_date, artifact_id=expired_art, tag="old")
    _insert_row(work_date=today, artifact_id=fresh_art, tag="new")

    assert cleanup.main(["--apply"]) == 0

    # 到期:文件没了、行还在、id 置空、清理时间记上
    with pytest.raises(ArtifactNotFound):
        artifacts.resolve(expired_art)
    old_row = _row("old")
    assert old_row.artifact_id is None
    assert old_row.photo_purged_at is not None
    # 未到期:文件、行都原样
    assert artifacts.resolve(fresh_art).read_bytes() == PAYLOAD
    new_row = _row("new")
    assert new_row.artifact_id == fresh_art
    assert new_row.photo_purged_at is None


# --- 段②:孤儿清扫 -----------------------------------------------------------


def test_老化窗口_刚登记的孤儿不删_做旧后才删():
    orphan = _register()  # 没有任何行引用 —— 正在进行的打卡此刻就长这样

    assert cleanup.main(["--apply"]) == 0
    assert artifacts.resolve(orphan).read_bytes() == PAYLOAD, "窗口内的孤儿不许删"

    _age_sidecar(orphan, hours=get_settings().attendance_cleanup_min_age_h + 2)
    assert cleanup.main(["--apply"]) == 0
    with pytest.raises(ArtifactNotFound):
        artifacts.resolve(orphan)


def test_created_at缺失时按mtime兜底():
    orphan = _register()
    sidecar = _sidecar_of(orphan)
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    del meta["created_at"]
    sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    old = time.time() - (get_settings().attendance_cleanup_min_age_h + 2) * 3600
    os.utime(sidecar, (old, old))

    assert cleanup.main(["--apply"]) == 0

    with pytest.raises(ArtifactNotFound):
        artifacts.resolve(orphan)


def test_有行引用的图不算孤儿():
    _, today = _dates()
    referenced = _register()
    _insert_row(work_date=today, artifact_id=referenced, tag="ref")
    _age_sidecar(referenced, hours=get_settings().attendance_cleanup_min_age_h + 2)

    assert cleanup.main(["--apply"]) == 0

    assert artifacts.resolve(referenced).read_bytes() == PAYLOAD


def test_非考勤kind的孤儿永不碰():
    """巡检照片 / 图纸 / 报告没有任何行引用也不许动 —— 留存策略只对考勤图定义过。"""
    photo = _register(kind=ArtifactKind.PHOTO)
    drawing = _register(kind=ArtifactKind.DRAWING)
    for artifact_id in (photo, drawing):
        _age_sidecar(artifact_id, hours=get_settings().attendance_cleanup_min_age_h + 24)

    assert cleanup.main(["--apply"]) == 0

    assert artifacts.resolve(photo).read_bytes() == PAYLOAD
    assert artifacts.resolve(drawing).read_bytes() == PAYLOAD


# --- 演练与幂等 --------------------------------------------------------------


def test_演练模式什么都不动(capsys: pytest.CaptureFixture[str]):
    expired_date, _ = _dates()
    expired_art = _register()
    _insert_row(work_date=expired_date, artifact_id=expired_art, tag="dry")
    orphan = _register()
    _age_sidecar(orphan, hours=get_settings().attendance_cleanup_min_age_h + 2)

    assert cleanup.main([]) == 0

    out = capsys.readouterr().out
    assert "将删" in out, "演练要把会删什么原样打出来"
    assert "演练" in out
    # 文件与行全部原样
    assert artifacts.resolve(expired_art).read_bytes() == PAYLOAD
    assert artifacts.resolve(orphan).read_bytes() == PAYLOAD
    assert _row("dry").artifact_id == expired_art
    assert _row("dry").photo_purged_at is None


def test_重复跑幂等():
    expired_date, _ = _dates()
    expired_art = _register()
    _insert_row(work_date=expired_date, artifact_id=expired_art, tag="idem")
    orphan = _register()
    _age_sidecar(orphan, hours=get_settings().attendance_cleanup_min_age_h + 2)

    assert cleanup.main(["--apply"]) == 0
    first_purged_at = _row("idem").photo_purged_at

    assert cleanup.main(["--apply"]) == 0, "第二轮没有可删的,也必须干净跑完"

    row = _row("idem")
    assert row.artifact_id is None
    assert row.photo_purged_at == first_purged_at, "重复跑不许刷新第一次的清理时间"
    with pytest.raises(ArtifactNotFound):
        artifacts.resolve(expired_art)
    with pytest.raises(ArtifactNotFound):
        artifacts.resolve(orphan)
