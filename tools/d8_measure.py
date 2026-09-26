"""D-8 范围 1：测量脚本。

造贴近 VM 规模的合成语义索引库（10 万 revision × ~1200 字 ≈ 22 万 chunk ×
512 维 float32 ≈ 457 MB），然后把一次 hybrid 查询拆开计时：
query embed / knn_chunks / 授权过滤 / 候选组装 / 状态检查，
分别在“热态”与“向量表放不进页缓存（冷态模拟）”两种条件下。
另测增量 reindex 无新数据时的耗时（证实/证伪 2 分钟推测）。
"""
from __future__ import annotations

import json
import os
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
N_CONV = 2500
MSGS = 40
TEXT_CHARS = 1200  # ~2.2 chunks/revision @600chars → 目标 ≈22 万 chunk

TOPIC_WORDS = ["deploy", "缓存", "索引", "发布", "评审", "向量", "预算", "登录",
               "权限", "备份", "时区", "流水线", "语义", "召回", "快照", "迁移",
               "凭据", "审计", "告警", "限流", "求职", "远程", "简历", "归档"]
SENT_TMPL = [
    "关于{w}的第{n}段讨论，我们分析了当前方案在高峰期的表现与回滚路径。",
    "{w}相关的工单在评审中被指出缺少灰度开关，需要补一条降级说明再合入。",
    "把{w}的结论同步给群里，附上这次压测的曲线截图与阈值配置，方便下周复盘。",
    "如果{w}在夜间批量窗口再次触发告警，就按预案切到备用队列并通知值班同学。",
]


def build_corpus() -> None:
    import importlib.util
    import zipfile

    sys.path.insert(0, str(ROOT / "integrations/agent_sync"))
    spec = importlib.util.spec_from_file_location(
        "sa_d8", ROOT / "integrations/agent_sync/sync_agents.py"
    )
    sa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sa)

    from personal_brain.history.db import connect
    from personal_brain.importers.importer import ChatGPTImporter
    from personal_brain.policy.labels import label_source

    PERF.mkdir(exist_ok=True)
    if DB.exists():
        print(f"复用现有库 {DB}")
        return
    conn = connect(DB)
    arc = PERF / "archives"
    arc.mkdir(exist_ok=True)
    importer = ChatGPTImporter(conn, arc)
    t0 = time.perf_counter()
    batch = []
    for c in range(N_CONV):
        conv = sa.Conv(f"d8-conv-{c:05d}", f"D8 合成对话 {c}")
        base = 1_700_000_000 + (c / N_CONV) * 900 * 86400
        for m in range(MSGS):
            w = TOPIC_WORDS[(c * 7 + m * 3) % len(TOPIC_WORDS)]
            # ~1200 字：句子重复到目标长度（合成数据，无真实内容）
            parts = []
            n = 0
            while sum(len(p) for p in parts) < TEXT_CHARS:
                parts.append(SENT_TMPL[n % len(SENT_TMPL)].format(w=w, n=n))
                n += 1
            conv.add(base + m * 90, "user" if m % 2 == 0 else "assistant",
                     "".join(parts), f"{c}:{m}")
        batch.append(conv.finish())
        if len(batch) == 500:
            zp = PERF / f"batch-{c:05d}.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("conversations.json", json.dumps(batch, ensure_ascii=False))
            importer.import_archive(zp, "perfuser")
            os.remove(zp)
            batch = []
    if batch:
        zp = PERF / "batch-last.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("conversations.json", json.dumps(batch, ensure_ascii=False))
        importer.import_archive(zp, "perfuser")
        os.remove(zp)
    src = conn.execute("SELECT source_id FROM sources LIMIT 1").fetchone()["source_id"]
    label_source(conn, src, scope_labels=("bench",))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    print(f"导入完成 {n} 事件，耗时 {time.perf_counter() - t0:.0f}s", flush=True)
    conn.close()


def build_index() -> None:
    from personal_brain.history.db import connect
    from personal_brain.retrieval.embeddings import HashEmbeddingProvider
    from personal_brain.retrieval.semantic_index import reindex_semantic

    conn = connect(DB)
    provider = HashEmbeddingProvider(dimension=DIM)
    t0 = time.perf_counter()
    stats = reindex_semantic(conn, provider)
    print(f"索引构建 {json.dumps(stats, ensure_ascii=False)} 耗时 {time.perf_counter() - t0:.0f}s")
    conn.close()


def build_purge_file() -> Path:
    """1.5 GB 垃圾文件：查询前整读一遍，把 OS 页缓存里的向量表挤出去。"""
    p = PERF / "purge.bin"
    if not p.exists() or p.stat().st_size != 1_500_000_000:
        with open(p, "wb") as f:
            for _ in range(96):
                f.write(os.urandom(16_000_000))
    return p


