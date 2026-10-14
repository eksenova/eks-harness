from __future__ import annotations

import json
from fractions import Fraction

import pytest
from pydantic import BaseModel, ValidationError

from eks_harness.ir.time import (
    FPS_PATTERN,
    MAX_BPM,
    TIME_PATTERN,
    Fps,
    Meter,
    MeterChange,
    Span,
    TempoChange,
    TempoMap,
    Time,
    TimeContext,
    TimeError,
    TimeSet,
    format_fps,
    parse_fps,
    t,
)


class Clip(BaseModel):
    at: Time
    hold: Time | None = None
    span: Span
    triggers: TimeSet
    fps: Fps
    tempo: TempoMap


def make_clip(**overrides: object) -> Clip:
    data: dict[str, object] = {
        "at": "beat:8 + 1b",
        "span": {"start": "0s", "end": "15s"},
        "triggers": {"stream": "beat", "start": 0, "stop": 8, "step": 2},
        "fps": 30,
        "tempo": {"bpm": 120, "offset": "0.1s"},
    }
    data.update(overrides)
    return Clip.model_validate(data)


def test_validates_strings() -> None:
    clip = make_clip(hold="300ms")
    assert clip.at == t("beat:8+1b")
    assert clip.hold == t("0.3s")
    assert clip.span == t.span(t("0s"), t("15s"))
    assert clip.triggers == t.beats()[0:8:2]
    assert clip.fps == Fraction(30)
    assert isinstance(clip.fps, Fraction)
    assert clip.tempo.offset == t("0.1s")


def test_accepts_time_objects() -> None:
    clip = make_clip(at=t("cue:logo-2f"), span=t.span(t("start"), t("end")))
    assert clip.at == t("cue:logo-2f")
    assert clip.span.end == t.end


@pytest.mark.parametrize("value", [1.5, 2, None, ["1s"], {"ticks": 5}])
def test_rejects_non_times(value: object) -> None:
    with pytest.raises(ValidationError):
        make_clip(at=value)


def test_rejects_bad_strings() -> None:
    with pytest.raises(ValidationError, match="no unit"):
        make_clip(at="1.5")
    with pytest.raises(ValidationError):
        make_clip(hold="1s+beat:3")


def test_json_dump_is_canonical() -> None:
    clip = make_clip(hold="1500ms", fps="30000/1001")
    dumped = clip.model_dump(mode="json")
    assert dumped["at"] == "beat:8+1b"
    assert dumped["hold"] == "1.5s"
    assert dumped["span"] == {"start": "0s", "end": "15s"}
    assert dumped["triggers"] == {
        "stream": "beat",
        "start": 0,
        "stop": 8,
        "step": 2,
        "times": [],
        "offset": "0s",
        "window": None,
        "every": None,
        "jitter": None,
        "pick": None,
        "seed": None,
    }
    assert dumped["fps"] == "30000/1001"
    assert dumped["tempo"]["offset"] == "0.1s"
    assert make_clip().model_dump(mode="json")["fps"] == 30
    assert json.loads(clip.model_dump_json()) == dumped


def test_json_round_trip() -> None:
    clip = make_clip(hold="cue:x-2f", triggers={"times": ["1s", "beat:2"], "offset": "1b"})
    again = Clip.model_validate_json(clip.model_dump_json())
    assert again == clip
    assert again.triggers.times == (t("1s"), t("beat:2"))


def test_python_dump_keeps_time_objects() -> None:
    dumped = make_clip(hold="1s").model_dump()
    assert dumped["at"] == t("beat:8+1b")
    assert isinstance(dumped["at"], Time)
    assert isinstance(dumped["hold"], Time)
    assert isinstance(dumped["span"]["start"], Time)
    assert dumped["triggers"]["offset"] == t("0s")


def test_json_schema() -> None:
    schema = Clip.model_json_schema()
    at = schema["properties"]["at"]
    assert {key: at[key] for key in ("type", "format", "pattern")} == {
        "type": "string",
        "format": "eks-time",
        "pattern": TIME_PATTERN,
    }
    hold = schema["properties"]["hold"]["anyOf"]
    assert {"type": "string", "format": "eks-time", "pattern": TIME_PATTERN} in hold
    assert {"type": "null"} in hold
    assert schema["properties"]["fps"]["oneOf"] == [
        {"type": "integer", "minimum": 1},
        {"type": "string", "pattern": FPS_PATTERN},
    ]
    span = schema["$defs"]["Span"]["properties"]
    assert span["start"]["format"] == "eks-time"
    assert schema["$defs"]["TimeSet"]["properties"]["offset"]["pattern"] == TIME_PATTERN


