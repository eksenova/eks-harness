from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from eks_harness.db.common import now, placeholders


def mark(conn: sqlite3.Connection, user_id: int, artifact_ids: Iterable[str], ts: float | None = None) -> int:
    ids = list(dict.fromkeys(artifact_ids))
    if not ids:
        return 0
    stamp = ts or now()
    existing = {r[0] for r in conn.execute(
        f"SELECT id FROM artifacts WHERE id IN ({placeholders(len(ids))})", ids)}
    rows = [(user_id, artifact_id, stamp) for artifact_id in ids if artifact_id in existing]
    conn.executemany("INSERT OR IGNORE INTO seen (user_id, artifact_id, seen_at) VALUES (?, ?, ?)", rows)
    return len(rows)


def unmark(conn: sqlite3.Connection, user_id: int, artifact_ids: Iterable[str]) -> int:
    ids = list(dict.fromkeys(artifact_ids))
    if not ids:
        return 0
    return conn.execute(
        f"DELETE FROM seen WHERE user_id = ? AND artifact_id IN ({placeholders(len(ids))})", [user_id, *ids]).rowcount


def is_seen(conn: sqlite3.Connection, user_id: int, artifact_id: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE user_id = ? AND artifact_id = ?",
                        (user_id, artifact_id)).fetchone() is not None


def seen_at(conn: sqlite3.Connection, user_id: int, artifact_id: str) -> float | None:
    row = conn.execute("SELECT seen_at FROM seen WHERE user_id = ? AND artifact_id = ?",
                       (user_id, artifact_id)).fetchone()
    return row[0] if row else None


def seen_ids(conn: sqlite3.Connection, user_id: int, artifact_ids: Iterable[str]) -> set[str]:
    ids = list(dict.fromkeys(artifact_ids))
    found: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        found |= {r[0] for r in conn.execute(
            f"SELECT artifact_id FROM seen WHERE user_id = ? AND artifact_id IN ({placeholders(len(chunk))})",
            [user_id, *chunk])}
    return found


def unseen_count(conn: sqlite3.Connection, user_id: int, project_id: str | None = None,
                 session_id: int | None = None, scope_sql: tuple[str, list] | None = None) -> int:
    clauses = ["NOT EXISTS (SELECT 1 FROM seen s WHERE s.artifact_id = a.id AND s.user_id = ?)"]
    params: list = [user_id]
    if project_id is not None:
        clauses.append("a.project_id = ?")
        params.append(project_id)
    if session_id is not None:
        clauses.append("a.session_id = ?")
        params.append(session_id)
    if scope_sql is not None:
        clauses.append(scope_sql[0])
        params.extend(scope_sql[1])
    return conn.execute(f"SELECT COUNT(*) FROM artifacts a WHERE {' AND '.join(clauses)}", params).fetchone()[0]


def clear_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("DELETE FROM seen WHERE user_id = ?", (user_id,)).rowcount
