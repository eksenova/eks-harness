from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import now, placeholders
from eks_harness.ids import slugify


@dataclass(frozen=True)
class Session:
    id: int
    name: str
    slug: str
    created_at: float
    last_active_at: float


@dataclass(frozen=True)
class SessionStats:
    artifact_count: int = 0
    unseen_count: int = 0
    size_bytes: int = 0
    note_count: int = 0
    active_leases: int = 0
    last_artifact_at: float | None = None


def _row(row: sqlite3.Row | None) -> Session | None:
    if row is None:
        return None
    return Session(id=row["id"], name=row["name"], slug=row["slug"], created_at=row["created_at"],
                   last_active_at=row["last_active_at"])


def get(conn: sqlite3.Connection, session_id: int) -> Session | None:
    return _row(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


def get_by_slug(conn: sqlite3.Connection, slug: str) -> Session | None:
    return _row(conn.execute("SELECT * FROM sessions WHERE slug = ?", (slug,)).fetchone())


def find(conn: sqlite3.Connection, name_or_slug: str) -> Session | None:
    return get_by_slug(conn, name_or_slug) or get_by_slug(conn, slugify(name_or_slug))


def find_in_project(conn: sqlite3.Connection, project_id: str, name_or_slug: str) -> Session | None:
    session = find(conn, name_or_slug)
    if session is None or not is_linked(conn, session.id, project_id):
        return None
    return session


def list_sessions(conn: sqlite3.Connection, session_ids: list[int] | None = None,
                  project_id: str | None = None) -> list[Session]:
    sql, params = "SELECT s.* FROM sessions s", []
    clauses: list[str] = []
    if project_id is not None:
        sql += " JOIN session_projects sp ON sp.session_id = s.id AND sp.project_id = ?"
        params.append(project_id)
    if session_ids is not None:
        if not session_ids:
            return []
        clauses.append(f"s.id IN ({placeholders(len(session_ids))})")
        params.extend(session_ids)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    order = "sp.last_active_at DESC" if project_id is not None else "s.last_active_at DESC"
    sql += f" ORDER BY {order}, s.id DESC"
    return [_row(r) for r in conn.execute(sql, params)]


def list_for_project(conn: sqlite3.Connection, project_id: str,
                     session_ids: list[int] | None = None) -> list[Session]:
    return list_sessions(conn, session_ids, project_id)


def project_ids(conn: sqlite3.Connection, session_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT project_id FROM session_projects WHERE session_id = ? ORDER BY project_id", (session_id,))]


def projects_by_session(conn: sqlite3.Connection, session_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {sid: [] for sid in session_ids}
    if not session_ids:
        return result
    for row in conn.execute(
            f"SELECT session_id, project_id FROM session_projects WHERE session_id IN "
            f"({placeholders(len(session_ids))}) ORDER BY project_id", list(session_ids)):
        result.setdefault(row[0], []).append(row[1])
    return result


def is_linked(conn: sqlite3.Connection, session_id: int, project_id: str) -> bool:
    return conn.execute("SELECT 1 FROM session_projects WHERE session_id = ? AND project_id = ?",
                        (session_id, project_id)).fetchone() is not None


def link(conn: sqlite3.Connection, session_id: int, project_id: str, ts: float | None = None) -> bool:
    stamp = ts or now()
    return conn.execute(
        "INSERT OR IGNORE INTO session_projects (session_id, project_id, created_at, last_active_at) "
        "VALUES (?, ?, ?, ?)", (session_id, project_id, stamp, stamp)).rowcount > 0


def unlink(conn: sqlite3.Connection, session_id: int, project_id: str) -> bool:
    return conn.execute("DELETE FROM session_projects WHERE session_id = ? AND project_id = ?",
                        (session_id, project_id)).rowcount > 0


def unique_slug(conn: sqlite3.Connection, base: str, exclude_id: int | None = None) -> str:
    slug = slugify(base)
    candidate, counter = slug, 2
    while True:
        row = conn.execute("SELECT id FROM sessions WHERE slug = ?", (candidate,)).fetchone()
        if row is None or row["id"] == exclude_id:
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


def create(conn: sqlite3.Connection, name: str, slug: str | None = None) -> Session:
    ts = now()
    final_slug = unique_slug(conn, slug or name)
    cursor = conn.execute(
        "INSERT INTO sessions (name, slug, created_at, last_active_at) VALUES (?, ?, ?, ?)",
        (name.strip() or final_slug, final_slug, ts, ts))
    return get(conn, cursor.lastrowid)


def ensure(conn: sqlite3.Connection, name: str) -> tuple[Session, bool]:
    existing = find(conn, name)
    if existing:
        return existing, False
    return create(conn, name), True


def ensure_in_project(conn: sqlite3.Connection, project_id: str, name: str) -> tuple[Session, bool]:
    session, created = ensure(conn, name)
    linked = link(conn, session.id, project_id)
    return session, created or linked


def rename(conn: sqlite3.Connection, session_id: int, name: str, slug: str | None = None) -> Session | None:
    if get(conn, session_id) is None:
        return None
    new_slug = unique_slug(conn, slug or name, exclude_id=session_id)
    conn.execute("UPDATE sessions SET name = ?, slug = ? WHERE id = ?", (name.strip() or new_slug, new_slug, session_id))
    return get(conn, session_id)


def touch(conn: sqlite3.Connection, session_id: int, ts: float | None = None, project_id: str | None = None) -> None:
    stamp = ts or now()
    conn.execute("UPDATE sessions SET last_active_at = MAX(last_active_at, ?) WHERE id = ?", (stamp, session_id))
    if project_id is not None:
        link(conn, session_id, project_id, stamp)
        conn.execute("UPDATE session_projects SET last_active_at = MAX(last_active_at, ?) "
                     "WHERE session_id = ? AND project_id = ?", (stamp, session_id, project_id))
        conn.execute("UPDATE projects SET updated_at = MAX(updated_at, ?) WHERE id = ?", (stamp, project_id))


def delete(conn: sqlite3.Connection, session_id: int) -> bool:
    return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0


def stats(conn: sqlite3.Connection, session_id: int, user_id: int | None = None, project_id: str | None = None,
          artifact_scope: tuple[str, list] | None = None) -> SessionStats:
    return stats_many(conn, [session_id], user_id, project_id, artifact_scope).get(session_id, SessionStats())


def stats_for_project(conn: sqlite3.Connection, project_id: str, user_id: int | None = None,
                      session_ids: list[int] | None = None) -> dict[int, SessionStats]:
    ids = session_ids if session_ids is not None else [s.id for s in list_for_project(conn, project_id)]
    return stats_many(conn, ids, user_id, project_id)


def stats_many(conn: sqlite3.Connection, session_ids: list[int], user_id: int | None = None,
               project_id: str | None = None,
               artifact_scope: tuple[str, list] | None = None) -> dict[int, SessionStats]:
    if not session_ids:
        return {}
    where, params = [f"a.session_id IN ({placeholders(len(session_ids))})"], list(session_ids)
    if project_id is not None:
        where.append("a.project_id = ?")
        params.append(project_id)
    if artifact_scope is not None:
        where.append(artifact_scope[0])
        params.extend(artifact_scope[1])
    unseen, unseen_params = "0", []
    if user_id is not None:
        unseen = ("SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id AND x.user_id = ?) "
                  "THEN 1 ELSE 0 END)")
        unseen_params = [user_id]
    artifact_rows = {r["sid"]: r for r in conn.execute(
        f"SELECT a.session_id AS sid, COUNT(*) AS n, COALESCE(SUM(a.size), 0) AS bytes, MAX(a.created_at) AS last, "
        f"{unseen} AS unseen FROM artifacts a WHERE {' AND '.join(where)} GROUP BY a.session_id",
        [*unseen_params, *params])}
    ids_sql = placeholders(len(session_ids))
    notes = {r[0]: r[1] for r in conn.execute(
        f"SELECT session_id, COUNT(*) FROM notes WHERE session_id IN ({ids_sql}) GROUP BY session_id", session_ids)}
    lease_sql = (f"SELECT session_id, COUNT(*) FROM leases WHERE session_id IN ({ids_sql}) "
                 f"AND state IN ('active', 'idle')")
    lease_params: list = list(session_ids)
    if project_id is not None:
        lease_sql += " AND project_id = ?"
        lease_params.append(project_id)
    leases = {r[0]: r[1] for r in conn.execute(lease_sql + " GROUP BY session_id", lease_params)}
    result = {}
    for sid in session_ids:
        row = artifact_rows.get(sid)
        result[sid] = SessionStats(
            artifact_count=row["n"] if row else 0, unseen_count=(row["unseen"] or 0) if row else 0,
            size_bytes=row["bytes"] if row else 0, note_count=notes.get(sid, 0), active_leases=leases.get(sid, 0),
            last_artifact_at=row["last"] if row else None)
    return result
