"""Track and segment models."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .animated import Animated
from .audio_effects import (
    AudioFade,
    DeepFilterDenoise,
    Ducking,
    EQBand,
    EQEffect,
    LoudnessNormalize,
    Reverb,
)
from .effects import Effect
from .media import MediaSource
from .time import Seconds, TimeRef
from .transitions import Cut, Transition

__all__ = [
    "EQ",
    "AudioFade",
    "AudioSegment",
    "AudioTrack",
    "DeepFilterDenoise",
    "Ducking",
    "EQBand",
    "EQEffect",
    "LoudnessNormalize",
    "Reverb",
    "Segment",
    "SidechainConfig",
    "Track",
]


def _zero_seconds() -> Seconds:
    return Seconds(t=0.0)


def _const_animated_float(value: float) -> Animated[float]:
    return Animated[float](root=value)


class Segment(BaseModel):
    """A clip placed on a video ``Track``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    start: TimeRef
    media: MediaSource
    in_: TimeRef = Field(
        default_factory=_zero_seconds,
        validation_alias=AliasChoices("in", "in_"),
        serialization_alias="in",
    )
    out: TimeRef
    speed: Animated[float] = Field(default_factory=lambda: _const_animated_float(1.0))
    effects: list[Effect] = Field(default_factory=list)
    transition_in: Transition = Field(default_factory=Cut)
    transition_out: Transition = Field(default_factory=Cut)


class AudioSegment(BaseModel):
    """A clip placed on an ``AudioTrack``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    start: TimeRef
    media: MediaSource
    in_: TimeRef = Field(
        default_factory=_zero_seconds,
        validation_alias=AliasChoices("in", "in_"),
        serialization_alias="in",
    )
    out: TimeRef
    gain_db: Animated[float] = Field(default_factory=lambda: _const_animated_float(0.0))
    ducking: Ducking | None = None
    fade: AudioFade | None = None
    reverb: Reverb | None = None
    denoise: DeepFilterDenoise | None = None


class EQ(BaseModel):
    """Three-band EQ used by ``AudioTrack.eq``."""

    model_config = ConfigDict(extra="forbid")

    low_db: float = 0.0
    mid_db: float = 0.0
    high_db: float = 0.0


class SidechainConfig(BaseModel):
    """Auto-ducking config attaching this track to a source track."""

    model_config = ConfigDict(extra="forbid")

    source: str
    threshold_db: float = -20.0
    ratio: float = 4.0
    attack_ms: float = 10.0
    release_ms: float = 200.0


class Track(BaseModel):
    """Video track. ``z`` is the compositing depth (higher = on top)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    z: int = 0
    blend: Literal["normal", "add", "multiply", "screen", "overlay"] = "normal"
    opacity: Animated[float] = Field(default_factory=lambda: _const_animated_float(1.0))
    segments: list[Segment] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)


class AudioTrack(BaseModel):
    """Audio track with full ``Animated`` parity for gain / pan / EQ."""

    model_config = ConfigDict(extra="forbid")

    name: str
    gain_db: Animated[float] = Field(default_factory=lambda: _const_animated_float(0.0))
    pan: Animated[float] = Field(default_factory=lambda: _const_animated_float(0.0))
    eq: Animated[EQ] | None = None
    sidechain: SidechainConfig | None = None
    loudness: LoudnessNormalize | None = None
    parametric_eq: EQEffect | None = None
    segments: list[AudioSegment] = Field(default_factory=list)
