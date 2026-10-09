from __future__ import annotations

from dataclasses import dataclass

from eks_harness.auth.users import AccountError
from eks_harness.db import Database
from eks_harness.db.repos import grants, projects, sessions, users
from eks_harness.ids import parse_project_id


@dataclass(frozen=True)
class GrantTarget:
    project_id: str
    session_id: int | None
    created_project: bool = False


def resolve_grant_target(db: Database, project: str, session: str | None, create_project: bool) -> GrantTarget:
    try:
        parse_project_id((project or "").strip())
    except ValueError as error:
        raise AccountError(400, "invalid_project", str(error)) from error
    project_id = project.strip()
    with db.transaction() as conn:
        existing = projects.get(conn, project_id)
        created = False
        if existing is None:
            if not create_project or session:
                raise AccountError(404, "project_not_found", f"No project {project_id}.")
            projects.ensure(conn, project_id)
            created = True
        if session:
            found = sessions.find_in_project(conn, project_id, session.strip())
            if found is None:
                raise AccountError(404, "session_not_found", f"No session {session} in {project_id}.")
            return GrantTarget(project_id, found.id, created)
    return GrantTarget(project_id, None, created)


def add_grant(db: Database, user: users.User, project: str, level: str, session: str | None = None,
              create_project: bool = True) -> grants.Grant:
    if user.builtin:
        raise AccountError(400, "builtin_user", "The built-in local user already has full access.")
    if user.is_admin:
        raise AccountError(400, "admin_user", f"{user.username} is an admin and already has full access.")
    if level not in grants.LEVELS:
        raise AccountError(400, "invalid_level", f"level must be one of {', '.join(grants.LEVELS)}")
    target = resolve_grant_target(db, project, session, create_project)
    with db.transaction() as conn:
        return grants.create(conn, user.id, target.project_id, level, target.session_id)
