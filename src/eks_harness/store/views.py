from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from eks_harness import project_settings
from eks_harness.api.links import Links
from eks_harness.api.schemas import (
    LeaseBrief,
    LeaseOut,
    LeaseUrls,
    ProjectBrief,
    ProjectOut,
    SessionBrief,
    SessionOut,
    SessionProjectOut,
    SidInfo,
    ts_to_datetime,
)
from eks_harness.db.common import now, placeholders
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos.grants import AccessScope
from eks_harness.db.repos.leases import Lease
from eks_harness.db.repos.projects import Project, ProjectStats
from eks_harness.db.repos.sessions import Session, SessionStats
from eks_harness.store import policy


def project_brief(links: Links, project: Project) -> ProjectBrief:
    return ProjectBrief(id=project.id, owner=project.owner, name=project.name, title=project.title,
                        url=links.project(project.id))


def session_url(links: Links, session: Session, project_id: str | None) -> str:
    return links.session(project_id, session.slug) if project_id else links.shared_session(session.slug)


def session_brief(links: Links, session: Session, project_id: str | None = None) -> SessionBrief:
    return SessionBrief(id=session.id, project_id=project_id, name=session.name, slug=session.slug,
                        url=session_url(links, session, project_id), shared_url=links.shared_session(session.slug))


def _idle_seconds(lease: Lease, at: float) -> float | None:
    if not lease.holding:
        return None
    reference = lease.heartbeat_at or lease.acquired_at
    return round(max(0.0, at - reference), 1) if reference else None


def _usernames(conn: sqlite3.Connection, user_ids: Sequence[int | None]) -> dict[int, str]:
    ids = sorted({u for u in user_ids if u is not None})
    if not ids:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT id, username FROM users WHERE id IN ({placeholders(len(ids))})", ids)}


def lease_brief(lease: Lease, owner_username: str | None = None, at: float | None = None) -> LeaseBrief:
    stamp = at or now()
    return LeaseBrief(id=lease.id, sid=lease.sid, kind=lease.kind, resource=lease.resource, state=lease.state,
                      phase=lease.phase, owner_instance=lease.owner_instance, owner_kind=lease.owner_kind,
                      owner_user=lease.owner_user, owner_username=owner_username,
                      acquired_at=ts_to_datetime(lease.acquired_at), heartbeat_at=ts_to_datetime(lease.heartbeat_at),
                      idle_seconds=_idle_seconds(lease, stamp))


def lease_briefs(conn: sqlite3.Connection, leases: Sequence[Lease]) -> list[LeaseBrief]:
    names = _usernames(conn, [l.owner_user for l in leases])
    stamp = now()
    return [lease_brief(l, names.get(l.owner_user), stamp) for l in leases]


def reacquire_command(lease: Lease) -> str:
    project = lease.project_id or "<owner/name>"
    session = lease.session_name or lease.session_slug or "<session>"
    quoted = f'"{session}"' if any(c.isspace() for c in session) else session
    return f"eks-harness lease acquire --project {project} --session {quoted} --kind {lease.kind}"


def lease_urls(links: Links, lease: Lease) -> LeaseUrls | None:
    if not lease.project_id:
        return None
    return LeaseUrls(session=links.session(lease.project_id, lease.session_slug), ui=links.ui(),
                     project=links.project(lease.project_id))


def sid_info(conn: sqlite3.Connection, links: Links, lease: Lease, lease_data: dict) -> SidInfo:
    project = projects_repo.get(conn, lease.project_id) if lease.project_id else None
    session = sessions_repo.get(conn, lease.session_id) if lease.session_id is not None else None
    return SidInfo(sid=lease.sid, valid=lease.holding, lease=LeaseOut.model_validate(lease_data),
                   project=project_brief(links, project) if project else None,
                   session=session_brief(links, session, lease.project_id) if session else None,
                   urls=lease_urls(links, lease), reacquire=None if lease.holding else reacquire_command(lease))


def project_out(links: Links, project: Project, stats: ProjectStats | None, *, access: str | None,
                active_leases: int = 0, default_retention_days: int | None = None) -> ProjectOut:
    stats = stats or ProjectStats()
    return ProjectOut(
        id=project.id, owner=project.owner, name=project.name, title=project.title, description=project.description,
        implicit=project.implicit, retention_days=project.retention_days,
        default_retention_days=default_retention_days,
        effective_retention_days=policy.project_days(project.retention_days, default_retention_days),
        retention_source=policy.project_source(project.retention_days),
        created_at=ts_to_datetime(project.created_at), updated_at=ts_to_datetime(project.updated_at),
        session_count=stats.session_count, artifact_count=stats.artifact_count, unseen_count=stats.unseen_count,
        size_bytes=stats.size_bytes, last_activity_at=ts_to_datetime(stats.last_activity_at),
        project_file_count=stats.project_file_count, active_leases=active_leases, access=access,
        settings=project_settings.effective(project.settings), url=links.project(project.id))


