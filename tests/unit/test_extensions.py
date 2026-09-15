"""Local extension catalog tests use only synthetic manifests."""

from __future__ import annotations

import json
from pathlib import Path

from personal_brain.extensions import discover_local_extensions, filter_extensions


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_discovery_classifies_codex_and_dsh_extensions(tmp_path: Path):
    skill_root = tmp_path / "codex-skills"
    (skill_root / "visible-skill").mkdir(parents=True)
    (skill_root / "visible-skill/SKILL.md").write_text(
        "---\nname: visible-skill\ndescription: Synthetic local skill\n---\n\nUse it.\n",
        encoding="utf-8",
    )

    plugin_root = tmp_path / "codex-cache"
    plugin_manifest = plugin_root / "personal/fixture-plugin/1.0.0/.codex-plugin/plugin.json"
    _write_json(
        plugin_manifest,
        {
            "name": "fixture-plugin",
            "version": "1.0.0",
            "description": "Synthetic plugin",
            "interface": {"displayName": "Fixture Plugin", "capabilities": ["read"]},
        },
    )
    config = tmp_path / "config.toml"
    config.write_text('[plugins."fixture-plugin@personal"]\nenabled = true\n', encoding="utf-8")

    profiles_root = tmp_path / "profiles"
    profile = profiles_root / "web"
    _write_json(
        profile / "package.json",
        {
            "name": "dsh-profile-web",
            "dependencies": {"fixture-dsh": "0.1.0"},
            "dsh": {"profile": {"bundles": ["fixture-dsh"]}},
        },
    )
    _write_json(
        profile / "node_modules/fixture-dsh/package.json",
        {
            "name": "fixture-dsh",
            "version": "0.1.0",
            "description": "Synthetic DSH bridge",
            "dsh": {"bundle": {"patch": "./fixture.patch.yml"}},
        },
    )
    _write_json(
        profile / "node_modules/@scope/extra-dsh/package.json",
        {
            "name": "@scope/extra-dsh",
            "version": "0.2.0",
            "dshWorkshop": {"permissions": ["read"]},
        },
    )

    snapshot = discover_local_extensions(
        max_items=100,
        roots={
            "codex_skill_roots": (skill_root,),
            "codex_plugin_cache_roots": (plugin_root,),
            "codex_config": config,
            "dsh_profiles_root": profiles_root,
            "project_roots": (),
            "expose_paths": False,
        },
    )

    assert snapshot["read_only"] is True
    assert snapshot["path_visibility"] == "hidden"
    assert snapshot["counts"]["total"] == len(snapshot["results"])
    json.dumps(snapshot)

    by_name = {item["name"]: item for item in snapshot["results"]}
    assert by_name["visible-skill"]["kind"] == "codex_skill"
    assert by_name["fixture-plugin"]["status"] == "active"
    assert by_name["fixture-dsh"]["status"] == "active"
    assert by_name["@scope/extra-dsh"]["status"] == "installed_not_bundled"


def test_filter_matches_all_query_terms_and_preserves_safety_note(tmp_path: Path):
    snapshot = discover_local_extensions(
        max_items=20,
        roots={
            "codex_skill_roots": (),
            "codex_plugin_cache_roots": (),
            "codex_config": tmp_path / "missing.toml",
            "dsh_profiles_root": tmp_path / "missing-profiles",
            "project_roots": (),
        },
    )
    snapshot["results"] = [
        {
            "kind": "codex_skill",
            "name": "memory-recall",
            "display_name": "memory-recall",
            "description": "Search local history",
            "source": "codex_skill_root",
            "status": "discovered",
        },
        {
            "kind": "codex_skill",
            "name": "other",
            "display_name": "other",
            "description": "Unrelated helper",
            "source": "codex_skill_root",
            "status": "discovered",
        },
    ]

    filtered = filter_extensions(snapshot, query="memory history", limit=10)
    assert [item["name"] for item in filtered["results"]] == ["memory-recall"]
    assert filtered["matched"] == 1
    assert "不会导入、安装、执行插件" in filtered["note"]
