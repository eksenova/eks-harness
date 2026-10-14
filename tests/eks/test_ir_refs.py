from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from eks_harness.ir.refs import (
    FilterStep,
    FindStep,
    FmtExpr,
    ItemExpr,
    LenStep,
    RArtifact,
    RBool,
    RCond,
    RefError,
    RefExpr,
    RFloat,
    RInt,
    RJson,
    RMedia,
    RStr,
    SliceStep,
    apply_path,
    collapse_fmt,
    is_expr_dict,
    map_exprs,
    parse_expr,
    ref_kind_of,
    referenced_items,
    referenced_nodes,
    walk_exprs,
)

N1 = "n0000000001"
N2 = "n0000000002"
N3 = "n0000000003"
EACH = "n00000000ea"


def ref(node: str, *path: Any) -> dict[str, Any]:
    return {"$ref": node, "path": list(path)}


def item(node: str, *path: Any) -> dict[str, Any]:
    return {"$item": node, "path": list(path)}


# is_expr_dict / parse_expr


@pytest.mark.parametrize(
    "value",
    [
        {"$ref": "x"},
        {"$ref": "x", "path": []},
        {"$item": "x"},
        {"$item": "x", "path": ["a"]},
        {"$fmt": ["a"]},
    ],
)
def test_is_expr_dict_positive(value: Any) -> None:
    assert is_expr_dict(value)


@pytest.mark.parametrize(
    "value",
    [
        {"$ref": "x", "extra": 1},
        {"$item": "x", "path": [], "x": 2},
        {"$fmt": [], "path": []},
        {"$ref": "x", "$item": "y"},
        {"path": []},
        {},
        "$ref",
        ["$ref"],
        None,
        7,
        FmtExpr.model_validate({"$fmt": ["a"]}),
    ],
)
def test_is_expr_dict_negative(value: Any) -> None:
    assert not is_expr_dict(value)


def test_parse_expr_models() -> None:
    parsed = parse_expr(ref(N1, "items", {"find": {"name": "Kadıköy"}}, "id"))
    assert isinstance(parsed, RefExpr)
    assert parsed.ref == N1
    assert parsed.path == ("items", FindStep(find={"name": "Kadıköy"}), "id")

    parsed_item = parse_expr({"$item": EACH})
    assert isinstance(parsed_item, ItemExpr)
    assert parsed_item.item == EACH
    assert parsed_item.path == ()

    parsed_fmt = parse_expr({"$fmt": ["/x?id=", ref(N1, "id")]})
    assert isinstance(parsed_fmt, FmtExpr)
    assert parsed_fmt.fmt == ("/x?id=", RefExpr.model_validate(ref(N1, "id")))


def test_parse_expr_path_steps() -> None:
    parsed = parse_expr(ref(N1, 0, -1, {"slice": [1, None, 2]}, {"filter": {"a": 1}}, {"len": True}))
    assert isinstance(parsed, RefExpr)
    assert parsed.path == (
        0,
        -1,
        SliceStep.model_validate({"slice": [1, None, 2]}),
        FilterStep.model_validate({"filter": {"a": 1}}),
        LenStep(),
    )


@pytest.mark.parametrize(
    "value",
    [
        {"$ref": "x", "extra": 1},
        {"$ref": 5},
        {"$ref": "x", "path": [True]},
        {"$ref": "x", "path": [1.0]},
        {"$ref": "x", "path": [b"key"]},
        {"$item": "x", "path": [{"slice": [True, None, None]}]},
        {"$item": "x", "path": [{"slice": ["1", None, None]}]},
        {"$item": "x", "path": [{"slice": [1.0, None, None]}]},
        {"$ref": "x", "path": [{"slice": [None, 2.5, None]}]},
        {"$ref": "x", "path": [{"unknown": 1}]},
        {"$fmt": "not-a-list"},
        {"$item": "x", "path": [{"len": False}]},
        "text",
    ],
)
def test_parse_expr_errors(value: Any) -> None:
    with pytest.raises(RefError):
        parse_expr(value)


