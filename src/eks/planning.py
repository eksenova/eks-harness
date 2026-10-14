from __future__ import annotations

import enum
import importlib
import inspect
import json
import math
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, NewType, cast

from pydantic import (
    ConfigDict,
    PydanticSchemaGenerationError,
    TypeAdapter,
    ValidationError,
)

from eks.checks import default_checks, to_use
from eks.discovery import AppInfo, find_app, find_tree
from eks.profiles import ProfileDefaultsError, session_defaults
from eks.ref import Encodable, Persona, Ref
from eks.tracing import SessionCore, TraceContext, tracing
from eks_harness.ir.contributions import (
    contributions,
    node_contributions,
    resource_contributions,
    session_scope,
    walk_scoped,
)
from eks_harness.ir.entries import (
    EditOptions,
    FlowOptions,
    SceneOptions,
    ScoreOptions,
    SeedOptions,
)
from eks_harness.ir.plan import CheckUse, Clock, Plan, SessionSpec
from eks_harness.ir.registry import (
    ENTRY_MODULE_PREFIX,
    EntrySpec,
    Provenance,
    Registry,
    RegistryError,
    default_registry,
    using_registry,
)
from eks_harness.ir.target import Platform, Target, TargetError, parse_target
from eks_harness.ir.time import Meter, TempoMap, Time, TimeError, coerce_time
from eks_harness.ir.types import MediaInfo, normalize_media_uri

FILE_PREFIX = "file:"

ROOT_FACTORIES: Mapping[str, str] = {
    "flow": "eks.session:open_flow_session",
    "seed": "eks.data:open_data",
    "edit": "eks.editing:open_edit",
    "scene": "eks.scenes:open_scene",
    "score": "eks.scoring:open_score",
}

RootFactory = Callable[[TraceContext, EntrySpec], Any]

PLATFORM_PARAM = "platform"
MEDIA_OP = "media.source"
PLATFORMS: tuple[str, ...] = typing.get_args(Platform)
TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
FALSE_WORDS = frozenset({"false", "0", "no", "off"})
_ARBITRARY = ConfigDict(arbitrary_types_allowed=True)
REQUIRES_FINALIZE_ORDER = 1_000_000


class PlanBuildError(ValueError):
    pass


def plan(entry: Callable[..., Any] | EntrySpec, /, **params: Any) -> Plan:
    registry = default_registry()
    if isinstance(entry, EntrySpec):
        spec = entry
    else:
        try:
            spec = registry.entry_for(entry)
        except RegistryError as error:
            raise PlanBuildError(f"{error}; decorate it with @eks.flow, @eks.seed, @eks.edit, @eks.scene or @eks.score") from error
    values = dict(params)
    platform = values.pop(PLATFORM_PARAM, None) if spec.kind == "flow" else None
    if platform is not None and not isinstance(platform, str):
        raise PlanBuildError(f"platform= takes a platform name such as 'web', got {platform!r}")
    tree = entry_tree(spec)
    registry, spec = home_entry(spec, tree, registry)
    coerced = coerce_params(spec, values, text=False)
    return trace(spec, params=coerced, platform=platform, registry=registry, tree=tree)


def home_entry(entry: EntrySpec, tree: Path | None, registry: Registry) -> tuple[Registry, EntrySpec]:
    if tree is None or not entry.file or not entry.module.startswith(ENTRY_MODULE_PREFIX):
        return registry, entry
    from eks.discovery import load_file
    from eks.workspace import workspace_registry

    home = workspace_registry(tree)
    with using_registry(home):
        entries = load_file(Path(entry.file))
    match = next((found for found in entries if found.kind == entry.kind and found.name == entry.name), None)
    if match is None:
        raise PlanBuildError(f"{entry.kind} {entry.name!r} is no longer defined in {entry.file}")
    return home, match


def trace(
    entry: EntrySpec,
    *,
    params: Mapping[str, Any] | None = None,
    platform: str | None = None,
    tree: Path | None = None,
    registry: Registry | None = None,
    root: RootFactory | None = None,
) -> Plan:
    chosen_registry = registry if registry is not None else default_registry()
    with using_registry(chosen_registry):
        return _trace(entry, params=params, platform=platform, tree=tree, registry=chosen_registry, root=root)


