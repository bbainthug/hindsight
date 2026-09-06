"""数据库连接与 PRAGMA 设置。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from personal_brain.history.schema import apply_migrations


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开数据库连接并应用全部迁移。

    - ``foreign_keys=ON``：§4 要求用外键与唯一约束落实契约。
    - ``journal_mode=WAL``：本地单用户写 + 读并发；备份必须走一致性快照（后续阶段）。
    - ``synchronous=FULL``：导入发布是唯一权威数据，写入必须持久。
    - ``busy_timeout``：避免短暂的锁竞争直接失败。
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    apply_migrations(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：发布可见性边界（§5.2 步骤 6）依赖原子提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
