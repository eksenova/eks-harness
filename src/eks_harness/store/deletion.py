from __future__ import annotations

import logging
import os
import posixpath
import secrets
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from eks_harness.api.errors import conflict
from eks_harness.daemon import events as ev
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.common import placeholders
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import events as events_repo
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos.projects import Project
from eks_harness.db.repos.sessions import Session
from eks_harness.paths import Paths
from eks_harness.store import layout

log = logging.getLogger("eks_harness.store.deletion")


@dataclass
class DeletePlan:
    artifact_ids: list[str] = field(default_factory=list)
    rel_dirs: list[str] = field(default_factory=list)
    bytes: int = 0
    sessions: int = 0
    notes: int = 0
    shares: int = 0
    session_ids: list[int] = field(default_factory=list)
    active_sids: list[str] = field(default_factory=list)

    def summary(self, deleted: bool) -> dict:
        return {"deleted": deleted, "artifacts": len(self.artifact_ids), "sessions": self.sessions,
                "notes": self.notes, "shares": self.shares, "bytes": self.bytes}


def _chunks(values: Sequence, size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _artifact_rows(conn: sqlite3.Connection, where: str, params: Sequence) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT a.id, a.rel_path, a.size FROM artifacts a WHERE {where}", params).fetchall()


def _fill_artifacts(conn: sqlite3.Connection, plan: DeletePlan, rows: Sequence[sqlite3.Row]) -> None:
    for row in rows:
        plan.artifact_ids.append(row["id"])
        plan.rel_dirs.append(posixpath.dirname(row["rel_path"]))
        plan.bytes += row["size"] or 0
    for chunk in _chunks(plan.artifact_ids):
        plan.shares += conn.execute(
            f"SELECT COUNT(*) FROM shares WHERE artifact_id IN ({placeholders(len(chunk))})", chunk).fetchone()[0]


def plan_artifacts(conn: sqlite3.Connection, artifact_ids: Sequence[str],
                   unpinned_before: float | None = None,
                   still_expired: tuple[str, Sequence] | None = None) -> DeletePlan:
    plan = DeletePlan()
    ids = list(dict.fromkeys(artifact_ids))
    rows = []
    for chunk in _chunks(ids):
        where = f"id IN ({placeholders(len(chunk))})"
        params: list = list(chunk)
        if unpinned_before is not None:
            where += " AND pinned = 0 AND created_at < ?"
            params.append(unpinned_before)
        if still_expired is not None:
            where += f" AND {still_expired[0]}"
            params.extend(still_expired[1])
        rows.extend(_artifact_rows(conn, where, params))
    _fill_artifacts(conn, plan, rows)
    return plan


def _live_sids(conn: sqlite3.Connection, where: str, params: Sequence) -> list[str]:
    return [r[0] for r in conn.execute(
        f"SELECT sid FROM leases WHERE state IN ({placeholders(len(leases_repo.LIVE_STATES))}) AND {where} "
        f"ORDER BY queued_at DESC", [*leases_repo.LIVE_STATES, *params])]


def plan_session(conn: sqlite3.Connection, session: Session, project_id: str | None = None) -> DeletePlan:
    linked = sessions_repo.project_ids(conn, session.id)
    whole = project_id is None or set(linked) <= {project_id}
    plan = DeletePlan(sessions=1 if whole else 0, session_ids=[session.id] if whole else [])
    if project_id is None:
        _fill_artifacts(conn, plan, _artifact_rows(conn, "session_id = ?", [session.id]))
        plan.active_sids = _live_sids(conn, "session_id = ?", [session.id])
    else:
        _fill_artifacts(conn, plan, _artifact_rows(conn, "session_id = ? AND project_id = ?",
                                                   [session.id, project_id]))
        plan.active_sids = _live_sids(conn, "session_id = ? AND (project_id = ? OR project_id IS NULL)",
                                      [session.id, project_id])
    if whole:
        plan.notes = conn.execute("SELECT COUNT(*) FROM notes WHERE session_id = ?", (session.id,)).fetchone()[0]
    return plan


def _sessions_only_in(conn: sqlite3.Connection, project_id: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT sp.session_id FROM session_projects sp WHERE sp.project_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM session_projects o WHERE o.session_id = sp.session_id AND o.project_id <> ?)",
        (project_id, project_id))]


def plan_project(conn: sqlite3.Connection, project: Project) -> DeletePlan:
    plan = DeletePlan()
    _fill_artifacts(conn, plan, _artifact_rows(conn, "project_id = ?", [project.id]))
    plan.session_ids = _sessions_only_in(conn, project.id)
    plan.sessions = len(plan.session_ids)
    if plan.session_ids:
        plan.notes = conn.execute(
            f"SELECT COUNT(*) FROM notes WHERE session_id IN ({placeholders(len(plan.session_ids))})",
            plan.session_ids).fetchone()[0]
    plan.active_sids = _live_sids(conn, "project_id = ?", [project.id])
    return plan


