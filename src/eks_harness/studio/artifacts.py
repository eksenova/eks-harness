"""Render artifact metadata + thumbnail extraction.

Each successful (or failed) render leaves a small dossier on disk so the
dev-server UI can browse history without re-running the renderer:

* ``<project>/renders/<job_id>/<filename>.mp4`` - the output (or absent on
  failure).
* ``<project>/renders/<job_id>/metadata.json`` - the
  :class:`~eks_harness.studio.jobs.JobRecord` snapshot at completion plus a
  ``frame``-stripped log of every emitted progress event.
* ``<project>/renders/<job_id>/thumbnail.jpg`` - single-frame poster, ffmpeg
  best-effort (missing ffmpeg is non-fatal).

The functions here are pure utilities - no MCP, no FastMCP context, no
WebSocket hub dependencies - so they're reusable from any render driver
caller (legacy inline path, task-augmented path, dev-server bridge).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .dev.ws_protocol import Artifact
from .jobs import JobRecord
from .urls import url as studio_url

__all__ = [
    "write_metadata",
    "extract_thumbnail",
    "list_artifacts",
    "read_metadata",
    "build_artifact",
]

_LOG = logging.getLogger(__name__)

_METADATA_SCHEMA_VERSION = 1


def _scrape_failure_summary(
    events: list[dict[str, Any]],
    record_dict: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a ``failure_summary`` dict by walking the tail of the event log.

    Looks for the most recent ``error`` event, then falls back to the most
    recent ``log`` event with ``level=='error'`` or ``'warning'``. ``last_log``
    is always populated when at least one log line exists so the UI has
    something to render even when only stderr noise is available.
    """

    category = record_dict.get("error_category")
    record_error = record_dict.get("error") or None

    tail = events[-10:] if events else []
    error_event: dict[str, Any] | None = None
    last_log_msg: str | None = None
    last_error_log: dict[str, Any] | None = None

    for ev in reversed(tail):
        kind = ev.get("event")
        if kind == "error" and error_event is None:
            error_event = ev
        elif kind == "log":
            level = str(ev.get("level", "")).lower()
            msg = str(ev.get("message", "")).strip()
            if last_log_msg is None and msg:
                last_log_msg = msg
            if level in ("error", "critical") and last_error_log is None:
                last_error_log = ev

    if error_event is None and last_error_log is None and not record_error:
        if last_log_msg is None and not category:
            return None

    message = (
        (error_event or {}).get("message")
        or (last_error_log or {}).get("message")
        or record_error
        or last_log_msg
        or "render failed"
    )
    traceback = (
        (error_event or {}).get("traceback")
        or (last_error_log or {}).get("traceback")
    )
    summary: dict[str, Any] = {
        "category": category,
        "message": str(message).strip(),
    }
    if traceback:
        summary["traceback"] = str(traceback)
    if last_log_msg:
        summary["last_log"] = last_log_msg
    return summary


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via temp file + replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def write_metadata(
    *,
    output_path: Path,
    record: JobRecord,
    condensed_events: list[dict[str, Any]],
) -> Path:
    """Persist the job's terminal snapshot + condensed event log next to the output.

    The metadata file always lives alongside the output (``output_path.parent``)
    so the artifacts scanner can pair them by directory.
    """

    metadata_path = output_path.parent / "metadata.json"
    record_dict = record.to_dict()
    body: dict[str, Any] = {
        "schema_version": _METADATA_SCHEMA_VERSION,
        "record": record_dict,
        "events": list(condensed_events),
    }
    if str(record_dict.get("status", "")) == "failed":
        failure_summary = _scrape_failure_summary(list(condensed_events), record_dict)
        if failure_summary is not None:
            body["failure_summary"] = failure_summary
    _atomic_write_text(metadata_path, json.dumps(body, indent=2, sort_keys=True))
    return metadata_path


def extract_thumbnail(
    *,
    output_path: Path,
    dest: Path | None = None,
) -> Path | None:
    """Pull a poster JPEG from ``output_path`` via ffmpeg. Returns ``None`` on failure.

    Thumbnails are nice-to-have; a missing or broken ffmpeg should never block
    metadata writing or fail a render, so every error path swallows.
    """

    if not output_path.exists():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        _LOG.debug("ffmpeg not on PATH; skipping thumbnail")
        return None
    target = dest if dest is not None else output_path.parent / "thumbnail.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg,
        "-y",
        "-ss", "0",
        "-i", str(output_path),
        "-frames:v", "1",
        "-q:v", "4",
        str(target),
    ]
    try:
        # Use a subprocess.run-style call but tolerate any failure shape.
        import subprocess

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except Exception:
        _LOG.debug("ffmpeg invocation raised", exc_info=True)
        return None
    if result.returncode != 0 or not target.exists():
        _LOG.debug(
            "ffmpeg thumbnail failed (rc=%s): %s",
            result.returncode,
            result.stderr[-200:] if result.stderr else b"",
        )
        return None
    return target


