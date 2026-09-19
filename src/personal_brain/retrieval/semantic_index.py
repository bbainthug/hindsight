"""本地语义索引（D-1 / 规格 §659 可重建 RetrievalIndex 契约）。

- 索引是派生数据（§3.2 L3）：可从 L1 ``event_revisions`` 全量重建；
  备份 manifest 不必包含，``restore-check`` 后可 ``--full`` 重建。
- 可索引判定与检索池 ``_BASE_FROM_WHERE`` 同一语义：来源未撤回 且
  当前 policy 状态 availability='available'。
- 命中已知凭据形态的 revision 不 embed、不入索引（§329 / §7.5）。
- ``index_version`` 绑定 (model_id, dimension, chunk_chars, chunk_overlap)：
  任一变化 → 全部失效 → 全量重建。
- 撤回/策略翻转的向量行清理走 ``purge_revision_semantic``，与撤回同事务
  （§14.2 依赖边 4）；扩展不可用时的孤儿向量由增量重建兜底清除。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_brain.history.normalization import normalize_with_map
from personal_brain.history.timeutil import utc_now_iso
from personal_brain.retrieval.credentials import contains_known_credentials
from personal_brain.retrieval.embeddings import (
    DEFAULT_SEMANTIC_MODEL,
    EmbeddingProvider,
)

DEFAULT_CHUNK_CHARS = 600
DEFAULT_CHUNK_OVERLAP = 100
KNN_CAP = 200
EMBED_BATCH = 64
# 查询侧噪声下限：向量归一化后 L2 距离 d → cos = 1 - d²/2。
# 无关文本对（bge）cos 约 0.1-0.3，同义/相关对 ≥0.5；低于下限的候选
# 视为 ANN 噪声，不进入结果（hash provider 的小语料噪声尤其明显）。
SEMANTIC_MIN_COSINE = 0.3


def distance_to_cosine(distance: float) -> float:
    return 1.0 - (distance * distance) / 2.0


@dataclass(frozen=True)
class SemanticConfig:
    model_id: str = DEFAULT_SEMANTIC_MODEL
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    cache_dir: str | None = None


def index_version_of(config: SemanticConfig, dimension: int) -> str:
    """模型/维度/分块参数任一变化 → 版本变化 → 视为全部失效。"""
    payload = json.dumps(
        {
            "model_id": config.model_id,
            "dimension": dimension,
            "chunk_chars": config.chunk_chars,
            "chunk_overlap": config.chunk_overlap,
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# sqlite-vec 扩展
# ---------------------------------------------------------------------------


def load_vec_extension(conn: sqlite3.Connection) -> bool:
    """加载本地 sqlite-vec 扩展；不可用时返回 False（查询路径据此退化）。"""
    try:
        import sqlite_vec
    except ImportError:
        return False
    enabled = False
    try:
        conn.enable_load_extension(True)
        enabled = True
        sqlite_vec.load(conn)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 0")  # 探活
        return True
    except (AttributeError, OSError, RuntimeError, sqlite3.Error):
        return False
    finally:
        if enabled:
            try:
                conn.enable_load_extension(False)
            except (AttributeError, RuntimeError, sqlite3.Error):
                pass


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _vec_table_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "revision_chunk_vectors")


def ensure_vector_table(conn: sqlite3.Connection, dimension: int) -> None:
    """按维度创建/重建 vec0 虚拟表（维度写入建表语句，无法 ALTER）。"""
    if _vec_table_exists(conn):
        meta = semantic_meta(conn)
        if meta is not None and meta["dimension"] == dimension:
            return
        conn.execute("DROP TABLE IF EXISTS revision_chunk_vectors")
    conn.execute(
        f"CREATE VIRTUAL TABLE revision_chunk_vectors USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding float[{dimension}])"
    )


# ---------------------------------------------------------------------------
# 元数据与状态
# ---------------------------------------------------------------------------


def semantic_meta(conn: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return conn.execute(
            "SELECT * FROM semantic_index_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # doctor/MCP may inspect a database created before migration 5; that is
        # an unbuilt index, not a fatal database-read error.
        if "no such table" in str(exc).lower():
            return None
        raise


def _indexable_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """返回当前可索引 revision；凭据形态不算作索引缺口。"""
    rows = conn.execute(
        """
        SELECT DISTINCT r.revision_id, r.raw_text
        FROM event_revisions r
        JOIN events e ON e.event_id = r.event_id
        JOIN sources s ON s.source_id = e.source_id
        WHERE s.status != 'withdrawn'
          AND EXISTS (
            SELECT 1 FROM revision_policy_state ps
            WHERE ps.revision_id = r.revision_id
              AND ps.valid_to IS NULL AND ps.availability = 'available')
        ORDER BY r.revision_id
        """
    ).fetchall()
    return [
        r for r in rows
        if r["raw_text"] and not contains_known_credentials(r["raw_text"])
    ]


def _indexable_revision_count(conn: sqlite3.Connection) -> int:
    return len(_indexable_revisions(conn))


def semantic_status(conn: sqlite3.Connection) -> dict:
    """只读状态汇总（doctor 用）：存在性、模型、维度、缺口。"""
    extension = load_vec_extension(conn)
    meta = semantic_meta(conn)
    indexable_ids = {r["revision_id"] for r in _indexable_revisions(conn)}
    chunks_table = _table_exists(conn, "revision_chunks")
    indexed_ids = (
        {
            r["revision_id"]
            for r in conn.execute("SELECT DISTINCT revision_id FROM revision_chunks")
        }
        if chunks_table
        else set()
    )
    vector_table = _vec_table_exists(conn)
    if meta is None:
        return {
            "available": False,
            "extension_loaded": extension,
            "index_exists": False,
            "chunks_table_exists": chunks_table,
            "vector_table_exists": vector_table,
            "indexed_revisions": 0,
            "indexable_revisions": len(indexable_ids),
            "missing_revisions": len(indexable_ids),
            "missing_reason": "index_not_built",
        }
    missing = indexable_ids - indexed_ids
    result = {
        "available": bool(extension and vector_table),
        "extension_loaded": extension,
        "index_exists": True,
        "chunks_table_exists": chunks_table,
        "vector_table_exists": vector_table,
        "model_id": meta["model_id"],
        "dimension": meta["dimension"],
        "chunk_chars": meta["chunk_chars"],
        "chunk_overlap": meta["chunk_overlap"],
        "index_version": meta["index_version"],
        "built_at": meta["built_at"],
        "indexed_revisions": len(indexed_ids),
        "chunks": (
            int(conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"])
            if chunks_table
            else 0
        ),
        "indexable_revisions": len(indexable_ids),
        "missing_revisions": len(missing),
    }
    if not extension:
        result["missing_reason"] = "vec_extension_unavailable"
    elif not chunks_table:
        result["missing_reason"] = "revision_chunks_missing"
    elif not vector_table:
        result["missing_reason"] = "vector_table_missing"
    return result


def semantic_unavailable_reason(
    conn: sqlite3.Connection, config: SemanticConfig, dimension: int | None = None
) -> str | None:
    """查询路径可用性判定；返回退化原因或 None（可用）。

    顺序：依赖 → 索引存在 → 模型/参数一致 → 扩展 → 非空。
    每条原因都对应一种 ``mode=hybrid`` 不报错的退化路径。
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return "sqlite_vec_not_installed"
    if not load_vec_extension(conn):
        return "vec_extension_unavailable"
    meta = semantic_meta(conn)
    if meta is None:
        return "index_not_built"
    # index_version 只取决于参数组合；模型不一致必然表现为版本不一致。
    if meta["index_version"] != index_version_of(config, int(meta["dimension"])):
        return "index_version_mismatch"
    if dimension is not None and int(meta["dimension"]) != dimension:
        return "provider_dimension_mismatch"
    if not _table_exists(conn, "revision_chunks"):
        return "revision_chunks_missing"
    if not _vec_table_exists(conn):
        return "vector_table_missing"
    chunks = conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"]
    if not chunks:
        return "index_empty"
    return None


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------


