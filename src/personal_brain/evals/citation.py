"""引用解析（§8.1/§9.2）：citation_id 必须解析到正确版本，100% 门槛。

citation_id = ``pb:{revision_id}``，其中 ``revision_id = {event_id}:r{hash20}``
（§4.2 派生规则，event_id 本身可含冒号）。resolver 校验：
- revision 存在且属于该 event（绑定不可变 revision）；
- 对给定 profile 正常可访问（availability + 标签 + 来源状态）；
- 返回版本元数据；不可解析/不可访问统一返回 None（不区分原因，防探测）。
"""

from __future__ import annotations

import sqlite3

from personal_brain.mcp_server.service import _authorized_revision_ids
from personal_brain.retrieval.search import SearchFilters

CITATION_PREFIX = "pb:"
_HEX = set("0123456789abcdef")


def parse_citation(citation_id: str) -> tuple[str, str] | None:
    """``pb:{revision_id}`` → (event_id, revision_id)；结构非法返回 None。"""
    if not citation_id.startswith(CITATION_PREFIX):
        return None
    revision_id = citation_id[len(CITATION_PREFIX):]
    idx = revision_id.rfind(":r")
    if idx <= 0:
        return None
    event_id = revision_id[:idx]
    hash_part = revision_id[idx + 2:]
    if len(hash_part) != 20 or not set(hash_part) <= _HEX:
        return None
    return event_id, revision_id


def resolve_citation(
    conn: sqlite3.Connection, citation_id: str, filters: SearchFilters
) -> dict | None:
    """解析 citation 到正确版本；不存在或不可访问返回 None。"""
    parsed = parse_citation(citation_id)
    if parsed is None:
        return None
    event_id, revision_id = parsed
    row = conn.execute(
        """
        SELECT r.revision_id, r.event_id, r.raw_text, r.source_created_at,
               r.content_type, e.speaker_type, e.current_revision_id
        FROM event_revisions r JOIN events e ON e.event_id = r.event_id
        WHERE r.revision_id = ? AND r.event_id = ?
        """,
        (revision_id, event_id),
    ).fetchone()
    if row is None:
        return None
    if revision_id not in _authorized_revision_ids(conn, filters, event_id):
        return None
    return {
        "event_id": event_id,
        "revision_id": revision_id,
        "is_current": row["current_revision_id"] == revision_id,
        "speaker_type": row["speaker_type"],
        "timestamp": row["source_created_at"],
        "content_type": row["content_type"],
    }
