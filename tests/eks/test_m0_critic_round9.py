from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from pydantic import Field, ValidationError

import eks
from eks import fx, t
from eks.tracing import TraceError
from eks_harness.engine.validator import Report
from eks_harness.ir.catalog.markers import (
    CLOCK_STREAMS,
    BeatTracker,
    OnsetMarkers,
    STTMarkers,
)
from eks_harness.ir.hashing import cache_inputs, cacheable, hash_plan, machine_dependent
from eks_harness.ir.ops.blender import BlenderRender
from eks_harness.ir.ops.check import TextCheck
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, RegistryError

FOOTAGE = "artifact:VID1@sha256:" + "a" * 64
VOICE = "artifact:VO1@sha256:" + "b" * 64
DRUMS = "artifact:DRUMS@sha256:" + "c" * 64
VOCALS = "artifact:VOCALS@sha256:" + "d" * 64


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def args(node: Node) -> Any:
    return node.args


def rehashed(data: dict[str, Any]) -> Plan:
    return hash_plan(Plan.model_validate(data))


def codes(report: Report) -> list[tuple[str, str | None, str | None]]:
    return [(issue.code, issue.node, issue.field) for issue in report.issues]


# Blender: EEVEE and Workbench have no CPU path, and CPU Cycles output depends on the worker platform


def blender_plan(name: str, engine: str, device: Any, platform: str | None = None) -> Plan:
    @eks.scene(name=name, kind="blender", engine=engine, samples=8, fps=30, duration=t("2s"), device=device,
               platform=platform)
    def cube(sc: eks.BlenderScene) -> None:
        sc.camera(lens=50)

    return eks.plan(cube)


@pytest.mark.parametrize("engine", ["eevee", "eevee_next", "BLENDER_EEVEE_NEXT", "workbench", "BLENDER_WORKBENCH"])
def test_eevee_and_workbench_reject_the_cpu_device(eks_registry: Registry, engine: str) -> None:
    with pytest.raises(RegistryError, match="has no CPU device"):
        eks.scene(name="cpu_raster", kind="blender", engine=engine, device="CPU")


def test_the_default_engine_rejects_the_cpu_device(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="BLENDER_EEVEE_NEXT has no CPU device"):
        eks.scene(name="default_cpu", kind="blender", device="CPU")


@pytest.mark.parametrize("engine", ["BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"])
def test_blender_render_settings_reject_a_cpu_raster_engine(engine: Any) -> None:
    with pytest.raises(ValidationError, match=f"{engine} has no CPU device"):
        BlenderRender(engine=engine, samples=8, device="CPU", seed=0, size=(1080, 1920))
    assert BlenderRender(engine=engine, samples=8, device="GPU", seed=0, size=(1080, 1920)).device == "GPU"


@pytest.mark.parametrize("engine", ["eevee", "workbench", "cycles"])
def test_every_gpu_render_is_machine_dependent(eks_registry: Registry, engine: str) -> None:
    plan = blender_plan(f"gpu_{engine}", engine, "GPU")
    render = one(plan, "blender.render")
    assert machine_dependent(render)
    assert cache_inputs(plan, render) == (render.id,)


def test_an_unpinned_cpu_cycles_render_is_keyed_by_the_worker_platform(eks_registry: Registry) -> None:
    plan = blender_plan("cpu_any", "cycles", "CPU")
    render = one(plan, "blender.render")
    assert "platform" not in args(render)["scene"]["render"]
    assert machine_dependent(render)
    assert cache_inputs(plan, render) == (render.id,)
    assert not cacheable(plan, render)


def test_a_platform_pinned_cpu_cycles_render_is_cacheable_by_node_hash(eks_registry: Registry) -> None:
    linux = blender_plan("cpu_linux", "cycles", "CPU", "linux-x86_64")
    mac = blender_plan("cpu_mac", "cycles", "CPU", "darwin-arm64")
    render = one(linux, "blender.render")
    assert args(render)["scene"]["render"]["platform"] == "linux-x86_64"
    assert not machine_dependent(render)
    assert cacheable(linux, render)
    assert render.hash != one(mac, "blender.render").hash
    gpu = one(blender_plan("gpu_linux", "cycles", "GPU", "linux-x86_64"), "blender.render")
    assert machine_dependent(gpu)


