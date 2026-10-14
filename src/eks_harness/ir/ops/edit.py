from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eks_harness.ir.catalog.animated import AFloat
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.catalog.effects import Effect
from eks_harness.ir.catalog.transitions import Transition
from eks_harness.ir.ops.overlay import Overlay
from eks_harness.ir.refs import RMedia
from eks_harness.ir.registry import Args, op
from eks_harness.ir.time import ZERO, Fps, Span, Time
from eks_harness.ir.types import Color

Blend = Literal["normal", "add", "multiply", "screen", "overlay"]
Fit = Literal["contain", "cover", "fill"]
TRACK_NAME = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"
CLIP_ID = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}/[0-9]+$"
Pixels = Annotated[int, Field(gt=0)]
SOFTWARE_ENCODERS: tuple[str, ...] = ("libsvtav1", "libx264")
HARDWARE_ENCODERS: tuple[str, ...] = ("amf", "nvenc", "qsv", "videotoolbox")
Encoder = Literal["libsvtav1", "libx264", "amf", "nvenc", "qsv", "videotoolbox"]
DEFAULT_ENCODER_THREADS = 8


class _Edit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RenderSettings(_Edit):
    encoder: Encoder | None = Field(default=None, description="Video encoder: libx264 or libsvtav1 (software, the same bytes on every machine for the same threads), or amf, nvenc, qsv or videotoolbox (hardware, keyed per machine); chosen per machine and recorded in the cache key when unset")
    threads: int = Field(default=DEFAULT_ENCODER_THREADS, ge=1, le=64, description="Encoder threads, always passed to the encoder; a software encoder's output depends on its thread count, so it is fixed by the plan instead of the machine's core count")
    preset: str = Field(default="tiktok-1080-h264", description="Output preset such as tiktok-1080-h264")
    quality: Literal["preview", "draft", "final"] = Field(default="final", description="Render quality: preview, draft or final")
    color_primaries: str = Field(default="bt709", description="Color primaries tag of the output")
    color_trc: str = Field(default="bt709", description="Transfer characteristics tag of the output")
    colorspace: str = Field(default="bt709", description="Color space tag of the output")
    pix_fmt: str = Field(default="yuv420p", description="Pixel format of the output")
    bitrate_kbps: int | None = Field(default=None, gt=0, description="Target bitrate in kbit/s; quality based when unset")
    crf: int | None = Field(default=None, ge=0, le=63, description="Constant rate factor; the preset's when unset")


class Clip(_Edit):
    id: str = Field(pattern=CLIP_ID, description="Clip id, track name and index such as main/0")
    media: RMedia = Field(description="Media the clip shows")
    start: Time = Field(default=ZERO, description="Where the clip starts on the timeline")
    src: Span | None = Field(default=None, description="Part of the source media the clip plays; all of it when unset")
    speed: AFloat = Field(default=1.0, description="Playback speed, animatable")
    opacity: AFloat = Field(default=1.0, description="Opacity from 0 to 1, animatable")
    x: AFloat = Field(default=0.0, description="Horizontal offset in pixels, animatable")
    y: AFloat = Field(default=0.0, description="Vertical offset in pixels, animatable")
    scale: AFloat = Field(default=1.0, description="Scale factor, animatable")
    rotation: AFloat = Field(default=0.0, description="Rotation in degrees, animatable")
    fit: Fit = Field(default="cover", description="How the media fits the frame")
    audio: bool = Field(default=False, description="Play the clip's own audio")
    gain: AFloat = Field(default=0.0, description="Gain of the clip's audio in dB, animatable")
    effects: tuple[Effect, ...] = Field(default=(), description="Effects applied in order")
    transition_in: Transition | None = Field(default=None, description="Transition from the previous clip")
    transition_out: Transition | None = Field(default=None, description="Transition to the next clip")


class Track(_Edit):
    name: str = Field(pattern=TRACK_NAME, description="Track name")
    z: int = Field(default=0, description="Stacking order; higher tracks draw on top")
    blend: Blend = Field(default="normal", description="Blend mode against the tracks below")
    opacity: AFloat = Field(default=1.0, description="Track opacity from 0 to 1, animatable")
    clips: tuple[Clip, ...] = Field(default=(), description="Clips of the track")
    overlays: tuple[Overlay, ...] = Field(default=(), description="Overlays drawn on the track")


class Timeline(_Edit):
    fps: Fps = Field(description="Frame rate")
    size: tuple[Pixels, Pixels] = Field(description="Frame size as (width, height) in pixels")
    duration: Duration | None = Field(default=None, description="Length of the edit; the end of the last clip when unset")
    seed: int = Field(default=0, description="Root random seed of effects and curves")
    background: Color = Field(default="#000000", description="Background color")
    tracks: tuple[Track, ...] = Field(default=(), description="Video tracks")
    audio: RMedia | None = Field(default=None, description="Mixed audio to mux in, a $ref to an audio.mix node")
    render: RenderSettings = Field(default=RenderSettings(), description="Encoder settings")

    @model_validator(mode="after")
    def _check_tracks(self) -> Self:
        names: set[str] = set()
        clip_ids: set[str] = set()
        for track in self.tracks:
            if track.name in names:
                raise ValueError(f"the timeline has two tracks named {track.name!r}; track names are unique")
            names.add(track.name)
            for clip in track.clips:
                if not clip.id.startswith(f"{track.name}/"):
                    raise ValueError(
                        f"clip {clip.id!r} is in track {track.name!r}; clip ids are '{track.name}/<index>'"
                    )
                if clip.id in clip_ids:
                    raise ValueError(f"the timeline has two clips with the id {clip.id!r}; clip ids are unique")
                clip_ids.add(clip.id)
        return self


@op("edit", name="render", out="media", pure=True, location="node", bounded=True,
    description="Render a video timeline: tracks of clips with effects, transitions and overlays, plus the audio mix",
    examples=[
        {
            "timeline": {
                "fps": 30,
                "size": [1080, 1920],
                "tracks": [
                    {
                        "name": "main",
                        "clips": [
                            {
                                "id": "main/0",
                                "media": {"$ref": "n0000000001", "path": []},
                                "start": "0s",
                                "src": {"start": "2s", "end": "14s"},
                                "effects": [{"kind": "zoom", "scale": 1.08}],
                                "transition_out": {"kind": "crossfade", "duration": "8f"},
                            },
                        ],
                    },
                ],
            },
        },
    ])
class Render(Args):
    timeline: Timeline = Field(description="The timeline to render")


__all__ = [
    "CLIP_ID",
    "DEFAULT_ENCODER_THREADS",
    "HARDWARE_ENCODERS",
    "SOFTWARE_ENCODERS",
    "TRACK_NAME",
    "Blend",
    "Clip",
    "Encoder",
    "Fit",
    "Pixels",
    "Render",
    "RenderSettings",
    "Timeline",
    "Track",
]
