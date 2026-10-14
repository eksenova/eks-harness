from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from pydantic import JsonValue, ValidationError

from eks_harness.daemon.events import Event, EventBus
from eks_harness.engine.journal import Journal
from eks_harness.engine.model import TERMINAL_STATUSES, NodeEventRecord, RunInfo

RUN_EVENT = "run.event"
RUN_CREATED = "run.created"
RUN_STARTED = "run.started"
RUN_PAUSED = "run.paused"
RUN_RESUMED = "run.resumed"
RUN_FINISHED = "run.finished"
RUN_PROGRESS = "run.progress"
ACTIVITY_TYPES = frozenset({RUN_CREATED, RUN_STARTED, RUN_PAUSED, RUN_RESUMED, RUN_FINISHED})
RESOURCE_PREFIX = "run:"
HEARTBEAT_SECONDS = 15.0
REPLAY_PAGE = 500

_POLL_SECONDS = 0.25
_TERMINAL_EVENTS = frozenset(f"run.{status}" for status in TERMINAL_STATUSES)


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
    return text


def run_resource(run_id: str) -> str:
    return f"{RESOURCE_PREFIX}{run_id}"


async def _resolve(value: Any) -> bool:
    if inspect.isawaitable(value):
        value = await value
    return bool(value)


class RunEvents:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def journaled(self, info: RunInfo, record: NodeEventRecord) -> None:
        self.bus.publish(RUN_EVENT, resource=run_resource(info.id), session_id=info.session_id,
                         project_id=info.project or None, actor=info.actor,
                         detail=record.model_dump(mode="json", by_alias=True), persist=False)

    def activity(self, info: RunInfo, type: str, detail: Mapping[str, JsonValue]) -> None:
        if type not in ACTIVITY_TYPES:
            raise ValueError(f"{type!r} is not a run activity type; expected one of {sorted(ACTIVITY_TYPES)}")
        payload: dict[str, Any] = dict(detail)
        payload["run"] = info.id
        payload["plan"] = {"kind": info.plan_kind, "name": info.plan_name}
        payload.setdefault("status", None)
        payload.setdefault("reason", None)
        self.bus.publish(type, resource=run_resource(info.id), session_id=info.session_id,
                         project_id=info.project or None, actor=info.actor, detail=payload, persist=True)

    def progress(self, info: RunInfo, detail: Mapping[str, JsonValue]) -> None:
        self.bus.publish(RUN_PROGRESS, resource=run_resource(info.id), session_id=info.session_id,
                         project_id=info.project or None, actor=info.actor, detail=dict(detail), persist=False)

    def stream(self, journal: Journal, run_id: str, *, last_event_id: int | None = None,
               is_disconnected: Callable[[], Any] | None = None, heartbeat: float = HEARTBEAT_SECONDS,
               finished: Callable[[], bool] | None = None) -> AsyncIterator[bytes]:
        bus = self.bus
        resource = run_resource(run_id)

        def accept(event: Event) -> bool:
            return event.resource == resource and event.type in (RUN_EVENT, RUN_PROGRESS)

        async def disconnected() -> bool:
            return is_disconnected is not None and await _resolve(is_disconnected())

        def stored_status() -> str | None:
            row = journal.run(run_id)
            return None if row is None else row.status

        async def frames() -> AsyncIterator[bytes]:
            subscription = bus.subscribe(accept)
            try:
                yield b"retry: 3000\n\n"
                last_sent = last_event_id or 0
                terminal_seen = False
                while True:
                    page = await asyncio.to_thread(journal.events, run_id, after=last_sent, limit=REPLAY_PAGE)
                    for record in page:
                        last_sent = record.id
                        terminal_seen = terminal_seen or record.type in _TERMINAL_EVENTS
                        yield format_run_sse(record)
                    if len(page) < REPLAY_PAGE or await disconnected():
                        break
                if finished is None and not terminal_seen:
                    terminal_seen = await asyncio.to_thread(stored_status) in TERMINAL_STATUSES
                written = time.monotonic()
                while True:
                    if await disconnected() or subscription.overflowed:
                        return
                    if finished() if finished is not None else terminal_seen:
                        tail = await asyncio.to_thread(journal.events, run_id, after=last_sent, limit=REPLAY_PAGE)
                        if tail:
                            for record in tail:
                                last_sent = record.id
                                yield format_run_sse(record)
                            continue
                        status = await asyncio.to_thread(stored_status)
                        if status is None:
                            return
                        if status in TERMINAL_STATUSES:
                            yield f"event: end\ndata: {status}\n\n".encode()
                            return
                        terminal_seen = False
                    idle = time.monotonic() - written
                    event = await subscription.next(timeout=max(min(_POLL_SECONDS, heartbeat - idle), 0.0))
                    if subscription.overflowed:
                        return
                    if event is None:
                        if subscription.closed:
                            return
                        if time.monotonic() - written >= heartbeat:
                            written = time.monotonic()
                            yield b": keep-alive\n\n"
                        continue
                    if event.type == RUN_PROGRESS:
                        written = time.monotonic()
                        yield f"event: progress\ndata: {_json(event.detail)}\n\n".encode()
                        continue
                    record = record_of(event)
                    if record is None or record.id <= last_sent:
                        continue
                    last_sent = record.id
                    terminal_seen = terminal_seen or record.type in _TERMINAL_EVENTS
                    written = time.monotonic()
                    yield format_run_sse(record)
            finally:
                subscription.close()

        return frames()


def record_of(event: Event) -> NodeEventRecord | None:
    if event.type != RUN_EVENT or event.resource is None or not event.resource.startswith(RESOURCE_PREFIX):
        return None
    try:
        return NodeEventRecord.model_validate(event.detail)
    except ValidationError:
        return None


def format_run_sse(record: NodeEventRecord) -> bytes:
    prefix = record.type.split(".", 1)[0]
    data = _json(record.model_dump(mode="json", by_alias=True))
    return f"id: {record.id}\nevent: {prefix}\ndata: {data}\n\n".encode()


__all__ = [
    "ACTIVITY_TYPES",
    "HEARTBEAT_SECONDS",
    "REPLAY_PAGE",
    "RESOURCE_PREFIX",
    "RUN_CREATED",
    "RUN_EVENT",
    "RUN_FINISHED",
    "RUN_PAUSED",
    "RUN_PROGRESS",
    "RUN_RESUMED",
    "RUN_STARTED",
    "RunEvents",
    "format_run_sse",
    "record_of",
    "run_resource",
]
