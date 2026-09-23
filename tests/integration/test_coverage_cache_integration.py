"""D-5b 集成测试：覆盖期缓存行为（合成库、临时目录、不出网）。

- 命中与未命中返回值逐字段一致；
- 另一个连接导入新事件后 known_end 前移（import_watermark 失效，同连接）；
- 新开的连接读到别的连接导入的结果（import_watermark 跨连接可比）；
- withdraw-event 后覆盖期更新（policy_epoch 失效）；
- 不同 profile（allow_scopes / deny_sensitivity 不同）互不串缓存。
"""
from __future__ import annotations

import json
from pathlib import Path

from personal_brain.history.db import connect
from personal_brain.mcp_server import service as svc
from personal_brain.policy.profiles import AccessProfile, load_mcp_config
from personal_brain.retrieval.search import SearchFilters
from tests.integration.test_import_inbox import (  # 复用批次构造
    _batch_file,
    _one_conv,
    _run_script,
)


def _mcp_config(tmp_path: Path, allow_scopes: tuple[str, ...] | None,
                deny_sensitivity: tuple[str, ...] | None = None):
    yaml = tmp_path / "mcp.yaml"
    scopes = json.dumps(list(allow_scopes)) if allow_scopes is not None else '["*"]'
    deny = json.dumps(list(deny_sensitivity)) if deny_sensitivity is not None else "[]"
    unclassified = "false" if allow_scopes is not None else "true"
    yaml.write_text(f"""
database_path: {tmp_path}/db/brain.sqlite
profile: p1
profiles:
  p1:
    allow_scopes: {scopes}
    allow_unclassified: {unclassified}
    deny_sensitivity: {deny}
    delivery_boundary: local_only
    tools: [search_history, get_event, get_recent_events, brain_status]
""", encoding="utf-8")
    return load_mcp_config(yaml)


def _filters_for(cfg) -> SearchFilters:
    from personal_brain.mcp_server.service import profile_filters

    f = profile_filters(cfg.profile)
    return SearchFilters(
        allow_scopes=f.allow_scopes,
        deny_sensitivity=f.deny_sensitivity,
        allow_unclassified=f.allow_unclassified,
    )


def test_import_from_other_connection_advances_known_end(tmp_path, monkeypatch):
    svc._coverage_cache.clear()
    cfg = _mcp_config(tmp_path, None)
    db = tmp_path / "db/brain.sqlite"
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archives").mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    filters = _filters_for(cfg)

    base = svc._coverage(conn, filters, "all")
    assert base["known_end"] is None, "空库覆盖期为 None"

    # 另一个连接（导入进程）导入新事件 —— 走 CLI 的 import-chatgpt
    src_dir = tmp_path / "inbox/codex"
    _one_conv("d5b-conv-1", "D5b 覆盖期缓存集成测试消息")
    from tests.integration.test_import_inbox import _batch_file

    _batch_file(
        [_one_conv("d5b-conv-1", "D5b 覆盖期缓存集成测试消息")],
        src_dir, "20260923T150000Z-d5bd5bd5bd5b.json",
    )
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr
    # 旧连接继续调用：import_watermark 变化 → 缓存失效 → known_end 前移
    after = svc._coverage(conn, filters, "all")
    assert after["known_end"] is not None, "导入后同一连接的 known_end 必须前移"
    assert after["known_end"] != base["known_end"]
    assert after["known_start"] is not None
    conn.close()
    svc._coverage_cache.clear()


def test_new_connection_sees_import_from_other_connection(tmp_path):
    """回归（原 bug）：PRAGMA data_version 只在同一连接内可比，REST 连接池
    新开的连接可能撞上旧连接缓存过的 data_version 数值，读到导入前的旧缓存。

    场景：连接 A 算出缓存 → 另一个（导入）连接写入 → 新开的连接 C 查询，
    known_end 必须前移，不能命中 A 留下的、按错误键计算出的旧缓存。
    """
    svc._coverage_cache.clear()
    cfg = _mcp_config(tmp_path, None)
    db = tmp_path / "db/brain.sqlite"
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archives").mkdir(parents=True, exist_ok=True)
    filters = _filters_for(cfg)

    conn_a = connect(db)
    base = svc._coverage(conn_a, filters, "all")
    assert base["known_end"] is None, "空库覆盖期为 None"
    conn_a.close()

    # 另一个连接（导入进程）导入新事件
    src_dir = tmp_path / "inbox/codex"
    _batch_file(
        [_one_conv("d5b-conv-c", "D5b 新连接可见性测试消息")],
        src_dir, "20260923T154000Z-d5bc5bd5bd5b.json",
    )
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr

    # 新开的连接 C：不是 A，也不是导入连接
    conn_c = connect(db)
    after = svc._coverage(conn_c, filters, "all")
    assert after["known_end"] is not None, "新连接必须看到导入后的 known_end"
    assert after["known_end"] != base["known_end"]
    conn_c.close()
    svc._coverage_cache.clear()


