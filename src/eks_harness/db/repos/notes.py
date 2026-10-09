from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import now


@dataclass(frozen=True)
class Note:
    id: int
    session_id: int
    lease_sid: str | None
    author: str
    body: str
    created_at: float


def _row(row: sqlite3.Row | None) -> Note | None:
    if row is None:
        return None
    return Note(id=row["id"], session_id=row["session_id"], lease_sid=row["lease_sid"], author=row["author"],
                body=row["body"], created_at=row["created_at"])


def insert(conn: sqlite3.Connection, session_id: int, author: str, body: str, lease_sid: str | None = None,
           ts: float | None = None) -> Note:
    cursor = conn.execute(
        "INSERT INTO notes (session_id, lease_sid, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, lease_sid, author, body, ts or now()))
    return get(conn, cursor.lastrowid)


def get(conn: sqlite3.Connection, note_id: int) -> Note | None:
    return _row(conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())


def list_for_session(conn: sqlite3.Connection, session_id: int, since: float | None = None,
                     limit: int | None = None) -> list[Note]:
    sql = "SELECT * FROM notes WHERE session_id = ?"
    params: list = [session_id]
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row(r) for r in conn.execute(sql, params)]


def update(conn: sqlite3.Connection, note_id: int, body: str) -> Note | None:
    conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body, note_id))
    return get(conn, note_id)


def delete(conn: sqlite3.Connection, note_id: int) -> bool:
    return conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0


def count_for_session(conn: sqlite3.Connection, session_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM notes WHERE session_id = ?", (session_id,)).fetchone()[0]


def count_for_project(conn: sqlite3.Connection, project_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM notes WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
        (project_id,)).fetchone()[0]
