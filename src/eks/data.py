from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from eks.planning import seed_plan
from eks.ref import SENTINEL_CLOSE, SENTINEL_OPEN, Json, Ref, encode, node_of, to_expr
from eks.tracing import TraceContext, TraceError
from eks_harness.ir.entries import SeedOptions
from eks_harness.ir.ops.data import (
    Resource,
    ResourceRef,
    keyless_position,
    referred_kind,
    refers_to,
    resource_path,
    resource_segment,
)
from eks_harness.ir.plan import Plan
from eks_harness.ir.refs import RefError, RefExpr, is_expr_dict, parse_expr, ref_kind_of
from eks_harness.ir.registry import (
    ENTRY_NAME_PATTERN,
    EntrySpec,
    RegistryError,
    ResourceSpec,
)

CREATE_OP = "data.create"
EXTEND_OP = "data.extend"
BASE_OP = "data.base"
_EXTENDING: ContextVar[tuple[str, ...]] = ContextVar("eks_seed_extending", default=())


@dataclass(frozen=True, slots=True)
class BaseResource:
    base: str
    origin: str
    kind: str
    key: str | None
    path: str
    persona: bool


class ResourceHandle:
    def __init__(self, data: Data, kind: str, key: str | None, path: str, ref: Ref[Json]) -> None:
        self._data = data
        self._kind = kind
        self._key = key
        self._path = path
        self._ref = ref

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def key(self) -> str | None:
        return self._key

    @property
    def path(self) -> str:
        return self._path

    @property
    def ref(self) -> Ref[Json]:
        return self._ref

    @property
    def expr(self) -> RefExpr:
        found = to_expr(self._ref)
        if not isinstance(found, RefExpr):
            raise TraceError(f"resource {self._path!r} has no node reference")
        return found

    def __eks_encode__(self) -> Any:
        return encode(self._ref)

    def __getattr__(self, name: str) -> ResourceFactory:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data._factory(name, self)

    def __repr__(self) -> str:
        return f"ResourceHandle({self._path})"


class ResourceFactory:
    def __init__(self, data: Data, kind: str, parent: ResourceHandle | None) -> None:
        self._data = data
        self._kind = kind
        self._parent = parent

    def __call__(self, *args: Any, **fields: Any) -> ResourceHandle:
        spec = self._data._spec(self._kind)
        owner = self._parent.kind if self._parent is not None else "d"
        call = f"{owner}.{self._kind}(...)"
        positional = list(args)
        key: str | None = None
        if spec.keyed:
            if not positional:
                raise TypeError(
                    f"{call} needs the resource key as its first argument, for example "
                    f"{owner}.{self._kind}('my-{self._kind.replace('_', '-')}', ...)"
                )
            key = _key(positional.pop(0), call)
        if len(positional) > len(spec.positional):
            expected = ", ".join(spec.positional) or "none"
            raise TypeError(
                f"{call} takes {len(spec.positional)} positional field(s) after the key ({expected}), "
                f"got {len(positional)}; pass the other fields by keyword"
            )
        for name, value in zip(spec.positional, positional, strict=False):
            if name in fields:
                raise TypeError(f"{call} got field {name!r} both positionally and by keyword")
            fields[name] = value
        return self._data._declare(spec, key, self._parent, fields)


