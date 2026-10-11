from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, current_principal, get_ctx
from eks_harness.api.errors import bad_request, forbidden, not_found
from eks_harness.api.routes_backends import backend_out, may_see_secrets
from eks_harness.api.routes_leases import authorize_target
from eks_harness.api.schemas import (
    BackendInfoOut,
    BackendLeaseAcquireRequest,
    BackendLeaseList,
    BackendLeaseReleaseRequest,
    BackendLeaseRenewRequest,
    BackendLeaseResponse,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.pools.base import BackendError, BackendLeaseGone

router = APIRouter(tags=["backends"])

WAIT_LIMIT = 120.0


def lease_or_404(ctx: AppContext, lease_id: str) -> tuple[dict, dict]:
    found = ctx.pools.backends.find_lease(lease_id)
    if found is None:
        raise not_found(f"No backend lease {lease_id}; it was released or expired. Acquire a new one with "
                        f"eks-harness backend acquire.", error="backend_lease_not_found", lease=lease_id)
    return found


def may_hold(ctx: AppContext, principal: Principal, lease: dict) -> bool:
    if principal.is_admin:
        return True
    if lease.get("userId") is not None and lease.get("userId") == principal.user_id:
        return True
    return bool(lease.get("project")) and access_scope(ctx.db, principal).can(lease["project"], None, "editor")


def authorize_holder(ctx: AppContext, principal: Principal, lease: dict) -> None:
    if not may_hold(ctx, principal, lease):
        raise forbidden("Only the holder of this backend lease, an editor of its project or an admin can use it.",
                        error="not_the_holder")


def respond(ctx: AppContext, principal: Principal, response: dict, secrets: bool = False) -> dict:
    out = dict(response)
    if out.get("backend") is not None:
        out["backend"] = backend_out(out["backend"], ctx, principal)
    lease = out.get("lease")
    if lease and out.get("backend"):
        out["info"] = ctx.pools.backends.info(lease["backendId"], secrets=secrets)
    return out


@router.post("/api/backend-leases", response_model=BackendLeaseResponse, response_model_exclude_none=False)
async def acquire(body: BackendLeaseAcquireRequest, request: Request,
                  wait_seconds: float = Query(25.0, alias="waitSeconds", ge=0, le=WAIT_LIMIT),
                  secrets: bool = Query(False),
                  principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    if not principal.is_admin:
        if not body.project or not body.session:
            raise forbidden("Name the project and session the backend lease is for (or ask an admin).",
                            error="admin_required")
        authorize_target(ctx, principal, body.project, body.session)
    payload = {"definition": body.definition, "tree": body.tree, "key": body.key, "params": body.params,
               "holder": body.holder or body.instance or principal.username, "owner": body.instance,
               "project": body.project, "session": body.session, "ttl": body.ttl, "idleTimeout": body.idle_timeout,
               "restart": body.restart, "kind": "agent", "userId": principal.user_id}
    try:
        response = await run_in_threadpool(ctx.pools.backends.acquire, payload)
    except BackendError as error:
        raise bad_request(str(error), error="backend_error") from error
    lease = response.get("lease") or {}
    if body.wait and response["status"] == "starting" and lease:
        await run_in_threadpool(ctx.pools.backends.wait_ready, lease["backendId"], wait_seconds)
        response = ctx.pools.backends.lease_response(lease["backendId"], lease["id"])
    return respond(ctx, principal, response, secrets)


@router.get("/api/backend-leases", response_model=BackendLeaseList)
def list_leases(request: Request, backend: str | None = Query(None), instance: str | None = Query(None),
                principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    items = ctx.pools.backends.leases(backend_id=backend, owner=instance)
    visible = []
    for lease in items:
        if may_hold(ctx, principal, lease):
            visible.append(lease)
            continue
        record = ctx.pools.backends.get(lease["backendId"]) or {}
        if may_see_secrets(ctx, principal, {**record, "leases": [lease]}):
            visible.append(lease)
    return {"items": visible}


@router.get("/api/backend-leases/{lease_id}", response_model=BackendLeaseResponse, response_model_exclude_none=False)
async def lease_status(lease_id: str, request: Request,
                       wait_seconds: float = Query(0.0, alias="waitSeconds", ge=0, le=WAIT_LIMIT),
                       secrets: bool = Query(False),
                       principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    record, lease = lease_or_404(ctx, lease_id)
    authorize_holder(ctx, principal, lease)
    if wait_seconds > 0:
        await run_in_threadpool(ctx.pools.backends.wait_ready, record["id"], wait_seconds)
    return respond(ctx, principal, ctx.pools.backends.lease_response(record["id"], lease_id), secrets)


@router.post("/api/backend-leases/{lease_id}/renew", response_model=BackendLeaseResponse,
             response_model_exclude_none=False)
def renew(lease_id: str, request: Request, body: BackendLeaseRenewRequest | None = None,
          principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    _, lease = lease_or_404(ctx, lease_id)
    authorize_holder(ctx, principal, lease)
    try:
        response = ctx.pools.backends.renew(lease_id, body.ttl if body else None)
    except BackendLeaseGone as error:
        raise not_found(str(error), error="backend_lease_not_found", lease=lease_id) from error
    except (BackendError, ValueError) as error:
        raise bad_request(str(error), error="backend_error") from error
    return respond(ctx, principal, response)


@router.post("/api/backend-leases/{lease_id}/release", response_model=BackendLeaseResponse,
             response_model_exclude_none=False)
def release(lease_id: str, request: Request, body: BackendLeaseReleaseRequest | None = None,
            principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    record, lease = lease_or_404(ctx, lease_id)
    authorize_holder(ctx, principal, lease)
    if lease.get("driverSid"):
        raise bad_request(f"This backend lease belongs to the driver lease {lease['driverSid']}; it ends with that "
                          f"lease (or unlink it with DELETE /api/leases/{lease['driverSid']}/backend).",
                          error="linked_lease")
    released = ctx.pools.backends.release(lease_id, (body.reason if body else None) or "released")
    current = ctx.pools.backends.get(record["id"])
    return {"status": "released", "lease": released,
            "backend": backend_out(ctx.pools.backends.public(current), ctx, principal) if current else None}


@router.get("/api/backend-leases/{lease_id}/info", response_model=BackendInfoOut)
def lease_info(lease_id: str, request: Request, secrets: bool = Query(False),
               principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    record, lease = lease_or_404(ctx, lease_id)
    authorize_holder(ctx, principal, lease)
    return ctx.pools.backends.info(record["id"], secrets=secrets)
