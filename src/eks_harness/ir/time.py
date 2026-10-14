from __future__ import annotations

import math
import numbers
import random
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from functools import cached_property
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    PlainSerializer,
    PlainValidator,
    SerializationInfo,
    WithJsonSchema,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, SchemaSerializer, core_schema

from eks_harness.ids import TURKISH_FOLD

TICKS_PER_SECOND = 705_600_000
TICKS_PER_MS = 705_600
SECONDS_DECIMALS = 5
MAX_BPM = 2000

AnchorKind = Literal["start", "end", "frame", "beat", "downbeat", "bar", "cue", "marker", "word"]
OffsetUnit = Literal["s", "ms", "tk", "f", "b", "bar"]

_DIGITS = r"[0-9]+"
_NUM = rf"{_DIGITS}(?:\.{_DIGITS})?"
_NAME = r"[A-Za-z_][A-Za-z0-9_.]*(?:-[A-Za-z_][A-Za-z0-9_.]*)*"
_QUOTED = r"'(?:[^'\\]|\\['\\])*'"
_UNIT = r"(?:ms|s|tk|f|bar|b)"
_QTY = rf"{_NUM}{_UNIT}"
_WS = r"[ \t]*"
_ANCHOR = (
    rf"(?:start|end|f:{_DIGITS}|(?:beat|downbeat|bar):{_NUM}|cue:{_NAME}|marker:{_NAME}(?:#{_DIGITS})?"
    rf"|word:(?:{_NAME}:)?(?:{_QUOTED}(?:#{_DIGITS})?|#{_DIGITS}))"
)
TIME_PATTERN = rf"^{_WS}(?:{_ANCHOR}|[+-]?{_WS}{_QTY})(?:{_WS}[+-]{_WS}{_QTY})*{_WS}$"
FPS_PATTERN = r"^[1-9]\d*(?:/[1-9]\d*)?$"

_NAME_RE = re.compile(_NAME)
_NUM_RE = re.compile(_NUM)
_UINT_RE = re.compile(_DIGITS)
_KEYWORD_RE = re.compile(r"[A-Za-z_]+:?")
_FPS_RE = re.compile(FPS_PATTERN, re.ASCII)
_SIGNED_DECIMAL_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_UNITS: tuple[OffsetUnit, ...] = ("bar", "ms", "tk", "s", "f", "b")
_WHITESPACE = " \t"
_TICKS_PER_SECOND_DIGIT = TICKS_PER_SECOND // 10**SECONDS_DECIMALS
_DEFAULT_HORIZON = 600 * TICKS_PER_SECOND
_GRID_KINDS: dict[str, AnchorKind] = {"beat": "beat", "downbeat": "downbeat", "bar": "bar"}
_ORDERED_ANCHORS = frozenset({"frame", "beat", "downbeat", "bar"})
_ANCHOR_HINT = (
    "anchors are start, end, f:N, beat:X, downbeat:X, bar:X, cue:NAME, marker:NAME#N, "
    "word:'TEXT'#N and word:#I"
)


class TimeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Anchor:
    kind: AnchorKind
    index: Fraction | None = None
    name: str | None = None
    text: str | None = None
    source: str | None = None
    nth: int | None = None

    def __post_init__(self) -> None:
        _check_anchor(self)


_ANCHOR_FIELDS: dict[str, frozenset[str]] = {
    "start": frozenset(),
    "end": frozenset(),
    "frame": frozenset({"index"}),
    "beat": frozenset({"index"}),
    "downbeat": frozenset({"index"}),
    "bar": frozenset({"index"}),
    "cue": frozenset({"name"}),
    "marker": frozenset({"name", "nth"}),
    "word": frozenset({"index", "text", "source", "nth"}),
}


def _anchor_field_index(anchor: Anchor, whole: bool) -> Fraction:
    value: object = anchor.index
    if value is None:
        raise TimeError(f"a {anchor.kind} anchor needs an index")
    if isinstance(value, bool) or not isinstance(value, int | Fraction):
        raise TimeError(f"a {anchor.kind} anchor index must be a Fraction, got {type(value).__name__} {value!r}")
    index = Fraction(value)
    if index < 0:
        raise TimeError(f"a {anchor.kind} anchor index must not be negative, got {index}; indices start at 0")
    if whole and index.denominator != 1:
        raise TimeError(f"a {anchor.kind} anchor index must be a whole number, got {index}")
    return _terminating(index, f"{anchor.kind} index")


def _anchor_field_nth(anchor: Anchor) -> int:
    value: object = 0 if anchor.nth is None else anchor.nth
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimeError(f"a {anchor.kind} anchor nth must be an int, got {type(value).__name__} {value!r}")
    if value < 0:
        raise TimeError(f"a {anchor.kind} anchor nth must not be negative, got {value}; occurrences start at 0")
    return value


def _check_anchor(anchor: Anchor) -> None:
    allowed = _ANCHOR_FIELDS.get(anchor.kind)
    if allowed is None:
        raise TimeError(f"unknown anchor kind {anchor.kind!r}; kinds are {', '.join(_ANCHOR_FIELDS)}")
    present = {
        name
        for name, value in (
            ("index", anchor.index),
            ("name", anchor.name),
            ("text", anchor.text),
            ("source", anchor.source),
            ("nth", anchor.nth),
        )
        if value is not None
    }
    extra = sorted(present - allowed)
    if extra:
        accepted = ", ".join(sorted(allowed)) if allowed else "no fields"
        raise TimeError(f"a {anchor.kind} anchor takes {accepted}, but {', '.join(extra)} was set")
    match anchor.kind:
        case "frame":
            object.__setattr__(anchor, "index", _anchor_field_index(anchor, whole=True))
        case "beat" | "downbeat" | "bar":
            object.__setattr__(anchor, "index", _anchor_field_index(anchor, whole=False))
        case "cue":
            _check_name(anchor.name, "cue")
        case "marker":
            _check_name(anchor.name, "marker stream")
            object.__setattr__(anchor, "nth", _anchor_field_nth(anchor))
        case "word":
            if (anchor.text is None) == (anchor.index is None):
                raise TimeError("a word anchor takes exactly one of text or index")
            if anchor.source is not None:
                _check_name(anchor.source, "transcript")
            if anchor.text is not None:
                if not isinstance(anchor.text, str):
                    raise TimeError(f"a word anchor text must be a string, got {type(anchor.text).__name__}")
                object.__setattr__(anchor, "nth", _anchor_field_nth(anchor))
            else:
                if anchor.nth is not None:
                    raise TimeError("a word anchor with an index picks one word and takes no nth")
                object.__setattr__(anchor, "index", _anchor_field_index(anchor, whole=True))


@dataclass(frozen=True, slots=True)
class TimeNeeds:
    fps: bool = False
    tempo: bool = False
    window: bool = False
    cues: frozenset[str] = frozenset()
    markers: frozenset[str] = frozenset()
    words: frozenset[str] = frozenset()
    default_words: bool = False
    downbeat: bool = False

    def __or__(self, other: TimeNeeds) -> TimeNeeds:
        return TimeNeeds(
            fps=self.fps or other.fps,
            tempo=self.tempo or other.tempo,
            window=self.window or other.window,
            cues=self.cues | other.cues,
            markers=self.markers | other.markers,
            words=self.words | other.words,
            default_words=self.default_words or other.default_words,
            downbeat=self.downbeat or other.downbeat,
        )

    @property
    def empty(self) -> bool:
        return not (
            self.fps
            or self.tempo
            or self.window
            or self.cues
            or self.markers
            or self.words
            or self.default_words
            or self.downbeat
        )


