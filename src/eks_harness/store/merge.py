from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from eks_harness.daemon import events as ev
from eks_harness.daemon.events import EventBus
from eks_harness.db.connection import Database
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos.artifacts import normalize_tag
from eks_harness.ids import parse_project_id

LEVELS = {"viewer": 0, "editor": 1}


class MergeError(ValueError):
    pass


@dataclass
class SourcePlan:
    project: str
    tag: str
    artifacts: int
    sessions: int
    grants: int
    retention_days: int | None
    pinned: int = 0
    retained: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"project": self.project, "tag": self.tag, "artifacts": self.artifacts, "sessions": self.sessions,
                "grants": self.grants, "retentionDays": self.retention_days, "pinnedToKeep": self.pinned,
                "artifactRetentionSet": self.retained}


@dataclass
class MergePlan:
    into: str
    created: bool
    sources: list[SourcePlan] = field(default_factory=list)
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"into": self.into, "created": self.created, "applied": self.applied,
                "sources": [s.as_dict() for s in self.sources],
                "artifacts": sum(s.artifacts for s in self.sources),
                "sessions": sum(s.sessions for s in self.sources)}


def _count(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _default_tag(project_id: str) -> str:
    return normalize_tag(project_id.split("/", 1)[1])


def plan(conn: sqlite3.Connection, sources: list[str], into: str, tags: dict[str, str] | None = None) -> MergePlan:
    try:
        parse_project_id(into)
    except ValueError as error:
        raise MergeError(str(error)) from None
    tags = tags or {}
    unique = list(dict.fromkeys(s.strip().lower() for s in sources if s.strip()))
    if not unique:
        raise MergeError("name at least one project to merge")
    if into in unique:
        raise MergeError(f"{into} is the target; leave it out of the sources")
    target = projects_repo.get(conn, into)
    result = MergePlan(into=into, created=target is None)
    for source in unique:
        project = projects_repo.get(conn, source)
        if project is None:
            raise MergeError(f"no project {source}")
        try:
            tag = normalize_tag(tags.get(source) or _default_tag(source))
        except ValueError as error:
            raise MergeError(str(error)) from None
        result.sources.append(SourcePlan(
            project=source, tag=tag,
            artifacts=_count(conn, "SELECT COUNT(*) FROM artifacts WHERE project_id = ?", source),
            sessions=_count(conn, "SELECT COUNT(*) FROM session_projects WHERE project_id = ?", source),
            grants=_count(conn, "SELECT COUNT(*) FROM grants WHERE project_id = ?", source),
            retention_days=project.retention_days))
    return result


def _retention(conn: sqlite3.Connection, source: SourcePlan, target_days: int | None) -> None:
    days = source.retention_days
    if days == target_days or days is None:
        return
    if days == 0:
        source.pinned = conn.execute(
            "UPDATE artifacts SET pinned = 1 WHERE project_id = ? AND pinned = 0", (source.project,)).rowcount
    else:
        source.retained = conn.execute(
            "UPDATE artifacts SET retention_days = ? WHERE project_id = ? AND retention_days IS NULL",
            (days, source.project)).rowcount


def merge(db: Database, sources: list[str], into: str, *, tags: dict[str, str] | None = None, title: str = "",
          description: str = "", events: EventBus | None = None, actor: str | None = None) -> MergePlan:
    with db.transaction() as conn:
        result = plan(conn, sources, into, tags)
        target = projects_repo.get(conn, into)
        if target is None:
            target = projects_repo.create(conn, into, title=title, description=description, implicit=False)
        elif title or description:
            target = projects_repo.update(conn, into, title=title or target.title,
                                          description=description or target.description)
        for source in result.sources:
            src = source.project
            conn.execute("INSERT OR IGNORE INTO artifact_tags (artifact_id, tag) "
                         "SELECT id, ? FROM artifacts WHERE project_id = ?", (source.tag, src))
            _retention(conn, source, target.retention_days)
            conn.execute("UPDATE artifacts SET project_id = ? WHERE project_id = ?", (into, src))
            conn.execute("UPDATE artifacts_fts SET project_id = ? WHERE project_id = ?", (into, src))
            conn.execute(
                "INSERT INTO session_projects (session_id, project_id, created_at, last_active_at) "
                "SELECT session_id, ?, created_at, last_active_at FROM session_projects WHERE project_id = ? "
                "ON CONFLICT (session_id, project_id) DO UPDATE SET "
                "created_at = MIN(session_projects.created_at, excluded.created_at), "
                "last_active_at = MAX(session_projects.last_active_at, excluded.last_active_at)", (into, src))
            for row in conn.execute("SELECT user_id, session_id, level, created_at FROM grants WHERE project_id = ?",
                                    (src,)).fetchall():
                existing = conn.execute(
                    "SELECT id, level FROM grants WHERE user_id = ? AND project_id = ? "
                    "AND COALESCE(session_id, 0) = COALESCE(?, 0)", (row["user_id"], into, row["session_id"])).fetchone()
                if existing is None:
                    conn.execute("INSERT INTO grants (user_id, project_id, session_id, level, created_at) "
                                 "VALUES (?, ?, ?, ?, ?)", (row["user_id"], into, row["session_id"], row["level"],
                                                            row["created_at"]))
                elif LEVELS[row["level"]] > LEVELS[existing["level"]]:
                    conn.execute("UPDATE grants SET level = ? WHERE id = ?", (row["level"], existing["id"]))
            columns = _asset_columns(conn)
            if columns:
                others = [c for c in columns if c != "project_id"]
                names = ", ".join(["project_id", *others])
                values = ", ".join(["?", *others])
                conn.execute(f"INSERT OR IGNORE INTO project_assets ({names}) "
                             f"SELECT {values} FROM project_assets WHERE project_id = ?", (into, src))
            conn.execute("UPDATE leases SET project_id = ? WHERE project_id = ?", (into, src))
            conn.execute("UPDATE events SET project_id = ? WHERE project_id = ?", (into, src))
            conn.execute("DELETE FROM projects WHERE id = ?", (src,))
        result.applied = True
    if events is not None:
        events.publish(ev.PROJECT_UPDATED, project_id=into, actor=actor,
                       detail={"project": into, "merged": [s.project for s in result.sources]})
        for source in result.sources:
            events.publish(ev.PROJECT_DELETED, project_id=source.project, actor=actor,
                           detail={"project": source.project, "mergedInto": into, "tag": source.tag})
    return result


def _asset_columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(project_assets)")]
