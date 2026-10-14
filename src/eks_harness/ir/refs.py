from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from types import UnionType
from typing import (
    Annotated,
    Any,
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
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from pydantic.fields import FieldInfo

REF_KEY = "$ref"
ITEM_KEY = "$item"
FMT_KEY = "$fmt"


@dataclass(frozen=True, slots=True)
class RefKind:
    type: str


class RefError(ValueError):
    pass


def reject_field_name_keys(model: type[BaseModel], data: object) -> object:
    if isinstance(data, dict):
        keys = cast("dict[object, object]", data)
        for name, field in model.model_fields.items():
            if field.alias is not None and field.alias != name and name in keys:
                raise ValueError(f"unknown key {name!r} in {model.__name__}; the key is {field.alias!r}")
    return data


class _Expr(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_by_alias=True, serialize_by_alias=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_field_name_keys(cls, data: object) -> object:
        return reject_field_name_keys(cls, data)


class FindStep(_Expr):
    find: dict[str, JsonValue] = Field(description="Field values the first matching list element must have")


class FilterStep(_Expr):
    filter_: dict[str, JsonValue] = Field(alias="filter", description="Field values every kept list element must have")


class SliceStep(_Expr):
    slice_: tuple[StrictInt | None, StrictInt | None, StrictInt | None] = Field(alias="slice", description="Python slice bounds (start, stop, step)")


class LenStep(_Expr):
    len_: Literal[True] = Field(default=True, alias="len", description="Take the length of the value")


PathStep = StrictStr | StrictInt | FindStep | FilterStep | SliceStep | LenStep


class RefExpr(_Expr):
    ref: str = Field(alias=REF_KEY, description="Id of the node whose output is read")
    path: tuple[PathStep, ...] = Field(default=(), description="Path into the output: keys, indexes and steps")


class ItemExpr(_Expr):
    item: str = Field(alias=ITEM_KEY, description="Id of the eks.each container whose current item is read")
    path: tuple[PathStep, ...] = Field(default=(), description="Path into the item")


class FmtExpr(_Expr):
    fmt: tuple[str | RefExpr | ItemExpr, ...] = Field(alias=FMT_KEY, description="Text parts and refs joined into one string at run time")


ValueExpr = RefExpr | ItemExpr

RStr = Annotated[StrictStr | FmtExpr | RefExpr | ItemExpr, RefKind("str")]
RInt = Annotated[StrictInt | RefExpr | ItemExpr, RefKind("int")]
RFloat = Annotated[StrictFloat | RefExpr | ItemExpr, RefKind("float")]
RBool = Annotated[StrictBool | RefExpr | ItemExpr, RefKind("bool")]
RJson = Annotated[JsonValue, RefKind("json")]
RCond = Annotated[RefExpr | ItemExpr, RefKind("bool")]
RMedia = Annotated[RefExpr | ItemExpr, RefKind("media")]
RArtifact = Annotated[RefExpr | ItemExpr, RefKind("artifact")]
RList = Annotated[RefExpr | ItemExpr, RefKind("list[json]")]


_EXPR_KEY_SETS = frozenset({
    frozenset({REF_KEY}),
    frozenset({REF_KEY, "path"}),
    frozenset({ITEM_KEY}),
    frozenset({ITEM_KEY, "path"}),
    frozenset({FMT_KEY}),
})


def is_expr_dict(value: Any) -> bool:
    return isinstance(value, dict) and frozenset(cast("dict[Any, Any]", value)) in _EXPR_KEY_SETS


def parse_expr(value: Any) -> RefExpr | ItemExpr | FmtExpr:
    if not is_expr_dict(value):
        raise RefError(
            f"expected a {{'$ref'|'$item': id, 'path': [...]}} or {{'$fmt': [...]}} object, got {value!r}"
        )
    model: type[RefExpr | ItemExpr | FmtExpr]
    if REF_KEY in value:
        model = RefExpr
    elif ITEM_KEY in value:
        model = ItemExpr
    else:
        model = FmtExpr
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise RefError(f"invalid {model.__name__} {value!r}: {error.errors()[0]['msg']}") from error


def walk_exprs(value: Any) -> Iterator[RefExpr | ItemExpr]:
    if isinstance(value, RefExpr | ItemExpr):
        yield value
        yield from _walk_steps(value.path)
    elif isinstance(value, FmtExpr):
        for part in value.fmt:
            yield from walk_exprs(part)
    elif isinstance(value, FindStep):
        yield from walk_exprs(value.find)
    elif isinstance(value, FilterStep):
        yield from walk_exprs(value.filter_)
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from walk_exprs(getattr(value, name))
        if value.model_extra:
            yield from walk_exprs(value.model_extra)
    elif isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        if is_expr_dict(mapping):
            yield from walk_exprs(parse_expr(mapping))
        else:
            for item in mapping.values():
                yield from walk_exprs(item)
    elif isinstance(value, list | tuple):
        for item in cast("Sequence[Any]", value):
            yield from walk_exprs(item)


def _walk_steps(path: Sequence[PathStep]) -> Iterator[RefExpr | ItemExpr]:
    for step in path:
        yield from walk_exprs(step)


def referenced_nodes(value: Any) -> list[str]:
    return list(dict.fromkeys(expr.ref for expr in walk_exprs(value) if isinstance(expr, RefExpr)))


def referenced_items(value: Any) -> list[str]:
    return list(dict.fromkeys(expr.item for expr in walk_exprs(value) if isinstance(expr, ItemExpr)))


def map_exprs(value: JsonValue, fn: Callable[[RefExpr | ItemExpr], JsonValue]) -> JsonValue:
    if isinstance(value, dict):
        if is_expr_dict(value):
            return _map_expr(value, fn)
        return {key: map_exprs(item, fn) for key, item in value.items()}
    if isinstance(value, list):
        return [map_exprs(item, fn) for item in value]
    return value


def _map_expr(value: dict[str, JsonValue], fn: Callable[[RefExpr | ItemExpr], JsonValue]) -> JsonValue:
    if FMT_KEY in value:
        parts = value[FMT_KEY]
        if not isinstance(parts, list):
            raise RefError(f"'$fmt' must hold a list of parts, got {parts!r}")
        return _join_parts([map_exprs(part, fn) for part in parts])
    mapped_value = dict(value)
    if "path" in value:
        mapped_value["path"] = map_exprs(value["path"], fn)
    expr = parse_expr(mapped_value)
    if isinstance(expr, FmtExpr):
        raise RefError(f"unexpected '$fmt' object {value!r}")
    return fn(expr)


def _join_parts(parts: list[JsonValue]) -> JsonValue:
    joined: list[JsonValue] = []
    for part in _flatten_parts(parts):
        if is_expr_dict(part):
            joined.append(part)
            continue
        text = _render_text(part)
        if not text:
            continue
        if joined and isinstance(joined[-1], str):
            joined[-1] = joined[-1] + text
        else:
            joined.append(text)
    if all(isinstance(part, str) for part in joined):
        return "".join(cast("list[str]", joined))
    return {FMT_KEY: joined}


def _flatten_parts(parts: list[JsonValue]) -> Iterator[JsonValue]:
    for part in parts:
        if isinstance(part, dict) and is_expr_dict(part) and FMT_KEY in part:
            inner = part[FMT_KEY]
            yield from _flatten_parts(inner if isinstance(inner, list) else [inner])
        else:
            yield part


def _render_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


_STEP_MODELS: dict[str, type[FindStep | FilterStep | SliceStep | LenStep]] = {
    "find": FindStep,
    "filter": FilterStep,
    "slice": SliceStep,
    "len": LenStep,
}
_STEP_HINT = "use a key, an index, {'slice': [...]}, {'find': {...}}, {'filter': {...}} or {'len': true}"


def apply_path(value: JsonValue, path: Sequence[PathStep]) -> JsonValue:
    current = value
    for position, raw_step in enumerate(path):
        current = _apply_step(current, _coerce_step(raw_step), path, position)
    return current


def _coerce_step(step: Any) -> PathStep:
    if isinstance(step, bool):
        raise RefError(f"path step {step!r} is a boolean; use an integer index or a key")
    if isinstance(step, str | int | FindStep | FilterStep | SliceStep | LenStep):
        return step
    model = _STEP_MODELS.get(next(iter(cast("dict[Any, Any]", step))), None) if _single_key(step) else None
    if model is None:
        raise RefError(f"invalid path step {step!r}; {_STEP_HINT}")
    try:
        return model.model_validate(step)
    except ValidationError as error:
        raise RefError(f"invalid path step {step!r}; {_STEP_HINT}") from error


def _single_key(step: Any) -> bool:
    return isinstance(step, dict) and len(cast("dict[Any, Any]", step)) == 1


def _where(path: Sequence[PathStep], position: int) -> str:
    return f"step {position} ({path[position]!r}) of path {list(path)!r}"


def _apply_step(value: JsonValue, step: PathStep, path: Sequence[PathStep], position: int) -> JsonValue:
    if isinstance(step, LenStep):
        if isinstance(value, list | dict | str):
            return len(value)
        raise RefError(f"{_where(path, position)}: len needs a list, an object or a string, got {_kind(value)}")
    if isinstance(step, str):
        if not isinstance(value, dict):
            raise RefError(f"{_where(path, position)}: key {step!r} needs an object, got {_kind(value)}")
        if step not in value:
            raise RefError(f"{_where(path, position)}: missing key {step!r}; keys are {sorted(value)!r}")
        return value[step]
    if not isinstance(value, list):
        raise RefError(f"{_where(path, position)}: needs a list, got {_kind(value)}")
    if isinstance(step, int):
        if not -len(value) <= step < len(value):
            raise RefError(f"{_where(path, position)}: index {step} is out of range for a list of {len(value)}")
        return value[step]
    if isinstance(step, SliceStep):
        start, stop, stride = step.slice_
        if stride == 0:
            raise RefError(f"{_where(path, position)}: slice step cannot be zero")
        return value[start:stop:stride]
    if isinstance(step, FindStep):
        for element in value:
            if _matches(element, step.find, path, position):
                return element
        raise RefError(f"{_where(path, position)}: no element matches {step.find!r}")
    return [element for element in value if _matches(element, step.filter_, path, position)]


def _matches(element: JsonValue, fields: dict[str, JsonValue], path: Sequence[PathStep], position: int) -> bool:
    if next(walk_exprs(fields), None) is not None:
        raise RefError(f"{_where(path, position)}: resolve the refs in {fields!r} before applying the path")
    if not isinstance(element, dict):
        return False
    return all(key in element and _json_equal(element[key], expected) for key, expected in fields.items())


def _json_equal(left: JsonValue, right: JsonValue) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def _kind(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    return "an object"


def collapse_fmt(value: FmtExpr) -> str | FmtExpr:
    parts: list[str | RefExpr | ItemExpr] = []
    for part in value.fmt:
        if isinstance(part, str):
            if not part:
                continue
            if parts and isinstance(parts[-1], str):
                parts[-1] = parts[-1] + part
                continue
        parts.append(part)
    if all(isinstance(part, str) for part in parts):
        return "".join(cast("list[str]", parts))
    return FmtExpr.model_validate({FMT_KEY: parts})


def _last_kind(metadata: Sequence[object]) -> str | None:
    kinds = [meta.type for meta in metadata if isinstance(meta, RefKind)]
    return kinds[-1] if kinds else None


def ref_kind_of(annotation: Any) -> str | None:
    return _ref_kind_of(annotation, set())


def _ref_kind_of(annotation: Any, seen: set[int]) -> str | None:
    if id(annotation) in seen:
        return None
    seen.add(id(annotation))
    if isinstance(annotation, FieldInfo):
        own = _last_kind(annotation.metadata)
        return own if own is not None else _ref_kind_of(annotation.annotation, seen)
    if isinstance(annotation, TypeAliasType | typing_extensions.TypeAliasType):
        return _ref_kind_of(annotation.__value__, seen)
    origin = get_origin(annotation)
    if origin is Annotated:
        own = _last_kind(annotation.__metadata__)
        return own if own is not None else _ref_kind_of(get_args(annotation)[0], seen)
    if origin in (Union, UnionType, tuple, list):
        for arg in get_args(annotation):
            found = _ref_kind_of(arg, seen)
            if found is not None:
                return found
        return None
    if origin is dict:
        args = get_args(annotation)
        return _ref_kind_of(args[1], seen) if len(args) == 2 else None
    return None


__all__ = [
    "FMT_KEY",
    "ITEM_KEY",
    "REF_KEY",
    "FilterStep",
    "FindStep",
    "FmtExpr",
    "ItemExpr",
    "LenStep",
    "PathStep",
    "RArtifact",
    "RBool",
    "RCond",
    "RFloat",
    "RInt",
    "RJson",
    "RList",
    "RMedia",
    "RStr",
    "RefError",
    "RefExpr",
    "RefKind",
    "SliceStep",
    "ValueExpr",
    "apply_path",
    "collapse_fmt",
    "is_expr_dict",
    "map_exprs",
    "parse_expr",
    "ref_kind_of",
    "referenced_items",
    "referenced_nodes",
    "reject_field_name_keys",
    "walk_exprs",
]
