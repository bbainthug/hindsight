"""D-5 集成测试：import_inbox.sh 批次导入（合成数据、临时目录、不出网）。

覆盖任务书要求的三个场景：
- 同一批次内容导入两次，第二次新增事件为 0（导入器幂等）；
- 损坏批次进 failed/，后续批次照常导入（批次级失败隔离）；
- flock 防重叠：持锁期间第二轮直接跳过。
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/vm/import_inbox.sh"
BRAIN = REPO / ".venv/bin/brain"

pytestmark = pytest.mark.skipif(
    not BRAIN.exists(), reason="需要项目 .venv 里的 brain CLI"
)


def _batch_file(payload: list[dict], src_dir: Path, name: str) -> Path:
    src_dir.mkdir(parents=True, exist_ok=True)
    f = src_dir / name
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return f


def _one_conv(cid: str, text: str) -> dict:
    spec = _load_sync_agents()
    conv = spec.Conv(cid, f"标题 {cid}")
    conv.add(1758000000.0, "user", text, f"{cid}:0")
    conv.add(1758000060.0, "assistant", f"回复：{text}", f"{cid}:1")
    return conv.finish()


def _load_sync_agents():
    import importlib.util

    if getattr(_load_sync_agents, "_spec", None) is None:
        s = importlib.util.spec_from_file_location(
            "sync_agents_it", REPO / "integrations/agent_sync/sync_agents.py"
        )
        mod = importlib.util.module_from_spec(s)
        sys.modules.setdefault("sync_agents_it", mod)
        s.loader.exec_module(mod)
        _load_sync_agents._spec = mod
    return _load_sync_agents._spec


def _run_script(tmp_path: Path, source: str = "codex") -> subprocess.CompletedProcess:
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)  # connect 不建父目录
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "HINDSIGHT_INBOX": str(tmp_path / "inbox"),
        "HINDSIGHT_VM_DB": str(tmp_path / "db/brain.sqlite"),
        "HINDSIGHT_VM_ARCHIVE_DIR": str(tmp_path / "archives"),
        "HINDSIGHT_BRAIN": str(BRAIN),
        "HINDSIGHT_SEMANTIC_REINDEX": "0",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=120
    )


def _init_db(tmp_path: Path) -> None:
    """import-chatgpt 首次运行即建库（connect 内置迁移），无需预建。"""


def _event_count(db_path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    finally:
        conn.close()


def test_same_batch_content_imported_twice_zero_new(tmp_path):
    db = tmp_path / "db/brain.sqlite"
    src_dir = tmp_path / "inbox/codex"
    conv = _one_conv("batch-conv-1", "批次幂等测试消息")
    _batch_file([conv], src_dir, "20260919T080000Z-aaaaaaaaaaaa.json")
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr
    first = _event_count(db)
    assert first > 0
    assert (tmp_path / "inbox/done/codex/20260919T080000Z-aaaaaaaaaaaa.json").exists()
    # 同内容不同名批次：导入器按消息 ID 幂等 → 新增事件 0
    _batch_file([conv], src_dir, "20260919T080100Z-aaaaaaaaaaaa.json")
    r2 = _run_script(tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert _event_count(db) == first, "同内容第二批次不得新增事件"
    assert "新事件 0" in r2.stdout or "新事件 0" in r2.stderr


def test_corrupt_batch_goes_to_failed_and_does_not_block(tmp_path):
    db = tmp_path / "db/brain.sqlite"
    src_dir = tmp_path / "inbox/codex"
    # 损坏批次排在前面（文件名序），好批次在后——必须仍被导入
    _batch_file([], src_dir, "20260919T080000Z-badbadbadbad.json")  # 空 JSON 数组：无对话数据
    good = _one_conv("batch-conv-2", "损坏隔离测试消息")
    _batch_file([good], src_dir, "20260919T080100Z-goodgoodgood.json")
    r = _run_script(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "inbox/failed/codex/20260919T080000Z-badbadbadbad.json").exists()
    assert (tmp_path / "inbox/done/codex/20260919T080100Z-goodgoodgood.json").exists()
    assert _event_count(db) > 0, "坏批次之后的好批次必须照常导入"
    assert "导入失败" in (r.stdout + r.stderr)


def test_lock_prevents_overlap(tmp_path):
    """持锁期间第二轮直接跳过（Linux=flock；macOS 走 mkdir 回退锁）。"""
    src_dir = tmp_path / "inbox/codex"
    _batch_file([_one_conv("batch-conv-3", "防重叠测试")], src_dir,
                "20260919T080000Z-locklocklock.json")
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    have_flock = subprocess.run(["bash", "-c", "command -v flock"],
                                capture_output=True).returncode == 0
    fh = None
    try:
        if have_flock:
            fh = (inbox / ".import.lock").open("w")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        else:
            (inbox / ".import.lockdir").mkdir()
        r = _run_script(tmp_path)
    finally:
        if fh is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
    assert r.returncode == 0
    assert "跳过" in (r.stdout + r.stderr), "持锁时第二轮必须直接跳过"
    assert not (tmp_path / "inbox/done/codex").exists() or not any(
        (tmp_path / "inbox/done/codex").iterdir()
    ), "跳过的那轮不得导入任何批次"
