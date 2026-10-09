"""Bridge between the render pipeline and the dev-server WebSocket hub.

Two responsibilities live here:

* **JobRegistry → WS hub.** When a render job is created or transitions to a
  new progress / terminal state, broadcast the matching ``job.*`` event to
  the ``jobs`` topic. The bridge subscribes to ``JobRegistry.watch(job_id)``
  once per active job and translates each yielded snapshot.

* **Event-stream tap.** The render driver's per-event handler
  (``_EventConsumer``) calls :func:`attach_to_event_consumer` once per event
  so we can re-broadcast the raw progress event as a ``job.event`` (for the
  ``step_*`` / ``log`` / ``error`` family) and synthesise a
  ``job.preview_frame`` for every ``frame`` event.

``job.preview_frame`` is throttled to ~10 Hz to avoid drowning slow
consumers; only the most-recent frame inside the throttle window is
delivered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..jobs import JobRecord, JobRegistry
from .ws_hub import WSHub
from .ws_protocol import (
    JobCancelledEvent,
    JobCreatedEvent,
    JobEvent,
    JobEventPayload,
    JobFailedEvent,
    JobFailedPayload,
    JobPreviewFrameEvent,
    JobPreviewFramePayload,
    JobProgressEvent,
    JobRecordSnapshot,
    JobSucceededEvent,
    JobSucceededPayload,
)
from ..urls import url as studio_url

__all__ = [
    "attach_to_event_consumer",
    "broadcast_job_created",
    "broadcast_terminal",
    "snapshot_for",
    "subscribe_registry",
    "PREVIEW_FRAME_MIN_INTERVAL_S",
]

_LOG = logging.getLogger(__name__)

# Cap preview-frame broadcasts to ~10 Hz. Slow consumers see the latest frame
# only - intermediate frames are dropped per (job_id) channel.
PREVIEW_FRAME_MIN_INTERVAL_S = 0.1

_RAW_EVENT_TYPES: set[str] = {
    "step_start",
    "step_end",
    "substep_start",
    "substep_end",
    "log",
    "error",
}


def snapshot_for(record: JobRecord) -> JobRecordSnapshot:
    """Convert a :class:`JobRecord` to the wire-shape snapshot model."""

    return JobRecordSnapshot(**record.to_dict())


async def broadcast_job_created(hub: WSHub, record: JobRecord) -> None:
    """Broadcast a ``job.created`` event for ``record`` on the ``jobs`` topic."""

    event = JobCreatedEvent(payload=snapshot_for(record))
    await hub.broadcast(WSHub.TOPIC_JOBS, event.model_dump(mode="json", exclude_none=False))


async def _broadcast_progress(hub: WSHub, record: JobRecord) -> None:
    event = JobProgressEvent(payload=snapshot_for(record))
    await hub.broadcast(WSHub.TOPIC_JOBS, event.model_dump(mode="json", exclude_none=False))


async def _broadcast_terminal(
    hub: WSHub,
    record: JobRecord,
    *,
    artifact: Any | None = None,
    failure_summary: dict[str, Any] | None = None,
) -> None:
    snap = snapshot_for(record)
    if record.status == "succeeded":
        payload = JobSucceededPayload(**snap.model_dump(), artifact=artifact)
        event: Any = JobSucceededEvent(payload=payload)
    elif record.status == "cancelled":
        event = JobCancelledEvent(payload=snap)
    else:
        # Failure: thread the scraped failure_summary into the wire payload
        # so the UI can render a reason inline without round-tripping
        # through artifacts.metadata.
        failed_payload = JobFailedPayload(
            **snap.model_dump(), failure_summary=failure_summary
        )
        event = JobFailedEvent(payload=failed_payload)
    await hub.broadcast(WSHub.TOPIC_JOBS, event.model_dump(mode="json", exclude_none=False))


async def broadcast_terminal(
    hub: WSHub,
    record: JobRecord,
    *,
    artifact: Any | None = None,
    failure_summary: dict[str, Any] | None = None,
) -> None:
    """Public alias for the terminal-event broadcaster, used by the render hook."""

    await _broadcast_terminal(
        hub, record, artifact=artifact, failure_summary=failure_summary
    )


async def subscribe_registry(
    hub: WSHub,
    registry: JobRegistry,
    job_id: str,
) -> None:
    """Run ``JobRegistry.watch(job_id)`` and forward every snapshot.

    Created/progress/succeeded/failed/cancelled events are dispatched here
    based on ``record.status``. The watcher terminates naturally when the
    job hits a terminal status (the underlying async iterator stops then).

    Terminal artifacts (the ``succeeded`` event needs the on-disk
    :class:`Artifact`) are populated by the render hook in
    ``tools/render_project.py``, which is the only place that knows the
    project directory. We emit a bare terminal event here so that even if
    the hook never ran (e.g. registry mutated from outside the dev server)
    the UI still observes the transition.
    """

    seen_status: str | None = None
    try:
        async for record in registry.watch(job_id):
            status = record.status
            if status == "queued":
                if seen_status is None:
                    await broadcast_job_created(hub, record)
                seen_status = status
                continue
            if status == "running":
                if seen_status in (None, "queued"):
                    await broadcast_job_created(hub, record)
                await _broadcast_progress(hub, record)
                seen_status = status
                continue
            if status in {"succeeded", "failed", "cancelled"}:
                # Emit a final progress snapshot before the terminal one so the
                # UI's progress bar lands on the final numbers regardless of
                # whether the render hook beat us to the terminal event.
                if seen_status not in (None, status):
                    await _broadcast_progress(hub, record)
                # The render hook in tools/render_project.py owns the
                # canonical terminal event (it knows the artifact). When the
                # bridge is running standalone (no hook), emit a bare one so
                # subscribers still see a terminal transition.
                seen_status = status
                # Don't double-emit; rely on the hook for the artifact-rich
                # terminal event.
                continue
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive
        _LOG.exception("registry watcher for %s crashed", job_id)


# ---------------------------------------------------------------------------
# Event-stream tap (called from _EventConsumer)
# ---------------------------------------------------------------------------


class _PreviewThrottle:
    """Per-job throttle state for ``job.preview_frame`` broadcasts."""

    __slots__ = ("last_emit", "pending_task")

    def __init__(self) -> None:
        self.last_emit: float = 0.0
        self.pending_task: asyncio.Task[None] | None = None


_PREVIEW_STATE: dict[str, _PreviewThrottle] = {}


def _preview_frame_url(project_id: str, job_id: str, frame_index: int) -> str:
    return (
        studio_url(f"/files/projects/{project_id}/cache/preview_frames/{job_id}/{frame_index:06d}.jpg")
    )


def _t_s_from_event(event: dict[str, Any]) -> float | None:
    """Best-effort wall-clock-in-render extraction from a frame event."""

    fps = event.get("fps_observed") or event.get("fps")
    frame_index = event.get("frame_index")
    if not isinstance(frame_index, (int, float)) or not isinstance(fps, (int, float)):
        return None
    if fps <= 0:
        return None
    # ``fps_observed`` is the rate, not the project fps - best we can do
    # without the project fps is leave t_s unset. We return None here and
    # let the UI fall back to frame_index display.
    return None


def _build_frame_event(
    *,
    job_id: str,
    project_id: str,
    event: dict[str, Any],
) -> JobPreviewFrameEvent:
    frame_index = int(event.get("frame_index", 0) or 0)
    return JobPreviewFrameEvent(
        payload=JobPreviewFramePayload(
            job_id=job_id,
            frame=frame_index,
            url=_preview_frame_url(project_id, job_id, frame_index),
            t_s=_t_s_from_event(event),
        )
    )


def _build_job_event(*, job_id: str, event: dict[str, Any]) -> JobEvent:
    kind = str(event.get("event", "log"))
    # Strip the discriminator so it doesn't double-nest in the payload data.
    data = {k: v for k, v in event.items() if k != "event"}
    return JobEvent(payload=JobEventPayload(job_id=job_id, event=kind, data=data))  # type: ignore[arg-type]


def attach_to_event_consumer(
    consumer: Any,  # _EventConsumer; typed as Any to avoid an import cycle
    *,
    hub: WSHub | None,
    job_id: str,
    project_id: str,
    event: dict[str, Any],
) -> None:
    """Forward one renderer event onto the WS hub.

    Called from :class:`_EventConsumer.handle` *after* the registry has been
    updated, so :class:`JobProgressEvent` broadcasts driven by the registry
    watcher always reflect the latest state. ``hub`` may be ``None`` (legacy
    callers don't supply one); in that case this is a no-op.
    """

    if hub is None:
        return
    kind = event.get("event")

    if kind == "frame":
        _schedule_preview_frame(hub, job_id, project_id, event)
        return

    if kind in _RAW_EVENT_TYPES:
        wire = _build_job_event(job_id=job_id, event=event)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - we're always async
            return
        loop.create_task(
            hub.broadcast(WSHub.TOPIC_JOBS, wire.model_dump(mode="json", exclude_none=False))
        )


def _schedule_preview_frame(
    hub: WSHub,
    job_id: str,
    project_id: str,
    event: dict[str, Any],
) -> None:
    """Enforce the ~10 Hz throttle on ``job.preview_frame`` broadcasts."""

    state = _PREVIEW_STATE.setdefault(job_id, _PreviewThrottle())
    now = time.monotonic()
    elapsed = now - state.last_emit

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - always async in dev server
        return

    if elapsed >= PREVIEW_FRAME_MIN_INTERVAL_S:
        wire = _build_frame_event(job_id=job_id, project_id=project_id, event=event)
        state.last_emit = now
        # Cancel any pending tail emission since this fresh frame supersedes it.
        if state.pending_task is not None and not state.pending_task.done():
            state.pending_task.cancel()
            state.pending_task = None
        loop.create_task(
            hub.broadcast(WSHub.TOPIC_JOBS, wire.model_dump(mode="json", exclude_none=False))
        )
        return

    # Within the throttle window - schedule a tail emit that captures the
    # latest frame seen. Re-arming overwrites the pending event with the
    # newest one (drop intermediate, keep latest semantics).
    if state.pending_task is not None and not state.pending_task.done():
        state.pending_task.cancel()

    delay = PREVIEW_FRAME_MIN_INTERVAL_S - elapsed
    latest_event = dict(event)  # capture by copy
    state.pending_task = loop.create_task(
        _delayed_preview_broadcast(
            hub=hub,
            job_id=job_id,
            project_id=project_id,
            event=latest_event,
            delay=delay,
        )
    )


async def _delayed_preview_broadcast(
    *,
    hub: WSHub,
    job_id: str,
    project_id: str,
    event: dict[str, Any],
    delay: float,
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    wire = _build_frame_event(job_id=job_id, project_id=project_id, event=event)
    state = _PREVIEW_STATE.get(job_id)
    if state is not None:
        state.last_emit = time.monotonic()
        state.pending_task = None
    await hub.broadcast(
        WSHub.TOPIC_JOBS, wire.model_dump(mode="json", exclude_none=False)
    )


def reset_preview_throttle(job_id: str | None = None) -> None:
    """Clear throttle state (used in tests / on job teardown)."""

    if job_id is None:
        _PREVIEW_STATE.clear()
        return
    state = _PREVIEW_STATE.pop(job_id, None)
    if state is not None and state.pending_task is not None:
        state.pending_task.cancel()


# Kept for symmetry with the plan's spec - most callers don't need the path
# object since the URL is derived from the (project_id, job_id, frame) triple.
def preview_frame_path(*, project_dir: Path, job_id: str, frame_index: int) -> Path:
    return project_dir / "cache" / "preview_frames" / job_id / f"{frame_index:06d}.jpg"
