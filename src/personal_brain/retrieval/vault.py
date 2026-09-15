"""只读的 Obsidian Vault 检索。

这个模块有意不把 Vault 内容导入 Personal Brain 数据库，也不执行或解释
Markdown 中的文字。返回值只是带有内容信任标记的原始证据片段；后续的
统一服务层可以在更高层组合它与 ChatGPT 历史检索。

安全边界是“尽量不因检索器误读而扩大读取范围”，不是 OS 级隔离：拥有相同
OS 身份的调用方仍可能直接读取 Vault。秘密检测也是已知形态的启发式防线，
不是完整 DLP。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from personal_brain.retrieval.credentials import contains_known_credentials

_MAX_RESULT_LIMIT = 50
_MAX_TEXT_CHARS = 8_000
_MAX_LINES = 80
_MAX_SNIPPET_CHARS = 400

_BASE_WARNINGS = (
    "modified_at is filesystem time, not document fact time",
    "secret filtering is heuristic and is not a complete DLP guarantee",
)

_ATX_HEADING = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)\s*$")
_SETEXT_UNDERLINE = re.compile(r"^\s*(?:=+|-+)\s*$")

# These names are deliberately conservative. They apply before content search,
# so a credential-looking Markdown file is not made visible merely because its
# contents do not happen to match a query.
_CREDENTIAL_NAME = re.compile(
    r"(?:^|[._-])(?:env|api[-_]?keys?|credentials?|secrets?|tokens?|passwords?|"
    r"passwd|private[-_]?keys?|id_(?:rsa|dsa|ecdsa|ed25519))(?:[._-]|$)",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class VaultConfig:
    """限制在一个本地 Markdown Vault 内的只读配置。

    ``include`` 是显式白名单，不能为空。模式只用于匹配 Vault 内的相对
    POSIX 路径或目录名；不允许用绝对路径或 ``..`` 改变 root 边界。
    """

    root: Path
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    max_file_bytes: int = 1_000_000
    max_files: int = 10_000

    def __post_init__(self) -> None:
        root = Path(self.root)
        if not str(root):
            raise ValueError("root must not be empty")

        # Reject a configured symlink root. Comparing absolute and resolved paths
        # also rejects a symlink in an ancestor component. A later replacement by
        # a symlink is checked again at read time.
        absolute_root = root.absolute()
        if absolute_root != absolute_root.resolve(strict=False):
            raise ValueError("root must not contain symlink components")
        if root.is_symlink():
            raise ValueError("root must not be a symlink")

        include = tuple(self.include)
        exclude = tuple(self.exclude)
        if not include:
            raise ValueError("include must not be empty")
        for pattern in (*include, *exclude):
            _validate_pattern(pattern)

        if (
            not isinstance(self.max_file_bytes, int)
            or isinstance(self.max_file_bytes, bool)
            or self.max_file_bytes <= 0
        ):
            raise ValueError("max_file_bytes must be a positive integer")
        if (
            not isinstance(self.max_files, int)
            or isinstance(self.max_files, bool)
            or self.max_files <= 0
        ):
            raise ValueError("max_files must be a positive integer")

        object.__setattr__(self, "root", absolute_root)
        object.__setattr__(self, "include", include)
        object.__setattr__(self, "exclude", exclude)


@dataclass(frozen=True)
class _Candidate:
    path: Path
    relative: str


@dataclass(frozen=True)
class _LoadedNote:
    candidate: _Candidate
    raw: bytes
    text: str
    revision_id: str
    modified_at: str


def _validate_pattern(pattern: str) -> None:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("include/exclude patterns must be non-empty strings")
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError("include/exclude patterns must be relative")
    if normalized.startswith("~"):
        raise ValueError("include/exclude patterns must not use '~'")
    if "\x00" in normalized:
        raise ValueError("include/exclude patterns must not contain NUL")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("include/exclude patterns must not contain '..'")


def _validate_request_limit(value: int, *, name: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, maximum)


def _runtime_root(config: VaultConfig) -> Path | None:
    """Return root only if it still exists and contains no symlink component."""

    root = config.root
    if root.is_symlink() or not root.is_dir():
        return None
    absolute_root = root.absolute()
    if absolute_root != absolute_root.resolve(strict=False):
        return None
    return absolute_root


def _normalize_pattern(pattern: str) -> str:
    normalized = pattern.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while normalized.endswith("/") and normalized != "/":
        normalized = normalized[:-1]
    return normalized


def _pattern_matches(relative: str, pattern: str) -> bool:
    """Match a relative path while making ``**/name`` useful at root level."""

    relative = relative.replace(os.sep, "/")
    pattern = _normalize_pattern(pattern)
    if fnmatch.fnmatchcase(relative, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:]):
        return True
    return False


def _ancestor_paths(relative: str) -> Iterator[str]:
    parts = PurePosixPath(relative).parts
    for end in range(1, len(parts)):
        yield "/".join(parts[:end])


def _default_excluded(relative: str, *, is_dir: bool) -> bool:
    parts = PurePosixPath(relative).parts
    if is_dir and any(part.startswith(".") for part in parts):
        return True
    if not is_dir and any(part.startswith(".") for part in parts[:-1]):
        return True
    if not is_dir and parts and parts[-1] == "AGENTS.md":
        return True
    return False


def _credential_filename(relative: str) -> bool:
    name = PurePosixPath(relative).name
    return bool(_CREDENTIAL_NAME.search(name))


def _is_excluded(config: VaultConfig, relative: str, *, is_dir: bool) -> bool:
    if _default_excluded(relative, is_dir=is_dir):
        return True
    if not is_dir and _credential_filename(relative):
        return True

    values = (relative,) if is_dir else (relative, *_ancestor_paths(relative))
    return any(_pattern_matches(value, pattern) for value in values for pattern in config.exclude)


def _is_included(config: VaultConfig, relative: str) -> bool:
    """Apply the explicit include allow-list to a file.

    A directory pattern includes descendants. For example, ``notes`` and
    ``notes/**`` both allow ``notes/today.md``. Files are still required to be
    Markdown files by the caller.
    """

    values = (relative, *_ancestor_paths(relative))
    return any(_pattern_matches(value, pattern) for value in values for pattern in config.include)


def _safe_relative_path(path: str | Path) -> tuple[str | None, str | None]:
    """Validate a user-supplied Vault-relative path without resolving it."""

    if not isinstance(path, (str, Path)):
        return None, "path must be a Vault-relative string"
    raw = str(path).replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        return None, "path must be a Vault-relative path"
    if "\x00" in raw:
        return None, "path contains NUL"
    parts = PurePosixPath(raw).parts
    if not parts or "." in parts or ".." in parts:
        return None, "path traversal is not allowed"
    relative = "/".join(parts)
    if not relative.lower().endswith(".md"):
        return None, "only Markdown notes are readable"
    return relative, None


def _safe_candidate(root: Path, relative: str) -> tuple[Path | None, str | None]:
    """Build a path while rejecting symlink components and missing components."""

    current = root
    for component in PurePosixPath(relative).parts:
        current = current / component
        try:
            if current.is_symlink():
                return None, "SYMLINK_REJECTED"
        except OSError:
            return None, "NOT_FOUND"
    try:
        if not current.exists():
            return None, "NOT_FOUND"
        if not current.is_file():
            return None, "NOT_FOUND"
    except OSError:
        return None, "NOT_FOUND"
    return current, None


def _scan_candidates(config: VaultConfig) -> tuple[list[_Candidate], bool, list[str]]:
    """Walk root without following symlinks and collect bounded file metadata."""

    root = _runtime_root(config)
    if root is None:
        return [], False, ["vault root is missing, not a directory, or contains a symlink"]

    candidates: list[_Candidate] = []
    warnings: list[str] = []
    stack: list[tuple[Path, str]] = [(root, "")]
    truncated = False

    while stack:
        directory, directory_relative = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            warnings.append("one or more vault directories could not be scanned")
            continue

        # Reverse push gives deterministic ascending traversal with a stack.
        for entry in reversed(entries):
            relative = f"{directory_relative}/{entry.name}" if directory_relative else entry.name
            relative = relative.replace(os.sep, "/")
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not _is_excluded(config, relative, is_dir=True):
                        stack.append((Path(entry.path), relative))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                warnings.append("one or more vault entries could not be inspected")
                continue

            if not relative.lower().endswith(".md"):
                continue
            if _is_excluded(config, relative, is_dir=False) or not _is_included(config, relative):
                continue
            if len(candidates) >= config.max_files:
                truncated = True
                break
            candidates.append(_Candidate(Path(entry.path), relative))

        if truncated:
            break

    if truncated:
        warnings.append(f"file scan limit reached at max_files={config.max_files}")
    return candidates, truncated, warnings


def _modified_at(stat_result: os.stat_result) -> str:
    return datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z")


def _contains_known_secret(raw: bytes) -> bool:
    # Ignore undecodable bytes for pattern matching. Hashing and size checks still
    # operate on the complete original byte sequence.
    text = raw.decode("utf-8", errors="ignore")
    return contains_known_credentials(text)


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    """Open every directory component without following symlinks, including root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        yield fd
    finally:
        os.close(fd)


def _load_candidate(
    config: VaultConfig, candidate: _Candidate
) -> tuple[_LoadedNote | None, str | None]:
    root = _runtime_root(config)
    if root is None:
        return None, "SOURCE_UNAVAILABLE"
    relative, path_error = _safe_relative_path(candidate.relative)
    if path_error or relative is None:
        return None, "INVALID_PATH"
    safe_path, path_error = _safe_candidate(root, relative)
    if safe_path is None:
        return None, path_error
    if _is_excluded(config, relative, is_dir=False) or not _is_included(config, relative):
        return None, "NOT_FOUND_OR_NOT_ALLOWED"

    # Reopen by directory descriptors after validation. O_NOFOLLOW closes the
    # check/open race for both parent directories and the final file. NONBLOCK
    # prevents a replacement FIFO from hanging before the regular-file check.
    try:
        with _open_directory(safe_path.parent) as parent_fd:
            fd = os.open(
                safe_path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
            try:
                stat_before = os.fstat(fd)
                if not stat.S_ISREG(stat_before.st_mode):
                    return None, "UNREADABLE"
                if stat_before.st_size > config.max_file_bytes:
                    return None, "FILE_TOO_LARGE"
                remaining = config.max_file_bytes + 1
                chunks: list[bytes] = []
                while remaining:
                    chunk = os.read(fd, min(remaining, 65_536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                stat_after = os.fstat(fd)
            finally:
                os.close(fd)
    except OSError:
        return None, "UNREADABLE"
    if len(raw) > config.max_file_bytes or stat_after.st_size > config.max_file_bytes:
        return None, "FILE_TOO_LARGE"
    if (stat_before.st_size, stat_before.st_mtime_ns, stat_before.st_ctime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
        stat_after.st_ctime_ns,
    ):
        return None, "UNREADABLE"
    if _contains_known_secret(raw):
        return None, "SECRET_FILTERED"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "INVALID_UTF8"
    revision_id = hashlib.sha256(raw).hexdigest()
    return (
        _LoadedNote(
            candidate=candidate,
            raw=raw,
            text=text,
            revision_id=revision_id,
            modified_at=_modified_at(stat_after),
        ),
        None,
    )


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        piece = unicodedata.normalize("NFKC", character).casefold()
        normalized.append(piece)
        offsets.extend([index] * len(piece))
    offsets.append(len(text))
    return "".join(normalized), offsets


def _document_title_and_heading(
    text: str, line_number: int, fallback: str
) -> tuple[str, str | None]:
    lines = text.splitlines()
    title: str | None = None
    heading: str | None = None
    for index, line in enumerate(lines):
        match = _ATX_HEADING.match(line)
        if match:
            value = match.group(2).strip()
            value = re.sub(r"\s+#+\s*$", "", value).strip()
            if title is None and len(match.group(1)) == 1:
                title = value
            if index + 1 <= line_number:
                heading = value
        elif (
            index > 0
            and _SETEXT_UNDERLINE.match(line)
            and lines[index - 1].strip()
            and title is None
        ):
            title = lines[index - 1].strip()
            if index + 1 <= line_number:
                heading = title
    if title is None:
        title = fallback
    return title, heading


def _raw_span_to_lines(text: str, start: int, end: int) -> tuple[int, int]:
    line_start = text.count("\n", 0, start) + 1
    last_character = max(start, end - 1)
    line_end = text.count("\n", 0, last_character) + 1
    return line_start, line_end


def _bounded_snippet(text: str, start: int, end: int, max_chars: int) -> tuple[str, bool]:
    """Return a bounded raw-text window around a match."""

    if len(text) <= max_chars:
        return text, False
    if max_chars == 1:
        return "…", True

    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    marker_budget = len(prefix) + len(suffix)
    available = max(1, max_chars - marker_budget)
    match_length = max(1, end - start)
    if match_length >= available:
        left = start
        right = min(len(text), start + available)
    else:
        context = available - match_length
        left = max(0, start - context // 2)
        right = min(len(text), end + context - (start - left))
        if right - left < available:
            left = max(0, right - available)

    body = text[left:right]
    result = ("…" if left > 0 else "") + body + ("…" if right < len(text) else "")
    if len(result) > max_chars:
        result = result[:max_chars]
    return result, True


def _clip_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _citation_id(relative: str, revision_id: str) -> str:
    # The full-content hash is part of the citation. Including the relative path
    # keeps two identical notes distinguishable without using an absolute path.
    return f"pb:vault:{revision_id}:{relative}"


def _common_warning_list() -> list[str]:
    return list(_BASE_WARNINGS)


def _error_result(code: str, warnings: list[str] | None = None, **extra: object) -> dict:
    result: dict[str, object] = {
        "error": code,
        "error_code": code,
        "warnings": warnings if warnings is not None else _common_warning_list(),
    }
    result.update(extra)
    return result


def search_vault(
    config: VaultConfig,
    query: str,
    *,
    limit: int = 10,
    max_text_chars: int = _MAX_TEXT_CHARS,
) -> dict:
    """在允许的 Markdown 文件内做 NFKC + casefold 连续子串检索。

    ``limit`` 和 ``max_text_chars`` 只能下调到模块硬上限；超过资源边界时
    ``truncated`` 为 true。搜索只返回相关文件的一条首个命中片段，不对文档
    内容做画像、摘要或指令执行。
    """

    try:
        result_limit = _validate_request_limit(limit, name="limit", maximum=_MAX_RESULT_LIMIT)
        text_budget = _validate_request_limit(
            max_text_chars, name="max_text_chars", maximum=_MAX_TEXT_CHARS
        )
    except ValueError as exc:
        return _error_result("INVALID_PARAMS", _common_warning_list() + [str(exc)])
    if not isinstance(query, str):
        return _error_result("INVALID_PARAMS", _common_warning_list() + ["query must be a string"])

    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    if not normalized_query.strip():
        return _error_result("INVALID_PARAMS")
    if _runtime_root(config) is None:
        return _error_result("SOURCE_UNAVAILABLE")
    try:
        with _open_directory(config.root) as root_fd, os.scandir(root_fd):
            pass
    except OSError:
        return _error_result("SOURCE_UNAVAILABLE")

    candidates, scan_truncated, warnings = _scan_candidates(config)
    warnings = _common_warning_list() + warnings
    limit_was_capped = limit > result_limit
    text_was_capped = (
        isinstance(max_text_chars, int)
        and not isinstance(max_text_chars, bool)
        and max_text_chars > text_budget
    )
    if limit_was_capped:
        warnings.append(f"limit capped at {_MAX_RESULT_LIMIT}")
    if text_was_capped:
        warnings.append(f"max_text_chars capped at {_MAX_TEXT_CHARS}")
    matched: list[dict[str, object]] = []
    skipped_large = 0
    skipped_secret = 0
    skipped_unreadable = 0
    skipped_invalid = 0
    snippet_was_truncated = False

    for candidate in candidates:
        note, reason = _load_candidate(config, candidate)
        if note is None:
            if reason == "FILE_TOO_LARGE":
                skipped_large += 1
            elif reason == "SECRET_FILTERED":
                skipped_secret += 1
            elif reason == "INVALID_UTF8":
                skipped_invalid += 1
            else:
                skipped_unreadable += 1
            continue

        normalized_text, offsets = _normalize_with_map(note.text)
        match_start = normalized_text.find(normalized_query)
        if match_start < 0:
            continue
        match_end = match_start + len(normalized_query)
        raw_start = offsets[match_start]
        raw_end = offsets[match_end]
        line_start, line_end = _raw_span_to_lines(note.text, raw_start, raw_end)
        snippet, snippet_truncated = _bounded_snippet(
            note.text, raw_start, raw_end, min(text_budget, _MAX_SNIPPET_CHARS)
        )
        snippet_was_truncated = snippet_was_truncated or snippet_truncated
        title, heading = _document_title_and_heading(
            note.text, line_start, Path(candidate.relative).stem
        )
        matched.append(
            {
                "path": candidate.relative,
                "vault_path": candidate.relative,
                "heading": heading,
                "title": title,
                "snippet": snippet,
                "line_start": line_start,
                "line_end": line_end,
                "revision_id": note.revision_id,
                "citation_id": _citation_id(candidate.relative, note.revision_id),
                "modified_at": note.modified_at,
                "modified_at_source": "filesystem",
                "content_trust": "untrusted_archive",
                "source_kind": "vault",
            }
        )

    if skipped_large:
        warnings.append(f"{skipped_large} file(s) exceeded max_file_bytes and were skipped")
    if skipped_secret:
        warnings.append(f"{skipped_secret} file(s) were skipped by secret heuristics")
    if skipped_invalid:
        warnings.append(f"{skipped_invalid} file(s) were skipped because they were not UTF-8")
    if skipped_unreadable:
        warnings.append(f"{skipped_unreadable} file(s) could not be read")

    matched.sort(key=lambda item: str(item["path"]))
    truncated = (
        scan_truncated
        or snippet_was_truncated
        or skipped_large > 0
        or skipped_invalid > 0
        or skipped_unreadable > 0
        or limit_was_capped
        or text_was_capped
        or len(matched) > result_limit
    )
    visible = matched[:result_limit]

    # Apply one response-level snippet budget after result limiting, preserving
    # line locations even when a result's displayed text must be shortened.
    remaining = text_budget
    text_truncated = False
    for item in visible:
        snippet = str(item["snippet"])
        if len(snippet) > remaining:
            item["snippet"] = snippet[:remaining]
            text_truncated = True
            remaining = 0
        else:
            remaining -= len(snippet)
    if text_truncated:
        truncated = True
        warnings.append("text budget reached; returned snippets are truncated")
    if snippet_was_truncated:
        warnings.append("one or more snippets were bounded by max_text_chars")
    if len(matched) > result_limit:
        warnings.append(f"result limit reached at limit={result_limit}")

    return {"results": visible, "truncated": truncated, "warnings": warnings}


def get_vault_note(
    config: VaultConfig,
    path: str | Path,
    *,
    revision_id: str | None = None,
    start_line: int = 1,
    max_lines: int = _MAX_LINES,
    max_text_chars: int = _MAX_TEXT_CHARS,
) -> dict:
    """读取 Vault 内一段 Markdown；可用全文 SHA-256 检查引用是否过期。"""

    warnings = _common_warning_list()
    try:
        requested_start = _validate_request_limit(start_line, name="start_line", maximum=10**9)
        requested_lines = _validate_request_limit(max_lines, name="max_lines", maximum=_MAX_LINES)
        text_budget = _validate_request_limit(
            max_text_chars, name="max_text_chars", maximum=_MAX_TEXT_CHARS
        )
    except ValueError as exc:
        return _error_result("INVALID_PARAMS", warnings + [str(exc)])

    relative, path_error = _safe_relative_path(path)
    if path_error is not None or relative is None:
        return _error_result("INVALID_PATH", warnings + [path_error or "invalid path"])

    root = _runtime_root(config)
    if root is None:
        return _error_result("NOT_FOUND", warnings + ["vault root is unavailable"])
    if _is_excluded(config, relative, is_dir=False) or not _is_included(config, relative):
        return _error_result("NOT_FOUND_OR_NOT_ALLOWED", warnings)

    candidate_path, candidate_error = _safe_candidate(root, relative)
    if candidate_path is None:
        return _error_result(candidate_error or "NOT_FOUND", warnings)
    candidate = _Candidate(candidate_path, relative)
    note, load_error = _load_candidate(config, candidate)
    if note is None:
        code = {
            "FILE_TOO_LARGE": "FILE_TOO_LARGE",
            "SECRET_FILTERED": "SECRET_FILTERED",
            "INVALID_UTF8": "INVALID_UTF8",
            "SYMLINK_REJECTED": "SYMLINK_REJECTED",
        }.get(load_error or "", "NOT_FOUND")
        return _error_result(code, warnings)

    if revision_id is not None and revision_id != note.revision_id:
        # Do not return current content with an old citation. The simple string
        # code is intentionally easy for MCP/HTTP adapters to recognize.
        return _error_result("STALE_REFERENCE", warnings)

    all_lines = note.text.splitlines(keepends=True)
    first_index = requested_start - 1
    selected_lines = all_lines[first_index : first_index + requested_lines]
    content = "".join(selected_lines)
    content, text_truncated = _clip_text(content, text_budget)
    actual_line_end = requested_start + len(selected_lines) - 1
    line_start = requested_start if selected_lines else requested_start
    title, heading = _document_title_and_heading(note.text, line_start, Path(relative).stem)

    result: dict[str, object] = {
        "path": relative,
        "vault_path": relative,
        "heading": heading,
        "title": title,
        "content": content,
        "line_start": line_start,
        "line_end": actual_line_end,
        "revision_id": note.revision_id,
        "citation_id": _citation_id(relative, note.revision_id),
        "modified_at": note.modified_at,
        "modified_at_source": "filesystem",
        "content_trust": "untrusted_archive",
        "source_kind": "vault",
        "truncated": text_truncated or len(selected_lines) < len(all_lines[first_index:]),
        "warnings": warnings,
    }
    if result["truncated"]:
        result["warnings"] = [
            *warnings,
            "note text or line budget reached; returned content is truncated",
        ]
    return result
