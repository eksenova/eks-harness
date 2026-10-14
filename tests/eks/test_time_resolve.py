from __future__ import annotations

from fractions import Fraction

import pytest

from eks_harness.ir.time import (
    TICKS_PER_SECOND,
    Meter,
    MeterChange,
    TempoChange,
    TempoMap,
    TimeContext,
    TimeError,
    Word,
    t,
    ticks_per_frame,
)

S = TICKS_PER_SECOND
TPF30 = 23_520_000


def clock(**tempo: object) -> TimeContext:
    return TimeContext.static(fps=Fraction(30), tempo=TempoMap.model_validate({"bpm": 120, **tempo}))


def test_beats_at_120_bpm() -> None:
    c = clock()
    assert c.resolve(t("beat:8")) == 4 * S == 2_822_400_000
    assert c.resolve(t("beat:2.5")) == S + S // 4
    assert c.resolve(t("beat:8+1b")) == 4 * S + S // 2
    assert c.resolve(t("bar:2")) == 4 * S
    assert c.resolve(t("bar:1+0.5bar")) == 3 * S
    assert c.resolve(t("bar:0")) == 0
    assert c.resolve(t("downbeat:2")) == 4 * S
    assert c.resolve(t("downbeat:1.5")) == 3 * S
    assert c.resolve(t("1b")) == S // 2
    assert c.resolve(t("2s+1b")) == 2 * S + S // 2
    assert c.resolve(t("beat:8-1b")) == 3 * S + S // 2


