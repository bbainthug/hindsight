"""D-3 远程接入：HTTP transport（MCP Streamable HTTP + 只读 REST + 静态查询页）。

只在 ``personal-brain-mcp --transport http`` 时被导入（stdio 默认路径不受影响）。

- 复用 ``server.build_server`` 与 ``service.tool_*``，不复制工具逻辑（§设计要求）。
- MCP 路径走能力 URL token（路径不匹配一律 404，不区分"存在但无权"）。
- REST/页面默认信任前置的 Cloudflare Access；配置了 ``access_aud`` +
  ``access_team_domain`` 才校验 ``Cf-Access-Jwt-Assertion``（JWKS 缓存 1 小时）。
- 所有响应 ``Cache-Control: no-store``；不设 CORS 头（页面与 API 同源）。
- 简单限速：每来源 IP 60 请求/分钟。
- 请求日志只记录路径、状态码、耗时；不记录 query 内容与返回正文。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

try:
    import asyncio

    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
    from starlette.types import ASGIApp, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover - 走友好提示而非 traceback
    raise ImportError(
        "HTTP transport 需要可选依赖组 remote：uv sync --extra remote"
        "（或 pip install '.[remote]'）"
    ) from exc

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from personal_brain.history.db import fts_rowid
from personal_brain.mcp_server.server import build_server
from personal_brain.mcp_server.service import (
    ToolError,
    run_with_epoch_retry,
    tool_brain_status,
    tool_get_event,
    tool_get_recent_events,
    tool_search_history,
)
from personal_brain.policy.profiles import McpServerConfig
from personal_brain.retrieval.search import SearchLimits

logger = logging.getLogger("personal_brain.remote_access")

STATIC_DIR = Path(__file__).parent / "static"
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_REQUESTS = 60
JWKS_CACHE_SECONDS = 3600.0
DEFAULT_POOL_SIZE = 4


# ---------------------------------------------------------------------------
# 只读连接池（REST 按请求借还；MCP 复用 build_server 的单一专属连接）
# ---------------------------------------------------------------------------


def _connect_readonly_shared(db_path: Path) -> sqlite3.Connection:
    """只读连接：允许跨线程借用（按请求借还，不跨线程共享同一实例）。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("fts_rowid", 1, fts_rowid)
    conn.execute("PRAGMA query_only=ON")
    return conn


class ReadonlyConnectionPool:
    """按请求借还的只读 SQLite 连接池。"""

    def __init__(self, db_path: Path, size: int = DEFAULT_POOL_SIZE) -> None:
        self._queue: asyncio.Queue[sqlite3.Connection] = asyncio.Queue()
        self._all: list[sqlite3.Connection] = []
        for _ in range(max(1, size)):
            conn = _connect_readonly_shared(db_path)
            self._all.append(conn)
            self._queue.put_nowait(conn)

    async def acquire(self) -> sqlite3.Connection:
        return await self._queue.get()

    def release(self, conn: sqlite3.Connection) -> None:
        self._queue.put_nowait(conn)

    def close(self) -> None:
        for conn in self._all:
            conn.close()