def _trace(
    entry: EntrySpec,
    *,
    params: Mapping[str, Any] | None,
    platform: str | None,
    tree: Path | None,
    registry: Registry,
    root: RootFactory | None,
) -> Plan:
    chosen_registry = registry
    values = dict(params or {})
    session: SessionSpec | None = None
    if entry.kind == "flow":
        app = flow_app(entry, tree)
        chosen = choose_platform(entry, platform, app)
        session = session_spec(entry, chosen, chosen_registry, profile=app.tables if app is not None else None)
        check_persona_params(entry, values, session, chosen_registry)
    elif platform is not None:
        raise PlanBuildError(f"platform= applies to flows only; {entry.kind} {entry.name!r} has no platform")
    ctx = TraceContext(
        kind=entry.kind, name=entry.name, registry=chosen_registry, entry=entry,
        clock=base_clock(entry, chosen_registry),
        session=session, params=_plain_params(values), tree=tree, entry_code=getattr(entry.fn, "__code__", None))
    with tracing(ctx):
        ctx.on_finalize(_require_contributions, order=REQUIRES_FINALIZE_ORDER)
        arguments = _media_arguments(ctx, entry, values)
        builder = (root or root_factory(entry.kind))(ctx, entry)
        if entry.kind == "flow" and isinstance(builder, SessionCore):
            with builder.activate():
                result = entry.fn(builder, **arguments)
        else:
            result = entry.fn(builder, **arguments)
        ctx.set_outputs(result)
        return ctx.finalize()


def _require_contributions(ctx: TraceContext) -> None:
    registry = ctx.registry
    session = ctx.session
    found = list(contributions(session, registry)) if session is not None else []
    for node, scope in walk_scoped(ctx.scope().nodes, session_scope(session)):
        found.extend(node_contributions(node, registry, scope=scope) or ())
    for resource in ctx.declared_resources():
        found.extend(resource_contributions(resource, registry))
    for item in found:
        ctx.require(Provenance(item.plugin, item.plugin_version))


def persona_params(entry: EntrySpec) -> list[inspect.Parameter]:
    parameters = list(inspect.signature(entry.fn).parameters.values())[1:]
    if not parameters:
        return []
    hints = _hints(entry, [parameter.name for parameter in parameters])
    return [parameter for parameter in parameters if _is_persona(hints.get(parameter.name, Any))]


def check_persona_params(entry: EntrySpec, values: Mapping[str, Any], session: SessionSpec,
                         registry: Registry) -> None:
    chosen = persona_params(entry)
    if not chosen or session.seed is None:
        return
    from eks.workspace import seed_personas

    known = seed_personas(session.seed, registry=registry)
    if known is None:
        return
    for parameter in chosen:
        value = values.get(parameter.name, parameter.default)
        if isinstance(value, str) and value not in known:
            raise _fail(entry, parameter.name, f"persona {value!r} is not an output of seed {session.seed!r}; its "
                        f"personas are {_names(sorted(known))}")


def _is_persona(annotation: Any) -> bool:
    while True:
        if annotation is Persona:
            return True
        origin = typing.get_origin(annotation)
        if origin is Annotated:
            annotation = typing.get_args(annotation)[0]
        elif origin in (typing.Union, types.UnionType):
            members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
            if len(members) != 1:
                return False
            annotation = members[0]
        else:
            return False


def flow_app(entry: EntrySpec, tree: Path | None) -> AppInfo | None:
    options = entry.options
    if tree is None or not isinstance(options, FlowOptions):
        return None
    return find_app(tree, options.app)


