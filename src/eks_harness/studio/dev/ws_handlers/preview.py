"""M4 dev-server handlers: catalog + on-demand preview cache.

Four request types land here:

* ``catalog.request`` - enumerate every effect or transition with its
  current cache state. Replies with ``{entries: [...]}`` AND broadcasts
  the same payload to the ``catalog`` topic so other subscribers stay in
  sync.
* ``preview.request_effect`` - return a cached preview URL when fresh; on
  a miss, enqueue a background render and immediately reply with
  ``{pending: true, request_id, ...}``. As the request moves through the
  shared single-worker queue we broadcast ``catalog.preview_progress``
  events (``queued`` -> ``rendering`` -> ``encoding`` -> ``done`` /
  ``failed``). The terminal success signal remains the existing
  ``catalog.preview_ready`` event so older clients keep working.
* ``preview.request_transition`` - same shape, for transition kinds.
* ``preview.cancel`` - drop a queued request before the worker picks it
  up. If the request is already actively rendering the cancel is a
  no-op (we don't kill ffmpeg mid-render - that would just waste the
  cycles already spent); the UI must tolerate a trailing
  ``catalog.preview_ready`` / ``catalog.preview_progress`` event for a
  cancelled ``request_id``.

Imports of the cache layer are deferred to inside each handler so a dev
server running in a mode that never touches previews doesn't pay the
cost of importing ``eks_harness.video.render``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ..ws_hub import WSHub
from ..ws_protocol import (
    CatalogRequest,
    ErrorEnvelope,
    PreviewCancelRequest,
    PreviewRequestEffect,
    PreviewRequestTransition,
    ReplyEnvelope,
)

__all__ = [
    "handle_catalog_request",
    "handle_preview_cancel",
    "handle_preview_request_effect",
    "handle_preview_request_transition",
    "register",
]


_LOG = logging.getLogger(__name__)


async def handle_catalog_request(
    request: CatalogRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    """Enumerate effects or transitions and broadcast the same list."""

    from .. import preview_catalog

    kind = request.payload.kind
    try:
        if kind == "effect":
            entries = preview_catalog.list_effects()
            broadcast_type = "catalog.effects"
        else:
            entries = preview_catalog.list_transitions()
            broadcast_type = "catalog.transitions"
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.exception("catalog.request(%s) failed", kind)
        return ErrorEnvelope(
            id=request.id, code="server_error", message=str(exc)
        )

    # Broadcast first so subscribers receive the canonical list even if the
    # client that asked never publishes it locally.
    await hub.broadcast(
        WSHub.TOPIC_CATALOG,
        {"type": broadcast_type, "payload": {"entries": entries}},
    )
    return ReplyEnvelope(
        type="catalog.request.reply",
        id=request.id,
        result={"entries": entries, "kind": kind},
    )


def _progress_callback(
    *, hub: WSHub, kind: str, name: str, request_id: str
):
    """Build the ``progress_cb`` handed to the preview cache.

    Each invocation broadcasts a ``catalog.preview_progress`` frame so
    every subscriber sees the same lifecycle phase for a given
    ``request_id``.
    """

    async def _emit(phase: str, **extra: Any) -> None:
        payload: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "request_id": request_id,
            "phase": phase,
        }
        for key, value in extra.items():
            if value is None:
                continue
            payload[key] = value
        await hub.broadcast(
            WSHub.TOPIC_CATALOG,
            {"type": "catalog.preview_progress", "payload": payload},
        )

    return _emit


async def _render_effect_then_broadcast(
    *, hub: WSHub, name: str, params: dict | None, request_id: str
) -> None:
    """Background task: render an effect preview, broadcast on success.

    Failures land in two places: ``catalog.preview_progress`` (phase
    ``failed``, with ``message``/``error_type``/``traceback`` fields
    populated by the cache layer's ``_shape_failure`` helper) AND a
    legacy ``error`` broadcast on the catalog topic for older
    subscribers. Both carry the same human-readable message; the
    progress event additionally carries the abbreviated traceback for
    debugging.
    """

    from .. import preview_cache
    from ..preview_renderer import PreviewUnavailableError

    progress_cb = _progress_callback(
        hub=hub, kind="effect", name=name, request_id=request_id
    )

    # Track whether the cache layer already emitted a terminal phase. The
    # cache's worker is guaranteed (post-fix) to emit ``done`` or ``failed``
    # for every queued request, but the synchronous portion of
    # ``ensure_effect_preview`` (key computation, freshness check) can still
    # raise BEFORE the worker sees the item - in which case we must emit
    # the terminal phase ourselves so the UI can flip out of its optimistic
    # ``queued`` state.
    terminal_emitted = {"done": False, "failed": False}

    async def _gated_cb(phase: str, **fields: Any) -> None:
        if phase in ("done", "failed"):
            terminal_emitted[phase] = True
        await progress_cb(phase, **fields)

    try:
        url, key, _stale = await preview_cache.ensure_effect_preview(
            name, params, progress_cb=_gated_cb, request_id=request_id
        )
    except PreviewUnavailableError as exc:
        _LOG.info("preview.request_effect(%s) unavailable: %s", name, exc)
        if not (terminal_emitted["done"] or terminal_emitted["failed"]):
            await progress_cb(
                "failed",
                message=str(exc) or "preview unavailable",
                error_type=type(exc).__name__,
            )
        await hub.broadcast(
            WSHub.TOPIC_CATALOG,
            {
                "type": "error",
                "code": "unavailable",
                "message": str(exc),
                "detail": {
                    "kind": "effect",
                    "name": name,
                    "error_type": type(exc).__name__,
                },
            },
        )
        return
    except Exception as exc:
        _LOG.exception("preview.request_effect(%s) failed", name)
        if not (terminal_emitted["done"] or terminal_emitted["failed"]):
            await progress_cb(
                "failed",
                message=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
            )
        await hub.broadcast(
            WSHub.TOPIC_CATALOG,
            {
                "type": "error",
                "code": "render_failed",
                "message": str(exc) or type(exc).__name__,
                "detail": {
                    "kind": "effect",
                    "name": name,
                    "error_type": type(exc).__name__,
                },
            },
        )
        return
    await hub.broadcast(
        WSHub.TOPIC_CATALOG,
        {
            "type": "catalog.preview_ready",
            "payload": {"kind": "effect", "name": name, "url": url, "key": key},
        },
    )


async def _render_transition_then_broadcast(
    *, hub: WSHub, name: str, params: dict | None, request_id: str
) -> None:
    from .. import preview_cache
    from ..preview_renderer import PreviewUnavailableError

    progress_cb = _progress_callback(
        hub=hub, kind="transition", name=name, request_id=request_id
    )

    terminal_emitted = {"done": False, "failed": False}

    async def _gated_cb(phase: str, **fields: Any) -> None:
        if phase in ("done", "failed"):
            terminal_emitted[phase] = True
        await progress_cb(phase, **fields)

    try:
        url, key, _stale = await preview_cache.ensure_transition_preview(
            name, params, progress_cb=_gated_cb, request_id=request_id
        )
    except PreviewUnavailableError as exc:
        _LOG.info("preview.request_transition(%s) unavailable: %s", name, exc)
        if not (terminal_emitted["done"] or terminal_emitted["failed"]):
            await progress_cb(
                "failed",
                message=str(exc) or "preview unavailable",
                error_type=type(exc).__name__,
            )
        await hub.broadcast(
            WSHub.TOPIC_CATALOG,
            {
                "type": "error",
                "code": "unavailable",
                "message": str(exc),
                "detail": {
                    "kind": "transition",
                    "name": name,
                    "error_type": type(exc).__name__,
                },
            },
        )
        return
    except Exception as exc:
        _LOG.exception("preview.request_transition(%s) failed", name)
        if not (terminal_emitted["done"] or terminal_emitted["failed"]):
            await progress_cb(
                "failed",
                message=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
            )
        await hub.broadcast(
            WSHub.TOPIC_CATALOG,
            {
                "type": "error",
                "code": "render_failed",
                "message": str(exc) or type(exc).__name__,
                "detail": {
                    "kind": "transition",
                    "name": name,
                    "error_type": type(exc).__name__,
                },
            },
        )
        return
    await hub.broadcast(
        WSHub.TOPIC_CATALOG,
        {
            "type": "catalog.preview_ready",
            "payload": {"kind": "transition", "name": name, "url": url, "key": key},
        },
    )


async def handle_preview_request_effect(
    request: PreviewRequestEffect, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    """Cached URL if fresh; else schedule render + reply ``pending=true``."""

    from .. import preview_cache
    from ..preview_renderer import PreviewUnavailableError

    name = request.payload.name
    params = request.payload.params

    try:
        key = preview_cache._compute_effect_key(name)
    except PreviewUnavailableError as exc:
        return ErrorEnvelope(
            id=request.id, code="not_found", message=str(exc)
        )
    except Exception as exc:  # pragma: no cover - defensive
        return ErrorEnvelope(
            id=request.id, code="server_error", message=str(exc)
        )

    if preview_cache.is_fresh(name, "effects", key):
        return ReplyEnvelope(
            type="preview.request_effect.reply",
            id=request.id,
            result={
                "url": preview_cache.preview_url(name, "effects"),
                "key": key,
                "stale": False,
                "name": name,
                "kind": "effect",
            },
        )

    request_id = uuid.uuid4().hex[:12]
    asyncio.create_task(
        _render_effect_then_broadcast(
            hub=hub, name=name, params=params, request_id=request_id
        ),
        name=f"preview-effect-{name}",
    )
    return ReplyEnvelope(
        type="preview.request_effect.reply",
        id=request.id,
        result={
            "pending": True,
            "request_id": request_id,
            "name": name,
            "kind": "effect",
        },
    )


async def handle_preview_request_transition(
    request: PreviewRequestTransition, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    """Cached URL if fresh; else schedule transition render + reply pending."""

    from .. import preview_cache
    from ..preview_renderer import PreviewUnavailableError

    name = request.payload.name
    params = request.payload.params

    try:
        key = preview_cache._compute_transition_key(name, params)
    except PreviewUnavailableError as exc:
        return ErrorEnvelope(
            id=request.id, code="not_found", message=str(exc)
        )
    except Exception as exc:  # pragma: no cover - defensive
        return ErrorEnvelope(
            id=request.id, code="server_error", message=str(exc)
        )

    if preview_cache.is_fresh(name, "transitions", key):
        return ReplyEnvelope(
            type="preview.request_transition.reply",
            id=request.id,
            result={
                "url": preview_cache.preview_url(name, "transitions"),
                "key": key,
                "stale": False,
                "name": name,
                "kind": "transition",
            },
        )

    request_id = uuid.uuid4().hex[:12]
    asyncio.create_task(
        _render_transition_then_broadcast(
            hub=hub, name=name, params=params, request_id=request_id
        ),
        name=f"preview-transition-{name}",
    )
    return ReplyEnvelope(
        type="preview.request_transition.reply",
        id=request.id,
        result={
            "pending": True,
            "request_id": request_id,
            "name": name,
            "kind": "transition",
        },
    )


async def handle_preview_cancel(
    request: PreviewCancelRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    """Drop a queued preview render request, if it's still pending.

    Best-effort: a request that has already started rendering is allowed
    to complete (the cycles are already spent). Replies with
    ``{ok: true, cancelled: bool}``; ``cancelled=false`` means the slot
    was already running or unknown.
    """

    from .. import preview_cache

    payload = request.payload
    cancelled = await preview_cache.cancel_request(
        kind=payload.kind, name=payload.name, request_id=payload.request_id
    )
    return ReplyEnvelope(
        type="preview.cancel.reply",
        id=request.id,
        result={"ok": True, "cancelled": cancelled},
    )


def register(register_handler) -> None:
    """Wire the M4 catalog + preview handlers into the dispatch registry."""

    register_handler("catalog.request", handle_catalog_request)
    register_handler("preview.request_effect", handle_preview_request_effect)
    register_handler("preview.request_transition", handle_preview_request_transition)
    register_handler("preview.cancel", handle_preview_cancel)