def test_platform_pins_are_blender_only_and_name_a_worker_platform(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="platform"):
        eks.scene(name="bad_platform", kind="blender", engine="cycles", device="CPU", platform="Linux x86")
    with pytest.raises(RegistryError, match="not of gfx scenes"):
        eks.scene(name="gfx_platform", kind="gfx", platform="linux-x86_64")


def test_a_hand_written_cpu_eevee_render_is_invalid_and_machine_dependent(eks_registry: Registry) -> None:
    data = json.loads(blender_plan("cpu_pinned", "cycles", "CPU", "linux-x86_64").to_json())
    render = next(node for node in data["nodes"] if node["op"] == "blender.render")
    render["args"]["scene"]["render"]["engine"] = "BLENDER_EEVEE_NEXT"
    plan = rehashed(data)
    node = one(plan, "blender.render")
    assert machine_dependent(node)
    report = eks.validate(plan)
    assert ("args.invalid", node.id, None) in codes(report)
    assert "has no CPU device" in report.text()


# speech to text is neural inference: only a pinned model, device and precision is the same everywhere


def stt_plan(name: str, source: STTMarkers) -> Plan:
    @eks.edit(name=name, fps=30, size=(1080, 1920), seed=7, duration=t("10s"))
    def edit(e: eks.Edit) -> None:
        footage = e.media(FOOTAGE)
        voice = e.media(VOICE)
        eks.media.markers(voice, source)
        e.track("main").clip(footage, start=t("0s")).effect(fx.captions("vo"))
        e.render_settings(encoder="libx264")

    return eks.plan(edit)


def test_an_unpinned_transcript_joins_the_cache_key_of_its_readers(eks_registry: Registry) -> None:
    for name, source in (
        ("stt_auto", STTMarkers(name="vo", language="tr")),
        ("stt_model_only", STTMarkers(name="vo", model="large-v3")),
        ("stt_no_precision", STTMarkers(name="vo", model="large-v3", device="cpu")),
    ):
        plan = stt_plan(name, source)
        markers = one(plan, "media.markers")
        assert machine_dependent(markers), name
        assert cache_inputs(plan, one(plan, "edit.render")) == (markers.id,), name


def test_a_pinned_transcript_is_cacheable_by_node_hash(eks_registry: Registry) -> None:
    pinned = STTMarkers(name="vo", model="large-v3", device="cpu", compute_type="float32")
    plan = stt_plan("stt_pinned", pinned)
    markers = one(plan, "media.markers")
    assert args(markers)["sources"][0] == {
        "kind": "stt_markers", "name": "vo", "backend": "faster-whisper", "model": "large-v3", "device": "cpu",
        "compute_type": "float32"}
    assert not machine_dependent(markers)
    assert cacheable(plan, one(plan, "edit.render"))
    other = stt_plan("stt_int8", pinned.model_copy(update={"compute_type": "int8"}))
    assert one(other, "media.markers").hash != markers.hash
    assert one(other, "edit.render").hash != one(plan, "edit.render").hash


def test_stt_devices_follow_the_backend() -> None:
    with pytest.raises(ValidationError, match="runs on cpu or mps"):
        STTMarkers(name="vo", backend="mlx-whisper", device="cuda")
    with pytest.raises(ValidationError, match="runs on cpu or cuda"):
        STTMarkers(name="vo", backend="faster-whisper", device="mps")
    assert STTMarkers(name="vo", backend="mlx-whisper", device="mps").pinned() is False


# OCR checks pick their engine per machine unless the plan pins it


