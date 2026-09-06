"""只读 MCP 服务层（§8）：4 个工具的统一实现，供 stdio 服务器与测试共用。

安全语义（§7.3/§7.6）：
- profile 来自受信任启动配置；请求参数只能**收窄**过滤，永不放宽。
- 过滤在 SQL 层、snippet/排序之前完成；计数（total_matched、
  undated_excluded、brain_status 覆盖）只统计调用者可见档案，
  不泄露隐藏记录数量（§7.3）。
- ``get_event`` 按事件逐次重新授权；无权限与不存在返回相同的
  ``NOT_FOUND_OR_NOT_ALLOWED``；撤回内容不因引用 ID 绕过（§8.1）。
- 请求返回前核对 policy epoch；执行中策略变化 → 整体重跑（最多 3 次），
  仍不稳定则放弃结果（TRANSIENT_POLICY_CHANGE）。
- 逻辑定位符只暴露 snapshot_id/node_id，不暴露磁盘路径（§8.1）。
"""

from __future__ import annotations

import sqlite3

from personal_brain.history.policy_epoch import current_policy_epoch
from personal_brain.policy.profiles import AccessProfile
from personal_brain.retrieval.search import (
    QueryTooBroad,
    SearchFilters,
    SearchHit,
    SearchLimits,
    parse_date_bound,
    recent_events,
    search_history,
)

SCHEMA_VERSION = "0.2"
CONTENT_TRUST = "untrusted_archive"
MAX_CONTEXT_RADIUS = 3
EPOCH_RETRIES = 3
CAPABILITY_NOTE = (
    "语义扩展未实现：同义表达（如“求职/找工作”）需分别检索（§6.3 已知能力边界）"
)


class PolicyEpochChurn(RuntimeError):
    """策略在请求执行中持续变化；放弃结果而非返回可能越权的正文。"""


class ToolError(Exception):
    """工具级错误：转为 MCP isError 结果，不中断服务器。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


# ---------------------------------------------------------------------------
# 授权过滤
# ---------------------------------------------------------------------------


def profile_filters(profile: AccessProfile) -> SearchFilters:
    """profile → 强制过滤。'*' 通配 = 不限 scope；agent 默认不开放未分类。"""
    return SearchFilters(
        allow_scopes=None if "*" in profile.allow_scopes else profile.allow_scopes,
        deny_sensitivity=profile.deny_sensitivity or None,
        allow_unclassified=profile.allow_unclassified,
    )


def _authorized_revision_ids(
    conn: sqlite3.Connection, filters: SearchFilters, event_id: str
) -> list[str]:
    """给定事件中调用者可见的 revision 列表（逐事件授权，§7.3）。"""
    params: list = [event_id]
    sql = """
    SELECT r.revision_id FROM event_revisions r
    JOIN sources s ON s.source_id = (
        SELECT source_id FROM events WHERE event_id = r.event_id)
    WHERE r.event_id = ? AND s.status != 'withdrawn'
      AND EXISTS (
        SELECT 1 FROM revision_policy_state ps
        WHERE ps.revision_id = r.revision_id
          AND ps.valid_to IS NULL AND ps.availability = 'available')
    """
    if filters.allow_scopes is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps3"
            " JOIN revision_scope_labels sl ON sl.state_id = ps3.state_id"
            " WHERE ps3.revision_id = r.revision_id AND ps3.valid_to IS NULL"
            " AND sl.label IN (" + ",".join("?" for _ in filters.allow_scopes) + "))"
        )
        params.extend(filters.allow_scopes)
    if filters.deny_sensitivity is not None:
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM revision_policy_state ps4"
            " JOIN revision_sensitivity_labels se ON se.state_id = ps4.state_id"
            " WHERE ps4.revision_id = r.revision_id AND ps4.valid_to IS NULL"
            " AND se.label IN (" + ",".join("?" for _ in filters.deny_sensitivity) + "))"
        )
        params.extend(filters.deny_sensitivity)
    if not filters.allow_unclassified:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps2"
            " WHERE ps2.revision_id = r.revision_id AND ps2.valid_to IS NULL"
            " AND ps2.classification_status != 'unclassified')"
        )
    sql += " ORDER BY r.recorded_at, r.revision_id"
    return [r["revision_id"] for r in conn.execute(sql, params)]


def _event_authorized(conn: sqlite3.Connection, filters: SearchFilters, event_id: str) -> bool:
    return bool(_authorized_revision_ids(conn, filters, event_id))


# ---------------------------------------------------------------------------
# 结果映射
# ---------------------------------------------------------------------------


def _occurrence_ref(conn: sqlite3.Connection, revision_id: str) -> dict[str, str]:
    """逻辑定位符：snapshot_id + node_id，不含磁盘路径（§8.1）。"""
    row = conn.execute(
        """
        SELECT s.snapshot_id, o.source_locator FROM revision_occurrences o
        JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id
        WHERE o.revision_id = ? ORDER BY s.imported_at LIMIT 1
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        return {"snapshot_id": "", "node_id": ""}
    locator = row["source_locator"] or ""
    node_id = locator.rsplit("/mapping/", 1)[-1] if "/mapping/" in locator else ""
    return {"snapshot_id": row["snapshot_id"], "node_id": node_id}


