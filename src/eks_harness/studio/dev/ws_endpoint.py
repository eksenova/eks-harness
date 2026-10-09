"""Starlette ``/ws`` endpoint: register, dispatch, deliver.

The endpoint owns the per-connection lifecycle. On connect it registers the
client with the hub and emits ``hello``. Two tasks then run concurrently:

* a reader task that parses incoming JSON via ``ClientMessageAdapter`` and
  dispatches by ``type``;
* a writer task that drains the hub's per-client queue and writes to the
  socket.

The handler registry is global and mutable - M1 wires only the three
``system`` handlers; later milestones overwrite entries via
``register_handler`` from inside their own module's import side.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from pydantic import ValidationError
from starlette.endpoints import WebSocketEndpoint
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import __version__
from ..roots import get_roots_manager
from . import ws_handlers as _ws_handlers_pkg
from .ws_hub import WSHub
from .ws_protocol import (
    ClientMessageAdapter,
    ErrorEnvelope,
    HelloEvent,
    HelloPayload,
    ReplyEnvelope,
)

__all__ = [
    "DevWebSocketEndpoint",
    "register_handler",
    "build_endpoint_class",
]

_LOG = logging.getLogger(__name__)


# Handler signature: (request_model, *, hub, client_id) -> ReplyEnvelope | ErrorEnvelope
Handler = Callable[..., Awaitable[ReplyEnvelope | ErrorEnvelope]]

# Module-global registry keyed by message ``type``. Later milestones overwrite
# entries from their own ``register`` callable.
_HANDLERS: dict[str, Handler] = {}

def register_handler(message_type: str, handler: Handler) -> None:
    """Install or replace the handler for ``message_type``.

    Future milestones (M2-M6) call this from their module-level ``register``
    to plug in concrete implementations without touching the endpoint.
    """

    _HANDLERS[message_type] = handler


# Auto-discover and register every handler module under ``ws_handlers``. Each
# module must expose ``register(register_handler)``; missing the hook is fine
# (we just skip it). Stage-2 milestones add new modules without touching this
# file.
def _autoload_handlers() -> None:
    import importlib
    import pkgutil

    for _, name, _ in pkgutil.iter_modules(_ws_handlers_pkg.__path__):
        try:
            mod = importlib.import_module(f"{_ws_handlers_pkg.__name__}.{name}")
        except Exception:  # pragma: no cover - one bad module shouldn't kill the rest
            _LOG.exception("ws_handlers.%s failed to import", name)
            continue
        register_fn = getattr(mod, "register", None)
        if callable(register_fn):
            try:
                register_fn(register_handler)
            except Exception:  # pragma: no cover
                _LOG.exception("ws_handlers.%s.register() raised", name)


_autoload_handlers()


async def _not_implemented(
    request: Any, *, hub: WSHub, client_id: str
) -> ErrorEnvelope:
    """Default handler for known message types not yet wired."""

    return ErrorEnvelope(
        id=getattr(request, "id", None),
        code="not_implemented",
        message=f"{request.type}: handler not landed yet",
    )


def _serialise(message: Any) -> dict[str, Any]:
    """Convert a pydantic model (or pre-built dict) to a JSON-safe dict."""

    if hasattr(message, "model_dump"):
        return message.model_dump(mode="json", exclude_none=False)
    return dict(message)  # already a plain dict


def build_endpoint_class(hub: WSHub, authorize: Callable[[WebSocket], bool] | None = None):
    """Construct a ``WebSocketEndpoint`` subclass bound to ``hub``.

    ``authorize`` runs before the socket is accepted; a falsy result closes
    the handshake with a policy violation.
    """

    class _Endpoint(WebSocketEndpoint):
        encoding = "text"

        def __init__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
            super().__init__(scope, receive, send)
            self._client_id: str | None = None
            self._writer_task: asyncio.Task[None] | None = None

        async def on_connect(self, websocket: WebSocket) -> None:
            if authorize is not None:
                try:
                    allowed = authorize(websocket)
                except Exception:
                    allowed = False
                if not allowed:
                    await websocket.close(code=1008)
                    return
            await websocket.accept()
            client = await hub.register()
            self._client_id = client.id

            # Resolve filesystem roots best-effort so the UI's Topbar can
            # display the active workspace without an extra round-trip.
            try:
                workspace_path = str(get_roots_manager().project_workspace())
            except Exception:  # pragma: no cover - defensive
                workspace_path = None
            try:
                media_root = get_roots_manager().media_library()
                media_root_path = str(media_root) if media_root else None
            except Exception:  # pragma: no cover - defensive
                media_root_path = None

            hello = HelloEvent(
                payload=HelloPayload(
                    server="eks-harness-studio",
                    version=__version__,
                    project_workspace=workspace_path,
                    media_library_root=media_root_path,
                )
            )
            await websocket.send_text(json.dumps(_serialise(hello)))

            self._writer_task = asyncio.create_task(
                _writer_loop(websocket, client.queue),
                name=f"ws-writer-{client.id}",
            )

        async def on_receive(self, websocket: WebSocket, data: str) -> None:
            client_id = self._client_id
            if client_id is None:
                return

            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                await _send_error(
                    websocket,
                    ErrorEnvelope(
                        code="bad_request",
                        message="invalid JSON",
                        detail={"reason": str(exc)},
                    ),
                )
                return

            try:
                request = ClientMessageAdapter.validate_python(payload)
            except ValidationError as exc:
                await _send_error(
                    websocket,
                    ErrorEnvelope(
                        id=payload.get("id") if isinstance(payload, dict) else None,
                        code="bad_request",
                        message="request failed validation",
                        detail={"errors": exc.errors()},
                    ),
                )
                return

            handler = _HANDLERS.get(request.type, _not_implemented)
            try:
                reply = await handler(request, hub=hub, client_id=client_id)
            except Exception as exc:  # pragma: no cover - defensive
                _LOG.exception("handler %s raised", request.type)
                reply = ErrorEnvelope(
                    id=getattr(request, "id", None),
                    code="handler_error",
                    message=str(exc),
                )
            await websocket.send_text(json.dumps(_serialise(reply)))

        async def on_disconnect(self, websocket: WebSocket, close_code: int) -> None:
            if self._writer_task is not None:
                self._writer_task.cancel()
                try:
                    await self._writer_task
                except (asyncio.CancelledError, Exception):  # pragma: no cover - cleanup
                    pass
            if self._client_id is not None:
                await hub.unregister(self._client_id)
                self._client_id = None

    return _Endpoint


# Backwards-compat name for static reference; the real class is built per-hub
# by build_endpoint_class above.
DevWebSocketEndpoint = WebSocketEndpoint


async def _send_error(websocket: WebSocket, env: ErrorEnvelope) -> None:
    try:
        await websocket.send_text(json.dumps(_serialise(env)))
    except Exception:  # pragma: no cover - socket already closed
        pass


async def _writer_loop(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Drain ``queue`` to the socket until cancellation or disconnect."""

    try:
        while True:
            message = await queue.get()
            try:
                await websocket.send_text(json.dumps(message))
            except WebSocketDisconnect:
                return
            except Exception:
                _LOG.debug("writer: send failed; ending loop", exc_info=True)
                return
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        return
