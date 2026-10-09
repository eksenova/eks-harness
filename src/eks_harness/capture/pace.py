from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MOVE_MS = 300
DWELL_MS = 400
TYPE_MS_PER_CHAR = 40
HOLD_MS = 700
SCREEN_HOLD_MS = 1500
MOBILE_PRESS_MS = 250

PRESETS = ("demo", "fast")

_CAMEL_TO_FIELD = {
    "moveMs": "move_ms",
    "dwellMs": "dwell_ms",
    "typeMsPerChar": "type_ms_per_char",
    "holdMs": "hold_ms",
    "screenHoldMs": "screen_hold_ms",
    "mobilePressMs": "mobile_press_ms",
}

PACE_FIELDS: tuple[str, ...] = ("move_ms", "dwell_ms", "type_ms_per_char", "hold_ms",
                                "screen_hold_ms", "mobile_press_ms")


@dataclass(frozen=True)
class Pace:
    move_ms: int = MOVE_MS
    dwell_ms: int = DWELL_MS
    type_ms_per_char: int = TYPE_MS_PER_CHAR
    hold_ms: int = HOLD_MS
    screen_hold_ms: int = SCREEN_HOLD_MS
    mobile_press_ms: int = MOBILE_PRESS_MS

    def __post_init__(self) -> None:
        for field in PACE_FIELDS:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer of milliseconds")

    def type_hold_ms(self, text: str) -> int:
        return len(text) * self.type_ms_per_char

    def hold_after(self, screen_changed: bool) -> int:
        return self.screen_hold_ms if screen_changed else self.hold_ms

    def to_dict(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in PACE_FIELDS}


FAST_PACE = Pace(move_ms=0, dwell_ms=100, type_ms_per_char=0, hold_ms=150,
                 screen_hold_ms=400, mobile_press_ms=0)


def _coerce_ms(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer of milliseconds")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer of milliseconds")
    return value


def resolve_pace(spec: str | dict[str, Any] | None) -> Pace:
    if spec is None or (isinstance(spec, str) and spec.strip() in ("", "demo")):
        return Pace()
    if isinstance(spec, str):
        text = spec.strip()
        if text == "fast":
            return FAST_PACE
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("--pace takes demo, fast, or a JSON object (got "
                             f"{spec[:80]!r})") from error
        return resolve_pace(parsed)
    if isinstance(spec, dict):
        values: dict[str, int] = {}
        for key, value in spec.items():
            field = _CAMEL_TO_FIELD.get(key, key)
            if field not in PACE_FIELDS:
                raise ValueError(f"unknown pace key {key!r}; known: "
                                 + ", ".join(sorted(set(_CAMEL_TO_FIELD) | set(PACE_FIELDS))))
            values[field] = _coerce_ms(field, value)
        base = Pace().to_dict()
        base.update(values)
        return Pace(**base)
    raise ValueError("--pace takes demo, fast, or a JSON object")


def pace_from_env(env: dict[str, str]) -> Pace:
    values: dict[str, int] = {}
    for field in PACE_FIELDS:
        raw = env.get("EKS_PACE_" + field.upper())
        if raw in (None, ""):
            continue
        try:
            values[field] = _coerce_ms(field, int(str(raw).strip()))
        except (TypeError, ValueError) as error:
            raise ValueError(f"pace env override is not an integer: {raw!r}") from error
    base = Pace().to_dict()
    base.update(values)
    return Pace(**base)


