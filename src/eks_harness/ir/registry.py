from __future__ import annotations

import difflib
import importlib
import inspect
import json
import keyword
import os
import re
import sys
import types
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Protocol,
    TypeVar,
    get_args,
    get_protocol_members,
    overload,
    runtime_checkable,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    ValidationError,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

from eks_harness.ir.target import BUILTIN_KINDS, PLATFORMS, SURFACES, TargetKind
from eks_harness.ir.types import RefTypeError, parse_ref_type

if TYPE_CHECKING:
    from eks_harness.plugins.sdk import Backend as BackendImpl
    from eks_harness.plugins.sdk import BackendPolicy as BackendPolicyImpl
    from eks_harness.plugins.sdk import ManagedBackend

Location = Literal["hub", "worker", "node", "blender", "browser_scene"]
Ordering = Literal["program", "graph"]
Phase = Literal["schema", "api", "finalize", "mongo"]
CheckFamily = Literal["checkpoint", "visual", "perf", "media"]
ResourceRole = Literal["lease", "backend"]
Category = Literal["effect", "transition", "easing", "curve", "overlay", "target", "marker"]
EntryKind = Literal["flow", "seed", "edit", "scene", "score"]

BUILTIN_PLUGIN = "eks"
PROJECT_PLUGIN = "project"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
QUALIFIED_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
PRESET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
COMPONENT_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
SHORTHAND_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
SCOPED_MODULE_PATTERN = re.compile(r"^_eks_harness_plugin_[A-Za-z0-9_]*_[0-9a-f]{12}(?:\.|$)")
ENTRY_MODULE_PREFIX = "eks_entry_"
BUILTIN_FAMILIES = frozenset({
    "api", "audio", "backend", "blender", "capture", "check", "ctl", "data", "debug", "edit", "expect", "fake",
    "gfx", "inject", "media", "mobile", "net", "overlay", "scene", "score", "session", "store", "ui", "web",
})
SESSION_MEMBERS = frozenset({
    "activate", "api", "app", "as_", "backend", "call", "checkpoint", "ctx", "debug", "dump", "eval", "explore", "fake",
    "har", "hold", "inject", "macro", "mobile", "net", "op", "overlay", "platform", "record", "record_container", "record_op",
    "resources", "shot", "spec", "ui", "web",
})
PROJECT_NAMESPACE = "project"
ENTRY_KINDS: tuple[EntryKind, ...] = get_args(EntryKind)
OVERLAY_RENDERERS = ("live", "screenshot", "video")
CREATE_HOOK = "create"
SCRIPT_SUFFIXES = (".js", ".mjs", ".ts")

M = TypeVar("M", bound=type[BaseModel])
F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=Callable[..., Any])
B = TypeVar("B", bound="type[BackendImpl | ManagedBackend]")
AppT = TypeVar("AppT", bound=type)
P = TypeVar("P", bound="type[BackendPolicyImpl]")


class RegistryError(ValueError):
    pass


class Args(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )


class PresetModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    preset_category: ClassVar[str] = ""


@dataclass(frozen=True, slots=True)
class Provenance:
    plugin: str = PROJECT_PLUGIN
    version: str = ""


@dataclass(frozen=True, slots=True)
class OpSpec:
    name: str
    family: str
    args: type[BaseModel]
    out: str = "none"
    version: str = "1"
    description: str = ""
    location: Location = "hub"
    executor: str | None = None
    executor_version: str = ""
    pure: bool = False
    idempotent: bool = False
    device_dependent: bool = False
    ordering: Ordering = "program"
    platforms: frozenset[str] | None = None
    resources: tuple[ResourceRole, ...] = ()
    container: bool = False
    branches: tuple[str, ...] = ()
    exports_body: bool = False
    pinnable: bool = False
    recording_only: bool = False
    bounded: bool = False
    provenance: Provenance = Provenance()
    examples: tuple[Mapping[str, Any], ...] = field(default=(), hash=False)


@dataclass(frozen=True, slots=True)
class MacroSpec:
    name: str
    namespace: str
    short: str
    fn: Callable[..., Any]
    platforms: frozenset[str] | None = None
    description: str = ""
    version: str = "1"
    provenance: Provenance = Provenance()


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    kind: str
    model: type[BaseModel]
    parent: str | None = None
    phase: Phase = "api"
    keyed: bool = True
    positional: tuple[str, ...] = ()
    persona: bool = False
    description: str = ""
    version: str = "1"
    provenance: Provenance = Provenance()
    create: str | None = None


@dataclass(frozen=True, slots=True)
class CheckSpec:
    kind: str
    family: CheckFamily
    args: type[BaseModel]
    platforms: frozenset[str] | None = None
    default: bool = False
    description: str = ""
    version: str = "1"
    provenance: Provenance = Provenance()
    impl: str | None = None
    device_dependent: bool = False


@runtime_checkable
class Pinnable(Protocol):
    def pinned(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class CatalogSpec:
    category: Category
    kind: str
    model: type[BaseModel]
    description: str = ""
    version: str = "1"
    surfaces: frozenset[str] = frozenset()
    shorthand: str | None = None
    renderers: Mapping[str, str] = field(default_factory=dict)
    platforms: frozenset[str] | None = None
    provenance: Provenance = Provenance()
    resolvers: Mapping[str, str] = field(default_factory=dict)
    device_dependent: bool = False


@dataclass(frozen=True, slots=True)
class RendererSpec:
    media_kind: str
    impl: str
    description: str = ""
    provenance: Provenance = Provenance()


@dataclass(frozen=True, slots=True)
class BackendRequirement:
    name: str
    key: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "key": self.key, "params": dict(self.params)}


BackendIdentityKind = Literal["tree", "host", "global"]


@dataclass(frozen=True, slots=True)
class BackendSpec:
    name: str
    impl: str
    description: str = ""
    provenance: Provenance = Provenance()
    requires: tuple[BackendRequirement, ...] = ()
    identity: BackendIdentityKind = "tree"
    idle_timeout: float | None = None


@dataclass(frozen=True, slots=True)
class AppSpec:
    name: str
    impl: str
    description: str = ""
    provenance: Provenance = Provenance()


@dataclass(frozen=True, slots=True)
class BackendPolicySpec:
    name: str
    impl: str
    description: str = ""
    provenance: Provenance = Provenance()


@dataclass(frozen=True, slots=True)
class PresetSpec:
    category: str
    name: str
    value: BaseModel
    description: str = ""
    provenance: Provenance = Provenance()
    source: str = ""


@dataclass(frozen=True, slots=True)
class EntrySpec:
    kind: EntryKind
    name: str
    fn: Callable[..., Any]
    options: BaseModel
    module: str
    qualname: str
    file: str | None = None
    line: int | None = None
    provenance: Provenance = Provenance()


@dataclass
class RegistryState:
    tables: dict[str, dict[Any, Any]]


_CONTRIBUTOR: ContextVar[Provenance | None] = ContextVar("eks_registry_contributor", default=None)
_ACTIVE: ContextVar[Registry | None] = ContextVar("eks_active_registry", default=None)


def builtin_version() -> str:
    from eks_harness import __version__

    return __version__


def provenance_for(module: str) -> Provenance:
    if module == "eks" or module.startswith(("eks.", "eks_harness.")):
        return Provenance(BUILTIN_PLUGIN, builtin_version())
    current = _CONTRIBUTOR.get()
    return current if current is not None else Provenance(PROJECT_PLUGIN, "")


RESERVED_PLUGIN_IDS = frozenset({BUILTIN_PLUGIN, PROJECT_PLUGIN})


