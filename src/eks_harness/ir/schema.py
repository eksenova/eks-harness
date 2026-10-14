from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, get_args

from pydantic import BaseModel
from pydantic.json_schema import (
    CoreModeRef,
    DefsRef,
    GenerateJsonSchema,
    models_json_schema,
)

from eks_harness.engine.validator import CHECKPOINT_FAMILIES, MEDIA_FAMILIES
from eks_harness.ir import SCHEMA_ID
from eks_harness.ir.ops.check import MediaCheck
from eks_harness.ir.ops.data import Create
from eks_harness.ir.plan import CheckUse, Node, Plan
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    ENTRY_MODULE_PREFIX,
    PROJECT_PLUGIN,
    RESOURCE_OPS,
    SCOPED_MODULE_PATTERN,
    UNTYPED_OUT,
    AnySpec,
    Category,
    OpSpec,
    Registry,
    default_registry,
    definition_of,
    using_registry,
)
from eks_harness.ir.target import Target
from eks_harness.ir.types import RESOURCE_TYPE_PATTERN

PLAN_SCHEMA_FILE = "plan.schema.json"
OPS_DIR = "ops"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DEFS = "$defs"
REF = "$ref"
DEFS_PREFIX = f"#/{DEFS}/"
ROOT_REF = "#"
EXTENSION_KEY = "x-eks"
PLAN_TITLE = "eks plan"
PLAN_DESCRIPTION = ("A plan built by eks: the typed, serializable graph of nodes, resources and clock that the "
                    "engine validates and runs. Every node is one of the registered ops.")
NODE_DESCRIPTION = "A plan node: one registered op with its arguments, and the branches of a container op"
NODE_OP_FIELDS = ("op", "v", "args", "out", "branches")
NO_OUT = "none"
DEFAULT_OP_VERSION = "1"
MODE = "validation"
SHARED_PACKAGES = frozenset({"eks", "eks_harness", "pydantic"})
SCOPED_PREFIX = re.compile(r"^_eks_harness_plugin_(?P<plugin>[A-Za-z0-9_]*?)_[0-9a-f]{12}$")
ENTRY_MODULE = re.compile(rf"^{ENTRY_MODULE_PREFIX}[0-9a-f]{{12}}_(?P<stem>.*)$")
TYPE_PATH = re.compile(r"[A-Za-z_]\w*(?:\.(?:\w+|<locals>))+")
CORE_REF_PARTS = re.compile(r"([\][,])")
UNSAFE_DEF_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class DefNaming:
    owners: Mapping[str, str] = field(default_factory=dict[str, str])
    reserved: frozenset[str] = frozenset()


def _module_key(module: str) -> str:
    scoped = SCOPED_MODULE_PATTERN.match(module)
    return scoped.group(0).removesuffix(".") if scoped else module


def _registry_specs(registry: Registry) -> Iterator[AnySpec]:
    yield from registry.ops()
    yield from registry.macros()
    yield from registry.resources()
    yield from registry.checks()
    for category in get_args(Category):
        yield from registry.catalogs(category)
    yield from registry.entries()


def def_naming(registry: Registry) -> DefNaming:
    candidates: dict[str, set[str]] = {}
    for spec in _registry_specs(registry):
        definition = definition_of(spec)
        plugin = definition.provenance.plugin
        if definition.module and plugin != BUILTIN_PLUGIN:
            candidates.setdefault(_module_key(definition.module), set()).add(plugin)
    owners = {module: min(plugins) for module, plugins in sorted(candidates.items())}
    return DefNaming(owners, frozenset(spec.name for spec in registry.ops()))


def _type_paths(core_ref: str) -> list[str]:
    parts = (part.rsplit(":", 1)[0].strip() for part in CORE_REF_PARTS.split(core_ref))
    return [part for part in parts if TYPE_PATH.fullmatch(part)]


def _shared(path: str) -> bool:
    return path.split(".", 1)[0] in SHARED_PACKAGES


def _module_of(path: str) -> str:
    parts = path.split(".")
    for end in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in sys.modules:
            return candidate
    return ".".join(parts[:-1])


def _owner_and_place(module: str, naming: DefNaming) -> tuple[str, str]:
    known = naming.owners.get(_module_key(module))
    scoped = SCOPED_MODULE_PATTERN.match(module)
    if scoped:
        prefix = SCOPED_PREFIX.match(scoped.group(0).removesuffix("."))
        fallback = prefix.group("plugin") if prefix else PROJECT_PLUGIN
        return known or fallback, module[scoped.end():]
    entry = ENTRY_MODULE.match(module)
    if entry:
        return known or PROJECT_PLUGIN, entry.group("stem")
    return known or module, module


