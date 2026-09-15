"""Vault 只读检索的合成安全与契约测试。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from personal_brain.retrieval import vault as vault_module
from personal_brain.retrieval.vault import VaultConfig, get_vault_note, search_vault


def _config(root: Path, *include: str, **kwargs) -> VaultConfig:
    return VaultConfig(root=root, include=tuple(include), **kwargs)


def _result(response: dict, path: str) -> dict:
    return next(item for item in response["results"] if item["path"] == path)


class TestVaultConfig:
    def test_include_is_explicitly_required(self, tmp_path: Path):
        with pytest.raises(ValueError, match="include"):
            VaultConfig(root=tmp_path, include=())

    def test_patterns_cannot_escape_root(self, tmp_path: Path):
        with pytest.raises(ValueError, match=r"\.\."):
            VaultConfig(root=tmp_path, include=("../outside/**",))
        with pytest.raises(ValueError, match="relative"):
            VaultConfig(root=tmp_path, include=("/outside/**",))


class TestVaultSafetyAndMatching:
    def test_path_traversal_and_symlink_are_not_read(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("outside-only-secret", encoding="utf-8")
        (vault / "inside.md").write_text("inside note", encoding="utf-8")
        (vault / "link.md").symlink_to(outside)

        config = _config(vault, "**/*.md")
        response = search_vault(config, "outside-only-secret")
        assert response["results"] == []
        assert "link.md" not in {item["path"] for item in search_vault(config, "inside")["results"]}

        traversal = get_vault_note(config, "../outside.md")
        assert traversal["error"] == "INVALID_PATH"
        linked = get_vault_note(config, "link.md")
        assert linked["error"] == "SYMLINK_REJECTED"

    def test_include_exclude_and_default_protected_paths(self, tmp_path: Path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "notes" / "private").mkdir()
        (vault / ".obsidian").mkdir()
        (vault / ".git").mkdir()
        (vault / "notes" / "keep.md").write_text("needle", encoding="utf-8")
        (vault / "notes" / "private" / "skip.md").write_text("needle", encoding="utf-8")
        (vault / ".obsidian" / "hidden.md").write_text("needle", encoding="utf-8")
        (vault / ".git" / "hidden.md").write_text("needle", encoding="utf-8")
        (vault / "AGENTS.md").write_text("needle", encoding="utf-8")
        (vault / "notes" / "image.txt").write_text("needle", encoding="utf-8")

        config = _config(vault, "notes/**", exclude=("notes/private/**",))
        response = search_vault(config, "needle")
        assert [item["path"] for item in response["results"]] == ["notes/keep.md"]

    def test_chinese_nfkc_casefold_and_line_locations(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "notes.md"
        note.write_text("# 计划\n第一行\nＡＩ 会帮助检索\n第三行\n", encoding="utf-8")

        response = search_vault(_config(vault, "*.md"), "ai")
        item = _result(response, "notes.md")
        assert item["line_start"] == item["line_end"] == 3
        assert item["title"] == "计划"
        assert item["heading"] == "计划"
        assert item["content_trust"] == "untrusted_archive"
        assert item["source_kind"] == "vault"
        assert item["modified_at_source"] == "filesystem"


class TestVaultBudgetsAndRevisions:
    def test_result_and_file_scan_limits_are_explicitly_truncated(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        for name in ("a.md", "b.md", "c.md"):
            (vault / name).write_text("needle", encoding="utf-8")

        response = search_vault(_config(vault, "*.md", max_files=2), "needle", limit=1)
        assert len(response["results"]) == 1
        assert response["truncated"] is True
        assert any("limit" in warning for warning in response["warnings"])

    def test_hash_is_for_full_content_and_stale_reference_is_rejected(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "note.md"
        note.write_text("# Note\nneedle\nfirst tail\n", encoding="utf-8")
        config = _config(vault, "*.md")

        first = _result(search_vault(config, "needle"), "note.md")
        expected = hashlib.sha256(note.read_bytes()).hexdigest()
        assert first["revision_id"] == expected
        assert expected in first["citation_id"]

        note.write_text("# Note\nneedle\nchanged tail\n", encoding="utf-8")
        stale = get_vault_note(config, "note.md", revision_id=first["revision_id"])
        assert stale["error"] == "STALE_REFERENCE"

        current = get_vault_note(config, "note.md")
        assert current["revision_id"] != first["revision_id"]
        assert current["content_trust"] == "untrusted_archive"

    def test_same_matching_snippet_different_full_documents_have_distinct_hashes(
        self, tmp_path: Path
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "one.md").write_text("needle\nA", encoding="utf-8")
        (vault / "two.md").write_text("needle\nB", encoding="utf-8")
        response = search_vault(_config(vault, "*.md"), "needle")
        one = _result(response, "one.md")
        two = _result(response, "two.md")
        assert one["snippet"].splitlines()[0] == two["snippet"].splitlines()[0]
        assert one["revision_id"] != two["revision_id"]

    def test_secret_looking_file_is_skipped_without_exposing_content(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        secret = "sk-proj-123456789012345678901234567890"
        (vault / "credentials.md").write_text(secret, encoding="utf-8")
        (vault / "ordinary.md").write_text("safe note", encoding="utf-8")
        config = _config(vault, "*.md")

        response = search_vault(config, "sk-proj")
        assert response["results"] == []
        assert any("DLP" in warning for warning in response["warnings"])
        direct = get_vault_note(config, "credentials.md")
        assert direct["error"] == "NOT_FOUND_OR_NOT_ALLOWED"

    def test_get_note_line_and_text_budgets(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "long.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        result = get_vault_note(
            _config(vault, "*.md"), "long.md", start_line=2, max_lines=2, max_text_chars=3
        )
        assert result["content"] == "two"
        assert result["line_start"] == 2
        assert result["line_end"] == 3
        assert result["truncated"] is True


@pytest.mark.parametrize("query", ["", "   ", "\u3000"])
def test_empty_query_is_invalid(tmp_path: Path, query: str):
    assert search_vault(_config(tmp_path, "*.md"), query)["error"] == "INVALID_PARAMS"


def test_missing_root_is_source_unavailable(tmp_path: Path):
    config = _config(tmp_path / "missing", "*.md")
    assert search_vault(config, "needle")["error"] == "SOURCE_UNAVAILABLE"


def test_each_snippet_is_bounded_without_starving_other_results(tmp_path: Path):
    for index in range(10):
        (tmp_path / f"{index}.md").write_text("needle " + "正文" * 5000, encoding="utf-8")
    response = search_vault(_config(tmp_path, "*.md"), "needle")
    assert len(response["results"]) == 10
    assert all(0 < len(item["snippet"]) <= 400 for item in response["results"])
    assert all("needle" in item["snippet"] for item in response["results"])
    assert response["truncated"]


@pytest.mark.parametrize("replace_parent", [False, True])
def test_symlink_swap_after_validation_is_rejected(tmp_path: Path, monkeypatch, replace_parent):
    root = tmp_path / "vault"
    parent = root / "notes"
    parent.mkdir(parents=True)
    (parent / "note.md").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("must never return", encoding="utf-8")
    config = _config(root, "notes/**")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        target = "notes" if replace_parent else "note.md"
        if path == target and not swapped:
            swapped = True
            if replace_parent:
                parent.rename(root / "saved")
                parent.symlink_to(outside, target_is_directory=True)
            else:
                (parent / "note.md").unlink()
                (parent / "note.md").symlink_to(outside / "note.md")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "open", swapping_open)
    response = get_vault_note(config, "notes/note.md")
    assert swapped
    assert "error" in response
    assert "must never return" not in str(response)


def test_growing_file_read_is_bounded(tmp_path: Path, monkeypatch):
    note = tmp_path / "note.md"
    note.write_text("needle", encoding="utf-8")
    config = _config(tmp_path, "*.md", max_file_bytes=32)
    real_read = os.read
    sizes: list[int] = []
    read_bytes = 0

    def growing_read(fd, size):
        nonlocal read_bytes
        if not sizes:
            with note.open("ab") as stream:
                stream.write(b"x" * 1000)
        sizes.append(size)
        data = real_read(fd, size)
        read_bytes += len(data)
        return data

    monkeypatch.setattr(vault_module.os, "read", growing_read)
    assert get_vault_note(config, "note.md")["error"] == "FILE_TOO_LARGE"
    assert max(sizes) <= 33
    assert read_bytes <= 33


def test_secret_in_ordinary_filename_skips_entire_file(tmp_path: Path):
    (tmp_path / "ordinary.md").write_text("needle\n" + "sk-proj-" + "A" * 40, encoding="utf-8")
    config = _config(tmp_path, "*.md")
    assert search_vault(config, "needle")["results"] == []
    assert get_vault_note(config, "ordinary.md")["error"] == "SECRET_FILTERED"
