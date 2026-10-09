from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import now


@dataclass(frozen=True)
class WebSession:
    id: str
    user_id: int
    csrf: str
    created_at: float
    expires_at: float
    last_seen_at: float | None
    ip: str | None
    user_agent: str | None

    def expired(self, at: float | None = None) -> bool:
        return (at or now()) >= self.expires_at


def _row(row: sqlite3.Row | None) -> WebSession | None:
    if row is None:
        return None
    return WebSession(
        id=row["id"], user_id=row["user_id"], csrf=row["csrf"], created_at=row["created_at"],
        expires_at=row["expires_at"], last_seen_at=row["last_seen_at"], ip=row["ip"], user_agent=row["user_agent"],
    )


def create(conn: sqlite3.Connection, session_id: str, user_id: int, csrf: str, expires_at: float,
           ip: str | None = None, user_agent: str | None = None) -> WebSession:
    ts = now()
    conn.execute(
        "INSERT INTO web_sessions (id, user_id, csrf, created_at, expires_at, last_seen_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (session_id, user_id, csrf, ts, expires_at, ts, ip, (user_agent or "")[:500]))
    return get(conn, session_id)


def get(conn: sqlite3.Connection, session_id: str) -> WebSession | None:
    return _row(conn.execute("SELECT * FROM web_sessions WHERE id = ?", (session_id,)).fetchone())


def list_for_user(conn: sqlite3.Connection, user_id: int) -> list[WebSession]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM web_sessions WHERE user_id = ? ORDER BY created_at DESC", (user_id,))]


def touch(conn: sqlite3.Connection, session_id: str, ts: float | None = None) -> None:
    conn.execute("UPDATE web_sessions SET last_seen_at = ? WHERE id = ?", (ts or now(), session_id))


def delete(conn: sqlite3.Connection, session_id: str) -> bool:
    return conn.execute("DELETE FROM web_sessions WHERE id = ?", (session_id,)).rowcount > 0


def delete_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,)).rowcount


def prune_expired(conn: sqlite3.Connection, at: float | None = None) -> int:
    return conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (at or now(),)).rowcount
