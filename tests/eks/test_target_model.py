from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eks_harness.ir.hashing import canonical_json
from eks_harness.ir.refs import FmtExpr, RefExpr
from eks_harness.ir.target import (
    BUILTIN_KINDS,
    By,
    Target,
    TargetError,
    TargetKind,
    by,
    describe,
    parse_target,
    validate_args,
)


def dump(target: Target) -> dict[str, object]:
    return target.model_dump(mode="json", by_alias=True, exclude_none=True)


def canonical(target: Target) -> str:
    return json.dumps(dump(target), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Chip(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    tone: str | None = None


CHIP = TargetKind("chip", _Chip, frozenset({"web"}), "chip", "a chip by label")


# design 4.5, line by line


def test_test_id_matches_hash_shorthand() -> None:
    target = by.test_id("credit-add-submit")
    assert target == parse_target("#credit-add-submit")
    assert dump(target) == {"by": "test_id", "args": {"id": "credit-add-submit"}}


def test_role_with_name_matches_shorthand() -> None:
    target = by.role("button", name="Sunuyu iptal et")
    assert target == parse_target("role=button:Sunuyu iptal et")
    assert dump(target) == {"by": "role", "args": {"role": "button", "name": "Sunuyu iptal et", "exact": False}}


def test_text_exact_flags() -> None:
    assert by.text("Kadıköy").args == {"text": "Kadıköy", "exact": True}
    assert by.text("Kadı", exact=False).args == {"text": "Kadı", "exact": False}
    assert by.text("Kadıköy") == parse_target("Kadıköy")


def test_label_and_placeholder() -> None:
    assert dump(by.label("E-posta")) == {"by": "label", "args": {"text": "E-posta", "exact": False}}
    assert dump(by.placeholder("Ara")) == {"by": "placeholder", "args": {"text": "Ara", "exact": False}}
    assert by.label("E-posta") == parse_target("label=E-posta")
    assert by.placeholder("Ara") == parse_target("placeholder=Ara")
    assert by.label("E-posta", exact=True).args["exact"] is True


def test_css_kind_object_layer() -> None:
    assert dump(by.css("li[data-package-id='3']")) == {
        "by": "css",
        "args": {"selector": "li[data-package-id='3']"},
    }
    assert dump(by.kind("button")) == {"by": "kind", "args": {"kind": "button"}}
    assert dump(by.object("Phone")) == {"by": "object", "args": {"name": "Phone"}}
    assert dump(by.layer("title")) == {"by": "layer", "args": {"name": "title"}}


def test_kind_rejects_unknown_node_kind() -> None:
    with pytest.raises(TargetError, match="kind"):
        by.kind("slider")  # pyright: ignore[reportArgumentType]


def test_point_matches_shorthand_and_stores_floats() -> None:
    target = by.point(120, 480)
    assert target == parse_target("point=120,480")
    assert dump(target) == {"by": "point", "args": {"x": 120.0, "y": 480.0}}


def test_rect_validates_size() -> None:
    assert dump(by.rect(1, 2, 3, 4)) == {"by": "rect", "args": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}}
    with pytest.raises(TargetError, match="rect"):
        by.rect(0, 0, 0, 10)


def test_within_and_nth_dump() -> None:
    target = by.test_id("list").within(by.role("dialog")).nth(2)
    assert target.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "by": "test_id",
        "args": {"id": "list"},
        "within": {"by": "role", "args": {"role": "dialog", "exact": False}},
        "nth": 2,
    }


def test_near() -> None:
    target = by.text("Manuel").near(by.text("Şablon ile"))
    assert dump(target) == {
        "by": "text",
        "args": {"text": "Manuel", "exact": True},
        "near": {"by": "text", "args": {"text": "Şablon ile", "exact": True}},
    }


def test_within_and_near_accept_shorthand() -> None:
    assert by.test_id("list").within("role=dialog") == by.test_id("list").within(by.role("dialog"))
    assert by.text("Manuel").near("Şablon ile") == by.text("Manuel").near(by.text("Şablon ile"))
    assert by.text("x").within("#panel").within_ == by.test_id("panel")


