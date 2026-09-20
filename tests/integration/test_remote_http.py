"""D-3 远程接入集成测试：`httpx.ASGITransport` 进程内跑，不监听端口、不出网。

覆盖：MCP over HTTP（initialize/tools/list/tools/call）、能力 URL token 校验、
四个 REST 端点的正常路径与错误码、无权/不存在响应体逐字节相同、限速 429、
Cloudflare Access JWT 校验（测试内置 JWKS，不出网）、请求日志不含 query。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from corpus import retrieval_corpus_conversations
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from synthetic import write_zip

from personal_brain.history.db import connect
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.mcp_server.http_app import AccessJwtVerifier, RateLimiter, create_app
from personal_brain.policy.labels import label_event, label_source
from personal_brain.policy.profiles import load_mcp_config

TOKEN = secrets.token_hex(32)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# 语料与配置
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def labeled_db(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("remote_http")
    conn = connect(tmp / "brain.sqlite")
    arc = tmp / "archives"
    arc.mkdir()
    importer = ChatGPTImporter(conn, arc)
    zp = write_zip(retrieval_corpus_conversations(), tmp / "corpus.zip")
    importer.import_archive(zp, "testuser")
    source_id = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
    label_source(conn, source_id, scope_labels=("career",))
    sec01 = conn.execute(
        "SELECT event_id FROM events WHERE event_id LIKE '%:sec01'"
    ).fetchone()["event_id"]
    label_event(conn, sec01, scope_labels=("home",), sensitivity_labels=("secret",))
    conn.close()
    return tmp / "brain.sqlite"


def _write_config(tmp_path: Path, db: Path, *, access_aud: str | None = None,
                   access_team_domain: str | None = None, max_scan: int | None = None,
                   name: str = "mcp.yaml") -> Path:
    p = tmp_path / name
    remote_block = ""
    if access_aud:
        remote_block = f"""
remote:
  access_aud: {access_aud}
  access_team_domain: {access_team_domain}
"""
    search_block = f"search:\n  max_scan: {max_scan}\n" if max_scan is not None else ""
    p.write_text(
        f"""
