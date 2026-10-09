from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from eks_harness.api.errors import forbidden, not_found, unauthorized
from eks_harness.api.links import Links
from eks_harness.auth import core as auth_core
from eks_harness.auth import middleware as auth_middleware
from eks_harness.auth.core import Principal
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.events import Event, EventBus, EventFilter
from eks_harness.db import Database
from eks_harness.db.repos import grants, projects, sessions
from eks_harness.db.repos.grants import AccessScope, level_at_least
from eks_harness.db.repos.projects import Project
from eks_harness.db.repos.sessions import Session
from eks_harness.pools import Pools

LEVELS = ("viewer", "editor")


def get_ctx(connection: HTTPConnection) -> AppContext:
    return connection.app.state.ctx


def get_db(connection: HTTPConnection) -> Database:
    return get_ctx(connection).db


def get_config(connection: HTTPConnection) -> Config:
    return get_ctx(connection).config


def get_events(connection: HTTPConnection) -> EventBus:
    return get_ctx(connection).events


def get_pools(connection: HTTPConnection) -> Pools:
    return get_ctx(connection).pools


def get_links(connection: HTTPConnection) -> Links:
    return get_ctx(connection).links


def resolve_principal(connection: HTTPConnection) -> Principal | None:
    cached = getattr(connection.state, "principal", None)
    if cached is not None:
        return cached
    ctx = get_ctx(connection)
    principal = auth_middleware.authenticate(ctx.db, ctx.config, connection)
    if principal is not None:
        auth_core.check_csrf(connection, principal)
        connection.state.principal = principal
    return principal


def optional_principal(request: Request) -> Principal | None:
    return resolve_principal(request)


def current_principal(request: Request) -> Principal:
    principal = resolve_principal(request)
    if principal is None:
        raise unauthorized()
    return principal


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_admin:
        raise forbidden("Only an admin can do this.", error="admin_required")
    return principal


def access_scope(db: Database, principal: Principal) -> AccessScope:
    return grants.scope_for_user(db.conn(), principal.user_id, principal.role)


def get_scope(request: Request, principal: Principal = Depends(current_principal)) -> AccessScope:
    cached = getattr(request.state, "access_scope", None)
    if cached is None:
        cached = access_scope(get_db(request), principal)
        request.state.access_scope = cached
    return cached


def project_level(db: Database, principal: Principal, project_id: str) -> str | None:
    if principal.is_admin:
        return "admin"
    return grants.project_level(db.conn(), principal.user_id, project_id)


def session_level(db: Database, principal: Principal, project_id: str, session_id: int | None) -> str | None:
    if principal.is_admin:
        return "admin"
    if session_id is None:
        return grants.project_level(db.conn(), principal.user_id, project_id)
    return grants.session_level(db.conn(), principal.user_id, project_id, session_id)


def check_level(db: Database, principal: Principal, project_id: str, session_id: int | None,
                required: str, what: str = "this") -> str:
    level = session_level(db, principal, project_id, session_id)
    if level_at_least(level, required):
        return level
    if level is None and not grants.has_any_in_project(db.conn(), principal.user_id, project_id):
        raise not_found(f"No project {project_id}.", error="project_not_found")
    raise forbidden(f"You need {required} access to {what}.", error="insufficient_grant", required=required,
                    level=level)


@dataclass(frozen=True)
class ProjectAccess:
    project: Project
    principal: Principal
    level: str
    limited: bool

    @property
    def project_id(self) -> str:
        return self.project.id


@dataclass(frozen=True)
class SessionAccess:
    project: Project
    session: Session
    principal: Principal
    level: str


def load_project(db: Database, owner: str, name: str) -> Project:
    project = projects.get(db.conn(), f"{owner}/{name}")
    if project is None:
        raise not_found(f"No project {owner}/{name}.", error="project_not_found")
    return project


def load_session(db: Database, project: Project, slug: str) -> Session:
    session = sessions.find_in_project(db.conn(), project.id, slug)
    if session is None:
        raise not_found(f"No session {slug} in {project.id}.", error="session_not_found")
    return session


