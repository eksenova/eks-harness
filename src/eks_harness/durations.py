from __future__ import annotations

import re
from typing import Any

NEVER = "never"
NEVER_WORDS = frozenset({"never", "off", "none", "forever", "inf", "infinite"})
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(seconds|second|secs|sec|ms|s|minutes|minute|mins|min|m|hours|hour|hrs|"
                            r"hr|h|days|day|d)?")
_UNITS = {"ms": 0.001, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60,
          "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400,
          "day": 86400, "days": 86400}


def parse_duration(value: Any) -> float | str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"a duration cannot be negative: {value}")
        return float(value)
    text = str(value).strip().lower()
    if text in NEVER_WORDS:
        return NEVER
    if not text:
        raise ValueError("an empty duration")
    total = 0.0
    position = 0
    for match in _DURATION_PART.finditer(text):
        if text[position:match.start()].strip():
            raise ValueError(f"not a duration: {value!r} (use seconds, 15m, 1h30m or never)")
        total += float(match.group(1)) * _UNITS[match.group(2) or "s"]
        position = match.end()
    if position == 0 or text[position:].strip():
        raise ValueError(f"not a duration: {value!r} (use seconds, 15m, 1h30m or never)")
    return total


def duration_seconds(value: float | str | None) -> float | None:
    if value is None or value == NEVER:
        return None
    return float(value)


_DESCRIBE_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def describe_duration(value: float | str | None) -> str:
    if value is None:
        return "-"
    if value == NEVER:
        return "never"
    seconds = max(0.0, float(value))
    if 0 < seconds < 1:
        return f"{round(seconds, 1):g}s"
    remaining = round(seconds)
    amounts = []
    for unit, size in _DESCRIBE_UNITS:
        amounts.append((unit, remaining // size))
        remaining %= size
    first = next((i for i, (_, amount) in enumerate(amounts) if amount), len(amounts) - 1)
    shown = [f"{amount}{unit}" for unit, amount in amounts[first:first + 2] if amount]
    return " ".join(shown) or "0s"