def test_span_and_timeset_are_frozen() -> None:
    span = t.span(t("0s"), t("1s"))
    with pytest.raises(ValidationError):
        span.start = t("2s")  # pyright: ignore[reportAttributeAccessIssue]
    times = t.beats()[0:4]
    with pytest.raises(ValidationError):
        times.step = 2  # pyright: ignore[reportAttributeAccessIssue]
    assert hash(span) == hash(t.span(t("0s"), t("1000ms")))


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        Span.model_validate({"start": "0s", "end": "1s", "extra": 1})


def test_timeset_field_constraints() -> None:
    with pytest.raises(ValidationError):
        TimeSet(stream="beat", step=0)
    with pytest.raises(ValidationError):
        TimeSet(stream="beat", start=-1)


MIXED_SHAPES: list[dict[str, object]] = [
    {"stream": "beat", "times": ["1s"]},
    {"times": ["1s"], "start": 3},
    {"times": ["1s"], "stop": 4},
    {"times": ["1s"], "step": 5},
    {"start": 3, "step": 5},
    {"stream": "not a name"},
    {"stream": "-kick"},
    {"stream": "9kick"},
    {"stream": ""},
    {"stream": "beat", "step": True},
    {"stream": "beat", "start": True},
    {"stream": "beat", "stop": False},
    {"stream": "beat", "step": 2.0},
    {"stream": "beat", "start": "1"},
    {"stream": "beat", "offset": "cue:x"},
    {"times": ["1s"], "offset": "beat:2"},
]


@pytest.mark.parametrize("data", MIXED_SHAPES)
def test_timeset_rejects_bad_shapes_from_json(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TimeSet.model_validate(data)
    with pytest.raises(ValidationError):
        TimeSet.model_validate_json(json.dumps(data))


def test_timeset_rejects_bad_shapes_from_constructor() -> None:
    with pytest.raises(ValidationError, match="either a stream slice or an explicit list"):
        TimeSet(stream="beat", times=(t("1s"),))
    with pytest.raises(ValidationError, match="explicit TimeSet"):
        TimeSet(times=(t("1s"),), start=3, step=5)
    with pytest.raises(ValidationError, match="explicit TimeSet"):
        TimeSet(stop=2)
    with pytest.raises(ValidationError, match="not a name"):
        TimeSet(stream="not a name")
    with pytest.raises(ValidationError):
        TimeSet(stream="beat", step=True)
    with pytest.raises(ValidationError, match="must be a duration"):
        TimeSet(stream="beat", offset=t("cue:x"))


def test_timeset_accepts_valid_shapes() -> None:
    assert TimeSet(stream="kick-drum.low", start=1, stop=9, step=2).stream == "kick-drum.low"
    assert TimeSet(times=(t("1s"), t("beat:2"))).times == (t("1s"), t("beat:2"))
    assert TimeSet().times == ()
    assert TimeSet(times=(t("1s"),), offset=t("1b"), window=t.span(t("0s"), t("2s"))).offset == t("1b")
    loaded = TimeSet.model_validate_json('{"stream": "beat", "start": 2, "stop": 8, "step": 2}')
    assert loaded == t.beats()[2:8:2]
    assert TimeSet.model_validate_json(t.set(t("1s")).model_dump_json()) == t.set(t("1s"))


@pytest.mark.parametrize("value", [1.5, 29.97, 0, -30, "0", "30.0", "29.97", "30/0", "-30", "11/2", 11, "11", True])
def test_fps_rejections(value: object) -> None:
    with pytest.raises(ValidationError):
        make_clip(fps=value)
    with pytest.raises(TimeError):
        parse_fps(value)


@pytest.mark.parametrize(
    ("value", "expected", "json_value"),
    [
        (30, Fraction(30), 30),
        ("30", Fraction(30), 30),
        ("30000/1001", Fraction(30000, 1001), "30000/1001"),
        ("24000/1001", Fraction(24000, 1001), "24000/1001"),
        ("60/2", Fraction(30), 30),
        (Fraction(25), Fraction(25), 25),
    ],
)
def test_fps_accepted(value: object, expected: Fraction, json_value: object) -> None:
    assert parse_fps(value) == expected
    assert format_fps(parse_fps(value)) == json_value
    assert make_clip(fps=value).model_dump(mode="json")["fps"] == json_value


def test_tempo_map_models() -> None:
    tempo = TempoMap.model_validate(
        {
            "bpm": 128,
            "meter": {"numerator": 3},
            "changes": [{"beat": 16, "bpm": 140}],
            "meters": [{"bar": 4, "meter": {"numerator": 4, "denominator": 4}}],
        }
    )
    assert tempo.meter == Meter(numerator=3)
    assert tempo.changes == (TempoChange(beat=16, bpm=140),)
    assert tempo.meters == (MeterChange(bar=4, meter=Meter()),)
    assert tempo.model_dump(mode="json")["offset"] == "0s"
    with pytest.raises(ValidationError):
        TempoMap(bpm=0)
    with pytest.raises(ValidationError):
        TempoMap.model_validate({"offset": 0.1})


def test_timeset_anchored_offset_rejected_in_validation() -> None:
    # The anchor's cue must not leak into needs() of a set that could never resolve.
    with pytest.raises(ValidationError, match="must be a duration"):
        TimeSet.model_validate({"stream": "beat", "offset": "cue:x"})
    assert TimeSet.model_validate({"stream": "beat", "offset": "-2f"}).offset == t("-2f")


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (TempoMap, {"bpm": True}),
        (TempoMap, {"bpm": False}),
        (TempoMap, {"bpm": "120"}),
        (TempoChange, {"beat": 4, "bpm": True}),
        (TempoChange, {"beat": True, "bpm": 120}),
        (TempoChange, {"beat": "4", "bpm": 120}),
        (TempoChange, {"beat": 4.0, "bpm": 120}),
        (TempoChange, {"beat": 4, "bpm": "120"}),
        (MeterChange, {"bar": True, "meter": {}}),
        (MeterChange, {"bar": "2", "meter": {}}),
        (MeterChange, {"bar": 2.0, "meter": {}}),
        (Meter, {"numerator": "4"}),
        (Meter, {"numerator": 4.0}),
        (Meter, {"numerator": True}),
        (Meter, {"denominator": True}),
        (Meter, {"denominator": "8"}),
    ],
)
def test_tempo_and_meter_fields_are_strict(model: type[BaseModel], data: dict[str, object]) -> None:
    # A bool must never become a 1 bpm clock, and JSON strings must not coerce into numbers.
    with pytest.raises(ValidationError):
        model.model_validate(data)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(data))


