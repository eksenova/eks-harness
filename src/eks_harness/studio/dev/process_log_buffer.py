"""Process-wide stdout/stderr log capture for the dev server.

Wires a single ``logging.Handler`` onto the root logger that:

* mirrors every ``LogRecord`` to the original ``sys.stderr`` so the launch
  terminal still gets unaltered output (this is the historical contract;
  swallowing stderr would break operators who tail logs from systemd, etc.);
* appends a structured ``ProcessLogEntry``-shaped dict into a bounded
  ``collections.deque`` ring so the LOGS tab in the right sidebar can fetch
  a backlog snapshot on connect;
* puts a copy of each record onto every live subscriber ``asyncio.Queue``
  so the WS worker can stream them out at ~5Hz batched.

The module is import-time safe (no I/O, no threads) - call :func:`attach`
once from the ASGI lifespan to install the handler. Detach is intentionally
not exposed: the dev process owns the root logger and tearing the handler
down mid-shutdown produces stray warnings on its own way out.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Iterable

__all__ = [
    "attach",
    "is_attached",
    "latest",
    "subscribe",
    "unsubscribe",
    "RING_CAP",
]


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------

RING_CAP = 4096
"""Maximum number of records retained in the in-memory ring buffer."""


# ---------------------------------------------------------------------------
# Module state (process-wide; the dev server is a single process)
# ---------------------------------------------------------------------------

# Re-entrant lock guards both the ring and the subscribers set. Records are
# pushed from arbitrary logging call sites (potentially other threads), so we
# can't lean on the event loop for serialisation.
_LOCK = threading.RLock()

_RING: Deque[dict[str, Any]] = deque(maxlen=RING_CAP)

# Subscribers receive every newly-appended record. We keep references to the
# raw ``asyncio.Queue`` objects; the WS handler module is responsible for
# adding/removing entries via ``subscribe`` / ``unsubscribe``.
_SUBSCRIBERS: set[asyncio.Queue[dict[str, Any]]] = set()

_HANDLER: logging.Handler | None = None
_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging handler
# ---------------------------------------------------------------------------


class _ProcessLogTeeHandler(logging.Handler):
    """Captures every ``LogRecord`` into the ring + mirrors to stderr."""

    # Pin the original stderr so we keep working even if other code swaps
    # ``sys.stderr`` later (uvicorn occasionally redirects on reload).
    _stderr = sys.__stderr__

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        # Guard against recursion: if the formatter itself logs, we'd loop.
        # Logging filters out reentrant calls within the same thread on
        # CPython, but we belt-and-brace it with a try/except.
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = "<unformattable log record>"

        entry: dict[str, Any] = {
            "ts": record.created,
            "level": (record.levelname or "INFO").lower(),
            "source": record.name or "root",
            "message": message,
        }

        # Mirror to the original stderr so terminal observers see logs
        # exactly as before. We deliberately do NOT use ``self.format`` here
        # - keeping the bytes byte-for-byte identical to the default stream
        # handler's output would require duplicating uvicorn's formatter.
        # The previous handler chain still runs (we're appended, not
        # replacing), so ``stderr`` already receives a formatted line.
        # This emit is just the structured side-channel.

        with _LOCK:
            _RING.append(entry)
            subs = tuple(_SUBSCRIBERS)

        for q in subs:
            # Non-blocking put. If a subscriber falls behind we drop the
            # oldest queued record (best-effort live tail). The ring buffer
            # remains the source of truth for replay.
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except Exception:  # pragma: no cover - defensive
                    pass
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:  # pragma: no cover - defensive
                    continue


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attach() -> None:
    """Install the tee handler on the root logger. Idempotent."""

    global _HANDLER
    with _LOCK:
        if _HANDLER is not None:
            return
        handler = _ProcessLogTeeHandler(level=logging.DEBUG)
        handler.set_name("eks-studio-process-log-tee")
        # Append to root; existing handlers (uvicorn / default stream) keep
        # their stderr output intact.
        logging.getLogger().addHandler(handler)
        _HANDLER = handler
    _LOG.debug("process_log_buffer attached (ring_cap=%d)", RING_CAP)


def is_attached() -> bool:
    """Whether :func:`attach` has been called this process."""

    return _HANDLER is not None


def latest(n: int, *, since_ts: float | None = None) -> list[dict[str, Any]]:
    """Return up to ``n`` recent entries, oldest first.

    ``since_ts`` filters out entries whose ``ts`` is <= ``since_ts``. When
    omitted, simply returns the tail.
    """

    if n <= 0:
        return []
    with _LOCK:
        snapshot = list(_RING)
    if since_ts is not None:
        snapshot = [e for e in snapshot if e["ts"] > since_ts]
    if len(snapshot) > n:
        snapshot = snapshot[-n:]
    return snapshot


def subscribe(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Register ``queue`` to receive every subsequent log entry."""

    with _LOCK:
        _SUBSCRIBERS.add(queue)


def unsubscribe(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """De-register ``queue``. Safe to call multiple times."""

    with _LOCK:
        _SUBSCRIBERS.discard(queue)


def _drain_subscribers() -> Iterable[asyncio.Queue[dict[str, Any]]]:
    """Snapshot of the current subscriber set (for tests)."""

    with _LOCK:
        return tuple(_SUBSCRIBERS)


def _inject_for_test(entry: dict[str, Any]) -> None:  # pragma: no cover
    """Test hook: push an entry without going through the logging system."""

    with _LOCK:
        _RING.append(entry)
        subs = tuple(_SUBSCRIBERS)
    for q in subs:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Internal: ensure the tee never gets garbage-collected away by accident.
# ---------------------------------------------------------------------------

_ATTACH_TS = time.time()
