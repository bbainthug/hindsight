"""只读规划与显式应用 agent 注入内容的逻辑撤回。"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_brain.injected_rules import RULES, InjectionRule, metadata_rule, text_rule

_SESSION_META_MARKER = re.compile(r'"type"\s*:\s*"session_meta"')
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SENSITIVE_TOKEN = re.compile(
    r"(?i)\b(?:sk-[a-z0-9-]{16,}|ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"xox[baprs]-[^\s]+|AKIA[0-9A-Z]{16})\b|\bBearer\s+[^\s,;]+"
)


@dataclass
class OwnerPreview:
    event_id: str
    conversation_id: str
    source: str
    rule: str | None
    text: str
    char_count: int
    source_created_at: str | None


@dataclass
class CleanPlan:
    # event_id -> rule name; conversation level matches include assistant events.
    event_reasons: dict[str, str] = field(default_factory=dict)
    withdrawn_owners: list[OwnerPreview] = field(default_factory=list)
    retained_owners: list[OwnerPreview] = field(default_factory=list)
    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    chars: Counter[tuple[str, str]] = field(default_factory=Counter)
    event_counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    owner_count: int = 0
    owner_chars: int = 0
    recent_current: dict[str, tuple[int, int]] = field(default_factory=dict)
    recent_expected: dict[str, tuple[int, int]] = field(default_factory=dict)
    recent_unknown: dict[str, int] = field(default_factory=dict)
    codex_sessions_seen: int = 0
    codex_subagent_sessions: int = 0


def discover_codex_subagent_sessions(sessions_dir: Path) -> tuple[dict[str, str], int, int]:
    """仅读取 Codex JSONL 中 type=session_meta 的行，不解析或输出消息正文。"""
    matches: dict[str, str] = {}
    files_seen = 0
    if not sessions_dir.is_dir():
        return matches, files_seen, 0
    for path in sessions_dir.glob("**/*.jsonl"):
        files_seen += 1
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if not _SESSION_META_MARKER.search(line):
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "session_meta":
                        continue
                    payload = record.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    rule = metadata_rule("codex", payload)
                    if rule is not None and rule.action == "skip_session":
                        session_id = path.stem.rsplit("-", 1)[-1]
                        matches[f"codex-{session_id}"] = rule.name
                        break
        except OSError:
            continue
    return matches, files_seen, len(matches)


def _source_type(namespace: str, conversation_id: str) -> str | None:
    value = namespace.casefold()
    for source in ("codex", "claude", "dsh"):
        if value == source or value.startswith(source + "-") or value.startswith(source + "_"):
            return source
        if conversation_id.startswith(source + "-"):
            return source
    return None


def _owner_rows(conn: sqlite3.Connection) -> sqlite3.Cursor:
    return conn.execute(
        """
        SELECT e.event_id, e.conversation_id, e.speaker_type,
               s.account_namespace AS source, r.raw_text, r.search_text,
               r.source_created_at
        FROM events e
        JOIN sources s ON s.source_id = e.source_id AND s.status = 'active'
        JOIN event_revisions r ON r.revision_id = e.current_revision_id
        WHERE e.speaker_type = 'owner'
          AND EXISTS (
            SELECT 1 FROM revision_policy_state ps
            WHERE ps.revision_id = r.revision_id
              AND ps.valid_to IS NULL AND ps.availability = 'available'
        )
        ORDER BY s.account_namespace, e.conversation_id, e.event_id
        """
    )


def _available_event_rows(conn: sqlite3.Connection) -> sqlite3.Cursor:
    return conn.execute(
        """
        SELECT e.event_id, e.conversation_id, e.speaker_type,
               s.account_namespace AS source
        FROM events e
        JOIN sources s ON s.source_id = e.source_id AND s.status = 'active'
        JOIN event_revisions r ON r.revision_id = e.current_revision_id
        WHERE EXISTS (
            SELECT 1 FROM revision_policy_state ps
            WHERE ps.revision_id = r.revision_id
              AND ps.valid_to IS NULL AND ps.availability = 'available'
        )
        ORDER BY e.conversation_id, e.event_id
        """
    )


def _body(row: sqlite3.Row) -> str:
    raw = row["raw_text"]
    return str(raw if raw is not None else row["search_text"] or "")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def analyze_injected(
    conn: sqlite3.Connection,
    *,
    codex_sessions_dir: Path | None = None,
    sample_size: int = 30,
    now: datetime | None = None,
) -> CleanPlan:
    """只读生成计划；不会改数据库、建迁移或触碰 WAL。"""
    plan = CleanPlan()
    session_rules: dict[str, str] = {}
    if codex_sessions_dir is not None:
        session_rules, plan.codex_sessions_seen, plan.codex_subagent_sessions = (
            discover_codex_subagent_sessions(codex_sessions_dir)
        )

    conversation_rules = dict(session_rules)
    direct_rules: dict[str, str] = {}
    for row in _owner_rows(conn):
        source_type = _source_type(row["source"], row["conversation_id"])
        if source_type is None:
            continue
        rule = text_rule(source_type, _body(row))
        if rule is None:
            continue
        if rule.action == "skip_session":
            conversation_rules.setdefault(row["conversation_id"], rule.name)
        else:
            direct_rules[row["event_id"]] = rule.name

    for row in _available_event_rows(conn):
        rule_name = conversation_rules.get(row["conversation_id"])
        if rule_name is None:
            rule_name = direct_rules.get(row["event_id"])
        if rule_name is not None:
            plan.event_reasons[row["event_id"]] = rule_name
            plan.event_counts[(row["source"], rule_name)] += 1

    rng = random.SystemRandom()
    withdrawn_owner_ids: set[str] = set()
    withdrawn_seen = retained_seen = 0
    for row in _owner_rows(conn):
        rule_name = plan.event_reasons.get(row["event_id"])
        body = _body(row)
        preview = OwnerPreview(
            event_id=row["event_id"],
            conversation_id=row["conversation_id"],
            source=row["source"],
            rule=rule_name,
            text=body[:40],
            char_count=len(body),
            source_created_at=row["source_created_at"],
        )
        if rule_name is None:
            retained_seen += 1
            if len(plan.retained_owners) < sample_size:
                plan.retained_owners.append(preview)
            else:
                slot = rng.randrange(retained_seen)
                if slot < sample_size:
                    plan.retained_owners[slot] = preview
            continue
        withdrawn_owner_ids.add(row["event_id"])
        plan.owner_count += 1
        plan.owner_chars += len(body)
        withdrawn_seen += 1
        if len(plan.withdrawn_owners) < sample_size:
            plan.withdrawn_owners.append(preview)
        else:
            slot = rng.randrange(withdrawn_seen)
            if slot < sample_size:
                plan.withdrawn_owners[slot] = preview
        key = (row["source"], rule_name)
        plan.counts[key] += 1
        plan.chars[key] += len(body)

    current: Counter[str] = Counter()
    current_chars: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    removed_chars: Counter[str] = Counter()
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(days=30)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
        cutoff = current_time - timedelta(days=30)
    current_time = current_time.astimezone(UTC)
    cutoff = cutoff.astimezone(UTC)
    for row in _owner_rows(conn):
        timestamp = _parse_time(row["source_created_at"])
        if timestamp is None:
            unknown[row["source"]] += 1
            continue
        if not cutoff <= timestamp <= current_time:
            continue
        current[row["source"]] += 1
        body_len = len(_body(row))
        current_chars[row["source"]] += body_len
        if row["event_id"] in withdrawn_owner_ids:
            removed[row["source"]] += 1
            removed_chars[row["source"]] += body_len
    sources = set(current) | set(removed) | set(unknown)
    for source in sources:
        plan.recent_current[source] = (current[source], current_chars[source])
        plan.recent_expected[source] = (
            current[source] - removed[source],
            current_chars[source] - removed_chars[source],
        )
        plan.recent_unknown[source] = unknown[source]
    return plan


def write_samples(plan: CleanPlan, destination: Path, sample_size: int = 30) -> tuple[int, int]:
    """随机抽样写出撤回与保留 owner 发言，每条正文最多 40 个字符。"""
    rng = random.SystemRandom()
    withdrawn = rng.sample(plan.withdrawn_owners, min(sample_size, len(plan.withdrawn_owners)))
    retained = rng.sample(plan.retained_owners, min(sample_size, len(plan.retained_owners)))
    lines = [
        "# D-11 owner 发言抽样核对",
        "",
        "每条只含正文前 40 个 Unicode 字符；撤回样本用于核对规则，保留样本用于检查误杀。",
        "",
    ]
    for title, entries in (("将被撤回", withdrawn), ("将被保留", retained)):
        lines.extend((f"## {title}（{len(entries)} 条）", ""))
        for index, item in enumerate(entries, 1):
            sample = item.text[:40]
            sample = _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", sample)
            sample = _SENSITIVE_TOKEN.sub("[REDACTED]", sample)
            sample = sample[:40]
            preview = json.dumps(sample, ensure_ascii=False)
            run = max((len(m.group(0)) for m in re.finditer(r"`+", preview)), default=0)
            fence = "`" * max(3, run + 1)
            rule_info = f" / rule={item.rule}" if item.rule else ""
            lines.extend(
                (
                    f"{index}. source={item.source}{rule_info}",
                    "",
                    f"   {fence}{preview}{fence}",
                    "",
                )
            )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return len(withdrawn), len(retained)


def rule_descriptions() -> list[InjectionRule]:
    return list(RULES)