def _hit_to_result(conn: sqlite3.Connection, hit: SearchHit) -> dict:
    return {
        "event_id": hit.event_id,
        "revision_id": hit.revision_id,
        "is_current": hit.is_current,
        "speaker_type": hit.speaker_type,
        "timestamp": hit.source_created_at,
        "time_known": hit.time_known,
        "snippet": hit.snippet,
        "citation_id": f"pb:{hit.revision_id}",
        "evidence_level": "message_record",
        "source_locator": _occurrence_ref(conn, hit.revision_id),
    }


def _coverage(conn: sqlite3.Connection, filters: SearchFilters, branch_scope: str) -> dict:
    """调用者可见档案的覆盖期（§7.3：不泄露隐藏记录数量）。"""
    params: list = []
    sql = """
    SELECT MIN(r.source_created_at) AS lo, MAX(r.source_created_at) AS hi
    FROM event_revisions r
    JOIN sources s ON s.source_id = (
        SELECT source_id FROM events WHERE event_id = r.event_id)
    WHERE s.status != 'withdrawn' AND r.source_created_at IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM revision_policy_state ps
        WHERE ps.revision_id = r.revision_id
          AND ps.valid_to IS NULL AND ps.availability = 'available')
    """
    if filters.allow_scopes is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps3"
            " JOIN revision_scope_labels sl ON sl.state_id = ps3.state_id"
            " WHERE ps3.revision_id = r.revision_id AND ps3.valid_to IS NULL"
            " AND sl.label IN (" + ",".join("?" for _ in filters.allow_scopes) + "))"
        )
        params.extend(filters.allow_scopes)
    if filters.deny_sensitivity is not None:
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM revision_policy_state ps4"
            " JOIN revision_sensitivity_labels se ON se.state_id = ps4.state_id"
            " WHERE ps4.revision_id = r.revision_id AND ps4.valid_to IS NULL"
            " AND se.label IN (" + ",".join("?" for _ in filters.deny_sensitivity) + "))"
        )
        params.extend(filters.deny_sensitivity)
    if not filters.allow_unclassified:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps2"
            " WHERE ps2.revision_id = r.revision_id AND ps2.valid_to IS NULL"
            " AND ps2.classification_status != 'unclassified')"
        )
    row = conn.execute(sql, params).fetchone()
    return {
        "known_start": row["lo"],
        "known_end": row["hi"],
        "completeness": "unknown",
        "branch_scope": branch_scope,
    }


def _envelope(
    results: list[dict],
    *,
    truncated: bool,
    next_cursor: str | None,
    coverage: dict,
    warnings: list[str] | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "content_trust": CONTENT_TRUST,
        "coverage": coverage,
        "results": results,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "warnings": warnings or [],
    }


# ---------------------------------------------------------------------------
# 4 个工具（§8）
# ---------------------------------------------------------------------------