def test_path_dumps_always_include_path() -> None:
    assert RefExpr.model_validate({"$ref": N1}).model_dump(mode="json", by_alias=True) == {"$ref": N1, "path": []}
    assert ItemExpr.model_validate({"$item": EACH}).model_dump(mode="json", by_alias=True) == {
        "$item": EACH,
        "path": [],
    }


# walk_exprs / referenced_*


def test_walk_exprs_document_order() -> None:
    value = {
        "first": ref(N1, "a"),
        "route": {"$fmt": ["/x/", item(EACH, "id"), "?q=", ref(N2)]},
        "list": [ref(N3, "items", {"find": {"name": ref(N2, "name")}}, "id"), "plain", 7],
        "nested": {"deep": [{"x": ref(N1, "b")}]},
    }
    found = [(type(e).__name__, e.ref if isinstance(e, RefExpr) else e.item) for e in walk_exprs(value)]
    assert found == [
        ("RefExpr", N1),
        ("ItemExpr", EACH),
        ("RefExpr", N2),
        ("RefExpr", N3),
        ("RefExpr", N2),
        ("RefExpr", N1),
    ]


def test_walk_exprs_inside_filter_steps_and_items() -> None:
    value = item(EACH, {"filter": {"owner": ref(N1, "id")}}, 0)
    assert [type(e).__name__ for e in walk_exprs(value)] == ["ItemExpr", "RefExpr"]


class _Args(BaseModel):
    text: RStr
    count: RInt = 0
    data: RJson = None


def test_walk_exprs_on_models() -> None:
    model = _Args.model_validate(
        {"text": {"$fmt": ["a", ref(N2)]}, "count": ref(N1, "n"), "data": {"k": [ref(N3)]}}
    )
    assert referenced_nodes(model) == [N2, N1, N3]
    assert referenced_nodes([model, model]) == [N2, N1, N3]


def test_walk_exprs_ignores_plain_values() -> None:
    assert list(walk_exprs({"a": [1, "x", None, {"b": True}]})) == []
    assert list(walk_exprs({"$ref": "x", "extra": ref(N1)})) == [RefExpr.model_validate(ref(N1))]


def test_referenced_nodes_and_items_dedupe_in_order() -> None:
    value = [ref(N2), item(EACH), ref(N1), {"x": ref(N2, "a")}, item(EACH, "b"), item("n00000000eb")]
    assert referenced_nodes(value) == [N2, N1]
    assert referenced_items(value) == [EACH, "n00000000eb"]


# map_exprs


def test_map_exprs_collapses_all_literal_fmt() -> None:
    value: JsonValue = {"route": {"$fmt": ["a", ref(N1), "b"]}, "n": ref(N1, "n"), "keep": [1, "z"]}
    mapped = map_exprs(value, lambda expr: "X")
    assert mapped == {"route": "aXb", "n": "X", "keep": [1, "z"]}


def test_map_exprs_renders_non_string_parts_as_json_text() -> None:
    value: JsonValue = {"$fmt": ["id=", ref(N1), "&ok=", ref(N2), "&v=", ref(N3)]}
    literals: dict[str, JsonValue] = {N1: 7, N2: True, N3: {"a": [1, 2]}}

    def resolve(expr: RefExpr | ItemExpr) -> JsonValue:
        assert isinstance(expr, RefExpr)
        return literals[expr.ref]

    assert map_exprs(value, resolve) == 'id=7&ok=true&v={"a":[1,2]}'


def test_map_exprs_keeps_fmt_when_refs_remain() -> None:
    value: JsonValue = {"$fmt": ["a", "", item(EACH), "", ref(N1), "b"]}

    def only_items(expr: RefExpr | ItemExpr) -> JsonValue:
        if isinstance(expr, ItemExpr):
            return "I"
        return expr.model_dump(mode="json", by_alias=True)

    assert map_exprs(value, only_items) == {"$fmt": ["aI", ref(N1), "b"]}


