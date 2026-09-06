"""事件身份与内容版本哈希测试（§4.2）。"""

from __future__ import annotations

from personal_brain.history.identity import (
    compute_event_id,
    compute_fallback_source_message_id,
    compute_revision_hash,
    content_digest,
    normalize_search_text,
    revision_id_for,
)
from personal_brain.history.timeutil import parse_unix_seconds


def _mk_time(v: float = 1754035200.0):
    return parse_unix_seconds(v)


class TestEventId:
    def test_deterministic_format(self):
        eid = compute_event_id("chatgpt", "alice", "m-0001")
        assert eid == "chatgpt:alice:m-0001"

    def test_namespace_separates_identities(self):
        """同一消息 ID 在不同账号命名空间下是不同事件。"""
        assert compute_event_id("chatgpt", "alice", "m-1") != compute_event_id(
            "chatgpt", "bob", "m-1"
        )


class TestFallbackIdentity:
    def test_same_text_different_position_not_merged(self):
        """仅凭文本 hash 不得把不同时间的相同发言合并（§4.2）。"""
        common = dict(
            source_kind="chatgpt",
            account_namespace="alice",
            conversation_id="conv-1",
            speaker_id="user",
            content_digest=content_digest("好的"),
        )
        a = compute_fallback_source_message_id(
            structural_position="root|0",
            original_time_value="1754035200.0",
            **common,
        )
        b = compute_fallback_source_message_id(
            structural_position="m-0001|1",
            original_time_value="1754035260.0",
            **common,
        )
        assert a != b

    def test_identical_position_and_time_is_stable(self):
        kwargs = dict(
            source_kind="chatgpt",
            account_namespace="alice",
            conversation_id="conv-1",
            structural_position="root|0",
            speaker_id="user",
            original_time_value="1754035200.0",
            content_digest="deadbeef",
        )
        assert compute_fallback_source_message_id(**kwargs) == (
            compute_fallback_source_message_id(**kwargs)
        )


class TestRevisionHash:
    def test_excludes_import_time_and_paths(self):
        """revision_hash 由调用参数构成：recorded_at / 归档路径 / 定位器
        根本不出现在哈希输入中——同内容跨导出必然同哈希。"""
        base = dict(
            content_type="text",
            raw_text="你好",
            speaker_id="user",
            speaker_type="owner",
            parsed_time=_mk_time(),
            source_updated_at="2026-08-01T08:01:00.000000Z",
            attachment_refs=[],
        )
        h1 = compute_revision_hash(**base)
        h2 = compute_revision_hash(**base)
        assert h1 == h2

    def test_content_change_changes_hash(self):
        base = dict(
            content_type="text",
            raw_text="你好",
            speaker_id="user",
            speaker_type="owner",
            parsed_time=_mk_time(),
            source_updated_at="2026-08-01T08:01:00.000000Z",
            attachment_refs=[],
        )
        changed = dict(base, raw_text="你好（已编辑）")
        assert compute_revision_hash(**base) != compute_revision_hash(**changed)

    def test_metadata_change_changes_hash(self):
        base = dict(
            content_type="text",
            raw_text="同文",
            speaker_id="user",
            speaker_type="owner",
            parsed_time=_mk_time(),
            source_updated_at="2026-08-01T08:01:00.000000Z",
            attachment_refs=[],
        )
        for field, value in [
            ("speaker_id", "assistant"),
            ("content_type", "code"),
            ("source_updated_at", "2026-08-01T08:02:00.000000Z"),
        ]:
            assert compute_revision_hash(**base) != compute_revision_hash(
                **{**base, field: value}
            ), f"{field} 必须参与哈希"

    def test_time_metadata_participates(self):
        base = dict(
            content_type="text",
            raw_text="x",
            speaker_id="user",
            speaker_type="owner",
            parsed_time=_mk_time(1754035200.0),
            source_updated_at=None,
            attachment_refs=[],
        )
        other = dict(base, parsed_time=_mk_time(1754035260.0))
        assert compute_revision_hash(**base) != compute_revision_hash(**other)


class TestDeterministicIds:
    def test_revision_id_deterministic(self):
        rid = revision_id_for("chatgpt:alice:m-1", "abc123")
        assert revision_id_for("chatgpt:alice:m-1", "abc123") == rid
        assert rid == "chatgpt:alice:m-1:rabc123"


class TestSearchText:
    def test_nfkc_casefold(self):
        assert normalize_search_text("ＡＩ工程") == "ai工程"
        assert normalize_search_text("Career") == "career"
