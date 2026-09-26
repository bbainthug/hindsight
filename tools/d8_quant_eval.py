"""D-8 范围 4 + 交付测量：convert、量化后 hybrid 拆解、召回对比、reindex 耗时。"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PERF = Path("/tmp/d8-perf")
DB = PERF / "brain.sqlite"
DIM = 512


def fp32_baseline() -> None:
    """从 fp32 表用 numpy 重算 float 全量 KNN top-10 基线（50 查询）。

    convert 之后 float vec0 已不在，但 fp32 行表就是全部 float 原值：
    全量暴力 L2 与 float vec0 KNN 的候选集合一致（同精度同算法）。
    """
    import numpy as np

    provider = get_provider()
    words = load_query_words()
    combos = [
        f"{words[i % 10]} {words[(i * 3 + 1) % 10]} {words[(i // 10) % 10]}的讨论"
        for i in range(50)
    ]
    conn = open_conn()
    rows = conn.execute(
        "SELECT chunk_id, embedding FROM revision_chunk_vectors_fp32 ORDER BY chunk_id"
    ).fetchall()
    ids = np.array([r["chunk_id"] for r in rows], dtype=np.int64)
    mat = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32)
    mat = mat.reshape(len(rows), -1)
    print(f"fp32 矩阵 {mat.shape}")
    out = {}
    for q in combos:
        qvec = np.asarray(provider.embed([q])[0], dtype=np.float32)
        d = np.sqrt(((mat - qvec) ** 2).sum(axis=1))
        top = ids[np.argsort(d, kind="stable")[:10]]
        out[q] = sorted(int(x) for x in top)
    (PERF / "recall_baseline.json").write_text(json.dumps(out))
    conn.close()
    print(f"fp32 基线（numpy 全量 L2）完成：{len(out)} 查询")


def connect_write() -> sqlite3.Connection:
    from personal_brain.retrieval.semantic_index import load_vec_extension

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn)
    return conn


def open_conn() -> sqlite3.Connection:
    from personal_brain.retrieval.semantic_index import load_vec_extension

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn)
    return conn


def get_provider(dim: int = DIM):
    from personal_brain.retrieval.embeddings import HashEmbeddingProvider

    return HashEmbeddingProvider(dimension=dim)


def load_query_words() -> list[str]:
    return ["求职", "远程", "简历", "deploy", "缓存", "告警", "权限", "归档",
            "向量", "流水线"]


from personal_brain.retrieval.semantic_index import knn_chunks  # noqa: E402


def measure_hybrid(label: str, cold: bool) -> dict:
    """拆解一次 hybrid 的语义路径：embed/knn/授权过滤/组装。"""
    from personal_brain.retrieval.search import SearchFilters, _filter_sql
    from personal_brain.retrieval.semantic_index import (
        SemanticConfig,
        knn_chunks,
        semantic_unavailable_reason,
    )

    provider = get_provider()
    purge = PERF / "purge.bin"
    words = load_query_words()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from personal_brain.retrieval.semantic_index import load_vec_extension

    load_vec_extension(conn)
    conn.execute(f"PRAGMA cache_size = -{'2048' if cold else '262144'}")
    runs = []
    for q in words * 3:
        if cold and purge.exists():
            with open(purge, "rb") as f:
                while f.read(1 << 20):
                    pass
        parts: dict[str, float] = {}
        t0 = time.perf_counter()
        reason = semantic_unavailable_reason(
            conn, SemanticConfig(provider.model_id), DIM)
        parts["status"] = (time.perf_counter() - t0) * 1000
        assert reason is None, reason
        t0 = time.perf_counter()
        qvec = provider.embed([q])[0]
        parts["embed"] = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        knn = knn_chunks(conn, qvec, 200)
        parts["knn_total"] = (time.perf_counter() - t0) * 1000
        ordered = sorted(knn, key=lambda r: (float(r["distance"]), r["revision_id"],
    int(r["chunk_id"])))
        cand = []
        for kr in ordered:
            if kr["revision_id"] not in cand:
                cand.append(kr["revision_id"])
        params: list = []
        sql = _filter_sql(SearchFilters(), params)
        sql += " AND r.revision_id IN (" + ",".join("?" for _ in cand) + ")"
        t0 = time.perf_counter()
        rows = conn.execute(sql, [*params, *cand]).fetchall()
        parts["auth_filter"] = (time.perf_counter() - t0) * 1000
        parts["candidates"] = len(rows)
        runs.append(parts)
    conn.close()
    agg: dict[str, dict] = {}
    for key in runs[0]:
        vals = [r[key] for r in runs]
        agg[key] = ({"p50": round(statistics.median(vals), 1),
                     "p95": round(sorted(vals)[max(0, int(len(vals) * 0.95) - 1)], 1)}
                    if isinstance(vals[0], float) else round(statistics.median(vals), 1))
    print(f"[{label}] {json.dumps(agg, ensure_ascii=False)}")
    return agg


def recall_compare() -> dict:
    """范围 4：50 个固定查询，float 全量 KNN vs 量化+重排的 top-10 重合率。"""


    provider = get_provider()
    words = load_query_words()
    # 50 个查询 = 10 词 × 5 组合（合成语料，同词不同组合仍是不同向量）
    # 三词组合保证 50 个查询互不相同（两词组合的模 10 循环只有 10 个唯一对）
    combos = [
        f"{words[i % 10]} {words[(i * 3 + 1) % 10]} {words[(i // 10) % 10]}的讨论"
        for i in range(50)
    ]
    conn = open_conn()

    def top10(q: str, oversample: int | None) -> set[int]:
        qvec = provider.embed([q])[0]
        rows = knn_chunks(conn, qvec, 10, oversample=oversample)
        return {int(r["chunk_id"]) for r in rows}

    # float 基线：quant meta 临时视为 none —— 直接查 vec0 表（此时已是量化表）
    # 所以 float 基线在 convert 之前采集（main() 里顺序保证），这里只做量化侧
    results: dict[str, list[float]] = {}
    saved = json.loads((PERF / "recall_baseline.json").read_text())
    for name, os_ in (("int8_default", None), ("int8_x8", 8), ("int8_x1", 1)):
        overlaps = []
        for q in combos:
            base = set(saved[q])
            got = top10(q, os_)
            overlaps.append(len(base & got) / 10)
        results[name] = {
            "mean": round(statistics.mean(overlaps), 4),
            "min": round(min(overlaps), 4),
        }
        print(f"[recall {name}] mean={results[name]['mean']} min={results[name]['min']}")
    conn.close()
    return results


RECALL_BASELINE: dict[str, set[int]] = {}


def collect_baseline() -> None:
    """convert 之前：float 全量 KNN top-10 基线。"""


    provider = get_provider()
    conn = open_conn()
    words = load_query_words()
    combos = [
        f"{words[i % 10]} {words[(i * 3 + 1) % 10]} {words[(i // 10) % 10]}的讨论"
        for i in range(50)
    ]
    for q in combos:
        qvec = provider.embed([q])[0]
        rows = knn_chunks(conn, qvec, 10)
        RECALL_BASELINE[q] = {int(r["chunk_id"]) for r in rows}
    conn.close()
    (PERF / "recall_baseline.json").write_text(
        json.dumps({q: sorted(ids) for q, ids in RECALL_BASELINE.items()})
    )
    print(f"float 基线采集完成：{len(RECALL_BASELINE)} 查询 → recall_baseline.json")


def bit_compare() -> None:
    """bit 量化召回对比（convert 到 bit 后再测一轮）。"""
    pass  # main 流程中单独调用 convert(bit) 后复用 recall_compare


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    from personal_brain.retrieval.semantic_index import (
        convert_vectors_to_quantized,
        reindex_semantic,
    )

    if step in ("baseline", "all"):
        collect_baseline()
    if step in ("convert", "all"):
        conn = open_conn()
        t0 = time.perf_counter()
        stats = convert_vectors_to_quantized(conn, DIM, quant_type="int8", oversample=4)
        stats["wall_s"] = round(time.perf_counter() - t0, 1)
        print("convert:", json.dumps(stats, ensure_ascii=False))
        conn.close()
    if step in ("measure", "all"):
        measure_hybrid("quantized hot", cold=False)
        measure_hybrid("quantized cold", cold=True)
    if step in ("recall", "all"):
        recall_compare()
    if step in ("bit", "all"):
        conn = open_conn()
        stats = convert_vectors_to_quantized(conn, DIM, quant_type="bit", oversample=8)
        print("convert(bit):", json.dumps(stats, ensure_ascii=False))
        conn.close()
        recall_compare()
    if step in ("fp32baseline", "all"):
        fp32_baseline()
    if step in ("reindex", "all"):
        provider = get_provider()
        conn = open_conn()
        from personal_brain.retrieval.semantic_index import quant_meta as _qm

        qm = _qm(conn)
        conn.close()
        conn = connect_write()
        t0 = time.perf_counter()
        stats = reindex_semantic(conn, provider,
                                 quant_type=qm["quant_type"] if qm else "none",
                                 oversample=int(qm["oversample"]) if qm else 4)
        print("reindex no-op:", json.dumps({k: stats[k] for k in
                                            ("mode", "revisions_scanned", "revisions_embedded",
                                             "elapsed_ms")}, ensure_ascii=False),
              f"wall={time.perf_counter() - t0:.1f}s")
        conn.close()


if __name__ == "__main__":
    main()
