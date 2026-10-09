"""Effect / transition preview cache, with key-based staleness + queueing.

Cache layout::

    <cache dir>/studio/preview_cache/
        .schema                   # bumped by ``_PREVIEW_SCHEMA_VERSION``
        effects/
            <EffectName>.mp4
            <EffectName>.key
        transitions/
            <TransitionKind>.mp4
            <TransitionKind>.key

The ``.key`` sidecar carries a 16-char hex sha1 derived from the effect /
transition schema + default params + sample-file mtimes + the renderer
schema-version constant. A request is fresh iff the current key matches
the sidecar's content.

Cache hits return immediately from :func:`ensure_effect_preview` /
:func:`ensure_transition_preview`. Misses are funnelled through a single
``asyncio.Queue`` drained by exactly one worker task: previews render
one-at-a-time across the whole process so a burst of hover requests
can't spin up N parallel ffmpeg children and starve the dev server.
Concurrent callers awaiting the same slot share the same eventual future.
A watchdog re-spawns the worker if it ever crashes between iterations.

On module import we compare ``.schema`` against ``_PREVIEW_SCHEMA_VERSION``
and wipe ``effects/`` + ``transitions/`` + ``_samples/`` when stale, so an
artifact-format bump (e.g. PNG → mp4) doesn't leave the cache half-baked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import traceback as _tb
from pathlib import Path
from typing import Any

from .file_routes import preview_cache_root
from .preview_renderer import (
    PreviewUnavailableError,
    default_params_for_effect,
    render_effect_preview,
    render_transition_preview,
)
from ..urls import url as studio_url

__all__ = [
    "PreviewCacheError",
    "ProgressCallback",
    "cancel_request",
    "ensure_effect_preview",
    "ensure_transition_preview",
    "is_fresh",
    "preview_url",
    "queue_length",
]


# Optional async callback handed to ``ensure_*_preview`` so the WS layer
# can broadcast ``catalog.preview_progress`` events as the request moves
# through queued -> rendering -> encoding -> done|failed. The callback
# receives a free-form payload; the cache code never inspects its return
# value and silently swallows exceptions.
ProgressCallback = Any  # async callable: (phase, **fields) -> None


_LOG = logging.getLogger(__name__)


# Bump this whenever the preview artifact format, sample recipe, or
# curated default params change in a way that invalidates the on-disk
# cache. The sentinel file at ``<cache_root>/.schema`` is compared on
# import; a mismatch wipes ``effects/`` + ``transitions/`` + ``_samples/``
# so stale PNGs / SMPTE samples from prior batches never linger.
_PREVIEW_SCHEMA_VERSION = "v6"


class PreviewCacheError(RuntimeError):
    """Raised when the cache cannot produce a preview (input invalid, etc.)."""


def _effects_dir() -> Path:
    target = preview_cache_root() / "effects"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _transitions_dir() -> Path:
    target = preview_cache_root() / "transitions"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _schema_sentinel() -> Path:
    return preview_cache_root() / ".schema"


def _bust_if_stale_schema() -> None:
    """Wipe cached artifacts when the schema version changes.

    Called once at module import time. Survives missing parent
    directories (fresh install) and refuses to throw - the worst case
    is a stale tile, never a broken import.
    """

    sentinel = _schema_sentinel()
    try:
        existing = sentinel.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError:
        existing = ""
    if existing == _PREVIEW_SCHEMA_VERSION:
        return
    root = preview_cache_root()
    for sub in ("effects", "transitions", "_samples"):
        try:
            shutil.rmtree(root / sub, ignore_errors=True)
        except OSError:  # pragma: no cover - defensive
            pass
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(_PREVIEW_SCHEMA_VERSION, encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        pass


# Run at import time. Idempotent across module reloads.
_bust_if_stale_schema()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _hash16(payload: dict[str, Any]) -> str:
    """sha1 → first 16 hex chars of the JSON-encoded payload."""

    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _effect_schema(name: str) -> dict[str, Any]:
    """Lazily fetch the JSON schema for an effect class by name."""

    from eks_harness.video.ir.effects import all_effect_models

    for cls in all_effect_models():
        if cls.__name__ == name:
            return cls.model_json_schema()
    return {}


def _transition_schema(kind: str) -> dict[str, Any]:
    from eks_harness.video.ir.transitions import Crossfade, Cut, PluginTransition

    mapping = {
        "Cut": Cut,
        "Crossfade": Crossfade,
        "PluginTransition": PluginTransition,
    }
    cls = mapping.get(kind)
    return cls.model_json_schema() if cls is not None else {}


def _compute_effect_key(name: str) -> str:
    """Build the cache-key for ``name``.

    Raises :class:`PreviewUnavailableError` when the effect has no
    curated default / no renderer plugin. The WS handler converts that
    into a synchronous ``not_found`` error envelope so the tile flips to
    its FAILED slot immediately instead of joining the render queue and
    failing later.
    """

    schema = _effect_schema(name)
    # Propagate PreviewUnavailableError - non-previewable effects must
    # short-circuit at the request boundary, not after queueing a doomed
    # render.
    default_params = default_params_for_effect(name)
    # We deliberately do NOT include the sample's mtime in the hash: the
    # mtime flips on the first cold render (when the sample is generated
    # lazily) which would otherwise invalidate every just-rendered tile.
    # The schema-version sentinel is the cache buster of record; if the
    # sample recipe changes we bump ``_PREVIEW_SCHEMA_VERSION``.
    return _hash16(
        {
            "name": name,
            "schema": schema,
            "default_params": default_params,
            "schema_version": _PREVIEW_SCHEMA_VERSION,
        }
    )


def _samples_exist() -> bool:
    """True iff the cached sample assets have already been generated.

    Used to avoid triggering ffmpeg sample generation just to compute a
    cache key; on a fresh machine the key falls back to mtime=0 and gets
    rewritten on the first real render request.
    """

    from .file_routes import samples_root

    candidate = samples_root() / "sample.mp4"
    return candidate.is_file()


def _compute_transition_key(kind: str, params: dict[str, Any] | None) -> str:
    schema = _transition_schema(kind)
    return _hash16(
        {
            "kind": kind,
            "schema": schema,
            "params": params or {},
            "schema_version": _PREVIEW_SCHEMA_VERSION,
        }
    )


def _key_path(kind: str, name: str) -> Path:
    base = _effects_dir() if kind == "effects" else _transitions_dir()
    return base / f"{name}.key"


def _artifact_path(kind: str, name: str) -> Path:
    """Path of the cached mp4 artifact for ``name`` under ``kind``."""

    base = _effects_dir() if kind == "effects" else _transitions_dir()
    return base / f"{name}.mp4"


def is_fresh(name: str, kind: str, key: str) -> bool:
    """True iff the cached sidecar for ``name`` matches ``key`` exactly."""

    artifact = _artifact_path(kind, name)
    key_file = _key_path(kind, name)
    if not artifact.is_file() or not key_file.is_file():
        return False
    try:
        stored = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stored == key


def preview_url(name: str, kind: str) -> str:
    return studio_url(f"/preview-cache/{kind}/{name}.mp4")


# ---------------------------------------------------------------------------
# Single-worker render queue
# ---------------------------------------------------------------------------
#
# We deliberately funnel every preview render through one worker so a
# hover-storm cannot spin up N parallel ffmpeg children. The queue holds
# :class:`_QueueItem` instances each wrapping a future the caller awaits.
# Multiple callers asking for the same ``slot`` share the same item - the
# coalescing logic from earlier milestones lives in ``_PENDING`` below.
#
# A watchdog runs inside ``_ensure_worker``: if the previously-spawned
# task is ``done()`` (either it raised between iterations or somebody
# cancelled it) we eagerly start a fresh one so the next request can't
# stall on an undrainable queue.


class _QueueItem:
    """One unit of work for the preview worker.

    ``progress_cbs`` is a list rather than a single callback so multiple
    callers awaiting the same slot (a hover-storm hitting the same effect
    or a duplicate request from a tile re-render) all receive the same
    queued/rendering/done/failed lifecycle frames. Without this, only the
    first caller's tile would observe progress; subsequent callers would
    sit on their optimistic ``queued`` state forever.
    """

    __slots__ = (
        "slot",
        "factory",
        "future",
        "progress_cbs",
        "request_ids",
        "cancelled",
    )

    def __init__(
        self,
        *,
        slot: str,
        factory,
        future: "asyncio.Future[str]",
        progress_cb: ProgressCallback | None,
        request_id: str | None,
    ) -> None:
        self.slot = slot
        self.factory = factory
        self.future = future
        self.progress_cbs: list[ProgressCallback] = (
            [progress_cb] if progress_cb is not None else []
        )
        self.request_ids: list[str | None] = [request_id]
        self.cancelled = False

    def add_subscriber(
        self,
        *,
        progress_cb: ProgressCallback | None,
        request_id: str | None,
    ) -> None:
        if progress_cb is not None:
            self.progress_cbs.append(progress_cb)
        self.request_ids.append(request_id)


_QUEUE: "asyncio.Queue[_QueueItem] | None" = None
_WORKER: "asyncio.Task[None] | None" = None
_PENDING: dict[str, _QueueItem] = {}
_PENDING_LOCK = asyncio.Lock()


def _ensure_worker() -> "asyncio.Queue[_QueueItem]":
    """Return the process queue, lazily (re)starting the worker on first use.

    The watchdog clause matters: if the worker task previously crashed
    with an uncaught exception (which the ``_worker_loop`` try/except
    below is meant to prevent, but defensively we still check) we'd
    otherwise leak the next request into an undrained queue. Restarting
    is cheap - the new task just consumes items from the same
    ``asyncio.Queue`` instance.
    """

    global _QUEUE, _WORKER
    if _QUEUE is None:
        _QUEUE = asyncio.Queue()
    if _WORKER is None or _WORKER.done():
        if _WORKER is not None and _WORKER.done():
            try:
                exc = _WORKER.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                exc = None
            if exc is not None:
                _LOG.warning(
                    "preview worker died with %s; restarting", type(exc).__name__
                )
        _WORKER = asyncio.create_task(_worker_loop(), name="preview-cache-worker")
    return _QUEUE


def queue_length() -> int:
    """Return the current depth of the render queue (excluding the active item)."""

    return _QUEUE.qsize() if _QUEUE is not None else 0


async def _safe_progress(cb: ProgressCallback | None, **fields: Any) -> None:
    if cb is None:
        return
    try:
        await cb(**fields)
    except Exception:  # pragma: no cover - progress is best-effort
        _LOG.debug("preview progress callback raised", exc_info=True)


async def _broadcast_progress(
    cbs: list[ProgressCallback], **fields: Any
) -> None:
    """Fan a progress frame out to every subscriber's callback."""

    for cb in cbs:
        await _safe_progress(cb, **fields)


