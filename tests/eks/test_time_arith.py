from __future__ import annotations

from fractions import Fraction

import pytest

from eks_harness.ir.time import (
    TICKS_PER_SECOND,
    BeatGrid,
    Span,
    Time,
    TimeError,
    TimeNeeds,
    TimeSet,
    coerce_time,
    ensure_time,
    needs_of,
    t,
)


def test_anchor_plus_duration_both_orders() -> None:
    assert t("beat:8") + t("1b") == t("beat:8+1b")
    assert t("1b") + t("beat:8") == t("beat:8+1b")
    assert t("cue:logo") - t("2f") == t("cue:logo-2f")


def test_two_anchors_cannot_be_added() -> None:
    with pytest.raises(TimeError, match="at most one"):
        _ = t("cue:a") + t("cue:b")


def test_subtracting_an_anchor_raises() -> None:
    with pytest.raises(TimeError, match="only durations"):
        _ = t("1s") - t("beat:1")


def test_negation() -> None:
    assert -t("1b-2s") == t("-1b+2s")
    with pytest.raises(TimeError):
        _ = -t("beat:1")


def test_multiplication() -> None:
    assert t("1b") * 4 == t("4b")
    assert 3 * t("1s") == t("3s")
    assert t("1bar+1f") * -2 == t("-2bar-2f")
    with pytest.raises(TypeError):
        _ = t("1s") * 1.5  # pyright: ignore[reportOperatorIssue]
    with pytest.raises(TypeError):
        _ = 1.5 * t("1s")  # pyright: ignore[reportOperatorIssue]
    with pytest.raises(TypeError):
        _ = t("1s") * True
    with pytest.raises(TimeError):
        _ = t("beat:1") * 2


def test_addition_with_other_types_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        _ = t("1s") + 1  # pyright: ignore[reportOperatorIssue]
    with pytest.raises(TypeError):
        _ = t("1s") - "1s"  # pyright: ignore[reportOperatorIssue]


def test_ordering_of_concrete_times() -> None:
    assert t("1s") < t("2s")
    assert t("2s") > t("1s")
    assert t("1s") <= t("1000ms")
    assert t("1s") >= t("1000ms")
    assert sorted([t("3s"), t("1tk"), t("-1s")]) == [t("-1s"), t("1tk"), t("3s")]


def test_ordering_of_symbolic_times_raises() -> None:
    with pytest.raises(TimeError, match="concrete"):
        _ = t("beat:1") < t("2s")
    with pytest.raises(TimeError):
        _ = t("1f") >= t("2s")
    with pytest.raises(TimeError):
        _ = t("1s") <= t("1b")


def test_equality_and_hash() -> None:
    assert t("300ms") == t("0.3s")
    assert hash(t("300ms")) == hash(t("0.3s"))
    assert hash(t("beat:8+1b")) == hash(t("1b") + t("beat:8"))
    assert len({t("1s"), t("1000ms"), t("1s")}) == 1
    assert t("1s") != t("1b")


def test_ensure_time() -> None:
    value = t("1s")
    assert ensure_time(value, "hold") is value
    with pytest.raises(TypeError, match="hold") as number:
        ensure_time(1.5, "hold")
    assert "bare numbers" in str(number.value)
    with pytest.raises(TypeError, match="at") as text:
        ensure_time("1s", "at")
    assert "pass t('1s')" in str(text.value)
    with pytest.raises(TypeError, match="timeout"):
        ensure_time(None, "timeout")
    with pytest.raises(TypeError, match="bare numbers"):
        ensure_time(2, "delay")


def test_coerce_time() -> None:
    assert coerce_time("1s") == t("1s")
    value = t("beat:2")
    assert coerce_time(value) is value
    assert t.parse("cue:x") == t("cue:x")
    with pytest.raises(TimeError, match="bare numbers"):
        coerce_time(1.5)
    with pytest.raises(TimeError):
        coerce_time(None)


def test_t_call_rejects_numbers() -> None:
    with pytest.raises(TypeError, match="bare numbers"):
        t(1.5)  # pyright: ignore[reportArgumentType]


