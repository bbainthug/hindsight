"""MCP 服务层契约测试（§7.3/§8）：逐结果授权、计数不泄露、epoch、信封。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import write_zip

from personal_brain.history.db import connect
from personal_brain.history.policy_epoch import bump_policy_epoch, current_policy_epoch
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.mcp_server.service import (
    PolicyEpochChurn,
    ToolError,
    profile_filters,
    run_with_epoch_retry,
    tool_brain_status,
    tool_get_event,
    tool_get_recent_events,
    tool_search_history,
)
from personal_brain.policy.labels import label_event, label_source
from personal_brain.policy.profiles import AccessProfile
from personal_brain.policy.withdraw import withdraw_source
from personal_brain.retrieval.search import SearchLimits

LIMITS = SearchLimits()


def agent_profile(**kw) -> AccessProfile:
    defaults = dict(
        name="agent",
        allow_scopes=("career",),
        deny_sensitivity=("secret",),
        allow_unclassified=False,
        tools=("brain_status", "search_history", "get_recent_events", "get_event"),
        delivery_boundary="local_only",
    )
    defaults.update(kw)
    return AccessProfile(**defaults)


@pytest.fixture()
def env(tmp_path: Path):
    """语料库 + 全量 career 标注 + sec01 标 secret（agent 不可见）。"""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    conn = connect(tmp_path / "brain.sqlite")
    importer = ChatGPTImporter(conn, archive_dir)
    zp = write_zip(retrieval_corpus_conversations(), tmp_path / "corpus.zip")
    importer.import_archive(zp, "testuser")
    source_id = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
    label_source(conn, source_id, scope_labels=("career",))
    sec01 = conn.execute(
        "SELECT event_id FROM events WHERE event_id LIKE '%:sec01'"
    ).fetchone()["event_id"]
    label_event(conn, sec01, scope_labels=("home",), sensitivity_labels=("secret",))
    return SimpleNamespace(
        conn=conn, importer=importer, source_id=source_id, tmp_path=tmp_path
    )


def _ev(conn, tail: str) -> str:
    return conn.execute(
        "SELECT event_id FROM events WHERE event_id LIKE ?", (f"%:{tail}",)
    ).fetchone()["event_id"]


class TestSearchAuthorization:
    def test_policy_filter_applied(self, env):
        """§7.3：过滤发生在结果排序/snippet 之前——secret 事件根本不出现。"""
        r = tool_search_history(env.conn, agent_profile(), {"query": "安全"}, LIMITS)
        assert r["results"] == []
        assert r["truncated"] is False

    def test_counts_do_not_leak_hidden_records(self, env):
        """§7.3：计数只统计调用者可见档案。"""
        agent = agent_profile()
        r = tool_search_history(env.conn, agent, {"query": "职业"}, LIMITS)
        # corpus 中职业命中 5 条（job01/rel01/both/early/undat），全部 career 且非 secret
        assert r["results"] and len(r["results"]) == 5
        # 本地全权视角确认 secret/未分类不存在泄露
        n_sec = tool_search_history(
            env.conn,
            AccessProfile("local", allow_scopes=("*",), deny_sensitivity=(),
                          allow_unclassified=True,
                          tools=("search_history",), delivery_boundary="local_only"),
            {"query": "职业"}, LIMITS,
        )
        assert n_sec["truncated"] is False
        assert len(n_sec["results"]) >= len(r["results"])

    def test_unclassified_hidden_by_default(self, env, tmp_path):
        """§7.2：未知分类默认不对普通 Agent 开放；计数同样不泄露。"""
        # 导入第二个来源且不标注 → agent 完全不可见
        zp = write_zip(retrieval_corpus_conversations(), tmp_path / "b.zip")
        env.importer.import_archive(zp, "other")
        r = tool_search_history(env.conn, agent_profile(), {"query": "职业"}, LIMITS)
        assert len(r["results"]) == 5  # 第二来源的 5 条职业命中不可见
        st = tool_brain_status(env.conn, agent_profile(), LIMITS)
        assert [s["account_namespace"] for s in st["sources"]] == ["testuser"]


class TestGetEventAuthorization:
    def test_not_found_indistinguishable_from_forbidden(self, env):
        """§7.3：无权限与不存在返回相同 NOT_FOUND_OR_NOT_ALLOWED。"""
        conn = env.conn
        sec01 = _ev(conn, "sec01")  # secret → agent 无权限
        with pytest.raises(ToolError) as e1:
            tool_get_event(conn, agent_profile(), {"event_id": sec01}, LIMITS)
        with pytest.raises(ToolError) as e2:
            tool_get_event(
                conn, agent_profile(), {"event_id": "chatgpt:testuser:missing"}, LIMITS
            )
        assert e1.value.code == e2.value.code == "NOT_FOUND_OR_NOT_ALLOWED"
        assert str(e1.value) == str(e2.value)

    def test_event_full_text_and_citation(self, env):
        conn = env.conn
        r = tool_get_event(
            conn, agent_profile(), {"event_id": _ev(conn, "job01")}, LIMITS
        )
        item = r["results"][0]
        assert item["citation_id"].startswith("pb:chatgpt:testuser:job01:")
        assert item["evidence_level"] == "message_record"
        assert "职业方向变化" in item["raw_text"]
        # 逻辑定位符不含磁盘路径（§8.1）
        assert set(item["source_locator"]) == {"snapshot_id", "node_id"}
        assert item["source_locator"]["node_id"] == "job01"
        assert "/" not in item["source_locator"]["snapshot_id"]

    def test_context_radius_filters_neighbors(self, env):
        """§7.3：邻接消息逐条授权——secret 邻接不可见，未授权不计数量。"""
        conn = env.conn
        # 半径 1：邻接为 root（无事件）与 sec01（secret）→ 全部略去，且不报数量
        r1 = tool_get_event(
            conn,
            agent_profile(),
            {"event_id": _ev(conn, "job01"), "context_radius": 1},
            LIMITS,
        )
        assert r1["results"][0]["context"] == []
        # 半径 2：更远的 ai01（career）可见，offset=+2
        r2 = tool_get_event(
            conn,
            agent_profile(),
            {"event_id": _ev(conn, "job01"), "context_radius": 2},
            LIMITS,
        )
        ctx = r2["results"][0]["context"]
        assert len(ctx) == 1
        assert ctx[0]["event_id"].endswith("ai01")
        assert ctx[0]["offset"] == 2

    def test_context_radius_bounds(self, env):
        with pytest.raises(ToolError, match="INVALID_PARAMS"):
            tool_get_event(
                env.conn,
                agent_profile(),
                {"event_id": _ev(env.conn, "job01"), "context_radius": 4},
                LIMITS,
            )

    def test_withdrawn_hidden_via_resolver(self, env):
        """§8.1：源撤回后 resolver 返回已不可用，不因引用 ID 绕过。"""
        conn = env.conn
        withdraw_source(conn, env.source_id)
        with pytest.raises(ToolError, match="NOT_FOUND_OR_NOT_ALLOWED"):
            tool_get_event(conn, agent_profile(), {"event_id": _ev(conn, "job01")}, LIMITS)
        assert tool_search_history(conn, agent_profile(), {"query": "职业"}, LIMITS)[
            "results"
        ] == []


class TestEnvelope:
    def test_envelope_fields(self, env):
        r = tool_search_history(env.conn, agent_profile(), {"query": "职业"}, LIMITS)
        assert r["schema_version"] == "0.2"
        assert r["content_trust"] == "untrusted_archive"
        assert r["coverage"]["completeness"] == "unknown"
        assert r["warnings"]
        assert any("语义扩展" in w for w in r["warnings"])
        assert set(r) == {
            "schema_version", "content_trust", "coverage", "results",
            "truncated", "next_cursor", "warnings",
        }

    def test_query_too_broad_as_tool_error(self, env):
        with pytest.raises(ToolError, match="QUERY_TOO_BROAD"):
            tool_search_history(
                env.conn,
                agent_profile(),
                {"query": "安"},
                SearchLimits(max_scan=3),
            )

    def test_empty_query_invalid(self, env):
        with pytest.raises(ToolError, match="INVALID_PARAMS"):
            tool_search_history(env.conn, agent_profile(), {"query": "  "}, LIMITS)


class TestRecentAndStatus:
    def test_recent_cursor_pagination(self, env):
        conn = env.conn
        p1 = tool_get_recent_events(
            conn, agent_profile(), {"days": 7, "limit": 3, "now": "2026-08-02T08:00:00+00:00"},
            LIMITS,
        )
        assert p1["truncated"] is True and p1["next_cursor"]
        assert p1["reference_time"].startswith("2026-08-02")
        p2 = tool_get_recent_events(
            conn,
            agent_profile(),
            {"days": 7, "limit": 3, "now": "2026-08-02T08:00:00+00:00",
             "cursor": p1["next_cursor"]},
            LIMITS,
        )
        ids1 = {x["revision_id"] for x in p1["results"]}
        ids2 = {x["revision_id"] for x in p2["results"]}
        assert ids1.isdisjoint(ids2)
        assert p1["results"][0]["timestamp"] >= p1["results"][-1]["timestamp"]

    def test_recent_respects_policy(self, env, tmp_path):
        zp = write_zip(retrieval_corpus_conversations(), tmp_path / "b.zip")
        env.importer.import_archive(zp, "other")
        r = tool_get_recent_events(
            env.conn, agent_profile(),
            {"days": 7, "now": "2026-08-02T08:00:00+00:00"}, LIMITS,
        )
        assert {x["event_id"].rsplit(":", 2)[-2] for x in r["results"]} <= {"testuser"}

    def test_brain_status_coverage_visible_only(self, env):
        r = tool_brain_status(env.conn, agent_profile(), LIMITS)
        cov = r["coverage"]
        assert cov["known_start"] and cov["known_end"]
        assert cov["known_start"] >= "2026-07-31"  # 只描述可见档案
        assert r["health"] == "ok"
        assert r["capabilities"]["read_only"] is True

    def test_brain_status_hides_withdrawn(self, env):
        withdraw_source(env.conn, env.source_id)
        r = tool_brain_status(env.conn, agent_profile(), LIMITS)
        assert r["sources"] == []
        assert r["coverage"]["known_start"] is None


class TestPolicyEpoch:
    def test_epoch_bumps_on_policy_change(self, env):
        conn = env.conn
        before = current_policy_epoch(conn)
        label_event(conn, _ev(conn, "job01"), scope_labels=("x",))
        after_label = current_policy_epoch(conn)
        withdraw_source(conn, env.source_id)
        after_withdraw = current_policy_epoch(conn)
        assert after_label == before + 1
        assert after_withdraw == after_label + 1

    def test_epoch_retry_reruns_when_changed(self, env):
        """§7.3：执行中策略变化 → 重新授权（重跑），持续变化则放弃。"""
        conn = env.conn
        calls = {"n": 0}

        def flaky(conn, profile, args, limits):
            calls["n"] += 1
            if calls["n"] == 1:
                bump_policy_epoch(conn)  # 模拟执行中发生标注/撤回
                conn.commit()
            return {"ok": True}

        out = run_with_epoch_retry(flaky, conn, agent_profile(), {}, LIMITS)
        assert out == {"ok": True} and calls["n"] == 2  # 重跑一次后稳定

        def always_changing(conn, profile, args, limits):
            bump_policy_epoch(conn)
            conn.commit()
            return {"ok": True}

        with pytest.raises(PolicyEpochChurn):
            run_with_epoch_retry(always_changing, conn, agent_profile(), {}, LIMITS)

    def test_profile_filters_wildcard(self):
        f = profile_filters(
            AccessProfile("l", allow_scopes=("*",), deny_sensitivity=(),
                          allow_unclassified=True, tools=("search_history",),
                          delivery_boundary="local_only")
        )
        assert f.allow_scopes is None and f.allow_unclassified is True
