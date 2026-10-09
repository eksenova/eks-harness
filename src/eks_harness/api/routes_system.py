from __future__ import annotations

import hmac
import platform
import sys
import time

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, event_filter_for, get_ctx, optional_principal
from eks_harness.api.errors import forbidden, unauthorized, unavailable
from eks_harness.api.routes_backends import backend_out
from eks_harness.api.routes_leases import visible
from eks_harness.api.routes_pools import browser_out, device_out, holders_map, queue_counts, running_browsers
from eks_harness.api.schemas import DaemonActionResponse, EventList, StatusResponse, VersionResponse, ts_to_datetime
from eks_harness.auth import middleware as auth_middleware
from eks_harness.auth.core import Principal
from eks_harness.daemon import fds
from eks_harness.daemon import housekeeping as housekeeping_module
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.events import Event
from eks_harness.daemon.leases import manager
from eks_harness.db.repos import events as events_repo
from eks_harness.store import retention as store_retention

router = APIRouter(tags=["system"])

CONTROL_HEADER = "X-Harness-Control"
SSE_HEARTBEAT_SECONDS = 15.0
SSE_REFRESH_SECONDS = 10.0


def control_token_ok(ctx: AppContext, token: str | None) -> bool:
    expected = ctx.control_token
    return bool(expected and token and hmac.compare_digest(str(expected), token))


def require_control(request: Request, token: str | None) -> str:
    ctx = get_ctx(request)
    if control_token_ok(ctx, token):
        return "local control"
    principal = optional_principal(request)
    if principal is None:
        raise unauthorized()
    if not principal.is_admin:
        raise forbidden("Only an admin can stop or restart the daemon.", error="admin_required")
    return principal.username


@router.get("/api/render-queue")
def render_queue(_: Principal = Depends(current_principal)) -> dict:
    from eks_harness import renderq

    return renderq.status()


@router.get("/api/version", response_model=VersionResponse)
def version(request: Request, _: Principal = Depends(current_principal)) -> dict:
    info = get_ctx(request).version
    return {"version": info.get("version"), "sourceHash": info.get("sourceHash"), "builtAt": info.get("builtAt"),
            "editable": bool(info.get("editable")), "python": sys.version.split()[0], "platform": platform.platform()}


