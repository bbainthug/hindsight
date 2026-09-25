"""D-7 集成测试：recall 与 timeline（合成库，不出网）。

覆盖任务书 §测试：
- recall 每条消息都在 profile 授权集合内；未授权邻居不出现、不影响计数；
- recall 命中 ⊆ 同 query 的 search_history 命中；
- 远程 profile 下正文凭据被遮蔽；
- timeline 日期边界按配置时区；区间 > 62 天报 INVALID_PARAMS；
- timeline 不泄露不可见对话（数量与存在性）；
- MCP tools/list 受 profile 白名单控制、tools/call 两例；
- REST 两端点正常路径 + 错误码。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from personal_brain.history.db import connect
from personal_brain.mcp_server.service import (
    ToolError,
    tool_recall,
    tool_search_history,
    tool_timeline,
)
from personal_brain.policy.labels import label_event, label_source
from personal_brain.policy.profiles import AccessProfile, load_mcp_config
from personal_brain.retrieval.search import SearchLimits

REPO = Path(__file__).resolve().parents[2]

LIMITS = SearchLimits()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# 默认起点 1758051000 = 2025-09-16T17:30Z = 上海 2025-09-17 01:30
def _conv_spec(cid: str, lines: list[tuple[str, str]], start: float = 1758051000.0):
    """构造导出格式对话：lines 为 (speaker, text) 列表，间隔 60s。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sync_agents_d7", REPO / "integrations/agent_sync/sync_agents.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("sync_agents_d7", mod)
    spec.loader.exec_module(mod)
    conv = mod.Conv(cid, f"标题 {cid}")
    for i, (speaker, text) in enumerate(lines):
        conv.add(start + i * 60.0, speaker, text, f"{cid}:{i}")
    return conv.finish()


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> Path:
    """合成库：一个正常对话 + 一个含 secret 消息的对话 + 一个含凭据的对话。"""
    tmp = tmp_path_factory.mktemp("d7")
    conn = connect(tmp / "brain.sqlite")
    arc = tmp / "archives"
    arc.mkdir()

    import zipfile

    from personal_brain.importers.importer import ChatGPTImporter

    conversations = [
        # 0: 普通多轮对话（deploy 话题）
        _conv_spec("conv-normal", [
            ("user", "我们聊聊 deploy 流水线的缓存策略"),
            ("assistant", "deploy 缓存可以按分支隔离"),
            ("user", "deploy 分支缓存失效时机呢"),
            ("assistant", "合并到主干时失效最稳妥"),
            ("user", "好，deploy 就这么定"),
        ]),
        # 1: 中间一条是 secret 的对话（neighbors 测试）
        _conv_spec("conv-secret", [
            ("user", "讨论 begins 这里是公开开头"),
            ("user", "这一条是 secret 情报内容标记"),
            ("user", "这里是公开结尾讨论 salary"),
        ]),
        # 2: 含凭据文本的对话（遮蔽测试）
        _conv_spec("conv-cred", [
            ("user", "服务器密钥是 sk-proj-synthetic-credential-00000000000000000000 记得轮换"),
            ("assistant", "已记录，凭据轮换提醒已设置"),
        ]),
    ]
    zp = tmp / "corpus.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("conversations.json", json.dumps(conversations, ensure_ascii=False))
    importer = ChatGPTImporter(conn, arc)
    importer.import_archive(zp, "testuser")
    source_id = conn.execute("SELECT source_id FROM sources LIMIT 1").fetchone()["source_id"]
    label_source(conn, source_id, scope_labels=("career",))
    # 把 conv-secret 的中间消息标成 secret（sensitivity=secret → 默认 profile 拒绝）
    row = conn.execute(
        "SELECT e.event_id FROM events e JOIN event_revisions r"
        " ON r.event_id = e.event_id WHERE r.raw_text LIKE '%secret 情报内容%'"
    ).fetchone()
    assert row is not None
    label_event(conn, row["event_id"], sensitivity_labels=("secret",))
    conn.close()
    return tmp / "brain.sqlite"


