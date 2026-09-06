"""快照发布与幂等 upsert（§5.2 步骤 4-6、§5.3 更新规则）。

所有发布写入处于同一数据库事务（§5.2：源发布与派生失效标记
必须处于同一事务；未发布批次不可见）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass, field

from personal_brain.history.identity import (
    SEARCH_TEXT_NORM_VERSION,
    compute_event_id,
    compute_fallback_source_message_id,
    compute_revision_hash,
    content_digest,
    normalize_search_text,
    revision_id_for,
)
from personal_brain.history.timeutil import ParsedTime
from personal_brain.importers.models import (
    IncomingConversation,
    IncomingMessage,
    JobError,
    MemberManifest,
)
from personal_brain.retrieval.fts import index_revision

POLICY_VERSION_INITIAL = "v0"

# 计入 unsupported_count 的错误码口径：所有"结构不支持/未解析"类错误
_UNSUPPORTED_CODES = {"UNSUPPORTED_CONTENT", "UNSUPPORTED_NODE", "UNSUPPORTED_CONVERSATION"}


@dataclass
class PublishCounts:
    conversations_seen: int = 0
    events_new: int = 0
    events_updated: int = 0
    revisions_new: int = 0
    occurrences_new: int = 0
    nodes_new: int = 0
    unsupported_count: int = 0


@dataclass
class PublishResult:
    snapshot_id: str
    source_id: str
    counts: PublishCounts
    errors: list[JobError] = field(default_factory=list)


def derive_source_id(source_kind: str, account_namespace: str) -> str:
    """内部稳定 ID：确定性 UUID，重复导入自然复用同一来源行。"""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"personal-brain:source:v1|{source_kind}|{account_namespace}",
        )
    )


def derive_snapshot_id(source_id: str, archive_sha256: str) -> str:
    """同一来源 + 同一归档摘要 → 同一快照（重复导入幂等）。"""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"personal-brain:snapshot:v1|{source_id}|{archive_sha256}",
        )
    )


def _version_key(updated: ParsedTime | None, created: ParsedTime | None) -> str | None:
    """版本排序键：优先 update_time，退回 create_time；未知时间为 None。"""
    for t in (updated, created):
        if t is not None and t.is_known:
            return t.utc_iso
    return None


def resolve_current(
    existing_keys: list[str | None],
    new_key: str | None,
    current_is_null: bool,
) -> tuple[bool, str]:
    """决定新版本是否成为 current，以及 resolution 状态。

    规则（§5.3「相同消息 ID、内容变化：新建 revision」+「遇到版本先后不明：
    保留冲突」+ Phase A 验收「新旧导出乱序」）：

    - 事件尚无版本 → current=新版本，resolved。
    - 新版本键 > 已知最大键 → current=新版本，resolved。
    - 新版本键 < 已知最大键（旧导出乱序到达）→ 保留 current 不变；
      若此前已因冲突 unresolved，维持 unresolved。
    - 新版本键 == 最大键但内容不同（hash 不同才会走到这里）→ 先后不明，
      current=NULL、unresolved，双方版本都保留，禁止后续自动激活记忆。
    - 新版本时间未知且事件已有版本 → 无法比较 → unresolved。
    - 已有版本全部未知时间且新版本带已知时间 → 同样无法证明有序 →
      unresolved（与上一条对称，保守处理）。

    键为等宽 UTC 微秒格式，字典序 == 时间序（含亚秒）。

    返回 (becomes_current, resolution)。
    """
    known = [k for k in existing_keys if k is not None]
    if not existing_keys:
        return True, "resolved"
    if new_key is None:
        return False, "unresolved"
    if not known:
        # 已有版本全部未知时间：与「新版本无时间」对称，统一为先后不明
        # （§5.3 版本先后不明保留冲突；保守处理，禁止后续自动激活的前提）
        return False, "unresolved"
    max_known = max(known)
    if new_key > max_known:
        return True, "resolved"
    if new_key < max_known:
        return False, ("unresolved" if current_is_null else "resolved")
    return False, "unresolved"


def publish_snapshot(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    account_namespace: str,
    archive_sha256: str,
    archive_relative_path: str,
    parser_version: str,
    recorded_at: str,
    conversations: list[IncomingConversation],
    errors: list[JobError],
    parse_status: str,
    coverage_start: str | None,
    coverage_end: str | None,
    coverage_notes: str | None,
    member_manifest: list[MemberManifest] | None = None,
) -> PublishResult:
    """在单一事务中发布快照；任何异常回滚，不产生部分可见状态。"""
    source_id = derive_source_id(source_kind, account_namespace)
    snapshot_id = derive_snapshot_id(source_id, archive_sha256)

    counts = PublishCounts()
    counts.unsupported_count = sum(
        1 for e in errors if e.code in _UNSUPPORTED_CODES
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO sources (source_id, source_kind, account_namespace, status, created_at)
            VALUES (?, ?, ?, 'active', ?)
            ON CONFLICT (source_id) DO NOTHING
            """,
            (source_id, source_kind, account_namespace, recorded_at),
        )

        conn.execute(
            """
            INSERT INTO source_snapshots (
                snapshot_id, source_id, archive_sha256, archive_relative_path,
                exported_at, imported_at, parser_version,
                coverage_start, coverage_end, parse_status, coverage_notes
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            (
                snapshot_id,
                source_id,
                archive_sha256,
                archive_relative_path,
                recorded_at,
                parser_version,
                coverage_start,
                coverage_end,
                parse_status,
                coverage_notes,
            ),
        )

        for asset in member_manifest or []:
            conn.execute(
                """
                INSERT INTO source_assets (
                    snapshot_id, member_path, asset_sha256, size_bytes, asset_kind
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    asset.member_path,
                    asset.sha256,
                    asset.size_bytes,
                    asset.kind,
                ),
            )

        job_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO import_jobs (
                job_id, source_id, archive_sha256, status, started_at, finished_at,
                conversations_seen
            ) VALUES (?, ?, ?, 'published', ?, ?, ?)
            """,
            (job_id, source_id, archive_sha256, recorded_at, recorded_at, len(conversations)),
        )
        for err in errors:
            conn.execute(
                """
                INSERT INTO import_job_errors (job_id, error_code, locator, message)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, err.code, err.locator, err.message),
            )

        for conv in conversations:
            counts.conversations_seen += 1
            _publish_conversation(
                conn,
                snapshot_id=snapshot_id,
                source_id=source_id,
                account_namespace=account_namespace,
                conv=conv,
                recorded_at=recorded_at,
                counts=counts,
            )

        conn.execute(
            """
            UPDATE import_jobs SET
                events_new=?, events_updated=?, revisions_new=?,
                occurrences_new=?, nodes_new=?, unsupported_count=?
            WHERE job_id=?
            """,
            (
                counts.events_new,
                counts.events_updated,
                counts.revisions_new,
                counts.occurrences_new,
                counts.nodes_new,
                counts.unsupported_count,
                job_id,
            ),
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return PublishResult(
        snapshot_id=snapshot_id, source_id=source_id, counts=counts, errors=errors
    )


def _conv_snapshot_id(snapshot_id: str, conversation_id: str) -> str:
    return f"{snapshot_id}:{hashlib.sha256(conversation_id.encode()).hexdigest()[:12]}"


def _node_key(conversation_id: str, node_id: str) -> str:
    return f"{conversation_id}::{node_id}"


def _publish_conversation(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_id: str,
    account_namespace: str,
    conv: IncomingConversation,
    recorded_at: str,
    counts: PublishCounts,
) -> None:
    conn_snapshot_id = _conv_snapshot_id(snapshot_id, conv.conversation_id)
    node_ids = {n.node_id for n in conv.nodes}

    conn.execute(
        """
        INSERT INTO conversation_snapshots (
            conv_snapshot_id, snapshot_id, conversation_id, title,
            current_node_id, active_path_known
        ) VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT (conv_snapshot_id) DO UPDATE SET
            title=excluded.title,
            current_node_id=excluded.current_node_id
        """,
        (
            conn_snapshot_id,
            snapshot_id,
            conv.conversation_id,
            conv.title,
            conv.current_node_id,
        ),
    )

    parent_of: dict[str, str] = {}
    for node in conv.nodes:
        nk = _node_key(conv.conversation_id, node.node_id)
        event_id: str | None = None
        if not node.is_structural:
            event_id = _upsert_event_with_revision(
                conn,
                snapshot_id=snapshot_id,
                source_id=source_id,
                account_namespace=account_namespace,
                conv=conv,
                node=node,
                recorded_at=recorded_at,
                counts=counts,
            )

        exists = conn.execute(
            "SELECT 1 FROM conversation_nodes WHERE node_key = ?", (nk,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO conversation_nodes (
                node_key, conversation_id, node_id, event_id, is_structural
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (node_key) DO UPDATE SET
                event_id=COALESCE(excluded.event_id, conversation_nodes.event_id)
            """,
            (nk, conv.conversation_id, node.node_id, event_id, 1 if node.is_structural else 0),
        )
        if exists is None:
            counts.nodes_new += 1

        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_snapshot_nodes (conv_snapshot_id, node_key)
            VALUES (?, ?)
            """,
            (conn_snapshot_id, nk),
        )

        if node.parent_node_id is not None and node.parent_node_id in node_ids:
            parent_of[node.node_id] = node.parent_node_id
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_node_edges (
                    parent_node_key, child_node_key, child_order
                ) VALUES (?, ?, ?)
                """,
                (
                    _node_key(conv.conversation_id, node.parent_node_id),
                    nk,
                    node.child_order,
                ),
            )

    _write_selected_path(
        conn, conv=conv, conn_snapshot_id=conn_snapshot_id, parent_of=parent_of
    )


def _write_selected_path(
    conn: sqlite3.Connection,
    *,
    conv: IncomingConversation,
    conn_snapshot_id: str,
    parent_of: dict[str, str],
) -> None:
    """从 current_node 沿父链回溯到根，写入所选路径（根深度 0）。

    无法确定（无 current_node、节点缺失、环）→ active_path_unknown，
    不写路径行，不暗中猜测（§4.3）。
    """
    known_nodes = {n.node_id for n in conv.nodes}
    declared_parent = {
        n.node_id: n.parent_node_id for n in conv.nodes if n.parent_node_id
    }
    if conv.current_node_id is None or conv.current_node_id not in known_nodes:
        conn.execute(
            "UPDATE conversation_snapshots SET active_path_known=0 WHERE conv_snapshot_id=?",
            (conn_snapshot_id,),
        )
        return

    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = conv.current_node_id
    while cur is not None:
        if cur in seen:
            return  # 环：视为路径未知，保持 active_path_known=0
        seen.add(cur)
        chain.append(cur)
        declared = declared_parent.get(cur)
        if declared is not None and declared not in known_nodes:
            # 父链悬空（父节点在 mapping 外）：锚点不是真实根，
            # 路径不可信 → active_path_unknown，不写猜测路径（§4.3）
            return
        cur = parent_of.get(cur)

    chain.reverse()  # 根 → 叶
    conn.execute(
        "UPDATE conversation_snapshots SET active_path_known=1 WHERE conv_snapshot_id=?",
        (conn_snapshot_id,),
    )
    for depth, node_id in enumerate(chain):
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_selected_path (conv_snapshot_id, depth, node_key)
            VALUES (?, ?, ?)
            """,
            (conn_snapshot_id, depth, _node_key(conv.conversation_id, node_id)),
        )


def _upsert_event_with_revision(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_id: str,
    account_namespace: str,
    conv: IncomingConversation,
    node: IncomingMessage,
    recorded_at: str,
    counts: PublishCounts,
) -> str:
    """事件身份 + 内容版本幂等 upsert（§5.3 全部规则在此实现）。"""
    digest = content_digest(node.raw_text)
    if node.source_message_id:
        identity_quality = "native"
        source_message_id = node.source_message_id
    else:
        identity_quality = "fallback"
        structural_position = f"{node.parent_node_id or ''}|{node.child_order}"
        source_message_id = compute_fallback_source_message_id(
            source_kind="chatgpt",
            account_namespace=account_namespace,
            conversation_id=conv.conversation_id,
            structural_position=structural_position,
            speaker_id=node.speaker_id,
            original_time_value=node.created.original_time_value if node.created else None,
            content_digest=digest,
        )

    event_id = compute_event_id("chatgpt", account_namespace, source_message_id)

    created = node.created
    updated = node.updated
    revision_hash = compute_revision_hash(
        content_type=node.content_type,
        raw_text=node.raw_text,
        speaker_id=node.speaker_id,
        speaker_type=node.speaker_type,
        parsed_time=created,
        source_updated_at=updated.utc_iso if updated else None,
        attachment_refs=[a.model_dump() for a in node.attachments],
    )
    revision_id = revision_id_for(event_id, revision_hash)

    existing_event = conn.execute(
        """
        SELECT current_revision_id, revision_resolution, speaker_id, speaker_type
        FROM events WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()

    if existing_event is None:
        conn.execute(
            """
            INSERT INTO events (
                event_id, source_id, conversation_id, source_message_id,
                identity_quality, current_revision_id, revision_resolution,
                speaker_id, speaker_type
            ) VALUES (?, ?, ?, ?, ?, NULL, 'resolved', ?, ?)
            """,
            (
                event_id,
                source_id,
                conv.conversation_id,
                source_message_id,
                identity_quality,
                node.speaker_id,
                node.speaker_type,
            ),
        )
        counts.events_new += 1
        existing_keys: list[str | None] = []
        current_is_null = True
    else:
        current_is_null = existing_event["current_revision_id"] is None
        existing_keys = [
            r["vk"]
            for r in conn.execute(
                """
                SELECT COALESCE(source_updated_at, source_created_at) AS vk
                FROM event_revisions WHERE event_id = ?
                """,
                (event_id,),
            )
        ]

    dup = conn.execute(
        "SELECT 1 FROM event_revisions WHERE event_id = ? AND revision_hash = ?",
        (event_id, revision_hash),
    ).fetchone()

    if dup is not None:
        # 相同内容版本已存在：不做任何 resolution 状态变更（重复导入必须
        # 保持事件状态稳定，§5.3），仅补充出现位置。
        _add_occurrence(conn, snapshot_id=snapshot_id, revision_id=revision_id,
                        locator=node.locator, counts=counts)
        return event_id

    _insert_revision(conn, node=node, revision_id=revision_id, revision_hash=revision_hash,
                     event_id=event_id, recorded_at=recorded_at, created=created, updated=updated)
    counts.revisions_new += 1

    new_key = _version_key(updated, created)
    becomes_current, resolution = resolve_current(existing_keys, new_key, current_is_null)

    if existing_event is None:
        # 新事件：首个版本即 current
        conn.execute(
            """
            UPDATE events SET current_revision_id=?, revision_resolution='resolved'
            WHERE event_id=?
            """,
            (revision_id, event_id),
        )
    elif becomes_current:
        conn.execute(
            """
            UPDATE events SET current_revision_id=?, revision_resolution='resolved',
                speaker_id=?, speaker_type=?
            WHERE event_id=?
            """,
            (revision_id, node.speaker_id, node.speaker_type, event_id),
        )
        counts.events_updated += 1
    elif (
        resolution == "unresolved"
        and existing_event["revision_resolution"] == "resolved"
        and existing_event["current_revision_id"] is not None
    ):
        # 先后不明：current 置空，双方版本保留（§5.3 保留冲突）
        conn.execute(
            "UPDATE events SET current_revision_id=NULL, revision_resolution='unresolved' "
            "WHERE event_id=?",
            (event_id,),
        )
    elif existing_event["revision_resolution"] != resolution:
        conn.execute(
            "UPDATE events SET revision_resolution=? WHERE event_id=?",
            (resolution, event_id),
        )

    _add_occurrence(conn, snapshot_id=snapshot_id, revision_id=revision_id,
                    locator=node.locator, counts=counts)

    return event_id


def _add_occurrence(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    revision_id: str,
    locator: str,
    counts: PublishCounts,
) -> None:
    occurred = conn.execute(
        """
        SELECT 1 FROM revision_occurrences WHERE revision_id=? AND snapshot_id=?
        """,
        (revision_id, snapshot_id),
    ).fetchone()
    if occurred is None:
        conn.execute(
            """
            INSERT INTO revision_occurrences (revision_id, snapshot_id, source_locator)
            VALUES (?, ?, ?)
            """,
            (revision_id, snapshot_id, locator),
        )
        counts.occurrences_new += 1


def _insert_revision(
    conn: sqlite3.Connection,
    *,
    node: IncomingMessage,
    revision_id: str,
    revision_hash: str,
    event_id: str,
    recorded_at: str,
    created: ParsedTime | None,
    updated: ParsedTime | None,
) -> None:
    conn.execute(
        """
        INSERT INTO event_revisions (
            revision_id, event_id, revision_hash, content_type, raw_text, search_text,
            search_text_norm_version, source_created_at, source_updated_at,
            original_time_value, time_precision, timezone_status, recorded_at, source_locator
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            event_id,
            revision_hash,
            node.content_type,
            node.raw_text,
            normalize_search_text(node.raw_text) if node.raw_text is not None else None,
            SEARCH_TEXT_NORM_VERSION,
            created.utc_iso if created else None,
            updated.utc_iso if updated else None,
            created.original_time_value if created else None,
            created.time_precision if created else "unknown",
            created.timezone_status if created else "unknown",
            recorded_at,
            node.locator,
        ),
    )
    for att in node.attachments:
        conn.execute(
            """
            INSERT INTO revision_attachments (
                revision_id, asset_pointer, content_type, member_path
            ) VALUES (?, ?, ?, ?)
            """,
            (revision_id, att.asset_pointer, att.content_type, att.member_path),
        )
    conn.execute(
        """
        INSERT INTO revision_policy_state (
            revision_id, policy_version, availability, classification_status, valid_from
        ) VALUES (?, ?, ?, 'unclassified', ?)
        """,
        (
            revision_id,
            POLICY_VERSION_INITIAL,
            (
                "withdrawn"
                if _event_or_source_withdrawn(conn, event_id=event_id)
                else "available"
            ),
            recorded_at,
        ),
    )
    # 派生 FTS 索引与内容同事务写入（§5.2 可见性边界）
    index_revision(conn, revision_id, node.raw_text)


def _event_or_source_withdrawn(conn: sqlite3.Connection, *, event_id: str) -> bool:
    """事件或其来源当前是否处于撤回状态（§14.2.3 撤回延续）。

    新内容版本的 policy_state 必须继承撤回状态：显式"忘记此内容"
    覆盖重复来源，不能靠另一份导出保活（验收缺陷 B）。
    """
    row = conn.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM sources s
            JOIN events e ON e.source_id = s.source_id
            WHERE e.event_id = ? AND s.status = 'withdrawn'
        )
        """,
        (event_id,),
    ).fetchone()
    if row[0]:
        return True
    row = conn.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM event_revisions r2
            JOIN revision_policy_state ps
              ON ps.revision_id = r2.revision_id AND ps.valid_to IS NULL
            WHERE r2.event_id = ? AND ps.availability = 'withdrawn'
        )
        """,
        (event_id,),
    ).fetchone()
    return bool(row[0])
