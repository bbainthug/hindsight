"""真实 stdio MCP 握手测试：扩展目录服务不依赖 history 数据库。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _send(proc: subprocess.Popen[str], payload: dict) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, f"MCP server stdout 提前关闭: {proc.stderr.read()[:500]}"
    return json.loads(line)


def _notify(proc: subprocess.Popen[str], payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def test_extensions_mcp_stdio_handshake(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    env["PERSONAL_BRAIN_EXTENSION_PROJECT_ROOTS"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "personal_brain.extensions_mcp"],
        cwd=Path(__file__).parents[2],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        init = _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
        )
        assert init["result"]["serverInfo"]["name"] == "personal-brain-extensions"
        _notify(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "list_local_extensions",
            "search_local_extensions",
            "local_extensions_status",
        ]
        result = _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "local_extensions_status", "arguments": {}},
            },
        )
        assert result["result"]["structuredContent"]["read_only"] is True
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
