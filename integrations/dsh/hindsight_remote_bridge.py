#!/usr/bin/env python3
"""Stdio bridge between DeepSeek Harness and the remote Hindsight MCP endpoint.

DSH's ``@deepseek-ai/dsh-mcp-client`` supports Streamable HTTP, but the Node.js
runtime ignores system proxy settings while the Hindsight cloud endpoint is only
reachable through the local HTTP proxy. This bridge therefore speaks MCP over
stdio on the DSH side and relays every message to the cloud endpoint with
``urllib`` through the proxy.

Protocol notes:

- stdin carries newline-delimited JSON-RPC messages (one object per line);
- responses are written to stdout as newline-delimited JSON, one per line;
- notifications (messages without an ``id``) are forwarded upstream but never
  produce a reply on stdout;
- the upstream answer is either a JSON document or an SSE stream; both are
  parsed, and response objects whose ``id`` matches the request are relayed;
- the access token is read from a local env file only and must never appear in
  stdout, stderr, or diagnostics; error messages use ``<token>`` instead;
- failures are reported as standard JSON-RPC error responses so the stdio loop
  never crashes the process.

Standard library only, so the script runs inside any DSH profile venv.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REMOTE_BASE = "https://brain.bbainthug.tech/mcp/"
DEFAULT_TOKEN_PATH = os.path.expanduser("~/.config/hindsight/remote.env")
DEFAULT_PROXY = "http://127.0.0.1:7891"
PROXY_ENV_KEY = "HINDSIGHT_PROXY"
TOKEN_ENV_KEY = "BRAIN_MCP_TOKEN"
REQUEST_TIMEOUT_S = 60.0
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_VERSION_HEADER = "Mcp-Protocol-Version"
ACCEPT_HEADER = "application/json, text/event-stream"
# Cloudflare in front of the cloud endpoint rejects ``Python-urllib/*`` user
# agents with 403; a neutral bridge UA passes.
USER_AGENT = "hindsight-dsh-bridge/0.1"
JSON_PARSE_ERROR = -32700
INTERNAL_ERROR = -32603
_REDACTED_TOKEN = "<token>"
_REDACTED_URL = "<remote-url>"
_REMOTE_URL_RE = re.compile(r"https?://[^\s\"']*/mcp/[^\s\"']*", re.IGNORECASE)

_ACTIVE_TOKEN: str | None = None


class TokenUnavailableError(RuntimeError):
    """Raised when the remote access token cannot be loaded."""


class SessionState:
    """Remembers the upstream session id and negotiated protocol version."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def update(self, headers: Any) -> None:
        session_id = headers.get(SESSION_HEADER)
        if session_id:
            self.session_id = session_id

    def learn_protocol_version(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            result = message.get("result") if isinstance(message, dict) else None
            if isinstance(result, dict) and result.get("protocolVersion"):
                self.protocol_version = str(result["protocolVersion"])
                return


def load_token(path: str = DEFAULT_TOKEN_PATH) -> str:
    """Read ``BRAIN_MCP_TOKEN`` from a dotenv-style file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TokenUnavailableError("token file unreadable") from exc
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == TOKEN_ENV_KEY:
            token = value.strip().strip('"').strip("'").strip()
            if token:
                return token
            break
    raise TokenUnavailableError(f"{TOKEN_ENV_KEY} is not set in the token file")


def build_url(token: str, base: str = DEFAULT_REMOTE_BASE) -> str:
    """Append the token to the remote base URL."""
    return base + token


def redact(message: str) -> str:
    """Scrub the token and any MCP URL out of a diagnostic message."""
    text = message
    if _ACTIVE_TOKEN:
        text = text.replace(_ACTIVE_TOKEN, _REDACTED_TOKEN)
    text = _REMOTE_URL_RE.sub(_REDACTED_URL, text)
    return text.replace(DEFAULT_REMOTE_BASE, _REDACTED_URL)


def _warn(message: str) -> None:
    print(redact(message), file=sys.stderr, flush=True)


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response with a redacted message."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": redact(message)}}


def _build_opener(proxy: str) -> urllib.request.OpenerDirector:
    proxy_map = {"http": proxy, "https": proxy} if proxy else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxy_map))


def post_message(
    url: str,
    body: bytes,
    proxy: str,
    timeout: float,
    state: SessionState,
) -> tuple[int, str, bytes, Any]:
    """POST one JSON-RPC message; returns (status, content type, body, headers)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": ACCEPT_HEADER,
        "User-Agent": USER_AGENT,
    }
    if state.session_id:
        headers[SESSION_HEADER] = state.session_id
    if state.protocol_version:
        headers[PROTOCOL_VERSION_HEADER] = state.protocol_version
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = _build_opener(proxy)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read(), error.headers
    with response:
        return (
            response.status,
            response.headers.get("Content-Type", ""),
            response.read(),
            response.headers,
        )


