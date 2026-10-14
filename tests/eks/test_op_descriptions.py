from __future__ import annotations

import typing
from collections.abc import Iterator

import pytest
from pydantic import BaseModel

import eks
from eks_harness.ir.presets import (
    NetProfile,
    OverlayStyle,
    Pace,
    PersonaPreset,
    StyleColors,
    StyleFonts,
    Viewport,
)
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    Category,
    Registry,
    RegistryError,
    default_registry,
)
from eks_harness.ir.schema import op_schema

CATEGORIES: tuple[Category, ...] = typing.get_args(Category)


def models_in(annotation: object) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from models_in(argument)


def reachable(roots: list[type[BaseModel]]) -> list[type[BaseModel]]:
    seen: dict[type[BaseModel], None] = {}
    pending = list(roots)
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen[model] = None
        for field in model.model_fields.values():
            pending.extend(models_in(field.annotation))
    return list(seen)


def builtin_models(registry: Registry) -> list[type[BaseModel]]:
    roots: list[type[BaseModel]] = [spec.args for spec in registry.ops() if spec.provenance.plugin == BUILTIN_PLUGIN]
    roots += [spec.args for spec in registry.checks() if spec.provenance.plugin == BUILTIN_PLUGIN]
    for category in CATEGORIES:
        roots += [spec.model for spec in registry.catalogs(category) if spec.provenance.plugin == BUILTIN_PLUGIN]
    return reachable(roots)


def test_every_builtin_argument_field_has_a_description() -> None:
    registry = default_registry()
    missing = [f"{model.__module__}.{model.__qualname__}.{name}"
               for model in builtin_models(registry) if model.__module__.startswith("eks_harness.ir")
               for name, field in model.model_fields.items() if not (field.description or "").strip()]
    assert missing == []


PRESET_MODELS: tuple[type[BaseModel], ...] = (
    Viewport, Pace, NetProfile, StyleFonts, StyleColors, OverlayStyle, PersonaPreset)


def undescribed_fields(models: list[type[BaseModel]]) -> list[str]:
    return [f"{model.__module__}.{model.__qualname__}.{name}"
            for model in models
            for name, field in model.model_fields.items() if not (field.description or "").strip()]


def test_every_preset_model_field_has_a_description() -> None:
    registry = default_registry()
    roots = list(PRESET_MODELS) + [type(spec.value) for spec in registry.presets()]
    models = reachable(roots)
    assert set(PRESET_MODELS) <= set(models)
    assert {type(spec.value) for spec in registry.presets()} >= {Viewport, Pace, NetProfile, OverlayStyle}
    assert undescribed_fields(models) == []


def test_preset_model_descriptions_reach_the_json_schema() -> None:
    style = OverlayStyle.model_json_schema()
    assert style["properties"]["ref_width"]["description"]
    assert style["properties"]["spotlight_dim"]["description"]
    assert style["$defs"]["StyleColors"]["properties"]["badge"]["description"]
    assert style["$defs"]["StyleFonts"]["properties"]["family"]["description"]
    persona = PersonaPreset.model_json_schema()
    assert all(prop.get("description") for prop in persona["properties"].values())


def test_preset_descriptions_keep_constraints_and_defaults() -> None:
    style = OverlayStyle.model_fields
    assert style["badge_shape"].default == "circle"
    with pytest.raises(ValueError):
        OverlayStyle.model_validate({**_kb_style_dump(), "spotlight_dim": 1.5})
    with pytest.raises(ValueError):
        OverlayStyle.model_validate({**_kb_style_dump(), "ref_width": 0})
    with pytest.raises(ValueError):
        OverlayStyle.model_validate({**_kb_style_dump(), "badge_shape": "square"})
    assert PersonaPreset(label="Host") == PersonaPreset(label="Host", username="", secret_ref="", tenant="", roles=())


def _kb_style_dump() -> dict[str, object]:
    spec = default_registry().preset("style", "kb")
    return spec.value.model_dump()


def test_op_schemas_carry_argument_descriptions_and_examples() -> None:
    registry = default_registry()
    for spec in registry.ops():
        schema = op_schema(spec, registry=registry)
        for name, prop in schema.get("properties", {}).items():
            assert prop.get("description"), (spec.name, name)
    nav = op_schema(registry.op("ui.nav"), registry=registry)
    assert {"route": "/tenant/settings/branches"} in nav["examples"]
    assert op_schema(registry.op("ui.back"), registry=registry)["examples"] == [{}]


def test_op_examples_are_validated_at_registration(eks_registry: Registry) -> None:
    class Rate(eks.Args):
        currency: str

    with pytest.raises(RegistryError, match="example 0 does not validate"):
        eks.op("acme", examples=[{"currency": 3}])(Rate)
    with pytest.raises(RegistryError, match="is not JSON"):
        eks.op("acme", name="rate_two", examples=[{"currency": float("nan")}])(Rate)
    eks.op("acme", name="rate_three", examples=[{"currency": "XAU"}])(Rate)
    assert eks_registry.op("acme.rate_three").examples == ({"currency": "XAU"},)


def test_store_is_a_reserved_builtin_family(eks_registry: Registry) -> None:
    class Upload(eks.Args):
        name: str

    with pytest.raises(RegistryError, match="built-in family 'store'"):
        eks.op("store")(Upload)


def test_every_builtin_op_has_a_validated_example() -> None:
    # design 15: the generated reference shows an example for every op, and op_schema emits them
    registry = default_registry()
    builtin = [spec for spec in registry.ops() if spec.provenance.plugin == BUILTIN_PLUGIN]
    assert builtin
    assert [spec.name for spec in builtin if not spec.examples] == []
    for spec in builtin:
        for example in spec.examples:
            spec.args.model_validate(example)
        assert op_schema(spec, registry=registry)["examples"] == [dict(example) for example in spec.examples]