def authorize_project(db: Database, principal: Principal, project: Project, level: str) -> ProjectAccess:
    have = project_level(db, principal, project.id)
    if level_at_least(have, level):
        return ProjectAccess(project, principal, have, limited=False)
    if not principal.is_admin and grants.has_any_in_project(db.conn(), principal.user_id, project.id):
        if level == "viewer":
            return ProjectAccess(project, principal, "viewer", limited=True)
        raise forbidden(f"You need {level} access to the project {project.id}.", error="insufficient_grant",
                        required=level, level=have)
    raise not_found(f"No project {project.id}.", error="project_not_found")


def authorize_session(db: Database, principal: Principal, project: Project, session: Session,
                      level: str) -> SessionAccess:
    have = session_level(db, principal, project.id, session.id)
    if level_at_least(have, level):
        return SessionAccess(project, session, principal, have)
    if have is None:
        if grants.has_any_in_project(db.conn(), principal.user_id, project.id):
            raise not_found(f"No session {session.slug} in {project.id}.", error="session_not_found")
        raise not_found(f"No project {project.id}.", error="project_not_found")
    raise forbidden(f"You need {level} access to the session {session.name}.", error="insufficient_grant",
                    required=level, level=have)


@dataclass(frozen=True)
class SharedSessionAccess:
    session: Session
    principal: Principal
    level: str
    project_levels: dict[str, str]


def authorize_shared_session(db: Database, principal: Principal, session: Session, level: str,
                             scope: AccessScope | None = None) -> SharedSessionAccess:
    scope = scope or access_scope(db, principal)
    linked = sessions.project_ids(db.conn(), session.id)
    levels = {p: lvl for p in linked if (lvl := scope.level_for(p, session.id))}
    best = grants.best_level(*levels.values())
    if best is None:
        raise not_found(f"No session {session.slug}.", error="session_not_found")
    if not level_at_least(best, level):
        raise forbidden(f"You need {level} access to the session {session.name}.", error="insufficient_grant",
                        required=level, level=best)
    return SharedSessionAccess(session, principal, best, levels)


def require_shared_session(level: str = "viewer") -> Callable[..., SharedSessionAccess]:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")

    def dependency(slug: str, request: Request,
                   principal: Principal = Depends(current_principal)) -> SharedSessionAccess:
        db = get_db(request)
        session = sessions.find(db.conn(), slug)
        if session is None:
            raise not_found(f"No session {slug}.", error="session_not_found")
        return authorize_shared_session(db, principal, session, level)

    dependency.__name__ = f"require_shared_session_{level}"
    return dependency


def require_project(level: str = "viewer") -> Callable[..., ProjectAccess]:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")

    def dependency(owner: str, name: str, request: Request,
                   principal: Principal = Depends(current_principal)) -> ProjectAccess:
        db = get_db(request)
        return authorize_project(db, principal, load_project(db, owner, name), level)

    dependency.__name__ = f"require_project_{level}"
    return dependency


def require_session(level: str = "viewer") -> Callable[..., SessionAccess]:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")

    def dependency(owner: str, name: str, slug: str, request: Request,
                   principal: Principal = Depends(current_principal)) -> SessionAccess:
        db = get_db(request)
        project = load_project(db, owner, name)
        return authorize_session(db, principal, project, load_session(db, project, slug), level)

    dependency.__name__ = f"require_session_{level}"
    return dependency


def event_visible(scope: AccessScope, event: Event) -> bool:
    if scope.everything:
        return True
    if event.session_id is not None and event.session_id in scope.sessions:
        return True
    if event.project_id is not None:
        return event.project_id in scope.projects
    return event.session_id is None


def event_filter_for(db: Database, principal: Principal, extra: EventFilter | None = None) -> EventFilter:
    scope = access_scope(db, principal)

    def accept(event: Event) -> bool:
        if not event_visible(scope, event):
            return False
        return extra(event) if extra is not None else True

    return accept


def admin_or_self(principal: Principal, user_id: int) -> None:
    if not principal.is_admin and principal.user_id != user_id:
        raise forbidden("You can only do this for your own account.", error="admin_required")
