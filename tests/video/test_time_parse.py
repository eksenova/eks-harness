from __future__ import annotations

import pytest

from eks_harness.video.ir.time import (
    BeatRef,
    Frames,
    MarkerRef,
    Seconds,
    TimeRefAdapter,
    WordRef,
    parse_time_string,
)


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("0:16.5", Seconds(t=16.5)),
        ("1:00", Seconds(t=60.0)),
        ("0:00", Seconds(t=0.0)),
        ("12.25", Seconds(t=12.25)),
        ("f:480", Frames(n=480)),
        ("b:kick", BeatRef(stream="kick")),
        ("b:kick:4", BeatRef(stream="kick", every=4)),
        ("w:hello", WordRef(text="hello")),
        ("w:#3", WordRef(index=3)),
        ("m:intro", MarkerRef(name="intro")),
    ],
)
def test_parse_time_string(spec: str, expected: object) -> None:
    assert parse_time_string(spec) == expected


@pytest.mark.parametrize(
    "spec",
    ["0:16.5", "f:480", "b:kick:4", "w:hello", "m:intro"],
)
def test_string_round_trip_through_adapter(spec: str) -> None:
    parsed = TimeRefAdapter.validate_python(spec)
    direct = parse_time_string(spec)
    assert parsed == direct
