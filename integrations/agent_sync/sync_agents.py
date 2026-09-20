#!/usr/bin/env python3
"""agent_sync — 把本机 coding agent 的会话增量导入 Hindsight。

原理：把各家 agent 的本地会话（Codex / DSH / Claude Code 的会话文件，Hermes 的
state.db）转写成 ChatGPT 导出格式（conversations.json），交给现有 `brain
import-chatgpt` 导入器——幂等、按消息 ID 去重，项目源码零改动。
只采 user/assistant 正文；工具调用、系统提示一律不进库。

用法：
  sync_agents.py [--db PATH] [--archive-dir DIR] [--state FILE]
                 [--brain CMD] [--sources codex,dsh,claude,hermes]
环境变量（优先级低于参数）：BRAIN_HOME、BRAIN_CLI、ZSTD
默认：BRAIN_HOME=~/.local/share/personal-brain，brain 命令从 PATH 找。
定时运行：见同目录 launchd/ 与 docs/agent-sync.md。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

HOME = Path.home()
BRAIN_HOME = Path(os.environ.get("BRAIN_HOME", HOME / ".local/share/personal-brain")).expanduser()
BRAIN_CLI = (
    os.environ.get("BRAIN_CLI") or shutil.which("brain") or str(BRAIN_HOME / "venv/bin/brain")
)
ZSTD = os.environ.get("ZSTD") or shutil.which("zstd") or "zstd"
STATE_FILE = BRAIN_HOME / "agent_sync/state.json"
ARCHIVE_DIR = BRAIN_HOME / "archives"


def _epoch(ts) -> float | None:
    """把各种时间表示统一成 epoch 秒。"""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return ts / 1000 if ts > 1e11 else ts  # 毫秒 → 秒
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text_of(content) -> str:
    """ChatGPT/DSH 内容块列表 → 纯文本。"""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict) and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "\n".join(t for t in (p.strip() for p in parts) if t)


class Conv:
    """累积一个会话的线性消息链，输出 ChatGPT 导出格式。"""

    def __init__(self, conversation_id: str, title: str):
        self.cid = conversation_id
        self.title = title or conversation_id[:40]
        self.msgs: list[tuple[float, str, str, str]] = []  # (epoch, role, text, stable_id)

    def add(self, epoch: float | None, role: str, text: str, stable_id: str):
        text = text.strip()
        if text and role in ("user", "assistant"):
            self.msgs.append((epoch or 0.0, role, text, stable_id))

    def finish(self) -> dict:
        self.msgs.sort(key=lambda m: m[0])
        if not self.msgs:
            return {}
        mapping: dict[str, dict] = {"root": {"message": None, "parent": None, "children": []}}
        prev = "root"
        for ts, role, text, sid in self.msgs:
            nid = "n" + hashlib.sha1(f"{self.cid}:{sid}".encode()).hexdigest()[:20]
            mapping[prev]["children"].append(nid)
            mapping[nid] = {
                "message": {
                    "author": {"role": role},
                    "create_time": ts or None,
                    "content": {"content_type": "text", "parts": [text]},
                },
                "parent": prev,
                "children": [],
            }
            prev = nid
        tss = [m[0] for m in self.msgs if m[0]]
        return {
            "title": self.title,
            "create_time": min(tss) if tss else None,
            "update_time": max(tss) if tss else None,
            "mapping": mapping,
            "current_node": prev,
            "conversation_id": self.cid,
        }


def parse_codex(path: Path) -> dict:
    uuid = path.stem.split("-", 0)[-1][-36:] if "-" in path.stem else path.stem
    uuid = path.stem.rsplit("-", 1)[-1]  # rollout-<ts>-<uuid>.jsonl 取末段 uuid
    conv = Conv(f"codex-{uuid}", "")
    session_start = None
    for idx, line in enumerate(path.open(encoding="utf-8", errors="replace")):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _epoch(d.get("timestamp"))
        if session_start is None and ts:
            session_start = ts
        if d.get("type") != "response_item":
            continue
        p = d.get("payload", {})
        if p.get("type") != "message":
            continue
        role = p.get("role")
        text = _text_of(p.get("content"))
        if role == "user" and not conv.title:
            conv.title = "codex: " + text[:38]
        conv.add(ts, role, text, f"{uuid}:{idx}")
    if not conv.title:
        conv.title = f"codex {datetime.fromtimestamp(session_start or 0, UTC):%Y-%m-%d}"
    return conv.finish()


def parse_dsh(path: Path) -> dict:
    raw = subprocess.run([ZSTD, "-dc", str(path)], capture_output=True, check=True).stdout
    sid, title, conv = None, "", None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t, data, ts = d.get("type"), d.get("data") or {}, _epoch(d.get("time"))
        if t == "session" and not sid:
            sid = d.get("id")
            conv = Conv(f"dsh-{sid}", "")
        elif t == "session/title":
            title = (data.get("title") or "").strip()
        elif t == "user/message":
            conv.add(ts, "user", _text_of(data.get("content")), str(data.get("id") or d.get("seq")))
        elif t == "assistant/message":
            m = data.get("message") or {}
            conv.add(ts, "assistant", _text_of(m.get("content")), str(m.get("id") or d.get("seq")))
    if conv is None:
        return {}
    if title:
        conv.title = title
    return conv.finish()


def parse_claude(path: Path) -> dict:
    """Claude Code: ~/.claude/projects/<proj>/<sessionId>.jsonl。

    只取 type=user/assistant 的正文行；isSidechain=true 的子代理线程跳过；
    tool_use/tool_result 块无 text 字段，_text_of 自然过滤。
    """
    conv = None
    seen = False
    first_user = ""
    for idx, line in enumerate(path.open(encoding="utf-8", errors="replace")):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t not in ("user", "assistant") or d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        role = msg.get("role") or t
        if role not in ("user", "assistant"):
            continue
        text = _text_of(msg.get("content"))
        if not text:
            continue
        if conv is None:
            conv = Conv(f"claude-{d.get('sessionId') or path.stem}", "")
        ts = _epoch(d.get("timestamp"))
        if role == "user" and not first_user and d.get("isMeta") is not True:
            first_user = text
        conv.add(ts, role, text, str(d.get("uuid") or f"{path.stem}:{idx}"))
        seen = True
    if not seen or conv is None:
        return {}
    if first_user:
        conv.title = "claude: " + first_user[:38]
    else:
        conv.title = "claude " + path.stem
    return conv.finish()


def parse_hermes(path: Path) -> list[dict]:
    """Hermes 把会话存在 sqlite（sessions / messages）；一个库对应多个会话。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    convs: list[dict] = []
    for s in conn.execute("SELECT id, title, display_name, started_at FROM sessions"):
        conv = Conv(f"hermes-{s['id']}", s["title"] or s["display_name"] or "")
        for m in conn.execute(
            "SELECT id, role, content, timestamp FROM messages WHERE session_id=? "
            "AND role IN ('user','assistant') ORDER BY id", (s["id"],)
        ):
            content = m["content"]
            if isinstance(content, str) and content.startswith("["):
                try:
                    content = _text_of(json.loads(content))
                except ValueError:
                    pass
            conv.add(_epoch(m["timestamp"]), m["role"], content or "", f"m{m['id']}")
        d = conv.finish()
        if d:
            convs.append(d)
    conn.close()
    return convs


