from __future__ import annotations

import inspect
import types
import typing
import weakref
from collections import Counter
from collections.abc import Callable, Mapping
from fractions import Fraction
from typing import Annotated, Any

from eks.blender import BlenderBuilder, open_blender
from eks.gfx import GfxBuilder, open_gfx
from eks.media import as_media, scene_analyses
from eks.planning import PlanBuildError, coerce_params
from eks.ref import Encodable, JsonLike, Media, Ref, encode, is_item, path_of
from eks.tracing import TraceContext, TraceError, active
from eks.webscene import (
    SCENE_RENDER_OPS,
    WebSceneBuilder,
    check_event,
    ensure_event,
    open_web_scene,
)
from eks_harness.ir.entries import SceneKind, SceneOptions
from eks_harness.ir.ops.scene import EventRef
from eks_harness.ir.registry import EntrySpec, RegistryError
from eks_harness.ir.types import MediaInfo

SceneBuilder = BlenderBuilder | WebSceneBuilder | GfxBuilder
SCENE_FINALIZE_ORDER = 100
RENDER_OPS = SCENE_RENDER_OPS
_EMBEDDED: weakref.WeakKeyDictionary[TraceContext, Counter[str]] = weakref.WeakKeyDictionary()


def build_scene(
    ctx: TraceContext, entry: EntrySpec, *, size: tuple[int, int] | None = None, fps: Fraction | None = None,
) -> SceneBuilder:
    options = entry.options
    if entry.kind != "scene" or not isinstance(options, SceneOptions):
        raise TraceError(
            f"{entry.kind} {entry.name!r} is not a scene; decorate it with @eks.scene(kind='blender'|'web'|'gfx')"
        )
    match options.kind:
        case "blender":
            return open_blender(ctx, entry, size=size, fps=fps)
        case "web":
            return open_web_scene(ctx, entry, size=size, fps=fps)
        case "gfx":
            return open_gfx(ctx, entry, size=size, fps=fps)


def open_scene(ctx: TraceContext, entry: EntrySpec) -> SceneBuilder:
    builder = build_scene(ctx, entry)

    def finalize(_ctx: TraceContext) -> None:
        builder.finalize()

    ctx.on_finalize(finalize, order=SCENE_FINALIZE_ORDER)
    return builder


def resolve_scene(ctx: TraceContext, scene: Callable[..., Any] | EntrySpec | str) -> EntrySpec:
    if isinstance(scene, EntrySpec):
        entry = scene
    elif isinstance(scene, str):
        try:
            entry = ctx.registry.entry("scene", scene)
        except RegistryError as error:
            raise TraceError(f"{error}; load the file that defines the scene or pass the scene function") from error
    elif callable(scene):
        try:
            entry = ctx.registry.entry_for(scene)
        except RegistryError as error:
            raise TraceError(str(error)) from error
    else:
        raise TypeError(
            f"a scene is a function decorated with @eks.scene, its EntrySpec or a registered scene name, "
            f"got {type(scene).__name__} {scene!r}"
        )
    if entry.kind != "scene" or not isinstance(entry.options, SceneOptions):
        raise TraceError(f"{entry.kind} {entry.name!r} is not a scene; only @eks.scene entries can be embedded")
    return entry


def embed_scene(
    ctx: TraceContext, scene: Callable[..., Any] | EntrySpec | str, inputs: Mapping[str, Any], *,
    kind: SceneKind | None = None,
) -> Media:
    entry = resolve_scene(ctx, scene)
    options = entry.options
    if not isinstance(options, SceneOptions):
        raise TraceError(f"{entry.kind} {entry.name!r} is not a scene")
    if kind is not None and options.kind != kind:
        raise TraceError(
            f"scene {entry.name!r} is a {options.kind} scene, but a {kind} scene is expected here; "
            f"embed it with the builder for {options.kind} scenes"
        )
    values = coerce_params(entry, inputs, text=False)
    arguments = dict(values)
    for name in _media_parameters(entry, values):
        arguments[name] = as_media(values[name], origin=None if name in inputs else entry.file or None)
    builder = build_scene(ctx, entry, size=_embedding_size(ctx), fps=ctx.clock.fps)
    counts = _EMBEDDED.setdefault(ctx, Counter())
    number = counts[entry.name]
    with scene_analyses(ctx, entry.name, number):
        entry.fn(builder, **arguments)
    counts[entry.name] = number + 1
    return builder.finalize(id_key=f"scene:{entry.name}#{number}")


def scene_event(media: Media, name: str, **where: JsonLike) -> EventRef:
    if not isinstance(media, Ref) or is_item(media):
        raise TypeError(
            f"scene_event() takes the render of a scene (the value of sc.scene(...)), got {type(media).__name__}"
        )
    if path_of(media):
        raise TypeError("scene_event() takes the render ref itself, not a path into it")
    event = EventRef.model_validate({
        "source": encode(media), "name": ensure_event(name, "scene_event()"), "where": encode(where)})
    ctx = active()
    if ctx is not None:
        check_event(ctx, event, "scene_event()", sources=RENDER_OPS)
    return event


def _embedding_size(ctx: TraceContext) -> tuple[int, int] | None:
    entry = ctx.entry
    if entry is None:
        return None
    size = getattr(entry.options, "size", None)
    if isinstance(size, tuple):
        width, height = typing.cast("tuple[int, int]", size)
        return (width, height)
    return None


def _media_parameters(entry: EntrySpec, values: Mapping[str, Any]) -> list[str]:
    names = [name for name, value in values.items()
             if not isinstance(value, Ref) and isinstance(value, str | Encodable)]
    if not names:
        return []
    fn = inspect.unwrap(entry.fn)
    annotations = inspect.get_annotations(fn)
    namespace: dict[str, Any] = getattr(fn, "__globals__", {})
    found: list[str] = []
    for name in names:
        if name not in annotations:
            continue
        holder = types.SimpleNamespace(__annotations__={name: annotations[name]})
        try:
            hint = typing.get_type_hints(holder, globalns=namespace, include_extras=True)[name]
        except (NameError, TypeError, AttributeError) as error:
            raise PlanBuildError(
                f"cannot read the type of parameter {name!r} of scene {entry.name!r}: {error}"
            ) from error
        if _is_media(hint):
            found.append(name)
    return found


def _is_media(annotation: Any) -> bool:
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _is_media(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(_is_media(member) for member in typing.get_args(annotation))
    return origin is Ref and typing.get_args(annotation) == (MediaInfo,)


__all__ = [
    "RENDER_OPS",
    "SCENE_FINALIZE_ORDER",
    "SceneBuilder",
    "build_scene",
    "embed_scene",
    "open_scene",
    "resolve_scene",
    "scene_event",
]
