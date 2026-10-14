from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from eks_harness.ir.ops.blender import (
    WORKER_PLATFORM,
    engine_device_problem,
    lookup_engine,
)
from eks_harness.ir.ops.scene import SCENE_SEED_MAX
from eks_harness.ir.plan import CheckUse, check_locale, check_timezone, normalize_now
from eks_harness.ir.registry import (
    COMPONENT_PATTERN,
    ENTRY_NAME_PATTERN,
    PRESET_NAME_PATTERN,
    EntryKind,
    EntrySpec,
    RegistryError,
    default_registry,
    provenance_for,
    registry_for,
)
from eks_harness.ir.target import Platform
from eks_harness.ir.time import Fps, Span, Time
from eks_harness.ir.types import Color

F = TypeVar("F", bound=Callable[..., Any])
ENTRY_NAME = ENTRY_NAME_PATTERN
TAG_PATTERN = re.compile(r"^[^\s,]+$")
RESERVED_FLOW_PARAMS = frozenset({"platform"})
ENTRY_ATTR = "__eks_entry__"
SceneKind = Literal["blender", "web", "gfx"]
Device = Literal["CPU", "GPU"]
ROOT_PARAMETERS: Mapping[EntryKind, str] = {
    "flow": "the session (s: eks.Session)",
    "seed": "the data builder (d: eks.Data)",
    "edit": "the edit builder (e: eks.Edit)",
    "scene": "the scene builder",
    "score": "the score builder (sc: eks.Score)",
}


def _require_pattern(value: str | None, pattern: re.Pattern[str], what: str) -> str | None:
    if value is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{what} {value!r} must match {pattern.pattern}")
    return value


def _require_duration(value: Time | None, what: str) -> Time | None:
    if value is None:
        return None
    parts = (value.bars, value.beats, value.frames, value.ticks)
    if value.anchor is not None or any(part < 0 for part in parts) or not any(parts):
        raise ValueError(f"{what} must be a positive duration without an anchor, such as t('30s'), got {value}")
    return value


def _require_size(value: tuple[int, int] | None) -> tuple[int, int] | None:
    if value is not None and (value[0] <= 0 or value[1] <= 0):
        raise ValueError(f"size {value} must be two positive integers (width, height)")
    return value


class _Options(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tags: tuple[str, ...] = ()
    description: str = ""

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for tag in value:
            _require_pattern(tag, TAG_PATTERN, "tag")
        if len(set(value)) != len(value):
            raise ValueError(f"tags {list(value)} repeat")
        return value

    @field_validator("now", check_fields=False)
    @classmethod
    def _check_now(cls, value: str | None) -> str | None:
        return normalize_now(value)


class FlowOptions(_Options):
    app: str
    platforms: tuple[Platform, ...] = ()
    seed: str | None = None
    persona: str | None = None
    backend: str | None = None
    viewport: str | None = None
    pace: str | None = None
    timeout: Time | None = None
    noise: tuple[str, ...] = ()
    checks: tuple[CheckUse, ...] = ()
    timezone: str | None = None
    locale: str | None = None
    permissions: Literal["ask", "grant", "deny"] | None = None
    now: str | None = None
    root_seed: int | None = None

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return check_timezone(value)

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        return check_locale(value)

    @field_validator("app", "persona")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("app= and as_= must not be empty")
        return value

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, value: tuple[Platform, ...]) -> tuple[Platform, ...]:
        if len(set(value)) != len(value):
            raise ValueError(f"platforms {list(value)} repeat")
        return value

    @field_validator("seed")
    @classmethod
    def _check_seed(cls, value: str | None) -> str | None:
        return _require_pattern(value, ENTRY_NAME_PATTERN, "seed")

    @field_validator("backend")
    @classmethod
    def _check_backend(cls, value: str | None) -> str | None:
        return _require_pattern(value, COMPONENT_PATTERN, "backend")

    @field_validator("viewport", "pace")
    @classmethod
    def _check_preset(cls, value: str | None) -> str | None:
        return _require_pattern(value, PRESET_NAME_PATTERN, "preset name")

    @field_validator("timeout")
    @classmethod
    def _check_timeout(cls, value: Time | None) -> Time | None:
        if value is not None and (not value.is_concrete or value.ticks <= 0):
            raise ValueError(f"timeout must be a positive wall-clock duration such as t('30s'), got {value}")
        return value


