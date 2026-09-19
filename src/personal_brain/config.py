"""运行配置：路径、时区、检索限额（§16「路径可配置」、§6.3「数量可配置」）。

配置来源优先级：CLI 显式参数 > 配置文件 > 默认值。
默认值不指向任何真实路径；首次使用必须显式配置（工作约定：
真实聊天、密钥、数据库不进 Git，也不写进仓库工作区）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SearchLimits:
    """§6.3 初始产品限制：可配置，服务端另有硬上限约束。"""

    default_limit: int = 10
    max_limit: int = 50
    snippet_chars: int = 400
    max_text_chars: int = 8000
    max_scan: int = 50_000  # 回退扫描资源上限，超限返回 QUERY_TOO_BROAD


@dataclass
class BrainConfig:
    db_path: Path = field(
        default_factory=lambda: Path.home() / ".local/share/personal-brain/brain.sqlite"
    )
    archive_dir: Path = field(
        default_factory=lambda: Path.home() / ".local/share/personal-brain/archives"
    )
    backup_dir: Path = field(
        default_factory=lambda: Path.home() / ".local/share/personal-brain/backups"
    )
    timezone: str = "UTC"  # 自然日期 → 区间边界所用配置时区（§4.4）
    search: SearchLimits = field(default_factory=SearchLimits)
    # D-1 本地语义检索（可选；未安装依赖时 mode=hybrid 退化为 exact）
    semantic_model: str = "BAAI/bge-small-zh-v1.5"
    semantic_cache_dir: str | None = None
    semantic_chunk_chars: int = 600
    semantic_chunk_overlap: int = 100

    @staticmethod
    def load(config_path: Path | None) -> BrainConfig:
        """加载 YAML 配置；文件缺失或字段缺省时用默认值。"""
        cfg = BrainConfig()
        if config_path is None:
            env = os.environ.get("PERSONAL_BRAIN_CONFIG")
            if not env:
                return cfg
            config_path = Path(env)
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"配置文件格式错误: {config_path}")
        if v := data.get("database_path"):
            cfg.db_path = Path(str(v)).expanduser()
        if v := data.get("archive_dir"):
            cfg.archive_dir = Path(str(v)).expanduser()
        if v := data.get("backup_dir"):
            cfg.backup_dir = Path(str(v)).expanduser()
        if v := data.get("timezone"):
            cfg.timezone = str(v)
        search = data.get("search") or {}
        if isinstance(search, dict):
            for key in (
                "default_limit",
                "max_limit",
                "snippet_chars",
                "max_text_chars",
                "max_scan",
            ):
                if key in search:
                    setattr(cfg.search, key, int(search[key]))
        semantic = data.get("semantic") or {}
        if isinstance(semantic, dict):
            if v := semantic.get("model"):
                cfg.semantic_model = str(v)
            if v := semantic.get("cache_dir"):
                cfg.semantic_cache_dir = str(Path(str(v)).expanduser())
            if v := semantic.get("chunk_chars"):
                cfg.semantic_chunk_chars = int(v)
            if v := semantic.get("chunk_overlap"):
                cfg.semantic_chunk_overlap = int(v)
        if cfg.semantic_chunk_chars < 1 or not (
            0 <= cfg.semantic_chunk_overlap < cfg.semantic_chunk_chars
        ):
            raise ValueError(
                "semantic.chunk_chars 必须 > 0 且 0 <= chunk_overlap < chunk_chars"
            )
        return cfg