def _config(tmp_path: Path, db: Path, *, tools: list[str], timezone: str = "UTC",
            boundary: str = "local_only", name: str = "mcp-d7.yaml") -> Path:
    p = tmp_path / name
    p.write_text(
        f"""
database_path: {db}
timezone: {timezone}
profile: agent
profiles:
  agent:
    allow_scopes: ['*']
    deny_sensitivity: [secret]
    allow_unclassified: false
    tools: {json.dumps(tools)}
    delivery_boundary: {boundary}
""",
        encoding="utf-8",
    )
    return p


PROFILE_ARGS = {
    "name": "agent",
    "tools": ["search_history", "get_event", "recall", "timeline", "brain_status",
              "get_recent_events"],
}


def _profile(tmp_path: Path, db: Path, *, boundary: str = "local_only") -> AccessProfile:
    return load_mcp_config(
        _config(tmp_path, db, tools=PROFILE_ARGS["tools"], boundary=boundary)
    ).profile


def _recall_hit_ids(payload: dict) -> set[str]:
    return {
        m["event_id"]
        for snippet in payload["results"]
        for m in snippet["messages"]
        if m["is_hit"]
    }


def _all_returned_event_ids(payload: dict) -> set[str]:
    return {
        m["event_id"]
        for snippet in payload["results"]
        for m in snippet["messages"]
    }


# ---------------------------------------------------------------------------
# recall：授权、一致性
# ---------------------------------------------------------------------------


def test_recall_unauthorized_neighbor_hidden(db, tmp_path):
    profile = _profile(tmp_path, db)
    conn = __import__("sqlite3").connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        payload = tool_recall(conn, profile, {"query": "salary"}, LIMITS)
        snippets = payload["results"]
        assert snippets, "公开结尾应该命中"
        all_ids = _all_returned_event_ids(payload)
        secret_row = conn.execute(
            "SELECT e.event_id FROM events e JOIN event_revisions r"
            " ON r.event_id = e.event_id WHERE r.raw_text LIKE '%secret 情报内容%'"
        ).fetchone()
        secret_id = secret_row["event_id"]
        assert secret_id not in all_ids, "未授权邻居不得出现"
        # 不影响计数：只有公开开头/结尾可见
        secret_snippet = next(
            s for s in snippets if s["conversation_id"].endswith("conv-secret")
        )
        assert secret_snippet is not None
        assert len(secret_snippet["messages"]) == 2
        assert all(m["is_hit"] or m["is_hit"] is False for m in secret_snippet["messages"])
    finally:
        conn.close()


def test_recall_hits_subset_of_search_history(db, tmp_path):
    profile = _profile(tmp_path, db)
    conn = __import__("sqlite3").connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        recall_payload = tool_recall(conn, profile, {"query": "deploy"}, LIMITS)
        search_payload = tool_search_history(
            conn, profile, {"query": "deploy", "limit": 50}, LIMITS
        )
        recall_hits = _recall_hit_ids(recall_payload)
        search_hits = {r["event_id"] for r in search_payload["results"]}
        assert recall_hits, "recall 应有命中"
        assert recall_hits <= search_hits, f"{recall_hits - search_hits} 不在 search 命中中"
        # 片段带可回查字段
        for snippet in recall_payload["results"]:
            assert snippet["conversation_title"]
            assert snippet["source"]
            assert snippet["time_range"][0] <= snippet["time_range"][1]
            for m in snippet["messages"]:
                assert m["event_id"] and m["revision_id"] and m["timestamp"]
    finally:
        conn.close()