@contextmanager
def contributing(plugin: str, version: str) -> Iterator[Provenance]:
    if plugin in RESERVED_PLUGIN_IDS:
        raise RegistryError(
            f"plugin id {plugin!r} is reserved for {'the harness' if plugin == BUILTIN_PLUGIN else 'project code'}; "
            "give the plugin an id of its own, for example 'acme.tools'")
    provenance = Provenance(plugin, version)
    token = _CONTRIBUTOR.set(provenance)
    try:
        yield provenance
    finally:
        _CONTRIBUTOR.reset(token)


def snake_case(name: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return text.lower()


def callable_ref(value: str | Callable[..., Any] | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return f"{value.__module__}:{value.__qualname__}"


def require_model(model: object, decorator: str) -> None:
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise RegistryError(f"{decorator} decorates a pydantic model class, got {model!r}")


def require_strict(model: type[BaseModel], what: str, base: type[BaseModel] | None = None,
                   base_name: str | None = None) -> None:
    shown = base_name or (f"eks.{base.__name__}" if base is not None else "")
    if base is not None and not issubclass(model, base):
        raise RegistryError(f"{what}: {model.__name__} must subclass {shown}")
    if model.model_config.get("extra") != "forbid":
        hint = (f"subclass {shown} without overriding extra" if base is not None
                else "set model_config = ConfigDict(extra='forbid')")
        raise RegistryError(f"{what}: {model.__name__} must forbid unknown fields so misspelled arguments fail; {hint}")


def names_of[T: str](value: Iterable[T] | None, what: str) -> tuple[T, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise RegistryError(f"{what}= takes a list such as [{value!r}], not a single string")
    return tuple(value)


def _names[T: str](value: Iterable[T], what: str) -> tuple[T, ...]:
    names = names_of(value, what)
    return names if names is not None else ()


def _platform_set(value: Iterable[str] | None, what: str) -> frozenset[str] | None:
    names = names_of(value, what)
    return frozenset(names) if names is not None else None


def model_kind(model: type[BaseModel]) -> str:
    info = model.model_fields.get("kind")
    if info is None or not isinstance(info.default, str):
        raise RegistryError(f"{model.__name__} needs a 'kind' field with a literal string default")
    return info.default


type AnySpec = (
    OpSpec | MacroSpec | ResourceSpec | CheckSpec | CatalogSpec | RendererSpec | BackendSpec | BackendPolicySpec
    | PresetSpec | EntrySpec | AppSpec
)


def _resolve(module: str, qualname: str) -> object | None:
    found: object | None = sys.modules.get(module)
    for part in qualname.split("."):
        if found is None or part == "<locals>":
            return None
        found = getattr(found, part, None)
    return found


def _first_line(target: object) -> int | None:
    code = getattr(target, "__code__", None)
    if isinstance(code, types.CodeType):
        return code.co_firstlineno
    line = getattr(target, "__firstlineno__", None)
    return line if isinstance(line, int) else None


def _shown_path(path: str) -> str:
    try:
        relative = os.path.relpath(path)
    except ValueError:
        return path
    return path if relative.startswith("..") else relative


def source_location(target: object, module: str, qualname: str) -> str:
    candidate = target if isinstance(target, type | types.FunctionType) else _resolve(module, qualname)
    code = getattr(candidate, "__code__", None)
    path = code.co_filename if isinstance(code, types.CodeType) else getattr(sys.modules.get(module), "__file__", None)
    if not isinstance(path, str) or not path:
        return ""
    line = _first_line(candidate) if candidate is not None else None
    shown = _shown_path(path)
    return f"{shown}:{line}" if line is not None else shown


def _is_generated(module: str) -> bool:
    return module.startswith(ENTRY_MODULE_PREFIX) or SCOPED_MODULE_PATTERN.match(module) is not None


def module_identity(module: str, plugin: str) -> str:
    scoped = SCOPED_MODULE_PATTERN.match(module)
    return f"{plugin}/{module[scoped.end():]}" if scoped else module


def defined_in(spec: AnySpec, module: str, plugin: str | None = None) -> bool:
    definition = definition_of(spec)
    if not definition.module:
        return False
    if plugin is None:
        return definition.module == module
    return module_identity(definition.module, definition.provenance.plugin) == module_identity(module, plugin)


@dataclass(frozen=True, slots=True)
class Definition:
    target: object
    module: str
    qualname: str
    provenance: Provenance
    location: str = ""

    @property
    def origin(self) -> str:
        if not self.module or not self.qualname:
            return ""
        return f"{module_identity(self.module, self.provenance.plugin)}:{self.qualname}"

    def same(self, other: Definition) -> bool:
        return self.target is other.target or (bool(self.origin) and self.origin == other.origin)

    def describe(self) -> str:
        source = self.provenance.plugin
        if self.provenance.version:
            source = f"{source} {self.provenance.version}"
        if _is_generated(self.module):
            location = self.location or source_location(self.target, self.module, self.qualname)
            if location:
                return f"{location} ({self.qualname}, from {source})"
        where = f"{self.module}:{self.qualname}" if self.module and self.qualname else repr(self.target)
        return f"{where} (from {source})"


def definition_of(spec: AnySpec) -> Definition:
    match spec:
        case OpSpec() | CheckSpec():
            target: object = spec.args
        case ResourceSpec() | CatalogSpec():
            target = spec.model
        case MacroSpec():
            target = spec.fn
        case EntrySpec():
            location = "" if spec.file is None else _shown_path(spec.file)
            if location and spec.line is not None:
                location = f"{location}:{spec.line}"
            return Definition(spec.fn, spec.module, spec.qualname, spec.provenance, location)
        case PresetSpec():
            module, _, qualname = spec.source.partition(":")
            return Definition(spec.value, module, qualname, spec.provenance)
        case RendererSpec() | BackendSpec() | BackendPolicySpec() | AppSpec():
            module, _, qualname = spec.impl.partition(":")
            return Definition(spec.impl, module, qualname, spec.provenance)
    return Definition(target, getattr(target, "__module__", ""), getattr(target, "__qualname__", ""),
                      spec.provenance)


def _store[K, S: AnySpec](table: dict[K, S], key: K, spec: S, what: str) -> S:
    existing = table.get(key)
    if existing is not None:
        old, new = definition_of(existing), definition_of(spec)
        if not old.same(new):
            raise RegistryError(
                f"{what} is already registered by {old.describe()}; {new.describe()} cannot register it again. "
                "Rename one of them or remove the duplicate definition")
    table[key] = spec
    return spec


def unknown_name(what: str, name: str, known: Iterable[str]) -> RegistryError:
    names = sorted(set(known))
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    message = f"unknown {what} {name!r}"
    if close:
        message += f"; did you mean {' or '.join(repr(c) for c in close)}?"
    elif names:
        shown = ", ".join(names[:20])
        message += f"; known: {shown}{', ...' if len(names) > 20 else ''}"
    else:
        message += "; none are registered"
    return RegistryError(message)


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _require_name(pattern: re.Pattern[str], value: object, what: str, example: str) -> None:
    if not _matches(pattern, value):
        raise RegistryError(f"{what} {value!r} must match {pattern.pattern} (for example {example!r})")


def _require_choice(value: object, choices: Iterable[str], what: str) -> None:
    allowed = tuple(choices)
    if value not in allowed:
        raise RegistryError(f"{what} {value!r} must be one of {', '.join(allowed)}")


def _require_platforms(platforms: frozenset[str] | None, what: str) -> None:
    if platforms is None:
        return
    unknown = sorted(set(platforms) - set(PLATFORMS))
    if unknown or not platforms:
        raise RegistryError(f"{what}: platforms {sorted(platforms)} must be a non-empty subset of {list(PLATFORMS)}")


def _require_impl(impl: str, what: str) -> None:
    module, colon, qualname = impl.partition(":")
    if not colon or not module or not qualname:
        raise RegistryError(f"{what}: implementation {impl!r} must be a class or function, or 'module:qualname'")


def _family_hint(provenance: Provenance) -> str:
    if provenance.plugin in (PROJECT_PLUGIN, BUILTIN_PLUGIN):
        return PROJECT_NAMESPACE
    hint = re.sub(r"[^a-z0-9_]", "_", provenance.plugin.lower()).strip("_")
    if not hint or not hint[0].isalpha():
        hint = f"plugin_{hint}".rstrip("_")
    if hint in BUILTIN_FAMILIES or hint in SESSION_MEMBERS:
        return f"{hint}_ext"
    return hint


def _require_reachable(space: str, what: str, provenance: Provenance) -> None:
    if space in SESSION_MEMBERS and provenance.plugin != BUILTIN_PLUGIN:
        raise RegistryError(
            f"{what}: {space!r} is a member of the session (s.{space}), so s.{space}.<name> would never reach it; "
            f"use a name of your own, for example {_family_hint(provenance)!r}")


def check_op_spec(spec: OpSpec) -> None:
    family, dot, short = spec.name.partition(".")
    if not dot or not _matches(NAME_PATTERN, family) or not _matches(NAME_PATTERN, short):
        raise RegistryError(
            f"op name {spec.name!r} must be 'family.name' with both parts matching {NAME_PATTERN.pattern} "
            "(for example 'acme.open_url')")
    if family != spec.family:
        raise RegistryError(f"op {spec.name!r} is named in family {family!r} but declares family {spec.family!r}")
    require_strict(spec.args, f"op {spec.name!r}", Args)
    if family in BUILTIN_FAMILIES and spec.provenance.plugin != BUILTIN_PLUGIN:
        raise RegistryError(
            f"op {spec.name!r} from {definition_of(spec).describe()} uses the built-in family {family!r}; "
            f"register it in a family of your own, for example '{_family_hint(spec.provenance)}.{short}'")
    _require_reachable(family, f"op {spec.name!r} family", spec.provenance)
    try:
        parse_ref_type(spec.out)
    except RefTypeError as error:
        raise RegistryError(f"op {spec.name!r}: out={spec.out!r} is not a ref type ({error})") from error
    _require_choice(spec.location, get_args(Location), f"op {spec.name!r} location")
    _require_choice(spec.ordering, get_args(Ordering), f"op {spec.name!r} ordering")
    for role in spec.resources:
        _require_choice(role, get_args(ResourceRole), f"op {spec.name!r} resource role")
    _require_platforms(spec.platforms, f"op {spec.name!r}")
    if spec.executor is not None:
        _require_impl(spec.executor, f"op {spec.name!r} executor")
    if spec.pure and spec.idempotent:
        raise RegistryError(
            f"op {spec.name!r} is declared pure and idempotent; idempotent marks a read the engine may run again "
            "(expect.eventually polls it), while a pure op's result is a function of its inputs served from the "
            "cache, so it would never change; drop one of them")
    if (spec.branches or spec.exports_body) and not spec.container:
        raise RegistryError(f"op {spec.name!r}: branches= and exports_body= need container=True")
    for branch in spec.branches:
        _require_name(NAME_PATTERN, branch, f"op {spec.name!r} branch", "body")
    if len(set(spec.branches)) != len(spec.branches):
        raise RegistryError(f"op {spec.name!r}: branch names {list(spec.branches)} repeat")
    for index, example in enumerate(spec.examples):
        _check_example(spec, index, example)


def _check_example(spec: OpSpec, index: int, example: Mapping[str, Any]) -> None:
    what = f"op {spec.name!r} example {index}"
    if not isinstance(example, Mapping):
        raise RegistryError(f"{what} must be a mapping of argument names to JSON values, got {example!r}")
    try:
        json.dumps(example, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RegistryError(f"{what} is not JSON: {error}") from error
    try:
        spec.args.model_validate(dict(example))
    except ValidationError as error:
        raise RegistryError(f"{what} does not validate against {spec.args.__name__}: {error}") from error


def check_macro_spec(spec: MacroSpec) -> None:
    _require_name(NAME_PATTERN, spec.namespace, "macro namespace", "acme")
    if not spec.short.isidentifier() or keyword.iskeyword(spec.short) or spec.short.startswith("_"):
        raise RegistryError(
            f"macro name {spec.short!r} must be a public Python identifier (it is called as s.{spec.namespace}.<name>)")
    if spec.name != f"{spec.namespace}.{spec.short}":
        raise RegistryError(f"macro {spec.name!r} must be named '{spec.namespace}.{spec.short}'")
    if spec.namespace in BUILTIN_FAMILIES and spec.provenance.plugin != BUILTIN_PLUGIN:
        raise RegistryError(
            f"macro {spec.name!r}: namespace {spec.namespace!r} would shadow the built-in s.{spec.namespace}; "
            f"pass namespace= with a name of your own, for example {_family_hint(spec.provenance)!r}")
    _require_reachable(spec.namespace, f"macro {spec.name!r} namespace", spec.provenance)
    _require_platforms(spec.platforms, f"macro {spec.name!r}")


def check_resource_spec(spec: ResourceSpec) -> None:
    _require_name(NAME_PATTERN, spec.kind, "resource kind", "bank_account")
    if spec.parent is not None:
        _require_name(NAME_PATTERN, spec.parent, f"resource {spec.kind!r} parent", "tenant")
    _require_choice(spec.phase, get_args(Phase), f"resource {spec.kind!r} phase")
    from eks_harness.ir.ops.data import Resource

    require_strict(spec.model, f"resource {spec.kind!r}", Resource)
    fields = set(spec.model.model_fields) | {
        info.alias for info in spec.model.model_fields.values() if info.alias}
    missing = [name for name in spec.positional if name not in fields]
    if missing:
        raise RegistryError(
            f"resource {spec.kind!r}: positional names {missing} are not fields of {spec.model.__name__} "
            f"(fields: {', '.join(sorted(fields)) or 'none'})")
    if len(set(spec.positional)) != len(spec.positional):
        raise RegistryError(f"resource {spec.kind!r}: positional names {list(spec.positional)} repeat")
    if spec.persona and not spec.keyed:
        raise RegistryError(f"resource {spec.kind!r}: a persona kind must be keyed (the key is the persona name)")
    if spec.create is not None:
        _require_impl(spec.create, f"resource {spec.kind!r} create")


def create_hook(model: type[BaseModel], kind: str) -> str | None:
    hook = inspect.getattr_static(model, CREATE_HOOK, None)
    if hook is None:
        return None
    if not inspect.isfunction(hook):
        raise RegistryError(
            f"resource {kind!r}: {model.__name__}.{CREATE_HOOK} must be a plain method `def {CREATE_HOOK}(self, ctx)`, "
            f"got {type(hook).__name__}")
    parameters = list(inspect.signature(hook).parameters.values())
    positional = [parameter for parameter in parameters
                  if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    required = [parameter for parameter in parameters[2:] if parameter.default is inspect.Parameter.empty
                and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    if len(positional) < 2 or required:
        raise RegistryError(
            f"resource {kind!r}: {model.__name__}.{CREATE_HOOK} must take the instance and the creation context, "
            f"`def {CREATE_HOOK}(self, ctx)`, got {CREATE_HOOK}{inspect.signature(hook)}")
    return callable_ref(hook)


def check_check_spec(spec: CheckSpec) -> None:
    _require_name(NAME_PATTERN, spec.kind, "check kind", "layout_spill")
    require_strict(spec.args, f"check {spec.kind!r}", Args)
    _require_choice(spec.family, get_args(CheckFamily), f"check {spec.kind!r} family")
    _require_platforms(spec.platforms, f"check {spec.kind!r}")
    if spec.impl is not None:
        _require_reference(spec.impl, f"check {spec.kind!r} implementation")
    if spec.device_dependent and spec.family != "media":
        raise RegistryError(
            f"check {spec.kind!r}: device_dependent=True keys cached check.media results by machine; it applies to "
            f"media checks only, not to {spec.family} checks, which run in a session and are never cached")


def _require_reference(reference: object, what: str) -> None:
    if not isinstance(reference, str) or not reference.strip():
        raise RegistryError(f"{what} must be a callable or a non-empty reference ('module:qualname' or a worker "
                            f"script path), got {reference!r}")
    if ":" in reference and not reference.endswith(SCRIPT_SUFFIXES):
        _require_impl(reference, what)


def require_implementation(impl: object, protocols: tuple[type, ...], what: str) -> None:
    names = " or ".join(f"eks_harness.plugins.sdk.{protocol.__name__}" for protocol in protocols)
    if not isinstance(impl, type):
        raise RegistryError(f"{what} decorates a class implementing {names}, got {type(impl).__name__} {impl!r}")
    gaps = {protocol: sorted(name for name in get_protocol_members(protocol)
                             if not callable(getattr(impl, name, None))) for protocol in protocols}
    if any(not missing for missing in gaps.values()):
        return
    closest = min(protocols, key=lambda protocol: len(gaps[protocol]))
    raise RegistryError(f"{what}: class {impl.__qualname__} does not implement "
                        f"eks_harness.plugins.sdk.{closest.__name__}; it lacks {', '.join(gaps[closest])}")


def catalog_base(category: str) -> type[BaseModel] | None:
    match category:
        case "effect":
            from eks_harness.ir.catalog.effects import EffectBase

            return EffectBase
        case "transition":
            from eks_harness.ir.catalog.transitions import TransitionBase

            return TransitionBase
        case "easing":
            from eks_harness.ir.catalog.easing import EasingBase

            return EasingBase
        case "curve":
            from eks_harness.ir.catalog.curves import CurveBase

            return CurveBase
        case "overlay":
            from eks_harness.ir.ops.overlay import OverlayBase

            return OverlayBase
        case "marker":
            from eks_harness.ir.catalog.markers import MarkerBase

            return MarkerBase
    return None


def check_catalog_spec(spec: CatalogSpec, registry: Registry) -> None:
    _require_choice(spec.category, get_args(Category), "catalog category")
    _require_name(NAME_PATTERN, spec.kind, f"{spec.category} kind", "zoom")
    base = catalog_base(spec.category)
    require_strict(spec.model, f"{spec.category} kind {spec.kind!r}", base,
                   f"{base.__module__}.{base.__name__}" if base is not None else None)
    if spec.category == "target":
        _check_target_kind(spec, registry)
    else:
        declared = model_kind(spec.model)
        if declared != spec.kind:
            raise RegistryError(
                f"{spec.category} {spec.kind!r}: the model {spec.model.__name__} declares kind {declared!r}")
    unknown = sorted(set(spec.renderers) - set(OVERLAY_RENDERERS))
    if unknown:
        raise RegistryError(f"{spec.category} {spec.kind!r}: renderer slots {unknown} must be among "
                            f"{', '.join(OVERLAY_RENDERERS)}")
    for slot, impl in spec.renderers.items():
        if not isinstance(impl, str) or not impl:
            raise RegistryError(f"{spec.category} {spec.kind!r}: the {slot} renderer must be a non-empty reference")
    if spec.resolvers and spec.category != "target":
        raise RegistryError(f"{spec.category} {spec.kind!r}: resolvers belong to target kinds only")
    if spec.device_dependent and spec.category != "effect":
        raise RegistryError(
            f"{spec.category} {spec.kind!r}: device_dependent=True keys cached edit.render results by the inference "
            f"device, model and weights; it applies to effects only, not to {spec.category} kinds")
    _require_platforms(spec.platforms, f"{spec.category} {spec.kind!r}")


def _check_target_kind(spec: CatalogSpec, registry: Registry) -> None:
    unknown = sorted(set(spec.surfaces) - set(SURFACES))
    if unknown or not spec.surfaces:
        raise RegistryError(
            f"target kind {spec.kind!r}: surfaces {sorted(spec.surfaces)} must be a non-empty subset of {list(SURFACES)}")
    undeclared = sorted(set(spec.resolvers) - set(spec.surfaces))
    if undeclared:
        raise RegistryError(f"target kind {spec.kind!r}: resolvers for {undeclared} name surfaces the kind does not "
                            f"declare ({', '.join(sorted(spec.surfaces))})")
    for surface, resolver in spec.resolvers.items():
        _require_reference(resolver, f"target kind {spec.kind!r} {surface} resolver")
    if spec.shorthand is None:
        return
    _require_name(SHORTHAND_PATTERN, spec.shorthand, f"target kind {spec.kind!r} shorthand", "acme")
    for other in registry.catalogs("target"):
        if other.kind != spec.kind and other.shorthand == spec.shorthand:
            raise RegistryError(
                f"target kind {spec.kind!r}: shorthand {spec.shorthand!r} is already used by target kind "
                f"{other.kind!r} from {definition_of(other).describe()}")


def check_preset_spec(spec: PresetSpec) -> None:
    _require_name(NAME_PATTERN, spec.category, "preset category", "viewport")
    _require_name(PRESET_NAME_PATTERN, spec.name, f"{spec.category} preset name", "slow-3g")
    declared = getattr(type(spec.value), "preset_category", "")
    if declared and declared != spec.category:
        raise RegistryError(
            f"preset {spec.name!r}: a {type(spec.value).__name__} belongs to category {declared!r}, "
            f"not {spec.category!r}")


def check_entry_spec(spec: EntrySpec) -> None:
    _require_choice(spec.kind, ENTRY_KINDS, "entry kind")
    _require_name(ENTRY_NAME_PATTERN, spec.name, f"{spec.kind} name", "checkout-flow")


_TABLES = (
    "_ops", "_macros", "_resources", "_checks", "_catalog", "_renderers", "_backends", "_policies", "_presets",
    "_entries", "_apps",
)
_TABLE_LABELS: Mapping[str, str] = {
    "_ops": "op", "_macros": "macro", "_resources": "resource kind", "_checks": "check kind",
    "_catalog": "catalog kind", "_renderers": "renderer for media kind", "_backends": "backend",
    "_policies": "backend policy", "_presets": "preset", "_entries": "entry", "_apps": "app runtime",
}


class Registry:
    def __init__(self) -> None:
        self._ops: dict[str, OpSpec] = {}
        self._macros: dict[str, MacroSpec] = {}
        self._resources: dict[str, ResourceSpec] = {}
        self._checks: dict[str, CheckSpec] = {}
        self._catalog: dict[tuple[str, str], CatalogSpec] = {}
        self._renderers: dict[str, RendererSpec] = {}
        self._backends: dict[str, BackendSpec] = {}
        self._policies: dict[str, BackendPolicySpec] = {}
        self._presets: dict[tuple[str, str], PresetSpec] = {}
        self._entries: dict[tuple[str, str], EntrySpec] = {}
        self._apps: dict[str, AppSpec] = {}
        self._base: RegistryState | None = None

    def register_op(self, spec: OpSpec) -> OpSpec:
        check_op_spec(spec)
        return _store(self._ops, spec.name, spec, f"op {spec.name!r}")

    def register_macro(self, spec: MacroSpec) -> MacroSpec:
        check_macro_spec(spec)
        return _store(self._macros, spec.name, spec, f"macro {spec.name!r}")

    def register_resource(self, spec: ResourceSpec) -> ResourceSpec:
        check_resource_spec(spec)
        return _store(self._resources, spec.kind, spec, f"resource kind {spec.kind!r}")

    def register_check(self, spec: CheckSpec) -> CheckSpec:
        check_check_spec(spec)
        return _store(self._checks, spec.kind, spec, f"check kind {spec.kind!r}")

    def register_catalog(self, spec: CatalogSpec) -> CatalogSpec:
        check_catalog_spec(spec, self)
        return _store(self._catalog, (spec.category, spec.kind), spec, f"{spec.category} kind {spec.kind!r}")

    def register_renderer(self, spec: RendererSpec) -> RendererSpec:
        _require_name(COMPONENT_PATTERN, spec.media_kind, "renderer media kind", "blender")
        _require_impl(spec.impl, f"renderer {spec.media_kind!r}")
        return _store(self._renderers, spec.media_kind, spec, f"renderer for media kind {spec.media_kind!r}")

    def register_backend(self, spec: BackendSpec) -> BackendSpec:
        _require_name(COMPONENT_PATTERN, spec.name, "backend name", "eksenapi")
        _require_impl(spec.impl, f"backend {spec.name!r}")
        return _store(self._backends, spec.name, spec, f"backend {spec.name!r}")

    def register_backend_policy(self, spec: BackendPolicySpec) -> BackendPolicySpec:
        _require_name(COMPONENT_PATTERN, spec.name, "backend policy name", "auto")
        _require_impl(spec.impl, f"backend policy {spec.name!r}")
        return _store(self._policies, spec.name, spec, f"backend policy {spec.name!r}")

    def register_app(self, spec: AppSpec) -> AppSpec:
        _require_impl(spec.impl, f"app runtime {spec.name!r}")
        return _store(self._apps, spec.name, spec, f"app runtime {spec.name!r}")

    def app(self, name: str) -> AppSpec | None:
        return self._apps.get(name)

    def apps(self) -> list[AppSpec]:
        return sorted(self._apps.values(), key=lambda s: s.name)

    def register_preset(self, spec: PresetSpec) -> PresetSpec:
        check_preset_spec(spec)
        return _store(self._presets, (spec.category, spec.name), spec, f"{spec.category} preset {spec.name!r}")

    def register_entry(self, spec: EntrySpec) -> EntrySpec:
        check_entry_spec(spec)
        return _store(self._entries, (spec.kind, spec.name), spec, f"{spec.kind} {spec.name!r}")

    def op(self, name: str) -> OpSpec:
        spec = self._ops.get(name)
        if spec is None:
            raise unknown_name("op", name, self._ops)
        return spec

    def has_op(self, name: str) -> bool:
        return name in self._ops

    def ops(self, family: str | None = None) -> list[OpSpec]:
        return sorted((s for s in self._ops.values() if family is None or s.family == family), key=lambda s: s.name)

    def families(self) -> list[str]:
        return sorted({s.family for s in self._ops.values()})

    def macro(self, name: str) -> MacroSpec:
        spec = self._macros.get(name)
        if spec is None:
            raise unknown_name("macro", name, self._macros)
        return spec

    def macro_for(self, fn: Callable[..., Any]) -> MacroSpec:
        found = [spec for spec in self._macros.values() if spec.fn is fn]
        if not found:
            label = getattr(fn, "__qualname__", repr(fn))
            raise RegistryError(f"{label!r} is not a registered macro; decorate it with @eks.macro(...) and load the "
                                "module or plugin that defines it")
        if len(found) > 1:
            raise RegistryError(f"{getattr(fn, '__qualname__', repr(fn))!r} is registered as several macros "
                                f"({', '.join(sorted(spec.name for spec in found))}); call the one you mean as "
                                "s.<namespace>.<name>(...)")
        return found[0]

    def macros(self, namespace: str | None = None) -> list[MacroSpec]:
        return sorted((s for s in self._macros.values() if namespace is None or s.namespace == namespace),
                      key=lambda s: s.name)

    def resource(self, kind: str) -> ResourceSpec:
        spec = self._resources.get(kind)
        if spec is None:
            raise unknown_name("resource kind", kind, self._resources)
        return spec

    def resources(self) -> list[ResourceSpec]:
        return sorted(self._resources.values(), key=lambda s: s.kind)

    def check(self, kind: str) -> CheckSpec:
        spec = self._checks.get(kind)
        if spec is None:
            raise unknown_name("check kind", kind, self._checks)
        return spec

    def checks(self, family: CheckFamily | None = None) -> list[CheckSpec]:
        return sorted((s for s in self._checks.values() if family is None or s.family == family), key=lambda s: s.kind)

    def catalog(self, category: str, kind: str) -> CatalogSpec:
        spec = self._catalog.get((category, kind))
        if spec is None:
            raise unknown_name(f"{category} kind", kind, (k for c, k in self._catalog if c == category))
        return spec

    def catalogs(self, category: str) -> list[CatalogSpec]:
        return sorted((s for (c, _), s in self._catalog.items() if c == category), key=lambda s: s.kind)

    def target_kinds(self) -> dict[str, TargetKind]:
        return {
            s.kind: TargetKind(s.kind, s.model, s.surfaces, s.shorthand, s.description)
            for s in self.catalogs("target")
        }

    def renderer(self, media_kind: str) -> RendererSpec:
        spec = self._renderers.get(media_kind)
        if spec is None:
            raise unknown_name("renderer media kind", media_kind, self._renderers)
        return spec

    def renderers(self) -> list[RendererSpec]:
        return sorted(self._renderers.values(), key=lambda s: s.media_kind)

    def backend(self, name: str) -> BackendSpec:
        spec = self._backends.get(name)
        if spec is None:
            raise unknown_name("backend", name, self._backends)
        return spec

    def backends(self) -> list[BackendSpec]:
        return sorted(self._backends.values(), key=lambda s: s.name)

    def backend_policy(self, name: str) -> BackendPolicySpec:
        spec = self._policies.get(name)
        if spec is None:
            raise unknown_name("backend policy", name, self._policies)
        return spec

    def backend_policies(self) -> list[BackendPolicySpec]:
        return sorted(self._policies.values(), key=lambda s: s.name)

    def preset(self, category: str, name: str) -> PresetSpec:
        spec = self._presets.get((category, name))
        if spec is None:
            raise unknown_name(f"{category} preset", name, (n for c, n in self._presets if c == category))
        return spec

    def presets(self, category: str | None = None) -> list[PresetSpec]:
        return sorted((s for s in self._presets.values() if category is None or s.category == category),
                      key=lambda s: (s.category, s.name))

    def entry(self, kind: str, name: str) -> EntrySpec:
        spec = self._entries.get((kind, name))
        if spec is None:
            raise unknown_name(kind, name, (n for k, n in self._entries if k == kind))
        return spec

    def entries(self, kind: str | None = None) -> list[EntrySpec]:
        return sorted((s for s in self._entries.values() if kind is None or s.kind == kind),
                      key=lambda s: (s.kind, s.name))

    def drop_module(self, module: str, plugin: str | None = None) -> list[AnySpec]:
        dropped: list[AnySpec] = []
        for name in _TABLES:
            table: dict[Any, AnySpec] = getattr(self, name)
            kept: dict[Any, AnySpec] = {}
            for key, spec in table.items():
                if defined_in(spec, module, plugin):
                    dropped.append(spec)
                else:
                    kept[key] = spec
            setattr(self, name, kept)
        return dropped

    def rollback(self, state: RegistryState, module: str, plugin: str | None = None) -> None:
        for name in _TABLES:
            current: dict[Any, AnySpec] = getattr(self, name)
            restored: dict[Any, AnySpec] = dict(state.tables[name])
            for key, spec in current.items():
                if restored.get(key) is not spec and not defined_in(spec, module, plugin):
                    restored[key] = spec
            for key, spec in state.tables[name].items():
                if key not in current and not defined_in(spec, module, plugin):
                    del restored[key]
            setattr(self, name, restored)

    def drop_entries(self, module: str) -> list[EntrySpec]:
        dropped = [spec for spec in self._entries.values() if spec.module == module]
        self._entries = {key: spec for key, spec in self._entries.items() if spec.module != module}
        return sorted(dropped, key=lambda s: (s.kind, s.name))

    def entry_for(self, fn: Callable[..., Any]) -> EntrySpec:
        key = vars(fn).get("__eks_entry__") if hasattr(fn, "__dict__") else None
        label = getattr(fn, "__qualname__", repr(fn))
        if not isinstance(key, tuple):
            raise RegistryError(
                f"{label!r} is not a registered flow, seed, edit, scene or score; decorate it with @eks.flow, "
                "@eks.seed, @eks.edit, @eks.scene or @eks.score")
        spec = self._entries.get(key)
        if spec is None:
            raise RegistryError(
                f"{label!r} was registered as {key[0]} {key[1]!r} but the registry no longer holds it; load its "
                "file again (eks.discovery.load_file)")
        return spec

    def versions(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for table in _TABLES:
            for spec in getattr(self, table).values():
                provenance = getattr(spec, "provenance", None)
                if isinstance(provenance, Provenance):
                    found[provenance.plugin] = provenance.version
        return dict(sorted(found.items()))

    def snapshot(self) -> RegistryState:
        return RegistryState({name: dict(getattr(self, name)) for name in _TABLES})

    def adopt(self, contributions: Iterable[tuple[str, Any, AnySpec]]) -> None:
        for table, key, spec in contributions:
            if table not in _TABLES:
                raise RegistryError(f"registry table {table!r} is not one of {', '.join(_TABLES)}")
            rows: dict[Any, AnySpec] = getattr(self, table)
            _store(rows, key, spec, f"{_TABLE_LABELS[table]} {key!r}")

    def rebase(self, base: Registry) -> None:
        current = base.snapshot()
        previous = self._base
        for name in _TABLES:
            before: Mapping[Any, AnySpec] = previous.tables[name] if previous is not None else {}
            merged: dict[Any, AnySpec] = dict(current.tables[name])
            for key, spec in getattr(self, name).items():
                if before.get(key) is not spec:
                    merged[key] = spec
            setattr(self, name, merged)
        self._base = current

    def restore(self, state: RegistryState) -> None:
        for name in _TABLES:
            setattr(self, name, dict(state.tables[name]))


REGISTRY = Registry()
_BUILTINS = {"loaded": False}
BUILTIN_MODULES = ("eks_harness.ir.catalog", "eks_harness.ir.ops", "eks_harness.ir.presets")


def ensure_builtins() -> None:
    if _BUILTINS["loaded"]:
        return
    _BUILTINS["loaded"] = True
    token = _ACTIVE.set(None)
    try:
        provenance = Provenance(BUILTIN_PLUGIN, builtin_version())
        for kind in BUILTIN_KINDS.values():
            REGISTRY.register_catalog(CatalogSpec(
                category="target", kind=kind.name, model=kind.args, description=kind.description,
                surfaces=kind.surfaces, shorthand=kind.shorthand, provenance=provenance))
        for module in BUILTIN_MODULES:
            importlib.import_module(module)
    except BaseException:
        _BUILTINS["loaded"] = False
        raise
    finally:
        _ACTIVE.reset(token)


def base_registry() -> Registry:
    ensure_builtins()
    return REGISTRY


def default_registry() -> Registry:
    ensure_builtins()
    active = _ACTIVE.get()
    return active if active is not None else REGISTRY


@contextmanager
def using_registry(registry: Registry) -> Iterator[Registry]:
    ensure_builtins()
    token = _ACTIVE.set(registry)
    try:
        yield registry
    finally:
        _ACTIVE.reset(token)


def registry_for(provenance: Provenance) -> Registry:
    if provenance.plugin == BUILTIN_PLUGIN:
        return REGISTRY
    active = _ACTIVE.get()
    return active if active is not None else REGISTRY


def _target(module: str) -> Registry:
    return registry_for(provenance_for(module))


def op(
    family: str,
    *,
    name: str | None = None,
    out: str = "none",
    version: str = "1",
    description: str = "",
    location: Location = "hub",
    executor: str | Callable[..., Any] | None = None,
    executor_version: str = "",
    pure: bool = False,
    idempotent: bool = False,
    device_dependent: bool = False,
    ordering: Ordering | None = None,
    platforms: Iterable[str] | None = None,
    resources: Iterable[ResourceRole] = (),
    container: bool = False,
    branches: Iterable[str] = (),
    exports_body: bool = False,
    pinnable: bool = False,
    recording_only: bool = False,
    bounded: bool = False,
    examples: Iterable[Mapping[str, Any]] = (),
) -> Callable[[M], M]:
    def register(model: M) -> M:
        require_model(model, "@eks.op")
        _target(model.__module__).register_op(OpSpec(
            name=f"{family}.{name or snake_case(model.__name__)}", family=family, args=model, out=out,
            version=version, description=description, location=location, executor=callable_ref(executor),
            executor_version=executor_version, pure=pure, idempotent=idempotent,
            device_dependent=device_dependent, ordering=ordering or ("graph" if pure else "program"),
            platforms=_platform_set(platforms, "platforms"), resources=_names(resources, "resources"),
            container=container, branches=_names(branches, "branches"), exports_body=exports_body, pinnable=pinnable,
            recording_only=recording_only, bounded=bounded, provenance=provenance_for(model.__module__),
            examples=tuple(dict(example) for example in examples)))
        return model

    return register


def macro(
    name: str | None = None,
    *,
    namespace: str | None = None,
    platforms: Iterable[str] | None = None,
    description: str = "",
    version: str = "1",
) -> Callable[[F], F]:
    def register(fn: F) -> F:
        if not callable(fn) or isinstance(fn, type):
            raise RegistryError(f"@eks.macro decorates a function taking the session first, got {fn!r}")
        provenance = provenance_for(fn.__module__)
        space = namespace or (provenance.plugin.replace(".", "_").replace("-", "_")
                              if provenance.plugin not in (PROJECT_PLUGIN, BUILTIN_PLUGIN) else PROJECT_NAMESPACE)
        short = name or fn.__name__
        registry_for(provenance).register_macro(MacroSpec(
            name=f"{space}.{short}", namespace=space, short=short, fn=fn,
            platforms=_platform_set(platforms, "platforms"), description=description,
            version=version, provenance=provenance))
        return fn

    return register


def resource(
    kind: str,
    *,
    parent: str | None = None,
    phase: Phase = "api",
    keyed: bool = True,
    positional: Iterable[str] = (),
    persona: bool = False,
    description: str = "",
    version: str = "1",
) -> Callable[[M], M]:
    def register(model: M) -> M:
        require_model(model, "@eks.resource")
        _target(model.__module__).register_resource(ResourceSpec(
            kind=kind, model=model, parent=parent, phase=phase, keyed=keyed, positional=_names(positional, "positional"),
            persona=persona, description=description, version=version,
            provenance=provenance_for(model.__module__), create=create_hook(model, kind)))
        return model

    return register


def check(
    kind: str,
    *,
    family: CheckFamily = "checkpoint",
    platforms: Iterable[str] | None = None,
    default: bool = False,
    description: str = "",
    version: str = "1",
    impl: str | Callable[..., Any] | None = None,
    device_dependent: bool = False,
) -> Callable[[M], M]:
    def register(model: M) -> M:
        require_model(model, "@eks.check")
        _target(model.__module__).register_check(CheckSpec(
            kind=kind, family=family, args=model, platforms=_platform_set(platforms, "platforms"),
            default=default, description=description, version=version,
            provenance=provenance_for(model.__module__), impl=callable_ref(impl), device_dependent=device_dependent))
        return model

    return register


def catalog(
    category: Category,
    *,
    description: str = "",
    version: str = "1",
    surfaces: Iterable[str] = (),
    shorthand: str | None = None,
    renderers: Mapping[str, str] | None = None,
    platforms: Iterable[str] | None = None,
    kind: str | None = None,
    resolvers: Mapping[str, str] | None = None,
    device_dependent: bool = False,
) -> Callable[[M], M]:
    def register(model: M) -> M:
        require_model(model, f"@eks.{category}" if category != "target" else "@eks.target_kind")
        _target(model.__module__).register_catalog(CatalogSpec(
            category=category, kind=kind or model_kind(model), model=model, description=description,
            version=version, surfaces=frozenset(_names(surfaces, "surfaces")), shorthand=shorthand,
            renderers=dict(renderers or {}), platforms=_platform_set(platforms, "platforms"),
            provenance=provenance_for(model.__module__), resolvers=dict(resolvers or {}),
            device_dependent=device_dependent))
        return model

    return register


def _catalog_entry[T: type[BaseModel]](
    category: Category, model: T | None, description: str, version: str, device_dependent: bool = False
) -> T | Callable[[T], T]:
    register = catalog(category, description=description, version=version, device_dependent=device_dependent)
    return register if model is None else register(model)


@overload
def effect[T: type[BaseModel]](model: T, /, *, device_dependent: bool = False) -> T: ...


@overload
def effect[T: type[BaseModel]](
    *, description: str = "", version: str = "1", device_dependent: bool = False
) -> Callable[[T], T]: ...


def effect[T: type[BaseModel]](
    model: T | None = None, /, *, description: str = "", version: str = "1", device_dependent: bool = False
) -> T | Callable[[T], T]:
    return _catalog_entry("effect", model, description, version, device_dependent)


@overload
def transition[T: type[BaseModel]](model: T, /) -> T: ...


@overload
def transition[T: type[BaseModel]](*, description: str = "", version: str = "1") -> Callable[[T], T]: ...


def transition[T: type[BaseModel]](
    model: T | None = None, /, *, description: str = "", version: str = "1"
) -> T | Callable[[T], T]:
    return _catalog_entry("transition", model, description, version)


@overload
def easing[T: type[BaseModel]](model: T, /) -> T: ...


@overload
def easing[T: type[BaseModel]](*, description: str = "", version: str = "1") -> Callable[[T], T]: ...


def easing[T: type[BaseModel]](
    model: T | None = None, /, *, description: str = "", version: str = "1"
) -> T | Callable[[T], T]:
    return _catalog_entry("easing", model, description, version)


@overload
def curve[T: type[BaseModel]](model: T, /) -> T: ...


@overload
def curve[T: type[BaseModel]](*, description: str = "", version: str = "1") -> Callable[[T], T]: ...


def curve[T: type[BaseModel]](
    model: T | None = None, /, *, description: str = "", version: str = "1"
) -> T | Callable[[T], T]:
    return _catalog_entry("curve", model, description, version)


@overload
def marker[T: type[BaseModel]](model: T, /) -> T: ...


@overload
def marker[T: type[BaseModel]](*, description: str = "", version: str = "1") -> Callable[[T], T]: ...


def marker[T: type[BaseModel]](
    model: T | None = None, /, *, description: str = "", version: str = "1"
) -> T | Callable[[T], T]:
    return _catalog_entry("marker", model, description, version)


def overlay_kind(
    *,
    live: str | None = None,
    screenshot: str | Callable[..., Any] | None = None,
    video: str | Callable[..., Any] | None = None,
    platforms: Iterable[str] = ("web", "ios", "android"),
    description: str = "",
    version: str = "1",
) -> Callable[[M], M]:
    renderers = {k: v for k, v in (("live", live), ("screenshot", callable_ref(screenshot)),
                                   ("video", callable_ref(video))) if v is not None}
    return catalog("overlay", description=description, version=version, renderers=renderers, platforms=platforms)


def target_kind(
    kind: str,
    *,
    surfaces: Iterable[str],
    shorthand: str | None = None,
    resolvers: Mapping[str, str | Callable[..., Any]] | None = None,
    description: str = "",
    version: str = "1",
) -> Callable[[M], M]:
    refs = {surface: callable_ref(resolver) or "" for surface, resolver in (resolvers or {}).items()}
    return catalog("target", kind=kind, surfaces=surfaces, shorthand=shorthand, description=description,
                   version=version, resolvers=refs)


def renderer(media_kind: str, *, description: str = "") -> Callable[[C], C]:
    def register(impl: C) -> C:
        module = getattr(impl, "__module__", "")
        _target(module).register_renderer(RendererSpec(media_kind, callable_ref(impl) or "", description,
                                                       provenance_for(module)))
        return impl

    return register


BACKEND_IDENTITIES: tuple[str, ...] = get_args(BackendIdentityKind)
MAX_BACKEND_REQUIREMENTS = 16


def requires(name: str, *, key: str | None = None, params: Mapping[str, Any] | None = None) -> BackendRequirement:
    _require_name(COMPONENT_PATTERN, name, "required backend name", "sellersgo-infra")
    if key is not None and (not isinstance(key, str) or not key):
        raise RegistryError(f"requires({name!r}): key must be a non-empty text, got {key!r}")
    if params is not None and not isinstance(params, Mapping):
        raise RegistryError(f"requires({name!r}): params must be a mapping, got {type(params).__name__}")
    plain = dict(params or {})
    try:
        json.dumps(plain)
    except (TypeError, ValueError) as error:
        raise RegistryError(f"requires({name!r}): params must be JSON values: {error}") from None
    return BackendRequirement(name, key, plain)


def _requirements(name: str, items: Iterable[str | BackendRequirement]) -> tuple[BackendRequirement, ...]:
    if isinstance(items, str | BackendRequirement):
        items = (items,)
    found: list[BackendRequirement] = []
    for item in items:
        requirement = requires(item) if isinstance(item, str) else item
        if not isinstance(requirement, BackendRequirement):
            raise RegistryError(f"@eks.backend({name!r}) requires names or eks.requires(...), got {item!r}")
        if requirement.name == name:
            raise RegistryError(f"@eks.backend({name!r}) cannot require itself")
        if any(other.name == requirement.name for other in found):
            raise RegistryError(f"@eks.backend({name!r}) requires {requirement.name!r} twice")
        found.append(requirement)
    if len(found) > MAX_BACKEND_REQUIREMENTS:
        raise RegistryError(f"@eks.backend({name!r}) requires {len(found)} backends; the limit is "
                            f"{MAX_BACKEND_REQUIREMENTS}")
    return tuple(found)


def _idle_seconds(name: str, value: object) -> float | None:
    if value is None:
        return None
    from eks_harness.ir.time import TICKS_PER_SECOND, Time, ensure_duration

    try:
        duration = ensure_duration(value, f"@eks.backend({name!r}) idle_timeout")
    except (TypeError, ValueError) as error:
        raise RegistryError(str(error)) from None
    if not isinstance(duration, Time) or not duration.is_concrete or duration.ticks < 0:
        raise RegistryError(f"@eks.backend({name!r}) idle_timeout must be a wall-clock duration such as t('10m')")
    return duration.ticks / TICKS_PER_SECOND


def backend(name: str, *, description: str = "", requires: Iterable[str | BackendRequirement] = (),
            identity: BackendIdentityKind = "tree", idle_timeout: object = None) -> Callable[[B], B]:
    if identity not in BACKEND_IDENTITIES:
        raise RegistryError(f"@eks.backend({name!r}) identity must be one of {', '.join(BACKEND_IDENTITIES)}, "
                            f"got {identity!r}")
    needs = _requirements(name, requires)
    idle = _idle_seconds(name, idle_timeout)

    def register(impl: B) -> B:
        from eks_harness.plugins.sdk import Backend as BackendImpl
        from eks_harness.plugins.sdk import ManagedBackend

        require_implementation(impl, (BackendImpl, ManagedBackend), f"@eks.backend({name!r})")
        module = getattr(impl, "__module__", "")
        _target(module).register_backend(BackendSpec(name, callable_ref(impl) or "", description, provenance_for(module),
                                                     requires=needs, identity=identity, idle_timeout=idle))
        return impl

    return register


APP_HOOKS = ("prepare", "boot", "login")


def app(name: str, *, description: str = "") -> Callable[[AppT], AppT]:
    def register(impl: AppT) -> AppT:
        if not isinstance(impl, type):
            raise RegistryError(f"@eks.app({name!r}) decorates a class with prepare, boot or login, got {impl!r}")
        if not any(callable(getattr(impl, hook, None)) for hook in APP_HOOKS):
            raise RegistryError(f"@eks.app({name!r}): class {impl.__qualname__} defines none of "
                                f"{', '.join(APP_HOOKS)}")
        module = getattr(impl, "__module__", "")
        _target(module).register_app(AppSpec(name, callable_ref(impl) or "", description, provenance_for(module)))
        return impl

    return register


def implementation(reference: str) -> object | None:
    module_name, _, qualname = reference.partition(":")
    if not module_name or not qualname:
        return None
    found: object | None = sys.modules.get(module_name)
    if found is None:
        try:
            found = importlib.import_module(module_name)
        except ImportError:
            return None
    for part in qualname.split("."):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found


def backend_policy(name: str, *, description: str = "") -> Callable[[P], P]:
    def register(impl: P) -> P:
        from eks_harness.plugins.sdk import BackendPolicy as BackendPolicyImpl

        require_implementation(impl, (BackendPolicyImpl,), f"@eks.backend_policy({name!r})")
        module = getattr(impl, "__module__", "")
        _target(module).register_backend_policy(BackendPolicySpec(name, callable_ref(impl) or "", description,
                                                                  provenance_for(module)))
        return impl

    return register


def preset(name: str, *, category: str | None = None, description: str = "") -> Callable[[F], F]:
    def register(fn: F) -> F:
        value = fn()
        if not isinstance(value, BaseModel):
            raise RegistryError(f"preset {name!r} must return a pydantic model, got {type(value).__name__}")
        kind = category or getattr(type(value), "preset_category", "")
        if not kind:
            raise RegistryError(f"preset {name!r}: pass category= or return a PresetModel subclass")
        _target(fn.__module__).register_preset(PresetSpec(
            category=kind, name=name, value=value, description=description,
            provenance=provenance_for(fn.__module__), source=callable_ref(fn) or ""))
        return fn

    return register


def dispatch(category: str, value: Any) -> Any:
    match value:
        case BaseModel():
            return value
        case {"kind": str() as kind}:
            return default_registry().catalog(category, kind).model.model_validate(value)
        case _:
            raise ValueError(f"expected an object with a string 'kind' naming a registered {category} kind, got {value!r}")


CREATE_OP = "data.create"
BASE_OP = "data.base"
RESOURCE_OPS = frozenset({CREATE_OP, BASE_OP})
RESOURCE_OUT_PREFIX = "resource:"
UNTYPED_OUT = "json"


def expected_out(spec: OpSpec, args: Mapping[str, Any]) -> str | None:
    if spec.name not in RESOURCE_OPS:
        return spec.out
    kind = args.get("kind")
    return RESOURCE_OUT_PREFIX + kind if isinstance(kind, str) else None


def out_problem(spec: OpSpec, args: Mapping[str, Any], out: str) -> str | None:
    expected = expected_out(spec, args)
    if expected is None or out == expected:
        return None
    if spec.name in RESOURCE_OPS:
        return f"{spec.name} of kind {args.get('kind')!r} produces {expected}, not {out}"
    if expected == UNTYPED_OUT and out != "none":
        try:
            parse_ref_type(out)
        except RefTypeError as error:
            return f"{spec.name}: {error}"
        return None
    return (f"{spec.name} produces {expected}, not {out}; out may only repeat the op's output type or narrow an op "
            "that produces untyped json")


@dataclass(frozen=True, slots=True)
class Poly:
    category: str

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_before_validator_function(partial(dispatch, self.category), handler(source))

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        models = [spec.model for spec in default_registry().catalogs(self.category)]
        return {"oneOf": [handler(model.__pydantic_core_schema__) for model in models]}


__all__ = [
    "APP_HOOKS",
    "BASE_OP",
    "BUILTIN_FAMILIES",
    "BUILTIN_MODULES",
    "BUILTIN_PLUGIN",
    "COMPONENT_PATTERN",
    "CREATE_HOOK",
    "CREATE_OP",
    "ENTRY_KINDS",
    "ENTRY_MODULE_PREFIX",
    "ENTRY_NAME_PATTERN",
    "NAME_PATTERN",
    "OVERLAY_RENDERERS",
    "PRESET_NAME_PATTERN",
    "PROJECT_NAMESPACE",
    "PROJECT_PLUGIN",
    "QUALIFIED_PATTERN",
    "REGISTRY",
    "RESERVED_PLUGIN_IDS",
    "RESOURCE_OPS",
    "RESOURCE_OUT_PREFIX",
    "SCOPED_MODULE_PATTERN",
    "SESSION_MEMBERS",
    "SHORTHAND_PATTERN",
    "UNTYPED_OUT",
    "AnySpec",
    "AppSpec",
    "Args",
    "BackendPolicySpec",
    "BackendRequirement",
    "BackendSpec",
    "CatalogSpec",
    "Category",
    "CheckFamily",
    "CheckSpec",
    "Definition",
    "EntryKind",
    "EntrySpec",
    "Location",
    "MacroSpec",
    "OpSpec",
    "Ordering",
    "Phase",
    "Pinnable",
    "Poly",
    "PresetModel",
    "PresetSpec",
    "Provenance",
    "Registry",
    "RegistryError",
    "RegistryState",
    "RendererSpec",
    "ResourceRole",
    "ResourceSpec",
    "app",
    "backend",
    "backend_policy",
    "base_registry",
    "builtin_version",
    "callable_ref",
    "catalog",
    "catalog_base",
    "check",
    "check_catalog_spec",
    "check_check_spec",
    "check_entry_spec",
    "check_macro_spec",
    "check_op_spec",
    "check_preset_spec",
    "check_resource_spec",
    "contributing",
    "create_hook",
    "curve",
    "default_registry",
    "defined_in",
    "definition_of",
    "dispatch",
    "easing",
    "effect",
    "ensure_builtins",
    "expected_out",
    "implementation",
    "macro",
    "marker",
    "model_kind",
    "module_identity",
    "names_of",
    "op",
    "out_problem",
    "overlay_kind",
    "preset",
    "provenance_for",
    "registry_for",
    "renderer",
    "require_implementation",
    "require_model",
    "require_strict",
    "requires",
    "resource",
    "snake_case",
    "source_location",
    "target_kind",
    "transition",
    "unknown_name",
    "using_registry",
]