def _decode_sse_data(data: str) -> list[Any]:
    if not data or data == "[DONE]":
        return []
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        _warn("hindsight bridge: dropped an undecodable SSE data frame")
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def parse_sse(text: str) -> list[Any]:
    """Parse an SSE stream into JSON payloads (multi-line data frames joined)."""
    events: list[Any] = []
    data_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                events.extend(_decode_sse_data("\n".join(data_lines)))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        events.extend(_decode_sse_data("\n".join(data_lines)))
    return events


def parse_upstream(content_type: str, body: bytes) -> list[Any]:
    """Parse a JSON or SSE upstream body into a list of JSON-RPC messages."""
    if not body.strip():
        return []  # e.g. 202 Accepted for notifications
    text = body.decode("utf-8", errors="replace")
    if content_type.split(";")[0].strip().lower() == "text/event-stream":
        return parse_sse(text)
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


def process_message(
    message: Any,
    *,
    url: str,
    proxy: str,
    timeout: float,
    state: SessionState,
) -> list[dict[str, Any]]:
    """Forward one JSON-RPC message and return the response objects to relay."""
    if not isinstance(message, dict):
        return [
            error_response(None, JSON_PARSE_ERROR, "hindsight bridge: message must be an object")
        ]
    request_id = message.get("id")
    is_notification = "id" not in message
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    try:
        status, content_type, payload, headers = post_message(url, body, proxy, timeout, state)
    except Exception as exc:  # noqa: BLE001 - any upstream failure must not crash the loop
        _warn(f"hindsight bridge: upstream request failed: {type(exc).__name__}")
        if is_notification:
            return []
        return [
            error_response(request_id, INTERNAL_ERROR, "hindsight bridge: upstream request failed")
        ]
    state.update(headers)
    if status >= 400:
        _warn(f"hindsight bridge: upstream returned HTTP {status}")
        if is_notification:
            return []
        return [
            error_response(request_id, INTERNAL_ERROR, f"hindsight bridge: upstream HTTP {status}")
        ]
    try:
        messages = parse_upstream(content_type, payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _warn("hindsight bridge: upstream sent an undecodable body")
        if is_notification:
            return []
        message_text = "hindsight bridge: undecodable upstream body"
        return [error_response(request_id, INTERNAL_ERROR, message_text)]
    state.learn_protocol_version([m for m in messages if isinstance(m, dict)])
    if is_notification:
        return []
    return [
        m
        for m in messages
        if isinstance(m, dict) and "id" in m and m.get("id") == request_id
    ]


def _flatten(message: Any) -> list[Any]:
    return message if isinstance(message, list) else [message]


def handle_line(
    line: str,
    *,
    url: str | None,
    proxy: str,
    timeout: float,
    state: SessionState,
) -> None:
    """Process one stdin line: forward it upstream and emit any responses."""
    if not line.strip():
        return
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        _emit(error_response(None, JSON_PARSE_ERROR, "hindsight bridge: invalid JSON line"))
        return
    for item in _flatten(message):
        if url is None:
            if isinstance(item, dict) and "id" in item:
                _emit(
                    error_response(
                        item.get("id"), INTERNAL_ERROR, "hindsight bridge: token unavailable"
                    )
                )
            continue
        try:
            outputs = process_message(item, url=url, proxy=proxy, timeout=timeout, state=state)
        except Exception as exc:  # noqa: BLE001 - the stdio loop must survive anything
            _warn(f"hindsight bridge: unexpected failure: {type(exc).__name__}")
            if isinstance(item, dict) and "id" in item:
                _emit(
                    error_response(
                        item.get("id"), INTERNAL_ERROR, "hindsight bridge: internal failure"
                    )
                )
            continue
        for output in outputs:
            _emit(output)


def main() -> int:
    """Run the stdio loop until EOF; never crash on individual bad messages."""
    global _ACTIVE_TOKEN
    proxy = os.environ.get(PROXY_ENV_KEY, DEFAULT_PROXY)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    try:
        token = load_token()
    except TokenUnavailableError as exc:
        _warn(f"hindsight bridge: {exc}")
        token = None
    _ACTIVE_TOKEN = token
    url = build_url(token) if token else None
    state = SessionState()
    for raw in sys.stdin:
        try:
            handle_line(raw, url=url, proxy=proxy, timeout=REQUEST_TIMEOUT_S, state=state)
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the loop itself
            _warn(f"hindsight bridge: loop failure: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
