from __future__ import annotations

import json
import mimetypes
import types
import typing
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAliasType, Union, get_args, get_origin

from pydantic import BaseModel, JsonValue

from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.refs import FmtExpr, ItemExpr, RefExpr
from eks_harness.ir.registry import (
    OpSpec,
    Poly,
    PresetModel,
    Registry,
    RegistryError,
    using_registry,
)
from eks_harness.ir.target import Target
from eks_harness.ir.time import TICKS_PER_SECOND, Time

WORKER_LOCATION = "worker"
VIEWPORT_FIELDS = frozenset({"viewport"})
STYLE_FIELDS = frozenset({"style"})
PRESET_CATEGORIES = {"viewport": "viewport", "style": "style"}
MEDIA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "mime": {"type": "string"}, "name": {"type": ["string", "null"]}},
    "required": ["path", "mime"],
    "additionalProperties": False,
}
TARGET_DEF = "Target"
REF_TYPES: tuple[type, ...] = (RefExpr, FmtExpr, ItemExpr)


class WireError(ValueError):
    pass


ArtifactFile = Callable[[str], Path | None]


def _unwrap(annotation: Any) -> Any:
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
            continue
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if isinstance(origin, TypeAliasType):
            annotation = origin.__value__
            continue
        return annotation


def _is_union(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return origin is Union or origin is types.UnionType


def _members(annotation: Any) -> list[Any]:
    annotation = _unwrap(annotation)
    if _is_union(annotation):
        found: list[Any] = []
        for member in get_args(annotation):
            found.extend(_members(member))
        return found
    return [annotation]


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _is_ref(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, REF_TYPES)


def _media_ref(annotation: Any) -> bool:
    for meta in _metadata(annotation):
        if getattr(meta, "type", None) == "media":
            return True
    return False


def _metadata(annotation: Any) -> list[Any]:
    found: list[Any] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, TypeAliasType):
            stack.append(current.__value__)
            continue
        origin = get_origin(current)
        if origin is Annotated:
            parts = get_args(current)
            found.extend(parts[1:])
            stack.append(parts[0])
        elif _is_union(current):
            stack.extend(get_args(current))
    return found


class _SchemaBuilder:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.defs: dict[str, Any] = {}
        self.names: dict[type[BaseModel], str] = {}

    def _name(self, model: type[BaseModel]) -> str:
        known = self.names.get(model)
        if known is not None:
            return known
        taken = set(self.names.values())
        name = model.__name__
        if name in taken:
            family = model.__module__.rsplit(".", 1)[-1].capitalize()
            name = f"{family}{model.__name__}"
            suffix = 2
            while name in taken:
                name = f"{family}{model.__name__}{suffix}"
                suffix += 1
        self.names[model] = name
        return name

    def model(self, model: type[BaseModel]) -> dict[str, Any]:
        if model is Target or issubclass(model, Target):
            return self._target()
        name = self._name(model)
        if name not in self.defs:
            self.defs[name] = {}
            properties: dict[str, Any] = {}
            required: list[str] = []
            for field_name, info in model.model_fields.items():
                key = info.alias or field_name
                properties[key] = self.annotation(info.annotation, field=field_name, metadata=info.metadata)
                if info.description:
                    properties[key] = {**properties[key], "description": info.description}
                if info.is_required():
                    required.append(key)
            if issubclass(model, PresetModel) and getattr(model, "preset_category", "") == "viewport":
                properties["name"] = {"type": "string"}
            self.defs[name] = {"type": "object", "properties": properties, "required": required,
                               "additionalProperties": False}
        return {"$ref": f"#/$defs/{name}"}

    def _target(self) -> dict[str, Any]:
        if TARGET_DEF not in self.defs:
            self.defs[TARGET_DEF] = {
                "type": "object",
                "properties": {
                    "by": {"type": "string"},
                    "args": {"type": "object"},
                    "within": {"anyOf": [{"$ref": f"#/$defs/{TARGET_DEF}"}, {"type": "null"}]},
                    "near": {"anyOf": [{"$ref": f"#/$defs/{TARGET_DEF}"}, {"type": "null"}]},
                    "nth": {"type": ["integer", "null"]},
                    "surface": {"enum": ["app", "native", None]},
                },
                "required": ["by"],
                "additionalProperties": False,
            }
        return {"$ref": f"#/$defs/{TARGET_DEF}"}

    def annotation(self, annotation: Any, *, field: str = "", metadata: list[Any] | None = None) -> dict[str, Any]:
        poly = next((meta for meta in [*(metadata or []), *_metadata(annotation)] if isinstance(meta, Poly)), None)
        if poly is not None:
            kinds = [self.model(spec.model) for spec in self.registry.catalogs(poly.category)]
            nullable = any(member is type(None) for member in _members(annotation))
            return {"anyOf": [*kinds, {"type": "null"}] if nullable else kinds}
        if _media_ref(annotation) or any(getattr(meta, "type", None) == "media" for meta in metadata or []):
            nullable = any(member is type(None) for member in _members(annotation))
            return {"anyOf": [MEDIA_SCHEMA, {"type": "null"}]} if nullable else MEDIA_SCHEMA
        members = [member for member in _members(annotation) if not _is_ref(member)]
        if len(members) > 1:
            return self._union(members, field)
        if not members:
            return {}
        return self._single(members[0], field)

    def _union(self, members: list[Any], field: str) -> dict[str, Any]:
        presets = [member for member in members if _is_model(member) and issubclass(member, PresetModel)]
        if presets:
            members = [member for member in members if member is not str]
        options: list[dict[str, Any]] = []
        for member in members:
            schema = self._single(member, field)
            if schema not in options:
                options.append(schema)
        return options[0] if len(options) == 1 else {"anyOf": options}

    def _single(self, annotation: Any, field: str) -> dict[str, Any]:
        annotation = _unwrap(annotation)
        if annotation is type(None):
            return {"type": "null"}
        if annotation is Time:
            return {"type": "integer", "description": "milliseconds"}
        if annotation is str:
            if field in VIEWPORT_FIELDS:
                return self.model(_viewport_model())
            if field in STYLE_FIELDS:
                return {"type": "object"}
            return {"type": "string"}
        if annotation is bool:
            return {"type": "boolean"}
        if annotation is int:
            return {"type": "integer"}
        if annotation is float:
            return {"type": "number"}
        if annotation is Any or annotation is JsonValue or annotation is object:
            return {}
        if _is_model(annotation):
            return self.model(annotation)
        origin = get_origin(annotation)
        if origin is Literal:
            return {"enum": list(get_args(annotation))}
        if origin in (tuple, list, typing.Sequence):
            parts = get_args(annotation)
            if origin is tuple and parts and parts[-1] is not Ellipsis:
                return {"type": "array", "prefixItems": [self.annotation(part) for part in parts],
                        "minItems": len(parts), "maxItems": len(parts)}
            return {"type": "array", "items": self.annotation(parts[0]) if parts else {}}
        if origin in (dict, typing.Mapping, Mapping):
            parts = get_args(annotation)
            return {"type": "object", "additionalProperties": self.annotation(parts[1]) if len(parts) == 2 else {}}
        return {}