def test_map_exprs_maps_nested_refs_in_path_steps_first() -> None:
    value: JsonValue = ref(N1, "items", {"find": {"name": item(EACH, "name")}}, "id")
    seen: list[Any] = []

    def resolve(expr: RefExpr | ItemExpr) -> JsonValue:
        seen.append(expr)
        if isinstance(expr, ItemExpr):
            return "Kadıköy"
        return {"$ref": "h", "path": [s if isinstance(s, str | int) else s.model_dump(by_alias=True) for s in expr.path]}

    assert map_exprs(value, resolve) == {"$ref": "h", "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]}
    assert isinstance(seen[0], ItemExpr)
    assert isinstance(seen[1], RefExpr)


def test_map_exprs_without_exprs_is_identity() -> None:
    value: JsonValue = {"a": [1, {"b": None}], "c": "text"}
    assert map_exprs(value, lambda expr: "never") == value


# apply_path

DATA: JsonValue = {
    "items": [
        {"name": "Kadıköy", "id": 7, "active": True},
        {"name": "Beşiktaş", "id": 8, "active": False},
        {"name": "Üsküdar", "id": 9, "active": True},
    ],
    "title": "Şubeler",
    "count": 3,
}


def test_apply_path_find_key() -> None:
    assert apply_path(DATA, ["items", FindStep(find={"name": "Kadıköy"}), "id"]) == 7


def test_apply_path_accepts_raw_step_dicts() -> None:
    assert apply_path(DATA, ["items", {"find": {"name": "Üsküdar"}}, "id"]) == 9  # pyright: ignore[reportArgumentType]


def test_apply_path_indexes_and_slices() -> None:
    assert apply_path(DATA, ["items", -1, "id"]) == 9
    assert apply_path(DATA, ["items", 0, "name"]) == "Kadıköy"
    sliced = apply_path(DATA, ["items", SliceStep.model_validate({"slice": [1, 3, None]})])
    assert isinstance(sliced, list)
    assert [entry["id"] for entry in sliced] == [8, 9]  # pyright: ignore[reportArgumentType, reportCallIssue, reportIndexIssue, reportOptionalSubscript]
    reversed_ids = apply_path(DATA, ["items", SliceStep.model_validate({"slice": [None, None, -1]}), 0, "id"])
    assert reversed_ids == 9


def test_apply_path_filter_and_len() -> None:
    active = apply_path(DATA, ["items", FilterStep.model_validate({"filter": {"active": True}})])
    assert active == [DATA["items"][0], DATA["items"][2]]  # pyright: ignore[reportArgumentType, reportCallIssue, reportIndexIssue, reportOptionalSubscript]
    assert apply_path(DATA, ["items", FilterStep.model_validate({"filter": {"active": 1}}), LenStep()]) == 0
    assert apply_path(DATA, ["items", FilterStep.model_validate({"filter": {"name": "x"}})]) == []
    assert apply_path(DATA, ["items", LenStep()]) == 3
    assert apply_path(DATA, ["title", LenStep()]) == 7
    assert apply_path(DATA, [LenStep()]) == 3


def test_apply_path_empty_path_is_identity() -> None:
    assert apply_path(DATA, []) is DATA


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (["missing"], "missing key"),
        (["items", 3], "out of range"),
        (["items", -4], "out of range"),
        (["items", "name"], "needs an object"),
        (["title", 0], "needs a list"),
        (["count", LenStep()], "len needs"),
        (["items", FindStep(find={"name": "Yok"})], "no element matches"),
        (["title", FindStep(find={"a": 1})], "needs a list"),
        (["items", FindStep(find={"name": {"$ref": N1, "path": []}})], "resolve the refs"),
        (["items", SliceStep.model_validate({"slice": [None, None, 0]})], "cannot be zero"),
        (["items", True], "boolean"),
        (["items", 1.0], "invalid path step"),
        (["items", {"slice": [True, None, None]}], "invalid path step"),
        (["items", {"slice": ["1", None, None]}], "invalid path step"),
        (["items", {"slice": [1.0, None, None]}], "invalid path step"),
        (["items", {"nope": 1}], "invalid path step"),
        (["items", 1.5], "invalid path step"),
    ],
)
def test_apply_path_errors(path: list[Any], message: str) -> None:
    with pytest.raises(RefError, match=message):
        apply_path(DATA, path)


