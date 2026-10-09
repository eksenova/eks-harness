"""``inspect_project`` tool.

Returns a structured summary of the loaded project so an LLM client can
reason about the timeline shape, the effect-compile split, the declared
marker sources, and the project-level random seed without re-deriving any
of that from raw JSON.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eks_harness.video.ir import Project, Track
from eks_harness.video.ir.effects import EffectIR

from ..roots import RootsManager
from ._project_io import load_project_any, resolve_project_path

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run(project_path: Path, *, roots: RootsManager) -> dict[str, Any]:
    resolved = resolve_project_path(project_path, roots)
    loaded = load_project_any(resolved)
    project = loaded.project
    return {
        "source_path": str(loaded.source_path),
        "source_kind": loaded.source_kind,
        "timeline_summary": _timeline_summary(project),
        "compile_split": _compile_split(project),
        "marker_sources": _marker_sources(project),
        "random_seed": project.random_seed,
    }


def _timeline_summary(project: Project) -> dict[str, Any]:
    tracks_info: list[dict[str, Any]] = []
    total_effects = 0
    for track in project.tracks:
        effect_count = sum(len(seg.effects) for seg in track.segments)
        total_effects += effect_count
        tracks_info.append(
            {
                "name": track.name,
                "z": track.z,
                "segment_count": len(track.segments),
                "effect_count": effect_count,
            }
        )
    audio_tracks_info = [
        {"name": at.name, "segment_count": len(at.segments)} for at in project.audio_tracks
    ]
    return {
        "fps": project.fps,
        "resolution": list(project.resolution),
        "duration": project.duration,
        "track_count": len(project.tracks),
        "audio_track_count": len(project.audio_tracks),
        "total_effect_count": total_effects,
        "tracks": tracks_info,
        "audio_tracks": audio_tracks_info,
    }


def _compile_split(project: Project) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for track in project.tracks:
        for segment in track.segments:
            for index, effect in enumerate(segment.effects):
                entries.append(_describe_effect_target(track, segment.id, index, effect))
    return entries


def _describe_effect_target(
    track: Track,
    segment_id: str,
    effect_index: int,
    effect: Any,
) -> dict[str, Any]:
    targets = sorted(getattr(effect, "compile_targets", frozenset()))
    chosen = _pick_target(targets)
    return {
        "track": track.name,
        "segment_id": segment_id,
        "effect_index": effect_index,
        "kind": getattr(effect, "kind", effect.__class__.__name__),
        "compile_targets": targets,
        "selected_target": chosen,
    }


def _pick_target(targets: list[str]) -> str | None:
    """Mirror the orchestrator's preference: ffmpeg_graph beats frame_pipeline."""

    if not targets:
        return None
    if "ffmpeg_graph" in targets:
        return "ffmpeg_graph"
    if "frame_pipeline" in targets:
        return "frame_pipeline"
    return targets[0]


def _marker_sources(project: Project) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source in project.markers:
        entry: dict[str, Any] = {
            "name": source.name,
            "kind": source.kind,
            "source": getattr(source, "source", None),
        }
        streams = getattr(source, "streams", None)
        if streams is not None:
            entry["streams"] = list(streams)
        entries.append(entry)
    return entries


# Keep EffectIR import live for type-hint clarity even though we only read attrs.
_ = EffectIR

DESCRIPTION = 'Summarize a video project: timeline shape, effect compile-target split (ffmpeg_graph vs frame_pipeline), declared marker sources, and random_seed. Useful for debugging slow renders.'
run = _run
