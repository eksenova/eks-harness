from __future__ import annotations

import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx
from eks_harness.api.errors import ApiError, bad_request, conflict
from eks_harness.api.routes_leases import authorize_lease
from eks_harness.api.schemas import (
    ArtifactOut,
    CaptureRequest,
    VideoResetResponse,
    VideoStartResponse,
    VideoStopResponse,
    ts_to_datetime,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.leases import manager
from eks_harness.db.repos.leases import Lease
from eks_harness.pools.base import PoolError
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store.encode import EncodeError, encode_recording

router = APIRouter(tags=["captures"])

BROWSER_HINT = ("Browser captures are made by the harness server and uploaded with POST /api/artifacts and the sid; "
                "the daemon captures only iOS and Android devices.")


def device_lease(ctx: AppContext, principal: Principal, sid: str) -> Lease:
    leases = manager(ctx)
    lease = leases.get_lease(sid)
    authorize_lease(ctx, principal, lease)
    if lease.ended:
        raise leases.released_error(lease)
    if lease.kind == "browser":
        raise bad_request(BROWSER_HINT, error="browser_capture")
    if lease.state == "queued" or not lease.resource:
        raise conflict(f"The lease {lease.sid} is still queued; it holds no device yet.", error="lease_queued")
    if lease.phase != "ready":
        raise conflict(f"The device of lease {lease.sid} is not ready yet (phase {lease.phase}).",
                       error="lease_not_ready")
    return lease


def staging_file(ctx: AppContext, lease: Lease, kind: str, suffix: str) -> Path:
    folder = ctx.paths.tmp_dir / f"capture-{secrets.token_hex(6)}"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    device = (lease.resource or "device").replace(":", "")
    return folder / f"{kind}-{device}-{stamp}{suffix}"


def device_meta(ctx: AppContext, lease: Lease, info: dict, extra: dict) -> dict:
    device = ctx.pools.host.devices.get(lease.resource or "") or {}
    meta = {"device": device.get("name"), "resource": lease.resource, "platform": lease.kind,
            "udid": device.get("udid"), "serial": device.get("serial"),
            "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    for key in ("wallSeconds", "startedAt", "lines", "since"):
        if info.get(key) is not None:
            meta[key] = info[key]
    meta.update(extra or {})
    return {k: v for k, v in meta.items() if v is not None}


async def pool_call(fn, *args) -> Any:
    try:
        return await run_in_threadpool(fn, *args)
    except PoolError as error:
        raise ApiError(409, "device_error", str(error)) from error


async def store_file(ctx: AppContext, principal: Principal, lease: Lease, path: Path, kind: str, *, caption: str,
                     tags: list[str], meta: dict, source: str | None, mime: str | None,
                     filename: str | None = None) -> dict:
    artifact = await run_in_threadpool(
        lambda: store_artifacts.ingest_file(
            ctx.db, ctx.config, sid=lease.sid, path_or_stream=path, kind=kind, caption=caption, tags=tags,
            meta=meta, source=source or "agent", user=principal, filename=filename or path.name, mime=mime,
            events=ctx.events, move=True))
    out = store_artifacts.to_out(ctx.db.conn(), ctx.links, ctx.paths, artifact, user_id=principal.user_id)
    return out.model_dump(by_alias=True, mode="json")


async def ingest(ctx: AppContext, principal: Principal, lease: Lease, path: Path, kind: str, body: CaptureRequest,
                 info: dict) -> dict:
    try:
        return await store_file(ctx, principal, lease, path, kind, caption=body.caption, tags=body.tags,
                                meta=device_meta(ctx, lease, info, body.meta), source=body.source,
                                mime=info.get("mime"))
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def live_hub(ctx: AppContext, lease: Lease):
    if lease.kind != "ios" or not ctx.has_service("live"):
        return None
    return ctx.service("live")


def step_times(steps: Any) -> list[float]:
    times: list[float] = []
    for step in steps if isinstance(steps, list) else []:
        at = step.get("t") if isinstance(step, dict) else None
        if isinstance(at, (int, float)) and not isinstance(at, bool) and at >= 0:
            times.append(float(at))
    return times


def merged_tags(tags: list[str], extra: str) -> list[str]:
    return list(dict.fromkeys([*tags, extra]))


@router.post("/api/captures/{sid}/screenshot", response_model=ArtifactOut)
async def screenshot(sid: str, request: Request, body: CaptureRequest | None = None,
                     principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    body = body or CaptureRequest()
    lease = device_lease(ctx, principal, sid)
    dest = staging_file(ctx, lease, "screenshot", ".png")
    try:
        info = await pool_call(ctx.pools.devices.screenshot, lease.resource, dest)
        if info.get("width") and info.get("height"):
            await run_in_threadpool(remember_screen, ctx, lease.resource, int(info["width"]), int(info["height"]))
        await run_in_threadpool(manager(ctx).heartbeat, lease.sid)
    except BaseException:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise
    return await ingest(ctx, principal, lease, Path(info.get("path") or dest), "screenshot", body, info)


def remember_screen(ctx: AppContext, resource: str, width: int, height: int) -> None:
    with ctx.pools.host.lock:
        device = ctx.pools.host.devices.get(resource)
        if device is not None:
            device["screen"] = {"width": width, "height": height}
            ctx.pools.host.save()


def remember_recording(ctx: AppContext, resource: str, sid: str) -> dict | None:
    with ctx.pools.host.lock:
        device = ctx.pools.host.devices.get(resource)
        if device is not None:
            device["recordingSid"] = sid
            ctx.pools.host.save()
        recording = (device or {}).get("recording")
    return recording if isinstance(recording, dict) else None


def forget_recording_owner(ctx: AppContext, resource: str) -> None:
    with ctx.pools.host.lock:
        (ctx.pools.host.devices.get(resource) or {}).pop("recordingSid", None)
        ctx.pools.host.save()


@router.post("/api/captures/{sid}/video/start", response_model=VideoStartResponse)
async def video_start(sid: str, request: Request, body: CaptureRequest | None = None,
                      principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    lease = device_lease(ctx, principal, sid)
    dest = staging_file(ctx, lease, "video", ".mp4")
    hub = live_hub(ctx, lease)
    if hub is not None:
        await run_in_threadpool(hub.suspend, lease.resource, "video capture")
    try:
        await pool_call(ctx.pools.devices.record_start, lease.resource, dest)
    except BaseException:
        shutil.rmtree(dest.parent, ignore_errors=True)
        if hub is not None:
            hub.resume(lease.resource)
        raise
    recording = await run_in_threadpool(remember_recording, ctx, lease.resource, lease.sid)
    await run_in_threadpool(manager(ctx).heartbeat, lease.sid)
    started = recording.get("startedAt") if recording else None
    return {"sid": lease.sid, "resource": lease.resource, "recording": True,
            "startedAt": ts_to_datetime(started or time.time())}


@router.post("/api/captures/{sid}/video/stop", response_model=VideoStopResponse)
async def video_stop(sid: str, request: Request, body: CaptureRequest | None = None,
                     principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    body = body or CaptureRequest()
    lease = device_lease(ctx, principal, sid)
    if not await run_in_threadpool(ctx.pools.devices.is_recording, lease.resource):
        raise conflict(f"{lease.resource} is not recording; start with POST /api/captures/{sid}/video/start.",
                       error="not_recording")
    owner = (ctx.pools.host.devices.get(lease.resource) or {}).get("recordingSid")
    if owner and owner != lease.sid:
        raise conflict(f"The recording on {lease.resource} belongs to lease {owner}.", error="not_your_recording")
    hub = live_hub(ctx, lease)
    dest = (ctx.pools.host.devices.get(lease.resource) or {}).get("recording", {}).get("dest")
    try:
        info = await pool_call(ctx.pools.devices.record_stop, lease.resource)
    except BaseException:
        if dest:
            shutil.rmtree(Path(dest).parent, ignore_errors=True)
        raise
    finally:
        await run_in_threadpool(forget_recording_owner, ctx, lease.resource)
        if hub is not None:
            hub.resume(lease.resource)
    raw = Path(info["path"])
    try:
        await run_in_threadpool(manager(ctx).heartbeat, lease.sid)
        trim = body.wants_trim()
        meta = device_meta(ctx, lease, info, {k: v for k, v in body.meta.items() if k != "trim"})
        return await encode_and_store(ctx, principal, lease, raw, info, body, meta, trim)
    finally:
        shutil.rmtree(raw.parent, ignore_errors=True)


@router.post("/api/captures/{sid}/video/reset", response_model=VideoResetResponse)
async def video_reset(sid: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    lease = device_lease(ctx, principal, sid)
    device = ctx.pools.host.devices.get(lease.resource) or {}
    recording = device.get("recording") if isinstance(device.get("recording"), dict) else None
    owner = device.get("recordingSid")
    if owner and owner != lease.sid:
        raise conflict(f"The recording on {lease.resource} belongs to lease {owner}; it was not reset.",
                       error="not_your_recording")
    hub = live_hub(ctx, lease)
    if hub is not None:
        await run_in_threadpool(hub.suspend, lease.resource, "recorder reset")
    leases = manager(ctx)
    await run_in_threadpool(leases.mark_busy, {lease.resource}, "recorder reset")
    try:
        report = await pool_call(ctx.pools.devices.record_reset, lease.resource)
    finally:
        await run_in_threadpool(leases.clear_busy, {lease.resource})
        if hub is not None:
            hub.resume(lease.resource)
    if recording and recording.get("dest"):
        shutil.rmtree(Path(recording["dest"]).parent, ignore_errors=True)
    await run_in_threadpool(leases.heartbeat, lease.sid)
    return {"sid": lease.sid, "resource": lease.resource, **report}


async def encode_and_store(ctx: AppContext, principal: Principal, lease: Lease, raw: Path, info: dict,
                           body: CaptureRequest, meta: dict, trim: bool) -> dict:
    wall = info.get("wallSeconds")
    pad_to = float(wall) if isinstance(wall, (int, float)) and wall > 0 else None
    full_path = raw.with_name(raw.stem + "-full.mp4")
    try:
        result = await run_in_threadpool(encode_recording, raw, full_path, pad_to=pad_to, trim=trim,
                                         events=step_times(meta.get("steps")), lease=lease.sid)
    except EncodeError as error:
        kept = await store_file(ctx, principal, lease, raw, "video", caption=body.caption,
                                tags=merged_tags(body.tags, "unencoded"),
                                meta={**meta, "encoded": False, "encodeError": str(error)}, source=body.source,
                                mime="video/mp4")
        raise ApiError(503 if error.missing_tool else 502, "encode_failed",
                       f"The recording was stopped but could not be encoded: {error}. The raw recording is kept "
                       f"as artifact {kept['id']}.", artifact=kept) from error
    base = raw.stem
    if result.trimmed is None:
        out = await store_file(ctx, principal, lease, result.full, "video", caption=body.caption, tags=body.tags,
                               meta={**meta, "trimmed": False, "encoding": result.report}, source=body.source,
                               mime="video/mp4", filename=f"{base}.mp4")
        out["encoding"] = result.report
        return out
    full = await store_file(ctx, principal, lease, result.full, "video",
                            caption=f"{body.caption} (full length)" if body.caption else "",
                            tags=merged_tags(body.tags, "full"),
                            meta={**meta, "trimmed": False, "encoding": result.report}, source=body.source,
                            mime="video/mp4", filename=f"{base}-full.mp4")
    out = await store_file(ctx, principal, lease, result.trimmed, "video", caption=body.caption,
                           tags=merged_tags(body.tags, "trimmed"),
                           meta={**meta, "trimmed": True, "fullId": full["id"], "encoding": result.report},
                           source=body.source, mime="video/mp4", filename=f"{base}.mp4")
    out["full"] = full
    out["encoding"] = result.report
    return out


@router.post("/api/captures/{sid}/log", response_model=ArtifactOut)
async def device_log(sid: str, request: Request, body: CaptureRequest | None = None,
                     since: float | None = Query(None, description="epoch seconds, or negative seconds back from now"),
                     principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    body = body or CaptureRequest()
    lease = device_lease(ctx, principal, sid)
    start = time.time() + since if since is not None and since < 0 else since
    dest = staging_file(ctx, lease, "log", ".log")
    try:
        info = await pool_call(ctx.pools.devices.device_log, lease.resource, start, dest)
    except BaseException:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise
    return await ingest(ctx, principal, lease, Path(info.get("path") or dest), "log", body, info)
