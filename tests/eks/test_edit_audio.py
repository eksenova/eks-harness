from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import media, t
from eks.audio import AudioMix, AudioTrackBuilder, marker_name
from eks.ref import node_of, type_of
from eks.tracing import TraceContext, TraceError, tracing
from eks_harness.ir.catalog.markers import OnsetMarkers
from eks_harness.ir.ops.audio import EqBand, Mix
from eks_harness.ir.plan import Clock, Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import TempoMap, TimeError


def context(registry: Registry, clock: Clock | None = None) -> TraceContext:
    return TraceContext(kind="edit", name="mix", registry=registry, clock=clock)


def mix_args(plan: Plan) -> dict[str, Any]:
    nodes = [node for node, _ in plan.walk() if node.op == "audio.mix"]
    assert len(nodes) == 1
    mix: Any = nodes[0].args["mix"]
    return mix


def test_track_defaults(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        assert mix.empty
        song = mix.track("music/song.ogg")
        assert isinstance(song, AudioTrackBuilder)
        assert not mix.empty
        voice = mix.track(media.source("https://cdn.example.com/vo/take-1.wav?x=1"))
        artifact = mix.track("artifact:AUD1")
        named = mix.track("music/song.ogg", name="bed")
        duplicate = mix.track("music/song.ogg")
        weird = mix.track("sfx/2 hits!.mp3")
        assert [song.name, voice.name, artifact.name, named.name, duplicate.name, weird.name] == [
            "song", "take-1", "track2", "bed", "song-2", "2_hits_"]
        assert song.media is named.media
        assert type_of(song.media) == "media"
        result = mix.finalize()
        assert result is not None
        assert mix.finalize() is result
        plan = ctx.finalize()
    tracks = mix_args(plan)["tracks"]
    assert tracks[0] == {
        "name": "song", "media": {"$ref": node_of(song.media), "path": []}, "start": "0s", "gain": 0.0,
        "pan": 0.0, "mute": False, "ducks": []}
    assert mix_args(plan)["master"] == {"limiter": True, "gain": 0.0}
    assert mix_args(plan)["sample_rate"] == 48000
    assert mix_args(plan)["channels"] == 2
    assert "duration" not in mix_args(plan)


def test_track_options_and_errors(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        mix.track("vo.wav", gain=eks.keys({t("0s"): -20.0, t("1s"): 0.0}), pan=-0.5,
                  start=t("bar:2"), src=t.span(t("1s"), t("9s")), mute=True)
        with pytest.raises(TraceError, match="already has an audio track named 'vo'"):
            mix.track("other.wav", name="vo")
        with pytest.raises(TraceError, match="must match"):
            mix.track("other.wav", name="bad name")
        with pytest.raises(TypeError, match="pass t"):
            mix.track("other.wav", start="1s")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="Span"):
            mix.track("other.wav", src=t("1s"))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="expects a number"):
            mix.track("other.wav", gain="loud")  # type: ignore[arg-type]
        mix.finalize(duration=t("15s"))
        plan = ctx.finalize()
    args = mix_args(plan)
    assert args["duration"] == "15s"
    (track,) = args["tracks"]
    assert track["gain"]["keys"][0] == {"t": "0s", "v": -20.0, "interp": "linear"}
    assert (track["pan"], track["start"], track["src"], track["mute"]) == (
        -0.5, "bar:2", {"start": "1s", "end": "9s"}, True)


