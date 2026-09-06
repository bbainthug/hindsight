"""profile 配置加载与校验测试（§7.4/§7.5）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_brain.policy.profiles import (
    ProfileConfigError,
    load_mcp_config,
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "mcp.yaml"
    p.write_text(text, encoding="utf-8")
    return p


VALID = """
database_path: {db}
timezone: UTC
profile: codex
profiles:
  codex:
    allow_scopes: [projects, career]
    deny_sensitivity: [secret, third_party_sensitive]
    allow_unclassified: false
    tools: [brain_status, search_history, get_recent_events, get_event]
    delivery_boundary: local_only
"""


class TestProfileLoading:
    def test_valid_config(self, tmp_path):
        cfg = load_mcp_config(_write(tmp_path, VALID.format(db=tmp_path / "b.sqlite")))
        assert cfg.profile.name == "codex"
        assert cfg.profile.allow_scopes == ("projects", "career")
        assert cfg.profile.allow_unclassified is False
        assert cfg.profile.delivery_boundary == "local_only"
        assert len(cfg.profile.tools) == 4

    def test_allow_unclassified_defaults_false(self, tmp_path):
        """§7.2：未知分类默认不对普通 Agent 开放——缺省即 False。"""
        cfg = load_mcp_config(
            _write(
                tmp_path,
                """
database_path: x
profile: p
profiles:
  p:
    allow_scopes: [career]
    tools: [brain_status]
    delivery_boundary: local_only
""",
            )
        )
        assert cfg.profile.allow_unclassified is False

    def test_local_review_transport_refused(self, tmp_path):
        """§7.4：local_review 不作为 MCP 服务身份。"""
        with pytest.raises(ProfileConfigError, match="local_cli_only"):
            load_mcp_config(
                _write(
                    tmp_path,
                    """
database_path: x
profile: local_review
profiles:
  local_review:
    allow_scopes: ['*']
    tools: [brain_status]
    delivery_boundary: local_only
    transport: local_cli_only
""",
                )
            )

    def test_unknown_tool_rejected(self, tmp_path):
        with pytest.raises(ProfileConfigError, match="未知或未实现"):
            load_mcp_config(
                _write(
                    tmp_path,
                    """
database_path: x
profile: p
profiles:
  p:
    allow_scopes: [career]
    tools: [search_memory]
    delivery_boundary: local_only
""",
                )
            )

    def test_delivery_boundary_required(self, tmp_path):
        """§7.5：delivery_boundary 必须显式声明，缺省拒绝启动。"""
        with pytest.raises(ProfileConfigError, match="delivery_boundary"):
            load_mcp_config(
                _write(
                    tmp_path,
                    """
database_path: x
profile: p
profiles:
  p:
    allow_scopes: [career]
    tools: [brain_status]
""",
                )
            )

    def test_wildcard_scope_exclusive(self, tmp_path):
        with pytest.raises(ProfileConfigError, match="通配"):
            load_mcp_config(
                _write(
                    tmp_path,
                    """
database_path: x
profile: p
profiles:
  p:
    allow_scopes: ['*', career]
    tools: [brain_status]
    delivery_boundary: local_only
""",
                )
            )

    def test_missing_profile_rejected(self, tmp_path):
        with pytest.raises(ProfileConfigError, match="profile 不存在"):
            load_mcp_config(
                _write(
                    tmp_path,
                    """
database_path: x
profile: nope
profiles:
  p:
    allow_scopes: [career]
    tools: [brain_status]
    delivery_boundary: local_only
""",
                )
            )
