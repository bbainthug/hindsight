#!/usr/bin/env python3
"""把本地 brain.sqlite 的一致性快照推到 VM（单向），原子替换后重启远端服务。

- 用 Python backup API 出快照（macOS 的 sqlite3 CLI 常因 TCC 打不开库），不 VACUUM，
  保持页布局稳定，rsync 才能只传变化块；
- 远端保留常驻 .staging 文件作为 rsync 基准，再 cp+mv 原子替换；
- 库没变化（mtime/size/WAL）就跳过。
- 不要让这条路走 Cloudflare 隧道：800 MB 级传输会把隧道和 1 GB 内存的小机器堵死，
  用 Tailscale 之类的私网直连。

用法：push_to_vm.py [--host SSH别名] [--remote 远端路径] [--force]
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BRAIN_HOME = Path(os.environ.get("BRAIN_HOME", HOME / ".local/share/personal-brain")).expanduser()
LOCAL_DB = BRAIN_HOME / "db/brain.sqlite"
SNAPSHOT = BRAIN_HOME / "backups/vm-snapshot.sqlite"
# ~/.ssh/config 里的别名；建议走 Tailscale 等私网
HOST = os.environ.get("HINDSIGHT_VM_HOST", "hindsight-vm")
REMOTE = os.environ.get("HINDSIGHT_VM_DB", "brain-data/db/brain.sqlite")
SERVICE = os.environ.get("HINDSIGHT_SERVICE_NAME", "hindsight-mcp")

def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def main() -> int:
    args = sys.argv[1:]
    host = args[args.index("--host") + 1] if "--host" in args else HOST
    remote = args[args.index("--remote") + 1] if "--remote" in args else REMOTE
    marker = SNAPSHOT.with_suffix(".pushed")
    sig = f"{LOCAL_DB.stat().st_mtime_ns}:{LOCAL_DB.stat().st_size}"
    wal = LOCAL_DB.with_name(LOCAL_DB.name + "-wal")
    if wal.exists():
        sig += f":{wal.stat().st_mtime_ns}:{wal.stat().st_size}"
    if "--force" not in args and marker.exists() and marker.read_text() == sig:
        log("db unchanged since last push, skip")
        return 0
    t0 = time.time()
    src = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(SNAPSHOT))
    src.backup(dst)
    dst.close()
    src.close()
    log(f"snapshot {SNAPSHOT.stat().st_size/1048576:.0f}MB in {time.time()-t0:.1f}s")
    t1 = time.time()
    incoming = f"{remote}.staging"  # 常驻暂存文件：保留上次内容，rsync 才能只传增量
    r = subprocess.run(["rsync", "-az", "--partial", "--inplace", "--stats",
                        str(SNAPSHOT), f"{host}:{incoming}"], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"rsync failed: {r.stderr.strip()[:300]}")
        return 1
    stats = [
        line.strip() for line in r.stdout.splitlines()
        if "Total bytes sent" in line or "Literal data" in line
    ]
    log(f"rsync ok in {time.time()-t1:.1f}s ({'; '.join(stats)})")
    swap = (
        f"cp {incoming} {remote}.tmp && mv -f {remote}.tmp {remote}"
        f" && systemctl --user restart {SERVICE} && echo restarted"
    )
    r = subprocess.run(["ssh", host, swap], capture_output=True, text=True)
    log(r.stdout.strip() or r.stderr.strip()[:300])
    if r.returncode == 0:
        marker.write_text(sig)
    return r.returncode

if __name__ == "__main__":
    sys.exit(main())
