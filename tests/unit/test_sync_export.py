"""D-5 单元测试：sync_agents.py 的批次导出（文件名稳定 / 原子写 / 双断点独立）。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sync_agents", Path(__file__).resolve().parents[2] / "integrations/agent_sync/sync_agents.py"
)
sync_agents = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("sync_agents", sync_agents)
_SPEC.loader.exec_module(sync_agents)


def _codex_session(path: Path, user_text: str) -> None:
    lines = [
        json.dumps({"timestamp": "2026-09-19T08:00:00Z", "type": "session_meta"}),
        json.dumps({
            "timestamp": "2026-09-19T08:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "text", "text": user_text}]},
        }),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["sync_agents.py", *argv])
    return sync_agents.main()


def test_batch_filename_stable_and_content_hashed():
    now = datetime(2026, 9, 19, 8, 30, 0, tzinfo=UTC)
    a = sync_agents.batch_filename("hello", now)
    b = sync_agents.batch_filename("hello", now)
    c = sync_agents.batch_filename("world", now)
    assert a == b, "同内容必须同名"
    assert a != c, "不同内容不同名"
    assert a.startswith("20260919T083000Z-")
    digest = hashlib.sha256(b"hello").hexdigest()[:12]
    assert a == f"20260919T083000Z-{digest}.json"


def test_write_batch_atomic_and_idempotent(tmp_path):
    dest1 = sync_agents.write_batch(tmp_path, "codex", "payload-1")
    assert dest1.parent == tmp_path / "codex"
    assert dest1.exists()
    assert not list(tmp_path.rglob("*.tmp")), "原子写不得残留 .tmp"
    first_bytes = dest1.read_bytes()
    # 同内容重复导出：同名、内容不变
    dest2 = sync_agents.write_batch(tmp_path, "codex", "payload-1")
    assert dest2 == dest1
    assert dest2.read_bytes() == first_bytes
    # 不同内容：不同文件
    dest3 = sync_agents.write_batch(tmp_path, "codex", "payload-2")
    assert dest3 != dest1
    assert sync_agents.write_batch(tmp_path, "claude", "payload-1").parent == tmp_path / "claude"


def _use_tmp_codex(monkeypatch: pytest.MonkeyPatch, sessions: Path, tmp_path: Path) -> None:
    """把 codex 来源指到临时目录（绝不扫描真实 ~/.codex）。"""
    monkeypatch.setitem(
        sync_agents.SOURCES, "codex", (sessions, "**/*.jsonl", sync_agents.parse_codex)
    )
    monkeypatch.setattr(sync_agents, "ALL_SOURCES", list(sync_agents.SOURCES))


def test_export_state_independent_of_import_state(tmp_path, monkeypatch):
    sessions = tmp_path / "codex-home/sessions"
    sessions.mkdir(parents=True)
    _use_tmp_codex(monkeypatch, sessions, tmp_path)
    _codex_session(sessions / "rollout-2026-a.jsonl", "第一条消息")
    export_dir = tmp_path / "outbox"
    state_file = tmp_path / "state.json"
    export_state_file = tmp_path / "export_state.json"

    rc = _run([
        "--sources", "codex",
        "--state", str(state_file),
        "--export-dir", str(export_dir),
        "--export-state", str(export_state_file),
        "--no-import",
    ], monkeypatch)
    assert rc == 0
    batches = sorted((export_dir / "codex").glob("*.json"))
    assert len(batches) == 1
    payload_text = batches[0].read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    assert payload[0]["conversation_id"] == "codex-a"
    assert "第一条消息" in payload_text, "批次必须包含会话正文"
    # 双断点互不干扰：导出断点已写；本地导入断点不得被 --no-import 推进
    assert export_state_file.exists()
    assert len(json.loads(export_state_file.read_text())) == 1
    assert not state_file.exists(), "--no-import 不得写导入断点"

    # 源文件没变：不产出新批次（导出断点生效）
    rc = _run([
        "--sources", "codex",
        "--state", str(state_file),
        "--export-dir", str(export_dir),
        "--export-state", str(export_state_file),
        "--no-import",
    ], monkeypatch)
    assert rc == 0
    assert len(sorted((export_dir / "codex").glob("*.json"))) == 1

    # 新会话文件出现：导出断点推进，产出第二个批次
    _codex_session(sessions / "rollout-2026-b.jsonl", "第二条消息")
    rc = _run([
        "--sources", "codex",
        "--state", str(state_file),
        "--export-dir", str(export_dir),
        "--export-state", str(export_state_file),
        "--no-import",
    ], monkeypatch)
    assert rc == 0
    batches = sorted((export_dir / "codex").glob("*.json"))
    assert len(batches) == 2
    assert len(json.loads(export_state_file.read_text())) == 2


def test_no_import_requires_export_dir(monkeypatch, tmp_path):
    rc = _run(["--sources", "codex", "--state", str(tmp_path / "s.json"), "--no-import"],
              monkeypatch)
    assert rc == 2, "--no-import 没有 --export-dir 应当报错退出"


def test_bad_source_file_no_batch_no_crash(tmp_path, monkeypatch):
    """坏内容文件：parse 静默产出 0 会话（既有设计），不崩、不产出批次。"""
    sessions = tmp_path / "codex-home/sessions"
    sessions.mkdir(parents=True)
    _use_tmp_codex(monkeypatch, sessions, tmp_path)
    (sessions / "broken.jsonl").write_text("{not json\n", encoding="utf-8")
    export_dir = tmp_path / "outbox"
    export_state_file = tmp_path / "export_state.json"
    rc = _run([
        "--sources", "codex", "--export-dir", str(export_dir),
        "--export-state", str(export_state_file), "--no-import",
    ], monkeypatch)
    assert rc == 0, "单个坏文件不应拖垮整体"
    assert not (export_dir / "codex").exists() or not any((export_dir / "codex").iterdir())
