"""Marker extraction.

Runs every registered :class:`~eks_harness.video.plugins.base.MarkerExtractor` against
the project's declared :class:`~eks_harness.video.ir.markers.MarkerSource` instances
and merges the results into a flat :class:`MarkerSet` keyed by stream name.

Phase 1 ships a single stub extractor (:class:`StubBeatExtractor`) that
emits an evenly-spaced grid driven by the source's ``bpm`` field, so tests
exercise the time-resolve and animated-resolve code paths without requiring
``madmom`` / ``faster-whisper``. Phase 2 replaces the stub with real
extractors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video.ir.markers import BeatTracker

if TYPE_CHECKING:
    from eks_harness.video.ir import Project

_LOG = logging.getLogger(__name__)


@dataclass
class WordHit:
    """One word emitted by an STT marker source."""

    text: str
    t_start: float
    t_end: float
    source: str


@dataclass
class MarkerSet:
    """Resolved marker times.

    ``streams`` maps stream name → ordered list of trigger seconds.
    ``words`` collects every STT hit across sources.
    ``named`` stores marker-source name → trigger seconds for ``MarkerRef``.
    """

    streams: dict[str, list[float]] = field(default_factory=dict)
    words: list[WordHit] = field(default_factory=list)
    named: dict[str, list[float]] = field(default_factory=dict)

    def merge(self, other: MarkerSet) -> None:
        for stream, times in other.streams.items():
            self.streams.setdefault(stream, []).extend(times)
            self.streams[stream].sort()
        self.words.extend(other.words)
        for name, times in other.named.items():
            self.named.setdefault(name, []).extend(times)
            self.named[name].sort()


class StubBeatExtractor:
    """Phase-1 stub.

    Emits an evenly spaced beat grid derived from ``BeatTracker.bpm`` (or 120
    BPM if unset) covering the whole project. Every declared stream gets the
    same grid; this is enough to exercise BeatPulse and BeatRef in tests.
    """

    name = "stub_beat"
    handles = "beat_tracker"

    def extract(self, source: BeatTracker, project_duration: float) -> MarkerSet:
        bpm = source.bpm if source.bpm is not None else 120.0
        period = 60.0 / bpm
        times = []
        t = 0.0
        while t <= project_duration + 1e-9:
            times.append(round(t, 9))
            t += period
        result = MarkerSet()
        for stream in source.streams:
            result.streams[stream] = list(times)
        result.named[source.name] = list(times)
        return result


def extract_markers(project: Project, ctx: object | None = None) -> MarkerSet:
    """Run every registered marker extractor against the project's marker sources.

    Falls back to :class:`StubBeatExtractor` for any ``BeatTracker`` source
    that no registered extractor can handle.

    ``ctx`` is the optional :class:`~eks_harness.video.render.context.RenderContext`.
    When called outside the renderer (tests, ad-hoc CLI), pass ``None`` -
    the function synthesises a minimal stand-in wrapping ``project`` so
    extractors that look up ``ctx.project`` / ``ctx.workspace`` (e.g. to
    resolve ``source="audio_tracks[0]"``) still work.
    """

    from eks_harness.video.plugins.registry import resolve_marker_extractor

    effective_ctx = ctx if ctx is not None else _MinimalMarkerCtx(project=project)

    out = MarkerSet()
    stub = StubBeatExtractor()
    for source in project.markers:
        extractor = None
        try:
            extractor = resolve_marker_extractor(source.kind)
        except LookupError:
            extractor = None

        if extractor is None and isinstance(source, BeatTracker):
            out.merge(stub.extract(source, project.duration))
            continue

        if extractor is None:
            continue

        result = extractor.extract(source, ctx=effective_ctx)  # type: ignore[arg-type]
        if isinstance(result, MarkerSet):
            out.merge(result)
    return out


@dataclass
class _MinimalMarkerCtx:
    """Stand-in for :class:`RenderContext` when markers are extracted outside the renderer."""

    project: "Project"
    workspace: Path = field(default_factory=Path.cwd)
    cache_dir: Path | None = None


@dataclass
class WordToken:
    """Lightweight word record returned by :func:`extract_speech_for_source`."""

    text: str
    start: float
    end: float
    confidence: float | None = None


@dataclass
class AudioMarkerReport:
    """Beat-tracker-style summary of a single audio source.

    Returned by :func:`extract_for_source`. Empty fields signal a missing
    backend (e.g. madmom not installed) or a non-audio source - callers must
    treat this as informational rather than fatal.
    """

    bpm: float | None = None
    beat_times: list[float] = field(default_factory=list)
    downbeat_times: list[float] = field(default_factory=list)
    kick_times: list[float] = field(default_factory=list)
    snare_times: list[float] = field(default_factory=list)


@dataclass
class SpeechReport:
    """Speech-to-text summary of a single audio source."""

    words: list[WordToken] = field(default_factory=list)
    language: str | None = None


def extract_for_source(audio_path: Path) -> AudioMarkerReport:
    """Run the registered beat tracker against ``audio_path``.

    Safe to call on any path -- missing files, video sources without audio,
    or hosts without madmom all produce an empty :class:`AudioMarkerReport`
    rather than raising, so callers (the MCP probe in particular) get a
    consistent shape.
    """

    path = Path(audio_path)
    if not path.exists():
        _LOG.debug("extract_for_source: %s does not exist; returning empty report", path)
        return AudioMarkerReport()

    try:
        from eks_harness.video.plugins.builtin.markers.beat_tracker import BeatTrackerExtractor

        extractor = BeatTrackerExtractor()
        source = BeatTracker(name="probe", source=str(path))
        result = extractor.extract(source, ctx=None)  # type: ignore[arg-type]
    except Exception:
        _LOG.exception("extract_for_source failed for %s", path)
        return AudioMarkerReport()

    beats = list(result.streams.get("beat", []))
    return AudioMarkerReport(
        bpm=_estimate_bpm(beats),
        beat_times=beats,
        downbeat_times=list(result.streams.get("downbeat", [])),
        kick_times=list(result.streams.get("kick", [])),
        snare_times=list(result.streams.get("snare", [])),
    )


def extract_speech_for_source(
    audio_path: Path, backend: str = "faster-whisper"
) -> SpeechReport:
    """Transcribe ``audio_path`` with the requested captioner backend.

    Mirrors the safety contract of :func:`extract_for_source`: missing files,
    unavailable backends, or transcription errors return an empty
    :class:`SpeechReport` rather than raising.
    """

    path = Path(audio_path)
    if not path.exists():
        _LOG.debug("extract_speech_for_source: %s does not exist; returning empty report", path)
        return SpeechReport()

    try:
        from eks_harness.video.plugins.builtin.captioners import select_captioner

        captioner = select_captioner(prefer=backend)
        raw_words = list(captioner.transcribe(path))
    except Exception:
        _LOG.exception("extract_speech_for_source failed for %s (backend=%s)", path, backend)
        return SpeechReport()

    words = [
        WordToken(
            text=str(token.text),
            start=float(token.start),
            end=float(token.end),
            confidence=None if token.confidence is None else float(token.confidence),
        )
        for token in raw_words
    ]
    return SpeechReport(words=words)


def _estimate_bpm(beat_times: list[float]) -> float | None:
    if len(beat_times) < 2:
        return None
    from itertools import pairwise

    intervals = [b - a for a, b in pairwise(beat_times) if b > a]
    if not intervals:
        return None
    avg = sum(intervals) / len(intervals)
    if avg <= 0.0:
        return None
    return round(60.0 / avg, 3)


__all__ = [
    "AudioMarkerReport",
    "MarkerSet",
    "SpeechReport",
    "StubBeatExtractor",
    "WordHit",
    "WordToken",
    "extract_for_source",
    "extract_markers",
    "extract_speech_for_source",
]