def test_is_concrete() -> None:
    assert t("1s").is_concrete
    assert t("-3tk").is_concrete
    assert t("0s").is_concrete
    assert not t("1f").is_concrete
    assert not t("1b").is_concrete
    assert not t("1bar").is_concrete
    assert not t("start").is_concrete
    assert not t("cue:x").is_concrete
    assert not t("f:0").is_concrete


def test_needs() -> None:
    assert t("2f").needs().fps
    assert t("f:3").needs().fps
    assert not t("2s").needs().fps
    assert t("bar:1").needs().tempo
    assert t("1b").needs().tempo
    assert t("1bar").needs().tempo
    assert t("downbeat:1").needs().tempo
    assert t("cue:x").needs().cues == {"x"}
    assert t("end").needs().window
    assert not t("start").needs().window
    assert t("marker:kick#2").needs().markers == {"kick"}
    assert t("word:'a'").needs().default_words
    assert t("word:'a'").needs().words == frozenset()
    assert t("word:vo:'a'").needs().words == {"vo"}
    assert not t("word:vo:'a'").needs().default_words
    assert t("word:vo:#2").needs().words == {"vo"}
    assert t("1s").needs().empty
    assert t("start+1s").needs().empty


def test_timeset_needs() -> None:
    assert t.beats("kick")[0:4].needs().markers == {"kick"}
    assert not t.beats("kick")[0:4].needs().tempo
    assert t.beats("downbeat")[0:8].needs().tempo
    assert t.beats("downbeat")[0:8].needs().markers == frozenset()
    assert t.beats()[0:8].needs().tempo
    assert t.beats("bar").all().needs().tempo
    explicit = t.set(t("cue:a"), t("2f"))
    assert explicit.needs() == TimeNeeds(fps=True, cues=frozenset({"a"}))
    shifted = t.beats("kick").all() + t("1f")
    assert shifted.needs() == TimeNeeds(fps=True, markers=frozenset({"kick"}))
    windowed = t.beats("kick").within(t.span(t("0s"), t("end")))
    assert windowed.needs() == TimeNeeds(window=True, markers=frozenset({"kick"}))


def test_span_needs_and_needs_of() -> None:
    span = t.span(t("cue:a"), t("end"))
    assert span.needs() == TimeNeeds(window=True, cues=frozenset({"a"}))
    combined = needs_of([t("2f"), span, t.beats("kick")[0:2], t("word:vo:'x'")])
    assert combined == TimeNeeds(
        fps=True,
        window=True,
        cues=frozenset({"a"}),
        markers=frozenset({"kick"}),
        words=frozenset({"vo"}),
    )
    assert needs_of([]).empty


def test_time_needs_union_and_empty() -> None:
    assert TimeNeeds().empty
    left = TimeNeeds(fps=True, cues=frozenset({"a"}))
    right = TimeNeeds(tempo=True, cues=frozenset({"b"}), words=frozenset({"vo"}), default_words=True)
    merged = left | right
    assert merged == TimeNeeds(
        fps=True,
        tempo=True,
        cues=frozenset({"a", "b"}),
        words=frozenset({"vo"}),
        default_words=True,
    )
    assert not merged.empty
    assert not TimeNeeds(window=True).empty
    assert not TimeNeeds(markers=frozenset({"m"})).empty
    assert not TimeNeeds(default_words=True).empty


def test_timeset_offset_and_window() -> None:
    base = t.beats()[0:8:2]
    shifted = base + t("1b") - t("2f")
    assert shifted.offset == t("1b-2f")
    assert base.offset == t("0s")
    with pytest.raises(TimeError, match="duration"):
        _ = base + t("cue:x")
    span = t.span(t("1s"), t("3s"))
    assert base.within(span).window == span
    assert base.window is None