class Data:
    def __init__(self, ctx: TraceContext, entry: EntrySpec) -> None:
        self._ctx = ctx
        self._entry = entry
        options = entry.options
        if not isinstance(options, SeedOptions):
            raise TraceError(f"{entry.kind} {entry.name!r} is not a seed; eks.Data is the root builder of @eks.seed")
        self._options = options
        self._backend = ctx.resource("backend", definition=options.backend, seed=entry.name)
        self._root_scope = ctx.scope()
        self._paths: set[str] = set()
        self._personas: dict[str, str] = {}
        self._keyless: dict[tuple[str | None, str], int] = {}
        self._extends: dict[str, Ref[None]] = {}
        self._declared: dict[str, ResourceHandle] = {}
        self._base_resources: dict[str, BaseResource] = {}
        self._base_personas: dict[str, BaseResource] = {}
        self._base_handles: dict[str, ResourceHandle] = {}

    @property
    def now(self) -> str | None:
        return self._ctx.clock.now

    @property
    def seed(self) -> int:
        return self._ctx.clock.seed

    def extend(self, base: str) -> Ref[None]:
        if not isinstance(base, str) or ENTRY_NAME_PATTERN.fullmatch(base) is None:
            raise ValueError(f"d.extend expects the name of another seed such as 'standard', got {base!r}")
        if base == self._entry.name:
            raise TraceError(f"seed {base!r} cannot extend itself; name another seed as its base")
        scope = self._ctx.scope()
        if scope is not self._root_scope:
            raise TraceError(
                f"d.extend({base!r}) is called inside {scope.key!r} in seed {self._entry.name!r}; d.extend is "
                "declared at the top level of the seed because a base snapshot is restored once, not per "
                "iteration or under a runtime condition, so move it out of eks.each, eks.when or @eks.dynamic"
            )
        existing = self._extends.get(base)
        if existing is not None:
            return existing
        if self._paths:
            raise TraceError(
                f"d.extend({base!r}) comes after resources of seed {self._entry.name!r}; the base snapshot is "
                "restored before the seed creates anything, so call d.extend before the first resource"
            )
        args: dict[str, Any] = {"base": base}
        plan = _base_plan(self._ctx, base, self._entry.name)
        if plan is not None:
            args["hash"] = plan.hash
            self._learn_base(base, plan)
        ref: Ref[None] = self._ctx.record(EXTEND_OP, args, uses=[self._backend], id_key=f"{EXTEND_OP}:{base}")
        self._extends[base] = ref
        return ref

    def base(self, path: str) -> ResourceHandle:
        if not isinstance(path, str):
            raise TypeError(f"d.base() takes the path of a base resource such as 'tenant:harman', got {path!r}")
        known = self._base_handles.get(path)
        if known is not None:
            return known
        resource = self._base_resources.get(path)
        if resource is None:
            if not self._extends:
                raise TraceError(f"d.base({path!r}) in seed {self._entry.name!r} needs a base seed; call "
                                 "d.extend(base) first")
            close = difflib.get_close_matches(path, sorted(self._base_resources), n=3, cutoff=0.6)
            hint = f"; did you mean {' or '.join(repr(item) for item in close)}?" if close else ""
            raise TraceError(f"no base seed of {self._entry.name!r} ({', '.join(self._extends)}) creates resource "
                             f"{path!r}{hint}")
        scope = self._ctx.scope()
        if scope is not self._root_scope:
            raise TraceError(f"d.base({path!r}) is called inside {scope.key!r} in seed {self._entry.name!r}; look up "
                             "base resources at the top level of the seed")
        args: dict[str, Any] = {"base": resource.base, "kind": resource.kind, "path": path}
        if resource.key is not None:
            args["key"] = resource.key
        ref: Ref[Json] = self._ctx.record(BASE_OP, args, uses=[self._backend], out=f"resource:{resource.kind}",
                                          id_key=f"{BASE_OP}:{path}")
        node = self._ctx.node(node_of(ref))
        restored = node_of(self._extends[resource.base])
        if restored not in node.after:
            node.after.append(restored)
        try:
            self._ctx.require(self._spec(resource.kind).provenance)
        except RegistryError as error:
            raise TraceError(f"d.base({path!r}): {error}") from error
        handle = ResourceHandle(self, resource.kind, resource.key, path, ref)
        self._declared[node_of(ref)] = handle
        self._base_handles[path] = handle
        return handle

    def _learn_base(self, base: str, plan: Plan) -> None:
        for resource in _chain_resources(self._ctx, base, plan, {self._entry.name}):
            known = self._base_resources.setdefault(resource.path, resource)
            if known.origin != resource.origin:
                raise TraceError(
                    f"seed {self._entry.name!r} extends {known.base!r} and {base!r}, and both create resource "
                    f"{resource.path!r} (in seeds {known.origin!r} and {resource.origin!r}); the composed seed would "
                    "hold two resources with one path, so their generated ids and numbers would collide; extend one "
                    "of them, or move the shared resource into a seed both extend"
                )
            if resource.persona and resource.key is not None:
                persona = self._base_personas.setdefault(resource.key, resource)
                if persona.origin != resource.origin:
                    raise TraceError(
                        f"seed {self._entry.name!r} extends {persona.base!r} and {base!r}, and both declare persona "
                        f"{resource.key!r} (in seeds {persona.origin!r} and {resource.origin!r}); persona keys name "
                        "one user in flows, so give one of them another key"
                    )
            position = keyless_position(resource.path) if resource.key is None else None
            if position is not None:
                parent, kind, index = position
                counter = (parent, kind)
                self._keyless[counter] = max(self._keyless.get(counter, 0), index + 1)

    def create(
        self, kind: str, key: str | None = None, *, parent: ResourceHandle | None = None, **fields: Any
    ) -> ResourceHandle:
        spec = self._spec(kind)
        call = f"d.create({kind!r}, ...)"
        return self._declare(spec, self._placed(spec, key, parent, call), parent, fields)

    def add(self, resource: Resource, *, key: str | None = None,
            parent: ResourceHandle | None = None) -> ResourceHandle:
        if not isinstance(resource, Resource):
            raise TypeError(f"d.add() takes an instance of a registered resource model such as Tenant(name=...), "
                            f"got {type(resource).__name__} {resource!r}")
        spec = self._spec_of(type(resource))
        call = f"d.add({type(resource).__name__}(...))"
        fields = resource.model_dump(mode="json", by_alias=True, exclude_unset=True)
        return self._declare(spec, self._placed(spec, key, parent, call), parent, fields)

    def _placed(self, spec: ResourceSpec, key: str | None, parent: ResourceHandle | None, call: str) -> str | None:
        if parent is not None and not isinstance(parent, ResourceHandle):
            raise TypeError(f"{call} parent= expects a resource handle, got {type(parent).__name__}")
        parent_kind = parent.kind if parent is not None else None
        if spec.parent != parent_kind:
            where = f"under a {spec.parent} handle" if spec.parent is not None else "at the seed root (no parent=)"
            given = f"under {parent.path!r}" if parent is not None else "at the seed root"
            raise TraceError(f"resource kind {spec.kind!r} is created {where}, not {given}")
        if spec.keyed and key is None:
            raise TypeError(f"{call} needs a key: resource kind {spec.kind!r} is keyed")
        if not spec.keyed and key is not None:
            raise TypeError(f"{call} takes no key: resource kind {spec.kind!r} is keyless (its path counts siblings)")
        return _key(key, call) if key is not None else None

    def _spec(self, kind: str) -> ResourceSpec:
        return self._ctx.registry.resource(kind)

    def _spec_of(self, model: type[Resource]) -> ResourceSpec:
        found = [spec for spec in self._ctx.registry.resources() if spec.model is model]
        if not found:
            raise TraceError(f"{model.__qualname__} is not a registered resource model; decorate it with "
                             "@eks.resource(kind) and load the plugin that defines it")
        if len(found) > 1:
            raise TraceError(f"{model.__qualname__} is the model of several resource kinds "
                             f"({', '.join(spec.kind for spec in found)}); create it with d.create(kind, ...)")
        return found[0]

    def _factory(self, name: str, parent: ResourceHandle | None) -> ResourceFactory:
        parent_kind = parent.kind if parent is not None else None
        specs = {spec.kind: spec for spec in self._ctx.registry.resources()}
        fitting = sorted(kind for kind, spec in specs.items() if spec.parent == parent_kind)
        if name in fitting:
            return ResourceFactory(self, name, parent)
        raise AttributeError(_placement_error(name, parent, specs.get(name), fitting))

    def _declare(
        self, spec: ResourceSpec, key: str | None, parent: ResourceHandle | None, fields: dict[str, Any]
    ) -> ResourceHandle:
        parent_path = parent.path if parent is not None else None
        counter = (parent_path, spec.kind)
        index = self._keyless.get(counter, 0)
        path = resource_path(parent_path, resource_segment(spec.kind, key, index))
        scope = self._ctx.scope()
        if scope is not self._root_scope:
            raise TraceError(
                f"resource {path!r} is declared inside {scope.key!r} in seed {self._entry.name!r}; resources are "
                "declared at the top level of the seed because their path names them, so use a plain Python for "
                "loop with a distinct key per resource instead of eks.each, eks.when or @eks.dynamic"
            )
        if path in self._paths:
            raise TraceError(
                f"resource {path!r} is declared twice in seed {self._entry.name!r}; give the second one another key"
            )
        created = self._base_resources.get(path)
        if created is not None:
            raise TraceError(
                f"resource {path!r} is already created by base seed {created.origin!r} (through d.extend"
                f"({created.base!r})); declaring it again in seed {self._entry.name!r} would create it twice, so "
                f"use d.base({path!r}) to add children to it or refer to it"
            )
        persona = self._base_personas.get(key) if spec.persona and key is not None else None
        if persona is not None:
            raise TraceError(
                f"persona {key!r} is already created by base seed {persona.origin!r} at {persona.path!r}; persona "
                f"keys name users in flows and must be unique, so give the persona of seed {self._entry.name!r} "
                "another key"
            )
        if spec.persona and key is not None:
            first_path = self._personas.get(key)
            if first_path is not None:
                raise TraceError(
                    f"persona {key!r} is declared twice in seed {self._entry.name!r}: {first_path!r} and {path!r}; "
                    "persona keys name users in flows and must be unique"
                )
        _check_references(spec, fields, path, self._declared)
        model = spec.model.model_validate(encode(fields))
        args: dict[str, Any] = {
            "kind": spec.kind,
            "path": path,
            "fields": model.model_dump(mode="json", by_alias=True),
            "phase": spec.phase,
        }
        if key is not None:
            args["key"] = key
        if parent is not None:
            args["parent"] = parent.ref
        ref: Ref[Json] = self._ctx.record(
            CREATE_OP, args, uses=[self._backend], out=f"resource:{spec.kind}", id_key=path)
        self._paths.add(path)
        if key is None:
            self._keyless[counter] = index + 1
        if spec.persona and key is not None:
            self._personas[key] = path
            self._ctx.add_persona(key)
        self._ctx.require(spec.provenance)
        handle = ResourceHandle(self, spec.kind, key, path, ref)
        self._declared[node_of(ref)] = handle
        return handle

    def __getattr__(self, name: str) -> ResourceFactory:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._factory(name, None)


