from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializeAsAny, field_validator, model_validator

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.registry import Poly, catalog
from eks_harness.ir.time import TICKS_PER_MS, Time

BeatStream = Literal["kick", "snare", "beat", "downbeat"]
BeatBackend = Literal["auto", "beatnet", "madmom", "librosa"]
EnergyBand = Literal["low", "mid", "high", "full"]
MARKER_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
MOMENTUM_STREAMS = ("drop", "build", "break", "section", "peak")
CLOCK_STREAMS = frozenset({"beat", "downbeat", "bar", "kick", "snare"})
SttBackend = Literal["faster-whisper", "whisperx", "mlx-whisper"]
SttDevice = Literal["cpu", "cuda", "mps"]
SttComputeType = Literal[
    "float32", "float16", "bfloat16", "int8", "int8_float32", "int8_float16", "int8_bfloat16", "int16"]
STT_DEVICES: dict[str, frozenset[str]] = {
    "faster-whisper": frozenset({"cpu", "cuda"}),
    "whisperx": frozenset({"cpu", "cuda"}),
    "mlx-whisper": frozenset({"cpu", "mps"}),
}


class MarkerBase(BaseModel, frozen=True, extra="forbid"):
    kind: str = Field(description="Marker analysis kind")
    name: str = Field(
        pattern=MARKER_NAME,
        description="Name of the analysis, unique in the plan; its streams are named name or name.stream. beat, "
                    "downbeat, bar, kick and snare are reserved for the clock tempo streams")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if value in CLOCK_STREAMS:
            raise ValueError(
                f"marker source name {value!r} is reserved: {', '.join(sorted(CLOCK_STREAMS))} name the clock tempo "
                "streams; name the analysis after its media, such as 'song' or 'drums'")
        return value

    def stream_names(self) -> tuple[str, ...]:
        return (self.name,)

    def pinned(self) -> bool:
        return True


@catalog("marker", description="Beat, downbeat, kick and snare streams of a song")
class BeatTracker(MarkerBase, frozen=True):
    kind: Literal["beat_tracker"] = Field(default="beat_tracker", description="Beat, downbeat, kick and snare streams of a song")
    streams: tuple[BeatStream, ...] = Field(default=("beat", "downbeat"), description="Beat streams to produce")
    bpm: float | None = Field(default=None, gt=0, description="Known tempo to lock the tracker to; detected when unset")
    backend: BeatBackend = Field(default="auto", description="Beat tracking backend")

    def stream_names(self) -> tuple[str, ...]:
        return tuple(f"{self.name}.{stream}" for stream in self.streams)

    def pinned(self) -> bool:
        return self.backend != "auto"


@catalog("marker", description="Word timings from speech to text")
class STTMarkers(MarkerBase, frozen=True):
    kind: Literal["stt_markers"] = Field(default="stt_markers", description="Word timings from speech to text")
    backend: SttBackend = Field(default="faster-whisper", description="Speech-to-text backend")
    language: str | None = Field(default=None, description="Spoken language code such as tr; detected when unset")
    model: str | None = Field(
        default=None, min_length=1, pattern=r"^[A-Za-z0-9_.:/-]+$",
        description="Speech-to-text model, such as large-v3 or a model repository id; the backend's default when unset")
    device: SttDevice | None = Field(
        default=None, description="Inference device: cpu, cuda (faster-whisper, whisperx) or mps (mlx-whisper); chosen "
                                  "per machine when unset")
    compute_type: SttComputeType | None = Field(
        default=None, description="Inference precision such as float32 or int8; chosen per machine and device when unset")

    @model_validator(mode="after")
    def _check_device(self) -> STTMarkers:
        if self.device is not None and self.device not in STT_DEVICES[self.backend]:
            raise ValueError(
                f"speech-to-text backend {self.backend} runs on {' or '.join(sorted(STT_DEVICES[self.backend]))}, "
                f"not on device {self.device!r}")
        return self

    def pinned(self) -> bool:
        return self.model is not None and self.device is not None and self.compute_type is not None


