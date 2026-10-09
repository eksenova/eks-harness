"""Declarative ``MarkerSource`` IR.

A ``MarkerSource`` only declares *what* should be extracted; the actual
extraction runs through ``MarkerExtractor`` plugins during the compile step.
``BeatRef``, ``WordRef`` and ``MarkerRef`` time references resolve against
the named streams these sources produce.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BeatTracker",
    "EnergyMarkers",
    "FaceMarkers",
    "MarkerSource",
    "MomentumMarkers",
    "MotionMarkers",
    "OnsetMarkers",
    "STTMarkers",
    "SceneMarkers",
    "SilenceMarkers",
]

BeatStream = Literal["kick", "snare", "beat", "downbeat"]
BeatBackend = Literal["auto", "beatnet", "madmom", "librosa"]
EnergyBand = Literal["low", "mid", "high", "full"]


def _default_beat_streams() -> list[BeatStream]:
    return ["kick", "snare", "beat", "downbeat"]


class _MarkerBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BeatTracker(_MarkerBase):
    """Beat / kick / snare / downbeat extraction from an audio source.

    ``source`` references either an audio track name or a path-like asset id;
    the marker extractor plugin resolves it.

    ``backend`` selects the extraction backend. ``"auto"`` (the default)
    tries BeatNet, then madmom, then librosa, then falls back to an
    evenly-spaced stub. Explicit values pin a backend for reproducibility;
    if the pinned backend is unavailable, the extractor logs a warning and
    falls back through the remaining backends in auto order.
    """

    kind: Literal["beat_tracker"] = "beat_tracker"
    name: str
    source: str
    streams: list[BeatStream] = Field(default_factory=_default_beat_streams)
    bpm: float | None = None
    backend: BeatBackend = "auto"


class STTMarkers(_MarkerBase):
    kind: Literal["stt_markers"] = "stt_markers"
    name: str
    source: str
    backend: Literal["faster-whisper", "whisperx", "mlx-whisper"] = "faster-whisper"


class SceneMarkers(_MarkerBase):
    kind: Literal["scene_markers"] = "scene_markers"
    name: str
    source: str
    threshold: float = 27.0


class SilenceMarkers(_MarkerBase):
    """Silent-region markers from an audio source.

    Emits one marker per detected silent region whose RMS stays below
    ``threshold_db`` for at least ``min_duration`` seconds. The marker
    sits at the *start* of each silent region, mirroring how other
    marker sources publish event times rather than ranges.
    """

    kind: Literal["silence_markers"] = "silence_markers"
    name: str
    source: str
    threshold_db: float = -40.0
    min_duration: float = 0.3


class OnsetMarkers(_MarkerBase):
    """Generic audio onset markers.

    Broader than :class:`BeatTracker`: any perceptible onset, not just
    musical pulses. Useful for sound-design cues, foley hits, or
    speech-onset alignment.
    """

    kind: Literal["onset_markers"] = "onset_markers"
    name: str
    source: str
    sensitivity: float = 0.5


class EnergyMarkers(_MarkerBase):
    """Band-filtered RMS energy markers.

    Emits markers at moments where the band-filtered RMS exceeds the
    ``percentile`` percentile measured over the whole source. Useful for
    "hype" or peak-energy detection.
    """

    kind: Literal["energy_markers"] = "energy_markers"
    name: str
    source: str
    band: EnergyBand = "full"
    percentile: float = 0.85


class MomentumMarkers(_MarkerBase):
    """Musical momentum analysis: drops, builds, breaks, sections and peaks.

    Fuses loudness, onset density, low-end weight and spectral brightness
    into one smoothed ``momentum`` curve in ``[0, 1]`` and publishes the
    structural moments of the song as separate streams named
    ``<name>.drop``, ``<name>.build``, ``<name>.break``, ``<name>.section``
    and ``<name>.peak``. ``MarkerRef(name="<name>.drop", index=0)`` lands
    on the first drop. Drops and breaks snap to the strongest onset within
    ``snap_window`` seconds so cuts hit the transient, not the envelope.

    ``curve_out`` (workspace-relative) also writes the sampled curve and
    every event with its strength as JSON, for planning a timeline.
    """

    kind: Literal["momentum_markers"] = "momentum_markers"
    name: str
    source: str
    smoothing: float = Field(default=0.75, gt=0.0)
    drop_threshold: float = Field(default=0.22, gt=0.0, le=1.0)
    break_threshold: float = Field(default=0.2, gt=0.0, le=1.0)
    min_build: float = Field(default=1.5, gt=0.0)
    min_section: float = Field(default=4.0, gt=0.0)
    snap_window: float = Field(default=0.12, ge=0.0)
    curve_out: str | None = None


class FaceMarkers(_MarkerBase):
    """Face-presence markers from a video source.

    Emits the start time of every contiguous run of frames containing at
    least one face larger than ``min_size`` pixels. Backed by OpenCV's
    Haar cascade (no extra weights required).
    """

    kind: Literal["face_markers"] = "face_markers"
    name: str
    source: str
    min_size: int = 80


class MotionMarkers(_MarkerBase):
    """High-motion markers from a video source.

    Emits a marker for every frame whose mean absolute difference against
    the previous frame exceeds ``threshold``. Backed by OpenCV.
    """

    kind: Literal["motion_markers"] = "motion_markers"
    name: str
    source: str
    threshold: float = 30.0


MarkerSource = Annotated[
    BeatTracker
    | STTMarkers
    | SceneMarkers
    | SilenceMarkers
    | OnsetMarkers
    | EnergyMarkers
    | MomentumMarkers
    | FaceMarkers
    | MotionMarkers,
    Field(discriminator="kind"),
]
