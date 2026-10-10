from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module, resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from eks_harness import __version__
from eks_harness.api.errors import install_handlers, not_found
from eks_harness.api.schemas import HealthResponse
from eks_harness.auth.middleware import RequestGuard, RobotsTag
from eks_harness.config import Config
from eks_harness.config import load as load_config
from eks_harness.daemon import hooks
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.events import EventBus
from eks_harness.db import open_database
from eks_harness.db.repos import users as users_repo
from eks_harness.paths import Paths, resolve_paths
from eks_harness.pools import create_pools

log = logging.getLogger("eks_harness.daemon")

ROUTER_MODULES = (
    "routes_system",
    "routes_settings",
    "routes_auth",
    "routes_projects",
    "routes_sessions",
    "routes_review",
    "routes_cleanup",
    "routes_update",
    "routes_artifacts",
    "routes_shares",
    "routes_leases",
    "routes_pools",
    "routes_backends",
    "routes_logs",
    "routes_captures",
    "routes_live",
    "routes_public",
    "routes_plugins",
    "routes_nodes",
    "routes_scores",
    "routes_drivers",
)
WEB_DIST_ENV = "EKS_HARNESS_WEB_DIST"
API_PREFIX = "/api"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def packaged_web_dist() -> Path | None:
    try:
        return Path(str(resources.files("eks_harness") / "web_dist"))
    except (ModuleNotFoundError, TypeError):
        return None


def web_dist_dir() -> Path | None:
    override = os.environ.get(WEB_DIST_ENV)
    if override:
        path = Path(override)
        return path if (path / "index.html").is_file() else None
    path = packaged_web_dist()
    return path if path is not None and (path / "index.html").is_file() else None


def web_dist_missing_message() -> str:
    index = None if os.environ.get(WEB_DIST_ENV) else packaged_web_dist()
    if index is not None and (index / "index.html").is_symlink():
        return ("The installed web UI files are symlinks into a uv cache that has since been cleaned. Reinstall "
                "with copied files: <code>uv tool install --force --reinstall --link-mode copy "
                "&lt;eks-harness requirement&gt;</code>.")
    return ("The eks-harness web UI is not built into this installation. Reinstall with Node.js and pnpm on "
            "PATH: <code>uv tool install --reinstall &lt;eks-harness checkout&gt;</code>.")


def build_context(config: Config | None = None, paths: Paths | None = None, *, fake_pools: bool | None = None,
                  managed: bool = False) -> AppContext:
    paths = (paths or (config.paths if config is not None else resolve_paths())).ensure()
    config = config or load_config(paths)
    db = open_database(paths.db_file)
    users_repo.local_user(db.conn())
    events = EventBus(db)
    pools = create_pools(config, paths, fake=fake_pools)

    def sink(type: str, **fields) -> None:
        events.publish(type, **fields)

    pools.host.event_sink = sink
    pools.host.managed = managed
    ctx = AppContext(config=config, paths=paths, db=db, events=events, pools=pools, managed=managed)
    pools.host.url = ctx.url
    pools.host.plugins = ctx.plugins
    return ctx


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_csp_cache: dict[str, tuple[int, str]] = {}


def spa_csp(index: Path) -> str:
    try:
        stamp = index.stat().st_mtime_ns
    except OSError:
        stamp = 0
    cached = _csp_cache.get(str(index))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        text = ""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
        for body in _INLINE_SCRIPT.findall(text))
    policy = ("default-src 'self'; script-src 'self'" + (f" {hashes}" if hashes else "") + "; "
              "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; "
              "font-src 'self' data:; connect-src 'self'; frame-src 'self'; worker-src 'none'; object-src 'none'; "
              "base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
    _csp_cache[str(index)] = (stamp, policy)
    return policy


def spa_headers(index: Path) -> dict[str, str]:
    return {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": spa_csp(index)}


def spa_relative(path: str) -> str | None:
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or ":" in part for part in parts):
        return None
    return "/".join(parts)


def _serve_spa(request: Request, path: str) -> Response:
    if path.startswith("api/") or path == "api":
        raise not_found(f"Unknown API path /{path}.", error="unknown_path")
    dist = web_dist_dir()
    if dist is None:
        return HTMLResponse(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>eks-harness</title></head>"
            f"<body><p>{web_dist_missing_message()}</p></body></html>",
            status_code=503, headers={"Cache-Control": "no-store"})
    root = dist.absolute()
    relative = spa_relative(path)
    if relative:
        candidate = root / relative
        if candidate.is_file():
            immutable = relative.startswith("assets/")
            headers = {"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
                       "X-Content-Type-Options": "nosniff"}
            return FileResponse(candidate, headers=headers)
    if path and "." in path.rsplit("/", 1)[-1] and not path.endswith(".html"):
        raise not_found(f"No file /{path}.", error="not_found")
    return FileResponse(root / "index.html", media_type="text/html", headers=spa_headers(root / "index.html"))


def mount_mcp(ctx: AppContext) -> tuple[object | None, object | None]:
    try:
        from eks_harness.mcp_http import build
    except ImportError as error:
        log.info("MCP over HTTP not mounted: %s", error)
        return None, None
    return build(ctx)


def mount_studio(app: FastAPI, ctx: AppContext) -> bool:
    try:
        from eks_harness.studio.router import studio_router
    except ImportError as error:
        log.info("studio not mounted (install eks-harness[video]): %s", error)
        return False
    app.include_router(studio_router(ctx, prefix=f"{API_PREFIX}/studio"), prefix=f"{API_PREFIX}/studio")
    return True


def create_app(config: Config | None = None, paths: Paths | None = None, *, fake_pools: bool | None = None,
               managed: bool = False, context: AppContext | None = None) -> FastAPI:
    ctx = context or build_context(config, paths, fake_pools=fake_pools, managed=managed)

    mcp_gate, mcp_app = mount_mcp(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await hooks.run_startup(ctx)
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
            await hooks.run_shutdown(ctx)
            try:
                ctx.pools.host.save()
            except OSError:
                log.exception("could not save pool state")
            ctx.db.close()

    app = FastAPI(title="eks-harness", version=__version__, lifespan=lifespan, docs_url=f"{API_PREFIX}/docs",
                  openapi_url=f"{API_PREFIX}/openapi.json", redoc_url=None)
    app.state.ctx = ctx
    install_handlers(app)
    app.add_middleware(RequestGuard, config=ctx.config)
    app.add_middleware(RobotsTag)

    @app.get(f"{API_PREFIX}/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    for module_name in ROUTER_MODULES:
        module = import_module(f"eks_harness.api.{module_name}")
        app.include_router(module.router)
    import_module("eks_harness.api.routes_plugins").mount_plugin_routes(app, ctx)
    mount_studio(app, ctx)
    if mcp_gate is not None:
        app.mount("/mcp", mcp_gate)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(request: Request, path: str) -> Response:
        return _serve_spa(request, path)

    return app


__all__ = ["ROUTER_MODULES", "build_context", "create_app", "spa_headers", "web_dist_dir"]
