from __future__ import annotations

import re
from fractions import Fraction

import pytest

from eks_harness.ir.time import (
    TICKS_PER_MS,
    TICKS_PER_SECOND,
    TIME_PATTERN,
    ZERO,
    Anchor,
    Time,
    TimeError,
    format_time,
    parse_time,
    t,
)

# Every example row of contracts-m0.md section 2.3 (input, canonical form).
CONTRACT_EXAMPLES = [
    ("1.5s", "1.5s"),
    ("300ms", "0.3s"),
    ("f:120", "f:120"),
    ("beat:8 + 1b", "beat:8+1b"),
    ("cue:logo - 2f", "cue:logo-2f"),
    ("word:'EksenGOLD'", "word:'EksenGOLD'"),
    ("marker:intro#2", "marker:intro#2"),
    ("-0.5s+1b", "1b-0.5s"),
    ("end-1bar", "end-1bar"),
    ("0ms", "0s"),
]

# Inputs and their canonical spelling, used for the round-trip and idempotence checks.
ROUND_TRIP = [
    ("-2s", "-2s"),
    ("2.5f", "2.5f"),
    ("beat:2.5", "beat:2.5"),
    ("downbeat:3", "downbeat:3"),
    ("bar:0", "bar:0"),
    ("f:0", "f:0"),
    ("cue:logo-intro", "cue:logo-intro"),
    ("cue:logo-2f", "cue:logo-2f"),
    ("word:'EksenGOLD'", "word:'EksenGOLD'"),
    ("word:vo:'Merhaba'#1", "word:vo:'Merhaba'#1"),
    ("word:#12", "word:#12"),
    ("word:vo:#3", "word:vo:#3"),
    ("word:'it\\'s'", "word:'it\\'s'"),
    ("word:'a\\\\b'", "word:'a\\\\b'"),
    ("word:'Merhaba'#0", "word:'Merhaba'"),
    ("marker:song.drop#0", "marker:song.drop"),
    ("marker:song.drop#4", "marker:song.drop#4"),
    ("end-1bar", "end-1bar"),
    ("1bar+2b-3f+0.25s", "1bar+2b-3f+0.25s"),
    ("1tk", "1tk"),
    ("0.25s+1tk", "176400001tk"),
    ("start", "start"),
    ("end", "end"),
    ("start+1s", "start+1s"),
    ("  start + 1s  ", "start+1s"),
    ("\tbeat:8\t-\t1b", "beat:8-1b"),
    ("1500ms", "1.5s"),
    ("0.00001s", "0.00001s"),
    ("1.23ms", "0.00123s"),
    ("2tk", "2tk"),
    ("705600000tk", "1s"),
    ("1s-1s", "0s"),
    ("beat:8-0b", "beat:8"),
    ("-1b-1f", "-1b-1f"),
    ("+1s", "1s"),
    ("- 1s", "-1s"),
    ("0.5bar", "0.5bar"),
    ("1f+1b+1bar", "1bar+1b+1f"),
    ("1s+1b", "1b+1s"),
    ("beat:08.50", "beat:8.5"),
    ("bar:1.25+0.5bar", "bar:1.25+0.5bar"),
    ("cue:a.b_c-d", "cue:a.b_c-d"),
    ("cue:_x", "cue:_x"),
    ("marker:kick#10-2f", "marker:kick#10-2f"),
    ("word:vo:'Merhaba dünya'", "word:vo:'Merhaba dünya'"),
    ("1.10s", "1.1s"),
    ("10s", "10s"),
    ("120f", "120f"),
]

REJECTED = [
    "",
    "   ",
    "1.5",
    "1 s",
    " 2 s",
    "2x",
    "1.s",
    ".5s",
    "beat:",
    "beat",
    "cue:",
    "cue:-x",
    "marker:",
    "marker:x#",
    "word:",
    "word:x",
    "word:'open",
    "word:'bad\\escape'",
    "word:vo'x'",
    "1s+beat:3",
    "beat:1+cue:x",
    "-beat:1",
    "1s 2s",
    "1s+",
    "0.0000001s",
    "1.234ms",
    "1.5tk",
    "startx",
    "ending",
    "f:1.5",
    "beat:-1",
    "1e3s",
    "1bars",
    "cue:logo-2nd",
    "1s\n",
    "١s",
]


@pytest.mark.parametrize(("text", "canonical"), CONTRACT_EXAMPLES)
def test_contract_examples(text: str, canonical: str) -> None:
    assert format_time(parse_time(text)) == canonical


@pytest.mark.parametrize(("text", "canonical"), ROUND_TRIP)
def test_canonical_form(text: str, canonical: str) -> None:
    assert format_time(parse_time(text)) == canonical


@pytest.mark.parametrize(("text", "canonical"), ROUND_TRIP + CONTRACT_EXAMPLES)
def test_round_trip_and_idempotence(text: str, canonical: str) -> None:
    parsed = parse_time(text)
    formatted = format_time(parsed)
    assert parse_time(formatted) == parsed
    assert format_time(parse_time(formatted)) == formatted
    assert formatted == canonical


