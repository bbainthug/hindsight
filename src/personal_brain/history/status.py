"""运行状态汇总（§16 brain status；§18 可观测性指标子集）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class SourceStatus:
    source_id: str
    account_namespace: str
    source_kind: str
    status: str
    snapshots: int
    events: int
    coverage_start: str | None
    coverage_end: str | None
    last_import_at: str | None


@dataclass
class BrainStatus:
    sources: list[SourceStatus] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    jobs: dict[str, int] = field(default_factory=dict)
    derived: dict[str, int | str] = field(default_factory=dict)


def collect_status(conn: sqlite3.Connection) -> BrainStatus:
    status = BrainStatus()

    for row in conn.execute(
        """
        SELECT s.source_id, s.account_namespace, s.source_kind, s.status,
               (SELECT COUNT(*) FROM source_snapshots sp
                WHERE sp.source_id = s.source_id) AS snapshots,
               (SELECT COUNT(*) FROM events e WHERE e.source_id = s.source_id) AS events,
               (SELECT MIN(coverage_start) FROM source_snapshots sp
                WHERE sp.source_id = s.source_id) AS coverage_start,
               (SELECT MAX(coverage_end) FROM source_snapshots sp
                WHERE sp.source_id = s.source_id) AS coverage_end,
               (SELECT MAX(imported_at) FROM source_snapshots sp
                WHERE sp.source_id = s.source_id) AS last_import_at
        FROM sources s ORDER BY s.created_at
        """
    ).fetchall():
        status.sources.append(
            SourceStatus(
                source_id=row["source_id"],
                account_namespace=row["account_namespace"],
                source_kind=row["source_kind"],
                status=row["status"],
                snapshots=row["snapshots"],
                events=row["events"],
                coverage_start=row["coverage_start"],
                coverage_end=row["coverage_end"],
                last_import_at=row["last_import_at"],
            )
        )

    for name, sql in {
        "snapshots": "SELECT COUNT(*) FROM source_snapshots",
        "events": "SELECT COUNT(*) FROM events",
        "revisions": "SELECT COUNT(*) FROM event_revisions",
        "occurrences": "SELECT COUNT(*) FROM revision_occurrences",
        "conversation_nodes": "SELECT COUNT(*) FROM conversation_nodes",
        "revisions_withdrawn": (
            "SELECT COUNT(*) FROM revision_policy_state "
            "WHERE valid_to IS NULL AND availability = 'withdrawn'"
        ),
        "unresolved_version_conflicts": (
            "SELECT COUNT(*) FROM events WHERE revision_resolution = 'unresolved'"
        ),
        "unsupported_structures": (
            "SELECT COUNT(*) FROM import_job_errors WHERE error_code LIKE 'UNSUPPORTED%'"
        ),
    }.items():
        status.totals[name] = conn.execute(sql).fetchone()[0]

    status.jobs = {
        "published": conn.execute(
            "SELECT COUNT(*) FROM import_jobs WHERE status='published'"
        ).fetchone()[0],
        "failed": conn.execute(
            "SELECT COUNT(*) FROM import_jobs WHERE status='failed'"
        ).fetchone()[0],
    }

    fts_rows = conn.execute(
        "SELECT COUNT(*) FROM event_revisions_fts"
    ).fetchone()[0]
    revisions = status.totals["revisions"]
    status.derived = {
        "fts_rows": fts_rows,
        "index_lag": revisions - fts_rows,  # >0 表示需要重建派生索引
    }
    return status
