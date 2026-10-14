from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from pydantic import BaseModel, JsonValue, ValidationError

from eks.effects import EaseLike, ease
from eks.media import MediaLike, as_media
from eks.ref import JsonLike, Media, RefUsageError, contains_refs, encode
from eks.tracing import TraceContext, TraceError, capture_function
from eks.ui import Text
from eks.webscene import (
    BLENDER_RENDER_OP,
    DEFAULT_SCENE_SIZE,
    add_events,
    ensure_event,
    fps_json,
    is_animated_field,
    is_numeric_animated_field,
    scene_options,
    scene_seed,
    scene_seed_key,
    trigger_json,
    without_none,
)
from eks_harness.ids import fold_ascii
from eks_harness.ir.catalog.durations import ensure_positive_duration
from eks_harness.ir.ops.blender import (
    DEFAULT_ENGINE,
    DEFAULT_SAMPLES,
    ENGINE_ALIASES,
    NODE_ID,
    WORKER_PLATFORM,
    Animation,
    BlenderAction,
    BlenderTrigger,
    BpyHook,
    CameraNode,
    Emit,
    EmptyNode,
    Engine,
    LightNode,
    LoadNode,
    MeshNode,
    Pulse,
    SetProp,
    TextNode,
    TextureBinding,
    World,
    engine_device_problem,
    lookup_engine,
)
from eks_harness.ir.ops.scene import EventRef, dependency_path
from eks_harness.ir.registry import EntrySpec
from eks_harness.ir.time import Time, TimeSet, ensure_time, optional_time

Vec3Like = tuple[float, float, float]
DEFAULT_DEVICE: Literal["CPU", "GPU"] = "GPU"
BPY_GLOBALS = ("math", "mathutils")
HALF_BEAT = Time(beats=Fraction(1, 2))
_NODE_ID = re.compile(NODE_ID)
_PLATFORM = re.compile(WORKER_PLATFORM)
_BLENDER_FINAL = "final:blender.render"
_SLUG_LENGTH = 56
_NODE_MODELS: dict[str, type[BaseModel]] = {
    "load": LoadNode,
    "camera": CameraNode,
    "light": LightNode,
    "mesh": MeshNode,
    "text": TextNode,
    "empty": EmptyNode,
}
_FIXED_FIELDS = frozenset({"id", "kind"})
_REFERENCE_FIELDS = frozenset({"parent", "look_at", "focus"})


@dataclass(frozen=True, slots=True)
class ObjectAction:
    node: str | None
    action: BlenderAction


def resolve_engine(name: str | None) -> Engine:
    chosen = DEFAULT_ENGINE if name is None else name
    if not isinstance(chosen, str):
        raise TraceError(f"the Blender engine is a name such as 'cycles' or 'eevee', got {chosen!r}")
    resolved = lookup_engine(chosen)
    if resolved is None:
        known = sorted(ENGINE_ALIASES) + sorted(DEFAULT_SAMPLES)
        raise TraceError(f"unknown Blender engine {chosen!r}; use one of {', '.join(repr(k) for k in known)}")
    return resolved


def node_slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", fold_ascii(text).lower()).strip("_")[:_SLUG_LENGTH].rstrip("_")
    if not slug:
        return fallback
    return f"_{slug}" if slug[0].isdigit() else slug


def _vec3(value: Vec3Like | None, what: str) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, tuple | list) or len(value) != 3:
        raise TypeError(f"{what} takes three numbers (x, y, z), got {value!r}")
    numbers: list[float] = []
    for item in cast("tuple[Any, ...]", tuple(value)):
        if isinstance(item, bool) or not isinstance(item, int | float) or not math.isfinite(item):
            raise TypeError(f"{what} takes three finite numbers (x, y, z), got {value!r}")
        numbers.append(item)
    return numbers


class MaterialHandle:
    def __init__(self, obj: ObjectHandle, name: str) -> None:
        self._obj = obj
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def texture(self, slot: str, media: MediaLike) -> MaterialHandle:
        self._obj.scene.add_texture(self._obj.id, self._name, slot, media)
        return self