# ---------------------------------------------------------------------------
# 简单限速（每来源 IP 60 请求/分钟）
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(
        self,
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        if not bucket:
            self._hits.pop(key, None)
            bucket = self._hits[key]
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


# ---------------------------------------------------------------------------
# Cloudflare Access JWT 校验（可选；配置了 aud/team_domain 才启用）
# ---------------------------------------------------------------------------


class AccessJwtVerifier:
    """校验 ``Cf-Access-Jwt-Assertion``（RS256，JWKS 缓存 1 小时，不出网测试用注入 fetcher）。"""

    def __init__(
        self,
        aud: str,
        team_domain: str,
        *,
        jwks_fetcher: Callable[[], dict] | None = None,
    ) -> None:
        self._aud = aud
        self._certs_url = f"https://{team_domain}/cdn-cgi/access/certs"
        self._fetcher = jwks_fetcher or self._fetch_jwks
        self._cache: dict | None = None
        self._cache_at: float = 0.0

    def _fetch_jwks(self) -> dict:  # pragma: no cover - 真实网络路径，测试用注入替代
        import urllib.request

        with urllib.request.urlopen(self._certs_url, timeout=5) as resp:  # noqa: S310
            return json.loads(resp.read())

    def _jwks(self, *, force_refresh: bool = False) -> dict:
        now = time.monotonic()
        if force_refresh or self._cache is None or now - self._cache_at > JWKS_CACHE_SECONDS:
            self._cache = self._fetcher()
            self._cache_at = now
        return self._cache

    def verify(self, token: str) -> bool:
        import jwt
        from jwt.algorithms import RSAAlgorithm

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return False
        kid = header.get("kid")
        jwks = self._jwks()
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key_data is None:
            # kid 未命中缓存：可能是密钥轮换，强制刷新一次再判定
            jwks = self._jwks(force_refresh=True)
            key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
            if key_data is None:
                return False
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
            if not isinstance(public_key, RSAPublicKey):
                return False
            jwt.decode(token, key=public_key, algorithms=["RS256"], audience=self._aud)
        except jwt.InvalidTokenError:
            return False
        return True


# ---------------------------------------------------------------------------
# 安全中间件：限速 + Access 校验 + Cache-Control + 请求日志（不记录 query/正文）
# ---------------------------------------------------------------------------


def _log_path(path: str) -> str:
    """MCP 路径段本身就是能力 URL token；日志里必须遮蔽，否则等于把密钥写进日志。"""
    if path.startswith("/mcp/"):
        return "/mcp/<redacted>"
    return path


class SecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_limiter: RateLimiter,
        access_verifier: AccessJwtVerifier | None,
    ) -> None:
        self.app = app
        self.rate_limiter = rate_limiter
        self.access_verifier = access_verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        path = scope["path"]
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        client_ip = headers.get("cf-connecting-ip") or (scope.get("client") or ("", 0))[0]

        if not self.rate_limiter.allow(client_ip):
            resp = JSONResponse({"error": "RATE_LIMITED"}, status_code=429)
            await self._respond(resp, scope, receive, send, path, started)
            return

        if self.access_verifier is not None and not path.startswith("/mcp/"):
            token = headers.get("cf-access-jwt-assertion", "")
            if not token or not self.access_verifier.verify(token):
                resp = JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
                await self._respond(resp, scope, receive, send, path, started)
                return

        status_holder: dict[str, int] = {}

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            status = status_holder.get("status", "-")
            logger.info("path=%s status=%s duration_ms=%.1f", _log_path(path), status, duration_ms)

    @staticmethod
    async def _respond(
        response: Response, scope: Scope, receive: Receive, send: Send, path: str, started: float
    ) -> None:
        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        await response(scope, receive, send_wrapper)
        duration_ms = (time.monotonic() - started) * 1000
        logger.info(
            "path=%s status=%s duration_ms=%.1f",
            _log_path(path), response.status_code, duration_ms,
        )


# ---------------------------------------------------------------------------
# MCP 能力 URL 网关：路径 token 不匹配一律 404
# ---------------------------------------------------------------------------


class McpTokenGate:
    def __init__(self, session_manager: StreamableHTTPSessionManager, token: str) -> None:
        self._session_manager = session_manager
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        # Starlette's Mount does not rewrite scope["path"] for the sub-app in
        # this SDK version; strip the "/mcp" mount prefix via root_path ourselves.
        root_path = scope.get("root_path", "")
        full_path = scope["path"]
        if root_path and full_path.startswith(root_path):
            relative = full_path[len(root_path):]
        else:
            relative = full_path
        candidate = relative[1:] if relative.startswith("/") else relative
        if not hmac.compare_digest(candidate, self._token):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        await self._session_manager.handle_request(scope, receive, send)


# ---------------------------------------------------------------------------
# REST 错误映射（§REST 表：INVALID_PARAMS→400、NOT_FOUND/无权→404、
# QUERY_TOO_BROAD→422、其他→500 且不带内部信息）
# ---------------------------------------------------------------------------

_STATUS_BY_CODE = {
    "INVALID_PARAMS": 400,
    "NOT_FOUND_OR_NOT_ALLOWED": 404,
    "QUERY_TOO_BROAD": 422,
}