def test_withdraw_updates_coverage_immediately(tmp_path):
    svc._coverage_cache.clear()
    cfg = _mcp_config(tmp_path, None)
    db = tmp_path / "db/brain.sqlite"
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archives").mkdir(parents=True, exist_ok=True)
    conn = connect(db)

    src_dir = tmp_path / "inbox/codex"
    _batch_file(
        [_one_conv("d5b-conv-w", "D5b 撤回覆盖期测试消息")],
        src_dir, "20260923T151000Z-d5bw5bd5bd5b.json",
    )
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr
    filters = _filters_for(cfg)
    before = svc._coverage(conn, filters, "all")
    assert before["known_end"] is not None
    # 缓存已热；此时撤回（同进程写连接，epoch bump + import_watermark 不变）。
    # 会话里每条消息是一个事件（user+assistant → 2 个），全部撤掉。
    from personal_brain.policy.withdraw import withdraw_event

    event_ids = [r["event_id"] for r in conn.execute("SELECT event_id FROM events")]
    assert len(event_ids) >= 2
    for eid in event_ids:
        withdraw_event(conn, eid)
    after = svc._coverage(conn, filters, "all")
    assert after["known_end"] is None, "全部事件撤回后覆盖期必须立即为空"
    conn.close()
    svc._coverage_cache.clear()


def test_different_profiles_do_not_share_cache(tmp_path):
    """allow_scopes / deny_sensitivity 不同的 profile 不得互串缓存。"""
    svc._coverage_cache.clear()
    db = tmp_path / "db/brain.sqlite"
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archives").mkdir(parents=True, exist_ok=True)
    _run_script_setup(tmp_path)
    conn = connect(db)

    prof_a = AccessProfile(
        name="a", allow_scopes=("career",), deny_sensitivity=(),
        allow_unclassified=False, tools=("search_history",),
        delivery_boundary="local_only",
    )
    prof_b = AccessProfile(
        name="b", allow_scopes=("personal",), deny_sensitivity=("secret",),
        allow_unclassified=False, tools=("search_history",),
        delivery_boundary="local_only",
    )
    fa = SearchFilters(allow_scopes=("career",), allow_unclassified=False)
    fb = SearchFilters(allow_scopes=("personal",), deny_sensitivity=("secret",),
                       allow_unclassified=False)
    va = svc._coverage(conn, fa, "all")
    vb = svc._coverage(conn, fb, "all")
    assert len(svc._coverage_cache) == 2, "两个不同签名必须各占一个缓存条目"
    # 签名不同 → 各自计算；这里主要断言互不覆盖
    assert va == svc._coverage(conn, fa, "all"), "A 命中不受 B 影响"
    assert vb == svc._coverage(conn, fb, "all"), "B 命中不受 A 影响"
    assert prof_a is not None and prof_b is not None  # profile 构造可用性
    conn.close()
    svc._coverage_cache.clear()


def _run_script_setup(tmp_path: Path) -> None:
    from tests.integration.test_import_inbox import _batch_file

    _batch_file(
        [_one_conv("d5b-conv-p", "D5b profile 隔离测试消息")],
        tmp_path / "inbox/codex", "20260923T152000Z-d5bp5bd5bd5b.json",
    )
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr


def test_hit_miss_identical_with_data(tmp_path):
    """有数据的库：命中与未命中逐字段一致。"""
    svc._coverage_cache.clear()
    cfg = _mcp_config(tmp_path, None)
    db = tmp_path / "db/brain.sqlite"
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archives").mkdir(parents=True, exist_ok=True)
    _batch_file(
        [_one_conv("d5b-conv-h", "D5b 命中一致性测试消息")],
        tmp_path / "inbox/codex", "20260923T153000Z-d5bh5bd5bd5b.json",
    )
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr
    conn = connect(db)
    filters = _filters_for(cfg)
    uncached = svc._coverage_uncached(conn, filters, "all")
    svc._coverage_cache.clear()
    miss = svc._coverage(conn, filters, "all")   # 未命中（缓存刚清空）
    hit = svc._coverage(conn, filters, "all")    # 命中
    assert miss == uncached
    assert hit == uncached
    assert list(hit.keys()) == list(uncached.keys())
    conn.close()
    svc._coverage_cache.clear()
