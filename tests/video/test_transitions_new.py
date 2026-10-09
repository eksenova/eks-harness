from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.ir.transitions import (
    Crossfade,
    Cut,
    DipToBlack,
    DipToWhite,
    PluginTransition,
    Push,
    Slide,
    Transition,
    Wipe,
)

_adapter: TypeAdapter[
    Cut | Crossfade | DipToBlack | DipToWhite | Wipe | Slide | Push | PluginTransition
] = TypeAdapter(Transition)


@pytest.mark.parametrize(
    "value",
    [
        DipToBlack(duration=0.5),
        DipToWhite(duration=0.25),
        Wipe(duration=0.5),
        Wipe(duration=0.5, direction="left"),
        Wipe(duration=0.5, direction="up"),
        Wipe(duration=0.5, direction="down"),
        Slide(duration=0.5),
        Slide(duration=0.5, direction="up"),
        Push(duration=0.5),
        Push(duration=0.5, direction="right"),
    ],
)
def test_new_transition_union_round_trip(
    value: DipToBlack | DipToWhite | Wipe | Slide | Push,
) -> None:
    payload = value.model_dump(by_alias=True, mode="json")
    rebuilt = _adapter.validate_python(payload)
    assert rebuilt == value
    rebuilt_from_text = _adapter.validate_python(json.loads(json.dumps(payload)))
    assert rebuilt_from_text == value


def test_default_directions() -> None:
    assert Wipe(duration=0.5).direction == "right"
    assert Slide(duration=0.5).direction == "right"
    assert Push(duration=0.5).direction == "left"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DipToBlack(duration=0.0),
        lambda: DipToWhite(duration=-1.0),
        lambda: Wipe(duration=0.0),
        lambda: Slide(duration=-0.1),
        lambda: Push(duration=0.0),
    ],
)
def test_duration_must_be_positive(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.parametrize(
    "model_cls",
    [Wipe, Slide, Push],
)
def test_direction_rejects_invalid_values(model_cls: type) -> None:
    with pytest.raises(ValidationError):
        model_cls(duration=0.5, direction="diagonal")


def test_discriminator_dispatch_via_dict() -> None:
    parsed = _adapter.validate_python({"kind": "dip_to_black", "duration": 0.5})
    assert isinstance(parsed, DipToBlack)
    assert parsed.duration == 0.5

    parsed_wipe = _adapter.validate_python(
        {"kind": "wipe", "duration": 0.5, "direction": "up"}
    )
    assert isinstance(parsed_wipe, Wipe)
    assert parsed_wipe.direction == "up"