class SeedOptions(_Options):
    backend: str
    now: str | None = None
    seed: int = 0
    base: str | None = None

    @field_validator("backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        _require_pattern(value, COMPONENT_PATTERN, "backend")
        return value

    @field_validator("base")
    @classmethod
    def _check_base(cls, value: str | None) -> str | None:
        return _require_pattern(value, ENTRY_NAME_PATTERN, "base seed")


class EditOptions(_Options):
    fps: Fps
    size: tuple[int, int]
    seed: int = 0
    duration: Time | None = None
    window: Span | None = None
    background: Color | None = None

    @field_validator("size")
    @classmethod
    def _check_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        _require_size(value)
        return value

    @field_validator("duration")
    @classmethod
    def _check_duration(cls, value: Time | None) -> Time | None:
        return _require_duration(value, "duration")


class SceneOptions(_Options):
    kind: SceneKind
    engine: str | None = None
    samples: int | None = Field(default=None, ge=1)
    device: Device | None = None
    seed: int = Field(default=0, ge=0, le=SCENE_SEED_MAX)
    fps: Fps | None = None
    size: tuple[int, int] | None = None
    duration: Time | None = None
    bpm: float | None = Field(default=None, gt=0)
    entry: str | None = None
    transparent: bool = True
    cpu: bool = False
    platform: str | None = Field(default=None, pattern=WORKER_PLATFORM)

    @field_validator("size")
    @classmethod
    def _check_size(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        return _require_size(value)

    @property
    def explicit_seed(self) -> int | None:
        return self.seed if "seed" in self.model_fields_set else None

    @model_validator(mode="after")
    def _check_cpu(self) -> SceneOptions:
        if self.cpu and self.kind != "gfx":
            raise ValueError(f"cpu=True selects the CPU reference renderer of gfx scenes, not of {self.kind} scenes")
        if self.platform is not None and self.kind != "blender":
            raise ValueError(f"platform= pins the farm platform of Blender renders, not of {self.kind} scenes")
        engine = lookup_engine(self.engine) if self.kind == "blender" else None
        problem = engine_device_problem(engine, self.device) if engine is not None and self.device else None
        if problem is not None:
            raise ValueError(problem)
        return self

    @field_validator("duration")
    @classmethod
    def _check_duration(cls, value: Time | None) -> Time | None:
        return _require_duration(value, "duration")


class ScoreOptions(_Options):
    fps: Fps
    song: str | None = None
    window: Span | None = None
    size: tuple[int, int] = (1080, 1920)
    seed: int = 0
    bpm: float | None = Field(default=None, gt=0)
    meter: tuple[int, int] | None = None
    duration: Time | None = None

    @field_validator("size")
    @classmethod
    def _check_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        _require_size(value)
        return value

    @field_validator("meter")
    @classmethod
    def _check_meter(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is not None and (value[0] <= 0 or value[1] <= 0 or value[1] & (value[1] - 1)):
            raise ValueError(f"meter {value} must be (beats per bar, note value) with a power-of-two note value")
        return value

    @field_validator("duration")
    @classmethod
    def _check_duration(cls, value: Time | None) -> Time | None:
        return _require_duration(value, "duration")


def _source_file(fn: Callable[..., Any]) -> str | None:
    try:
        return inspect.getsourcefile(fn)
    except TypeError:
        return None


def _options[O: _Options](model: type[O], kind: EntryKind, values: dict[str, Any]) -> O:
    try:
        return model.model_validate(values)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'options'}: {item['msg']}" for item in error.errors())
        raise RegistryError(f"@eks.{kind}: invalid options ({problems})") from error


def _label(fn: object) -> str:
    return str(getattr(fn, "__qualname__", None) or repr(fn))


def check_signature(kind: EntryKind, fn: Callable[..., Any]) -> list[inspect.Parameter]:
    if not inspect.isfunction(fn):
        raise RegistryError(f"@eks.{kind} decorates a function, got {fn!r}")
    if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn) or inspect.isgeneratorfunction(fn):
        raise RegistryError(
            f"{kind} {_label(fn)} must be a plain function; tracing calls it once and records the ops it calls")
    params = list(inspect.signature(fn).parameters.values())
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if not params or params[0].kind not in positional:
        raise RegistryError(f"{kind} {_label(fn)} must take {ROOT_PARAMETERS[kind]} as its first parameter")
    variadic = [p.name for p in params if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    if variadic:
        raise RegistryError(
            f"{kind} {_label(fn)}: parameters are typed by the signature, so *args and **kwargs ({', '.join(variadic)}) "
            "are not allowed; declare each parameter by name")
    positional_only = [p.name for p in params[1:] if p.kind is inspect.Parameter.POSITIONAL_ONLY]
    if positional_only:
        raise RegistryError(
            f"{kind} {_label(fn)}: parameters {positional_only} are positional-only; parameters are passed by name")
    return params


def register_entry(kind: EntryKind, name: str | None, fn: Callable[..., Any], options: BaseModel) -> EntrySpec:
    check_signature(kind, fn)
    entry_name = name if name is not None else fn.__name__
    if not isinstance(entry_name, str) or not ENTRY_NAME.fullmatch(entry_name):
        raise RegistryError(
            f"{kind} name {entry_name!r} must match {ENTRY_NAME.pattern}; pass name= such as 'checkout-flow'")
    code = getattr(fn, "__code__", None)
    spec = EntrySpec(
        kind=kind, name=entry_name, fn=fn, options=options, module=fn.__module__, qualname=fn.__qualname__,
        file=_source_file(fn), line=code.co_firstlineno if code is not None else None,
        provenance=provenance_for(fn.__module__))
    registry_for(spec.provenance).register_entry(spec)
    vars(fn)[ENTRY_ATTR] = (kind, entry_name)
    return spec


def _check_flow_signature(fn: Callable[..., Any]) -> None:
    params = check_signature("flow", fn)
    reserved = sorted(RESERVED_FLOW_PARAMS.intersection(p.name for p in params[1:]))
    if reserved:
        raise RegistryError(
            f"flow {_label(fn)}: parameter names {reserved} are reserved (eks.plan(fn, platform=...) picks the "
            "platform); rename the parameter")


def flow(
    name: str | None = None,
    *,
    app: str,
    platform: Platform | None = None,
    platforms: Sequence[Platform] | None = None,
    seed: str | None = None,
    as_: str | None = None,
    backend: str | None = None,
    viewport: str | None = None,
    pace: str | None = None,
    timeout: Time | None = None,
    noise: Sequence[str] = (),
    checks: Sequence[str | CheckUse | BaseModel] = (),
    timezone: str | None = None,
    locale: str | None = None,
    permissions: Literal["ask", "grant", "deny"] | None = None,
    tags: Sequence[str] = (),
    now: str | None = None,
    root_seed: int | None = None,
    description: str = "",
) -> Callable[[F], F]:
    if platform is not None and platforms is not None:
        raise RegistryError("@eks.flow: pass platform= or platforms=, not both")
    if isinstance(platforms, str):
        raise RegistryError(f"@eks.flow: platforms= takes a list such as [{platforms!r}]; use platform= for one")
    if platforms is not None and not platforms:
        raise RegistryError("@eks.flow: platforms= needs at least one platform; leave it out to use the app's")
    chosen = (platform,) if platform is not None else tuple(platforms or ())
    options = _options(FlowOptions, "flow", {
        "app": app, "platforms": chosen, "seed": seed, "persona": as_, "backend": backend, "viewport": viewport,
        "pace": pace, "timeout": timeout, "noise": _sequence(noise, "flow", "noise"),
        "checks": _check_uses(checks, "flow"), "timezone": timezone, "locale": locale,
        "permissions": permissions, "tags": _sequence(tags, "flow", "tags"), "now": now,
        "root_seed": root_seed, "description": description})

    def register(fn: F) -> F:
        _check_flow_signature(fn)
        register_entry("flow", name, fn, options)
        return fn

    return register


def _check_use(value: object, kind: EntryKind) -> CheckUse:
    if isinstance(value, CheckUse):
        return value
    if isinstance(value, str):
        return CheckUse(kind=value)
    if isinstance(value, BaseModel):
        matches = [spec for spec in default_registry().checks() if spec.args is type(value)]
        if len(matches) != 1:
            raise RegistryError(
                f"@eks.{kind}: checks= item {type(value).__name__} is not the params model of exactly one registered "
                "check; pass the kind name or eks.checks.use(kind, **params)")
        return CheckUse(kind=matches[0].kind, params=value.model_dump(mode="json", by_alias=True, exclude_none=True))
    raise RegistryError(
        f"@eks.{kind}: checks= items are check kind names, eks.checks.use(...) or check params models, got "
        f"{type(value).__name__} {value!r}")


def _check_uses(value: Sequence[object], kind: EntryKind) -> tuple[CheckUse, ...]:
    if isinstance(value, str):
        raise RegistryError(f"@eks.{kind}: checks= takes a list such as [{value!r}]")
    return tuple(_check_use(item, kind) for item in value)


def _sequence(value: Sequence[str], kind: EntryKind, what: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise RegistryError(f"@eks.{kind}: {what}= takes a list of names, not a single string")
    return tuple(value)


def seed(
    name: str,
    *,
    backend: str,
    now: str | None = None,
    seed: int = 0,
    base: str | None = None,
    tags: Sequence[str] = (),
    description: str = "",
) -> Callable[[F], F]:
    options = _options(SeedOptions, "seed", {
        "backend": backend, "now": now, "seed": seed, "base": base, "tags": _sequence(tags, "seed", "tags"),
        "description": description})

    def register(fn: F) -> F:
        register_entry("seed", name, fn, options)
        return fn

    return register


def edit(
    name: str | None = None,
    *,
    fps: int | str,
    size: tuple[int, int],
    seed: int = 0,
    duration: Time | None = None,
    window: Span | None = None,
    background: str | None = None,
    tags: Sequence[str] = (),
    description: str = "",
) -> Callable[[F], F]:
    options = _options(EditOptions, "edit", {
        "fps": fps, "size": size, "seed": seed, "duration": duration, "window": window,
        "background": background, "tags": _sequence(tags, "edit", "tags"), "description": description})

    def register(fn: F) -> F:
        register_entry("edit", name, fn, options)
        return fn

    return register


def scene(
    name: str | None = None,
    *,
    kind: SceneKind,
    engine: str | None = None,
    samples: int | None = None,
    device: Device | None = None,
    seed: int | None = None,
    fps: int | str | None = None,
    size: tuple[int, int] | None = None,
    duration: Time | None = None,
    bpm: float | None = None,
    entry: str | None = None,
    transparent: bool = True,
    cpu: bool = False,
    platform: str | None = None,
    tags: Sequence[str] = (),
    description: str = "",
) -> Callable[[F], F]:
    values: dict[str, Any] = {
        "kind": kind, "engine": engine, "samples": samples, "device": device, "fps": fps,
        "size": size, "duration": duration, "bpm": bpm, "entry": entry, "transparent": transparent, "cpu": cpu,
        "platform": platform,
        "tags": _sequence(tags, "scene", "tags"), "description": description}
    if seed is not None:
        values["seed"] = seed
    options = _options(SceneOptions, "scene", values)

    def register(fn: F) -> F:
        register_entry("scene", name, fn, options)
        return fn

    return register


def score(
    name: str | None = None,
    *,
    fps: int | str,
    song: str | None = None,
    window: Span | None = None,
    size: tuple[int, int] = (1080, 1920),
    seed: int = 0,
    bpm: float | None = None,
    meter: tuple[int, int] | None = None,
    duration: Time | None = None,
    tags: Sequence[str] = (),
    description: str = "",
) -> Callable[[F], F]:
    options = _options(ScoreOptions, "score", {
        "fps": fps, "song": song, "window": window, "size": size, "seed": seed, "bpm": bpm, "meter": meter,
        "duration": duration, "tags": _sequence(tags, "score", "tags"), "description": description})

    def register(fn: F) -> F:
        register_entry("score", name, fn, options)
        return fn

    return register


__all__ = [
    "ENTRY_ATTR",
    "ENTRY_NAME",
    "RESERVED_FLOW_PARAMS",
    "ROOT_PARAMETERS",
    "TAG_PATTERN",
    "Device",
    "EditOptions",
    "FlowOptions",
    "SceneKind",
    "SceneOptions",
    "ScoreOptions",
    "SeedOptions",
    "check_signature",
    "edit",
    "flow",
    "register_entry",
    "scene",
    "score",
    "seed",
]
