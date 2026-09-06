"""策略纪元（§7.3）：权限/撤回状态的单调计数器。

- 任何标签或撤回状态变更必须在**同一事务**内递增纪元。
- MCP 请求在返回前核对纪元：执行中纪元变化 → 重新授权（重跑）或放弃。
- 缓存键（未来引入时）必须包含 profile + policy epoch + 数据 generation。
"""

from __future__ import annotations

import sqlite3

from personal_brain.history.timeutil import utc_now_iso


def current_policy_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT epoch FROM policy_epoch WHERE id = 1").fetchone()
    if row is None:  # 旧库未跑迁移的防御
        return 0
    return int(row["epoch"])


def bump_policy_epoch(conn: sqlite3.Connection) -> int:
    """在调用方事务内递增纪元，返回新值。不自行提交。"""
    conn.execute(
        """
        UPDATE policy_epoch
        SET epoch = epoch + 1, updated_at = ?
        WHERE id = 1
        """,
        (utc_now_iso(),),
    )
    return current_policy_epoch(conn)
