from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from eks_harness.db.common import dumps, loads, now

JOB_STATES = ("queued", "assigned", "running", "done", "failed", "cancelled")
LIVE_JOB_STATES = ("queued", "assigned", "running")


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    token_prefix: str
    token_sha256: str
    created_at: float
    last_seen_at: float | None
    capabilities: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "createdAt": self.created_at, "lastSeenAt": self.last_seen_at,
                "capabilities": self.capabilities, "config": self.config, "disabled": self.disabled,
                "tokenPrefix": self.token_prefix}


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    state: str
    node_id: str | None
    slot: str | None
    requirements: dict[str, Any]
    payload: dict[str, Any]
    inputs: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: str | None
    progress: float | None
    message: str | None
    attempts: int
    priority: int
    owner: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None

    @property
    def live(self) -> bool:
        return self.state in LIVE_JOB_STATES

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "state": self.state, "node": self.node_id, "slot": self.slot,
                "requirements": self.requirements, "payload": self.payload, "inputs": self.inputs,
                "result": self.result, "error": self.error, "progress": self.progress, "message": self.message,
                "attempts": self.attempts, "priority": self.priority, "owner": self.owner,
                "createdAt": self.created_at, "startedAt": self.started_at, "finishedAt": self.finished_at}


def _node(row: sqlite3.Row | None) -> Node | None:
    if row is None:
        return None
    return Node(id=row["id"], label=row["label"], token_prefix=row["token_prefix"], token_sha256=row["token_sha256"],
                created_at=row["created_at"], last_seen_at=row["last_seen_at"],
                capabilities=loads(row["capabilities"]), config=loads(row["config"]), disabled=bool(row["disabled"]))


def _job(row: sqlite3.Row | None) -> Job | None:
    if row is None:
        return None
    return Job(id=row["id"], kind=row["kind"], state=row["state"], node_id=row["node_id"], slot=row["slot"],
               requirements=loads(row["requirements"]), payload=loads(row["payload"]),
               inputs=loads(row["inputs"], default=[]), result=loads(row["result"], default=None) if row["result"] else None,
               error=row["error"], progress=row["progress"], message=row["message"], attempts=row["attempts"],
               priority=row["priority"], owner=row["owner"], created_at=row["created_at"],
               started_at=row["started_at"], finished_at=row["finished_at"])


def insert_node(conn: sqlite3.Connection, node_id: str, label: str, prefix: str, secret_sha256: str,
                config: dict[str, Any] | None = None) -> Node:
    conn.execute("INSERT INTO nodes (id, label, token_prefix, token_sha256, created_at, config) VALUES (?, ?, ?, ?, ?, ?)",
                 (node_id, label, prefix, secret_sha256, now(), dumps(config or {})))
    return get_node(conn, node_id)


def get_node(conn: sqlite3.Connection, node_id: str) -> Node | None:
    return _node(conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())


def get_node_by_prefix(conn: sqlite3.Connection, prefix: str) -> Node | None:
    return _node(conn.execute("SELECT * FROM nodes WHERE token_prefix = ?", (prefix,)).fetchone())


def list_nodes(conn: sqlite3.Connection) -> list[Node]:
    return [_node(r) for r in conn.execute("SELECT * FROM nodes ORDER BY id")]


def update_node(conn: sqlite3.Connection, node_id: str, **fields: Any) -> Node | None:
    columns = {"label": str, "capabilities": dumps, "config": dumps, "disabled": int, "last_seen_at": float,
               "token_prefix": str, "token_sha256": str}
    sets, params = [], []
    for key, value in fields.items():
        if key not in columns:
            raise KeyError(key)
        sets.append(f"{key} = ?")
        params.append(columns[key](value) if value is not None else None)
    if sets:
        conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?", (*params, node_id))
    return get_node(conn, node_id)


def delete_node(conn: sqlite3.Connection, node_id: str) -> bool:
    return conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,)).rowcount > 0


def insert_job(conn: sqlite3.Connection, job_id: str, kind: str, *, requirements: dict[str, Any],
               payload: dict[str, Any], inputs: list[dict[str, Any]], priority: int = 0,
               owner: str | None = None) -> Job:
    conn.execute("INSERT INTO node_jobs (id, kind, state, requirements, payload, inputs, priority, owner, created_at) "
                 "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                 (job_id, kind, dumps(requirements), dumps(payload), dumps(inputs), priority, owner, now()))
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: str) -> Job | None:
    return _job(conn.execute("SELECT * FROM node_jobs WHERE id = ?", (job_id,)).fetchone())


def list_jobs(conn: sqlite3.Connection, *, states: tuple[str, ...] | None = None, node_id: str | None = None,
              kind: str | None = None, limit: int = 200) -> list[Job]:
    clauses, params = [], []
    if states:
        clauses.append(f"state IN ({', '.join('?' for _ in states)})")
        params.extend(states)
    if node_id:
        clauses.append("node_id = ?")
        params.append(node_id)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM node_jobs {where} ORDER BY created_at DESC LIMIT ?", (*params, limit))
    return [_job(r) for r in rows]


def queued_jobs(conn: sqlite3.Connection) -> list[Job]:
    rows = conn.execute("SELECT * FROM node_jobs WHERE state = 'queued' ORDER BY priority DESC, created_at")
    return [_job(r) for r in rows]


def update_job(conn: sqlite3.Connection, job_id: str, **fields: Any) -> Job | None:
    encoders = {"state": str, "node_id": lambda v: v, "slot": lambda v: v, "result": dumps, "error": str,
                "progress": float, "message": str, "attempts": int, "started_at": float, "finished_at": float}
    sets, params = [], []
    for key, value in fields.items():
        if key not in encoders:
            raise KeyError(key)
        sets.append(f"{key} = ?")
        params.append(encoders[key](value) if value is not None else None)
    if sets:
        conn.execute(f"UPDATE node_jobs SET {', '.join(sets)} WHERE id = ?", (*params, job_id))
    return get_job(conn, job_id)


def prune_jobs(conn: sqlite3.Connection, before: float) -> int:
    return conn.execute("DELETE FROM node_jobs WHERE state IN ('done', 'failed', 'cancelled') AND finished_at < ?",
                        (before,)).rowcount