def test_beat_grid_items_and_slices() -> None:
    assert t.beats()[3] == t("beat:3")
    assert t.beats("downbeat")[2] == t("downbeat:2")
    assert t.beats("bar")[0] == t("bar:0")
    assert t.beats("kick")[2] == t("marker:kick#2")
    assert t.beats("kick")[0] == t("marker:kick")
    assert t.beats()[0:8:2] == TimeSet(stream="beat", start=0, stop=8, step=2)
    assert t.beats()[:4] == TimeSet(stream="beat", start=0, stop=4, step=1)
    assert t.beats()[4:] == TimeSet(stream="beat", start=4, stop=None, step=1)
    assert t.beats("kick").every(2, start=1, stop=9) == TimeSet(stream="kick", start=1, stop=9, step=2)
    assert t.beats("kick").all() == TimeSet(stream="kick")
    span = t.span(t("0s"), t("2s"))
    assert t.beats().within(span) == TimeSet(stream="beat", window=span)
    assert isinstance(t.beats(), BeatGrid)
    with pytest.raises(TimeError):
        t.beats()[-1]
    with pytest.raises(TimeError):
        t.beats()[-2:]
    with pytest.raises(TimeError):
        t.beats()[0:8:0]
    with pytest.raises(TimeError):
        t.beats()[0:8:-1]
    with pytest.raises(TimeError):
        t.beats().every(0)
    with pytest.raises(TimeError):
        t.beats("not a name")


def test_factory_methods() -> None:
    assert t.start == t("start")
    assert t.end == t("end")
    assert t.span(t("0s"), t("15s")) == Span(start=t("0s"), end=t("15s"))
    assert t.set(t("1s"), t("beat:2")).times == (t("1s"), t("beat:2"))
    assert t.secs(1.5) == t("1.5s")
    assert t.secs(0.1) == t("0.1s")
    assert t.secs(2) == t("2s")
    assert t.secs("-0.25") == t("-0.25s")
    assert t.secs(Fraction(1, 4)) == t("0.25s")
    assert t.ms(300) == t("0.3s")
    assert t.ms(0.5) == t("0.5ms")
    assert t.ticks(5) == Time(ticks=5)
    assert t.frames(2.5) == t("2.5f")
    assert t.frames(-2) == t("-2f")
    assert t.frame(120) == t("f:120")
    assert t.beat(2.5) == t("beat:2.5")
    assert t.beat("8") == t("beat:8")
    assert t.downbeat(3) == t("downbeat:3")
    assert t.bar(Fraction(1, 2)) == t("bar:0.5")
    assert t.cue("logo-intro") == t("cue:logo-intro")
    assert t.marker("song.drop") == t("marker:song.drop#0")
    assert t.marker("intro", nth=2) == t("marker:intro#2")
    assert t.word("EksenGOLD") == t("word:'EksenGOLD'")
    assert t.word("Merhaba", source="vo", nth=1) == t("word:vo:'Merhaba'#1")
    assert t.word(index=12) == t("word:#12")
    assert t.word(index=3, source="vo") == t("word:vo:#3")
    assert t.word("it's") == t("word:'it\\'s'")


def test_factory_rejections() -> None:
    with pytest.raises(TimeError, match="finer than one tick"):
        t.secs(1e-7)
    with pytest.raises(TimeError, match="finer than one tick"):
        t.ms("1.234")
    with pytest.raises(TimeError):
        t.secs(float("nan"))
    with pytest.raises(TimeError):
        t.secs("1/3")
    with pytest.raises(TimeError, match="terminating"):
        t.frames(Fraction(1, 3))
    with pytest.raises(TypeError):
        t.ticks(1.5)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        t.secs(True)
    with pytest.raises(TimeError):
        t.frame(-1)
    with pytest.raises(TimeError):
        t.beat(-0.5)
    with pytest.raises(TimeError):
        t.cue("logo-2nd")
    with pytest.raises(TimeError):
        t.cue("")
    with pytest.raises(TimeError):
        t.marker("x", nth=-1)
    with pytest.raises(TimeError, match="exactly one"):
        t.word()
    with pytest.raises(TimeError, match="exactly one"):
        t.word("a", index=1)
    with pytest.raises(TimeError):
        t.word(index=1, nth=2)
    with pytest.raises(TimeError):
        t.word("a", source="bad source")
    with pytest.raises(TypeError, match="t.span start"):
        t.span(1, t("1s"))  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="t.set"):
        t.set("1s")  # pyright: ignore[reportArgumentType]


def test_seconds_constructors_are_exact() -> None:
    assert t.secs(0.1).ticks == TICKS_PER_SECOND // 10
    assert t.secs(0.1) + t.secs(0.2) == t.secs(0.3)