def ocr_plan(name: str, engine: Any) -> Plan:
    @eks.edit(name=name, fps=30, size=(1080, 1920), seed=7, duration=t("10s"))
    def edit(e: eks.Edit) -> None:
        e.track("main").clip(e.media(FOOTAGE), start=t("0s"))
        e.render_settings(encoder="libx264")
        e.expect.text("EksenGOLD", at=t("2s"), engine=engine, languages=("tr-TR",))
        e.expect.safe_area(engine=engine)

    return eks.plan(edit)


def test_auto_ocr_checks_are_keyed_per_machine(eks_registry: Registry) -> None:
    plan = ocr_plan("ocr_auto", "auto")
    text, safe_area = nodes_of(plan, "check.media")
    assert args(text)["params"] == {"text": "EksenGOLD", "engine": "auto", "languages": ["tr-TR"]}
    assert args(safe_area)["params"]["engine"] == "auto"
    for node in (text, safe_area):
        assert machine_dependent(node)
        assert cache_inputs(plan, node) == (node.id,)
    assert eks.validate(plan).ok


@pytest.mark.parametrize("engine", ["vision", "tesseract"])
def test_pinned_ocr_checks_are_cacheable_by_node_hash(eks_registry: Registry, engine: str) -> None:
    plan = ocr_plan(f"ocr_{engine}", engine)
    for node in nodes_of(plan, "check.media"):
        assert not machine_dependent(node)
        assert cacheable(plan, node)


def test_text_check_languages_are_language_tags() -> None:
    assert TextCheck(text="x").languages == ("tr-TR", "en-US")
    with pytest.raises(ValidationError):
        TextCheck(text="x", languages=())
    with pytest.raises(ValidationError):
        TextCheck(text="x", languages=("tr TR",))


def test_unknown_or_invalid_media_checks_are_machine_dependent(eks_registry: Registry) -> None:
    media = {"$ref": "n0000000001", "path": []}
    unknown = Node(id="n0000000002", op="check.media", args={"media": media, "kind": "acme_unknown"}, out="json")
    invalid = Node(id="n0000000003", op="check.media", args={"media": media, "kind": "cut",
                                                             "params": {"threshold": "high"}}, out="json")
    pure = Node(id="n0000000004", op="check.media", args={"media": media, "kind": "cut"}, out="json")
    assert machine_dependent(unknown)
    assert machine_dependent(invalid)
    assert not machine_dependent(pure)


def test_a_plugin_media_check_can_declare_itself_device_dependent(eks_registry: Registry) -> None:
    @eks.check("acme_faces", family="media", device_dependent=True, description="Faces are detected")
    class FacesCheck(eks.Args):
        min_faces: int = 1

    @eks.check("acme_glyphs", family="media", description="Glyphs read the same")
    class GlyphsCheck(eks.Args):
        detector: Literal["auto", "fixed"] = "auto"

        def pinned(self) -> bool:
            return self.detector != "auto"

    media = {"$ref": "n0000000001", "path": []}

    def check(kind: str, params: dict[str, Any]) -> Node:
        return Node(id="n0000000002", op="check.media", args={"media": media, "kind": kind, "params": params},
                    out="json")

    assert eks_registry.check("acme_faces").device_dependent
    assert machine_dependent(check("acme_faces", {}))
    assert machine_dependent(check("acme_glyphs", {}))
    assert not machine_dependent(check("acme_glyphs", {"detector": "fixed"}))
    assert FacesCheck(min_faces=2).min_faces == 2
    assert GlyphsCheck(detector="fixed").pinned()


def test_device_dependent_is_for_media_checks_only(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="media checks only"):
        @eks.check("acme_tidy", family="checkpoint", device_dependent=True)
        class Tidy(eks.Args):
            strict: bool = False

        assert Tidy().strict is False


# marker source, stream and transcript names share one plan-wide namespace


def test_a_repeated_analysis_name_is_refused(eks_registry: Registry) -> None:
    @eks.edit(name="twice", fps=30, size=(1080, 1920), duration=t("10s"))
    def twice(e: eks.Edit) -> None:
        eks.media.markers(e.media(DRUMS), OnsetMarkers(name="hits"))
        eks.media.markers(e.media(VOCALS), OnsetMarkers(name="hits", sensitivity=0.9))

    with pytest.raises(TraceError, match="OnsetMarkers 'hits' provides 'hits', which OnsetMarkers 'hits' already"):
        eks.plan(twice)


