"""服务端访问 profile（§7.4/§7.5）。

- profile 由**受信任启动配置**绑定（MCP 服务进程启动时确定），
  绝不是 tool 参数；调用方不能选择或合并 profile。
- `local_review` 是本地人工审核用途（transport=local_cli_only），
  MCP 服务拒绝以其身份启动（§7.4「不自动配置给 Agent」）。
- `allow_unclassified` 缺省 False：未知分类默认不对普通 Agent 开放
  （§7.2）；本批同时翻转检索层的本地默认（决策 23 的 Phase B 事项）。
- `delivery_boundary` 必须显式声明（§7.5）；缺省视为未声明 → 拒绝启动，
  防止"未声明客户端交付个人正文"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ALLOWED_TOOLS = ("brain_status", "search_history", "get_recent_events", "get_event")
DELIVERY_BOUNDARIES = ("local_only", "remote_model_allowed")
TRANSPORTS = ("stdio_mcp", "local_cli_only")


class ProfileConfigError(ValueError):
    """受信任配置非法：拒绝启动而非猜测语义。"""


@dataclass(frozen=True)
class AccessProfile:
    name: str
    allow_scopes: tuple[str, ...]  # ("*",) 表示全部 scope
    deny_sensitivity: tuple[str, ...]
    allow_unclassified: bool
    tools: tuple[str, ...]
    delivery_boundary: str  # local_only | remote_model_allowed
    transport: str = "stdio_mcp"


@dataclass(frozen=True)
class McpServerConfig:
    db_path: Path
    timezone: str
    profile: AccessProfile
    search_limits: dict[str, int]  # §6.3 可配置项（受 SearchLimits 硬上限钳制）


def _parse_profile(name: str, raw: dict) -> AccessProfile:
    if not isinstance(raw, dict):
        raise ProfileConfigError(f"profile {name}: 必须是映射")
    scopes = raw.get("allow_scopes")
    if not isinstance(scopes, list) or not scopes:
        raise ProfileConfigError(f"profile {name}: allow_scopes 必须是非空列表")
    if "*" in scopes and len(scopes) > 1:
        raise ProfileConfigError(f"profile {name}: 通配 '*' 不得与其他 scope 并用")
    tools = raw.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ProfileConfigError(f"profile {name}: tools 必须是非空列表")
    unknown = [t for t in tools if t not in ALLOWED_TOOLS]
    if unknown:
        raise ProfileConfigError(
            f"profile {name}: 未知或未实现的工具 {unknown}（Phase B 仅 4 个只读工具）"
        )
    boundary = raw.get("delivery_boundary")
    if boundary not in DELIVERY_BOUNDARIES:
        raise ProfileConfigError(
            f"profile {name}: delivery_boundary 必须显式声明为 "
            f"{DELIVERY_BOUNDARIES} 之一（§7.5）"
        )
    transport = raw.get("transport", "stdio_mcp")
    if transport not in TRANSPORTS:
        raise ProfileConfigError(f"profile {name}: 未知 transport {transport}")
    return AccessProfile(
        name=name,
        allow_scopes=tuple(scopes),
        deny_sensitivity=tuple(raw.get("deny_sensitivity", ())),
        allow_unclassified=bool(raw.get("allow_unclassified", False)),
        tools=tuple(tools),
        delivery_boundary=boundary,
        transport=transport,
    )


def load_mcp_config(path: Path) -> McpServerConfig:
    """加载受信任启动配置（§16：personal-brain-mcp --config <trusted-config-path>）。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ProfileConfigError("MCP 配置必须是映射")
    if "database_path" not in raw:
        raise ProfileConfigError("MCP 配置缺少 database_path")
    profile_name = raw.get("profile")
    profiles_raw = raw.get("profiles")
    if not profile_name or not isinstance(profiles_raw, dict):
        raise ProfileConfigError("MCP 配置需要 profile 与 profiles 映射")
    if profile_name not in profiles_raw:
        raise ProfileConfigError(f"profile 不存在: {profile_name}")
    profile = _parse_profile(profile_name, profiles_raw[profile_name])
    if profile.transport == "local_cli_only":
        raise ProfileConfigError(
            f"profile {profile_name} 是本地人工审核用途"
            "（transport=local_cli_only），不作为 MCP 服务身份（§7.4）"
        )
    return McpServerConfig(
        db_path=Path(raw["database_path"]).expanduser(),
        timezone=str(raw.get("timezone", "UTC")),
        profile=profile,
        search_limits=dict(raw.get("search", {})),
    )
