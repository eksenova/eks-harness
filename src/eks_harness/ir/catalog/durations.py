from __future__ import annotations

import types
import typing
from fractions import Fraction
from typing import Annotated, Any, LiteralString

from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

from eks_harness.ir.time import Time, TimeError, ensure_duration, format_time

ANCHORED_ERROR: LiteralString = "duration_anchor"
NEGATIVE_ERROR: LiteralString = "time_negative"
ZERO_ERROR: LiteralString = "duration_zero"


def _parts(value: Time) -> tuple[Fraction | int, ...]:
    return (value.bars, value.beats, value.frames, value.ticks)


def _is_negative(value: Time) -> bool:
    parts = _parts(value)
    return all(part <= 0 for part in parts) and any(part < 0 for part in parts)


def _is_zero(value: Time) -> bool:
    return all(part == 0 for part in _parts(value))


def _anchored(value: Time) -> str | None:
    if value.anchor is not None:
        return f"is a length such as t('2s'), t('1b') or t('8f'), got the point in time {format_time(value)!r}"
    return None


def _not_positive(value: Time) -> str:
    return f"must be a positive length such as t('2s'), t('1b') or t('8f'), got {format_time(value)!r}"


def _negative_length(value: Time) -> str:
    return f"must not be negative, got {format_time(value)!r}"


def duration_problem(value: Time) -> str | None:
    anchored = _anchored(value)
    if anchored is not None:
        return anchored
    if _is_negative(value) or _is_zero(value):
        return _not_positive(value)
    return None


def length_problem(value: Time) -> str | None:
    anchored = _anchored(value)
    if anchored is not None:
        return anchored
    return _negative_length(value) if _is_negative(value) else None


def _typed(code: LiteralString, what: str, problem: str) -> PydanticCustomError:
    return PydanticCustomError(code, "{what} {problem}", {"what": what, "problem": problem})


def _positive_duration(value: Time) -> Time:
    anchored = _anchored(value)
    if anchored is not None:
        raise _typed(ANCHORED_ERROR, "the duration", anchored)
    if _is_negative(value):
        raise _typed(NEGATIVE_ERROR, "the duration", _not_positive(value))
    if _is_zero(value):
        raise _typed(ZERO_ERROR, "the duration", _not_positive(value))
    return value


def _non_negative_length(value: Time) -> Time:
    anchored = _anchored(value)
    if anchored is not None:
        raise _typed(ANCHORED_ERROR, "the length of time", anchored)
    if _is_negative(value):
        raise _typed(NEGATIVE_ERROR, "the length of time", _negative_length(value))
    return value


class LengthBound:
    __slots__ = ("allow_zero",)

    def __init__(self, *, allow_zero: bool) -> None:
        self.allow_zero = allow_zero

    def __repr__(self) -> str:
        return f"LengthBound(allow_zero={self.allow_zero})"


Duration = Annotated[Time, AfterValidator(_positive_duration), LengthBound(allow_zero=False)]
Length = Annotated[Time, AfterValidator(_non_negative_length), LengthBound(allow_zero=True)]


def length_bound(annotation: Any) -> LengthBound | None:
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        base, *metadata = typing.get_args(annotation)
        found = next((item for item in metadata if isinstance(item, LengthBound)), None)
        return found if found is not None else length_bound(base)
    if origin in (typing.Union, types.UnionType):
        for member in typing.get_args(annotation):
            found = length_bound(member)
            if found is not None:
                return found
    return None


def ensure_positive_duration(value: object, what: str) -> Time:
    duration = ensure_duration(value, what)
    problem = duration_problem(duration)
    if problem is not None:
        raise TimeError(f"{what} {problem}")
    return duration


def optional_positive_duration(value: object, what: str) -> Time | None:
    return None if value is None else ensure_positive_duration(value, what)


def ensure_length(value: object, what: str) -> Time:
    length = ensure_duration(value, what)
    problem = length_problem(length)
    if problem is not None:
        raise TimeError(f"{what} {problem}")
    return length


def optional_length(value: object, what: str) -> Time | None:
    return None if value is None else ensure_length(value, what)


__all__ = [
    "ANCHORED_ERROR",
    "NEGATIVE_ERROR",
    "ZERO_ERROR",
    "Duration",
    "Length",
    "LengthBound",
    "duration_problem",
    "ensure_length",
    "ensure_positive_duration",
    "length_bound",
    "length_problem",
    "optional_length",
    "optional_positive_duration",
]
