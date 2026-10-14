import textwrap
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

import eks
from eks import by, t
from eks.discovery import load_file
from eks.editing import EditBuilder
from eks.ref import node_of, type_of
from eks.tracing import TraceError, current
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.ops.edit import Timeline
from eks_harness.ir.ops.overlay import Callout, Caption, OverlayBase
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, RegistryError, contributing
from eks_harness.ir.target import TargetError
from eks_harness.ir.time import Time, TimeError

DESIGN_EDIT = '''\
import eks
from eks import t
from eks_harness.ir.ops.overlay import Caption


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def demo(e: eks.Edit):
    clip = e.media("artifact:VID123")
    music = e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")
    v = e.track("main")
    (v.clip(clip, start=t("0s"), src=t.span(t("2s"), t("14s")))
     .effect(eks.fx.zoom(1.0, 1.08, ease="out_cubic"))
     .transition_out(eks.tx.crossfade(t("8f"))))
    e.track("overlays").overlay(Caption(text="Kontör işlemleri", window=t.span(t("1s"), t("4s"))))
    e.expect.cut(at=t("beat:8"))
'''

EDITED_EDIT = '''\
import eks
from eks import t
from eks_harness.ir.ops.overlay import Caption

# the same edit with blank lines and comments added


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def demo(e: eks.Edit):

    # the screen recording
    clip = e.media("artifact:VID123")

    music = e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")  # the song

    v = e.track("main")


    (v.clip(clip, start=t("0s"), src=t.span(t("2s"), t("14s")))

     .effect(eks.fx.zoom(1.0, 1.08, ease="out_cubic"))  # slow push in
     .transition_out(eks.tx.crossfade(t("8f"))))
    e.track("overlays").overlay(Caption(text="Kontör işlemleri", window=t.span(t("1s"), t("4s"))))

    e.expect.cut(at=t("beat:8"))
'''


def plan_source(path: Path, source: str) -> Plan:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    entries = load_file(path)
    assert len(entries) == 1
    return eks.plan(entries[0].fn)


def walk(plan: Plan) -> list[Node]:
    return [node for node, _ in plan.walk()]


def only(plan: Plan, op: str) -> Node:
    found = [node for node in walk(plan) if node.op == op]
    assert len(found) == 1, [node.op for node in walk(plan)]
    return found[0]


def timeline_of(plan: Plan) -> dict[str, Any]:
    timeline: Any = only(plan, "edit.render").args["timeline"]
    return timeline


@pytest.fixture
def design(tmp_path: Path) -> Plan:
    return plan_source(tmp_path / "demo.py", DESIGN_EDIT)


def test_design_edit_nodes_in_walk_order(design: Plan) -> None:
    nodes = walk(design)
    assert [node.op for node in nodes] == [
        "media.source", "media.source", "media.markers", "audio.mix", "edit.render", "check.media"]
    video, song, markers, mix, render, check = nodes
    assert video.args == {"uri": "artifact:VID123"}
    assert song.args == {"uri": "file:music/song.ogg"}
    assert markers.args == {
        "media": {"$ref": song.id, "path": []},
        "sources": [{"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"], "backend": "auto"}],
    }
    mix_args: Any = mix.args["mix"]
    assert [track["name"] for track in mix_args["tracks"]] == ["song"]
    assert mix_args["tracks"][0]["gain"] == -6
    assert mix_args["tracks"][0]["media"] == {"$ref": song.id, "path": []}
    assert render.out == "media"
    assert check.args == {
        "media": {"$ref": render.id, "path": []}, "kind": "cut", "params": {"threshold": 0.1}, "at": "beat:8",
        "soft": False}
    assert check.after == [render.id, markers.id]
    assert all(node.uses == [] for node in nodes)
    assert design.resources == []


