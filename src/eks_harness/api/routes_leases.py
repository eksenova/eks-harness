from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, check_level, current_principal, get_ctx
from eks_harness.api.errors import forbidden, not_found
from eks_harness.api.schemas import (
    InstanceEndedRequest,
    LeaseAcquireRequest,
    LeaseAcquireResponse,
    LeaseBreakRequest,
    LeaseBrowserResponse,
    LeaseHeartbeatResponse,
    LeaseIdleRequest,
    LeaseIdleResponse,
    LeaseList,
    LeaseOut,
    LeaseReleaseRequest,
    LeaseReleaseResponse,
    LeaseResumeRequest,
    ts_to_datetime,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.leases import LeaseManager, manager, normalize_kind
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos.leases import Lease

router = APIRouter(tags=["leases"])


def leases_of(request: Request) -> LeaseManager:
    return manager(get_ctx(request))


def authorize_target(ctx: AppContext, principal: Principal, project_id: str, session_name: str) -> None:
    if principal.is_admin:
        return
    project = projects_repo.get(ctx.db.conn(), project_id)
    if project is None:
        raise forbidden(f"Only an admin can create the project {project_id}.", error="admin_required")
    session = sessions_repo.find_in_project(ctx.db.conn(), project.id, session_name) if session_name else None
    check_level(ctx.db, principal, project.id, session.id if session else None, "editor",
                f"the project {project.id}")


def authorize_lease(ctx: AppContext, principal: Principal, lease: Lease, level: str = "editor") -> None:
    if principal.is_admin:
        return
    if lease.owner_kind == "manual" and lease.owner_user == principal.user_id:
        return
    if not lease.project_id:
        raise not_found(f"No lease with sid {lease.sid}.", error="lease_not_found")
    check_level(ctx.db, principal, lease.project_id, lease.session_id, level, f"the lease {lease.sid}")


def visible(ctx: AppContext, principal: Principal, lease: Lease) -> bool:
    if principal.is_admin:
        return True
    if lease.owner_kind == "manual" and lease.owner_user == principal.user_id:
        return True
    if not lease.project_id:
        return False
    return access_scope(ctx.db, principal).can(lease.project_id, lease.session_id, "viewer")


def actor_of(principal: Principal, instance: str | None = None) -> str:
    if principal.via == "local" and instance:
        return instance
    return principal.username


@router.post("/api/leases/acquire", response_model=LeaseAcquireResponse, response_model_exclude_none=False)
async def acquire(body: LeaseAcquireRequest, request: Request,
                  wait_seconds: float = Query(25.0, alias="waitSeconds", ge=0, le=120),
                  principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    authorize_target(ctx, principal, body.project, body.session)
    payload = body.model_dump()
    return await run_in_threadpool(leases_of(request).acquire, payload, actor_of(principal, body.instance),
                                   body.wait, wait_seconds)


@router.post("/api/leases/resume", response_model=LeaseAcquireResponse)
async def resume(body: LeaseResumeRequest, request: Request,
                 wait_seconds: float = Query(25.0, alias="waitSeconds", ge=0, le=120),
                 principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    old = leases.get_lease(body.sid)
    authorize_lease(ctx, principal, old)
    payload = body.model_dump()
    return await run_in_threadpool(leases.resume, payload, actor_of(principal, body.instance), body.wait,
                                   wait_seconds)


@router.get("/api/leases", response_model=LeaseList)
def list_leases(request: Request, state: list[str] | None = Query(None), kind: str | None = None,
                instance: str | None = None, project: str | None = None, session: str | None = None,
                include_ended: bool = Query(False, alias="includeEnded"),
                limit: int = Query(500, ge=1, le=5000),
                principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    states = tuple(state) if state else (leases_repo.STATES if include_ended else leases_repo.LIVE_STATES)
    filters: dict[str, Any] = {"kind": normalize_kind(kind) if kind else None, "instance": instance}
    if project:
        found = projects_repo.get(ctx.db.conn(), project)
        if found is None:
            return {"items": [], "queue": []}
        if session:
            match = sessions_repo.find_in_project(ctx.db.conn(), found.id, session)
            if match is None:
                return {"items": [], "queue": []}
            filters["session_id"] = match.id
    elif session:
        match = sessions_repo.find(ctx.db.conn(), session)
        if match is None:
            return {"items": [], "queue": []}
        filters["session_id"] = match.id
    rows = leases_repo.list_leases(ctx.db.conn(), states=states, limit=limit, newest_first=include_ended,
                                   **{k: v for k, v in filters.items() if v is not None})
    if project:
        rows = [r for r in rows if r.project_id == project]
    rows = [r for r in rows if visible(ctx, principal, r)]
    items, queue, positions = [], [], {}
    for lease in rows:
        if lease.state == "queued":
            positions[lease.kind] = positions.get(lease.kind, 0) + 1
            queue.append(leases.lease_out(lease, queue_position=positions[lease.kind]))
        else:
            items.append(leases.lease_out(lease))
    return {"items": items, "queue": queue}


@router.post("/api/leases/break", response_model=LeaseReleaseResponse)
async def break_lease(body: LeaseBreakRequest, request: Request,
                      principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    target = body.sid or body.lease
    if not target:
        raise not_found("send sid or lease", error="lease_not_found")
    lease = leases.get_lease(target)
    authorize_lease(ctx, principal, lease)
    ended = await run_in_threadpool(leases.break_lease, lease.sid, body.reason, principal.username)
    return {"released": [e.resource for e in ended if e.resource], "sids": [e.sid for e in ended]}


@router.get("/api/leases/{sid}", response_model=LeaseOut)
def get_lease(sid: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    lease = leases.get_lease(sid)
    if not visible(ctx, principal, lease):
        raise not_found(f"No lease with sid {sid}.", error="lease_not_found")
    return leases.lease_out(lease)


@router.post("/api/leases/{sid}/browser", response_model=LeaseBrowserResponse)
async def ensure_browser(sid: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    authorize_lease(ctx, principal, leases.get_lease(sid))
    return await run_in_threadpool(leases.ensure_browser, sid)


@router.post("/api/leases/{sid}/heartbeat", response_model=LeaseHeartbeatResponse)
async def heartbeat(sid: str, request: Request, body: dict | None = Body(None),
                    principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    authorize_lease(ctx, principal, leases.get_lease(sid))
    meta = (body or {}).get("meta") if isinstance(body, dict) else None
    lease = await run_in_threadpool(leases.heartbeat, sid, meta if isinstance(meta, dict) else None)
    return {"sid": lease.sid, "state": lease.state, "phase": lease.phase,
            "heartbeatAt": ts_to_datetime(lease.heartbeat_at)}


@router.post("/api/leases/{sid}/meta", response_model=LeaseOut)
def update_meta(sid: str, request: Request, body: dict = Body(...),
                principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    authorize_lease(ctx, principal, leases.get_lease(sid))
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else body
    return leases.lease_out(leases.update_meta(sid, meta))


@router.post("/api/leases/{sid}/idle", response_model=LeaseIdleResponse)
def idle(sid: str, request: Request, body: LeaseIdleRequest | None = None,
         principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    authorize_lease(ctx, principal, leases.get_lease(sid))
    lease, grace = leases.idle(sid, body.grace if body else None)
    return {"sid": lease.sid, "grace": grace, "state": lease.state}


@router.post("/api/leases/{sid}/release", response_model=LeaseReleaseResponse)
async def release(sid: str, request: Request, body: LeaseReleaseRequest | None = None,
                  principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    lease = leases.get_lease(sid)
    authorize_lease(ctx, principal, lease)
    ended = await run_in_threadpool(leases.release, sid, body.reason if body else None,
                                    actor_of(principal, lease.owner_instance))
    return {"released": [e.resource for e in ended if e.resource], "sids": [e.sid for e in ended]}


def instance_leases(ctx: AppContext, principal: Principal, instance: str, kind: str | None = None) -> list[Lease]:
    found = leases_repo.list_leases(ctx.db.conn(), states=leases_repo.LIVE_STATES, instance=instance,
                                    kind=normalize_kind(kind) if kind else None)
    for lease in found:
        authorize_lease(ctx, principal, lease)
    return found


@router.post("/api/instances/{instance:path}/ended", response_model=LeaseReleaseResponse)
async def instance_ended(instance: str, request: Request, body: InstanceEndedRequest | None = None,
                         principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    instance_leases(ctx, principal, instance)
    if not principal.is_admin and any(r.get("instance") == instance for r in ctx.pools.host.backends.values()):
        raise forbidden("Only an admin can stop the backends of an instance.", error="admin_required")
    ended = await run_in_threadpool(leases_of(request).instance_ended, instance, body.reason if body else None,
                                    actor_of(principal, instance))
    return {"released": [e.resource for e in ended if e.resource], "sids": [e.sid for e in ended]}


@router.post("/api/instances/{instance:path}/heartbeat")
def instance_heartbeat(instance: str, request: Request, body: dict | None = Body(None),
                       principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    kind = (body or {}).get("kind") if isinstance(body, dict) else None
    instance_leases(ctx, principal, instance, kind)
    return {"instance": instance, "leases": leases_of(request).heartbeat_instance(instance, kind)}


@router.post("/api/instances/{instance:path}/idle")
def instance_idle(instance: str, request: Request, body: LeaseIdleRequest | None = None,
                  principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    instance_leases(ctx, principal, instance)
    sids, grace = leases_of(request).idle_instance(instance, body.grace if body else None)
    return {"instance": instance, "sids": sids, "grace": grace}


@router.post("/api/instances/{instance:path}/release", response_model=LeaseReleaseResponse)
async def instance_release(instance: str, request: Request, body: dict | None = Body(None),
                           principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    data = body if isinstance(body, dict) else {}
    kind = data.get("kind")
    instance_leases(ctx, principal, instance, kind)
    ended = await run_in_threadpool(leases_of(request).release_instance, instance, kind,
                                    data.get("reason") or "requested", actor_of(principal, instance))
    return {"released": [e.resource for e in ended if e.resource], "sids": [e.sid for e in ended]}


@router.get("/api/instances/{instance:path}/leases", response_model=LeaseList)
def instance_list(instance: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = leases_of(request)
    rows = [r for r in leases_repo.list_leases(ctx.db.conn(), states=leases_repo.LIVE_STATES, instance=instance)
            if visible(ctx, principal, r)]
    return {"items": [leases.lease_out(r) for r in rows if r.state != "queued"],
            "queue": [leases.lease_out(r) for r in rows if r.state == "queued"]}
