from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import check_level, current_principal, get_ctx
from eks_harness.api.errors import ApiError, forbidden
from eks_harness.auth.core import Principal
from eks_harness.daemon import hooks
from eks_harness.daemon.context import AppContext
from eks_harness.db import Database
from eks_harness.db.repos import leases as leases_repo
from eks_harness.live import LiveHub, LiveTarget, LiveUnavailable, Viewer, device_target, prepare, profile_target

router = APIRouter(tags=["live"])

SERVICE = "live"
BOUNDARY = "frame"
MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"
FIRST_FRAME_WAIT = 12.0
KEEPALIVE_SECONDS = 5.0
RECHECK_SECONDS = 5.0
STREAM_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
}


def live_hub(ctx: AppContext) -> LiveHub:
    with ctx.lock:
        if not ctx.has_service(SERVICE):
            ctx.register_service(SERVICE, LiveHub.from_config(ctx.config))
        return ctx.service(SERVICE)


@hooks.on_startup(order=60)
def _start_live(ctx: AppContext) -> None:
    live_hub(ctx)


@hooks.on_shutdown(order=10)
def _stop_live(ctx: AppContext) -> None:
    if ctx.has_service(SERVICE):
        ctx.service(SERVICE).shutdown()


def authorize_live(db: Database, principal: Principal, target: LiveTarget) -> None:
    if principal.is_admin:
        return
    holder = leases_repo.holder_of(db.conn(), target.resource)
    if holder is None:
        raise forbidden(f"Only an admin can watch {target.label} while no session holds it.",
                        error="live_forbidden")
    if holder.owner_user is not None and holder.owner_user == principal.user_id:
        return
    if holder.session_id is None or holder.project_id is None:
        raise forbidden(f"Only an admin can watch {target.label} while no session holds it.",
                        error="live_forbidden")
    check_level(db, principal, holder.project_id, holder.session_id, "viewer",
                what=f"the live view of {target.label}")


def _allowed(db: Database, principal: Principal, target: LiveTarget) -> bool:
    try:
        authorize_live(db, principal, target)
    except ApiError:
        return False
    return True


def _as_api_error(error: LiveUnavailable) -> ApiError:
    return ApiError(error.status, error.error, error.message)


def _part(frame: bytes) -> bytes:
    return (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode("ascii")
            + frame + b"\r\n")


async def _stream(request: Request, ctx: AppContext, principal: Principal, target: LiveTarget,
                  max_frames: int | None) -> StreamingResponse:
    try:
        factory = await run_in_threadpool(prepare, ctx.pools, ctx.db, target)
    except LiveUnavailable as error:
        raise _as_api_error(error) from None
    hub = live_hub(ctx)
    loop = asyncio.get_running_loop()
    viewer = hub.attach(target.resource, factory, loop, label=principal.username)
    try:
        first = await viewer.next_frame(FIRST_FRAME_WAIT)
    except BaseException:
        hub.detach(viewer)
        raise
    if first is None and viewer.closed:
        error = viewer.error or "the stream ended"
        hub.detach(viewer)
        raise ApiError(502, "live_failed", f"The live view of {target.label} failed: {error}")
    interval = 1.0 / max(1, int(ctx.config["live.maxFps"]))
    recheck: Callable[[], bool] = lambda: _allowed(ctx.db, principal, target)
    return StreamingResponse(_body(request, hub, viewer, first, interval, recheck, max_frames),
                             media_type=MEDIA_TYPE, headers=STREAM_HEADERS)


async def _body(request: Request, hub: LiveHub, viewer: Viewer, first: bytes | None, interval: float,
                recheck: Callable[[], bool], max_frames: int | None) -> AsyncIterator[bytes]:
    sent = 0
    last_sent = 0.0
    checked = time.monotonic()
    try:
        frame = first
        while True:
            if frame is not None:
                yield _part(frame)
                viewer.frames_sent += 1
                sent += 1
                last_sent = time.monotonic()
                if max_frames is not None and sent >= max_frames:
                    return
            if await request.is_disconnected():
                return
            wait = interval - (time.monotonic() - last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            frame = await viewer.next_frame(KEEPALIVE_SECONDS)
            if frame is None:
                if viewer.closed:
                    return
                frame = viewer.latest
            if time.monotonic() - checked >= RECHECK_SECONDS:
                checked = time.monotonic()
                if not await run_in_threadpool(recheck):
                    return
    finally:
        hub.detach(viewer)


@router.get("/api/devices/{kind}/{index}/live", response_class=StreamingResponse,
            summary="Live view of a device as an MJPEG stream (multipart/x-mixed-replace)",
            responses={200: {"content": {MEDIA_TYPE: {}}}})
async def device_live(request: Request, kind: str = Path(..., pattern="^(ios|android)$"),
                      index: int = Path(..., ge=1),
                      frames: int | None = Query(None, ge=1, le=10000,
                                                 description="end the stream after this many frames"),
                      principal: Principal = Depends(current_principal)) -> StreamingResponse:
    ctx = get_ctx(request)
    try:
        target = device_target(ctx.pools, kind, index)
    except LiveUnavailable as error:
        raise _as_api_error(error) from None
    await run_in_threadpool(authorize_live, ctx.db, principal, target)
    return await _stream(request, ctx, principal, target, frames)


@router.get("/api/profiles/{profile_id}/live", response_class=StreamingResponse,
            summary="Live view of a browser profile's page as an MJPEG stream (multipart/x-mixed-replace)",
            responses={200: {"content": {MEDIA_TYPE: {}}}})
async def profile_live(request: Request, profile_id: str,
                       frames: int | None = Query(None, ge=1, le=10000,
                                                  description="end the stream after this many frames"),
                       principal: Principal = Depends(current_principal)) -> StreamingResponse:
    ctx = get_ctx(request)
    try:
        target = profile_target(ctx.pools, profile_id)
    except LiveUnavailable as error:
        raise _as_api_error(error) from None
    await run_in_threadpool(authorize_live, ctx.db, principal, target)
    return await _stream(request, ctx, principal, target, frames)
