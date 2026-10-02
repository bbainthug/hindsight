"""profile.md「自动维护」小节草稿（任务书范围 3 / 用户故事 5）。

- 只替换 ``<!-- facts:auto:start -->`` 与 ``<!-- facts:auto:end -->``
  之间的内容；手写部分一律不动。
- 默认只打印 diff（unified），``--write`` 才落盘。
- 目标文件已存在但没有标记对：拒绝写入（防止破坏手写内容），
  提示用户加标记；目标文件不存在：生成带标记的新文件草稿。
"""

from __future__ import annotations

import difflib
import sqlite3
from pathlib import Path

from personal_brain.facts.views import list_claims

MARKER_START = "<!-- facts:auto:start -->"
MARKER_END = "<!-- facts:auto:end -->"

_TYPE_HEADINGS: dict[str, str] = {
    "preference": "## 偏好",
    "goal": "## 目标",
    "decision": "## 决定",
    "project_fact": "## 项目事实",
    "open_question": "## 待解问题",
}
_TYPE_ORDER = ("preference", "goal", "decision", "project_fact", "open_question")


def render_auto_section(conn: sqlite3.Connection) -> str:
    """渲染自动维护小节（只含 approved + active 主张）。"""
    claims = [
        row
        for row in list_claims(conn, status="active")
        if row.review_status == "approved" and row.lifecycle_status == "active"
    ]
    lines = [MARKER_START, "", "以下由 `brain facts export-profile` 自动维护，",
             "请勿在本区间外添加自动内容；手工编辑请写在区间之外。", ""]
    by_type: dict[str, list] = {}
    for row in claims:
        by_type.setdefault(row.memory_type, []).append(row)
    for memory_type in _TYPE_ORDER:
        rows = by_type.pop(memory_type, [])
        if not rows:
            continue
        lines.append(_TYPE_HEADINGS[memory_type])
        for row in rows:
            when = (row.asserted_at or "????-??-??")[:10]
            lines.append(
                f"- {row.content}（{when}，{row.evidence_count} 条原话证据"
                f" · brain facts explain {row.claim_id[:12]}）"
            )
        lines.append("")
    del by_type
    lines.append(MARKER_END)
    lines.append("")
    return "\n".join(lines)


def _replace_region(current: str, section: str) -> str:
    """替换标记区间；标记不成对或缺失时抛 ValueError。"""
    start = current.find(MARKER_START)
    end = current.find(MARKER_END)
    if (
        start < 0
        or end < 0
        or end < start
        or current.count(MARKER_START) != 1
        or current.count(MARKER_END) != 1
    ):
        raise ValueError(
            f"目标文件必须恰有一对顺序正确的 {MARKER_START} / {MARKER_END} 标记："
            "为保护手写内容，请在文件中加入标记对后重试"
        )
    end_off = end + len(MARKER_END)
    head = current[:start]
    tail = current[end_off:]
    section_without_trailing_newlines = section.rstrip("\n")
    return head + section_without_trailing_newlines + tail


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def build_profile_update(
    conn: sqlite3.Connection, out_path: Path
) -> tuple[str, str]:
    """返回 (当前文件全文, 替换后的全文)。文件缺失时当前为空串。"""
    section = render_auto_section(conn)
    if out_path.exists():
        current = out_path.read_text(encoding="utf-8")
        return current, _replace_region(current, section)
    header = (
        "# 个人档案（profile）\n\n"
        "<!-- 手写区：这里以下是手动维护的内容 -->\n\n"
    )
    return "", _ensure_trailing_newline(header + section)


def unified_profile_diff(current: str, updated: str, out_path: Path) -> str:
    """unified diff（打印给用户确认；不做静默写入）。"""
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"{out_path} (当前)",
        tofile=f"{out_path} (草稿)",
    )
    return "".join(diff)


def write_profile(out_path: Path, updated: str) -> None:
    """写入（仅在用户显式 --write 时由 CLI 调用）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_ensure_trailing_newline(updated), encoding="utf-8")
