"""Read-only discovery of local Codex skills, Codex plugins, and DSH bundles.

The catalog deliberately reads manifests only.  It never imports a plugin,
executes a discovered command, installs a package, or reads manifest-adjacent
credential files.  This makes it suitable as a small MCP bridge shared by
Codex and DeepSeek Harness.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

MAX_MANIFEST_BYTES = 512_000
MAX_SKILL_BYTES = 256_000
DEFAULT_MAX_ITEMS = 1_000
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "dist", "build"}


class ExtensionScanError(ValueError):
    """A caller supplied an invalid extension scan argument."""


def _path_list_from_env(name: str) -> tuple[Path, ...]:
    raw = os.environ.get(name, "")
    return tuple(Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip())


def _discover_dsh_runtime_roots() -> tuple[Path, ...]:
    """Find the node_modules root behind the installed ``dsh`` launcher."""

    launcher = Path.home() / ".local" / "bin" / "dsh"
    try:
        resolved = launcher.resolve(strict=True)
    except OSError:
        return ()
    for parent in resolved.parents:
        if parent.name == "node_modules":
            return (parent,)
    return ()


def default_roots() -> dict[str, Any]:
    """Return the narrow, standard local roots used by the bridge.

    Project roots are opt-in through ``PERSONAL_BRAIN_EXTENSION_PROJECT_ROOTS``
    so a request to inspect the agent installations does not silently crawl a
    whole Documents directory.
    """

    home = Path.home()
    return {
        "codex_skill_roots": (
            home / ".codex" / "skills",
            home / ".agents" / "skills",
        ),
        "codex_plugin_cache_roots": (home / ".codex" / "plugins" / "cache",),
        "codex_config": home / ".codex" / "config.toml",
        "dsh_profiles_root": home / ".dsh" / "profiles",
        "dsh_runtime_package_roots": _discover_dsh_runtime_roots(),
        "project_roots": _path_list_from_env("PERSONAL_BRAIN_EXTENSION_PROJECT_ROOTS"),
        "expose_paths": os.environ.get("PERSONAL_BRAIN_EXTENSION_EXPOSE_PATHS", "1")
        not in {"0", "false", "no"},
    }


def _safe_path(value: Path) -> Path:
    return value.expanduser()


def _path_value(path: Path, expose_paths: bool) -> str | None:
    return str(path) if expose_paths else None


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            warnings.append(f"manifest too large: {path}")
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(f"invalid manifest skipped: {path} ({type(exc).__name__})")
        return None
    return data if isinstance(data, dict) else None


def _read_toml(path: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            warnings.append(f"config too large: {path}")
            return {}
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"invalid config skipped: {path} ({type(exc).__name__})")
        return {}


def _read_frontmatter(path: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        if path.stat().st_size > MAX_SKILL_BYTES:
            warnings.append(f"skill too large: {path}")
            return {}
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        warnings.append(f"skill metadata unreadable: {path} ({type(exc).__name__})")
        return {}
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    match = re.search(r"\n---\s*(?:\n|$)", text[3:])
    if match is None:
        warnings.append(f"skill frontmatter unterminated: {path}")
        return {}
    body = text[3 : 3 + match.start()]
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        warnings.append(f"skill frontmatter invalid: {path} ({type(exc).__name__})")
        return {}
    return data if isinstance(data, dict) else {}


def _walk_files(root: Path, filename: str, max_files: int, warnings: list[str]) -> list[Path]:
    root = _safe_path(root)
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        warnings.append(f"scan root is not a directory: {root}")
        return []
    found: list[Path] = []
    try:
        for path in root.rglob(filename):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            found.append(path)
            if len(found) >= max_files:
                warnings.append(f"scan limit reached under: {root}")
                break
    except OSError as exc:
        warnings.append(f"scan root unreadable: {root} ({type(exc).__name__})")
    return sorted(found)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _base_record(
    *,
    kind: str,
    name: str,
    display_name: str,
    description: str,
    version: str,
    status: str,
    source: str,
    path: Path | None,
    expose_paths: bool,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "display_name": display_name or name,
        "description": description,
        "version": version,
        "status": status,
        "source": source,
        "path": _path_value(path, expose_paths) if path else None,
    }
    result.update(extra)
    return result


def _codex_enabled_plugins(config_path: Path | None, warnings: list[str]) -> set[str]:
    if config_path is None or not config_path.exists():
        return set()
    data = _read_toml(config_path, warnings)
    enabled: set[str] = set()
    raw_plugins = data.get("plugins", {})
    if not isinstance(raw_plugins, dict):
        return enabled
    for plugin_id, config in raw_plugins.items():
        if isinstance(config, dict) and config.get("enabled") is True:
            enabled.add(str(plugin_id))
    return enabled


def _codex_plugin_identity(manifest: Path, cache_root: Path, name: str) -> set[str]:
    try:
        relative = manifest.relative_to(cache_root).parts
    except ValueError:
        relative = ()
    identities = {name}
    if len(relative) >= 4 and relative[-2] == ".codex-plugin":
        marketplace, plugin_dir = relative[0], relative[1]
        identities.add(f"{plugin_dir}@{marketplace}")
        identities.add(f"{name}@{marketplace}")
    return identities


def _companion_exists(manifest: Path, value: Any, fallback: str) -> bool:
    if isinstance(value, dict):
        return True
    if isinstance(value, str):
        candidate = manifest.parent.parent / value.removeprefix("./")
        return candidate.exists() and not candidate.is_symlink()
    candidate = manifest.parent.parent / fallback
    return candidate.exists() and not candidate.is_symlink()


def _scan_codex_plugins(
    roots: Iterable[Path],
    config_path: Path | None,
    max_files: int,
    expose_paths: bool,
    warnings: list[str],
) -> list[dict[str, Any]]:
    enabled = _codex_enabled_plugins(config_path, warnings)
    records: list[dict[str, Any]] = []
    for root in roots:
        for manifest in _walk_files(root, "plugin.json", max_files, warnings):
            if ".codex-plugin" not in manifest.parts:
                continue
            data = _read_json(manifest, warnings)
            if data is None:
                continue
            raw_interface = data.get("interface")
            interface: dict[str, Any] = raw_interface if isinstance(raw_interface, dict) else {}
            name = _text(data.get("name")) or manifest.parent.parent.name
            display = _text(interface.get("displayName"))
            identities = _codex_plugin_identity(manifest, root, name)
            active = bool(enabled.intersection(identities))
            records.append(
                _base_record(
                    kind="codex_plugin",
                    name=name,
                    display_name=display,
                    description=_text(data.get("description"))
                    or _text(interface.get("shortDescription")),
                    version=_text(data.get("version")),
                    status="active" if active else "cache_only",
                    source="codex_plugin_cache",
                    path=manifest,
                    expose_paths=expose_paths,
                    capabilities=_string_list(interface.get("capabilities")),
                    has_skills=_companion_exists(manifest, data.get("skills"), "skills"),
                    has_mcp=_companion_exists(manifest, data.get("mcpServers"), ".mcp.json"),
                    has_apps=_companion_exists(manifest, data.get("apps"), ".app.json"),
                    config_ids=sorted(identities.intersection(enabled)),
                )
            )
    return records


def _scan_codex_skills(
    roots: Iterable[Path],
    max_files: int,
    expose_paths: bool,
    warnings: list[str],
    source: str = "codex_skill_root",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in roots:
        for skill_file in _walk_files(root, "SKILL.md", max_files, warnings):
            metadata = _read_frontmatter(skill_file, warnings)
            name = _text(metadata.get("name")) or skill_file.parent.name
            records.append(
                _base_record(
                    kind="codex_skill",
                    name=name,
                    display_name=name,
                    description=_text(metadata.get("description")),
                    version="",
                    status="discovered",
                    source=source,
                    path=skill_file,
                    expose_paths=expose_paths,
                    root=_path_value(root, expose_paths),
                )
            )
    return records


def _profile_package_paths(
    profile: Path, package_name: str, runtime_roots: Iterable[Path] = ()
) -> list[Path]:
    relative = Path(*package_name.split("/")) / "package.json"
    return [profile / "node_modules" / relative] + [root / relative for root in runtime_roots]


def _direct_package_manifests(node_modules: Path, warnings: list[str]) -> list[Path]:
    """Return manifests for packages directly installed in one node_modules."""

    if not node_modules.exists() or node_modules.is_symlink() or not node_modules.is_dir():
        return []
    found: list[Path] = []
    try:
        for entry in sorted(node_modules.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name.startswith("@"):
                for package_dir in sorted(entry.iterdir()):
                    if package_dir.is_symlink() or not package_dir.is_dir():
                        continue
                    manifest = package_dir / "package.json"
                    if manifest.is_file() and not manifest.is_symlink():
                        found.append(manifest)
            else:
                manifest = entry / "package.json"
                if manifest.is_file() and not manifest.is_symlink():
                    found.append(manifest)
    except OSError as exc:
        warnings.append(f"node_modules unreadable: {node_modules} ({type(exc).__name__})")
    return found


def _scan_dsh_profiles(
    profiles_root: Path | None,
    runtime_roots: Iterable[Path],
    max_files: int,
    expose_paths: bool,
    warnings: list[str],
) -> list[dict[str, Any]]:
    if profiles_root is None or not profiles_root.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        profile_dirs = sorted(
            path for path in profiles_root.iterdir() if path.is_dir() and not path.is_symlink()
        )
    except OSError as exc:
        warnings.append(f"DSH profiles unreadable: {profiles_root} ({type(exc).__name__})")
        return records

    for profile in profile_dirs:
        profile_manifest = profile / "package.json"
        profile_data = _read_json(profile_manifest, warnings)
        if profile_data is None:
            continue
        raw_dsh = profile_data.get("dsh")
        dsh_data: dict[str, Any] = raw_dsh if isinstance(raw_dsh, dict) else {}
        raw_profile = dsh_data.get("profile")
        profile_data_dsh: dict[str, Any] = (
            raw_profile if isinstance(raw_profile, dict) else {}
        )
        bundles = _string_list(profile_data_dsh.get("bundles"))
        dependencies = profile_data.get("dependencies")
        direct = set(dependencies) if isinstance(dependencies, dict) else set()
        records.append(
            _base_record(
                kind="dsh_profile",
                name=profile.name,
                display_name=profile.name,
                description="DeepSeek Harness profile",
                version="",
                status="active_profile" if profile.name in {"web", "headless"} else "discovered",
                source="dsh_profile",
                path=profile_manifest,
                expose_paths=expose_paths,
                bundles=bundles,
                direct_dependencies=sorted(direct),
            )
        )

        package_names = set(bundles) | direct
        installed_manifests: dict[str, tuple[Path, dict[str, Any]]] = {}
        for path in _direct_package_manifests(profile / "node_modules", warnings)[:max_files]:
            package_data = _read_json(path, warnings)
            if package_data is None:
                continue
            dsh_package = package_data.get("dsh")
            workshop = package_data.get("dshWorkshop")
            if not isinstance(dsh_package, dict) and not isinstance(workshop, dict):
                continue
            package_name = _text(package_data.get("name")) or path.parent.name
            installed_manifests[package_name] = (path, package_data)
            package_names.add(package_name)
        seen: set[tuple[str, str]] = set()
        for package_name in sorted(package_names):
            installed = installed_manifests.get(package_name)
            manifest = installed[0] if installed else next(
                (
                    candidate
                    for candidate in _profile_package_paths(profile, package_name, runtime_roots)
                    if candidate.exists()
                ),
                None,
            )
            if manifest is None:
                if package_name not in bundles:
                    continue
                records.append(
                    _base_record(
                        kind="dsh_plugin",
                        name=package_name,
                        display_name=package_name,
                        description="Declared DSH bundle; package manifest unavailable",
                        version="",
                        status="declared_but_missing",
                        source=f"dsh_profile:{profile.name}",
                        path=None,
                        expose_paths=expose_paths,
                        profile=profile.name,
                        role="profile_bundle",
                    )
                )
                continue
            package_data = installed[1] if installed else _read_json(manifest, warnings)
            if package_data is None:
                continue
            package_actual_name = _text(package_data.get("name")) or package_name
            dsh_package = package_data.get("dsh")
            workshop = package_data.get("dshWorkshop")
            if not isinstance(dsh_package, dict) and not isinstance(workshop, dict):
                continue
            key = (profile.name, package_actual_name)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                _base_record(
                    kind="dsh_plugin",
                    name=package_actual_name,
                    display_name=package_actual_name,
                    description=_text(package_data.get("description")),
                    version=_text(package_data.get("version")),
                    status="active" if package_actual_name in bundles else "installed_not_bundled",
                    source=f"dsh_profile:{profile.name}",
                    path=manifest,
                    expose_paths=expose_paths,
                    profile=profile.name,
                    role=(
                        "profile_bundle"
                        if package_actual_name in bundles
                        else (
                            "direct_dependency"
                            if package_actual_name in direct
                            else "transitive_dependency"
                        )
                    ),
                    has_bundle=(
                        isinstance(dsh_package, dict)
                        and isinstance(dsh_package.get("bundle"), dict)
                    ),
                    has_client=(
                        isinstance(dsh_package, dict)
                        and isinstance(dsh_package.get("client"), dict)
                    ),
                    permissions=(
                        workshop.get("permissions", []) if isinstance(workshop, dict) else []
                    ),
                )
            )
    return records


def _scan_dsh_runtime_packages(
    runtime_roots: Iterable[Path],
    known_names: set[str],
    max_files: int,
    expose_paths: bool,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """List DSH-capable packages available in the launcher runtime."""

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for root in runtime_roots:
        for path in _direct_package_manifests(root, warnings)[:max_files]:
            package_data = _read_json(path, warnings)
            if package_data is None:
                continue
            dsh_package = package_data.get("dsh")
            workshop = package_data.get("dshWorkshop")
            if not isinstance(dsh_package, dict) and not isinstance(workshop, dict):
                continue
            package_name = _text(package_data.get("name")) or path.parent.name
            if package_name in known_names:
                continue
            key = (package_name, _text(package_data.get("version")))
            if key in seen:
                continue
            seen.add(key)
            records.append(
                _base_record(
                    kind="dsh_plugin",
                    name=package_name,
                    display_name=package_name,
                    description=_text(package_data.get("description")),
                    version=_text(package_data.get("version")),
                    status="runtime_available",
                    source="dsh_runtime",
                    path=path,
                    expose_paths=expose_paths,
                    role="runtime_available",
                    has_bundle=(
                        isinstance(dsh_package, dict)
                        and isinstance(dsh_package.get("bundle"), dict)
                    ),
                    has_client=(
                        isinstance(dsh_package, dict)
                        and isinstance(dsh_package.get("client"), dict)
                    ),
                    permissions=(
                        workshop.get("permissions", []) if isinstance(workshop, dict) else []
                    ),
                )
            )
    return records


def discover_local_extensions(
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    roots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan configured local extension roots and return metadata only."""

    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5_000:
        raise ExtensionScanError("max_items 必须是 1..5000 的整数")
    resolved = default_roots() | (roots or {})
    warnings: list[str] = []
    expose_paths = bool(resolved.get("expose_paths", True))
    max_files = max(max_items, 100)
    records: list[dict[str, Any]] = []
    records.extend(
        _scan_codex_skills(
            resolved.get("codex_skill_roots", ()), max_files, expose_paths, warnings
        )
    )
    records.extend(
        _scan_codex_skills(
            resolved.get("project_roots", ()), max_files, expose_paths, warnings,
            source="project_skill_root",
        )
    )
    records.extend(
        _scan_codex_plugins(
            resolved.get("codex_plugin_cache_roots", ()),
            resolved.get("codex_config"),
            max_files,
            expose_paths,
            warnings,
        )
    )
    records.extend(
        _scan_codex_plugins(
            resolved.get("project_roots", ()),
            resolved.get("codex_config"),
            max_files,
            expose_paths,
            warnings,
        )
    )
    records.extend(
        _scan_dsh_profiles(
            resolved.get("dsh_profiles_root"),
            resolved.get("dsh_runtime_package_roots", ()),
            max_files,
            expose_paths,
            warnings,
        )
    )
    dsh_records = [record for record in records if record["kind"] == "dsh_plugin"]
    records.extend(
        _scan_dsh_runtime_packages(
            resolved.get("dsh_runtime_package_roots", ()),
            {record["name"] for record in dsh_records},
            max_files,
            expose_paths,
            warnings,
        )
    )

    unique: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (
            record["kind"],
            record["name"],
            record["version"],
            record.get("profile") or "",
            record.get("path") or record.get("profile") or "",
        )
        unique[key] = record
    items = sorted(
        unique.values(),
        key=lambda item: (item["kind"], item["name"].casefold(), item.get("path") or ""),
    )
    truncated = len(items) > max_items
    items = items[:max_items]
    return {
        "schema_version": "1.0",
        "scanned_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "path_visibility": "absolute" if expose_paths else "hidden",
        "results": items,
        "counts": {
            "total": len(items),
            "by_kind": dict(Counter(item["kind"] for item in items)),
            "by_status": dict(Counter(item["status"] for item in items)),
        },
        "truncated": truncated,
        "warnings": list(dict.fromkeys(warnings)),
        "note": (
            "只读取 skill/plugin/package manifest；不会导入、安装、执行插件，"
            "也不会读取密钥或第三方账号内容。缓存不等于已启用。"
        ),
    }


