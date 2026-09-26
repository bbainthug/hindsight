"""D-8 范围 2/4：真实模型（bge-small-zh）召回对比。

HashEmbeddingProvider 没有语义结构（近邻距离接近均匀），不能用来判断量化
是否伤害召回——用真实 fastembed 模型，在本机能承受的小合成库
（目标 ~2.5 万 chunk）上，固定 50 个互不重复的查询，比较：
  float 全量 KNN top-10  vs  量化（int8 动态 scale / bit）+ fp32 重排 top-10。

用法：
  uv run python tools/d8_recall_eval.py all        # 建库 + 建 float 索引 + 全部对比
  uv run python tools/d8_recall_eval.py corpus      # 只建库（已存在则跳过，复用）
  uv run python tools/d8_recall_eval.py baseline    # 只采 float 基线（需先有 corpus+index）
  uv run python tools/d8_recall_eval.py compare     # int8/bit 各 oversample 对比（读 baseline）

产物都在 /tmp/d8-perf/recall/ 下：recall.sqlite、baseline.json、report.json。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("/tmp/d8-perf/recall")
DB = OUT / "recall.sqlite"

# 语料规模：DEFAULT_CHUNK_CHARS=600/overlap=100 时，TEXT_CHARS=1200 的正文
# 约切 2.2 个 chunk；100 对话 × 40 消息 = 4000 revision ≈ 8800 chunk。
# 刻意比 d8_measure.py 的语料小得多：真实模型（bge-small-zh）在本机的
# embed 吞吐实测 ~4.7 chunk/s（受 EMBED_MAX_CHUNKS_PER_CALL=8 的小批量
# 调用开销主导，与 VM 上"22 万 chunk 全量 embed ~7 小时"的量级一致），
# 3 万 chunk 会跑一个多小时；8800 chunk 约 30 分钟，本机能承受。
N_CONV = 100
MSGS = 40
TEXT_CHARS = 1200

TOPIC_WORDS = ["deploy", "缓存", "索引", "发布", "评审", "向量", "预算", "登录",
               "权限", "备份", "时区", "流水线", "语义", "召回", "快照", "迁移",
               "凭据", "审计", "告警", "限流", "求职", "远程", "简历", "归档"]
# 每句都带 (c, m)：保证 4000 条消息文本两两不同——早期版本的模板不含消息
# 自身的区分信息，只随 24 个 TOPIC_WORDS 循环，产出的合成库里每种话题的
# 文本逐字相同（500 条一样），float 与量化两边的 top-10 都在一大片"距离
# 恰好为 0"的完全重复向量里各自随机取整，重合率因此失真为 0，跟量化质量
# 无关（发现于第一次真实模型评测：int8 三个 oversample 全部 mean=0.0）。
SENT_TMPL = [
    "会话{c}第{m}条关于{w}的第{n}段讨论，我们分析了当前方案在高峰期的表现与回滚路径。",
    "{w}相关的工单（会话{c}消息{m}）在评审中被指出缺少灰度开关，需要补一条降级说明再合入。",
    "把{w}的结论同步给群里（会话{c}消息{m}），附上这次压测的曲线截图与阈值配置，方便下周复盘。",
    "如果{w}在夜间批量窗口再次触发告警（会话{c}消息{m}），就按预案切到备用队列并通知值班同学。",
]

QUERY_WORDS = ["求职", "远程", "简历", "deploy", "缓存", "告警", "权限", "归档",
               "向量", "流水线"]


def load_queries() -> list[str]:
    """50 个互不重复的查询：三词组合 + 序号，避免任何循环节。"""
    n = len(QUERY_WORDS)
    combos = [
        f"{QUERY_WORDS[i % n]}和{QUERY_WORDS[(i * 7 + 3) % n]}在{QUERY_WORDS[(i * 11 + 5) % n]}"
        f"场景下的第{i}轮讨论"
        for i in range(50)
    ]
    assert len(set(combos)) == 50, f"查询组合有重复，只有 {len(set(combos))} 个唯一值"
    return combos


def build_corpus() -> None:
    import importlib.util
    import zipfile

    sys.path.insert(0, str(ROOT / "integrations/agent_sync"))
    spec = importlib.util.spec_from_file_location(
        "sa_d8r", ROOT / "integrations/agent_sync/sync_agents.py"
    )
    sa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sa)

    from personal_brain.history.db import connect
    from personal_brain.importers.importer import ChatGPTImporter
    from personal_brain.policy.labels import label_source

    OUT.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        print(f"复用现有库 {DB}")
        return
    conn = connect(DB)
    arc = OUT / "archives"
    arc.mkdir(exist_ok=True)
    importer = ChatGPTImporter(conn, arc)
    t0 = time.perf_counter()
    batch = []
    for c in range(N_CONV):
        conv = sa.Conv(f"d8r-conv-{c:05d}", f"D8 recall 合成对话 {c}")
        base = 1_700_000_000 + (c / N_CONV) * 900 * 86400
        for m in range(MSGS):
            w = TOPIC_WORDS[(c * 7 + m * 3) % len(TOPIC_WORDS)]
            parts = []
            n = 0
            while sum(len(p) for p in parts) < TEXT_CHARS:
                parts.append(SENT_TMPL[n % len(SENT_TMPL)].format(w=w, n=n, c=c, m=m))
                n += 1
            conv.add(base + m * 90, "user" if m % 2 == 0 else "assistant",
                     "".join(parts), f"{c}:{m}")
        batch.append(conv.finish())
        if len(batch) == 500:
            zp = OUT / f"batch-{c:05d}.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("conversations.json", json.dumps(batch, ensure_ascii=False))
            importer.import_archive(zp, "recalluser")
            zp.unlink()
            batch = []
    if batch:
        zp = OUT / "batch-last.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("conversations.json", json.dumps(batch, ensure_ascii=False))
        importer.import_archive(zp, "recalluser")
        zp.unlink()
    src = conn.execute("SELECT source_id FROM sources LIMIT 1").fetchone()["source_id"]
    label_source(conn, src, scope_labels=("bench",))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    print(f"导入完成 {n} 事件，耗时 {time.perf_counter() - t0:.0f}s", flush=True)
    conn.close()


def open_conn(readonly: bool = False) -> sqlite3.Connection:
    from personal_brain.retrieval.semantic_index import load_vec_extension

    conn = (
        sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        if readonly else sqlite3.connect(DB)
    )
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn)
    return conn


def get_provider():
    from personal_brain.retrieval.embeddings import FastEmbedProvider

    return FastEmbedProvider()


def build_int8_index() -> None:
    """一次 embed 建 int8 索引：quant_type='int8' 时 reindex_semantic 会把
    float 原值写进 fp32 行表、量化 vec0 用动态 scale 填充——float 基线不需要
    再单独建一份 float vec0，直接从 fp32 表用 numpy 精确重算（见
    collect_baseline），bit 对比也复用这份 fp32（见 fill_quant）。"""
    from personal_brain.history.db import connect
    from personal_brain.retrieval.semantic_index import reindex_semantic

    conn = connect(DB)
    provider = get_provider()
    t0 = time.perf_counter()
    stats = reindex_semantic(conn, provider, quant_type="int8", oversample=4)
    print(f"int8 索引构建（含 embed） {json.dumps(stats, ensure_ascii=False)}"
          f" 耗时 {time.perf_counter() - t0:.0f}s")
    conn.close()


def collect_baseline() -> None:
    """float 全量 KNN 基线：从完整的 fp32 表用 numpy 精确 L2 重算 top-10
    （与真正的 float vec0 全量 KNN 同精度同候选集合，见
    d8_quant_eval.py::fp32_baseline 的同一手法）。"""
    import numpy as np

    provider = get_provider()
    conn = open_conn(readonly=True)
    rows = conn.execute(
        "SELECT chunk_id, embedding FROM revision_chunk_vectors_fp32 ORDER BY chunk_id"
    ).fetchall()
    ids = np.array([r["chunk_id"] for r in rows], dtype=np.int64)
    mat = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32)
    mat = mat.reshape(len(rows), -1)
    conn.close()
    print(f"fp32 矩阵 {mat.shape}")

    queries = load_queries()
    out: dict[str, list[int]] = {}
    t0 = time.perf_counter()
    for q in queries:
        qvec = np.asarray(provider.embed([q])[0], dtype=np.float32)
        d = np.sqrt(((mat - qvec) ** 2).sum(axis=1))
        top = ids[np.argsort(d, kind="stable")[:10]]
        out[q] = sorted(int(x) for x in top)
    (OUT / "baseline.json").write_text(json.dumps(out, ensure_ascii=False))
    print(f"float 基线采集完成：{len(out)} 查询，耗时 {time.perf_counter() - t0:.1f}s")


def fill_quant(quant_type: str) -> tuple[float, float]:
    """直接从 fp32 表流式填充量化 vec0（跳过 convert_vectors_to_quantized
    要求"当前是 float 结构"这条前提）——int8 与 bit 两次对比共用同一份
    fp32 表，不需要在两者之间反复重建 float 结构（那会强制重新 embed，
    revision_chunk_vectors_fp32 在 full 重建时会被清空）。

    返回 (fill scale, wall_s)；scale 供 int8 写 quant_meta 用，bit 忽略。
    """
    from personal_brain.retrieval.semantic_index import (
        _fill_quant_from_fp32,
        _set_quant_meta,
    )

    provider = get_provider()
    conn = open_conn()
    t0 = time.perf_counter()
    scale = _fill_quant_from_fp32(conn, provider.dimension, quant_type)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _set_quant_meta(conn, quant_type, oversample=4, scale=scale)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    wall_s = time.perf_counter() - t0
    conn.close()
    print(f"fill_quant({quant_type}) scale={scale} wall={wall_s:.1f}s")
    return scale, wall_s


def compare(quant_type: str, oversamples: list[int]) -> dict:
    from personal_brain.retrieval.semantic_index import knn_chunks

    provider = get_provider()
    baseline = json.loads((OUT / "baseline.json").read_text())
    queries = load_queries()
    assert set(queries) == set(baseline), "查询集与基线不一致"

    conn = open_conn(readonly=True)
    out: dict[str, dict] = {}
    for os_ in oversamples:
        overlaps = []
        for q in queries:
            qvec = provider.embed([q])[0]
            rows = knn_chunks(conn, qvec, 10, oversample=os_)
            got = {int(r["chunk_id"]) for r in rows}
            base = set(baseline[q])
            overlaps.append(len(base & got) / 10)
        out[f"oversample={os_}"] = {
            "mean": round(statistics.mean(overlaps), 4),
            "min": round(min(overlaps), 4),
        }
        print(f"[{quant_type} oversample={os_}] "
              f"mean={out[f'oversample={os_}']['mean']} min={out[f'oversample={os_}']['min']}")
    conn.close()
    return out


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    report: dict = {}
    if step in ("corpus", "all"):
        build_corpus()
    if step in ("index", "all"):
        build_int8_index()
    if step in ("baseline", "all"):
        collect_baseline()
    if step in ("compare", "all"):
        # 索引已经是 int8（build_int8_index 建的），直接对比，不用再 fill 一次
        report["int8"] = {"overlap": compare("int8", [1, 4, 8])}
        _scale, wall_s = fill_quant("bit")
        report["bit"] = {"fill_wall_s": round(wall_s, 1), "overlap": compare("bit", [4, 8])}
        (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"写入 {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
