from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, cast

from pydantic import JsonValue

from eks.media import MediaLike, as_media
from eks.ref import JsonLike, Media, encode
from eks.tracing import TraceContext, TraceError
from eks_harness.ir.entries import SceneKind, SceneOptions
from eks_harness.ir.hashing import derived_seed
from eks_harness.ir.ops.scene import (
    BLENDER_RENDER_OP,
    EVENT_NAME,
    EVENT_SOURCE_OPS,
    GFX_RENDER_OP,
    SCENE_RENDER_OP,
    SCENE_RENDER_OPS,
    SCENE_SEED_MAX,
    TAKE_OP,
    EventRef,
    declared_scene_events,
    event_source_problem,
    field_members,
    is_animated_field,
    is_numeric_animated_field,
)
from eks_harness.ir.registry import EntrySpec
from eks_harness.ir.time import Time, TimeSet, format_fps, optional_time

DEFAULT_SCENE_SIZE: tuple[int, int] = (1080, 1920)
_WEB_FINAL = "final:scene.render"
_EVENT = re.compile(EVENT_NAME)


def ensure_trigger(value: object, what: str) -> Time | TimeSet | EventRef:
    if isinstance(value, Time | TimeSet | EventRef):
        return value
    if isinstance(value, str):
        raise TypeError(
            f"{what} expects a Time, a TimeSet or a scene event, got the string {value!r}; "
            f"pass t({value!r}) instead of a string"
        )
    raise TypeError(
        f"{what} expects a Time such as t('bar:2'), a TimeSet such as t.beats('downbeat')[0:8] or a scene "
        f"event from eks.scenes.scene_event(...), got {type(value).__name__} {value!r}"
    )


def declared_events(ctx: TraceContext, source: str) -> list[str]:
    return declared_scene_events(ctx.node(source).args)


def check_event(
    ctx: TraceContext, event: EventRef, what: str, *, sources: frozenset[str] = EVENT_SOURCE_OPS,
) -> None:
    source = event.source.ref
    try:
        node = ctx.node(source)
    except TraceError as error:
        raise TraceError(
            f"{what}: event {event.name!r} names node {source!r}, which is not recorded in this plan trace; "
            "build the event with eks.scenes.scene_event(render, name) from a scene rendered earlier in the plan"
        ) from error
    problem = event_source_problem(event.name, source, node.op, node.args, sources)
    if problem is not None:
        raise TraceError(f"{what}: {problem}")


def trigger_json(ctx: TraceContext, value: object, what: str) -> JsonValue:
    trigger = ensure_trigger(value, what)
    if isinstance(trigger, EventRef):
        check_event(ctx, trigger, what)
    return encode(trigger)


def ensure_event(name: object, what: str) -> str:
    if not isinstance(name, str) or _EVENT.fullmatch(name) is None:
        raise ValueError(
            f"{what}: {name!r} is not an event name; use letters, digits, '_', '.', ':' and '-' (at most 64), "
            "starting with a letter or '_', such as 'flip' or 'logo.done'"
        )
    return name


def add_events(events: list[str], names: tuple[str, ...], what: str) -> None:
    for name in names:
        checked = ensure_event(name, what)
        if checked not in events:
            events.append(checked)


def scene_options(entry: EntrySpec, kind: SceneKind) -> SceneOptions:
    options = entry.options
    if not isinstance(options, SceneOptions):
        raise TraceError(f"{entry.kind} {entry.name!r} is not a scene; decorate it with @eks.scene(kind={kind!r})")
    if options.kind != kind:
        raise TraceError(
            f"scene {entry.name!r} is a {options.kind} scene, not a {kind} scene; "
            f"open it with the {options.kind} builder or declare @eks.scene(kind={kind!r})"
        )
    return options


def scene_seed(ctx: TraceContext, explicit: int | None, key: str) -> int:
    if explicit is not None:
        return explicit
    return derived_seed(ctx.clock.seed, f"{key}/seed") % (SCENE_SEED_MAX + 1)


def scene_seed_key(id_key: str | None, name: str | None, default: str) -> str:
    if id_key is not None:
        return id_key
    return f"scene:{name}" if name is not None else default


def fps_json(fps: Fraction | None) -> int | str | None:
    return format_fps(fps) if fps is not None else None


def without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