def _local_names(path: str, bare: str, naming: DefNaming) -> list[str]:
    owner, place = _owner_and_place(_module_of(path), naming)
    names = [f"{owner}.{bare}"]
    if place and place != owner:
        names.append(f"{owner}.{place}.{bare}")
    return [UNSAFE_DEF_CHARS.sub("_", name) for name in names]


class EksJsonSchema(GenerateJsonSchema):
    naming: ClassVar[DefNaming] = DefNaming()

    def get_defs_ref(self, core_mode_ref: CoreModeRef) -> DefsRef:
        key = super().get_defs_ref(core_mode_ref)
        defaults = self._prioritized_defsref_choices[key]
        foreign = [path for path in _type_paths(core_mode_ref[0]) if not _shared(path)]
        choices: Iterable[DefsRef] = defaults
        if foreign:
            choices = [*(DefsRef(name) for name in _local_names(foreign[0], defaults[0], self.naming)), key]
        self._prioritized_defsref_choices[key] = [
            choice for choice in dict.fromkeys(choices) if choice == key or choice not in self.naming.reserved]
        return key


def schema_generator(registry: Registry) -> type[GenerateJsonSchema]:
    class Generator(EksJsonSchema):
        naming = def_naming(registry)

    return Generator


def op_summary(spec: OpSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "family": spec.family,
        "version": spec.version,
        "out": spec.out,
        "description": spec.description,
        "location": spec.location,
        "executor": spec.executor,
        "pure": spec.pure,
        "idempotent": spec.idempotent,
        "deviceDependent": spec.device_dependent,
        "ordering": spec.ordering,
        "platforms": sorted(spec.platforms) if spec.platforms is not None else None,
        "resources": list(spec.resources),
        "container": spec.container,
        "branches": list(spec.branches),
        "exportsBody": spec.exports_body,
        "pinnable": spec.pinnable,
        "recordingOnly": spec.recording_only,
        "bounded": spec.bounded,
        "plugin": spec.provenance.plugin,
        "pluginVersion": spec.provenance.version,
    }


def _labelled(schema: Mapping[str, Any], spec: OpSpec) -> dict[str, Any]:
    head: dict[str, Any] = {"title": spec.name}
    if spec.description:
        head["description"] = spec.description
    body = {key: value for key, value in schema.items() if key not in ("title", "description", "examples")}
    if spec.examples:
        body["examples"] = [dict(example) for example in spec.examples]
    return {**head, **body, EXTENSION_KEY: op_summary(spec)}


def _def_name(ref: Mapping[str, Any]) -> str:
    target = ref.get(REF)
    if not isinstance(target, str) or not target.startswith(DEFS_PREFIX):
        raise ValueError(f"expected a local {DEFS} reference, got {target!r}")
    return target.removeprefix(DEFS_PREFIX)


def _rename_refs(value: Any, renames: Mapping[str, str]) -> Any:
    match value:
        case dict():
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key == REF and isinstance(item, str) and item.startswith(DEFS_PREFIX):
                    result[key] = renames.get(item.removeprefix(DEFS_PREFIX), item)
                else:
                    result[key] = _rename_refs(item, renames)
            return result
        case list():
            return [_rename_refs(item, renames) for item in value]
        case _:
            return value


def _branches_schema(spec: OpSpec, node_ref: str) -> dict[str, Any]:
    if not spec.container:
        return {"type": "null"}
    branch = {"type": "array", "items": {REF: node_ref}}
    return {
        "type": "object",
        "properties": {name: branch for name in spec.branches},
        "additionalProperties": False,
    }


def _out_schema(spec: OpSpec) -> dict[str, Any]:
    if spec.name in RESOURCE_OPS:
        return {"type": "string", "pattern": RESOURCE_TYPE_PATTERN,
                "description": "resource:KIND, the resource kind of args.kind"}
    if spec.out == UNTYPED_OUT:
        return {"type": "string", "description": "json, or a narrower ref type the producer guarantees"}
    if spec.out == NO_OUT:
        return {"const": NO_OUT, "default": NO_OUT}
    return {"const": spec.out}


def _node_variant(spec: OpSpec, common: Mapping[str, Any], required: list[str], args_required: bool,
                  node_ref: str) -> dict[str, Any]:
    version: dict[str, Any] = {"const": spec.version}
    needed = {"op", *required}
    if spec.version == DEFAULT_OP_VERSION:
        version["default"] = DEFAULT_OP_VERSION
    else:
        needed.add("v")
    if args_required:
        needed.add("args")
    out = _out_schema(spec)
    if spec.name in RESOURCE_OPS or spec.out != NO_OUT:
        needed.add("out")
    shared = {key: value for key, value in common.items() if key not in NODE_OP_FIELDS}
    return {
        "type": "object",
        "title": spec.name,
        "properties": {
            "op": {"const": spec.name},
            "v": version,
            "args": {REF: f"{DEFS_PREFIX}{spec.name}"},
            "out": out,
            **shared,
            "branches": _branches_schema(spec, node_ref),
        },
        "required": sorted(needed),
        "additionalProperties": False,
    }


