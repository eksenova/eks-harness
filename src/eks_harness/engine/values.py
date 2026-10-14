from __future__ import annotations

import copy
import json
import math
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache, partial
from types import UnionType
from typing import (
    Annotated,
    Any,
    ForwardRef,
    Literal,
    TypeAliasType,
    Union,
    cast,
    get_args,
    get_origin,
)

import typing_extensions
from pydantic import (
    BaseModel,
    GetCoreSchemaHandler,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    create_model,
)
from pydantic.fields import FieldInfo
from pydantic_core import CoreSchema, core_schema

from eks_harness.engine.errors import NodeFailure, OutputTypeError, RefResolutionError
from eks_harness.engine.model import ROOT_INSTANCE
from eks_harness.ir.hashing import canonical_json, digest
from eks_harness.ir.plan import Node
from eks_harness.ir.refs import (
    FMT_KEY,
    ITEM_KEY,
    REF_KEY,
    FmtExpr,
    ItemExpr,
    RefError,
    RefExpr,
    RefKind,
    apply_path,
    is_expr_dict,
    parse_expr,
)
from eks_harness.ir.registry import OpSpec, Poly, default_registry
from eks_harness.ir.types import (
    Artifact,
    MediaInfo,
    RefType,
    RefTypeError,
    parse_ref_type,
)

INSTANCE_SEPARATOR = "/"
ITEM_SEPARATOR = "#"
PAUSE_VALUES_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class ResolvedArgs:
    model: BaseModel
    data: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Segment:
    node_id: str
    index: int | None


_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ARGS_ERRORS = 3


def _check_segment(segment: str, instance: str) -> Segment:
    if not segment:
        raise ValueError(f"instance key {instance!r} has an empty segment")
    node_id, separator, index = segment.partition(ITEM_SEPARATOR)
    if not node_id:
        raise ValueError(f"instance key {instance!r}: segment {segment!r} has no node id")
    if not separator:
        return Segment(node_id, None)
    if not _INDEX.fullmatch(index):
        raise ValueError(
            f"instance key {instance!r}: segment {segment!r} needs a non-negative integer index after {ITEM_SEPARATOR!r}"
        )
    return Segment(node_id, int(index))


def child_instance(parent: str, segment: str) -> str:
    if not segment or INSTANCE_SEPARATOR in segment:
        raise ValueError(f"invalid instance segment {segment!r}: a segment is not empty and has no {INSTANCE_SEPARATOR!r}")
    _check_segment(segment, segment)
    if parent == ROOT_INSTANCE:
        return segment
    instance_segments(parent)
    return parent + INSTANCE_SEPARATOR + segment


def each_segment(each_id: str, index: int) -> str:
    if isinstance(index, bool) or index < 0:
        raise ValueError(f"each iteration index {index!r} of {each_id} is not a non-negative integer")
    if not each_id or INSTANCE_SEPARATOR in each_id or ITEM_SEPARATOR in each_id:
        raise ValueError(f"invalid each node id {each_id!r}")
    return f"{each_id}{ITEM_SEPARATOR}{index}"


def dynamic_segment(node_id: str) -> str:
    if not node_id or INSTANCE_SEPARATOR in node_id or ITEM_SEPARATOR in node_id:
        raise ValueError(f"invalid dynamic node id {node_id!r}")
    return node_id


def instance_segments(instance: str) -> list[Segment]:
    if instance == ROOT_INSTANCE:
        return []
    return [_check_segment(segment, instance) for segment in instance.split(INSTANCE_SEPARATOR)]


def instance_prefixes(instance: str) -> list[str]:
    parts = instance.split(INSTANCE_SEPARATOR) if instance != ROOT_INSTANCE else []
    for part in parts:
        _check_segment(part, instance)
    prefixes = [INSTANCE_SEPARATOR.join(parts[:length]) for length in range(len(parts), 0, -1)]
    prefixes.append(ROOT_INSTANCE)
    return prefixes


def _value_key(node_id: str, instance: str) -> str:
    return node_id if instance == ROOT_INSTANCE else f"{node_id}@{instance}"


