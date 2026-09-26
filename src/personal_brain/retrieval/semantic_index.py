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
import math
import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict, cast

from personal_brain.history.normalization import normalize_with_map
from personal_brain.history.policy_epoch import current_policy_epoch
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
# D-8 量化：'none'=float vec0（D-1 结构）；'int8'/'bit'=量化粗排 + float 重排。
# 选型依据见 tools/d8_measure.py 的测量结论与范围 4 的召回对比（交付文档）。
QUANT_TYPES = ("none", "int8", "bit")
DEFAULT_QUANT_TYPE = "int8"
DEFAULT_OVERSAMPLE = 4
# 重排行数上限：粗排 k*oversample 超过它就截断（fp32 主键随机读的 IO 预算）
MAX_RERANK = 2000
# int8 对称量化 scale：归一化向量分量 ∈ [-1,1]，固定映射 v*127 → int8。
# 固定 scale 使 reindex 单遍写入即可（无需预扫描求 max_abs），
# 分量典型值 ~0.04 的量化噪声约 5%，由 oversample 重排兜底。
QUANT_INT8_SCALE = 127.0
# 粗排候选上限再乘以这个比例封顶（相对 KNN_CAP=200 的 10 倍）
MAX_KNN_OVERSAMPLE_RATIO = 10
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
    # None = 查询时跟随索引的量化结构；'none'/'int8'/'bit' = 显式指定
    quant_type: str | None = None
    oversample: int = DEFAULT_OVERSAMPLE

    def __post_init__(self) -> None:
        if self.quant_type is not None and self.quant_type not in QUANT_TYPES:
            raise ValueError(f"未知量化类型: {self.quant_type}（可选 {QUANT_TYPES} 或 None）")
        if self.quant_type not in (None, "none") and self.oversample < 1:
            raise ValueError("oversample 必须 ≥ 1")


def index_version_of(config: SemanticConfig, dimension: int) -> str:
    """模型/维度/分块/量化参数任一变化 → 版本变化 → 视为不匹配。

    量化为 'none' 时版本与 D-1（float）完全一致：老库升级到 v7 不触发
    全量重嵌；量化参数变化只产生版本后缀，转换（不重嵌）即可切换。
    """
    payload = json.dumps(
        {
            "model_id": config.model_id,
            "dimension": dimension,
            "chunk_chars": config.chunk_chars,
            "chunk_overlap": config.chunk_overlap,
        },
        sort_keys=True,
    )
    base = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    quant = config.quant_type or "none"
    if quant == "none":
        return base
    qpayload = json.dumps(
        {"base": base, "quant_type": quant,
         "oversample": config.oversample},
        sort_keys=True,
    )
    return base + "-" + hashlib.sha1(qpayload.encode("utf-8")).hexdigest()[:8]


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


def ensure_vector_table(
    conn: sqlite3.Connection, dimension: int, quant_type: str = "none"
) -> None:
    """按维度与量化类型创建/重建 vec0 虚拟表（维度/类型写入建表语句）。

    - none : float[dim]  —— D-1 结构（查询即全量 float KNN）；
    - int8 : int8[dim]   —— 粗排表，float 原值另存 fp32 表供重排；
    - bit  : bit[dim]    —— 同上，粗排用 hamming。
    """
    if quant_type not in QUANT_TYPES:
        raise ValueError(f"未知量化类型: {quant_type}")
    if _vec_table_exists(conn):
        meta = semantic_meta(conn)
        current = _current_vec_type(conn)
        if meta is not None and meta["dimension"] == dimension and current == quant_type:
            return
        conn.execute("DROP TABLE IF EXISTS revision_chunk_vectors")
    element = {"none": "float", "int8": "int8", "bit": "bit"}[quant_type]
    conn.execute(
        f"CREATE VIRTUAL TABLE revision_chunk_vectors USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding {element}[{dimension}])"
    )