def test_within_rejects_other_types() -> None:
    with pytest.raises(TypeError, match=r"within\(\)"):
        by.text("x").within(3)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match=r"near\(\)"):
        by.text("x").near(None)  # pyright: ignore[reportArgumentType]


def test_within_replaces_previous_scope() -> None:
    target = by.text("x").within(by.role("dialog")).within(by.role("main"))
    assert target.within_ == by.role("main")


def test_first_and_last() -> None:
    base = by.role("row")
    assert base.first.nth_ == 0
    assert base.last.nth_ == -1
    assert dump(base.last) == {"by": "role", "args": {"role": "row", "exact": False}, "nth": -1}
    assert base.nth_ is None


def test_nth_rejects_non_int() -> None:
    with pytest.raises(TypeError, match="nth"):
        by.text("x").nth(True)
    with pytest.raises(TypeError, match="nth"):
        by.text("x").nth(1.5)  # pyright: ignore[reportArgumentType]


def test_modifiers_return_new_targets() -> None:
    base = by.test_id("list")
    scoped = base.within(by.role("dialog"))
    assert base.within_ is None
    assert scoped is not base
    assert scoped.within_ == by.role("dialog")


def test_frozen_assignment_raises() -> None:
    target = by.test_id("list")
    with pytest.raises(ValidationError):
        target.by = "text"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        target.nth_ = 1  # pyright: ignore[reportAttributeAccessIssue]


def test_json_round_trip() -> None:
    target = by.test_id("list").within(by.role("dialog", name="Sil")).near(by.text("x")).last
    data = dump(target)
    again = Target.model_validate(data)
    assert again == target
    assert Target.model_validate_json(target.model_dump_json(by_alias=True, exclude_none=True)) == target


def test_model_rejects_bad_kind_name_and_extra_keys() -> None:
    with pytest.raises(ValidationError):
        Target.model_validate({"by": "Not A Kind", "args": {}})
    with pytest.raises(ValidationError):
        Target.model_validate({"by": "text", "args": {"text": "x"}, "extra": 1})


def test_non_str_value_uses_str() -> None:
    class Thing:
        def __str__(self) -> str:
            return "x"

    assert by.text(Thing()).args == {"text": "x", "exact": True}
    assert by.test_id(Thing()).args == {"id": "x"}
    assert by.role("button", name=Thing()).args["name"] == "x"
    assert by.label(Thing()).args["text"] == "x"
    assert by.placeholder(Thing()).args["text"] == "x"
    assert by.css(Thing()).args == {"selector": "x"}
    assert by.text(42).args["text"] == "42"


def test_ref_encodings_pass_through() -> None:
    fmt = FmtExpr.model_validate({"$fmt": ["#", {"$ref": "n1"}]})
    target = by.text(fmt)
    assert target.args["text"] == {"$fmt": ["#", {"$ref": "n1", "path": []}]}
    assert by.test_id({"$ref": "n2", "path": ["id"]}).args == {"id": {"$ref": "n2", "path": ["id"]}}
    assert by.css(RefExpr.model_validate({"$ref": "n3"})).args == {"selector": {"$ref": "n3", "path": []}}


def test_role_name_none_is_dropped() -> None:
    assert by.role("dialog").args == {"role": "dialog", "exact": False}
    assert "name" not in by.role("dialog", name=None).args


def test_custom_with_test_kind() -> None:
    target = by.custom("chip", label="Yeni")
    assert dump(target) == {"by": "chip", "args": {"label": "Yeni"}}
    assert validate_args(target, {"chip": CHIP}) == target
    assert validate_args(by.custom("chip", label="Yeni", tone=None), {"chip": CHIP}).args == {"label": "Yeni"}


def test_custom_with_builtin_kind_validates() -> None:
    assert by.custom("text", text="Kaydet") == by.text("Kaydet")
    with pytest.raises(TargetError):
        by.custom("text", text="Kaydet", bogus=1)


def test_custom_converts_non_json_values() -> None:
    class Thing:
        def __str__(self) -> str:
            return "thing"

    target = by.custom("chip", label=Thing(), tags=("a", Thing()), meta={"k": Thing()})
    assert target.args == {"label": "thing", "tags": ["a", "thing"], "meta": {"k": "thing"}}


