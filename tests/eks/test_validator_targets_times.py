from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import by, t
from eks.planning import trace
from eks.ref import node_of
from eks.tracing import SessionCore, TraceContext
from eks_harness.engine.validator import Issue, Report, validate
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.target import Platform
from eks_harness.ir.time import TempoMap


def flow_plan(body: Callable[[SessionCore], Any], *, platform: Platform = "web") -> Plan:
    # a frozen now keeps the clock.now warning out of these reports
    @eks.flow(app="web-app", platform=platform, now="2026-01-15T09:00+03:00")
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def entry_plan(registry: Registry, kind: str, name: str, body: Callable[[TraceContext], Any]) -> Plan:
    return trace(registry.entry(kind, name), root=lambda ctx, _entry: ctx, registry=registry)


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def tap_with_timeout(timeout: Any) -> Callable[[SessionCore], None]:
    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.test_id("save"), "timeout": timeout})

    return body


def test_css_target_on_ios() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.css("li.row")})

    report = validate(flow_plan(body, platform="ios"))
    [issue] = issues_of(report, "target.surface")
    assert issue.field == "target"
    assert "'css' is not supported on ios" in issue.message
    assert issues_of(validate(flow_plan(body, platform="web")), "target.surface") == []


def test_within_modifier_and_nested_kind_on_the_surface() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.test_id("row").within(by.css(".dialog"))})

    found = issues_of(validate(flow_plan(body, platform="android")), "target.surface")
    assert len(found) == 1
    assert "'css'" in found[0].message


def test_unknown_target_kind() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.test_id("save")})

    plan = flow_plan(body)
    tap = by_op(plan, "ui.tap")[0]
    tap.args["target"] = {"by": "hologram", "args": {"id": "x"}}
    report = validate(plan)
    [issue] = issues_of(report, "target.kind")
    assert issue.field == "target.by"
    assert "'hologram'" in issue.message
    assert issues_of(report, "target.surface") == []
    tap.args["target"] = {"by": "test_id", "args": {"id": "x"}, "near": {"by": "hologram", "args": {}}}
    [issue] = issues_of(validate(plan), "target.kind")
    assert issue.field == "target.near.by"


def test_bad_target_args() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.test_id("save")})

    plan = flow_plan(body)
    assert issues_of(validate(plan), "target.args") == []
    tap = by_op(plan, "ui.tap")[0]
    tap.args["target"] = {"by": "rect", "args": {"x": 1, "y": 2, "w": -5, "h": 3}}
    [issue] = issues_of(validate(plan), "target.args")
    assert issue.field == "target.args"
    assert "w" in issue.message


def test_plugin_target_kind_is_known(eks_registry: Registry) -> None:
    @eks.target_kind("chip", surfaces=("web",), shorthand="chip")
    class ByChip(eks.Args):
        label: str

    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.custom("chip", label="Yeni")})

    assert validate(flow_plan(body)).ok
    found = issues_of(validate(flow_plan(body, platform="ios")), "target.surface")
    assert len(found) == 1
    assert eks_registry.catalog("target", "chip").model is ByChip


def annotate_body(anchor: Any) -> Callable[[SessionCore], None]:
    def body(s: SessionCore) -> None:
        shot = s.record_op("capture.shot", {"name": "liste"})
        s.ctx.record("overlay.annotate", {
            "artifact": shot,
            "overlays": [{"kind": "callout", "anchor": anchor, "text": "Burada"}],
        })

    return body


def test_annotate_anchors_are_checked_on_screenshots() -> None:
    plan = flow_plan(annotate_body(by.object("Phone")))
    annotate = by_op(plan, "overlay.annotate")[0]
    [issue] = issues_of(validate(plan), "target.surface")
    assert issue.node == annotate.id
    assert issue.field == "overlays.0.anchor"
    assert "screenshot" in issue.message
    report = validate(flow_plan(annotate_body(by.css(".total"))))
    assert report.ok, report.text()