def _current_vec_type(conn: sqlite3.Connection) -> str | None:
    """从 vec0 建表 SQL 推断当前量化类型（none/int8/bit）；表不存在返回 None。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='revision_chunk_vectors'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return None
    declared = str(row["sql"])
    if "int8[" in declared:
        return "int8"
    if "bit[" in declared:
        return "bit"
    return "none"


def fp32_table_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "revision_chunk_vectors_fp32")


def quant_meta(conn: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT * FROM semantic_quant_meta WHERE id = 1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise


def _set_quant_meta(
    conn: sqlite3.Connection, quant_type: str, oversample: int, scale: float | None
) -> None:
    conn.execute("DELETE FROM semantic_quant_meta")
    conn.execute(
        "INSERT INTO semantic_quant_meta (id, quant_type, quant_scale, oversample)"
        " VALUES (1, ?, ?, ?)",
        (quant_type, scale, oversample),
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


def _revision_rowid_high_water(conn: sqlite3.Connection) -> int:
    """event_revisions 的 MAX(rowid)：只增不改（正文不可变，只 INSERT），
    未变化 = 没有新/改动的 revision（增量短路判定用，见 reindex_semantic）。
    """
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM event_revisions").fetchone()
    return int(row["m"])


_INDEXABLE_PAGE = 500
# 实测 1 GB VM：一次 32 块长文本推理峰值 577MB，8 块 340MB（模型常驻约 220MB）
EMBED_MAX_CHUNKS_PER_CALL = 8


def _iter_indexable_revisions(conn: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    """按 revision_id 分页流式产出当前可索引 revision；凭据形态不算作索引缺口。

    不一次性物化全库正文：6 万条级、含超长消息的库在 1 GB 内存机器上会被 OOM 杀掉。
    每页 fetchall 后再产出，迭代期间不持有打开的游标，调用方可在两页之间写库。
    """
    last = ""
    while True:
        rows = conn.execute(
            """
            SELECT DISTINCT r.revision_id, r.raw_text
            FROM event_revisions r
            JOIN events e ON e.event_id = r.event_id
            JOIN sources s ON s.source_id = e.source_id
            WHERE s.status != 'withdrawn'
              AND r.revision_id > ?
              AND EXISTS (
                SELECT 1 FROM revision_policy_state ps
                WHERE ps.revision_id = r.revision_id
                  AND ps.valid_to IS NULL AND ps.availability = 'available')
            ORDER BY r.revision_id
            LIMIT ?
            """,
            (last, _INDEXABLE_PAGE),
        ).fetchall()
        if not rows:
            return
        last = rows[-1]["revision_id"]
        for r in rows:
            if r["raw_text"] and not contains_known_credentials(r["raw_text"]):
                yield r


def _fetch_revisions_for_embed(
    conn: sqlite3.Connection, revision_ids: set[str]
) -> Iterator[sqlite3.Row]:
    """按 revision_id 精确取 raw_text，不扫全表（D-8 范围 5：增量提速）。

    reindex_semantic 的 need_embed 集合通常只有几十/几百个 revision_id；
    用主键 IN (...) 批量点查（分批 500 个，避开 SQLite 变量数上限）比
    _iter_indexable_revisions 那种"扫全部可索引 revision、逐行过滤"快得多——
    后者对"无新数据"或"只新增几十个"的增量场景仍然要读全部 raw_text。
    """
    ids = sorted(revision_ids)
    for i in range(0, len(ids), 500):
        batch = ids[i : i + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT revision_id, raw_text FROM event_revisions"
            f" WHERE revision_id IN ({placeholders})",
            batch,
        ).fetchall()
        for r in rows:
            if r["raw_text"] and not contains_known_credentials(r["raw_text"]):
                yield r


def _indexable_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全量列表（仅供小库 / 测试使用；生产路径用 _iter_indexable_revisions）。"""
    return list(_iter_indexable_revisions(conn))


def _indexable_revision_count(conn: sqlite3.Connection) -> int:
    return sum(1 for _ in _iter_indexable_revisions(conn))


def semantic_status(conn: sqlite3.Connection) -> dict:
    """只读状态汇总（doctor 用）：存在性、模型、维度、缺口。"""
    extension = load_vec_extension(conn)
    meta = semantic_meta(conn)
    indexable_ids = {r["revision_id"] for r in _iter_indexable_revisions(conn)}
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
    fp32_rows = (
        int(conn.execute(
            "SELECT COUNT(*) c FROM revision_chunk_vectors_fp32"
        ).fetchone()["c"])
        if fp32_table_exists(conn) else 0
    )  # sqlite3.Row 索引在 fetchone() 非空时安全（COUNT 恒 1 行）
    fp32_bytes = (
        int(conn.execute(
            "SELECT COALESCE(SUM(LENGTH(embedding)), 0) b FROM revision_chunk_vectors_fp32"
        ).fetchone()["b"])
        if fp32_table_exists(conn) else 0
    )
    quant_rows = 0
    quant_bytes = 0
    if vector_table:
        vtype = _current_vec_type(conn)
        per = {"none": 4, "int8": 1, "bit": 0.125}.get(vtype or "none", 4)
        quant_rows = int(conn.execute(
            "SELECT COUNT(*) c FROM revision_chunk_vectors"
        ).fetchone()["c"])
        dim = int(meta["dimension"]) if meta is not None else 0
        quant_bytes = int(quant_rows * dim * per)
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
        "quant_type": qrow["quant_type"] if (qrow := quant_meta(conn)) else "none",
        "oversample": int(qrow["oversample"]) if qrow else 0,
        "fp32_rows": fp32_rows,
        "fp32_bytes": fp32_bytes,
        "quant_vector_rows": quant_rows,
        "quant_vector_bytes_est": quant_bytes,
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
    if not meta["built_at"]:
        return "index_building"
    qmeta = quant_meta(conn)
    index_quant = qmeta["quant_type"] if qmeta is not None else "none"
    # 查询侧未显式指定量化方式时跟随索引；显式指定则必须一致
    # （转换只改存储结构不重嵌，切量化方式 = 重跑一次 convert）。
    effective = config.quant_type if config.quant_type is not None else index_quant
    if effective != index_quant:
        return "index_version_mismatch"
    effective_cfg = SemanticConfig(
        config.model_id, config.chunk_chars, config.chunk_overlap,
        cache_dir=config.cache_dir,
        quant_type=effective,
        oversample=(qmeta["oversample"] if qmeta is not None else DEFAULT_OVERSAMPLE)
        if effective != "none" else 0,
    )
    # index_version 只取决于参数组合；模型不一致必然表现为版本不一致。
    if meta["index_version"] != index_version_of(effective_cfg, int(meta["dimension"])):
        return "index_version_mismatch"
    if dimension is not None and int(meta["dimension"]) != dimension:
        return "provider_dimension_mismatch"
    if not _table_exists(conn, "revision_chunks"):
        return "revision_chunks_missing"
    if not _vec_table_exists(conn):
        return "vector_table_missing"
    if effective != "none":
        # 量化结构：粗排 vec0 是 int8/bit，重排依赖 fp32 行表
        if not fp32_table_exists(conn):
            return "fp32_table_missing"
        if _current_vec_type(conn) != effective:
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


