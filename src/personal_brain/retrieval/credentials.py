"""Known credential-shape detection and same-length redaction.

This is a conservative heuristic boundary for remote retrieval responses.  It
is deliberately not presented as complete DLP: credentials with an unknown
format, transformed values, or values split across unrelated fields may still
need a stronger policy or a human review.
"""

from __future__ import annotations

import re

# Keep these patterns conservative and high-confidence.  The same detector is
# used for Vault candidate filtering and for remote ChatGPT-history delivery so
# the two read-only paths do not silently drift apart.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
        r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"refresh[_-]?token|id[_-]?token|client[_-]?secret|password|passwd|"
        r"private[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"'`]{8,}"
    ),
)


def credential_spans(text: str | None) -> tuple[tuple[int, int], ...]:
    """Return merged character spans matching known credential shapes."""

    if not text:
        return ()
    spans = [match.span() for pattern in _CREDENTIAL_PATTERNS for match in pattern.finditer(text)]
    if not spans:
        return ()
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged if end > start)


def contains_known_credentials(text: str | None) -> bool:
    """Return whether text contains at least one known credential shape."""

    return bool(credential_spans(text))


def redact_known_credentials(text: str | None) -> str | None:
    """Mask known credential spans while preserving text length and offsets."""

    if text is None:
        return None
    spans = credential_spans(text)
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append("█" * (end - start))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
