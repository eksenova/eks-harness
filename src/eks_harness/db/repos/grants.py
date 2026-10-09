from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from eks_harness.db.common import now, placeholders

LEVELS = ("viewer", "editor")
LEVEL_RANK = {"viewer": 1, "editor": 2, "admin": 3}


def level_at_least(level: str | None, required: str) -> bool:
    return LEVEL_RANK.get(level or "", 0) >= LEVEL_RANK[required]


def best_level(*levels: str | None) -> str | None:
    ranked = [lvl for lvl in levels if lvl]
    return max(ranked, key=lambda lvl: LEVEL_RANK[lvl]) if ranked else None


@dataclass(frozen=True)
class Grant:
    id: int
    user_id: int
    project_id: str
    session_id: int | None
    level: str
    created_at: float
    username: str | None = None
    session_slug: str | None = None
    session_name: str | None = None


def _row(row: sqlite3.Row | None) -> Grant | None:
    if row is None:
        return None
    return Grant(
        id=row["id"], user_id=row["user_id"], project_id=row["project_id"], session_id=row["session_id"],
        level=row["level"], created_at=row["created_at"], username=row["username"],
        session_slug=row["session_slug"], session_name=row["session_name"],
    )


_SELECT = (
    "SELECT g.*, u.username AS username, s.slug AS session_slug, s.name AS session_name "
    "FROM grants g JOIN users u ON u.id = g.user_id LEFT JOIN sessions s ON s.id = g.session_id"
)


def create(conn: sqlite3.Connection, user_id: int, project_id: str, level: str,
           session_id: int | None = None) -> Grant:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    existing = conn.execute(
        "SELECT id FROM grants WHERE user_id = ? AND project_id = ? AND COALESCE(session_id, 0) = COALESCE(?, 0)",
        (user_id, project_id, session_id)).fetchone()
    if existing:
        conn.execute("UPDATE grants SET level = ? WHERE id = ?", (level, existing["id"]))
        return get(conn, existing["id"])
    cursor = conn.execute(
        "INSERT INTO grants (user_id, project_id, session_id, level, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, project_id, session_id, level, now()))
    return get(conn, cursor.lastrowid)


def get(conn: sqlite3.Connection, grant_id: int) -> Grant | None:
    return _row(conn.execute(f"{_SELECT} WHERE g.id = ?", (grant_id,)).fetchone())


def list_grants(conn: sqlite3.Connection, user_id: int | None = None, project_id: str | None = None,
                session_id: int | None = None) -> list[Grant]:
    clauses, params = [], []
    if user_id is not None:
        clauses.append("g.user_id = ?")
        params.append(user_id)
    if project_id is not None:
        clauses.append("g.project_id = ?")
        params.append(project_id)
    if session_id is not None:
        clauses.append("g.session_id = ?")
        params.append(session_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return [_row(r) for r in conn.execute(f"{_SELECT}{where} ORDER BY g.project_id, g.session_id, u.username", params)]


def delete(conn: sqlite3.Connection, grant_id: int) -> bool:
    return conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,)).rowcount > 0


def delete_matching(conn: sqlite3.Connection, user_id: int, project_id: str, session_id: int | None = None) -> int:
    return conn.execute(
        "DELETE FROM grants WHERE user_id = ? AND project_id = ? AND COALESCE(session_id, 0) = COALESCE(?, 0)",
        (user_id, project_id, session_id)).rowcount


def project_level(conn: sqlite3.Connection, user_id: int, project_id: str) -> str | None:
    row = conn.execute(
        "SELECT level FROM grants WHERE user_id = ? AND project_id = ? AND session_id IS NULL",
        (user_id, project_id)).fetchone()
    return row["level"] if row else None


def session_level(conn: sqlite3.Connection, user_id: int, project_id: str, session_id: int) -> str | None:
    rows = conn.execute(
        "SELECT level FROM grants WHERE user_id = ? AND project_id = ? AND (session_id IS NULL OR session_id = ?)",
        (user_id, project_id, session_id)).fetchall()
    return best_level(*(r["level"] for r in rows))


def has_any_in_project(conn: sqlite3.Connection, user_id: int, project_id: str) -> bool:
    return conn.execute("SELECT 1 FROM grants WHERE user_id = ? AND project_id = ? LIMIT 1",
                        (user_id, project_id)).fetchone() is not None


@dataclass(frozen=True)
class AccessScope:
    everything: bool = False
    projects: dict[str, str] = field(default_factory=dict)
    sessions: dict[int, str] = field(default_factory=dict)
    session_projects: dict[int, str] = field(default_factory=dict)

    def project_ids(self) -> set[str]:
        return set(self.projects) | set(self.session_projects.values())

    def can_see_project(self, project_id: str) -> bool:
        return self.everything or project_id in self.projects or project_id in self.session_projects.values()

    def level_for(self, project_id: str | None, session_id: int | None = None) -> str | None:
        if self.everything:
            return "admin"
        if project_id is None:
            return None
        return best_level(self.projects.get(project_id),
                          self.sessions.get(session_id) if session_id is not None else None)

    def can(self, project_id: str | None, session_id: int | None, required: str) -> bool:
        return level_at_least(self.level_for(project_id, session_id), required)

    def has_level_anywhere(self, required: str) -> bool:
        if self.everything:
            return True
        return any(level_at_least(level, required) for level in [*self.projects.values(), *self.sessions.values()])

    def sql(self, project_column: str, session_column: str, required: str = "viewer") -> tuple[str, list]:
        if self.everything:
            return "1 = 1", []
        projects = [p for p, lvl in self.projects.items() if level_at_least(lvl, required)]
        sessions = [s for s, lvl in self.sessions.items() if level_at_least(lvl, required)]
        parts, params = [], []
        if projects:
            parts.append(f"{project_column} IN ({placeholders(len(projects))})")
            params.extend(projects)
        if sessions:
            parts.append(f"{session_column} IN ({placeholders(len(sessions))})")
            params.extend(sessions)
        if not parts:
            return "1 = 0", []
        return "(" + " OR ".join(parts) + ")", params

    def project_sql(self, project_column: str) -> tuple[str, list]:
        if self.everything:
            return "1 = 1", []
        ids = sorted(self.project_ids())
        if not ids:
            return "1 = 0", []
        return f"{project_column} IN ({placeholders(len(ids))})", ids


def scope_for_user(conn: sqlite3.Connection, user_id: int, role: str) -> AccessScope:
    if role == "admin":
        return AccessScope(everything=True)
    projects: dict[str, str] = {}
    sessions: dict[int, str] = {}
    session_projects: dict[int, str] = {}
    for row in conn.execute("SELECT project_id, session_id, level FROM grants WHERE user_id = ?", (user_id,)):
        if row["session_id"] is None:
            projects[row["project_id"]] = best_level(projects.get(row["project_id"]), row["level"])
        else:
            sessions[row["session_id"]] = best_level(sessions.get(row["session_id"]), row["level"])
            session_projects[row["session_id"]] = row["project_id"]
    return AccessScope(everything=False, projects=projects, sessions=sessions, session_projects=session_projects)