def _shape_failure(exc: BaseException) -> dict[str, Any]:
    """Build a rich failure payload from an exception.

    Returns ``{message, error_type, traceback}``. The ``message`` is the
    human-readable form (for :class:`PreviewUnavailableError` we strip the
    type prefix so the UI shows the curator's actual sentence, not the
    raw repr). ``traceback`` is capped at 600 chars from the tail so a
    massive ffmpeg stderr blob doesn't blow the WS frame budget.
    """

    if isinstance(exc, PreviewUnavailableError):
        message = str(exc) or "preview unavailable"
    else:
        message = str(exc) or exc.__class__.__name__
    tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    tb_short = tb_text[-600:] if len(tb_text) > 600 else tb_text
    return {
        "message": message,
        "error_type": type(exc).__name__,
        "traceback": tb_short,
    }


async def _worker_loop() -> None:
    """Drain ``_QUEUE`` one item at a time, forever.

    Every render is wrapped in its own try/except so a runaway exception
    from a single effect can never escape the loop. The try/finally
    guarantees a terminal ``failed`` phase is emitted for every request
    if it didn't already emit ``done`` or ``failed`` - so the UI can
    never get stuck on ``queued``.

    A single per-iteration ``terminal_emitted`` flag prevents duplicate
    terminal phases.
    """

    assert _QUEUE is not None
    while True:
        try:
            item = await _QUEUE.get()
        except asyncio.CancelledError:
            raise
        except BaseException:  # pragma: no cover - queue itself shouldn't raise
            _LOG.exception("preview worker queue.get raised; continuing")
            continue
        terminal_emitted = False
        try:
            if item.cancelled:
                if not item.future.done():
                    item.future.cancel()
                # Emit a terminal frame so listeners know the slot is dead.
                await _broadcast_progress(
                    item.progress_cbs,
                    phase="failed",
                    message="cancelled",
                    error_type="Cancelled",
                )
                terminal_emitted = True
                continue
            await _broadcast_progress(item.progress_cbs, phase="rendering")
            try:
                result = await item.factory()
            except (KeyboardInterrupt, SystemExit):  # pragma: no cover
                # Genuine shutdown signals must propagate.
                raise
            except BaseException as exc:  # noqa: BLE001 - intentionally broad
                shaped = _shape_failure(exc)
                if not item.future.done():
                    item.future.set_exception(exc)
                await _broadcast_progress(
                    item.progress_cbs, phase="failed", **shaped
                )
                terminal_emitted = True
            else:
                if not item.future.done():
                    item.future.set_result(result)
                await _broadcast_progress(item.progress_cbs, phase="done")
                terminal_emitted = True
        except (KeyboardInterrupt, SystemExit):  # pragma: no cover
            # Belt-and-suspenders for the rare case the worker is cancelled
            # mid-iteration: try to emit a terminal phase before bubbling up.
            if not terminal_emitted:
                try:
                    await _broadcast_progress(
                        item.progress_cbs,
                        phase="failed",
                        message="worker shutdown",
                        error_type="WorkerShutdown",
                    )
                except BaseException:  # pragma: no cover
                    pass
            raise
        except BaseException as exc:  # noqa: BLE001 - watchdog
            _LOG.exception("preview worker iteration crashed; continuing")
            if not item.future.done():
                item.future.set_exception(exc)
        finally:
            # Final guarantee: if nothing above reported a terminal phase
            # (a code path we don't anticipate, or the inner ``continue``
            # for cancellation hitting the no-callback branch), emit one
            # now so the UI's per-slot state transitions out of ``queued``.
            if not terminal_emitted:
                try:
                    await _broadcast_progress(
                        item.progress_cbs,
                        phase="failed",
                        message="worker emitted no terminal phase",
                        error_type="MissingTerminal",
                    )
                except BaseException:  # pragma: no cover
                    pass
            async with _PENDING_LOCK:
                _PENDING.pop(item.slot, None)