def _compact(value: JsonValue) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class ValueStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[tuple[str, str], JsonValue] = {}
        self._items: dict[tuple[str, str], list[JsonValue]] = {}

    def put(self, node_id: str, instance: str, value: JsonValue) -> None:
        instance_segments(instance)
        with self._lock:
            self._values[(node_id, instance)] = value

    def has(self, node_id: str, instance: str) -> bool:
        with self._lock:
            return (node_id, instance) in self._values

    def producer(self, node_id: str, instance: str) -> str | None:
        prefixes = instance_prefixes(instance)
        with self._lock:
            for prefix in prefixes:
                if (node_id, prefix) in self._values:
                    return prefix
        return None

    def get(self, node_id: str, instance: str) -> JsonValue:
        prefixes = instance_prefixes(instance)
        with self._lock:
            for prefix in prefixes:
                key = (node_id, prefix)
                if key in self._values:
                    return self._values[key]
        raise KeyError(f"no value of node {node_id} is visible from instance {instance!r}")

    def drop(self, node_id: str, instance: str) -> None:
        with self._lock:
            self._values.pop((node_id, instance), None)

    def set_items(self, each_id: str, instance: str, items: Sequence[JsonValue]) -> None:
        instance_segments(instance)
        with self._lock:
            self._items[(each_id, instance)] = list(items)

    def items(self, each_id: str, instance: str) -> list[JsonValue]:
        with self._lock:
            found = self._items.get((each_id, instance))
            if found is None:
                raise KeyError(f"each node {each_id} has no items at instance {instance!r}")
            return list(found)

    def item(self, each_id: str, instance: str) -> JsonValue:
        segments = instance_segments(instance)
        for position, segment in enumerate(segments):
            if segment.node_id != each_id or segment.index is None:
                continue
            owner = INSTANCE_SEPARATOR.join(instance.split(INSTANCE_SEPARATOR)[:position])
            with self._lock:
                found = self._items.get((each_id, owner))
            if found is None:
                raise KeyError(
                    f"each node {each_id} has no items at instance {owner!r} (consumer instance {instance!r})"
                )
            if segment.index >= len(found):
                raise KeyError(
                    f"each node {each_id} has {len(found)} items at instance {owner!r}, no item {segment.index} "
                    f"(consumer instance {instance!r})"
                )
            return found[segment.index]
        raise KeyError(f"instance {instance!r} is not inside an iteration of each node {each_id}")

    def snapshot(self, limit_bytes: int = PAUSE_VALUES_BYTES) -> dict[str, JsonValue]:
        with self._lock:
            entries = list(self._values.items())
        result: dict[str, JsonValue] = {}
        size = 2
        for position, ((node_id, instance), value) in enumerate(entries):
            key = _value_key(node_id, instance)
            entry = len(_compact(key).encode()) + 1 + len(_compact(value).encode()) + (1 if result else 0)
            if size + entry > limit_bytes:
                result["..."] = f"{len(entries) - position} more"
                break
            result[key] = value
            size += entry
        return result


def truthy(value: JsonValue) -> bool:
    return bool(value)


def fmt_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _expr_text(value: JsonValue) -> str:
    try:
        return _compact(value)
    except (TypeError, ValueError):
        return repr(value)


def _resolution_error(subject: str, expr: JsonValue, instance: str, cause: str) -> RefResolutionError:
    return RefResolutionError(
        f"cannot resolve {_expr_text(expr)} ({subject}) at instance {instance!r}: {cause}",
        detail={"node": subject, "expression": expr, "instance": instance, "cause": cause},
    )


def _subject_of(expr: dict[str, JsonValue]) -> str:
    for key in (REF_KEY, ITEM_KEY):
        target = expr.get(key)
        if isinstance(target, str):
            return f"node {target}"
    return "a $fmt"


def resolve(value: JsonValue, store: ValueStore, instance: str = ROOT_INSTANCE) -> JsonValue:
    if isinstance(value, dict):
        if is_expr_dict(value):
            return _resolve_expr(value, store, instance)
        return {key: resolve(item, store, instance) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, store, instance) for item in value]
    return value


def _resolve_expr(expr: dict[str, JsonValue], store: ValueStore, instance: str) -> JsonValue:
    subject = _subject_of(expr)
    if FMT_KEY in expr:
        parts = expr[FMT_KEY]
        if not isinstance(parts, list):
            raise _resolution_error(subject, expr, instance, f"'$fmt' must hold a list of parts, got {parts!r}")
        texts: list[str] = []
        for part in parts:
            resolved = part if isinstance(part, str) else resolve(part, store, instance)
            try:
                texts.append(fmt_text(resolved))
            except ValueError as error:
                raise _resolution_error(subject, expr, instance, str(error)) from error
        return "".join(texts)
    raw_path = expr.get("path", [])
    resolved_path = resolve(raw_path, store, instance)
    candidate: dict[str, JsonValue] = {key: item for key, item in expr.items() if key != "path"}
    if "path" in expr:
        candidate["path"] = resolved_path
    try:
        parsed = parse_expr(candidate)
    except RefError as error:
        raise _resolution_error(subject, expr, instance, str(error)) from error
    try:
        if isinstance(parsed, RefExpr):
            base = store.get(parsed.ref, instance)
        elif isinstance(parsed, ItemExpr):
            base = store.item(parsed.item, instance)
        else:
            raise _resolution_error(subject, expr, instance, "unexpected '$fmt' object")
        return copy.deepcopy(apply_path(base, parsed.path))
    except KeyError as error:
        raise _resolution_error(subject, expr, instance, str(error.args[0]) if error.args else str(error)) from error
    except RefError as error:
        raise _resolution_error(subject, expr, instance, str(error)) from error