def test_design_edit_timeline(design: Plan) -> None:
    nodes = walk(design)
    video, mix = nodes[0], nodes[3]
    timeline = timeline_of(design)
    assert timeline["fps"] == 30
    assert timeline["size"] == [1080, 1920]
    assert timeline["seed"] == 7
    assert timeline["audio"] == {"$ref": mix.id, "path": []}
    main, overlays = timeline["tracks"]
    assert (main["name"], main["z"], overlays["name"], overlays["z"]) == ("main", 0, "overlays", 1)
    (clip,) = main["clips"]
    assert clip["id"] == "main/0"
    assert clip["media"] == {"$ref": video.id, "path": []}
    assert clip["start"] == "0s"
    assert clip["src"] == {"start": "2s", "end": "14s"}
    assert clip["effects"] == [{
        "kind": "zoom",
        "scale": {"keys": [
            {"t": "start", "v": 1.0, "interp": "linear"},
            {"t": "end", "v": 1.08, "ease": {"kind": "out_cubic"}, "interp": "linear"},
        ]},
        "cx": 0.5, "cy": 0.5,
    }]
    assert clip["transition_out"] == {"kind": "crossfade", "duration": "8f"}
    assert "transition_in" not in clip
    assert overlays["clips"] == []
    assert overlays["overlays"] == [{
        "kind": "caption", "window": {"start": "1s", "end": "4s"}, "keep": False, "emphasis": "none",
        "text": "Kontör işlemleri", "position": "bottom"}]
    assert main["overlays"] == []


def test_design_edit_clock(design: Plan) -> None:
    markers = walk(design)[2]
    assert design.clock.fps == 30
    assert design.clock.seed == 7
    assert design.clock.tempo is not None
    assert design.clock.tempo.source == markers.id
    assert design.clock.tempo.bpm is None
    assert design.kind == "edit"
    assert design.name == "demo"


def test_design_edit_is_deterministic(tmp_path: Path) -> None:
    first = plan_source(tmp_path / "demo.py", DESIGN_EDIT)
    second = plan_source(tmp_path / "demo.py", DESIGN_EDIT)
    assert first.to_json().encode() == second.to_json().encode()
    assert first.hash == second.hash
    entries = load_file(tmp_path / "demo.py")
    third = eks.plan(entries[0].fn)
    assert third.to_json().encode() == first.to_json().encode()
    assert [clip["id"] for track in timeline_of(first)["tracks"] for clip in track["clips"]] == ["main/0"]


def test_blank_lines_and_comments_keep_ids_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "demo.py"
    before = plan_source(path, DESIGN_EDIT)
    after = plan_source(path, EDITED_EDIT)
    assert [node.id for node in walk(before)] == [node.id for node in walk(after)]
    assert [node.hash for node in walk(before)] == [node.hash for node in walk(after)]
    assert before.hash == after.hash
    assert [node.src for node in walk(before)] != [node.src for node in walk(after)]
    assert timeline_of(before) == timeline_of(after)


def test_track_z_order_get_or_create_and_configure() -> None:
    seen: dict[str, Any] = {}

    @eks.edit(fps=30, size=(1080, 1920))
    def tracks(e: eks.Edit) -> None:
        base = e.track("base")
        top = e.track("top", z=10, blend="screen", opacity=0.5)
        again = e.track("base", blend="multiply")
        seen["same"] = again is base
        e.track("middle")
        top.configure(opacity=eks.keys({t("0s"): 0.0, t("1s"): 1.0}))
        with pytest.raises(TypeError, match="z expects an int"):
            base.configure(z=True)
        with pytest.raises(ValidationError):
            base.configure(blend="glow")  # type: ignore[arg-type]
        with pytest.raises(TraceError, match="track name"):
            e.track("bad name")

    timeline = timeline_of(eks.plan(tracks))
    assert seen["same"]
    assert [(track["name"], track["z"], track["blend"]) for track in timeline["tracks"]] == [
        ("base", 0, "multiply"), ("top", 10, "screen"), ("middle", 2, "normal")]
    assert timeline["tracks"][1]["opacity"]["keys"][1]["v"] == 1.0