@dataclass(frozen=True, slots=True)
class StreamReference:
    pass


@dataclass(frozen=True, slots=True)
class WordsReference:
    pass


STREAM_REFERENCE = StreamReference()
WORDS_REFERENCE = WordsReference()
CLOCK_NAME_PATTERN = rf"^{_NAME}$"

StreamName = Annotated[
    str,
    STREAM_REFERENCE,
    Field(pattern=CLOCK_NAME_PATTERN, json_schema_extra={"x-eks-clock": "stream"}),
]
WordSource = Annotated[
    str,
    WORDS_REFERENCE,
    Field(pattern=CLOCK_NAME_PATTERN, json_schema_extra={"x-eks-clock": "words"}),
]


def stream_needs(stream: str) -> TimeNeeds:
    if stream in _GRID_KINDS:
        return TimeNeeds(tempo=True, downbeat=stream == "downbeat")
    return TimeNeeds(markers=frozenset({stream}))


def words_needs(source: str) -> TimeNeeds:
    return TimeNeeds(words=frozenset({source}))


def field_needs(metadata: Sequence[Any], value: Any) -> TimeNeeds:
    if not isinstance(value, str):
        return TimeNeeds()
    needs = TimeNeeds()
    for item in metadata:
        if isinstance(item, StreamReference):
            needs |= stream_needs(value)
        elif isinstance(item, WordsReference):
            needs |= words_needs(value)
    return needs


def _validate_time(value: Any) -> Time:
    if isinstance(value, Time):
        return value
    if isinstance(value, str):
        return parse_time(value)
    raise ValueError(f"expected a time such as '1.5s' or 'beat:8', got {type(value).__name__} {value!r}")


def _serialize_time(value: Time, info: SerializationInfo) -> Time | str:
    return format_time(value) if info.mode_is_json() else value


_TIME_SERIALIZATION = core_schema.plain_serializer_function_ser_schema(_serialize_time, info_arg=True)


def _decimal_places(denominator: int) -> int | None:
    rest = denominator
    twos = 0
    fives = 0
    while rest % 2 == 0:
        rest //= 2
        twos += 1
    while rest % 5 == 0:
        rest //= 5
        fives += 1
    if rest != 1:
        return None
    return max(twos, fives)