def _base_plan(ctx: TraceContext, base: str, extending: str) -> Plan | None:
    entry = next((spec for spec in ctx.registry.entries("seed") if spec.name == base), None)
    if entry is None:
        return None
    chain = _EXTENDING.get()
    if base in chain or base == extending:
        return None
    token = _EXTENDING.set((*chain, extending))
    try:
        return seed_plan(entry, registry=ctx.registry)
    finally:
        _EXTENDING.reset(token)


def _chain_resources(ctx: TraceContext, base: str, plan: Plan, seen: set[str]) -> list[BaseResource]:
    found: list[BaseResource] = []
    seen = seen | {plan.name}
    for node, _ancestors in plan.walk():
        if node.op == EXTEND_OP:
            inner = node.args.get("base")
            if isinstance(inner, str) and inner not in seen:
                inner_plan = _base_plan(ctx, inner, plan.name)
                if inner_plan is not None:
                    found.extend(_chain_resources(ctx, base, inner_plan, seen))
        elif node.op == CREATE_OP:
            kind, path, key = node.args.get("kind"), node.args.get("path"), node.args.get("key")
            if not isinstance(kind, str) or not isinstance(path, str):
                continue
            try:
                persona = ctx.registry.resource(kind).persona
            except RegistryError:
                persona = False
            found.append(BaseResource(base, plan.name, kind, key if isinstance(key, str) else None, path, persona))
    return found


