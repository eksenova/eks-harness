from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from eks_harness.api.deps import access_scope, current_principal, get_ctx
from eks_harness.api.errors import forbidden, not_found
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos import leases as leases_repo
from eks_harness.drivers.workers import WorkerManager

router = APIRouter(tags=["drivers"])

PLATFORMS = {"browser": "web", "chrome": "web", "ios": "ios", "android": "android"}


def _manager(ctx: AppContext) -> WorkerManager:
    return WorkerManager(ctx.paths)


def _lease(ctx: AppContext, sid: str, principal: Principal, level: str = "editor") -> Any:
    lease = leases_repo.get_by_sid(ctx.db.conn(), sid)
    if lease is None:
        raise not_found(f"No lease {sid}.", error="lease_not_found")
    if not principal.is_admin:
        scope = access_scope(ctx.db, principal)
        if not lease.project_id or not scope.can(lease.project_id, lease.session_id, level):
            raise forbidden("You cannot drive this lease.", error="lease_forbidden")
    return lease


@router.get("/api/drivers/workers")
def workers(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    items = []
    for handle in _manager(ctx).list():
        lease = leases_repo.get_by_sid(ctx.db.conn(), handle.key)
        if not principal.is_admin:
            scope = access_scope(ctx.db, principal)
            if lease is None or not lease.project_id or not scope.can(lease.project_id, lease.session_id, "viewer"):
                continue
        items.append({"kind": handle.kind, "sid": handle.key, "port": handle.port, "pid": handle.pid,
                      "startedAt": handle.started_at, "lease": lease.kind if lease else None,
                      "state": lease.state if lease else None})
    return {"items": items}


@router.get("/api/drivers/{sid}")
def worker(sid: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    lease = _lease(ctx, sid, principal, "viewer")
    platform = PLATFORMS.get(lease.kind, lease.kind)
    manager = _manager(ctx)
    handle = manager.get("web", sid) if platform == "web" else manager.get("mobile", sid)
    return {"sid": sid, "platform": platform, "lease": lease.kind, "state": lease.state,
            "worker": {"kind": handle.kind, "port": handle.port, "startedAt": handle.started_at} if handle else None}
