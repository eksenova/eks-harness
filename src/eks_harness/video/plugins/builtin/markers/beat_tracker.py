"""Beat / kick / snare / downbeat extractor.

Backend chain (under ``BeatTracker.backend="auto"``):

1. **BeatNet** (``pip install BeatNet``) - CRNN + DBN, current
   state-of-the-art open-source beat/downbeat tracker. Produces only
   beats and downbeats; kick/snare are derived from a shared onset
   detector + spectral-centroid split.
2. **madmom** (``eks-harness[ml]``) - RNN beats + DBN downbeats, plus the
   CNN onset detector that feeds kick/snare splitting.
3. **librosa** - ``librosa.beat.beat_track`` + ``librosa.onset.onset_detect``,
   always-available fallback.
4. **Stub** - evenly-spaced beats from ``BeatTracker.bpm``; only when
   neither real backend is available. Emits a single warning per run.

Explicit ``backend`` values pin a backend; if the pinned backend is
missing the chain falls through to the next available one in the auto
order and logs a warning (so a misconfigured pin never silently
degrades to stub).

Cache layout: ``cache/markers/beat-<source-sha>-<bpm-hint>-<resolved-backend>.json``.
The resolved backend is encoded in the key so switching backends does
not poison cache entries.

All heavy imports are lazy so the import-time cost on a stock install
is zero.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.compile.markers import MarkerSet, StubBeatExtractor
from eks_harness.video.ir.markers import BeatBackend, BeatTracker
from eks_harness.video.plugins.base import MarkerExtractor
from eks_harness.video.render.progress import NoopProgressReporter, ProgressReporter, emit_log

from ._cache import MarkerCache, compute_audio_digest

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["BeatTrackerExtractor"]

_LOG = logging.getLogger(__name__)
_WARNED_NO_BACKEND = False

_AUTO_ORDER: tuple[BeatBackend, ...] = ("beatnet", "madmom", "librosa")


class BeatTrackerExtractor(MarkerExtractor):
    """Marker extractor for ``BeatTracker`` IR sources."""

    name: ClassVar[str] = "beat_tracker"
    handles: ClassVar[str] = "beat_tracker"

    def extract(self, source: BeatTracker, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        reporter = _resolve_reporter(ctx)
        audio_path = self._resolve_audio_path(source, ctx)
        if audio_path is None or not audio_path.exists():
            return self._stub(source, ctx)

        order = self._chain_for(source.backend)

        cache_root = self._cache_root(ctx)
        cache = MarkerCache(cache_root) if cache_root is not None else None
        digest = compute_audio_digest(audio_path)
        bpm_hint = "auto" if source.bpm is None else f"{source.bpm:.3f}"

        payload: dict[str, list[float]] | None = None
        resolved: str = "stub"
        for backend in order:
            cache_key = f"beat-{digest}-{bpm_hint}-{backend}"
            if cache is not None:
                cached = cache.lookup(cache_key)
                if cached is not None:
                    cache.close()
                    emit_log(
                        reporter,
                        level="info",
                        message=f"beat tracker cache hit (backend={backend})",
                        source=source.name,
                        backend=backend,
                    )
                    return _payload_to_marker_set(source, cached)

            try:
                payload = self._run_backend(backend, audio_path, source)
            except _BackendUnavailable as exc:
                _LOG.debug("backend %s unavailable (%s); trying next", backend, exc)
                emit_log(
                    reporter,
                    level="warning",
                    message=f"beat tracker backend={backend} unavailable; trying next",
                    source=source.name,
                    backend=backend,
                )
                payload = None
            except Exception:
                _LOG.exception(
                    "backend %s failed for %s; trying next", backend, audio_path
                )
                emit_log(
                    reporter,
                    level="warning",
                    message=f"beat tracker backend={backend} raised; trying next",
                    source=source.name,
                    backend=backend,
                )
                payload = None

            if payload is not None:
                resolved = backend
                if cache is not None:
                    cache.store(cache_key, payload)
                break

        if cache is not None:
            cache.close()

        if payload is None:
            self._warn_no_backend(source.backend)
            emit_log(
                reporter,
                level="warning",
                message="no beat backend available; using stub",
                source=source.name,
            )
            return self._stub(source, ctx)

        if source.backend != "auto" and resolved != source.backend:
            _LOG.warning(
                "BeatTracker backend=%r unavailable; using %r instead",
                source.backend,
                resolved,
            )
            emit_log(
                reporter,
                level="warning",
                message=(
                    f"beat tracker backend={source.backend!r} unavailable; using {resolved!r}"
                ),
                source=source.name,
                requested=source.backend,
                resolved=resolved,
            )
        counts = {stream: len(payload.get(stream, [])) for stream in source.streams}
        emit_log(
            reporter,
            level="info",
            message=(
                f"beat tracker resolved backend={resolved} "
                + ", ".join(f"{k}={v}" for k, v in counts.items())
            ),
            source=source.name,
            backend=resolved,
            counts=counts,
        )
        return _payload_to_marker_set(source, payload)

    def _chain_for(self, backend: BeatBackend) -> tuple[BeatBackend, ...]:
        if backend == "auto":
            return _AUTO_ORDER
        rest = tuple(b for b in _AUTO_ORDER if b != backend)
        return (backend, *rest)

    def _run_backend(
        self, backend: BeatBackend, audio_path: Path, source: BeatTracker
    ) -> dict[str, list[float]]:
        if backend == "beatnet":
            return self._run_beatnet(audio_path, source)
        if backend == "madmom":
            return self._run_madmom(audio_path, source)
        if backend == "librosa":
            return self._run_librosa(audio_path, source)
        raise _BackendUnavailable(f"unknown backend {backend!r}")

    def _resolve_audio_path(self, source: BeatTracker, ctx: RenderContext | None) -> Path | None:
        """Resolve ``source.source`` to a concrete audio file path.

        Supports three forms:

        * Direct path (absolute or workspace-relative).
        * ``audio_tracks[N]`` - first segment media of the Nth audio track.
        * ``audio_tracks[name]`` - first segment media of the named audio
          track. Names containing only digits are interpreted as indices,
          so prefer truly distinctive names if both work.
        """

        spec = source.source
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

    def _cache_root(self, ctx: RenderContext | None) -> Path | None:
        if ctx is None:
            return None
        return ctx.cache_dir

    def _stub(self, source: BeatTracker, ctx: RenderContext | None) -> MarkerSet:
        duration = float(ctx.project.duration) if ctx is not None else 60.0
        return StubBeatExtractor().extract(source, duration)

    def _run_beatnet(
        self, audio_path: Path, source: BeatTracker
    ) -> dict[str, list[float]]:
        try:
            from BeatNet.BeatNet import BeatNet  # type: ignore[import-not-found]
        except ImportError as exc:
            raise _BackendUnavailable("BeatNet not installed") from exc

        estimator = BeatNet(
            1,
            mode="offline",
            inference_model="DBN",
            plot=[],
            thread=False,
        )
        try:
            raw = estimator.process(str(audio_path))
        except Exception as exc:
            raise _BackendUnavailable(f"BeatNet.process failed: {exc}") from exc

        beats, downbeats = _parse_beatnet_output(raw)

        onsets = _detect_onsets(audio_path)
        kicks, snares = _split_kicks_and_snares(audio_path, onsets)

        payload: dict[str, list[float]] = {
            "beat": beats,
            "downbeat": downbeats,
            "kick": kicks,
            "snare": snares,
        }
        return {stream: payload.get(stream, []) for stream in source.streams}

    def _run_madmom(self, audio_path: Path, source: BeatTracker) -> dict[str, list[float]]:
        try:
            from madmom.features.beats import (  # type: ignore[import-not-found]
                DBNBeatTrackingProcessor,
                RNNBeatProcessor,
            )
            from madmom.features.downbeats import (  # type: ignore[import-not-found]
                DBNDownBeatTrackingProcessor,
                RNNDownBeatProcessor,
            )
            from madmom.features.onsets import (  # type: ignore[import-not-found]
                CNNOnsetProcessor,
            )
        except ImportError as exc:
            raise _BackendUnavailable("madmom not installed") from exc

        beats = list(_call_madmom(RNNBeatProcessor(), DBNBeatTrackingProcessor(fps=100), audio_path))
        downbeats: list[float] = []
        try:
            db_act = RNNDownBeatProcessor()(str(audio_path))
            db_proc = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)
            db_result = db_proc(db_act)
            downbeats = [float(row[0]) for row in db_result if round(row[1]) == 1]
        except Exception:
            _LOG.debug("downbeat tracking failed; leaving downbeats empty", exc_info=True)

        onsets: list[float] = []
        try:
            onset_act = CNNOnsetProcessor()(str(audio_path))
            onsets = _peak_pick(onset_act, fps=100, threshold=0.5)
        except Exception:
            _LOG.debug("onset detection failed; leaving onsets empty", exc_info=True)

        kicks, snares = _split_kicks_and_snares(audio_path, onsets)

        payload: dict[str, list[float]] = {
            "beat": beats,
            "downbeat": downbeats,
            "kick": kicks,
            "snare": snares,
        }
        return {stream: payload.get(stream, []) for stream in source.streams}

    def _run_librosa(
        self, audio_path: Path, source: BeatTracker
    ) -> dict[str, list[float]]:
        try:
            import librosa
        except ImportError as exc:
            raise _BackendUnavailable("librosa not installed") from exc

        y, sr = librosa.load(str(audio_path), sr=None, mono=True)

        bpm_hint = source.bpm if source.bpm is not None else None
        _tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, start_bpm=bpm_hint or 120.0)
        beat_times = [float(t) for t in librosa.frames_to_time(beat_frames, sr=sr).tolist()]

        onset_frames = librosa.onset.onset_detect(y=y, sr=sr, backtrack=True)
        onset_times = [float(t) for t in librosa.frames_to_time(onset_frames, sr=sr).tolist()]

        # `beat_track` decides where the pulse *should* be but can put each
        # beat up to ~150ms away from the actual audio attack - visually
        # that reads as "not synced". Snap every beat to the nearest real
        # onset within a tight window (150ms) so the kick stream lands on
        # a true audio event without ever pulling from an adjacent beat's
        # onset. Remaining onsets become snares.
        kicks, used_onset_idxs = _snap_beats_to_onsets(
            beat_times, onset_times, window_s=0.15
        )
        snares = [t for i, t in enumerate(onset_times) if i not in used_onset_idxs]

        # Every 4th snapped kick as a coarse downbeat proxy. Real downbeat
        # tracking lives in madmom / BeatNet; this is "good enough for now".
        downbeats = kicks[::4]

        payload: dict[str, list[float]] = {
            "beat": beat_times,
            "downbeat": downbeats,
            "kick": kicks,
            "snare": snares,
        }
        return {stream: payload.get(stream, []) for stream in source.streams}

    def _warn_no_backend(self, requested: BeatBackend) -> None:
        global _WARNED_NO_BACKEND
        if _WARNED_NO_BACKEND:
            return
        logging.warning(
            "no beat backend available (requested=%r) - falling back to stub. "
            "Install `eks-harness[ml]` for BeatNet/madmom, or `pip install librosa`.",
            requested,
        )
        _WARNED_NO_BACKEND = True


class _BackendUnavailable(RuntimeError):
    pass


def _resolve_reporter(ctx: Any) -> ProgressReporter:
    """Pull the active :class:`ProgressReporter` off ``ctx`` if present.

    Marker extractors run before the full :class:`RenderContext` exists in
    some flows (probe, tests), so this also handles the minimal
    extractor-time ctx that exposes only ``extra``.
    """

    if ctx is None:
        return NoopProgressReporter()
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        reporter = extra.get("progress_reporter")
        if reporter is not None and hasattr(reporter, "emit"):
            return reporter  # type: ignore[return-value]
    return NoopProgressReporter()


def _parse_beatnet_output(raw: Any) -> tuple[list[float], list[float]]:
    """Convert BeatNet's ``(N, 2)`` ndarray into (beats, downbeats).

    Column 0 is the beat time in seconds, column 1 is the beat position
    within the bar (1.0 means downbeat). BeatNet occasionally emits a 1-D
    array (single beat); handle that defensively.
    """

    import numpy as np

    arr = np.asarray(raw, dtype=float)
    if arr.size == 0:
        return [], []
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.shape[1] < 2:
        # No position column - treat every row as a plain beat with no downbeats.
        beats = [float(row[0]) for row in arr]
        return beats, []

    beats = [float(row[0]) for row in arr]
    downbeats = [float(row[0]) for row in arr if round(float(row[1])) == 1]
    return beats, downbeats


def _detect_onsets(audio_path: Path) -> list[float]:
    """Detect onsets, preferring madmom's CNN detector and falling back to librosa."""

    try:
        from madmom.features.onsets import CNNOnsetProcessor
    except ImportError:
        return _detect_onsets_librosa(audio_path)

    try:
        activations = CNNOnsetProcessor()(str(audio_path))
        return _peak_pick(activations, fps=100, threshold=0.5)
    except Exception:
        _LOG.debug("madmom onset detection failed; trying librosa", exc_info=True)
        return _detect_onsets_librosa(audio_path)


