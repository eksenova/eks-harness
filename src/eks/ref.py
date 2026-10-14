from __future__ import annotations

import itertools
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NewType, Protocol, cast, overload, runtime_checkable

from pydantic import BaseModel, JsonValue

from eks_harness.ir.refs import (
    FMT_KEY,
    ITEM_KEY,
    REF_KEY,
    FilterStep,
    FindStep,
    FmtExpr,
    ItemExpr,
    LenStep,
    PathStep,
    RefExpr,
    SliceStep,
    is_expr_dict,
)
from eks_harness.ir.time import Time, format_time
from eks_harness.ir.types import Artifact, MediaInfo, RefTypeError, infer_path

Json = JsonValue
type JsonLike = Json | Ref[Any] | Encodable | Mapping[str, JsonLike] | Sequence[JsonLike]
Persona = NewType("Persona", str)

SENTINEL_OPEN = "\ue000"
SENTINEL_CLOSE = "\ue001"
SENTINEL_PATTERN = re.compile("\ue000(r[0-9]+)\ue001")
_TOKEN = re.compile("r([0-9]+)")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RefUsageError(TypeError):
    pass


@runtime_checkable
class Encodable(Protocol):
    def __eks_encode__(self) -> Any: ...


_TOKEN_NUMBERS = itertools.count()


class SentinelTable:
    def __init__(self) -> None:
        self._refs: dict[int, Ref[Any]] = {}
        self._numbers: dict[int, int] = {}

    def token(self, ref: Ref[Any]) -> str:
        number = self._numbers.get(id(ref))
        if number is None:
            number = next(_TOKEN_NUMBERS)
            self._refs[number] = ref
            self._numbers[id(ref)] = number
        return f"r{number}"

    def lookup(self, token: str) -> Ref[Any]:
        match = _TOKEN.fullmatch(token)
        ref = self._refs.get(int(match.group(1))) if match is not None else None
        if ref is None:
            raise RefUsageError(
                f"unknown ref token {token!r}; the string was built from a ref of another plan trace"
            )
        return ref


_TABLE: ContextVar[SentinelTable | None] = ContextVar("eks_sentinels", default=None)


@contextmanager
def sentinel_scope(table: SentinelTable | None = None) -> Iterator[SentinelTable]:
    current = table or SentinelTable()
    token = _TABLE.set(current)
    try:
        yield current
    finally:
        _TABLE.reset(token)