def filter_extensions(
    snapshot: dict[str, Any],
    *,
    query: str | None = None,
    kind: str = "all",
    status: str = "all",
    limit: int = 50,
) -> dict[str, Any]:
    """Filter a discovery snapshot without reading any additional files."""

    if kind != "all" and kind not in {"codex_skill", "codex_plugin", "dsh_plugin", "dsh_profile"}:
        raise ExtensionScanError("kind 不合法")
    if status != "all" and status not in {
        "active", "discovered", "cache_only", "active_profile", "installed_not_bundled",
        "declared_but_missing", "runtime_available",
    }:
        raise ExtensionScanError("status 不合法")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ExtensionScanError("limit 必须是 1..200 的整数")
    needle = query.strip().casefold() if isinstance(query, str) else ""
    terms = needle.split()
    matched: list[dict[str, Any]] = []
    for item in snapshot.get("results", []):
        if kind != "all" and item.get("kind") != kind:
            continue
        if status != "all" and item.get("status") != status:
            continue
        haystack = " ".join(
            str(item.get(field, ""))
            for field in ("name", "display_name", "description", "source", "path", "profile")
        ).casefold()
        if needle and any(term not in haystack for term in terms):
            continue
        matched.append(item)
    return {
        **snapshot,
        "results": matched[:limit],
        "matched": len(matched),
        "truncated": snapshot.get("truncated", False) or len(matched) > limit,
    }
