from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal, cast

from pydantic import BaseModel, JsonValue, ValidationError

from eks.blender import HALF_BEAT
from eks.effects import AnimFloat, EaseLike, animated, ease, keys
from eks.media import FontLike, MediaLike, as_font, as_media
from eks.ref import JsonLike, Media, encode
from eks.tracing import TraceContext, TraceError
from eks.ui import Text
from eks.webscene import (
    DEFAULT_SCENE_SIZE,
    GFX_RENDER_OP,
    add_events,
    fps_json,
    is_animated_field,
    is_numeric_animated_field,
    scene_options,
    scene_seed,
    scene_seed_key,
    trigger_json,
    without_none,
)
from eks_harness.ir.catalog.animated import Driven
from eks_harness.ir.catalog.curves import CurveBase
from eks_harness.ir.catalog.durations import ensure_positive_duration
from eks_harness.ir.catalog.keys import Keys
from eks_harness.ir.ops.edit import Fit
from eks_harness.ir.ops.gfx import (
    LAYER_ID,
    Animate,
    EllipseLayer,
    GfxAction,
    GfxBlend,
    GfxTrigger,
    GradientStop,
    GroupLayer,
    ImageLayer,
    LinearGradient,
    MaskMode,
    PathLayer,
    Pulse,
    RadialGradient,
    RectLayer,
    SetProp,
    TextLayer,
    VideoLayer,
)
from eks_harness.ir.ops.scene import EventRef, dependency_path
from eks_harness.ir.registry import EntrySpec
from eks_harness.ir.time import Span, Time, TimeSet, ensure_time, optional_time
from eks_harness.ir.types import DEFAULT_FONT

ColorLike = str
type PaintLike = ColorLike | LinearGradient | RadialGradient
_LAYER_ID = re.compile(LAYER_ID)
_GFX_FINAL = "final:gfx.render"
_LAYER_MODELS: dict[str, type[BaseModel]] = {
    "text": TextLayer,
    "rect": RectLayer,
    "ellipse": EllipseLayer,
    "path": PathLayer,
    "image": ImageLayer,
    "video": VideoLayer,
    "group": GroupLayer,
}
_FIXED_FIELDS = frozenset({"id", "kind", "children"})


type GfxValue = JsonLike | CurveBase | Keys[Any] | Driven


@dataclass(frozen=True, slots=True)
class LayerAction:
    layer: str
    action: GfxAction


def _anim(value: Any) -> Any:
    if isinstance(value, CurveBase | Keys | Driven):
        return encode(animated(value))
    return encode(value)


def _ease(value: EaseLike) -> Any:
    return ease(value) if value is not None else None


def _paint(value: PaintLike | None) -> Any:
    if isinstance(value, LinearGradient | RadialGradient):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def linear_gradient(
    start: tuple[float, float], end: tuple[float, float], stops: list[GradientStop] | tuple[GradientStop, ...],
) -> LinearGradient:
    return LinearGradient(start=start, end=end, stops=tuple(stops))


def radial_gradient(
    center: tuple[float, float], radius: float, stops: list[GradientStop] | tuple[GradientStop, ...],
) -> RadialGradient:
    return RadialGradient(center=center, radius=radius, stops=tuple(stops))