class Ref[T]:
    __slots__ = ("_eks_node", "_eks_path", "_eks_type")
    __eks_resolved__: T

    def __init__(self, node: str, type_: str = "json", path: tuple[PathStep, ...] = ()) -> None:
        object.__setattr__(self, "_eks_node", node)
        object.__setattr__(self, "_eks_type", type_)
        object.__setattr__(self, "_eks_path", path)

    def __getattr__(self, name: str) -> Ref[Json]:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._eks_step(name)

    @overload
    def __getitem__(self, key: slice) -> Ref[Json]: ...

    @overload
    def __getitem__(self, key: int | str) -> Ref[Json]: ...

    def __getitem__(self, key: int | str | slice) -> Ref[Json]:
        if isinstance(key, slice):
            bounds = (key.start, key.stop, key.step)
            if any(bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)) for bound in bounds):
                raise RefUsageError(
                    f"slice bounds of {self!r} must be integers or None, got {bounds!r}; "
                    "dynamic bounds are not supported"
                )
            if key.step == 0:
                raise RefUsageError(f"slice step of {self!r} cannot be zero")
            return self._eks_step(SliceStep.model_validate({"slice": bounds}))
        if isinstance(key, bool) or not isinstance(key, int | str):
            raise RefUsageError(
                f"{self!r}[{key!r}]: index with a literal int, str or slice; "
                "select by value with .find(field=value) or .filter(field=value)"
            )
        return self._eks_step(key)

    def find(self, **eq: JsonLike) -> Ref[Json]:
        return self._eks_step(FindStep.model_validate({"find": self._eks_fields("find", eq)}))

    def filter(self, **eq: JsonLike) -> Ref[Json]:
        return self._eks_step(FilterStep.model_validate({"filter": self._eks_fields("filter", eq)}))

    @property
    def first(self) -> Ref[Json]:
        return self._eks_step(0)

    @property
    def last(self) -> Ref[Json]:
        return self._eks_step(-1)

    def len(self) -> Ref[int]:
        return self._eks_step(LenStep())

    def _eks_fields(self, method: str, eq: dict[str, Any]) -> dict[str, Any]:
        if not eq:
            raise RefUsageError(f"{self!r}.{method}() needs at least one field=value pair")
        for name in eq:
            if _has_sentinel(name):
                raise RefUsageError(
                    f"{self!r}.{method}() field name {name!r} is built from a ref; "
                    "field names must be literal, only the values can be refs"
                )
        return {name: encode(value) for name, value in eq.items()}

    def _eks_step(self, step: PathStep) -> Ref[Any]:
        if isinstance(step, str) and _has_sentinel(step):
            raise RefUsageError(
                f"{self!r}: path key {step!r} is built from a ref; keys must be literal, "
                "select by value with .find(field=ref) or .filter(field=ref)"
            )
        try:
            new_type = infer_path(self._eks_type, (step,))
        except RefTypeError as error:
            raise RefUsageError(f"{self!r}: {error}") from error
        return type(self)(self._eks_node, new_type, (*self._eks_path, step))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Refs are read-only path expressions")

    def __format__(self, spec: str) -> str:
        if spec:
            raise RefUsageError(
                f"format specs are not supported on refs ({self!r} with {spec!r}); "
                "format the value in the op that consumes it"
            )
        table = _TABLE.get()
        if table is None:
            raise RefUsageError(f"format refs only inside a plan trace; {self!r} was formatted outside eks.plan(...)")
        return SENTINEL_OPEN + table.token(self) + SENTINEL_CLOSE

    def __str__(self) -> str:
        return self.__format__("")

    def __repr__(self) -> str:
        steps = "".join(_repr_step(step) for step in self._eks_path)
        return f"{type(self).__name__}({self._eks_node}{steps}: {self._eks_type})"

    def __bool__(self) -> bool:
        raise RefUsageError(
            f"{self!r} has no value at plan time; branch with `with eks.when(ref):` instead of if/and/or/not"
        )

    def __iter__(self) -> Iterator[Any]:
        raise RefUsageError(f"{self!r} has no value at plan time; loop with `with eks.each(ref) as item:`")

    def __len__(self) -> int:
        raise RefUsageError(f"{self!r} has no value at plan time; use ref.len() instead of len(ref)")

    def __index__(self) -> int:
        raise RefUsageError(
            f"{self!r} has no value at plan time; read the number inside an @eks.dynamic function that takes "
            "the ref as a default, for example def per_row(n=s.ui.count('#row')), or loop with eks.each"
        )

    def __contains__(self, item: object) -> bool:
        raise RefUsageError(
            f"{self!r} has no value at plan time; test membership with ref.find(...), "
            "ref.filter(...).len() or expect(ref).contains(...)"
        )


def _repr_step(step: PathStep) -> str:
    if isinstance(step, str):
        return f".{step}" if _IDENTIFIER.fullmatch(step) else f"[{step!r}]"
    if isinstance(step, int):
        return f"[{step}]"
    if isinstance(step, SliceStep):
        start, stop, stride = ("" if bound is None else str(bound) for bound in step.slice_)
        return f"[{start}:{stop}{':' + stride if stride else ''}]"
    if isinstance(step, FindStep):
        return f".find({_repr_fields(step.find)})"
    if isinstance(step, FilterStep):
        return f".filter({_repr_fields(step.filter_)})"
    return ".len()"


def _repr_fields(fields: dict[str, Any]) -> str:
    return ", ".join(f"{name}={value!r}" for name, value in fields.items())


class ItemRef[T](Ref[T]):
    __slots__ = ()


