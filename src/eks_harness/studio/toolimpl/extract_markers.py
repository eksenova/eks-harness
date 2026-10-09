"""``extract_markers`` tool.

Runs the same marker extraction pass the renderer uses, but standalone so an
LLM client can inspect the resolved beat / word / energy / face / motion
streams a project will sync against before committing to a render.

Optional ML backends (madmom, faster-whisper, opencv, …) are NOT required.
When a backend is missing the extractor returns empty streams plus a
``warnings`` entry; this tool surfaces those rather than raising.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eks_harness.video.compile.markers import extract_markers

from ..roots import RootsManager
from ._project_io import load_project_any, resolve_project_path

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run(project_path: Path, *, roots: RootsManager) -> dict[str, Any]:
    resolved = resolve_project_path(project_path, roots)
    loaded = load_project_any(resolved)
    project = loaded.project

    warnings: list[str] = []
    try:
        marker_set = extract_markers(project)
    except Exception as exc:
        _LOG.exception("extract_markers failed")
        return {
            "source_path": str(loaded.source_path),
            "streams": {},
            "named": {},
            "words": [],
            "warnings": [f"extraction failed: {exc}"],
        }

    declared_streams = _declared_streams(project)
    for stream in declared_streams:
        if stream not in marker_set.streams or not marker_set.streams[stream]:
            warnings.append(
                f"stream {stream!r} produced no markers - backend may be unavailable"
            )
    declared_named = {source.name for source in project.markers}
    for name in declared_named:
        if name not in marker_set.named or not marker_set.named[name]:
            warnings.append(
                f"marker source {name!r} produced no markers - backend may be unavailable"
            )

    return {
        "source_path": str(loaded.source_path),
        "streams": {k: list(v) for k, v in marker_set.streams.items()},
        "named": {k: list(v) for k, v in marker_set.named.items()},
        "words": [
            {
                "text": w.text,
                "t_start": w.t_start,
                "t_end": w.t_end,
                "source": w.source,
            }
            for w in marker_set.words
        ],
        "warnings": warnings,
    }


def _declared_streams(project: Any) -> set[str]:
    streams: set[str] = set()
    for source in project.markers:
        for stream in getattr(source, "streams", []) or []:
            streams.add(stream)
    return streams

DESCRIPTION = 'Run every declared MarkerSource through its extractor and return {streams, named, words, warnings}. Missing optional ML backends (madmom/faster-whisper/opencv) are reported via warnings, never raised.'
run = _run
