"""统一 history/Vault 检索的真实 stdio MCP 端到端测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from tests.integration.test_mcp_stdio import (
    McpClient,
    _config,
)
from tests.integration.test_mcp_stdio import (
    labeled_db as labeled_db,
)

UNIFIED_TOOLS = [
    "brain_status",
    "search_history",
    "search_brain",
    "search_vault",
    "get_vault_note",
]


def _write_config(
    tmp_path: Path,
    db: Path,
    vault_root: Path,
    *,
    tools: list[str] = UNIFIED_TOOLS,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_text_chars: int = 64,
) -> Path:
    config = {
        "database_path": str(db),
        "profile": "unified",
        "vault": {"root": str(vault_root)},
        "search": {
            "default_limit": 10,
            "max_limit": 50,
            "max_text_chars": max_text_chars,
        },
        "profiles": {
            "unified": {
                "allow_scopes": ["*"],
                "deny_sensitivity": ["secret"],
                "allow_unclassified": False,
                "tools": tools,
                "delivery_boundary": "local_only",
                "vault_include": include if include is not None else ["wiki/*.md"],
                "vault_exclude": exclude if exclude is not None else ["wiki/excluded.md"],
            }
        },
    }
    path = tmp_path / "mcp-unified.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture()
def unified_setup(tmp_path: Path, labeled_db: Path) -> dict[str, Path | str]:
    """合成 history + 仅授权 wiki/*.md 的临时 Vault。"""
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "projects").mkdir()

    career_text = "# 生涯\n求职记录：统一检索原文。\nhash-marker-v1\n"
    (vault / "wiki/career.md").write_text(career_text, encoding="utf-8")
    (vault / "wiki/excluded.md").write_text(
        "excluded-vault-marker 不应被 profile 返回。\n", encoding="utf-8"
    )
    (vault / "projects/outside.md").write_text(
        "outside-vault-marker 不在 include 匹配范围。\n", encoding="utf-8"
    )
    for name in ("budget-a.md", "budget-b.md"):
        (vault / "wiki" / name).write_text(
            "budget-marker " + "长文本预算检查。" * 40 + "\n", encoding="utf-8"
        )

    return {
        "db": labeled_db,
        "vault": vault,
        "career_text": career_text,
        "config": _write_config(tmp_path, labeled_db, vault),
    }


@pytest.fixture()
def unified_client(unified_setup):
    client = McpClient(unified_setup["config"])
    yield client
    client.close()


def _payload(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return result["structuredContent"]


def _vault_hash(item: dict) -> str:
    value = item.get("revision_id")
    assert isinstance(value, str) and len(value) == 64, f"Vault 结果缺少内容 hash: {item}"
    int(value, 16)
    return value


def _note_item(payload: dict) -> dict:
    if isinstance(payload.get("note"), dict):
        return payload["note"]
    if isinstance(payload.get("results"), list) and payload["results"]:
        assert len(payload["results"]) == 1
        return payload["results"][0]
    return payload


def _note_text(item: dict) -> str:
    value = item.get("content")
    assert isinstance(value, str), f"get_vault_note 结果缺少原文字段: {item}"
    return value


class TestUnifiedStdio:
    def test_search_brain_returns_both_sources_and_owner_only_filters_history(
        self, unified_client, unified_setup
    ):
        unified_client.start()
        assert unified_client.list_tools() == [
            "brain_status", "search_history", "search_brain", "search_vault", "get_vault_note",
        ]

        all_payload = _payload(
            unified_client.call(
                "search_brain", {"query": "求职", "source": "all", "limit": 10}
            )
        )
        all_results = all_payload["results"]
        assert {item["source_kind"] for item in all_results} == {"history", "vault"}
        history_results = [item for item in all_results if item["source_kind"] == "history"]
        assert {item["speaker_type"] for item in history_results} == {"owner", "assistant"}
        vault_results = [item for item in all_results if item["source_kind"] == "vault"]
        assert [item["path"] for item in vault_results] == ["wiki/career.md"]
        assert all_payload["sources"]["history"]["status"] == "ok"
        assert all_payload["sources"]["vault"]["status"] == "ok"

        owner_payload = _payload(
            unified_client.call(
                "search_brain",
                {"query": "求职", "source": "all", "speaker_type": "owner", "limit": 10},
            )
        )
        owner_history = [
            item for item in owner_payload["results"] if item["source_kind"] == "history"
        ]
        owner_vault = [
            item for item in owner_payload["results"] if item["source_kind"] == "vault"
        ]
        assert owner_history and all(item["speaker_type"] == "owner" for item in owner_history)
        assert owner_vault, "speaker_type=owner 不应把 Vault 结果误过滤掉"
        assert all(item["source_kind"] == "vault" for item in owner_vault)

        allowed = _payload(
            unified_client.call(
                "search_brain", {"query": "hash-marker-v1", "source": "vault"}
            )
        )
        assert [item["path"] for item in allowed["results"]] == ["wiki/career.md"]
        excluded = _payload(
            unified_client.call(
                "search_brain", {"query": "excluded-vault-marker", "source": "vault"}
            )
        )
        outside = _payload(
            unified_client.call(
                "search_brain", {"query": "outside-vault-marker", "source": "vault"}
            )
        )
        assert excluded["results"] == []
        assert outside["results"] == []

    def test_get_vault_note_returns_relative_path_content_and_hash(
        self, unified_client, unified_setup
    ):
        unified_client.start()
        search = _payload(
            unified_client.call(
                "search_brain", {"query": "hash-marker-v1", "source": "vault"}
            )
        )
        hit = search["results"][0]
        path = hit["path"]
        content_hash = _vault_hash(hit)
        assert path == "wiki/career.md"
        assert not Path(path).is_absolute()
        assert content_hash == hashlib.sha256(
            unified_setup["career_text"].encode("utf-8")
        ).hexdigest()

        note = _note_item(
            _payload(unified_client.call("get_vault_note", {"path": path}))
        )
        assert note["path"] == path
        assert _vault_hash(note) == content_hash
        assert _note_text(note) == unified_setup["career_text"]

    def test_vault_changes_and_deletes_are_visible_without_restart(
        self, unified_client, unified_setup
    ):
        unified_client.start()
        note_path = unified_setup["vault"] / "wiki/career.md"

        first = _payload(
            unified_client.call(
                "search_brain", {"query": "hash-marker-v1", "source": "vault"}
            )
        )
        old_hash = _vault_hash(first["results"][0])

        changed_text = "# 生涯\n求职记录：内容已经更新。\nhash-marker-v2\n"
        note_path.write_text(changed_text, encoding="utf-8")
        changed = _payload(
            unified_client.call(
                "search_brain", {"query": "hash-marker-v2", "source": "vault"}
            )
        )
        assert changed["results"]
        assert _vault_hash(changed["results"][0]) != old_hash
        stale_reference = unified_client.call(
            "get_vault_note",
            {"path": "wiki/career.md", "revision_id": old_hash},
        )
        assert stale_reference["isError"] is True
        assert stale_reference["structuredContent"]["error"] == "STALE_REFERENCE"
        stale = _payload(
            unified_client.call(
                "search_brain", {"query": "hash-marker-v1", "source": "vault"}
            )
        )
        assert stale["results"] == []

        note_path.unlink()
        deleted = _payload(
            unified_client.call(
                "search_brain", {"query": "hash-marker-v2", "source": "vault"}
            )
        )
        assert deleted["results"] == []

    def test_unified_response_obeys_result_limit_and_text_budget(
        self, unified_client
    ):
        unified_client.start()
        payload = _payload(
            unified_client.call(
                "search_brain", {"query": "budget-marker", "source": "vault", "limit": 50}
            )
        )
        snippets = [item["snippet"] for item in payload["results"]]
        assert len(snippets) <= 50
        assert sum(len(snippet) for snippet in snippets) <= 64
        assert payload["truncated"] is True

        limited = _payload(
            unified_client.call(
                "search_brain", {"query": "求职", "source": "all", "limit": 1}
            )
        )
        assert len(limited["results"]) <= 1
        assert limited["truncated"] is True

    def test_search_brain_cannot_bypass_missing_vault_authorization(
        self, tmp_path, labeled_db, unified_setup
    ):
        config = _write_config(
            tmp_path,
            labeled_db,
            unified_setup["vault"],
            tools=["brain_status", "search_history", "search_brain"],
        )
        client = McpClient(config)
        try:
            client.start()
            assert "search_brain" in client.list_tools()
            result = client.call(
                "search_brain", {"query": "hash-marker-v1", "source": "vault"}
            )
            assert result["isError"] is True
            payload = result["structuredContent"]
            assert payload["error"] == "NO_AVAILABLE_SOURCE"
            assert payload["sources"]["vault"]["status"] == "not_configured_or_not_allowed"

            direct = client.call("search_vault", {"query": "hash-marker-v1"})
            assert direct["isError"] is True
            assert direct["structuredContent"]["error"] == "NOT_FOUND_OR_NOT_ALLOWED"
        finally:
            client.close()

        history_denied_config = _write_config(
            tmp_path,
            labeled_db,
            unified_setup["vault"],
            tools=["brain_status", "search_brain", "search_vault"],
        )
        history_denied = McpClient(history_denied_config)
        try:
            history_denied.start()
            result = history_denied.call(
                "search_brain", {"query": "职业", "source": "history"}
            )
            assert result["isError"] is True
            payload = result["structuredContent"]
            assert payload["error"] == "NO_AVAILABLE_SOURCE"
            assert payload["sources"]["history"]["status"] == "not_configured_or_not_allowed"
        finally:
            history_denied.close()

    def test_legacy_profile_still_exposes_exactly_four_tools(self, tmp_path, labeled_db):
        client = McpClient(_config(tmp_path, labeled_db))
        try:
            client.start()
            assert client.list_tools() == [
                "brain_status", "search_history", "get_recent_events", "get_event",
            ]
        finally:
            client.close()
