from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eks_harness.ir.target import (
    BUILTIN_KINDS,
    MODIFIER_SURFACES,
    SURFACES,
    Target,
    TargetError,
    TargetKind,
    by,
    check_target,
    validate_args,
)


class _Chip(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str


CHIP = TargetKind("chip", _Chip, frozenset({"web"}), "chip", "a chip by label")


def test_css_on_ios_is_a_problem() -> None:
    problems = check_target(by.css(".x"), "ios")
    assert len(problems) == 1
    assert "'css'" in problems[0]
    assert "ios" in problems[0]


def test_css_on_web_is_fine() -> None:
    assert check_target(by.css(".x"), "web") == []


def test_kind_on_web_is_a_problem() -> None:
    problems = check_target(by.kind("button"), "web")
    assert len(problems) == 1
    assert "'kind'" in problems[0]


def test_kind_on_android_is_fine() -> None:
    assert check_target(by.kind("button"), "android") == []


def test_within_on_blender_is_a_problem() -> None:
    target = by.object("Phone").within(by.object("Rig"))
    problems = check_target(target, "blender")
    assert len(problems) == 1
    assert "within" in problems[0]
    assert "blender" in problems[0]


def test_near_on_gfx_is_a_problem() -> None:
    problems = check_target(by.layer("a").near(by.layer("b")), "gfx")
    assert len(problems) == 1
    assert "near" in problems[0]


def test_point_on_gfx_is_fine() -> None:
    assert check_target(by.point(1, 2), "gfx") == []


def test_nth_everywhere() -> None:
    for surface in SURFACES:
        assert check_target(by.point(1, 2).nth(0), surface) == []
    assert MODIFIER_SURFACES["nth"] == frozenset(SURFACES)


def test_object_on_web_is_a_problem() -> None:
    problems = check_target(by.object("Phone"), "web")
    assert len(problems) == 1
    assert "blender" in problems[0]


def test_layer_on_gfx_is_fine() -> None:
    assert check_target(by.layer("title"), "gfx") == []


@pytest.mark.parametrize("kind", sorted(BUILTIN_KINDS))
def test_builtin_kinds_follow_their_surface_tables(kind: str) -> None:
    sample = {
        "test_id": by.test_id("a"),
        "role": by.role("button"),
        "text": by.text("a"),
        "label": by.label("a"),
        "placeholder": by.placeholder("a"),
        "css": by.css(".a"),
        "kind": by.kind("button"),
        "point": by.point(1, 2),
        "rect": by.rect(1, 2, 3, 4),
        "object": by.object("a"),
        "layer": by.layer("a"),
    }[kind]
    for surface in SURFACES:
        problems = check_target(sample, surface)
        assert (problems == []) == (surface in BUILTIN_KINDS[kind].surfaces), (kind, surface, problems)


def test_problems_inside_within_and_near_are_reported() -> None:
    target = by.test_id("a").within(by.css(".dialog")).near(by.object("Phone"))
    problems = check_target(target, "ios")
    assert len(problems) == 2
    assert any("'css'" in p for p in problems)
    assert any("'object'" in p for p in problems)


def test_nested_modifiers_are_checked() -> None:
    inner = by.object("Rig").within(by.object("Root"))
    target = by.object("Phone").nth(0)
    target = target.model_copy(update={"within_": inner})
    problems = check_target(target, "blender")
    assert len(problems) == 2
    assert all("within" in p for p in problems)


def test_unknown_kind_is_reported() -> None:
    target = Target.model_validate({"by": "chip", "args": {"label": "x"}})
    problems = check_target(target, "web")
    assert len(problems) == 1
    assert "unknown target kind 'chip'" in problems[0]


def test_plugin_kind_accepted_when_passed() -> None:
    target = by.custom("chip", label="x")
    assert check_target(target, "web", {"chip": CHIP}) == []
    problems = check_target(target, "ios", {"chip": CHIP})
    assert len(problems) == 1
    assert "not supported on ios" in problems[0]


def test_unknown_surface_is_reported() -> None:
    problems = check_target(by.text("x"), "desktop")
    assert len(problems) == 1
    assert "desktop" in problems[0]


def test_validate_args_rejects_wrong_types() -> None:
    target = Target.model_validate({"by": "text", "args": {"text": 1}})
    with pytest.raises((ValidationError, TargetError)):
        validate_args(target)


def test_validate_args_rejects_extra_args() -> None:
    target = Target.model_validate({"by": "css", "args": {"selector": ".a", "xpath": "//a"}})
    with pytest.raises(TargetError, match="css"):
        validate_args(target)


def test_validate_args_drops_none_role_name_and_fills_defaults() -> None:
    target = Target.model_validate({"by": "role", "args": {"role": "dialog", "name": None}})
    assert validate_args(target).args == {"role": "dialog", "exact": False}


def test_validate_args_recurses() -> None:
    target = Target.model_validate(
        {
            "by": "text",
            "args": {"text": "x"},
            "within": {"by": "role", "args": {"role": "dialog"}},
            "near": {"by": "point", "args": {"x": 1, "y": 2}},
        }
    )
    checked = validate_args(target)
    assert checked.args == {"text": "x", "exact": True}
    assert checked.within_ is not None
    assert checked.within_.args == {"role": "dialog", "exact": False}
    assert checked.near_ is not None
    # compare the JSON text, since 1 == 1.0 would hide unnormalized ints
    assert json.dumps(checked.near_.args, sort_keys=True) == '{"x": 1.0, "y": 2.0}'
    assert all(type(value) is float for value in checked.near_.args.values())
    bad = Target.model_validate(
        {"by": "text", "args": {"text": "x"}, "within": {"by": "rect", "args": {"x": 0, "y": 0, "w": 0, "h": 1}}}
    )
    with pytest.raises(TargetError, match="rect"):
        validate_args(bad)


def test_validate_args_unknown_kind_passes_through() -> None:
    target = Target.model_validate({"by": "chip", "args": {"anything": [1, 2]}})
    assert validate_args(target) is target


def test_validate_args_uses_plugin_kinds() -> None:
    bad = Target.model_validate({"by": "chip", "args": {"label": 3}})
    with pytest.raises(TargetError, match="chip"):
        validate_args(bad, {"chip": CHIP})


def test_validate_args_keeps_ref_encodings() -> None:
    target = Target.model_validate({"by": "test_id", "args": {"id": {"$fmt": ["row-", {"$ref": "n1"}]}}})
    assert validate_args(target).args == {"id": {"$fmt": ["row-", {"$ref": "n1", "path": []}]}}


def test_validate_args_is_idempotent() -> None:
    target = by.test_id("list").within(by.role("dialog")).nth(2)
    assert validate_args(target) is target


def test_validate_args_normalizes_type_only_changes() -> None:
    point = validate_args(Target.model_validate({"by": "point", "args": {"x": 1, "y": 2}}))
    assert json.dumps(point.args, sort_keys=True) == '{"x": 1.0, "y": 2.0}'
    rect = validate_args(Target.model_validate({"by": "rect", "args": {"x": 0, "y": 0, "w": 3, "h": 4}}))
    assert all(type(value) is float for value in rect.args.values())


@pytest.mark.parametrize("kind", ["text", "role", "label", "placeholder"])
@pytest.mark.parametrize("exact", [1, 0, "yes", "on", "true"])
def test_validate_args_rejects_non_bool_exact(kind: str, exact: object) -> None:
    first = "role" if kind == "role" else "text"
    target = Target.model_validate({"by": kind, "args": {first: "x", "exact": exact}})
    with pytest.raises(TargetError, match="exact"):
        validate_args(target)


def test_validate_args_keeps_bool_exact() -> None:
    target = validate_args(Target.model_validate({"by": "text", "args": {"text": "x", "exact": False}}))
    assert target.args["exact"] is False


@pytest.mark.parametrize("value", ["120", True, None])
def test_validate_args_rejects_non_number_coordinates(value: object) -> None:
    target = Target.model_validate({"by": "point", "args": {"x": value, "y": 1}})
    with pytest.raises(TargetError, match="point"):
        validate_args(target)