def choose_platform(entry: EntrySpec, requested: str | None, app: AppInfo | None) -> str:
    options = entry.options
    app_platforms = app.platforms if app is not None else ()
    chosen = resolve_platform(entry, requested, app_platforms)
    if chosen is not None:
        return chosen
    flow_platforms = list(options.platforms) if isinstance(options, FlowOptions) else []
    candidates = flow_platforms or list(app_platforms)
    app_name = options.app if isinstance(options, FlowOptions) else entry.name
    if candidates:
        where = f"it runs on {candidates}"
    elif app is None:
        where = f"neither the flow nor an app profile named {app_name!r} lists its platforms"
        candidates = list(PLATFORMS)
    else:
        where = f"app {app_name!r} lists no platforms"
        candidates = list(PLATFORMS)
    raise PlanBuildError(f"flow {entry.name!r} does not name one platform ({where}); pass platform= (or --platform "
                         f"on the CLI) with one of {candidates}")


def app_defaults(profile: Mapping[str, Any] | None, platform: str, app: str) -> dict[str, Any]:
    if profile is None:
        return {}
    try:
        return session_defaults(profile, platform, app)
    except ProfileDefaultsError as error:
        raise PlanBuildError(str(error)) from error


def merged_session(defaults: Mapping[str, Any], own: Mapping[str, Any], registry: Registry,
                   what: str, platform: str) -> dict[str, Any]:
    merged = {**{key: value for key, value in defaults.items() if key not in ("noise", "checks")},
              **{key: value for key, value in own.items() if value is not None and key not in ("noise", "checks")}}
    merged["noise"] = tuple(dict.fromkeys((*defaults.get("noise", ()), *own.get("noise", ()))))
    own_checks: tuple[CheckUse, ...] = tuple(own.get("checks", ()))
    kinds = {use.kind for use in own_checks}
    combined = (*(use for use in defaults.get("checks", ()) if use.kind not in kinds), *own_checks)
    try:
        merged["checks"] = tuple(to_use(use, registry) for use in combined)
        merged["defaults"] = default_checks(platform, merged["checks"], registry)
    except (RegistryError, ValidationError, TypeError) as error:
        raise PlanBuildError(f"{what} checks: {error}") from error
    return merged


def entry_tree(entry: EntrySpec) -> Path | None:
    return find_tree(Path(entry.file).parent) if entry.file else None


def seed_plan(entry: EntrySpec, *, registry: Registry | None = None) -> Plan:
    if entry.kind != "seed":
        raise PlanBuildError(f"{entry.kind} {entry.name!r} is not a seed")
    tree = entry_tree(entry)
    if registry is None:
        registry, entry = home_entry(entry, tree, default_registry())
    return trace(entry, registry=registry, tree=tree)