async def cancel_request(
    *, kind: str, name: str, request_id: str | None = None
) -> bool:
    """Mark a queued preview request as cancelled.

    Returns ``True`` when a pending item was found and skipped, ``False``
    when the slot was either already running or unknown. The worker
    still consumes the item from the queue but skips the render. If the
    request is already actively rendering this call is a no-op - killing
    ffmpeg mid-render would only waste the cycles we have already spent,
    so the worker is allowed to finish; the UI must tolerate a trailing
    ``preview_ready`` / ``preview_progress`` event for the cancelled id.
    """

    slot_prefix = "effects" if kind == "effect" else "transitions"
    async with _PENDING_LOCK:
        for slot, item in _PENDING.items():
            if not slot.startswith(f"{slot_prefix}:{name}:"):
                continue
            if request_id is not None and request_id not in item.request_ids:
                continue
            item.cancelled = True
            return True
    return False


async def _enqueue(
    slot: str,
    factory,
    *,
    progress_cb: ProgressCallback | None,
    request_id: str | None,
) -> str:
    """Schedule ``factory`` on the worker; concurrent callers share the slot.

    Coalescing semantics: when a sibling caller arrives while the slot is
    in flight we attach their progress callback to the live ``_QueueItem``
    AND immediately re-emit a synthetic ``queued`` event to their callback
    so their UI tile flips out of its optimistic state. They then receive
    every subsequent lifecycle phase from the shared worker run.
    """

    queue = _ensure_worker()
    loop = asyncio.get_running_loop()
    new_item = False
    async with _PENDING_LOCK:
        existing = _PENDING.get(slot)
        if existing is not None:
            existing.add_subscriber(
                progress_cb=progress_cb, request_id=request_id
            )
            future = existing.future
        else:
            future = loop.create_future()
            item = _QueueItem(
                slot=slot,
                factory=factory,
                future=future,
                progress_cb=progress_cb,
                request_id=request_id,
            )
            _PENDING[slot] = item
            new_item = True
    if new_item:
        await queue.put(_PENDING[slot])
    # ``queue_position`` is 1-based - for a freshly-queued item it's qsize().
    # For a coalesced subscriber we send ``queue_position=0`` since the item
    # is either already running or near the head; the UI just needs the
    # ``queued`` signal to clear its optimistic placeholder.
    await _safe_progress(
        progress_cb,
        phase="queued",
        queue_position=queue.qsize() if new_item else 0,
    )
    return await future


