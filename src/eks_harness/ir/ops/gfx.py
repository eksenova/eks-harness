from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from eks_harness.ir.catalog.animated import AColor, AFloat
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.catalog.easing import Easing
from eks_harness.ir.catalog.keys import Keyframe
from eks_harness.ir.ops.edit import Fit
from eks_harness.ir.ops.scene import EVENT_NAME, SCENE_SEED_MAX, Trigger, typed_value
from eks_harness.ir.refs import RMedia, RStr
from eks_harness.ir.registry import Args, op
from eks_harness.ir.time import Fps, Span, Time
from eks_harness.ir.types import DEFAULT_FONT, Color, Font

LAYER_ID = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
GFX_FIXED_FIELDS = frozenset({"id", "kind", "children"})
GfxBlend = Literal[
    "normal", "add", "multiply", "screen", "overlay", "src_atop", "dst_in", "dst_out", "xor", "darken", "lighten",
    "color_dodge", "color_burn", "soft_light", "hard_light", "difference", "exclusion",
]
MaskMode = Literal["alpha", "luminance", "alpha_inverted", "luminance_inverted"]
GradientStop = tuple[Annotated[float, Field(ge=0.0, le=1.0)], Color]


class _Gfx(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Shadow(_Gfx):
    color: Color = Field(default="#00000080", description="Shadow color")
    blur: float = Field(default=8.0, ge=0.0, description="Shadow blur radius in pixels")
    dx: float = Field(default=0.0, description="Horizontal shadow offset in pixels")
    dy: float = Field(default=4.0, description="Vertical shadow offset in pixels")


def _ascending(stops: tuple[GradientStop, ...]) -> None:
    offsets = [offset for offset, _color in stops]
    if offsets != sorted(offsets):
        raise ValueError(f"gradient stop offsets must not decrease, got {offsets}")


class LinearGradient(_Gfx):
    kind: Literal["linear"] = Field(default="linear", description="Linear gradient")
    start: tuple[float, float] = Field(description="Start point in the layer's own coordinates: pixels from the top-left corner of its box, or path data coordinates for a path")
    end: tuple[float, float] = Field(description="End point in the layer's own coordinates: pixels from the top-left corner of its box, or path data coordinates for a path")
    stops: tuple[GradientStop, ...] = Field(min_length=2, description="Color stops as (offset from 0 to 1, color)")

    @model_validator(mode="after")
    def _check_stops(self) -> LinearGradient:
        _ascending(self.stops)
        return self


class RadialGradient(_Gfx):
    kind: Literal["radial"] = Field(default="radial", description="Radial gradient")
    center: tuple[float, float] = Field(description="Center in the layer's own coordinates: pixels from the top-left corner of its box, or path data coordinates for a path")
    radius: float = Field(gt=0.0, description="Radius in pixels")
    stops: tuple[GradientStop, ...] = Field(min_length=2, description="Color stops as (offset from 0 to 1, color)")

    @model_validator(mode="after")
    def _check_stops(self) -> RadialGradient:
        _ascending(self.stops)
        return self


Gradient = Annotated[LinearGradient | RadialGradient, Field(discriminator="kind")]


class LayerBase(_Gfx):
    id: str = Field(pattern=LAYER_ID, description="Layer id, unique in the scene")
    name: str | None = Field(default=None, description="Display name")
    start: Time | None = Field(default=None, description="When the layer appears; the scene start when unset")
    end: Time | None = Field(default=None, description="When the layer disappears; the scene end when unset")
    x: AFloat = Field(default=0.0, description="Horizontal position in pixels, animatable")
    y: AFloat = Field(default=0.0, description="Vertical position in pixels, animatable")
    anchor: tuple[float, float] = Field(default=(0.5, 0.5), description="Anchor point as fractions of the layer size")
    scale: AFloat = Field(default=1.0, description="Scale factor, animatable")
    rotation: AFloat = Field(default=0.0, description="Rotation in degrees, animatable")
    opacity: AFloat = Field(default=1.0, description="Opacity from 0 to 1, animatable")
    blend: GfxBlend = Field(default="normal", description="Blend mode against the layers below")
    mask: str | None = Field(default=None, description="Id of the layer used as this layer's mask; the mask layer is not drawn on its own")
    mask_mode: MaskMode | None = Field(default=None, description="How the mask layer hides this layer: by its alpha (when unset) or luminance, or inverted")
    shadow: Shadow | None = Field(default=None, description="Drop shadow")
    blur: AFloat | None = Field(default=None, description="Gaussian blur radius in pixels, animatable")


class TextLayer(LayerBase):
    kind: Literal["text"] = Field(default="text", description="Text")
    text: RStr = Field(description="Text content")
    font: Font = Field(default=DEFAULT_FONT, description="Font: a $ref to a media.source font file (kind file), or the name of a family the renderer ships (IBM Plex Sans, IBM Plex Mono)")
    size: AFloat = Field(default=64.0, description="Font size in pixels, animatable")
    weight: int = Field(default=600, ge=100, le=900, description="Font weight from 100 to 900")
    color: AColor = Field(default="#ffffff", description="Text color, animatable")
    align: Literal["left", "center", "right"] = Field(default="center", description="Horizontal alignment")
    max_width: float | None = Field(default=None, gt=0, description="Wrap width in pixels; no wrapping when unset")
    line_height: float | None = Field(default=None, gt=0, description="Line height as a multiple of the font size")
    tracking: AFloat = Field(default=0.0, description="Letter spacing in pixels, animatable")


class RectLayer(LayerBase):
    kind: Literal["rect"] = Field(default="rect", description="Rectangle")
    width: AFloat = Field(description="Width in pixels, animatable")
    height: AFloat = Field(description="Height in pixels, animatable")
    radius: AFloat = Field(default=0.0, description="Corner radius in pixels, animatable")
    fill: AColor | Gradient | None = Field(default=None, description="Fill color, animatable, or a gradient")
    stroke: AColor | Gradient | None = Field(default=None, description="Stroke color, animatable, or a gradient")
    stroke_width: AFloat = Field(default=0.0, description="Stroke width in pixels, animatable")


class EllipseLayer(LayerBase):
    kind: Literal["ellipse"] = Field(default="ellipse", description="Ellipse")
    width: AFloat = Field(description="Width in pixels, animatable")
    height: AFloat = Field(description="Height in pixels, animatable")
    fill: AColor | Gradient | None = Field(default=None, description="Fill color, animatable, or a gradient")
    stroke: AColor | Gradient | None = Field(default=None, description="Stroke color, animatable, or a gradient")
    stroke_width: AFloat = Field(default=0.0, description="Stroke width in pixels, animatable")


class PathLayer(LayerBase):
    kind: Literal["path"] = Field(default="path", description="SVG path")
    d: str = Field(min_length=1, description="SVG path data")
    fill: AColor | Gradient | None = Field(default=None, description="Fill color, animatable, or a gradient")
    stroke: AColor | Gradient | None = Field(default=None, description="Stroke color, animatable, or a gradient")
    stroke_width: AFloat = Field(default=0.0, description="Stroke width in pixels, animatable")
    trim_start: AFloat = Field(default=0.0, description="Start of the drawn part of the path from 0 to 1, animatable")
    trim_end: AFloat = Field(default=1.0, description="End of the drawn part of the path from 0 to 1, animatable")


class ImageLayer(LayerBase):
    kind: Literal["image"] = Field(default="image", description="Image")
    media: RMedia = Field(description="Image media: a raster image, or an SVG file drawn as vectors")
    width: AFloat | None = Field(default=None, description="Width in pixels; the image width when unset")
    height: AFloat | None = Field(default=None, description="Height in pixels; the image height when unset")
    fit: Fit = Field(default="contain", description="How the image fits its box")


class VideoLayer(LayerBase):
    kind: Literal["video"] = Field(default="video", description="Video texture")
    media: RMedia = Field(description="Video media")
    src: Span | None = Field(default=None, description="Part of the video that plays; all of it when unset")
    speed: float = Field(default=1.0, gt=0, description="Playback speed")
    width: AFloat | None = Field(default=None, description="Width in pixels; the video width when unset")
    height: AFloat | None = Field(default=None, description="Height in pixels; the video height when unset")
    fit: Fit = Field(default="cover", description="How the video fits its box")


class GroupLayer(LayerBase):
    kind: Literal["group"] = Field(default="group", description="Group of layers transformed together")
    children: tuple[Layer, ...] = Field(default=(), description="Child layers, drawn in order")


Layer = Annotated[
    TextLayer | RectLayer | EllipseLayer | PathLayer | ImageLayer | VideoLayer | GroupLayer,
    Field(discriminator="kind"),
]

GroupLayer.model_rebuild()


class Pulse(_Gfx):
    kind: Literal["pulse"] = Field(default="pulse", description="Scale a property up and back")
    prop: str = Field(default="scale", description="Property to pulse")
    amount: float = Field(description="Peak factor such as 1.04")
    duration: Duration = Field(description="Length of the pulse")
    ease: Easing | None = Field(default=None, description="Easing of the pulse")


class SetProp(_Gfx):
    kind: Literal["set"] = Field(default="set", description="Set a property")
    prop: str = Field(min_length=1, description="Property name")
    value: JsonValue = Field(default=None, description="New value")


class Animate(_Gfx):
    kind: Literal["animate"] = Field(default="animate", description="Animate a property with keyframes from the trigger time")
    prop: str = Field(min_length=1, description="Property name")
    keys: tuple[Keyframe[JsonValue], ...] = Field(min_length=1, description="Keyframes relative to the trigger")


GfxAction = Annotated[Pulse | SetProp | Animate, Field(discriminator="kind")]


class GfxTrigger(_Gfx):
    at: Trigger = Field(description="Time, time set or event that fires the action")
    layer: str = Field(pattern=LAYER_ID, description="Id of the layer the action applies to")
    action: GfxAction = Field(description="Action to run")


class GfxScene(_Gfx):
    size: tuple[int, int] = Field(description="Frame size as (width, height) in pixels")
    fps: Fps | None = Field(default=None, description="Frame rate; the embedding clock's when unset")
    duration: Duration | None = Field(default=None, description="Length of the scene; when unset, the length of the embedding clock's bounds (its window, else its duration), which clips cut from; required on an open clock (no window and no duration)")
    background: Color | None = Field(default=None, description="Background color; transparent when unset")
    seed: int = Field(
        default=0, ge=0, le=SCENE_SEED_MAX,
        description="Seed of random curves and effects in the scene; derived from the embedding clock's root seed and the scene's embedding key unless the scene sets seed=")
    layers: tuple[Layer, ...] = Field(default=(), description="Layers, drawn in order")
    triggers: tuple[GfxTrigger, ...] = Field(default=(), description="Actions fired on the clock or by events")
    events: tuple[str, ...] = Field(default=(), description="Events the scene emits back into the score")


@op("gfx", name="render", out="media", pure=True, location="node", bounded=True,
    description="Render declarative 2D layers on the GPU, or on the CPU reference path for goldens",
    examples=[
        {
            "scene": {
                "size": [1080, 1920],
                "duration": "4s",
                "layers": [
                    {"id": "title", "kind": "text", "text": "EksenGOLD", "size": 96.0, "color": "#f5c542", "y": -600.0},
                ],
            },
        },
        {
            "scene": {
                "size": [1080, 1920],
                "layers": [{"id": "bg", "kind": "rect", "width": 1080.0, "height": 1920.0}],
            },
            "cpu": True,
        },
    ])
class RenderGfx(Args):
    scene: GfxScene = Field(description="The gfx scene to render")
    cpu: bool = Field(default=False, description="Use the CPU reference renderer (goldens and visual checks) instead of the GPU")


def gfx_layer_index(layers: Sequence[LayerBase]) -> dict[str, LayerBase]:
    found: dict[str, LayerBase] = {}
    for layer in layers:
        found.setdefault(layer.id, layer)
        if isinstance(layer, GroupLayer):
            for key, child in gfx_layer_index(layer.children).items():
                found.setdefault(key, child)
    return found


def gfx_set_values(scene: GfxScene) -> list[tuple[int, Any]]:
    index = gfx_layer_index(scene.layers)
    found: list[tuple[int, Any]] = []
    for position, trigger in enumerate(scene.triggers):
        action = trigger.action
        layer = index.get(trigger.layer)
        if not isinstance(action, SetProp) or layer is None or action.prop in GFX_FIXED_FIELDS:
            continue
        typed, problem = typed_value(layer, action.prop, action.value)
        if problem is None and typed is not None:
            found.append((position, typed))
    return found


__all__ = [
    "EVENT_NAME",
    "GFX_FIXED_FIELDS",
    "LAYER_ID",
    "Animate",
    "EllipseLayer",
    "GfxAction",
    "GfxBlend",
    "GfxScene",
    "GfxTrigger",
    "Gradient",
    "GradientStop",
    "GroupLayer",
    "ImageLayer",
    "Layer",
    "LayerBase",
    "LinearGradient",
    "MaskMode",
    "PathLayer",
    "Pulse",
    "RadialGradient",
    "RectLayer",
    "RenderGfx",
    "SetProp",
    "Shadow",
    "TextLayer",
    "VideoLayer",
    "gfx_layer_index",
    "gfx_set_values",
]