def active_lease_counts(conn: sqlite3.Connection, project_ids: Sequence[str]) -> dict[str, int]:
    ids = list(dict.fromkeys(project_ids))
    if not ids:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        "SELECT l.project_id, COUNT(*) FROM leases l "
        f"WHERE l.state IN ('active', 'idle') AND l.project_id IN ({placeholders(len(ids))}) GROUP BY l.project_id",
        ids)}


def projects_for(conn: sqlite3.Connection, links: Links, scope: AccessScope, user_id: int,
                 items: Sequence[Project], default_retention_days: int | None = None) -> list[ProjectOut]:
    ids = [p.id for p in items]
    artifact_scope = None if scope.everything else scope.sql("a.project_id", "a.session_id")
    stats = projects_repo.stats_all(conn, user_id, ids, artifact_scope)
    leases = active_lease_counts(conn, ids)
    result = []
    for project in items:
        project_stats = stats.get(project.id, ProjectStats())
        if not scope.everything and project.id not in scope.projects:
            granted = sum(1 for p in scope.session_projects.values() if p == project.id)
            project_stats = ProjectStats(session_count=granted, artifact_count=project_stats.artifact_count,
                                         unseen_count=project_stats.unseen_count, size_bytes=project_stats.size_bytes,
                                         last_activity_at=project_stats.last_activity_at)
        result.append(project_out(links, project, project_stats, access=scope.level_for(project.id) or "viewer",
                                  active_leases=leases.get(project.id, 0),
                                  default_retention_days=default_retention_days))
    return result


def session_projects(conn: sqlite3.Connection, links: Links, session_ids: Sequence[int], user_id: int | None,
                     scope: AccessScope) -> dict[int, list[SessionProjectOut]]:
    ids = list(dict.fromkeys(session_ids))
    result: dict[int, list[SessionProjectOut]] = {sid: [] for sid in ids}
    if not ids:
        return result
    linked = conn.execute(
        f"SELECT session_id, project_id, last_active_at FROM session_projects WHERE session_id IN "
        f"({placeholders(len(ids))}) ORDER BY project_id", ids).fetchall()
    unseen, unseen_params = "0", []
    if user_id is not None:
        unseen = ("SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM seen x WHERE x.artifact_id = a.id AND x.user_id = ?) "
                  "THEN 1 ELSE 0 END)")
        unseen_params = [user_id]
    counts = {(r[0], r[1]): r for r in conn.execute(
        f"SELECT a.session_id, a.project_id, COUNT(*), COALESCE(SUM(a.size), 0), {unseen} FROM artifacts a "
        f"WHERE a.session_id IN ({placeholders(len(ids))}) GROUP BY a.session_id, a.project_id",
        [*unseen_params, *ids])}
    leases = {(r[0], r[1]): r[2] for r in conn.execute(
        f"SELECT session_id, project_id, COUNT(*) FROM leases WHERE state IN ('active', 'idle') AND session_id IN "
        f"({placeholders(len(ids))}) GROUP BY session_id, project_id", ids)}
    projects = {p.id: p for p in (projects_repo.get(conn, pid) for pid in {r[1] for r in linked}) if p}
    slugs = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, slug FROM sessions WHERE id IN ({placeholders(len(ids))})", ids)}
    for session_id, project_id, last_active in linked:
        project = projects.get(project_id)
        if project is None or not scope.can(project_id, session_id, "viewer"):
            continue
        row = counts.get((session_id, project_id))
        result[session_id].append(SessionProjectOut(
            project=project_brief(links, project), artifact_count=row[2] if row else 0,
            size_bytes=row[3] if row else 0, unseen_count=(row[4] or 0) if row else 0,
            active_leases=leases.get((session_id, project_id), 0), last_active_at=ts_to_datetime(last_active),
            url=links.session(project_id, slugs[session_id])))
    return result


def session_out(conn: sqlite3.Connection, links: Links, session: Session, stats: SessionStats | None, *,
                access: str | None, leases: Sequence[Lease] | None = None, project_id: str | None = None,
                projects: Sequence[SessionProjectOut] | None = None) -> SessionOut:
    stats = stats or SessionStats()
    if leases is not None:
        holding = list(leases)
    else:
        holding = leases_repo.for_session(conn, session.id, leases_repo.HOLDING_STATES)
        if project_id is not None:
            holding = [l for l in holding if l.project_id in (None, project_id)]
    project_list = list(projects or [])
    return SessionOut(
        id=session.id, project_id=project_id, project_ids=[p.project.id for p in project_list],
        projects=project_list, name=session.name, slug=session.slug,
        created_at=ts_to_datetime(session.created_at), last_active_at=ts_to_datetime(session.last_active_at),
        artifact_count=stats.artifact_count, unseen_count=stats.unseen_count, size_bytes=stats.size_bytes,
        note_count=stats.note_count, active_leases=lease_briefs(conn, holding), access=access,
        url=session_url(links, session, project_id), shared_url=links.shared_session(session.slug))