def _detect_onsets_librosa(audio_path: Path) -> list[float]:
    try:
        import librosa
    except ImportError:
        return []
    try:
        y, sr = librosa.load(str(audio_path), sr=None, mono=True)
        frames = librosa.onset.onset_detect(y=y, sr=sr, backtrack=True)
        return [float(t) for t in librosa.frames_to_time(frames, sr=sr).tolist()]
    except Exception:
        _LOG.debug("librosa onset detection failed", exc_info=True)
        return []


def _call_madmom(activator: Any, tracker: Any, audio_path: Path) -> list[float]:
    activations = activator(str(audio_path))
    result = tracker(activations)
    return [float(t) for t in result]


def _peak_pick(activations: Any, fps: int, threshold: float) -> list[float]:
    import numpy as np

    arr = np.asarray(activations, dtype=float).ravel()
    peaks: list[float] = []
    for i in range(1, len(arr) - 1):
        if arr[i] >= threshold and arr[i] > arr[i - 1] and arr[i] >= arr[i + 1]:
            peaks.append(i / fps)
    return peaks


def _resolve_audio_track_ref(key: str, ctx: RenderContext) -> Path | None:
    """Resolve ``key`` (index or name) to the first AudioSegment media path."""

    from eks_harness.video.ir.media import AudioFile

    tracks = list(getattr(ctx.project, "audio_tracks", []) or [])
    if not tracks:
        return None

    track = None
    try:
        idx = int(key)
        if 0 <= idx < len(tracks):
            track = tracks[idx]
    except ValueError:
        for candidate in tracks:
            if candidate.name == key:
                track = candidate
                break

    if track is None or not track.segments:
        return None

    media = track.segments[0].media
    if not isinstance(media, AudioFile):
        return None
    path = Path(media.path)
    if path.is_absolute():
        return path
    ws_candidate = ctx.workspace / path
    return ws_candidate if ws_candidate.exists() else path