def scan(pattern: str, base: Path) -> list[Path]:
    return sorted(base.glob(pattern)) if base.exists() else []


SOURCES = {
    "codex": (HOME / ".codex/sessions", "**/*.jsonl", parse_codex),
    "dsh": (HOME / ".dsh/sessions", "**/session.jsonl.zstd", parse_dsh),
    "claude": (HOME / ".claude/projects", "**/*.jsonl", parse_claude),
    "hermes": (HOME / ".hermes", "state.db", parse_hermes),  # 单文件多会话
}
ALL_SOURCES = list(SOURCES)


def main() -> int:
    args = sys.argv[1:]

    def opt(name: str, default):
        return args[args.index(name) + 1] if name in args else default

    db = Path(opt("--db", BRAIN_HOME / "db/brain.sqlite")).expanduser()
    archive_dir = Path(opt("--archive-dir", ARCHIVE_DIR)).expanduser()
    state_file = Path(opt("--state", STATE_FILE)).expanduser()
    brain_cli = opt("--brain", BRAIN_CLI)
    want = [s for s in opt("--sources", ",".join(ALL_SOURCES)).split(",") if s]
    unknown = [s for s in want if s not in SOURCES]
    if unknown:
        print(f"未知来源: {unknown}（可选 {ALL_SOURCES}）", file=sys.stderr)
        return 2
    state = json.loads(state_file.read_text()) if state_file.exists() else {}

    total_new = 0
    for src in want:
        base, pattern, parser = SOURCES[src]
        convs: list[dict] = []
        files = scan(pattern, base)
        changed = 0
        for f in files:
            st = f.stat()
            key = str(f)
            prev = state.get(key)
            if prev and prev["mtime"] == st.st_mtime and prev["size"] == st.st_size:
                continue
            try:
                c = parser(f)
            except Exception as exc:  # 单文件坏不拖垮整体
                print(f"  [跳过] {f.name}: {exc}", file=sys.stderr)
                continue
            state[key] = {"mtime": st.st_mtime, "size": st.st_size}
            if isinstance(c, list):
                convs.extend(x for x in c if x)
            elif c:
                convs.append(c)
            changed += 1
        if not convs:
            print(f"[{src}] 无新增会话（扫描 {len(files)} 个文件）")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "conversations.json"
            out.write_text(json.dumps(convs, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run(
                [brain_cli, "--db", str(db), "--archive-dir", str(archive_dir),
                 "import-chatgpt", str(out.parent), "--source", src],
                capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[{src}] 导入失败:\n{r.stderr[-500:]}", file=sys.stderr)
                return 1
            tail = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
            print(f"[{src}] 转写 {changed} 个新/变化会话，导入 {len(convs)} 个对话。{tail}")
            total_new += len(convs)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=0))
    print(f"完成：本轮新导入 {total_new} 个对话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
