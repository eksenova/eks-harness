from __future__ import annotations

import typing
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
)

from eks_harness.ir.catalog.animated import Vec3
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.catalog.keys import Keys
from eks_harness.ir.refs import RefExpr, RefKind, RJson, RMedia, walk_exprs
from eks_harness.ir.registry import Args, op
from eks_harness.ir.time import Fps, Time, TimeSet

EVENT_NAME = r"^[A-Za-z_][A-Za-z0-9_.:-]{0,63}$"
SCENE_SEED_MAX = 2**31 - 1
SCENE_RENDER_OP = "scene.render"
BLENDER_RENDER_OP = "blender.render"
GFX_RENDER_OP = "gfx.render"
TAKE_OP = "score.take"
SCENE_RENDER_OPS = frozenset({BLENDER_RENDER_OP, GFX_RENDER_OP, SCENE_RENDER_OP})
EVENT_SOURCE_OPS = SCENE_RENDER_OPS | {TAKE_OP}
NUMERIC_KEYS: tuple[type[BaseModel], ...] = (Keys[float], Keys[Vec3])
type PropUse = Literal["set", "animate", "pulse"]


def _whole_node(expr: RefExpr) -> RefExpr:
    if not isinstance(expr, RefExpr):
        return expr
    if expr.path:
        raise ValueError(
            f"an event source is the render or take node itself, got a path {list(expr.path)!r} into "
            f"{expr.ref!r}; pass the render ref without a path"
        )
    return expr


EventSource = Annotated[RefExpr, AfterValidator(_whole_node), RefKind("media")]


