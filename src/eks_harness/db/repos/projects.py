from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import UNSET, assignments, now
from eks_harness.ids import parse_project_id


@dataclass(frozen=True)
class Project:
    id: str
    owner: str
    name: str
    title: str
    description: str
    implicit: bool
    retention_days: int | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class ProjectStats:
    session_count: int = 0
    artifact_count: int = 0
    unseen_count: int = 0
    size_bytes: int = 0
    last_activity_at: float | None = None
    project_file_count: int = 0


def _row(row: sqlite3.Row | None) -> Project | None:
    if row is None:
        return None
    return Project(id=row["id"], owner=row["owner"], name=row["name"], title=row["title"],
                   description=row["description"], implicit=bool(row["implicit"]),
                   retention_days=row["retention_days"], created_at=row["created_at"], updated_at=row["updated_at"])


def get(conn: sqlite3.Connection, project_id: str) -> Project | None:
    return _row(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())


def list_projects(conn: sqlite3.Connection, scope_sql: tuple[str, list] | None = None) -> list[Project]:
    where, params = (f" WHERE {scope_sql[0]}", scope_sql[1]) if scope_sql else ("", [])
    return [_row(r) for r in conn.execute(f"SELECT * FROM projects{where} ORDER BY id", params)]


def create(conn: sqlite3.Connection, project_id: str, *, title: str = "", description: str = "",
           implicit: bool = False, retention_days: int | None = None) -> Project:
    owner, name = parse_project_id(project_id)
    ts = now()
    conn.execute(
        "INSERT INTO projects (id, owner, name, title, description, implicit, retention_days, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, owner, name, title or "", description or "", int(implicit), retention_days, ts, ts))
    return get(conn, project_id)


def ensure(conn: sqlite3.Connection, project_id: str) -> tuple[Project, bool]:
    existing = get(conn, project_id)
    if existing:
        return existing, False
    parse_project_id(project_id)
    ts = now()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO projects (id, owner, name, title, description, implicit, retention_days, created_at, "
        "updated_at) VALUES (?, ?, ?, '', '', 1, NULL, ?, ?)", (project_id, *project_id.split("/", 1), ts, ts))
    return get(conn, project_id), cursor.rowcount > 0


def update(conn: sqlite3.Connection, project_id: str, *, title=UNSET, description=UNSET,
           retention_days=UNSET) -> Project | None:
    sql, params = assignments({"title": title, "description": description, "retention_days": retention_days})
    if sql:
        conn.execute(f"UPDATE projects SET {sql}, implicit = 0, updated_at = ? WHERE id = ?",
                     (*params, now(), project_id))
    return get(conn, project_id)


def touch(conn: sqlite3.Connection, project_id: str, ts: float | None = None) -> None:
    conn.execute("UPDATE projects SET updated_at = MAX(updated_at, ?) WHERE id = ?", (ts or now(), project_id))


def delete(conn: sqlite3.Connection, project_id: str) -> bool:
    return conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount > 0


def stats(conn: sqlite3.Connection, project_id: str, user_id: int | None = None,
          scope_sql: tuple[str, list] | None = None) -> ProjectStats:
    return stats_all(conn, user_id, [project_id], scope_sql).get(project_id, ProjectStats())


def stats_all(conn: sqlite3.Connection, user_id: int | None = None, project_ids: list[str] | None = None,
              scope_sql: tuple[str, list] | None = None) -> dict[str, ProjectStats]:
    project_filter, project_params = "", []
    if project_ids is not None:
        if not project_ids:
            return {}
        project_filter = f" AND a.project_id IN ({','.join('?' * len(project_ids))})"
        project_params = list(project_ids)
    scope_clause, scope_params = scope_sql if scope_sql else ("1 = 1", [])
    unseen_expr = "0"
    unseen_params: list = []
    if user_id is not None:
        unseen_expr = "SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM seen s WHERE s.artifact_id = a.id AND s.user_id = ?) THEN 1 ELSE 0 END)"
        unseen_params = [user_id]
    rows = conn.execute(
        f"SELECT a.project_id AS pid, COUNT(*) AS n, COALESCE(SUM(a.size), 0) AS bytes, MAX(a.created_at) AS last, "
        f"SUM(CASE WHEN a.session_id IS NULL THEN 1 ELSE 0 END) AS project_files, "
        f"{unseen_expr} AS unseen FROM artifacts a WHERE {scope_clause}{project_filter} GROUP BY a.project_id",
        [*unseen_params, *scope_params, *project_params]).fetchall()
    result: dict[str, ProjectStats] = {}
    by_project = {r["pid"]: r for r in rows}
    session_filter, session_params = "", []
    if project_ids is not None:
        session_filter = f" WHERE project_id IN ({','.join('?' * len(project_ids))})"
        session_params = list(project_ids)
    sessions = {r["project_id"]: (r["n"], r["last"]) for r in conn.execute(
        f"SELECT project_id, COUNT(*) AS n, MAX(last_active_at) AS last FROM session_projects{session_filter} "
        f"GROUP BY project_id", session_params)}
    for pid in set(by_project) | set(sessions):
        row = by_project.get(pid)
        session_count, session_last = sessions.get(pid, (0, None))
        last = max([v for v in (row["last"] if row else None, session_last) if v is not None], default=None)
        result[pid] = ProjectStats(
            session_count=session_count,
            artifact_count=row["n"] if row else 0,
            unseen_count=(row["unseen"] or 0) if row else 0,
            size_bytes=row["bytes"] if row else 0,
            last_activity_at=last,
            project_file_count=(row["project_files"] or 0) if row else 0,
        )
    return result