async def extract_thumbnail_async(
    *,
    output_path: Path,
    dest: Path | None = None,
) -> Path | None:
    """Async variant - runs ``extract_thumbnail`` in a worker thread."""

    return await asyncio.to_thread(
        extract_thumbnail, output_path=output_path, dest=dest
    )


def build_artifact(
    *,
    project_id: str,
    job_id: str,
    record_dict: dict[str, Any],
    output_path: Path | None,
    thumbnail_path: Path | None,
    metadata_started_at: float | None = None,
    failure_summary: dict[str, Any] | None = None,
) -> Artifact:
    """Construct an :class:`Artifact` model from a metadata record + on-disk paths."""

    started_at = float(
        record_dict.get("started_at", metadata_started_at or 0.0) or 0.0
    )
    finished_at = record_dict.get("updated_at")
    finished_at_f = float(finished_at) if finished_at is not None else None
    duration_s = (
        finished_at_f - started_at if finished_at_f is not None else None
    )

    output_url: str | None = None
    if output_path is not None and output_path.exists():
        output_url = (
            studio_url(f"/files/projects/{project_id}/renders/{job_id}/{output_path.name}")
        )
    thumbnail_url: str | None = None
    if thumbnail_path is not None and thumbnail_path.exists():
        thumbnail_url = (
            studio_url(f"/files/projects/{project_id}/renders/{job_id}/{thumbnail_path.name}")
        )

    return Artifact(
        job_id=job_id,
        mode=str(record_dict.get("mode", "")),
        status=str(record_dict.get("status", "")),
        started_at=started_at,
        finished_at=finished_at_f,
        duration_s=duration_s,
        output_url=output_url,
        thumbnail_url=thumbnail_url,
        total_frames=record_dict.get("frame_total") or None,
        error_category=record_dict.get("error_category"),
        failure_summary=failure_summary,
    )


def _output_path_from_record(renders_dir: Path, job_id: str, record_dict: dict[str, Any]) -> Path | None:
    """Best-effort recovery of the on-disk output path for a job.

    Prefer the ``record.output_path`` URI; fall back to the lone mp4 (or any
    media file) sitting under ``renders/<job_id>/``.
    """

    raw = record_dict.get("output_path")
    if isinstance(raw, str) and raw:
        from urllib.parse import urlparse, unquote

        if raw.startswith("file://"):
            parsed = urlparse(raw)
            path = unquote(parsed.path)
            if path.startswith("/") and len(path) > 3 and path[2] == ":":
                path = path[1:]
            candidate = Path(path)
        else:
            candidate = Path(raw)
        if candidate.exists():
            return candidate
    # Fallback: scan the job dir for a mp4.
    job_dir = renders_dir / job_id
    if not job_dir.exists():
        return None
    for entry in sorted(job_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            return entry
    return None


def list_artifacts(project_dir: Path) -> list[Artifact]:
    """Scan ``<project>/renders/*/metadata.json`` and return one Artifact each.

    Newest-first by ``record.started_at``. Entries without a parseable
    ``metadata.json`` are skipped - they're either in-flight or salvageable
    only via the live registry.
    """

    renders_dir = project_dir / "renders"
    if not renders_dir.is_dir():
        return []
    project_id = project_dir.name

    out: list[Artifact] = []
    for job_dir in renders_dir.iterdir():
        if not job_dir.is_dir():
            continue
        metadata = job_dir / "metadata.json"
        if not metadata.is_file():
            continue
        try:
            body = json.loads(metadata.read_text(encoding="utf-8"))
        except Exception:
            _LOG.debug("could not parse %s", metadata, exc_info=True)
            continue
        record_dict = body.get("record") or {}
        job_id = str(record_dict.get("job_id") or job_dir.name)
        output_path = _output_path_from_record(renders_dir, job_id, record_dict)
        thumb_path = job_dir / "thumbnail.jpg"
        failure_summary = body.get("failure_summary") if isinstance(body, dict) else None
        if not isinstance(failure_summary, dict):
            failure_summary = None
        artifact = build_artifact(
            project_id=project_id,
            job_id=job_id,
            record_dict=record_dict,
            output_path=output_path,
            thumbnail_path=thumb_path if thumb_path.exists() else None,
            failure_summary=failure_summary,
        )
        out.append(artifact)

    out.sort(key=lambda a: a.started_at, reverse=True)
    return out


def read_metadata(*, project_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Return the parsed ``metadata.json`` for one job, or ``None`` if absent."""

    path = project_dir / "renders" / job_id / "metadata.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _LOG.debug("could not parse %s", path, exc_info=True)
        return None
