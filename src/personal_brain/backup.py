"""备份与恢复校验（§14.3、§3.1）。

- 使用 SQLite 在线 backup API 生成一致性快照（正确处理 WAL，
  不允许运行时仅复制主文件，§3.1 硬约束）。
- 备份包含：数据库一致性快照 + Raw 归档目录副本 + manifest。
- 撤回/清除登记保存在数据库内：撤回后的备份恢复出来同样保持
  withdrawn，不会使已删除内容复活（§14.3 恢复顺序要求的存储层保证）。
- ``restore_check``：完整性校验 + 外键校验 + 迁移版本 + 计数摘要 +
  manifest 摘要比对。完整恢复流程（先应用撤回登记再开放检索）在
  存储层天然满足；面向用户的 restore 向导属后续阶段。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from personal_brain import __version__


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackupResult:
    backup_dir: Path
    snapshot_path: Path
    snapshot_sha256: str
    archive_files: int
    created_at: str
    integrity: str = "ok"


@dataclass
class RestoreCheckResult:
    backup_dir: Path
    integrity: str
    foreign_key_violations: int
    schema_version: int
    counts: dict[str, int] = field(default_factory=dict)
    snapshot_sha256: str = ""
    manifest_ok: bool = True


def backup(
    db_path: Path,
    backup_dir: Path,
    archive_dir: Path | None = None,
) -> BackupResult:
    """生成一致性备份目录：snapshot.sqlite + archives/ + manifest.json。"""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")
    backup_dir = Path(backup_dir)
    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    dest = backup_dir / f"backup-{created_at.replace(':', '')}"
    dest.mkdir(parents=True, exist_ok=False)

    snapshot_path = dest / "snapshot.sqlite"
    tmp_path = dest / "snapshot.sqlite.tmp"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(tmp_path))
        try:
            src.backup(dst)  # 一致性快照，包含 WAL 内容
            dst.execute("PRAGMA integrity_check")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    integrity = sqlite3.connect(str(tmp_path)).execute(
        "PRAGMA integrity_check"
    ).fetchone()[0]
    if integrity != "ok":
        tmp_path.unlink(missing_ok=True)
        dest.rmdir()
        raise RuntimeError(f"备份完整性校验失败: {integrity}")
    tmp_path.replace(snapshot_path)

    archive_files = 0
    if archive_dir is not None and Path(archive_dir).exists():
        for p in Path(archive_dir).rglob("*"):
            if p.is_file():
                archive_files += 1
        shutil.copytree(archive_dir, dest / "archives", dirs_exist_ok=True)

    counts = _table_counts(snapshot_path)
    snapshot_sha = _sha256_file(snapshot_path)
    manifest = {
        "created_at": created_at,
        "app_version": __version__,
        "snapshot_sha256": snapshot_sha,
        "archive_files": archive_files,
        "counts": counts,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return BackupResult(
        backup_dir=dest,
        snapshot_path=snapshot_path,
        snapshot_sha256=snapshot_sha,
        archive_files=archive_files,
        created_at=created_at,
    )


def restore_check(backup_path: Path) -> RestoreCheckResult:
    """校验备份可用性：完整性、外键、迁移版本、计数、manifest 摘要。"""
    backup_path = Path(backup_path)
    snapshot = backup_path / "snapshot.sqlite" if backup_path.is_dir() else backup_path
    if not snapshot.exists():
        raise FileNotFoundError(f"备份快照不存在: {snapshot}")
    manifest_ok = True
    sha = ""
    manifest_path = (
        backup_path / "manifest.json" if backup_path.is_dir() else None
    )
    if manifest_path and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sha = str(manifest.get("snapshot_sha256", ""))
        actual = _sha256_file(snapshot)
        if manifest.get("snapshot_sha256") != actual:
            manifest_ok = False
    conn = sqlite3.connect(str(snapshot))
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        schema_version = int(version_row["v"]) if version_row else 0
        counts = _table_counts(snapshot)
    finally:
        conn.close()
    return RestoreCheckResult(
        backup_dir=backup_path,
        integrity=str(integrity),
        foreign_key_violations=fk_violations,
        schema_version=schema_version,
        counts=counts,
        snapshot_sha256=sha,
        manifest_ok=manifest_ok,
    )


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            "sources",
            "source_snapshots",
            "events",
            "event_revisions",
            "revision_occurrences",
            "conversation_nodes",
            "import_jobs",
        ]
        out: dict[str, int] = {}
        for t in tables:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out
    finally:
        conn.close()