def test_round_trip_count_is_large_enough() -> None:
    assert len(ROUND_TRIP) >= 30


@pytest.mark.parametrize("text", REJECTED)
def test_rejections(text: str) -> None:
    with pytest.raises(TimeError):
        parse_time(text)


def test_rejection_messages_explain() -> None:
    with pytest.raises(TimeError, match="no unit"):
        parse_time("1.5")
    with pytest.raises(TimeError, match="anchor can only come first"):
        parse_time("1s+beat:3")
    with pytest.raises(TimeError, match="finer than one tick"):
        parse_time("0.0000001s")
    with pytest.raises(TimeError, match="finer than one tick"):
        parse_time("1.234ms")
    with pytest.raises(TimeError, match="empty time"):
        parse_time("")
    with pytest.raises(TimeError, match="unknown anchor"):
        parse_time("ending")


def test_structure_of_parsed_values() -> None:
    assert parse_time("1.5s") == Time(ticks=1_058_400_000)
    assert parse_time("300ms") == Time(ticks=300 * TICKS_PER_MS)
    assert parse_time("f:120") == Time(anchor=Anchor(kind="frame", index=Fraction(120)))
    assert parse_time("beat:2.5") == Time(anchor=Anchor(kind="beat", index=Fraction(5, 2)))
    assert parse_time("cue:logo-intro") == Time(anchor=Anchor(kind="cue", name="logo-intro"))
    assert parse_time("cue:logo-2f") == Time(anchor=Anchor(kind="cue", name="logo"), frames=Fraction(-2))
    assert parse_time("marker:song.drop") == Time(anchor=Anchor(kind="marker", name="song.drop", nth=0))
    assert parse_time("marker:song.drop#0") == parse_time("marker:song.drop")
    assert parse_time("word:'it\\'s'") == Time(anchor=Anchor(kind="word", text="it's", nth=0))
    assert parse_time("word:vo:'Merhaba'#1") == Time(
        anchor=Anchor(kind="word", text="Merhaba", source="vo", nth=1)
    )
    assert parse_time("word:#12") == Time(anchor=Anchor(kind="word", index=Fraction(12)))
    assert parse_time("end-1bar") == Time(anchor=Anchor(kind="end"), bars=Fraction(-1))
    assert parse_time("1bar+2b-3f+0.25s") == Time(
        bars=Fraction(1), beats=Fraction(2), frames=Fraction(-3), ticks=TICKS_PER_SECOND // 4
    )
    assert parse_time("0ms") == ZERO


def test_seconds_and_ticks_canonical_spelling() -> None:
    assert format_time(t.ticks(1)) == "1tk"
    assert format_time(t.ticks(7056)) == "0.00001s"
    assert format_time(t.ticks(7057)) == "7057tk"
    assert format_time(t.ticks(-7057)) == "-7057tk"
    assert format_time(parse_time("300ms")) == format_time(parse_time("0.3s")) == "0.3s"
    assert parse_time("300ms") == parse_time("0.3s")


def test_signs_of_first_token() -> None:
    assert format_time(parse_time("-1b+2s")) == "-1b+2s"
    assert format_time(parse_time("cue:x+1b")) == "cue:x+1b"
    assert format_time(parse_time("cue:x-1b")) == "cue:x-1b"


def test_word_text_escaping_round_trips() -> None:
    for text in ["it's", "back\\slash", "both '\\", "", "Kadıköy"]:
        time = t.word(text)
        assert parse_time(format_time(time)) == time


@pytest.mark.parametrize("canonical", [canonical for _, canonical in ROUND_TRIP + CONTRACT_EXAMPLES])
def test_time_pattern_accepts_canonical_output(canonical: str) -> None:
    assert re.fullmatch(TIME_PATTERN, canonical)


def test_time_pattern_accepts_contract_inputs() -> None:
    for text, _ in CONTRACT_EXAMPLES:
        assert re.fullmatch(TIME_PATTERN, text), text


@pytest.mark.parametrize("text", ["1.5", "1 s", "2x", "1s+beat:3", "beat:1+cue:x", "", "word:'bad\\e'"])
def test_time_pattern_rejects(text: str) -> None:
    assert re.fullmatch(TIME_PATTERN, text) is None


def test_determinism() -> None:
    first = parse_time("beat:8 + 1b - 2f")
    second = parse_time("beat:8 + 1b - 2f")
    assert first == second
    assert format_time(first).encode() == format_time(second).encode()
    assert t("300ms") == t.ms(300) == t.secs("0.3") == t.secs(0.3)
    assert format_time(t("300ms")) == format_time(t.ms(300)) == format_time(t.secs("0.3")) == "0.3s"


def test_str_and_repr() -> None:
    assert str(t("beat:8 + 1b")) == "beat:8+1b"
    assert repr(t("beat:8 + 1b")) == "t('beat:8+1b')"


def test_parse_time_rejects_non_strings() -> None:
    with pytest.raises(TimeError):
        parse_time(1.5)  # pyright: ignore[reportArgumentType]


