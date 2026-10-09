"""``media.list`` handler - enumerates media assets for the Topbar's media
library panel.

Two scopes are honoured:

* ``project`` - walks the named project directory recursively and surfaces
  every video / audio / image file found there.
* ``global`` - falls back to the ``media_library`` root if the MCP client
  granted one; otherwise returns an empty list. Browser code lists the
  three kinds via separate calls so the response stays small.

Each entry includes a ``url`` pointing at the dev server's ``/files``
route so the inspector can play the file in-page without copying it.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Iterable, Literal

from ...roots import get_roots_manager
from ..ws_hub import WSHub
from ..ws_protocol import (
    ErrorEnvelope,
    MediaChangedEvent,
    MediaChangedPayload,
    MediaDeleteRequest,
    MediaItem,
    MediaListRequest,
    MediaRenameRequest,
    MediaUploadRequest,
    ReplyEnvelope,
)
from ...urls import url as studio_url

__all__ = ["register"]

_LOG = logging.getLogger(__name__)

MediaKind = Literal["video", "audio", "image"]

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}

_KIND_FOR_EXT: dict[str, MediaKind] = {
    **{e: "video" for e in _VIDEO_EXTS},
    **{e: "audio" for e in _AUDIO_EXTS},
    **{e: "image" for e in _IMAGE_EXTS},
}

# Don't recurse into these - they're outputs / caches the user doesn't want
# to wade through to find a source file.
_SKIP_DIRS = {
    "renders",
    "cache",
    "out",
    ".cache",
    "__pycache__",
    "preview_frames",
    ".git",
    ".venv",
    "node_modules",
}

# Hard cap to keep responses bounded.
_MAX_ITEMS = 512


def _classify(path: Path) -> MediaKind | None:
    return _KIND_FOR_EXT.get(path.suffix.lower())


def _iter_media(root: Path, *, want: MediaKind | None) -> Iterable[Path]:
    """Walk ``root`` skipping noisy dirs, yielding files whose extension
    matches ``want`` (or every supported kind when ``want`` is None)."""

    if not root.is_dir():
        return
    yielded = 0
    for current, dirs, files in os.walk(root):
        # Prune in-place so os.walk doesn't recurse into junk.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            kind = _classify(Path(name))
            if kind is None:
                continue
            if want is not None and kind != want:
                continue
            yield Path(current) / name
            yielded += 1
            if yielded >= _MAX_ITEMS:
                return


def _project_url(project_id: str, abs_path: Path, project_dir: Path) -> str | None:
    """Build a ``/files/projects/<pid>/<relpath>`` URL when ``abs_path``
    lives under ``project_dir``; None for files outside (those are listed
    but not playable in-page)."""

    try:
        rel = abs_path.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return None
    # Forward slashes for URL form even on Windows.
    rel_str = "/".join(rel.parts)
    return studio_url(f"/files/projects/{project_id}/{rel_str}")


def _global_url(abs_path: Path, media_root: Path) -> str | None:
    """For the global scope we don't have a stable URL route yet - return
    None so the UI just shows metadata. (A future iteration can add a
    ``/files/media/...`` route mirroring the file routes for projects.)"""

    del abs_path, media_root
    return None


def _to_item(
    path: Path,
    kind: MediaKind,
    *,
    url: str | None,
) -> MediaItem:
    """Build a MediaItem from on-disk metadata only. We deliberately don't
    invoke ffprobe here - listing 100+ files via ffprobe is slow enough to
    block the UI for seconds. The MediaInspector lazily probes a single
    selection when the user opens it."""

    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = None
    if url is None:
        url = path.resolve().as_uri()
        # Touch mimetypes so its cache is warm for any downstream consumer.
        mimetypes.guess_type(str(path))
    return MediaItem(
        path=str(path),
        name=path.name,
        url=url,
        kind=kind,
        size_bytes=size_bytes,
    )


def _resolve_project_dir(project_id: str) -> Path | None:
    """Resolve a project_id (or absolute path) to the workspace dir."""

    if not project_id:
        return None
    direct = Path(project_id)
    if direct.is_absolute() and direct.is_dir():
        return direct
    roots = get_roots_manager()
    workspace = roots.project_workspace()
    candidate = workspace / project_id
    if candidate.is_dir():
        return candidate
    # When the dev server is launched from inside the project directory, the
    # workspace *is* the project (no <workspace>/<project_id> subdir).
    if workspace.name == project_id:
        return workspace
    # Also try cwd as a sibling - covers the "project_workspace was a
    # fallback to cwd" case.
    cwd_candidate = Path(os.getcwd()) / project_id
    if cwd_candidate.is_dir():
        return cwd_candidate
    if Path(os.getcwd()).name == project_id:
        return Path(os.getcwd())
    return None


# ---------------------------------------------------------------------------
# Mutate helpers (upload / delete / rename).
# ---------------------------------------------------------------------------


# Conventional subdir under <project_dir> that holds uploaded assets. We keep
# everything under one folder so cleanup, backups and the media library
# routes have a single place to look.
_MEDIA_SUBDIR = "media"

# Hard upload cap. WebSocket uploads carry the body as base64, so anything
# bigger is better routed through the multipart HTTP endpoint - but we still
# bound the total at 1 GiB to protect the server.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB


def _ensure_media_dir(project_dir: Path) -> Path:
    """Return ``<project_dir>/media`` (creating it on first touch)."""

    target = project_dir / _MEDIA_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def _sanitise_filename(name: str) -> str | None:
    """Strip path components from ``name`` and reject empty / dotfile names.

    Returns ``None`` if the value is unusable (path-traversal attempt, drive
    letter, absolute path, empty after sanitisation).
    """

    if not name:
        return None
    if "\x00" in name:
        return None
    # Reject absolute paths / drive letters straight away - these are common
    # browser quirks where the input value ends up being a full local path.
    if os.path.isabs(name):
        return None
    if len(name) >= 2 and name[1] == ":":  # Windows drive letter
        return None
    # Reject any traversal segment.
    parts = name.replace("\\", "/").split("/")
    if any(part in ("", "..", ".") for part in parts):
        # An empty leading part (e.g. ``/foo``) or any ``..`` segment.
        if parts == [name]:
            # Single-segment input with no traversal: keep going.
            pass
        else:
            return None
    base = os.path.basename(name.replace("\\", "/"))
    if not base or base in (".", ".."):
        return None
    return base


def _resolve_inside(media_dir: Path, relative: str) -> Path | None:
    """Resolve ``relative`` against ``media_dir`` if and only if it stays inside.

    Mirrors :func:`file_routes._safe_resolve` but constrained to the media
    folder, so callers can pass arbitrary user input safely.
    """

    try:
        candidate = (media_dir / relative).resolve()
    except (OSError, ValueError):
        return None
    try:
        base = media_dir.resolve()
    except OSError:
        return None
    try:
        if not candidate.is_relative_to(base):
            return None
    except AttributeError:  # pragma: no cover - py<3.9 fallback
        if not str(candidate).startswith(str(base) + os.sep):
            return None
    return candidate


def _kind_for(filename: str) -> MediaKind | None:
    return _KIND_FOR_EXT.get(Path(filename).suffix.lower())


async def broadcast_media_changed(hub: WSHub, project_id: str) -> None:
    """Push a ``media.changed`` event on the ``media`` topic.

    Exposed module-level so the HTTP upload route can call it too - the
    Starlette handler doesn't have direct access to the WS hub otherwise.
    """

    event = MediaChangedEvent(payload=MediaChangedPayload(project_id=project_id))
    await hub.broadcast(WSHub.TOPIC_MEDIA, event.model_dump(mode="json"))


def write_upload_bytes(
    project_dir: Path,
    *,
    kind: MediaKind,
    filename: str,
    data: bytes,
    overwrite: bool,
) -> tuple[Path, str]:
    """Validate + atomically persist ``data`` under ``<project>/media/``.

    Returns ``(absolute_path, rel_url_path)``. Raises:

    * ``ValueError`` with a code string for client-facing errors
      (``bad_filename``, ``bad_kind``, ``too_large``, ``exists``).
    """

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("too_large")
    safe = _sanitise_filename(filename)
    if safe is None:
        raise ValueError("bad_filename")
    detected = _kind_for(safe)
    if detected is None or detected != kind:
        raise ValueError("bad_kind")

    media_dir = _ensure_media_dir(project_dir)
    target = media_dir / safe
    if target.exists() and not overwrite:
        raise ValueError("exists")

    # Atomic write: temp file + rename to avoid partial files on crash.
    import tempfile

    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-upload-", dir=str(media_dir))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    return target, studio_url(f"/files/projects/{project_dir.name}/{_MEDIA_SUBDIR}/{safe}")


def register(register_handler: Any) -> None:
    async def handle_media_list(
        request: MediaListRequest, *, hub: WSHub, client_id: str
    ) -> ReplyEnvelope | ErrorEnvelope:
        del hub, client_id

        payload = request.payload
        want_kind: MediaKind | None = payload.kind

        items: list[MediaItem] = []
        if payload.scope == "project":
            if not payload.project_id:
                return ErrorEnvelope(
                    id=request.id,
                    code="bad_request",
                    message="media.list requires project_id when scope is project",
                )
            project_dir = _resolve_project_dir(payload.project_id)
            if project_dir is None:
                return ErrorEnvelope(
                    id=request.id,
                    code="not_found",
                    message=f"unknown project: {payload.project_id}",
                )
            for path in _iter_media(project_dir, want=want_kind):
                kind = _classify(path)
                if kind is None:
                    continue
                items.append(
                    _to_item(path, kind, url=_project_url(payload.project_id, path, project_dir))
                )
        else:  # global
            media_root = get_roots_manager().media_library()
            if media_root is None or not media_root.is_dir():
                # No grant - return empty rather than erroring; the UI
                # already shows a "NO MEDIA" empty slot.
                items = []
            else:
                for path in _iter_media(media_root, want=want_kind):
                    kind = _classify(path)
                    if kind is None:
                        continue
                    items.append(_to_item(path, kind, url=_global_url(path, media_root)))

        # Stable sort: kind, then name (case-insensitive).
        items.sort(key=lambda i: (i.kind, i.name.lower()))

        return ReplyEnvelope(
            id=request.id,
            type="media.list.reply",
            result={
                "scope": payload.scope,
                "kind": payload.kind,
                "items": [i.model_dump() for i in items],
            },
        )

    async def handle_media_upload(
        request: MediaUploadRequest, *, hub: WSHub, client_id: str
    ) -> ReplyEnvelope | ErrorEnvelope:
        del client_id
        import base64
        import binascii

        payload = request.payload
        project_dir = _resolve_project_dir(payload.project_id)
        if project_dir is None:
            return ErrorEnvelope(
                id=request.id,
                code="not_found",
                message=f"unknown project: {payload.project_id}",
            )
        try:
            blob = base64.b64decode(payload.data_base64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return ErrorEnvelope(
                id=request.id, code="bad_request", message=f"invalid base64: {exc}"
            )
        try:
            target, url = write_upload_bytes(
                project_dir,
                kind=payload.kind,
                filename=payload.filename,
                data=blob,
                overwrite=payload.overwrite,
            )
        except ValueError as exc:
            code = str(exc)
            return ErrorEnvelope(
                id=request.id,
                code=code,
                message={
                    "too_large": "upload exceeds 1 GiB cap",
                    "bad_filename": "filename rejected (path traversal or empty)",
                    "bad_kind": "filename extension does not match declared kind",
                    "exists": "target file exists; pass overwrite=true to replace",
                }.get(code, str(exc)),
            )
        await broadcast_media_changed(hub, payload.project_id)
        return ReplyEnvelope(
            type="media.upload.reply",
            id=request.id,
            result={"url": url, "path": str(target)},
        )

    async def handle_media_delete(
        request: MediaDeleteRequest, *, hub: WSHub, client_id: str
    ) -> ReplyEnvelope | ErrorEnvelope:
        del client_id
        payload = request.payload
        project_dir = _resolve_project_dir(payload.project_id)
        if project_dir is None:
            return ErrorEnvelope(
                id=request.id,
                code="not_found",
                message=f"unknown project: {payload.project_id}",
            )
        media_dir = _ensure_media_dir(project_dir)
        # Accept either an absolute on-disk path or a media-relative path. We
        # always re-resolve against media_dir to guarantee the deletion stays
        # inside the project's media folder.
        candidate_rel = payload.path
        if os.path.isabs(payload.path):
            try:
                candidate_rel = str(Path(payload.path).resolve().relative_to(media_dir.resolve()))
            except (OSError, ValueError):
                return ErrorEnvelope(
                    id=request.id,
                    code="bad_request",
                    message="path is outside the project media dir",
                )
        resolved = _resolve_inside(media_dir, candidate_rel)
        if resolved is None or not resolved.is_file():
            return ErrorEnvelope(
                id=request.id, code="not_found", message=f"no file at {payload.path!r}"
            )
        try:
            resolved.unlink()
        except OSError as exc:
            return ErrorEnvelope(
                id=request.id, code="io_error", message=str(exc)
            )
        await broadcast_media_changed(hub, payload.project_id)
        return ReplyEnvelope(
            type="media.delete.reply", id=request.id, result={"ok": True}
        )

    async def handle_media_rename(
        request: MediaRenameRequest, *, hub: WSHub, client_id: str
    ) -> ReplyEnvelope | ErrorEnvelope:
        del client_id
        payload = request.payload
        project_dir = _resolve_project_dir(payload.project_id)
        if project_dir is None:
            return ErrorEnvelope(
                id=request.id,
                code="not_found",
                message=f"unknown project: {payload.project_id}",
            )
        media_dir = _ensure_media_dir(project_dir)
        candidate_rel = payload.path
        if os.path.isabs(payload.path):
            try:
                candidate_rel = str(Path(payload.path).resolve().relative_to(media_dir.resolve()))
            except (OSError, ValueError):
                return ErrorEnvelope(
                    id=request.id,
                    code="bad_request",
                    message="path is outside the project media dir",
                )
        resolved = _resolve_inside(media_dir, candidate_rel)
        if resolved is None or not resolved.is_file():
            return ErrorEnvelope(
                id=request.id, code="not_found", message=f"no file at {payload.path!r}"
            )
        safe_new = _sanitise_filename(payload.new_name)
        if safe_new is None:
            return ErrorEnvelope(
                id=request.id, code="bad_request", message="new_name rejected"
            )
        # Require the new extension to remain in the supported set, but allow
        # cross-kind renames (the UI only renames among siblings, so trust the
        # caller's intent here as long as the new ext is one we know).
        if _kind_for(safe_new) is None:
            return ErrorEnvelope(
                id=request.id, code="bad_kind", message="new_name has unsupported extension"
            )
        new_target = media_dir / safe_new
        if new_target.exists():
            return ErrorEnvelope(
                id=request.id, code="exists", message=f"target {safe_new!r} already exists"
            )
        try:
            os.replace(resolved, new_target)
        except OSError as exc:
            return ErrorEnvelope(
                id=request.id, code="io_error", message=str(exc)
            )
        await broadcast_media_changed(hub, payload.project_id)
        new_url = studio_url(f"/files/projects/{project_dir.name}/{_MEDIA_SUBDIR}/{safe_new}")
        return ReplyEnvelope(
            type="media.rename.reply",
            id=request.id,
            result={"url": new_url, "path": str(new_target)},
        )

    register_handler("media.list", handle_media_list)
    register_handler("media.upload", handle_media_upload)
    register_handler("media.delete", handle_media_delete)
    register_handler("media.rename", handle_media_rename)