def test_every_configuration_method(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        voice = mix.track("vo.wav")
        music = mix.track("music/song.ogg")
        chained = (
            music.duck("vo", amount_db=-9.0, attack=t("20ms"))
            .eq(EqBand(frequency_hz=120.0, gain_db=-3.0, type="low_shelf"), EqBand(frequency_hz=3000.0))
            .compress(threshold_db=-20.0, ratio=3.0, release=t("150ms"))
            .fade(fade_in=t("500ms"), fade_out=t("2s"))
            .reverb(wet=eks.curves.sine(0.5), room_size=0.8)
            .denoise(attenuation_limit_db=30.0)
            .stems("drums", "bass")
            .set(pan=0.25, mute=False)
        )
        assert chained is music
        voice.compress().fade(fade_out=t("1s")).stems("vocals")
        voice.eq(EqBand(frequency_hz=200.0))
        voice.eq(EqBand(frequency_hz=8000.0, q=2.0))
        found = voice.markers(OnsetMarkers(name="hits"))
        assert ctx.node(node_of(found)).args["media"] == {"$ref": node_of(voice.media), "path": []}
        mix.master(loudness=-14.0, true_peak=-1.0, lra=7.0)
        with pytest.raises(TypeError, match="at least one EqBand"):
            voice.eq()
        with pytest.raises(TypeError, match="EqBand"):
            voice.eq(120.0)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="fade_in=, fade_out="):
            voice.fade()
        with pytest.raises(TypeError, match="at least one stem"):
            voice.stems()
        with pytest.raises(ValidationError):
            voice.stems("guitar")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="cannot be set"):
            voice.set(name="x")  # pyright: ignore[reportCallIssue]
        with pytest.raises(TypeError, match="cannot be set"):
            voice.set(volume=1.0)  # pyright: ignore[reportCallIssue]
        with pytest.raises(ValidationError):
            voice.compress(ratio=0.5)
        with pytest.raises(ValidationError):
            mix.master(loudness=3.0)
        with pytest.raises(TraceError, match="under itself"):
            voice.duck("vo")
        mix.finalize()
        plan = ctx.finalize()
    args = mix_args(plan)
    vo, song = args["tracks"]
    assert song["ducks"] == [{"under": "vo", "amount_db": -9.0, "threshold_db": -30.0, "attack": "0.02s",
                              "release": "0.25s"}]
    assert song["eq"] == {"bands": [
        {"frequency_hz": 120.0, "gain_db": -3.0, "q": 1.0, "type": "low_shelf"},
        {"frequency_hz": 3000.0, "gain_db": 0.0, "q": 1.0, "type": "peak"}]}
    assert song["compressor"] == {"threshold_db": -20.0, "ratio": 3.0, "attack": "0.01s", "release": "0.15s",
                                  "makeup_db": 0.0}
    assert song["fades"] == {"fade_in": "0.5s", "fade_out": "2s"}
    assert song["reverb"]["wet"]["curve"]["kind"] == "sine"
    assert song["reverb"]["room_size"] == 0.8
    assert song["denoise"] == {"attenuation_limit_db": 30.0}
    assert song["stems"] == ["drums", "bass"]
    assert (song["pan"], song["mute"]) == (0.25, False)
    assert vo["eq"] == {"bands": [{"frequency_hz": 8000.0, "gain_db": 0.0, "q": 2.0, "type": "peak"}]}
    assert vo["compressor"]["ratio"] == 4.0
    assert vo["fades"] == {"fade_out": "1s"}
    assert vo["stems"] == ["vocals"]
    assert args["master"] == {"loudness": -14.0, "true_peak": -1.0, "lra": 7.0, "limiter": True, "gain": 0.0}


