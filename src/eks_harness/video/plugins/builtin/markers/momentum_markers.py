"""Musical momentum extractor: drops, builds, breaks, sections and peaks.

The extractor fuses four frame-level descriptors into a single
``momentum`` curve in ``[0, 1]``:

* loudness (RMS in dB),
* onset density (smoothed onset strength),
* low-end weight (20-150 Hz band energy, the kick/bass body),
* brightness (spectral centroid, rises through risers and drops).

Each descriptor is normalised against its own 5th/95th percentiles so a
quiet master and a brickwalled one land on the same scale, then the
weighted sum is Gaussian-smoothed with ``smoothing`` seconds of sigma.

Events derived from the curve:

* ``drop``: half the momentum jump (mean of the 0.5 s after ``t`` minus
  the 1.5 s before) plus half the low-end jump (the same on the 20-150 Hz
  band, 0.4 s after vs 1.0 s before), because a drop is where the kick and
  bass arrive, not where a bright riser starts. Peaks of that score above
  ``drop_threshold``, at least ``min_section`` apart, snapped to the
  strongest low-band onset within ``snap_window``.
* ``break``: the mirror image, momentum falling by ``break_threshold``.
* ``build``: for every drop, the last momentum valley in the preceding
  16 s, kept when the rise lasts at least ``min_build`` seconds.
* ``section``: peaks of a checkerboard novelty over a self-similarity
  matrix of MFCC + chroma, at least ``min_section`` apart.
* ``peak``: local momentum maxima with at least 0.15 prominence.

Every stream is published as ``<name>.<event>``; ``named[<name>]`` holds
the union. ``curve_out`` writes the sampled curve plus every event with
its strength, which is the input a timeline planner wants.

Backend: librosa + scipy. Missing librosa yields empty streams and a
single warning, mirroring the other audio extractors.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import MomentumMarkers
from eks_harness.video.plugins.base import MarkerExtractor

from ._cache import MarkerCache, compute_audio_digest
from .energy_markers import _resolve_audio_path

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["MOMENTUM_EVENTS", "MomentumMarkersExtractor", "analyze_momentum"]

_LOG = logging.getLogger(__name__)
_WARNED_NO_LIBROSA = False

MOMENTUM_EVENTS: tuple[str, ...] = ("drop", "build", "break", "section", "peak")

_SR = 22050
_HOP = 512
_CURVE_RATE = 20.0
_WEIGHTS = {"loudness": 0.4, "onset": 0.25, "low": 0.2, "bright": 0.15}


class MomentumMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``MomentumMarkers`` IR sources."""

    name: ClassVar[str] = "momentum_markers"
    handles: ClassVar[str] = "momentum_markers"

    def extract(self, source: MomentumMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        audio_path = _resolve_audio_path(source.source, ctx)
        if audio_path is None or not audio_path.exists():
            _LOG.warning(
                "momentum_markers: cannot resolve audio for source=%r; emitting empty streams",
                source.source,
            )
            return _to_marker_set(source, _empty_analysis())

        cache = _open_cache(ctx)
        cache_key = "momentum-" + compute_audio_digest(audio_path) + "-" + _param_digest(source)

        analysis: dict[str, Any] | None = None
        if cache is not None:
            cached = cache.lookup(cache_key)
            if isinstance(cached, dict) and "events" in cached:
                analysis = cached

        if analysis is None:
            try:
                analysis = analyze_momentum(audio_path, source)
            except _LibrosaUnavailable:
                _warn_missing_librosa()
                analysis = _empty_analysis()
            except Exception:
                _LOG.exception("momentum analysis failed for %s; emitting empty streams", audio_path)
                analysis = _empty_analysis()
            if cache is not None:
                cache.store(cache_key, analysis)

        if cache is not None:
            cache.close()

        if source.curve_out:
            _write_curve(source.curve_out, analysis, audio_path, ctx)

        return _to_marker_set(source, analysis)


class _LibrosaUnavailable(RuntimeError):
    pass


def analyze_momentum(audio_path: Path, source: MomentumMarkers) -> dict[str, Any]:
    """Run the full analysis and return ``{"curve": ..., "events": ..., "tempo": ...}``."""

    try:
        import librosa
        import numpy as np
        from scipy.ndimage import gaussian_filter1d
        from scipy.signal import find_peaks
    except ImportError as exc:
        raise _LibrosaUnavailable from exc

    y, sr = librosa.load(str(audio_path), sr=_SR, mono=True)
    if y.size < sr:
        return _empty_analysis()

    fps = sr / _HOP
    stft = np.abs(librosa.stft(y, n_fft=2048, hop_length=_HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

    rms = librosa.feature.rms(S=stft, frame_length=2048, hop_length=_HOP)[0]
    loud = librosa.amplitude_to_db(rms, ref=np.max)
    onset = librosa.onset.onset_strength(S=librosa.amplitude_to_db(stft, ref=np.max), sr=sr)
    low_mask = (freqs >= 20.0) & (freqs <= 150.0)
    low = np.log1p(stft[low_mask].sum(axis=0))
    bright = librosa.feature.spectral_centroid(S=stft, sr=sr)[0]

    n = min(len(loud), len(onset), len(low), len(bright))
    loud, onset, low, bright = loud[:n], onset[:n], low[:n], bright[:n]

    onset_density = gaussian_filter1d(onset.astype(float), sigma=0.5 * fps)
    raw = (
        _WEIGHTS["loudness"] * _robust01(loud)
        + _WEIGHTS["onset"] * _robust01(onset_density)
        + _WEIGHTS["low"] * _robust01(gaussian_filter1d(low, sigma=0.25 * fps))
        + _WEIGHTS["bright"] * _robust01(gaussian_filter1d(bright, sigma=0.25 * fps))
    )
    momentum = _robust01(gaussian_filter1d(raw, sigma=source.smoothing * fps))
    times = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=_HOP)

    delta = _window_delta(momentum, round(0.5 * fps), round(1.5 * fps))
    low_curve = _robust01(gaussian_filter1d(low, sigma=0.12 * fps))
    low_delta = _window_delta(low_curve, round(0.4 * fps), round(1.0 * fps))
    drop_score = 0.5 * delta + 0.5 * low_delta

    min_gap = max(1, round(source.min_section * fps))
    low_onset = librosa.onset.onset_strength(
        S=librosa.amplitude_to_db(stft[freqs <= 250.0], ref=np.max), sr=sr
    )[:n]

    drop_idx, drop_props = find_peaks(drop_score, height=source.drop_threshold, distance=min_gap)
    break_idx, break_props = find_peaks(-delta, height=source.break_threshold, distance=min_gap)
    drops = [
        _event(times, _snap(i, low_onset, source.snap_window, fps), float(h))
        for i, h in zip(drop_idx, drop_props["peak_heights"], strict=True)
    ]
    breaks = [
        _event(times, _snap(i, onset, source.snap_window, fps), float(h))
        for i, h in zip(break_idx, break_props["peak_heights"], strict=True)
    ]

    builds: list[dict[str, float]] = []
    window = round(16.0 * fps)
    for i in drop_idx:
        lo = max(0, i - window)
        if i - lo < 2:
            continue
        segment = momentum[lo:i]
        valleys, _ = find_peaks(-segment, prominence=0.03)
        start = lo + (int(valleys[-1]) if len(valleys) else int(np.argmin(segment)))
        if (i - start) / fps >= source.min_build:
            rise = float(momentum[i] - momentum[start])
            builds.append(_event(times, start, max(0.0, rise)))

    peak_idx, peak_props = find_peaks(
        momentum, prominence=0.15, distance=max(1, min_gap // 2)
    )
    peaks = [
        _event(times, int(i), float(p))
        for i, p in zip(peak_idx, peak_props["prominences"], strict=True)
    ]

    sections = _sections(y, sr, source.min_section)

    try:
        tempo_arr, _ = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=_HOP)
        tempo = float(np.atleast_1d(tempo_arr)[0])
    except Exception:
        tempo = 0.0

    step = max(1, round(fps / _CURVE_RATE))
    return {
        "duration": float(len(y) / sr),
        "tempo": round(tempo, 3),
        "curve": {
            "t": [round(float(t), 3) for t in times[::step]],
            "momentum": [round(float(v), 4) for v in momentum[::step]],
        },
        "events": {
            "drop": drops,
            "build": builds,
            "break": breaks,
            "section": sections,
            "peak": peaks,
        },
    }


def _sections(y: Any, sr: int, min_section: float) -> list[dict[str, float]]:
    import librosa
    import numpy as np
    from scipy.signal import find_peaks

    hop = 2048
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    n = min(mfcc.shape[1], chroma.shape[1])
    feats = np.vstack([mfcc[:, :n], chroma[:, :n]])
    feats = (feats - feats.mean(axis=1, keepdims=True)) / (feats.std(axis=1, keepdims=True) + 1e-9)
    norm = feats / (np.linalg.norm(feats, axis=0, keepdims=True) + 1e-9)
    ssm = norm.T @ norm

    fps = sr / hop
    half = max(2, round(0.5 * min_section * fps))
    sign = np.ones((2 * half, 2 * half))
    sign[:half, half:] = -1.0
    sign[half:, :half] = -1.0
    gauss = np.exp(-0.5 * (np.linspace(-2.0, 2.0, 2 * half) ** 2))
    kernel = sign * np.outer(gauss, gauss)

    padded = np.pad(ssm, half, mode="constant")
    novelty = np.array(
        [float(np.sum(padded[i : i + 2 * half, i : i + 2 * half] * kernel)) for i in range(n)]
    )
    novelty = np.clip(novelty, 0.0, None)
    if novelty.max() <= 0.0:
        return []
    novelty /= novelty.max()
    idx, props = find_peaks(novelty, height=0.15, distance=max(1, round(min_section * fps)))
    times = librosa.frames_to_time(idx, sr=sr, hop_length=hop)
    return [
        {"t": round(float(t), 3), "strength": round(float(h), 4)}
        for t, h in zip(times, props["peak_heights"], strict=True)
    ]


def _window_delta(curve: Any, after: int, before: int) -> Any:
    import numpy as np

    n = len(curve)
    after, before = max(1, after), max(1, before)
    csum = np.concatenate([[0.0], np.cumsum(curve)])
    idx = np.arange(n)
    a_hi = np.minimum(n, idx + after)
    b_lo = np.maximum(0, idx - before)
    mean_after = (csum[a_hi] - csum[idx]) / np.maximum(1, a_hi - idx)
    mean_before = (csum[idx] - csum[b_lo]) / np.maximum(1, idx - b_lo)
    return mean_after - mean_before


def _robust01(x: Any) -> Any:
    import numpy as np

    lo, hi = np.percentile(x, [5.0, 95.0])
    if hi - lo <= 1e-12:
        return np.zeros_like(x, dtype=float)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def _snap(i: int, envelope: Any, window_s: float, fps: float) -> int:
    import numpy as np

    w = round(window_s * fps)
    if w <= 0:
        return int(i)
    lo = max(0, i - w)
    hi = min(len(envelope), i + w + 1)
    return int(lo + int(np.argmax(envelope[lo:hi])))


def _event(times: Any, i: int, strength: float) -> dict[str, float]:
    return {"t": round(float(times[int(i)]), 3), "strength": round(strength, 4)}


def _empty_analysis() -> dict[str, Any]:
    return {
        "duration": 0.0,
        "tempo": 0.0,
        "curve": {"t": [], "momentum": []},
        "events": {event: [] for event in MOMENTUM_EVENTS},
    }


def _param_digest(source: MomentumMarkers) -> str:
    return "-".join(
        f"{v:.4f}"
        for v in (
            source.smoothing,
            source.drop_threshold,
            source.break_threshold,
            source.min_build,
            source.min_section,
            source.snap_window,
        )
    )


def _write_curve(
    spec: str, analysis: dict[str, Any], audio_path: Path, ctx: RenderContext | None
) -> None:
    out = Path(spec)
    if not out.is_absolute():
        base = getattr(ctx, "workspace", None) or Path.cwd()
        out = Path(base) / out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": str(audio_path), **analysis}
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _to_marker_set(source: MomentumMarkers, analysis: dict[str, Any]) -> MarkerSet:
    out = MarkerSet()
    union: list[float] = []
    for event in MOMENTUM_EVENTS:
        times = sorted(float(e["t"]) for e in analysis["events"].get(event, []))
        out.streams[f"{source.name}.{event}"] = times
        union.extend(times)
    out.named[source.name] = sorted(union)
    for event in MOMENTUM_EVENTS:
        out.named[f"{source.name}.{event}"] = list(out.streams[f"{source.name}.{event}"])
    return out


def _open_cache(ctx: RenderContext | None) -> MarkerCache | None:
    if ctx is None or getattr(ctx, "cache_dir", None) is None:
        return None
    return MarkerCache(ctx.cache_dir)


def _warn_missing_librosa() -> None:
    global _WARNED_NO_LIBROSA
    if _WARNED_NO_LIBROSA:
        return
    _LOG.warning(
        "librosa not installed; install via `pip install librosa` to enable momentum markers"
    )
    _WARNED_NO_LIBROSA = True
