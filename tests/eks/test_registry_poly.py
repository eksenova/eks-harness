from __future__ import annotations

import json
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, SerializeAsAny, ValidationError

import eks
from eks_harness.ir.catalog.curves import CurveBase
from eks_harness.ir.catalog.effects import Effect, EffectBase, Zoom
from eks_harness.ir.catalog.transitions import Transition
from eks_harness.ir.registry import Poly, Registry, dispatch


class Clip(BaseModel):
    effect: Effect
    effects: list[Effect] = []


class Cut(BaseModel):
    transition: Transition | None = None


def test_dict_dispatches_to_the_registered_model_and_dumps_back() -> None:
    clip = Clip.model_validate({"effect": {"kind": "zoom", "scale": 1.2}})
    assert isinstance(clip.effect, Zoom)
    assert clip.effect.scale == 1.2
    dumped = clip.model_dump(mode="json")
    assert dumped["effect"]["kind"] == "zoom"
    assert dumped["effect"]["scale"] == 1.2
    assert dumped["effect"]["cx"] == 0.5
    assert Clip.model_validate(dumped) == clip
    assert Clip.model_validate_json(clip.model_dump_json()) == clip


def test_model_instances_pass_through() -> None:
    zoom = Zoom(scale=2.0)
    clip = Clip(effect=zoom, effects=[zoom])
    assert clip.effect is zoom
    assert clip.model_dump()["effects"][0]["scale"] == 2.0


def test_unknown_kind_is_a_validation_error_naming_it() -> None:
    with pytest.raises(ValidationError) as error:
        Clip.model_validate({"effect": {"kind": "zooom", "scale": 1}})
    text = str(error.value)
    assert "unknown effect kind 'zooom'" in text
    assert "did you mean 'zoom'" in text


@pytest.mark.parametrize("value", [{"scale": 1}, {"kind": 3}, "zoom", 7])
def test_values_without_a_string_kind_are_rejected(value: Any) -> None:
    with pytest.raises(ValidationError, match="string 'kind'"):
        Clip.model_validate({"effect": value})


def test_fields_of_the_dispatched_model_are_validated() -> None:
    with pytest.raises(ValidationError):
        Clip.model_validate({"effect": {"kind": "zoom"}})
    with pytest.raises(ValidationError):
        Clip.model_validate({"effect": {"kind": "zoom", "scale": 1, "extra": True}})


def test_a_kind_of_another_category_is_unknown() -> None:
    with pytest.raises(ValidationError, match="unknown effect kind 'crossfade'"):
        Clip.model_validate({"effect": {"kind": "crossfade"}})
    assert Cut.model_validate({"transition": {"kind": "cut"}}).transition is not None


def test_a_test_registered_kind_dispatches(eks_registry: Registry) -> None:
    @eks.effect(description="Sepia tone")
    class Sepia(EffectBase, frozen=True):
        kind: Literal["acme_sepia"] = "acme_sepia"
        amount: float = 1.0

    clip = Clip.model_validate({"effect": {"kind": "acme_sepia", "amount": 0.4},
                                "effects": [{"kind": "zoom", "scale": 1.1}, {"kind": "acme_sepia"}]})
    assert isinstance(clip.effect, Sepia)
    assert clip.effect.amount == 0.4
    assert [type(e) for e in clip.effects] == [Zoom, Sepia]
    assert clip.model_dump(mode="json")["effect"] == {"kind": "acme_sepia", "amount": 0.4}
    assert dispatch("effect", {"kind": "acme_sepia"}) == Sepia()


def schema_kinds(schema: dict[str, Any], one_of: list[dict[str, Any]]) -> list[str]:
    # pydantic inlines a model used once and refers to the others through $defs; read the kind const of both.
    kinds = []
    for item in one_of:
        ref = item.get("$ref")
        body = schema["$defs"][ref.rsplit("/", 1)[-1]] if ref else item
        kinds.append(body["properties"]["kind"]["const"])
    return kinds


def test_json_schema_lists_builtins_and_registered_kinds(eks_registry: Registry) -> None:
    @eks.effect()
    class Sepia(EffectBase, frozen=True):
        kind: Literal["acme_sepia"] = "acme_sepia"

    schema = Clip.model_json_schema()
    kinds = schema_kinds(schema, schema["properties"]["effect"]["oneOf"])
    assert "zoom" in kinds
    assert "acme_sepia" in kinds
    assert sorted(kinds) == [spec.kind for spec in eks_registry.catalogs("effect")]
    assert '"oneOf"' in json.dumps(schema)


def test_json_schema_follows_the_registry_at_generation_time(eks_registry: Registry) -> None:
    before = Clip.model_json_schema()
    assert "acme_late" not in schema_kinds(before, before["properties"]["effect"]["oneOf"])

    @eks.effect()
    class Late(EffectBase, frozen=True):
        kind: Literal["acme_late"] = "acme_late"

    after = Clip.model_json_schema()
    assert "acme_late" in schema_kinds(after, after["properties"]["effect"]["oneOf"])


def test_poly_on_a_custom_base(eks_registry: Registry) -> None:
    @eks.curve()
    class Wobble(CurveBase, frozen=True):
        kind: Literal["acme_wobble"] = "acme_wobble"
        rate: float = 2.0

    class Holder(BaseModel):
        shape: Annotated[SerializeAsAny[CurveBase], Poly("curve")]

    holder = Holder.model_validate({"shape": {"kind": "acme_wobble"}})
    assert isinstance(holder.shape, Wobble)
    assert holder.model_dump() == {"shape": {"kind": "acme_wobble", "rate": 2.0}}
    assert eks_registry.catalog("curve", "acme_wobble").model is Wobble
