from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.api.errors import bad_request, conflict, forbidden, lease_released, not_found
from eks_harness.auth.core import Principal
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import grants, leases, projects, sessions
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.grants import level_at_least
from eks_harness.db.repos.leases import Lease
from eks_harness.db.repos.projects import Project
from eks_harness.db.repos.sessions import Session
from eks_harness.ids import is_sid, parse_project_id
from eks_harness.store.layout import PROJECT_LEVEL_SLUG


@dataclass(frozen=True)
class Target:
    project: Project
    session: Session | None
    lease: Lease | None = None
    created_project: bool = False
    created_session: bool = False

    @property
    def session_id(self) -> int | None:
        return self.session.id if self.session else None

    @property
    def session_slug(self) -> str | None:
        return self.session.slug if self.session else None

    @property
    def lease_sid(self) -> str | None:
        return self.lease.sid if self.lease else None


def level_for(conn: sqlite3.Connection, principal: Principal, project_id: str, session_id: int | None) -> str | None:
    if principal.is_admin:
        return "admin"
    if session_id is None:
        return grants.project_level(conn, principal.user_id, project_id)
    return grants.session_level(conn, principal.user_id, project_id, session_id)


def require_level(conn: sqlite3.Connection, principal: Principal | None, project_id: str, session_id: int | None,
                  required: str, *, missing_error: str = "project_not_found", missing_message: str | None = None) -> str:
    if principal is None:
        return "admin"
    level = level_for(conn, principal, project_id, session_id)
    if level_at_least(level, required):
        return level
    if level is None:
        raise not_found(missing_message or f"No project {project_id}.", error=missing_error)
    raise forbidden(f"You need {required} access for this.", error="insufficient_grant", required=required,
                    level=level)


def authorize_artifact(conn: sqlite3.Connection, principal: Principal | None, artifact: Artifact,
                       required: str = "viewer") -> str:
    return require_level(conn, principal, artifact.project_id, artifact.session_id, required,
                         missing_error="artifact_not_found", missing_message=f"No artifact {artifact.id}.")


def lease_for_sid(conn: sqlite3.Connection, sid: str) -> Lease:
    value = (sid or "").strip().lower()
    lease = leases.get_by_sid(conn, value) if is_sid(value) else None
    if lease is None:
        raise not_found(f"No lease has the session id {sid}.", error="sid_not_found", sid=sid)
    return lease


def require_holding(lease: Lease) -> None:
    if not lease.holding:
        raise lease_released(lease.project_id, lease.session_name or lease.session_slug, lease.kind, lease.sid,
                             lease.state)


def parse_project(value: str) -> str:
    try:
        owner, name = parse_project_id((value or "").strip().lower())
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_project") from None
    return f"{owner}/{name}"


def resolve_target(conn: sqlite3.Connection, *, project: str | None = None, session: str | None = None,
                   sid: str | None = None, principal: Principal | None = None, required: str = "editor",
                   create: bool = True, require_valid_sid: bool = True) -> Target:
    if sid:
        lease = lease_for_sid(conn, sid)
        if require_valid_sid:
            require_holding(lease)
        if lease.session_id is None or not lease.project_id:
            raise conflict(f"The lease {lease.sid} is not attached to a project session.", error="sid_without_session")
        found_session = sessions.get(conn, lease.session_id)
        found_project = projects.get(conn, lease.project_id) if found_session else None
        if found_session is None or found_project is None:
            raise conflict(f"The session of lease {lease.sid} was deleted.", error="sid_without_session")
        if project and parse_project(project) != found_project.id:
            raise bad_request(f"The sid {lease.sid} belongs to {found_project.id}, not {project}.",
                              error="sid_project_mismatch")
        require_level(conn, principal, found_project.id, found_session.id, required,
                      missing_error="sid_not_found", missing_message=f"No lease has the session id {sid}.")
        return Target(found_project, found_session, lease)
    if not project:
        raise bad_request("Send a sid, or a project (owner/name) with an optional session.", error="no_target")
    project_id = parse_project(project)
    found_project = projects.get(conn, project_id)
    created_project = False
    if found_project is None:
        if not create:
            raise not_found(f"No project {project_id}.", error="project_not_found")
        if principal is not None and not principal.is_admin:
            raise not_found(f"No project {project_id}.", error="project_not_found")
        found_project, created_project = projects.ensure(conn, project_id)
    session_name = (session or "").strip()
    if not session_name or session_name == PROJECT_LEVEL_SLUG:
        require_level(conn, principal, found_project.id, None, required)
        return Target(found_project, None, created_project=created_project)
    found_session = sessions.find_in_project(conn, found_project.id, session_name)
    if found_session is None:
        if not create:
            raise not_found(f"No session {session_name} in {found_project.id}.", error="session_not_found")
        require_level(conn, principal, found_project.id, None, required)
        found_session, created_session = sessions.ensure_in_project(conn, found_project.id, session_name)
        return Target(found_project, found_session, created_project=created_project, created_session=created_session)
    require_level(conn, principal, found_project.id, found_session.id, required,
                  missing_error="session_not_found",
                  missing_message=f"No session {session_name} in {found_project.id}.")
    return Target(found_project, found_session, created_project=created_project)


def visible_artifact(db: Database, principal: Principal | None, artifact_id: str,
                     required: str = "viewer") -> Artifact:
    conn = db.conn()
    artifact = artifacts_repo.get(conn, artifact_id) if artifact_id else None
    if artifact is None:
        raise not_found(f"No artifact {artifact_id}.", error="artifact_not_found")
    authorize_artifact(conn, principal, artifact, required)
    return artifact