def test_direct_construction_validates_components() -> None:
    with pytest.raises(TimeError, match="terminating"):
        Time(frames=Fraction(1, 3))
    with pytest.raises(TimeError):
        Time(ticks=Fraction(1, 2))  # pyright: ignore[reportArgumentType]
    assert Time(bars=1) == Time(bars=Fraction(1))  # pyright: ignore[reportArgumentType]
    assert isinstance(Time(bars=1).bars, Fraction)  # pyright: ignore[reportArgumentType]


# Anchors built directly (not through the parser) must still format to text that parses back to them.
DIRECT_ANCHORS = [
    Anchor(kind="start"),
    Anchor(kind="end"),
    Anchor(kind="frame", index=Fraction(120)),
    Anchor(kind="frame", index=7),  # pyright: ignore[reportArgumentType]
    Anchor(kind="beat", index=Fraction(5, 2)),
    Anchor(kind="downbeat", index=Fraction(3)),
    Anchor(kind="bar", index=Fraction(0)),
    Anchor(kind="cue", name="logo-intro"),
    Anchor(kind="marker", name="song.drop"),
    Anchor(kind="marker", name="kick", nth=3),
    Anchor(kind="word", text="EksenGOLD"),
    Anchor(kind="word", text="it's", source="vo", nth=2),
    Anchor(kind="word", index=Fraction(12)),
    Anchor(kind="word", index=Fraction(4), source="vo"),
]


@pytest.mark.parametrize("anchor", DIRECT_ANCHORS, ids=repr)
def test_direct_anchors_round_trip(anchor: Anchor) -> None:
    for time in (Time(anchor=anchor), Time(anchor=anchor, beats=Fraction(1), ticks=-TICKS_PER_SECOND)):
        text = format_time(time)
        assert parse_time(text) == time
        assert hash(parse_time(text)) == hash(time)
        assert re.fullmatch(TIME_PATTERN, text)


def test_direct_anchor_defaults_are_normalised() -> None:
    assert Anchor(kind="marker", name="x").nth == 0
    assert Time(anchor=Anchor(kind="marker", name="x")) == t("marker:x")
    assert hash(Time(anchor=Anchor(kind="marker", name="x"))) == hash(t("marker:x"))
    assert Anchor(kind="word", text="a").nth == 0
    assert Time(anchor=Anchor(kind="word", text="a")) == t("word:'a'")
    assert Anchor(kind="word", index=Fraction(3)).nth is None
    assert isinstance(Anchor(kind="frame", index=7).index, Fraction)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("anchor_fields", "message"),
    [
        ({"kind": "frame", "index": Fraction(1, 2)}, "whole number"),
        ({"kind": "frame"}, "needs an index"),
        ({"kind": "frame", "index": Fraction(-1)}, "negative"),
        ({"kind": "beat", "index": Fraction(-1)}, "negative"),
        ({"kind": "downbeat", "index": Fraction(-1, 2)}, "negative"),
        ({"kind": "bar", "index": Fraction(1, 3)}, "terminating"),
        ({"kind": "beat"}, "needs an index"),
        ({"kind": "beat", "index": 1.5}, "Fraction"),
        ({"kind": "beat", "index": True}, "Fraction"),
        ({"kind": "beat", "index": Fraction(1), "name": "x"}, "name"),
        ({"kind": "start", "name": "zzz"}, "name"),
        ({"kind": "end", "index": Fraction(1)}, "index"),
        ({"kind": "cue"}, "not a valid name"),
        ({"kind": "cue", "name": "1 bad"}, "not a valid name"),
        ({"kind": "cue", "name": "logo", "nth": 1}, "nth"),
        ({"kind": "marker", "name": "a b"}, "not a valid name"),
        ({"kind": "marker", "name": "kick", "nth": -1}, "negative"),
        ({"kind": "marker", "name": "kick", "nth": True}, "int"),
        ({"kind": "word"}, "exactly one"),
        ({"kind": "word", "text": "x", "index": Fraction(3)}, "exactly one"),
        ({"kind": "word", "index": Fraction(3), "nth": 1}, "no nth"),
        ({"kind": "word", "index": Fraction(5, 2)}, "whole number"),
        ({"kind": "word", "index": Fraction(-1)}, "negative"),
        ({"kind": "word", "text": "x", "source": "bad source"}, "not a valid name"),
        ({"kind": "word", "text": 5}, "string"),
        ({"kind": "word", "text": "x", "nth": -2}, "negative"),
        ({"kind": "nope"}, "unknown anchor kind"),
    ],
)
def test_invalid_anchor_shapes(anchor_fields: dict[str, object], message: str) -> None:
    with pytest.raises(TimeError, match=message):
        Anchor(**anchor_fields)  # pyright: ignore[reportArgumentType]


def test_time_rejects_a_non_anchor() -> None:
    with pytest.raises(TimeError, match="Anchor"):
        Time(anchor="beat:1")  # pyright: ignore[reportArgumentType]