def _tool_error_response(exc: ToolError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(exc.code, 500)
    if status == 500:
        return JSONResponse({"error": "INTERNAL_ERROR"}, status_code=500)
    body: dict[str, str] = {"error": exc.code}
    if exc.message:
        body["message"] = exc.message
    return JSONResponse(body, status_code=status)


def _internal_error_response(context: str, exc: Exception) -> JSONResponse:
    # 服务端记录异常类型定位问题；响应体不带内部信息（不泄露正文/堆栈）。
    logger.error("internal error in %s: %s", context, type(exc).__name__)
    return JSONResponse({"error": "INTERNAL_ERROR"}, status_code=500)


def _parse_int_param(request: Request, name: str) -> int | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ToolError("INVALID_PARAMS", f"{name} 必须是整数") from exc


# ---------------------------------------------------------------------------
# Starlette 应用装配
# ---------------------------------------------------------------------------


def create_app(
    config: McpServerConfig,
    *,
    token: str,
    pool_size: int = DEFAULT_POOL_SIZE,
    rate_limiter: RateLimiter | None = None,
    access_verifier: AccessJwtVerifier | None = None,
) -> Starlette:
    profile = config.profile
    limits = SearchLimits(
        default_limit=int(config.search_limits.get("default_limit", 10)),
        max_limit=int(config.search_limits.get("max_limit", 50)),
        snippet_chars=int(config.search_limits.get("snippet_chars", 400)),
        max_text_chars=int(config.search_limits.get("max_text_chars", 8000)),
        max_scan=int(config.search_limits.get("max_scan", 50_000)),
    )

    if access_verifier is not None:
        # 显式注入（测试用内置 JWKS，不出网）优先于配置派生。
        effective_verifier = access_verifier
    elif config.access_aud and config.access_team_domain:
        effective_verifier = AccessJwtVerifier(config.access_aud, config.access_team_domain)
    else:
        effective_verifier = None
        logger.warning(
            "REMOTE ACCESS WARNING: remote.access_aud/access_team_domain 未设置，"
            "REST 与查询页不校验 Cloudflare Access；任何能连接到该端口的请求都会被当作已认证。"
            "生产部署必须放在 Cloudflare Access（或等效前置身份层）之后。"
        )

    mcp_conn = _connect_readonly_shared(config.db_path)
    mcp_server = build_server(config, mcp_conn)
    session_manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)

    pool = ReadonlyConnectionPool(config.db_path, size=pool_size)

    async def api_status(request: Request) -> Response:
        conn = await pool.acquire()
        try:
            payload = run_with_epoch_retry(tool_brain_status, conn, profile, limits)
        except ToolError as exc:
            return _tool_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _internal_error_response("api_status", exc)
        finally:
            pool.release(conn)
        return JSONResponse(payload)

    async def api_search(request: Request) -> Response:
        qp = request.query_params
        args: dict[str, Any] = {"query": qp.get("q")}
        for src, dst in (
            ("mode", "mode"),
            ("match_mode", "match_mode"),
            ("speaker", "speaker_type"),
            ("from", "date_from"),
            ("to", "date_to"),
            ("source", "source_id"),
            ("cursor", "cursor"),
        ):
            value = qp.get(src)
            if value is not None:
                args[dst] = value
        try:
            limit = _parse_int_param(request, "limit")
        except ToolError as exc:
            return _tool_error_response(exc)
        if limit is not None:
            args["limit"] = limit
        conn = await pool.acquire()
        try:
            payload = run_with_epoch_retry(
                tool_search_history, conn, profile, args, limits, config.timezone,
                semantic_config=config.semantic,
            )
        except ToolError as exc:
            return _tool_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _internal_error_response("api_search", exc)
        finally:
            pool.release(conn)
        return JSONResponse(payload)

    async def api_recent(request: Request) -> Response:
        args: dict[str, Any] = {}
        try:
            limit = _parse_int_param(request, "limit")
        except ToolError as exc:
            return _tool_error_response(exc)
        if limit is not None:
            args["limit"] = limit
        cursor = request.query_params.get("cursor")
        if cursor is not None:
            args["cursor"] = cursor
        conn = await pool.acquire()
        try:
            payload = run_with_epoch_retry(tool_get_recent_events, conn, profile, args, limits)
        except ToolError as exc:
            return _tool_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _internal_error_response("api_recent", exc)
        finally:
            pool.release(conn)
        return JSONResponse(payload)

    async def api_event(request: Request) -> Response:
        args: dict[str, Any] = {"event_id": request.path_params["event_id"]}
        try:
            text_offset = _parse_int_param(request, "text_offset")
            context_radius = _parse_int_param(request, "context_radius")
        except ToolError as exc:
            return _tool_error_response(exc)
        if text_offset is not None:
            args["text_offset"] = text_offset
        if context_radius is not None:
            args["context_radius"] = context_radius
        conn = await pool.acquire()
        try:
            payload = run_with_epoch_retry(tool_get_event, conn, profile, args, limits)
        except ToolError as exc:
            return _tool_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return _internal_error_response("api_event", exc)
        finally:
            pool.release(conn)
        return JSONResponse(payload)

    async def index_page(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html")

    routes = [
        Mount("/mcp", app=McpTokenGate(session_manager, token)),
        Route("/api/status", api_status, methods=["GET"]),
        Route("/api/search", api_search, methods=["GET"]),
        Route("/api/recent", api_recent, methods=["GET"]),
        Route("/api/event/{event_id}", api_event, methods=["GET"]),
        Route("/", index_page, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR)),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            try:
                yield
            finally:
                pool.close()
                mcp_conn.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(
        SecurityMiddleware,
        rate_limiter=rate_limiter or RateLimiter(),
        access_verifier=effective_verifier,
    )
    return app


def run_http(
    config: McpServerConfig, *, host: str, port: int, token: str,
    pool_size: int = DEFAULT_POOL_SIZE,
) -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    app = create_app(config, token=token, pool_size=pool_size)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
