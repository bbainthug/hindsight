"""CLI 端到端测试（§16）：临时配置全链路，--json 结构化输出断言。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import write_zip

from personal_brain.cli import main


class CliEnv:
    def __init__(self, config: Path, zip_path: Path):
        self.config = config
        self.zip_path = zip_path


@pytest.fixture()
def cli_env(tmp_path: Path) -> CliEnv:
    """临时配置：独立 DB/归档/备份目录。"""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"database_path: {tmp_path / 'brain.sqlite'}",
                f"archive_dir: {archive_dir}",
                f"backup_dir: {tmp_path / 'backups'}",
                "timezone: UTC",
                "search:",
                "  default_limit: 10",
                "  max_limit: 50",
                "  snippet_chars: 400",
                "  max_text_chars: 8000",
                "  max_scan: 50000",
            ]
        ),
        encoding="utf-8",
    )
    zip_path = write_zip(retrieval_corpus_conversations(), tmp_path / "corpus.zip")
    return CliEnv(config=config, zip_path=zip_path)


@pytest.fixture()
def run(cli_env, capsys):
    """便捷执行：main([--config, cfg, *argv]) → (exit_code, stdout JSON)。"""

    def _run(*argv: str) -> tuple[int, dict]:
        code = main(["--config", str(cli_env.config), "--json", *argv])
        out = capsys.readouterr().out
        return code, json.loads(out)

    return _run


@pytest.fixture()
def imported(run) -> dict:
    """导入一次并返回 import 结果。"""
    code, out = run("import-chatgpt", str(run.__self__.zip_path), "--source", "testuser")  # type: ignore[attr-defined]
    assert code == 0, out
    return out


class TestCliE2E:
    def test_full_flow(self, cli_env, run):
        # 1. 导入
        code, out = run(
            "import-chatgpt", str(cli_env.zip_path), "--source", "testuser"
        )
        assert code == 0
        assert out["kind"] == "import"
        assert out["result"]["events_new"] > 0

        # 2. 状态
        code, st = run("status")
        assert code == 0
        assert st["totals"]["events"] > 0
        assert st["derived"]["index_lag"] == 0
        assert st["sources"][0]["account_namespace"] == "testuser"
        source_id = st["sources"][0]["source_id"]

        # 3. 检索（中文 FTS 路径 + snippet 原文）
        code, out = run("search-history", "职业方向")
        assert code == 0
        assert len(out["hits"]) == 1
        assert "职业方向" in out["hits"][0]["snippet"]
        event_id = out["hits"][0]["event_id"]

        # 4. 查看事件
        code, out = run("show-event", event_id)
        assert code == 0
        assert out["event"]["event_id"] == event_id
        assert out["event"]["revisions"][0]["availability"] == "available"

        # 5. 标注（来源级）+ §16 警示
        code, out = run("label-source", source_id, "--scope", "career")
        assert code == 0
        assert out["revisions_relabeled"] > 0
        assert any("混合话题" in w for w in out["warnings"])

        # 6. scope 过滤检索
        code, out = run("search-history", "职业", "--scope", "career")
        assert code == 0
        assert len(out["hits"]) > 0

        # 7. 撤回来源
        code, out = run("withdraw-source", source_id)
        assert code == 0
        assert out["revisions_withdrawn"] > 0

        # 8. 撤回后检索排除
        code, out = run("search-history", "职业")
        assert code == 0
        assert out["hits"] == []
        assert out["total_matched"] == 0

        # 9. show-event 仍可见（显式查看 = 本地受信边界）且带 withdrawn 标记
        code, out = run("show-event", event_id)
        assert code == 0
        assert out["event"]["revisions"][0]["availability"] == "withdrawn"

        # 10. 备份 + 恢复校验
        code, out = run("backup")
        assert code == 0
        backup_dir = out["backup_dir"]
        code, out = run("restore-check", backup_dir)
        assert code == 0
        assert out["integrity"] == "ok"
        assert out["manifest_ok"] is True
        assert out["counts"]["events"] > 0

    def test_single_hanzi_search_via_cli(self, run, cli_env, capsys):
        run("import-chatgpt", str(cli_env.zip_path), "--source", "testuser")
        capsys.readouterr()
        code, out = run("search-history", "安")
        assert code == 0
        assert len(out["hits"]) == 2
        assert out["query_plans"][0]["fallback"] is True

    def test_import_bad_source_alias(self, cli_env, capsys):
        code = main(
            [
                "--config",
                str(cli_env.config),
                "import-chatgpt",
                str(cli_env.zip_path),
                "--source",
                "bad alias!",
            ]
        )
        assert code == 2

    def test_empty_query_rejected(self, cli_env, run, capsys):
        run("import-chatgpt", str(cli_env.zip_path), "--source", "testuser")
        capsys.readouterr()
        code = main(["--config", str(cli_env.config), "search-history", "  "])
        assert code == 2

    def test_date_filters_via_cli(self, run, cli_env, capsys):
        run("import-chatgpt", str(cli_env.zip_path), "--source", "testuser")
        capsys.readouterr()
        code, out = run(
            "search-history", "职业", "--from", "2026-08-01", "--to", "2026-08-01"
        )
        assert code == 0
        assert out["interpreted"]["date_from"] == "2026-08-01T00:00:00.000000Z"
        assert out["interpreted"]["date_to"] == "2026-08-02T00:00:00.000000Z"
        assert out["undated_excluded"] == 1
