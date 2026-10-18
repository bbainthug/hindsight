"""每日备份轮换：只保留最新 N 份完整备份。"""

from __future__ import annotations

import pytest

from personal_brain.backup import backup, prune_backups
from personal_brain.history.db import connect


def _fake_backup(root, stamp, complete=True):
    d = root / f"backup-{stamp}"
    d.mkdir()
    (d / "snapshot.sqlite").write_bytes(b"x")
    if complete:
        (d / "manifest.json").write_text("{}")
    return d


STAMPS = [f"2026-10-{day:02d}T133000.000000Z" for day in range(1, 6)]


def test_keeps_newest_n(tmp_path):
    dirs = [_fake_backup(tmp_path, s) for s in STAMPS]
    removed = prune_backups(tmp_path, keep=2)
    assert removed == dirs[:3]
    assert sorted(p.name for p in tmp_path.iterdir()) == [d.name for d in dirs[3:]]


def test_leaves_other_files_and_incomplete_backups(tmp_path):
    for s in STAMPS:
        _fake_backup(tmp_path, s)
    partial = _fake_backup(tmp_path, "2026-09-30T133000.000000Z", complete=False)
    (tmp_path / "backup.log").write_text("log")
    (tmp_path / "vm-snapshot.sqlite").write_bytes(b"x")
    other = tmp_path / "backup-manual"
    other.mkdir()
    prune_backups(tmp_path, keep=1)
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {
        f"backup-{STAMPS[-1]}", partial.name, "backup.log", "vm-snapshot.sqlite", "backup-manual",
    }


def test_never_deletes_protected(tmp_path):
    dirs = [_fake_backup(tmp_path, s) for s in STAMPS]
    removed = prune_backups(tmp_path, keep=1, protect=dirs[0])
    assert dirs[0].exists() and dirs[-1].exists()
    assert removed == dirs[1:4]


def test_keep_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        prune_backups(tmp_path, keep=0)


def test_missing_dir_is_noop(tmp_path):
    assert prune_backups(tmp_path / "nope", keep=3) == []


def test_real_backup_then_prune(tmp_path):
    db = tmp_path / "db.sqlite"
    connect(db).close()  # 应用全部迁移的空库
    out = tmp_path / "backups"
    out.mkdir()
    for s in STAMPS:
        _fake_backup(out, s)
    result = backup(db, out)
    removed = prune_backups(out, keep=2, protect=result.backup_dir)
    remaining = sorted(p.name for p in out.iterdir())
    assert result.backup_dir.name in remaining and len(remaining) == 2
    assert len(removed) == 4
