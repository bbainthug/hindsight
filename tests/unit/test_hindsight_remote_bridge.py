"""Unit tests for the Hindsight stdio-to-HTTP MCP bridge.

All upstream calls are intercepted in-process. No local token file, network
socket, real credential, or external service is required.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

BRIDGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "integrations"
    / "dsh"
    / "hindsight_remote_bridge.py"
)
FAKE_TOKEN = "SYNTHETIC-BRIDGE-TOKEN"


def _load_bridge() -> Any:
    spec = importlib.util.spec_from_file_location("hindsight_remote_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class FakeResponse:
    def __init__(self, status: int, content_type: str, body: bytes,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeUpstream:
    """Intercept urllib opener calls and capture request data without sockets."""

    url = f"http://mcp.example.invalid/mcp/{FAKE_TOKEN}"

    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests = []

    def open(self, request, timeout: float) -> FakeResponse:
        self.requests.append({"request": request, "timeout": timeout})
        result = self.responder(request)
        if isinstance(result, BaseException):
            raise result
        status, content_type, body, headers = result
        if status >= 400:
            message = Message()
            message["Content-Type"] = content_type
            raise urllib.error.HTTPError(
                request.full_url, status, "synthetic failure", message,
                io.BytesIO(body),
            )
        return FakeResponse(status, content_type, body, headers)


def _install(monkeypatch: pytest.MonkeyPatch, upstream: FakeUpstream) -> None:
    monkeypatch.setattr(bridge, "_build_opener", lambda _proxy: upstream)


def _json_upstream(payload: dict[str, Any]) -> FakeUpstream:
    body = json.dumps(payload).encode("utf-8")
    return FakeUpstream(lambda _request: (200, "application/json", body, {}))


def _process(
    upstream: FakeUpstream, message: dict[str, Any], state: Any | None = None
) -> list[dict[str, Any]]:
    return bridge.process_message(
        message,
        url=upstream.url,
        proxy="http://unused.invalid",
        timeout=5.0,
        state=state or bridge.SessionState(),
    )


def test_load_token_parses_synthetic_temporary_file(tmp_path: Path) -> None:
    token_file = tmp_path / "synthetic.env"
    token_file.write_text(
        '# comment\nOTHER=1\nBRAIN_MCP_TOKEN=" spaced-token "\n', encoding="utf-8"
    )
    assert bridge.load_token(str(token_file)) == "spaced-token"


def test_load_token_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(bridge.TokenUnavailableError):
        bridge.load_token(str(tmp_path / "missing.env"))


def test_json_response_is_relayed(monkeypatch: pytest.MonkeyPatch) -> None:
    result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "hindsight"}}
    upstream = _json_upstream({"jsonrpc": "2.0", "id": 1, "result": result})
    _install(monkeypatch, upstream)
    outputs = _process(
        upstream, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert outputs == [{"jsonrpc": "2.0", "id": 1, "result": result}]
    sent = json.loads(upstream.requests[0]["request"].data.decode("utf-8"))
    assert sent["method"] == "initialize"


def test_required_headers_are_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _json_upstream({"jsonrpc": "2.0", "id": 2, "result": {}})
    _install(monkeypatch, upstream)
    _process(upstream, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    headers = {key.lower(): value for key, value in upstream.requests[0]["request"].header_items()}
    assert headers["accept"] == "application/json, text/event-stream"
    assert headers["content-type"] == "application/json"


def test_sse_response_is_relayed_and_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    notification = {"jsonrpc": "2.0", "method": "notifications/progress"}
    response = {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}
    stream = (
        ": keepalive\n"
        f"data: {json.dumps(notification)}\n\n"
        f"data: {json.dumps(response)}\n\n"
    ).encode()
    upstream = FakeUpstream(lambda _request: (200, "text/event-stream", stream, {}))
    _install(monkeypatch, upstream)
    outputs = _process(upstream, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert outputs == [response]


def test_sse_multiline_data_frame_is_joined() -> None:
    stream = 'event: message\ndata: {"a":\ndata: 1}\n\ndata: [DONE]\n\n'
    assert bridge.parse_sse(stream) == [{"a": 1}]


def test_notification_is_forwarded_without_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _json_upstream({"jsonrpc": "2.0", "id": 99, "result": {}})
    _install(monkeypatch, upstream)
    outputs = _process(
        upstream, {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert outputs == []
    assert len(upstream.requests) == 1
    sent = json.loads(upstream.requests[0]["request"].data.decode("utf-8"))
    assert sent["method"] == "notifications/initialized"


def test_http_500_becomes_jsonrpc_error_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = f"synthetic failure at /mcp/{FAKE_TOKEN}".encode()
    upstream = FakeUpstream(lambda _request: (500, "text/plain", body, {}))
    _install(monkeypatch, upstream)
    outputs = _process(upstream, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    encoded = json.dumps(outputs)
    assert outputs[0]["error"]["code"] == bridge.INTERNAL_ERROR
    assert FAKE_TOKEN not in encoded
    assert "example.invalid" not in encoded


def test_connection_failure_yields_error_and_bridge_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def responder(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return urllib.error.URLError("synthetic connection failure")
        return 200, "application/json", b'{"jsonrpc":"2.0","id":5,"result":{"ok":true}}', {}

    upstream = FakeUpstream(responder)
    _install(monkeypatch, upstream)
    state = bridge.SessionState()
    outputs = _process(
        upstream, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, state
    )
    encoded = json.dumps(outputs)
    assert outputs[0]["error"]["code"] == bridge.INTERNAL_ERROR
    assert FAKE_TOKEN not in encoded
    recovered = _process(
        upstream, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, state
    )
    assert recovered[0]["result"] == {"ok": True}


def test_session_and_protocol_headers_are_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        (
            200,
            "application/json",
            b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}',
            {"Mcp-Session-Id": "session-42"},
        ),
        (200, "application/json", b'{"jsonrpc":"2.0","id":2,"result":{}}', {}),
    ]
    upstream = FakeUpstream(lambda _request: responses.pop(0))
    _install(monkeypatch, upstream)
    state = bridge.SessionState()
    _process(
        upstream,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        state,
    )
    _process(upstream, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, state)
    second_headers = {
        key.lower(): value for key, value in upstream.requests[1]["request"].header_items()
    }
    assert second_headers["mcp-session-id"] == "session-42"
    assert second_headers["mcp-protocol-version"] == "2025-06-18"


def test_malformed_line_yields_parse_error(capsys: pytest.CaptureFixture[str]) -> None:
    bridge.handle_line(
        "not json", url=None, proxy="", timeout=5.0, state=bridge.SessionState()
    )
    emitted = json.loads(capsys.readouterr().out.strip())
    assert emitted["error"]["code"] == bridge.JSON_PARSE_ERROR
    assert emitted["id"] is None


def test_redact_masks_token_and_url() -> None:
    bridge._ACTIVE_TOKEN = FAKE_TOKEN
    try:
        leaked = (
            f"failed for https://brain.bbainthug.tech/mcp/{FAKE_TOKEN} and {FAKE_TOKEN}"
        )
        scrubbed = bridge.redact(leaked)
        assert FAKE_TOKEN not in scrubbed
        assert "bbainthug" not in scrubbed
        assert bridge._REDACTED_TOKEN in scrubbed
        assert bridge._REDACTED_URL in scrubbed
    finally:
        bridge._ACTIVE_TOKEN = None


def test_redact_handles_missing_token_gracefully() -> None:
    bridge._ACTIVE_TOKEN = None
    assert bridge.redact("plain message") == "plain message"