def resolve_args(node: Node, spec: OpSpec, store: ValueStore, instance: str = ROOT_INSTANCE) -> ResolvedArgs:
    try:
        data = resolve(cast("JsonValue", node.args), store, instance)
    except RefResolutionError as error:
        raise RefResolutionError(
            f"{node.op} {node.id}: {error.message}", detail={**error.detail, "consumer": node.id}
        ) from error
    return validate_args(spec, data)


def validate_args(spec: OpSpec, data: JsonValue) -> ResolvedArgs:
    resolved = _resolved_args_model(spec.args)
    try:
        model = resolved.model_validate(data)
    except ValidationError as error:
        problems = error.errors(include_url=False, include_context=False, include_input=False)
        shown: list[JsonValue] = [
            {"loc": ".".join(str(part) for part in problem["loc"]), "msg": problem["msg"], "type": problem["type"]}
            for problem in problems[:_ARGS_ERRORS]
        ]
        first = problems[0]
        where = ".".join(str(part) for part in first["loc"]) or "(arguments)"
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise NodeFailure(
            "args_invalid",
            f"invalid arguments for {spec.name}: {where}: {first['msg']}{more}",
            detail={"op": spec.name, "errors": shown, "count": len(problems)},
        ) from error
    dumped = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    return ResolvedArgs(model=model, data=cast("dict[str, JsonValue]", dumped))


_REF_TYPES: tuple[type[BaseModel], ...] = (RefExpr, ItemExpr, FmtExpr)
_ANNOTATED: Any = Annotated
_UNION: Any = Union
_KIND_TYPES: dict[str, Any] = {
    "str": StrictStr,
    "int": StrictInt,
    "float": StrictFloat,
    "bool": StrictBool,
    "none": None,
    "media": MediaInfo,
    "artifact": Artifact,
}
_RESOLVING = threading.local()


@dataclass(frozen=True, slots=True)
class _ResolvedPoly:
    category: str

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_before_validator_function(partial(_dispatch_resolved, self.category), handler(source))


def _dispatch_resolved(category: str, value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, dict):
        kind = cast("dict[str, Any]", value).get("kind")
        if isinstance(kind, str):
            model = default_registry().catalog(category, kind).model
            return _resolved_args_model(model).model_validate(value)
    raise ValueError(f"expected an object with a string 'kind' naming a registered {category} kind, got {value!r}")


def _kind_of(metadata: Sequence[object], inherited: str | None) -> str | None:
    kinds = [item.type for item in metadata if isinstance(item, RefKind)]
    return kinds[-1] if kinds else inherited


def _kind_type(kind: str | None, ref_only: bool) -> Any:
    if kind is None:
        return JsonValue
    if ref_only and kind == "bool":
        return JsonValue
    return _KIND_TYPES.get(kind, JsonValue)


def _is_ref_type(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, _REF_TYPES)


def _resolved_metadata(metadata: Sequence[object]) -> list[object]:
    return [_ResolvedPoly(item.category) if isinstance(item, Poly) else item for item in metadata]


def _resolved_type(annotation: Any, kind: str | None) -> Any:
    if annotation is JsonValue:
        return annotation
    if _is_ref_type(annotation):
        return _kind_type(kind, ref_only=True)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _model_reference(annotation)
    if isinstance(annotation, TypeAliasType | typing_extensions.TypeAliasType):
        inner = _resolved_type(annotation.__value__, kind)
        return annotation if inner is annotation.__value__ else inner
    origin = get_origin(annotation)
    args: tuple[Any, ...] = get_args(annotation)
    if origin is Annotated:
        metadata: list[object] = list(getattr(annotation, "__metadata__", ()))
        inner_kind = _kind_of(metadata, kind)
        inner = _resolved_type(args[0], inner_kind)
        new_metadata = _resolved_metadata(metadata)
        if inner is args[0] and all(a is b for a, b in zip(new_metadata, metadata, strict=True)):
            return annotation
        return _ANNOTATED[(inner, *new_metadata)]
    if origin in (Union, UnionType):
        return _resolved_union(annotation, args, kind)
    if origin is None or origin is Literal or not args:
        return annotation
    new_args: list[Any] = [arg if arg is Ellipsis else _resolved_type(arg, kind) for arg in args]
    if all(a is b for a, b in zip(new_args, args, strict=True)):
        return annotation
    subscriptable: Any = origin
    return subscriptable[tuple(new_args) if len(new_args) > 1 else new_args[0]]