def test_apply_path_find_distinguishes_bools_from_numbers() -> None:
    data: JsonValue = [{"v": 1}, {"v": True}]
    assert apply_path(data, [FindStep(find={"v": True})]) == {"v": True}
    assert apply_path(data, [FindStep(find={"v": 1.0})]) == {"v": 1}


# collapse_fmt


def test_collapse_fmt_merges_and_drops() -> None:
    fmt = FmtExpr.model_validate({"$fmt": ["a", "", "b", ref(N1), "", "c", "d"]})
    collapsed = collapse_fmt(fmt)
    assert isinstance(collapsed, FmtExpr)
    assert collapsed.model_dump(mode="json", by_alias=True) == {"$fmt": ["ab", ref(N1), "cd"]}


def test_collapse_fmt_to_plain_string() -> None:
    assert collapse_fmt(FmtExpr.model_validate({"$fmt": ["a", "", "b"]})) == "ab"
    assert collapse_fmt(FmtExpr.model_validate({"$fmt": []})) == ""


# ref_kind_of


def test_ref_kind_of() -> None:
    assert ref_kind_of(RStr) == "str"
    assert ref_kind_of(RInt) == "int"
    assert ref_kind_of(RFloat) == "float"
    assert ref_kind_of(RBool) == "bool"
    assert ref_kind_of(RJson) == "json"
    assert ref_kind_of(RCond) == "bool"
    assert ref_kind_of(RArtifact) == "artifact"
    assert ref_kind_of(RMedia | None) == "media"
    assert ref_kind_of(tuple[RStr, ...]) == "str"
    assert ref_kind_of(list[RArtifact]) == "artifact"
    assert ref_kind_of(dict[str, RJson]) == "json"
    assert ref_kind_of(str) is None
    assert ref_kind_of(int | None) is None
    assert ref_kind_of(dict[str, int]) is None


class _Media(BaseModel):
    clip: RMedia | None = None
    texts: tuple[RStr, ...] = ()


def test_ref_kind_of_reads_field_info() -> None:
    assert ref_kind_of(_Args.model_fields["text"]) == "str"
    assert ref_kind_of(_Args.model_fields["count"]) == "int"
    assert ref_kind_of(_Media.model_fields["clip"]) == "media"
    assert ref_kind_of(_Media.model_fields["texts"]) == "str"


# field aliases on a model


class _Fields(BaseModel):
    name: RStr
    count: RInt
    ratio: RFloat = 1.0
    flag: RBool = False
    clip: RMedia | None = None


def test_field_aliases_validate_literals_and_exprs() -> None:
    literal = _Fields.model_validate({"name": "Kadıköy", "count": 3})
    assert literal.name == "Kadıköy"
    assert literal.count == 3

    model = _Fields.model_validate(
        {
            "name": {"$fmt": ["/x?id=", ref(N1, "id")]},
            "count": ref(N1, "items", {"len": True}),
            "ratio": item(EACH, "r"),
            "flag": ref(N2),
            "clip": ref(N3),
        }
    )
    assert isinstance(model.name, FmtExpr)
    assert isinstance(model.count, RefExpr)
    assert isinstance(model.ratio, ItemExpr)
    assert isinstance(model.clip, RefExpr)
    assert model.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "name": {"$fmt": ["/x?id=", ref(N1, "id")]},
        "count": ref(N1, "items", {"len": True}),
        "ratio": item(EACH, "r"),
        "flag": ref(N2),
        "clip": ref(N3),
    }


