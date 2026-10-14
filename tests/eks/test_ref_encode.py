from __future__ import annotations

import math
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel, Field

from eks.ref import (
    Encodable,
    ItemRef,
    Ref,
    RefUsageError,
    contains_refs,
    encode,
    sentinel_scope,
)
from eks_harness.ir.refs import RStr
from eks_harness.ir.target import ByTestId, Target

N = "n0123456789"
EACH = "n00000000ea"


class Handle:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __eks_encode__(self) -> Any:
        return self.value


class Inner(BaseModel):
    text: RStr
    note: str | None = None


class Outer(BaseModel):
    name: str = Field(alias="displayName")
    inner: Inner
    skipped: int | None = None
    values: tuple[int, ...] = ()


def test_ref_and_item() -> None:
    assert encode(Ref(N, "json").items[0]) == {"$ref": N, "path": ["items", 0]}
    assert encode(ItemRef(EACH, "json")) == {"$item": EACH, "path": []}


def test_plain_strings() -> None:
    assert encode("Kadıköy") == "Kadıköy"
    assert encode("") == ""


def test_strings_with_sentinels() -> None:
    ref = Ref(N, "json").id
    with sentinel_scope():
        assert encode(f"/x?id={ref}") == {"$fmt": ["/x?id=", {"$ref": N, "path": ["id"]}]}
        assert encode(f"{ref}") == {"$fmt": [{"$ref": N, "path": ["id"]}]}


def test_strings_with_sentinels_outside_scope_raise() -> None:
    ref = Ref(N, "json").id
    with sentinel_scope():
        text = f"{ref}"
    with pytest.raises(RefUsageError):
        encode(text)


@pytest.mark.parametrize("value", [None, True, False, 0, -7, 2**70, 1.5, -0.25])
def test_scalars_pass_through(value: Any) -> None:
    result = encode(value)
    assert result == value
    assert type(result) is type(value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_raise(value: float) -> None:
    with pytest.raises(RefUsageError, match="finite"):
        encode(value)


def test_pydantic_model_dumps_by_alias_without_none() -> None:
    ref = Ref(N, "json").name
    model = Outer.model_validate(
        {"displayName": "Harman", "inner": {"text": {"$ref": N, "path": ["name"]}}, "values": (1, 2)}
    )
    assert encode(model) == {
        "displayName": "Harman",
        "inner": {"text": {"$ref": N, "path": ["name"]}},
        "values": [1, 2],
    }
    assert contains_refs(model)
    assert encode(Inner(text="plain")) == {"text": "plain"}
    assert not contains_refs(Inner(text="plain"))
    assert encode({"r": ref}) == {"r": {"$ref": N, "path": ["name"]}}


def test_target_model() -> None:
    # Targets are plain pydantic models here; by/parse_target belong to another package.
    target = Target(by="test_id", args={"id": "row"}, nth=2)
    assert encode(target) == {"by": "test_id", "args": {"id": "row"}, "nth": 2}
    scoped = Target(by="text", args={"text": "Kadıköy"}, within=Target(by="role", args={"role": "dialog"}))
    assert encode(scoped) == {
        "by": "text",
        "args": {"text": "Kadıköy"},
        "within": {"by": "role", "args": {"role": "dialog"}},
    }
    assert not contains_refs(target)
    with_ref = Target(by="test_id", args={"id": {"$ref": N, "path": ["id"]}})
    assert contains_refs(with_ref)
    assert encode(ByTestId(id="row")) == {"id": "row"}


def test_encodable() -> None:
    handle = Handle({"$ref": N, "path": []})
    assert isinstance(handle, Encodable)
    assert encode(handle) == {"$ref": N, "path": []}
    assert encode(Handle(Ref(N, "json").x)) == {"$ref": N, "path": ["x"]}
    assert encode(Handle((1, Handle("a")))) == [1, "a"]
    assert contains_refs(Handle(Ref(N, "json")))
    assert not contains_refs(Handle([1, 2]))


def test_refs_are_not_encodable() -> None:
    assert not isinstance(Ref(N, "json"), Encodable)


def test_mappings() -> None:
    ref = Ref(N, "json").id
    assert encode({"a": 1, "b": [ref, {"c": None}]}) == {"a": 1, "b": [{"$ref": N, "path": ["id"]}, {"c": None}]}
    assert encode(OrderedDict([("z", 1), ("a", 2)])) == {"z": 1, "a": 2}
    assert list(encode(OrderedDict([("z", 1), ("a", 2)]))) == ["z", "a"]
    assert encode(MappingProxyType({"k": (1, 2)})) == {"k": [1, 2]}


@pytest.mark.parametrize("value", [{1: "a"}, {("a",): 1}, {"ok": {None: 1}}])
def test_non_string_keys_raise(value: Any) -> None:
    with pytest.raises(RefUsageError, match="string keys"):
        encode(value)


def test_sequences() -> None:
    assert encode((1, "a", (2, 3))) == [1, "a", [2, 3]]
    assert encode([]) == []
    assert encode([Ref(N, "json"), ItemRef(EACH, "json").x]) == [
        {"$ref": N, "path": []},
        {"$item": EACH, "path": ["x"]},
    ]


@pytest.mark.parametrize(
    "value",
    [Path("a/b"), Fraction(1, 3), {1, 2}, frozenset({1}), b"bytes", object(), 1j, range(3)],
)
def test_unsupported_types_raise(value: Any) -> None:
    with pytest.raises(RefUsageError, match=type(value).__name__):
        encode(value)


def test_nested_unsupported_type_raises() -> None:
    with pytest.raises(RefUsageError, match="PosixPath|WindowsPath"):
        encode({"a": [Path("x")]})


def test_contains_refs() -> None:
    ref = Ref(N, "json")
    assert contains_refs(ref)
    assert contains_refs(ItemRef(EACH, "json"))
    assert contains_refs({"a": [1, {"b": ref.x}]})
    assert contains_refs([{"$ref": N}])
    assert contains_refs({"$fmt": ["a", {"$ref": N, "path": []}]})
    with sentinel_scope():
        assert contains_refs(f"a{ref}b")
    assert not contains_refs("plain")
    assert not contains_refs({"a": [1, 2.5, None, True]})
    assert not contains_refs({"$ref": "x", "extra": 1})
    assert not contains_refs([])


def test_mapping_keys_built_from_refs_raise() -> None:
    ref = Ref(N, "json").id
    with sentinel_scope():
        with pytest.raises(RefUsageError, match="mapping key .* built from a ref"):
            encode({f"k{ref}": 1})
        with pytest.raises(RefUsageError, match="mapping key .* built from a ref"):
            encode({"outer": [{f"{ref}": "v"}]})
        with pytest.raises(RefUsageError, match="mapping key .* built from a ref"):
            contains_refs({f"k{ref}": 1})
    # stray sentinel characters in a key are rejected even outside a trace
    with pytest.raises(RefUsageError, match="mapping key"):
        encode({"k": 1})