class WebSceneBuilder:
    def __init__(
        self, ctx: TraceContext, *, entry: str, size: tuple[int, int], fps: Fraction | None = None,
        duration: Time | None = None, transparent: bool = True, seed: int | None = None, name: str | None = None,
        origin: str | None = None,
    ) -> None:
        if not isinstance(entry, str) or not entry.strip():
            raise TraceError(
                f"a web scene needs its entry page, got {entry!r}; declare "
                "@eks.scene(kind='web', entry='scenes/<name>/index.html')"
            )
        self._ctx = ctx
        self._entry = entry
        self._entry_media = as_media(entry, kind="bundle", origin=origin)
        self._size = size
        self._fps = fps
        self._duration = optional_time(duration, "web scene duration")
        self._transparent = transparent
        self._seed = seed
        self._name = name
        self._props: dict[str, Any] = {}
        self._textures: dict[str, Any] = {}
        self._triggers: list[dict[str, Any]] = []
        self._events: list[str] = []
        self._render: Media | None = None

    @property
    def entry(self) -> str:
        return self._entry

    def prop(self, name: str, value: JsonLike) -> WebSceneBuilder:
        key = self._key(name, "prop", self._props)
        self._props[key] = encode(value)
        return self

    def texture(self, name: str, media: MediaLike) -> WebSceneBuilder:
        key = self._key(name, "texture", self._textures)
        self._textures[key] = encode(as_media(media))
        return self

    def on(self, trigger: Time | TimeSet | EventRef, *, emit: str, data: JsonLike = None) -> WebSceneBuilder:
        self._check_open("on")
        self._triggers.append(without_none({
            "at": trigger_json(self._ctx, trigger, "web scene on()"),
            "emit": ensure_event(emit, "web scene on(emit=)"),
            "data": encode(data),
        }))
        return self

    def emits(self, *events: str) -> WebSceneBuilder:
        self._check_open("emits")
        add_events(self._events, events, "web scene emits()")
        return self

    def finalize(self, *, id_key: str | None = None) -> Media:
        self._check_open("finalize")
        seed_key = scene_seed_key(id_key, self._name, _WEB_FINAL)
        scene = without_none({
            "entry": encode(self._entry_media),
            "size": list(self._size),
            "fps": fps_json(self._fps),
            "duration": encode(self._duration) if self._duration is not None else None,
            "transparent": self._transparent,
            "seed": scene_seed(self._ctx, self._seed, seed_key),
            "props": self._props,
            "textures": self._textures,
            "triggers": self._triggers,
            "events": self._events,
        })
        self._render = cast("Media", self._ctx.record(SCENE_RENDER_OP, {"scene": scene},
                                                      id_key=id_key if id_key is not None else _WEB_FINAL))
        return self._render

    def _key(self, name: str, what: str, table: dict[str, Any]) -> str:
        self._check_open(what)
        if not isinstance(name, str) or not name:
            raise TypeError(f"web scene {what}() takes a non-empty name, got {name!r}")
        if name in table:
            raise TraceError(f"web scene {what} {name!r} is set twice; set each {what} once")
        return name

    def _check_open(self, what: str) -> None:
        if self._render is not None:
            raise TraceError(
                f"web scene {self._entry!r} is already rendered; call {what}() inside the scene function"
            )


WebScene = WebSceneBuilder


def open_web_scene(
    ctx: TraceContext, entry: EntrySpec, *, size: tuple[int, int] | None = None, fps: Fraction | None = None,
) -> WebSceneBuilder:
    options = scene_options(entry, "web")
    if options.entry is None:
        raise TraceError(
            f"web scene {entry.name!r} has no entry page; declare "
            f"@eks.scene(kind='web', entry='scenes/{entry.name}/index.html')"
        )
    return WebSceneBuilder(
        ctx, entry=options.entry, size=size or options.size or DEFAULT_SCENE_SIZE, fps=fps or options.fps,
        duration=options.duration, transparent=options.transparent, seed=options.explicit_seed, name=entry.name,
        origin=entry.file or None)


__all__ = [
    "BLENDER_RENDER_OP",
    "DEFAULT_SCENE_SIZE",
    "EVENT_SOURCE_OPS",
    "GFX_RENDER_OP",
    "SCENE_RENDER_OP",
    "SCENE_RENDER_OPS",
    "TAKE_OP",
    "WebScene",
    "WebSceneBuilder",
    "add_events",
    "check_event",
    "declared_events",
    "ensure_event",
    "ensure_trigger",
    "field_members",
    "fps_json",
    "is_animated_field",
    "is_numeric_animated_field",
    "open_web_scene",
    "scene_options",
    "scene_seed",
    "scene_seed_key",
    "trigger_json",
    "without_none",
]