def test_empty_mix_records_nothing(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        mix.master(loudness=-14.0)
        assert mix.empty
        assert mix.finalize() is None
        assert mix.finalize(duration=t("1s")) is None
        plan = ctx.finalize()
    assert plan.nodes == []

    @eks.edit(fps=30, size=(1080, 1920))
    def silent(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4")

    edit_plan = eks.plan(silent)
    assert [node.op for node, _ in edit_plan.walk()] == ["media.source", "edit.render"]
    timeline: Any = edit_plan.nodes[-1].args["timeline"]
    assert "audio" not in timeline


def test_duck_references_another_track(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        music = mix.track("music/song.ogg").duck("voice", amount_db=-15.0)
        mix.track("vo/take.wav", name="voice")
        music.duck("sfx")
        with pytest.raises(TraceError, match="ducks under 'sfx', which is not another track"):
            mix.finalize()
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        mix.track("music/song.ogg").duck("voice")
        mix.track("vo/take.wav", name="voice")
        result = mix.finalize()
        assert result is not None
        plan = ctx.finalize()
    song, voice = mix_args(plan)["tracks"]
    assert [duck["under"] for duck in song["ducks"]] == ["voice"]
    assert voice["ducks"] == []


def test_a_bad_duck_names_the_call_that_made_it() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def ducked(e: eks.Edit) -> None:
        music = e.audio.track("music/song.ogg")
        music.duck("vocals")

    with pytest.raises(TraceError) as raised:
        eks.plan(ducked)
    message = str(raised.value)
    assert "track.duck('vocals') at tests/eks/test_edit_audio.py:" in message
    assert "audio track 'song' ducks under 'vocals', which is not another track of the mix; other tracks: none" in (
        message)

    @eks.edit(fps=30, size=(1080, 1920))
    def set_ducks(e: eks.Edit) -> None:
        e.audio.track("vo/take.wav", name="voice")
        e.audio.track("music/song.ogg").set(ducks=[{"under": "drums"}])  # pyright: ignore[reportArgumentType]

    with pytest.raises(TraceError) as raised:
        eks.plan(set_ducks)
    message = str(raised.value)
    assert "track.set(ducks=...) at tests/eks/test_edit_audio.py:" in message
    assert "other tracks: 'voice'" in message


def test_beats_sets_the_tempo_source_once(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        song = mix.track("music/song.ogg").beats()
        markers = [node for node in ctx.scope().nodes if node.op == "media.markers"]
        assert len(markers) == 1
        assert markers[0].args["sources"] == [
            {"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"], "backend": "auto"}]
        assert ctx.clock.tempo == TempoMap(source=markers[0].id)
        mix.track("music/other-song.ogg").beats("kick", "snare", bpm=128.0, backend="librosa", name="drums")
        second = [node for node in ctx.scope().nodes if node.op == "media.markers"][1]
        assert second.args["sources"] == [{
            "kind": "beat_tracker", "name": "drums", "streams": ["kick", "snare"], "bpm": 128.0,
            "backend": "librosa"}]
        assert ctx.clock.tempo == TempoMap(source=markers[0].id)
        assert song.name == "song"
        mix.track("music/9 lives.ogg").beats()
        third = [node for node in ctx.scope().nodes if node.op == "media.markers"][2]
        third_sources: Any = third.args["sources"]
        assert third_sources[0]["name"] == "_9_lives"
    ctx = context(eks_registry, Clock(tempo=TempoMap(bpm=120.0)))
    with tracing(ctx):
        AudioMix(ctx).track("music/song.ogg").beats()
        assert ctx.clock.tempo == TempoMap(bpm=120.0)


def test_marker_name() -> None:
    assert marker_name("song") == "song"
    assert marker_name("my-song.v2") == "my_song_v2"
    assert marker_name("2nd") == "_2nd"
    assert marker_name("\u015fark\u0131") == "sarki"
    assert marker_name("\u0130stanbul") == "Istanbul"


def test_turkish_and_percent_encoded_names_fold_to_ascii(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        first = mix.track("m\u00fczik/\u015fark\u0131.ogg")
        second = mix.track("m\u00fczik/\u00e7ark\u0131.ogg")
        third = mix.track("https://cdn.example.com/m%C3%BCzik/%C5%9Fark%C4%B1%20%C4%B0zmir.ogg")
        fourth = mix.track("file:\u0130stanbul \u00d6zel.ogg").beats()
        assert [first.name, second.name, third.name, fourth.name] == [
            "sarki", "carki", "sarki_Izmir", "Istanbul_Ozel"]
        markers = [node for node in ctx.scope().nodes if node.op == "media.markers"]
        marker_sources: Any = markers[0].args["sources"]
        assert marker_sources[0]["name"] == "Istanbul_Ozel"


def test_mix_is_closed_after_finalize(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        track = mix.track("music/song.ogg")
        mix.finalize()
        with pytest.raises(TraceError, match="already finalized"):
            mix.track("late.wav")
        with pytest.raises(TraceError, match="already finalized"):
            track.fade(fade_in=t("1s"))
        with pytest.raises(TraceError, match="already finalized"):
            mix.master(loudness=-14.0)


def test_edit_audio_mix_feeds_the_timeline() -> None:
    @eks.edit(fps=30, size=(1080, 1920), duration=t("15s"))
    def scored(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")
        e.audio.track("vo.wav").duck("song")
        e.audio.master(loudness=-14.0)
        e.track("main").clip("clips/a.mp4")

    plan = eks.plan(scored)
    ops = [node.op for node, _ in plan.walk()]
    assert ops == ["media.source", "media.markers", "media.source", "media.source", "audio.mix", "edit.render"]
    mix_node = plan.nodes[4]
    assert mix_node.args["mix"]["duration"] == "15s"  # type: ignore[index]
    assert mix_args(plan)["master"]["loudness"] == -14.0
    timeline: Any = plan.nodes[5].args["timeline"]
    assert timeline["audio"] == {"$ref": mix_node.id, "path": []}
    assert plan.clock.tempo is not None
    assert plan.clock.tempo.source == plan.nodes[1].id


def test_mix_model_invariants() -> None:
    song: dict[str, Any] = {"name": "song", "media": {"$ref": "n0000000001", "path": []}}
    voice: dict[str, Any] = {"name": "voice", "media": {"$ref": "n0000000002", "path": []}}
    mix = Mix.model_validate({"tracks": [{**song, "ducks": [{"under": "voice"}]}, voice]})
    assert mix.tracks[0].ducks[0].under == "voice"
    with pytest.raises(ValidationError, match="two tracks named 'song'"):
        Mix.model_validate({"tracks": [song, song]})
    with pytest.raises(ValidationError, match="ducks under 'sfx', which is not another track"):
        Mix.model_validate({"tracks": [{**song, "ducks": [{"under": "sfx"}]}, voice]})
    with pytest.raises(ValidationError, match="ducks under 'song'"):
        Mix.model_validate({"tracks": [{**song, "ducks": [{"under": "song"}]}]})
    with pytest.raises(ValidationError, match="positive length"):
        Mix.model_validate({"tracks": [{**song, "fades": {"fade_in": "0s"}}]})


def test_audio_lengths_must_be_positive_durations(eks_registry: Registry) -> None:
    ctx = context(eks_registry)
    with tracing(ctx):
        mix = AudioMix(ctx)
        song = mix.track("music/song.ogg")
        mix.track("vo.wav", name="voice")
        with pytest.raises(TimeError, match="positive length"):
            song.duck("voice", attack=t("0s"))
        with pytest.raises(TimeError, match="point in time"):
            song.duck("voice", release=t("beat:4"))
        with pytest.raises(TimeError, match="positive length"):
            song.compress(attack=t("0s") - t("5ms"))
        with pytest.raises(TimeError, match="positive length"):
            song.fade(fade_out=t("0s"))
        with pytest.raises(TimeError, match="point in time"):
            song.fade(fade_in=t("end"))
        song.fade(fade_in=t("1b"), fade_out=t("8f"))
        with pytest.raises(TimeError, match="positive length"):
            mix.finalize(duration=t("0s"))
