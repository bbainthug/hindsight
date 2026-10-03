"""D-2 单元：提炼分段（选中路径、时间/字符切分、有限上下文重叠）。"""

from __future__ import annotations

from facts_testkit import import_conversations, linear_conversation
from synthetic import T0

from personal_brain.facts.segmentation import build_segments
from tests.fixtures.synthetic import (  # noqa: F401  (path 由 conftest 注入)
    node,
    text_message,
)


def _import_two_gapStatements(db, importer):
    """同一对话内两条 owner 发言，间隔 3 小时（> 默认 3600s 阈值）。"""
    conv = linear_conversation(
        "conv-gap", "间隔切分",
        [
            ("g0", "user", "上午：我决定主攻 AI 测试开发。", T0),
            ("g1", "assistant", "好的。", T0 + 60),
            ("g2", "user", "下午：我把简历改好了。", T0 + 3 * 3600),
            ("g3", "assistant", "收到。", T0 + 3 * 3600 + 60),
        ],
    )
    import_conversations(importer, [conv])


def test_gap_split_creates_two_segments_with_context_carry(db, importer):
    _import_two_gapStatements(db, importer)
    segments, path_unknown = build_segments(db, since=None, until=None)
    assert path_unknown == 0
    assert len(segments) == 2
    first, second = segments
    # 上午的发言在第一段；下午的在第二段
    assert any("上午" in m.text for m in first.messages)
    assert not any("下午" in m.text for m in first.messages)
    # 第二段以有限上下文开始（重叠消息 role=context）
    ctx = [m for m in second.messages if m.role == "context"]
    assert ctx, "切分处应携带有限上下文"
    assert all(m.speaker_type == "assistant" or m.role == "context" for m in ctx)
    # 下午的发言在新段里是 support
    support_second = [m for m in second.messages if m.role == "support"]
    assert any("简历" in m.text for m in support_second)


def test_char_limit_splits_and_dedupes_overlap(db, importer):
    long_text = "很长的发言" * 30  # 150 字
    msgs = [(f"c{i}", "user", f"{long_text}第{i}条", T0 + i * 60) for i in range(4)]
    conv = linear_conversation("conv-long", "长文本切分", msgs)
    import_conversations(importer, [conv])
    segments, _ = build_segments(
        db, since=None, until=None, max_segment_chars=400, context_messages=2
    )
    assert len(segments) >= 2
    # 任何 owner 消息在单个段内只出现一次；重叠处以 context 角色出现
    for seg in segments:
        support_ids = [m.revision_id for m in seg.messages if m.role == "support"]
        assert len(support_ids) == len(set(support_ids))


def test_zero_context_splits_without_replaying_previous_messages(db, importer):
    msgs = [
        (f"z{i}", "user", f"第{i}条 owner 发言。" * 8, T0 + i * 60)
        for i in range(4)
    ]
    import_conversations(
        importer, [linear_conversation("conv-no-context", "无上下文", msgs)]
    )
    segments, _ = build_segments(
        db, since=None, until=None, max_segment_chars=200, context_messages=0
    )
    assert len(segments) >= 2
    assert all(msg.role == "support" for seg in segments for msg in seg.messages)


def test_assistant_only_window_is_not_sent_for_extraction(db, importer):
    conv = linear_conversation(
        "conv-assistant-only", "只含助手", [
            ("ao1", "assistant", "助手的总结，不是用户事实。", T0 + 60),
        ],
    )
    import_conversations(importer, [conv])
    segments, _ = build_segments(db, since=None, until=None)
    assert segments == []


def test_off_path_branch_excluded(db, importer):
    """分支对话：current 在左支 → 右支消息不进入提炼输入（§4.3 选中路径）。"""
    m1 = text_message("br0", "user", "共享祖先消息", T0)
    left = text_message("br-left", "user", "左支：我决定走工程路线", T0 + 60)
    right = text_message("br-right", "user", "右支：我决定走学术路线", T0 + 90)
    mapping = dict(
        [
            node("root2", None, None, ["br0"]),
            node("br0", m1, "root2", ["br-left", "br-right"]),
            node("br-left", left, "br0", []),
            node("br-right", right, "br0", []),
        ]
    )
    conv = linear_conversation("conv-branchx", "分支", [("root2", "user", None, None)])
    conv["mapping"] = mapping
    conv["current_node"] = "br-left"
    import_conversations(importer, [conv])
    segments, _ = build_segments(db, since=None, until=None)
    all_texts = [m.text for seg in segments for m in seg.messages]
    assert any("工程路线" in t for t in all_texts)
    assert not any("学术路线" in t for t in all_texts)  # 非所选路径不提炼


def test_withdrawn_revision_excluded(db, importer):
    conv = linear_conversation(
        "conv-wd", "撤回",
        [
            ("w1", "user", "会被撤回的发言：我讨厌所有会议。", T0),
            ("w2", "user", "保留的发言：我喜欢短会。", T0 + 60),
        ],
    )
    import_conversations(importer, [conv])
    rev = db.execute(
        "SELECT revision_id, event_id FROM event_revisions WHERE raw_text LIKE '会被撤回%'"
    ).fetchone()
    from personal_brain.policy.withdraw import withdraw_event

    withdraw_event(db, rev["event_id"])
    segments, _ = build_segments(db, since=None, until=None)
    texts = [m.text for seg in segments for m in seg.messages]
    assert not any("讨厌所有会议" in t for t in texts)
    assert any("喜欢短会" in t for t in texts)


def test_max_owner_chars_skips_long_owner_keeps_context(db, importer):
    long_text = "粘贴的超长日志。" + "x" * 3000
    conv = linear_conversation(
        "conv-cap", "收窄",
        [
            ("c1", "user", long_text, T0),
            ("c2", "assistant", "助手回复上下文。", T0 + 30),
            ("c3", "user", "短发言：我偏好异步沟通。", T0 + 60),
        ],
    )
    import_conversations(importer, [conv])
    segments, _ = build_segments(db, since=None, until=None, max_owner_chars=500)
    owners = [m.text for seg in segments for m in seg.messages
              if m.speaker_type == "owner"]
    assert owners == ["短发言：我偏好异步沟通。"]  # 超长 owner 被跳过
    contexts = [m.text for seg in segments for m in seg.messages
                if m.speaker_type == "assistant"]
    assert contexts == ["助手回复上下文。"]  # assistant 上下文保留
    # 默认不过滤
    segments, _ = build_segments(db, since=None, until=None)
    owners = [m.text for seg in segments for m in seg.messages
              if m.speaker_type == "owner"]
    assert any("粘贴的超长日志" in t for t in owners)


def test_include_assistant_context_false_drops_assistant(db, importer):
    conv = linear_conversation(
        "conv-noasst", "无上下文",
        [
            ("n1", "user", "我决定换笔记本键盘布局。", T0),
            ("n2", "assistant", "助手的话不进分段。", T0 + 30),
        ],
    )
    import_conversations(importer, [conv])
    segments, _ = build_segments(
        db, since=None, until=None, include_assistant_context=False
    )
    texts = [m.text for seg in segments for m in seg.messages]
    assert texts == ["我决定换笔记本键盘布局。"]
