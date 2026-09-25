"""D-6 `/ingest` 服务端测试：进程内 ASGI（httpx.ASGITransport），不出网。

覆盖任务书 §测试：token 错误/缺失/未配置 → 404；超大请求、结构不合格 → 400 且
不落盘；同内容两次提交 → 同一文件名（200）；namespace 不在白名单 → 404；
全程数据库文件未被修改（db + WAL 哈希不变）；日志不含对话内容。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from pathlib import Path

import pytest

from personal_brain.mcp_server.http_app import create_app
from personal_brain.policy.profiles import load_mcp_config
from tests.integration.test_remote_http import _AppClient, _write_config

INGEST_TOKEN = secrets.token_hex(32)  # 64 hex chars = 32 字节


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def labeled_db(tmp_path_factory) -> Path:
    """极小合成语料库：只为验证 /ingest 全程不碰数据库文件。"""
    from corpus import retrieval_corpus_conversations
    from synthetic import write_zip

    from personal_brain.history.db import connect
    from personal_brain.importers.importer import ChatGPTImporter

    tmp = tmp_path_factory.mktemp("ingest")
    conn = connect(tmp / "brain.sqlite")
    arc = tmp / "archives"
    arc.mkdir()
    importer = ChatGPTImporter(conn, arc)
    zp = write_zip(retrieval_corpus_conversations(), tmp / "corpus.zip")
    importer.import_archive(zp, "testuser")
    conn.close()
    return tmp / "brain.sqlite"


def _write_ingest_config(tmp_path: Path, db: Path, *, namespaces: str = "[main]") -> Path:
    p = tmp_path / "ingest.yaml"
    p.write_text(
        f"""
database_path: {db}
profile: agent
profiles:
  agent:
    allow_scopes: ['*']
    deny_sensitivity: [secret]
    allow_unclassified: false
    tools: [brain_status, search_history]
    delivery_boundary: remote_model_allowed
ingest:
  inbox: {tmp_path / "inbox"}
  namespaces: {namespaces}
