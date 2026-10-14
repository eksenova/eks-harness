from __future__ import annotations

import re
from typing import Any

import pytest

from eks.ref import (
    SENTINEL_CLOSE,
    SENTINEL_OPEN,
    SENTINEL_PATTERN,
    ItemRef,
    Ref,
    RefUsageError,
    SentinelTable,
    encode,
    format_text,
    is_item,
    node_of,
    path_of,
    sentinel_scope,
    to_expr,
    type_of,
)
from eks_harness.ir.refs import (
    FilterStep,
    FindStep,
    FmtExpr,
    ItemExpr,
    LenStep,
    RefExpr,
    SliceStep,
)

N = "n0123456789"
EACH = "n00000000ea"


def dump(ref: Ref[Any]) -> dict[str, Any]:
    return to_expr(ref).model_dump(mode="json", by_alias=True)


def test_design_example_path() -> None:
    ref = Ref(N, "json").items.find(name="Kadıköy").id
    assert dump(ref) == {"$ref": N, "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]}
    assert type_of(ref) == "json"
    assert node_of(ref) == N
    assert path_of(ref) == ("items", FindStep(find={"name": "Kadıköy"}), "id")
    assert not is_item(ref)


def test_accessors_on_new_ref() -> None:
    ref: Ref[Any] = Ref(N)
    assert type_of(ref) == "json"
    assert path_of(ref) == ()
    assert dump(ref) == {"$ref": N, "path": []}


def test_key_step_vs_first_property() -> None:
    base = Ref(N, "json")
    assert path_of(base["first"]) == ("first",)
    assert path_of(base.first) == (0,)
    assert path_of(base.last) == (-1,)
    assert path_of(base["find"]) == ("find",)
    assert path_of(base["with space"]) == ("with space",)


def test_index_and_slice_steps() -> None:
    base = Ref(N, "list[artifact]")
    assert path_of(base[2]) == (2,)
    assert type_of(base[2]) == "artifact"
    sliced = base[1:3]
    assert path_of(sliced) == (SliceStep.model_validate({"slice": [1, 3, None]}),)
    assert type_of(sliced) == "list[artifact]"
    assert dump(base[::2]) == {"$ref": N, "path": [{"slice": [None, None, 2]}]}
    assert type_of(base.first.url) == "str"
    assert type_of(base.last.size) == "int"


def test_filter_and_len() -> None:
    base = Ref(N, "list[json]")
    filtered = base.filter(active=True)
    assert path_of(filtered) == (FilterStep.model_validate({"filter": {"active": True}}),)
    assert type_of(filtered) == "list[json]"
    length = filtered.len()
    assert type_of(length) == "int"
    assert path_of(length)[-1] == LenStep()
    assert type_of(Ref(N, "json").items.len()) == "int"
    assert type_of(Ref(N, "str").len()) == "int"


def test_find_values_are_encoded_refs() -> None:
    other = Ref("n000000000b", "json").name
    found = Ref(N, "list[json]").find(name=other)
    assert dump(found) == {
        "$ref": N,
        "path": [{"find": {"name": {"$ref": "n000000000b", "path": ["name"]}}}],
    }
    assert type_of(found) == "json"


def test_find_and_filter_need_fields() -> None:
    with pytest.raises(RefUsageError, match="field=value"):
        Ref(N, "json").find()
    with pytest.raises(RefUsageError, match="field=value"):
        Ref(N, "json").filter()


def test_steps_return_new_refs() -> None:
    base = Ref(N, "json")
    child = base.items
    assert child is not base
    assert path_of(base) == ()
    assert child is not base.items


def test_impossible_steps_raise() -> None:
    with pytest.raises(RefUsageError, match="foo"):
        _ = Ref(N, "int").foo
    with pytest.raises(RefUsageError):
        _ = Ref(N, "str")[0]
    with pytest.raises(RefUsageError):
        Ref(N, "bool").len()
    with pytest.raises(RefUsageError, match="size"):
        _ = Ref(N, "artifact").size.foo
    with pytest.raises(RefUsageError):
        _ = Ref(N, "list[str]").name


def test_invalid_index_types() -> None:
    base = Ref(N, "json")
    with pytest.raises(RefUsageError, match="find"):
        _ = base[1.5]  # pyright: ignore[reportArgumentType, reportCallIssue]
    with pytest.raises(RefUsageError):
        _ = base[True]
    with pytest.raises(RefUsageError):
        _ = base[Ref(N, "int")]  # pyright: ignore[reportArgumentType, reportCallIssue]
    with pytest.raises(RefUsageError, match="slice"):
        _ = base[Ref(N, "int") : 2]  # pyright: ignore[reportArgumentType, reportCallIssue]
    with pytest.raises(RefUsageError, match="zero"):
        _ = base[::0]


def test_private_attributes_raise_attribute_error() -> None:
    base = Ref(N, "json")
    with pytest.raises(AttributeError):
        _ = base._x
    with pytest.raises(AttributeError):
        _ = base.__eks_encode__  # pyright: ignore[reportAttributeAccessIssue]
    assert not hasattr(base, "_private")


def test_attribute_assignment_raises() -> None:
    base = Ref(N, "json")
    with pytest.raises(AttributeError, match="read-only"):
        base.name = 1  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(AttributeError):
        base._eks_node = "n000000000z"  # pyright: ignore[reportAttributeAccessIssue]


def test_python_protocols_raise_with_hints() -> None:
    base = Ref(N, "bool")
    with pytest.raises(RefUsageError, match=r"eks\.when"):
        bool(base)
    with pytest.raises(RefUsageError, match=r"eks\.when"):
        _ = base and 1
    with pytest.raises(RefUsageError, match=r"eks\.each"):
        iter(Ref(N, "list[str]"))
    with pytest.raises(RefUsageError, match=r"eks\.each"):
        list(Ref(N, "list[str]"))
    with pytest.raises(RefUsageError, match=r"ref\.len\(\)"):
        len(Ref(N, "list[str]"))
    with pytest.raises(RefUsageError, match="membership"):
        _ = "x" in Ref(N, "list[str]")
    # __index__ lets an @eks.dynamic default such as n=s.ui.count('#row') type-check as range(n), where it is a
    # real int at run time; at plan time the conversion raises like the other protocols
    with pytest.raises(RefUsageError, match=r"@eks\.dynamic"):
        range(Ref(N, "int"))
    with pytest.raises(RefUsageError, match=r"@eks\.dynamic"):
        _ = [1, 2][Ref(N, "int")]


def test_identity_equality_and_hash() -> None:
    one = Ref(N, "json")
    two = Ref(N, "json")
    assert one == one  # noqa: PLR0124
    assert one != two
    assert hash(one) != hash(two)
    assert {one: 1}[one] == 1


def test_format_inside_scope() -> None:
    ref = Ref(N, "json").items.find(name="Kadıköy").id
    with sentinel_scope():
        text = f"/x?id={ref}"
        assert SENTINEL_OPEN in text
        result = format_text(text)
    assert isinstance(result, FmtExpr)
    assert result.fmt == ("/x?id=", to_expr(ref))
    assert result.model_dump(mode="json", by_alias=True) == {
        "$fmt": ["/x?id=", {"$ref": N, "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]}]
    }


def test_same_ref_same_token() -> None:
    one = Ref(N, "json").a
    two = Ref(N, "json").a
    with sentinel_scope() as table:
        first = f"{one}"
        assert f"{one}" == first
        assert str(one) == first
        assert format(one, "") == first
        assert f"{two}" != first
        tokens = SENTINEL_PATTERN.findall(f"{one}-{two}")
        assert len(tokens) == 2
        assert all(re.fullmatch(r"r[0-9]+", token) for token in tokens)
        assert tokens[0] != tokens[1]
        assert table.lookup(tokens[0]) is one
        assert table.lookup(tokens[1]) is two


def test_format_spec_raises() -> None:
    ref = Ref(N, "int")
    with sentinel_scope(), pytest.raises(RefUsageError, match="format specs are not supported"):
        _ = f"{ref:>5}"


def test_format_outside_scope_raises() -> None:
    ref = Ref(N, "int")
    with pytest.raises(RefUsageError, match="only inside a plan trace"):
        _ = f"{ref}"
    with pytest.raises(RefUsageError, match="only inside a plan trace"):
        str(ref)


def test_format_text_without_sentinels() -> None:
    assert format_text("plain /x?id=7") == "plain /x?id=7"
    assert format_text("") == ""


def test_format_text_drops_empty_parts() -> None:
    ref = Ref(N, "str")
    with sentinel_scope():
        result = format_text(f"{ref}{ref}")
    assert isinstance(result, FmtExpr)
    assert result.fmt == (to_expr(ref), to_expr(ref))


def test_format_text_errors() -> None:
    ref = Ref(N, "str")
    with sentinel_scope():
        good = f"{ref}"
        with pytest.raises(RefUsageError, match="broken ref sentinel"):
            format_text("a" + SENTINEL_OPEN + "b")
        with pytest.raises(RefUsageError, match="broken ref sentinel"):
            format_text(good + SENTINEL_CLOSE)
        with pytest.raises(RefUsageError, match="broken ref sentinel"):
            format_text(good[:-1])
        with pytest.raises(RefUsageError, match="unknown ref token"):
            format_text(SENTINEL_OPEN + "r99" + SENTINEL_CLOSE)
    with pytest.raises(RefUsageError, match="no plan trace"):
        format_text(good)


def test_tokens_from_another_table_are_unknown() -> None:
    ref = Ref(N, "str")
    with sentinel_scope():
        text = f"{ref}"
    with sentinel_scope(), pytest.raises(RefUsageError, match="unknown ref token"):
        format_text(text)


def test_tokens_from_another_table_are_unknown_when_that_table_holds_refs() -> None:
    # Tokens are numbered process-wide, so a busy second table never resolves
    # a foreign token to one of its own refs.
    a = Ref(N, "json").a
    b = Ref(N, "json").b
    with sentinel_scope():
        text = f"{a}"
    with sentinel_scope() as second:
        own = f"{b}"
        assert own != text
        assert format_text(own) == FmtExpr.model_validate({"$fmt": [to_expr(b)]})
        with pytest.raises(RefUsageError, match="unknown ref token"):
            format_text(text)
        with pytest.raises(RefUsageError, match="unknown ref token"):
            second.lookup(SENTINEL_PATTERN.findall(text)[0])


def test_sentinel_scope_with_explicit_table_and_nesting() -> None:
    table = SentinelTable()
    ref = Ref(N, "str")
    with sentinel_scope(table) as active:
        assert active is table
        outer = f"{ref}"
        with sentinel_scope() as inner:
            assert inner is not table
            # Token numbers are process-wide: a fresh table never reuses one.
            inner_text = f"{Ref(N, 'str')}"
            assert inner_text != outer
            with pytest.raises(RefUsageError, match="unknown ref token"):
                format_text(outer)
        assert f"{ref}" == outer
    assert table.lookup(SENTINEL_PATTERN.findall(outer)[0]) is ref


def test_sentinel_table_lookup_errors() -> None:
    table = SentinelTable()
    with pytest.raises(RefUsageError):
        table.lookup("r0")
    first = table.token(Ref(N))
    with pytest.raises(RefUsageError):
        table.lookup("x0")
    with pytest.raises(RefUsageError):
        table.lookup(first + "\n")
    second = table.token(Ref(N))
    assert int(second[1:]) > int(first[1:])


def test_repr_is_debug_form() -> None:
    ref = Ref(N, "json").items[0].id
    assert repr(ref) == f"Ref({N}.items[0].id: json)"
    assert SENTINEL_OPEN not in repr(ref)
    with sentinel_scope():
        assert SENTINEL_OPEN not in repr(ref)
    fancy = Ref(N, "json")["a b"][1:3].find(name="x").filter(k=1).len()
    assert repr(fancy) == f"Ref({N}['a b'][1:3].find(name='x').filter(k=1).len(): int)"
    assert repr(ItemRef(EACH, "json").id) == f"ItemRef({EACH}.id: json)"
    assert repr(Ref(N, "list[str]")[::-2]) == f"Ref({N}[::-2]: list[str])"


def test_item_ref_encodes_as_item() -> None:
    item: ItemRef[Any] = ItemRef(EACH, "json")
    child = item.rows[0]
    assert isinstance(child, ItemRef)
    assert is_item(child)
    expr = to_expr(child)
    assert isinstance(expr, ItemExpr)
    assert encode(child) == {"$item": EACH, "path": ["rows", 0]}
    with sentinel_scope():
        assert encode(f"/x/{item.id}") == {"$fmt": ["/x/", {"$item": EACH, "path": ["id"]}]}


def test_ref_encodes_as_ref() -> None:
    expr = to_expr(Ref(N, "json").a)
    assert isinstance(expr, RefExpr)
    assert encode(Ref(N, "json").a) == {"$ref": N, "path": ["a"]}


def test_path_keys_built_from_refs_raise() -> None:
    base = Ref(N, "json")
    other = Ref("n9876543210", "str")
    with sentinel_scope():
        with pytest.raises(RefUsageError, match=r"built from a ref.*\.find\(field=ref\)"):
            _ = base[f"x_{other}"]
        with pytest.raises(RefUsageError, match="built from a ref"):
            _ = getattr(base, f"x{other}")
        with pytest.raises(RefUsageError, match="field name .* built from a ref"):
            base.find(**{f"k{other}": 1})
        with pytest.raises(RefUsageError, match="field name .* built from a ref"):
            base.filter(**{f"k{other}": 1})
        # refs as find/filter values are the supported way to select by a ref
        assert dump(base.find(name=other)) == {
            "$ref": N,
            "path": [{"find": {"name": {"$ref": "n9876543210", "path": []}}}],
        }
