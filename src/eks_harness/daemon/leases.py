from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eks_harness.api.errors import ApiError, bad_request, conflict, lease_released, not_found
from eks_harness.daemon import events as ev
from eks_harness.daemon.hooks import on_shutdown, on_startup
from eks_harness.db.common import now
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos import users as users_repo
from eks_harness.db.repos.leases import Lease
from eks_harness.ids import is_sid, parse_project_id
from eks_harness.pools.backends import InstanceRegistry, alive, repo_cache_root
from eks_harness.pools.base import PoolError, parse_browser_resource

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.leases")

SERVICE = "leases"
KINDS = leases_repo.KINDS
DEVICE_KINDS = ("ios", "android")
WAIT_SLICE_SECONDS = 25.0
DEVICE_STATE_FILES = ("ios-udid", "ios-sim-name", "android-serial", "android-avd")


def normalize_kind(kind: str | None) -> str | None:
    return leases_repo.normalize_kind(kind)


def reacquire_command(project: str | None, session: str | None, kind: str) -> str:
    return (f"eks-harness lease acquire --project {project or '<owner/name>'} "
            f"--session {session or '<session>'} --kind {kind}")


def browser_index(resource: str | None) -> int | None:
    if not resource or not resource.startswith("browser:"):
        return None
    try:
        return parse_browser_resource(resource)[0]
    except ValueError:
        return None


@dataclass
class PendingEvents:
    items: list[tuple[str, dict]] = field(default_factory=list)

    def add(self, type: str, **fields: Any) -> None:
        self.items.append((type, fields))