def _snap_beats_to_onsets(
    beats: list[float], onsets: list[float], window_s: float
) -> tuple[list[float], set[int]]:
    """Snap each beat to the nearest onset within ``window_s``.

    Returns ``(snapped_beat_times, indices_of_onsets_used)`` so callers can
    tell which onsets were "claimed" by a beat (useful for partitioning
    onsets into kick vs snare streams without double-counting).

    Beats with no onset in range are kept at their original time so the
    pulse stays continuous through quiet passages.
    """

    if not beats:
        return [], set()
    if not onsets:
        return list(beats), set()

    onsets_sorted = sorted((t, i) for i, t in enumerate(onsets))
    onset_times_only = [t for t, _ in onsets_sorted]

    snapped: list[float] = []
    used: set[int] = set()
    last_used_onset_idx_into_sorted = -1

    for beat in beats:
        lo, hi = last_used_onset_idx_into_sorted + 1, len(onset_times_only)
        best_pos: int | None = None
        best_dist = window_s
        for pos in range(lo, hi):
            d = abs(onset_times_only[pos] - beat)
            if d <= best_dist:
                best_dist = d
                best_pos = pos
            elif onset_times_only[pos] - beat > window_s:
                break
        if best_pos is None:
            snapped.append(float(beat))
        else:
            snapped.append(float(onset_times_only[best_pos]))
            used.add(onsets_sorted[best_pos][1])
            last_used_onset_idx_into_sorted = best_pos

    return snapped, used


