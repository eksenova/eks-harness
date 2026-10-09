from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request

from eks_harness.api.deps import (
    ProjectAccess,
    SessionAccess,
    SharedSessionAccess,
    authorize_shared_session,
    current_principal,
    get_ctx,
    get_scope,
    require_project,
    require_session,
    require_shared_session,
)
from eks_harness.api.errors import bad_request, forbidden, not_found
from eks_harness.api.schemas import (
    DeleteSummary,
    NoteCreate,
    NoteOut,
    SeenResponse,
    SessionList,
    SessionOut,
    SessionUpdate,
    SidInfo,
    TimelineResponse,
    datetime_to_ts,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon import events as ev
from eks_harness.daemon.leases import manager as lease_manager
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos.grants import AccessScope, best_level, level_at_least
from eks_harness.db.repos.sessions import Session
from eks_harness.store import access as store_access
from eks_harness.store import deletion, timeline, views
from eks_harness.store import notes as store_notes
from eks_harness.store import seen as store_seen

router = APIRouter(tags=["sessions"])
SESSION_PATH = "/api/projects/{owner}/{name}/sessions/{slug}"
SHARED_PATH = "/api/sessions/{slug}"


def _artifact_scope(scope: AccessScope) -> tuple[str, list] | None:
    return None if scope.everything else scope.sql("a.project_id", "a.session_id")


def _session_out(request: Request, session: Session, principal: Principal, level: str | None,
                 project_id: str | None, scope: AccessScope) -> SessionOut:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    stats = sessions_repo.stats(conn, session.id, principal.user_id, project_id,
                                None if project_id else _artifact_scope(scope))
    projects = views.session_projects(conn, ctx.links, [session.id], principal.user_id, scope)[session.id]
    return views.session_out(conn, ctx.links, session, stats, access=level, project_id=project_id,
                             projects=projects)


def _rename(request: Request, session: Session, body: SessionUpdate, actor: str,
            project_id: str | None) -> Session:
    ctx = get_ctx(request)
    with ctx.db.transaction() as conn:
        updated = sessions_repo.rename(conn, session.id, body.name or session.name, body.slug)
    ctx.events.publish(ev.SESSION_UPDATED, session_id=updated.id, project_id=project_id, actor=actor,
                       detail={"sessionId": updated.id, "name": updated.name, "slug": updated.slug,
                               "previousSlug": session.slug, "previousName": session.name})
    return updated


def _ts(value: datetime | None) -> float | None:
    return datetime_to_ts(value) if value is not None else None


def _timeline(request: Request, session: Session, principal: Principal, level: str, scope: AccessScope,
              project_id: str | None, since: datetime | None, until: datetime | None, limit: int,
              types: str | None) -> TimelineResponse:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    wanted = [t.strip() for t in types.split(",") if t.strip()] if types else None
    items = timeline.session_timeline(conn, ctx.links, ctx.paths, session, user_id=principal.user_id,
                                      since=_ts(since), until=_ts(until), limit=limit, types=wanted,
                                      project_id=project_id,
                                      artifact_scope=None if project_id else _artifact_scope(scope))
    return TimelineResponse(session=_session_out(request, session, principal, level, project_id, scope),
                            items=items)


def _add_note(request: Request, session: Session, body: NoteCreate, principal: Principal,
              project_id: str | None) -> NoteOut:
    ctx = get_ctx(request)
    lease_sid = None
    if body.sid:
        lease = store_access.lease_for_sid(ctx.db.conn(), body.sid)
        if lease.session_id != session.id:
            raise bad_request(f"The sid {lease.sid} does not belong to this session.", error="sid_session_mismatch")
        lease_sid = lease.sid
        project_id = project_id or lease.project_id
    note = store_notes.add_note(ctx.db, session, body.body, principal.username, lease_sid, ctx.events,
                                project_id=project_id)
    return store_notes.note_out(note)


@router.get("/api/projects/{owner}/{name}/sessions", response_model=SessionList)
def list_sessions(request: Request, access: ProjectAccess = Depends(require_project("viewer")),
                  scope: AccessScope = Depends(get_scope)) -> SessionList:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    session_ids = None
    if access.limited:
        session_ids = [sid for sid, pid in scope.session_projects.items() if pid == access.project.id]
    items = sessions_repo.list_for_project(conn, access.project.id, session_ids)
    ids = [s.id for s in items]
    stats = sessions_repo.stats_for_project(conn, access.project.id, access.principal.user_id, ids)
    projects = views.session_projects(conn, ctx.links, ids, access.principal.user_id, scope)
    return SessionList(items=[
        views.session_out(conn, ctx.links, s, stats.get(s.id), access=scope.level_for(access.project.id, s.id),
                          project_id=access.project.id, projects=projects.get(s.id))
        for s in items])


@router.get(SESSION_PATH, response_model=SessionOut)
def get_session(request: Request, access: SessionAccess = Depends(require_session("viewer")),
                scope: AccessScope = Depends(get_scope)) -> SessionOut:
    return _session_out(request, access.session, access.principal, access.level, access.project.id, scope)


@router.patch(SESSION_PATH, response_model=SessionOut)
def update_session(body: SessionUpdate, request: Request, access: SessionAccess = Depends(require_session("editor")),
                   scope: AccessScope = Depends(get_scope)) -> SessionOut:
    session = access.session
    if body.name is not None or body.slug is not None:
        shared = _shared_access(request, access.principal, session, scope)
        _check_editor_everywhere(request, shared)
        session = _rename(request, session, body, access.principal.username, access.project.id)
    return _session_out(request, session, access.principal, access.level, access.project.id, scope)


@router.delete(SESSION_PATH, response_model=DeleteSummary)
def delete_session(request: Request, access: SessionAccess = Depends(require_session("editor")),
                   dry_run: bool = Query(False, alias="dryRun"), force: bool = Query(False)) -> DeleteSummary:
    ctx = get_ctx(request)
    if dry_run:
        plan = deletion.plan_session(ctx.db.conn(), access.session, access.project.id)
        return DeleteSummary(**plan.summary(False))
    plan = deletion.delete_session(ctx.db, ctx.paths, access.session, project_id=access.project.id, force=force,
                                   events=ctx.events, actor=access.principal.username)
    return DeleteSummary(**plan.summary(True))


@router.get(SESSION_PATH + "/timeline", response_model=TimelineResponse)
def session_timeline(request: Request, access: SessionAccess = Depends(require_session("viewer")),
                     scope: AccessScope = Depends(get_scope),
                     since: datetime | None = Query(None), until: datetime | None = Query(None),
                     limit: int = Query(500, ge=1, le=5000), types: str | None = Query(None)) -> TimelineResponse:
    return _timeline(request, access.session, access.principal, access.level, scope, access.project.id, since,
                     until, limit, types)


@router.get(SESSION_PATH + "/notes", response_model=list[NoteOut])
def list_notes(request: Request, access: SessionAccess = Depends(require_session("viewer"))) -> list[NoteOut]:
    return store_notes.session_notes(get_ctx(request).db, access.session)


@router.post(SESSION_PATH + "/notes", response_model=NoteOut, status_code=201)
def create_note(body: NoteCreate, request: Request,
                access: SessionAccess = Depends(require_session("editor"))) -> NoteOut:
    return _add_note(request, access.session, body, access.principal, access.project.id)


@router.post(SESSION_PATH + "/seen", response_model=SeenResponse)
def mark_session_seen(request: Request, access: SessionAccess = Depends(require_session("viewer")),
                      seen: bool = Query(True)) -> SeenResponse:
    ctx = get_ctx(request)
    updated = store_seen.set_session_seen(ctx.db, access.principal.user_id, access.session.id, seen,
                                          project_id=access.project.id)
    return SeenResponse(updated=updated, seen=seen)


def _shared_access(request: Request, principal: Principal, session: Session,
                   scope: AccessScope) -> SharedSessionAccess:
    return authorize_shared_session(get_ctx(request).db, principal, session, "viewer", scope)


def _check_editor_everywhere(request: Request, access: SharedSessionAccess) -> None:
    linked = sessions_repo.project_ids(get_ctx(request).db.conn(), access.session.id)
    blocked = [p for p in linked if not level_at_least(access.project_levels.get(p), "editor")]
    if blocked:
        raise forbidden(f"The session {access.session.name} is shared with projects you cannot edit "
                        f"({', '.join(blocked)}).", error="insufficient_grant", required="editor")


@router.get("/api/sessions", response_model=SessionList)
def list_shared_sessions(request: Request, principal: Principal = Depends(current_principal),
                         scope: AccessScope = Depends(get_scope), project: str | None = None,
                         q: str | None = Query(None, max_length=200),
                         limit: int = Query(500, ge=1, le=5000)) -> SessionList:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    project_id = store_access.parse_project(project) if project else None
    items = sessions_repo.list_sessions(conn, project_id=project_id)
    ids = [s.id for s in items]
    projects = views.session_projects(conn, ctx.links, ids, principal.user_id, scope)
    items = [s for s in items if projects.get(s.id)]
    if q:
        needle = q.strip().lower()
        items = [s for s in items if needle in s.name.lower() or needle in s.slug]
    items = items[:limit]
    stats = sessions_repo.stats_many(conn, [s.id for s in items], principal.user_id, None, _artifact_scope(scope))
    out = []
    for s in items:
        best = best_level(*(scope.level_for(p.project.id, s.id) for p in projects[s.id]))
        out.append(views.session_out(conn, ctx.links, s, stats.get(s.id), access=best, projects=projects[s.id]))
    return SessionList(items=out)


@router.get(SHARED_PATH, response_model=SessionOut)
def get_shared_session(request: Request, access: SharedSessionAccess = Depends(require_shared_session("viewer")),
                       scope: AccessScope = Depends(get_scope)) -> SessionOut:
    return _session_out(request, access.session, access.principal, access.level, None, scope)


@router.patch(SHARED_PATH, response_model=SessionOut)
def update_shared_session(body: SessionUpdate, request: Request,
                          access: SharedSessionAccess = Depends(require_shared_session("editor")),
                          scope: AccessScope = Depends(get_scope)) -> SessionOut:
    session = access.session
    if body.name is not None or body.slug is not None:
        _check_editor_everywhere(request, access)
        session = _rename(request, session, body, access.principal.username, None)
    return _session_out(request, session, access.principal, access.level, None, scope)


@router.delete(SHARED_PATH, response_model=DeleteSummary)
def delete_shared_session(request: Request,
                          access: SharedSessionAccess = Depends(require_shared_session("editor")),
                          dry_run: bool = Query(False, alias="dryRun"), force: bool = Query(False)) -> DeleteSummary:
    ctx = get_ctx(request)
    _check_editor_everywhere(request, access)
    if dry_run:
        return DeleteSummary(**deletion.plan_session(ctx.db.conn(), access.session).summary(False))
    plan = deletion.delete_session(ctx.db, ctx.paths, access.session, force=force, events=ctx.events,
                                   actor=access.principal.username)
    return DeleteSummary(**plan.summary(True))


@router.get(SHARED_PATH + "/timeline", response_model=TimelineResponse)
def shared_session_timeline(request: Request,
                            access: SharedSessionAccess = Depends(require_shared_session("viewer")),
                            scope: AccessScope = Depends(get_scope),
                            since: datetime | None = Query(None), until: datetime | None = Query(None),
                            limit: int = Query(500, ge=1, le=5000),
                            types: str | None = Query(None)) -> TimelineResponse:
    return _timeline(request, access.session, access.principal, access.level, scope, None, since, until, limit,
                     types)


@router.get(SHARED_PATH + "/notes", response_model=list[NoteOut])
def list_shared_notes(request: Request,
                      access: SharedSessionAccess = Depends(require_shared_session("viewer"))) -> list[NoteOut]:
    return store_notes.session_notes(get_ctx(request).db, access.session)


@router.post(SHARED_PATH + "/notes", response_model=NoteOut, status_code=201)
def create_shared_note(body: NoteCreate, request: Request,
                       access: SharedSessionAccess = Depends(require_shared_session("editor"))) -> NoteOut:
    return _add_note(request, access.session, body, access.principal, None)


@router.post(SHARED_PATH + "/seen", response_model=SeenResponse)
def mark_shared_session_seen(request: Request,
                             access: SharedSessionAccess = Depends(require_shared_session("viewer")),
                             scope: AccessScope = Depends(get_scope), seen: bool = Query(True)) -> SeenResponse:
    ctx = get_ctx(request)
    visible = [p for p, lvl in access.project_levels.items() if level_at_least(lvl, "viewer")]
    updated = sum(store_seen.set_session_seen(ctx.db, access.principal.user_id, access.session.id, seen,
                                              project_id=p) for p in visible)
    return SeenResponse(updated=updated, seen=seen)


@router.get("/api/sid/{sid}", response_model=SidInfo, tags=["sessions"])
def sid_info(sid: str, request: Request, principal: Principal = Depends(current_principal)) -> SidInfo:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    lease = store_access.lease_for_sid(conn, sid)
    if lease.project_id is not None:
        store_access.require_level(conn, principal, lease.project_id, lease.session_id, "viewer",
                                   missing_error="sid_not_found",
                                   missing_message=f"No lease has the session id {sid}.")
    elif not principal.is_admin and lease.owner_user != principal.user_id:
        raise not_found(f"No lease has the session id {sid}.", error="sid_not_found")
    return views.sid_info(conn, ctx.links, lease, lease_manager(ctx).lease_out(lease))


@router.post("/api/sid/{sid}/notes", response_model=NoteOut, status_code=201, tags=["sessions"])
def create_sid_note(sid: str, body: NoteCreate, request: Request,
                    principal: Principal = Depends(current_principal)) -> NoteOut:
    ctx = get_ctx(request)
    with ctx.db.transaction() as conn:
        target = store_access.resolve_target(conn, sid=sid, principal=principal, required="editor",
                                             require_valid_sid=False)
    note = store_notes.add_note(ctx.db, target.session, body.body, principal.username, target.lease_sid, ctx.events,
                                project_id=target.project.id)
    return store_notes.note_out(note)
