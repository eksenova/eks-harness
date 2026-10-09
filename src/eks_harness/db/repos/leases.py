from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from eks_harness.db.common import UNSET, dumps, loads, now, placeholders
from eks_harness.ids import new_lease_id, new_sid

KINDS = ("browser", "ios", "android")
KIND_ALIASES = {"chrome": "browser"}
STATES = ("queued", "active", "idle", "released", "broken")
HOLDING_STATES = ("active", "idle")
LIVE_STATES = ("queued", "active", "idle")
ENDED_STATES = ("released", "broken")
PHASES = ("preparing", "ready", "failed")

_COLUMNS = (
    "id", "sid", "kind", "resource", "session_id", "owner_instance", "owner_kind", "owner_user", "backend_id",
    "state", "phase", "reason", "error", "queued_at", "acquired_at", "heartbeat_at", "last_poll_at", "released_at",
    "idle_limit", "owner_pid", "owner_started", "tree", "state_dir", "label", "previous_sid", "meta",
)


@dataclass(frozen=True)
class Lease:
    id: str
    sid: str
    kind: str
    resource: str | None
    session_id: int | None
    owner_instance: str
    owner_kind: str
    owner_user: int | None
    backend_id: str | None
    state: str
    phase: str | None
    reason: str | None
    error: str | None
    queued_at: float
    acquired_at: float | None
    heartbeat_at: float | None
    last_poll_at: float | None
    released_at: float | None
    idle_limit: float | None
    owner_pid: int | None
    owner_started: str | None
    tree: str | None
    state_dir: str | None
    label: str | None
    previous_sid: str | None
    meta: dict[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    session_slug: str | None = None
    session_name: str | None = None

    @property
    def holding(self) -> bool:
        return self.state in HOLDING_STATES

    @property
    def ended(self) -> bool:
        return self.state in ENDED_STATES


def normalize_kind(kind: str | None) -> str | None:
    return KIND_ALIASES.get(kind or "", kind)


def _row(row: sqlite3.Row | None) -> Lease | None:
    if row is None:
        return None
    keys = row.keys()
    values = {c: row[c] for c in _COLUMNS}
    values["meta"] = loads(values["meta"])
    return Lease(**values,
                 project_id=row["project_id"] if "project_id" in keys else None,
                 session_slug=row["session_slug"] if "session_slug" in keys else None,
                 session_name=row["session_name"] if "session_name" in keys else None)


_SELECT = (
    "SELECT l.*, s.slug AS session_slug, s.name AS session_name "
    "FROM leases l LEFT JOIN sessions s ON s.id = l.session_id"
)


def unique_sid(conn: sqlite3.Connection) -> str:
    while True:
        sid = new_sid()
        if conn.execute("SELECT 1 FROM leases WHERE sid = ?", (sid,)).fetchone() is None:
            return sid


def insert(conn: sqlite3.Connection, *, kind: str, owner_instance: str, state: str = "queued",
           resource: str | None = None, session_id: int | None = None, project_id: str | None = None,
           owner_kind: str = "agent",
           owner_user: int | None = None, backend_id: str | None = None, phase: str | None = None,
           reason: str | None = None, owner_pid: int | None = None, owner_started: str | None = None,
           tree: str | None = None, state_dir: str | None = None, label: str | None = None,
           previous_sid: str | None = None, meta: dict | None = None, sid: str | None = None,
           lease_id: str | None = None, queued_at: float | None = None, acquired_at: float | None = None,
           heartbeat_at: float | None = None) -> Lease:
    kind = normalize_kind(kind)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    ts = now()
    lease_id = lease_id or new_lease_id()
    if project_id is None and session_id is not None:
        linked = conn.execute("SELECT project_id FROM session_projects WHERE session_id = ? LIMIT 2",
                              (session_id,)).fetchall()
        project_id = linked[0][0] if len(linked) == 1 else None
    conn.execute(
        f"INSERT INTO leases (id, sid, kind, resource, session_id, project_id, owner_instance, owner_kind, owner_user, backend_id, "
        f"state, phase, reason, queued_at, acquired_at, heartbeat_at, last_poll_at, owner_pid, owner_started, tree, "
        f"state_dir, label, previous_sid, meta) VALUES ({placeholders(24)})",
        (lease_id, sid or unique_sid(conn), kind, resource, session_id, project_id, owner_instance, owner_kind, owner_user,
         backend_id, state, phase, reason, queued_at or ts,
         acquired_at if acquired_at is not None else (ts if state in HOLDING_STATES else None),
         heartbeat_at if heartbeat_at is not None else ts, ts, owner_pid, owner_started, tree, state_dir, label,
         previous_sid, dumps(meta or {})))
    return get(conn, lease_id)


def get(conn: sqlite3.Connection, lease_id: str) -> Lease | None:
    return _row(conn.execute(f"{_SELECT} WHERE l.id = ?", (lease_id,)).fetchone())


def get_by_sid(conn: sqlite3.Connection, sid: str) -> Lease | None:
    return _row(conn.execute(f"{_SELECT} WHERE l.sid = ?", (sid,)).fetchone())


def list_leases(conn: sqlite3.Connection, *, states: tuple[str, ...] | list[str] | None = None,
                kind: str | None = None, instance: str | None = None, session_id: int | None = None,
                resource: str | None = None, backend_id: str | None = None, owner_kind: str | None = None,
                limit: int | None = None, newest_first: bool = False) -> list[Lease]:
    clauses, params = [], []
    if states:
        clauses.append(f"l.state IN ({placeholders(len(states))})")
        params.extend(states)
    if kind:
        clauses.append("l.kind = ?")
        params.append(normalize_kind(kind))
    if instance is not None:
        clauses.append("l.owner_instance = ?")
        params.append(instance)
    if session_id is not None:
        clauses.append("l.session_id = ?")
        params.append(session_id)
    if resource is not None:
        clauses.append("l.resource = ?")
        params.append(resource)
    if backend_id is not None:
        clauses.append("l.backend_id = ?")
        params.append(backend_id)
    if owner_kind is not None:
        clauses.append("l.owner_kind = ?")
        params.append(owner_kind)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = " ORDER BY COALESCE(l.acquired_at, l.queued_at) DESC" if newest_first else " ORDER BY l.queued_at, l.id"
    sql = f"{_SELECT}{where}{order}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row(r) for r in conn.execute(sql, params)]


def holding(conn: sqlite3.Connection, kind: str | None = None) -> list[Lease]:
    return list_leases(conn, states=HOLDING_STATES, kind=kind)


def queue(conn: sqlite3.Connection, kind: str | None = None) -> list[Lease]:
    return list_leases(conn, states=("queued",), kind=kind)


def live_for_instance(conn: sqlite3.Connection, instance: str, kind: str | None = None) -> Lease | None:
    found = list_leases(conn, states=LIVE_STATES, instance=instance, kind=kind)
    return found[0] if found else None


def holder_of(conn: sqlite3.Connection, resource: str) -> Lease | None:
    found = list_leases(conn, states=HOLDING_STATES, resource=resource)
    return found[0] if found else None


def taken_resources(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT resource FROM leases WHERE state IN ('active', 'idle') AND resource IS NOT NULL")}


def history_for_resource(conn: sqlite3.Connection, resource: str, limit: int = 50) -> list[Lease]:
    return list_leases(conn, resource=resource, limit=limit, newest_first=True)


def for_session(conn: sqlite3.Connection, session_id: int, states: tuple[str, ...] | None = None) -> list[Lease]:
    return list_leases(conn, session_id=session_id, states=states, newest_first=True)


def update(conn: sqlite3.Connection, lease_id: str, **fields: Any) -> Lease | None:
    unknown = set(fields) - set(_COLUMNS) - {"project_id"}
    if unknown:
        raise ValueError(f"unknown lease fields: {', '.join(sorted(unknown))}")
    values = {k: v for k, v in fields.items() if v is not UNSET}
    if "state" in values and values["state"] not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    if "phase" in values and values["phase"] is not None and values["phase"] not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    if "meta" in values:
        values["meta"] = dumps(values["meta"] or {})
    if values:
        sql = ", ".join(f"{k} = ?" for k in values)
        conn.execute(f"UPDATE leases SET {sql} WHERE id = ?", (*values.values(), lease_id))
    return get(conn, lease_id)


def activate(conn: sqlite3.Connection, lease_id: str, resource: str, phase: str = "preparing",
             ts: float | None = None) -> Lease | None:
    stamp = ts or now()
    return update(conn, lease_id, state="active", resource=resource, phase=phase, acquired_at=stamp,
                  heartbeat_at=stamp)


def end(conn: sqlite3.Connection, lease_id: str, state: str, reason: str | None = None,
        ts: float | None = None) -> Lease | None:
    if state not in ENDED_STATES:
        raise ValueError(f"an ended lease is {' or '.join(ENDED_STATES)}")
    return update(conn, lease_id, state=state, reason=reason, released_at=ts or now())


def heartbeat(conn: sqlite3.Connection, lease_id: str, ts: float | None = None) -> Lease | None:
    stamp = ts or now()
    conn.execute(
        "UPDATE leases SET heartbeat_at = ?, last_poll_at = ?, idle_limit = NULL, "
        "state = CASE WHEN state = 'idle' THEN 'active' ELSE state END WHERE id = ?", (stamp, stamp, lease_id))
    return get(conn, lease_id)


def poll(conn: sqlite3.Connection, lease_id: str, ts: float | None = None) -> None:
    conn.execute("UPDATE leases SET last_poll_at = ? WHERE id = ?", (ts or now(), lease_id))


def delete(conn: sqlite3.Connection, lease_id: str) -> bool:
    return conn.execute("DELETE FROM leases WHERE id = ?", (lease_id,)).rowcount > 0


def prune_ended(conn: sqlite3.Connection, before_ts: float) -> int:
    return conn.execute(
        "DELETE FROM leases WHERE state IN ('released', 'broken') AND released_at < ? "
        "AND sid NOT IN (SELECT lease_sid FROM artifacts WHERE lease_sid IS NOT NULL)", (before_ts,)).rowcount
