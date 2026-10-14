from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from eks_harness.ir.catalog.durations import Length
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    REGISTRY,
    PresetModel,
    PresetSpec,
    Provenance,
    builtin_version,
    preset,
)
from eks_harness.ir.time import TICKS_PER_MS, Time
from eks_harness.ir.types import Color, ShippedFont, ShippedFontFile


class Viewport(PresetModel):
    preset_category: ClassVar[str] = "viewport"

    width: int = Field(gt=0, description="Width in CSS pixels")
    height: int = Field(gt=0, description="Height in CSS pixels")
    scale: float = Field(default=1.0, gt=0, description="Device pixel ratio")
    mobile: bool = Field(default=False, description="Emulate a mobile browser (meta viewport, mobile user agent)")
    touch: bool = Field(default=False, description="Emulate a touch screen")


class Pace(PresetModel):
    preset_category: ClassVar[str] = "pace"

    move: Length = Field(description="Pointer travel time to a target")
    dwell: Length = Field(description="Pause on a target before acting")
    type_per_char: Length = Field(description="Time per typed character")
    hold: Length = Field(description="Pause after an action")
    screen_hold: Length = Field(description="Pause after a screen change")
    press: Length = Field(description="Length of a press")


class NetProfile(PresetModel):
    preset_category: ClassVar[str] = "network"

    latency: Length = Field(description="Added round-trip latency")
    down_kbps: int | None = Field(default=None, gt=0, description="Download bandwidth in kbit/s; unlimited when unset")
    up_kbps: int | None = Field(default=None, gt=0, description="Upload bandwidth in kbit/s; unlimited when unset")
    loss: float = Field(default=0.0, ge=0.0, le=1.0, description="Share of dropped packets from 0 to 1")
    offline: bool = Field(default=False, description="No network at all")


class StyleFonts(PresetModel):
    family: ShippedFont = Field(description="Family of every overlay text, one the overlay renderer ships")
    regular: ShippedFontFile = Field(description="Shipped font file of the regular weight, used for callouts, captions and subtitles")
    semibold: ShippedFontFile = Field(description="Shipped font file of the semibold weight, used for badges and titles")


class StyleColors(PresetModel):
    badge: Color = Field(description="Fill of numbered step badges")
    badge_text: Color = Field(description="Number text inside step badges")
    callout: Color = Field(description="Fill of the callout box")
    callout_text: Color = Field(description="Text of callouts and arrow labels")
    highlight: Color = Field(description="Outline and tint of highlight boxes around a target")
    arrow: Color = Field(description="Arrow lines, arrow heads and callout leader lines")
    spotlight: Color = Field(description="Dimming layer painted around a spotlighted target")
    caption_bar: Color = Field(description="Background bar behind captions")
    caption_text: Color = Field(description="Caption text")
    title_card: Color = Field(description="Full frame background of title cards")
    title_text: Color = Field(description="Title and subtitle text on title cards")


class OverlayStyle(PresetModel):
    preset_category: ClassVar[str] = "style"

    ref_width: int = Field(gt=0, description=(
        "Reference frame width in pixels; every pixel size of the style is scaled by the actual frame width "
        "divided by this"))
    fonts: StyleFonts = Field(description="Fonts used for overlay text")
    colors: StyleColors = Field(description="Colors of each overlay element")
    stroke: int = Field(ge=0, description="Outline width of highlight boxes, in reference pixels")
    halo: int = Field(ge=0, description="Width of the white halo around badges and callout boxes, in reference pixels")
    badge_shape: Literal["circle", "rounded"] = Field(
        default="circle", description="Shape of step badges: a circle or a rounded square")
    badge_size: int = Field(gt=0, description="Diameter (or side) of step badges, in reference pixels")
    badge_font_size: int = Field(gt=0, description="Font size of badge numbers, in reference pixels")
    callout_font_size: int = Field(gt=0, description="Font size of callout and arrow label text, in reference pixels")
    callout_padding: int = Field(ge=0, description="Inner padding of the callout box, in reference pixels")
    callout_leader_width: int = Field(
        ge=0, description="Width of the leader line from a callout to its target, in reference pixels")
    callout_max_width: int = Field(gt=0, description="Maximum callout box width before text wraps, in reference pixels")
    arrow_width: int = Field(gt=0, description="Line width of arrows, in reference pixels")
    arrow_head: int = Field(gt=0, description="Size of the arrow head, in reference pixels")
    spotlight_dim: float = Field(
        ge=0.0, le=1.0, description="Opacity of the dimming layer around a spotlight, from 0 (none) to 1 (opaque)")
    caption_font_size: int = Field(gt=0, description="Font size of caption text, in reference pixels")
    caption_bar_height: int = Field(gt=0, description="Height of the caption bar, in reference pixels")
    title_font_size: int = Field(gt=0, description="Font size of title card titles, in reference pixels")
    title_sub_font_size: int = Field(gt=0, description="Font size of title card subtitles, in reference pixels")


