from __future__ import annotations

from pathlib import Path

import pytest
from synthetic import T0, build_conversation, node, text_message, write_zip

from personal_brain.history.db import connect
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.mcp_server.service import (
    CREDENTIAL_REDACTION_NOTE,
    ToolError,
    tool_get_event,
    tool_get_recent_events,
    tool_search_history,
)
from personal_brain.mcp_server.unified import search_brain
from personal_brain.policy.labels import label_event
from personal_brain.policy.profiles import AccessProfile, McpServerConfig
from personal_brain.retrieval.search import SearchLimits

FAKE_KEY = "sk-proj-synthetic-credential-00000000000000000000"
FAKE_ASSIGNMENT = "api_key=synthetic-assignment-credential-000000"


def remote_profile(**overrides) -> AccessProfile:
    values = dict(
        name="remote-test",
        allow_scopes=("*",),
        deny_sensitivity=("secret", "private"),
        allow_unclassified=True,
        tools=("search_history", "get_recent_events", "get_event", "search_brain"),
        delivery_boundary="remote_model_allowed",
    )
    values.update(overrides)
    return AccessProfile(**values)


@pytest.fixture()
def env(tmp_path: Path):
    main_text = (
        f"旧观点验收标记：我曾询问如何连接历史记忆。公开说明 {FAKE_KEY}。"
        " owner=用户"
    )
    assistant_text = f"AI建议内容，供上下文区分；{FAKE_ASSIGNMENT}；speaker=AI"
    mapping = dict(
        [
            node("root", None, None, ["cred-user"]),
            node(
                "cred-user",
                text_message("cred-user", "user", main_text, T0),
                "root",
                ["cred-assistant"],
            ),
            node(
                "cred-assistant",
                text_message("cred-assistant", "assistant", assistant_text, T0 + 60),
                "cred-user",
                [],
            ),
        ]
    )
    conversation = build_conversation(
        "credential-conversation", "凭据拦截合成测试", T0, mapping, "cred-assistant"
    )
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    conn = connect(tmp_path / "brain.sqlite")
    importer = ChatGPTImporter(conn, archive_dir)
    importer.import_archive(
        write_zip([conversation], tmp_path / "credential.zip"), "synthetic-user"
    )
    user_event = conn.execute(
        "SELECT event_id FROM events WHERE source_message_id = ?", ("cred-user",)
    ).fetchone()["event_id"]
    assistant_event = conn.execute(
        "SELECT event_id FROM events WHERE source_message_id = ?", ("cred-assistant",)
    ).fetchone()["event_id"]
    yield conn, user_event, assistant_event, main_text, assistant_text
    conn.close()


def test_remote_search_recent_and_context_redact_without_changing_offsets(env):
    conn, user_event, assistant_event, main_text, assistant_text = env
    profile = remote_profile()

    search = tool_search_history(
        conn,
        profile,
        {"query": "旧观点验收标记", "limit": 10},
        SearchLimits(snippet_chars=400),
    )
    assert len(search["results"]) == 1
    search_hit = search["results"][0]
    assert FAKE_KEY not in search_hit["snippet"]
    assert "旧观点验收标记" in search_hit["snippet"]
    assert search_hit["speaker_type"] == "owner"
    assert CREDENTIAL_REDACTION_NOTE in search["warnings"]

    unified = search_brain(
        conn,
        McpServerConfig(
            db_path=Path(":memory:"),
            timezone="UTC",
            profile=profile,
            search_limits={},
            vault=None,
        ),
        {"query": "旧观点验收标记", "source": "history"},
        SearchLimits(snippet_chars=400),
    )
    assert unified["results"]
    assert FAKE_KEY not in unified["results"][0]["snippet"]

    recent = tool_get_recent_events(
        conn,
        profile,
        {"days": 3650, "limit": 10, "now": "2026-08-02T00:00:00+00:00"},
        SearchLimits(snippet_chars=400),
    )
    recent_text = " ".join(item["snippet"] or "" for item in recent["results"])
    assert FAKE_KEY not in recent_text
    assert FAKE_ASSIGNMENT not in recent_text
    assert CREDENTIAL_REDACTION_NOTE in recent["warnings"]

    event = tool_get_event(
        conn,
        profile,
        {"event_id": user_event, "context_radius": 1},
        SearchLimits(max_text_chars=8000),
    )["results"][0]
    assert FAKE_KEY not in event["raw_text"]
    assert len(event["raw_text"]) == len(main_text)
    assert event["raw_text_total_chars"] == len(main_text)
    assert event["speaker_type"] == "owner"
    assert event["context"]
    assert event["context"][0]["speaker_type"] == "assistant"
    assert FAKE_ASSIGNMENT not in event["context"][0]["snippet"]
    assert "AI建议内容" in event["context"][0]["snippet"]

    # The second message is a control for the same remote boundary: metadata
    # and non-secret evidence remain usable after redaction.
    assistant = tool_get_event(
        conn,
        profile,
        {"event_id": assistant_event},
        SearchLimits(max_text_chars=8000),
    )["results"][0]
    assert assistant["speaker_type"] == "assistant"
    assert "AI建议内容" in assistant["raw_text"]
    assert FAKE_ASSIGNMENT not in assistant["raw_text"]
    assert len(assistant["raw_text"]) == len(assistant_text)


def test_remote_pagination_masks_secret_before_slicing(env):
    conn, user_event, _assistant_event, main_text, _assistant_text = env
    profile = remote_profile()
    limits = SearchLimits(max_text_chars=11)
    secret_start = main_text.index(FAKE_KEY)

    offsets = [0, max(0, secret_start - 2), secret_start + len(FAKE_KEY) - 2]
    for offset in offsets:
        page = tool_get_event(
            conn,
            profile,
            {"event_id": user_event, "text_offset": offset},
            limits,
        )["results"][0]
        assert FAKE_KEY not in page["raw_text"]
        assert page["raw_text_total_chars"] == len(main_text)


def test_remote_policy_still_rejects_labeled_secret(env):
    conn, user_event, _assistant_event, _main_text, _assistant_text = env
    label_event(conn, user_event, sensitivity_labels=("secret",))
    profile = remote_profile()

    search = tool_search_history(
        conn, profile, {"query": "旧观点验收标记"}, SearchLimits()
    )
    assert search["results"] == []
    with pytest.raises(ToolError) as exc:
        tool_get_event(conn, profile, {"event_id": user_event}, SearchLimits())
    assert exc.value.code == "NOT_FOUND_OR_NOT_ALLOWED"