def _handles(value: object, declared: Mapping[str, ResourceHandle]) -> list[ResourceHandle]:
    if isinstance(value, ResourceHandle):
        return [value]
    if isinstance(value, RefExpr):
        found = declared.get(value.ref)
        return [found] if found is not None and not value.path else []
    if is_expr_dict(value):
        try:
            expr = parse_expr(value)
        except RefError:
            return []
        return _handles(expr, declared) if isinstance(expr, RefExpr) else []
    if isinstance(value, list | tuple):
        return [handle for item in cast("Sequence[object]", value) for handle in _handles(item, declared)]
    return []


def _check_references(spec: ResourceSpec, fields: Mapping[str, Any], path: str,
                      declared: Mapping[str, ResourceHandle]) -> None:
    for name, info in spec.model.model_fields.items():
        expected = referred_kind(ref_kind_of(info))
        if expected is None:
            continue
        for handle in _handles(fields.get(info.alias or name, fields.get(name)), declared):
            if handle.kind != expected:
                raise TraceError(
                    f"resource {path!r}: field {name!r} refers to a {expected} resource, but {handle.path!r} is a "
                    f"{handle.kind}"
                )


def _key(value: object, call: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{call} key must be a literal string such as 'harman', got {type(value).__name__} {value!r}")
    if SENTINEL_OPEN in value or SENTINEL_CLOSE in value:
        raise TypeError(f"{call} key is built from a ref; resource keys must be literal because they name the path")
    return value


def _placement_error(
    name: str, parent: ResourceHandle | None, spec: ResourceSpec | None, fitting: list[str]
) -> str:
    here = f"under {parent.path!r} ({parent.kind})" if parent is not None else "at the seed root"
    listing = f"kinds that fit here: {', '.join(fitting)}" if fitting else "no resource kind fits here"
    if spec is not None:
        where = f"under a {spec.parent} handle" if spec.parent is not None else "at the seed root, d." + name + "(...)"
        return f"resource kind {name!r} is created {where}, not {here}; {listing}"
    close = difflib.get_close_matches(name, fitting, n=3, cutoff=0.6)
    hint = f"did you mean {' or '.join(repr(kind) for kind in close)}? " if close else ""
    return f"unknown resource kind {name!r} {here}; {hint}{listing}"


def open_data(ctx: TraceContext, entry: EntrySpec) -> Data:
    data = Data(ctx, entry)
    options = entry.options
    if isinstance(options, SeedOptions) and options.base is not None:
        data.extend(options.base)
    return data


__all__ = [
    "BaseResource",
    "Data",
    "Resource",
    "ResourceFactory",
    "ResourceHandle",
    "ResourceRef",
    "open_data",
    "refers_to",
]
