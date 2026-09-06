"""SQLite Schema 与顺序迁移。

对应 v0.2 设计 §4 数据契约：
- §4.1 来源与导入快照：sources / source_snapshots / source_assets / import_jobs
- §4.2 事件身份与版本：events / event_revisions / revision_occurrences
  访问标签与 availability 物理上放在单独版本化表（revision_policy_state + 标签关联表），
  修改权限不改写内容 revision，也不重算 revision_hash。
- §4.3 对话树和分支：conversation_nodes / conversation_node_edges /
  conversation_snapshots / conversation_snapshot_nodes / conversation_selected_path
  分支成员关系由图和路径推导，不设 branch_id 列。
- 数组关系使用关联表，不存未校验 JSON。
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE sources (
            source_id         TEXT PRIMARY KEY,
            source_kind       TEXT NOT NULL
                CHECK (source_kind IN ('chatgpt', 'manual', 'wechat', 'other')),
            account_namespace TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'withdrawn', 'purging', 'purged')),
            created_at        TEXT NOT NULL,
            UNIQUE (source_kind, account_namespace)
        );

        CREATE TABLE source_snapshots (
            snapshot_id            TEXT PRIMARY KEY,
            source_id              TEXT NOT NULL REFERENCES sources(source_id),
            archive_sha256         TEXT NOT NULL,
            archive_relative_path  TEXT NOT NULL,
            exported_at            TEXT,
            imported_at            TEXT NOT NULL,
            parser_version         TEXT NOT NULL,
            coverage_start         TEXT,
            coverage_end           TEXT,
            parse_status           TEXT NOT NULL
                CHECK (parse_status IN ('complete', 'partial', 'failed')),
            coverage_notes         TEXT,
            supersedes_snapshot_id TEXT REFERENCES source_snapshots(snapshot_id),
            UNIQUE (source_id, archive_sha256)
        );

        CREATE TABLE source_assets (
            asset_id     INTEGER PRIMARY KEY,
            snapshot_id  TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            member_path  TEXT NOT NULL,
            asset_sha256 TEXT NOT NULL,
            size_bytes   INTEGER NOT NULL,
            asset_kind   TEXT NOT NULL
                CHECK (asset_kind IN ('conversation_data', 'attachment', 'metadata', 'other')),
            event_id     TEXT REFERENCES events(event_id)
        );
        CREATE UNIQUE INDEX ux_source_assets_path
            ON source_assets(snapshot_id, member_path);

        CREATE TABLE import_jobs (
            job_id           TEXT PRIMARY KEY,
            source_id        TEXT NOT NULL REFERENCES sources(source_id),
            archive_sha256   TEXT NOT NULL,
            status           TEXT NOT NULL
                CHECK (status IN ('staged', 'published', 'failed')),
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            conversations_seen INTEGER,
            events_new       INTEGER,
            events_updated   INTEGER,
            revisions_new    INTEGER,
            occurrences_new  INTEGER,
            nodes_new        INTEGER,
            unsupported_count INTEGER
        );
        CREATE INDEX ix_import_jobs_source ON import_jobs(source_id, started_at);

        CREATE TABLE import_job_errors (
            error_id   INTEGER PRIMARY KEY,
            job_id     TEXT NOT NULL REFERENCES import_jobs(job_id),
            error_code TEXT NOT NULL,
            locator    TEXT,
            message    TEXT NOT NULL
        );
        CREATE INDEX ix_import_job_errors_job ON import_job_errors(job_id);

        CREATE TABLE events (
            event_id            TEXT PRIMARY KEY,
            source_id           TEXT NOT NULL REFERENCES sources(source_id),
            conversation_id     TEXT NOT NULL,
            source_message_id   TEXT NOT NULL,
            identity_quality    TEXT NOT NULL DEFAULT 'native'
                CHECK (identity_quality IN ('native', 'fallback')),
            current_revision_id TEXT
                REFERENCES event_revisions(revision_id)
                DEFERRABLE INITIALLY DEFERRED,
            revision_resolution TEXT NOT NULL
                CHECK (revision_resolution IN ('resolved', 'unresolved')),
            speaker_id          TEXT,
            speaker_type        TEXT NOT NULL
                CHECK (speaker_type IN ('owner', 'assistant', 'contact', 'system', 'unknown'))
        );
        CREATE INDEX ix_events_conversation ON events(conversation_id);
        CREATE INDEX ix_events_source ON events(source_id);

        CREATE TABLE event_revisions (
            revision_id       TEXT PRIMARY KEY,
            event_id          TEXT NOT NULL REFERENCES events(event_id),
            revision_hash     TEXT NOT NULL,
            content_type      TEXT NOT NULL,
            raw_text          TEXT,
            search_text       TEXT,
            search_text_norm_version TEXT NOT NULL DEFAULT 'nfkc-casefold-v1',
            source_created_at TEXT,
            source_updated_at TEXT,
            original_time_value TEXT,
            time_precision    TEXT NOT NULL
                CHECK (time_precision IN ('second', 'minute', 'day', 'month', 'year', 'unknown')),
            timezone_status   TEXT NOT NULL
                CHECK (timezone_status IN ('utc', 'offset', 'naive', 'unknown')),
            recorded_at       TEXT NOT NULL,
            source_locator    TEXT NOT NULL,
            UNIQUE (event_id, revision_hash)
        );
        CREATE INDEX ix_revisions_event ON event_revisions(event_id);

        CREATE TABLE revision_occurrences (
            revision_id    TEXT NOT NULL REFERENCES event_revisions(revision_id),
            snapshot_id    TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            source_locator TEXT NOT NULL,
            PRIMARY KEY (revision_id, snapshot_id)
        );
        CREATE INDEX ix_occurrences_snapshot ON revision_occurrences(snapshot_id);

        CREATE TABLE revision_attachments (
            attachment_id INTEGER PRIMARY KEY,
            revision_id   TEXT NOT NULL REFERENCES event_revisions(revision_id),
            asset_pointer TEXT NOT NULL,
            content_type  TEXT,
            member_path   TEXT
        );
        CREATE INDEX ix_revision_attachments_revision
            ON revision_attachments(revision_id);

        -- 访问标签与 availability 的版本化状态（§4.2：物理实现放在单独版本化表中）
        CREATE TABLE revision_policy_state (
            state_id             INTEGER PRIMARY KEY,
            revision_id          TEXT NOT NULL REFERENCES event_revisions(revision_id),
            policy_version       TEXT NOT NULL,
            availability         TEXT NOT NULL DEFAULT 'available'
                CHECK (availability IN ('available', 'withdrawn', 'purged')),
            classification_status TEXT NOT NULL DEFAULT 'unclassified'
                CHECK (classification_status IN
                       ('unclassified', 'rule_classified', 'human_reviewed')),
            valid_from           TEXT NOT NULL,
            valid_to             TEXT
        );
        CREATE INDEX ix_policy_state_revision ON revision_policy_state(revision_id);

        CREATE TABLE revision_scope_labels (
            state_id INTEGER NOT NULL REFERENCES revision_policy_state(state_id),
            label    TEXT NOT NULL,
            PRIMARY KEY (state_id, label)
        );

        CREATE TABLE revision_sensitivity_labels (
            state_id INTEGER NOT NULL REFERENCES revision_policy_state(state_id),
            label    TEXT NOT NULL,
            PRIMARY KEY (state_id, label)
        );

        CREATE TABLE conversation_nodes (
            node_key      TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            node_id       TEXT NOT NULL,
            event_id      TEXT REFERENCES events(event_id),
            is_structural INTEGER NOT NULL DEFAULT 0,
            UNIQUE (conversation_id, node_id)
        );

        CREATE TABLE conversation_node_edges (
            parent_node_key TEXT NOT NULL REFERENCES conversation_nodes(node_key),
            child_node_key  TEXT NOT NULL REFERENCES conversation_nodes(node_key),
            child_order     INTEGER NOT NULL,
            PRIMARY KEY (parent_node_key, child_node_key)
        );

        CREATE TABLE conversation_snapshots (
            conv_snapshot_id  TEXT PRIMARY KEY,
            snapshot_id       TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            conversation_id   TEXT NOT NULL,
            title             TEXT,
            current_node_id   TEXT,
            active_path_known INTEGER NOT NULL DEFAULT 0,
            UNIQUE (snapshot_id, conversation_id)
        );

        CREATE TABLE conversation_snapshot_nodes (
            conv_snapshot_id TEXT NOT NULL REFERENCES conversation_snapshots(conv_snapshot_id),
            node_key         TEXT NOT NULL REFERENCES conversation_nodes(node_key),
            PRIMARY KEY (conv_snapshot_id, node_key)
        );

        CREATE TABLE conversation_selected_path (
            conv_snapshot_id TEXT NOT NULL REFERENCES conversation_snapshots(conv_snapshot_id),
            depth            INTEGER NOT NULL,
            node_key         TEXT NOT NULL REFERENCES conversation_nodes(node_key),
            PRIMARY KEY (conv_snapshot_id, depth)
        );
        """,
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> None:
    """按版本号顺序应用迁移；每条迁移在独立事务中提交。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def _utc_now_iso() -> str:
    from personal_brain.history.timeutil import utc_now_iso

    return utc_now_iso()
