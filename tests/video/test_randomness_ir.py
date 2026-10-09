from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.ir.randomness import EveryNth, RandomTrigger, TriggerSource
from eks_harness.video.ir.time import BeatRef, MarkerRef, WordRef

_adapter: TypeAdapter[RandomTrigger | EveryNth] = TypeAdapter(TriggerSource)


def _round_trip(value: RandomTrigger | EveryNth) -> None:
    payload = value.model_dump(by_alias=True, mode="json")
    rebuilt = type(value).model_validate(payload)
    assert rebuilt == value
    rebuilt_via_union = _adapter.validate_python(payload)
    assert rebuilt_via_union == value
    rebuilt_from_text = _adapter.validate_python(json.loads(json.dumps(payload)))
    assert rebuilt_from_text == value


@pytest.mark.parametrize(
    "value",
    [
        RandomTrigger(seed=1),
        RandomTrigger(seed=42, rate_hz=2.5, jitter=0.3),
        EveryNth(source=BeatRef(stream="kick"), n=4),
        EveryNth(source=BeatRef(stream="kick", every=2), n=3, offset=1),
        EveryNth(source=WordRef(text="hello"), n=1, jitter=0.5, seed=7),
        EveryNth(source=MarkerRef(name="intro"), n=2, offset=0, jitter=0.0),
    ],
)
def test_round_trip(value: RandomTrigger | EveryNth) -> None:
    _round_trip(value)


def test_random_trigger_defaults() -> None:
    rt = RandomTrigger(seed=0)
    assert rt.rate_hz == 1.0
    assert rt.jitter == 0.0
    assert rt.kind == "random_trigger"


def test_every_nth_defaults() -> None:
    en = EveryNth(source=BeatRef(stream="kick"))
    assert en.n == 1
    assert en.offset == 0
    assert en.jitter == 0.0
    assert en.seed == 0
    assert en.kind == "every_nth"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RandomTrigger(seed=1, rate_hz=0.0),
        lambda: RandomTrigger(seed=1, rate_hz=-1.0),
        lambda: RandomTrigger(seed=1, jitter=-0.1),
        lambda: RandomTrigger(seed=1, jitter=1.5),
    ],
)
def test_random_trigger_validation(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EveryNth(source=BeatRef(stream="kick"), n=0),
        lambda: EveryNth(source=BeatRef(stream="kick"), n=-2),
        lambda: EveryNth(source=BeatRef(stream="kick"), offset=-1),
        lambda: EveryNth(source=BeatRef(stream="kick"), jitter=-0.1),
        lambda: EveryNth(source=BeatRef(stream="kick"), jitter=1.5),
    ],
)
def test_every_nth_validation(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        RandomTrigger.model_validate({"seed": 1, "unexpected": True})
    with pytest.raises(ValidationError):
        EveryNth.model_validate(
            {"source": {"kind": "beat", "stream": "kick"}, "unexpected": True}
        )


def test_discriminator_dispatch_via_dict() -> None:
    parsed = _adapter.validate_python(
        {"kind": "random_trigger", "seed": 5, "rate_hz": 4.0, "jitter": 0.1}
    )
    assert isinstance(parsed, RandomTrigger)
    assert parsed.seed == 5

    parsed_en = _adapter.validate_python(
        {
            "kind": "every_nth",
            "source": {"kind": "beat", "stream": "kick"},
            "n": 4,
        }
    )
    assert isinstance(parsed_en, EveryNth)
    assert parsed_en.n == 4
    assert isinstance(parsed_en.source, BeatRef)