def test_a_repeated_name_inside_one_analysis_is_refused(eks_registry: Registry) -> None:
    @eks.edit(name="same_call", fps=30, size=(1080, 1920), duration=t("10s"))
    def same_call(e: eks.Edit) -> None:
        eks.media.markers(e.media(VOCALS), OnsetMarkers(name="x"), STTMarkers(name="x"))

    with pytest.raises(TraceError, match="STTMarkers 'x' provides 'x'"):
        eks.plan(same_call)


def test_a_track_analysis_cannot_reuse_another_name(eks_registry: Registry) -> None:
    @eks.edit(name="song_twice", fps=30, size=(1080, 1920), duration=t("10s"))
    def song_twice(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg").beats()
        e.audio.track(e.media(VOCALS), name="vocals").markers(OnsetMarkers(name="song"))

    with pytest.raises(TraceError, match="'song'"):
        eks.plan(song_twice)


@pytest.mark.parametrize("name", sorted(CLOCK_STREAMS))
def test_clock_tempo_stream_names_are_reserved(name: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        OnsetMarkers(name=name)
    with pytest.raises(ValidationError, match="reserved"):
        BeatTracker(name=name)


def test_a_plugin_analysis_cannot_publish_a_clock_stream(eks_registry: Registry) -> None:
    @eks.marker(description="Bar starts of a song")
    class BarGrid(eks.MarkerBase, frozen=True):
        kind: Literal["acme_bar_grid"] = Field(default="acme_bar_grid", description="Bar starts of a song")

        def stream_names(self) -> tuple[str, ...]:
            return (self.name, "bar")

    @eks.edit(name="bar_grid", fps=30, size=(1080, 1920), duration=t("10s"))
    def bar_grid(e: eks.Edit) -> None:
        eks.media.markers(e.media(DRUMS), BarGrid(name="grid"))

    with pytest.raises(TraceError, match="reserved for the clock tempo"):
        eks.plan(bar_grid)

    @eks.edit(name="onsets", fps=30, size=(1080, 1920), duration=t("10s"))
    def onsets(e: eks.Edit) -> None:
        eks.media.markers(e.media(DRUMS), OnsetMarkers(name="grid"))

    data = json.loads(eks.plan(onsets).to_json())
    markers = next(node for node in data["nodes"] if node["op"] == "media.markers")
    markers["args"]["sources"] = [{"kind": "acme_bar_grid", "name": "grid"}]
    report = eks.validate(rehashed(data))
    assert ("markers.duplicate", markers["id"], "sources.0.name") in codes(report)


def test_the_validator_reports_duplicate_names_in_hand_written_plans(eks_registry: Registry) -> None:
    @eks.edit(name="distinct", fps=30, size=(1080, 1920), duration=t("10s"))
    def distinct(e: eks.Edit) -> None:
        eks.media.markers(e.media(DRUMS), OnsetMarkers(name="drums"))
        eks.media.markers(e.media(VOCALS), STTMarkers(name="vo"))
        eks.media.markers(e.media(VOICE), STTMarkers(name="talk"))
        e.track("main").clip(e.media(FOOTAGE), start=t("marker:drums#0"))

    plan = eks.plan(distinct)
    assert eks.validate(plan).ok, eks.validate(plan).text()
    data = json.loads(plan.to_json())
    analyses = [node for node in data["nodes"] if node["op"] == "media.markers"]
    analyses[1]["args"]["sources"] = [{"kind": "onset_markers", "name": "drums"}]
    analyses[2]["args"]["sources"] = [{"kind": "stt_markers", "name": "vo"}, {"kind": "silence_markers", "name": "vo"}]
    report = eks.validate(rehashed(data))
    found = [entry for entry in codes(report) if entry[0] == "markers.duplicate"]
    assert found == [
        ("markers.duplicate", analyses[1]["id"], "sources.0.name"),
        ("markers.duplicate", analyses[2]["id"], "sources.1.name"),
    ]


def test_a_scene_embedded_twice_takes_its_analysis_name_as_an_input(eks_registry: Registry) -> None:
    @eks.scene(name="fixed_pulse", kind="gfx", size=(1080, 1920))
    def fixed_pulse(sc: eks.GfxScene, clip: eks.Media) -> None:
        eks.media.markers(clip, OnsetMarkers(name="hits"))
        dot = sc.ellipse(id="dot", width=80.0, height=80.0)
        sc.on(t.beats("hits")[0:4], dot.pulse(amount=1.3, duration=t("200ms")))

    @eks.edit(name="fixed_twice", fps=30, size=(1080, 1920), duration=t("10s"))
    def fixed_twice(e: eks.Edit) -> None:
        e.track("left").clip(e.gfx(fixed_pulse, clip=DRUMS))
        e.track("right").clip(e.gfx(fixed_pulse, clip=VOCALS))

    with pytest.raises(TraceError, match="embedding #2 of scene 'fixed_pulse'.*must take the name of its analysis"):
        eks.plan(fixed_twice)

    @eks.scene(name="named_pulse", kind="gfx", size=(1080, 1920))
    def named_pulse(sc: eks.GfxScene, clip: eks.Media, hits: str = "hits") -> None:
        eks.media.markers(clip, OnsetMarkers(name=hits))
        dot = sc.ellipse(id="dot", width=80.0, height=80.0)
        sc.on(t.beats(hits)[0:4], dot.pulse(amount=1.3, duration=t("200ms")))

    @eks.edit(name="named_twice", fps=30, size=(1080, 1920), duration=t("10s"))
    def named_twice(e: eks.Edit) -> None:
        e.track("left").clip(e.gfx(named_pulse, clip=DRUMS, hits="drums"))
        e.track("right").clip(e.gfx(named_pulse, clip=VOCALS, hits="vocals"))
        e.render_settings(encoder="libx264")

    plan = eks.plan(named_twice)
    assert eks.validate(plan).ok, eks.validate(plan).text()
    by_id = {node.id: node for node, _ancestors in plan.walk()}
    names = [[args(by_id[after])["sources"][0]["name"] for after in render.after if by_id[after].op == "media.markers"]
             for render in nodes_of(plan, "gfx.render")]
    assert names == [["drums"], ["vocals"]]


def test_an_unqualified_word_needs_exactly_one_transcript(eks_registry: Registry) -> None:
    def words_plan(name: str, sources: tuple[str, ...]) -> Plan:
        @eks.edit(name=name, fps=30, size=(1080, 1920), duration=t("10s"))
        def edit(e: eks.Edit) -> None:
            for index, source in enumerate(sources):
                eks.media.markers(e.media(f"artifact:VO{index}@sha256:" + "e" * 64), STTMarkers(name=source))
            e.track("main").clip(e.media(FOOTAGE), start=t("word:'merhaba'"))
            e.track("named").clip(e.media(FOOTAGE), start=t(f"word:{sources[0]}:'merhaba'"))

        return eks.plan(edit)

    assert eks.validate(words_plan("one_transcript", ("vo",))).ok
    report = eks.validate(words_plan("two_transcripts", ("vo", "talk")))
    assert [code for code, _node, _field in codes(report)] == ["time.unresolvable"]
    assert "words of one speech-to-text source, but the plan has 2" in report.text()
    assert "word:talk:'text'" in report.text()


# tempo: external only on scenes, and a tempo source must give a beat grid


def test_external_tempo_is_honoured_only_on_scene_plans(eks_registry: Registry) -> None:
    @eks.edit(name="external_edit", fps=30, size=(1080, 1920), duration=t("10s"))
    def external_edit(e: eks.Edit) -> None:
        footage = e.media(FOOTAGE)
        e.track("a").clip(footage, start=t("cue:missing"))
        e.track("b").clip(footage, start=t("marker:nothing#3"))
        e.track("c").clip(footage, start=t("word:'nowhere'"))
        e.track("d").clip(footage, start=t("bar:2"))

    plan = eks.plan(external_edit)
    before = codes(eks.validate(plan))
    assert [code for code, _node, _field in before] == ["time.unresolvable"] * 4
    data = json.loads(plan.to_json())
    data["clock"]["tempo"] = {"external": True}
    after = codes(eks.validate(rehashed(data)))
    assert after == [("time.unresolvable", None, "clock.tempo.external"), *before]

    @eks.scene(name="external_scene", kind="gfx", size=(1080, 1920), duration=t("4s"), fps=30)
    def external_scene(sc: eks.GfxScene) -> None:
        dot = sc.ellipse(id="dot", width=80.0, height=80.0)
        sc.on(t.beats("downbeat")[0:4], dot.pulse(amount=1.3))

    scene = eks.plan(external_scene)
    assert scene.clock.tempo is not None and scene.clock.tempo.external
    assert eks.validate(scene).ok, eks.validate(scene).text()


def tempo_plan(name: str, *streams: Any) -> Plan:
    @eks.edit(name=name, fps=30, size=(1080, 1920), duration=t("10s"))
    def edit(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg").beats(*streams)
        footage = e.media(FOOTAGE)
        e.track("bars").clip(footage, start=t("bar:2"))
        e.track("downbeats").clip(footage, start=t("downbeat:2"))

    return eks.plan(edit)


def test_a_tempo_source_must_track_beats(eks_registry: Registry) -> None:
    plan = tempo_plan("tempo_ok")
    assert eks.validate(plan).ok, eks.validate(plan).text()
    data = json.loads(plan.to_json())
    source = next(node for node in data["nodes"] if node["id"] == data["clock"]["tempo"]["source"])
    source["args"]["sources"] = [{"kind": "onset_markers", "name": "song"}]
    report = eks.validate(rehashed(data))
    assert ("time.unresolvable", None, "clock.tempo.source") in codes(report)
    assert "needs exactly one beat_tracker with the 'beat' stream" in report.text()
    assert "'bar:2' needs a tempo" in report.text()
    source["args"]["sources"] = [
        {"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"]},
        {"kind": "beat_tracker", "name": "song_alt", "streams": ["beat", "downbeat"]},
    ]
    report = eks.validate(rehashed(data))
    assert ("time.unresolvable", None, "clock.tempo.source") in codes(report)
    assert "but it has 2 ('song', 'song_alt')" in report.text()


def test_track_beats_without_beat_cannot_become_the_tempo_source(eks_registry: Registry) -> None:
    with pytest.raises(TraceError, match="a tempo needs the 'beat' stream"):
        tempo_plan("kick_only", "kick")

    @eks.edit(name="kick_second", fps=30, size=(1080, 1920), duration=t("10s"))
    def kick_second(e: eks.Edit) -> None:
        e.audio.track("music/song.ogg").beats()
        e.audio.track(e.media(DRUMS), name="drums").beats("kick")
        e.track("main").clip(e.media(FOOTAGE), start=t("marker:drums.kick#1"))

    plan = eks.plan(kick_second)
    tempo = plan.clock.tempo
    assert tempo is not None
    source = next(node for node in nodes_of(plan, "media.markers") if node.id == tempo.source)
    assert args(source)["sources"][0]["name"] == "song"
    assert eks.validate(plan).ok, eks.validate(plan).text()


def test_downbeats_on_an_analysed_tempo_need_the_downbeat_stream(eks_registry: Registry) -> None:
    report = eks.validate(tempo_plan("beat_only", "beat"))
    assert [code for code, _node, _field in codes(report)] == ["time.unresolvable"]
    assert "'downbeat:2' needs downbeats, which the tempo source's beat tracker 'song' does not track" in report.text()
    assert t("downbeat:2").needs().downbeat
    assert t.beats("downbeat")[0:4].needs().downbeat
    assert not t("bar:2").needs().downbeat
