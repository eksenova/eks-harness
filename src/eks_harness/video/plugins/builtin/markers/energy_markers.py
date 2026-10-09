"""Band-filtered energy marker extractor.

Computes the RMS of an audio source over short frames (optionally
band-pass filtered), then emits a marker at every frame whose RMS
exceeds the configured ``percentile`` of the whole-source distribution.

Backend: librosa only. When librosa is unavailable the extractor emits
an empty stream and logs a single warning.

Band ranges (Hz):

* ``low``  -- ``20  -  250``
* ``mid``  -- ``250 - 2000``
* ``high`` -- ``2000 - 8000``
* ``full`` -- no filtering

Cache layout: ``cache/markers/energy-<source-sha>-<band>-<percentile>.json``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import EnergyBand, EnergyMarkers
from eks_harness.video.plugins.base import MarkerExtractor

from ._cache import MarkerCache, compute_audio_digest
from .beat_tracker import _resolve_audio_track_ref

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["EnergyMarkersExtractor"]

_LOG = logging.getLogger(__name__)
_WARNED_NO_LIBROSA = False

_BAND_RANGES: dict[EnergyBand, tuple[float, float] | None] = {
    "low": (20.0, 250.0),
    "mid": (250.0, 2000.0),
    "high": (2000.0, 8000.0),
    "full": None,
}


class EnergyMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``EnergyMarkers`` IR sources."""

    name: ClassVar[str] = "energy_markers"
    handles: ClassVar[str] = "energy_markers"

    def extract(self, source: EnergyMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        audio_path = _resolve_audio_path(source.source, ctx)
        if audio_path is None or not audio_path.exists():
            _LOG.warning(
                "energy_markers: cannot resolve audio for source=%r; emitting empty stream",
                source.source,
            )
            return _empty_marker_set(source)

        cache = _open_cache(ctx)
        cache_key = (
            f"energy-{compute_audio_digest(audio_path)}-{source.band}-{source.percentile:.4f}"
        )

        peaks: list[float] | None = None
        if cache is not None:
            cached = cache.lookup(cache_key)
            if isinstance(cached, dict):
                peaks = [float(t) for t in cached.get("energy", [])]

        if peaks is None:
            try:
                peaks = _detect_energy_peaks(audio_path, source.band, source.percentile)
            except _LibrosaUnavailable:
                _warn_missing_librosa()
                peaks = []
            except Exception:
                _LOG.exception(
                    "energy detection failed for %s; emitting empty stream", audio_path
                )
                peaks = []
            if cache is not None:
                cache.store(cache_key, {"energy": peaks})

        if cache is not None:
            cache.close()

        return _peaks_to_marker_set(source, peaks)


class _LibrosaUnavailable(RuntimeError):
    pass


def _detect_energy_peaks(
    audio_path: Path, band: EnergyBand, percentile: float
) -> list[float]:
    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise _LibrosaUnavailable from exc

    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    if y.size == 0:
        return []

    filtered = _bandpass(y, sr, _BAND_RANGES[band])

    frame_length = 2048
    hop_length = 512
    rms = librosa.feature.rms(y=filtered, frame_length=frame_length, hop_length=hop_length)[0]
    if rms.size == 0:
        return []

    pct = float(np.clip(percentile, 0.0, 1.0))
    cutoff = float(np.quantile(rms, pct))
    if cutoff <= 0.0:
        return []

    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)
    peaks: list[float] = []
    above = rms >= cutoff
    # Collapse contiguous runs into a single event at the run's start so
    # callers do not get a dense burst per loud region.
    in_run = False
    for i, is_above in enumerate(above):
        if is_above and not in_run:
            peaks.append(float(times[i]))
            in_run = True
        elif not is_above and in_run:
            in_run = False
    return sorted({round(t, 9) for t in peaks})


def _bandpass(y: Any, sr: int, band: tuple[float, float] | None) -> Any:
    if band is None:
        return y
    try:
        import numpy as np
        from scipy.signal import butter, sosfiltfilt  # type: ignore[import-not-found]
    except ImportError:
        return y

    low_hz, high_hz = band
    nyquist = 0.5 * float(sr)
    low = max(1e-3, low_hz / nyquist)
    high = min(0.999, high_hz / nyquist)
    if high <= low:
        return y
    sos = butter(N=4, Wn=[low, high], btype="bandpass", output="sos")
    return np.asarray(sosfiltfilt(sos, y), dtype=y.dtype)


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
        "librosa not installed; install via `pip install librosa` to enable energy markers"
    )
    _WARNED_NO_LIBROSA = True


def _empty_marker_set(source: EnergyMarkers) -> MarkerSet:
    out = MarkerSet()
    out.streams[source.name] = []
    out.named[source.name] = []
    return out


def _peaks_to_marker_set(source: EnergyMarkers, peaks: list[float]) -> MarkerSet:
    out = MarkerSet()
    sorted_peaks = sorted(float(t) for t in peaks)
    out.streams[source.name] = sorted_peaks
    out.named[source.name] = list(sorted_peaks)
    return out