def test_clips_ids_and_fields() -> None:
    seen: dict[str, Any] = {}

    @eks.edit(fps=25, size=(1920, 1080), seed=3, duration=t("10s"), background="#112233")
    def clips(e: eks.Edit) -> None:
        main = e.track("main")
        first = main.clip("clips/a.mp4", speed=2.0, fit="contain", audio=True, gain=-3.0)
        second = main.clip(e.media("clips/b.mp4"), start=t("4s"))
        seen["ids"] = [first.id, second.id, e.track("broll").clip("clips/c.mp4").id]
        second.set(x=0.25, scale=eks.keys({t.start: 1.0, t.end: 1.2}), rotation=eks.curves.sine(1.0))
        second.transition_in(eks.tx.dip_to_black(t("6f"))).effect(eks.fx.blur(2.0), eks.fx.invert())
        second.effect(eks.fx.vignette())
        with pytest.raises(TypeError, match="cannot be set"):
            second.set(id="x/1")  # pyright: ignore[reportCallIssue]
        with pytest.raises(TypeError, match="cannot be set"):
            second.set(volume=1.0)  # pyright: ignore[reportCallIssue]
        with pytest.raises(TypeError, match="pass t"):
            second.set(start="1s")  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="Span"):
            second.set(src=(t("0s"), t("1s")))  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="effects are eks.fx"):
            second.effect(eks.tx.cut())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="transition"):
            second.transition_out(eks.fx.invert())  # type: ignore[arg-type]

    plan = eks.plan(clips)
    timeline = timeline_of(plan)
    assert timeline["fps"] == 25
    assert timeline["duration"] == "10s"
    assert timeline["background"] == "#112233"
    assert "audio" not in timeline
    main = timeline["tracks"][0]
    first, second = main["clips"]
    assert (first["speed"], first["fit"], first["audio"], first["gain"]) == (2.0, "contain", True, -3.0)
    assert second["start"] == "4s"
    assert second["x"] == 0.25
    assert second["rotation"] == {"curve": {"kind": "sine", "freq_hz": 1.0, "phase": 0.0, "amp": 1.0,
                                            "offset": 0.0}, "scale": 1.0, "offset": 0.0}
    assert [effect["kind"] for effect in second["effects"]] == ["blur", "invert", "vignette"]
    assert second["transition_in"] == {"kind": "dip_to_black", "duration": "6f"}
    assert plan.clock.tempo is None
    assert plan.clock.duration == t("10s")


def test_clip_ids_are_stable_across_plans() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def ids(e: eks.Edit) -> None:
        for name in ("a", "b"):
            track = e.track(name)
            for index in range(3):
                track.clip(f"clips/{name}{index}.mp4")

    first = timeline_of(eks.plan(ids))
    second = timeline_of(eks.plan(ids))
    expected = [f"{name}/{index}" for name in ("a", "b") for index in range(3)]
    assert [clip["id"] for track in first["tracks"] for clip in track["clips"]] == expected
    assert first == second


def test_cue_adds_a_clock_cue() -> None:
    seen: dict[str, Any] = {}

    @eks.edit(fps=30, size=(1080, 1920))
    def cued(e: eks.Edit) -> None:
        seen["logo"] = e.cue("logo", t("2s"))
        seen["again"] = e.cue("logo", t("2s"))
        e.cue("drop", t("bar:4"))
        with pytest.raises(TraceError, match="already defined"):
            e.cue("logo", t("3s"))
        with pytest.raises(TraceError, match="another cue"):
            e.cue("late", t("cue:logo") + t("1s"))
        with pytest.raises(TypeError, match="pass t"):
            e.cue("x", "1s")  # type: ignore[arg-type]
        e.track("main").clip("clips/a.mp4", start=seen["logo"])

    plan = eks.plan(cued)
    assert seen["logo"] == t.cue("logo") == seen["again"]
    assert plan.clock.cues == {"logo": t("2s"), "drop": t("bar:4")}
    assert timeline_of(plan)["tracks"][0]["clips"][0]["start"] == "cue:logo"


def test_render_settings() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def settings(e: eks.Edit) -> None:
        e.render_settings(quality="draft")
        e.render_settings(crf=18, encoder="libx264")
        with pytest.raises(ValidationError):
            e.render_settings(quality="best")  # pyright: ignore[reportArgumentType]
        with pytest.raises(ValidationError):
            e.render_settings(bogus=1)  # pyright: ignore[reportCallIssue]

    render = timeline_of(eks.plan(settings))["render"]
    assert render["quality"] == "draft"
    assert render["crf"] == 18
    assert render["encoder"] == "libx264"
    assert render["preset"] == "tiktok-1080-h264"


