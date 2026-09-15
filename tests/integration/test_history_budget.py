from pathlib import Path

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import write_zip

from personal_brain.history.db import connect, connect_readonly
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.mcp_server.service import ToolError, tool_get_event
from personal_brain.policy.labels import label_source
from personal_brain.policy.profiles import AccessProfile
from personal_brain.retrieval.search import SearchLimits


def test_readonly_connection_cannot_write_or_create(tmp_path):
    import sqlite3

    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(missing)
    assert not missing.exists()
    conn = connect(tmp_path / "ok.sqlite")
    conn.close()
    conn = connect_readonly(tmp_path / "ok.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE forbidden (id INTEGER)")
    conn.close()


def test_long_event_paginates_original_without_losing_evidence(tmp_path: Path):
    conn = connect(tmp_path / "brain.sqlite")
    arc = tmp_path / "archives"
    arc.mkdir()
    ChatGPTImporter(conn, arc).import_archive(
        write_zip(retrieval_corpus_conversations(), tmp_path / "input.zip"), "testuser"
    )
    sid = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
    label_source(conn, sid, scope_labels=("career",))
    profile = AccessProfile("test", ("career",), ("secret",), False, ("get_event",), "local_only")
    limits = SearchLimits(max_text_chars=5)
    args = {"event_id": "chatgpt:testuser:job01"}
    first = tool_get_event(conn, profile, args, limits)
    item = first["results"][0]
    original = conn.execute(
        "SELECT raw_text FROM event_revisions WHERE revision_id=?", (item["revision_id"],)
    ).fetchone()["raw_text"]
    pieces = [item["raw_text"]]
    while item["next_text_offset"] is not None:
        assert len(item["raw_text"]) <= 5
        item = tool_get_event(
            conn, profile,
            {**args, "revision_id": item["revision_id"], "text_offset": item["next_text_offset"]},
            limits,
        )["results"][0]
        pieces.append(item["raw_text"])
    assert "".join(pieces) == original
    with pytest.raises(ToolError):
        tool_get_event(conn, profile, {**args, "text_offset": -1}, limits)
    conn.close()