def _split_kicks_and_snares_from_audio(
    y: Any, sr: int, onsets: list[float]
) -> tuple[list[float], list[float]]:
    """Same spectral-centroid split, but reusing an already-loaded waveform."""

    if not onsets:
        return [], []
    try:
        import librosa
        import numpy as np
    except ImportError:
        return [], []

    kicks: list[float] = []
    snares: list[float] = []
    window = int(0.04 * sr)
    for t in onsets:
        center = int(t * sr)
        start = max(0, center - window // 2)
        end = min(len(y), center + window // 2)
        if end - start < 32:
            continue
        chunk = y[start:end]
        centroid_arr = librosa.feature.spectral_centroid(y=chunk, sr=sr)
        centroid = float(np.mean(centroid_arr))
        if centroid < 1500.0:
            kicks.append(float(t))
        else:
            snares.append(float(t))
    return kicks, snares


def _split_kicks_and_snares(audio_path: Path, onsets: list[float]) -> tuple[list[float], list[float]]:
    """Pragmatic kick/snare split via spectral centroid at each onset."""

    if not onsets:
        return [], []
    try:
        import librosa
    except ImportError:
        return [], []

    try:
        y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    except Exception:
        return [], []

    return _split_kicks_and_snares_from_audio(y, int(sr), onsets)


def _payload_to_marker_set(source: BeatTracker, payload: dict[str, Any]) -> MarkerSet:
    out = MarkerSet()
    union: list[float] = []
    for stream in source.streams:
        times = [float(t) for t in payload.get(stream, [])]
        out.streams[stream] = sorted(times)
        union.extend(times)
    out.named[source.name] = sorted(set(round(t, 9) for t in union))
    return out
