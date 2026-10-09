"""Generic audio-onset marker extractor.

Backend chain:

1. **madmom** (``eks-harness[ml]``) - ``CNNOnsetProcessor`` with peak picking.
2. **librosa** - ``librosa.onset.onset_detect`` as the always-available
   fallback.
3. **Stub** - when neither is installed, emits an empty stream and logs
   a single warning. Onsets are typically optional, so we never raise.

Cache layout: ``cache/markers/onset-<source-sha>-<sensitivity>-<backend>.json``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import OnsetMarkers
from eks_harness.video.plugins.base import MarkerExtractor

from ._cache import MarkerCache, compute_audio_digest
from .beat_tracker import _resolve_audio_track_ref

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["OnsetMarkersExtractor"]

_LOG = logging.getLogger(__name__)
_WARNED_NO_BACKEND = False

OnsetBackend = Literal["madmom", "librosa"]
_AUTO_ORDER: tuple[OnsetBackend, ...] = ("madmom", "librosa")


class OnsetMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``OnsetMarkers`` IR sources."""

    name: ClassVar[str] = "onset_markers"
    handles: ClassVar[str] = "onset_markers"

    def extract(self, source: OnsetMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        audio_path = _resolve_audio_path(source.source, ctx)
        if audio_path is None or not audio_path.exists():
            _LOG.warning(
                "onset_markers: cannot resolve audio for source=%r; emitting empty stream",
                source.source,
            )
            return _empty_marker_set(source)

        cache = _open_cache(ctx)
        digest = compute_audio_digest(audio_path)

        onsets: list[float] | None = None
        resolved: str = "stub"
        for backend in _AUTO_ORDER:
            cache_key = f"onset-{digest}-{source.sensitivity:.3f}-{backend}"
            if cache is not None:
                cached = cache.lookup(cache_key)
                if isinstance(cached, dict):
                    onsets = [float(t) for t in cached.get("onset", [])]
                    resolved = backend
                    break

            try:
                onsets = _run_backend(backend, audio_path, source.sensitivity)
            except _BackendUnavailable as exc:
                _LOG.debug("onset backend %s unavailable (%s); trying next", backend, exc)
                onsets = None
            except Exception:
                _LOG.exception("onset backend %s failed for %s; trying next", backend, audio_path)
                onsets = None

            if onsets is not None:
                resolved = backend
                if cache is not None:
                    cache.store(cache_key, {"onset": onsets})
                break

        if cache is not None:
            cache.close()

        if onsets is None:
            _warn_no_backend()
            onsets = []

        _LOG.debug("onset_markers resolved backend=%s count=%d", resolved, len(onsets))
        return _onsets_to_marker_set(source, onsets)


class _BackendUnavailable(RuntimeError):
    pass


def _run_backend(backend: OnsetBackend, audio_path: Path, sensitivity: float) -> list[float]:
    if backend == "madmom":
        return _run_madmom(audio_path, sensitivity)
    if backend == "librosa":
        return _run_librosa(audio_path, sensitivity)
    raise _BackendUnavailable(f"unknown backend {backend!r}")


def _run_madmom(audio_path: Path, sensitivity: float) -> list[float]:
    try:
        from madmom.features.onsets import CNNOnsetProcessor  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _BackendUnavailable("madmom not installed") from exc

    try:
        activations = CNNOnsetProcessor()(str(audio_path))
    except Exception as exc:
        raise _BackendUnavailable(f"CNNOnsetProcessor failed: {exc}") from exc

    threshold = max(0.05, min(0.95, 1.0 - float(sensitivity)))
    return _peak_pick(activations, fps=100, threshold=threshold)


def _run_librosa(audio_path: Path, sensitivity: float) -> list[float]:
    try:
        import librosa
    except ImportError as exc:
        raise _BackendUnavailable("librosa not installed") from exc

    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    # ``delta`` is the peak-picking threshold relative to the local mean.
    # Higher sensitivity -> lower delta -> more onsets.
    delta = max(0.01, min(1.0, 1.0 - float(sensitivity)))
    frames = librosa.onset.onset_detect(y=y, sr=sr, backtrack=True, delta=delta)
    return [float(t) for t in librosa.frames_to_time(frames, sr=sr).tolist()]


def _peak_pick(activations: Any, fps: int, threshold: float) -> list[float]:
    import numpy as np

    arr = np.asarray(activations, dtype=float).ravel()
    peaks: list[float] = []
    for i in range(1, len(arr) - 1):
        if arr[i] >= threshold and arr[i] > arr[i - 1] and arr[i] >= arr[i + 1]:
            peaks.append(i / fps)
    return peaks


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


def _warn_no_backend() -> None:
    global _WARNED_NO_BACKEND
    if _WARNED_NO_BACKEND:
        return
    _LOG.warning(
        "no onset backend available; install `eks-harness[ml]` (madmom) or `pip install librosa`"
    )
    _WARNED_NO_BACKEND = True


def _empty_marker_set(source: OnsetMarkers) -> MarkerSet:
    out = MarkerSet()
    out.streams[source.name] = []
    out.named[source.name] = []
    return out


def _onsets_to_marker_set(source: OnsetMarkers, onsets: list[float]) -> MarkerSet:
    out = MarkerSet()
    sorted_onsets = sorted(float(t) for t in onsets)
    out.streams[source.name] = sorted_onsets
    out.named[source.name] = list(sorted_onsets)
    return out
