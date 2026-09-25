#!/usr/bin/env python3
"""把本地 outbox 里的未推送批次 rsync 到 VM（D-5 批次同步，替代整库推送）。

- outbox 布局：$HINDSIGHT_OUTBOX/<source>/<UTC时间戳>-<内容sha256前12位>.json
  （由 sync_agents.py --export-dir 产出）。
- 推送：rsync 各来源子目录 → VM 的 ~/brain-data/inbox/<source>/；
  rsync 成功（rc=0）后本地批次移到 outbox/sent/<source>/（14 天后清理）。
- 失败不删、下次重试；只传批次，永不传库——批次通常几十 KB，
  走 Cloudflare 隧道的 SSH（HINDSIGHT_VM_HOST，默认 hindsight-vm）即可。
- 日志只记批次名、批次数字节数，不记会话正文。

环境变量：HINDSIGHT_VM_HOST、HINDSIGHT_OUTBOX、HINDSIGHT_VM_INBOX、
BRAIN_HOME、HINDSIGHT_RETENTION_DAYS（sent 保留天数，默认 14）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BRAIN_HOME = Path(os.environ.get("BRAIN_HOME", HOME / ".local/share/personal-brain")).expanduser()
OUTBOX = Path(os.environ.get("HINDSIGHT_OUTBOX", BRAIN_HOME / "agent_sync/outbox"))
VM_HOST = os.environ.get("HINDSIGHT_VM_HOST", "hindsight-vm")
VM_INBOX = os.environ.get("HINDSIGHT_VM_INBOX", "brain-data/inbox")
RETENTION_DAYS = int(os.environ.get("HINDSIGHT_RETENTION_DAYS", "14"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def pending_batches() -> dict[str, list[Path]]:
    """outbox/<source>/*.json（不含 sent/）；按文件名排序。"""
    out: dict[str, list[Path]] = {}
    if not OUTBOX.is_dir():
        return out
    for src_dir in sorted(p for p in OUTBOX.iterdir() if p.is_dir()):
        if src_dir.name == "sent":
            continue
        batches = sorted(f for f in src_dir.glob("*.json") if f.is_file())
        if batches:
            out[src_dir.name] = batches
    return out


def push_source(source: str, batches: list[Path]) -> bool:
    remote_dir = f"{VM_HOST}:{VM_INBOX}/{source}/"
    m = subprocess.run(["ssh", VM_HOST, "mkdir", "-p", f"{VM_INBOX}/{source}/"],
                       capture_output=True, text=True)
    if m.returncode != 0:
        log(f"ssh mkdir 失败 [{source}]: {(m.stderr or m.stdout).strip()[-300:]}")
        return False
    r = subprocess.run(
        ["rsync", "-az", "--partial",
         *[str(b) for b in batches], remote_dir],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log(f"rsync 失败 [{source}]: {(r.stderr or r.stdout).strip()[-300:]}")
        return False
    sent_dir = OUTBOX / "sent" / source
    sent_dir.mkdir(parents=True, exist_ok=True)
    total = sum(b.stat().st_size for b in batches)
    for b in batches:
        shutil.move(str(b), sent_dir / b.name)
    log(f"[{source}] 推送 {len(batches)} 个批次，共 {total} 字节 → {VM_INBOX}/{source}/")
    return True


def clean_sent() -> int:
    """outbox/sent/ 里超过保留期的批次删除；返回删除数。"""
    sent_root = OUTBOX / "sent"
    if not sent_root.is_dir():
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for f in sent_root.rglob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def main() -> int:
    pending = pending_batches()
    if not pending:
        log("outbox 无未推送批次")
    else:
        ok = True
        for source, batches in pending.items():
            ok = push_source(source, batches) and ok
        if not ok:
            return 1  # 失败的批次留在 outbox，下次 launchd 触发重试
    removed = clean_sent()
    if removed:
        log(f"清理 outbox/sent 过期批次 {removed} 个（保留 {RETENTION_DAYS} 天）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
