from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, get_args

from pydantic import BaseModel, JsonValue, ValidationError

from eks_harness.ir.catalog.animated import Driven
from eks_harness.ir.catalog.keys import Keys
from eks_harness.ir.plan import Binding, CheckUse, Node, Resource, SessionSpec
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    CatalogSpec,
    Category,
    PresetSpec,
    Registry,
    RegistryError,
    catalog_base,
)
from eks_harness.ir.target import Target

CHECK_CATEGORY = "check"
CHECK_MEDIA_OP = "check.media"
SOURCE_OP = "media.source"
RENDERER_CATEGORY = "renderer"
BACKEND_CATEGORY = "backend"
BACKEND_RESOURCE = "backend"
RESOURCE_OPS = frozenset({"data.create", "data.base"})
DYNAMIC_OP = "ctl.dynamic"
TAKE_OP = "score.take"
OP_CATEGORY = "op"
RESOURCE_CATEGORY = "resource"
PERSONA_CATEGORY = "persona"
PERSONA_FIELD = "persona"
LITERAL_BINDING = "literal"
TARGET_KEY = "by"
KIND_KEY = "kind"
KEYS_KEY = "keys"
CURVE_KEY = "curve"
VIEWPORT_CATEGORY = "viewport"
TARGET_CATEGORY = "target"
OVERLAY_CATEGORY = "overlay"
STYLE_CATEGORY = "style"

PRESET_FIELDS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "capture.shot": (("viewport", "viewport"),),
    "capture.dump": (("viewport", "viewport"),),
    "capture.record": (("viewport", "viewport"), ("pace", "pace")),
    "check.checkpoint": (("viewport", "viewport"),),
    "web.viewport": (("name", "viewport"),),
    "ui.type": (("pace", "pace"),),
    "net.shape": (("profile", "network"),),
    "overlay.annotate": (("style", "style"),),
}
SESSION_PRESETS = (("viewport", "viewport"), ("pace", "pace"))
UNVERSIONED_PLUGINS = frozenset({BUILTIN_PLUGIN, PROJECT_PLUGIN})


@dataclass(frozen=True, slots=True)
class SessionScope:
    viewports: frozenset[str] = frozenset()
    seeded: bool = False


NO_SESSION = SessionScope()


@dataclass(frozen=True, slots=True)
class Contribution:
    category: str
    kind: str
    version: str
    plugin: str
    plugin_version: str
    value: str | None = None

    @property
    def preset(self) -> bool:
        return self.value is not None

    def sort_key(self) -> tuple[str, str, str, str, str, str]:
        return (self.category, self.kind, self.version, self.plugin, self.plugin_version, self.value or "")


def preset_digest(spec: PresetSpec) -> str:
    from eks_harness.ir.hashing import canonical_json, digest

    return digest(canonical_json(spec.value))


class JsonDecoder:
    def __init__(self, registry: Registry) -> None:
        self.catalog: dict[type[BaseModel], CatalogSpec] = {}
        self.kinds: dict[str, list[CatalogSpec]] = {}
        for category in get_args(Category):
            if category == TARGET_CATEGORY:
                continue
            for spec in registry.catalogs(category):
                self.catalog[spec.model] = spec
                self.kinds.setdefault(spec.kind, []).append(spec)
        self.check_kinds = frozenset(spec.kind for spec in registry.checks())
        self.check_args: dict[str, type[BaseModel]] = {spec.kind: spec.args for spec in registry.checks()}
        self.media_check = registry.op(CHECK_MEDIA_OP).args if registry.has_op(CHECK_MEDIA_OP) else None

    def check_params(self, value: BaseModel) -> BaseModel | None:
        if isinstance(value, CheckUse):
            kind: object = value.kind
            params: object = value.params
        elif self.media_check is not None and type(value) is self.media_check:
            kind = getattr(value, KIND_KEY, None)
            params = getattr(value, "params", None)
        else:
            return None
        model = self.check_args.get(kind) if isinstance(kind, str) else None
        if model is None or not isinstance(params, Mapping):
            return None
        try:
            return model.model_validate(dict(cast("Mapping[str, Any]", params)))
        except ValidationError:
            return None

    def models(self, mapping: Mapping[str, Any]) -> list[BaseModel]:
        found: list[BaseModel] = []
        data = dict(mapping)
        if isinstance(mapping.get(TARGET_KEY), str):
            _decode(Target, data, found)
        if KEYS_KEY in mapping:
            _decode(Keys[JsonValue], data, found)
        if CURVE_KEY in mapping:
            _decode(Driven, data, found)
        kind = mapping.get(KIND_KEY)
        if isinstance(kind, str):
            for spec in self.kinds.get(kind, ()):
                _decode(spec.model, data, found)
            if kind in self.check_kinds:
                _decode(CheckUse, data, found)
        return found