def tool_brain_status(
    conn: sqlite3.Connection, profile: AccessProfile, limits: SearchLimits
) -> dict:
    """健康、调用者可见覆盖期、最后成功导入、解析警告、可用能力。"""
    filters = profile_filters(profile)
    coverage = _coverage(conn, filters, branch_scope="all")
    params: list = []
    visible_sql = """
    SELECT s.source_id, s.account_namespace, s.status,
           MAX(sp.imported_at) AS last_import,
           MAX(sp.parse_status) AS any_partial,
           (SELECT COUNT(*) FROM import_job_errors e
            JOIN import_jobs j ON j.job_id = e.job_id
            WHERE j.source_id = s.source_id AND e.error_code LIKE 'UNSUPPORTED%') AS unsupported
    FROM sources s
    JOIN source_snapshots sp ON sp.source_id = s.source_id
    WHERE s.status != 'withdrawn'
    """
    if filters.allow_scopes is not None:
        visible_sql += (
            " AND EXISTS (SELECT 1 FROM events e2"
            " JOIN revision_policy_state ps3 ON ps3.revision_id = ("
            "   SELECT revision_id FROM event_revisions r WHERE r.event_id = e2.event_id)"
            " JOIN revision_scope_labels sl ON sl.state_id = ps3.state_id"
            " WHERE e2.source_id = s.source_id AND ps3.valid_to IS NULL"
            " AND sl.label IN (" + ",".join("?" for _ in filters.allow_scopes) + "))"
        )
        params.extend(filters.allow_scopes)
    visible_sql += " GROUP BY s.source_id ORDER BY s.created_at"
    sources = []
    warnings: list[str] = []
    for r in conn.execute(visible_sql, params).fetchall():
        sources.append(
            {
                "account_namespace": r["account_namespace"],
                "last_successful_import": r["last_import"],
            }
        )
        if r["unsupported"]:
            # 只报存在性，不报数量/位置（§7.3 计数与错误信息也过权限）
            warnings.append(
                f"来源 {r['account_namespace']} 存在未支持的对话结构（已跳过）"
            )
        if r["any_partial"] == "partial":
            warnings.append(f"来源 {r['account_namespace']} 存在部分解析的快照")
    return _envelope(
        [],
        truncated=False,
        next_cursor=None,
        coverage=coverage,
        warnings=warnings,
    ) | {
        "health": "ok",
        "sources": sources,
        "capabilities": {"tools": list(profile.tools), "read_only": True,
                         "delivery_boundary": profile.delivery_boundary},
    }


def tool_search_history(
    conn: sqlite3.Connection,
    profile: AccessProfile,
    args: dict,
    limits: SearchLimits,
    timezone: str = "UTC",
) -> dict:
    """授权匹配结果、游标、覆盖说明（§8 search_history）。"""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_PARAMS", "query 必须是非空字符串")
    match_mode = args.get("match_mode", "literal")
    order = args.get("order", "chronological")
    branch_scope = args.get("branch_scope", "all")
    if branch_scope not in ("selected", "all"):
        raise ToolError("INVALID_PARAMS", f"未知 branch_scope: {branch_scope}")
    filters = profile_filters(profile)
    filters = SearchFilters(
        date_from=parse_date_bound(args["date_from"], timezone, end_of_day=False)
        if args.get("date_from") else None,
        date_to=parse_date_bound(args["date_to"], timezone, end_of_day=True)
        if args.get("date_to") else None,
        source_id=args.get("source_id"),
        account_namespace=args.get("account_namespace"),
        conversation_id=args.get("conversation_id"),
        speaker_type=args.get("speaker_type"),
        path_mode=branch_scope,
        allow_scopes=filters.allow_scopes,
        deny_sensitivity=filters.deny_sensitivity,
        allow_unclassified=filters.allow_unclassified,
    )
    try:
        result = search_history(
            conn,
            query,
            match_mode=match_mode if match_mode in ("literal", "all_terms") else "literal",
            filters=filters,
            order=(
                order
                if order in ("chronological", "reverse_chronological", "relevance")
                else "chronological"
            ),
            limit=args.get("limit"),
            cursor=args.get("cursor"),
            limits=limits,
        )
    except QueryTooBroad as exc:
        raise ToolError("QUERY_TOO_BROAD", str(exc)) from exc
    except ValueError as exc:
        raise ToolError("INVALID_PARAMS", str(exc)) from exc
    return _envelope(
        [_hit_to_result(conn, h) for h in result.hits],
        truncated=result.truncated,
        next_cursor=result.next_cursor,
        coverage=_coverage(conn, profile_filters(profile), branch_scope),
        warnings=[CAPABILITY_NOTE],
    )