def test_tempo_strict_from_constructor() -> None:
    with pytest.raises(ValidationError):
        TempoMap(bpm=True)
    with pytest.raises(ValidationError):
        TempoMap.model_validate_json('{"bpm": "120"}')
    with pytest.raises(ValidationError):
        Meter.model_validate({"numerator": "4"})


def test_tempo_accepts_int_and_float_bpm() -> None:
    assert TempoMap(bpm=120).bpm == 120.0
    assert TempoMap(bpm=120.5).bpm == 120.5
    assert TempoMap.model_validate_json('{"bpm": 128}').bpm == 128.0
    assert TempoChange.model_validate_json('{"beat": 8, "bpm": 99.5}') == TempoChange(beat=8, bpm=99.5)
    assert Meter.model_validate_json('{"numerator": 7, "denominator": 8}') == Meter(numerator=7, denominator=8)


@pytest.mark.parametrize("bpm", [float("inf"), float("-inf"), float("nan"), 1e300, MAX_BPM + 1, 0, -120])
def test_tempo_bpm_is_bounded(bpm: float) -> None:
    with pytest.raises(ValidationError):
        TempoMap(bpm=bpm)
    with pytest.raises(ValidationError):
        TempoChange(beat=4, bpm=bpm)


@pytest.mark.parametrize("payload", ['{"bpm": 1e300}', '{"bpm": 2001}', '{"bpm": "Infinity"}', '{"bpm": "NaN"}'])
def test_tempo_bpm_is_bounded_from_json(payload: str) -> None:
    with pytest.raises(ValidationError):
        TempoMap.model_validate_json(payload)
    with pytest.raises(ValidationError):
        TempoChange.model_validate_json('{"beat": 0, ' + payload[1:])


def test_tempo_bpm_at_the_bound_resolves() -> None:
    clock = TimeContext.static(tempo=TempoMap(bpm=MAX_BPM), end=60 * 705_600_000)
    assert clock.resolve(t("beat:2000")) == 60 * 705_600_000


def test_unvalidated_tempo_raises_time_error() -> None:
    # model_construct skips validation; resolution must still refuse a non-finite tempo cleanly.
    with pytest.raises(TimeError, match="out of range"):
        TimeContext.static(tempo=TempoMap.model_construct(bpm=float("inf")))
    with pytest.raises(TimeError, match="out of range"):
        TimeContext.static(tempo=TempoMap(bpm=120, changes=(TempoChange.model_construct(beat=1, bpm=1e300),)))