def _decode(model: type[BaseModel], data: dict[str, Any], found: list[BaseModel]) -> None:
    try:
        found.append(model.model_validate(data))
    except ValidationError:
        return


class _Walker:
    def __init__(self, registry: Registry, scope: SessionScope) -> None:
        self.registry = registry
        self.scope = scope
        self.found: set[Contribution] = set()
        self.decoder = JsonDecoder(registry)
        self.catalog = self.decoder.catalog
        self.preset_names: dict[str, list[str]] = {}
        for spec in registry.presets():
            self.preset_names.setdefault(spec.name, []).append(spec.category)
        self.overlay_base = catalog_base(OVERLAY_CATEGORY)
        self.media_check = registry.op(CHECK_MEDIA_OP).args if registry.has_op(CHECK_MEDIA_OP) else None
        self.dynamic = registry.op(DYNAMIC_OP).args if registry.has_op(DYNAMIC_OP) else None

    def kind(self, category: str, kind: str, version: str, plugin: str, plugin_version: str) -> None:
        self.found.add(Contribution(category, kind, version, plugin, plugin_version))

    def preset(self, category: str, name: object, viewports: Collection[str]) -> None:
        if not isinstance(name, str):
            return
        if category == VIEWPORT_CATEGORY and name in viewports:
            return
        try:
            spec = self.registry.preset(category, name)
        except RegistryError:
            return
        self.found.add(Contribution(category, name, "", spec.provenance.plugin, spec.provenance.version,
                                    preset_digest(spec)))

    def persona(self, name: object, seeded: bool) -> None:
        if not seeded:
            self.preset(PERSONA_CATEGORY, name, ())

    def target(self, target: Target) -> None:
        try:
            spec = self.registry.catalog(TARGET_CATEGORY, target.by)
        except RegistryError:
            return
        self.kind(TARGET_CATEGORY, spec.kind, spec.version, spec.provenance.plugin, spec.provenance.version)
        try:
            validated = spec.model.model_validate(target.args)
        except ValidationError:
            return
        self.walk(validated)

    def op_own(self, op: str, model: BaseModel) -> None:
        spec = self.registry.op(op)
        if spec.provenance.plugin not in UNVERSIONED_PLUGINS:
            self.kind(OP_CATEGORY, op, spec.version, spec.provenance.plugin, spec.provenance.version)
        for field, category in PRESET_FIELDS.get(op, ()):
            self.preset(category, getattr(model, field, None), self.scope.viewports)
        if PERSONA_FIELD in type(model).model_fields:
            self.persona(getattr(model, PERSONA_FIELD), self.scope.seeded)
        if op in RESOURCE_OPS:
            self.resource(getattr(model, KIND_KEY, None))
        if op == SOURCE_OP:
            self.renderer(getattr(model, RENDERER_CATEGORY, None))

    def renderer(self, media_kind: object) -> None:
        if not isinstance(media_kind, str):
            return
        try:
            spec = self.registry.renderer(media_kind)
        except RegistryError:
            return
        self.kind(RENDERER_CATEGORY, spec.media_kind, "", spec.provenance.plugin, spec.provenance.version)

    def resource(self, kind: object) -> None:
        if not isinstance(kind, str):
            return
        try:
            spec = self.registry.resource(kind)
        except RegistryError:
            return
        self.kind(RESOURCE_CATEGORY, spec.kind, spec.version, spec.provenance.plugin, spec.provenance.version)

    def json(self, value: Mapping[Any, Any]) -> None:
        keys = cast("Mapping[str, Any]", value)
        for model in self.decoder.models(keys):
            self.model(model)
        for item in value.values():
            self.walk(item)

    def names(self, value: object) -> None:
        if isinstance(value, str):
            for category in self.preset_names.get(value, ()):
                self.preset(category, value, self.scope.viewports)
        elif isinstance(value, Mapping):
            for item in cast("Mapping[Any, Any]", value).values():
                self.names(item)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in cast("Collection[Any]", value):
                self.names(item)

    def check(self, kind: str, params: Mapping[str, Any]) -> None:
        try:
            spec = self.registry.check(kind)
        except RegistryError:
            return
        self.kind(CHECK_CATEGORY, spec.kind, spec.version, spec.provenance.plugin, spec.provenance.version)
        try:
            validated = spec.args.model_validate(dict(params))
        except ValidationError:
            return
        self.walk(validated)

    def session(self, session: SessionSpec) -> None:
        for field, category in SESSION_PRESETS:
            self.preset(category, getattr(session, field), session.viewports)
        self.persona(session.persona, session.seed is not None)

    def walk(self, value: object) -> None:
        if isinstance(value, BaseModel):
            self.model(value)
        elif isinstance(value, Mapping):
            self.json(cast("Mapping[Any, Any]", value))
        elif isinstance(value, list | tuple | set | frozenset):
            for item in cast("Collection[Any]", value):
                self.walk(item)

    def model(self, value: BaseModel) -> None:
        spec = self.catalog.get(type(value))
        if spec is not None:
            self.kind(spec.category, spec.kind, spec.version, spec.provenance.plugin, spec.provenance.version)
        if isinstance(value, Target):
            self.target(value)
        elif isinstance(value, CheckUse):
            self.check(value.kind, value.params)
        elif self.media_check is not None and type(value) is self.media_check:
            kind = getattr(value, "kind", None)
            params = getattr(value, "params", None)
            if isinstance(kind, str) and isinstance(params, Mapping):
                self.check(kind, cast("Mapping[str, Any]", params))
        elif isinstance(value, SessionSpec):
            self.session(value)
        elif isinstance(value, Binding):
            if value.kind == LITERAL_BINDING:
                self.names(value.value)
        elif self.dynamic is not None and type(value) is self.dynamic:
            self.names(getattr(value, "inputs", None))
        if self.overlay_base is not None and isinstance(value, self.overlay_base):
            self.preset(STYLE_CATEGORY, getattr(value, "style", None), ())
        for name in type(value).model_fields:
            self.walk(getattr(value, name))

    def result(self) -> list[Contribution]:
        return sorted(self.found, key=Contribution.sort_key)