def _resolved_union(annotation: Any, members: tuple[Any, ...], kind: str | None) -> Any:
    refs = [member for member in members if _is_ref_type(member)]
    others = [member for member in members if not _is_ref_type(member)]
    resolved_others = [_resolved_type(member, kind) for member in others]
    if not refs and all(a is b for a, b in zip(resolved_others, others, strict=True)):
        return annotation
    chosen: list[Any] = []
    for member in resolved_others:
        if not any(member is seen for seen in chosen):
            chosen.append(member)
    if refs:
        replacement = _kind_type(kind, ref_only=not others or all(o is type(None) for o in others))
        if not any(replacement is seen for seen in chosen):
            chosen.insert(0, replacement)
    flattened = [type(None) if member is None else member for member in chosen]
    return _UNION[tuple(flattened)] if len(flattened) > 1 else flattened[0]


def _resolved_field(info: FieldInfo) -> tuple[Any, FieldInfo] | None:
    kind = _kind_of(info.metadata, None)
    annotation = _resolved_type(info.annotation, kind)
    metadata = _resolved_metadata(info.metadata)
    if annotation is info.annotation and all(a is b for a, b in zip(metadata, info.metadata, strict=True)):
        return None
    field = copy.copy(info)
    field.metadata = metadata
    field.annotation = annotation
    return annotation, field


def _annotation_parts(annotation: Any, seen_aliases: set[int]) -> tuple[bool, list[type[BaseModel]]]:
    if annotation is JsonValue:
        return False, []
    if _is_ref_type(annotation):
        return True, []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return False, [annotation]
    if isinstance(annotation, TypeAliasType | typing_extensions.TypeAliasType):
        if id(annotation) in seen_aliases:
            return False, []
        seen_aliases.add(id(annotation))
        return _annotation_parts(annotation.__value__, seen_aliases)
    origin = get_origin(annotation)
    args: tuple[Any, ...] = get_args(annotation)
    direct = False
    models: list[type[BaseModel]] = []
    if origin is Annotated:
        direct = any(isinstance(item, Poly) for item in getattr(annotation, "__metadata__", ()))
        args = args[:1]
    elif origin is None or origin is Literal:
        return False, []
    for arg in args:
        if arg is Ellipsis:
            continue
        found, nested = _annotation_parts(arg, seen_aliases)
        direct = direct or found
        models.extend(nested)
    return direct, models


def _model_parts(model: type[BaseModel]) -> tuple[bool, list[type[BaseModel]]]:
    direct = False
    models: list[type[BaseModel]] = []
    seen_aliases: set[int] = set()
    for info in model.model_fields.values():
        if any(isinstance(item, Poly) for item in info.metadata):
            direct = True
        found, nested = _annotation_parts(info.annotation, seen_aliases)
        direct = direct or found
        models.extend(nested)
    return direct, models


@lru_cache(maxsize=4096)
def _needs_derivation(model: type[BaseModel]) -> bool:
    pending: list[type[BaseModel]] = [model]
    visited: set[type[BaseModel]] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        direct, nested = _model_parts(current)
        if direct:
            return True
        pending.extend(item for item in nested if item not in visited)
    return False


@dataclass(slots=True)
class _Build:
    building: dict[type[BaseModel], ForwardRef]
    made: dict[type[BaseModel], type[BaseModel]]
    namespace: dict[str, type[BaseModel]]


_DERIVED: dict[type[BaseModel], type[BaseModel]] = {}
_DERIVED_LOCK = threading.RLock()
_NON_IDENTIFIER = re.compile(r"\W")


def _derive(model: type[BaseModel], build: _Build) -> type[BaseModel]:
    reference_name = f"_eks_resolved_{_NON_IDENTIFIER.sub('_', model.__name__)}_{len(build.namespace)}_{id(model):x}"
    build.building[model] = ForwardRef(reference_name)
    try:
        overrides: dict[str, Any] = {}
        for name, info in model.model_fields.items():
            resolved = _resolved_field(info)
            if resolved is not None:
                overrides[name] = resolved
    finally:
        del build.building[model]
    derived = create_model(model.__name__, __base__=model, __module__=model.__module__, **overrides)
    derived.__qualname__ = model.__qualname__
    build.namespace[reference_name] = derived
    build.made[model] = derived
    return derived