def _viewport_model() -> type[BaseModel]:
    from eks_harness.ir.presets import Viewport

    return Viewport


def worker_ops(registry: Registry, platform: str | None = None) -> list[OpSpec]:
    found: list[OpSpec] = []
    for spec in registry.ops():
        if spec.location != WORKER_LOCATION:
            continue
        if platform is not None and spec.platforms and platform not in spec.platforms:
            continue
        found.append(spec)
    return found


def wire_schemas(registry: Registry, platform: str | None = None) -> dict[str, Any]:
    builder = _SchemaBuilder(registry)
    ops: dict[str, Any] = {}
    with using_registry(registry):
        for spec in worker_ops(registry, platform):
            ops[spec.name] = builder.model(spec.args) if spec.args is not None else {
                "type": "object", "properties": {}, "additionalProperties": False}
    return {"ops": ops, "$defs": builder.defs}


def duration_ms(time: Time, field: str) -> int:
    if not time.is_concrete:
        raise WireError(f"{field} must be a plain duration such as 2s or 300ms on a device, got {time}")
    return round(time.ticks * 1000 / TICKS_PER_SECOND)


class _Converter:
    def __init__(self, session: SessionSpec | None, registry: Registry, artifact_file: ArtifactFile | None) -> None:
        self.session = session
        self.registry = registry
        self.artifact_file = artifact_file

    def model(self, value: BaseModel) -> dict[str, Any]:
        if isinstance(value, Target):
            return self.target(value)
        out: dict[str, Any] = {}
        for name, info in type(value).model_fields.items():
            key = info.alias or name
            out[key] = self.value(getattr(value, name), field=name, annotation=info.annotation,
                                  metadata=info.metadata)
        return out

    def target(self, target: Target) -> dict[str, Any]:
        dumped: dict[str, Any] = target.model_dump(mode="json", by_alias=True, exclude_none=True)
        return dumped

    def value(self, value: Any, *, field: str = "", annotation: Any = None, metadata: list[Any] | None = None) -> Any:
        if value is None:
            return None
        if annotation is not None and (_media_ref(annotation) or any(
                getattr(meta, "type", None) == "media" for meta in metadata or [])):
            return self.media(value, field)
        if isinstance(value, Time):
            return duration_ms(value, field)
        if isinstance(value, BaseModel):
            return self.model(value)
        if isinstance(value, str):
            if field in VIEWPORT_FIELDS:
                return self.viewport(value)
            if field in STYLE_FIELDS:
                return self.preset("style", value)
            if annotation is not None and any(
                    _is_model(member) and issubclass(member, PresetModel) for member in _members(annotation)):
                preset = next(member for member in _members(annotation)
                              if _is_model(member) and issubclass(member, PresetModel))
                return self.preset(preset.preset_category, value)
            return value
        if isinstance(value, (tuple, list)):
            inner = _sequence_item(annotation)
            return [self.value(item, field=field, annotation=inner) for item in value]
        if isinstance(value, dict):
            inner = _mapping_value(annotation)
            return {str(key): self.value(item, field=field, annotation=inner) for key, item in value.items()}
        return value

    def preset(self, category: str, name: str) -> dict[str, Any]:
        try:
            spec = self.registry.preset(category, name)
        except RegistryError as problem:
            raise WireError(f"{category} preset {name!r} is not registered") from problem
        return self.model(spec.value)

    def viewport(self, name: str) -> dict[str, Any]:
        if self.session is not None and name in self.session.viewports:
            found = self.model(self.session.viewports[name])
        else:
            found = self.preset("viewport", name)
        return {"name": name, **found}

    def media(self, value: Any, field: str) -> dict[str, Any]:
        data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        if not isinstance(data, dict):
            raise WireError(f"{field} must be a media or artifact value, got {data!r}")
        artifact = data.get("artifact") or data.get("id")
        uri = data.get("uri")
        if not artifact and isinstance(uri, str) and uri.startswith("artifact:"):
            artifact = uri.removeprefix("artifact:").split("@", 1)[0]
        if not isinstance(artifact, str) or self.artifact_file is None:
            raise WireError(f"{field} does not name a stored artifact")
        path = self.artifact_file(artifact)
        if path is None:
            raise WireError(f"artifact {artifact} of {field} has no file on this hub")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {"path": str(path), "mime": mime, "name": data.get("name")}