def chunk_text(
    text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[tuple[int, int]]:
    """归一化坐标分块：短文本单块；重叠保证边界语义不断裂。"""
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [(0, len(text))]
    step = max(1, chunk_chars - overlap)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        spans.append((start, end))
        if end >= len(text):
            break
        start += step
    return spans


# ---------------------------------------------------------------------------
# 索引构建
# ---------------------------------------------------------------------------


def purge_revision_semantic(conn: sqlite3.Connection, revision_ids: list[str]) -> int:
    """撤回/策略不可用时的依赖边清理（§14.2 步骤 4）。

    chunk 行无条件删除；向量行尽力删除（扩展不可用时留下孤儿，
    由下一次增量重建的孤儿清扫兜底——孤儿向量不可能被查询路径返回，
    因为候选必须联接 revision_chunks 且经过同一套授权过滤）。
    返回删除的 chunk 数。
    """
    if not revision_ids:
        return 0
    placeholders = ",".join("?" for _ in revision_ids)
    if _vec_table_exists(conn) and load_vec_extension(conn):
        conn.execute(
            "DELETE FROM revision_chunk_vectors WHERE chunk_id IN ("
            f"SELECT chunk_id FROM revision_chunks WHERE revision_id IN ({placeholders}))",
            revision_ids,
        )
    cur = conn.execute(
        f"DELETE FROM revision_chunks WHERE revision_id IN ({placeholders})",
        revision_ids,
    )
    return cur.rowcount


def reindex_semantic(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    *,
    full: bool = False,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> dict:
    """构建/增量更新语义索引；幂等：同一库连跑两次第二次 0 新增。

    - 参数（模型/维度/分块）与现存 meta 不一致或 ``full=True`` → 全量重建。
    - 增量：删除不再可索引的 revision 的 chunk；孤儿向量清扫；
      为缺失 chunk 的可索引 revision 补 embed。
    """
    if not load_vec_extension(conn):
        raise RuntimeError(
            "sqlite-vec 扩展不可用：请安装可选依赖 pip install 'personal-brain[semantic]'"
            "（离线验证可用 --provider hash + 本地 sqlite-vec）"
        )
    config = SemanticConfig(provider.model_id, chunk_chars, chunk_overlap)
    version = index_version_of(config, provider.dimension)
    meta = semantic_meta(conn)
    if full or meta is None or meta["index_version"] != version:
        full = True

    # 预先规范化，保证 provider 只接收与 FTS 相同的归一化文本；索引坐标
    # 因此天然是 normalized 坐标，查询时可交给 normalize_with_map 还原。
    indexable = _indexable_revisions(conn)
    prepared: list[tuple[sqlite3.Row, str, list[tuple[int, int]]]] = []
    expected_chunks: dict[str, int] = {}
    for row in indexable:
        normalized, _ = normalize_with_map(row["raw_text"] or "")
        spans = chunk_text(normalized, chunk_chars, chunk_overlap)
        expected_chunks[row["revision_id"]] = len(spans)
        if spans:
            prepared.append((row, normalized, spans))
    eligible_ids = set(expected_chunks)

    # 调用方连接上可能有隐式事务（如刚做过 DML）；先落盘再开显式事务
    if conn.in_transaction:
        conn.commit()

    started = datetime.now(UTC)
    if full:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _vec_table_exists(conn):
                conn.execute("DROP TABLE IF EXISTS revision_chunk_vectors")
            conn.execute("DELETE FROM revision_chunks")
            conn.execute("DELETE FROM semantic_index_meta")
            ensure_vector_table(conn, provider.dimension)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_vector_table(conn, provider.dimension)
            existing_rows = conn.execute(
                "SELECT DISTINCT revision_id FROM revision_chunks"
            ).fetchall()
            stale_ids = {
                r["revision_id"] for r in existing_rows
            } - eligible_ids
            # 失效/部分构建的 revision 不能继续被当作已有索引；先删旧 chunks
            # 和 vectors，再在本轮重新 embed，避免增量构建留下静默缺口。
            existing_counts = conn.execute(
                """
                SELECT c.revision_id, COUNT(*) AS chunks,
                       COUNT(v.chunk_id) AS vectors
                FROM revision_chunks c
                LEFT JOIN revision_chunk_vectors v ON v.chunk_id = c.chunk_id
                GROUP BY c.revision_id
                """
            ).fetchall()
            for row in existing_counts:
                rid = row["revision_id"]
                if rid in eligible_ids and (
                    int(row["chunks"]) != expected_chunks[rid]
                    or int(row["vectors"]) != expected_chunks[rid]
                ):
                    stale_ids.add(rid)
            if stale_ids:
                purge_revision_semantic(conn, sorted(stale_ids))
            # 孤儿向量清扫（撤回时扩展不可用留下的）
            conn.execute(
                """
                DELETE FROM revision_chunk_vectors WHERE chunk_id NOT IN (
                    SELECT chunk_id FROM revision_chunks
                )
                """
            )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    # 逐 revision 比较期望 chunk 数与实际 chunk/vector 数，避免部分构建被
    # 误判为完整索引。
    complete_counts = {
        row["revision_id"]: (int(row["chunks"]), int(row["vectors"]))
        for row in conn.execute(
            """
            SELECT c.revision_id, COUNT(*) AS chunks,
                   COUNT(v.chunk_id) AS vectors
            FROM revision_chunks c
            LEFT JOIN revision_chunk_vectors v ON v.chunk_id = c.chunk_id
            GROUP BY c.revision_id
            """
        ).fetchall()
    }
    have = {
        rid for rid, expected in expected_chunks.items()
        if expected > 0 and complete_counts.get(rid) == (expected, expected)
    }
    todo = [item for item in prepared if item[0]["revision_id"] not in have]
    chunks_new = 0
    for i in range(0, len(todo), EMBED_BATCH):
        batch = todo[i : i + EMBED_BATCH]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row, normalized, spans in batch:
                vectors = provider.embed([normalized[a:b] for a, b in spans])
                for j, ((a, b), vec) in enumerate(zip(spans, vectors, strict=True)):
                    cur = conn.execute(
                        """
                        INSERT INTO revision_chunks
                            (revision_id, chunk_index, text_start, text_end)
                        VALUES (?, ?, ?, ?)
                        """,
                        (row["revision_id"], j, a, b),
                    )
                    conn.execute(
                        "INSERT INTO revision_chunk_vectors (chunk_id, embedding)"
                        " VALUES (?, ?)",
                        (cur.lastrowid, json.dumps(vec)),
                    )
                    chunks_new += 1
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    total_indexed = int(
        conn.execute("SELECT COUNT(DISTINCT revision_id) c FROM revision_chunks").fetchone()["c"]
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO semantic_index_meta
                (id, model_id, dimension, chunk_chars, chunk_overlap,
                 index_version, built_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider.model_id,
                provider.dimension,
                chunk_chars,
                chunk_overlap,
                version,
                utc_now_iso(),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {
        "mode": "full" if full else "incremental",
        "model_id": provider.model_id,
        "dimension": provider.dimension,
        "index_version": version,
        "revisions_scanned": len(indexable),
        "revisions_embedded": len(todo),
        "chunks_new": chunks_new,
        "chunks_total": int(
            conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"]
        ),
        "revisions_total": total_indexed,
        "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
    }


# ---------------------------------------------------------------------------
# 查询侧：KNN + 候选行
# ---------------------------------------------------------------------------


def knn_chunks(
    conn: sqlite3.Connection, query_vec: list[float], k: int
) -> list[sqlite3.Row]:
    """vec0 KNN：全局取最近 k 个 chunk（后续按同一套过滤条件授权过滤）。"""
    return conn.execute(
        """
        SELECT v.chunk_id, v.distance, c.revision_id,
               c.text_start, c.text_end
        FROM revision_chunk_vectors v
        JOIN revision_chunks c ON c.chunk_id = v.chunk_id
        WHERE v.embedding MATCH ? AND v.k = ?
        ORDER BY v.distance
        """,
        (json.dumps(query_vec), k),
    ).fetchall()