def test_field_aliases_reject_wrong_shapes() -> None:
    with pytest.raises(ValidationError):
        _Fields.model_validate({"name": "a", "count": 1, "clip": "song.ogg"})
    with pytest.raises(ValidationError):
        _Fields.model_validate({"name": {"$ref": N1, "extra": 1}, "count": 1})


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "a", "count": "5"},
        {"name": "a", "count": True},
        {"name": "a", "count": 5.0},
        {"name": "a", "count": 1, "flag": "yes"},
        {"name": "a", "count": 1, "flag": 1},
        {"name": "a", "count": 1, "ratio": "1.5"},
        {"name": "a", "count": 1, "ratio": True},
        {"name": 5, "count": 1},
    ],
)
def test_field_aliases_reject_coercible_scalars(fields: dict[str, Any]) -> None:
    # Scalars are strict so JSON-authored and builder-authored plans store the same values.
    with pytest.raises(ValidationError):
        _Fields.model_validate(fields)
    with pytest.raises(ValidationError):
        _Fields.model_validate_json(json.dumps(fields))


def test_field_alias_str_rejects_bytes() -> None:
    with pytest.raises(ValidationError):
        _Fields.model_validate({"name": b"a", "count": 1})


def test_field_alias_float_accepts_ints() -> None:
    assert _Fields.model_validate({"name": "a", "count": 1, "ratio": 2}).ratio == 2.0
    assert _Fields.model_validate_json('{"name": "a", "count": 1, "ratio": 2}').ratio == 2.0


def test_map_exprs_splices_fmt_results_into_fmt() -> None:
    value: JsonValue = {"$fmt": ["/a/", ref(N1), "/b"]}

    def to_fmt(expr: RefExpr | ItemExpr) -> JsonValue:
        return {"$fmt": ["x-", item(EACH), ""]}

    assert map_exprs(value, to_fmt) == {"$fmt": ["/a/x-", item(EACH), "/b"]}


# expression models accept only their canonical $-keyed / aliased forms


@pytest.mark.parametrize(
    "fields",
    [
        {"name": {"ref": N1}, "count": 1},
        {"name": {"item": EACH}, "count": 1},
        {"name": {"fmt": ["/x?id=", ref(N1)]}, "count": 1},
        {"name": {"$fmt": ["/x?id=", {"ref": N1}]}, "count": 1},
        {"name": "a", "count": {"ref": N1, "path": ["n"]}},
        {"name": "a", "count": {"item": EACH}},
    ],
)
def test_field_aliases_reject_field_name_keys(fields: dict[str, Any]) -> None:
    # Only the $-keyed forms are refs; the raw-JSON tooling (walk_exprs, hashing) sees nothing else.
    with pytest.raises(ValidationError):
        _Fields.model_validate(fields)
    with pytest.raises(ValidationError):
        _Fields.model_validate_json(json.dumps(fields))


@pytest.mark.parametrize(
    "step",
    [{"slice_": [0, 1, None]}, {"filter_": {"a": 1}}, {"len_": True}, {"len_": False}],
)
def test_path_steps_reject_field_names(step: dict[str, Any]) -> None:
    data = {"$ref": N1, "path": [step]}
    with pytest.raises(ValidationError, match="unknown key"):
        RefExpr.model_validate(data)
    # pydantic's JSON fast path silently drops by-name keys unless a before validator runs
    with pytest.raises(ValidationError, match="unknown key"):
        RefExpr.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "data",
    [{"ref": N1}, {"item": EACH}, {"fmt": ["a"]}],
)
def test_expr_models_reject_field_names(data: dict[str, Any]) -> None:
    for model in (RefExpr, ItemExpr, FmtExpr):
        with pytest.raises(ValidationError):
            model.model_validate(data)
