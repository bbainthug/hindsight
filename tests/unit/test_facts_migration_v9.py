"""D-12 回归：已在 v8（含 withdrawal_audit、不含 claims 等表）的库，
打开后能正确升级到 v9 并建出 D-2 的表，且既有数据保持可用。

背景：D-2 的迁移最初编号为 8，与 D-11 的 withdrawal_audit（也是 8）冲突；
rebase 后 D-2 的表改为 migration 9。本地库与 VM 库在 D-11 时已升到 8，
这里模拟真实升级路径。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_brain.history.db import connect, fts_rowid
from personal_brain.history.schema import MIGRATIONS


def _build_v8_db(path: Path) -> None:
    """按迁移 1-8 逐步建库（不经 apply_migrations，停在 v8）。"""
    conn = sqlite3.connect(path)
    conn.create_function("fts_rowid", 1, fts_rowid)  # 迁移 4 的 FTS 回填用到
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    for version, sql in MIGRATIONS:
        if version > 8:
            break
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, "2026-10-03T00:00:00+00:00"),
        )
    conn.commit()
    # 放一条真实的 v8 数据：撤回审计必须跨升级保留。
    conn.execute(
        "INSERT INTO sources(source_id, source_kind, account_namespace, created_at) "
        "VALUES ('src-1', 'other', 'claude', '2026-10-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events(event_id, source_id, conversation_id, source_message_id, "
        "revision_resolution, speaker_type) "
        "VALUES ('ev-1', 'src-1', 'conv-1', 'm-1', 'resolved', 'owner')"
    )
    conn.execute(
        "INSERT INTO withdrawal_audit(event_id, reason, applied_at) "
        "VALUES ('ev-1', 'injected_by_agent:test', '2026-10-03T00:00:00+00:00')"
    )
    conn.commit()
    assert conn.execute("SELECT MAX(version) v FROM schema_migrations").fetchone()[0] == 8
    # v8 上没有 facts 表
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "claims" not in names and "withdrawal_audit" in names
    conn.close()


def test_v8_database_upgrades_to_v9_and_keeps_data(tmp_path):
    db_path = tmp_path / "brain-v8.sqlite"
    _build_v8_db(db_path)

    conn = connect(db_path)  # 打开即升级到最新版
    try:
        assert conn.execute(
            "SELECT MAX(version) v FROM schema_migrations"
        ).fetchone()[0] == MIGRATIONS[-1][0]

        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        # D-2 的表全部建出
        assert {"claims", "claim_revisions", "claim_evidence",
                "claim_relations", "extraction_runs", "review_log"} <= tables
        # D-11 的表与数据原样保留
        row = conn.execute(
            "SELECT event_id, reason FROM withdrawal_audit"
        ).fetchone()
        assert (row["event_id"], row["reason"]) == ("ev-1", "injected_by_agent:test")

        # 升级后 facts 表可直接写入（外键与约束按 v9 生效）
        conn.execute(
            "INSERT INTO claims(claim_id, memory_type, created_at, updated_at) "
            "VALUES ('cl-1', 'preference', '2026-10-03T00:00:00+00:00', "
            "'2026-10-03T00:00:00+00:00')"
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
