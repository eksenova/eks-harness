"""Video studio tools as plain functions (validate, inspect, render, cancel, markers, effects, edits).

``TOOLS`` maps each tool name to its function and description so an MCP
server, the HTTP API or the CLI can expose the same set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .roots import RootsManager, get_roots_manager
from .toolimpl import add_effect as _add_effect
from .toolimpl import add_segment as _add_segment
from .toolimpl import cancel_job as _cancel_job
from .toolimpl import extract_markers as _extract_markers
from .toolimpl import inspect_project as _inspect_project
from .toolimpl import list_effects as _list_effects
from .toolimpl import render_project as _render_project
from .toolimpl import set_property as _set_property
from .toolimpl import validate_project as _validate_project

__all__ = [
    "TOOLS",
    "add_effect",
    "add_segment",
    "cancel_job",
    "extract_markers",
    "inspect_project",
    "list_effects",
    "render_project",
    "set_property",
    "validate_project",
]


def _roots(roots: RootsManager | None) -> RootsManager:
    return roots or get_roots_manager()


def validate_project(project_path: str | Path, *, roots: RootsManager | None = None) -> dict[str, Any]:
    return _validate_project.run(Path(project_path), roots=_roots(roots))


def inspect_project(project_path: str | Path, *, roots: RootsManager | None = None) -> dict[str, Any]:
    return _inspect_project.run(Path(project_path), roots=_roots(roots))


def extract_markers(project_path: str | Path, *, roots: RootsManager | None = None) -> dict[str, Any]:
    return _extract_markers.run(Path(project_path), roots=_roots(roots))


def list_effects() -> dict[str, Any]:
    return _list_effects.run()


def add_segment(project_path: str | Path, track_name: str, segment_json: dict[str, Any], *,
                create_track: bool = False, roots: RootsManager | None = None) -> dict[str, Any]:
    return _add_segment.run(project_path=Path(project_path), track_name=track_name, segment_json=segment_json,
                            create_track=create_track, roots=_roots(roots))


def add_effect(project_path: str | Path, segment_id: str, effect_json: dict[str, Any], *,
               roots: RootsManager | None = None) -> dict[str, Any]:
    return _add_effect.run(project_path=Path(project_path), segment_id=segment_id, effect_json=effect_json,
                           roots=_roots(roots))


def set_property(project_path: str | Path, path: str, value: Any, *,
                 roots: RootsManager | None = None) -> dict[str, Any]:
    return _set_property.run(project_path=Path(project_path), path=path, value=value, roots=_roots(roots))


render_project = _render_project.render
cancel_job = _cancel_job.cancel

TOOLS: dict[str, tuple[Any, str]] = {
    "validate_project": (validate_project, _validate_project.DESCRIPTION),
    "inspect_project": (inspect_project, _inspect_project.DESCRIPTION),
    "render_project": (render_project, _render_project.DESCRIPTION),
    "cancel_job": (cancel_job, _cancel_job.DESCRIPTION),
    "extract_markers": (extract_markers, _extract_markers.DESCRIPTION),
    "list_effects": (list_effects, _list_effects.DESCRIPTION),
    "add_segment": (add_segment, _add_segment.DESCRIPTION),
    "add_effect": (add_effect, _add_effect.DESCRIPTION),
    "set_property": (set_property, _set_property.DESCRIPTION),
}