database_path: {db}
profile: agent
profiles:
  agent:
    allow_scopes: ['*']
    deny_sensitivity: [secret]
    allow_unclassified: false
    tools: [brain_status, search_history, get_recent_events, get_event]
    delivery_boundary: remote_model_allowed
{remote_block}{search_block}""",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def config(tmp_path, labeled_db):
    return load_mcp_config(_write_config(tmp_path, labeled_db))


@pytest.fixture()
def broad_query_config(tmp_path, labeled_db):
    """max_scan=1：单字/短词回退扫描必然超限，用来真实触发 QUERY_TOO_BROAD。"""
    return load_mcp_config(
        _write_config(tmp_path, labeled_db, max_scan=1, name="mcp-broad.yaml")
    )


def _sec01_and_missing_ids(labeled_db: Path) -> tuple[str, str]:
    conn = connect(labeled_db)
    try:
        sec01 = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:sec01'"
        ).fetchone()["event_id"]
    finally:
        conn.close()
    return sec01, "chatgpt:testuser:missing-event-id"


# ---------------------------------------------------------------------------
# 测试用 Cloudflare Access JWKS（不出网：RSA 密钥对在进程内生成）
# ---------------------------------------------------------------------------


class _FakeAccess:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-kid-1"
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk["kid"] = self.kid
        jwk["alg"] = "RS256"
        jwk["use"] = "sig"
        self.jwks = {"keys": [jwk]}

    def sign(self, aud: str, *, kid: str | None = None, key=None) -> str:
        payload = {"aud": aud, "exp": time.time() + 300, "email": "owner@example.com"}
        return jwt.encode(
            payload, key or self.key, algorithm="RS256",
            headers={"kid": kid if kid is not None else self.kid},
        )


@pytest.fixture()
def fake_access() -> _FakeAccess:
    return _FakeAccess()


# ---------------------------------------------------------------------------
# 应用工厂 + httpx.ASGITransport 客户端（手动驱动 lifespan）
# ---------------------------------------------------------------------------


class _AppClient:
    """包装 create_app + ASGITransport + lifespan，不监听端口、不出网。"""

    def __init__(self, app):
        self.app = app
        self._cm = None
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        self._cm = self.app.router.lifespan_context(self.app)
        await self._cm.__aenter__()
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")
        return self.client

    async def __aexit__(self, *exc_info) -> None:
        assert self.client is not None
        await self.client.aclose()
        await self._cm.__aexit__(*exc_info)


def _make_app(config, *, access_verifier=None, rate_limiter=None):
    return create_app(
        config, token=TOKEN,
        rate_limiter=rate_limiter or RateLimiter(),
        access_verifier=access_verifier,
    )


MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def _parse_sse_json(body: str) -> dict:
    """取 SSE 响应中 'data: ' 行的 JSON payload。"""
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no data line in SSE body: {body!r}")


# ---------------------------------------------------------------------------
# MCP over HTTP
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestMcpOverHttp:
    async def test_initialize_list_and_call(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            init = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "pytest", "version": "0"}},
            }
            r = await client.post(f"/mcp/{TOKEN}", json=init, headers=MCP_HEADERS)
            assert r.status_code == 200
            result = _parse_sse_json(r.text)["result"]
            assert result["serverInfo"]["name"] == "personal-brain"

            list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            r = await client.post(f"/mcp/{TOKEN}", json=list_req, headers=MCP_HEADERS)
            names = [t["name"] for t in _parse_sse_json(r.text)["result"]["tools"]]
            # 与 stdio 工具列表一致（同一 profile.tools 配置）
            assert names == [
                "brain_status", "search_history", "get_recent_events", "get_event",
            ]

            call_req = {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "search_history", "arguments": {"query": "职业"}},
            }
            r = await client.post(f"/mcp/{TOKEN}", json=call_req, headers=MCP_HEADERS)
            payload = _parse_sse_json(r.text)["result"]["structuredContent"]
            assert payload["content_trust"] == "untrusted_archive"
            assert len(payload["results"]) == 5

    async def test_wrong_token_is_404(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get(f"/mcp/{'0' * len(TOKEN)}", headers=MCP_HEADERS)
            assert r.status_code == 404

    async def test_missing_token_is_404(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/mcp", headers=MCP_HEADERS)
            assert r.status_code in (404, 307)
            r = await client.get("/mcp/", headers=MCP_HEADERS)
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# REST 端点
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestRestEndpoints:
    async def test_status(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/api/status")
            assert r.status_code == 200
            body = r.json()
            assert body["health"] == "ok"
            assert body["coverage"]["known_start"] is not None

    async def test_search_happy_path(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/api/search", params={"q": "职业"})
            assert r.status_code == 200
            body = r.json()
            assert len(body["results"]) == 5

    async def test_search_hybrid_and_filters(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get(
                "/api/search",
                params={"q": "职业", "mode": "exact", "speaker": "owner", "limit": 2},
            )
            assert r.status_code == 200
            body = r.json()
            assert len(body["results"]) <= 2
            assert all(x["speaker_type"] == "owner" for x in body["results"])

    async def test_search_missing_query_is_400(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/api/search")
            assert r.status_code == 400
            assert r.json()["error"] == "INVALID_PARAMS"

    async def test_search_bad_limit_is_400(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/api/search", params={"q": "职业", "limit": "abc"})
            assert r.status_code == 400
            assert r.json()["error"] == "INVALID_PARAMS"

    async def test_search_query_too_broad_is_422(self, broad_query_config):
        app = _make_app(broad_query_config)
        async with _AppClient(app) as client:
            r = await client.get("/api/search", params={"q": "安"})
            assert r.status_code == 422
            assert r.json()["error"] == "QUERY_TOO_BROAD"

    async def test_recent(self, config):
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/api/recent", params={"limit": 5})
            assert r.status_code == 200
            assert "results" in r.json()

    async def test_event_happy_path(self, config, labeled_db):
        app = _make_app(config)
        conn = connect(labeled_db)
        job01 = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:job01'"
        ).fetchone()["event_id"]
        conn.close()
        async with _AppClient(app) as client:
            r = await client.get(f"/api/event/{job01}")
            assert r.status_code == 200
            assert r.json()["results"][0]["event_id"] == job01

    async def test_event_not_found_and_forbidden_are_byte_identical(self, config, labeled_db):
        """§REST：无权 event 与不存在 event 响应体逐字节相同。"""
        sec01, missing = _sec01_and_missing_ids(labeled_db)
        app = _make_app(config)
        async with _AppClient(app) as client:
            r_forbidden = await client.get(f"/api/event/{sec01}")
            r_missing = await client.get(f"/api/event/{missing}")
        assert r_forbidden.status_code == r_missing.status_code == 404
        assert r_forbidden.content == r_missing.content

    async def test_internal_error_does_not_leak_details(self, config, monkeypatch):
        from personal_brain.mcp_server import http_app as mod

        def boom(*a, **kw):
            raise RuntimeError("some internal detail: secret_marker_zzz")

        monkeypatch.setattr(mod, "run_with_epoch_retry", boom)
        app = _make_app(config)
        async with _AppClient(app) as client:
            r = await client.get("/api/status")
            assert r.status_code == 500
            assert r.json() == {"error": "INTERNAL_ERROR"}
            assert "secret_marker_zzz" not in r.text


# ---------------------------------------------------------------------------
# 限速
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestRateLimit:
    async def test_rate_limit_triggers_429(self, config):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        app = _make_app(config, rate_limiter=limiter)
        async with _AppClient(app) as client:
            statuses = []
            for _ in range(5):
                r = await client.get("/api/status")
                statuses.append(r.status_code)
            assert statuses.count(429) >= 1
            assert statuses[:3] == [200, 200, 200]


# ---------------------------------------------------------------------------
# Cloudflare Access JWT 校验
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestAccessJwt:
    async def test_missing_header_is_401(self, config, fake_access):
        verifier = AccessJwtVerifier(
            "my-aud", "team.cloudflareaccess.com", jwks_fetcher=lambda: fake_access.jwks
        )
        app = _make_app(config, access_verifier=verifier)
        async with _AppClient(app) as client:
            r = await client.get("/api/status")
            assert r.status_code == 401

    async def test_valid_token_is_accepted(self, config, fake_access):
        verifier = AccessJwtVerifier(
            "my-aud", "team.cloudflareaccess.com", jwks_fetcher=lambda: fake_access.jwks
        )
        app = _make_app(config, access_verifier=verifier)
        token = fake_access.sign("my-aud")
        async with _AppClient(app) as client:
            r = await client.get("/api/status", headers={"Cf-Access-Jwt-Assertion": token})
            assert r.status_code == 200

    async def test_forged_signature_is_401(self, config, fake_access):
        verifier = AccessJwtVerifier(
            "my-aud", "team.cloudflareaccess.com", jwks_fetcher=lambda: fake_access.jwks
        )
        app = _make_app(config, access_verifier=verifier)
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = fake_access.sign("my-aud", key=other_key)  # 签名密钥与 JWKS 公钥不匹配
        async with _AppClient(app) as client:
            r = await client.get("/api/status", headers={"Cf-Access-Jwt-Assertion": forged})
            assert r.status_code == 401

    async def test_wrong_audience_is_401(self, config, fake_access):
        verifier = AccessJwtVerifier(
            "my-aud", "team.cloudflareaccess.com", jwks_fetcher=lambda: fake_access.jwks
        )
        app = _make_app(config, access_verifier=verifier)
        token = fake_access.sign("some-other-aud")
        async with _AppClient(app) as client:
            r = await client.get("/api/status", headers={"Cf-Access-Jwt-Assertion": token})
            assert r.status_code == 401

    async def test_mcp_path_bypasses_access_check(self, config, fake_access):
        """MCP 端点走能力 URL token，不受 Access 校验影响（客户端无法加自定义 header）。"""
        verifier = AccessJwtVerifier(
            "my-aud", "team.cloudflareaccess.com", jwks_fetcher=lambda: fake_access.jwks
        )
        app = _make_app(config, access_verifier=verifier)
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "0"}},
        }
        async with _AppClient(app) as client:
            r = await client.post(f"/mcp/{TOKEN}", json=init, headers=MCP_HEADERS)
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# 请求日志不含 query 内容
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestLogging:
    async def test_query_not_logged(self, config, caplog):
        marker = "very_distinctive_query_marker_職業秘密"
        app = _make_app(config)
        with caplog.at_level(logging.INFO, logger="personal_brain.remote_access"):
            async with _AppClient(app) as client:
                await client.get("/api/search", params={"q": marker})
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert marker not in joined
        assert "path=/api/search" in joined

    async def test_mcp_token_not_logged(self, config, caplog):
        """MCP 路径段本身是能力 URL token；请求日志必须遮蔽，不能原样写路径。"""
        app = _make_app(config)
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "0"}},
        }
        with caplog.at_level(logging.INFO, logger="personal_brain.remote_access"):
            async with _AppClient(app) as client:
                await client.post(f"/mcp/{TOKEN}", json=init, headers=MCP_HEADERS)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in joined
        assert "path=/mcp/<redacted>" in joined


# ---------------------------------------------------------------------------
# 没有 remote 可选组时的启动体验（子进程隔离，不污染主进程 sys.modules）
# ---------------------------------------------------------------------------


def test_missing_remote_extra_gives_clear_message_not_traceback(tmp_path, labeled_db):
    """没有 remote 可选组（此处用 uvicorn 模拟未安装）→ 清楚提示而不是 traceback。

    ``starlette`` 是 mcp>=2.1.1 自身的硬依赖（连 stdio 也要 import），无法在保留
    ``mcp`` 可用的前提下单独模拟缺失；``uvicorn`` 只被 HTTP transport 需要，是
    可以真实复现的缺失场景（见 docs/remote-access.md 的已知问题说明）。
    """
    config_path = _write_config(tmp_path, labeled_db)
    script = (
        "import sys\n"
        "sys.modules['uvicorn'] = None\n"
        "from personal_brain.mcp_server.server import main\n"
        "sys.exit(main(['--config', sys.argv[1], '--transport', 'http']))\n"
    )
    env = dict(os.environ)
    env["BRAIN_MCP_TOKEN"] = secrets.token_hex(32)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "remote" in proc.stderr


def test_missing_token_env_gives_clear_message(tmp_path, labeled_db):
    config_path = _write_config(tmp_path, labeled_db)
    env = dict(os.environ)
    env.pop("BRAIN_MCP_TOKEN", None)
    proc = subprocess.run(
        [sys.executable, "-m", "personal_brain.mcp_server.server",
         "--config", str(config_path), "--transport", "http"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "BRAIN_MCP_TOKEN" in proc.stderr


def test_rest_error_status_map_matches_spec():
    from personal_brain.mcp_server.http_app import _STATUS_BY_CODE

    assert _STATUS_BY_CODE["INVALID_PARAMS"] == 400
    assert _STATUS_BY_CODE["NOT_FOUND_OR_NOT_ALLOWED"] == 404
    assert _STATUS_BY_CODE["QUERY_TOO_BROAD"] == 422


def test_public_host_requires_explicit_flag(tmp_path, labeled_db):
    config_path = _write_config(tmp_path, labeled_db)
    env = dict(os.environ)
    env["BRAIN_MCP_TOKEN"] = secrets.token_hex(32)
    proc = subprocess.run(
        [sys.executable, "-m", "personal_brain.mcp_server.server",
         "--config", str(config_path), "--transport", "http", "--host", "0.0.0.0"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "i-know-this-is-public" in proc.stderr
