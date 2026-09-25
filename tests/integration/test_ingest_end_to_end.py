"""D-6 端到端（合成数据、不出网）：

/ingest/main 收到的批次 → D-5 的 import_inbox.sh 导入 → search_history 可检到；
与已存在的同一对话（相同消息 ID）重复导入，新增事件为 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from personal_brain.mcp_server.http_app import create_app
from personal_brain.policy.profiles import load_mcp_config
from tests.integration.test_import_inbox import BRAIN, REPO, SCRIPT, _load_sync_agents
from tests.integration.test_ingest import (
    INGEST_TOKEN,
    _AppClient,
    _write_ingest_config,
)

INGEST_TOKEN_E2E = INGEST_TOKEN


@pytest.fixture
def ingest_env(monkeypatch):
    monkeypatch.setenv("BRAIN_INGEST_TOKEN", INGEST_TOKEN_E2E)

pytestmark = pytest.mark.skipif(
    not BRAIN.exists(), reason="需要项目 .venv 里的 brain CLI"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _conv(cid: str, text: str, update_time: float = 1758000100.0) -> dict:
    spec = _load_sync_agents()
    conv = spec.Conv(cid, f"标题 {cid}")
    conv.add(1758000000.0, "user", text, f"{cid}:0")
    conv.add(1758000060.0, "assistant", f"回复：{text}", f"{cid}:1")
    out = conv.finish()
    out["update_time"] = update_time
    return out


async def _ingest(tmp_path: Path, db: Path, body: bytes) -> httpx.Response:
    config = load_mcp_config(_write_ingest_config(tmp_path, db))
    async with _AppClient(create_app(config, token="t")) as client:
        return await client.post(
            "/ingest/main", content=body,
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
        )


def _init_db(tmp_path: Path) -> None:
    """connect() 即建库+迁移：/ingest 的只读连接要求库文件已存在。"""
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    code = (
        "import sys; from pathlib import Path;"
        "sys.path.insert(0, str(Path.home() / 'hindsight/src')) if False else None;"
        f"sys.path.insert(0, {str(REPO / 'src')!r});"
        f"from personal_brain.history.db import connect;"
        f"connect(Path({str(tmp_path / 'db/brain.sqlite')!r})).close()"
    )
    subprocess.run([str(BRAIN), "--help"], capture_output=True)  # 确认 CLI 存在
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


def _run_import(tmp_path: Path) -> subprocess.CompletedProcess:
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
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


def _search(tmp_path: Path, query: str) -> str:
    return subprocess.run(
        [str(BRAIN), "--db", str(tmp_path / "db/brain.sqlite"),
         "--archive-dir", str(tmp_path / "archives"),
         "search-history", query, "--limit", "5"],
        capture_output=True, text=True, env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "HINDSIGHT_VM_DB": str(tmp_path / "db/brain.sqlite"),
            "HINDSIGHT_VM_ARCHIVE_DIR": str(tmp_path / "archives"),
            "HINDSIGHT_SEMANTIC_REINDEX": "0",
        }, timeout=120,
    ).stdout


def _event_count(tmp_path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{tmp_path / 'db/brain.sqlite'}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.anyio
async def test_ingest_batch_flows_to_search_history(tmp_path, ingest_env):
    _init_db(tmp_path)
    marker = "E2E ingest 独有标记词"
    conv = _conv("conv-e2e-1", marker)
    body = json.dumps([conv], ensure_ascii=False).encode()
    resp = await _ingest(tmp_path, tmp_path / "db/brain.sqlite", body)
    assert resp.status_code == 202
    assert list((tmp_path / "inbox" / "main").glob("*.json")), "批次已落 inbox/main"

    result = _run_import(tmp_path)
    assert result.returncode == 0, result.stderr
    hits = _search(tmp_path, "E2E ingest 独有标记词")
    assert marker in hits, f"search_history 未检到导入的对话: {hits!r}"


@pytest.mark.anyio
async def test_same_conversation_reingested_zero_new_events(tmp_path, ingest_env):
    _init_db(tmp_path)
    conv = _conv("conv-e2e-2", "幂等端到端消息")
    body = json.dumps([conv], ensure_ascii=False).encode()
    r1 = await _ingest(tmp_path, tmp_path / "db/brain.sqlite", body)
    assert r1.status_code == 202
    assert _run_import(tmp_path).returncode == 0
    count_after_first = _event_count(tmp_path)
    assert count_after_first > 0

    # 首轮导入后批次文件已被 D-5 归档移走：再次提交同内容（或内容更新、
    # 消息 ID 不变）会得到新批次文件，导入后按事件 ID 幂等、新增事件为 0。
    conv2 = dict(conv)
    conv2["update_time"] = 1758000200.0  # 模拟对话更新，消息 ID 不变
    body2 = json.dumps([conv2], ensure_ascii=False).encode()
    r2 = await _ingest(tmp_path, tmp_path / "db/brain.sqlite", body)
    r3 = await _ingest(tmp_path, tmp_path / "db/brain.sqlite", body2)
    assert r2.status_code in (200, 202)
    assert r3.status_code in (200, 202)
    assert _run_import(tmp_path).returncode == 0
    assert _event_count(tmp_path) == count_after_first  # 新增事件为 0