""",
        encoding="utf-8",
    )
    return p


def _conversation(conv_id: str = "conv-x") -> dict:
    return {
        "title": "t",
        "create_time": 1758000000.0,
        "update_time": 1758000100.0,
        "mapping": {
            "root": {"message": None, "parent": None, "children": []},
        },
        "current_node": "root",
        "conversation_id": conv_id,
    }


def _ingest_headers(token: str = INGEST_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def ingest_env(monkeypatch):
    monkeypatch.setenv("BRAIN_INGEST_TOKEN", INGEST_TOKEN)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_writes_batch_and_returns_202(tmp_path, labeled_db, ingest_env):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    app = _AppClient(create_app(config, token="t"))
    async with app as client:
        body = json.dumps([_conversation()]).encode()
        resp = await client.post("/ingest/main", content=body, headers=_ingest_headers())
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["conversations"] == 1
    batch_dir = tmp_path / "inbox" / "main"
    files = list(batch_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == payload["batch"]
    assert files[0].read_bytes() == body  # 原样落盘
    assert not list(batch_dir.glob("*.tmp"))  # 原子写：无残留


@pytest.mark.anyio
async def test_ingest_duplicate_content_same_filename_200(tmp_path, labeled_db, ingest_env):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    body = json.dumps([_conversation()]).encode()
    async with _AppClient(create_app(config, token="t")) as client:
        r1 = await client.post("/ingest/main", content=body, headers=_ingest_headers())
        r2 = await client.post("/ingest/main", content=body, headers=_ingest_headers())
    assert r1.status_code == 202 and r2.status_code == 200
    assert r1.json()["batch"] == r2.json()["batch"]
    assert len(list((tmp_path / "inbox" / "main").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# 鉴权与白名单：错误一律 404，不区分原因
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers", [{}, _ingest_headers("wrong"), {"Authorization": INGEST_TOKEN}],
    ids=["missing", "wrong", "malformed"],
)
async def test_ingest_auth_failure_404(tmp_path, labeled_db, ingest_env, headers):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post(
            "/ingest/main",
            content=json.dumps([_conversation()]).encode(), headers=headers,
        )
    assert resp.status_code == 404
    assert not (tmp_path / "inbox" / "main").exists()  # 不落盘


@pytest.mark.anyio
async def test_ingest_namespace_not_in_whitelist_404(tmp_path, labeled_db, ingest_env):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post(
            "/ingest/other",
            content=json.dumps([_conversation()]).encode(), headers=_ingest_headers(),
        )
    assert resp.status_code == 404
    assert not (tmp_path / "inbox" / "other").exists()


# ---------------------------------------------------------------------------
# 未配置 token / 配置 → /ingest 不注册（404）
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("env_value", [None, "", "short", "g" * 64, "z" * 63],
                         ids=["unset", "empty", "short", "non-hex", "63chars"])
async def test_ingest_not_registered_without_valid_token(
    tmp_path, labeled_db, monkeypatch, env_value,
):
    if env_value is None:
        monkeypatch.delenv("BRAIN_INGEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BRAIN_INGEST_TOKEN", env_value)
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post(
            "/ingest/main",
            content=json.dumps([_conversation()]).encode(), headers=_ingest_headers(),
        )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_ingest_not_registered_without_ingest_config(tmp_path, labeled_db, ingest_env):
    config = load_mcp_config(_write_config(tmp_path, labeled_db))  # 无 ingest 段
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post(
            "/ingest/main",
            content=json.dumps([_conversation()]).encode(), headers=_ingest_headers(),
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 结构/大小校验：400 且不落盘
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b'{"a": 1}',
        b"[]",  # 空数组
        json.dumps(["str"]).encode(),
        json.dumps([{"conversation_id": "c"}]).encode(),  # 缺 mapping
        json.dumps([{"mapping": {}}]).encode(),  # 缺 conversation_id
        json.dumps([{"mapping": {}, "conversation_id": 1}]).encode(),  # 类型错
    ],
    ids=["notjson", "obj", "notlist", "empty", "list-of-str", "no-mapping",
         "no-conv-id", "conv-id-type"],
)
async def test_ingest_invalid_payload_400_no_file(tmp_path, labeled_db, ingest_env, payload):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post("/ingest/main", content=payload, headers=_ingest_headers())
    assert resp.status_code == 400
    assert not (tmp_path / "inbox" / "main").exists() or \
        not list((tmp_path / "inbox" / "main").glob("*"))


@pytest.mark.anyio
async def test_ingest_body_over_20mb_400(tmp_path, labeled_db, ingest_env):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    big = json.dumps([_conversation(), {"pad": "x" * 21_000_000}]).encode()
    assert len(big) > 20_000_000
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post("/ingest/main", content=big, headers=_ingest_headers())
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 数据库全程未被修改；日志不含内容
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_never_touches_database(tmp_path, labeled_db, ingest_env, caplog):
    def digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = {
        p.name: digest(p)
        for p in labeled_db.parent.iterdir()
        if p.name.startswith("brain.sqlite")
    }
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    content = json.dumps([_conversation()]).encode()
    async with _AppClient(create_app(config, token="t")) as client:
        r1 = await client.post("/ingest/main", content=content, headers=_ingest_headers())
        r2 = await client.post("/ingest/main", content=content, headers=_ingest_headers())
    assert r1.status_code == 202 and r2.status_code == 200
    after = {
        p.name: digest(p)
        for p in labeled_db.parent.iterdir()
        if p.name.startswith("brain.sqlite")
    }
    assert before == after  # db/-wal/-shm 逐字节未变

    with caplog.at_level(logging.INFO, logger="personal_brain.remote_access"):
        async with _AppClient(create_app(config, token="t")) as client:
            await client.post("/ingest/main", content=content, headers=_ingest_headers())
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "deploy" not in logs and "conv-x" not in logs and "帮我" not in logs
    assert "namespace=main" in logs and "conversations=1" in logs


# ---------------------------------------------------------------------------
# 每 IP 节流：每分钟 10 次
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_rate_limited_10_per_minute(tmp_path, labeled_db, ingest_env):
    config = load_mcp_config(_write_ingest_config(tmp_path, labeled_db))
    statuses = []
    async with _AppClient(create_app(config, token="t")) as client:
        for i in range(12):
            payload = json.dumps([_conversation(f"conv-rate-{i}")]).encode()
            resp = await client.post(
                "/ingest/main", content=payload, headers=_ingest_headers(),
            )
            statuses.append(resp.status_code)
    assert statuses[:10] == [202] * 10  # 前 10 次正常
    assert statuses[10] == 429 and statuses[11] == 429