def timed(fn, *args, **kw):
    t0 = time.perf_counter()
    r = fn(*args, **kw)
    return (time.perf_counter() - t0) * 1000, r


def measure() -> dict:
    from personal_brain.retrieval.embeddings import HashEmbeddingProvider
    from personal_brain.retrieval.search import SearchFilters, _filter_sql
    from personal_brain.retrieval.semantic_index import (
        SemanticConfig,
        knn_chunks,
        semantic_unavailable_reason,
    )

    provider = HashEmbeddingProvider(dimension=DIM)
    purge = build_purge_file()
    query_words = ["求职", "远程", "简历", "deploy", "缓存", "告警", "权限", "归档"]

    def fresh_conn(cache_kb: int) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA cache_size = -{cache_kb}")
        return conn

    def one_hybrid(cache_kb: int, cold: bool) -> dict:
        conn = fresh_conn(cache_kb)
        q = query_words[int(time.time()) % len(query_words)]
        if cold:
            # 挤页缓存：整读 purge 文件
            with open(purge, "rb") as f:
                while f.read(1 << 20):
                    pass
        parts: dict[str, float] = {}
        t, _ = timed(semantic_unavailable_reason, conn, SemanticConfig(), 512)
        parts["status_check"] = t
        t, qvec = timed(provider.embed, [q])
        parts["query_embed"] = t
        k = 200
        t, knn = timed(knn_chunks, conn, qvec[0], k)
        parts["knn_chunks"] = t
        # 授权过滤（与查询路径同一套 _filter_sql）
        ordered = sorted(knn, key=lambda r: (float(r["distance"]), r["revision_id"],
    int(r["chunk_id"])))
        cand = []
        for kr in ordered:
            if kr["revision_id"] not in cand:
                cand.append(kr["revision_id"])
        params: list = []
        sql = _filter_sql(SearchFilters(), params)
        sql += " AND r.revision_id IN (" + ",".join("?" for _ in cand) + ")"
        t, srows = timed(lambda: conn.execute(sql, [*params, *cand]).fetchall())
        parts["auth_filter"] = t
        parts["n_knn"] = len(knn)
        parts["n_after_filter"] = len(srows)
        # 重排成本对照：从 float 表按主键读 k 条
        ids = [int(r["chunk_id"]) for r in ordered[:100]]
        t, _ = timed(lambda: conn.execute(
            "SELECT chunk_id, embedding FROM revision_chunk_vectors WHERE chunk_id IN ("
            + ",".join("?" * len(ids)) + ")", ids).fetchall())
        parts["float_reshuffle_read_100"] = t
        conn.close()
        return parts

    results: dict = {}
    # --- 热态（大 cache，重复 10 次取中位）---
    for _ in range(3):
        one_hybrid(262144, cold=False)  # 256MB cache 预热
    hot_runs = [one_hybrid(262144, cold=False) for _ in range(10)]
    # --- 冷态模拟（2MB cache + 挤页缓存）---
    cold_runs = [one_hybrid(2048, cold=True) for _ in range(5)]

    def agg(runs: list[dict]) -> dict:
        out = {}
        for key in runs[0]:
            vals = [r[key] for r in runs]
            if isinstance(vals[0], int) and key.startswith("n_"):
                out[key] = int(statistics.median(vals))
            else:
                out[key] = {"p50": round(statistics.median(vals), 1),
                            "p95": round(sorted(vals)[int(len(vals) * 0.95)], 1)}
        return out

    results["hot"] = agg(hot_runs)
    results["cold"] = agg(cold_runs)

    # --- 向量表与库大小 ---
    conn = fresh_conn(65536)
    results["db_bytes"] = DB.stat().st_size
    results["vec_pages"] = int(conn.execute(
        "SELECT COUNT(*) c FROM pragma_page_count").fetchone()["c"])
    conn.close()

    # --- 增量 reindex 无新数据耗时（现状实现）---
    from personal_brain.history.db import connect
    from personal_brain.retrieval.semantic_index import reindex_semantic

    conn = connect(DB)
    t0 = time.perf_counter()
    stats = reindex_semantic(conn, provider)
    results["reindex_noop"] = {"elapsed_ms": stats["elapsed_ms"],
                               "wall_s": round(time.perf_counter() - t0, 1),
                               "revisions_scanned": stats["revisions_scanned"]}
    conn.close()
    return results


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("corpus", "all"):
        build_corpus()
    if step in ("index", "all"):
        build_index()
    if step in ("measure", "all"):
        out = measure()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        (PERF / "measure.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
