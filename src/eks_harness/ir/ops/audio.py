from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eks_harness.ir.catalog.animated import AFloat
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.refs import RMedia
from eks_harness.ir.registry import Args, op
from eks_harness.ir.time import TICKS_PER_MS, ZERO, Span, Time

Stem = Literal["vocals", "drums", "bass", "other"]
TRACK_NAME = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"


class _Mix(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Compressor(_Mix):
    threshold_db: float = Field(default=-18.0, description="Level in dB above which the compressor acts")
    ratio: float = Field(default=4.0, ge=1.0, description="Compression ratio, input dB over output dB above the threshold")
    attack: Duration = Field(default=Time(ticks=10 * TICKS_PER_MS), description="How fast gain reduction starts")
    release: Duration = Field(default=Time(ticks=200 * TICKS_PER_MS), description="How fast gain reduction recovers")
    makeup_db: float = Field(default=0.0, description="Gain added after compression in dB")


class Duck(_Mix):
    under: str = Field(pattern=TRACK_NAME, description="Name of the track this one ducks under, such as voiceover")
    amount_db: float = Field(default=-12.0, le=0.0, description="Gain reduction while the other track plays, in dB")
    threshold_db: float = Field(default=-30.0, description="Level of the other track that triggers ducking, in dB")
    attack: Duration = Field(default=Time(ticks=10 * TICKS_PER_MS), description="How fast the duck engages")
    release: Duration = Field(default=Time(ticks=250 * TICKS_PER_MS), description="How fast the duck releases")


class EqBand(_Mix):
    frequency_hz: float = Field(gt=0, description="Center or corner frequency in Hz")
    gain_db: AFloat = Field(default=0.0, description="Band gain in dB, animatable")
    q: float = Field(default=1.0, gt=0, description="Bandwidth of the band")
    type: Literal["peak", "low_shelf", "high_shelf", "low_pass", "high_pass"] = Field(default="peak", description="Filter shape of the band")


class Eq(_Mix):
    bands: tuple[EqBand, ...] = Field(default=(), description="Equalizer bands applied in order")


class Fades(_Mix):
    fade_in: Duration | None = Field(default=None, description="Fade-in length at the start of the track")
    fade_out: Duration | None = Field(default=None, description="Fade-out length at the end of the track")


class Reverb(_Mix):
    wet: AFloat = Field(default=0.3, description="Share of reverberated signal, animatable")
    room_size: float = Field(default=0.5, ge=0.0, le=1.0, description="Size of the simulated room from 0 to 1")
    damping: float = Field(default=0.5, ge=0.0, le=1.0, description="High-frequency damping from 0 to 1")


class Denoise(_Mix):
    attenuation_limit_db: float = Field(default=60.0, ge=0.0, description="Largest noise reduction in dB")


class AudioTrack(_Mix):
    name: str = Field(pattern=TRACK_NAME, description="Track name, used by ducks and stems")
    media: RMedia = Field(description="Audio or video media the track plays")
    start: Time = Field(default=ZERO, description="Where the track starts on the timeline")
    src: Span | None = Field(default=None, description="Part of the source media that plays; all of it when unset")
    gain: AFloat = Field(default=0.0, description="Gain in dB, animatable")
    pan: AFloat = Field(default=0.0, description="Stereo pan from -1 (left) to 1 (right), animatable")
    mute: bool = Field(default=False, description="Silence the track")
    stems: tuple[Stem, ...] | None = Field(default=None, description="Separated stems to keep, such as drums and bass; the full mix when unset")
    eq: Eq | None = Field(default=None, description="Equalizer")
    compressor: Compressor | None = Field(default=None, description="Compressor")
    ducks: tuple[Duck, ...] = Field(default=(), description="Sidechain ducks under other tracks")
    fades: Fades | None = Field(default=None, description="Fade in and out")
    reverb: Reverb | None = Field(default=None, description="Reverb")
    denoise: Denoise | None = Field(default=None, description="Noise reduction")


class Master(_Mix):
    loudness: float | None = Field(default=None, le=0.0, description="Target integrated loudness in LUFS")
    true_peak: float | None = Field(default=None, le=0.0, description="True peak ceiling in dBTP")
    lra: float | None = Field(default=None, gt=0.0, description="Target loudness range in LU")
    limiter: bool = Field(default=True, description="Run a limiter on the master bus")
    gain: float = Field(default=0.0, description="Master gain in dB")


class Mix(_Mix):
    tracks: tuple[AudioTrack, ...] = Field(default=(), description="Tracks of the mix")
    master: Master = Field(default=Master(), description="Master bus settings")
    sample_rate: int = Field(default=48000, ge=8000, description="Output sample rate in Hz")
    channels: Literal[1, 2] = Field(default=2, description="Output channels: 1 (mono) or 2 (stereo)")
    duration: Duration | None = Field(default=None, description="Length of the mix; the end of the last track when unset")

    @model_validator(mode="after")
    def _check_tracks(self) -> Self:
        names = [track.name for track in self.tracks]
        for index, name in enumerate(names):
            if name in names[:index]:
                raise ValueError(f"the mix has two tracks named {name!r}; audio track names are unique")
        for track in self.tracks:
            for duck in track.ducks:
                if duck.under == track.name or duck.under not in names:
                    known = ", ".join(repr(name) for name in names if name != track.name) or "none"
                    raise ValueError(
                        f"audio track {track.name!r} ducks under {duck.under!r}, which is not another track of the "
                        f"mix; other tracks: {known}"
                    )
        return self


@op("audio", name="mix", out="media", pure=True, location="node", bounded=True,
    description="Render the audio mix graph: tracks, stems, gain, EQ, compression, ducking, loudness and true peak",
    examples=[
        {
            "mix": {
                "tracks": [
                    {"name": "song", "media": {"$ref": "n0000000001", "path": []}, "start": "0s", "gain": -6.0},
                    {"name": "voiceover", "media": {"$ref": "n0000000002", "path": []}, "start": "1s"},
                ],
                "master": {"loudness": -14.0, "true_peak": -1.0},
            },
        },
    ])
class MixDown(Args):
    mix: Mix = Field(description="The mix graph to render")


__all__ = [
    "TRACK_NAME",
    "AudioTrack",
    "Compressor",
    "Denoise",
    "Duck",
    "Eq",
    "EqBand",
    "Fades",
    "Master",
    "Mix",
    "MixDown",
    "Reverb",
    "Stem",
]