def test_frames_need_fps() -> None:
    report = validate(flow_plan(tap_with_timeout(t("2f"))))
    [issue] = issues_of(report, "time.unresolvable")
    assert issue.field == "timeout"
    assert "'2f'" in issue.message
    assert "fps" in issue.message


def media_check_at(at: Any) -> Callable[[SessionCore], None]:
    # check.media takes a point in time, so it exercises resolution where timeouts (lengths) refuse anchors
    def body(s: SessionCore) -> None:
        clip = s.record_op("api.get", {"path": "/api/media"})
        s.record_op("check.media", {"media": clip, "kind": "cut", "at": at})

    return body


def test_timeouts_refuse_anchored_times() -> None:
    for anchored in (t("bar:1"), t("cue:x"), t.end):
        with pytest.raises(ValidationError, match="point in time"):
            flow_plan(tap_with_timeout(anchored))


def test_bars_need_a_tempo() -> None:
    [issue] = issues_of(validate(flow_plan(media_check_at(t("bar:1")))), "time.unresolvable")
    assert "tempo" in issue.message


def test_undeclared_cue() -> None:
    [issue] = issues_of(validate(flow_plan(media_check_at(t("cue:x")))), "time.unresolvable")
    assert "cue 'x'" in issue.message


def test_end_needs_a_window_outside_clip_local_contexts() -> None:
    [issue] = issues_of(validate(flow_plan(media_check_at(t.end))), "time.unresolvable")
    assert "'end'" in issue.message

    def body(s: SessionCore) -> None:
        s.record_op("overlay.show", {"overlay": {
            "kind": "caption", "text": "Kaydedildi", "window": t.span(t("1s"), t.end)}})

    assert validate(flow_plan(body)).ok


def test_marker_stream_without_markers_node() -> None:
    [issue] = issues_of(validate(flow_plan(media_check_at(t("marker:song.drop")))), "time.unresolvable")
    assert "marker stream 'song.drop'" in issue.message


def gfx_scene(at: Any) -> dict[str, Any]:
    return {
        "size": [1080, 1920],
        "layers": [{"kind": "text", "id": "title", "text": "EksenGOLD", "end": "end"}],
        "triggers": [{"at": at, "layer": "title", "action": {"kind": "pulse", "amount": 1.04, "duration": "0.25s"}}],
    }


def test_scene_plan_with_external_tempo(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def titles(sc: Any) -> None:
        sc.record("gfx.render", {"scene": gfx_scene("bar:1")})

    @eks.edit(fps=30, size=(1080, 1920))
    def plain(e: Any) -> None:
        e.record("gfx.render", {"scene": gfx_scene("bar:1")})

    scene = entry_plan(eks_registry, "scene", "titles", titles)
    assert scene.clock.tempo == TempoMap(external=True)
    report = validate(scene)
    assert report.ok, report.text()
    [issue] = issues_of(validate(entry_plan(eks_registry, "edit", "plain", plain)), "time.unresolvable")
    assert issue.field == "scene.triggers.0.at"


def test_edit_with_fps_and_a_beat_tracker(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920), duration=t("16s"))
    def demo(e: TraceContext) -> None:
        song = e.record("media.source", {"uri": "file:music/song.ogg", "kind": "audio"})
        markers = e.record("media.markers", {"media": song, "sources": [
            {"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat", "kick"]},
            {"kind": "momentum_markers", "name": "song_energy", "smoothing": t("1s"), "min_build": t("2s"),
             "min_section": t("4s"), "snap_window": t("0.5s")},
        ]})
        e.update_clock(tempo=TempoMap(source=node_of(markers)))
        e.record("check.media", {"media": song, "kind": "cut", "at": t("beat:8") + t("2f")})
        e.record("check.media", {"media": song, "kind": "cut", "at": t("marker:song_energy.drop")})
        e.record("check.media", {"media": song, "kind": "cut", "at": t("marker:kick#2")})
        e.record("gfx.render", {"scene": gfx_scene(t.beats("downbeat")[0:4])})
        e.record("gfx.render", {"scene": gfx_scene(t.beats("kick").every(2))})

    report = validate(entry_plan(eks_registry, "edit", "demo", demo))
    assert report.ok, report.text()


