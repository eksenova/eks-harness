from __future__ import annotations

import pytest

from eks_harness.capture.pace import (
    FAST_PACE,
    PACE_FIELDS,
    Pace,
    pace_from_env,
    resolve_pace,
)


def test_defaults_match_contract() -> None:
    pace = Pace()
    assert (pace.move_ms, pace.dwell_ms, pace.type_ms_per_char) == (300, 400, 40)
    assert (pace.hold_ms, pace.screen_hold_ms, pace.mobile_press_ms) == (700, 1500, 250)


def test_demo_resolves_to_defaults() -> None:
    assert resolve_pace(None) == Pace()
    assert resolve_pace("demo") == Pace()
    assert resolve_pace("") == Pace()


def test_fast_is_quicker_on_every_axis() -> None:
    pace = resolve_pace("fast")
    assert pace == FAST_PACE
    for field in PACE_FIELDS:
        assert getattr(pace, field) <= getattr(Pace(), field)


def test_json_override_merges_over_defaults() -> None:
    pace = resolve_pace('{"dwellMs": 50, "hold_ms": 100}')
    assert pace.dwell_ms == 50
    assert pace.hold_ms == 100
    assert pace.move_ms == 300


def test_dict_override_accepts_snake_and_camel_keys() -> None:
    pace = resolve_pace({"moveMs": 10, "mobile_press_ms": 20})
    assert pace.move_ms == 10
    assert pace.mobile_press_ms == 20


def test_unknown_pace_key_fails() -> None:
    with pytest.raises(ValueError, match="unknown pace key"):
        resolve_pace({"settleMs": 100})


def test_negative_pace_value_fails() -> None:
    with pytest.raises(ValueError):
        resolve_pace({"holdMs": -1})
    with pytest.raises(ValueError):
        Pace(hold_ms=-5)


def test_garbage_pace_spec_fails() -> None:
    with pytest.raises(ValueError):
        resolve_pace("cinematic")
    with pytest.raises(ValueError):
        resolve_pace(42)  # type: ignore[arg-type]


def test_type_hold_scales_per_character() -> None:
    pace = Pace()
    assert pace.type_hold_ms("") == 0
    assert pace.type_hold_ms("Kaydet") == 6 * 40


def test_screen_change_holds_longer() -> None:
    pace = Pace()
    assert pace.hold_after(True) == 1500
    assert pace.hold_after(False) == 700


def test_env_overrides_pace() -> None:
    pace = pace_from_env({"EKS_PACE_DWELL_MS": "50", "EKS_PACE_HOLD_MS": "60"})
    assert pace.dwell_ms == 50
    assert pace.hold_ms == 60
    assert pace.move_ms == 300


def test_env_override_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        pace_from_env({"EKS_PACE_DWELL_MS": "soon"})


def test_pace_round_trips_through_dict() -> None:
    assert Pace(**Pace().to_dict()) == Pace()
    assert resolve_pace(Pace().to_dict()) == Pace()
