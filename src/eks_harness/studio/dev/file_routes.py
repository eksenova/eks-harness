"""HTTP file-serving routes for the dev server.

Three endpoints, all returning ``FileResponse`` with built-in HTTP-range
support so the browser can scrub long mp4s:

* ``/files/projects/{project_id}/{path:path}`` - project tree (renders +
  preview frames + media). Path-traversal guarded.
* ``/preview-cache/{kind}/{name}`` - entries under
  ``<cache dir>/studio/preview_cache/<kind>/``.
* ``/sample/{kind}`` - ffmpeg-generated sample clips under
  ``<cache dir>/studio/preview_cache/_samples/``.

A miss returns plain JSON ``{"error": "not_found", "path": ...}`` - there
is no MCP envelope here.

The module also exposes ``POST /files/projects/{project_id}/upload`` for the
drag-drop UI path. The browser sends multipart/form-data (more efficient
than base64 over WS for large videos); the handler reuses the validation
logic from :mod:`ws_handlers.media`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from ..workspace import default_workspace_root

__all__ = ["build_file_routes", "preview_cache_root", "samples_root"]

_LOG = logging.getLogger(__name__)


def preview_cache_root() -> Path:
    from ..paths import preview_cache_dir

    return preview_cache_dir()


def samples_root() -> Path:
    return preview_cache_root() / "_samples"


def _not_found(path: str) -> JSONResponse:
    return JSONResponse({"error": "not_found", "path": path}, status_code=404)


def _resolve_project_root(workspace: Path, project_id: str) -> Path:
    """Map a ``project_id`` to its on-disk directory.

    A project usually lives in ``<workspace>/<project_id>``. But when the dev
    server is launched from *inside* a project directory, the workspace IS the
    project - there is no ``<workspace>/<project_id>`` subdir, and render
    artifacts are written directly under ``<workspace>/renders/...``. Handle
    both: prefer the subdir when it exists, otherwise fall back to the
    workspace itself when its name matches the requested project id. An
    absolute ``project_id`` is honoured as-is.
    """

    raw = Path(project_id)
    if raw.is_absolute() and raw.is_dir():
        return raw
    subdir = workspace / project_id
    if subdir.is_dir():
        return subdir
    if workspace.name == project_id:
        return workspace
    return subdir


def _safe_resolve(root: Path, relative: str) -> Path | None:
    """Return ``root / relative`` if and only if it stays inside ``root``."""

    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    try:
        if not candidate.is_relative_to(root_resolved):
            return None
    except AttributeError:  # pragma: no cover - py<3.9 path
        if not str(candidate).startswith(str(root_resolved) + os.sep):
            return None
    return candidate


def build_file_routes(
    workspace_root: Path | None = None,
    *,
    hub: Any | None = None,
) -> list[Route]:
    """Build the file-serving + upload routes.

    ``workspace_root`` defaults to cwd. When ``hub`` is supplied the upload
    handler broadcasts a ``media.changed`` event after each successful write
    so the UI auto-refreshes without polling.
    """

    workspace = (workspace_root or default_workspace_root()).resolve()

    async def projects_file(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        relative = request.path_params["path"]
        project_root = _resolve_project_root(workspace, project_id)
        resolved = _safe_resolve(project_root, relative)
        if resolved is None or not resolved.is_file():
            return _not_found(f"projects/{project_id}/{relative}")
        return FileResponse(str(resolved))

    async def preview_cache_file(request: Request) -> Response:
        kind = request.path_params["kind"]
        name = request.path_params["name"]
        if "/" in kind or ".." in kind or "/" in name or ".." in name:
            return _not_found(f"preview-cache/{kind}/{name}")
        target = preview_cache_root() / kind / name
        resolved = _safe_resolve(preview_cache_root(), f"{kind}/{name}")
        if resolved is None or not target.is_file():
            return _not_found(f"preview-cache/{kind}/{name}")
        return FileResponse(str(resolved))

    async def sample_file(request: Request) -> Response:
        kind = request.path_params["kind"]
        if "/" in kind or ".." in kind:
            return _not_found(f"sample/{kind}")
        target = samples_root() / kind
        resolved = _safe_resolve(samples_root(), kind)
        if resolved is None or not target.is_file():
            return _not_found(f"sample/{kind}")
        return FileResponse(str(resolved))

    async def upload_file(request: Request) -> Response:
        """Multipart upload - writes one file under ``<project>/media/``.

        Form keys: ``file`` (binary). Query keys: ``kind`` (video/audio/image),
        ``filename`` (override the multipart filename), ``overwrite`` (bool).
        Returns ``{url, path}`` on success.
        """

        # Late import to avoid pulling ws_handlers at module load time.
        from .ws_handlers.media import (
            MAX_UPLOAD_BYTES,
            _resolve_project_dir,
            broadcast_media_changed,
            write_upload_bytes,
        )

        project_id = request.path_params["project_id"]
        project_dir = _resolve_project_dir(project_id)
        if project_dir is None:
            return JSONResponse(
                {"error": "not_found", "project_id": project_id}, status_code=404
            )

        # Cheap pre-flight on Content-Length so we reject huge bodies before
        # buffering them in memory. Starlette's form parser will still read
        # everything in, but at least we can deny obvious abuse early.
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"error": "too_large", "limit": MAX_UPLOAD_BYTES}, status_code=413
            )

        kind = request.query_params.get("kind")
        if kind not in ("video", "audio", "image"):
            return JSONResponse(
                {"error": "bad_request", "reason": "kind must be video/audio/image"},
                status_code=400,
            )
        overwrite = request.query_params.get("overwrite", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        filename_override = request.query_params.get("filename")

        try:
            form = await request.form()
        except Exception as exc:  # pragma: no cover - bad multipart
            return JSONResponse(
                {"error": "bad_request", "reason": f"form parse failed: {exc}"},
                status_code=400,
            )
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {"error": "bad_request", "reason": "missing 'file' part"},
                status_code=400,
            )
        filename = filename_override or getattr(upload, "filename", None) or ""
        try:
            data = await upload.read()  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - read failure
            return JSONResponse(
                {"error": "io_error", "reason": str(exc)}, status_code=500
            )
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"error": "too_large", "limit": MAX_UPLOAD_BYTES}, status_code=413
            )

        try:
            target, url = write_upload_bytes(
                project_dir,
                kind=kind,  # type: ignore[arg-type]
                filename=filename,
                data=data,
                overwrite=overwrite,
            )
        except ValueError as exc:
            code = str(exc)
            status = 413 if code == "too_large" else 400
            if code == "exists":
                status = 409
            return JSONResponse({"error": code}, status_code=status)

        if hub is not None:
            try:
                await broadcast_media_changed(hub, project_id)
            except Exception:  # pragma: no cover - broadcast best-effort
                _LOG.exception("media.changed broadcast failed")

        return JSONResponse({"url": url, "path": str(target)})

    return [
        Route(
            "/files/projects/{project_id}/upload",
            upload_file,
            methods=["POST"],
        ),
        Route(
            "/files/projects/{project_id}/{path:path}",
            projects_file,
            methods=["GET", "HEAD"],
        ),
        Route(
            "/preview-cache/{kind}/{name}",
            preview_cache_file,
            methods=["GET", "HEAD"],
        ),
        Route("/sample/{kind}", sample_file, methods=["GET", "HEAD"]),
    ]