def _model_reference(model: type[BaseModel]) -> type[BaseModel] | ForwardRef:
    if not _needs_derivation(model):
        return model
    with _DERIVED_LOCK:
        cached = _DERIVED.get(model)
        if cached is not None:
            return cached
        build: _Build | None = getattr(_RESOLVING, "build", None)
        if build is None:
            return _resolved_args_model(model)
        made = build.made.get(model)
        if made is not None:
            return made
        forward = build.building.get(model)
        if forward is not None:
            return forward
        return _derive(model, build)


def _resolved_args_model(model: type[BaseModel]) -> type[BaseModel]:
    if not _needs_derivation(model):
        return model
    with _DERIVED_LOCK:
        cached = _DERIVED.get(model)
        if cached is not None:
            return cached
        outer: _Build | None = getattr(_RESOLVING, "build", None)
        build = _Build(building={}, made={}, namespace={})
        _RESOLVING.build = build
        try:
            derived = _derive(model, build)
            for made in build.made.values():
                if not made.__pydantic_complete__:
                    made.model_rebuild(force=True, _types_namespace=build.namespace)
            _DERIVED.update(build.made)
        finally:
            _RESOLVING.build = outer
        return derived


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _json_problem(value: object, where: str) -> str | None:
    if value is None or isinstance(value, str | bool | int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{where} is {value!r}, which JSON cannot hold"
    if isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            problem = _json_problem(item, f"{where}[{index}]")
            if problem is not None:
                return problem
        return None
    if isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            if not isinstance(key, str):
                return f"{where} has the key {key!r}, which is not a string"
            problem = _json_problem(item, f"{where}.{key}")
            if problem is not None:
                return problem
        return None
    return f"{where} is a {type(value).__name__}, which is not a JSON value"


def _model_problem(model: type[BaseModel], value: object) -> str | None:
    if not isinstance(value, dict):
        return f"got {_json_type(value)}"
    try:
        model.model_validate(value)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "(value)"
        return f"got an object that does not validate as {model.__name__} ({where}: {first['msg']})"
    return None


def _type_problem(expected: RefType, value: object) -> str | None:
    name = expected.name
    checks: dict[str, Callable[[object], bool]] = {
        "none": lambda v: v is None,
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "float": lambda v: isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v),
        "bool": lambda v: isinstance(v, bool),
    }
    if name in checks:
        if checks[name](value):
            return None
        if name == "float" and isinstance(value, float):
            return f"got the non-finite float {value!r}"
        return f"got {_json_type(value)}"
    if name == "json":
        return _json_problem(value, "the value")
    if name == "artifact":
        return _model_problem(Artifact, value)
    if name == "media":
        return _model_problem(MediaInfo, value)
    if name == "resource":
        return None if isinstance(value, dict) else f"got {_json_type(value)}"
    if name == "list" and expected.item is not None:
        if not isinstance(value, list):
            return f"got {_json_type(value)}"
        for index, item in enumerate(cast("list[object]", value)):
            problem = _type_problem(expected.item, item)
            if problem is not None:
                return f"item {index} is not {expected.item}: {problem}"
        return None
    return f"unknown type {expected}"


def output_problem(out: str, value: JsonValue) -> str | None:
    try:
        expected = parse_ref_type(out)
    except RefTypeError as error:
        return f"the out type {out!r} is invalid: {error}"
    problem = _type_problem(expected, value)
    if problem is None:
        return None
    return f"expected {out}, {problem}"


def checked_output(node: Node, value: JsonValue) -> JsonValue:
    problem = output_problem(node.out, value)
    if problem is not None:
        raise OutputTypeError(
            f"{node.op} {node.id} produced a value of the wrong type: {problem}",
            detail={"out": node.out, "produced": _json_type(value)},
        )
    return value


def value_digest(value: JsonValue) -> str:
    return digest(canonical_json(value))


__all__ = [
    "INSTANCE_SEPARATOR",
    "ITEM_SEPARATOR",
    "PAUSE_VALUES_BYTES",
    "ResolvedArgs",
    "Segment",
    "ValueStore",
    "checked_output",
    "child_instance",
    "dynamic_segment",
    "each_segment",
    "fmt_text",
    "instance_prefixes",
    "instance_segments",
    "output_problem",
    "resolve",
    "resolve_args",
    "truthy",
    "validate_args",
    "value_digest",
]
