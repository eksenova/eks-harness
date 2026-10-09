"""Silence-region marker extractor.

Backend chain:

1. **librosa** - RMS over short frames; regions whose dB level stays
   below ``threshold_db`` for at least ``min_duration`` seconds yield
   a marker at the region's start.
2. **Stub** - when librosa is unavailable, emits an empty stream and
   logs a single warning. Silence cues are typically optional, so we
   never raise here.

Cache layout: ``cache/markers/silence-<source-sha>-<threshold>-<min-duration>.json``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import SilenceMarkers
from eks_harness.video.plugins.base import MarkerExtractor

from ._cache import MarkerCache, compute_audio_digest
from .beat_tracker import _resolve_audio_track_ref

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["SilenceMarkersExtractor"]

_LOG = logging.getLogger(__name__)
_WARNED_NO_LIBROSA = False


class SilenceMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``SilenceMarkers`` IR sources."""

    name: ClassVar[str] = "silence_markers"
    handles: ClassVar[str] = "silence_markers"

    def extract(self, source: SilenceMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        audio_path = _resolve_audio_path(source.source, ctx)
        if audio_path is None or not audio_path.exists():
            _LOG.warning(
                "silence_markers: cannot resolve audio for source=%r; emitting empty stream",
                source.source,
            )
            return _empty_marker_set(source)

        cache = _open_cache(ctx)
        cache_key = (
            f"silence-{compute_audio_digest(audio_path)}-"
            f"{source.threshold_db:.3f}-{source.min_duration:.3f}"
        )

        starts: list[float] | None = None
        if cache is not None:
            cached = cache.lookup(cache_key)
            if isinstance(cached, dict):
                starts = [float(t) for t in cached.get("silence", [])]

        if starts is None:
            try:
                starts = _detect_silences(audio_path, source.threshold_db, source.min_duration)
            except _LibrosaUnavailable:
                _warn_missing_librosa()
                starts = []
            except Exception:
                _LOG.exception(
                    "silence detection failed for %s; emitting empty stream", audio_path
                )
                starts = []
            if cache is not None:
                cache.store(cache_key, {"silence": starts})

        if cache is not None:
            cache.close()

        return _starts_to_marker_set(source, starts)


class _LibrosaUnavailable(RuntimeError):
    pass


def _detect_silences(audio_path: Path, threshold_db: float, min_duration: float) -> list[float]:
    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise _LibrosaUnavailable from exc

    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    if y.size == 0:
        return []

    frame_length = 2048
    hop_length = 512
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length)[0]
    db = librosa.amplitude_to_db(np.maximum(rms, 1e-10), ref=1.0)
    times = librosa.frames_to_time(np.arange(len(db)), sr=sr, hop_length=hop_length)

    silent = db < threshold_db
    starts: list[float] = []
    in_silence = False
    region_start_idx = 0
    for i, is_silent in enumerate(silent):
        if is_silent and not in_silence:
            in_silence = True
            region_start_idx = i
        elif not is_silent and in_silence:
            in_silence = False
            duration = float(times[i] - times[region_start_idx])
            if duration >= min_duration:
                starts.append(float(times[region_start_idx]))
    if in_silence:
        duration = float(times[-1] - times[region_start_idx])
        if duration >= min_duration:
            starts.append(float(times[region_start_idx]))

    return sorted({round(t, 9) for t in starts})


def _resolve_audio_path(spec: str, ctx: RenderContext | None) -> Path | None:
    candidate = Path(spec)
    if candidate.exists():
        return candidate
    if ctx is not None:
        ws_candidate = ctx.workspace / spec
        if ws_candidate.exists():
            return ws_candidate
    if ctx is not None and spec.startswith("audio_tracks[") and spec.endswith("]"):
        return _resolve_audio_track_ref(spec[len("audio_tracks["):-1], ctx)
    return None


def _open_cache(ctx: RenderContext | None) -> MarkerCache | None:
    if ctx is None or getattr(ctx, "cache_dir", None) is None:
        return None
    return MarkerCache(ctx.cache_dir)


def _warn_missing_librosa() -> None:
    global _WARNED_NO_LIBROSA
    if _WARNED_NO_LIBROSA:
        return
    _LOG.warning(
        "librosa not installed; install via `pip install librosa` to enable silence markers"
    )
    _WARNED_NO_LIBROSA = True


def _empty_marker_set(source: SilenceMarkers) -> MarkerSet:
    out = MarkerSet()
    out.streams[source.name] = []
    out.named[source.name] = []
    return out


def _starts_to_marker_set(source: SilenceMarkers, starts: list[float]) -> MarkerSet:
    out = MarkerSet()
    sorted_starts = sorted(float(t) for t in starts)
    out.streams[source.name] = sorted_starts
    out.named[source.name] = list(sorted_starts)
    return out