class LayerHandle:
    def __init__(self, scene: GfxBuilder, layer_id: str) -> None:
        self._scene = scene
        self._id = layer_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def scene(self) -> GfxBuilder:
        return self._scene

    def __eks_encode__(self) -> str:
        return self._id

    def set(self, **props: GfxValue) -> LayerHandle:
        self._scene.set_fields(self._id, props, "set")
        return self

    def animate(self, prop: str, keys: Mapping[Time, JsonLike], *, ease: EaseLike = None) -> LayerHandle:
        self._scene.check_animated(self._id, prop, "animate", numeric=False)
        self._scene.set_fields(self._id, {prop: _keys(prop, keys, ease)}, "animate")
        return self

    def show(self, start: Time | None = None, end: Time | None = None) -> LayerHandle:
        if start is None and end is None:
            raise TypeError("show() needs start=, end= or both, for example show(t('beat:4'), t('beat:12'))")
        fields: dict[str, Any] = {}
        if start is not None:
            fields["start"] = ensure_time(start, "show start")
        if end is not None:
            fields["end"] = ensure_time(end, "show end")
        self._scene.set_fields(self._id, fields, "show")
        return self

    def pulse(
        self, *, prop: str = "scale", amount: float, duration: Time | None = None, ease: EaseLike = None
    ) -> LayerAction:
        self._scene.check_animated(self._id, prop, "pulse", numeric=True)
        pulse = Pulse(
            prop=prop, amount=amount,
            duration=ensure_positive_duration(duration, "pulse duration") if duration is not None else HALF_BEAT,
            ease=_ease(ease))
        return LayerAction(self._id, pulse)

    def assign(self, prop: str, value: GfxValue) -> LayerAction:
        return LayerAction(self._id, SetProp(prop=prop, value=self._scene.check_value(self._id, prop, value, "assign")))

    def tween(self, prop: str, keys: Mapping[Time, JsonLike], *, ease: EaseLike = None) -> LayerAction:
        self._scene.check_animated(self._id, prop, "tween", numeric=False)
        animation = self._scene.check_value(self._id, prop, _keys(prop, keys, ease), "tween")
        frames = cast("dict[str, Any]", animation)["keys"]
        return LayerAction(self._id, Animate.model_validate({"prop": prop, "keys": frames}))


def _reference(value: Any) -> Any:
    return value.id if isinstance(value, LayerHandle) else value


def _keys(prop: str, points: Mapping[Time, JsonLike], ease_like: EaseLike) -> Any:
    if not isinstance(points, Mapping) or not points:
        raise TypeError(
            f"{prop!r} keys take a non-empty mapping of times to values, for example {{t('0s'): 0, t('1s'): 1}}"
        )
    mapping = cast("Mapping[Any, Any]", points)
    for time in mapping:
        ensure_time(time, f"{prop!r} key")
    return keys(mapping, ease=ease_like)


