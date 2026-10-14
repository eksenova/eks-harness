from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, SerializeAsAny

from eks_harness.ir.catalog.animated import AFloat, AInt
from eks_harness.ir.catalog.curves import Curve
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.refs import RMedia, RStr
from eks_harness.ir.registry import Poly, effect
from eks_harness.ir.time import Time, TimeSet, WordSource
from eks_harness.ir.types import DEFAULT_FONT, Color, Font

GRAPH = frozenset({"ffmpeg_graph"})
FRAMES = frozenset({"frame_pipeline"})
BOTH = frozenset({"ffmpeg_graph", "frame_pipeline"})


class EffectBase(BaseModel, frozen=True, extra="forbid"):
    compile_targets: ClassVar[frozenset[str]] = frozenset()

    kind: str = Field(description="Effect kind")


@effect(description="Brightness offset")
class Brightness(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["brightness"] = Field(default="brightness", description="Brightness offset")
    amount: AFloat = Field(description="Brightness change, animatable")


@effect(description="Contrast multiplier")
class Contrast(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["contrast"] = Field(default="contrast", description="Contrast multiplier")
    amount: AFloat = Field(description="Contrast factor, animatable")


@effect(description="Saturation multiplier")
class Saturation(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["saturation"] = Field(default="saturation", description="Saturation multiplier")
    amount: AFloat = Field(description="Saturation factor, animatable")


@effect(description="Crop to a rectangle in source pixels")
class Crop(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["crop"] = Field(default="crop", description="Crop to a rectangle in source pixels")
    x: AInt = Field(description="Left edge in pixels, animatable")
    y: AInt = Field(description="Top edge in pixels, animatable")
    w: AInt = Field(description="Width in pixels, animatable")
    h: AInt = Field(description="Height in pixels, animatable")


@effect(description="Mirror horizontally, vertically or both")
class Mirror(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["mirror"] = Field(default="mirror", description="Mirror horizontally, vertically or both")
    axis: Literal["horizontal", "vertical", "both"] = Field(default="horizontal", description="Axis to mirror")


@effect(description="Zoom around a normalized center")
class Zoom(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["zoom"] = Field(default="zoom", description="Zoom around a normalized center")
    scale: AFloat = Field(description="Zoom factor, animatable")
    cx: AFloat = Field(default=0.5, description="Zoom center x as a fraction of the width, animatable")
    cy: AFloat = Field(default=0.5, description="Zoom center y as a fraction of the height, animatable")


@effect(description="Pan by pixels")
class Pan(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["pan"] = Field(default="pan", description="Pan by pixels")
    dx: AInt = Field(description="Horizontal shift in pixels, animatable")
    dy: AInt = Field(description="Vertical shift in pixels, animatable")


@effect(description="Playback speed factor")
class SpeedRamp(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["speed_ramp"] = Field(default="speed_ramp", description="Playback speed factor")
    factor: AFloat = Field(description="Speed factor, animatable")


@effect(description="Freeze the frame at a clip-local time for a while")
class Freeze(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["freeze"] = Field(default="freeze", description="Freeze the frame at a clip-local time for a while")
    at: Time = Field(description="Frame to freeze, clip-local unless anchored")
    hold: Duration = Field(description="How long the frame is held")


@effect(description="Key out a color")
class ChromaKey(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["chroma_key"] = Field(default="chroma_key", description="Key out a color")
    color: Color = Field(default="#00ff00", description="Key color")
    similarity: float = Field(default=0.1, ge=0.0, le=1.0, description="How close a color must be to the key to vanish")
    blend: float = Field(default=0.0, ge=0.0, le=1.0, description="Edge softness")


@effect(description="Exposure, temperature and tint, with an optional LUT")
class ColorGrade(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["color_grade"] = Field(default="color_grade", description="Exposure, temperature and tint, with an optional LUT")
    lut: RMedia | None = Field(default=None, description="LUT file applied first, a $ref to a media.source node of kind file")
    exposure: AFloat = Field(default=0.0, description="Exposure change in stops, animatable")
    temperature: AFloat = Field(default=0.0, description="Color temperature shift, animatable")
    tint: AFloat = Field(default=0.0, description="Green-magenta tint shift, animatable")


@effect(description="Fade the clip in or out")
class Fade(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["fade"] = Field(default="fade", description="Fade the clip in or out")
    direction: Literal["in", "out"] = Field(description="Fade in at the clip start or out at its end")
    duration: Duration = Field(description="Fade length")


@effect(description="Gaussian blur")
class Blur(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["blur"] = Field(default="blur", description="Gaussian blur")
    radius: AFloat = Field(description="Blur radius in pixels, animatable")


@effect(description="Unsharp mask")
class Sharpen(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["sharpen"] = Field(default="sharpen", description="Unsharp mask")
    amount: AFloat = Field(description="Sharpening strength, animatable")


@effect(description="Darken the frame edges")
class Vignette(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["vignette"] = Field(default="vignette", description="Darken the frame edges")
    angle: float = Field(default=0.628, description="Vignette angle in radians")
    x0: float = Field(default=0.5, description="Vignette center x as a fraction of the width")
    y0: float = Field(default=0.5, description="Vignette center y as a fraction of the height")


@effect(description="Seeded film grain; the seed derives from the plan when unset")
class FilmGrain(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["film_grain"] = Field(default="film_grain", description="Seeded film grain; the seed derives from the plan when unset")
    intensity: AFloat = Field(description="Grain strength, animatable")
    seed: int | None = Field(default=None, description="Random seed; derived from the node hash and root seed when unset")


@effect(description="Apply a 3D LUT file")
class LUT(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["lut"] = Field(default="lut", description="Apply a 3D LUT file")
    path: RMedia = Field(description="LUT file, a $ref to a media.source node of kind file")


@effect(description="Invert colors")
class Invert(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["invert"] = Field(default="invert", description="Invert colors")


@effect(description="Reduce color levels")
class Posterize(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["posterize"] = Field(default="posterize", description="Reduce color levels")
    levels: AInt = Field(description="Color levels per channel, animatable")


@effect(description="Burn text into the clip")
class TextOverlay(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = GRAPH
    kind: Literal["text_overlay"] = Field(default="text_overlay", description="Burn text into the clip")
    text: RStr = Field(description="Text")
    font: Font = Field(default=DEFAULT_FONT, description="Font: a $ref to a media.source font file (kind file), or the name of a family the renderer ships (IBM Plex Sans, IBM Plex Mono)")
    size: int = Field(default=48, gt=0, description="Font size in pixels")
    color: Color = Field(default="#ffffffff", description="Text color")
    x: AInt = Field(default=100, description="Left position in pixels, animatable")
    y: AInt = Field(default=100, description="Top position in pixels, animatable")
    start: Time | None = Field(default=None, description="When the text appears, clip-local unless anchored")
    duration: Duration | None = Field(default=None, description="How long the text stays; to the clip end when unset")


@effect(description="Lower third title and subtitle")
class LowerThird(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["lower_third"] = Field(default="lower_third", description="Lower third title and subtitle")
    title: RStr = Field(description="Title line")
    subtitle: RStr = Field(default="", description="Line under the title")
    font: Font = Field(default=DEFAULT_FONT, description="Font: a $ref to a media.source font file (kind file), or the name of a family the renderer ships (IBM Plex Sans, IBM Plex Mono)")
    title_size: int = Field(default=72, gt=0, description="Title size in pixels")
    subtitle_size: int = Field(default=36, gt=0, description="Subtitle size in pixels")
    position: Literal["left", "center", "right"] = Field(default="left", description="Horizontal position")
    margin: int = Field(default=120, ge=0, description="Distance from the frame edges in pixels")
    color: Color = Field(default="#ffffffff", description="Text color")
    bg_color: Color = Field(default="#000000b4", description="Background box color")


@effect(description="Image watermark")
class Watermark(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["watermark"] = Field(default="watermark", description="Image watermark")
    path: RMedia = Field(description="Image file, a $ref to a media.source node")
    x: AInt = Field(default=20, description="Left position in pixels, animatable")
    y: AInt = Field(default=20, description="Top position in pixels, animatable")
    opacity: AFloat = Field(default=1.0, description="Opacity from 0 to 1, animatable")
    scale: AFloat = Field(default=1.0, description="Scale factor, animatable")


@effect(description="Animated sticker image")
class StickerOverlay(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["sticker"] = Field(default="sticker", description="Animated sticker image")
    path: RMedia = Field(description="Image file, a $ref to a media.source node")
    x: AInt = Field(default=0, description="Left position in pixels, animatable")
    y: AInt = Field(default=0, description="Top position in pixels, animatable")
    scale: AFloat = Field(default=1.0, description="Scale factor, animatable")
    rotation: AFloat = Field(default=0.0, description="Rotation in degrees, animatable")


@effect(description="Solid color flash at times, shaped by an optional curve")
class Flash(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = BOTH
    kind: Literal["flash"] = Field(default="flash", description="Solid color flash at times, shaped by an optional curve")
    at: Time | TimeSet = Field(description="Time or time set of the flashes, such as t.beats('downbeat')[0:8]")
    duration: Duration = Field(description="Length of each flash")
    color: Color = Field(default="#ffffff", description="Flash color")
    curve: Curve | None = Field(default=None, description="Shape of each flash; a linear fade when unset")


@effect(description="Chromatic channel split")
class RGBSplit(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["rgb_split"] = Field(default="rgb_split", description="Chromatic channel split")
    offset_x: AInt = Field(description="Horizontal channel offset in pixels, animatable")
    offset_y: AInt = Field(default=0, description="Vertical channel offset in pixels, animatable")


@effect(description="Seeded block glitch")
class Glitch(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["glitch"] = Field(default="glitch", description="Seeded block glitch")
    intensity: AFloat = Field(description="Glitch strength, animatable")
    block_size: int = Field(default=16, gt=0, description="Size of displaced blocks in pixels")
    seed: int | None = Field(default=None, description="Random seed; derived from the node hash and root seed when unset")


@effect(description="Camera shake")
class Shake(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["shake"] = Field(default="shake", description="Camera shake")
    intensity: AFloat = Field(description="Shake strength in pixels, animatable")
    freq_hz: float = Field(default=6.0, gt=0, description="Shake frequency in Hz")
    decay: float = Field(default=0.0, description="How fast the shake fades")
    seed: int | None = Field(default=None, description="Random seed; derived from the node hash and root seed when unset")


@effect(description="VHS look")
class VHS(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["vhs"] = Field(default="vhs", description="VHS look")
    scanlines: bool = Field(default=True, description="Draw scanlines")
    chroma_blur: float = Field(default=1.5, description="Chroma blur in pixels")
    noise: float = Field(default=0.05, description="Noise strength")


@effect(description="Datamosh smear")
class Datamosh(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["datamosh"] = Field(default="datamosh", description="Datamosh smear")
    intensity: AFloat = Field(default=0.5, description="Datamosh strength, animatable")


@effect(description="Word-timed captions from a speech-to-text marker source")
class Captions(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["captions"] = Field(default="captions", description="Word-timed captions from a speech-to-text marker source")
    source: WordSource = Field(description="Name of the speech-to-text marker source (STTMarkers name) whose words are shown")
    font: Font = Field(default=DEFAULT_FONT, description="Font: a $ref to a media.source font file (kind file), or the name of a family the renderer ships (IBM Plex Sans, IBM Plex Mono)")
    size: int = Field(default=64, gt=0, description="Font size in pixels")
    color: Color = Field(default="#ffffffff", description="Text color")
    stroke: Color | None = Field(default=None, description="Outline color")
    stroke_width: int = Field(default=0, ge=0, description="Outline width in pixels")
    position: Literal["top", "center", "bottom"] = Field(default="bottom", description="Vertical position")
    margin: int = Field(default=120, ge=0, description="Distance from the frame edge in pixels")
    style: Literal["tiktok", "subtitle", "kinetic"] = Field(default="tiktok", description="Caption style")
    word_animation: Literal["pop", "fade", "none"] = Field(default="pop", description="How each word appears")
    max_words_per_card: int = Field(default=4, ge=1, description="Largest number of words shown at once")


@effect(description="Background matting (MatAnyone)", device_dependent=True)
class MatAnyoneRemove(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["matanyone_remove"] = Field(default="matanyone_remove", description="Background matting (MatAnyone)")
    background: Color = Field(default="#00000000", description="Color behind the subject; transparent by default")


@effect(description="Background matting (BiRefNet)", device_dependent=True)
class BiRefNetRemove(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["birefnet_remove"] = Field(default="birefnet_remove", description="Background matting (BiRefNet)")
    background: Color = Field(default="#00000000", description="Color behind the subject; transparent by default")


@effect(description="Open-vocabulary segmentation and tracking (SAM 3)", device_dependent=True)
class SAM3Track(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["sam3_track"] = Field(default="sam3_track", description="Open-vocabulary segmentation and tracking (SAM 3)")
    prompt: str = Field(min_length=1, description="Text prompt naming the object to track")
    output: Literal["mask", "matte", "highlight"] = Field(default="mask", description="What to produce from the track")
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0, description="Smallest detection confidence kept")


@effect(description="Reframe to an aspect ratio following faces or salience", device_dependent=True)
class AutoReframe(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["auto_reframe"] = Field(default="auto_reframe", description="Reframe to an aspect ratio following faces or salience")
    target_aspect: tuple[int, int] = Field(default=(9, 16), description="Target aspect ratio as (width, height)")
    tracker: Literal["face", "salience"] = Field(default="face", description="What the reframe follows")
    smoothing: float = Field(default=0.85, ge=0.0, le=1.0, description="Camera path smoothing from 0 to 1")


@effect(description="Punch in on the speaker at triggers", device_dependent=True)
class PunchIn(EffectBase, frozen=True):
    compile_targets: ClassVar[frozenset[str]] = FRAMES
    kind: Literal["punch_in"] = Field(default="punch_in", description="Punch in on the speaker at triggers")
    track_speaker: bool = Field(default=True, description="Center the punch-in on the active speaker")
    zoom: AFloat = Field(default=1.2, description="Zoom factor of the punch-in, animatable")
    trigger: Time | TimeSet | None = Field(default=None, description="Times of the punch-ins; every speaker change when unset")


Effect = Annotated[SerializeAsAny[EffectBase], Poly("effect")]

__all__ = [
    "BOTH",
    "FRAMES",
    "GRAPH",
    "LUT",
    "VHS",
    "AutoReframe",
    "BiRefNetRemove",
    "Blur",
    "Brightness",
    "Captions",
    "ChromaKey",
    "ColorGrade",
    "Contrast",
    "Crop",
    "Datamosh",
    "Effect",
    "EffectBase",
    "Fade",
    "FilmGrain",
    "Flash",
    "Freeze",
    "Glitch",
    "Invert",
    "LowerThird",
    "MatAnyoneRemove",
    "Mirror",
    "Pan",
    "Posterize",
    "PunchIn",
    "RGBSplit",
    "SAM3Track",
    "Saturation",
    "Shake",
    "Sharpen",
    "SpeedRamp",
    "StickerOverlay",
    "TextOverlay",
    "Vignette",
    "Watermark",
    "Zoom",
]