def _sequence_item(annotation: Any) -> Any:
    for member in _members(annotation) if annotation is not None else []:
        if get_origin(member) in (tuple, list):
            parts = get_args(member)
            return parts[0] if parts else None
    return None


def _mapping_value(annotation: Any) -> Any:
    for member in _members(annotation) if annotation is not None else []:
        if get_origin(member) in (dict, Mapping):
            parts = get_args(member)
            return parts[1] if len(parts) == 2 else None
    return None


CHECKPOINT_OP = "check.checkpoint"


def to_wire(spec: OpSpec, data: Mapping[str, JsonValue], *, session: SessionSpec | None, registry: Registry,
            artifact_file: ArtifactFile | None = None) -> dict[str, Any]:
    if spec.args is None:
        return {}
    from eks_harness.engine.values import validate_args

    with using_registry(registry):
        model = validate_args(spec, dict(data)).model
        wire = _Converter(session, registry, artifact_file).model(model)
    if spec.name == CHECKPOINT_OP and wire.get("checks") is None:
        uses = [*session.defaults, *session.checks] if session is not None else []
        wire["checks"] = [use.model_dump(mode="json") for use in uses]
    return wire


TS_HEADER = "export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }\n"


def _ts(schema: Any) -> str:
    if not isinstance(schema, dict) or not schema:
        return "Json"
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(dict.fromkeys(_ts(option) for option in schema["anyOf"]))
    if "enum" in schema:
        return " | ".join("null" if value is None else json.dumps(value) for value in schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_ts({**schema, "type": item}) for item in kind)
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        if "prefixItems" in schema:
            return "[" + ", ".join(_ts(item) for item in schema["prefixItems"]) + "]"
        return f"Array<{_ts(schema.get('items'))}>"
    if kind == "object":
        properties = schema.get("properties")
        if properties:
            required = set(schema.get("required", []))
            fields = "; ".join(f"{json.dumps(key)}{'' if key in required else '?'}: {_ts(value)}"
                               for key, value in properties.items())
            return "{ " + fields + " }"
        extra = schema.get("additionalProperties")
        return f"{{ [key: string]: {_ts(extra) if isinstance(extra, dict) else 'Json'} }}"
    return "Json"


def _ts_name(op: str) -> str:
    return "".join(part.capitalize() for part in op.replace("_", ".").split(".")) + "Args"


def typescript(registry: Registry) -> str:
    schemas = wire_schemas(registry)
    lines = [TS_HEADER]
    for name, schema in sorted(schemas["$defs"].items()):
        lines.append(f"export type {name} = {_ts(schema)}\n")
    entries: list[str] = []
    for op, schema in sorted(schemas["ops"].items()):
        alias = _ts_name(op)
        lines.append(f"export type {alias} = {_ts(schema)}\n")
        entries.append(f"  {json.dumps(op)}: {alias}")
    lines.append("export interface OpArgs {\n" + "\n".join(entries) + "\n}\n")
    return "\n".join(lines)


__all__ = [
    "MEDIA_SCHEMA",
    "WORKER_LOCATION",
    "ArtifactFile",
    "WireError",
    "duration_ms",
    "to_wire",
    "typescript",
    "wire_schemas",
    "worker_ops",
]
