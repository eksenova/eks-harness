from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, current_principal, get_ctx
from eks_harness.api.errors import bad_request, forbidden, not_found
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos import leases as leases_repo
from eks_harness.drivers.client import WorkerError
from eks_harness.drivers.sessions import WorkerSession
from eks_harness.drivers.workers import WorkerManager

router = APIRouter(tags=["drivers"])

PLATFORMS = {"browser": "web", "chrome": "web", "ios": "ios", "android": "android"}
OBSERVE_LIMIT = 200_000


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


def _session(ctx: AppContext, sid: str, principal: Principal) -> WorkerSession:
    lease = _lease(ctx, sid, principal)
    platform = PLATFORMS.get(lease.kind, lease.kind)
    manager = _manager(ctx)
    handle = manager.get("web", sid) if platform == "web" else manager.get("mobile", sid)
    if handle is None:
        raise not_found(f"No driver worker runs for lease {sid}. Start one with a flow or 'eks-harness driver'.",
                        error="no_worker")
    return WorkerSession(handle.client(), platform=platform, sid=sid)


def _trim(value: Any) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= OBSERVE_LIMIT:
        return value
    return {"truncated": True, "preview": text[:OBSERVE_LIMIT]}


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


@router.post("/api/drivers/{sid}/act")
async def act(sid: str, request: Request, body: dict = Body(...), principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    action = str(body.get("action") or "")
    if not action:
        raise bad_request("Send the action, for example press, fill, navigate, back, evaluate, arm.", error="no_action")
    session = _session(ctx, sid, principal)
    params = dict(body.get("params") or {})
    try:
        result = await run_in_threadpool(session.act, action, **params)
    except WorkerError as error:
        raise bad_request(str(error), error="action_failed") from error
    ctx.events.publish("driver.action", resource=f"lease:{sid}", lease_sid=sid, actor=principal.username,
                       persist=False, detail={"action": action})
    return {"sid": sid, **_trim(result)}


@router.post("/api/drivers/{sid}/observe")
async def observe(sid: str, request: Request, body: dict = Body(...),
                  principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    query = str(body.get("query") or "")
    if not query:
        raise bad_request("Send the query, for example tree, text, route, logs.", error="no_query")
    session = _session(ctx, sid, principal)
    try:
        result = await run_in_threadpool(session.observe, query, **dict(body.get("params") or {}))
    except WorkerError as error:
        raise bad_request(str(error), error="observe_failed") from error
    return {"sid": sid, **_trim(result)}


@router.post("/api/drivers/{sid}/capture")
async def capture(sid: str, request: Request, body: dict = Body(...),
                  principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    kind = str(body.get("kind") or "")
    if not kind:
        raise bad_request("Send the capture kind: screenshot, video.start, video.stop, dom, mhtml, a11y.",
                          error="no_kind")
    session = _session(ctx, sid, principal)
    try:
        result = await run_in_threadpool(session.capture, kind, **dict(body.get("params") or {}))
    except WorkerError as error:
        raise bad_request(str(error), error="capture_failed") from error
    return {"sid": sid, **_trim(result)}


@router.get("/api/drivers/{sid}/events")
async def events(sid: str, request: Request, principal: Principal = Depends(current_principal)) -> StreamingResponse:
    ctx = get_ctx(request)
    session = _session(ctx, sid, principal)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=500)
    stop = threading.Event()

    def pump() -> None:
        try:
            for event in session.events(timeout=None):
                if stop.is_set():
                    return
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as error:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(error)[:300]})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=pump, daemon=True).start()

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield f"data: {json.dumps(item, default=str)}\n\n"
        finally:
            stop.set()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
