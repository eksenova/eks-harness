from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializeAsAny

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.registry import Poly, transition
from eks_harness.ir.time import StreamName

Direction = Literal["left", "right", "up", "down"]


class TransitionBase(BaseModel, frozen=True, extra="forbid"):
    kind: str = Field(description="Transition kind")


class _Timed(TransitionBase, frozen=True):
    duration: Duration = Field(description="Length of the transition")


@transition(description="Hard cut")
class Cut(TransitionBase, frozen=True):
    kind: Literal["cut"] = Field(default="cut", description="Hard cut")


@transition(description="Crossfade")
class Crossfade(_Timed, frozen=True):
    kind: Literal["crossfade"] = Field(default="crossfade", description="Crossfade")


@transition(description="Fade through black")
class DipToBlack(_Timed, frozen=True):
    kind: Literal["dip_to_black"] = Field(default="dip_to_black", description="Fade through black")


@transition(description="Fade through white")
class DipToWhite(_Timed, frozen=True):
    kind: Literal["dip_to_white"] = Field(default="dip_to_white", description="Fade through white")


@transition(description="Hard-edged wipe")
class Wipe(_Timed, frozen=True):
    kind: Literal["wipe"] = Field(default="wipe", description="Hard-edged wipe")
    direction: Direction = Field(default="right", description="Direction the wipe moves")


@transition(description="Incoming clip slides over")
class Slide(_Timed, frozen=True):
    kind: Literal["slide"] = Field(default="slide", description="Incoming clip slides over")
    direction: Direction = Field(default="right", description="Direction the incoming clip slides")


@transition(description="Incoming clip pushes the outgoing one away")
class Push(_Timed, frozen=True):
    kind: Literal["push"] = Field(default="push", description="Incoming clip pushes the outgoing one away")
    direction: Direction = Field(default="left", description="Direction the clips are pushed")


@transition(description="Pixelize dissolve with a chromatic split")
class GlitchTransition(_Timed, frozen=True):
    kind: Literal["glitch_transition"] = Field(default="glitch_transition", description="Pixelize dissolve with a chromatic split")
    intensity: float = Field(default=0.6, ge=0.0, le=1.0, description="Glitch strength from 0 to 1")


@transition(description="Zoom blur into the incoming clip")
class ZoomTransition(_Timed, frozen=True):
    kind: Literal["zoom"] = Field(default="zoom", description="Zoom blur into the incoming clip")


@transition(description="Noise dissolve")
class Dissolve(_Timed, frozen=True):
    kind: Literal["dissolve"] = Field(default="dissolve", description="Noise dissolve")


@transition(description="Blur out and in")
class BlurThrough(_Timed, frozen=True):
    kind: Literal["blur_through"] = Field(default="blur_through", description="Blur out and in")


@transition(description="Circular iris")
class Iris(_Timed, frozen=True):
    kind: Literal["iris"] = Field(default="iris", description="Circular iris")
    mode: Literal["open", "close"] = Field(default="open", description="Open onto the next clip or close the current one")


@transition(description="Radial sweep")
class Radial(_Timed, frozen=True):
    kind: Literal["radial"] = Field(default="radial", description="Radial sweep")


@transition(description="Mosaic blend")
class Pixelize(_Timed, frozen=True):
    kind: Literal["pixelize"] = Field(default="pixelize", description="Mosaic blend")


@transition(description="Desaturate through the boundary")
class FadeGrays(_Timed, frozen=True):
    kind: Literal["fade_grays"] = Field(default="fade_grays", description="Desaturate through the boundary")


@transition(description="Feathered luma wipe")
class LumaWipe(_Timed, frozen=True):
    kind: Literal["luma_wipe"] = Field(default="luma_wipe", description="Feathered luma wipe")
    direction: Direction = Field(default="right", description="Direction of the luma gradient")


@transition(description="Wipe with an RGB split edge")
class RGBShiftWipe(_Timed, frozen=True):
    kind: Literal["rgb_shift_wipe"] = Field(default="rgb_shift_wipe", description="Wipe with an RGB split edge")
    direction: Direction = Field(default="right", description="Direction of the wipe")
    intensity: float = Field(default=0.6, ge=0.0, le=1.0, description="Channel shift strength from 0 to 1")


@transition(description="Motion-blurred whip pan")
class WhipPan(_Timed, frozen=True):
    kind: Literal["whip_pan"] = Field(default="whip_pan", description="Motion-blurred whip pan")
    direction: Direction = Field(default="right", description="Direction of the pan")
    intensity: float = Field(default=0.6, ge=0.0, le=1.0, description="Motion blur strength from 0 to 1")


@transition(description="Crossfade with a light leak bloom")
class LightLeak(_Timed, frozen=True):
    kind: Literal["light_leak"] = Field(default="light_leak", description="Crossfade with a light leak bloom")
    intensity: float = Field(default=0.7, ge=0.0, le=1.0, description="Light leak strength from 0 to 1")


@transition(description="Warm film burn over a crossfade")
class FilmBurn(_Timed, frozen=True):
    kind: Literal["film_burn"] = Field(default="film_burn", description="Warm film burn over a crossfade")
    intensity: float = Field(default=0.7, ge=0.0, le=1.0, description="Burn strength from 0 to 1")


@transition(description="Crossfade that flashes on every beat of a stream inside the window")
class BeatFlash(_Timed, frozen=True):
    kind: Literal["beat_flash"] = Field(default="beat_flash", description="Crossfade that flashes on every beat of a stream inside the window")
    stream: StreamName = Field(default="beat", description="Stream the flash lands on: beat, downbeat or bar of the clock tempo, or a media.markers stream")
    intensity: float = Field(default=0.7, ge=0.0, le=1.0, description="Flash strength from 0 to 1")


@transition(description="Pixelize blend that glitches on every beat of a stream inside the window")
class BeatGlitch(_Timed, frozen=True):
    kind: Literal["beat_glitch"] = Field(default="beat_glitch", description="Pixelize blend that glitches on every beat of a stream inside the window")
    stream: StreamName = Field(default="beat", description="Stream the glitch lands on: beat, downbeat or bar of the clock tempo, or a media.markers stream")
    intensity: float = Field(default=0.7, ge=0.0, le=1.0, description="Glitch strength from 0 to 1")


Transition = Annotated[SerializeAsAny[TransitionBase], Poly("transition")]

__all__ = [
    "BeatFlash",
    "BeatGlitch",
    "BlurThrough",
    "Crossfade",
    "Cut",
    "DipToBlack",
    "DipToWhite",
    "Direction",
    "Dissolve",
    "FadeGrays",
    "FilmBurn",
    "GlitchTransition",
    "Iris",
    "LightLeak",
    "LumaWipe",
    "Pixelize",
    "Push",
    "RGBShiftWipe",
    "Radial",
    "Slide",
    "Transition",
    "TransitionBase",
    "WhipPan",
    "Wipe",
    "ZoomTransition",
]