def _arg_models(specs: list[OpSpec]) -> dict[type[BaseModel], list[OpSpec]]:
    models: dict[type[BaseModel], list[OpSpec]] = {}
    for spec in specs:
        models.setdefault(spec.args, []).append(spec)
    return models


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _model_like(value: Mapping[str, Any]) -> bool:
    return value.get("type") == "object" and isinstance(value.get("properties"), dict) and "title" in value


def _hoist(value: Any, known: Mapping[str, str]) -> Any:
    match value:
        case dict():
            if _model_like(value):
                name = known.get(_canonical(value))
                if name is not None:
                    return {REF: f"{DEFS_PREFIX}{name}"}
            return {key: _hoist(item, known) for key, item in value.items()}
        case list():
            return [_hoist(item, known) for item in value]
        case _:
            return value


def _hoisted(defs: Mapping[str, Any]) -> dict[str, Any]:
    known = {_canonical(body): name for name, body in sorted(defs.items()) if _model_like(body)}
    return {name: {key: _hoist(item, known) for key, item in body.items()} for name, body in defs.items()}


def _refs(value: Any) -> Iterator[str]:
    match value:
        case dict():
            for key, item in value.items():
                if key == REF and isinstance(item, str) and item.startswith(DEFS_PREFIX):
                    yield item.removeprefix(DEFS_PREFIX)
                else:
                    yield from _refs(item)
        case list():
            for item in value:
                yield from _refs(item)
        case _:
            return


def _reachable(root: Mapping[str, Any], defs: Mapping[str, Any]) -> set[str]:
    seen: set[str] = set()
    pending = list(_refs(root))
    while pending:
        name = pending.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        pending.extend(_refs(defs[name]))
    return seen


def _catalog_models(registry: Registry) -> list[type[BaseModel]]:
    return [spec.model for category in get_args(Category) for spec in registry.catalogs(category)]


@dataclass(frozen=True, slots=True)
class _Kind:
    name: str
    model: type[BaseModel]
    description: str


@dataclass(frozen=True, slots=True)
class _Dispatch:
    key: str
    payload: str
    kinds: tuple[_Kind, ...]


def _check_kinds(registry: Registry, families: Iterable[str]) -> tuple[_Kind, ...]:
    wanted = frozenset(families)
    return tuple(_Kind(spec.kind, spec.args, spec.description) for spec in registry.checks() if spec.family in wanted)


def _dispatches(registry: Registry) -> dict[type[BaseModel], _Dispatch]:
    return {
        Target: _Dispatch("by", "args", tuple(
            _Kind(spec.kind, spec.model, spec.description) for spec in registry.catalogs("target"))),
        CheckUse: _Dispatch("kind", "params", _check_kinds(registry, CHECKPOINT_FAMILIES)),
        MediaCheck: _Dispatch("kind", "params", _check_kinds(registry, MEDIA_FAMILIES)),
        Create: _Dispatch("kind", "fields", tuple(
            _Kind(spec.kind, spec.model, spec.description) for spec in registry.resources())),
    }


def _requires_fields(model: type[BaseModel]) -> bool:
    return any(info.is_required() for info in model.model_fields.values())


def _kind_variant(kind: _Kind, dispatch: _Dispatch, def_name: str) -> dict[str, Any]:
    variant: dict[str, Any] = {"title": kind.name}
    if kind.description:
        variant["description"] = kind.description
    variant["properties"] = {dispatch.key: {"const": kind.name}, dispatch.payload: {REF: f"{DEFS_PREFIX}{def_name}"}}
    variant["required"] = [dispatch.key, dispatch.payload] if _requires_fields(kind.model) else [dispatch.key]
    return variant


def _dispatched(body: Mapping[str, Any], dispatch: _Dispatch, def_names: Mapping[type[BaseModel], str]) -> dict[str, Any]:
    properties: dict[str, Any] = dict(body["properties"])
    properties[dispatch.key] = {**properties[dispatch.key], "enum": [kind.name for kind in dispatch.kinds]}
    result: dict[str, Any] = {**body, "properties": properties}
    if dispatch.kinds:
        result["oneOf"] = [_kind_variant(kind, dispatch, def_names[kind.model]) for kind in dispatch.kinds]
    return result


@dataclass(frozen=True, slots=True)
class _Built:
    root: dict[str, Any]
    defs: dict[str, Any]
    bodies: dict[str, dict[str, Any]]
    owners: dict[str, str]


def _build(registry: Registry) -> _Built:
    with using_registry(registry):
        return _build_with(registry)