def test_custom_rejects_bad_kind_name() -> None:
    with pytest.raises(TargetError, match="kind"):
        by.custom("Bad Kind", label="x")


def test_parse_method() -> None:
    assert by.parse("#a") == by.test_id("a")
    assert isinstance(by, By)


def test_str_is_describe() -> None:
    target = by.test_id("list").within(by.role("dialog")).nth(2)
    assert str(target) == describe(target) == "#list within role=dialog nth 2"


def test_builtin_kinds_cover_design_kinds() -> None:
    assert set(BUILTIN_KINDS) == {
        "test_id", "role", "text", "label", "placeholder", "css", "kind", "point", "rect", "object", "layer",
    }


def test_determinism() -> None:
    def build() -> Target:
        return by.test_id("list").within(by.role("dialog", name="Şube")).near("Kaydet").nth(2)

    first, second = build(), build()
    assert first == second
    assert canonical(first) == canonical(second)
    assert canonical(by.point(1, 2)) == canonical(parse_target("point=1,2"))
    # a JSON-authored target with int coordinates normalizes to the same canonical form as the builder
    from_json = validate_args(Target.model_validate({"by": "point", "args": {"x": 1, "y": 2}}))
    assert canonical(from_json) == canonical(by.point(1, 2))
    assert canonical_json(from_json) == canonical_json(by.point(1, 2))


@pytest.mark.parametrize("nth", [True, "2", 1.0])
def test_nth_is_strict_in_json(nth: object) -> None:
    with pytest.raises(ValidationError):
        Target.model_validate({"by": "text", "args": {"text": "x"}, "nth": nth})


def test_nth_accepts_int_from_json() -> None:
    assert Target.model_validate_json('{"by": "text", "args": {"text": "x"}, "nth": -1}').nth_ == -1


# args are stored under their JSON (alias) keys, so validation is idempotent


class _AliasedChip(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label_: str = Field(alias="label")


ALIASED_CHIP = TargetKind("chip", _AliasedChip, frozenset({"web"}), "chip", "a chip by label")


def test_aliased_plugin_args_store_alias_keys() -> None:
    kinds = {"chip": ALIASED_CHIP}
    once = validate_args(by.custom("chip", label="x"), kinds)
    assert once.args == {"label": "x"}
    assert validate_args(once, kinds) == once
    assert parse_target("chip=x", kinds).args == {"label": "x"}
    assert validate_args(parse_target("chip=x", kinds), kinds) == parse_target("chip=x", kinds)


# non-finite numbers never reach the plan JSON


class _Gauge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: float


GAUGE = TargetKind("gauge", _Gauge, frozenset({"web"}), None, "a gauge by level")


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_point_rejects_non_finite(value: float) -> None:
    with pytest.raises(TargetError, match="point"):
        by.point(value, 0)


def test_rect_rejects_non_finite() -> None:
    with pytest.raises(TargetError, match="rect"):
        by.rect(0, 0, float("nan"), 1)
    with pytest.raises(TargetError, match="rect"):
        by.rect(0, 0, float("inf"), 1)


def test_point_shorthand_overflow_is_rejected() -> None:
    with pytest.raises(TargetError, match="finite"):
        parse_target("point=1e999,1")
    with pytest.raises(TargetError, match="finite"):
        parse_target("rect=0,0,1e999,1")


def test_custom_rejects_non_finite_values() -> None:
    with pytest.raises(TargetError, match="finite"):
        by.custom("chip", v=float("nan"))
    with pytest.raises(TargetError, match="finite"):
        by.custom("chip", tags=[1.0, float("inf")])


def test_plugin_args_model_allowing_nan_is_still_rejected() -> None:
    target = Target(by="gauge", args={"level": 1.5})
    assert validate_args(target, {"gauge": GAUGE}).args == {"level": 1.5}
    with pytest.raises(TargetError, match="finite"):
        validate_args(Target(by="gauge", args={"level": float("nan")}), {"gauge": GAUGE})
