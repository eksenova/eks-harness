"""Transitions between adjacent segments."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BeatFlash",
    "BeatGlitch",
    "BlurThrough",
    "Crossfade",
    "Cut",
    "DipToBlack",
    "DipToWhite",
    "Dissolve",
    "FadeGrays",
    "FilmBurn",
    "GlitchTransition",
    "Iris",
    "LightLeak",
    "LumaWipe",
    "Pixelize",
    "PluginTransition",
    "Push",
    "RGBShiftWipe",
    "Radial",
    "Slide",
    "Transition",
    "WhipPan",
    "Wipe",
    "ZoomTransition",
]


_Direction = Literal["left", "right", "up", "down"]


class _TransitionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cut(_TransitionBase):
    """Hard cut. Default for adjacent segments."""

    kind: Literal["cut"] = "cut"


class Crossfade(_TransitionBase):
    kind: Literal["crossfade"] = "crossfade"
    duration: float = Field(gt=0)


class DipToBlack(_TransitionBase):
    """Fade outgoing segment to black, then fade in incoming.

    Renders via ffmpeg ``xfade=transition=fadeblack``.
    """

    kind: Literal["dip_to_black"] = "dip_to_black"
    duration: float = Field(gt=0)


class DipToWhite(_TransitionBase):
    """Fade outgoing segment to white, then fade in incoming.

    Renders via ffmpeg ``xfade=transition=fadewhite``.
    """

    kind: Literal["dip_to_white"] = "dip_to_white"
    duration: float = Field(gt=0)


class Wipe(_TransitionBase):
    """Hard-edged wipe in the given direction.

    Renders via ffmpeg ``xfade=transition=wipe<direction>``.
    """

    kind: Literal["wipe"] = "wipe"
    duration: float = Field(gt=0)
    direction: _Direction = "right"


class Slide(_TransitionBase):
    """Incoming segment slides over outgoing one.

    Renders via ffmpeg ``xfade=transition=slide<direction>``.
    """

    kind: Literal["slide"] = "slide"
    duration: float = Field(gt=0)
    direction: _Direction = "right"


class Push(_TransitionBase):
    """Incoming segment pushes outgoing one off-screen.

    Renders via ffmpeg ``xfade=transition=squeeze<direction>`` - the
    squeeze presets are the closest analogue to a true push in ffmpeg's
    built-in xfade set.
    """

    kind: Literal["push"] = "push"
    duration: float = Field(gt=0)
    direction: _Direction = "left"


class GlitchTransition(_TransitionBase):
    """Digital-glitch transition between two segments.

    Not a plain ffmpeg ``xfade`` preset - the boundary is rendered as a
    blocky ``pixelize`` dissolve with a chromatic RGB-channel split
    (``rgbashift``) gated to the transition window, giving a genuine
    digital-corruption look rather than a smooth blend. ``intensity``
    scales the chroma-split magnitude (0 = none, 1 = strong).
    """

    kind: Literal["glitch_transition"] = "glitch_transition"
    duration: float = Field(gt=0)
    intensity: float = Field(default=0.6, ge=0.0, le=1.0)


class ZoomTransition(_TransitionBase):
    """Zoom-blur push into the incoming segment.

    Renders via ffmpeg ``xfade=transition=zoomin``. Named ``ZoomTransition``
    rather than ``Zoom`` because :class:`eks_harness.video.ir.effects.Zoom` already
    occupies that name in the public namespace; the discriminator ``kind`` is
    still ``"zoom"``.
    """

    kind: Literal["zoom"] = "zoom"
    duration: float = Field(gt=0)


class Dissolve(_TransitionBase):
    """Random-noise dissolve between segments.

    Renders via ffmpeg ``xfade=transition=dissolve``.
    """

    kind: Literal["dissolve"] = "dissolve"
    duration: float = Field(gt=0)


class BlurThrough(_TransitionBase):
    """Blur-out / blur-in blend through the boundary.

    Renders via ffmpeg ``xfade=transition=hblur``.
    """

    kind: Literal["blur_through"] = "blur_through"
    duration: float = Field(gt=0)


class Iris(_TransitionBase):
    """Circular iris that opens onto or closes off the incoming segment.

    Renders via ffmpeg ``xfade=transition=circleopen`` (``mode="open"``) or
    ``circleclose`` (``mode="close"``).
    """

    kind: Literal["iris"] = "iris"
    duration: float = Field(gt=0)
    mode: Literal["open", "close"] = "open"


class Radial(_TransitionBase):
    """Radial sweep wipe around the frame centre.

    Renders via ffmpeg ``xfade=transition=radial``.
    """

    kind: Literal["radial"] = "radial"
    duration: float = Field(gt=0)


class Pixelize(_TransitionBase):
    """Mosaic pixelisation blend between segments.

    Renders via ffmpeg ``xfade=transition=pixelize``.
    """

    kind: Literal["pixelize"] = "pixelize"
    duration: float = Field(gt=0)


class FadeGrays(_TransitionBase):
    """Desaturate to grayscale through the boundary, then recolour.

    Renders via ffmpeg ``xfade=transition=fadegrays``.
    """

    kind: Literal["fade_grays"] = "fade_grays"
    duration: float = Field(gt=0)


class LumaWipe(_TransitionBase):
    """Soft gradient (luma) wipe in the given direction.

    Renders via ffmpeg's built-in gradient wipes
    ``xfade=transition=smooth<direction>`` (smoothleft / smoothright /
    smoothup / smoothdown) - a feathered edge rather than the hard edge of
    :class:`Wipe`.
    """

    kind: Literal["luma_wipe"] = "luma_wipe"
    duration: float = Field(gt=0)
    direction: _Direction = "right"


class RGBShiftWipe(_TransitionBase):
    """Directional wipe with a chromatic RGB-channel split over the boundary.

    Not a plain xfade preset: an ``xfade=transition=wipe<direction>`` is
    followed by an ``rgbashift`` gated to the transition window, splitting the
    red and blue channels apart for a chromatic-aberration edge. ``intensity``
    scales the channel-split magnitude (0 = none, 1 = strong).
    """

    kind: Literal["rgb_shift_wipe"] = "rgb_shift_wipe"
    duration: float = Field(gt=0)
    direction: _Direction = "right"
    intensity: float = Field(default=0.6, ge=0.0, le=1.0)


class WhipPan(_TransitionBase):
    """Motion-blurred whip-pan slide in the given direction.

    Not a plain xfade preset: an ``xfade=transition=slide<direction>`` is
    followed by a directional ``avgblur`` gated to the transition window,
    smearing the frame along the pan axis. ``intensity`` scales the blur
    kernel size (0 = none, 1 = strong).
    """

    kind: Literal["whip_pan"] = "whip_pan"
    duration: float = Field(gt=0)
    direction: _Direction = "right"
    intensity: float = Field(default=0.6, ge=0.0, le=1.0)


class LightLeak(_TransitionBase):
    """Crossfade overlaid with a Gaussian brightness bloom peaking mid-window.

    Not a plain xfade preset: an ``xfade=transition=fade`` is followed by a
    time-varying ``eq`` brightness pulse (``eval=frame``) that swells to a
    peak at the centre of the transition window, mimicking a lens light
    leak. ``intensity`` scales the peak brightness (0 = none, 1 = strong).
    """

    kind: Literal["light_leak"] = "light_leak"
    duration: float = Field(gt=0)
    intensity: float = Field(default=0.7, ge=0.0, le=1.0)


class FilmBurn(_TransitionBase):
    """Stylized warm film-burn over a crossfade boundary.

    A procedural approximation: a crossfade is overlaid with a Gaussian
    brightness bloom, a warm ``colorbalance`` push, and a gated ``rgbashift``
    chroma bleed. This is a synthesised burn from ffmpeg primitives, not a
    photoreal film-burn - a texture-overlay burn is future work. ``intensity``
    scales the bloom, chroma bleed, and overall strength (0 = none, 1 = strong).
    """

    kind: Literal["film_burn"] = "film_burn"
    duration: float = Field(gt=0)
    intensity: float = Field(default=0.7, ge=0.0, le=1.0)


class BeatFlash(_TransitionBase):
    """Crossfade that flashes white on every beat inside the transition window.

    Not a plain xfade preset: an ``xfade=transition=fade`` is overlaid with a
    sum of narrow Gaussian brightness bumps (``eq``, ``eval=frame``), one per
    beat of ``stream`` that falls inside the window. The renderer threads the
    window-local beat times from the project's resolved markers; when no beat
    lands in the window it falls back to a single pulse at mid-window so the
    transition is never a silent no-op. ``intensity`` scales the flash
    brightness (0 = none, 1 = strong).
    """

    kind: Literal["beat_flash"] = "beat_flash"
    duration: float = Field(gt=0)
    stream: str = "kick"
    intensity: float = Field(default=0.7, ge=0.0, le=1.0)


class BeatGlitch(_TransitionBase):
    """Pixelize blend that bursts a chromatic glitch on every beat in the window.

    Not a plain xfade preset: an ``xfade=transition=pixelize`` is followed by an
    ``rgbashift`` whose channel split is gated to a short pulse around each beat
    of ``stream`` inside the transition window. The renderer threads the
    window-local beat times from the project's resolved markers; when no beat
    lands in the window it falls back to a single pulse at mid-window so the
    transition is never a silent no-op. ``intensity`` scales the channel-split
    magnitude (0 = none, 1 = strong).
    """

    kind: Literal["beat_glitch"] = "beat_glitch"
    duration: float = Field(gt=0)
    stream: str = "kick"
    intensity: float = Field(default=0.7, ge=0.0, le=1.0)


class PluginTransition(_TransitionBase):
    """A transition rendered by a registered transition plugin (``name``)."""

    kind: Literal["plugin"] = "plugin"
    name: str
    duration: float = Field(default=0.5, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)


Transition = Annotated[
    Cut
    | Crossfade
    | DipToBlack
    | DipToWhite
    | Wipe
    | Slide
    | Push
    | GlitchTransition
    | ZoomTransition
    | Dissolve
    | BlurThrough
    | Iris
    | Radial
    | Pixelize
    | FadeGrays
    | LumaWipe
    | RGBShiftWipe
    | WhipPan
    | LightLeak
    | FilmBurn
    | BeatFlash
    | BeatGlitch
    | PluginTransition,
    Field(discriminator="kind"),
]
