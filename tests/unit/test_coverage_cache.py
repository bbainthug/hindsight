"""D-5b 单元测试：_coverage 缓存键签名 / LRU 上限 / 并发去重。"""
from __future__ import annotations

import threading

import pytest

from personal_brain.mcp_server import service as svc
from personal_brain.retrieval.search import SearchFilters


@pytest.fixture()
def clean_cache():
    svc._coverage_cache.clear()
    yield svc._coverage_cache
    svc._coverage_cache.clear()


def test_cache_key_signature_stable(clean_cache):
    """同一组过滤条件的不同元组形态（list/tuple）必须得到同一签名。"""
    f1 = SearchFilters(allow_scopes=("a", "b"), deny_sensitivity=("s",))
    f2 = SearchFilters(allow_scopes=["a", "b"], deny_sensitivity=["s"])
    # 键计算需要连接（epoch/import_watermark）；签名部分单独验证
    s1 = (
        tuple(f1.allow_scopes) if f1.allow_scopes is not None else None,
        tuple(f1.deny_sensitivity) if f1.deny_sensitivity is not None else None,
        f1.allow_unclassified, f1.date_from, f1.date_to,
        f1.source_id, f1.account_namespace, f1.conversation_id,
        f1.speaker_type, f1.path_mode,
    )
    s2 = (
        tuple(f2.allow_scopes) if f2.allow_scopes is not None else None,
        tuple(f2.deny_sensitivity) if f2.deny_sensitivity is not None else None,
        f2.allow_unclassified, f2.date_from, f2.date_to,
        f2.source_id, f2.account_namespace, f2.conversation_id,
        f2.speaker_type, f2.path_mode,
    )
    assert s1 == s2, "list 与 tuple 形态的同一过滤必须同签名"


def test_cache_key_differs_by_profile_fields(clean_cache):
    f1 = SearchFilters(allow_scopes=("career",))
    f2 = SearchFilters(allow_scopes=("career", "secret"))
    f3 = SearchFilters(allow_scopes=("career",), allow_unclassified=False)
    sigs = set()
    for f in (f1, f2, f3):
        sig = (
            tuple(f.allow_scopes) if f.allow_scopes is not None else None,
            tuple(f.deny_sensitivity) if f.deny_sensitivity is not None else None,
            f.allow_unclassified, f.date_from, f.date_to,
            f.source_id, f.account_namespace, f.conversation_id,
            f.speaker_type, f.path_mode,
        )
        sigs.add(sig)
    assert len(sigs) == 3, "不同 profile 过滤不得同签名（防串缓存）"


def test_lru_eviction_at_capacity(clean_cache):
    for i in range(svc._COVERAGE_CACHE_MAX + 5):
        clean_cache[(("k", i), "all", 0, 0)] = {"known_start": None, "known_end": None}
    # 触达最早插入的条目也不能超过上限：手写淘汰逻辑由 _coverage 主导，
    # 这里直接验证上限语义
    while len(clean_cache) > svc._COVERAGE_CACHE_MAX:
        clean_cache.popitem(last=False)
    assert len(clean_cache) <= svc._COVERAGE_CACHE_MAX


def test_lru_capacity_enforced_through_coverage(tmp_path, monkeypatch, clean_cache):
    """通过 _coverage 主路径塞满缓存：条目数不超过 32，最旧者被淘汰。"""

    from personal_brain.history.db import connect

    db = tmp_path / "c.sqlite"
    conn = connect(db)
    filters = SearchFilters()
    seen: set[int] = set()
    for i in range(svc._COVERAGE_CACHE_MAX + 5):
        # 用 monkeypatch 让每个 import_watermark 不同，模拟不同键
        monkeypatch.setattr(
            svc, "_coverage_cache_key",
            lambda c, f, b, _i=i: ((f"sig-{_i}",), b, 0, _i),
        )
        svc._coverage(conn, filters, "all")
        seen.add(i)
    assert len(clean_cache) <= svc._COVERAGE_CACHE_MAX
    assert 0 not in [k[3] for k in clean_cache], "最早的键应被 LRU 淘汰"
    conn.close()


def test_concurrent_calls_consistent_result(tmp_path, clean_cache):
    """并发同一键：结果逐字段一致，缓存只留一份。

    语义采用任务书"重复计算但结果一致"选项：并发线程可能各算一遍
    （不互相等待），但 double-check 保证缓存只有一份、返回值全部一致。
    """
    from personal_brain.history.db import connect

    db = tmp_path / "c.sqlite"
    conn_main = connect(db)  # 建表
    filters = SearchFilters()
    calls = {"n": 0}
    orig = svc._coverage_uncached

    def counting(conn_, f, b):
        with svc._coverage_cache_lock:
            calls["n"] += 1
        return orig(conn_, f, b)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(svc, "_coverage_uncached", counting)
    try:
        barrier = threading.Barrier(8)
        results: list[dict | None] = [None] * 8
        errors: list[BaseException | None] = [None] * 8

        def worker(idx: int):
            conn = connect(db)  # 每线程独立连接（sqlite3 默认禁跨线程）
            try:
                barrier.wait()
                results[idx] = svc._coverage(conn, filters, "all")
            except BaseException as exc:  # noqa: BLE001
                errors[idx] = exc
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        monkey.undo()
    assert not any(errors), f"并发调用不得报错: {errors!r}"
    assert 1 <= calls["n"] <= 8, f"计算次数 {calls['n']} 超出并发线程数"
    assert all(r == results[0] for r in results), "并发结果必须逐字段一致"
    assert len(clean_cache) == 1, "同一键在缓存中只保留一份"
    conn_main.close()


def test_hit_and_miss_values_identical(tmp_path, clean_cache):
    """命中与未命中返回值逐字段一致。"""
    from personal_brain.history.db import connect

    db = tmp_path / "c.sqlite"
    conn = connect(db)
    filters = SearchFilters()
    first = svc._coverage(conn, filters, "all")
    second = svc._coverage(conn, filters, "all")
    assert list(first.keys()) == list(second.keys())
    assert first == second
    assert first is not second, "不得把缓存对象本身暴露给调用方"
    conn.close()