class PersonaPreset(PresetModel):
    preset_category: ClassVar[str] = "persona"

    label: str = Field(description="Human readable name of the persona shown in reports and pickers")
    username: str = Field(default="", description="Login user name or e-mail; empty when the persona has none")
    secret_ref: str = Field(
        default="", description="Reference to the stored secret (password) of the persona, never the secret itself")
    tenant: str = Field(default="", description="Tenant the persona logs in to; empty for host level personas")
    roles: tuple[str, ...] = Field(default=(), description="Role names the persona holds")


def _ms(value: int) -> Time:
    return Time(ticks=value * TICKS_PER_MS)


def _style(badge: str) -> OverlayStyle:
    return OverlayStyle(
        ref_width=720,
        fonts=StyleFonts(family="IBM Plex Sans", regular="IBMPlexSans-Regular.ttf",
                         semibold="IBMPlexSans-SemiBold.ttf"),
        colors=StyleColors(
            badge=badge, badge_text="#ffffff", callout="#ffffff", callout_text="#1a1a1a", highlight=badge,
            arrow=badge, spotlight="#000000", caption_bar="#111111", caption_text="#ffffff", title_card="#14213d",
            title_text="#ffffff"),
        stroke=3,
        halo=3,
        badge_shape="circle",
        badge_size=28,
        badge_font_size=16,
        callout_font_size=15,
        callout_padding=10,
        callout_leader_width=2,
        callout_max_width=280,
        arrow_width=3,
        arrow_head=12,
        spotlight_dim=0.6,
        caption_font_size=16,
        caption_bar_height=44,
        title_font_size=40,
        title_sub_font_size=20,
    )


@preset("desktop", description="1920x1080 desktop browser")
def desktop() -> Viewport:
    return Viewport(width=1920, height=1080)


@preset("phone", description="390x844 phone at 3x with touch")
def phone() -> Viewport:
    return Viewport(width=390, height=844, scale=3.0, mobile=True, touch=True)


@preset("tablet", description="820x1180 tablet at 2x with touch")
def tablet() -> Viewport:
    return Viewport(width=820, height=1180, scale=2.0, mobile=True, touch=True)


def _demo_pace() -> Pace:
    return Pace(move=_ms(300), dwell=_ms(400), type_per_char=_ms(40), hold=_ms(700), screen_hold=_ms(1500),
                press=_ms(250))


def _network(latency_ms: int, down: int | None, up: int | None, loss: float = 0.0, offline: bool = False) -> NetProfile:
    return NetProfile(latency=_ms(latency_ms), down_kbps=down, up_kbps=up, loss=loss, offline=offline)


def _builtin_presets() -> list[tuple[str, str, PresetModel, str]]:
    return [
        ("pace", "demo", _demo_pace(), "Demo pacing: 300ms moves, 400ms dwell, 40ms per typed character"),
        ("network", "wifi", _network(2, None, None), "Wi-Fi: 2ms latency, unlimited bandwidth"),
        ("network", "4g", _network(60, 9000, 9000), "4G: 60ms latency, 9000/9000 kbps"),
        ("network", "3g", _network(563, 1440, 675), "Fast 3G (Chrome): 563ms latency, 1440/675 kbps"),
        ("network", "slow-3g", _network(2000, 400, 400), "Slow 3G (Chrome): 2000ms latency, 400/400 kbps"),
        ("network", "offline", _network(0, None, None, offline=True), "No network"),
        ("style", "kb", _style("#b8860b"), "Knowledge base overlays: gold badges and arrows"),
        ("style", "review", _style("#c0392b"), "Review overlays: red badges and arrows"),
    ]


def register_builtin_presets() -> None:
    provenance = Provenance(BUILTIN_PLUGIN, builtin_version())
    source = f"{__name__}:register_builtin_presets"
    for category, name, value, description in _builtin_presets():
        REGISTRY.register_preset(PresetSpec(
            category=category, name=name, value=value, description=description, provenance=provenance,
            source=source))


register_builtin_presets()


__all__ = [
    "NetProfile",
    "OverlayStyle",
    "Pace",
    "PersonaPreset",
    "StyleColors",
    "StyleFonts",
    "Viewport",
    "desktop",
    "phone",
    "register_builtin_presets",
    "tablet",
]
