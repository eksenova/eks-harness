"""Resolve symbolic ``TimeRef`` instances to concrete seconds / frame indices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eks_harness.video.ir.time import (
    BeatRef,
    Frames,
    MarkerRef,
    Seconds,
    WordRef,
)

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project
    from eks_harness.video.ir.time import TimeRef


def resolve_time(ref: TimeRef, project: Project, markers: MarkerSet) -> float:
    """Return the absolute time in seconds for ``ref``."""

    if isinstance(ref, Seconds):
        return ref.t
    if isinstance(ref, Frames):
        return ref.n / project.fps
    if isinstance(ref, BeatRef):
        return _resolve_beat(ref, project, markers)
    if isinstance(ref, WordRef):
        return _resolve_word(ref, markers)
    if isinstance(ref, MarkerRef):
        return _resolve_marker(ref, markers)
    raise TypeError(f"unrecognized TimeRef variant: {type(ref).__name__}")


def resolve_time_to_frame(ref: TimeRef, project: Project, markers: MarkerSet) -> int:
    """Return the frame index (rounded) for ``ref``."""

    seconds = resolve_time(ref, project, markers)
    return round(seconds * project.fps)


def _resolve_beat(ref: BeatRef, project: Project, markers: MarkerSet) -> float:
    if ref.stream not in markers.streams:
        raise LookupError(f"beat stream {ref.stream!r} not present in extracted markers")
    times = markers.streams[ref.stream]
    if ref.range is not None:
        lo = resolve_time(ref.range[0], project, markers)
        hi = resolve_time(ref.range[1], project, markers)
        times = [t for t in times if lo - 1e-9 <= t <= hi + 1e-9]
    if not times:
        raise LookupError(f"no beats in stream {ref.stream!r} within requested range")
    selected = times[:: ref.every][0] if ref.every > 1 else times[0]
    return float(selected) + ref.offset_ms / 1000.0


def _resolve_word(ref: WordRef, markers: MarkerSet) -> float:
    candidates = markers.words
    if ref.source is not None:
        candidates = [w for w in candidates if w.source == ref.source]
    if ref.text is not None:
        text = ref.text.lower()
        for word in candidates:
            if word.text.lower() == text:
                return word.t_start
        raise LookupError(f"word {ref.text!r} not found in STT markers")
    assert ref.index is not None
    if ref.index < 0 or ref.index >= len(candidates):
        raise IndexError(f"word index {ref.index} out of range (have {len(candidates)})")
    return candidates[ref.index].t_start


def _resolve_marker(ref: MarkerRef, markers: MarkerSet) -> float:
    if ref.name not in markers.named:
        raise LookupError(f"marker {ref.name!r} not present in extracted markers")
    times = markers.named[ref.name]
    if ref.index is None:
        return float(times[0])
    if ref.index < 0 or ref.index >= len(times):
        raise IndexError(f"marker {ref.name!r} index {ref.index} out of range (have {len(times)})")
    return float(times[ref.index])


__all__ = ["resolve_time", "resolve_time_to_frame"]