Media = Ref[MediaInfo]
ArtifactRef = Ref[Artifact]


def node_of(ref: Ref[Any]) -> str:
    return ref._eks_node


def type_of(ref: Ref[Any]) -> str:
    return ref._eks_type


def path_of(ref: Ref[Any]) -> tuple[PathStep, ...]:
    return ref._eks_path


def is_item(ref: Ref[Any]) -> bool:
    return isinstance(ref, ItemRef)


def to_expr(ref: Ref[Any]) -> RefExpr | ItemExpr:
    if isinstance(ref, ItemRef):
        return ItemExpr.model_validate({ITEM_KEY: ref._eks_node, "path": ref._eks_path})
    return RefExpr.model_validate({REF_KEY: ref._eks_node, "path": ref._eks_path})


def format_text(text: str) -> str | FmtExpr:
    if not _has_sentinel(text):
        return text
    table = _TABLE.get()
    if table is None:
        raise RefUsageError(f"{text!r} contains ref sentinels but no plan trace is active")
    parts: list[str | RefExpr | ItemExpr] = []
    position = 0
    for match in SENTINEL_PATTERN.finditer(text):
        parts.append(_literal_part(text, text[position:match.start()]))
        parts.append(to_expr(table.lookup(match.group(1))))
        position = match.end()
    parts.append(_literal_part(text, text[position:]))
    return FmtExpr.model_validate({FMT_KEY: [part for part in parts if part != ""]})


def _has_sentinel(text: str) -> bool:
    return SENTINEL_OPEN in text or SENTINEL_CLOSE in text


def _literal_part(text: str, part: str) -> str:
    if _has_sentinel(part):
        raise RefUsageError(
            f"{text!r} contains a broken ref sentinel; build strings from refs only with f-strings or str(ref)"
        )
    return part


def encode(value: Any) -> Any:
    if isinstance(value, Ref):
        return to_expr(value).model_dump(mode="json", by_alias=True)
    if isinstance(value, str):
        text = format_text(value)
        return text if isinstance(text, str) else text.model_dump(mode="json", by_alias=True)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RefUsageError(f"{value!r} is not a finite number and cannot be encoded in a plan")
        return value
    if isinstance(value, Time):
        return format_time(value)
    if isinstance(value, BaseModel):
        return encode(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    if isinstance(value, Encodable):
        return encode(value.__eks_encode__())
    if isinstance(value, Mapping):
        mapping = cast("Mapping[Any, Any]", value)
        result: dict[str, Any] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise RefUsageError(
                    f"mapping key {key!r} is a {type(key).__name__}; plan values use string keys only"
                )
            if _has_sentinel(key):
                raise RefUsageError(
                    f"mapping key {key!r} is built from a ref; refs can only be values, not keys "
                    "(build the key in the op or use a list of pairs)"
                )
            result[key] = encode(item)
        return result
    if isinstance(value, list | tuple):
        return [encode(item) for item in cast("Sequence[Any]", value)]
    raise RefUsageError(
        f"cannot encode {type(value).__name__} {value!r} in a plan; "
        "pass JSON values, refs, Time, Target or pydantic models (convert paths with str())"
    )


def contains_refs(value: Any) -> bool:
    return _has_expr(encode(value))


def _has_expr(value: Any) -> bool:
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        return is_expr_dict(mapping) or any(_has_expr(item) for item in mapping.values())
    if isinstance(value, list):
        return any(_has_expr(item) for item in cast("list[Any]", value))
    return False


__all__ = [
    "SENTINEL_CLOSE",
    "SENTINEL_OPEN",
    "SENTINEL_PATTERN",
    "ArtifactRef",
    "Encodable",
    "ItemRef",
    "Json",
    "JsonLike",
    "Media",
    "Persona",
    "Ref",
    "RefUsageError",
    "SentinelTable",
    "contains_refs",
    "encode",
    "format_text",
    "is_item",
    "node_of",
    "path_of",
    "sentinel_scope",
    "to_expr",
    "type_of",
]