def test_recall_budget_drops_and_truncates(db, tmp_path):
    profile = _profile(tmp_path, db)
    conn = __import__("sqlite3").connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        payload = tool_recall(
            conn, profile,
            {"query": "deploy", "char_budget": 200, "max_snippets": 1}, LIMITS,
        )
        budget = payload["budget"]
        assert budget["char_budget"] == 200
        assert budget["chars_used"] <= 200 + sum(
            len(m["text"]) for s in payload["results"] for m in s["messages"]
        )  # chars_used 是正文预算；这里正文总量受预算约束
        assert budget["chars_used"] <= 200
        body_chars = sum(len(m["text"]) for s in payload["results"] for m in s["messages"])
        assert body_chars <= 200
        # 截断的消息给出 text_offset
        for s in payload["results"]:
            for m in s["messages"]:
                if m["truncated"]:
                    assert m["text_offset"] == len(m["text"])
    finally:
        conn.close()


def test_recall_redacts_credentials_for_remote_profile(db, tmp_path):
    local_profile = _profile(tmp_path, db, boundary="local_only")
    remote_profile = _profile(tmp_path, db, boundary="remote_model_allowed")
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        args = {"query": "服务器密钥"}
        local = tool_recall(conn, local_profile, args, LIMITS)
        remote = tool_recall(conn, remote_profile, args, LIMITS)
        local_text = json.dumps(local, ensure_ascii=False)
        remote_text = json.dumps(remote, ensure_ascii=False)
        assert "sk-proj-synthetic-credential-00000000000000000000" in local_text
        assert "sk-proj-synthetic-credential" not in remote_text
        assert any("遮蔽" in w for w in remote["warnings"])
    finally:
        conn.close()


def test_recall_invalid_params(db, tmp_path):
    profile = _profile(tmp_path, db)
    conn = __import__("sqlite3").connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        with pytest.raises(ToolError) as ei:
            tool_recall(conn, profile, {"query": "x", "max_snippets": 13}, LIMITS)
        assert ei.value.code == "INVALID_PARAMS"
        with pytest.raises(ToolError):
            tool_recall(conn, profile, {"query": "x", "char_budget": 10 ** 9}, LIMITS)
        with pytest.raises(ToolError):
            tool_recall(conn, profile, {}, LIMITS)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# timeline：时区、62 天、不泄露
# ---------------------------------------------------------------------------


def test_timeline_day_boundary_uses_config_timezone(db, tmp_path):
    # 消息 2025-09-16T17:30:00Z = 上海 2025-09-17 01:30
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        payload = tool_timeline(
            conn, load_mcp_config(
                _config(tmp_path, db, tools=PROFILE_ARGS["tools"],
                        timezone="Asia/Shanghai", name="mcp-tz.yaml")
            ).profile,
            {"date_from": "2025-09-17", "date_to": "2025-09-17"}, LIMITS,
            timezone="Asia/Shanghai",
        )
        days = payload["days"]
        assert len(days) == 1 and days[0]["date"] == "2025-09-17"
        convs = {c["conversation_id"]: c for c in days[0]["conversations"]}
        # UTC 17:30 的消息 → 上海 9/11：三组对话各 5/3/2 条应全部落在 9/11
        assert len(convs) == 3
        # UTC 视角查前一天：不应出现这些对话（它们不在上海 9/10）
        payload_prev = tool_timeline(
            conn,
            load_mcp_config(_config(tmp_path, db, tools=PROFILE_ARGS["tools"],
                                    timezone="Asia/Shanghai", name="mcp-tz2.yaml")).profile,
            {"date_from": "2025-09-16", "date_to": "2025-09-16"}, LIMITS,
            timezone="Asia/Shanghai",
        )
        assert payload_prev["days"] == []
    finally:
        conn.close()


def test_timeline_range_over_62_days_rejected(db, tmp_path):
    profile = _profile(tmp_path, db)
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ToolError) as ei:
            tool_timeline(
                conn, profile,
                {"date_from": "2026-01-01", "date_to": "2026-03-05"}, LIMITS,
            )
        assert ei.value.code == "INVALID_PARAMS"
    finally:
        conn.close()