class GfxBuilder:
    def __init__(
        self, ctx: TraceContext, *, size: tuple[int, int], fps: Fraction | None = None,
        duration: Time | None = None, background: ColorLike | None = None, seed: int | None = None,
        cpu: bool = False, name: str | None = None,
    ) -> None:
        if not isinstance(cpu, bool):
            raise TypeError(f"cpu= takes True or False, got {type(cpu).__name__} {cpu!r}")
        self._ctx = ctx
        self._cpu = cpu
        self._size = size
        self._fps = fps
        self._duration = optional_time(duration, "gfx scene duration")
        self._background = background
        self._seed = seed
        self._name = name
        self._layers: dict[str, dict[str, Any]] = {}
        self._children: dict[str, list[str]] = {}
        self._roots: list[str] = []
        self._stack: list[list[str]] = [self._roots]
        self._triggers: list[dict[str, Any]] = []
        self._events: list[str] = []
        self._render: Media | None = None

    def text(
        self, text: Text, *, id: str | None = None, x: AnimFloat = 0.0, y: AnimFloat = 0.0, size: AnimFloat = 64.0,
        font: FontLike = DEFAULT_FONT, weight: int = 600, color: ColorLike = "#ffffff",
        align: Literal["left", "center", "right"] = "center", max_width: float | None = None,
        line_height: float | None = None, tracking: AnimFloat | None = None,
        start: Time | None = None, end: Time | None = None,
    ) -> LayerHandle:
        return self._layer("text", id, start, end, {
            "text": encode(text), "x": _anim(x), "y": _anim(y), "size": _anim(size), "font": as_font(font, "sc.text font"),
            "weight": weight, "color": color, "align": align, "max_width": max_width, "line_height": line_height,
            "tracking": _anim(tracking)})

    def rect(
        self, width: AnimFloat, height: AnimFloat, *, id: str | None = None, x: AnimFloat = 0.0,
        y: AnimFloat = 0.0, radius: AnimFloat = 0.0, fill: PaintLike | None = None, stroke: PaintLike | None = None,
        stroke_width: AnimFloat = 0.0, start: Time | None = None, end: Time | None = None,
    ) -> LayerHandle:
        return self._layer("rect", id, start, end, {
            "width": _anim(width), "height": _anim(height), "x": _anim(x), "y": _anim(y),
            "radius": _anim(radius), "fill": _paint(fill), "stroke": _paint(stroke), "stroke_width": _anim(stroke_width)})

    def ellipse(
        self, width: AnimFloat, height: AnimFloat, *, id: str | None = None, x: AnimFloat = 0.0,
        y: AnimFloat = 0.0, fill: PaintLike | None = None, stroke: PaintLike | None = None,
        stroke_width: AnimFloat = 0.0, start: Time | None = None, end: Time | None = None,
    ) -> LayerHandle:
        return self._layer("ellipse", id, start, end, {
            "width": _anim(width), "height": _anim(height), "x": _anim(x), "y": _anim(y), "fill": _paint(fill),
            "stroke": _paint(stroke), "stroke_width": _anim(stroke_width)})

    def path(
        self, d: str, *, id: str | None = None, x: AnimFloat = 0.0, y: AnimFloat = 0.0,
        fill: PaintLike | None = None, stroke: PaintLike | None = None, stroke_width: AnimFloat = 0.0,
        trim_start: AnimFloat = 0.0, trim_end: AnimFloat = 1.0, start: Time | None = None, end: Time | None = None,
    ) -> LayerHandle:
        return self._layer("path", id, start, end, {
            "d": d, "x": _anim(x), "y": _anim(y), "fill": _paint(fill), "stroke": _paint(stroke),
            "stroke_width": _anim(stroke_width), "trim_start": _anim(trim_start), "trim_end": _anim(trim_end)})

    def image(
        self, media: MediaLike, *, id: str | None = None, x: AnimFloat = 0.0, y: AnimFloat = 0.0,
        width: AnimFloat | None = None, height: AnimFloat | None = None, fit: Fit = "contain",
        start: Time | None = None, end: Time | None = None,
    ) -> LayerHandle:
        return self._layer("image", id, start, end, {
            "media": encode(as_media(media)), "x": _anim(x), "y": _anim(y), "width": _anim(width),
            "height": _anim(height), "fit": fit})

    def video(
        self, media: MediaLike, *, id: str | None = None, x: AnimFloat = 0.0, y: AnimFloat = 0.0,
        width: AnimFloat | None = None, height: AnimFloat | None = None, fit: Fit = "cover",
        src: Span | None = None, speed: float = 1.0, start: Time | None = None, end: Time | None = None,
    ) -> LayerHandle:
        if src is not None and not isinstance(src, Span):
            raise TypeError(f"video src takes a Span such as t.span(t('2s'), t('6s')), got {src!r}")
        return self._layer("video", id, start, end, {
            "media": encode(as_media(media)), "x": _anim(x), "y": _anim(y), "width": _anim(width),
            "height": _anim(height), "fit": fit, "src": encode(src) if src is not None else None, "speed": speed})

    @contextmanager
    def group(
        self, *, id: str | None = None, x: AnimFloat = 0.0, y: AnimFloat = 0.0, blend: GfxBlend = "normal",
        start: Time | None = None, end: Time | None = None,
    ) -> Iterator[LayerHandle]:
        handle = self._layer("group", id, start, end, {"x": _anim(x), "y": _anim(y), "blend": blend})
        children: list[str] = []
        self._children[handle.id] = children
        self._stack.append(children)
        try:
            yield handle
        finally:
            self._stack.pop()

    def mask(self, layer: LayerHandle, mask: LayerHandle, *, mode: MaskMode | None = None) -> None:
        target = self._own(layer, "mask() layer")
        self.set_fields(target, {"mask": mask} if mode is None else {"mask": mask, "mask_mode": mode}, "mask")

    def on(self, trigger: Time | TimeSet | EventRef, *actions: LayerAction) -> None:
        self._check_open("on")
        at = trigger_json(self._ctx, trigger, "gfx scene on()")
        if not actions:
            raise TypeError("on() needs at least one action, for example sc.on(t('beat:4'), title.pulse(amount=1.1))")
        entries: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, LayerAction):
                raise TypeError(
                    f"on() takes actions built by a layer (layer.pulse, layer.assign, layer.tween), got "
                    f"{type(action).__name__} {action!r}"
                )
            self._layer_record(action.layer)
            entry = {"at": at, "layer": action.layer, "action": encode(action.action)}
            GfxTrigger.model_validate(entry)
            entries.append(entry)
        self._triggers.extend(entries)

    def emits(self, *events: str) -> None:
        self._check_open("emits")
        add_events(self._events, events, "gfx scene emits()")

    def finalize(self, *, id_key: str | None = None) -> Media:
        self._check_open("finalize")
        seed_key = scene_seed_key(id_key, self._name, _GFX_FINAL)
        if len(self._stack) != 1:
            raise TraceError("a gfx group is still open; close every `with sc.group():` block before rendering")
        scene = without_none({
            "size": list(self._size),
            "fps": fps_json(self._fps),
            "duration": encode(self._duration) if self._duration is not None else None,
            "background": self._background,
            "seed": scene_seed(self._ctx, self._seed, seed_key),
            "layers": [self._tree(layer_id) for layer_id in self._roots],
            "triggers": self._triggers,
            "events": self._events,
        })
        self._render = cast("Media", self._ctx.record(GFX_RENDER_OP, {"scene": scene, "cpu": self._cpu},
                                                      id_key=id_key if id_key is not None else _GFX_FINAL))
        return self._render

    def set_fields(self, layer_id: str, fields: Mapping[str, Any], what: str) -> None:
        self._check_open(what)
        record = self._layer_record(layer_id)
        model = _LAYER_MODELS[record["kind"]]
        updated = dict(record)
        for name, value in fields.items():
            self._check_field(record, model, name, what)
            if name == "mask" and value is not None:
                value = self._mask_source(layer_id, value, what)
            updated[name] = _anim(_reference(value))
        model.model_validate(updated)
        self._layers[layer_id] = without_none(updated)

    def check_prop(self, layer_id: str, prop: str, what: str) -> None:
        self._check_open(what)
        record = self._layer_record(layer_id)
        if not isinstance(prop, str) or not prop:
            raise TypeError(f"{what}() takes a property name such as 'opacity', got {prop!r}")
        self._check_field(record, _LAYER_MODELS[record["kind"]], prop, what)

    def check_value(self, layer_id: str, prop: str, value: Any, what: str) -> JsonValue:
        self.check_prop(layer_id, prop, what)
        record = self._layer_record(layer_id)
        model = _LAYER_MODELS[record["kind"]]
        if prop == "mask" and value is not None:
            value = self._mask_source(layer_id, value, what)
        encoded = _anim(_reference(value))
        try:
            validated = model.model_validate({**record, prop: encoded})
        except ValidationError as error:
            reason = error.errors()[0]["msg"]
            raise TypeError(
                f"{what}(): {value!r} is not a valid {prop!r} for {record['kind']} layer {layer_id!r} ({reason}); "
                f"pass a value of the type of {model.__name__}.{prop}"
            ) from error
        return validated.model_dump(mode="json", by_alias=True, exclude_none=True, include={prop}).get(prop)

    def check_animated(self, layer_id: str, prop: str, what: str, *, numeric: bool) -> None:
        self.check_prop(layer_id, prop, what)
        record = self._layer_record(layer_id)
        model = _LAYER_MODELS[record["kind"]]
        accepts = is_numeric_animated_field if numeric else is_animated_field
        if not accepts(model, prop):
            kind = "an animatable number" if numeric else "animatable"
            allowed = sorted(name for name in model.model_fields if accepts(model, name))
            raise TypeError(
                f"{what}(): {prop!r} of {record['kind']} layer {layer_id!r} is not {kind}; "
                f"{what} one of {', '.join(allowed)}"
            )

    def _layer(
        self, kind: str, explicit: str | None, start: Time | None, end: Time | None, fields: Mapping[str, Any],
    ) -> LayerHandle:
        self._check_open(kind)
        layer_id = self._allocate(kind, explicit)
        record = without_none({
            "kind": kind, "id": layer_id, **fields,
            "start": encode(optional_time(start, f"{kind} start")) if start is not None else None,
            "end": encode(optional_time(end, f"{kind} end")) if end is not None else None})
        _LAYER_MODELS[kind].model_validate(record)
        self._layers[layer_id] = record
        self._stack[-1].append(layer_id)
        return LayerHandle(self, layer_id)

    def _allocate(self, kind: str, explicit: str | None) -> str:
        if explicit is not None:
            if not isinstance(explicit, str) or _LAYER_ID.fullmatch(explicit) is None:
                raise ValueError(
                    f"gfx layer id {explicit!r} is not valid; use letters, digits, '_', '.' and '-' (at most 64), "
                    "starting with a letter or '_'"
                )
            if explicit in self._layers:
                raise TraceError(f"gfx layer id {explicit!r} is used twice; give each layer its own id=")
            return explicit
        number = 1
        while f"{kind}{number}" in self._layers:
            number += 1
        return f"{kind}{number}"

    def _tree(self, layer_id: str) -> dict[str, Any]:
        record = dict(self._layers[layer_id])
        if layer_id in self._children:
            record["children"] = [self._tree(child) for child in self._children[layer_id]]
        return record

    def _mask_source(self, layer_id: str, value: object, what: str) -> str:
        source = self._own(value, f"{what}() mask")
        if source == layer_id:
            raise TraceError(f"layer {layer_id!r} cannot mask itself; pass another layer as the mask")
        path = self._dependency_path(source, layer_id)
        if path is not None:
            raise TraceError(
                f"mask of layer {layer_id!r} would form a cycle: {' -> '.join([layer_id, *path])}; "
                "a layer cannot be masked by a layer that is masked by it or by a group that contains it"
            )
        return source

    def _dependency_path(self, start: str, goal: str) -> list[str] | None:
        return dependency_path(start, goal, self._following)

    def _following(self, layer_id: str) -> list[str]:
        following = list(self._children.get(layer_id, ()))
        mask = self._layers[layer_id].get("mask")
        if isinstance(mask, str):
            following.append(mask)
        return following

    def _own(self, handle: object, what: str) -> str:
        if isinstance(handle, LayerHandle):
            if handle.scene is not self:
                raise TraceError(f"{what} names layer {handle.id!r} of another gfx scene")
            layer_id = handle.id
        elif isinstance(handle, str):
            layer_id = handle
        else:
            raise TypeError(f"{what} takes a layer of this scene, got {type(handle).__name__} {handle!r}")
        self._layer_record(layer_id)
        return layer_id

    def _layer_record(self, layer_id: str) -> dict[str, Any]:
        record = self._layers.get(layer_id)
        if record is None:
            raise TraceError(f"{layer_id!r} is not a layer of this gfx scene")
        return record

    def _check_field(self, record: Mapping[str, Any], model: type[BaseModel], name: str, what: str) -> None:
        if name in _FIXED_FIELDS or name not in model.model_fields:
            settable = sorted(set(model.model_fields) - _FIXED_FIELDS)
            raise TypeError(
                f"{what}(): {record['kind']} layer {record['id']!r} has no property {name!r}; "
                f"settable: {', '.join(settable)}"
            )

    def _check_open(self, what: str) -> None:
        if self._render is not None:
            raise TraceError(f"this gfx scene is already rendered; call {what}() inside the scene function")


GfxScene = GfxBuilder


def open_gfx(
    ctx: TraceContext, entry: EntrySpec, *, size: tuple[int, int] | None = None, fps: Fraction | None = None,
) -> GfxBuilder:
    options = scene_options(entry, "gfx")
    return GfxBuilder(
        ctx, size=size or options.size or DEFAULT_SCENE_SIZE, fps=fps or options.fps, duration=options.duration,
        seed=options.explicit_seed, cpu=options.cpu, name=entry.name)


__all__ = [
    "GFX_RENDER_OP",
    "ColorLike",
    "GfxBuilder",
    "GfxScene",
    "LayerAction",
    "LayerHandle",
    "PaintLike",
    "linear_gradient",
    "open_gfx",
    "radial_gradient",
]