def semantic_pending(
    conn: sqlite3.Connection, config: SemanticConfig
) -> dict:
    """增量前置判定（不加载模型、不做 normalize）：下一步该做什么。

    action:
    - skip    : 无待索引内容（CLI 直接退出，不构建 provider）；
    - reindex : 有新增/变化/撤回的 revision（需要 provider）；
    - convert : 基础参数一致、仅量化方式/参数不同（跑 --convert，不重嵌）；
    - full    : 模型/维度/分块变化或索引未建（需要 provider 全量重建）。
    quant_type 返回索引当前的结构（CLI reindex 时跟随它，避免误触发全量）。
    """
    meta = semantic_meta(conn)
    qmeta = quant_meta(conn)
    index_quant = qmeta["quant_type"] if qmeta is not None else "none"
    scanned = stale = pending = 0
    base: dict[str, object] = {
        "quant_type": index_quant,
    }
    if meta is None:
        return {**base, "action": "full", "reason": "index_not_built"}
    if not meta["built_at"]:
        return {**base, "action": "reindex", "reason": "index_building_resume"}
    effective_cfg = SemanticConfig(
        config.model_id, config.chunk_chars, config.chunk_overlap,
        cache_dir=config.cache_dir,
        quant_type=index_quant,
        oversample=(int(qmeta["oversample"]) if qmeta is not None else DEFAULT_OVERSAMPLE)
        if index_quant != "none" else 0,
    )
    version = index_version_of(effective_cfg, int(meta["dimension"]))
    if meta["index_version"].split("-")[0] != version.split("-")[0]:
        return {**base, "action": "full", "reason": "index_version_mismatch"}
    if meta["index_version"] != version:
        return {**base, "action": "convert", "reason": "quantization_mismatch"}
    state = _load_chunk_state(conn)
    for rid in _iter_indexable_revision_ids(conn):
        scanned += 1
        entry = state.get(rid)
        if entry is None or entry[1] != version:
            pending += 1
    base["scanned"] = scanned
    base["pending"] = pending
    base["stale"] = stale
    if pending == 0:
        return {**base, "action": "skip"}
    return {**base, "action": "reindex"}


def _iter_indexable_revision_ids(conn: sqlite3.Connection) -> Iterator[str]:
    """增量判定用的可索引 revision_id 列表——不读 raw_text。

    event_revisions 的正文不可变：改内容会经 revision_hash 派生出新的
    revision_id（同一 revision_id 永远对应同一段文本，见
    history/store.py::_insert_revision），所以判定"这个 revision 要不要
    重嵌"只需要 revision_id 本身 + 当前 index_version（分块参数已编码在
    index_version 里），不需要任何内容指纹/哈希；旧版按"长度+首尾 64 字符"
    做指纹既多余（内容不可能变）又不可靠（检测不到中间内容的改动）。
    """
    last = ""
    while True:
        rows = conn.execute(
            """
            SELECT DISTINCT r.revision_id
            FROM event_revisions r
            JOIN events e ON e.event_id = r.event_id
            JOIN sources s ON s.source_id = e.source_id
            WHERE s.status != 'withdrawn'
              AND r.revision_id > ?
              AND EXISTS (
                SELECT 1 FROM revision_policy_state ps
                WHERE ps.revision_id = r.revision_id
                  AND ps.valid_to IS NULL AND ps.availability = 'available')
            ORDER BY r.revision_id
            LIMIT ?
            """,
            (last, _INDEXABLE_PAGE),
        ).fetchall()
        if not rows:
            return
        last = rows[-1]["revision_id"]
        for row in rows:
            yield row["revision_id"]


