"""Top-level ``Project`` model and cross-cutting validators."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .effects import Captions
from .markers import MarkerSource
from .render_settings import RenderSettings
from .time import (
    BeatRef,
    Frames,
    MarkerRef,
    Seconds,
    TimeRef,
    WordRef,
)
from .tracks import AudioTrack, Track

__all__ = ["PluginRequirement", "Project"]


class PluginRequirement(BaseModel):
    """Pinned plugin requirement declared at project level."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version_spec: str = "*"


class Project(BaseModel):
    """Root timeline model.

    Holds tracks, audio tracks, marker declarations, render settings and a
    list of plugin requirements. The ``schema_version`` field is reserved for
    future migrations.

    ``random_seed`` is the global salt for reproducible randomness across
    every ``Noise``, ``RandomChoice``, ``RandomTrigger`` (and similar)
    node in the project. Per-node seeds are combined with this salt so
    that a project rendered twice with the same ``random_seed`` produces
    identical output.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = "0.1"
    fps: float = Field(gt=0)
    resolution: tuple[int, int]
    duration: float = Field(gt=0)
    tracks: list[Track] = Field(default_factory=list)
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    markers: list[MarkerSource] = Field(default_factory=list)
    render_settings: RenderSettings = Field(default_factory=RenderSettings)
    plugins: list[PluginRequirement] = Field(default_factory=list)
    random_seed: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_consistency(self) -> Project:
        self._validate_segment_durations()
        self._validate_marker_references()
        self._validate_captions_sources()
        return self

    def _validate_segment_durations(self) -> None:
        for track in self.tracks:
            for segment in track.segments:
                start_s = _time_to_seconds_no_markers(segment.start, self.fps)
                in_s = _time_to_seconds_no_markers(segment.in_, self.fps)
                out_s = _time_to_seconds_no_markers(segment.out, self.fps)
                if in_s is None or out_s is None or start_s is None:
                    continue
                clip_duration = out_s - in_s
                if clip_duration <= 0:
                    raise ValueError(
                        f"segment {segment.id!r}: out ({out_s}s) must be greater than in ({in_s}s)"
                    )
                if start_s + clip_duration > self.duration + 1e-6:
                    raise ValueError(
                        f"segment {segment.id!r} ends at {start_s + clip_duration}s "
                        f"which exceeds project duration {self.duration}s"
                    )

    def _validate_marker_references(self) -> None:
        beat_streams: set[str] = set()
        marker_names: set[str] = set()
        for source in self.markers:
            if source.kind == "beat_tracker":
                for stream in source.streams:
                    beat_streams.add(stream)
            marker_names.add(source.name)

        for ref in _walk_time_refs(self):
            if isinstance(ref, BeatRef) and ref.stream not in beat_streams:
                raise ValueError(
                    f"BeatRef references unknown beat stream {ref.stream!r}; "
                    f"declared streams: {sorted(beat_streams) or '[]'}"
                )
            if isinstance(ref, MarkerRef) and ref.name not in marker_names:
                raise ValueError(
                    f"MarkerRef references unknown marker source {ref.name!r}; "
                    f"declared marker sources: {sorted(marker_names) or '[]'}"
                )

    def _validate_captions_sources(self) -> None:
        stt_sources = {s.name for s in self.markers if s.kind == "stt_markers"}
        for caption in _walk_captions(self):
            if caption.source not in stt_sources:
                raise ValueError(
                    f"Captions effect references unknown STT marker source {caption.source!r}; "
                    f"declared STT marker sources: {sorted(stt_sources) or '[]'}"
                )


def _time_to_seconds_no_markers(ref: TimeRef, fps: float) -> float | None:
    if isinstance(ref, Seconds):
        return ref.t
    if isinstance(ref, Frames):
        return ref.n / fps
    return None


def _walk_time_refs(obj: Any) -> list[BeatRef | WordRef | MarkerRef]:
    found: list[BeatRef | WordRef | MarkerRef] = []
    _walk_collect(obj, found)
    return found


def _walk_collect(obj: Any, found: list[BeatRef | WordRef | MarkerRef]) -> None:
    if isinstance(obj, (BeatRef, WordRef, MarkerRef)):
        found.append(obj)
    if isinstance(obj, BaseModel):
        for name in obj.__class__.model_fields:
            _walk_collect(getattr(obj, name), found)
        root = getattr(obj, "root", None)
        if root is not None and root is not obj:
            _walk_collect(root, found)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_collect(item, found)
    elif isinstance(obj, dict):
        for item in obj.values():
            _walk_collect(item, found)


def _walk_captions(obj: Any) -> list[Captions]:
    found: list[Captions] = []
    _walk_captions_collect(obj, found)
    return found


def _walk_captions_collect(obj: Any, found: list[Captions]) -> None:
    if isinstance(obj, Captions):
        found.append(obj)
    if isinstance(obj, BaseModel):
        for name in obj.__class__.model_fields:
            _walk_captions_collect(getattr(obj, name), found)
        root = getattr(obj, "root", None)
        if root is not None and root is not obj:
            _walk_captions_collect(root, found)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_captions_collect(item, found)
    elif isinstance(obj, dict):
        for item in obj.values():
            _walk_captions_collect(item, found)
