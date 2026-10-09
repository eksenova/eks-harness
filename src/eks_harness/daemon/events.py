from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from eks_harness.db import Database
from eks_harness.db.repos import events as events_repo

log = logging.getLogger("eks_harness.events")

EventFilter = Callable[["Event"], bool]

LEASE_ACQUIRED = "lease.acquired"
LEASE_QUEUED = "lease.queued"
LEASE_READY = "lease.ready"
LEASE_FAILED = "lease.failed"
LEASE_IDLE = "lease.idle"
LEASE_RELEASED = "lease.released"
LEASE_BROKEN = "lease.broken"
DEVICE_STATUS = "device.status"
DEVICE_ACTION = "device.action"
PROFILE_STATUS = "profile.status"
PROFILE_ACTION = "profile.action"
BROWSER_STATUS = "browser.status"
BACKEND_STATUS = "backend.status"
ARTIFACT_CREATED = "artifact.created"
ARTIFACT_UPDATED = "artifact.updated"
ARTIFACT_DELETED = "artifact.deleted"
NOTE_CREATED = "note.created"
SESSION_UPDATED = "session.updated"
SESSION_DELETED = "session.deleted"
PROJECT_UPDATED = "project.updated"
PROJECT_DELETED = "project.deleted"
SHARE_CREATED = "share.created"
SHARE_REVOKED = "share.revoked"
TAG_UPDATED = "tag.updated"
SETTINGS_CHANGED = "settings.changed"
DAEMON_STATUS = "daemon.status"


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    type: str
    resource: str | None = None
    lease_sid: str | None = None
    session_id: int | None = None
    project_id: str | None = None
    actor: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    persisted: bool = True

    @property
    def category(self) -> str:
        return self.type.split(".", 1)[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": self.type,
            "resource": self.resource,
            "leaseSid": self.lease_sid,
            "sessionId": self.session_id,
            "projectId": self.project_id,
            "actor": self.actor,
            "detail": self.detail,
        }

    @classmethod
    def from_row(cls, row: events_repo.EventRow) -> "Event":
        return cls(id=row.id, ts=row.ts, type=row.type, resource=row.resource, lease_sid=row.lease_sid,
                   session_id=row.session_id, project_id=row.project_id, actor=row.actor, detail=row.detail)


