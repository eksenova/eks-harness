"""``studio_router(ctx)``: the studio's live API for the harness daemon.

Mounted by the daemon under ``prefix`` (``/api/studio``); URLs the studio
hands out (renders, previews, media) carry that prefix:

* ``/ws`` - the studio WebSocket (request/reply envelopes plus topics).
* ``/files/projects/{project_id}/upload`` and ``/files/projects/{project_id}/{path}`` - project files.
* ``/preview-cache/{kind}/{name}`` and ``/sample/{kind}`` - effect and transition previews.

Every route needs an admin principal: studio projects are Python programs that
the daemon runs.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .dev import process_log_buffer
from .dev.file_routes import build_file_routes
from .dev.ws_endpoint import build_endpoint_class
from .dev.ws_handlers import logs as logs_handler
from .dev.ws_hub import WSHub
from .roots import MEDIA_LIBRARY_ROOT, PROJECT_WORKSPACE_ROOT, RootsManager, set_roots_manager
from .stats import SystemStatsSampler
from .urls import set_prefix

__all__ = ["configure_roots", "studio_router", "workspace_root"]

_LOG = logging.getLogger(__name__)


def workspace_root(ctx: Any) -> Path:
    configured = str(ctx.config["video.workspace"] or "").strip()
    root = Path(configured).expanduser() if configured else ctx.paths.data_dir / "video"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def configure_roots(ctx: Any) -> RootsManager:
    manager = RootsManager()
    manager.register(PROJECT_WORKSPACE_ROOT, workspace_root(ctx))
    media = str(ctx.config["video.mediaLibrary"] or "").strip()
    if media:
        manager.register(MEDIA_LIBRARY_ROOT, Path(media).expanduser())
    set_roots_manager(manager)
    return manager


def _principal(ctx: Any, connection: Any) -> Any:
    from eks_harness.api.deps import resolve_principal

    return resolve_principal(connection)


def _same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc.lower() == (websocket.headers.get("host") or "").lower()


def _guard(ctx: Any, endpoint: Any) -> Any:
    async def guarded(request: Request) -> Response:
        try:
            principal = _principal(ctx, request)
        except Exception as problem:
            status = int(getattr(problem, "status", getattr(problem, "status_code", 401)) or 401)
            return JSONResponse({"error": getattr(problem, "error", "unauthorized"),
                                 "message": str(problem)}, status_code=status)
        if principal is None:
            return JSONResponse({"error": "unauthorized", "message": "Log in or send an API key."}, status_code=401)
        if not principal.is_admin:
            return JSONResponse({"error": "admin_required", "message": "Only an admin can use the studio."},
                                status_code=403)
        return await endpoint(request)

    return guarded


def studio_router(ctx: Any, prefix: str = "/api/studio") -> APIRouter:
    set_prefix(prefix)
    configure_roots(ctx)
    hub = WSHub()
    sampler = SystemStatsSampler()

    def authorize(websocket: WebSocket) -> bool:
        principal = _principal(ctx, websocket)
        if principal is None or not principal.is_admin:
            return False
        return principal.via != "cookie" or _same_origin(websocket)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        process_log_buffer.attach()
        sampler.start(hub)
        logs_handler.start_process_log_worker(hub)
        try:
            yield
        finally:
            await sampler.stop()

    router = APIRouter(lifespan=lifespan)
    for route in build_file_routes(workspace_root(ctx), hub=hub):
        router.routes.append(Route(route.path, _guard(ctx, route.endpoint), methods=list(route.methods or [])))
    router.routes.append(WebSocketRoute("/ws", build_endpoint_class(hub, authorize)))
    router.studio_hub = hub  # type: ignore[attr-defined]
    router.studio_sampler = sampler  # type: ignore[attr-defined]
    return router
