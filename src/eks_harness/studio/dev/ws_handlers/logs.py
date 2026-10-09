"""WS handlers for the right-sidebar LOGS tab.

Two surfaces:

* a ``process.logs`` request → reply with up to ``payload.limit`` recent
  ring-buffer entries (optionally filtered by ``since_ts``);
* a background fan-out task that drains the
  :mod:`eks_harness.studio.dev.process_log_buffer` subscriber set and broadcasts
  batched ``process.log`` events to the ``process_logs`` topic.

The fan-out task is started by :func:`start_process_log_worker` from the
ASGI lifespan. It batches at most ~20 records per outbound frame and emits
at most once per ``BATCH_INTERVAL_S`` to keep the websocket cheap even under
log floods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import process_log_buffer
from ..ws_hub import WSHub
from ..ws_protocol import (
    ProcessLogEntry,
    ProcessLogEvent,
    ProcessLogEventPayload,
    ProcessLogsRequest,
    ReplyEnvelope,
    TOPIC_PROCESS_LOGS,
)

__all__ = [
    "handle_process_logs",
    "register",
    "start_process_log_worker",
    "stop_process_log_worker",
]

_LOG = logging.getLogger(__name__)

# Batch size + flush cadence. The right-sidebar LOGS tab tolerates a 200ms
# latency easily and batching cuts socket overhead by ~10x during noisy
# preview-render storms.
_BATCH_MAX = 20
_BATCH_INTERVAL_S = 0.2

# Subscriber queue size. Logs can burst (e.g. ffmpeg's per-frame chatter);
# we drop-oldest when the queue saturates, matching the broader hub policy.
_QUEUE_MAX = 512

# Backlog cap on a single ``process.logs`` reply. The pydantic field allows
# higher numbers, but anything above this risks an oversized frame.
_REPLY_HARD_CAP = 2000


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


async def handle_process_logs(
    request: ProcessLogsRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope:
    """Reply with a backlog snapshot from the in-memory ring buffer.

    The client uses this on tab-mount to populate the LOGS view before
    live-tail kicks in. Once the reply is delivered, the client should be
    subscribed to the ``process_logs`` topic to receive subsequent records.
    """

    del hub, client_id  # unused - process logs are global, not per-client

    limit = max(1, min(int(request.payload.limit or 512), _REPLY_HARD_CAP))
    raw = process_log_buffer.latest(limit, since_ts=request.payload.since_ts)
    entries = [ProcessLogEntry(**e) for e in raw]
    return ReplyEnvelope(
        type="process.logs.reply",
        id=request.id,
        result={
            "entries": [e.model_dump(mode="json") for e in entries],
            "attached": process_log_buffer.is_attached(),
            "ring_cap": process_log_buffer.RING_CAP,
        },
    )


# ---------------------------------------------------------------------------
# Background fan-out worker
# ---------------------------------------------------------------------------


_WORKER_TASK: asyncio.Task[None] | None = None
_WORKER_QUEUE: asyncio.Queue[dict[str, Any]] | None = None


async def _fanout_loop(hub: WSHub, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Drain ``queue`` and broadcast batched process.log events."""

    try:
        while True:
            # Block for the first record so an idle process burns no CPU.
            first = await queue.get()
            batch: list[dict[str, Any]] = [first]
            queue.task_done()

            # Greedy-drain anything that's already queued; this is the
            # "burst absorber" path. Cap at _BATCH_MAX so we don't emit
            # frames the UI can't keep up with.
            while len(batch) < _BATCH_MAX:
                try:
                    nxt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                batch.append(nxt)
                queue.task_done()

            payload = ProcessLogEventPayload(
                entries=[ProcessLogEntry(**e) for e in batch]
            )
            event = ProcessLogEvent(payload=payload)
            try:
                await hub.broadcast(
                    TOPIC_PROCESS_LOGS,
                    event.model_dump(mode="json"),
                )
            except Exception:  # pragma: no cover - defensive
                _LOG.debug("process.log fanout broadcast failed", exc_info=True)

            # Rate-limit: even if more records arrive immediately, wait a
            # tick before the next broadcast.
            await asyncio.sleep(_BATCH_INTERVAL_S)
    except asyncio.CancelledError:
        return


def start_process_log_worker(hub: WSHub) -> None:
    """Launch the fan-out task. Idempotent."""

    global _WORKER_TASK, _WORKER_QUEUE
    if _WORKER_TASK is not None and not _WORKER_TASK.done():
        return

    # Make sure the underlying tee handler is installed. Idempotent.
    process_log_buffer.attach()

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAX)
    process_log_buffer.subscribe(queue)
    _WORKER_QUEUE = queue

    loop = asyncio.get_event_loop()
    _WORKER_TASK = loop.create_task(
        _fanout_loop(hub, queue), name="process-log-fanout"
    )
    _LOG.debug("process.log fan-out worker started")


async def stop_process_log_worker() -> None:
    """Cancel the fan-out task. Tolerant of repeated calls."""

    global _WORKER_TASK, _WORKER_QUEUE
    task = _WORKER_TASK
    queue = _WORKER_QUEUE
    _WORKER_TASK = None
    _WORKER_QUEUE = None
    if queue is not None:
        process_log_buffer.unsubscribe(queue)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # pragma: no cover - cleanup
        pass


# ---------------------------------------------------------------------------
# Auto-registration
# ---------------------------------------------------------------------------


def register(register_handler) -> None:
    """Wire the process.logs request handler."""

    register_handler("process.logs", handle_process_logs)