def _decimal(value: Fraction) -> str:
    magnitude = abs(Fraction(value))
    places = _decimal_places(magnitude.denominator)
    if places is None:
        raise TimeError(f"{value} is not a terminating decimal; times use decimals such as 0.25 or 2.5")
    digits = str(magnitude.numerator * (10**places // magnitude.denominator))
    if places:
        digits = digits.rjust(places + 1, "0")
        digits = f"{digits[:-places]}.{digits[-places:]}"
    return f"-{digits}" if value < 0 else digits


def _terminating(value: Fraction, what: str) -> Fraction:
    if _decimal_places(value.denominator) is None:
        raise TimeError(
            f"{what} {value} is not a terminating decimal; use a decimal such as 0.25 or 2.5 "
            "(thirds and other repeating fractions cannot be written in the time grammar)"
        )
    return value


def _offset_component(value: object, what: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, int | Fraction):
        raise TimeError(f"Time.{what} must be a Fraction, got {type(value).__name__} {value!r}")
    return _terminating(Fraction(value), f"Time.{what}")


def _tick_component(value: object) -> int:
    if isinstance(value, bool):
        raise TimeError(f"Time.ticks must be an int, got bool {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    raise TimeError(f"Time.ticks must be a whole number of ticks, got {type(value).__name__} {value!r}")


def _round(value: Fraction) -> int:
    return round(value)


def _quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True, slots=True)
class Time:
    anchor: Anchor | None = None
    bars: Fraction = Fraction(0)
    beats: Fraction = Fraction(0)
    frames: Fraction = Fraction(0)
    ticks: int = 0

    __pydantic_serializer__: ClassVar[SchemaSerializer] = SchemaSerializer(
        core_schema.any_schema(serialization=_TIME_SERIALIZATION)
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", _offset_component(self.bars, "bars"))
        object.__setattr__(self, "beats", _offset_component(self.beats, "beats"))
        object.__setattr__(self, "frames", _offset_component(self.frames, "frames"))
        object.__setattr__(self, "ticks", _tick_component(self.ticks))
        if self.anchor is not None and not isinstance(self.anchor, Anchor):
            raise TimeError(f"Time.anchor must be an Anchor, got {type(self.anchor).__name__} {self.anchor!r}")

    @property
    def is_concrete(self) -> bool:
        return self.anchor is None and self.bars == 0 and self.beats == 0 and self.frames == 0

    def needs(self) -> TimeNeeds:
        anchor = self.anchor
        kind = anchor.kind if anchor is not None else None
        cues: frozenset[str] = frozenset()
        markers: frozenset[str] = frozenset()
        words: frozenset[str] = frozenset()
        default_words = False
        if anchor is not None and anchor.name is not None and kind == "cue":
            cues = frozenset({anchor.name})
        if anchor is not None and anchor.name is not None and kind == "marker":
            markers = frozenset({anchor.name})
        if anchor is not None and kind == "word":
            if anchor.source is None:
                default_words = True
            else:
                words = frozenset({anchor.source})
        return TimeNeeds(
            fps=self.frames != 0 or kind == "frame",
            tempo=self.bars != 0 or self.beats != 0 or kind in {"beat", "downbeat", "bar"},
            window=kind == "end",
            cues=cues,
            markers=markers,
            words=words,
            default_words=default_words,
            downbeat=kind == "downbeat",
        )

    def __add__(self, other: Time) -> Time:
        if not isinstance(other, Time):
            return NotImplemented
        if self.anchor is not None and other.anchor is not None:
            raise TimeError(
                f"cannot add {format_time(self)!r} and {format_time(other)!r}: both carry an anchor and a time "
                "has at most one; add a duration such as t('1b') or t('2f') instead"
            )
        return Time(
            anchor=self.anchor if self.anchor is not None else other.anchor,
            bars=self.bars + other.bars,
            beats=self.beats + other.beats,
            frames=self.frames + other.frames,
            ticks=self.ticks + other.ticks,
        )

    def __sub__(self, other: Time) -> Time:
        if not isinstance(other, Time):
            return NotImplemented
        if other.anchor is not None:
            raise TimeError(
                f"cannot subtract {format_time(other)!r} from {format_time(self)!r}: only durations can be "
                "subtracted; write a duration such as t('2f') or t('1b')"
            )
        return Time(
            anchor=self.anchor,
            bars=self.bars - other.bars,
            beats=self.beats - other.beats,
            frames=self.frames - other.frames,
            ticks=self.ticks - other.ticks,
        )

    def __neg__(self) -> Time:
        if self.anchor is not None:
            raise TimeError(f"cannot negate {format_time(self)!r}: only durations such as t('1s') can be negated")
        return Time(bars=-self.bars, beats=-self.beats, frames=-self.frames, ticks=-self.ticks)

    def __mul__(self, factor: int) -> Time:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise TypeError(
                f"a time can only be multiplied by an int, got {type(factor).__name__} {factor!r}; "
                "write the scaled time directly, e.g. t('1.5s') or t.secs(1.5)"
            )
        if self.anchor is not None:
            raise TimeError(
                f"cannot multiply {format_time(self)!r}: only durations such as t('1b') can be multiplied"
            )
        return Time(
            bars=self.bars * factor,
            beats=self.beats * factor,
            frames=self.frames * factor,
            ticks=self.ticks * factor,
        )

    def __rmul__(self, factor: int) -> Time:
        return self.__mul__(factor)

    def _ordered(self, other: Time, symbol: str) -> tuple[int, int]:
        if not (self.is_concrete and other.is_concrete):
            raise TimeError(
                f"cannot evaluate {format_time(self)!r} {symbol} {format_time(other)!r}: only concrete times "
                "(s, ms, tk) can be ordered; resolve symbolic times on a clock first"
            )
        return self.ticks, other.ticks

    def __lt__(self, other: Time) -> bool:
        if not isinstance(other, Time):
            return NotImplemented
        left, right = self._ordered(other, "<")
        return left < right

    def __le__(self, other: Time) -> bool:
        if not isinstance(other, Time):
            return NotImplemented
        left, right = self._ordered(other, "<=")
        return left <= right

    def __gt__(self, other: Time) -> bool:
        if not isinstance(other, Time):
            return NotImplemented
        left, right = self._ordered(other, ">")
        return left > right

    def __ge__(self, other: Time) -> bool:
        if not isinstance(other, Time):
            return NotImplemented
        left, right = self._ordered(other, ">=")
        return left >= right

    def __str__(self) -> str:
        return format_time(self)

    def __repr__(self) -> str:
        return f"t({format_time(self)!r})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            _validate_time,
            serialization=_TIME_SERIALIZATION,
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "format": "eks-time", "pattern": TIME_PATTERN}


ZERO = Time()


def _ticks_of(value: Fraction, scale: int, spelled: str, source: str) -> int:
    scaled = value * scale
    if scaled.denominator != 1:
        raise TimeError(
            f"{source}: {spelled} is finer than one tick (1/{TICKS_PER_SECOND} s); use at most "
            f"{SECONDS_DECIMALS} decimals of seconds or 2 of milliseconds"
        )
    return scaled.numerator


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self, problem: str, hint: str) -> TimeError:
        return TimeError(f"invalid time {self.text!r}: {problem} at position {self.pos}; {hint}")

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in _WHITESPACE:
            self.pos += 1

    def match(self, pattern: re.Pattern[str]) -> str | None:
        found = pattern.match(self.text, self.pos)
        if found is None:
            return None
        self.pos = found.end()
        return found.group()

    def sign(self) -> int | None:
        char = self.peek()
        if char in {"+", "-"}:
            self.pos += 1
            return -1 if char == "-" else 1
        return None

    def parse(self) -> Time:
        self.skip_ws()
        if self.pos >= len(self.text):
            raise TimeError(
                f"empty time {self.text!r}; write a time such as '1.5s', 'beat:8' or 'cue:logo-2f'"
            )
        anchor: Anchor | None = None
        terms: list[tuple[int, OffsetUnit, Fraction, str]] = []
        if _is_letter(self.peek()):
            anchor = self.anchor()
        else:
            sign = self.sign() or 1
            self.skip_ws()
            terms.append(self.quantity(sign))
        while True:
            self.skip_ws()
            if self.pos >= len(self.text):
                break
            sign = self.sign()
            if sign is None:
                raise self.fail(
                    f"unexpected {self.text[self.pos:]!r}",
                    "join terms with '+' or '-', e.g. 'beat:8+1b' or 'cue:logo-2f'",
                )
            self.skip_ws()
            if _is_letter(self.peek()):
                raise self.fail(
                    "an anchor can only come first",
                    "write the anchor first and add durations to it, e.g. 'beat:3+1s'",
                )
            terms.append(self.quantity(sign))
        return self.build(anchor, terms)

    def build(self, anchor: Anchor | None, terms: list[tuple[int, OffsetUnit, Fraction, str]]) -> Time:
        bars = Fraction(0)
        beats = Fraction(0)
        frames = Fraction(0)
        ticks = 0
        for sign, unit, value, spelled in terms:
            match unit:
                case "bar":
                    bars += sign * value
                case "b":
                    beats += sign * value
                case "f":
                    frames += sign * value
                case "s":
                    ticks += sign * _ticks_of(value, TICKS_PER_SECOND, spelled, f"invalid time {self.text!r}")
                case "ms":
                    ticks += sign * _ticks_of(value, TICKS_PER_MS, spelled, f"invalid time {self.text!r}")
                case "tk":
                    ticks += sign * _ticks_of(value, 1, spelled, f"invalid time {self.text!r}")
        return Time(anchor=anchor, bars=bars, beats=beats, frames=frames, ticks=ticks)

    def quantity(self, sign: int) -> tuple[int, OffsetUnit, Fraction, str]:
        number = self.match(_NUM_RE)
        if number is None:
            raise self.fail(
                f"expected a number with a unit, got {self.text[self.pos:]!r}",
                "write a duration such as '1.5s', '300ms', '2f', '1b' or '1bar'",
            )
        for unit in _UNITS:
            if self.text.startswith(unit, self.pos):
                self.pos += len(unit)
                return sign, unit, Fraction(number), f"{number}{unit}"
        raise self.fail(
            f"the number {number!r} has no unit",
            f"write the unit right after it (ms, s, tk, f, b or bar), e.g. '{number}s'",
        )

    def anchor(self) -> Anchor:
        keyword = self.match(_KEYWORD_RE) or ""
        match keyword:
            case "start":
                return Anchor(kind="start")
            case "end":
                return Anchor(kind="end")
            case "f:":
                return Anchor(kind="frame", index=Fraction(self.uint("a frame number, e.g. 'f:120'")))
            case "beat:" | "downbeat:" | "bar:":
                kind = _GRID_KINDS[keyword[:-1]]
                number = self.match(_NUM_RE)
                if number is None:
                    raise self.fail(f"{keyword} needs an index", f"write e.g. '{keyword}8' or '{keyword}2.5'")
                return Anchor(kind=kind, index=Fraction(number))
            case "cue:":
                return Anchor(kind="cue", name=self.name("cue", "e.g. 'cue:logo-intro'"))
            case "marker:":
                stream = self.name("marker stream", "e.g. 'marker:intro#2'")
                nth = self.uint("an occurrence after '#', e.g. 'marker:intro#2'") if self.take("#") else 0
                return Anchor(kind="marker", name=stream, nth=nth)
            case "word:":
                return self.word()
            case _:
                raise self.fail(f"unknown anchor {keyword!r}", _ANCHOR_HINT)

    def take(self, literal: str) -> bool:
        if self.text.startswith(literal, self.pos):
            self.pos += len(literal)
            return True
        return False

    def uint(self, what: str) -> int:
        digits = self.match(_UINT_RE)
        if digits is None:
            raise self.fail("expected a whole number", f"write {what}")
        return int(digits)

    def name(self, what: str, example: str) -> str:
        found = self.match(_NAME_RE)
        if found is None:
            raise self.fail(
                f"expected a {what} name",
                f"names start with a letter or '_' and may contain digits, '_', '.' and '-', {example}",
            )
        return found

    def word(self) -> Anchor:
        source: str | None = None
        if self.peek() not in {"'", "#"}:
            source = self.name("transcript", "e.g. \"word:vo:'Merhaba'\"")
            if not self.take(":"):
                raise self.fail(
                    "expected ':' after the transcript name",
                    "write \"word:SOURCE:'TEXT'\" or \"word:SOURCE:#I\"",
                )
        if self.take("#"):
            return Anchor(kind="word", index=Fraction(self.uint("a word index, e.g. 'word:#12'")), source=source)
        if self.peek() != "'":
            raise self.fail("expected a quoted word or '#INDEX'", "write e.g. \"word:'EksenGOLD'\" or 'word:#12'")
        text = self.quoted()
        nth = self.uint("an occurrence after '#', e.g. \"word:'Merhaba'#1\"") if self.take("#") else 0
        return Anchor(kind="word", text=text, source=source, nth=nth)

    def quoted(self) -> str:
        self.pos += 1
        chars: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "'":
                self.pos += 1
                return "".join(chars)
            if char == "\\":
                escaped = self.text[self.pos + 1 : self.pos + 2]
                if escaped not in {"'", "\\"} or not escaped:
                    raise self.fail("unsupported escape in quoted word", "only \\' and \\\\ are escapes")
                chars.append(escaped)
                self.pos += 2
                continue
            chars.append(char)
            self.pos += 1
        raise self.fail("unterminated quoted word", "close the word with a single quote, e.g. \"word:'Merhaba'\"")


def _is_letter(char: str) -> bool:
    return char.isascii() and (char.isalpha() or char == "_")


def parse_time(text: str) -> Time:
    if not isinstance(text, str):
        raise TimeError(f"expected a time string such as '1.5s', got {type(text).__name__} {text!r}")
    return _Parser(text).parse()


def _format_anchor(anchor: Anchor) -> str:
    match anchor.kind:
        case "start" | "end":
            return anchor.kind
        case "frame":
            return f"f:{_decimal(_need(anchor.index, anchor))}"
        case "beat" | "downbeat" | "bar":
            return f"{anchor.kind}:{_decimal(_need(anchor.index, anchor))}"
        case "cue":
            return f"cue:{_need(anchor.name, anchor)}"
        case "marker":
            suffix = f"#{anchor.nth}" if anchor.nth else ""
            return f"marker:{_need(anchor.name, anchor)}{suffix}"
        case "word":
            prefix = f"word:{anchor.source}:" if anchor.source is not None else "word:"
            if anchor.text is not None:
                suffix = f"#{anchor.nth}" if anchor.nth else ""
                return f"{prefix}{_quote(anchor.text)}{suffix}"
            return f"{prefix}#{_decimal(_need(anchor.index, anchor))}"


def _need[T](value: T | None, anchor: Anchor) -> T:
    if value is None:
        raise TimeError(f"incomplete {anchor.kind} anchor {anchor!r}")
    return value


def _format_term(value: Fraction, unit: str, first: bool) -> str:
    if value < 0:
        return f"-{_decimal(-value)}{unit}"
    return f"{'' if first else '+'}{_decimal(value)}{unit}"


def format_time(time: Time) -> str:
    parts: list[str] = []
    if time.anchor is not None:
        parts.append(_format_anchor(time.anchor))
    for value, unit in ((time.bars, "bar"), (time.beats, "b"), (time.frames, "f")):
        if value != 0:
            parts.append(_format_term(value, unit, not parts))
    if time.ticks != 0:
        if time.ticks % _TICKS_PER_SECOND_DIGIT == 0:
            parts.append(_format_term(Fraction(time.ticks, TICKS_PER_SECOND), "s", not parts))
        else:
            parts.append(_format_term(Fraction(time.ticks), "tk", not parts))
    return "".join(parts) or "0s"


def _is_number(value: object) -> bool:
    return isinstance(value, numbers.Number) and not isinstance(value, bool)


def ensure_time(value: object, what: str) -> Time:
    if isinstance(value, Time):
        return value
    if isinstance(value, str):
        raise TypeError(f"{what} expects a Time, got the string {value!r}; pass t({value!r}) instead of a string")
    if _is_number(value):
        raise TypeError(
            f"{what} expects a Time, got the number {value!r}; bare numbers are not times, "
            "write the unit explicitly, e.g. t('1.5s')"
        )
    raise TypeError(f"{what} expects a Time such as t('1.5s'), got {type(value).__name__} {value!r}")


def optional_time(value: object, what: str) -> Time | None:
    return None if value is None else ensure_time(value, what)


def ensure_duration(value: object, what: str) -> Time:
    duration = ensure_time(value, what)
    if duration.anchor is not None:
        raise TimeError(
            f"{what} is a duration such as t('2s') or t('2b'), got the point in time {format_time(duration)!r}"
        )
    return duration


def coerce_time(value: object) -> Time:
    if isinstance(value, Time):
        return value
    if isinstance(value, str):
        return parse_time(value)
    if _is_number(value):
        raise TimeError(f"bare numbers are not times, got {value!r}; write a string with a unit such as '1.5s'")
    raise TimeError(f"expected a time string such as '1.5s', got {type(value).__name__} {value!r}")


def parse_fps(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise TimeError(f"invalid frame rate {value!r}; write an integer such as 30 or a ratio such as '30000/1001'")
    if isinstance(value, Fraction):
        fps = value
    elif isinstance(value, int):
        fps = Fraction(value)
    elif isinstance(value, str):
        if _FPS_RE.fullmatch(value) is None:
            raise TimeError(
                f"invalid frame rate {value!r}; write an integer such as 30 or a ratio such as '30000/1001'"
            )
        fps = Fraction(value)
    elif isinstance(value, float | Decimal):
        raise TimeError(
            f"frame rate {value!r} is a decimal number, which is not exact; write 30 or a ratio such as '30000/1001'"
        )
    else:
        raise TimeError(
            f"invalid frame rate {type(value).__name__} {value!r}; write an integer such as 30 or '30000/1001'"
        )
    if fps <= 0:
        raise TimeError(f"frame rate {value!r} must be positive; write e.g. 30 or '30000/1001'")
    ticks_per_frame(fps)
    return fps


def format_fps(value: Fraction) -> int | str:
    rate = Fraction(value)
    if rate.denominator == 1:
        return rate.numerator
    return f"{rate.numerator}/{rate.denominator}"


def ticks_per_frame(fps: Fraction) -> int:
    rate = Fraction(fps)
    if rate <= 0:
        raise TimeError(f"frame rate {format_fps(rate)} must be positive")
    ticks = TICKS_PER_SECOND / rate
    if ticks.denominator != 1:
        raise TimeError(
            f"frame rate {format_fps(rate)} does not divide {TICKS_PER_SECOND} ticks per second exactly; use 24, "
            "25, 30, 48, 50, 60, 120 or an NTSC rate such as '24000/1001', '30000/1001' or '60000/1001'"
        )
    return ticks.numerator


Fps = Annotated[
    Fraction,
    PlainValidator(parse_fps),
    PlainSerializer(format_fps, when_used="json"),
    WithJsonSchema({"oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": FPS_PATTERN}]}),
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Span(_Frozen):
    start: Time = Field(description="Start time")
    end: Time = Field(description="End time")

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if span_reversed(self.start, self.end):
            raise ValueError(
                f"span {format_time(self.start)!r} to {format_time(self.end)!r} ends before it starts; swap the times"
            )
        return self

    def needs(self) -> TimeNeeds:
        return self.start.needs() | self.end.needs()


def _anchor_step(start: Anchor | None, end: Anchor | None) -> Fraction | None:
    if start == end:
        return Fraction(0)
    if start is None or end is None or start.kind != end.kind:
        return None
    if start.kind in _ORDERED_ANCHORS and start.index is not None and end.index is not None:
        return end.index - start.index
    same_stream = start.kind == "marker" and start.name == end.name
    same_word = start.kind == "word" and (start.text, start.source) == (end.text, end.source)
    if (same_stream or same_word) and start.nth is not None and end.nth is not None and start.index == end.index:
        return Fraction(end.nth - start.nth)
    if start.kind == "word" and start.source == end.source and start.index is not None and end.index is not None:
        return end.index - start.index
    return None


def span_reversed(start: Time, end: Time) -> bool:
    step = _anchor_step(start.anchor, end.anchor)
    if step is None:
        return False
    differences = (step, end.bars - start.bars, end.beats - start.beats, end.frames - start.frames,
                   end.ticks - start.ticks)
    return all(part <= 0 for part in differences) and any(part < 0 for part in differences)


def _anchored_offset_message(time: Time, what: str) -> str:
    return (
        f"{what} {format_time(time)!r} carries an anchor; a TimeSet offset must be a duration such as "
        "t('1b') or t('-2f')"
    )


def _duration_offset(time: Time, what: str) -> Time:
    if time.anchor is not None:
        raise TimeError(_anchored_offset_message(time, what))
    return time


def _negative_parts(time: Time) -> bool:
    return any(part < 0 for part in (time.bars, time.beats, time.frames, time.ticks))


def _positive_length(time: Time) -> bool:
    parts = (time.bars, time.beats, time.frames, time.ticks)
    return time.anchor is None and not _negative_parts(time) and any(parts)


def _seed(given: int | None, current: int | None) -> int | None:
    if given is not None and (isinstance(given, bool) or not isinstance(given, int)):
        raise TimeError(f"seed= takes an int, got {type(given).__name__} {given!r}")
    return given if given is not None else current


class TimeSet(_Frozen):
    stream: str | None = Field(default=None, description="Beat or marker stream, such as downbeat or song.drop; explicit times when unset")
    start: int = Field(default=0, ge=0, strict=True, description="First index of the stream slice")
    stop: int | None = Field(default=None, ge=0, strict=True, description="Index after the last one of the slice; to the end when unset")
    step: int = Field(default=1, ge=1, strict=True, description="Step of the slice")
    times: tuple[Time, ...] = Field(default=(), description="Explicit times when there is no stream")
    offset: Time = Field(default=ZERO, description="Length added to every time")
    window: Span | None = Field(default=None, description="Keep only times inside this span")
    every: Time | None = Field(
        default=None, description="Period of a periodic set: one time every this long from the window start to its end")
    jitter: Time | None = Field(
        default=None, description="Move every time by a seeded random length of at most this much, earlier or later")
    pick: int | None = Field(
        default=None, ge=1, strict=True, description="Keep only this many of the times, chosen at random with the seed")
    seed: int | None = Field(
        default=None, strict=True, description="Seed of jitter and pick; derived from the node seed when unset")

    @model_validator(mode="after")
    def _check_shape(self) -> TimeSet:
        self._check_random()
        if self.every is not None:
            if self.stream is not None or self.times:
                raise ValueError("a periodic TimeSet (every=) has no stream and no explicit times")
            if self.window is None:
                raise ValueError("a periodic TimeSet (every=) needs a window; build it with t.every(period, span)")
            if not _positive_length(self.every):
                raise ValueError(
                    f"TimeSet every {format_time(self.every)!r} must be a positive length without an anchor, "
                    "such as t('1s') or t('2b')")
        if self.stream is not None:
            if _NAME_RE.fullmatch(self.stream) is None:
                raise ValueError(
                    f"TimeSet stream {self.stream!r} is not a name; use letters, digits, '_', '.' and inner "
                    "hyphens, starting with a letter or '_', such as 'beat' or 'kick'"
                )
            if self.times:
                raise ValueError(
                    f"a TimeSet is either a stream slice or an explicit list; this one has stream {self.stream!r} "
                    "and explicit times"
                )
        elif (self.start, self.stop, self.step) != (0, None, 1):
            raise ValueError(
                "start, stop and step slice a stream; an explicit TimeSet (stream None) cannot set them, "
                f"got start={self.start}, stop={self.stop}, step={self.step}"
            )
        if self.offset.anchor is not None:
            raise ValueError(_anchored_offset_message(self.offset, "TimeSet offset"))
        return self

    def __add__(self, other: Time) -> TimeSet:
        if not isinstance(other, Time):
            return NotImplemented
        offset = self.offset + _duration_offset(other, "cannot shift a TimeSet by")
        return self.model_copy(update={"offset": offset})

    def __sub__(self, other: Time) -> TimeSet:
        if not isinstance(other, Time):
            return NotImplemented
        offset = self.offset - _duration_offset(other, "cannot shift a TimeSet by")
        return self.model_copy(update={"offset": offset})

    def _check_random(self) -> None:
        if self.jitter is not None and (self.jitter.anchor is not None or _negative_parts(self.jitter)):
            raise ValueError(
                f"TimeSet jitter {format_time(self.jitter)!r} must be a non-negative length without an anchor, "
                "such as t('50ms')")
        if self.seed is not None and self.jitter is None and self.pick is None:
            raise ValueError("TimeSet seed= only seeds jitter and pick; set one of them or drop the seed")

    def within(self, span: Span) -> TimeSet:
        if not isinstance(span, Span):
            raise TypeError(f"within() expects a Span such as t.span(t('0s'), t('15s')), got {span!r}")
        return self.model_copy(update={"window": span})

    def random(self, count: int, *, seed: int | None = None) -> TimeSet:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise TimeError(f"random() keeps a positive number of times, got {count!r}")
        return TimeSet.model_validate({**self._fields(), "pick": count, "seed": _seed(seed, self.seed)})

    def jittered(self, amount: Time, *, seed: int | None = None) -> TimeSet:
        if not isinstance(amount, Time):
            raise TypeError(f"jittered() takes a length such as t('50ms'), got {type(amount).__name__} {amount!r}")
        return TimeSet.model_validate({**self._fields(), "jitter": amount, "seed": _seed(seed, self.seed)})

    def _fields(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    def needs(self) -> TimeNeeds:
        needs = TimeNeeds()
        for length in (self.every, self.jitter):
            if length is not None:
                needs |= length.needs()
        if self.stream is not None:
            needs |= stream_needs(self.stream)
        for time in self.times:
            needs |= time.needs()
        needs |= self.offset.needs()
        if self.window is not None:
            needs |= self.window.needs()
        return needs


class Meter(_Frozen):
    numerator: int = Field(default=4, ge=1, strict=True, description="Beats per bar")
    denominator: int = Field(default=4, ge=1, strict=True, description="Note value of one beat (4 = quarter note)")


class TempoChange(_Frozen):
    beat: int = Field(ge=0, strict=True, description="Beat index the new tempo starts at")
    bpm: float = Field(gt=0, le=MAX_BPM, allow_inf_nan=False, strict=True, description="Tempo from that beat on")


class MeterChange(_Frozen):
    bar: int = Field(ge=0, strict=True, description="Bar index the new meter starts at")
    meter: Meter = Field(description="Time signature from that bar on")


class TempoMap(_Frozen):
    bpm: float | None = Field(default=None, gt=0, le=MAX_BPM, allow_inf_nan=False, strict=True,
                              description="Constant tempo in beats per minute; unset when the beats come from analysis")
    offset: Time = Field(default=ZERO, description="Time of beat 0")
    meter: Meter = Field(default=Meter(), description="Time signature at bar 0")
    changes: tuple[TempoChange, ...] = Field(default=(), description="Tempo changes in beat order")
    meters: tuple[MeterChange, ...] = Field(default=(), description="Time signature changes in bar order")
    source: str | None = Field(default=None, description="Id of the media.markers node whose beats drive the clock")
    external: bool = Field(default=False, description="The tempo comes from outside the plan, such as a live preview")


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    ticks: int


def _known(names: Sequence[str] | Mapping[str, object]) -> str:
    listed = sorted(names)
    return ", ".join(repr(name) for name in listed) if listed else "none"


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.translate(TURKISH_FOLD).casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _grid_interval(grid: Sequence[int], index: int, what: str) -> int:
    interval = grid[index + 1] - grid[index]
    if interval <= 0:
        raise TimeError(
            f"the {what} grid is not strictly ascending at index {index} ({grid[index]} then {grid[index + 1]} ticks)"
        )
    return interval


def _check_grid(grid: Sequence[int], what: str) -> None:
    if len(grid) < 2:
        raise TimeError(
            f"the clock's {what} grid has {len(grid)} point(s) but resolving {what} times needs at least 2; "
            "give the clock a bpm or an analysed song"
        )


def _grid_at(grid: Sequence[int], index: Fraction, what: str) -> Fraction:
    if index < 0:
        raise TimeError(f"{what} index {_decimal(index)} is negative; {what} indices start at 0")
    _check_grid(grid, what)
    last = len(grid) - 1
    whole = min(math.floor(index), last - 1)
    return grid[whole] + (index - whole) * _grid_interval(grid, whole, what)


def _grid_index(grid: Sequence[int], ticks: Fraction, what: str) -> Fraction:
    _check_grid(grid, what)
    last = len(grid) - 1
    if ticks <= grid[0]:
        return (ticks - grid[0]) / _grid_interval(grid, 0, what)
    if ticks >= grid[last]:
        return last + (ticks - grid[last]) / _grid_interval(grid, last - 1, what)
    whole = bisect_right(grid, ticks) - 1
    return whole + (ticks - grid[whole]) / _grid_interval(grid, whole, what)


def _beat_ticks(bpm: float) -> Fraction:
    if isinstance(bpm, bool) or not isinstance(bpm, int | float) or not math.isfinite(bpm) or not 0 < bpm <= MAX_BPM:
        raise TimeError(f"tempo {bpm!r} bpm is out of range; a tempo is a finite number above 0 and at most {MAX_BPM}")
    return Fraction(60 * TICKS_PER_SECOND) / Fraction(repr(bpm))


@dataclass(frozen=True)
class TimeContext:
    fps: Fraction | None = None
    beats: tuple[int, ...] = ()
    downbeats: tuple[int, ...] = ()
    meter: Meter = field(default_factory=Meter)
    meters: tuple[MeterChange, ...] = ()
    cues: Mapping[str, int] = field(default_factory=dict)
    markers: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    words: Mapping[str, tuple[Word, ...]] = field(default_factory=dict)
    default_words: str | None = None
    start: int = 0
    end: int | None = None

    @classmethod
    def static(
        cls,
        *,
        fps: Fraction | None = None,
        tempo: TempoMap | None = None,
        cues: Mapping[str, Time] | None = None,
        start: int = 0,
        end: int | None = None,
        horizon: int | None = None,
    ) -> TimeContext:
        tempo_map = tempo if tempo is not None else TempoMap()
        rate = parse_fps(fps) if fps is not None else None
        base = cls(fps=rate, meter=tempo_map.meter, meters=tempo_map.meters, start=start, end=end)
        limit = horizon if horizon is not None else end if end is not None else _DEFAULT_HORIZON
        clock = replace(base, beats=base._static_beats(tempo_map, limit))
        if clock.beats:
            clock = replace(clock, downbeats=clock._bar_starts)
        resolved: dict[str, int] = {}
        for name, time in (cues or {}).items():
            if time.anchor is not None and time.anchor.kind == "cue":
                raise TimeError(
                    f"cue {name!r} is {format_time(time)!r}, which refers to another cue; cues may not reference "
                    "other cues, give each one a time on the clock"
                )
            resolved[name] = clock.resolve(time)
        return replace(clock, cues=resolved)

    def _static_beats(self, tempo: TempoMap, limit: int) -> tuple[int, ...]:
        if tempo.external or tempo.bpm is None:
            return ()
        if not tempo.offset.is_concrete:
            raise TimeError(
                f"tempo offset {format_time(tempo.offset)!r} must be a concrete time such as t('0.1s')"
            )
        changes = sorted(tempo.changes, key=lambda change: change.beat)
        settled = Fraction(changes[-1].beat if changes else 0)
        if self.meters:
            last_meter_bar = max(change.bar for change in self.meters)
            settled = max(settled, self.bar_start_beat(Fraction(last_meter_bar)))
        settled_bar = math.floor(self.bar_index_at_beat(settled))
        minimum = math.ceil(self.bar_start_beat(Fraction(settled_bar + 3)))
        position = Fraction(tempo.offset.ticks)
        duration = _beat_ticks(tempo.bpm)
        points: list[int] = []
        pending = 0
        index = 0
        while True:
            points.append(_round(position))
            if index >= minimum and position >= limit:
                break
            while pending < len(changes) and changes[pending].beat <= index:
                duration = _beat_ticks(changes[pending].bpm)
                pending += 1
            position += duration
            index += 1
        return tuple(points)

    def resolve(self, time: Time) -> int:
        return _round(self._position(time))

    def resolve_set(self, times: TimeSet, *, seed: int | None = None) -> list[int]:
        offset = _duration_offset(times.offset, "TimeSet offset")
        if times.every is not None:
            positions = self._periodic_positions(times)
        elif times.stream is not None:
            if times.times:
                raise TimeError(
                    f"a TimeSet is either a stream slice or an explicit list; this one has stream {times.stream!r} "
                    "and explicit times"
                )
            positions = self._stream_positions(times)
        else:
            positions = [self._position(time) for time in times.times]
        resolved = sorted(_round(self._apply(position, offset)) for position in positions)
        if times.window is not None:
            low, high = self.resolve_span(times.window)
            resolved = [value for value in resolved if low <= value < high]
        return self._randomize(times, resolved, seed)

    def _periodic_positions(self, times: TimeSet) -> list[Fraction]:
        if times.window is None or times.every is None:
            raise TimeError("a periodic TimeSet needs every= and a window")
        low, high = self.resolve_span(times.window)
        positions: list[Fraction] = []
        position = Fraction(low)
        while position < high:
            positions.append(position)
            following = self._apply(position, times.every)
            if following <= position:
                raise TimeError(f"TimeSet every {format_time(times.every)!r} does not advance at {_round(position)}")
            position = following
        return positions

    def _randomize(self, times: TimeSet, resolved: list[int], seed: int | None) -> list[int]:
        if times.jitter is None and times.pick is None:
            return resolved
        chosen = times.seed if times.seed is not None else seed
        if chosen is None:
            raise TimeError("a TimeSet with jitter or pick needs a seed: set seed= or resolve it with the node seed")
        rng = random.Random(chosen)
        if times.jitter is not None:
            jitter = times.jitter
            moved: list[int] = []
            for value in resolved:
                width = _round(self._apply(Fraction(value), jitter)) - value
                moved.append(value + rng.randint(-width, width))
            resolved = sorted(moved)
        if times.pick is not None and times.pick < len(resolved):
            resolved = sorted(rng.sample(resolved, times.pick))
        return resolved

    def resolve_span(self, span: Span) -> tuple[int, int]:
        start = self.resolve(span.start)
        end = self.resolve(span.end)
        if end < start:
            raise TimeError(
                f"span {format_time(span.start)!r} to {format_time(span.end)!r} ends before it starts "
                f"({end} < {start} ticks); swap the times or fix the anchors"
            )
        return start, end

    def ticks_per_frame(self) -> int:
        if self.fps is None:
            raise TimeError("frame times (f and f:N) need a frame rate, and the clock has no fps; set fps on it")
        return ticks_per_frame(self.fps)

    def frame_of(self, ticks: int) -> int:
        return _round(Fraction(ticks, self.ticks_per_frame()))

    def beat_time(self, index: Fraction) -> int:
        return _round(self._beat_position(Fraction(index)))

    def beat_index_at(self, ticks: int) -> Fraction:
        return _grid_index(self.beats, Fraction(ticks), "beat")

    def bar_start_beat(self, bar: Fraction) -> Fraction:
        bar = Fraction(bar)
        whole = math.floor(bar)
        if whole < 0:
            return bar * self._beats_in_bar(0)
        total = Fraction(0)
        current = 0
        numerator = self.meter.numerator
        for change in self._meter_changes:
            if change.bar >= whole:
                break
            total += (change.bar - current) * numerator
            current = change.bar
            numerator = change.meter.numerator
        total += (whole - current) * numerator
        return total + (bar - whole) * self._beats_in_bar(whole)

    def bar_index_at_beat(self, beat: Fraction) -> Fraction:
        beat = Fraction(beat)
        if beat < 0:
            return beat / self._beats_in_bar(0)
        current_bar = 0
        current_beat = Fraction(0)
        numerator = self.meter.numerator
        for change in self._meter_changes:
            segment = (change.bar - current_bar) * numerator
            if beat < current_beat + segment:
                break
            current_beat += segment
            current_bar = change.bar
            numerator = change.meter.numerator
        return current_bar + (beat - current_beat) / numerator

    @cached_property
    def _meter_changes(self) -> tuple[MeterChange, ...]:
        return tuple(sorted(self.meters, key=lambda change: change.bar))

    def _beats_in_bar(self, bar: int) -> int:
        numerator = self.meter.numerator
        for change in self._meter_changes:
            if change.bar > bar:
                break
            numerator = change.meter.numerator
        return numerator

    @cached_property
    def _bar_starts(self) -> tuple[int, ...]:
        _check_grid(self.beats, "beat")
        last_beat = len(self.beats) - 1
        starts: list[int] = []
        bar = 0
        while (beat := self.bar_start_beat(Fraction(bar))) <= last_beat:
            starts.append(_round(self._beat_position(beat)))
            bar += 1
        return tuple(starts)

    def _beat_position(self, index: Fraction) -> Fraction:
        return _grid_at(self.beats, index, "beat")

    def _position(self, time: Time) -> Fraction:
        position = self._anchor_position(time.anchor) if time.anchor is not None else Fraction(0)
        return self._apply(position, time)

    def _apply(self, position: Fraction, time: Time) -> Fraction:
        if time.bars != 0:
            bar = self.bar_index_at_beat(_grid_index(self.beats, position, "beat")) + time.bars
            position = self._beat_position(self.bar_start_beat(bar))
        if time.beats != 0:
            position = self._beat_position(_grid_index(self.beats, position, "beat") + time.beats)
        if time.frames != 0:
            position += _round(time.frames * self.ticks_per_frame())
        return position + time.ticks

    def _anchor_position(self, anchor: Anchor) -> Fraction:
        match anchor.kind:
            case "start":
                return Fraction(self.start)
            case "end":
                if self.end is None:
                    raise TimeError("'end' needs a known end, and this clock has none; give it a duration or a window")
                return Fraction(self.end)
            case "frame":
                return _need(anchor.index, anchor) * self.ticks_per_frame()
            case "beat":
                return self._beat_position(_need(anchor.index, anchor))
            case "downbeat":
                index = _need(anchor.index, anchor)
                if self.downbeats:
                    return _grid_at(self.downbeats, index, "downbeat")
                return self._beat_position(self.bar_start_beat(self._non_negative(index, "bar")))
            case "bar":
                index = self._non_negative(_need(anchor.index, anchor), "bar")
                return self._beat_position(self.bar_start_beat(index))
            case "cue":
                name = _need(anchor.name, anchor)
                if name not in self.cues:
                    raise TimeError(f"unknown cue {name!r}; known cues: {_known(self.cues)}")
                return Fraction(self.cues[name])
            case "marker":
                return Fraction(self._marker(_need(anchor.name, anchor), anchor.nth or 0))
            case "word":
                return Fraction(self._word(anchor))

    @staticmethod
    def _non_negative(index: Fraction, what: str) -> Fraction:
        if index < 0:
            raise TimeError(f"{what} index {_decimal(index)} is negative; {what} indices start at 0")
        return index

    def _marker(self, stream: str, nth: int) -> int:
        if stream not in self.markers:
            raise TimeError(f"unknown marker stream {stream!r}; known streams: {_known(self.markers)}")
        points = self.markers[stream]
        if not 0 <= nth < len(points):
            raise TimeError(
                f"marker:{stream}#{nth} is out of range; stream {stream!r} has {len(points)} marker(s), "
                f"indices 0 to {len(points) - 1}"
            )
        return points[nth]

    def _word(self, anchor: Anchor) -> int:
        source = anchor.source if anchor.source is not None else self.default_words
        if source is None:
            raise TimeError(
                f"{_format_anchor(anchor)!r} names no transcript and the clock has no default transcript; "
                f"write word:SOURCE:... with one of: {_known(self.words)}"
            )
        if source not in self.words:
            raise TimeError(f"unknown transcript {source!r}; known transcripts: {_known(self.words)}")
        words = self.words[source]
        if anchor.text is None:
            index = _need(anchor.index, anchor)
            if index.denominator != 1 or not 0 <= index < len(words):
                raise TimeError(
                    f"word #{_decimal(index)} is out of range; transcript {source!r} has {len(words)} word(s)"
                )
            return words[index.numerator].ticks
        wanted = _fold(anchor.text)
        matches = [word.ticks for word in words if _fold(word.text) == wanted]
        nth = anchor.nth or 0
        if nth >= len(matches):
            raise TimeError(
                f"word {anchor.text!r}#{nth} not found: transcript {source!r} contains it {len(matches)} time(s)"
            )
        return matches[nth]

    def _stream_positions(self, times: TimeSet) -> list[Fraction]:
        stream = times.stream
        if stream == "beat":
            _check_grid(self.beats, "beat")
            grid: Sequence[int] = self.beats
        elif stream == "bar":
            grid = self._bar_starts
        elif stream == "downbeat":
            grid = self.downbeats or self._bar_starts
        elif stream is not None and stream in self.markers:
            grid = self.markers[stream]
        else:
            raise TimeError(
                f"unknown time stream {stream!r}; use 'beat', 'downbeat', 'bar' or a marker stream "
                f"({_known(self.markers)})"
            )
        positions: list[Fraction] = []
        index = times.start
        while index < len(grid) and (times.stop is None or index < times.stop):
            value = grid[index]
            if self.end is not None and value >= self.end:
                break
            positions.append(Fraction(value))
            index += times.step
        return positions


def _check_name(value: object, what: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise TimeError(
            f"{what} {value!r} is not a valid name; names start with a letter or '_' and may contain digits, "
            "'_', '.' and '-' (followed by a letter), e.g. 'logo-intro'"
        )
    return value


def _check_index(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int, got {type(value).__name__} {value!r}")
    if value < 0:
        raise TimeError(f"{what} {value} is negative; indices start at 0")
    return value


def _number(value: object, what: str) -> Fraction:
    if isinstance(value, bool):
        raise TypeError(f"{what} must be a number or a decimal string, got bool {value!r}")
    if isinstance(value, int | Fraction):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TimeError(f"{what} {value!r} is not a finite number")
        return Fraction(repr(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TimeError(f"{what} {value!r} is not a finite number")
        return Fraction(value)
    if isinstance(value, str):
        if _SIGNED_DECIMAL_RE.fullmatch(value) is None:
            raise TimeError(f"{what} {value!r} is not a decimal number; write e.g. '0.3' or '-2.5'")
        return Fraction(value)
    raise TypeError(f"{what} must be a number or a decimal string, got {type(value).__name__} {value!r}")


def _anchor_index(value: object, what: str) -> Fraction:
    index = _terminating(_number(value, what), what)
    if index < 0:
        raise TimeError(f"{what} {_decimal(index)} is negative; indices start at 0")
    return index


class BeatGrid:
    def __init__(self, stream: str) -> None:
        self.stream = stream

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            return self.every(
                1 if key.step is None else key.step,
                start=0 if key.start is None else key.start,
                stop=key.stop,
            )
        index = _check_index(key, f"t.beats({self.stream!r}) index")
        kind = _GRID_KINDS.get(self.stream)
        if kind is not None:
            return Time(anchor=Anchor(kind=kind, index=Fraction(index)))
        return Time(anchor=Anchor(kind="marker", name=self.stream, nth=index))

    def every(self, step: int, *, start: int = 0, stop: int | None = None) -> TimeSet:
        what = f"t.beats({self.stream!r})"
        step = _check_index(step, f"{what} step")
        if step < 1:
            raise TimeError(f"{what} step must be at least 1, got {step}")
        start = _check_index(start, f"{what} start")
        stop = None if stop is None else _check_index(stop, f"{what} stop")
        return TimeSet(stream=self.stream, start=start, stop=stop, step=step)

    def within(self, span: Span) -> TimeSet:
        return self.all().within(span)

    def all(self) -> TimeSet:
        return TimeSet(stream=self.stream)


class TimeFactory:
    def __call__(self, text: str) -> Time:
        if isinstance(text, str):
            return parse_time(text)
        if _is_number(text):
            raise TypeError(f"t() takes a time string, got the number {text!r}; bare numbers are not times, write t('1.5s')")
        raise TypeError(f"t() takes a time string such as t('1.5s'), got {type(text).__name__} {text!r}")

    @property
    def start(self) -> Time:
        return Time(anchor=Anchor(kind="start"))

    @property
    def end(self) -> Time:
        return Time(anchor=Anchor(kind="end"))

    def span(self, start: Time, end: Time) -> Span:
        return Span(start=ensure_time(start, "t.span start"), end=ensure_time(end, "t.span end"))

    def set(self, *times: Time) -> TimeSet:
        return TimeSet(times=tuple(ensure_time(time, "t.set") for time in times))

    def beats(self, stream: str = "beat") -> BeatGrid:
        return BeatGrid(_check_name(stream, "beat stream"))

    def every(self, period: Time, span: Span) -> TimeSet:
        if not isinstance(span, Span):
            raise TypeError(f"t.every() takes the span to fill, such as t.span(t('0s'), t('15s')), got {span!r}")
        return TimeSet(every=ensure_time(period, "t.every period"), window=span)

    def secs(self, value: float | str | Fraction) -> Time:
        amount = _number(value, "t.secs")
        return Time(ticks=_ticks_of(amount, TICKS_PER_SECOND, f"{value!r} s", "t.secs"))

    def ms(self, value: float | str | Fraction) -> Time:
        amount = _number(value, "t.ms")
        return Time(ticks=_ticks_of(amount, TICKS_PER_MS, f"{value!r} ms", "t.ms"))

    def ticks(self, value: int) -> Time:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"t.ticks takes an int, got {type(value).__name__} {value!r}")
        return Time(ticks=value)

    def frames(self, value: float | str | Fraction) -> Time:
        return Time(frames=_terminating(_number(value, "t.frames"), "t.frames"))

    def frame(self, index: int) -> Time:
        return Time(anchor=Anchor(kind="frame", index=Fraction(_check_index(index, "t.frame index"))))

    def beat(self, index: float | str | Fraction) -> Time:
        return Time(anchor=Anchor(kind="beat", index=_anchor_index(index, "t.beat index")))

    def downbeat(self, index: float | str | Fraction) -> Time:
        return Time(anchor=Anchor(kind="downbeat", index=_anchor_index(index, "t.downbeat index")))

    def bar(self, index: float | str | Fraction) -> Time:
        return Time(anchor=Anchor(kind="bar", index=_anchor_index(index, "t.bar index")))

    def cue(self, name: str) -> Time:
        return Time(anchor=Anchor(kind="cue", name=_check_name(name, "cue")))

    def marker(self, stream: str, nth: int = 0) -> Time:
        return Time(
            anchor=Anchor(
                kind="marker",
                name=_check_name(stream, "marker stream"),
                nth=_check_index(nth, "t.marker nth"),
            )
        )

    def word(
        self, text: str | None = None, *, index: int | None = None, source: str | None = None, nth: int = 0
    ) -> Time:
        if (text is None) == (index is None):
            raise TimeError("t.word takes exactly one of text or index, e.g. t.word('EksenGOLD') or t.word(index=12)")
        checked_source = None if source is None else _check_name(source, "transcript")
        occurrence = _check_index(nth, "t.word nth")
        if text is None:
            if occurrence != 0:
                raise TimeError("t.word nth only applies to a text match; t.word(index=...) already picks one word")
            position = _check_index(index, "t.word index")
            return Time(anchor=Anchor(kind="word", index=Fraction(position), source=checked_source))
        if not isinstance(text, str):
            raise TypeError(f"t.word text must be a string, got {type(text).__name__} {text!r}")
        return Time(anchor=Anchor(kind="word", text=text, source=checked_source, nth=occurrence))

    def parse(self, value: Time | str) -> Time:
        return coerce_time(value)


t = TimeFactory()


def needs_of(values: Sequence[Time | Span | TimeSet]) -> TimeNeeds:
    needs = TimeNeeds()
    for value in values:
        needs |= value.needs()
    return needs


__all__ = [
    "CLOCK_NAME_PATTERN",
    "FPS_PATTERN",
    "MAX_BPM",
    "STREAM_REFERENCE",
    "TICKS_PER_MS",
    "TICKS_PER_SECOND",
    "TIME_PATTERN",
    "WORDS_REFERENCE",
    "ZERO",
    "Anchor",
    "AnchorKind",
    "BeatGrid",
    "Fps",
    "Meter",
    "MeterChange",
    "OffsetUnit",
    "Span",
    "StreamName",
    "StreamReference",
    "TempoChange",
    "TempoMap",
    "Time",
    "TimeContext",
    "TimeError",
    "TimeFactory",
    "TimeNeeds",
    "TimeSet",
    "Word",
    "WordSource",
    "WordsReference",
    "coerce_time",
    "ensure_duration",
    "ensure_time",
    "field_needs",
    "format_fps",
    "format_time",
    "needs_of",
    "optional_time",
    "parse_fps",
    "parse_time",
    "span_reversed",
    "stream_needs",
    "t",
    "ticks_per_frame",
    "words_needs",
]