def test_meter_change() -> None:
    c = clock(meters=(MeterChange(bar=1, meter=Meter(numerator=3)),))
    assert c.resolve(t("bar:1")) == 2 * S
    assert c.resolve(t("bar:2")) == 2 * S + S + S // 2
    assert c.resolve(t("bar:3")) == 5 * S
    assert c.resolve(t("bar:1.5")) == 2 * S + 3 * S // 4
    assert c.resolve(t("bar:1+1bar")) == 2 * S + S + S // 2
    assert c.resolve(t("downbeat:2")) == 2 * S + S + S // 2
    assert c.bar_start_beat(Fraction(2)) == 7
    assert c.bar_index_at_beat(Fraction(7)) == 2
    assert c.bar_index_at_beat(Fraction(5)) == Fraction(4, 3)
    assert c.resolve_set(t.beats("bar")[0:4]) == [0, 2 * S, 2 * S + 3 * S // 2, 5 * S]


def test_tempo_change() -> None:
    c = clock(changes=(TempoChange(beat=4, bpm=60),))
    assert c.resolve(t("beat:4")) == 2 * S
    assert c.resolve(t("beat:5")) == 3 * S
    assert c.resolve(t("beat:4.5")) == 2 * S + S // 2
    assert c.beat_index_at(3 * S) == 5
    assert c.resolve(t("beat:100")) == 2 * S + 96 * S


def test_tempo_offset_shifts_beats() -> None:
    c = clock(offset=t("0.1s"))
    assert c.resolve(t("beat:0")) == S // 10
    assert c.resolve(t("beat:8")) == 4 * S + S // 10
    assert c.resolve(t("bar:1")) == 2 * S + S // 10


def test_tempo_offset_must_be_concrete() -> None:
    with pytest.raises(TimeError, match="concrete"):
        clock(offset=t("beat:1"))


def test_external_tempo_has_no_beat_grid() -> None:
    c = TimeContext.static(tempo=TempoMap(external=True))
    assert c.beats == ()
    assert TimeContext.static(tempo=TempoMap(source="song")).beats == ()
    with pytest.raises(TimeError, match="at least 2"):
        c.resolve(t("beat:1"))


def test_exact_accumulation_without_drift() -> None:
    c = TimeContext.static(tempo=TempoMap(bpm=97))
    beat = Fraction(60 * S, 97)
    assert c.beats[1] == round(beat)
    assert c.beats[3] == round(3 * beat)
    assert c.beats[97] == 60 * S
    c2 = TimeContext.static(tempo=TempoMap(bpm=120.5))
    assert c2.beats[241] == 120 * S


def test_horizon_and_end() -> None:
    c = TimeContext.static(tempo=TempoMap(bpm=120), end=10 * S)
    assert c.beats[-1] >= 10 * S
    assert c.resolve(t("end-1bar")) == 8 * S
    assert c.resolve(t("end")) == 10 * S
    assert c.resolve(t("end-1s+1tk")) == 9 * S + 1
    long = TimeContext.static(tempo=TempoMap(bpm=120), horizon=700 * S)
    assert long.beats[-1] >= 700 * S
    default = TimeContext.static(tempo=TempoMap(bpm=120))
    assert default.beats[-1] >= 600 * S


def test_cues_and_frames() -> None:
    c = TimeContext.static(fps=Fraction(30), tempo=TempoMap(bpm=120), cues={"logo": t("3s"), "drop": t("beat:8")})
    assert c.cues == {"logo": 3 * S, "drop": 4 * S}
    assert c.resolve(t("cue:logo-2f")) == 3 * S - 2 * 23_520_000
    assert c.resolve(t("cue:drop+1b")) == 4 * S + S // 2
    assert c.resolve(t("f:120")) == 4 * S
    assert c.resolve(t("f:120-1f")) == 120 * TPF30 - TPF30
    assert c.ticks_per_frame() == TPF30
    assert c.frame_of(4 * S) == 120


def test_cues_cannot_reference_cues() -> None:
    with pytest.raises(TimeError, match="other cues"):
        TimeContext.static(cues={"a": t("1s"), "b": t("cue:a+1s")})


def test_ntsc_frame_rates() -> None:
    assert ticks_per_frame(Fraction(30000, 1001)) == 23_543_520
    assert ticks_per_frame(Fraction(24000, 1001)) == 29_429_400
    assert ticks_per_frame(Fraction(60000, 1001)) == 11_771_760
    for fps in (24, 25, 30, 48, 50, 60, 120):
        assert ticks_per_frame(Fraction(fps)) * fps == S
    c = TimeContext.static(fps=Fraction(30000, 1001))
    assert c.resolve(t("f:1")) == 23_543_520
    assert c.resolve(t("2f")) == 2 * 23_543_520


def test_frame_of_rounds_half_to_even() -> None:
    c = TimeContext(fps=Fraction(30))
    assert c.frame_of(TPF30 // 2) == 0
    assert c.frame_of(3 * TPF30 // 2) == 2
    assert c.frame_of(5 * TPF30 // 2) == 2
    assert c.frame_of(5 * TPF30 // 2 + 1) == 3
    assert c.frame_of(-(TPF30 // 2)) == 0


def test_fractional_frames_round_half_to_even() -> None:
    c = TimeContext(fps=Fraction(30000, 1001))
    assert c.resolve(t("0.5f")) == 11_771_760
    c2 = TimeContext(beats=(0, 3))
    assert c2.resolve(t("beat:0.5")) == 2
    assert c2.resolve(t("beat:1.5")) == 4


def test_resolve_set_beat_slices() -> None:
    c = clock()
    assert c.resolve_set(t.beats()[0:8:2]) == [0, S, 2 * S, 3 * S]
    assert c.resolve_set(t.beats()[0:8:2] + t("1b")) == [S // 2, 3 * S // 2, 5 * S // 2, 7 * S // 2]
    assert c.resolve_set(t.beats()[0:8:2] - t("0.1s")) == [-S // 10, 9 * S // 10, 19 * S // 10, 29 * S // 10]
    assert c.resolve_set(t.beats()[0:8:2].within(t.span(t("1s"), t("3s")))) == [S, 2 * S]
    assert c.resolve_set(t.beats().every(4, start=2, stop=11)) == [S, 3 * S, 5 * S]
    assert c.resolve_set(t.beats("downbeat")[0:3]) == [0, 2 * S, 4 * S]
    assert c.resolve_set(t.beats("bar")[1:3]) == [2 * S, 4 * S]


def test_resolve_set_stops_at_end() -> None:
    c = TimeContext.static(tempo=TempoMap(bpm=120), end=4 * S)
    assert c.resolve_set(t.beats().all()) == [k * S // 2 for k in range(8)]
    assert c.resolve_set(t.beats("bar").all()) == [0, 2 * S]


def test_resolve_set_explicit_times() -> None:
    c = clock()
    assert c.resolve_set(t.set(t("3s"), t("beat:1"), t("1f"))) == [TPF30, S // 2, 3 * S]
    assert c.resolve_set(t.set(t("3s"), t("1s")) + t("1b")) == [3 * S // 2, 7 * S // 2]
    assert c.resolve_set(t.set(t("3s"), t("1s")).within(t.span(t("0s"), t("2s")))) == [S]
    assert c.resolve_set(t.set()) == []


def hand_built() -> TimeContext:
    return TimeContext(
        fps=Fraction(25),
        beats=(0, 100, 200, 300),
        downbeats=(0, 400),
        markers={"kick": (10, 20, 30), "empty": ()},
        words={
            "vo": (
                Word("Merhaba", 5),
                Word("İSTANBUL", 50),
                Word("dünya", 70),
                Word("istanbul", 90),
            ),
            "sub": (Word("Hello", 7),),
        },
        default_words="vo",
        start=3,
        end=1000,
    )


def test_markers_and_words() -> None:
    c = hand_built()
    assert c.resolve(t("marker:kick")) == 10
    assert c.resolve(t("marker:kick#2")) == 30
    assert c.resolve(t("marker:kick#2+5tk")) == 35
    assert c.resolve(t("word:'Merhaba'")) == 5
    assert c.resolve(t("word:'istanbul'")) == 50
    assert c.resolve(t("word:'İstanbul'#1")) == 90
    assert c.resolve(t("word:'ISTANBUL'#1")) == 90
    assert c.resolve(t("word:'DUNYA'")) == 70
    assert c.resolve(t("word:vo:#2")) == 70
    assert c.resolve(t("word:#0")) == 5
    assert c.resolve(t("word:sub:'hello'")) == 7
    assert c.resolve(t("start")) == 3
    assert c.resolve(t("end")) == 1000


def test_word_folding_keeps_non_latin_letters() -> None:
    # Two Cyrillic words that differ only in their letters must not collide after folding.
    c = TimeContext(
        words={"vo": (Word("дом1", 5), Word("кот1", 9), Word("Ωmega", 13), Word("mega", 17), Word("Straße", 21))},
        default_words="vo",
    )
    assert c.resolve(t("word:'кот1'")) == 9
    assert c.resolve(t("word:'дом1'")) == 5
    assert c.resolve(t("word:'КОТ1'")) == 9
    assert c.resolve(t("word:'mega'")) == 17
    assert c.resolve(t("word:'ωMEGA'")) == 13
    assert c.resolve(t("word:'STRASSE'")) == 21
    with pytest.raises(TimeError, match="0 time"):
        c.resolve(t("word:'пес1'"))


def test_marker_and_word_errors() -> None:
    c = hand_built()
    with pytest.raises(TimeError, match="'kick'"):
        c.resolve(t("marker:snare"))
    with pytest.raises(TimeError, match="out of range"):
        c.resolve(t("marker:kick#3"))
    with pytest.raises(TimeError, match="'sub', 'vo'"):
        c.resolve(t("word:other:'x'"))
    with pytest.raises(TimeError, match="2 time"):
        c.resolve(t("word:'istanbul'#2"))
    with pytest.raises(TimeError, match="0 time"):
        c.resolve(t("word:'yok'"))
    with pytest.raises(TimeError, match="out of range"):
        c.resolve(t("word:vo:#4"))
    no_default = TimeContext(words={"vo": (Word("a", 1),)})
    with pytest.raises(TimeError, match="no default transcript"):
        no_default.resolve(t("word:'a'"))


def test_hand_built_grids() -> None:
    c = hand_built()
    assert c.resolve(t("beat:3.5")) == 350
    assert c.resolve(t("beat:1+1b")) == 200
    assert c.beat_index_at(150) == Fraction(3, 2)
    assert c.beat_index_at(-50) == Fraction(-1, 2)
    assert c.beat_index_at(500) == 5
    assert c.beat_time(Fraction(5, 2)) == 250
    assert c.resolve(t("downbeat:0.5")) == 200
    assert c.resolve(t("downbeat:2")) == 800
    assert c.resolve(t("bar:1")) == 400
    assert c.resolve(t("2f")) == 2 * ticks_per_frame(Fraction(25))
    assert c.resolve_set(t.beats("kick").all()) == [10, 20, 30]
    assert c.resolve_set(t.beats("kick")[1:]) == [20, 30]
    assert c.resolve_set(t.beats("empty").all()) == []
    assert c.resolve_set(t.beats("downbeat").all()) == [0, 400]
    assert c.resolve_set(t.beats().all()) == [0, 100, 200, 300]


def test_end_filters_stream_slices() -> None:
    c = TimeContext(markers={"kick": (10, 20, 30)}, end=25)
    assert c.resolve_set(t.beats("kick").all()) == [10, 20]


def test_downbeat_without_grid_is_bar() -> None:
    c = TimeContext(beats=(0, 100, 200, 300))
    assert c.resolve(t("downbeat:1")) == c.resolve(t("bar:1")) == 400
    assert c.resolve_set(t.beats("downbeat").all()) == [0]


def test_resolve_span() -> None:
    c = clock()
    assert c.resolve_span(t.span(t("beat:2"), t("bar:1"))) == (S, 2 * S)
    # different anchors only order once resolved, so resolve_span still catches a reversed span
    with pytest.raises(TimeError, match="ends before it starts"):
        c.resolve_span(t.span(t("bar:1"), t("beat:2")))


@pytest.mark.parametrize(("start", "end"), [("2s", "1s"), ("f:30", "f:10"), ("cue:logo+2s", "cue:logo+1s"),
                                            ("beat:4+1b", "beat:4"), ("bar:2+1f", "bar:2"), ("bar:3", "bar:2-1b"),
                                            ("marker:drop#2", "marker:drop#1")])
def test_spans_that_are_reversed_without_a_clock_raise(start: str, end: str) -> None:
    # both ends concrete, or offsets of the same anchor that only shrink: no clock can make the span ordered
    with pytest.raises(ValueError, match="ends before it starts"):
        t.span(t(start), t(end))


@pytest.mark.parametrize(("start", "end"), [("1s", "1s"), ("cue:logo", "cue:logo+1b"), ("beat:4+1b", "beat:4+1s"),
                                            ("bar:1", "beat:2"), ("bar:3", "bar:2+2b"), ("marker:a#2", "marker:b#1")])
def test_spans_that_may_be_ordered_are_accepted(start: str, end: str) -> None:
    assert t.span(t(start), t(end)).end == t(end)


def test_resolution_errors() -> None:
    c = TimeContext.static(fps=Fraction(30), tempo=TempoMap(bpm=120), cues={"logo": t("3s"), "intro": t("0s")})
    with pytest.raises(TimeError, match="'intro', 'logo'"):
        c.resolve(t("cue:missing"))
    with pytest.raises(TimeError, match="end"):
        c.resolve(t("end"))
    with pytest.raises(TimeError, match="negative"):
        c.beat_time(Fraction(-1))
    with pytest.raises(TimeError, match="negative"):
        c.resolve(t("beat:0-1b"))
    with pytest.raises(TimeError, match="at least 2"):
        TimeContext(beats=(0,)).resolve(t("beat:1"))
    with pytest.raises(TimeError, match="at least 2"):
        TimeContext().resolve(t("1b"))
    with pytest.raises(TimeError, match="fps"):
        TimeContext().resolve(t("f:1"))
    with pytest.raises(TimeError, match="divide"):
        TimeContext.static(fps=Fraction(11))
    with pytest.raises(TimeError, match="divide"):
        TimeContext(fps=Fraction(11)).resolve(t("1f"))
    with pytest.raises(TimeError, match="divide"):
        ticks_per_frame(Fraction(2997, 100))
    with pytest.raises(TimeError, match="unknown time stream"):
        c.resolve_set(t.beats("snare").all())
    with pytest.raises(TimeError):
        c.resolve_set(t.beats().all().model_copy(update={"times": (t("1s"),)}))
    with pytest.raises(TimeError, match="duration"):
        c.resolve_set(t.set(t("1s")).model_copy(update={"offset": t("cue:logo")}))


def test_static_without_tempo_resolves_concrete_times() -> None:
    c = TimeContext.static()
    assert c.beats == ()
    assert c.downbeats == ()
    assert c.resolve(t("1.5s")) == 3 * S // 2
    assert c.resolve(t("start+1tk")) == 1


def test_periodic_random_and_jittered_sets() -> None:
    # the random_trigger timing of the old video IR: a period, a seeded jitter and a seeded subset
    from eks_harness.ir.time import TimeContext, TimeError, TimeSet, t

    ctx = TimeContext.static(fps=Fraction(30))
    periodic = t.every(t("1s"), t.span(t("0s"), t("5s")))
    assert ctx.resolve_set(periodic) == [t(f"{n}s").ticks for n in range(5)]
    jittered = periodic.jittered(t("100ms"), seed=4)
    first = ctx.resolve_set(jittered)
    assert first == ctx.resolve_set(jittered)
    assert first != ctx.resolve_set(periodic)
    assert all(abs(value - t(f"{n}s").ticks) <= t("100ms").ticks
               for n, value in enumerate(sorted(first)))
    picked = periodic.random(2)
    with pytest.raises(TimeError, match="needs a seed"):
        ctx.resolve_set(picked)
    # without its own seed the set takes the node seed it is resolved with
    chosen = ctx.resolve_set(picked, seed=11)
    assert len(chosen) == 2
    assert chosen == ctx.resolve_set(picked, seed=11)
    assert set(chosen) <= set(ctx.resolve_set(periodic))
    beats = t.beats("downbeat")[0:8].random(3, seed=2)
    assert (beats.stream, beats.pick, beats.seed) == ("downbeat", 3, 2)
    assert TimeSet.model_validate(beats.model_dump(mode="json")) == beats
    with pytest.raises(ValueError, match="needs a window"):
        TimeSet(every=t("1s"))
    with pytest.raises(ValueError, match="positive length"):
        t.every(t("beat:2"), t.span(t("0s"), t("1s")))
    with pytest.raises(ValueError, match="only seeds jitter and pick"):
        TimeSet(times=(t("1s"),), seed=3)