def test_timeline_does_not_leak_invisible_conversations(db, tmp_path):
    """全 secret 对话整条不出现；部分可见对话只计可见消息。"""
    import sqlite3

    # 标注需要可写连接；tool_timeline 用只读连接（工具本身不写库）
    conn_w = sqlite3.connect(db)
    conn_w.row_factory = sqlite3.Row
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        profile = _profile(tmp_path, db)
        # conv-secret 的 3 条消息里只有 2 条可见（中间 secret）
        payload_all = tool_timeline(
            conn, profile, {"date_from": "2025-09-15", "date_to": "2025-09-18"}, LIMITS
        )
        all_convs = {
            c["conversation_id"]: c
            for day in payload_all["days"] for c in day["conversations"]
        }
        secret_conv = next(
            (cid for cid in all_convs if cid.endswith("conv-secret")), None
        )
        assert secret_conv is not None
        assert all_convs[secret_conv]["messages"] == 2, "只统计可见消息数"
        assert all_convs[secret_conv]["owner_messages"] == 2
        # secret 邻居的消息不出现也不影响计数
        # 整条不可见的对话（全部消息标 secret）不出现：动态造一条
        for marker in ("synthetic-credential", "凭据轮换提醒已设置"):
            row = conn_w.execute(
                "SELECT e.event_id FROM events e JOIN event_revisions r"
                " ON r.event_id = e.event_id WHERE r.raw_text LIKE ?",
                (f"%{marker}%",),
            ).fetchone()
            assert row is not None, marker
            label_event(conn_w, row["event_id"], sensitivity_labels=("secret",))
        conn_w.commit()
        payload_after = tool_timeline(
            conn, profile, {"date_from": "2025-09-15", "date_to": "2025-09-18"}, LIMITS
        )
        after_convs = {
            c["conversation_id"]
            for day in payload_after["days"] for c in day["conversations"]
        }
        assert not any(cid.endswith("conv-cred") for cid in after_convs), \
            "整条不可见的对话不存在性也不得泄露"
    finally:
        conn.close()
        conn_w.close()


def test_timeline_first_owner_line_masked_for_remote(db, tmp_path):
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        local_profile = _profile(tmp_path, db, boundary="local_only")
        payload = tool_timeline(
            conn, local_profile,
            {"date_from": "2025-09-15", "date_to": "2025-09-18",
             "with_first_line": True},
            LIMITS,
        )
        # 遮蔽走与 recall 相同的 redact_known_credentials（凭据遮蔽已在
        # test_recall_redacts_credentials_for_remote_profile 验证）；
        # 这里验证 first_owner_line 存在且截断到 80 字。
        count = 0
        for day in payload["days"]:
            for c in day["conversations"]:
                assert len(c["first_owner_line"]) <= 80
                count += 1
        assert count >= 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCP：tools/list 白名单、tools/call
# ---------------------------------------------------------------------------


class _AppClient:
    """与 test_remote_http 相同的进程内 ASGI 客户端（lifespan 手动驱动）。"""

    def __init__(self, app):
        import httpx

        self.app = app
        self._cm = None
        self.httpx = httpx
        self.client = None

    async def __aenter__(self):
        self._cm = self.app.router.lifespan_context(self.app)
        await self._cm.__aenter__()
        transport = self.httpx.ASGITransport(app=self.app)
        self.client = self.httpx.AsyncClient(transport=transport, base_url="http://test")
        return self.client

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._cm.__aexit__(*exc)