@catalog("marker", description="Scene cuts in a video")
class SceneMarkers(MarkerBase, frozen=True):
    kind: Literal["scene_markers"] = Field(default="scene_markers", description="Scene cuts in a video")
    threshold: float = Field(default=27.0, description="Content change threshold for a scene cut")


@catalog("marker", description="Starts of silent regions")
class SilenceMarkers(MarkerBase, frozen=True):
    kind: Literal["silence_markers"] = Field(default="silence_markers", description="Starts of silent regions")
    threshold_db: float = Field(default=-40.0, description="Level in dB below which audio counts as silence")
    min_duration: Duration = Field(default=Time(ticks=300 * TICKS_PER_MS), description="Shortest silence that counts")


@catalog("marker", description="Audio onsets")
class OnsetMarkers(MarkerBase, frozen=True):
    kind: Literal["onset_markers"] = Field(default="onset_markers", description="Audio onsets")
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0, description="Onset detector sensitivity from 0 to 1")


@catalog("marker", description="Moments where band energy exceeds a percentile")
class EnergyMarkers(MarkerBase, frozen=True):
    kind: Literal["energy_markers"] = Field(default="energy_markers", description="Moments where band energy exceeds a percentile")
    band: EnergyBand = Field(default="full", description="Frequency band whose energy is measured")
    percentile: float = Field(default=0.85, gt=0.0, lt=1.0, description="Energy percentile above which a frame is marked")


@catalog("marker", description="Drops, builds, breaks, sections and peaks of a song")
class MomentumMarkers(MarkerBase, frozen=True):
    kind: Literal["momentum_markers"] = Field(default="momentum_markers", description="Drops, builds, breaks, sections and peaks of a song")
    smoothing: Duration = Field(default=Time(ticks=750 * TICKS_PER_MS), description="Smoothing window of the energy curve")
    drop_threshold: float = Field(default=0.22, gt=0.0, le=1.0, description="Energy rise that counts as a drop")
    break_threshold: float = Field(default=0.2, gt=0.0, le=1.0, description="Energy fall that counts as a break")
    min_build: Duration = Field(default=Time(ticks=1500 * TICKS_PER_MS), description="Shortest build before a drop")
    min_section: Duration = Field(default=Time(ticks=4000 * TICKS_PER_MS), description="Shortest section between markers")
    snap_window: Duration | None = Field(default=Time(ticks=120 * TICKS_PER_MS), description="Snap markers to beats within this distance; no snapping when unset")

    def stream_names(self) -> tuple[str, ...]:
        return tuple(f"{self.name}.{part}" for part in MOMENTUM_STREAMS)


@catalog("marker", description="Starts of runs of frames with a face")
class FaceMarkers(MarkerBase, frozen=True):
    kind: Literal["face_markers"] = Field(default="face_markers", description="Starts of runs of frames with a face")
    min_size: int = Field(default=80, gt=0, description="Smallest face size in pixels")


@catalog("marker", description="Frames with high motion")
class MotionMarkers(MarkerBase, frozen=True):
    kind: Literal["motion_markers"] = Field(default="motion_markers", description="Frames with high motion")
    threshold: float = Field(default=30.0, gt=0.0, description="Motion level that counts as high motion")


MarkerSource = Annotated[SerializeAsAny[MarkerBase], Poly("marker")]

__all__ = [
    "CLOCK_STREAMS",
    "MARKER_NAME",
    "MOMENTUM_STREAMS",
    "STT_DEVICES",
    "BeatBackend",
    "BeatStream",
    "BeatTracker",
    "EnergyBand",
    "EnergyMarkers",
    "FaceMarkers",
    "MarkerBase",
    "MarkerSource",
    "MomentumMarkers",
    "MotionMarkers",
    "OnsetMarkers",
    "STTMarkers",
    "SceneMarkers",
    "SilenceMarkers",
    "SttBackend",
    "SttComputeType",
    "SttDevice",
]
