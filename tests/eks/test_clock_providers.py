from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import fx, t, tx
from eks.media import STTMarkers
from eks_harness.engine.validator import validate
from eks_harness.ir.catalog.effects import Captions
from eks_harness.ir.catalog.markers import BeatBackend
from eks_harness.ir.hashing import cache_inputs, clock_dependencies, hash_plan
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry

PIN = "@sha256:" + "ef" * 32


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def order(plan: Plan) -> list[str]:
    return [node.id for node, _ancestors in plan.walk()]


def codes(plan: Plan) -> list[str]:
    return [issue.code for issue in validate(plan).issues]


def beat_card(name: str) -> Any:
    @eks.scene(name=name, kind="gfx", size=(1080, 1920), duration=t("8s"), fps=30)
    def card(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        sc.on(t.beats("downbeat")[0:8], title.pulse(amount=1.06))

    return card


def song_plan(song_first: bool, backend: BeatBackend = "auto", card: Any = None) -> Plan:
    card = card if card is not None else beat_card(f"card_{song_first}_{backend}")

    @eks.edit(name=f"song_{song_first}_{backend}", fps=30, size=(1080, 1920), duration=t("8s"))
    def edit(e: eks.Edit) -> None:
        if song_first:
            e.audio.track("artifact:SONG1", name="song").beats("beat", "downbeat", backend=backend)
        e.track("graphics").clip(e.gfx(card))
        if not song_first:
            e.audio.track("artifact:SONG1", name="song").beats("beat", "downbeat", backend=backend)

    return eks.plan(edit)


# clock providers are graph dependencies of the nodes whose times resolve against them


@pytest.mark.parametrize("song_first", [True, False])
def test_a_beat_synced_embed_plans_in_both_declaration_orders(eks_registry: Registry, song_first: bool) -> None:
    plan = song_plan(song_first)
    markers = one(plan, "media.markers")
    render = one(plan, "gfx.render")
    assert plan.clock.tempo is not None and plan.clock.tempo.source == markers.id
    assert markers.id in render.after
    assert order(plan).index(markers.id) < order(plan).index(render.id)
    assert order(plan).index(one(plan, "media.source").id) < order(plan).index(markers.id)
    assert codes(plan) == []


def test_both_declaration_orders_give_the_same_render_hashes(eks_registry: Registry) -> None:
    card = beat_card("shared_card")
    first, last = song_plan(True, card=card), song_plan(False, card=card)
    for op in ("media.source", "media.markers", "gfx.render", "audio.mix", "edit.render"):
        assert one(first, op).hash == one(last, op).hash, op


def test_a_score_waits_for_the_song_declared_after_its_embed(eks_registry: Registry) -> None:
    card = beat_card("late_card")

    @eks.score(name="late_song", fps=30, window=t.span(t("0s"), t("8s")))
    def late_song(sc: eks.Score) -> None:
        sc.edit.track("graphics").clip(sc.gfx(card))
        sc.audio.track(eks.media.source("artifact:SONG1"), name="song").beats("beat", "downbeat")

    plan = eks.plan(late_song)
    markers = one(plan, "media.markers")
    render = one(plan, "gfx.render")
    assert markers.id in render.after
    assert order(plan).index(markers.id) < order(plan).index(render.id)
    assert clock_dependencies(plan)[render.id] == (markers.id,)
    assert codes(plan) == []


def test_hashing_a_late_provider_on_demand_hashes_its_inputs_first(eks_registry: Registry) -> None:
    plan = song_plan(True)
    expected = {node.id: node.hash for node, _ancestors in plan.walk()}
    moved = Plan.model_validate_json(plan.to_json())
    renders = [node for node in moved.nodes if node.op == "gfx.render"]
    moved.nodes = [*renders, *(node for node in moved.nodes if node.op != "gfx.render")]
    assert [node.op for node in moved.nodes][:3] == ["gfx.render", "media.source", "media.markers"]
    for node, _ancestors in moved.walk():
        node.hash = ""
    hash_plan(moved)
    assert {node.id: node.hash for node, _ancestors in moved.walk()} == expected


# stream and word-source names are clock references


def captioned(language: str, source: str) -> Plan:
    @eks.edit(name=f"captioned_{language}_{source}", fps=30, size=(1080, 1920), duration=t("10s"))
    def edit(e: eks.Edit) -> None:
        voice = eks.media.source("artifact:VOICE1")
        eks.media.markers(voice, STTMarkers(name="vo", language=language))
        e.audio.track(voice, name="voice")
        e.track("main").clip(e.media("artifact:VID123"), start=t("0s"), src=t.span(t("0s"), t("10s"))).effect(
            fx.captions(source))

    return eks.plan(edit)


def test_captions_depend_on_their_speech_to_text_analysis(eks_registry: Registry) -> None:
    turkish, english = captioned("tr", "vo"), captioned("en", "vo")
    markers = one(turkish, "media.markers")
    render = one(turkish, "edit.render")
    assert markers.id in render.after
    assert one(turkish, "media.markers").hash != one(english, "media.markers").hash
    assert render.hash != one(english, "edit.render").hash
    assert codes(turkish) == []


def test_captions_naming_no_word_source_are_unresolvable(eks_registry: Registry) -> None:
    plan = captioned("tr", "subtitles")
    report = validate(plan)
    assert [(issue.code, issue.field) for issue in report.issues] == [
        ("time.unresolvable", "timeline.tracks.0.clips.0.effects.0.source")]


def test_captions_take_a_word_source_name_not_a_file() -> None:
    with pytest.raises(ValidationError):
        Captions.model_validate({"source": "subs/voice.srt"})


def flashy(bpm: float | None, stream: str, backend: BeatBackend = "auto") -> Plan:
    @eks.edit(name=f"flashy_{int(bpm or 0)}_{stream}_{backend}", fps=30, size=(1080, 1920), duration=t("8s"))
    def edit(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        song = eks.media.source(f"artifact:SONG1{PIN}")
        e.audio.track(song, name="song").beats("beat", "downbeat", bpm=bpm, backend=backend)
        main = e.track("main")
        main.clip(e.media(f"artifact:VID1{PIN}"), start=t("0s"), src=t.span(t("0s"), t("4s"))).transition_out(
            tx.beat_flash(t("1s"), stream=stream))
        main.clip(e.media(f"artifact:VID2{PIN}"), start=t("4s"), src=t.span(t("0s"), t("4s")))

    return eks.plan(edit)


def test_a_beat_transition_follows_the_beat_grid(eks_registry: Registry) -> None:
    detected, locked = flashy(None, "beat"), flashy(120.0, "beat")
    markers = one(detected, "media.markers")
    render = one(detected, "edit.render")
    assert markers.id in render.after
    assert render.hash != one(locked, "edit.render").hash
    assert cache_inputs(detected, render) == (markers.id,)
    assert cache_inputs(flashy(None, "beat", "madmom"), one(flashy(None, "beat", "madmom"), "edit.render")) == ()


def test_a_beat_transition_on_an_unknown_stream_is_unresolvable(eks_registry: Registry) -> None:
    plan = flashy(None, "snare")
    assert "time.unresolvable" in codes(plan)


def checked(bpm: float | None, stream: str, with_song: bool = True) -> Plan:
    @eks.edit(name=f"checked_{int(bpm or 0)}_{stream}_{with_song}", fps=30, size=(1080, 1920), duration=t("8s"))
    def edit(e: eks.Edit) -> None:
        if with_song:
            e.audio.track(eks.media.source("artifact:SONG1"), name="song").beats("beat", "downbeat", bpm=bpm)
        e.track("main").clip(e.media("artifact:VID1"), start=t("0s"), src=t.span(t("0s"), t("8s")))
        e.expect.beats(stream)

    return eks.plan(edit)


def test_a_beats_media_check_depends_on_the_beat_grid(eks_registry: Registry) -> None:
    detected, locked = checked(None, "beat"), checked(120.0, "beat")
    markers = one(detected, "media.markers")
    check = one(detected, "check.media")
    assert markers.id in check.after
    assert check.hash != one(locked, "check.media").hash
    assert markers.id in (cache_inputs(detected, check) or ())


def test_a_beats_media_check_on_a_stream_nothing_provides_is_unresolvable(eks_registry: Registry) -> None:
    plan = checked(None, "snare", with_song=False)
    report = validate(plan)
    assert [issue.code for issue in report.issues] == ["time.unresolvable"]
    assert report.issues[0].field == "params.stream"


# cache inputs follow the cues a node uses


def test_a_cue_over_an_auto_beat_grid_joins_the_cache_key(eks_registry: Registry) -> None:
    @eks.edit(name="cued", fps=30, size=(1080, 1920), duration=t("20s"))
    def cued(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.audio.track(eks.media.source(f"artifact:SONG1{PIN}"), name="song").beats("beat", "downbeat")
        drop = e.cue("drop", t("beat:16"))
        e.track("main").clip(e.media(f"artifact:VID1{PIN}"), start=t("0s"), src=t.span(t("0s"), t("20s")))
        e.track("captions").overlay(eks.overlay.caption("Drop", at=t.span(drop, drop + t("1s"))))

    plan = eks.plan(cued)
    markers = one(plan, "media.markers")
    render = one(plan, "edit.render")
    assert plan.clock.cues
    assert cache_inputs(plan, render) == (markers.id,)
    assert markers.id in render.after
