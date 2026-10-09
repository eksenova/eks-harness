from __future__ import annotations

import sqlite3
from typing import Any

from eks_harness.db.common import dumps, loads, now


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return loads(row[0], default={"v": default}).get("v", default)


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, dumps({"v": value}), now()))


def delete(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("DELETE FROM kv WHERE key = ?", (key,)).rowcount > 0
