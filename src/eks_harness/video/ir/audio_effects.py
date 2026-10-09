"""Audio effect IR models.

Audio effects live as typed optional fields on :class:`AudioSegment` or
:class:`AudioTrack` (matching the existing pattern set by ``eq`` and
``sidechain``). They are NOT part of a discriminated union - each has its
own attribute name on the carrier model, so a ``kind`` discriminator is
unnecessary.

Scope:

- Segment-scoped: :class:`Ducking`, :class:`AudioFade`, :class:`Reverb`.
  Applied while building the per-segment chain in
  :mod:`eks_harness.video.render.audio` before the per-track ``amix``.
- Track-scoped: :class:`LoudnessNormalize`, :class:`EQEffect`.
  Applied to the post-mix track chain so they operate on the summed bus.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .animated import Animated

__all__ = [
    "AudioFade",
    "DeepFilterDenoise",
    "Ducking",
    "EQBand",
    "EQEffect",
    "LoudnessNormalize",
    "Reverb",
]


def _const_animated_float(value: float) -> Animated[float]:
    return Animated[float](root=value)


class Ducking(BaseModel):
    """Segment-level sidechain ducking keyed off another audio source.

    Mirrors :class:`SidechainConfig` (which is track-level) but attaches to
    an individual :class:`AudioSegment`, enabling per-clip ducking decisions
    such as ducking music only under a specific VO segment.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    threshold_db: float = -20.0
    ratio: float = 4.0
    attack_ms: float = 10.0
    release_ms: float = 200.0


class LoudnessNormalize(BaseModel):
    """EBU R128 loudness normalization applied to the post-mix track bus.

    Defaults target short-form social platforms (TikTok / Reels / Shorts).
    """

    model_config = ConfigDict(extra="forbid")

    target_lufs: float = -16.0
    true_peak_db: float = -1.5
    lra: float = 11.0


class AudioFade(BaseModel):
    """Per-segment fade-in and/or fade-out applied via ffmpeg ``afade``."""

    model_config = ConfigDict(extra="forbid")

    fade_in: float = 0.0
    fade_out: float = 0.0


class EQBand(BaseModel):
    """One band of a parametric EQ."""

    model_config = ConfigDict(extra="forbid")

    frequency_hz: float
    gain_db: Animated[float] = Field(default_factory=lambda: _const_animated_float(0.0))
    q: float = 1.0
    type: Literal["peak", "low_shelf", "high_shelf"] = "peak"


class EQEffect(BaseModel):
    """Multi-band parametric EQ; supersedes the 3-band :class:`EQ` when set.

    Each :class:`EQBand` becomes one ``equalizer`` filter node in the
    rendered track chain.
    """

    model_config = ConfigDict(extra="forbid")

    bands: list[EQBand] = Field(default_factory=list)


class Reverb(BaseModel):
    """Algorithmic reverb via a tuned ``aecho`` tap chain.

    ``room_size`` and ``damping`` shape the underlying echo taps; ``wet``
    is the post-effect mix amount (0..1, animatable).
    """

    model_config = ConfigDict(extra="forbid")

    wet: Animated[float] = Field(default_factory=lambda: _const_animated_float(0.3))
    room_size: float = 0.5
    damping: float = 0.5


class DeepFilterDenoise(BaseModel):
    """Per-segment audio noise removal via DeepFilterNet 3.

    Runs as a pre-pass on the segment's input audio (before ``atrim`` /
    ``afade`` / ``adelay``) and the rest of the audio chain operates on the
    denoised WAV. Cached on disk by ``(source path, source mtime, params)``
    so repeated renders reuse the result.

    Lazy-loaded; requires ``pip install eks-harness[denoise]``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    attenuation_limit_db: float = Field(default=60.0, ge=0)
