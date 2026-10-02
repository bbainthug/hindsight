"""D-2 单元：profile 自动维护小节（只改标记区间、手写部分不动）。"""

from __future__ import annotations

import pytest
from facts_testkit import make_claim

from personal_brain.facts.profile_export import (
    MARKER_END,
    MARKER_START,
    _replace_region,
    build_profile_update,
    render_auto_section,
    unified_profile_diff,
    write_profile,
)


def test_auto_section_only_contains_approved_active(db):
    make_claim(
        db, "已生效偏好", memory_type="preference",
        review_status="approved", lifecycle_status="active",
    )
    make_claim(db, "待审核候选", memory_type="goal")  # pending：不出现
    make_claim(
        db, "被替代旧事实", memory_type="decision",
        review_status="approved", lifecycle_status="superseded",
    )
    section = render_auto_section(db)
    assert "已生效偏好" in section
    assert "待审核候选" not in section
    assert "被替代旧事实" not in section
    assert section.startswith(MARKER_START)
    assert section.rstrip().endswith(MARKER_END)


def test_new_file_draft_creates_markers(db):
    from pathlib import Path

    make_claim(
        db, "主攻 AI 测试开发", memory_type="decision",
        review_status="approved", lifecycle_status="active",
    )
    current, updated = build_profile_update(db, Path("/tmp/nonexistent-profile.md"))
    assert current == ""
    assert MARKER_START in updated
    assert MARKER_END in updated
    assert "主攻 AI 测试开发" in updated


def test_missing_markers_refused(db, tmp_path):
    target = tmp_path / "profile.md"
    target.write_text("# 手写档案\n\n没有任何标记。\n", encoding="utf-8")
    with pytest.raises(ValueError, match="标记"):
        build_profile_update(db, target)
    # 文件未被改动
    assert target.read_text(encoding="utf-8").startswith("# 手写档案")


def test_only_marker_region_is_replaced(db, tmp_path):
    target = tmp_path / "profile.md"
    handwritten = (
        "# 我的档案\n\n"
        "## 手写自我介绍\n"
        "这一段是手写的，永远不能被机器改。\n\n"
        f"{MARKER_START}\n"
        "旧自动内容（已过时）\n"
        f"{MARKER_END}\n"
        "\n结尾手写备注。\n"
    )
    target.write_text(handwritten, encoding="utf-8")
    make_claim(
        db, "主攻 AI 测试开发（2026-10）", memory_type="decision",
        review_status="approved", lifecycle_status="active",
    )
    current, updated = build_profile_update(db, target)
    assert current == handwritten
    # 手写部分原样保留
    assert "这一段是手写的，永远不能被机器改。" in updated
    assert "结尾手写备注。" in updated
    marker_end = updated.index(MARKER_END) + len(MARKER_END)
    assert updated[:updated.index(MARKER_START)] == handwritten[:handwritten.index(MARKER_START)]
    assert updated[marker_end:] == handwritten[
        handwritten.index(MARKER_END) + len(MARKER_END):
    ]
    # 旧自动内容被替换
    assert "旧自动内容（已过时）" not in updated
    assert "主攻 AI 测试开发（2026-10）" in updated
    assert updated.index(MARKER_START) < updated.index("主攻 AI 测试开发")
    assert updated.index("主攻 AI 测试开发") < updated.index(MARKER_END)


def test_diff_and_write_roundtrip(db, tmp_path):
    target = tmp_path / "profile.md"
    target.write_text(f"{MARKER_START}\n旧\n{MARKER_END}\n", encoding="utf-8")
    make_claim(
        db, "偏好夜间工作", memory_type="preference",
        review_status="approved", lifecycle_status="active",
    )
    current, updated = build_profile_update(db, target)
    diff = unified_profile_diff(current, updated, target)
    assert "-旧" in diff
    assert "+- 偏好夜间工作" in diff
    write_profile(target, updated)
    after = target.read_text(encoding="utf-8")
    assert "偏好夜间工作" in after
    assert after.rstrip().endswith(MARKER_END)


def test_replace_region_requires_paired_markers():
    with pytest.raises(ValueError):
        _replace_region("只有开始标记 " + MARKER_START, "new")
    with pytest.raises(ValueError):
        _replace_region("只有结束标记 " + MARKER_END, "new")
    with pytest.raises(ValueError, match="恰有一对"):
        _replace_region(
            f"{MARKER_START}\nold\n{MARKER_END}\n"
            f"{MARKER_START}\nsecond\n{MARKER_END}",
            "new",
        )