def _load_chunk_state(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """revision_chunk_state 全量读入：rid -> (chunk_count, index_version)。"""
    try:
        rows = conn.execute(
            "SELECT revision_id, chunk_count, index_version"
            " FROM revision_chunk_state"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise
    return {
        r["revision_id"]: (int(r["chunk_count"]), r["index_version"])
        for r in rows
    }


def existing_any_chunks(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "revision_chunks") and bool(
        conn.execute("SELECT 1 FROM revision_chunks LIMIT 1").fetchone()
    )


def _backfill_chunk_state(conn: sqlite3.Connection, version: str) -> None:
    """v7 升级后第一次增量：按现状回填 revision_chunk_state。

    chunk_count 取 revision_chunks 实际行数（D-1 构建时的完整性口径）；
    不需要内容指纹（见 _iter_indexable_revision_ids 文档字符串）。
    """
    counts = {
        r["revision_id"]: int(r["chunks"])
        for r in conn.execute(
            "SELECT revision_id, COUNT(*) AS chunks FROM revision_chunks GROUP BY revision_id"
        )
    }
    batch: list[tuple[str, int]] = []
    for rid in _iter_indexable_revision_ids(conn):
        batch.append((rid, counts.get(rid, 0)))
        if len(batch) >= 5000:
            _commit_state_batch(conn, batch, version)
            batch.clear()
    if batch:
        _commit_state_batch(conn, batch, version)


def _quant_insert_sql(quant_type: str) -> str:
    """vec0 写入 SQL（int8/bit 需要显式包装函数声明元素类型）。"""
    if quant_type == "int8":
        return (
            "INSERT OR IGNORE INTO revision_chunk_vectors"
            " (chunk_id, embedding) VALUES (?, vec_int8(?))"
        )
    if quant_type == "bit":
        return (
            "INSERT OR IGNORE INTO revision_chunk_vectors"
            " (chunk_id, embedding) VALUES (?, vec_bit(?))"
        )
    return "INSERT OR REPLACE INTO revision_chunk_vectors (chunk_id, embedding) VALUES (?, ?)"


_PURGE_BATCH = 500  # SQLite 变量数上限 999（部分构建更低）；留够余量分批


def purge_revision_semantic(conn: sqlite3.Connection, revision_ids: list[str]) -> int:
    """撤回/策略不可用时的依赖边清理（§14.2 步骤 4）。

    chunk 行无条件删除；向量行尽力删除（扩展不可用时留下孤儿，
    由下一次增量重建的孤儿清扫兜底——孤儿向量不可能被查询路径返回，
    因为候选必须联接 revision_chunks 且经过同一套授权过滤）。
    返回删除的 chunk 数。

    revision_ids 按 `_PURGE_BATCH` 分批：一次性把全部 ID 绑进单条 IN (...)
    在 stale revision 数量大（增量重跑时可能上万）时会超过 SQLite 的
    变量数上限，直接报错。
    """
    if not revision_ids:
        return 0
    have_vec = _vec_table_exists(conn) and load_vec_extension(conn)
    have_fp32 = fp32_table_exists(conn)
    chunks_deleted = 0
    for i in range(0, len(revision_ids), _PURGE_BATCH):
        batch = revision_ids[i : i + _PURGE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        if have_vec:
            conn.execute(
                "DELETE FROM revision_chunk_vectors WHERE chunk_id IN ("
                f"SELECT chunk_id FROM revision_chunks WHERE revision_id IN ({placeholders}))",
                batch,
            )
        if have_fp32:
            conn.execute(
                "DELETE FROM revision_chunk_vectors_fp32 WHERE chunk_id IN ("
                f"SELECT chunk_id FROM revision_chunks WHERE revision_id IN ({placeholders}))",
                batch,
            )
        conn.execute(
            f"DELETE FROM revision_chunk_state WHERE revision_id IN ({placeholders})",
            batch,
        )
        cur = conn.execute(
            f"DELETE FROM revision_chunks WHERE revision_id IN ({placeholders})",
            batch,
        )
        chunks_deleted += cur.rowcount
    return chunks_deleted


def reindex_semantic(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    *,
    full: bool = False,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    quant_type: str = "none",
    oversample: int = DEFAULT_OVERSAMPLE,
) -> dict:
    """构建/增量更新语义索引；幂等：同一库连跑两次第二次 0 新增。

    D-8 增量判定改为 ``revision_id`` + ``index_version`` 与
    ``revision_chunk_state`` 的比对（不用内容哈希/指纹——正文不可变，见
    ``_iter_indexable_revision_ids``）：文本没变的 revision 不再重新归一化
    与分块，也不再做全表 LEFT JOIN 计数；孤儿向量清扫只针对本轮失效的
    revision（撤回路径已即时清理），全表清扫移到 ``full=True``。另有一层
    水位短路（``event_revisions`` 高水位 rowid + ``policy_epoch``）：两者
    都没变时跳过整段可索引性扫描，直接返回。
    ``quant_type`` 指定重建后的向量存储结构（'none' 纯 float vec0；
    'int8'/'bit' 量化粗排 + fp32 重排，float 原值进 fp32 行表）。
    """
    if quant_type not in QUANT_TYPES:
        raise ValueError(f"未知量化类型: {quant_type}")
    if not load_vec_extension(conn):
        raise RuntimeError(
            "sqlite-vec 扩展不可用：请安装可选依赖 pip install 'personal-brain[semantic]'"
            "（离线验证可用 --provider hash + 本地 sqlite-vec）"
        )
    config = SemanticConfig(
        provider.model_id, chunk_chars, chunk_overlap,
        quant_type=quant_type, oversample=oversample,
    )
    version = index_version_of(config, provider.dimension)
    meta = semantic_meta(conn)
    if full or meta is None or meta["index_version"] != version:
        full = True

    if not full and meta is not None and meta["built_at"]:
        # 短路：event_revisions 只增不改、policy_epoch 只在撤回/改标签时才
        # 变——两个水位都和上次 reindex 完成时一致，说明没有任何新增/变化/
        # 撤回的 revision，下面昂贵的可索引性扫描（JOIN+EXISTS，10 万
        # revision 实测 ~10s）注定会得出同样的空 need_embed/空 stale_ids，
        # 可以直接跳过。built_at 为空说明上次构建被中断，必须继续走完整
        # 判定，不能短路。
        current_rowid = _revision_rowid_high_water(conn)
        current_epoch = current_policy_epoch(conn)
        if (meta["last_revision_rowid"] == current_rowid
                and meta["last_policy_epoch"] == current_epoch):
            fp32_rows = 0 if quant_type == "none" else int(
                conn.execute(
                    "SELECT COUNT(*) c FROM revision_chunk_vectors_fp32"
                ).fetchone()["c"]
            )
            return {
                "mode": "incremental",
                "quant_type": quant_type,
                "fp32_rows": fp32_rows,
                "model_id": provider.model_id,
                "dimension": provider.dimension,
                "index_version": version,
                "revisions_scanned": 0,
                "revisions_embedded": 0,
                "chunks_new": 0,
                "chunks_total": int(
                    conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"]
                ),
                "revisions_total": int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT revision_id) c FROM revision_chunks"
                    ).fetchone()["c"]
                ),
                "elapsed_ms": 0,
                "short_circuited": True,
            }

    # 第一遍流式扫描：只取 revision_id 列表（不读 raw_text——内容不可变，
    # 不需要指纹；6.8 万 revision 上省掉这些 IO 是增量提速主因）。
    eligible_ids = set(_iter_indexable_revision_ids(conn))
    state = _load_chunk_state(conn)  # rid -> (chunk_count, index_version)
    # full 分支会清掉全部 chunk/state，have 只在增量路径有意义
    have = set() if full else {
        rid for rid, (_n, v) in state.items()
        if rid in eligible_ids and v == version
    }

    # 调用方连接上可能有隐式事务（如刚做过 DML）；先落盘再开显式事务
    if conn.in_transaction:
        conn.commit()

    started = datetime.now(UTC)
    if full:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _vec_table_exists(conn):
                conn.execute("DROP TABLE IF EXISTS revision_chunk_vectors")
            # fp32 先删：revision_chunk_vectors_fp32.chunk_id 外键引用
            # revision_chunks(chunk_id)，connect() 打开 foreign_keys=ON，
            # 反过来删会在"已有数据的库上再跑一次 full"时报 FK 约束失败
            # （首次全新建库两张表都还是空的，不会触发，容易漏掉）。
            conn.execute("DELETE FROM revision_chunk_vectors_fp32")
            conn.execute("DELETE FROM revision_chunks")
            conn.execute("DELETE FROM semantic_index_meta")
            conn.execute("DELETE FROM revision_chunk_state")
            ensure_vector_table(conn, provider.dimension, quant_type)
            # 先写一行"构建中"（built_at 为空串）：中途被杀后，下次按相同参数增量续建，
            # 而不是因为没有 meta 又从头全量重建；查询路径在构建完成前视为不可用。
            conn.execute(
                """
                INSERT INTO semantic_index_meta
                    (id, model_id, dimension, chunk_chars, chunk_overlap,
                     index_version, built_at)
                VALUES (1, ?, ?, ?, ?, ?, '')
                """,
                (provider.model_id, provider.dimension, chunk_chars, chunk_overlap, version),
            )
            _set_quant_meta(conn, quant_type, oversample if quant_type != "none" else 0, None)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    else:
        # v6→v7 升级后的第一次增量：老库没有 state 行。按现状回填
        # （COUNT chunks per revision），保证老索引在不重嵌的前提下继续
        # 增量；有 state 的部分不受影响。
        if not state and existing_any_chunks(conn):
            _backfill_chunk_state(conn, version)
            state = _load_chunk_state(conn)
            have = {
                rid for rid, (_n, v) in state.items()
                if rid in eligible_ids and v == version
            }
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_vector_table(conn, provider.dimension, quant_type)
            # 失效判定：state 缺失（未构建/部分构建）、index_version 不匹配、
            # 或已不可索引。不再做全表 LEFT JOIN 计数——state 行即完整性承诺。
            existing_chunk_rids = {
                r["revision_id"] for r in conn.execute(
                    "SELECT DISTINCT revision_id FROM revision_chunks"
                )
            }
            stale_ids = (existing_chunk_rids - eligible_ids) | (
                (existing_chunk_rids & eligible_ids) - have
            )
            if stale_ids:
                purge_revision_semantic(conn, sorted(stale_ids))
            # 孤儿向量只可能来自撤回时扩展不可用；撤回路径已即时清理，
            # 兜底全表清扫移到 full 路径（D-8 范围 5）。
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    # 增量路径新增 chunk 的 int8 打包 scale：复用现有索引已经定下的动态 scale
    # （查询侧也在用同一个值），不为几十个新 chunk 重新扫全量求 max|v|——
    # 单行 outlier 被夹紧到 ±127，对粗排排序的影响可忽略（見 flush() 内注释）。
    incremental_scale = QUANT_INT8_SCALE
    if not full and quant_type == "int8":
        existing_quant = quant_meta(conn)
        if existing_quant is not None and existing_quant["quant_scale"] is not None:
            incremental_scale = float(existing_quant["quant_scale"])
    # 第二遍流式扫描：只对缺失的 revision 重新归一化、分块、embed；每 EMBED_BATCH 个提交一次
    chunks_new = 0
    revisions_embedded = 0
    pending: list[tuple[str, list[tuple[int, int]], list[list[float]]]] = []

    def flush() -> None:
        nonlocal chunks_new
        if not pending:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for rid, spans, vectors in pending:
                for j, ((a, b), vec) in enumerate(zip(spans, vectors, strict=True)):
                    cur = conn.execute(
                        """
                        INSERT INTO revision_chunks
                            (revision_id, chunk_index, text_start, text_end)
                        VALUES (?, ?, ?, ?)
                        """,
                        (rid, j, a, b),
                    )
                    cid = cur.lastrowid
                    if quant_type == "none":
                        conn.execute(_quant_insert_sql(quant_type), (cid, json.dumps(vec)))
                    else:
                        # int8/bit：先写 fp32 行表；量化 vec0 表在批量结束后
                        # 统一按动态 scale 填充（full），或按现有 scale 追加
                        # （incremental，单行 outlier 夹紧到 ±127，影响可忽略）。
                        conn.execute(
                            "INSERT INTO revision_chunk_vectors_fp32 (chunk_id, embedding)"
                            " VALUES (?, ?)",
                            (cid, struct.pack(f"{len(vec)}f", *vec)),
                        )
                        if not full:
                            blob = (
                                _pack_int8(vec, incremental_scale)
                                if quant_type == "int8" else _pack_bit(vec)
                            )
                            conn.execute(_quant_insert_sql(quant_type), (cid, blob))
                    chunks_new += 1
                conn.execute(
                    """
                    INSERT OR REPLACE INTO revision_chunk_state
                        (revision_id, chunk_count, index_version)
                    VALUES (?, ?, ?)
                    """,
                    (rid, int(len(spans)), version),
                )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        pending.clear()

    need_embed = eligible_ids - have  # 缺失/index_version 不匹配的
    for row in _fetch_revisions_for_embed(conn, need_embed):
        rid = row["revision_id"]
        normalized, _ = normalize_with_map(row["raw_text"] or "")
        spans = chunk_text(normalized, chunk_chars, chunk_overlap)
        if not spans:
            continue
        vectors: list[list[float]] = []
        for k in range(0, len(spans), EMBED_MAX_CHUNKS_PER_CALL):
            part = spans[k : k + EMBED_MAX_CHUNKS_PER_CALL]
            vectors.extend(provider.embed([normalized[a:b] for a, b in part]))
        pending.append((rid, spans, vectors))
        revisions_embedded += 1
        if len(pending) >= EMBED_BATCH:
            flush()
    flush()

    if full and quant_type != "none":
        fill_scale = _fill_quant_from_fp32(conn, provider.dimension, quant_type)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _set_quant_meta(conn, quant_type, oversample, fill_scale)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    total_indexed = int(
        conn.execute("SELECT COUNT(DISTINCT revision_id) c FROM revision_chunks").fetchone()["c"]
    )
    # 水位：这次 reindex 完成时刻的 event_revisions 高水位 + policy_epoch，
    # 供下一次调用的短路判定用（见函数开头）。
    watermark_rowid = _revision_rowid_high_water(conn)
    watermark_epoch = current_policy_epoch(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO semantic_index_meta
                (id, model_id, dimension, chunk_chars, chunk_overlap,
                 index_version, built_at, last_revision_rowid, last_policy_epoch)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider.model_id,
                provider.dimension,
                chunk_chars,
                chunk_overlap,
                version,
                utc_now_iso(),
                watermark_rowid,
                watermark_epoch,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    quant_written = 0 if quant_type == "none" else int(
        conn.execute("SELECT COUNT(*) c FROM revision_chunk_vectors_fp32").fetchone()["c"]
    )
    return {
        "mode": "full" if full else "incremental",
        "quant_type": quant_type,
        "fp32_rows": quant_written,
        "model_id": provider.model_id,
        "dimension": provider.dimension,
        "index_version": version,
        "revisions_scanned": len(eligible_ids),
        "revisions_embedded": revisions_embedded,
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


# ---------------------------------------------------------------------------
# D-8 迁移：float vec0 → 量化粗排 + fp32 重排（就地转换，不重新 embed）
# ---------------------------------------------------------------------------


def convert_vectors_to_quantized(
    conn: sqlite3.Connection,
    dimension: int,
    *,
    quant_type: str = DEFAULT_QUANT_TYPE,
    oversample: int = DEFAULT_OVERSAMPLE,
    batch_chunks: int = 20000,
    vacuum: bool = True,
) -> dict:
    """把 float vec0 向量就地转换为 ``量化粗排 + fp32 重排`` 结构。

    前置：已存在完整 float 索引（meta.built_at 非空，vec0 为 float[dim]）。
    步骤（每步提交，任一步中断后重跑即可续）：
      A. built_at 置空 → 查询路径进入 index_building，不返回半截结果；
      B. float vec0 → fp32 行表（按 chunk_id 主键，分批 INSERT OR IGNORE，
         断点后重跑自动跳过已完成行）；
      C. DROP float vec0 → 建量化 vec0 → 从 fp32 流式量化写入（OR IGNORE）；
      D. 填 revision_chunk_state（revision_id/chunk_count/index_version，
         不需要内容哈希——正文不可变，见 _iter_indexable_revision_ids）；
      E. 写 meta（index_version 带量化后缀）与 semantic_quant_meta，
         built_at 写回 → 查询恢复；
      F. 可选 VACUUM 回收旧 float vec0 的空间（需要约等于删除数据量的
         临时磁盘；空间紧张可 --no-vacuum，空闲页会随写入逐步复用）。
    峰值内存 ≈ batch_chunks × (dim×4 + dim×1) 字节 + 基线，2 万批约 50 MB。
    """
    if quant_type not in ("int8", "bit"):
        raise ValueError(f"转换目标必须是 int8 或 bit，不是 {quant_type!r}")
    if not load_vec_extension(conn):
        raise RuntimeError("sqlite-vec 扩展不可用")
    conn.row_factory = sqlite3.Row
    meta = semantic_meta(conn)
    if meta is None:
        raise RuntimeError("语义索引未构建（semantic_index_meta 缺失）")
    if meta["dimension"] != dimension:
        raise RuntimeError(
            f"维度不匹配：索引 {meta['dimension']} vs provider {dimension}"
        )
    if not meta["built_at"]:
        interrupted_convert = (
            (_table_exists(conn, "revision_chunk_vectors_fp32")
             and conn.execute(
                 "SELECT 1 FROM revision_chunk_vectors_fp32 LIMIT 1").fetchone())
            or _current_vec_type(conn) != "none"
        )
        if not interrupted_convert:
            raise RuntimeError("索引正在构建中（built_at 为空），先完成构建再转换")
        # convert 中断续跑：继续走，B/C 步都是幂等写
    if _current_vec_type(conn) != "none" and meta["built_at"]:
        raise RuntimeError("vec0 表不是 float 结构，无需转换（如需换参数请 --full 重建）")
    # built_at 为空且 vec0 已是量化结构 = 上一次 convert 中断 → 允许续跑
    if not fp32_table_exists(conn):
        raise RuntimeError("revision_chunk_vectors_fp32 不存在：请先把 schema 升到 v7")
    base_version = meta["index_version"].split("-")[0]
    target_cfg = SemanticConfig(
        meta["model_id"], int(meta["chunk_chars"]), int(meta["chunk_overlap"]),
        quant_type=quant_type, oversample=oversample,
    )
    target_version = base_version + "-" + index_version_of(
        target_cfg, dimension
    ).split("-")[1]

    if conn.in_transaction:
        conn.commit()
    started = datetime.now(UTC)

    # A：进入 index_building
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE semantic_index_meta SET built_at = '' WHERE id = 1"
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()

    # B：float vec0 → fp32（分批，OR IGNORE 幂等 = 断点续跑）。
    # 续跑时 float vec0 可能已被 DROP（中断发生在 C 之后）：跳过 B，
    # fp32 已有行保持原样，绝不能从量化表回灌（维度/精度都不同）。
    if _current_vec_type(conn) == "none":
        conn.execute("BEGIN IMMEDIATE")
        try:
            moved = 0
            last = -1
            while True:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO revision_chunk_vectors_fp32
                        (chunk_id, embedding)
                    SELECT chunk_id, embedding FROM revision_chunk_vectors
                    WHERE chunk_id > ? AND chunk_id <= ? + ?
                      AND chunk_id NOT IN (SELECT chunk_id FROM revision_chunk_vectors_fp32)
                    ORDER BY chunk_id
                    """,
                    (last, last, batch_chunks),
                )
                conn.commit()
                moved += max(0, cur.rowcount)
                nxt = conn.execute(
                    "SELECT MAX(chunk_id) m FROM revision_chunk_vectors_fp32"
                ).fetchone()["m"]
                if nxt is None or int(nxt) <= last:
                    break
                last = int(nxt)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    # C：换 vec0 结构，从 fp32 流式量化写入。int8 的 scale 是动态的
    # （max|v|/127，见 _fill_quant_from_fp32 文档字符串），不能再用固定
    # QUANT_INT8_SCALE——那正是召回重合率 0.68 的根因。
    # 无条件先 DROP 再建：ensure_vector_table 只在维度/类型不匹配时才会
    # DROP，中断续跑时新表的维度/类型和上次中断时已经一致，它会跳过
    # DROP，留下的部分行会让下面的 INSERT OR IGNORE 撞上 vec0 虚拟表——
    # 实测 sqlite-vec 的 vec0 不支持 ON CONFLICT（INSERT OR IGNORE 仍报
    # UNIQUE constraint failed），必须保证这里总是从空表开始。
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS revision_chunk_vectors")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    scale = _fill_quant_from_fp32(conn, dimension, quant_type, batch_chunks=batch_chunks)
    total = int(conn.execute("SELECT COUNT(*) c FROM revision_chunk_vectors_fp32").fetchone()["c"])

    # D：state 表（增量判定要用；转换不重嵌，不需要内容哈希）
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM revision_chunk_state")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    counts = dict(
        (r["revision_id"], int(r["chunks"]))
        for r in conn.execute(
            "SELECT revision_id, COUNT(*) AS chunks FROM revision_chunks GROUP BY revision_id"
        )
    )
    last_rid = ""
    batch: list[tuple[str, int]] = []
    for rid in _iter_indexable_revision_ids(conn):
        if rid <= last_rid:
            continue
        last_rid = rid
        batch.append((rid, counts.get(rid, 0)))
        if len(batch) >= 5000:
            _commit_state_batch(conn, batch, target_version)
            batch.clear()
    if batch:
        _commit_state_batch(conn, batch, target_version)

    # E：meta + quant meta + built_at 写回
    conn.execute("BEGIN IMMEDIATE")
    try:
        _set_quant_meta(conn, quant_type, oversample, scale)
        conn.execute(
            """
            INSERT OR REPLACE INTO semantic_index_meta
                (id, model_id, dimension, chunk_chars, chunk_overlap,
                 index_version, built_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                meta["model_id"], int(meta["dimension"]),
                int(meta["chunk_chars"]), int(meta["chunk_overlap"]),
                target_version, utc_now_iso(),
            ),
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    if vacuum:
        conn.execute("VACUUM")
    return {
        "quant_type": quant_type,
        "oversample": oversample,
        "index_version": target_version,
        "chunks": total,
        "elapsed_s": round((datetime.now(UTC) - started).total_seconds(), 1),
    }


def _tensor_max_abs(conn: sqlite3.Connection, batch_chunks: int = 20000) -> float:
    """预扫 fp32 表求全局 max|v|（纯 Python 流式，30 万行 ~20s）。"""
    last = 0
    max_abs = 1e-12
    while True:
        rows = conn.execute(
            "SELECT chunk_id, embedding FROM revision_chunk_vectors_fp32"
            " WHERE chunk_id > ? ORDER BY chunk_id LIMIT ?",
            (last, batch_chunks),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            blob = r["embedding"]
            for x in struct.unpack(f"{len(blob) // 4}f", blob):
                ax = abs(x)
                if ax > max_abs:
                    max_abs = ax
        last = int(rows[-1]["chunk_id"])
    return max_abs


def _fill_quant_from_fp32(
    conn: sqlite3.Connection, dimension: int, quant_type: str,
    batch_chunks: int = 20000,
) -> float:
    """从 fp32 表（完整）流式量化写 vec0 表；返回写库用的 scale。

    _pack_int8 是"乘 scale 再 round"（q8 = round(v * scale)），所以 scale
    必须是"每单位 float 对应多少 int8 刻度"，即 127/max_abs——不是
    max_abs/127（早前一版正是反过来算的，等价于把所有分量都压缩到 0 附近，
    量化后全部趋同、粗排排序信号完全丢失；实测 top-10 重合率 0.0，
    发现于用真实 bge-small-zh 模型评测时，见 D-8 交付）。
    固定 scale=127（假设分量已接近 ±1）会让 bge 的典型分量只占 int8 动态
    范围的 ~4%（bge 分量典型幅值 ~1/sqrt(512)≈0.044），量化噪声吞掉排序
    信号；动态 scale 让最大分量用满 ±127，显著提高粗排信号/噪声比。
    调用方必须保证 fp32 表完整。
    """
    ensure_vector_table(conn, dimension, quant_type)
    quant_insert = _quant_insert_sql(quant_type)
    if quant_type == "int8":
        scale = round(127.0 / _tensor_max_abs(conn), 8)
    else:
        scale = QUANT_INT8_SCALE
    last = 0
    while True:
        rows = conn.execute(
            "SELECT chunk_id, embedding FROM revision_chunk_vectors_fp32"
            " WHERE chunk_id > ? ORDER BY chunk_id LIMIT ?",
            (last, batch_chunks),
        ).fetchall()
        if not rows:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                vec = list(struct.unpack(f"{len(r['embedding']) // 4}f", r["embedding"]))
                if quant_type == "int8":
                    blob = _pack_int8(vec, scale)
                else:
                    blob = _pack_bit(vec)
                conn.execute(quant_insert, (int(r["chunk_id"]), blob))
                last = int(r["chunk_id"])
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    return scale


def _commit_state_batch(
    conn: sqlite3.Connection, batch: list[tuple[str, int]], version: str
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            """
            INSERT OR REPLACE INTO revision_chunk_state
                (revision_id, chunk_count, index_version)
            VALUES (?, ?, ?)
            """,
            [(rid, n, version) for rid, n in batch],
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


# ---------------------------------------------------------------------------
# 查询侧：量化粗排 + float 重排（D-8）
# ---------------------------------------------------------------------------


def _pack_int8(vec: list[float], scale: float = QUANT_INT8_SCALE) -> bytes:
    """对称量化：q8 = round(v×scale) clamp 到 [-128,127]。

    scale 由调用方决定（写入时是动态 max|v|/127，查询时用同一次索引记录
    的 scale，见 semantic_quant_meta），本函数本身不关心 scale 怎么来的，
    只保证写入与查询路径用同一个值、同一套量化公式。默认值 QUANT_INT8_SCALE
    仅供未启用动态 scale 时的旧路径/测试兜底。
    """
    return struct.pack(
        "b" * len(vec),
        *(max(-128, min(127, round(v * scale))) for v in vec),
    )


def _pack_bit(vec: list[float]) -> bytes:
    """二值量化：v ≥ 0 记 1。dim 必须 8 的倍数（bge 512 / hash 512 均满足）。"""
    if len(vec) % 8:
        raise ValueError("bit 量化要求维度是 8 的倍数")
    out = bytearray(len(vec) // 8)
    for i, v in enumerate(vec):
        if v >= 0:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _l2_distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b, strict=True)))


class KnnRow(TypedDict):
    """knn_chunks 返回行：distance 恒为 fp32 精确 L2（量化路径经重排覆写）。"""

    chunk_id: int
    distance: float
    revision_id: str
    text_start: int
    text_end: int


def knn_chunks(
    conn: sqlite3.Connection, query_vec: list[float], k: int,
    *, oversample: int | None = None,
) -> list[KnnRow]:
    """取最近 k 个 chunk；返回行含 float L2 ``distance``（与 D-1 语义一致）。

    - float 结构（quant='none'）：vec0 全量 KNN，同 D-1。
    - 量化结构（fp32 表 + int8/bit vec0 粗排）：先在量化表上取
      k×oversample（≤ MAX_RERANK）个候选，再从 fp32 表按主键读回，
      用精确 float L2 重排取前 k —— 粗排只决定候选顺序，返回的
      distance 与 SEMANTIC_MIN_COSINE 判定全部基于 float 重排结果，
      字段语义与 D-1 完全一致。
    """
    if k <= 0:
        return []
    qmeta = quant_meta(conn)
    quant_type = qmeta["quant_type"] if qmeta is not None else "none"
    if quant_type == "none" or not fp32_table_exists(conn):
        return [
            cast(KnnRow, dict(r)) for r in conn.execute(
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
        ]

    if oversample:
        over = oversample
    elif qmeta is not None:
        over = int(qmeta["oversample"])
    else:
        over = DEFAULT_OVERSAMPLE
    coarse_k = min(k * max(1, over), MAX_RERANK, KNN_CAP * MAX_KNN_OVERSAMPLE_RATIO)
    if quant_type == "int8":
        assert qmeta is not None  # quant_type 来自 qmeta，int8 时必非空
        scale = float(qmeta["quant_scale"])
        coarse = conn.execute(
            """
            SELECT v.chunk_id, v.distance, c.revision_id,
                   c.text_start, c.text_end
            FROM revision_chunk_vectors v
            JOIN revision_chunks c ON c.chunk_id = v.chunk_id
            WHERE v.embedding MATCH vec_int8(?) AND v.k = ?
            ORDER BY v.distance
            """,
            (_pack_int8(query_vec, scale), coarse_k),
        ).fetchall()
    else:  # bit
        coarse = conn.execute(
            """
            SELECT v.chunk_id, v.distance, c.revision_id,
                   c.text_start, c.text_end
            FROM revision_chunk_vectors v
            JOIN revision_chunks c ON c.chunk_id = v.chunk_id
            WHERE v.embedding MATCH vec_bit(?) AND v.k = ?
            ORDER BY v.distance
            """,
            (_pack_bit(query_vec), coarse_k),
        ).fetchall()
    if not coarse:
        return []
    # 重排：fp32 表按主键随机读（粗排候选只有几百~两千行）
    ids = [int(r["chunk_id"]) for r in coarse]
    fp: dict[int, list[float]] = {}
    for i in range(0, len(ids), 900):  # SQLite 变量数上限 999
        chunk_ids = ids[i : i + 900]
        rows = conn.execute(
            "SELECT chunk_id, embedding FROM revision_chunk_vectors_fp32"
            " WHERE chunk_id IN (" + ",".join("?" * len(chunk_ids)) + ")",
            chunk_ids,
        ).fetchall()
        for r in rows:
            blob = r["embedding"]
            fp[int(r["chunk_id"])] = list(struct.unpack(f"{len(blob) // 4}f", blob))
    rescored = []
    for r in coarse:
        vec = fp.get(int(r["chunk_id"]))
        if vec is None:
            continue  # fp32 缺行：粗排候选不可信（应只在转换中断时出现）
        rescored.append((_l2_distance(query_vec, vec), r))
    rescored.sort(key=lambda t: (t[0], t[1]["revision_id"], int(t[1]["chunk_id"])))
    # distance 覆写为 fp32 精确 L2（与 float 结构同语义，cosine 换算不变）
    out: list[KnnRow] = []
    for exact, r in rescored[:k]:
        row = cast(KnnRow, dict(r))
        row["distance"] = exact
        out.append(row)
    return out