def _execute(db: Database, paths: Paths, collect: Callable[[sqlite3.Connection], DeletePlan],
             remove_rows: Callable[[sqlite3.Connection, DeletePlan], None]) -> DeletePlan:
    trash = paths.tmp_dir / f"trash-{os.getpid()}-{secrets.token_hex(8)}"
    moved: list[tuple[Path, Path]] = []
    try:
        with db.transaction() as conn:
            plan = collect(conn)
            for index, rel_dir in enumerate(dict.fromkeys(plan.rel_dirs)):
                source = layout.file_path(paths, rel_dir)
                if not source.exists():
                    continue
                target = trash / f"{index:06d}"
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
                moved.append((source, target))
            remove_rows(conn, plan)
    except BaseException:
        for source, target in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, source)
            except OSError:
                log.exception("could not restore %s after a failed delete", source)
        shutil.rmtree(trash, ignore_errors=True)
        raise
    shutil.rmtree(trash, ignore_errors=True)
    for source, _ in moved:
        layout.remove_empty_parents(source.parent, paths.store_dir)
    return plan


def delete_artifacts(db: Database, paths: Paths, artifact_ids: Sequence[str], *, events: EventBus | None = None,
                     actor: str | None = None, unpinned_before: float | None = None,
                     still_expired: tuple[str, Sequence] | None = None) -> DeletePlan:
    snapshot = {a.id: a for a in artifacts_repo.get_many(db.conn(), artifact_ids)}

    def remove(conn: sqlite3.Connection, plan: DeletePlan) -> None:
        artifacts_repo.delete_many(conn, plan.artifact_ids)

    plan = _execute(db, paths, lambda conn: plan_artifacts(conn, artifact_ids, unpinned_before, still_expired), remove)
    if events is not None:
        for artifact_id in plan.artifact_ids:
            artifact = snapshot.get(artifact_id)
            if artifact is None:
                continue
            events.publish(ev.ARTIFACT_DELETED, lease_sid=artifact.lease_sid, session_id=artifact.session_id,
                           project_id=artifact.project_id, actor=actor,
                           detail={"id": artifact.id, "kind": artifact.kind, "filename": artifact.filename,
                                   "size": artifact.size})
    return plan


def delete_session(db: Database, paths: Paths, session: Session, *, project_id: str | None = None,
                   force: bool = False, events: EventBus | None = None, actor: str | None = None) -> DeletePlan:
    where = f"{session.name} in {project_id}" if project_id else session.name

    def collect(conn: sqlite3.Connection) -> DeletePlan:
        plan = plan_session(conn, session, project_id)
        if plan.active_sids and not force:
            raise conflict(f"The session {where} has live leases ({', '.join(plan.active_sids)}). "
                           f"Release them first or delete with force.", error="session_in_use",
                           sids=plan.active_sids)
        return plan

    def remove(conn: sqlite3.Connection, plan: DeletePlan) -> None:
        artifacts_repo.delete_many(conn, plan.artifact_ids)
        if plan.session_ids:
            events_repo.delete_for_session(conn, session.id)
            sessions_repo.delete(conn, session.id)
        else:
            conn.execute("DELETE FROM events WHERE session_id = ? AND project_id = ?", (session.id, project_id))
            conn.execute("UPDATE leases SET session_id = NULL WHERE session_id = ? AND project_id = ?",
                         (session.id, project_id))
            sessions_repo.unlink(conn, session.id, project_id)

    plan = _execute(db, paths, collect, remove)
    if events is not None:
        events.publish(ev.SESSION_DELETED, project_id=project_id, actor=actor,
                       detail={"sessionId": session.id, "slug": session.slug, "name": session.name,
                               "project": project_id, "whole": bool(plan.session_ids), **plan.summary(True)})
    return plan


def delete_project(db: Database, paths: Paths, project: Project, *, force: bool = False,
                   events: EventBus | None = None, actor: str | None = None) -> DeletePlan:
    def collect(conn: sqlite3.Connection) -> DeletePlan:
        plan = plan_project(conn, project)
        if plan.active_sids and not force:
            raise conflict(f"The project {project.id} has live leases ({', '.join(plan.active_sids)}). "
                           f"Release them first or delete with force.", error="project_in_use",
                           sids=plan.active_sids)
        return plan

    def remove(conn: sqlite3.Connection, plan: DeletePlan) -> None:
        artifacts_repo.delete_many(conn, plan.artifact_ids)
        for session_id in plan.session_ids:
            events_repo.delete_for_session(conn, session_id)
            sessions_repo.delete(conn, session_id)
        events_repo.delete_for_project(conn, project.id)
        conn.execute("UPDATE leases SET session_id = NULL WHERE project_id = ?", (project.id,))
        projects_repo.delete(conn, project.id)

    plan = _execute(db, paths, collect, remove)
    project_dir = paths.store_dir / project.owner / project.name
    if project_dir.is_dir():
        layout.remove_empty_parents(project_dir, paths.store_dir)
    if events is not None:
        events.publish(ev.PROJECT_DELETED, project_id=project.id, actor=actor, detail={"project": project.id, **plan.summary(True)})
    return plan