def test_edit_marker_streams_must_exist(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def demo(e: TraceContext) -> None:
        song = e.record("media.source", {"uri": "file:music/song.ogg", "kind": "audio"})
        markers = e.record("media.markers", {"media": song, "sources": [{"kind": "beat_tracker", "name": "song"}]})
        e.update_clock(tempo=TempoMap(source=node_of(markers)))
        e.record("check.media", {"media": song, "kind": "cut", "at": t("marker:song.drop")})
        e.record("check.media", {"media": song, "kind": "cut", "at": t("marker:snare")})
        e.record("check.media", {"media": song, "kind": "cut", "at": t("word:'EksenGOLD'")})

    plan = entry_plan(eks_registry, "edit", "demo", demo)
    found = issues_of(validate(plan), "time.unresolvable")
    assert len(found) == 3
    assert "'song.drop'" in found[0].message
    assert "'snare'" in found[1].message
    assert "speech-to-text" in found[2].message


def test_clip_local_end_in_an_edit(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def demo(e: TraceContext) -> None:
        clip = e.record("media.source", {"uri": "artifact:VID123", "kind": "video"})
        e.record("edit.render", {"timeline": {
            "fps": 30, "size": [1080, 1920],
            "tracks": [{"name": "main", "clips": [{
                "id": "main/0", "media": clip, "start": "0s",
                "scale": {"keys": [{"t": "start", "v": 1.0}, {"t": "end-8f", "v": 1.08}]},
            }]}],
        }})

    report = validate(entry_plan(eks_registry, "edit", "demo", demo))
    assert report.ok, report.text()


def test_negative_durations() -> None:
    def body(s: SessionCore) -> None:
        with s.record_container("capture.record", {"name": "demo"}):
            s.record_op("capture.hold", {"duration": t("1s")})
            s.record_op("capture.hold", {"duration": t("1s")})

    with pytest.raises(ValidationError, match="time_negative"):
        flow_plan(lambda s: s.record_op("capture.hold", {"duration": t("-1s")}))
    plan = flow_plan(body)
    # a hand-written or MCP plan reaches the validator, which reports the typed error as time.negative
    by_op(plan, "capture.hold")[0].args["duration"] = "-1s"
    [issue] = issues_of(validate(plan), "time.negative")
    holds = by_op(plan, "capture.hold")
    assert issue.node == holds[0].id
    assert issue.field == "duration"
    assert "'-1s'" in issue.message


def test_negative_durations_in_nested_models() -> None:
    # check params are a nested model; net.route delays now reject a negative length in the model itself
    def body(s: SessionCore) -> None:
        clip = s.record_op("api.get", {"path": "/api/media"})
        s.record_op("check.media", {"media": clip, "kind": "black", "params": {"longest": t("-800ms")}})
        s.record_op("check.media", {"media": clip, "kind": "frozen", "params": {"longest": t("800ms")}})
        checkpoint = eks.checks.use("perf", lcp=t("2.5s"))
        s.record_op("check.checkpoint", {"label": "x", "checks": [checkpoint]})

    plan = flow_plan(body)
    by_op(plan, "check.checkpoint")[0].args["checks"] = [{"kind": "perf", "params": {"lcp": "-1s", "inp": "200ms"}}]
    [lcp] = [issue for issue in issues_of(validate(plan), "time.negative") if issue.field and "lcp" in issue.field]
    assert lcp.field == "checks.0.params.lcp"
    by_op(plan, "check.checkpoint")[0].args["checks"] = [{"kind": "perf", "params": {"lcp": "2.5s"}}]
    [issue] = issues_of(validate(plan), "time.negative")
    assert issue.node == by_op(plan, "check.media")[0].id
    assert issue.field == "params.longest"