def _build_with(registry: Registry) -> _Built:
    specs = registry.ops()
    owners = _arg_models(specs)
    dispatches = _dispatches(registry)
    kind_models = [kind.model for dispatch in dispatches.values() for kind in dispatch.kinds]
    inputs = list(dict.fromkeys([Plan, Node, *dispatches, *owners, *_catalog_models(registry), *kind_models]))
    keys, generated = models_json_schema([(model, MODE) for model in inputs], by_alias=True,
                                         schema_generator=schema_generator(registry))
    def_names = {model: _def_name(keys[(model, MODE)]) for model in inputs}
    raw: dict[str, Any] = dict(generated.get(DEFS, {}))
    for model, dispatch in dispatches.items():
        raw[def_names[model]] = _dispatched(raw[def_names[model]], dispatch, def_names)
    defs = _hoisted(raw)
    plan_def = def_names[Plan]
    node_def = def_names[Node]
    node_ref = f"{DEFS_PREFIX}{node_def}"
    renames: dict[str, str] = {plan_def: ROOT_REF}
    op_defs: dict[str, dict[str, Any]] = {}
    bodies: dict[str, dict[str, Any]] = {}
    op_owners: dict[str, str] = {}
    args_required: dict[type[BaseModel], bool] = {}
    for model, model_specs in owners.items():
        model_def = def_names[model]
        first, *others = model_specs
        renames[model_def] = f"{DEFS_PREFIX}{first.name}"
        model_body = defs.pop(model_def)
        args_required[model] = bool(model_body.get("required"))
        op_defs[first.name] = _labelled(model_body, first)
        for other in others:
            op_defs[other.name] = _labelled({REF: f"{DEFS_PREFIX}{first.name}"}, other)
        for spec in model_specs:
            bodies[spec.name] = model_body
            op_owners[spec.name] = first.name
    node_schema: dict[str, Any] = defs[node_def]
    common: dict[str, Any] = node_schema.get("properties", {})
    required = [name for name in node_schema.get("required", []) if name not in NODE_OP_FIELDS]
    defs[node_def] = {
        "title": node_schema.get("title", Node.__name__),
        "description": NODE_DESCRIPTION,
        "oneOf": [
            _node_variant(spec, common, required, args_required[spec.args], node_ref)
            for spec in specs
        ],
    }
    root_body: dict[str, Any] = _rename_refs(defs.pop(plan_def), renames)
    defs.update(op_defs)
    return _Built(
        root={key: value for key, value in root_body.items() if key not in ("title", "description")},
        defs={name: _rename_refs(value, renames) for name, value in defs.items()},
        bodies={name: _rename_refs(body, renames) for name, body in bodies.items()},
        owners=op_owners,
    )


def _plan_document(built: _Built) -> dict[str, Any]:
    kept = _reachable(built.root, built.defs)
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": PLAN_TITLE,
        "description": PLAN_DESCRIPTION,
        **built.root,
        DEFS: {name: built.defs[name] for name in sorted(kept)},
    }


def _op_document(built: _Built, spec: OpSpec) -> dict[str, Any]:
    own = {spec.name: ROOT_REF, built.owners[spec.name]: ROOT_REF}
    body: dict[str, Any] = _rename_refs(built.bodies[spec.name], own)
    defs = {name: _rename_refs(value, own) for name, value in built.defs.items() if name not in own}
    kept = _reachable(body, defs)
    if kept:
        body = {**body, DEFS: {name: defs[name] for name in sorted(kept)}}
    return _labelled(body, spec)


def plan_schema(registry: Registry | None = None) -> dict[str, Any]:
    return _plan_document(_build(registry if registry is not None else default_registry()))


def op_schema(spec: OpSpec, *, registry: Registry | None = None) -> dict[str, Any]:
    return _op_document(_build(registry if registry is not None else default_registry()), spec)


def encode_schema(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def _files(chosen: Registry) -> Iterator[tuple[Path, Mapping[str, Any]]]:
    built = _build(chosen)
    yield Path(PLAN_SCHEMA_FILE), _plan_document(built)
    for spec in chosen.ops():
        yield Path(OPS_DIR) / f"{spec.name}.json", _op_document(built, spec)


def write_schemas(out: Path, *, registry: Registry | None = None) -> list[Path]:
    chosen = registry if registry is not None else default_registry()
    written: list[Path] = []
    for relative, schema in _files(chosen):
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encode_schema(schema), encoding="utf-8")
        written.append(target)
    return sorted(written)


__all__ = [
    "OPS_DIR",
    "PLAN_SCHEMA_FILE",
    "DefNaming",
    "EksJsonSchema",
    "def_naming",
    "encode_schema",
    "op_schema",
    "op_summary",
    "plan_schema",
    "schema_generator",
    "write_schemas",
]