@router.get("/api/status", response_model=StatusResponse)
async def status(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    return await run_in_threadpool(build_status, ctx, principal)


def build_status(ctx: AppContext, principal: Principal) -> dict:
    leases = manager(ctx)
    stamp = time.time()
    holding, queued = leases.list_out()
    by_sid = {lease.sid: lease for lease in (leases.get_lease(item["sid"]) for item in holding + queued)}
    holding = [item for item in holding if visible(ctx, principal, by_sid[item["sid"]])]
    queued = [item for item in queued if visible(ctx, principal, by_sid[item["sid"]])]
    holders, queues, running = holders_map(ctx), queue_counts(ctx), running_browsers(ctx)
    devices = [device_out(ctx, principal, leases, key, device, holders, queues, stamp)
               for key, device in sorted(ctx.pools.host.devices.items(),
                                         key=lambda item: (item[1].get("kind"), item[1].get("index")))]
    browsers = [browser_out(ctx, principal, leases, process, holders, queues, running)
                for process in ctx.pools.browsers.processes()]
    backends = [backend_out(item, ctx, principal) for item in ctx.pools.backends.list()]
    try:
        storage = store_retention.storage_usage(ctx.db, ctx.config, ctx.paths)
    except Exception:
        storage = None
    info = ctx.version
    return {
        "pid": ctx.pid, "managed": ctx.managed, "fakePools": ctx.pools.fake, "version": info.get("version"),
        "sourceHash": info.get("sourceHash"), "url": ctx.url, "publicUrl": ctx.config.public_url(),
        "startedAt": ts_to_datetime(ctx.started_at), "uptimeSeconds": round(stamp - ctx.started_at, 1),
        "authEnabled": ctx.auth_enabled, "configFile": str(ctx.paths.config_file),
        "restartPending": bool(ctx.restart_pending_keys()), "leases": holding, "queue": queued, "devices": devices,
        "browsers": browsers, "backends": backends, "storage": storage,
        "fileDescriptors": fds.usage(ctx.db.open_connections()),
    }


@router.post("/api/daemon/restart", response_model=DaemonActionResponse)
def restart(request: Request, token: str | None = Header(None, alias=CONTROL_HEADER)) -> dict:
    ctx = get_ctx(request)
    actor = require_control(request, token)
    if not ctx.request_restart():
        raise unavailable("This daemon was not started by 'eks-harness daemon serve', so it cannot restart itself.",
                          error="restart_unsupported")
    ctx.events.publish("daemon.status", actor=actor, detail={"action": "restart"})
    return {"action": "restart", "message": "restarting: leases, browsers, devices and backends are kept"}


@router.post("/api/daemon/stop", response_model=DaemonActionResponse)
def stop(request: Request, token: str | None = Header(None, alias=CONTROL_HEADER)) -> dict:
    ctx = get_ctx(request)
    actor = require_control(request, token)
    if not ctx.request_stop():
        raise unavailable("This daemon was not started by 'eks-harness daemon serve', so it cannot stop itself.",
                          error="stop_unsupported")
    ctx.events.publish("daemon.status", actor=actor, detail={"action": "stop"})
    return {"action": "stop", "message": "stopping; browsers, devices and backends keep running"}


@router.post("/api/daemon/sweep")
async def sweep(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    if not principal.is_admin:
        raise forbidden("Only an admin can sweep.", error="admin_required")
    ctx = get_ctx(request)
    return await run_in_threadpool(housekeeping_module.housekeeping(ctx).sweep)


@router.post("/api/daemon/housekeeping")
async def run_housekeeping(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    if not principal.is_admin:
        raise forbidden("Only an admin can run housekeeping.", error="admin_required")
    ctx = get_ctx(request)
    return await run_in_threadpool(housekeeping_module.housekeeping(ctx).run_once, True)


def event_extra(types: list[str] | None, resource: str | None, session_id: int | None, project_id: str | None,
                lease_sid: str | None):
    prefixes = tuple(t.rstrip("*") for t in types or [] if t)

    def accept(event: Event) -> bool:
        if prefixes and not event.type.startswith(prefixes):
            return False
        if resource and event.resource != resource and not (event.resource or "").startswith(resource + ":"):
            return False
        if session_id is not None and event.session_id != session_id:
            return False
        if project_id and event.project_id != project_id:
            return False
        return not (lease_sid and event.lease_sid != lease_sid)

    return accept


@router.get("/api/events/stream")
async def event_stream(request: Request, type: list[str] | None = Query(None),
                       resource: str | None = None, session_id: int | None = Query(None, alias="sessionId"),
                       project_id: str | None = Query(None, alias="projectId"),
                       lease_sid: str | None = Query(None, alias="sid"),
                       last_event_id: str | None = Header(None, alias="Last-Event-ID"),
                       after: int | None = Query(None, alias="lastEventId"),
                       principal: Principal = Depends(current_principal)) -> StreamingResponse:
    ctx = get_ctx(request)
    extra = event_extra(type, resource, session_id, project_id, lease_sid)
    accept = event_filter_for(ctx.db, principal, extra)
    replay = after
    if replay is None and last_event_id and last_event_id.strip().isdigit():
        replay = int(last_event_id.strip())

    def refresh():
        current = auth_middleware.authenticate(ctx.db, ctx.config, request)
        if current is None or current.user_id != principal.user_id or current.role != principal.role:
            return None
        return event_filter_for(ctx.db, current, extra)

    stream = ctx.events.sse(accept, replay, SSE_HEARTBEAT_SECONDS, request.is_disconnected, refresh=refresh,
                            refresh_seconds=SSE_REFRESH_SECONDS)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/events", response_model=EventList)
def list_events(request: Request, type: list[str] | None = Query(None), resource: str | None = None,
                session_id: int | None = Query(None, alias="sessionId"),
                project_id: str | None = Query(None, alias="projectId"), lease_sid: str | None = Query(None, alias="sid"),
                before: int | None = None, limit: int = Query(100, ge=1, le=1000),
                principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    accept = event_filter_for(ctx.db, principal, event_extra(type, None, None, None, None))
    rows = events_repo.list_events(ctx.db.conn(), before_id=before, resource_prefix=resource, session_id=session_id,
                                   project_id=project_id, lease_sid=lease_sid, limit=limit * 4)
    items = [Event.from_row(r) for r in rows]
    items = [e for e in items if accept(e)][:limit]
    return {"items": [{"id": e.id, "ts": ts_to_datetime(e.ts), "type": e.type, "resource": e.resource,
                       "leaseSid": e.lease_sid, "sessionId": e.session_id, "projectId": e.project_id,
                       "actor": e.actor, "detail": e.detail} for e in items]}