class _Scene(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventRef(_Scene):
    source: EventSource = Field(description="Node of the scene or take that emits the event")
    name: str = Field(pattern=EVENT_NAME, description="Event name")
    where: dict[str, JsonValue] = Field(default_factory=dict, description="Event data fields that must match")
    nth: int | None = Field(default=None, ge=0, description="Which matching occurrence, 0-based; every one when unset")


Trigger = Time | TimeSet | EventRef


class WebTrigger(_Scene):
    at: Trigger = Field(description="Time, time set or event that fires the trigger")
    emit: str = Field(pattern=EVENT_NAME, description="Event the scene receives")
    data: RJson = Field(default=None, description="Data passed with the event")


class WebScene(_Scene):
    entry: RMedia = Field(description="Entry page of the web scene, a $ref to a media.source node of kind bundle (the entry's directory, fingerprinted as a whole)")
    size: tuple[int, int] = Field(description="Frame size as (width, height) in pixels")
    fps: Fps | None = Field(default=None, description="Frame rate; the embedding clock's when unset")
    duration: Duration | None = Field(default=None, description="Length of the scene; when unset, the length of the embedding clock's bounds (its window, else its duration), which clips cut from; required on an open clock (no window and no duration)")
    transparent: bool = Field(default=True, description="Render with an alpha channel")
    seed: int = Field(
        default=0, ge=0, le=SCENE_SEED_MAX,
        description="Seed of Math.random and crypto.getRandomValues in the scene; derived from the embedding clock's root seed and the scene's embedding key unless the scene sets seed=")
    props: dict[str, RJson] = Field(default_factory=dict, description="Props passed to the scene")
    textures: dict[str, RMedia] = Field(default_factory=dict, description="Media bound to the scene's texture slots")
    triggers: tuple[WebTrigger, ...] = Field(default=(), description="Events sent into the scene on the clock")
    events: tuple[str, ...] = Field(default=(), description="Events the scene emits back into the score")


@op("scene", name="render", out="media", pure=True, location="browser_scene", bounded=True,
    description="Render a web scene on the virtual clock with seeded randomness",
    examples=[
        {
            "scene": {
                "entry": {"$ref": "n0000000001", "path": []},
                "size": [1080, 1350],
                "fps": 60,
                "duration": "8s",
                "props": {"title": "Altın"},
                "triggers": [{"at": "bar:4", "emit": "reveal"}],
            },
        },
    ])
class RenderWebScene(Args):
    scene: WebScene = Field(description="The web scene to render")


def declared_scene_events(args: Mapping[str, Any]) -> list[str]:
    scene = args.get("scene")
    if not isinstance(scene, dict):
        return []
    events = cast("dict[str, Any]", scene).get("events")
    if not isinstance(events, list):
        return []
    return [event for event in cast("list[object]", events) if isinstance(event, str)]


def event_source_problem(name: str, source: str, op: str, args: Mapping[str, Any],
                         sources: frozenset[str] = EVENT_SOURCE_OPS) -> str | None:
    if op not in sources:
        return f"event {name!r} names node {source!r}, which is {op}; events come from {', '.join(sorted(sources))} nodes"
    if op in SCENE_RENDER_OPS:
        events = declared_scene_events(args)
        if name not in events:
            declared = ", ".join(repr(event) for event in events) or "no events"
            return (f"the scene rendered by {source!r} never emits {name!r} (it declares {declared}); "
                    f"declare it in that scene with sc.emits({name!r}) or use one of its events")
    return None


def field_members(model: type[BaseModel], name: str) -> tuple[Any, ...]:
    field = model.model_fields.get(name)
    if field is None:
        return ()
    annotation: Any = field.annotation
    members = typing.get_args(annotation)
    return members if members else (annotation,)


def is_animated_field(model: type[BaseModel], name: str) -> bool:
    return any(isinstance(member, type) and issubclass(member, Keys) for member in field_members(model, name))


def is_numeric_animated_field(model: type[BaseModel], name: str) -> bool:
    return any(member in NUMERIC_KEYS for member in field_members(model, name))


def prop_problem(model: type[BaseModel], prop: str, fixed: Collection[str], use: PropUse, *,
                 custom: bool) -> str | None:
    if prop in fixed:
        return f"the {prop} of a {model.__name__} is fixed; build another one instead"
    if prop not in model.model_fields:
        if custom:
            return None
        settable = sorted(set(model.model_fields) - set(fixed))
        return f"{model.__name__} has no property {prop!r}; it has {', '.join(settable)}"
    if use == "animate" and not is_animated_field(model, prop):
        animatable = sorted(name for name in model.model_fields if is_animated_field(model, name))
        return f"{prop!r} of a {model.__name__} is not animatable; animate one of {', '.join(animatable)}"
    if use == "pulse" and not is_numeric_animated_field(model, prop):
        numeric = sorted(name for name in model.model_fields if is_numeric_animated_field(model, name))
        return f"{prop!r} of a {model.__name__} is not an animatable number; pulse one of {', '.join(numeric)}"
    return None


def typed_value(record: BaseModel, prop: str, value: JsonValue) -> tuple[Any, str | None]:
    model = type(record)
    if prop not in model.model_fields:
        return None, None
    if any(True for _expr in walk_exprs(value)) and not _holds_refs(model, prop):
        return None, "it holds a ref, but the property takes a value known when the plan is built"
    data = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    try:
        validated = model.model_validate({**data, prop: value})
    except ValidationError as error:
        return None, str(error.errors()[0]["msg"])
    return getattr(validated, prop), None


def keyframe_problem(record: BaseModel, prop: str, value: JsonValue) -> str | None:
    if any(True for _expr in walk_exprs(value)):
        return "a keyframe holds a ref; keyframes hold values known when the plan is built"
    typed, problem = typed_value(record, prop, value)
    if problem is not None:
        return problem
    if isinstance(typed, BaseModel):
        return "a keyframe holds a plain value, not keys or a driver"
    return None


def _holds_refs(model: type[BaseModel], prop: str) -> bool:
    return any(isinstance(member, type) and issubclass(member, RefExpr) for member in field_members(model, prop))


def dependency_path(start: str, goal: str, following: Callable[[str], Sequence[str]]) -> list[str] | None:
    visited: set[str] = set()
    stack: list[list[str]] = [[start]]
    while stack:
        path = stack.pop()
        current = path[-1]
        if current == goal:
            return path
        if current in visited:
            continue
        visited.add(current)
        stack.extend([*path, step] for step in reversed(following(current)))
    return None


def find_cycle(ids: Sequence[str], following: Callable[[str], Sequence[str]]) -> list[str] | None:
    for start in dict.fromkeys(ids):
        for step in following(start):
            path = dependency_path(step, start, following)
            if path is not None:
                return [start, *path]
    return None


def repeated_ids(ids: Sequence[str]) -> list[int]:
    seen: set[str] = set()
    repeated: list[int] = []
    for position, item in enumerate(ids):
        if item in seen:
            repeated.append(position)
        seen.add(item)
    return repeated


__all__ = [
    "BLENDER_RENDER_OP",
    "EVENT_NAME",
    "EVENT_SOURCE_OPS",
    "GFX_RENDER_OP",
    "NUMERIC_KEYS",
    "SCENE_RENDER_OP",
    "SCENE_RENDER_OPS",
    "SCENE_SEED_MAX",
    "TAKE_OP",
    "EventRef",
    "EventSource",
    "PropUse",
    "RenderWebScene",
    "Trigger",
    "WebScene",
    "WebTrigger",
    "declared_scene_events",
    "dependency_path",
    "event_source_problem",
    "field_members",
    "find_cycle",
    "is_animated_field",
    "is_numeric_animated_field",
    "keyframe_problem",
    "prop_problem",
    "repeated_ids",
    "typed_value",
]
