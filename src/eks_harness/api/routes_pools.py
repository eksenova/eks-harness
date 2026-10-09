from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, current_principal, event_filter_for, get_ctx
from eks_harness.api.errors import ApiError, bad_request, conflict, forbidden, not_found
from eks_harness.api.schemas import (
    BrowserDetail,
    BrowserList,
    DeviceDetail,
    DeviceList,
    PoolAction,
    PoolActionRequest,
    PoolActionResponse,
    ProfileDetail,
    ProfileList,
    ts_to_datetime,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon import events as ev
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.events import Event
from eks_harness.daemon.leases import LeaseManager, browser_index, manager
from eks_harness.db.repos import events as events_repo
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos.leases import Lease
from eks_harness.live.chrome import list_pages
from eks_harness.live.common import LiveError
from eks_harness.live.sources import profile_contexts, profile_target
from eks_harness.pools.base import DEVICE_KINDS, PoolError, parse_browser_resource
from eks_harness.store.media import jpeg_dimensions

router = APIRouter(tags=["pools"])

ACTIONS = ("start", "shutdown", "reset", "delete")
ACTIVITY_LIMIT = 50
HISTORY_LIMIT = 30


def event_out(row: events_repo.EventRow) -> dict:
    return {"id": row.id, "ts": ts_to_datetime(row.ts), "type": row.type, "resource": row.resource,
            "leaseSid": row.lease_sid, "sessionId": row.session_id, "projectId": row.project_id,
            "actor": row.actor, "detail": row.detail}


def session_brief(ctx: AppContext, principal: Principal, lease: Lease | None) -> dict | None:
    if lease is None or lease.session_id is None or not lease.project_id:
        return None
    if not principal.is_admin and not access_scope(ctx.db, principal).can(lease.project_id, lease.session_id,
                                                                            "viewer"):
        return None
    return {"id": lease.session_id, "projectId": lease.project_id, "name": lease.session_name,
            "slug": lease.session_slug, "url": ctx.links.session(lease.project_id, lease.session_slug),
            "sharedUrl": ctx.links.shared_session(lease.session_slug)}


def redact(ctx: AppContext, principal: Principal, leases: LeaseManager, lease: Lease) -> dict:
    brief = leases.lease_brief(lease)
    if not principal.is_admin and session_brief(ctx, principal, lease) is None \
            and not (lease.owner_kind == "manual" and lease.owner_user == principal.user_id):
        brief["sid"] = "hidden"
        brief["ownerInstance"] = "another user"
    return brief


def redact_full(ctx: AppContext, principal: Principal, leases: LeaseManager, lease: Lease) -> dict:
    out = leases.lease_out(lease)
    if not principal.is_admin and session_brief(ctx, principal, lease) is None \
            and not (lease.owner_kind == "manual" and lease.owner_user == principal.user_id):
        out.update({"sid": "hidden", "ownerInstance": "another user", "sessionName": None, "sessionSlug": None,
                    "projectId": None, "sessionId": None, "urls": None, "tree": None, "stateDir": None,
                    "label": None, "meta": {}, "cdp": None})
    return out


def live_hub(ctx: AppContext):
    return ctx.service("live") if ctx.has_service("live") else None


def live_state(ctx: AppContext, resource: str) -> tuple[int, tuple[int, int] | None]:
    hub = live_hub(ctx)
    if hub is None:
        return 0, None
    info = hub.info(resource)
    frame = hub.last_frame(resource)
    return (info.viewers if info is not None else 0), (jpeg_dimensions(frame) if frame else None)


def screen_fields(size: tuple[int, int] | None, viewers: int) -> dict:
    return {"screenWidth": size[0] if size else None, "screenHeight": size[1] if size else None,
            "liveViewers": viewers}


def minutes(seconds: float) -> str:
    value = max(0, int(seconds // 60))
    return f"{value} min" if value < 120 else f"{value // 60} h"


def device_status_text(device: dict, holder: Lease | None, queue_length: int, stamp: float) -> str:
    status = device.get("status") or "unknown"
    busy = device.get("busy")
    if busy:
        text = {"shutdown": "Shutting down", "reset": "Resetting", "delete": "Deleting"}.get(busy, busy.title())
    elif status == "booting":
        text = "Booting"
    elif status == "stopping":
        text = "Shutting down"
    elif status == "off":
        text = "Shut down"
    elif status == "on" and holder is not None:
        text = {"preparing": "Preparing", "failed": "Failed"}.get(holder.phase or "", "In use")
        if holder.state == "idle":
            text = "In use (agent idle)"
    elif status == "on":
        text = f"Idle {minutes(stamp - float(device.get('last_used') or device.get('statusSince') or stamp))}"
    else:
        text = "Unknown"
    if device.get("recording"):
        text += ", recording"
    if queue_length:
        text += f", queued ({queue_length})"
    return text


def device_out(ctx: AppContext, principal: Principal, leases: LeaseManager, key: str, device: dict,
               holders: dict[str, Lease], queues: dict[str, int], stamp: float) -> dict:
    holder = holders.get(key)
    kind = device.get("kind")
    last_used = device.get("last_used")
    return {
        "key": key, "kind": kind, "index": device.get("index"), "name": device.get("name"),
        "status": device.get("status") or "unknown",
        "statusText": device_status_text(device, holder, queues.get(kind, 0), stamp),
        "statusSince": ts_to_datetime(device.get("statusSince")), "udid": device.get("udid"),
        "serial": device.get("serial"), "port": device.get("port"), "retired": bool(device.get("retired")),
        "lastUsedAt": ts_to_datetime(last_used),
        "idleSeconds": round(stamp - float(last_used), 1) if last_used and holder is None
        and device.get("status") == "on" else None,
        "lease": redact(ctx, principal, leases, holder) if holder else None,
        "session": session_brief(ctx, principal, holder), "queueLength": queues.get(kind, 0),
        "liveUrl": ctx.links.device_live(kind, device.get("index")) if device.get("status") == "on" else None,
        **device_screen(ctx, key, device),
    }


def device_screen(ctx: AppContext, key: str, device: dict) -> dict:
    viewers, size = live_state(ctx, key)
    if size is None:
        screen = device.get("screen") or {}
        if screen.get("width") and screen.get("height"):
            size = (int(screen["width"]), int(screen["height"]))
    return screen_fields(size, viewers)


def holders_map(ctx: AppContext) -> dict[str, Lease]:
    return {lease.resource: lease for lease in leases_repo.holding(ctx.db.conn()) if lease.resource}


def queue_counts(ctx: AppContext) -> dict[str, int]:
    counts: dict[str, int] = {}
    for lease in leases_repo.queue(ctx.db.conn()):
        counts[lease.kind] = counts.get(lease.kind, 0) + 1
    return counts


def device_key_of(kind: str, index: int) -> str:
    if kind not in DEVICE_KINDS:
        raise not_found(f"No device kind {kind}; use ios or android.", error="device_not_found")
    return f"{kind}:{index}"


def lookup_device(ctx: AppContext, kind: str, index: int) -> tuple[str, dict]:
    key = device_key_of(kind, index)
    device = ctx.pools.host.devices.get(key)
    if device is None:
        raise not_found(f"No device {key} in the pool.", error="device_not_found")
    return key, device


def activity(ctx: AppContext, principal: Principal, resource: str | None = None,
             prefix: str | None = None) -> list[dict]:
    rows = events_repo.list_events(ctx.db.conn(), resource=resource, resource_prefix=prefix, limit=ACTIVITY_LIMIT)
    accept = event_filter_for(ctx.db, principal)
    return [event_out(r) for r in rows if accept(Event.from_row(r))]


def visible_history(ctx: AppContext, principal: Principal, leases: LeaseManager, resource: str) -> list[dict]:
    items = []
    for lease in leases_repo.history_for_resource(ctx.db.conn(), resource, HISTORY_LIMIT):
        out = leases.lease_out(lease)
        if not principal.is_admin and session_brief(ctx, principal, lease) is None:
            out.update({"sid": "hidden", "ownerInstance": "another user", "sessionName": None, "sessionSlug": None,
                        "projectId": None, "sessionId": None, "urls": None, "tree": None, "stateDir": None,
                        "meta": {}})
        items.append(out)
    return items


@router.get("/api/devices", response_model=DeviceList)
def list_devices(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    stamp = time.time()
    holders, queues = holders_map(ctx), queue_counts(ctx)
    devices = sorted(ctx.pools.host.devices.items(), key=lambda item: (item[1].get("kind"), item[1].get("index")))
    items = [device_out(ctx, principal, leases, key, device, holders, queues, stamp) for key, device in devices]
    return {"items": items, "maxRunning": int(ctx.config["devices.maxRunning"]),
            "running": leases.pools.devices.running_count()}


@router.get("/api/devices/{kind}/{index}", response_model=DeviceDetail)
def device_detail(kind: str, index: int, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    key, device = lookup_device(ctx, kind, index)
    out = device_out(ctx, principal, leases, key, device, holders_map(ctx), queue_counts(ctx), time.time())
    out["queue"] = [redact_full(ctx, principal, leases, lease) for lease in leases.queue_for(kind)]
    out["activity"] = activity(ctx, principal, resource=key)
    out["history"] = visible_history(ctx, principal, leases, key)
    out["logResource"] = key
    return out


def profile_out(ctx: AppContext, principal: Principal, leases: LeaseManager, profile_id: str,
                holders: dict[str, Lease], queues: dict[str, int], running: dict[int, bool]) -> dict:
    index, number = parse_browser_resource(profile_id)
    holder = holders.get(profile_id)
    busy = leases.busy.get(profile_id)
    if busy:
        status, text = "busy", {"reset": "Resetting", "delete": "Deleting"}.get(busy, busy.title())
    elif holder is not None:
        status = "in_use"
        text = {"preparing": "Preparing", "failed": "Failed"}.get(holder.phase or "", "In use")
        if holder.state == "idle":
            text = "In use (agent idle)"
    elif running.get(index):
        status, text = "free", "Free"
    else:
        status, text = "off", "Browser not running"
    queue_length = queues.get("browser", 0)
    if queue_length:
        text += f", queued ({queue_length})"
    record = ctx.pools.host.browsers.get(str(index)) or {}
    if holder is not None:
        since = holder.acquired_at
    elif running.get(index):
        since = leases.profile_released_at(profile_id) or record.get("launched")
    else:
        since = None
    viewers, size = live_state(ctx, profile_id)
    if size is None and holder is not None:
        viewport = (holder.meta or {}).get("viewport") or {}
        if isinstance(viewport, dict) and viewport.get("width") and viewport.get("height"):
            size = (int(viewport["width"]), int(viewport["height"]))
    visible = holder is not None and (principal.is_admin or session_brief(ctx, principal, holder) is not None)
    return {"id": profile_id, "browser": index, "profile": number, "status": status, "statusText": text,
            "statusSince": ts_to_datetime(since),
            "pageUrl": (holder.meta or {}).get("pageUrl") if visible else None,
            "lease": redact(ctx, principal, leases, holder) if holder else None,
            "session": session_brief(ctx, principal, holder), "queueLength": queue_length,
            "liveUrl": ctx.links.profile_live(profile_id) if holder is not None and running.get(index) else None,
            **screen_fields(size, viewers)}


def current_page_url(ctx: AppContext, profile_id: str, holder: Lease | None) -> str | None:
    if holder is None or ctx.pools.fake:
        return None
    index = browser_index(profile_id)
    cdp = ctx.pools.browsers.cdp_url(index) if index is not None else None
    if not cdp:
        return None
    try:
        contexts = profile_contexts(ctx.db, profile_target(ctx.pools, profile_id))
        pages = list_pages(cdp, contexts, timeout=2.0)
    except LiveError:
        return None
    if not pages:
        return None
    attached = [p for p in pages if p["attached"]]
    return (attached or pages)[-1].get("url")


def running_browsers(ctx: AppContext) -> dict[int, bool]:
    return {index: bool((ctx.pools.host.browsers.get(str(index)) or {}).get("pid"))
            for index in ctx.pools.browsers.indices()}


@router.get("/api/profiles", response_model=ProfileList)
def list_profiles(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    holders, queues, running = holders_map(ctx), queue_counts(ctx), running_browsers(ctx)
    return {"items": [profile_out(ctx, principal, leases, p, holders, queues, running)
                      for p in ctx.pools.browsers.profile_ids()]}


def lookup_profile(ctx: AppContext, profile_id: str) -> str:
    if profile_id not in ctx.pools.browsers.profile_ids():
        raise not_found(f"No browser profile {profile_id}; profiles look like browser:1:2.", error="profile_not_found")
    return profile_id


@router.get("/api/profiles/{profile_id}", response_model=ProfileDetail)
def profile_detail(profile_id: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    lookup_profile(ctx, profile_id)
    out = profile_out(ctx, principal, leases, profile_id, holders_map(ctx), queue_counts(ctx), running_browsers(ctx))
    out["queue"] = [redact_full(ctx, principal, leases, lease) for lease in leases.queue_for("browser")]
    out["activity"] = activity(ctx, principal, resource=profile_id)
    out["history"] = visible_history(ctx, principal, leases, profile_id)
    index = browser_index(profile_id)
    out["cdp"] = (ctx.pools.host.browsers.get(str(index)) or {}).get("cdp") if principal.is_admin else None
    holder = holders_map(ctx).get(profile_id)
    if holder is not None and (principal.is_admin or session_brief(ctx, principal, holder) is not None):
        out["pageUrl"] = current_page_url(ctx, profile_id, holder) or out.get("pageUrl")
    return out


def browser_out(ctx: AppContext, principal: Principal, leases: LeaseManager, process: dict,
                holders: dict[str, Lease], queues: dict[str, int], running: dict[int, bool]) -> dict:
    index = int(process["index"])
    alive = bool(process.get("alive"))
    if leases.busy.get(f"browser:{index}"):
        status, text = "busy", leases.busy[f"browser:{index}"].title()
    elif process.get("pid") and alive:
        status, text = "running", "Running"
    elif process.get("pid"):
        status, text = "unresponsive", "Not answering"
    else:
        status, text = "stopped", "Stopped"
    profiles = [profile_out(ctx, principal, leases, p, holders, queues, running)
                for p in ctx.pools.browsers.profile_ids() if p.startswith(f"browser:{index}:")]
    in_use = sum(1 for p in profiles if p["status"] == "in_use")
    if status == "running":
        text = f"Running, {in_use} of {len(profiles)} profiles in use"
    return {"index": index, "status": status, "statusText": text, "alive": alive, "pid": process.get("pid"),
            "port": process.get("port"), "cdp": process.get("cdp") if principal.is_admin else None,
            "binary": process.get("binary"), "launchedAt": ts_to_datetime(process.get("launched")),
            "lastUsedAt": ts_to_datetime(process.get("last_used")), "profiles": profiles,
            "logResource": f"browser:{index}"}


@router.get("/api/browsers", response_model=BrowserList)
async def list_browsers(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    processes = await run_in_threadpool(ctx.pools.browsers.processes)
    holders, queues, running = holders_map(ctx), queue_counts(ctx), running_browsers(ctx)
    return {"items": [browser_out(ctx, principal, leases, p, holders, queues, running) for p in processes],
            "capacity": ctx.pools.browsers.capacity(), "command": str(ctx.config["browser.command"])}


@router.get("/api/browsers/{index}", response_model=BrowserDetail)
async def browser_detail(index: int, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    if index not in ctx.pools.browsers.indices():
        raise not_found(f"No browser {index}; browser.instances is {ctx.config['browser.instances']}.",
                        error="browser_not_found")
    processes = await run_in_threadpool(ctx.pools.browsers.processes)
    process = next(p for p in processes if int(p["index"]) == index)
    out = browser_out(ctx, principal, leases, process, holders_map(ctx), queue_counts(ctx), running_browsers(ctx))
    out["activity"] = activity(ctx, principal, prefix=f"browser:{index}")
    return out


def check_action(action: str) -> PoolAction:
    if action not in ACTIONS:
        raise not_found(f"Unknown action {action}; use {', '.join(ACTIONS)}.", error="unknown_action")
    return action


def require_confirmation(body: PoolActionRequest, names: set[str], interrupted: list[Lease], action: str,
                         what: str) -> None:
    if body.force or (body.confirm or "").strip() in names:
        return
    raise conflict(
        f"{action.title()} {what} needs a confirmation: send confirm={sorted(names)[0]!r}"
        + (f"; it interrupts {len(interrupted)} lease(s)" if interrupted else ""),
        error="confirmation_required", confirm=sorted(names)[0],
        interrupts=[{"sid": lease.sid, "session": lease.session_name, "project": lease.project_id,
                     "instance": lease.owner_instance, "resource": lease.resource} for lease in interrupted])


def authorize_action(ctx: AppContext, principal: Principal, action: str, interrupted: list[Lease]) -> None:
    if principal.is_admin:
        return
    scope = access_scope(ctx.db, principal)
    if action == "start":
        if not scope.has_level_anywhere("editor"):
            raise forbidden("Starting a pool device or profile needs editor access to at least one project.",
                            error="insufficient_grant")
        return
    if action in ("reset", "delete"):
        raise forbidden(f"Only an admin can {action} pool resources.", error="admin_required")
    if not interrupted:
        raise forbidden("Only an admin can shut down a resource that no lease of yours holds.",
                        error="admin_required")
    for lease in interrupted:
        if lease.owner_kind == "manual" and lease.owner_user == principal.user_id:
            continue
        if not lease.project_id or not scope.can(lease.project_id, lease.session_id, "editor"):
            raise forbidden("This would interrupt a lease you have no editor access to; ask an admin.",
                            error="insufficient_grant")


async def claim(ctx: AppContext, leases: LeaseManager, principal: Principal, resources: set[str],
                holders_of: set[str], action: str, confirmed: list[Lease]) -> list[Lease]:
    await run_in_threadpool(leases.mark_busy, resources, action)
    try:
        interrupted = await run_in_threadpool(leases.holders, holders_of)
        known = {lease.id for lease in confirmed}
        if any(lease.id not in known for lease in interrupted):
            authorize_action(ctx, principal, action, interrupted)
        return interrupted
    except BaseException:
        await run_in_threadpool(leases.clear_busy, resources)
        raise


def publish_action(ctx: AppContext, type: str, resource: str, action: str, principal: Principal,
                   broken: list[Lease], **detail: Any) -> None:
    ctx.events.publish(type, resource=resource, actor=principal.username,
                       detail={"action": action, "brokenLeases": [b.sid for b in broken], **detail})


async def run_pool(fn, *args) -> None:
    try:
        await run_in_threadpool(fn, *args)
    except PoolError as error:
        raise ApiError(502, "pool_error", str(error)) from error


def interruption_reason(action: str, principal: Principal, reason: str | None) -> str:
    text = {"shutdown": "shut down", "reset": "reset", "delete": "deleted"}[action]
    return f"{text} by {principal.username} from the UI" + (f": {reason}" if reason else "")


@router.post("/api/devices/{kind}/{index}/{action}", response_model=PoolActionResponse)
async def device_action(kind: str, index: int, action: str, request: Request, body: PoolActionRequest | None = None,
                        principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    action = check_action(action)
    body = body or PoolActionRequest()
    key, device = lookup_device(ctx, kind, index)
    if action == "start":
        authorize_action(ctx, principal, action, [])
        lease = await run_in_threadpool(leases.manual_start, key, principal, body.reason)
        publish_action(ctx, ev.DEVICE_ACTION, key, action, principal, [], sid=lease.sid)
        return {"resource": key, "action": action, "lease": leases.lease_out(lease),
                "message": f"{device.get('name')} is starting under the manual lease {lease.sid}; release it from "
                           f"the UI or with eks-harness lease release {lease.sid}"}
    confirmed = await run_in_threadpool(leases.holders, {key})
    authorize_action(ctx, principal, action, confirmed)
    require_confirmation(body, {key, str(device.get("name"))}, confirmed, action, str(device.get("name")))
    interrupted = await claim(ctx, leases, principal, {key}, {key}, action, confirmed)
    try:
        await run_in_threadpool(leases.wait_prepared, {key})
        broken = await run_in_threadpool(leases.end_leases, interrupted, "broken",
                                         interruption_reason(action, principal, body.reason), principal.username)
        pool = ctx.pools.devices
        await run_pool({"shutdown": pool.shutdown, "reset": pool.reset, "delete": pool.delete}[action], key)
    finally:
        await run_in_threadpool(leases.clear_busy, {key})
    publish_action(ctx, ev.DEVICE_ACTION, key, action, principal, broken, name=device.get("name"))
    verb = {"shutdown": "shut down", "reset": "reset", "delete": "deleted"}[action]
    return {"resource": key, "action": action, "brokenLeases": [b.sid for b in broken],
            "message": f"{device.get('name')} {verb}" + (f"; broke {', '.join(b.sid for b in broken)}" if broken else "")}


async def browser_index_action(ctx: AppContext, leases: LeaseManager, principal: Principal, index: int, action: str,
                               body: PoolActionRequest, resource: str, event_type: str) -> dict:
    profiles = set(ctx.pools.browsers.profile_ids_of(index))
    confirmed = await run_in_threadpool(leases.holders, profiles)
    authorize_action(ctx, principal, action, confirmed)
    require_confirmation(body, {resource, f"browser:{index}", f"browser {index}"}, confirmed, action,
                         f"browser {index}")
    busy = profiles | {f"browser:{index}"}
    interrupted = await claim(ctx, leases, principal, busy, profiles, action, confirmed)
    try:
        await run_in_threadpool(leases.wait_prepared, profiles)
        broken = await run_in_threadpool(leases.end_leases, interrupted, "broken",
                                         interruption_reason(action, principal, body.reason), principal.username)
        pool = ctx.pools.browsers
        if action == "shutdown":
            await run_pool(pool.stop, index, f"shut down by {principal.username}")
        else:
            await run_pool(pool.reset if action == "reset" else pool.delete, resource)
    finally:
        await run_in_threadpool(leases.clear_busy, busy)
    publish_action(ctx, event_type, resource, action, principal, broken, browser=index)
    verb = {"shutdown": "stopped", "reset": "reset (user data erased)", "delete": "deleted"}[action]
    return {"resource": resource, "action": action, "brokenLeases": [b.sid for b in broken],
            "message": f"browser {index} {verb}" + (f"; broke {', '.join(b.sid for b in broken)}" if broken else "")}


@router.post("/api/profiles/{profile_id}/{action}", response_model=PoolActionResponse)
async def profile_action(profile_id: str, action: str, request: Request, body: PoolActionRequest | None = None,
                         principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    action = check_action(action)
    body = body or PoolActionRequest()
    lookup_profile(ctx, profile_id)
    if action == "start":
        authorize_action(ctx, principal, action, [])
        lease = await run_in_threadpool(leases.manual_start, profile_id, principal, body.reason)
        publish_action(ctx, ev.PROFILE_ACTION, profile_id, action, principal, [], sid=lease.sid)
        return {"resource": profile_id, "action": action, "lease": leases.lease_out(lease),
                "message": f"{profile_id} is held by the manual lease {lease.sid}"}
    confirmed = await run_in_threadpool(leases.holders, {profile_id})
    authorize_action(ctx, principal, action, confirmed)
    require_confirmation(body, {profile_id}, confirmed, action, profile_id)
    interrupted = await claim(ctx, leases, principal, {profile_id}, {profile_id}, action, confirmed)
    try:
        await run_in_threadpool(leases.wait_prepared, {profile_id})
        contexts = await run_in_threadpool(leases.browser_contexts_for, profile_id)
        broken = await run_in_threadpool(leases.end_leases, interrupted, "broken",
                                         interruption_reason(action, principal, body.reason), principal.username)
        try:
            closed = await run_in_threadpool(ctx.pools.browsers.close_profile, profile_id, contexts)
        except PoolError as error:
            raise ApiError(502, "pool_error", str(error)) from error
    finally:
        await run_in_threadpool(leases.clear_busy, {profile_id})
    publish_action(ctx, ev.PROFILE_ACTION, profile_id, action, principal, broken, closedContexts=len(closed))
    verb = {"shutdown": "closed", "reset": "reset (its browser contexts were closed; the next lease starts clean)",
            "delete": "deleted (its browser contexts were closed; the pool recreates it on demand)"}[action]
    return {"resource": profile_id, "action": action, "brokenLeases": [b.sid for b in broken],
            "message": f"{profile_id} {verb}" + (f"; broke {', '.join(b.sid for b in broken)}" if broken else "")}


@router.post("/api/browsers/{index}/{action}", response_model=PoolActionResponse)
async def browser_action(index: int, action: str, request: Request, body: PoolActionRequest | None = None,
                         principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    leases = manager(ctx)
    if action == "stop":
        action = "shutdown"
    action = check_action(action)
    body = body or PoolActionRequest()
    if index not in ctx.pools.browsers.indices():
        raise not_found(f"No browser {index}.", error="browser_not_found")
    resource = f"browser:{index}"
    if action == "start":
        authorize_action(ctx, principal, action, [])
        if f"browser:{index}" in leases.busy:
            raise bad_request(f"browser {index} is busy", error="resource_busy")
        try:
            cdp = await run_in_threadpool(ctx.pools.browsers.ensure, index)
        except PoolError as error:
            raise ApiError(502, "pool_error", str(error)) from error
        publish_action(ctx, ev.BROWSER_STATUS, resource, action, principal, [])
        return {"resource": resource, "action": action,
                "message": f"browser {index} running" + (f" (CDP {cdp})" if principal.is_admin else "")}
    return await browser_index_action(ctx, leases, principal, index, action, body, resource, ev.BROWSER_STATUS)