async def ensure_effect_preview(
    name: str,
    params: dict[str, Any] | None,
    *,
    progress_cb: ProgressCallback | None = None,
    request_id: str | None = None,
) -> tuple[str, str, bool]:
    """Return ``(url, key, stale)`` for ``name``; renders if needed.

    ``stale=False`` means the cached artifact was already fresh on the
    first inspection - no render was performed for this call.
    ``stale=True`` indicates we (or a coalesced sibling caller) had to
    (re)render. Misses are queued onto the shared single-worker render
    queue.
    """

    key = _compute_effect_key(name)
    url = preview_url(name, "effects")
    if is_fresh(name, "effects", key):
        return url, key, False

    slot = f"effects:{name}:{key}"

    async def _do_render() -> str:
        artifact_path = _artifact_path("effects", name)
        key_path = _key_path("effects", name)
        # Re-check freshness inside the slot in case a sibling has just
        # finished. Cheap, and avoids a double-render race.
        if is_fresh(name, "effects", key):
            return key
        try:
            await render_effect_preview(name, params, out_path=artifact_path)
        except PreviewUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise PreviewCacheError(f"effect {name!r} preview failed: {exc}") from exc
        # Sidecar last, so a partial render never looks fresh.
        key_path.write_text(key, encoding="utf-8")
        return key

    await _enqueue(slot, _do_render, progress_cb=progress_cb, request_id=request_id)
    return url, key, True


async def ensure_transition_preview(
    kind: str,
    params: dict[str, Any] | None,
    *,
    progress_cb: ProgressCallback | None = None,
    request_id: str | None = None,
) -> tuple[str, str, bool]:
    """Return ``(url, key, stale)`` for transition ``kind``."""

    key = _compute_transition_key(kind, params)
    url = preview_url(kind, "transitions")
    if is_fresh(kind, "transitions", key):
        return url, key, False

    slot = f"transitions:{kind}:{key}"

    async def _do_render() -> str:
        artifact_path = _artifact_path("transitions", kind)
        key_path = _key_path("transitions", kind)
        if is_fresh(kind, "transitions", key):
            return key
        try:
            await render_transition_preview(kind, params, out_path=artifact_path)
        except PreviewUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise PreviewCacheError(
                f"transition {kind!r} preview failed: {exc}"
            ) from exc
        key_path.write_text(key, encoding="utf-8")
        return key

    await _enqueue(slot, _do_render, progress_cb=progress_cb, request_id=request_id)
    return url, key, True
