#!/usr/bin/env python3
"""D-5b 交付用探针：REST /api/search 与 MCP tools/call search_history 的
热态 P50/P95（对照本地跑 hindsight-mcp --transport http 的正在运行进程）。

只用标准库（urllib），不依赖 httpx —— VM 生产环境只装 `remote` 可选组
（starlette/uvicorn），httpx 只在 dev 依赖组，不能假设 VM 上有。

用法（在 VM 上，服务已用 systemctl --user 跑起来）：
  uv run deploy/vm/perf_probe.py                       # 空载
  uv run deploy/vm/perf_probe.py --label 导入进行中
  # 上一条与 import_inbox 并发跑时另开一次；--label 只影响输出里的标签

- token 默认从 ~/.config/hindsight/env 的 BRAIN_MCP_TOKEN 读取（install.sh 生成的那份）；
- 只读 GET/POST，不改变库内容；
- 每次测量前先跑 --warmup 次（默认 3）暖缓存/连接池，不计入统计。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".config/hindsight/env"


def _read_token() -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("BRAIN_MCP_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    n = len(ordered)

    def pct(q: float) -> float:
        idx = min(n - 1, int(round(q * (n - 1))))
        return round(ordered[idx], 2)

    return {
        "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99),
        "min": round(ordered[0], 2), "max": round(ordered[-1], 2),
        "mean": round(statistics.fmean(ordered), 2), "n": n,
    }


def _get(url: str, *, timeout: float) -> None:
    # url 固定拼自 --base（本机/内网地址），不接受外部输入
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        resp.read()


def _post_json(url: str, payload: dict, *, timeout: float) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def probe_rest_search(base: str, query: str, *, warmup: int, repeats: int,
                       timeout: float) -> list[float]:
    url = f"{base}/api/search?{urllib.parse.urlencode({'q': query})}"
    for _ in range(warmup):
        _get(url, timeout=timeout)
    out = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        _get(url, timeout=timeout)
        out.append((time.perf_counter() - t0) * 1000)
    return out


def probe_mcp_search(base: str, token: str, query: str, *, warmup: int, repeats: int,
                      timeout: float) -> list[float]:
    url = f"{base}/mcp/{token}"
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_history", "arguments": {"query": query}},
    }
    for _ in range(warmup):
        _post_json(url, body, timeout=timeout)
    out = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        _post_json(url, body, timeout=timeout)
        out.append((time.perf_counter() - t0) * 1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default=None, help="默认读 ~/.config/hindsight/env")
    ap.add_argument("--query", default="职业")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--label", default="空载", help="仅用于输出标签，如“导入进行中”")
    args = ap.parse_args()

    token = args.token or _read_token()
    if not token:
        print("未找到 token：传 --token 或确认 ~/.config/hindsight/env 存在 BRAIN_MCP_TOKEN",
              file=sys.stderr)
        return 1

    try:
        rest_ms = probe_rest_search(args.base, args.query, warmup=args.warmup,
                                     repeats=args.repeats, timeout=args.timeout)
        mcp_ms = probe_mcp_search(args.base, token, args.query, warmup=args.warmup,
                                   repeats=args.repeats, timeout=args.timeout)
    except urllib.error.URLError as exc:
        print(f"请求失败：{exc}（服务是否已用 systemctl --user start hindsight-mcp 跑起来？）",
              file=sys.stderr)
        return 1

    result = {
        "label": args.label,
        "rest_api_search": _percentiles(rest_ms),
        "mcp_tools_call_search_history": _percentiles(mcp_ms),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
