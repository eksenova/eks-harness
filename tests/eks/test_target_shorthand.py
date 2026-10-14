from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from eks_harness.ir.target import (
    BUILTIN_KINDS,
    Target,
    TargetError,
    TargetKind,
    by,
    describe,
    ensure_target,
    parse_target,
)


class _Chip(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    tone: str | None = None


CHIP = TargetKind("chip", _Chip, frozenset({"web"}), "chip", "a chip by label")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#save", by.test_id("save")),
        ("#a.b:c-d_e9", by.test_id("a.b:c-d_e9")),
        ("testid=a b", by.test_id("a b")),
        ("#a b", by.text("#a b")),
        ("#", by.text("#")),
        ("#ş", by.text("#ş")),
        ("css=.x > a", by.css(".x > a")),
        ("role=button", by.role("button")),
        ("role=button:Save: now", by.role("button", name="Save: now")),
        ("text=#1", by.text("#1")),
        ("text=role=x", by.text("role=x")),
        ("li > a", by.text("li > a")),
        (".btn-primary", by.text(".btn-primary")),
        ("kind=button", by.kind("button")),
        ("label=E-posta", by.label("E-posta")),
        ("placeholder=Ara", by.placeholder("Ara")),
        ("object=Phone", by.object("Phone")),
        ("layer=title", by.layer("title")),
        ("point=120,480", by.point(120, 480)),
        ("point=-1.5, 2e2", by.point(-1.5, 200)),
        ("rect=1,2,3,4", by.rect(1, 2, 3, 4)),
        ("foo=bar", by.text("foo=bar")),
        ("Role=button", by.text("Role=button")),
        ("", by.text("")),
        ("  padded  ", by.text("  padded  ")),
        ("text=line\nbreak", by.text("line\nbreak")),
    ],
)
def test_parse(text: str, expected: Target) -> None:
    assert parse_target(text) == expected


def test_role_without_name_has_no_name() -> None:
    target = parse_target("role=button")
    assert "name" not in target.args
    assert target.args == {"role": "button", "exact": False}


def test_role_name_keeps_colons() -> None:
    assert parse_target("role=button:Save: now").args["name"] == "Save: now"
    assert parse_target("role=link:a:b:c").args == {"role": "link", "name": "a:b:c", "exact": False}


def test_text_shorthand_is_exact() -> None:
    assert parse_target("text=#1").args == {"text": "#1", "exact": True}
    assert parse_target("li > a").args == {"text": "li > a", "exact": True}


@pytest.mark.parametrize(
    "text",
    [
        "point=1,2,3",
        "point=1",
        "point=a,b",
        "point=nan,1",
        "point=1,",
        "rect=1,2,3",
        "rect=1,2,3,4,5",
        "rect=0,0,0,1",
        "rect=0,0,1,-1",
        "role=",
        "role=:name",
        "role=button:",
        "text=",
        "css=",
        "testid=",
        "kind=slider",
    ],
)
def test_malformed_prefixed_forms_raise(text: str) -> None:
    with pytest.raises(TargetError) as info:
        parse_target(text)
    assert repr(text) in str(info.value)


def test_parse_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        parse_target(5)  # pyright: ignore[reportArgumentType]


def test_plugin_shorthand() -> None:
    kinds = {**BUILTIN_KINDS, "chip": CHIP}
    target = parse_target("chip=Yeni", kinds)
    assert target.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "by": "chip",
        "args": {"label": "Yeni"},
    }
    assert parse_target("chip=Yeni") == by.text("chip=Yeni")


def test_plugin_shorthand_with_only_plugin_kinds_keeps_builtins() -> None:
    kinds = {"chip": CHIP}
    assert parse_target("chip=Yeni", kinds).by == "chip"
    assert parse_target("role=button", kinds) == by.role("button")
    assert parse_target("#a", kinds) == by.test_id("a")


def test_plugin_kind_without_shorthand_is_text() -> None:
    kinds = {"chip": TargetKind("chip", _Chip, frozenset({"web"}))}
    assert parse_target("chip=Yeni", kinds) == by.text("chip=Yeni")