def contributions(value: object, registry: Registry, *, op: str | None = None,
                  scope: SessionScope = NO_SESSION) -> list[Contribution]:
    walker = _Walker(registry, scope)
    if op is not None and isinstance(value, BaseModel) and registry.has_op(op):
        walker.op_own(op, value)
    walker.walk(value)
    return walker.result()


def resource_contributions(resource: Resource, registry: Registry) -> list[Contribution]:
    if resource.kind != BACKEND_RESOURCE or resource.definition is None:
        return []
    try:
        spec = registry.backend(resource.definition)
    except RegistryError:
        return []
    return [Contribution(BACKEND_CATEGORY, spec.name, "", spec.provenance.plugin, spec.provenance.version)]


def node_args(node: Node, registry: Registry) -> BaseModel | None:
    if not registry.has_op(node.op):
        return None
    try:
        return registry.op(node.op).args.model_validate(node.args)
    except ValidationError:
        return None


def node_contributions(node: Node, registry: Registry, *, scope: SessionScope = NO_SESSION,
                       model: BaseModel | None = None) -> list[Contribution] | None:
    validated = model if model is not None else node_args(node, registry)
    if validated is None:
        return None
    return contributions(validated, registry, op=node.op, scope=scope)


def take_session(node: Node) -> SessionSpec | None:
    if node.op != TAKE_OP:
        return None
    try:
        return SessionSpec.model_validate(node.args.get("session"))
    except ValidationError:
        return None


def session_scope(session: SessionSpec | None) -> SessionScope:
    if session is None:
        return NO_SESSION
    return SessionScope(frozenset(session.viewports), session.seed is not None)


def inner_scope(node: Node, outer: SessionScope) -> SessionScope:
    if node.op != TAKE_OP:
        return outer
    return session_scope(take_session(node))


def walk_scoped(nodes: Sequence[Node], scope: SessionScope) -> Iterator[tuple[Node, SessionScope]]:
    for node in nodes:
        yield node, scope
        if node.branches is not None:
            inner = inner_scope(node, scope)
            for children in node.branches.values():
                yield from walk_scoped(children, inner)


__all__ = [
    "BACKEND_CATEGORY",
    "CHECK_CATEGORY",
    "NO_SESSION",
    "OP_CATEGORY",
    "PERSONA_CATEGORY",
    "PERSONA_FIELD",
    "PRESET_FIELDS",
    "RENDERER_CATEGORY",
    "RESOURCE_CATEGORY",
    "SESSION_PRESETS",
    "UNVERSIONED_PLUGINS",
    "Contribution",
    "JsonDecoder",
    "SessionScope",
    "contributions",
    "inner_scope",
    "node_args",
    "node_contributions",
    "preset_digest",
    "resource_contributions",
    "session_scope",
    "take_session",
    "walk_scoped",
]