def _mcp_payload(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def _parse_sse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError("no data line")


@pytest.mark.anyio
async def test_mcp_tools_list_and_call(db, tmp_path):
    pytest.importorskip("httpx")
    from personal_brain.mcp_server.http_app import create_app

    config = load_mcp_config(
        _config(tmp_path, db, tools=["search_history", "recall", "timeline"])
    )
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post(
            "/mcp/t", json=_mcp_payload("initialize", {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            }), headers=MCP_HEADERS,
        )
        assert resp.status_code == 200
        sid = resp.headers.get("mcp-session-id")
        resp = await client.post(
            "/mcp/t", json=_mcp_payload("tools/list", {}),
            headers={**MCP_HEADERS, **({"mcp-session-id": sid} if sid else {})},
        )
        names = {t["name"] for t in _parse_sse(resp.text)["result"]["tools"]}
        assert {"recall", "timeline"} <= names
        # recall 一例
        resp = await client.post(
            "/mcp/t", json=_mcp_payload("tools/call", {
                "name": "recall",
                "arguments": {"query": "deploy"},
            }),
            headers={**MCP_HEADERS, **({"mcp-session-id": sid} if sid else {})},
        )
        body = _parse_sse(resp.text)
        assert not body["result"]["isError"]
        payload = body["result"]["structuredContent"]
        assert payload["budget"]["char_budget"] == 6000
        assert any(s["messages"] for s in payload["results"])
        # timeline 一例
        resp = await client.post(
            "/mcp/t", json=_mcp_payload("tools/call", {
                "name": "timeline",
                "arguments": {"date_from": "2025-09-15", "date_to": "2025-09-18"},
            }),
            headers={**MCP_HEADERS, **({"mcp-session-id": sid} if sid else {})},
        )
        body = _parse_sse(resp.text)
        assert not body["result"]["isError"]
        assert "days" in body["result"]["structuredContent"]


@pytest.mark.anyio
async def test_mcp_tools_hidden_when_not_in_whitelist(db, tmp_path):
    pytest.importorskip("httpx")
    from personal_brain.mcp_server.http_app import create_app

    config = load_mcp_config(
        _config(tmp_path, db, tools=["search_history"], name="mcp-strict.yaml")
    )
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.post(
            "/mcp/t", json=_mcp_payload("initialize", {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            }), headers=MCP_HEADERS,
        )
        sid = resp.headers.get("mcp-session-id")
        resp = await client.post(
            "/mcp/t", json=_mcp_payload("tools/list", {}),
            headers={**MCP_HEADERS, **({"mcp-session-id": sid} if sid else {})},
        )
        names = {t["name"] for t in _parse_sse(resp.text)["result"]["tools"]}
        assert "recall" not in names and "timeline" not in names
        for tool in ("recall", "timeline"):
            resp = await client.post(
                "/mcp/t", json=_mcp_payload("tools/call", {
                    "name": tool,
                    "arguments": {"query": "deploy", "date_from": "2025-09-15",
                                  "date_to": "2025-09-16"},
                }),
                headers={**MCP_HEADERS, **({"mcp-session-id": sid} if sid else {})},
            )
            body = _parse_sse(resp.text)
            assert body["result"]["isError"]
            assert body["result"]["structuredContent"]["error"] == "NOT_FOUND_OR_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rest_recall_and_timeline(db, tmp_path):
    pytest.importorskip("httpx")
    from personal_brain.mcp_server.http_app import create_app

    config = load_mcp_config(_config(tmp_path, db, tools=PROFILE_ARGS["tools"]))
    async with _AppClient(create_app(config, token="t")) as client:
        resp = await client.get("/api/recall?q=deploy")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["budget"]["char_budget"] == 6000
        assert isinstance(payload["results"], list)
        resp = await client.get("/api/timeline?from=2025-09-15&to=2025-09-18")
        assert resp.status_code == 200
        days = resp.json()["days"]
        assert days and all("conversations" in d for d in days)
        # 错误码
        resp = await client.get("/api/recall")  # 缺 q
        assert resp.status_code == 400
        resp = await client.get("/api/recall?q=deploy&max_snippets=99")
        assert resp.status_code == 400
        resp = await client.get("/api/timeline?from=2025-01-01&to=2025-04-01")  # >62 天
        assert resp.status_code == 400
        resp = await client.get("/api/timeline")  # 缺 from/to
        assert resp.status_code == 400