class ObjectHandle:
    def __init__(self, scene: BlenderBuilder, node_id: str) -> None:
        self._scene = scene
        self._id = node_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def scene(self) -> BlenderBuilder:
        return self._scene

    def __eks_encode__(self) -> str:
        return self._id

    def material(self, name: str) -> MaterialHandle:
        if not isinstance(name, str) or not name:
            raise TypeError(f"material() takes the material name of node {self._id!r}, got {name!r}")
        return MaterialHandle(self, name)

    def set(self, **props: JsonLike) -> ObjectHandle:
        self._scene.set_fields(self._id, props)
        return self

    def animate(self, prop: str, keys: Mapping[Time, JsonLike], *, ease: EaseLike = None) -> ObjectHandle:
        self._scene.add_animation(self._id, prop, keys, ease)
        return self

    def pulse(
        self, *, scale: float | None = None, prop: str = "scale", amount: float | None = None,
        duration: Time | None = None, ease: EaseLike = None,
    ) -> ObjectAction:
        if scale is not None:
            if amount is not None:
                raise TypeError("pulse() takes either scale= or amount=, not both")
            if prop != "scale":
                raise TypeError(f"pulse(scale=) pulses the scale; use pulse(prop={prop!r}, amount=...) instead")
            amount = scale
        if amount is None:
            raise TypeError("pulse() needs scale= or amount=, for example pulse(scale=1.04)")
        self._scene.check_pulse(self._id, prop)
        pulse = Pulse(
            prop=prop, amount=amount,
            duration=ensure_positive_duration(duration, "pulse duration") if duration is not None else HALF_BEAT,
            ease=_ease(ease))
        return ObjectAction(self._id, pulse)

    def assign(self, prop: str, value: JsonLike) -> ObjectAction:
        return ObjectAction(self._id, SetProp(prop=prop, value=self._scene.check_value(self._id, prop, value)))

    def emit(self, event: str, data: JsonLike = None) -> ObjectAction:
        return ObjectAction(self._id, Emit(event=ensure_event(event, "emit()"), data=encode(data)))


class CameraHandle(ObjectHandle):
    def look_at(self, target: ObjectHandle) -> CameraHandle:
        self._scene.set_fields(self._id, {"look_at": self._scene.check_target(target, self._id, "look_at")})
        return self

    def focus(self, target: ObjectHandle, *, f_stop: float | None = None) -> CameraHandle:
        fields: dict[str, Any] = {"focus": self._scene.check_target(target, self._id, "focus")}
        if f_stop is not None:
            fields["f_stop"] = f_stop
        self._scene.set_fields(self._id, fields)
        return self


def _ease(value: EaseLike) -> Any:
    return ease(value) if value is not None else None


def _reference(value: Any) -> Any:
    return value.id if isinstance(value, ObjectHandle) else value


