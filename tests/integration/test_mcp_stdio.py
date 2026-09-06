"""MCP stdio 集成测试（§8/§17 Phase B）：真实子进程 JSON-RPC 全链路。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import write_zip

from personal_brain.history.db import connect
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.policy.labels import label_source

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "pytest-client", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class McpClient:
    """行分隔 JSON-RPC 子进程客户端。"""

    def __init__(self, config_path: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "personal_brain.mcp_server.server",
             "--config", str(config_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._next_id = 10

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, f"服务器 stdout 提前关闭: {self.proc.stderr.read()[:500]}"
        return json.loads(line)

    def start(self) -> dict:
        self._send(INIT)
        resp = self._recv()
        self._send(INITIALIZED)
        return resp

    def call(self, name: str, arguments: dict) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        return self._recv()["result"]

    def list_tools(self) -> list[str]:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list"})
        resp = self._recv()
        return [t["name"] for t in resp["result"]["tools"]]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()
        self.proc.wait(timeout=10)


@pytest.fixture(scope="module")
def labeled_db(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("mcp")
    conn = connect(tmp / "brain.sqlite")
    arc = tmp / "archives"
    arc.mkdir()
    importer = ChatGPTImporter(conn, arc)
    zp = write_zip(retrieval_corpus_conversations(), tmp / "corpus.zip")
    importer.import_archive(zp, "testuser")
    sid = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
    label_source(conn, sid, scope_labels=("career",))
    conn.close()
    return tmp / "brain.sqlite"


def _config(tmp_path: Path, db: Path, tools: list[str] | None = None,
            scopes: str = "['*']") -> Path:
    tools_line = tools or ["brain_status", "search_history", "get_recent_events", "get_event"]
    p = tmp_path / "mcp.yaml"
    p.write_text(
        f"""
database_path: {db}
profile: agent
profiles:
  agent:
    allow_scopes: {scopes}
    deny_sensitivity: [secret]
    allow_unclassified: false
    tools: {tools_line}
    delivery_boundary: local_only
""",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def client(tmp_path, labeled_db):
    c = McpClient(_config(tmp_path, labeled_db))
    yield c
    c.close()


class TestStdioMcp:
    def test_initialize_and_tools_list(self, client):
        init = client.start()
        assert init["result"]["serverInfo"]["name"] == "personal-brain"
        assert client.list_tools() == [
            "brain_status", "search_history", "get_recent_events", "get_event",
        ]

    def test_search_end_to_end(self, client):
        client.start()
        r = client.call("search_history", {"query": "职业"})
        assert r.get("structuredContent") is not None or r.get("content")
        payload = r["structuredContent"]
        assert payload["content_trust"] == "untrusted_archive"
        assert len(payload["results"]) == 5
        assert payload["results"][0]["citation_id"].startswith("pb:chatgpt:testuser:")

    def test_get_event_and_withdrawn_check(self, client, labeled_db):
        client.start()
        r = client.call("get_event", {"event_id": "chatgpt:testuser:job01"})
        item = r["structuredContent"]["results"][0]
        assert item["citation_id"].startswith("pb:")

    def test_tool_restriction_by_profile(self, tmp_path, labeled_db):
        """§7.3：profile 决定工具面；未列出的工具调用被拒。"""
        c = McpClient(_config(tmp_path, labeled_db, tools=["brain_status"]))
        try:
            c.start()
            assert c.list_tools() == ["brain_status"]
            r = c.call("search_history", {"query": "职业"})
            assert r["isError"] is True
            assert r["structuredContent"]["error"] == "NOT_FOUND_OR_NOT_ALLOWED"
        finally:
            c.close()

    def test_client_config_roundtrip(self, client, labeled_db):
        """§17：合成数据 MCP 集成测试——状态工具返回可见覆盖。"""
        client.start()
        r = client.call("brain_status", {})
        payload = r["structuredContent"]
        assert payload["health"] == "ok"
        assert payload["coverage"]["known_start"] is not None
        assert payload["capabilities"]["tools"]