def test_output_is_the_render_ref_after_finalization() -> None:
    holder: dict[str, EditBuilder] = {}

    @eks.edit(fps=30, size=(1080, 1920))
    def output(e: eks.Edit) -> None:
        holder["e"] = e
        with pytest.raises(TraceError, match="once the edit is finalized"):
            _ = e.output
        e.track("main").clip("clips/a.mp4")

    plan = eks.plan(output)
    edit = holder["e"]
    render = only(plan, "edit.render")
    assert node_of(edit.output) == render.id
    assert type_of(edit.output) == "media"
    assert edit.finalize() is edit.output
    with pytest.raises(TraceError, match="already finalized"):
        edit.track("late")
    with pytest.raises(TraceError, match="already finalized"):
        edit.expect.cut(at=t("1s"))


def test_returning_the_output_inside_the_entry_fails() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def early(e: eks.Edit) -> Any:
        return e.output

    with pytest.raises(TraceError, match="once the edit is finalized"):
        eks.plan(early)


def test_overlays_are_checked_on_the_video_surface(eks_registry: Registry) -> None:
    @eks.overlay_kind(live="badge-live", description="A live-only badge")
    class Badge(OverlayBase, frozen=True):
        kind: Literal["badge"] = "badge"

    @eks.edit(fps=30, size=(1080, 1920))
    def overlays(e: eks.Edit) -> None:
        track = e.track("overlays")
        track.overlay(Callout(anchor=by.text("Toplam"), text="Anlık kur", window=t.span(t("1s"), t("2s"))))
        track.overlay(Callout(anchor=by.point(120, 480), text="Burada"))
        with pytest.raises(TargetError, match="video"):
            track.overlay(Callout(anchor=by.css("#total"), text="x"))
        with pytest.raises(TraceError, match="no video renderer"):
            track.overlay(Badge())
        with pytest.raises(TypeError, match="overlay models"):
            track.overlay("Kontör")  # type: ignore[arg-type]

    assert eks_registry.catalog("overlay", "badge").renderers == {"live": "badge-live"}
    track = timeline_of(eks.plan(overlays))["tracks"][0]
    assert [overlay["anchor"] for overlay in track["overlays"]] == [
        {"by": "text", "args": {"text": "Toplam", "exact": True}},
        {"by": "point", "args": {"x": 120.0, "y": 480.0}},
    ]


def test_every_expectation_kind_records_a_media_check() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def checks(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4")
        e.expect.cut(at=t("beat:8"), tolerance=t("2f"), threshold=0.2, label="cut on 8")
        e.expect.beats("downbeat", at=t.span(t("0s"), t("8s")), min_ratio=0.9, soft=True)
        e.expect.onset(at=t("1s"), sensitivity=0.7)
        e.expect.loudness(-14.0, true_peak=-1.0)
        e.expect.text("Kontör", at=t.span(t("1s"), t("4s")), region=by.rect(0, 0, 1080, 400))
        e.expect.motion(min=0.1)
        e.expect.black(longest=t("100ms"))
        e.expect.frozen(at=t.span(t("0s"), t("5s")))
        e.expect.safe_area(region=by.test_id("cta"))
        e.expect.template("logo.png", at=t("end"), threshold=0.9)
        with pytest.raises(TypeError, match="pass t"):
            e.expect.cut(at="beat:8")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="Span"):
            e.expect.motion(at=t("1s"))  # type: ignore[arg-type]
        with pytest.raises(TargetError, match="video"):
            e.expect.text("x", at=t("1s"), region=by.css(".x"))
        e.expect.text("shorthand", at=t("1s"), region="#cta")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            e.expect.beats(min_ratio=2.0)

    plan = eks.plan(checks)
    render = only(plan, "edit.render")
    found = [node for node in walk(plan) if node.op == "check.media"]
    assert [node.args["kind"] for node in found] == [
        "cut", "beats", "onset", "loudness", "text", "motion", "black", "frozen", "safe_area", "template", "text"]
    assert all(node.args["media"] == {"$ref": render.id, "path": []} for node in found)
    args: list[Any] = [node.args for node in found]
    cut, beats, onset, loudness, text, motion, black, frozen, safe_area, template, shorthand = args
    assert shorthand["params"]["region"] == {"by": "test_id", "args": {"id": "cta"}}
    assert cut == {"media": {"$ref": render.id, "path": []}, "kind": "cut", "params": {"threshold": 0.2},
                   "at": "beat:8", "tolerance": "2f", "label": "cut on 8", "soft": False}
    assert found[0].label == "cut on 8"
    assert beats["params"] == {"stream": "downbeat", "min_ratio": 0.9}
    assert beats["at"] == {"start": "0s", "end": "8s"}
    assert beats["soft"] is True
    assert onset["params"] == {"sensitivity": 0.7}
    assert loudness["params"] == {"lufs": -14.0, "tolerance": 1.0, "true_peak": -1.0}
    assert "at" not in loudness
    assert text["params"] == {"text": "Kontör", "region": {"by": "rect", "args": {
        "x": 0.0, "y": 0.0, "w": 1080.0, "h": 400.0}}, "engine": "auto", "languages": ["tr-TR", "en-US"]}
    assert motion["params"] == {"min": 0.1}
    assert black["params"] == {"longest": "0.1s"}
    assert frozen["params"] == {}
    assert safe_area["params"]["region"] == {"by": "test_id", "args": {"id": "cta"}}
    assert safe_area["params"]["margins"] == [0.1, 0.05, 0.2, 0.05]
    (logo,) = [node for node in walk(plan) if node.op == "media.source" and node.args["uri"] == "file:logo.png"]
    assert logo.args == {"uri": "file:logo.png", "kind": "image"}
    assert template["params"] == {"template": {"$ref": logo.id, "path": []}, "threshold": 0.9}
    assert template["at"] == "end"