def tool_get_recent_events(
    conn: sqlite3.Connection, profile: AccessProfile, args: dict, limits: SearchLimits
) -> dict:
    """按源时间倒序的授权事件；明确参考时间（§8 get_recent_events）。"""
    from datetime import UTC, datetime

    days = args.get("days", 7)
    if not isinstance(days, int) or days < 0 or days > 3650:
        raise ToolError("INVALID_PARAMS", "days 必须是 0..3650 的整数")
    ref_raw = args.get("now")  # 测试注入参考时间；生产忽略
    now = (
        datetime.fromisoformat(ref_raw)
        if isinstance(ref_raw, str) and ref_raw
        else datetime.now(UTC)
    )
    filters = profile_filters(profile)
    filters = SearchFilters(
        source_id=args.get("source_id"),
        account_namespace=args.get("account_namespace"),
        conversation_id=args.get("conversation_id"),
        allow_scopes=filters.allow_scopes,
        deny_sensitivity=filters.deny_sensitivity,
        allow_unclassified=filters.allow_unclassified,
    )
    result = recent_events(
        conn,
        days=days,
        limit=args.get("limit"),
        limits=limits,
        now=now,
        cursor=args.get("cursor"),
        filters=filters,
    )
    return _envelope(
        [_hit_to_result(conn, h) for h in result.hits],
        truncated=result.truncated,
        next_cursor=result.next_cursor,
        coverage=_coverage(conn, profile_filters(profile), branch_scope="all"),
        warnings=[],
    ) | {"reference_time": now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"}


def tool_get_event(
    conn: sqlite3.Connection, profile: AccessProfile, args: dict, limits: SearchLimits
) -> dict:
    """原文 + 有界上下文 + 版本/分支信息；逐次重新授权（§8 get_event）。"""
    event_id = args.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ToolError("INVALID_PARAMS", "event_id 必须是非空字符串")
    radius = args.get("context_radius", 0)
    if not isinstance(radius, int) or radius < 0 or radius > MAX_CONTEXT_RADIUS:
        raise ToolError(
            "INVALID_PARAMS", f"context_radius 必须是 0..{MAX_CONTEXT_RADIUS} 的整数"
        )
    filters = profile_filters(profile)
    auth_revs = _authorized_revision_ids(conn, filters, event_id)
    if not auth_revs:
        # 无权限与不存在返回相同错误（§7.3）
        raise ToolError("NOT_FOUND_OR_NOT_ALLOWED")
    ev = conn.execute(
        """
        SELECT e.event_id, e.current_revision_id, e.revision_resolution,
               e.speaker_id, e.speaker_type, e.conversation_id, s.account_namespace
        FROM events e JOIN sources s ON s.source_id = e.source_id
        WHERE e.event_id = ?
        """,
        (event_id,),
    ).fetchone()
    requested = args.get("revision_id")
    if requested and requested not in auth_revs:
        raise ToolError("NOT_FOUND_OR_NOT_ALLOWED")
    revision_id = requested or (
        ev["current_revision_id"] if ev["current_revision_id"] in auth_revs else auth_revs[-1]
    )
    rev = conn.execute(
        "SELECT * FROM event_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    ref = _occurrence_ref(conn, revision_id)

    context: list[dict] = []
    if radius:
        context = _authorized_context(
            conn, filters, event_id, ev["conversation_id"], revision_id, radius
        )

    versions = [
        {
            "revision_id": rid,
            "is_current": rid == ev["current_revision_id"],
        }
        for rid in auth_revs
    ]
    result = {
        "event_id": event_id,
        "revision_id": revision_id,
        "is_current": revision_id == ev["current_revision_id"],
        "speaker_type": ev["speaker_type"],
        "speaker_id": ev["speaker_id"],
        "conversation_id": ev["conversation_id"],
        "account_namespace": ev["account_namespace"],
        "timestamp": rev["source_created_at"],
        "content_type": rev["content_type"],
        "raw_text": rev["raw_text"],
        "revision_resolution": ev["revision_resolution"],
        "versions": versions,
        "branch_context": {
            "conversation_id": ev["conversation_id"],
            "note": "仅所选路径上的有界邻接，不返回完整对话树（§8）",
        },
        "citation_id": f"pb:{revision_id}",
        "evidence_level": "message_record",
        "source_locator": ref,
        "context_radius": radius,
        "context": context,
    }
    return _envelope(
        [result],
        truncated=False,
        next_cursor=None,
        coverage=_coverage(conn, filters, branch_scope="all"),
        warnings=[],
    )


def _authorized_context(
    conn: sqlite3.Connection,
    filters: SearchFilters,
    event_id: str,
    conversation_id: str,
    revision_id: str,
    radius: int,
) -> list[dict]:
    """所选路径上有界邻接；每个邻接事件**单独授权**，未授权静默略去。"""
    node = conn.execute(
        "SELECT node_key FROM conversation_nodes WHERE conversation_id = ? AND event_id = ?",
        (conversation_id, event_id),
    ).fetchone()
    if node is None:
        return []
    path = conn.execute(
        """
        SELECT sp.node_key, sp.depth, cn.event_id
        FROM conversation_selected_path sp
        JOIN conversation_nodes cn ON cn.node_key = sp.node_key
        WHERE sp.conv_snapshot_id = (
            SELECT cs2.conv_snapshot_id FROM conversation_snapshots cs2
            WHERE cs2.conversation_id = ?
            ORDER BY cs2.rowid DESC LIMIT 1
        ) ORDER BY sp.depth
        """,
        (conversation_id,),
    ).fetchall()
    keys = [r["node_key"] for r in path]
    try:
        pos = keys.index(node["node_key"])
    except ValueError:
        return []
    out: list[dict] = []
    for i in range(max(0, pos - radius), min(len(path), pos + radius + 1)):
        if i == pos:
            continue
        neighbor_event = path[i]["event_id"]
        if not neighbor_event or not _event_authorized(conn, filters, neighbor_event):
            continue
        nr = conn.execute(
            """
            SELECT r.revision_id, r.raw_text, r.source_created_at,
                   e.speaker_type, e.current_revision_id
            FROM event_revisions r JOIN events e ON e.event_id = r.event_id
            WHERE r.event_id = ? AND r.revision_id = e.current_revision_id
            """,
            (neighbor_event,),
        ).fetchone()
        if nr is None or nr["revision_id"] not in _authorized_revision_ids(
            conn, filters, neighbor_event
        ):
            continue
        out.append(
            {
                "event_id": neighbor_event,
                "revision_id": nr["revision_id"],
                "offset": i - pos,  # 负=之前，正=之后
                "speaker_type": nr["speaker_type"],
                "timestamp": nr["source_created_at"],
                "snippet": (nr["raw_text"] or "")[:200],
                "citation_id": f"pb:{nr['revision_id']}",
            }
        )
    return out


# ---------------------------------------------------------------------------
# epoch 重验（§7.3：返回前核对，执行中变化 → 重新授权或放弃）
# ---------------------------------------------------------------------------


def run_with_epoch_retry(fn, *args, **kwargs):
    """执行工具并在返回前核对 policy epoch；不稳定则整体重跑。"""
    conn = args[0]
    last: dict | None = None
    for _ in range(EPOCH_RETRIES):
        before = current_policy_epoch(conn)
        last = fn(*args, **kwargs)
        if current_policy_epoch(conn) == before:
            return last
    raise PolicyEpochChurn(
        "策略状态在请求执行中持续变化，已放弃本次结果（TRANSIENT_POLICY_CHANGE）"
    )