class LeaseManager:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.pools = ctx.pools
        self.host = ctx.pools.host
        self.config = ctx.config
        self.lock = self.host.lock
        self.changed = threading.Condition(self.lock)
        self.instances = InstanceRegistry(repo_cache_root(ctx.paths))
        self.last_activity = time.time()
        self._usernames: dict[int, str] = {}
        self.busy: dict[str, str] = {}
        self.preparing: dict[str, int] = {}
        self.host.binding_source = self.bindings_for_backend
        self.pools.browsers.contexts_for = self.browser_contexts_for

    def conn(self):
        return self.db.conn()

    def publish(self, pending: PendingEvents) -> None:
        for type, fields in pending.items:
            try:
                self.ctx.events.publish(type, **fields)
            except Exception:
                log.exception("could not publish %s", type)
        pending.items.clear()

    def lease_event(self, pending: PendingEvents, type: str, lease: Lease, actor: str | None = None,
                    **detail: Any) -> None:
        pending.add(type, resource=lease.resource, lease_sid=lease.sid, session_id=lease.session_id,
                    project_id=lease.project_id, actor=actor or lease.owner_instance,
                    detail={"kind": lease.kind, "state": lease.state, "phase": lease.phase,
                            "instance": lease.owner_instance, "ownerKind": lease.owner_kind,
                            "reason": lease.reason, **detail})

    def notify(self) -> None:
        with self.changed:
            self.changed.notify_all()

    def username(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in self._usernames:
            user = users_repo.get(self.conn(), user_id)
            self._usernames[user_id] = user.username if user else str(user_id)
        return self._usernames[user_id]

    def resolve_target(self, project_id: str, session_name: str) -> tuple[projects_repo.Project, sessions_repo.Session,
                                                                          bool, bool]:
        try:
            parse_project_id(project_id)
        except ValueError as error:
            raise bad_request(str(error), error="invalid_project") from error
        name = (session_name or "").strip()
        if not name:
            raise bad_request("session is required (a name such as the branch, e.g. feature/xyz)",
                              error="invalid_session")
        with self.db.transaction() as conn:
            project, project_created = projects_repo.ensure(conn, project_id)
            session, session_created = sessions_repo.ensure_in_project(conn, project.id, name)
            sessions_repo.touch(conn, session.id, project_id=project.id)
        return project, session, project_created, session_created

    def check_kind(self, kind: str | None) -> str:
        kind = normalize_kind(kind)
        if kind not in KINDS:
            raise bad_request("kind must be browser, ios or android", error="invalid_kind")
        if kind in DEVICE_KINDS:
            ok, reason = self.pools.devices.available(kind)
            if not ok:
                raise bad_request(f"no {kind} devices on this machine: {reason}", error="kind_unavailable")
        return kind

    def state_root(self) -> Path:
        return (repo_cache_root(self.ctx.paths) / "state").resolve()

    def checked_state_dir(self, value: str | None) -> str | None:
        if not value:
            return None
        root = self.state_root()
        try:
            resolved = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = None
        if resolved is None or resolved == root or not resolved.is_relative_to(root):
            raise bad_request(f"state_dir must be a harness instance state directory under {root}.",
                              error="invalid_state_dir")
        return str(resolved)

    def acquire(self, body: dict, actor: str | None = None, wait: bool = True,
                wait_seconds: float = WAIT_SLICE_SECONDS) -> dict:
        body = {**body, "state_dir": self.checked_state_dir(body.get("state_dir"))}
        kind = self.check_kind(body.get("kind"))
        instance = str(body.get("instance") or "").strip()
        if not instance:
            raise bad_request("instance is required", error="invalid_instance")
        project, session, project_created, session_created = self.resolve_target(
            str(body.get("project") or ""), str(body.get("session") or ""))
        pending = PendingEvents()
        if project_created:
            pending.add(ev.PROJECT_UPDATED, project_id=project.id, actor=actor,
                        detail={"created": True, "implicit": True})
        if session_created:
            pending.add(ev.SESSION_UPDATED, project_id=project.id, session_id=session.id, actor=actor,
                        detail={"created": True, "name": session.name, "slug": session.slug})
        with self.lock:
            self.last_activity = time.time()
            with self.db.transaction() as conn:
                existing = leases_repo.live_for_instance(conn, instance, kind)
                if existing is not None and existing.holding:
                    moved = existing.session_id != session.id or existing.project_id != project.id
                    leases_repo.update(conn, existing.id, idle_limit=None, heartbeat_at=now(), last_poll_at=now(),
                                       state="active", session_id=session.id, project_id=project.id,
                                       backend_id=body.get("backend") or existing.backend_id,
                                       owner_pid=body.get("owner_pid") or existing.owner_pid,
                                       owner_started=body.get("owner_started") or existing.owner_started,
                                       tree=body.get("tree") or existing.tree,
                                       state_dir=body.get("state_dir") or existing.state_dir,
                                       label=body.get("label") or existing.label)
                    lease = leases_repo.get(conn, existing.id)
                    if moved:
                        self.lease_event(pending, ev.LEASE_ACQUIRED, lease, actor,
                                         movedFrom=existing.session_id)
                elif existing is not None:
                    leases_repo.update(conn, existing.id, last_poll_at=now(), session_id=session.id,
                                       project_id=project.id, backend_id=body.get("backend") or existing.backend_id)
                    lease = leases_repo.get(conn, existing.id)
                else:
                    lease = leases_repo.insert(
                        conn, kind=kind, owner_instance=instance, state="queued", session_id=session.id,
                        project_id=project.id, backend_id=body.get("backend"), owner_pid=body.get("owner_pid"),
                        owner_started=body.get("owner_started"), tree=body.get("tree"),
                        state_dir=body.get("state_dir"), label=body.get("label"),
                        previous_sid=body.get("previous_sid"), meta=body.get("meta") or {})
                    self.lease_event(pending, ev.LEASE_QUEUED, lease, actor)
            self.publish(pending)
            self.process_queue()
            lease_id = lease.id
        return self.wait_for(lease_id, wait, wait_seconds)

    def wait_for(self, lease_id: str, wait: bool, wait_seconds: float) -> dict:
        deadline = time.time() + max(0.0, wait_seconds)
        with self.changed:
            while True:
                lease = leases_repo.get(self.conn(), lease_id)
                if lease is None:
                    raise not_found("the lease disappeared", error="lease_not_found")
                status = self.acquire_status(lease)
                remaining = deadline - time.time()
                if not wait or status in ("granted", "failed") or lease.ended or remaining <= 0:
                    return self.acquire_response(lease, status)
                self.changed.wait(min(remaining, 1.0))

    def acquire_status(self, lease: Lease) -> str:
        if lease.state == "queued":
            return "queued"
        if lease.ended:
            return "failed"
        if lease.phase == "failed":
            return "failed"
        if lease.phase == "ready":
            return "granted"
        return "preparing"

    def acquire_response(self, lease: Lease, status: str) -> dict:
        out = self.lease_out(lease)
        response: dict[str, Any] = {"status": status, "sid": lease.sid, "lease": out, "resource": lease.resource,
                                    "urls": out.get("urls"), "project": None, "session": None}
        if lease.project_id:
            project = projects_repo.get(self.conn(), lease.project_id)
            if project:
                response["project"] = {"id": project.id, "owner": project.owner, "name": project.name,
                                       "title": project.title, "url": self.ctx.links.project(project.id)}
        if lease.session_id is not None:
            session = sessions_repo.get(self.conn(), lease.session_id)
            if session:
                response["session"] = {"id": session.id, "projectId": lease.project_id, "name": session.name,
                                       "slug": session.slug,
                                       "url": self.ctx.links.session(lease.project_id, session.slug),
                                       "sharedUrl": self.ctx.links.shared_session(session.slug)}
        if status == "failed":
            response["error"] = lease.error or lease.reason
        if lease.state == "queued":
            queue = leases_repo.queue(self.conn(), lease.kind)
            ids = [w.id for w in queue]
            response["position"] = ids.index(lease.id) + 1 if lease.id in ids else None
            response["waiting"] = len(queue)
            holders = leases_repo.holding(self.conn(), lease.kind)
            stamp = time.time()
            response["holders"] = [{"sid": h.sid, "instance": h.owner_instance, "resource": h.resource,
                                    "idleSeconds": round(stamp - (h.heartbeat_at or stamp)),
                                    "session": h.session_name} for h in holders]
        return response

    def allocate(self, kind: str, taken: set[str]) -> str | None:
        if kind == "browser":
            return self.pools.browsers.allocate(taken)
        return self.pools.devices.allocate(kind, taken)

    def busy_resources(self) -> set[str]:
        found = {key for key, device in self.host.devices.items() if device.get("busy")}
        return found | set(self.busy)

    def mark_busy(self, resources: set[str], action: str) -> None:
        with self.lock:
            clash = (resources & self.busy_resources())
            if clash:
                raise conflict(f"{', '.join(sorted(clash))} is busy with another action.", error="resource_busy")
            for resource in resources:
                self.busy[resource] = action
                device = self.host.devices.get(resource)
                if device is not None:
                    device["busy"] = action

    def clear_busy(self, resources: set[str]) -> None:
        with self.lock:
            for resource in resources:
                self.busy.pop(resource, None)
                device = self.host.devices.get(resource)
                if device is not None:
                    device.pop("busy", None)
            self.host.save()
        self.notify()
        self.process_queue()

    def process_queue(self) -> None:
        pending = PendingEvents()
        started: list[Lease] = []
        with self.lock:
            with self.db.transaction() as conn:
                taken = leases_repo.taken_resources(conn) | self.busy_resources()
                blocked: set[str] = set()
                for waiter in leases_repo.queue(conn):
                    if waiter.kind in blocked:
                        continue
                    try:
                        resource = self.allocate(waiter.kind, taken)
                    except Exception as error:
                        log.exception("allocation for %s failed", waiter.kind)
                        leases_repo.update(conn, waiter.id, error=str(error)[:500])
                        blocked.add(waiter.kind)
                        continue
                    if not resource:
                        blocked.add(waiter.kind)
                        continue
                    taken.add(resource)
                    lease = leases_repo.activate(conn, waiter.id, resource)
                    started.append(lease)
                    self.host.log(f"lease {resource} -> {lease.owner_instance} ({lease.sid})")
                    self.lease_event(pending, ev.LEASE_ACQUIRED, lease)
            self.publish(pending)
        for lease in started:
            self.host.spawn(self.prepare, lease.id)
        if started:
            self.notify()

    def prepare(self, lease_id: str) -> None:
        lease = leases_repo.get(self.conn(), lease_id)
        if lease is None or not lease.holding or not lease.resource:
            return
        pending = PendingEvents()
        resource_key = lease.resource
        with self.changed:
            while resource_key in self.busy_resources():
                current = leases_repo.get(self.conn(), lease_id)
                if current is None or not current.holding:
                    return
                self.changed.wait(1.0)
            self.preparing[resource_key] = self.preparing.get(resource_key, 0) + 1
        try:
            if lease.kind == "browser":
                index = browser_index(lease.resource)
                if index is None:
                    raise PoolError(f"not a browser profile: {lease.resource}")
                self.pools.browsers.ensure(index)
            else:
                resource = lease.resource
                deadline = time.time() + 120
                while (self.host.devices.get(resource) or {}).get("status") == "stopping" and time.time() < deadline:
                    time.sleep(1)
                while self.pools.devices.running_count(exclude=resource) >= int(self.config["devices.maxRunning"]) \
                        and time.time() < deadline:
                    time.sleep(1)
                self.pools.devices.boot(resource)
                if lease.owner_kind != "manual":
                    self.pools.devices.clean_app(resource)
            with self.lock:
                with self.db.transaction() as conn:
                    current = leases_repo.get(conn, lease_id)
                    if current is None or not current.holding:
                        return
                    lease = leases_repo.update(conn, lease_id, phase="ready", heartbeat_at=now(), error=None)
                self.lease_event(pending, ev.LEASE_READY, lease)
                self.publish(pending)
            self.host.log(f"ready: {lease.resource} -> {lease.owner_instance}")
        except Exception as error:
            self.host.log(f"could not prepare {lease.resource}: {error}")
            with self.lock:
                with self.db.transaction() as conn:
                    current = leases_repo.get(conn, lease_id)
                    if current is None or not current.holding:
                        return
                    lease = leases_repo.update(conn, lease_id, phase="failed", error=str(error)[:500])
                self.lease_event(pending, ev.LEASE_FAILED, lease, error=str(error)[:500])
                self.publish(pending)
        finally:
            with self.lock:
                remaining = self.preparing.get(resource_key, 1) - 1
                if remaining > 0:
                    self.preparing[resource_key] = remaining
                else:
                    self.preparing.pop(resource_key, None)
            self.notify()

    def wait_prepared(self, resources: set[str], timeout: float = 600.0) -> None:
        deadline = time.time() + timeout
        with self.changed:
            while resources & set(self.preparing) and time.time() < deadline:
                self.changed.wait(min(1.0, max(0.0, deadline - time.time())))

    def resume_preparing(self) -> None:
        for lease in leases_repo.holding(self.conn()):
            if lease.phase == "preparing":
                self.host.spawn(self.prepare, lease.id)

    def get_lease(self, sid: str) -> Lease:
        lease = leases_repo.get_by_sid(self.conn(), sid) if is_sid(sid) else None
        if lease is None:
            lease = leases_repo.get(self.conn(), sid)
        if lease is None:
            raise not_found(f"No lease with sid {sid}.", error="lease_not_found")
        return lease

    def released_error(self, lease: Lease) -> ApiError:
        project = lease.project_id
        return lease_released(project, lease.session_name or lease.session_slug, lease.kind, lease.sid,
                              lease.state if lease.ended else "released")

    def require_holding(self, sid: str) -> Lease:
        lease = self.get_lease(sid)
        if lease.ended:
            raise self.released_error(lease)
        if lease.state == "queued":
            raise conflict(f"The lease {sid} is still waiting in the queue; it holds no {lease.kind} yet.",
                           error="lease_queued", sid=sid)
        return lease

    def ensure_browser(self, sid: str) -> dict:
        lease = self.require_holding(sid)
        if lease.kind != "browser" or not lease.resource:
            raise conflict(f"The lease {sid} holds a {lease.kind}, not a browser profile.", error="not_a_browser_lease")
        if lease.phase != "ready":
            raise conflict(f"The browser of lease {sid} is not ready yet (phase {lease.phase}).",
                           error="lease_not_ready")
        index, profile = parse_browser_resource(lease.resource)
        was_alive = self.pools.browsers.alive(index)
        try:
            cdp = self.pools.browsers.ensure(index)
        except PoolError as error:
            raise ApiError(409, "browser_error", str(error)) from error
        if not was_alive:
            self.host.log(f"browser {index} was not running for lease {sid}; started it again")
        self.heartbeat(sid)
        return {"sid": sid, "resource": lease.resource, "browser": index, "profile": profile, "cdp": cdp,
                "restarted": not was_alive}

    def heartbeat(self, sid: str, meta: dict | None = None) -> Lease:
        lease = self.get_lease(sid)
        if lease.ended:
            raise self.released_error(lease)
        with self.lock:
            self.last_activity = time.time()
            with self.db.transaction() as conn:
                if lease.state == "queued":
                    leases_repo.poll(conn, lease.id)
                else:
                    leases_repo.heartbeat(conn, lease.id)
                if meta:
                    current = leases_repo.get(conn, lease.id)
                    leases_repo.update(conn, lease.id, meta={**(current.meta or {}), **meta})
                lease = leases_repo.get(conn, lease.id)
        if lease.kind == "browser" and lease.phase == "ready":
            index = browser_index(lease.resource)
            if index is not None and not self.pools.browsers.alive(index):
                self.host.spawn(self.relaunch_browser, lease.id, index)
        return lease

    def relaunch_browser(self, lease_id: str, index: int) -> None:
        try:
            self.pools.browsers.ensure(index)
        except Exception as error:
            self.host.log(f"browser {index} could not be relaunched for lease {lease_id}: {error}")

    def update_meta(self, sid: str, meta: dict) -> Lease:
        lease = self.require_holding(sid)
        with self.lock:
            with self.db.transaction() as conn:
                return leases_repo.update(conn, lease.id, meta={**(lease.meta or {}), **meta})

    def heartbeat_instance(self, instance: str, kind: str | None = None) -> list[str]:
        kind = normalize_kind(kind) if kind else None
        touched: list[str] = []
        with self.lock:
            self.last_activity = time.time()
            with self.db.transaction() as conn:
                for lease in leases_repo.list_leases(conn, states=leases_repo.LIVE_STATES, instance=instance,
                                                     kind=kind):
                    if lease.state == "queued":
                        leases_repo.poll(conn, lease.id)
                    else:
                        leases_repo.heartbeat(conn, lease.id)
                        touched.append(lease.kind)
        return touched

    def idle(self, sid: str, grace: float | None = None) -> tuple[Lease, float]:
        lease = self.require_holding(sid)
        grace = float(grace if grace is not None else self.config["lease.agentStopGraceSeconds"])
        pending = PendingEvents()
        with self.lock:
            with self.db.transaction() as conn:
                lease = self._set_idle(conn, lease, grace)
            self.lease_event(pending, ev.LEASE_IDLE, lease, grace=grace)
            self.publish(pending)
        return lease, grace

    def _set_idle(self, conn, lease: Lease, grace: float) -> Lease:
        idle_for = max(0.0, time.time() - (lease.heartbeat_at or time.time()))
        return leases_repo.update(conn, lease.id, idle_limit=idle_for + grace, state="idle")

    def idle_instance(self, instance: str, grace: float | None = None) -> tuple[list[str], float]:
        grace = float(grace if grace is not None else self.config["lease.agentStopGraceSeconds"])
        pending = PendingEvents()
        sids: list[str] = []
        with self.lock:
            with self.db.transaction() as conn:
                for lease in leases_repo.list_leases(conn, states=leases_repo.HOLDING_STATES, instance=instance):
                    updated = self._set_idle(conn, lease, grace)
                    sids.append(updated.sid)
                    self.lease_event(pending, ev.LEASE_IDLE, updated, grace=grace)
            self.publish(pending)
        return sids, grace

    def end_leases(self, targets: list[Lease], state: str, reason: str, actor: str | None = None) -> list[Lease]:
        pending = PendingEvents()
        ended: list[Lease] = []
        with self.lock:
            with self.db.transaction() as conn:
                for target in targets:
                    current = leases_repo.get(conn, target.id)
                    if current is None or current.ended:
                        continue
                    was_holding = current.holding
                    lease = leases_repo.end(conn, current.id, state, reason)
                    ended.append(lease)
                    self.lease_event(pending, ev.LEASE_BROKEN if state == "broken" else ev.LEASE_RELEASED, lease,
                                     actor, heldResource=was_holding)
            self.publish(pending)
        for lease in ended:
            if lease.resource and lease.acquired_at is not None:
                self.cleanup(lease, reason)
        self.process_queue()
        self.notify()
        return ended

    def cleanup(self, lease: Lease, reason: str) -> None:
        self.host.log(f"released: {lease.resource} <- {lease.owner_instance} ({reason})")
        try:
            if lease.kind == "browser":
                stop_control = getattr(self.pools.browsers, "stop_control_server", None)
                if stop_control is not None and lease.owner_kind != "manual":
                    stop_control(lease.owner_instance)
                index = browser_index(lease.resource)
                with self.lock:
                    record = self.host.browsers.get(str(index)) if index is not None else None
                    if record is not None:
                        record["last_used"] = time.time()
                        self.host.save()
            else:
                resource = lease.resource
                if resource in self.host.devices:
                    self.discard_recording(resource, lease)
                    if lease.owner_kind != "manual" and lease.phase in ("ready", "failed") \
                            and not (self.host.devices.get(resource) or {}).get("busy"):
                        self.pools.devices.clean_app(resource)
                    with self.lock:
                        device = self.host.devices.get(resource)
                        if device is not None:
                            device["last_used"] = time.time()
                            self.host.save()
                folder = Path(lease.state_dir).resolve() if lease.state_dir else None
                if folder is not None and folder != self.state_root() and folder.is_relative_to(self.state_root()):
                    for name in DEVICE_STATE_FILES:
                        (folder / name).unlink(missing_ok=True)
        except Exception:
            log.exception("cleanup of %s failed", lease.resource)

    def discard_recording(self, resource: str, lease: Lease) -> None:
        device = self.host.devices.get(resource) or {}
        owner = device.get("recordingSid")
        if not self.pools.devices.is_recording(resource) or (owner and owner != lease.sid):
            return
        dest = (device.get("recording") or {}).get("dest") if isinstance(device.get("recording"), dict) else None
        try:
            info = self.pools.devices.record_stop(resource)
            dest = info.get("path") or dest
            self.host.log(f"{resource}: the recording of lease {lease.sid} was discarded on release")
        except Exception as error:
            self.host.log(f"{resource}: the recording of lease {lease.sid} could not be stopped: {error}")
        if dest:
            discard_capture(Path(dest))
        with self.lock:
            (self.host.devices.get(resource) or {}).pop("recordingSid", None)
            self.host.save()
        if self.ctx.has_service("live"):
            self.ctx.service("live").resume(resource)

    def release(self, sid: str, reason: str | None = None, actor: str | None = None) -> list[Lease]:
        lease = self.get_lease(sid)
        if lease.ended:
            return []
        return self.end_leases([lease], "released", reason or "requested", actor)

    def break_lease(self, sid: str, reason: str | None = None, actor: str | None = None) -> list[Lease]:
        lease = self.get_lease(sid)
        if lease.ended:
            return []
        return self.end_leases([lease], "broken", reason or "broken by hand", actor)

    def release_instance(self, instance: str, kind: str | None = None, reason: str = "requested",
                         actor: str | None = None) -> list[Lease]:
        kind = normalize_kind(kind) if kind else None
        targets = leases_repo.list_leases(self.conn(), states=leases_repo.LIVE_STATES, instance=instance, kind=kind)
        return self.end_leases(targets, "released", reason, actor)

    def instance_ended(self, instance: str, reason: str | None = None, actor: str | None = None) -> list[Lease]:
        ended = self.release_instance(instance, None, reason or "harness instance ended", actor)
        self.pools.backends.instance_ended(instance)
        return ended

    def resume(self, body: dict, actor: str | None = None, wait: bool = True,
               wait_seconds: float = WAIT_SLICE_SECONDS) -> dict:
        body = {**body, "state_dir": self.checked_state_dir(body.get("state_dir"))}
        old = self.get_lease(str(body.get("sid") or ""))
        if old.holding or old.state == "queued":
            return self.wait_for(old.id, wait, wait_seconds)
        if old.session_id is None or not old.project_id:
            raise conflict(f"The lease {old.sid} belonged to no session; acquire a new one with --project and "
                           f"--session.", error="not_resumable")
        request = {
            "kind": old.kind, "project": old.project_id, "session": old.session_slug,
            "instance": body.get("instance") or old.owner_instance, "backend": body.get("backend") or old.backend_id,
            "owner_pid": body.get("owner_pid") if body.get("owner_pid") is not None else old.owner_pid,
            "owner_started": body.get("owner_started") or old.owner_started,
            "tree": body.get("tree") or old.tree, "state_dir": body.get("state_dir") or old.state_dir,
            "label": body.get("label") or old.label, "previous_sid": old.sid,
        }
        if old.owner_kind == "manual":
            raise conflict(f"The lease {old.sid} was a manual lease; start the device again instead.",
                           error="not_resumable")
        return self.acquire(request, actor, wait, wait_seconds)

    def stale_reason(self, lease: Lease, stamp: float | None = None) -> str | None:
        stamp = stamp or time.time()
        if lease.phase == "failed":
            return "preparation failed"
        if lease.owner_kind == "manual":
            return None
        if lease.owner_pid and not alive(lease.owner_pid, lease.owner_started):
            return "the owning session has ended"
        if lease.owner_instance.startswith("session:") and self.instances.available() \
                and self.instances.find(lease.owner_instance) is None:
            return "its harness instance no longer exists"
        idle = stamp - (lease.heartbeat_at or lease.acquired_at or stamp)
        limit = lease.idle_limit or float(self.config["lease.idleSeconds"])
        if idle > limit:
            return f"no activity for {round(idle)}s"
        return None

    def expire_queue(self) -> list[Lease]:
        timeout = float(self.config["lease.queueTimeoutSeconds"])
        stamp = time.time()
        stale = [w for w in leases_repo.queue(self.conn())
                 if stamp - (w.last_poll_at or w.queued_at) >= timeout]
        return self.end_leases(stale, "released", f"stopped waiting (no poll for {round(timeout)}s)") if stale else []

    def tick(self) -> dict:
        report: dict[str, Any] = {"expiredWaiters": [], "stale": [], "idleDevices": [], "idleBrowsers": []}
        report["expiredWaiters"] = [lease.sid for lease in self.expire_queue()]
        stamp = time.time()
        stale = [(lease, reason) for lease in leases_repo.holding(self.conn())
                 if (reason := self.stale_reason(lease, stamp))]
        for lease, reason in stale:
            ended = self.end_leases([lease], "released", f"stale: {reason}")
            report["stale"] += [{"sid": item.sid, "reason": reason} for item in ended]
        self.pools.backends.tick()
        self.process_queue()
        with self.lock:
            stamp = time.time()
            taken = leases_repo.taken_resources(self.conn()) | self.busy_resources()
            device_idle = float(self.config["devices.idleSeconds"])
            idle_devices = [k for k, d in self.host.devices.items()
                            if d.get("status") == "on" and k not in taken and not d.get("busy")
                            and stamp - float(d.get("last_used") or 0) > device_idle]
            browser_idle = float(self.config["browser.idleSeconds"])
            idle_browsers = [int(i) for i, record in self.host.browsers.items()
                             if not any(r.startswith(f"browser:{i}:") for r in taken)
                             and stamp - float(record.get("last_used") or stamp) > browser_idle]
            if leases_repo.list_leases(self.conn(), states=leases_repo.LIVE_STATES, limit=1) or any(
                    b.get("status") in ("running", "preparing", "starting") for b in self.host.backends.values()):
                self.last_activity = stamp
        for key in idle_devices:
            if not self.claim_idle({key}, "idle shutdown", device=key):
                continue
            self.host.log(f"{self.host.devices[key].get('name', key)} idle for {round(device_idle)}s; shutting it down")
            try:
                self.pools.devices.shutdown(key)
                report["idleDevices"].append(key)
            except Exception:
                log.exception("idle shutdown of %s failed", key)
            finally:
                self.clear_busy({key})
        for index in idle_browsers:
            claimed = set(self.pools.browsers.profile_ids_of(index)) | {f"browser:{index}"}
            if not self.claim_idle(claimed, "idle stop", browser=index):
                continue
            try:
                self.pools.browsers.stop(index, f"no profiles for {round(browser_idle)}s")
                report["idleBrowsers"].append(index)
            except Exception:
                log.exception("idle stop of browser %s failed", index)
            finally:
                self.clear_busy(claimed)
        return report

    def claim_idle(self, resources: set[str], action: str, device: str | None = None,
                   browser: int | None = None) -> bool:
        with self.lock:
            taken = leases_repo.taken_resources(self.conn()) | self.busy_resources()
            if resources & taken or set(self.preparing) & resources:
                return False
            if device is not None:
                record = self.host.devices.get(device)
                if record is None or record.get("status") != "on":
                    return False
            if browser is not None and str(browser) not in self.host.browsers:
                return False
            for resource in resources:
                self.busy[resource] = action
                record = self.host.devices.get(resource)
                if record is not None:
                    record["busy"] = action
            self.host.save()
        return True

    def nothing_running(self) -> bool:
        if leases_repo.list_leases(self.conn(), states=leases_repo.LIVE_STATES, limit=1):
            return False
        if self.host.browsers:
            return False
        if any(d.get("status") in ("on", "booting") for d in self.host.devices.values()):
            return False
        return not any(b.get("status") in ("running", "preparing", "starting") for b in self.host.backends.values())

    def bindings_for_backend(self, backend_id: str) -> list[str]:
        found = []
        for lease in leases_repo.list_leases(self.conn(), states=leases_repo.LIVE_STATES, backend_id=backend_id):
            if lease.state == "queued":
                found.append(f"waiting {lease.kind}")
            else:
                found.append(f"lease {lease.resource}")
        return found

    def browser_contexts_for(self, resource: str) -> list[str]:
        lease = leases_repo.holder_of(self.conn(), resource)
        if lease is None:
            return []
        value = (lease.meta or {}).get("browserContextIds") or []
        return [str(v) for v in value] if isinstance(value, list) else []

    def holders(self, resources: set[str]) -> list[Lease]:
        return [lease for lease in leases_repo.holding(self.conn()) if lease.resource in resources]

    def profile_released_at(self, resource: str) -> float | None:
        row = self.conn().execute("SELECT MAX(released_at) FROM leases WHERE resource = ?", (resource,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def queue_for(self, kind: str) -> list[Lease]:
        return leases_repo.queue(self.conn(), kind)

    def manual_start(self, resource: str, principal, reason: str | None = None) -> Lease:
        kind = "browser" if resource.startswith("browser:") else resource.split(":", 1)[0]
        kind = self.check_kind(kind)
        pending = PendingEvents()
        with self.lock:
            holder = leases_repo.holder_of(self.conn(), resource)
            if holder is not None:
                if holder.owner_kind == "manual" and holder.owner_user == principal.user_id:
                    return holder
                raise conflict(f"{resource} is held by {holder.owner_instance} (sid {holder.sid}); it is not "
                               f"taken from a running lease.", error="resource_busy", sid=holder.sid)
            if resource in self.busy_resources():
                raise conflict(f"{resource} is busy with another action.", error="resource_busy")
            if kind in DEVICE_KINDS:
                device = self.host.devices.get(resource)
                if device is None or device.get("retired"):
                    raise not_found(f"No device {resource} in the pool.", error="device_not_found")
                if device.get("status") not in ("on", "booting"):
                    running = self.pools.devices.running_count(exclude=resource)
                    if running >= int(self.config["devices.maxRunning"]):
                        taken = leases_repo.taken_resources(self.conn())
                        idle = [k for k, d in self.host.devices.items()
                                if d.get("status") == "on" and k not in taken and k != resource]
                        if not idle:
                            raise conflict(f"devices.maxRunning ({self.config['devices.maxRunning']}) devices are "
                                           f"running and all are in use; nothing was stopped.",
                                           error="pool_full")
                        victim = sorted(idle, key=lambda k: self.host.devices[k].get("last_used", 0))[0]
                        self.host.log(f"{self.host.devices[victim].get('name', victim)} is idle; shutting it down "
                                      f"to start {resource}")
                        self.host.devices[victim]["status"] = "stopping"
                        self.host.spawn(self.pools.devices.shutdown, victim)
            else:
                if resource not in self.pools.browsers.profile_ids():
                    raise not_found(f"No browser profile {resource}.", error="profile_not_found")
            with self.db.transaction() as conn:
                lease = leases_repo.insert(
                    conn, kind=kind, owner_instance=f"manual:{principal.username}", state="active",
                    resource=resource, owner_kind="manual", owner_user=principal.user_id, phase="preparing",
                    reason=reason, label=f"started by {principal.username}", meta={"manual": True})
            self.lease_event(pending, ev.LEASE_ACQUIRED, lease, principal.username, manual=True)
            self.publish(pending)
        self.host.spawn(self.prepare, lease.id)
        self.notify()
        return lease

    def lease_out(self, lease: Lease, stamp: float | None = None, queue_position: int | None = None) -> dict:
        stamp = stamp or time.time()
        out: dict[str, Any] = {
            "id": lease.id, "sid": lease.sid, "kind": lease.kind, "resource": lease.resource, "state": lease.state,
            "phase": lease.phase, "ownerInstance": lease.owner_instance, "ownerKind": lease.owner_kind,
            "ownerUser": lease.owner_user, "ownerUsername": self.username(lease.owner_user),
            "acquiredAt": iso(lease.acquired_at), "heartbeatAt": iso(lease.heartbeat_at),
            "idleSeconds": round(stamp - lease.heartbeat_at, 1) if lease.heartbeat_at and lease.holding else None,
            "sessionId": lease.session_id, "projectId": lease.project_id, "sessionSlug": lease.session_slug,
            "sessionName": lease.session_name, "backendId": lease.backend_id, "reason": lease.reason,
            "error": lease.error, "queuedAt": iso(lease.queued_at), "releasedAt": iso(lease.released_at),
            "idleLimit": lease.idle_limit, "label": lease.label, "tree": lease.tree, "stateDir": lease.state_dir,
            "previousSid": lease.previous_sid, "stale": self.stale_reason(lease, stamp) if lease.holding else None,
            "queuePosition": queue_position, "cdp": None, "browser": None, "profile": None, "device": None,
            "urls": None, "meta": lease.meta or {},
        }
        if lease.kind == "browser" and lease.resource:
            try:
                index, profile = parse_browser_resource(lease.resource)
            except ValueError:
                index, profile = None, None
            out["browser"], out["profile"] = index, profile
            record = self.host.browsers.get(str(index)) or {}
            out["cdp"] = record.get("cdp") if lease.holding else None
        elif lease.resource in self.host.devices:
            device = self.host.devices[lease.resource]
            out["device"] = {"key": lease.resource, "kind": device.get("kind"), "index": device.get("index"),
                             "name": device.get("name"), "udid": device.get("udid"), "serial": device.get("serial"),
                             "port": device.get("port"), "status": device.get("status")}
        if lease.project_id:
            out["urls"] = {"session": self.ctx.links.session(lease.project_id, lease.session_slug),
                           "ui": self.ctx.links.ui(), "project": self.ctx.links.project(lease.project_id)}
        return out

    def lease_brief(self, lease: Lease, stamp: float | None = None) -> dict:
        full = self.lease_out(lease, stamp)
        keys = ("id", "sid", "kind", "resource", "state", "phase", "ownerInstance", "ownerKind", "ownerUser",
                "ownerUsername", "acquiredAt", "heartbeatAt", "idleSeconds")
        return {k: full[k] for k in keys}

    def list_out(self, states: tuple[str, ...] = leases_repo.LIVE_STATES, **filters: Any) -> tuple[list[dict], list[dict]]:
        stamp = time.time()
        leases = leases_repo.list_leases(self.conn(), states=states, **filters)
        holding = [self.lease_out(lease, stamp) for lease in leases if lease.state != "queued"]
        positions: dict[str, int] = {}
        queued: list[dict] = []
        for lease in [lease for lease in leases if lease.state == "queued"]:
            positions[lease.kind] = positions.get(lease.kind, 0) + 1
            queued.append(self.lease_out(lease, stamp, positions[lease.kind]))
        return holding, queued

    def sweep(self) -> dict:
        if self.pools.fake:
            return {"skipped": "fake pools: the sweep never runs"}
        report: dict[str, Any] = {}
        self.pools.devices.refresh()
        for area, pool in (("devices", self.pools.devices), ("browsers", self.pools.browsers)):
            try:
                report.update(pool.sweep())
            except Exception as error:
                log.exception("sweep of %s failed", area)
                report[f"{area}Error"] = str(error)
        return report


def discard_capture(path: Path) -> None:
    folder = path.parent
    if folder.name.startswith("capture-"):
        shutil.rmtree(folder, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def manager(ctx: "AppContext") -> LeaseManager:
    if not ctx.has_service(SERVICE):
        ctx.register_service(SERVICE, LeaseManager(ctx))
    return ctx.service(SERVICE)


@on_startup(order=20)
def _start(ctx: "AppContext") -> None:
    leases = manager(ctx)
    ctx.pools.backends.reconcile()
    leases.resume_preparing()
    leases.process_queue()


@on_shutdown(order=80)
def _stop(ctx: "AppContext") -> None:
    if ctx.has_service(SERVICE):
        ctx.service(SERVICE).notify()