def _plain_params(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return _plain_params(value.value)
    if isinstance(value, Mapping):
        return {key: _plain_params(item) for key, item in cast("Mapping[Any, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [_plain_params(item) for item in cast("Sequence[Any]", value)]
    return value


def _media_arguments(ctx: TraceContext, entry: EntrySpec, values: Mapping[str, Any]) -> dict[str, Any]:
    arguments = dict(values)
    texts = [name for name, value in values.items() if isinstance(value, str)]
    if not texts:
        return arguments
    hints = _hints(entry, texts)
    for name, value in values.items():
        if isinstance(value, str) and _is_media(_strip(hints.get(name, Any))[0]):
            try:
                uri = normalize_media_uri(value)
            except ValueError as error:
                raise PlanBuildError(f"{entry.kind} {entry.name!r} parameter {name!r}: {error}") from error
            if uri.startswith(FILE_PREFIX):
                uri = ctx.media_uri(uri.removeprefix(FILE_PREFIX), entry.file or None)
            arguments[name] = ctx.record(MEDIA_OP, {"uri": uri}, id_key=f"param:{name}")
    return arguments


def coerce_params(entry: EntrySpec, values: Mapping[str, Any], *, text: bool = False) -> dict[str, Any]:
    parameters = list(inspect.signature(entry.fn).parameters.values())[1:]
    named = [p for p in parameters if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    known = [p.name for p in named]
    unknown = [name for name in values if name not in known]
    if unknown:
        raise PlanBuildError(
            f"{entry.kind} {entry.name!r} has no parameter {_names(unknown)}; known parameters: {_names(known)}"
        )
    hints = _hints(entry, list(values))
    result: dict[str, Any] = {}
    missing: list[str] = []
    for parameter in named:
        if parameter.name in values:
            annotation = hints.get(parameter.name, Any)
            result[parameter.name] = _coerce(entry, parameter.name, annotation, values[parameter.name], text)
        elif parameter.default is not inspect.Parameter.empty:
            result[parameter.name] = parameter.default
        else:
            missing.append(parameter.name)
    if missing:
        raise PlanBuildError(
            f"{entry.kind} {entry.name!r} needs a value for {_names(missing)}; known parameters: {_names(known)}"
        )
    return result


def _names(names: Sequence[str]) -> str:
    return ", ".join(repr(name) for name in names) if names else "(none)"


def _hints(entry: EntrySpec, names: Sequence[str]) -> dict[str, Any]:
    if not names:
        return {}
    try:
        hints = typing.get_type_hints(entry.fn, include_extras=True)
    except (NameError, TypeError, AttributeError):
        hints = _parameter_hints(entry, names)
    return {name: hints[name] for name in names if name in hints}


def _parameter_hints(entry: EntrySpec, names: Sequence[str]) -> dict[str, Any]:
    fn = inspect.unwrap(entry.fn)
    annotations = inspect.get_annotations(fn)
    namespace: dict[str, Any] = getattr(fn, "__globals__", {})
    hints: dict[str, Any] = {}
    for name in names:
        if name not in annotations:
            continue
        holder = types.SimpleNamespace(__annotations__={name: annotations[name]})
        try:
            hints.update(typing.get_type_hints(holder, globalns=namespace, include_extras=True))
        except (NameError, TypeError, AttributeError) as error:
            raise PlanBuildError(
                f"cannot read the type of parameter {name!r} of {entry.kind} {entry.name!r}: {error}"
            ) from error
    return hints


def _strip(annotation: Any) -> tuple[Any, bool]:
    optional = False
    while True:
        origin = typing.get_origin(annotation)
        if origin is Annotated:
            annotation = typing.get_args(annotation)[0]
        elif origin in (typing.Union, types.UnionType):
            members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
            if len(members) != 1:
                return annotation, optional
            optional = True
            annotation = members[0]
        elif isinstance(annotation, NewType):
            annotation = annotation.__supertype__
        else:
            return annotation, optional


def _is_media(annotation: Any) -> bool:
    return typing.get_origin(annotation) is Ref and typing.get_args(annotation) == (MediaInfo,)


def _fail(entry: EntrySpec, name: str, message: str) -> PlanBuildError:
    return PlanBuildError(f"{entry.kind} {entry.name!r} parameter {name!r}: {message}")


def _coerce(entry: EntrySpec, name: str, annotation: Any, value: Any, text: bool) -> Any:
    base, optional = _strip(annotation)
    if value is None and optional:
        return None
    if text:
        if not isinstance(value, str):
            raise _fail(entry, name, f"text mode takes strings, got {type(value).__name__} {value!r}")
        return _from_text(entry, name, base, annotation, value)
    return _from_object(entry, name, base, annotation, value)


def _from_text(entry: EntrySpec, name: str, base: Any, annotation: Any, value: str) -> Any:
    if base is Any:
        return value
    if _is_media(base):
        return value
    if base is Time:
        try:
            return coerce_time(value)
        except TimeError as error:
            raise _fail(entry, name, str(error)) from error
    if base is Target:
        return _target(entry, name, value)
    return _validate(entry, name, annotation, _parse_text(entry, name, base, value))


def _parse_text(entry: EntrySpec, name: str, base: Any, value: str) -> Any:
    if base is str:
        return value
    if base is bool:
        word = value.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise _fail(entry, name, f"{value!r} is not a boolean; write true/false, 1/0, yes/no or on/off")
    if base is int:
        try:
            return int(value)
        except ValueError as error:
            raise _fail(entry, name, f"{value!r} is not an integer") from error
    if base is float:
        try:
            number = float(value)
        except ValueError as error:
            raise _fail(entry, name, f"{value!r} is not a number") from error
        if not math.isfinite(number):
            raise _fail(entry, name, f"{value!r} is not a finite number")
        return number
    if typing.get_origin(base) is Literal:
        return _literal(entry, name, base, value)
    if isinstance(base, type) and issubclass(base, enum.Enum):
        return _enum(entry, name, base, value)
    if typing.get_origin(base) is list and typing.get_args(base) == (str,):
        return [part.strip() for part in value.split(",")] if value.strip() else []
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise _fail(entry, name, f"{value!r} is not JSON ({error.msg}); write the value as JSON") from error


def _from_object(entry: EntrySpec, name: str, base: Any, annotation: Any, value: Any) -> Any:
    if base is Any:
        return value
    if base is Time:
        if not isinstance(value, Time):
            raise _fail(entry, name, f"expects a Time, got {type(value).__name__} {value!r}; pass t('...')")
        return value
    if base is Target:
        if isinstance(value, Target):
            return value
        if isinstance(value, str):
            return _target(entry, name, value)
        raise _fail(entry, name, f"expects a Target or a target string, got {type(value).__name__} {value!r}")
    if _is_media(base):
        if isinstance(value, Ref | str | Encodable):
            return value
        raise _fail(entry, name, f"expects media (a path, an artifact:ID, a media Ref), got {type(value).__name__}")
    if typing.get_origin(base) is Ref:
        if isinstance(value, Ref):
            return value
        raise _fail(entry, name, f"expects a Ref, got {type(value).__name__} {value!r}")
    return _validate(entry, name, annotation, value)


def _target(entry: EntrySpec, name: str, value: str) -> Target:
    try:
        return parse_target(value, default_registry().target_kinds())
    except (TargetError, TypeError) as error:
        raise _fail(entry, name, str(error)) from error


def _literal(entry: EntrySpec, name: str, base: Any, value: str) -> Any:
    choices = typing.get_args(base)
    for choice in choices:
        if (choice if isinstance(choice, str) else json.dumps(choice)) == value:
            return choice
    raise _fail(entry, name, f"{value!r} is not one of {list(choices)}")


def _enum(entry: EntrySpec, name: str, base: type[enum.Enum], value: str) -> enum.Enum:
    for member in base:
        if value in (member.name, str(member.value)):
            return member
    raise _fail(entry, name, f"{value!r} is not one of {[member.name for member in base]}")


def _adapter(annotation: Any) -> TypeAdapter[Any]:
    try:
        return TypeAdapter(annotation)
    except PydanticSchemaGenerationError:
        return TypeAdapter(annotation, config=_ARBITRARY)


def _validate(entry: EntrySpec, name: str, annotation: Any, value: Any) -> Any:
    try:
        return _adapter(annotation).validate_python(value)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(str(part) for part in item['loc']) or 'value'}: {item['msg']}"
                             for item in error.errors())
        raise _fail(entry, name, f"{value!r} is invalid ({problems})") from error


def parse_sets(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator or not key:
            raise PlanBuildError(f"--set takes KEY=VALUE, got {item!r}")
        if key in values:
            raise PlanBuildError(f"--set {key} is given twice ({values[key]!r} and {value!r}); keep one")
        values[key] = value
    return values


def _flow_platforms(entry: EntrySpec) -> list[str]:
    options = entry.options
    return list(options.platforms) if isinstance(options, FlowOptions) else []


def resolve_platform(entry: EntrySpec, requested: str | None, app_platforms: Sequence[str] = ()) -> str | None:
    if entry.kind != "flow":
        raise PlanBuildError(f"{entry.kind} {entry.name!r} is not a flow and has no platform")
    candidates = _flow_platforms(entry) or list(app_platforms)
    if requested is not None:
        if requested not in PLATFORMS:
            raise PlanBuildError(f"unknown platform {requested!r}; use one of {list(PLATFORMS)}")
        if candidates and requested not in candidates:
            raise PlanBuildError(
                f"flow {entry.name!r} runs on {candidates}; platform {requested!r} is not one of them"
            )
        return requested
    if len(candidates) == 1:
        return candidates[0]
    return None


def seed_options(registry: Registry, name: str | None) -> SeedOptions | None:
    if name is None:
        return None
    entry = next((spec for spec in registry.entries("seed") if spec.name == name), None)
    return entry.options if entry is not None and isinstance(entry.options, SeedOptions) else None


def inherited(registry: Registry, seed: str | None, *, now: str | None, root_seed: int | None,
              backend: str | None) -> tuple[str | None, int | None, str | None]:
    scenario = seed_options(registry, seed)
    if scenario is None:
        return now, root_seed, backend
    return (now if now is not None else scenario.now, root_seed if root_seed is not None else scenario.seed,
            backend if backend is not None else scenario.backend)


def session_spec(entry: EntrySpec, platform: str, registry: Registry | None = None, *,
                 profile: Mapping[str, Any] | None = None) -> SessionSpec:
    options = entry.options
    if not isinstance(options, FlowOptions):
        raise PlanBuildError(f"{entry.kind} {entry.name!r} is not a flow and has no session")
    chosen = registry if registry is not None else default_registry()
    now, root_seed, backend = inherited(chosen, options.seed, now=options.now, root_seed=options.root_seed,
                                        backend=options.backend)
    own: dict[str, Any] = {
        "viewport": options.viewport, "pace": options.pace, "timeout": options.timeout, "noise": options.noise,
        "checks": options.checks, "timezone": options.timezone, "locale": options.locale,
        "permissions": options.permissions,
    }
    fields = merged_session(app_defaults(profile, platform, options.app), own, chosen, f"flow {entry.name!r}",
                            platform)
    try:
        return SessionSpec.model_validate({
            "key": "main", "app": options.app, "platform": platform, "seed": options.seed,
            "persona": options.persona, "backend": backend, "root_seed": root_seed, "now": now, **fields,
        })
    except ValidationError as error:
        raise PlanBuildError(f"flow {entry.name!r}: the session does not validate: {error}") from error


def base_clock(entry: EntrySpec, registry: Registry | None = None) -> Clock:
    options = entry.options
    if isinstance(options, FlowOptions):
        now, root_seed, _backend = inherited(registry if registry is not None else default_registry(), options.seed,
                                             now=options.now, root_seed=options.root_seed, backend=options.backend)
        return Clock(now=now, seed=root_seed or 0)
    if isinstance(options, SeedOptions):
        return Clock(now=options.now, seed=options.seed)
    if isinstance(options, EditOptions):
        return Clock.model_validate({
            "seed": options.seed, "fps": options.fps, "duration": options.duration, "window": options.window})
    if isinstance(options, SceneOptions):
        tempo = TempoMap(bpm=options.bpm) if options.bpm else TempoMap(external=True)
        return Clock.model_validate({
            "seed": options.seed, "fps": options.fps, "duration": options.duration, "tempo": tempo})
    if isinstance(options, ScoreOptions):
        tempo = None
        if options.bpm:
            meter = Meter(numerator=options.meter[0], denominator=options.meter[1]) if options.meter else Meter()
            tempo = TempoMap(bpm=options.bpm, meter=meter)
        return Clock.model_validate({
            "seed": options.seed, "fps": options.fps, "window": options.window, "duration": options.duration,
            "tempo": tempo})
    raise PlanBuildError(f"{entry.kind} {entry.name!r} has options of unknown type {type(options).__name__}")


def root_factory(kind: str) -> RootFactory:
    target = ROOT_FACTORIES.get(kind)
    if target is None:
        raise PlanBuildError(f"no root builder for plan kind {kind!r}; known kinds: {sorted(ROOT_FACTORIES)}")
    module_name, _, attribute = target.partition(":")
    module = importlib.import_module(module_name)
    return cast("RootFactory", getattr(module, attribute))


__all__ = [
    "REQUIRES_FINALIZE_ORDER",
    "ROOT_FACTORIES",
    "PlanBuildError",
    "RootFactory",
    "app_defaults",
    "base_clock",
    "check_persona_params",
    "choose_platform",
    "coerce_params",
    "entry_tree",
    "flow_app",
    "home_entry",
    "inherited",
    "merged_session",
    "parse_sets",
    "persona_params",
    "plan",
    "resolve_platform",
    "root_factory",
    "seed_options",
    "seed_plan",
    "session_spec",
    "trace",
]
