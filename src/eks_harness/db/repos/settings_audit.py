from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from eks_harness.db.common import dumps, loads, now


@dataclass(frozen=True)
class SettingsAuditEntry:
    id: int
    ts: float
    user: str | None
    key: str
    old: Any
    new: Any


def _unwrap(text: str | None) -> Any:
    data = loads(text, default={})
    return data.get("v") if isinstance(data, dict) else None


def _row(row: sqlite3.Row | None) -> SettingsAuditEntry | None:
    if row is None:
        return None
    return SettingsAuditEntry(id=row["id"], ts=row["ts"], user=row["user"], key=row["key"],
                              old=_unwrap(row["old"]), new=_unwrap(row["new"]))


def insert(conn: sqlite3.Connection, user: str | None, key: str, old: Any, new: Any) -> SettingsAuditEntry:
    cursor = conn.execute("INSERT INTO settings_audit (ts, user, key, old, new) VALUES (?, ?, ?, ?, ?)",
                          (now(), user, key, dumps({"v": old}), dumps({"v": new})))
    return get(conn, cursor.lastrowid)


def get(conn: sqlite3.Connection, entry_id: int) -> SettingsAuditEntry | None:
    return _row(conn.execute("SELECT * FROM settings_audit WHERE id = ?", (entry_id,)).fetchone())


def list_entries(conn: sqlite3.Connection, key: str | None = None, limit: int = 200) -> list[SettingsAuditEntry]:
    sql = "SELECT * FROM settings_audit"
    params: list = []
    if key:
        sql += " WHERE key = ?"
        params.append(key)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_row(r) for r in conn.execute(sql, params)]
