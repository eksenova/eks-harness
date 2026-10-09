"""``add_segment`` tool.

Appends a serialized :class:`eks_harness.video.ir.Segment` to a named video track in
``project.json``. Mutation tools never touch ``project.py`` - Python source
is not round-trip-editable in a safe, lossless way. The full project is
re-validated against :class:`eks_harness.video.ir.Project` before the file is
rewritten so partial edits never break the snapshot on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from eks_harness.video.ir import Project, Segment, Track

from ..roots import RootsManager
from ._project_io import load_project_json, resolve_project_path

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run(
    *,
    project_path: Path,
    track_name: str,
    segment_json: dict[str, Any],
    create_track: bool,
    roots: RootsManager,
) -> dict[str, Any]:
    resolved = resolve_project_path(project_path, roots)
    json_path, raw = load_project_json(resolved)

    segment = Segment.model_validate(segment_json, by_alias=True)

    tracks_raw = raw.setdefault("tracks", [])
    if not isinstance(tracks_raw, list):
        raise ValueError("tracks field in project.json is not a list")

    target_track = _find_track_dict(tracks_raw, track_name)
    if target_track is None:
        if not create_track:
            raise ValueError(
                f"track {track_name!r} not found; pass create_track=True to add it"
            )
        target_track = _new_track_dict(track_name)
        tracks_raw.append(target_track)

    segments = target_track.setdefault("segments", [])
    if not isinstance(segments, list):
        raise ValueError(f"track {track_name!r} segments field is not a list")
    segments.append(segment.model_dump(by_alias=True))

    try:
        validated = Project.model_validate(raw, by_alias=True)
    except ValidationError as exc:
        raise _validation_error_with_context("add_segment", exc) from exc

    json_path.write_text(
        validated.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "updated_path": str(json_path),
        "track_name": track_name,
        "segment_id": segment.id,
        "created_track": create_track and target_track is tracks_raw[-1],
    }


def _find_track_dict(tracks: list[Any], name: str) -> dict[str, Any] | None:
    for track in tracks:
        if isinstance(track, dict) and track.get("name") == name:
            return track
    return None


def _new_track_dict(name: str) -> dict[str, Any]:
    """Build a defaults-filled track dict matching :class:`Track`'s defaults."""

    fresh = Track(name=name)
    return fresh.model_dump(by_alias=True)


def _validation_error_with_context(op: str, exc: ValidationError) -> ValidationError:
    _LOG.warning("%s rejected by Project validator: %s", op, exc.errors())
    return exc

DESCRIPTION = 'Append a Segment (as JSON dict) to a named video track in project.json. Set create_track=True to add a new track when track_name does not match an existing one. Re-validates the whole project before writing.'
run = _run