class Subscription:
    def __init__(self, bus: "EventBus", filter: EventFilter | None, max_queue: int,
                 loop: asyncio.AbstractEventLoop | None) -> None:
        self.bus = bus
        self.filter = filter
        self.loop = loop
        self.max_queue = max_queue
        self.overflowed = False
        self.closed = False
        self._async_queue: asyncio.Queue[Event | None] | None = asyncio.Queue(max_queue) if loop else None
        self._sync_queue: queue.Queue[Event | None] | None = None if loop else queue.Queue(max_queue)

    def accepts(self, event: Event) -> bool:
        if self.filter is None:
            return True
        try:
            return bool(self.filter(event))
        except Exception:
            log.exception("event filter failed")
            return False

    def _put_async(self, event: Event | None) -> None:
        assert self._async_queue is not None
        try:
            self._async_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True

    def deliver(self, event: Event | None) -> None:
        if self.closed and event is not None:
            return
        if self._async_queue is not None and self.loop is not None:
            try:
                if self.loop.is_closed():
                    self.closed = True
                    return
                running = None
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if running is self.loop:
                    self._put_async(event)
                else:
                    self.loop.call_soon_threadsafe(self._put_async, event)
            except RuntimeError:
                self.closed = True
        elif self._sync_queue is not None:
            try:
                self._sync_queue.put_nowait(event)
            except queue.Full:
                self.overflowed = True

    async def next(self, timeout: float | None = None) -> Event | None:
        if self._async_queue is None:
            raise RuntimeError("this subscription was created outside an event loop; use get()")
        if timeout is None:
            return await self._async_queue.get()
        try:
            return await asyncio.wait_for(self._async_queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def get(self, timeout: float | None = None) -> Event | None:
        if self._sync_queue is None:
            raise RuntimeError("this subscription belongs to an event loop; use await next()")
        try:
            return self._sync_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.bus.unsubscribe(self)
            self.deliver(None)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class EventBus:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._subscribers: list[Subscription] = []
        self._listeners: list[Callable[[Event], None]] = []

    def publish(self, type: str, *, resource: str | None = None, lease_sid: str | None = None,
                session_id: int | None = None, project_id: str | None = None, actor: str | None = None,
                detail: dict[str, Any] | None = None, persist: bool = True) -> Event:
        if persist:
            row = events_repo.insert(self.db.conn(), type, resource=resource, lease_sid=lease_sid,
                                     session_id=session_id, project_id=project_id, actor=actor, detail=detail)
            event = Event.from_row(row)
        else:
            event = Event(id=0, ts=time.time(), type=type, resource=resource, lease_sid=lease_sid,
                          session_id=session_id, project_id=project_id, actor=actor, detail=dict(detail or {}),
                          persisted=False)
        self.fan_out(event)
        return event

    def fan_out(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            listeners = list(self._listeners)
        for subscription in subscribers:
            if subscription.accepts(event):
                subscription.deliver(event)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                log.exception("event listener failed")

    def subscribe(self, filter: EventFilter | None = None, max_queue: int = 1000) -> Subscription:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        subscription = Subscription(self, filter, max_queue, loop)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def add_listener(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def history(self, filter: EventFilter | None = None, after_id: int | None = None, limit: int = 500,
                **query: Any) -> list[Event]:
        rows = events_repo.list_events(self.db.conn(), after_id=after_id, limit=limit, ascending=after_id is not None,
                                       **query)
        found = [Event.from_row(r) for r in rows]
        return [e for e in found if filter is None or filter(e)]

    def prune(self, retention_days: float) -> int:
        return events_repo.prune(self.db.conn(), time.time() - retention_days * 86400)

    async def sse(self, filter: EventFilter | None = None, last_event_id: int | None = None,
                  heartbeat_seconds: float = 15.0, is_disconnected: Callable[[], Any] | None = None,
                  replay_limit: int = 500, refresh: Callable[[], EventFilter | None] | None = None,
                  refresh_seconds: float = 10.0) -> AsyncIterator[bytes]:
        subscription = self.subscribe(filter)
        try:
            yield b"retry: 3000\n\n"
            last_sent = 0
            if last_event_id is not None:
                for event in await asyncio.to_thread(self.history, filter, last_event_id, replay_limit):
                    last_sent = max(last_sent, event.id)
                    yield format_sse(event)
            refreshed = time.monotonic()
            while True:
                if is_disconnected is not None:
                    result = is_disconnected()
                    if asyncio.iscoroutine(result):
                        result = await result
                    if result:
                        return
                if refresh is not None and time.monotonic() - refreshed >= refresh_seconds:
                    refreshed = time.monotonic()
                    current = await asyncio.to_thread(refresh)
                    if current is None:
                        return
                    subscription.filter = current
                if subscription.overflowed:
                    return
                event = await subscription.next(timeout=min(heartbeat_seconds, refresh_seconds)
                                                if refresh is not None else heartbeat_seconds)
                if subscription.closed and event is None:
                    return
                if subscription.overflowed:
                    return
                if event is None:
                    yield b": keep-alive\n\n"
                    continue
                if event.persisted and event.id <= last_sent:
                    continue
                if not subscription.accepts(event):
                    continue
                if event.persisted:
                    last_sent = event.id
                yield format_sse(event)
        finally:
            subscription.close()


def format_sse(event: Event) -> bytes:
    data = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)
    head = f"id: {event.id}\n" if event.persisted else ""
    return f"{head}event: {event.category}\ndata: {data}\n\n".encode("utf-8")
