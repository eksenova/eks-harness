from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from eks_harness.db.common import dumps, loads, now, placeholders


@dataclass(frozen=True)
class EventRow:
    id: int
    ts: float
    type: str
    resource: str | None = None
    lease_sid: str | None = None
    session_id: int | None = None
    project_id: str | None = None
    actor: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _row(row: sqlite3.Row | None) -> EventRow | None:
    if row is None:
        return None
    return EventRow(id=row["id"], ts=row["ts"], type=row["type"], resource=row["resource"],
                    lease_sid=row["lease_sid"], session_id=row["session_id"], project_id=row["project_id"],
                    actor=row["actor"], detail=loads(row["detail"]))


def insert(conn: sqlite3.Connection, type: str, *, resource: str | None = None, lease_sid: str | None = None,
           session_id: int | None = None, project_id: str | None = None, actor: str | None = None,
           detail: dict[str, Any] | None = None, ts: float | None = None) -> EventRow:
    stamp = ts or now()
    cursor = conn.execute(
        "INSERT INTO events (ts, resource, lease_sid, session_id, project_id, actor, type, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (stamp, resource, lease_sid, session_id, project_id, actor, type, dumps(detail or {})))
    return EventRow(id=cursor.lastrowid, ts=stamp, type=type, resource=resource, lease_sid=lease_sid,
                    session_id=session_id, project_id=project_id, actor=actor, detail=dict(detail or {}))


def get(conn: sqlite3.Connection, event_id: int) -> EventRow | None:
    return _row(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())


def list_events(conn: sqlite3.Connection, *, after_id: int | None = None, before_id: int | None = None,
                resource: str | None = None, resource_prefix: str | None = None, lease_sid: str | None = None,
                session_id: int | None = None, project_id: str | None = None, types: Iterable[str] | None = None,
                type_prefix: str | None = None, since_ts: float | None = None, until_ts: float | None = None,
                limit: int = 200, ascending: bool = False) -> list[EventRow]:
    clauses, params = [], []
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    if resource is not None:
        clauses.append("resource = ?")
        params.append(resource)
    if resource_prefix is not None:
        clauses.append("resource LIKE ? ESCAPE '\\'")
        params.append(resource_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if lease_sid is not None:
        clauses.append("lease_sid = ?")
        params.append(lease_sid)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    type_list = list(types or [])
    if type_list:
        clauses.append(f"type IN ({placeholders(len(type_list))})")
        params.extend(type_list)
    if type_prefix is not None:
        clauses.append("type LIKE ?")
        params.append(type_prefix + "%")
    if since_ts is not None:
        clauses.append("ts >= ?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("ts <= ?")
        params.append(until_ts)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ASC" if ascending else "DESC"
    params.append(limit)
    return [_row(r) for r in conn.execute(f"SELECT * FROM events{where} ORDER BY id {order} LIMIT ?", params)]


def latest_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM events").fetchone()
    return row[0] or 0


def prune(conn: sqlite3.Connection, before_ts: float) -> int:
    return conn.execute("DELETE FROM events WHERE ts < ?", (before_ts,)).rowcount


def delete_for_session(conn: sqlite3.Connection, session_id: int) -> int:
    return conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,)).rowcount


def delete_for_project(conn: sqlite3.Connection, project_id: str) -> int:
    return conn.execute("DELETE FROM events WHERE project_id = ?", (project_id,)).rowcount
