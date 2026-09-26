"""D-8 单元/集成测试：向量量化、转换、增量判定、撤回清理。"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from pathlib import Path

import pytest

from personal_brain.history.db import connect
from personal_brain.retrieval.embeddings import HashEmbeddingProvider
from personal_brain.retrieval.semantic_index import (
    QUANT_INT8_SCALE,
    SEMANTIC_MIN_COSINE,
    SemanticConfig,
    _pack_bit,
    _pack_int8,
    convert_vectors_to_quantized,
    distance_to_cosine,
    index_version_of,
    knn_chunks,
    purge_revision_semantic,
    reindex_semantic,
    semantic_pending,
    semantic_status,
    semantic_unavailable_reason,
)


class CountingProvider(HashEmbeddingProvider):
    """统计 embed 调用次数（批数）与文本数——增量判定断言用。"""

    def __init__(self, dimension: int = 256):
        super().__init__(dimension=dimension)
        self.calls = 0
        self.texts = 0

    def embed(self, texts):
        self.calls += 1
        self.texts += len(texts)
        return super().embed(texts)


def _import_corpus(tmp_path: Path) -> sqlite3.Connection:
    """复用 D-1 测试的导入方式：sync_agents 导出格式 → ChatGPTImporter。"""
    import importlib.util
    import zipfile

    spec = importlib.util.spec_from_file_location(
        "sa_d8_test", Path(__file__).resolve().parents[2]
        / "integrations/agent_sync/sync_agents.py"
    )
    sa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sa)

    from personal_brain.importers.importer import ChatGPTImporter
    from personal_brain.policy.labels import label_source

    conn = connect(tmp_path / "brain.sqlite")
    arc = tmp_path / "archives"
    arc.mkdir()
    conv = sa.Conv("conv-1", "D8 测试对话")
    for i in range(6):
        conv.add(1_700_000_000.0 + i * 60, "user" if i % 2 == 0 else "assistant",
                 f"第{i}条关于向量检索与语义召回的讨论内容，包含量化与重排的取舍分析" * 8,
                 f"conv-1:{i}")
    zp = tmp_path / "corpus.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("conversations.json", json.dumps([conv.finish()], ensure_ascii=False))
    ChatGPTImporter(conn, arc).import_archive(zp, "testuser")
    src = conn.execute("SELECT source_id FROM sources LIMIT 1").fetchone()["source_id"]
    label_source(conn, src, scope_labels=("bench",))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 量化/反量化纯函数
# ---------------------------------------------------------------------------


def test_pack_int8_roundtrip_tolerance():
    vec = [1.0, -0.5, 0.0, 0.0431, -0.999, 0.5]
    blob = _pack_int8(vec)
    assert len(blob) == len(vec)
    back = [x / QUANT_INT8_SCALE for x in struct.unpack(f"{len(vec)}b", blob)]
    # 固定 scale 下的量化误差 ≤ 0.5/127 ≈ 0.004
    for a, b in zip(vec, back, strict=True):
        assert abs(a - b) <= 0.5 / QUANT_INT8_SCALE + 1e-9


def test_pack_bit_matches_sign():
    # v>=0 → 该位 1（位序由 sqlite-vec 的 vec_bit/MATCH 解释；
    # 这里只断言符号位映射与确定性——正/零置 1，负置 0）
    blob = _pack_bit([1.0, 0.0, -1.0] + [0.0] * 5)
    same = _pack_bit([1.0, 0.0, -1.0] + [0.0] * 5)
    assert blob == same
    neg = _pack_bit([-1.0, -1.0, -1.0] + [0.0] * 5)
    assert neg != blob
    assert _pack_bit([0.5] * 8) == _pack_bit([1.0] * 8)  # 全正 → 同一 pattern


def test_dynamic_int8_scale_uses_full_int8_range():
    """scale 必须是 127/max_abs（乘数），不是 max_abs/127。

    早前一版算反了：q8 = round(v * scale) 里 scale 用了 max_abs/127
    （一个远小于 1 的数），把所有分量都压到 0 附近，量化后不同 chunk 的
    向量趋同、粗排排序信号完全丢失——用真实 bge-small-zh 模型评测时
    int8 三个 oversample 的 top-10 重合率全部精确为 0.0 才发现。
    这里直接构造一个已知 max_abs 的 fp32 表，断言算出的 scale 与量化后
    最大分量的还原值都落在预期范围，防止这个方向再次反过来。
    """
    import struct

    from personal_brain.retrieval.semantic_index import (
        _fill_quant_from_fp32,
        _tensor_max_abs,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from personal_brain.retrieval.semantic_index import load_vec_extension

    load_vec_extension(conn)
    conn.execute(
        "CREATE TABLE revision_chunk_vectors_fp32 (chunk_id INTEGER PRIMARY KEY,"
        " embedding BLOB NOT NULL)"
    )
    dim = 8
    vecs = {
        1: [0.32, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 全局 max|v| = 0.32
        2: [0.04, -0.01, 0.02, 0.0, 0.0, 0.0, 0.0, 0.0],  # 典型小分量
    }
    for cid, vec in vecs.items():
        conn.execute(
            "INSERT INTO revision_chunk_vectors_fp32 (chunk_id, embedding) VALUES (?, ?)",
            (cid, struct.pack(f"{dim}f", *vec)),
        )
    conn.commit()

    max_abs = _tensor_max_abs(conn)
    assert max_abs == pytest.approx(0.32, abs=1e-6)

    scale = _fill_quant_from_fp32(conn, dim, "int8")
    # 期望 scale ≈ 127/0.32 ≈ 396.9（乘数远大于 1），不是 0.32/127 ≈ 0.0025
    assert scale == pytest.approx(127.0 / 0.32, rel=1e-4)
    assert scale > 1.0  # 反过来的 bug 会让 scale << 1

    blob = conn.execute(
        "SELECT vec_to_json(embedding) AS j FROM revision_chunk_vectors WHERE chunk_id = 1"
    ).fetchone()["j"]
    quantized = json.loads(blob)
    # max_abs 分量应该几乎用满 int8 range（±127），不能被压到 0 附近
    assert abs(quantized[0]) >= 120
    conn.close()


def test_bit_quantized_knn_query_works(tmp_path):
    """bit 量化的查询路径必须能跑（早前少包了 vec_bit(?)，报
    "expected type bit, but a float32 vector was provided"——因为现有
    quantized_env 夹具只测 int8，这条回归一直没被现有测试捕捉到）。
    """
    conn = _import_corpus(tmp_path)
    provider = CountingProvider()
    reindex_semantic(conn, provider)
    convert_vectors_to_quantized(conn, provider.dimension, quant_type="bit", oversample=4)
    qvec = provider.embed(["向量检索 语义召回"])[0]
    rows = knn_chunks(conn, qvec, 5)
    assert rows


def test_index_version_tracks_quantization_params():
    base = SemanticConfig()
    dim = 512
    v_none = index_version_of(base, dim)
    v_int8 = index_version_of(SemanticConfig(quant_type="int8"), dim)
    v_bit = index_version_of(SemanticConfig(quant_type="bit"), dim)
    v_oversample8 = index_version_of(SemanticConfig(quant_type="int8", oversample=8), dim)
    versions = {v_none, v_int8, v_bit, v_oversample8}
    assert len(versions) == 4  # 全部互不相同
    # float 版本与 D-1 完全一致（v6 库升级不触发全量重嵌）
    assert v_none == index_version_of(SemanticConfig(), dim)


# ---------------------------------------------------------------------------
# reindex → convert → knn：量化结构端到端（hash provider，小语料）
# ---------------------------------------------------------------------------


@pytest.fixture()
def quantized_env(tmp_path):
    conn = _import_corpus(tmp_path)
    provider = CountingProvider()
    stats = reindex_semantic(conn, provider)  # float 结构（D-1 兼容）
    assert stats["chunks_new"] > 0
    conv_stats = convert_vectors_to_quantized(
        conn, provider.dimension, quant_type="int8", oversample=4
    )
    return conn, provider, stats, conv_stats


def test_quantized_knn_returns_exact_float_distances(quantized_env):
    """重排后 distance 是 fp32 精确 L2：与该 chunk 在 float 结构下的值一致。"""
    conn, provider, float_stats, conv_stats = quantized_env
    # convert 会改变 index_version；查 quant 结构下的 KNN
    qvec = provider.embed(["向量检索 语义召回"])[0]
    rows = knn_chunks(conn, qvec, 5)
    assert rows
    # 与 float 结构直接计算对比：把 fp32 表里的原向量拿出来手算 L2
    best = rows[0]
    blob = conn.execute(
        "SELECT embedding FROM revision_chunk_vectors_fp32 WHERE chunk_id = ?",
        (best["chunk_id"],),
    ).fetchone()["embedding"]
    vec = list(struct.unpack(f"{len(blob) // 4}f", blob))
    manual = math.sqrt(sum((a - b) ** 2 for a, b in zip(qvec, vec, strict=True)))
    assert abs(float(best["distance"]) - manual) < 1e-9
    # cosine 换算仍是 float L2 语义，MIN_COSINE 判定不漂移
    assert distance_to_cosine(float(best["distance"])) >= SEMANTIC_MIN_COSINE - 1e-9


def test_quantized_topk_overlaps_float_baseline(quantized_env, tmp_path):
    """同一批查询：量化+重排 top-5 ⊆ float top-5 或高度重合（小语料应接近 1.0）。"""
    conn, provider, _s, _c = quantized_env
    # 重建一份 float 结构对照库
    (tmp_path / "float_ref").mkdir()
    conn2 = _import_corpus(tmp_path / "float_ref")
    reindex_semantic(conn2, provider)
    overlaps = []
    for q in ("向量检索", "语义召回", "量化 重排", "归一化 文本"):
        qvec = provider.embed([q])[0]
        a = {int(r["chunk_id"]) for r in knn_chunks(conn, qvec, 5)}
        b = {int(r["chunk_id"]) for r in knn_chunks(conn2, qvec, 5)}
        overlaps.append(len(a & b) / 5)
    assert sum(overlaps) / len(overlaps) >= 0.8  # hash 向量 + 小语料的保守下限
    conn2.close()


def test_quant_unavailable_reason_paths(quantized_env):
    conn, provider, _s, _c = quantized_env
    # 默认 config（quant 跟随索引）→ 可用（model_id 要与建库 provider 一致）
    assert semantic_unavailable_reason(
        conn, SemanticConfig(provider.model_id), provider.dimension
    ) is None
    # 显式要求 bit 而库是 int8 → 版本不匹配
    assert semantic_unavailable_reason(
        conn, SemanticConfig(provider.model_id, quant_type="bit"), provider.dimension
    ) == "index_version_mismatch"


def test_semantic_status_reports_quant_type_and_table_sizes(quantized_env):
    """brain doctor 的数据来源：quant_type/oversample/fp32 与量化表行数、
    估算字节数都要非零——曾经 bit 类型下 quant_vector_rows 被写死成 0
    （COUNT(*) 在 bit 类型 vec0 表上其实工作正常，是防御性代码写错了条件），
    doctor 会一直汇报 bit 量化表大小为 0，误导容量规划。
    """
    conn, provider, _s, _c = quantized_env  # int8，见 quantized_env 夹具
    status = semantic_status(conn)
    assert status["quant_type"] == "int8"
    assert status["oversample"] == 4
    assert status["fp32_rows"] > 0
    assert status["fp32_bytes"] > 0
    assert status["quant_vector_rows"] > 0
    assert status["quant_vector_bytes_est"] > 0

    # 换成 bit：直接从已有 fp32 表重填（convert_vectors_to_quantized 只接受
    # "当前是 float 结构"，这里已经是 int8，用底层 helper 绕开这条前提，
    # 跟 tools/d8_recall_eval.py 里对比 int8/bit 召回时的手法一致）。
    from personal_brain.retrieval.semantic_index import _fill_quant_from_fp32, _set_quant_meta

    scale = _fill_quant_from_fp32(conn, provider.dimension, "bit")
    conn.execute("BEGIN IMMEDIATE")
    _set_quant_meta(conn, "bit", 8, scale)
    conn.commit()
    status_bit = semantic_status(conn)
    assert status_bit["quant_type"] == "bit"
    assert status_bit["quant_vector_rows"] > 0  # 回归点：以前恒为 0
    assert status_bit["quant_vector_bytes_est"] > 0


def test_convert_resumes_after_interrupt(quantized_env, tmp_path):
    """中断续跑：truncate fp32 后重跑 convert，结果与一次跑完一致。"""
    conn, provider, _s, _c = quantized_env
    qvec = provider.embed(["向量检索"])[0]
    full = {int(r["chunk_id"]): float(r["distance"]) for r in knn_chunks(conn, qvec, 5)}
    # 模拟真实中断（Phase C 中途）：量化 vec0 表只写了一半、built_at 为空；
    # fp32 表完整（Phase B 完成后才 DROP float vec0，fp32 不会被部分清空）
    conn.execute("DELETE FROM revision_chunk_vectors WHERE chunk_id % 2 = 0")
    conn.execute("UPDATE semantic_index_meta SET built_at = '' WHERE id = 1")
    conn.commit()
    # 中断中：查询视为不可用
    assert semantic_unavailable_reason(conn, SemanticConfig()) == "index_building"
    convert_vectors_to_quantized(conn, provider.dimension, quant_type="int8", oversample=4)
    after = {int(r["chunk_id"]): float(r["distance"]) for r in knn_chunks(conn, qvec, 5)}
    assert after == full  # 断点续跑与一次跑完结果完全一致


def test_purge_clears_all_three_stores(quantized_env):
    """撤回回归：chunk、fp32、量化向量三处都清掉，语义检索不再返回。"""
    conn, provider, _s, _c = quantized_env
    rid = conn.execute(
        "SELECT DISTINCT revision_id FROM revision_chunks LIMIT 1"
    ).fetchone()["revision_id"]
    chunk_ids = [int(r["chunk_id"]) for r in conn.execute(
        "SELECT chunk_id FROM revision_chunks WHERE revision_id = ?", (rid,)
    )]
    assert chunk_ids
    purge_revision_semantic(conn, [rid])
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) c FROM revision_chunks WHERE revision_id = ?", (rid,)
    ).fetchone()["c"] == 0
    qmarks = ",".join("?" * len(chunk_ids))
    assert conn.execute(
        f"SELECT COUNT(*) c FROM revision_chunk_vectors_fp32 WHERE chunk_id IN ({qmarks})",
        chunk_ids,
    ).fetchone()["c"] == 0
    # 量化 vec0 表：加载扩展后按 chunk_id 查
    from personal_brain.retrieval.semantic_index import load_vec_extension

    load_vec_extension(conn)
    assert conn.execute(
        f"SELECT COUNT(*) c FROM revision_chunk_vectors WHERE chunk_id IN ({qmarks})",
        chunk_ids,
    ).fetchone()["c"] == 0


def test_full_rebuild_on_already_quantized_db_does_not_violate_fk(quantized_env):
    """重跑 --full 时 revision_chunk_vectors_fp32 必须先于 revision_chunks 删。

    revision_chunk_vectors_fp32.chunk_id 外键引用 revision_chunks(chunk_id)，
    history.db.connect() 打开 foreign_keys=ON。全新建库时两张表都是空的，
    删除顺序反了也不会报错；只有在"已经有数据的库上再跑一次 full"（换量化
    参数、手动 --full 重建等）才会触发 FK 约束失败——这条测试专门覆盖后者。
    """
    conn, provider, _s, _c = quantized_env  # 已经是 int8 量化结构、非空
    assert conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"] > 0
    assert (
        conn.execute("SELECT COUNT(*) c FROM revision_chunk_vectors_fp32").fetchone()["c"] > 0
    )
    stats = reindex_semantic(conn, provider, quant_type="none", full=True)
    assert stats["mode"] == "full"
    assert stats["chunks_new"] > 0


# ---------------------------------------------------------------------------
# 增量判定：embed 调用计数；无新数据 0 调用
# ---------------------------------------------------------------------------


def test_incremental_noop_makes_zero_embed_calls(tmp_path):
    conn = _import_corpus(tmp_path)
    provider = CountingProvider()
    reindex_semantic(conn, provider)
    provider.calls = provider.texts = 0
    stats = reindex_semantic(conn, provider)
    assert stats["revisions_embedded"] == 0 and stats["chunks_new"] == 0
    assert provider.calls == 0 and provider.texts == 0
    # pending 判定也给出 skip
    assert semantic_pending(conn, SemanticConfig(provider.model_id))["action"] == "skip"


def test_incremental_only_embeds_changed_revision(tmp_path):
    """event_revisions 正文不可变（D-8 范围 3a）：模拟"编辑一条消息"必须走
    真实的重新导入路径（同 event_id、新 revision_hash → 新 revision_id，
    旧行原样保留，见 test_import_versions.py::test_new_revision_keeps_old），
    不能直接 UPDATE raw_text——那条路径在真实系统里不存在。
    重新导入后只有新增的那个 revision_id 需要 embed；旧 revision_id 的内容
    没变、state 里 index_version 仍匹配，不会重嵌。
    """
    import importlib.util
    import zipfile

    conn = _import_corpus(tmp_path)
    provider = CountingProvider()
    reindex_semantic(conn, provider)
    provider.calls = provider.texts = 0

    before_rids = {
        r["revision_id"] for r in conn.execute("SELECT revision_id FROM event_revisions")
    }

    # 同一份对话，第 0 条消息内容被编辑（stable_id/conversation_id 不变 →
    # 同一 event_id，内容变了 → 新 revision_hash/revision_id，旧行保留）。
    spec = importlib.util.spec_from_file_location(
        "sa_d8_test2", Path(__file__).resolve().parents[2]
        / "integrations/agent_sync/sync_agents.py"
    )
    sa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sa)
    from personal_brain.importers.importer import ChatGPTImporter

    conv = sa.Conv("conv-1", "D8 测试对话")
    for i in range(6):
        text = (
            "编辑后的内容：关于向量检索的讨论有更新" * 8
            if i == 0 else
            f"第{i}条关于向量检索与语义召回的讨论内容，包含量化与重排的取舍分析" * 8
        )
        conv.add(1_700_000_000.0 + i * 60,
                 "user" if i % 2 == 0 else "assistant", text, f"conv-1:{i}")
    zp = tmp_path / "corpus-edit.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("conversations.json", json.dumps([conv.finish()], ensure_ascii=False))
    ChatGPTImporter(conn, tmp_path / "archives").import_archive(zp, "testuser")
    conn.commit()

    after_rids = {
        r["revision_id"] for r in conn.execute("SELECT revision_id FROM event_revisions")
    }
    new_rids = after_rids - before_rids
    assert len(new_rids) == 1, f"编辑一条消息应该只多出一个新 revision_id，实际 {new_rids}"
    assert before_rids <= after_rids  # 旧 revision 行原样保留，一个没少

    stats = reindex_semantic(conn, provider)
    assert stats["revisions_embedded"] == 1  # 只有新 revision_id 需要重嵌
    assert provider.texts >= 1
    assert stats["revisions_scanned"] == len(after_rids)  # 新旧 revision 都仍可索引


def test_semantic_pending_reports_convert_for_quant_mismatch(tmp_path):
    conn = _import_corpus(tmp_path)
    provider = CountingProvider()
    reindex_semantic(conn, provider)  # float 结构
    p = semantic_pending(conn, SemanticConfig(provider.model_id))
    assert p["action"] == "skip"
    convert_vectors_to_quantized(conn, provider.dimension, quant_type="int8")
    # config 不带 quant（跟随索引）→ skip；version 已带量化后缀
    p = semantic_pending(conn, SemanticConfig(provider.model_id))
    assert p["action"] == "skip"
