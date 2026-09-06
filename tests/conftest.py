"""共享夹具：内存外临时库、归档目录、导入器实例。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).parent / "integration"))

from personal_brain.history.db import connect  # noqa: E402
from personal_brain.importers.importer import ChatGPTImporter  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path):
    conn = connect(tmp_path / "brain.sqlite")
    yield conn
    conn.close()


@pytest.fixture()
def archive_dir(tmp_path: Path) -> Path:
    d = tmp_path / "archives"
    d.mkdir()
    return d


@pytest.fixture()
def importer(db, archive_dir) -> ChatGPTImporter:
    return ChatGPTImporter(db, archive_dir)