def test_expectation_lengths_must_be_positive_unanchored_durations() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def lengths(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4")
        bad_lengths = [t("beat:4"), t("bar:2"), t("0s"), t("0s") - t("1s")]
        for length in bad_lengths:
            # tolerance of every kind, through the named builders and expect.use
            with pytest.raises(TimeError):
                e.expect.cut(at=t("1s"), tolerance=length)
            with pytest.raises(TimeError):
                e.expect.beats(tolerance=length)
            with pytest.raises(TimeError):
                e.expect.onset(at=t("1s"), tolerance=length)
            with pytest.raises(TimeError):
                e.expect.use("cut", at=t("1s"), tolerance=length)
            with pytest.raises(TimeError):
                e.expect.black(longest=length)
            with pytest.raises(TimeError):
                e.expect.frozen(longest=length)
        with pytest.raises(TimeError, match="point in time"):
            e.expect.cut(at=t("1s"), tolerance=t("beat:4"))
        with pytest.raises(TimeError, match="positive length"):
            e.expect.black(longest=t("0s"))
        # beat and frame counts are lengths
        e.expect.cut(at=t("1s"), tolerance=t("1b"))
        e.expect.frozen(longest=t("8f"))

    plan = eks.plan(lengths)
    checks: list[Any] = [node.args for node in walk(plan) if node.op == "check.media"]
    assert [check["kind"] for check in checks] == ["cut", "frozen"]
    assert checks[0]["tolerance"] == "1b"
    assert checks[1]["params"] == {"longest": "8f"}


def test_expectation_ids_survive_inserting_another(eks_registry: Registry, tmp_path: Path) -> None:
    source = '''\
    import eks
    from eks import t


    @eks.edit(fps=30, size=(1080, 1920))
    def checked(e: eks.Edit):
        e.track("main").clip("clips/a.mp4")
        {extra}
        e.expect.cut(at=t("beat:8"))
        e.expect.black()
    '''
    path = tmp_path / "checked.py"
    before = plan_source(path, source.replace("{extra}", ""))
    after = plan_source(path, source.replace("{extra}", 'e.expect.onset(at=t("1s"))'))
    ids_before = {node.args["kind"]: node.id for node in walk(before) if node.op == "check.media"}
    ids_after = {node.args["kind"]: node.id for node in walk(after) if node.op == "check.media"}
    assert set(ids_after) == {"cut", "black", "onset"}
    assert ids_after["cut"] == ids_before["cut"]
    assert ids_after["black"] == ids_before["black"]


def test_unregistered_effect_in_a_clip_fails() -> None:
    class Rogue(EffectBase, frozen=True):
        kind: Literal["rogue"] = "rogue"

    @eks.edit(fps=30, size=(1080, 1920))
    def rogue(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4").effect(Rogue())

    with pytest.raises(RegistryError, match="unknown effect kind 'rogue'"):
        eks.plan(rogue)


def test_an_empty_edit_renders_an_empty_timeline() -> None:
    @eks.edit(fps="30000/1001", size=(1080, 1920))
    def empty(e: eks.Edit) -> None:
        return None

    plan = eks.plan(empty)
    assert [node.op for node in walk(plan)] == ["edit.render"]
    timeline = timeline_of(plan)
    assert timeline["fps"] == "30000/1001"
    assert timeline["tracks"] == []
    assert only(plan, "edit.render").id == eks.plan(empty).nodes[0].id
    assert isinstance(Caption(text="x"), OverlayBase)


def test_plugin_media_checks_are_queued_through_expect(eks_registry: Registry) -> None:
    with contributing("sharpness-check", "0.3.0"):
        @eks.check("sharpness", family="media", description="Frames are in focus")
        class Sharpness(eks.Args):
            min_score: float = 0.5

    @eks.edit(fps=30, size=(1080, 1920))
    def sharp(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4")
        e.expect.sharpness(at=t.span(t("0s"), t("2s")), tolerance=t("2f"), min_score=0.8, label="in focus")
        e.expect.use("sharpness", at=t("1s"), soft=True)
        e.expect.use("cut", at=t("beat:8"), threshold=0.3)
        with pytest.raises(AttributeError, match="did you mean 'sharpness'"):
            e.expect.sharpnes  # noqa: B018
        with pytest.raises(AttributeError, match="media checks: "):
            e.expect.zzz  # noqa: B018
        with pytest.raises(ValidationError):
            e.expect.sharpness(at=t("1s"), unknown=1)
        with pytest.raises(TypeError, match="pass t"):
            e.expect.sharpness(at="1s")
        with pytest.raises(RegistryError, match="unknown check kind"):
            e.expect.use("nonexistent")

    plan = eks.plan(sharp)
    render = only(plan, "edit.render")
    found: list[Any] = [node.args for node in walk(plan) if node.op == "check.media"]
    assert found == [
        {"media": {"$ref": render.id, "path": []}, "kind": "sharpness", "params": {"min_score": 0.8},
         "at": {"start": "0s", "end": "2s"}, "tolerance": "2f", "label": "in focus", "soft": False},
        {"media": {"$ref": render.id, "path": []}, "kind": "sharpness", "params": {"min_score": 0.5},
         "at": "1s", "soft": True},
        {"media": {"$ref": render.id, "path": []}, "kind": "cut", "params": {"threshold": 0.3},
         "at": "beat:8", "soft": False},
    ]
    assert [(item.plugin, item.version) for item in plan.requires] == [("sharpness-check", "0.3.0")]


def test_expect_use_applies_time_and_target_strictness(eks_registry: Registry) -> None:
    with contributing("focus-check", "1.0.0"):
        @eks.check("focus", family="media", description="A region is in focus")
        class Focus(eks.Args):
            region: eks.Target
            extra: tuple[eks.Target, ...] = ()
            hold: Time | None = None
            note: str | eks.Target | None = None

    @eks.edit(fps=30, size=(1080, 1920))
    def strict(e: eks.Edit) -> None:
        e.track("main").clip("clips/a.mp4")
        with pytest.raises(TypeError, match="pass t"):
            e.expect.use("black", longest="100ms")
        with pytest.raises(TypeError, match="bare numbers are not times"):
            e.expect.black(longest=1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="pass t"):
            e.expect.focus(at=t("1s"), region="#cta", hold="1s")
        e.expect.use("text", at=t("1s"), text="hi", region="#cta")
        e.expect.focus(at=t("2s"), region="#cta", extra=["#a", by.text("Ok")], hold=t("500ms"), note="plain")
        with pytest.raises(TargetError, match=r"expect\.focus region .*not supported on video"):
            e.expect.focus(at=t("3s"), region=by.css("#total"))
        with pytest.raises(TargetError, match=r"expect\.focus extra\[1\] .*not supported on video"):
            e.expect.use("focus", at=t("3s"), region="#cta", extra=["#a", by.css("#total")])

    plan = eks.plan(strict)
    checks: list[Any] = [node.args for node in walk(plan) if node.op == "check.media"]
    assert [check["kind"] for check in checks] == ["text", "focus"]
    assert checks[0]["params"] == {"text": "hi", "region": {"by": "test_id", "args": {"id": "cta"}},
                                   "engine": "auto", "languages": ["tr-TR", "en-US"]}
    assert checks[1]["params"] == {
        "region": {"by": "test_id", "args": {"id": "cta"}},
        "extra": [{"by": "test_id", "args": {"id": "a"}}, {"by": "text", "args": {"text": "Ok", "exact": True}}],
        "hold": "0.5s", "note": "plain"}


def test_non_media_checks_cannot_be_expected_from_an_edit(eks_registry: Registry) -> None:
    @eks.check("tidy", family="checkpoint", description="A checkpoint check")
    class Tidy(eks.Args):
        pass

    @eks.edit(fps=30, size=(1080, 1920))
    def tidy(e: eks.Edit) -> None:
        with pytest.raises(AttributeError, match="no media check 'tidy'"):
            e.expect.tidy  # noqa: B018
        with pytest.raises(TraceError, match="not a media check"):
            e.expect.use("tidy")

    eks.plan(tidy)


class Flag(eks.Args):
    pass


def test_edit_builders_refuse_calls_inside_when_and_each_bodies() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def branched(e: eks.Edit) -> None:
        main = e.track("main")
        clip = main.clip("clips/a.mp4")
        song = e.audio.track("music/song.ogg")
        if not current().registry.has_op("demo.flag"):
            eks.op("demo", name="flag", out="bool", description="A flag known when the plan runs")(Flag)
        flag = current().record("demo.flag")
        with eks.when(flag):
            with pytest.raises(TraceError, match="edits are static timelines"):
                main.clip("clips/b.mp4")
            with pytest.raises(TraceError, match="edits are static timelines"):
                e.track("other")
            with pytest.raises(TraceError, match="edits are static timelines"):
                clip.effect(eks.fx.blur(2.0))
            with pytest.raises(TraceError, match="edits are static timelines"):
                main.overlay(Caption(text="x"))
            with pytest.raises(TraceError, match="edits are static timelines"):
                e.expect.cut(at=t("beat:8"))
            with pytest.raises(TraceError, match="edits are static timelines"):
                e.cue("drop", t("2s"))
            with pytest.raises(TraceError, match="an audio mix is static"):
                e.audio.track("music/other.ogg")
            with pytest.raises(TraceError, match="an audio mix is static"):
                song.beats()
            with pytest.raises(TraceError, match="an audio mix is static"):
                e.audio.master(loudness=-14.0)
        with eks.each([1, 2]), pytest.raises(TraceError, match="edits are static timelines"):
            main.clip("clips/c.mp4")

    plan = eks.plan(branched)
    assert [node.op for node in walk(plan) if node.op in ("check.media", "media.markers")] == []
    assert plan.clock.tempo is None
    assert [clip["id"] for clip in timeline_of(plan)["tracks"][0]["clips"]] == ["main/0"]


def test_timeline_model_invariants() -> None:
    clip: dict[str, Any] = {"id": "a/0", "media": {"$ref": "n0000000001", "path": []}}
    base: dict[str, Any] = {"fps": 30, "size": [1080, 1920]}
    assert Timeline.model_validate({**base, "tracks": [{"name": "a", "clips": [clip]}]}).tracks[0].clips[0].id == "a/0"
    with pytest.raises(ValidationError, match="two tracks named 'a'"):
        Timeline.model_validate({**base, "tracks": [{"name": "a"}, {"name": "a"}]})
    with pytest.raises(ValidationError, match="greater than 0"):
        Timeline.model_validate({"fps": 30, "size": [0, -5]})
    with pytest.raises(ValidationError, match="clip ids are 'a/<index>'"):
        Timeline.model_validate({**base, "tracks": [{"name": "a", "clips": [{**clip, "id": "b/0"}]}]})
    with pytest.raises(ValidationError, match="two clips with the id 'a/0'"):
        Timeline.model_validate({**base, "tracks": [{"name": "a", "clips": [clip, clip]}]})
    with pytest.raises(ValidationError, match="positive length"):
        Timeline.model_validate({**base, "duration": "0s"})