def test_plugin_kind_without_fields_cannot_parse() -> None:
    class _Empty(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

    kinds = {"blank": TargetKind("blank", _Empty, frozenset({"web"}), "blank")}
    with pytest.raises(TargetError, match="blank"):
        parse_target("blank=x", kinds)


def test_ensure_target() -> None:
    assert ensure_target("#a", "target") == by.test_id("a")
    target = by.role("dialog")
    assert ensure_target(target, "target") == target
    with pytest.raises(TypeError, match="target"):
        ensure_target(5, "target")
    with pytest.raises(TypeError, match="anchor"):
        ensure_target(None, "anchor")


def test_ensure_target_validates_target_args() -> None:
    raw = Target.model_validate({"by": "text", "args": {"text": "x"}})
    assert ensure_target(raw, "target").args == {"text": "x", "exact": True}
    with pytest.raises(TargetError):
        ensure_target(Target.model_validate({"by": "text", "args": {}}), "target")


def test_ensure_target_uses_plugin_kinds() -> None:
    assert ensure_target("chip=Yeni", "target", {"chip": CHIP}).by == "chip"


def test_describe_outputs() -> None:
    assert describe(by.test_id("list").within(by.role("dialog")).nth(2)) == "#list within role=dialog nth 2"
    assert describe(by.test_id("a b")) == "testid=a b"
    assert describe(by.text("Kaydet")) == "Kaydet"
    assert describe(by.text("#1")) == "text=#1"
    assert describe(by.text("a=b")) == "text=a=b"
    assert describe(by.text("chip=x")) == "text=chip=x"
    assert describe(by.text("Kadı", exact=False)) == 'by=text {"exact": false, "text": "Kadı"}'
    assert describe(by.text("")) == 'by=text {"exact": true, "text": ""}'
    assert describe(by.role("button", name="Sunuyu iptal et")) == "role=button:Sunuyu iptal et"
    assert describe(by.role("button", name="x", exact=True)) == (
        'by=role {"exact": true, "name": "x", "role": "button"}'
    )
    assert describe(by.label("E-posta")) == "label=E-posta"
    assert describe(by.label("E-posta", exact=True)) == 'by=label {"exact": true, "text": "E-posta"}'
    assert describe(by.point(120, 480)) == "point=120,480"
    assert describe(by.point(1.5, -2)) == "point=1.5,-2"
    assert describe(by.rect(0, 0, 10, 20)) == "rect=0,0,10,20"
    assert describe(by.kind("button")) == "kind=button"
    assert describe(by.css(".x > a")) == "css=.x > a"
    assert describe(by.custom("chip", label="Yeni")) == 'by=chip {"label": "Yeni"}'
    assert describe(by.text({"$ref": "n1"})) == 'by=text {"exact": true, "text": {"$ref": "n1", "path": []}}'
    assert describe(by.text("Manuel").near(by.text("Şablon ile"))) == "Manuel near Şablon ile"
    assert describe(by.role("row").last) == "role=row nth -1"


def test_describe_parenthesizes_nested_modifiers() -> None:
    target = by.text("x").within(by.role("dialog").nth(1)).near(by.test_id("a").within("#b"))
    assert describe(target) == "x within (role=dialog nth 1) near (#a within #b)"


@pytest.mark.parametrize(
    "target",
    [
        by.test_id("credit-add-submit"),
        by.test_id("a b"),
        by.role("button"),
        by.role("button", name="Save: now"),
        by.text("Kadıköy"),
        by.text("#1"),
        by.text("a=b"),
        by.text("  spaced "),
        by.label("E-posta"),
        by.placeholder("Ara"),
        by.css("li[data-package-id='3']"),
        by.kind("view"),
        by.point(120, 480),
        by.point(0.25, -3.5),
        by.rect(1, 2, 3, 4),
        by.object("Phone"),
        by.layer("title"),
    ],
)
def test_describe_round_trips(target: Target) -> None:
    assert parse_target(describe(target)) == target


@pytest.mark.parametrize("text", ["point=1e999,1", "point=1,-1e999", "rect=0,0,1e400,1"])
def test_overflowing_numbers_are_target_errors(text: str) -> None:
    with pytest.raises(TargetError, match="finite"):
        parse_target(text)


def test_modifier_and_parse_strings_use_passed_kinds() -> None:
    kinds = {"chip": CHIP}
    scoped = by.text("Kaydet").within("chip=Yeni", kinds=kinds)
    assert scoped.within_ == Target(by="chip", args={"label": "Yeni"})
    anchored = by.text("Kaydet").near("chip=Yeni", kinds=kinds)
    assert anchored.near_ == Target(by="chip", args={"label": "Yeni"})
    assert by.parse("chip=Yeni", kinds=kinds).by == "chip"


def test_describe_uses_plugin_shorthands() -> None:
    kinds = {"chip": CHIP}
    target = by.custom("chip", label="Yeni")
    assert describe(target, kinds) == "chip=Yeni"
    assert parse_target(describe(target, kinds), kinds) == target
    # A chip with a non-default argument has no shorthand form.
    toned = Target(by="chip", args={"label": "Yeni", "tone": "red"})
    assert describe(toned, kinds) == 'by=chip {"label": "Yeni", "tone": "red"}'
    assert describe(by.test_id("list").within(target), kinds) == "#list within chip=Yeni"


def test_registered_kinds_are_the_default(eks_registry: object) -> None:
    import eks

    @eks.target_kind("chip", surfaces=["web"], shorthand="chip", description="a chip by label")
    class _RegisteredChip(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        label: str

    chip = Target(by="chip", args={"label": "x"})
    assert by.text("a").within("chip=x").within_ == chip
    assert by.text("a").near("chip=x").near_ == chip
    assert eks.by.parse("chip=x") == chip
    assert parse_target("chip=x") == chip
    assert ensure_target("chip=x", "target") == chip
    assert describe(chip) == "chip=x"
    assert describe(by.text("a").within("chip=x")) == "a within chip=x"
    # Passing kinds explicitly still means exactly those kinds over the builtins.
    assert parse_target("chip=x", {}) == by.text("chip=x")


@pytest.mark.parametrize("token", [0, 1, 12])
def test_test_id_with_ref_sentinel(token: int) -> None:
    from eks.ref import SENTINEL_CLOSE, SENTINEL_OPEN

    sentinel = f"{SENTINEL_OPEN}r{token}{SENTINEL_CLOSE}"
    for text in (f"#row-{sentinel}", f"#{sentinel}", f"#{sentinel}-{sentinel}.x"):
        target = parse_target(text)
        assert target.by == "test_id"
        assert target.args == {"id": text[1:]}
        assert describe(target) == text
        assert parse_target(describe(target)) == target


def test_broken_sentinels_are_text() -> None:
    from eks.ref import SENTINEL_CLOSE, SENTINEL_OPEN

    for text in (
        f"#row-{SENTINEL_OPEN}x{SENTINEL_CLOSE}",
        f"#row-{SENTINEL_OPEN}r1",
        f"#row-r1{SENTINEL_CLOSE}",
        f"#row-{SENTINEL_OPEN}r{SENTINEL_CLOSE}",
    ):
        assert parse_target(text) == by.text(text)


def test_shorthand_collision_with_builtin_raises() -> None:
    kinds = {"mytext": TargetKind("mytext", _Chip, frozenset({"web"}), "text")}
    with pytest.raises(TargetError, match="'text' and 'mytext' both declare shorthand 'text'"):
        parse_target("text=x", kinds)
    with pytest.raises(TargetError, match="mytext"):
        parse_target("#a", kinds)
    # describe never raises; it falls back to the explicit form.
    assert describe(by.test_id("a"), kinds) == 'by=test_id {"id": "a"}'


def test_shorthand_collision_between_plugins_raises() -> None:
    kinds = {
        "chip": CHIP,
        "badge": TargetKind("badge", _Chip, frozenset({"web"}), "chip"),
    }
    with pytest.raises(TargetError, match="'chip' and 'badge' both declare shorthand 'chip'"):
        parse_target("chip=x", kinds)


def test_replacing_a_kind_keeps_its_shorthand() -> None:
    kinds = {"text": BUILTIN_KINDS["text"], "chip": CHIP}
    assert parse_target("text=x", kinds) == by.text("x")


@pytest.mark.parametrize("shorthand", ["my chip", "9x", "a:b", "", "_x", "chip="])
def test_unreachable_shorthand_raises(shorthand: str) -> None:
    kinds = {"chip": TargetKind("chip", _Chip, frozenset({"web"}), shorthand)}
    with pytest.raises(TargetError, match="can never be written"):
        parse_target("chip=x", kinds)


@pytest.mark.parametrize("shorthand", ["chip", "c", "my-chip", "my.chip", "my_chip", "Chip2"])
def test_valid_shorthands_parse(shorthand: str) -> None:
    kinds = {"chip": TargetKind("chip", _Chip, frozenset({"web"}), shorthand)}
    assert parse_target(f"{shorthand}=x", kinds) == Target(by="chip", args={"label": "x"})
