#!/usr/bin/env python3
"""把浏览器里导出的 Gemini 对话（gemini-export.json）转成 ChatGPT 导出格式，
交给现有 `brain import-chatgpt` 导入器（与 agent_sync 同一思路，项目源码零改动）。

输入格式（由 export_gemini.js 在 gemini.google.com 页面里生成）::

    {"exported_at": "...", "conversations": [
        {"id": "c_abc", "title": "...", "created_at": <epoch 秒或 null>,
         "turns": [{"role": "user"|"assistant", "text": "..."}]}]}

Gemini 网页不显示逐条消息时间：有会话级时间（created_at）时，该会话内消息按
顺序取 created_at + 序号秒（只用于保序，time 仍是近似值）；没有时消息时间留空，
导入后为"时间未知"，可检索但不参与日期过滤——不伪造时间。

消息的稳定 ID = 会话 ID + 序号 + 内容哈希：重复导出同一内容幂等；对话续聊后
新增的消息是新 ID，旧消息不变。

用法：convert_gemini.py gemini-export.json -o conversations.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_sync"))
from sync_agents import Conv  # noqa: E402


def _clean(s: str) -> str:
    """页面 textContent 偶尔把 emoji 拆成孤立代理项：成对的拼回原字符，落单的换成 U+FFFD。"""
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def convert(export: dict) -> list[dict]:
    out = []
    for c in export.get("conversations", []):
        cid = str(c.get("id") or "").strip()
        turns = c.get("turns") or []
        if not cid or not turns:
            continue
        conv = Conv(f"gemini-{cid}", _clean((c.get("title") or "").strip()))
        base = c.get("created_at")
        for i, t in enumerate(turns):
            text = _clean((t.get("text") or "").strip())
            role = t.get("role")
            digest = hashlib.sha1(f"{role}\x00{text}".encode()).hexdigest()[:12]
            epoch = (float(base) + i) if isinstance(base, (int, float)) else None
            conv.add(epoch, role, text, f"{i}:{digest}")
        conv_json = conv.finish()
        if conv_json:
            if not isinstance(base, (int, float)):
                # 无时间：Conv 内部用 0.0 保序，这里还原成"未知"而不是 1970 年
                for node in conv_json["mapping"].values():
                    if node["message"]:
                        node["message"]["create_time"] = None
                conv_json["create_time"] = conv_json["update_time"] = None
            out.append(conv_json)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("export", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args(argv)
    export = json.loads(args.export.read_text(encoding="utf-8"))
    convs = convert(export)
    args.out.write_text(json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    n_msgs = sum(len(c["mapping"]) - 1 for c in convs)
    timed = sum(1 for c in convs if c["create_time"])
    print(f"会话 {len(convs)}（有时间 {timed}），消息 {n_msgs} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