class BlenderBuilder:
    def __init__(
        self, ctx: TraceContext, *, engine: str | None = None, samples: int | None = None,
        device: Literal["CPU", "GPU"] | None = None, seed: int | None = None,
        size: tuple[int, int] = DEFAULT_SCENE_SIZE, fps: Fraction | None = None, duration: Time | None = None,
        transparent: bool = True, name: str | None = None, platform: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._engine: Engine = resolve_engine(engine)
        self._samples = samples if samples is not None else DEFAULT_SAMPLES[self._engine]
        self._device: Literal["CPU", "GPU"] = device if device is not None else DEFAULT_DEVICE
        problem = engine_device_problem(self._engine, self._device)
        if problem is not None:
            raise TraceError(f"Blender scene {name!r}: {problem}" if name else problem)
        if platform is not None and (not isinstance(platform, str) or _PLATFORM.fullmatch(platform) is None):
            raise TraceError(
                f"Blender platform pin {platform!r} is not a worker platform; write it as os-arch the way nodes "
                "report it, such as 'linux-x86_64' or 'darwin-arm64'")
        self._platform = platform
        self._seed = seed
        self._name = name
        self._size = size
        self._fps = fps
        self._duration = optional_time(duration, "Blender scene duration")
        self._transparent = transparent
        self._nodes: dict[str, dict[str, Any]] = {}
        self._textures: list[dict[str, Any]] = []
        self._animations: list[dict[str, Any]] = []
        self._triggers: list[dict[str, Any]] = []
        self._hooks: list[dict[str, Any]] = []
        self._camera: str | None = None
        self._world: dict[str, Any] | None = None
        self._events: list[str] = []
        self._render: Media | None = None

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def device(self) -> Literal["CPU", "GPU"]:
        return self._device

    @property
    def platform(self) -> str | None:
        return self._platform

    def load(
        self, path: str, *, obj: str | None = None, collection: str | None = None, link: bool = False,
        id: str | None = None,
    ) -> ObjectHandle:
        if not isinstance(path, str) or not path:
            raise TypeError(f"load() takes the path of a .blend file, got {path!r}")
        base = node_slug(obj if obj is not None else PurePosixPath(path.split(":", 1)[-1]).stem, "load")
        node_id = self._add_node("load", base, id, {
            "path": encode(as_media(path, kind="file")), "obj": obj, "collection": collection, "link": link})
        return ObjectHandle(self, node_id)

    def camera(
        self, *, lens: float = 50.0, id: str | None = None, location: Vec3Like | None = None,
        rotation: Vec3Like | None = None, active: bool = True,
    ) -> CameraHandle:
        if active and self._camera is not None:
            raise TraceError(
                f"camera {self._camera!r} is already the active camera; pass active=False for further cameras"
            )
        node_id = self._add_node("camera", "camera", id, {
            "lens": lens, "location": _vec3(location, "camera location"),
            "rotation": _vec3(rotation, "camera rotation")})
        if active:
            self._camera = node_id
        return CameraHandle(self, node_id)

    def light(
        self, kind: Literal["point", "sun", "spot", "area"] = "area", *, energy: float = 1000.0,
        color: str = "#ffffff", size: float = 1.0, id: str | None = None, location: Vec3Like | None = None,
        rotation: Vec3Like | None = None,
    ) -> ObjectHandle:
        node_id = self._add_node("light", "light", id, {
            "light": kind, "energy": energy, "color": color, "size": size,
            "location": _vec3(location, "light location"), "rotation": _vec3(rotation, "light rotation")})
        return ObjectHandle(self, node_id)

    def mesh(
        self, primitive: Literal["cube", "plane", "sphere", "cylinder", "cone", "torus"], *, size: float = 1.0,
        material: str | None = None, id: str | None = None, location: Vec3Like | None = None,
    ) -> ObjectHandle:
        base = node_slug(primitive, "mesh") if isinstance(primitive, str) else "mesh"
        node_id = self._add_node("mesh", base, id, {
            "primitive": primitive, "size": size, "material": material,
            "location": _vec3(location, "mesh location")})
        return ObjectHandle(self, node_id)

    def text(
        self, body: Text, *, font: MediaLike | None = None, size: float = 1.0, extrude: float = 0.0,
        id: str | None = None, location: Vec3Like | None = None,
    ) -> ObjectHandle:
        node_id = self._add_node("text", "text", id, {
            "body": encode(body), "font": None if font is None else encode(as_media(font, kind="file")),
            "size": size, "extrude": extrude,
            "location": _vec3(location, "text location")})
        return ObjectHandle(self, node_id)

    def empty(self, *, id: str | None = None, location: Vec3Like | None = None) -> ObjectHandle:
        node_id = self._add_node("empty", "empty", id, {"location": _vec3(location, "empty location")})
        return ObjectHandle(self, node_id)

    def world(self, *, hdri: MediaLike | None = None, color: str | None = None, strength: float = 1.0) -> None:
        self._check_open("world")
        if self._world is not None:
            raise TraceError("world() is already set for this Blender scene; call it once")
        world = without_none({"hdri": None if hdri is None else encode(as_media(hdri, kind="image")),
                              "color": color, "strength": strength})
        World.model_validate(world)
        self._world = world

    def on(self, trigger: Time | TimeSet | EventRef, *actions: ObjectAction) -> None:
        self._check_open("on")
        at = trigger_json(self._ctx, trigger, "Blender scene on()")
        if not actions:
            raise TypeError("on() needs at least one action, for example sc.on(t('bar:1'), obj.pulse(scale=1.04))")
        entries: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, ObjectAction):
                raise TypeError(
                    f"on() takes actions built by an object (obj.pulse, obj.assign, obj.emit), got "
                    f"{type(action).__name__} {action!r}"
                )
            if action.node is not None and action.node not in self._nodes:
                raise TraceError(f"on() got an action of node {action.node!r}, which is not part of this scene")
            entry = without_none({"at": at, "node": action.node, "action": encode(action.action)})
            BlenderTrigger.model_validate(entry)
            entries.append(entry)
        self._triggers.extend(entries)

    def bpy[F: Callable[..., Any]](self, fn: F) -> F:
        return self._hook(fn, "build")

    def bpy_frame[F: Callable[..., Any]](self, fn: F) -> F:
        return self._hook(fn, "frame")

    def emits(self, *events: str) -> None:
        self._check_open("emits")
        add_events(self._events, events, "Blender scene emits()")

    def finalize(self, *, id_key: str | None = None) -> Media:
        self._check_open("finalize")
        seed_key = scene_seed_key(id_key, self._name, _BLENDER_FINAL)
        events = list(self._events)
        emitted = tuple(trigger["action"]["event"] for trigger in self._triggers
                        if trigger["action"]["kind"] == "emit")
        add_events(events, emitted, "Blender scene emit()")
        render = without_none({
            "engine": self._engine, "samples": self._samples, "device": self._device,
            "seed": scene_seed(self._ctx, self._seed, seed_key),
            "size": list(self._size), "fps": fps_json(self._fps), "transparent": self._transparent,
            "platform": self._platform})
        scene = without_none({
            "nodes": list(self._nodes.values()),
            "textures": self._textures,
            "animations": self._animations,
            "triggers": self._triggers,
            "hooks": self._hooks,
            "camera": self._camera,
            "world": self._world,
            "render": render,
            "duration": encode(self._duration) if self._duration is not None else None,
            "events": events,
        })
        self._render = cast("Media", self._ctx.record(BLENDER_RENDER_OP, {"scene": scene},
                                                      id_key=id_key if id_key is not None else _BLENDER_FINAL))
        return self._render

    def set_fields(self, node_id: str, fields: Mapping[str, Any]) -> None:
        self._check_open("set")
        node = self._node(node_id)
        model = _NODE_MODELS[node["kind"]]
        updated = dict(node)
        for name, value in fields.items():
            self._check_field(node, model, name, "set")
            if name in _REFERENCE_FIELDS and value is not None:
                value = self._reference_field(node_id, name, value)
            updated[name] = encode(list(value) if isinstance(value, tuple) else value)
        model.model_validate(updated)
        self._nodes[node_id] = without_none(updated)

    def add_texture(self, node_id: str, material: str, slot: str, media: MediaLike) -> None:
        self._check_open("texture")
        self._node(node_id)
        if not isinstance(slot, str) or not slot:
            raise TypeError(f"texture() takes the slot name of material {material!r}, got {slot!r}")
        binding = {"node": node_id, "material": material, "slot": slot, "media": encode(as_media(media))}
        TextureBinding.model_validate(binding)
        self._textures.append(binding)

    def add_animation(self, node_id: str, prop: str, keys: Mapping[Time, JsonLike], ease_like: EaseLike) -> None:
        self._check_open("animate")
        node = self._node(node_id)
        model = _NODE_MODELS[node["kind"]]
        if not isinstance(prop, str) or not prop:
            raise TypeError(f"animate() takes a property name such as 'location', got {prop!r}")
        if prop in model.model_fields and (prop in _FIXED_FIELDS or not is_animated_field(model, prop)):
            animatable = sorted(name for name in model.model_fields if is_animated_field(model, name))
            raise TypeError(
                f"animate(): {prop!r} of {node['kind']} node {node_id!r} is not animatable; "
                f"animate one of {', '.join(animatable)}"
            )
        if not isinstance(keys, Mapping) or not keys:
            raise TypeError(
                "animate() takes a non-empty mapping of times to values, "
                "for example {t('bar:0'): (0, -4, 1), t('bar:4'): (0, -2, 1)}"
            )
        points = cast("Mapping[Any, Any]", keys)
        for value in points.values():
            if contains_refs(value):
                raise RefUsageError(
                    f"animate({prop!r}) value {value!r} holds a Ref; keyframes hold values known when the plan "
                    "is built"
                )
        frames = [{"t": encode(ensure_time(time, f"animate({prop!r}) key")),
                   "v": self._keyframe_value(node, model, prop, value)}
                  for time, value in points.items()]
        animation = without_none({"node": node_id, "prop": prop, "keys": frames, "ease": encode(_ease(ease_like))})
        Animation.model_validate(animation)
        for index, existing in enumerate(self._animations):
            if existing["node"] == node_id and existing["prop"] == prop:
                self._animations[index] = animation
                return
        self._animations.append(animation)

    def _keyframe_value(self, node: Mapping[str, Any], model: type[BaseModel], prop: str, value: Any) -> JsonValue:
        encoded = encode(list(value) if isinstance(value, tuple) else value)
        if prop not in model.model_fields:
            return encoded
        reason: str | None = None
        normalized: JsonValue = None
        try:
            validated = model.model_validate({**node, prop: encoded})
        except ValidationError as error:
            reason = error.errors()[0]["msg"]
        else:
            normalized = validated.model_dump(mode="json", by_alias=True, exclude_none=True, include={prop}).get(prop)
            if isinstance(normalized, dict):
                reason = "a keyframe holds a plain value, not keys or a driver"
        if reason is not None:
            raise TypeError(
                f"animate(): keyframe value {value!r} is not a valid {prop!r} for {node['kind']} node "
                f"{node['id']!r} ({reason}); pass a value of the type of {model.__name__}.{prop}"
            )
        return normalized

    def check_prop(self, node_id: str, prop: str, what: str) -> None:
        self._check_open(what)
        self._node(node_id)
        if not isinstance(prop, str) or not prop:
            raise TypeError(f"{what}() takes a property name such as 'scale', got {prop!r}")

    def check_pulse(self, node_id: str, prop: str) -> None:
        self.check_prop(node_id, prop, "pulse")
        node = self._node(node_id)
        model = _NODE_MODELS[node["kind"]]
        if prop in model.model_fields and not is_numeric_animated_field(model, prop):
            numeric = sorted(name for name in model.model_fields if is_numeric_animated_field(model, name))
            raise TypeError(
                f"pulse(): {prop!r} of {node['kind']} node {node_id!r} is not an animatable number; "
                f"pulse one of {', '.join(numeric)}"
            )

    def check_value(self, node_id: str, prop: str, value: Any) -> JsonValue:
        self.check_prop(node_id, prop, "assign")
        node = self._node(node_id)
        model = _NODE_MODELS[node["kind"]]
        if prop in _FIXED_FIELDS:
            raise TypeError(f"assign(): the {prop} of node {node_id!r} is fixed; build another node instead")
        if prop not in model.model_fields:
            return encode(_reference(value))
        if prop in _REFERENCE_FIELDS and value is not None:
            value = self._reference_field(node_id, prop, value)
        encoded = encode(value)
        try:
            validated = model.model_validate({**node, prop: encoded})
        except ValidationError as error:
            reason = error.errors()[0]["msg"]
            raise TypeError(
                f"assign(): {value!r} is not a valid {prop!r} for {node['kind']} node {node_id!r} ({reason}); "
                f"pass a value of the type of {model.__name__}.{prop}"
            ) from error
        return validated.model_dump(mode="json", by_alias=True, exclude_none=True, include={prop}).get(prop)

    def check_target(self, target: object, node_id: str, what: str) -> str:
        if isinstance(target, ObjectHandle):
            if target.scene is not self:
                raise TraceError(f"{what} of node {node_id!r} names an object of another scene ({target.id!r})")
            target_id = target.id
        elif isinstance(target, str):
            target_id = target
        else:
            raise TypeError(f"{what} takes an object of this scene, got {type(target).__name__} {target!r}")
        if target_id not in self._nodes:
            raise TraceError(f"{what} of node {node_id!r} names {target_id!r}, which is not a node of this scene")
        if target_id == node_id:
            raise TraceError(f"{what} of node {node_id!r} cannot name the node itself")
        return target_id

    def _reference_field(self, node_id: str, name: str, value: object) -> str:
        target_id = self.check_target(value, node_id, name)
        if name == "parent":
            path = dependency_path(target_id, node_id, self._parents)
            if path is not None:
                raise TraceError(
                    f"parent of node {node_id!r} would form a cycle: {' -> '.join([node_id, *path])}; "
                    "parent each node under one that does not descend from it"
                )
        return target_id

    def _parents(self, node_id: str) -> list[str]:
        parent = self._nodes[node_id].get("parent")
        return [parent] if isinstance(parent, str) else []

    def _hook[F: Callable[..., Any]](self, fn: F, phase: Literal["build", "frame"]) -> F:
        self._check_open("bpy")
        if not callable(fn):
            raise TypeError(f"@sc.bpy decorates a function defined with def, got {fn!r}")
        hook = {"fn": capture_function(fn, allowed_globals=BPY_GLOBALS), "phase": phase}
        self._hooks.append(BpyHook.model_validate(hook).model_dump(mode="json", exclude_none=True))
        return fn

    def _add_node(self, kind: str, base: str, explicit: str | None, fields: Mapping[str, Any]) -> str:
        self._check_open(kind)
        node_id = self._allocate(base, explicit)
        node = without_none({"kind": kind, "id": node_id, **fields})
        _NODE_MODELS[kind].model_validate(node)
        self._nodes[node_id] = node
        return node_id

    def _allocate(self, base: str, explicit: str | None) -> str:
        if explicit is not None:
            if not isinstance(explicit, str) or _NODE_ID.fullmatch(explicit) is None:
                raise ValueError(
                    f"Blender node id {explicit!r} is not valid; use letters, digits, '_', '.' and '-' "
                    "(at most 64), starting with a letter or '_'"
                )
            if explicit in self._nodes:
                raise TraceError(f"Blender node id {explicit!r} is used twice; give each node its own id=")
            return explicit
        candidate, number = base, 1
        while candidate in self._nodes:
            number += 1
            candidate = f"{base}_{number}"
        return candidate

    def _node(self, node_id: str) -> dict[str, Any]:
        node = self._nodes.get(node_id)
        if node is None:
            raise TraceError(f"{node_id!r} is not a node of this Blender scene")
        return node

    def _check_field(self, node: Mapping[str, Any], model: type[BaseModel], name: str, what: str) -> None:
        if name in _FIXED_FIELDS or name not in model.model_fields:
            settable = sorted(set(model.model_fields) - _FIXED_FIELDS)
            raise TypeError(
                f"{what}(): {node['kind']} node {node['id']!r} has no property {name!r}; "
                f"settable: {', '.join(settable)}"
            )

    def _check_open(self, what: str) -> None:
        if self._render is not None:
            raise TraceError(f"this Blender scene is already rendered; call {what}() inside the scene function")


BlenderScene = BlenderBuilder


def open_blender(
    ctx: TraceContext, entry: EntrySpec, *, size: tuple[int, int] | None = None, fps: Fraction | None = None,
) -> BlenderBuilder:
    options = scene_options(entry, "blender")
    return BlenderBuilder(
        ctx, engine=options.engine, samples=options.samples, device=options.device, seed=options.explicit_seed,
        size=size or options.size or DEFAULT_SCENE_SIZE, fps=fps or options.fps, duration=options.duration,
        transparent=options.transparent, name=entry.name, platform=options.platform)


__all__ = [
    "BLENDER_RENDER_OP",
    "BPY_GLOBALS",
    "DEFAULT_DEVICE",
    "DEFAULT_ENGINE",
    "HALF_BEAT",
    "BlenderBuilder",
    "BlenderScene",
    "CameraHandle",
    "MaterialHandle",
    "ObjectAction",
    "ObjectHandle",
    "Vec3Like",
    "node_slug",
    "open_blender",
    "resolve_engine",
]
