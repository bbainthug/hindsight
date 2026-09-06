"""ChatGPT 解析器测试（§5.1）。"""

from __future__ import annotations

import json

from synthetic import (
    branched_conversation,
    multimodal_conversation,
    naive_time_conversation,
    simple_conversation,
)

from personal_brain.importers.chatgpt import (
    find_conversation_members,
    parse_conversation,
)


def _parse_one(conv: dict, member: str = "conversations.json"):
    errors: list = []
    return parse_conversation(conv, member, errors), errors


class TestStructureDetection:
    def test_renamed_member_still_detected(self):
        """按结构识别，不凭 conversations.json 文件名猜测（§5.1）。"""
        data = json.dumps([simple_conversation()]).encode("utf-8")
        hits, errors = find_conversation_members([("data/anything.json", data)])
        assert len(hits) == 1
        assert errors == []

    def test_object_with_conversations_key_detected(self):
        data = json.dumps({"conversations": [simple_conversation()]}).encode("utf-8")
        hits, _ = find_conversation_members([("c.json", data)])
        assert len(hits) == 1

    def test_non_conversation_json_ignored(self):
        data = json.dumps([{"hello": "world"}]).encode("utf-8")
        hits, errors = find_conversation_members([("x.json", data)])
        assert hits == []
        assert any(e.code == "NO_CONVERSATION_DATA" for e in errors)

    def test_invalid_json_reported(self):
        hits, errors = find_conversation_members([("x.json", b"not json")])
        assert hits == []
        assert any(e.code == "MEMBER_NOT_JSON" for e in errors)


class TestMessageParsing:
    def test_roles_and_text(self):
        conv, errors = _parse_one(simple_conversation())
        assert errors == []
        by_id = {n.node_id: n for n in conv.nodes}
        assert by_id["m-0001"].speaker_type == "owner"
        assert by_id["m-0001"].raw_text == "我最近正在考虑职业方向变化"
        assert by_id["a-0001"].speaker_type == "assistant"
        assert by_id["m-0001"].created.utc_iso == "2026-08-01T08:00:00.000000Z"
        assert by_id["a-0002"].updated.utc_iso == "2026-08-01T08:03:00.000000Z"

    def test_parent_and_child_order(self):
        conv, _ = _parse_one(branched_conversation())
        by_id = {n.node_id: n for n in conv.nodes}
        assert by_id["b-0003"].parent_node_id == "b-0002"
        assert by_id["b-0003"].child_order == 0  # 左支
        assert by_id["b-0004"].child_order == 1  # 右支

    def test_structural_node(self):
        conv, _ = _parse_one(simple_conversation())
        root = next(n for n in conv.nodes if n.node_id == "root")
        assert root.is_structural is True
        assert root.source_message_id is None

    def test_multimodal_attachments_and_unsupported(self):
        conv, errors = _parse_one(multimodal_conversation())
        mm = next(n for n in conv.nodes if n.node_id == "mm-0001")
        assert mm.content_type == "multimodal_text"
        assert mm.raw_text == "看看这张图"
        assert len(mm.attachments) == 1
        assert mm.attachments[0].asset_pointer == "file-service://synthetic-image-1"
        unsupported = [e for e in errors if e.code == "UNSUPPORTED_CONTENT"]
        assert len(unsupported) == 1
        assert unsupported[0].locator.endswith("/mapping/mm-0001")

    def test_code_content_type_preserved(self):
        conv, _ = _parse_one(multimodal_conversation())
        code = next(n for n in conv.nodes if n.node_id == "mm-0002")
        assert code.content_type == "code"
        assert code.raw_text == "print('hello')"

    def test_message_without_id_falls_back(self):
        conv, _ = _parse_one(multimodal_conversation())
        no_id = next(n for n in conv.nodes if n.node_id == "mm-0003")
        assert no_id.source_message_id is None

    def test_missing_time_is_unknown(self):
        conv, _ = _parse_one(naive_time_conversation())
        m = next(n for n in conv.nodes if n.node_id == "t-0001")
        assert m.created is None
        assert m.updated is None

    def test_locator_uses_stable_node_id(self):
        """定位器用成员路径 + 稳定节点 ID，不用数组下标（跨导出稳定）。"""
        conv, _ = _parse_one(simple_conversation(), member="deep/dir/conversations.json")
        m = next(n for n in conv.nodes if n.node_id == "m-0001")
        assert m.locator == "deep/dir/conversations.json#/conversations/conv-1/mapping/m-0001"

    def test_missing_conversation_id_derives_fallback(self):
        raw = simple_conversation()
        raw.pop("conversation_id")
        conv, _ = _parse_one(raw)
        assert conv.conversation_id.startswith("conv-fb-")

    def test_unparseable_mapping_node_recorded(self):
        raw = simple_conversation()
        raw["mapping"]["broken"] = "not-a-dict"
        conv, errors = _parse_one(raw)
        assert any(e.code == "UNSUPPORTED_NODE" for e in errors)
        # 其余节点仍完整解析
        assert any(n.node_id == "m-0001" for n in conv.nodes)
