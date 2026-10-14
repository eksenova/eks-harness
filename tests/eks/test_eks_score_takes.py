from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import eks
import eks.media
from eks import t
from eks.checks import default_checks
from eks.discovery import load_file
from eks.editing import EditBuilder
from eks.planning import trace
from eks.ref import encode, node_of
from eks.scoring import ScoreBuilder, TakeHandle, open_score
from eks.tracing import TraceError, current
from eks_harness.engine.validator import Report
from eks_harness.ir.hashing import derive_id
from eks_harness.ir.ops.scene import EventRef
from eks_harness.ir.ops.score import Take
from eks_harness.ir.plan import Node, Plan, SessionSpec
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import Meter, TempoMap, TimeError

SONG = "music/song.ogg"
MOBILE_DEFAULTS = [{"kind": "error_toasts", "params": {}}, {"kind": "page_errors", "params": {"noise": []}}]


def register_seed() -> None:
    # a tiny scenario whose personas the takes act as
    @eks.resource("member", persona=True, description="A user of the shop")
    class Member(eks.Resource):
        role: str = "admin"

    @eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
    def standard(d: eks.Data) -> None:
        d.member("host", role="host")
        d.member("harman-admin")


def define_phone() -> Callable[..., Any]:
    @eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
    def phone(sc: eks.BlenderScene, screen: eks.Media) -> None:
        p = sc.load("assets/phone.blend", obj="Phone")
        p.material("Screen").texture("screen", screen)
        sc.camera(lens=50).look_at(p)
        sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

    return phone


def define_titles() -> Callable[..., Any]:
    @eks.scene(kind="gfx", size=(720, 1280))
    def titles(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        sc.text("Anlık kur", id="subtitle", y=120.0)
        sc.on(t.beats("downbeat")[0:4], title.pulse(amount=1.1))

    return titles


def walk(plan: Plan) -> list[tuple[Node, tuple[Node, ...]]]:
    return list(plan.walk())


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def ops(nodes: list[Node]) -> list[str]:
    return [node.op for node in nodes]


def ref_to(node_id: str) -> dict[str, Any]:
    return {"$ref": node_id, "path": []}


def assert_valid(plan: Plan) -> Report:
    report = eks.validate(plan)
    assert report.ok, report.text()
    assert report.issues == (), report.text()
    return report


def two_take_score() -> tuple[Callable[..., Any], dict[str, bool]]:
    register_seed()
    phone = define_phone()
    define_titles()
    flags: dict[str, bool] = {}

    @eks.score(fps=30, bpm=120, window=t.span(t("0s"), t("16s")))
    def ad(sc: eks.Score) -> dict[str, Any]:
        first = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin",
                        timezone="Europe/Istanbul", locale="tr-TR")
        with first.flow() as s:
            flags["take"] = current().has_flag("take")
            s.ui.tap("#new-invoice", at=t("bar:1"))
            s.ui.fill("#amount", "5", at=t("beat:6"))
            s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))
        second = sc.take(platform="android", app="mobile-app", seed="standard", viewport="phone",
                         lead_in=t("2s"), warp=False, size=(720, 1280), root_seed=11, timezone="Europe/Istanbul", locale="tr-TR")
        with second.flow() as s:
            s.ui.tap("#menu", at=t("bar:3"))
            s.hold(t("1b"))
        flags["outside"] = current().has_flag("take")
        front = sc.scene(phone, screen=first)
        back = sc.scene("phone", screen=second)
        titles = sc.gfx("titles")
        sc.edit.track("scene").clip(front)
        sc.edit.track("scene").clip(back, start=t("bar:4"))
        sc.edit.track("graphics").clip(titles)
        sc.audio.master(loudness=-14)
        return {"front": front, "back": back, "takes": [first, second]}

    return ad, flags


def test_two_takes_build_sessions_resources_and_bodies(eks_registry: Registry) -> None:
    ad, flags = two_take_score()
    plan = eks.plan(ad)
    assert plan.kind == "score"
    takes = by_op(plan, "score.take")
    assert len(takes) == 2
    first, second = takes
    assert [node.args["session"]["key"] for node in takes] == ["take0", "take1"]  # type: ignore[index]

    # each take gets its own lease and backend, keyed by the take
    leases = {resource.id: resource for resource in plan.resources if resource.kind.startswith("lease.")}
    assert sorted((resource.kind, resource.session) for resource in leases.values()) == [
        ("lease.android", "take1"), ("lease.ios", "take0")]
    assert first.uses[0] != second.uses[0]
    assert plan.resource(first.uses[0]).kind == "lease.ios"
    assert plan.resource(first.uses[1]).kind == "backend"
    assert plan.resource(first.uses[1]).seed == "standard"
    assert plan.resource(second.uses[0]).kind == "lease.android"
    assert first.uses[1] != second.uses[1]

    # the take args carry the session spec and the take options
    assert first.args == {
        # backend, now and root seed come from the standard seed the take names
        "session": {"key": "take0", "app": "mobile-app", "platform": "ios", "seed": "standard",
                    "persona": "harman-admin", "backend": "eksenapi", "noise": [], "root_seed": 2026,
                    "now": "2026-01-15T09:00:00+03:00", "timezone": "Europe/Istanbul", "locale": "tr-TR",
                    "network_ignore": [], "viewports": {}, "checks": [],
                    "defaults": MOBILE_DEFAULTS},
        "lead_in": "1s", "warp": True}
    assert second.args == {
        "session": {"key": "take1", "app": "mobile-app", "platform": "android", "seed": "standard",
                    "backend": "eksenapi", "viewport": "phone", "noise": [], "root_seed": 11, "now": "2026-01-15T09:00:00+03:00",
                    "timezone": "Europe/Istanbul", "locale": "tr-TR", "network_ignore": [], "viewports": {}, "checks": [],
                    "defaults": MOBILE_DEFAULTS},
        "lead_in": "2s", "warp": False, "size": [720, 1280]}

    # bodies are recorded inside the containers, with the take flag on the scope
    assert first.branches is not None
    assert second.branches is not None
    assert ops(first.branches["body"]) == ["session.as", "ui.tap", "ui.fill", "overlay.show"]
    assert ops(second.branches["body"]) == ["ui.tap", "capture.hold"]
    assert flags == {"take": True, "outside": False}
    for node, ancestors in walk(plan):
        if node.op in {"ui.tap", "ui.fill", "overlay.show", "capture.hold", "session.as"}:
            assert [ancestor.op for ancestor in ancestors] == ["score.take"]
    for node in first.branches["body"]:
        assert node.uses[0] == first.uses[0]
    for node in second.branches["body"]:
        assert node.uses[0] == second.uses[0]
    assert first.branches["body"][0].args == {"persona": "harman-admin"}
    assert first.branches["body"][0].pin is None


def test_pins_are_stored_and_validated(eks_registry: Registry) -> None:
    plan = eks.plan(two_take_score()[0])
    first, second = by_op(plan, "score.take")
    assert first.branches is not None
    assert second.branches is not None
    assert [node.pin for node in first.branches["body"]] == [None, t("bar:1"), t("beat:6"), t("bar:2")]
    assert [node.pin for node in second.branches["body"]] == [t("bar:3"), None]
    # pin.order runs on the static 120 bpm tempo and is satisfied
    assert plan.clock.tempo == TempoMap(bpm=120)
    assert_valid(plan)


def test_pins_out_of_order_fail_validation_with_a_static_tempo(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def late(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#a", at=t("bar:2"))
            s.ui.tap("#b", at=t("beat:2"))

    report = eks.validate(eks.plan(late))
    assert [issue.code for issue in report.errors] == ["pin.order"]


def test_take_flow_opens_once(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def twice(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.nav("/a")
        with take.flow() as s:
            s.ui.nav("/b")

    with pytest.raises(TraceError, match="already written"):
        eks.plan(twice)


def test_take_without_persona_records_no_session_as(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def plain(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", backend="eksenapi")
        with take.flow() as s:
            s.ui.nav("/tenant")
            s.as_("host", at=t("1s"))

    plan = eks.plan(plain)
    (take,) = by_op(plan, "score.take")
    assert take.branches is not None
    assert ops(take.branches["body"]) == ["ui.nav", "session.as"]
    assert take.branches["body"][1].pin == t("1s")
    assert plan.resource(take.uses[1]).definition == "eksenapi"


def test_take_without_flow_keeps_an_empty_body(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def idle(sc: eks.Score) -> None:
        sc.take(platform="web", app="web-app", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul",
                locale="tr-TR")

    plan = eks.plan(idle)
    (take,) = by_op(plan, "score.take")
    assert take.branches == {"body": []}
    report = eks.validate(plan)
    assert report.ok
    assert [issue.code for issue in report.warnings] == ["control.empty"]


def test_take_handle_encodes_as_its_ref_and_builds_events(eks_registry: Registry) -> None:
    seen: dict[str, Any] = {}

    @eks.score(fps=30)
    def handles(sc: eks.Score) -> None:
        take = sc.take(platform="ios", app="mobile-app")
        seen["take"] = take
        seen["encoded"] = encode(take)
        seen["media"] = eks.media.as_media(take)
        seen["event"] = take.event("submitted", step=2)
        with pytest.raises(ValueError, match="not an event name"):
            take.event("1bad")

    plan = eks.plan(handles)
    (node,) = by_op(plan, "score.take")
    take = seen["take"]
    assert isinstance(take, TakeHandle)
    assert node_of(take.ref) == node.id
    assert take.spec == SessionSpec(key="take0", app="mobile-app", platform="ios", defaults=default_checks("ios"))
    assert seen["encoded"] == ref_to(node.id)
    assert node_of(seen["media"]) == node.id
    event = seen["event"]
    assert isinstance(event, EventRef)
    assert encode(event) == {"source": ref_to(node.id), "name": "submitted", "where": {"step": 2}}
    assert "take0" in repr(take)


def test_scenes_by_function_and_by_name(eks_registry: Registry) -> None:
    plan = eks.plan(two_take_score()[0])
    first, second = by_op(plan, "score.take")
    renders = by_op(plan, "blender.render")
    assert len(renders) == 2
    textures = [render.args["scene"]["textures"][0]["media"] for render in renders]  # type: ignore[index]
    assert textures == [ref_to(first.id), ref_to(second.id)]
    # embedded scenes render on the score's size and fps and get stable embedding ids
    settings = renders[0].args["scene"]["render"]  # type: ignore[index]
    assert settings["size"] == [1080, 1920]  # type: ignore[index]
    assert settings["fps"] == 30  # type: ignore[index]
    assert [render.id for render in renders] == [
        derive_id("n", "score:ad/scene:phone#0#0"), derive_id("n", "score:ad/scene:phone#1#0")]
    (gfx,) = by_op(plan, "gfx.render")
    assert gfx.id == derive_id("n", "score:ad/scene:titles#0#0")
    assert gfx.args["scene"]["size"] == [1080, 1920]  # type: ignore[index]


def test_gfx_requires_a_gfx_scene(eks_registry: Registry) -> None:
    phone = define_phone()

    @eks.score(fps=30)
    def wrong(sc: eks.Score) -> None:
        sc.gfx(phone, screen="artifact:SCREEN1")

    with pytest.raises(TraceError, match="is a blender scene, but a gfx scene is expected"):
        eks.plan(wrong)

    @eks.score(fps=30)
    def unknown(sc: eks.Score) -> None:
        sc.gfx("nowhere")

    with pytest.raises(TraceError, match="nowhere"):
        eks.plan(unknown)


def test_song_track_sets_the_tempo_source(eks_registry: Registry) -> None:
    refs: dict[str, Any] = {}

    @eks.score(fps=30, song=SONG, window=t.span(t("0s"), t("15s")))
    def ad(sc: eks.Score) -> None:
        refs["song"] = sc.song
        refs["audio"] = sc.audio
        refs["edit"] = sc.edit
        sc.audio.master(loudness=-14)

    plan = eks.plan(ad)
    assert ops(plan.nodes) == ["media.source", "media.markers", "audio.mix", "edit.render"]
    source, markers, mix, render = plan.nodes
    assert source.args == {"uri": f"file:{SONG}"}
    assert refs["song"] is not None
    assert node_of(refs["song"]) == source.id
    assert refs["audio"] is refs["edit"].audio
    assert markers.args["media"] == ref_to(source.id)
    assert markers.args["sources"] == [
        {"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"], "backend": "auto"}]
    assert plan.clock.tempo == TempoMap(source=markers.id)
    assert plan.clock.window == t.span(t("0s"), t("15s"))
    tracks = mix.args["mix"]["tracks"]  # type: ignore[index]
    assert [track["name"] for track in tracks] == ["song"]  # type: ignore[index,union-attr]
    assert tracks[0]["media"] == ref_to(source.id)  # type: ignore[index]
    assert mix.args["mix"]["master"]["loudness"] == -14.0  # type: ignore[index]
    assert render.args["timeline"]["audio"] == ref_to(mix.id)  # type: ignore[index]
    assert_valid(plan)


def test_a_static_bpm_wins_over_the_song_markers(eks_registry: Registry) -> None:
    @eks.score(fps=30, song=SONG, bpm=120)
    def fixed(sc: eks.Score) -> None:
        return None

    plan = eks.plan(fixed)
    assert ops(plan.nodes) == ["media.source", "media.markers", "audio.mix", "edit.render"]
    assert plan.clock.tempo == TempoMap(bpm=120)


def test_a_song_score_keeps_its_meter(eks_registry: Registry) -> None:
    # the tempo comes from the song markers, the meter from the score options
    @eks.score(fps=30, song=SONG, meter=(3, 4))
    def waltz(sc: eks.Score) -> None:
        return None

    plan = eks.plan(waltz)
    markers = by_op(plan, "media.markers")[0]
    assert plan.clock.tempo == TempoMap(source=markers.id, meter=Meter(numerator=3, denominator=4))
    assert plan.clock.tempo is not None
    assert plan.clock.tempo.meter == Meter(numerator=3, denominator=4)
    assert_valid(plan)


def test_a_static_bpm_keeps_its_meter_over_the_song(eks_registry: Registry) -> None:
    @eks.score(fps=30, song=SONG, bpm=90, meter=(6, 8))
    def fixed(sc: eks.Score) -> None:
        return None

    plan = eks.plan(fixed)
    assert plan.clock.tempo == TempoMap(bpm=90, meter=Meter(numerator=6, denominator=8))


def test_a_meter_without_bpm_or_song_is_rejected(eks_registry: Registry) -> None:
    @eks.score(fps=30, meter=(3, 4))
    def silent(sc: eks.Score) -> None:
        return None

    with pytest.raises(TraceError, match=r"meter=\(3, 4\) needs a tempo"):
        eks.plan(silent)


def test_one_mix_and_one_render_with_outputs(eks_registry: Registry) -> None:
    register_seed()
    phone = define_phone()
    finals: dict[str, Any] = {}

    @eks.score(fps=30, song=SONG, duration=t("15s"))
    def full(sc: eks.Score) -> dict[str, Any]:
        take = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin",
                       timezone="Europe/Istanbul", locale="tr-TR")
        with take.flow() as s:
            s.ui.tap("#go", at=t("bar:1"))
        screen = sc.scene(phone, screen=take)
        sc.edit.track("scene").clip(screen)
        sc.audio.track("music/voice.ogg", name="voice", gain=-3)
        finals["score"] = sc
        return {"screen": screen, "take": take}

    plan = eks.plan(full)
    assert len(by_op(plan, "audio.mix")) == 1
    (render,) = by_op(plan, "edit.render")
    (mix,) = by_op(plan, "audio.mix")
    assert [track["name"] for track in mix.args["mix"]["tracks"]] == ["song", "voice"]  # type: ignore[index,union-attr]
    assert render.args["timeline"]["duration"] == "15s"  # type: ignore[index]
    assert render.id == derive_id("n", "score:full/final:edit.render#0")
    (scene,) = by_op(plan, "blender.render")
    (take,) = by_op(plan, "score.take")
    assert plan.outputs == {"screen": ref_to(scene.id), "take": ref_to(take.id)}
    score = finals["score"]
    assert isinstance(score, ScoreBuilder)
    assert node_of(score.finalize()) == render.id
    assert node_of(score.edit.output) == render.id
    assert [handle.spec.key for handle in score.takes] == ["take0"]
    assert score.options.duration == t("15s")
    assert_valid(plan)


def test_score_without_song_has_no_mix(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=100)
    def silent(sc: eks.Score) -> None:
        assert sc.song is None
        assert isinstance(sc.edit, EditBuilder)

    plan = eks.plan(silent)
    assert ops(plan.nodes) == ["edit.render"]
    assert "audio" not in plan.nodes[0].args["timeline"]  # type: ignore[operator]


def test_cues_update_the_clock(eks_registry: Registry) -> None:
    seen: dict[str, Any] = {}

    @eks.score(fps=30, bpm=120)
    def cued(sc: eks.Score) -> None:
        seen["drop"] = sc.cue("drop", t("bar:4"))
        take = sc.take(platform="web", app="web-app", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul", locale="tr-TR")
        with take.flow() as s:
            s.ui.tap("#go", at=seen["drop"])

    plan = eks.plan(cued)
    assert seen["drop"] == t.cue("drop")
    assert plan.clock.cues == {"drop": t("bar:4")}
    assert_valid(plan)


def test_a_score_is_static(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def nested(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow():
            sc.take(platform="web", app="web-app")

    with pytest.raises(TraceError, match=r"sc.take\(\): a score is a static arrangement"):
        eks.plan(nested)

    titles = define_titles()

    @eks.score(fps=30)
    def scene_in_take(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow():
            sc.gfx(titles)

    with pytest.raises(TraceError, match=r"sc.gfx\(\)"):
        eks.plan(scene_in_take)

    @eks.score(fps=30, bpm=120)
    def cue_in_when(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s, eks.when(s.ui.exists("#x")):
            sc.cue("x", t("bar:1"))

    with pytest.raises(TraceError, match=r"sc.cue\(\)"):
        eks.plan(cue_in_when)


def test_take_arguments_are_validated(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def desktop(sc: eks.Score) -> None:
        sc.take(platform="desktop", app="web-app")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        eks.plan(desktop)


@pytest.mark.parametrize(("kwargs", "error", "message"), [
    ({"app": ""}, ValueError, r"take0 app= must not be empty; pass a name such as 'mobile-app'"),
    ({"app": "   "}, ValueError, r"take0 app= must not be empty"),
    ({"as_": ""}, ValueError, r"take0 as_= must not be empty; pass a name such as 'harman-admin'"),
    ({"seed": ""}, ValueError, r"take0 seed= must not be empty"),
    ({"backend": ""}, ValueError, r"take0 backend= must not be empty"),
    ({"viewport": ""}, ValueError, r"take0 viewport= must not be empty"),
    ({"seed": "two words"}, ValueError, r"take0 seed='two words' must match"),
    ({"backend": "EksenAPI"}, ValueError, r"take0 backend='EksenAPI' must match"),
    ({"viewport": "-phone"}, ValueError, r"take0 viewport='-phone' must match"),
    ({"app": 3}, TypeError, r"take0 app= expects a name"),
    ({"as_": 3}, TypeError, r"take0 as_= expects a name"),
])
def test_take_string_arguments_are_checked(
    eks_registry: Registry, kwargs: dict[str, Any], error: type[Exception], message: str,
) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        options: dict[str, Any] = {"platform": "web", "app": "web-app", **kwargs}
        sc.take(**options)

    with pytest.raises(error, match=message):
        eks.plan(ad)


def test_second_take_errors_name_their_take(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        sc.take(platform="web", app="web-app")
        sc.take(platform="web", app="")

    with pytest.raises(ValueError, match=r"take1 app= must not be empty"):
        eks.plan(ad)


@pytest.mark.parametrize(("lead_in", "message"), [
    ("bar:1", "take0 lead_in is a duration"),
    ("-1s", "take0 lead_in must not be negative"),
])
def test_take_lead_in_is_a_non_negative_length(eks_registry: Registry, lead_in: str, message: str) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        sc.take(platform="web", app="web-app", lead_in=t(lead_in))

    with pytest.raises(TimeError, match=message):
        eks.plan(ad)


def test_takes_with_a_frozen_now_warn_without_timezone_and_locale(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def zoned(sc: eks.Score) -> None:
        bare = sc.take(platform="web", app="web-app", now="2026-01-15T09:00+03:00")
        with bare.flow() as s:
            s.ui.nav("/a")
        half = sc.take(platform="web", app="web-app", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul")
        with half.flow() as s:
            s.ui.nav("/b")

    report = eks.validate(eks.plan(zoned))
    assert report.ok
    zone = [issue for issue in report.warnings if issue.code == "clock.zone"]
    assert [issue.field for issue in zone] == ["session.timezone", "session.locale"]
    assert "timezone or locale" in zone[0].message
    with pytest.raises(ValueError, match="IANA"):
        @eks.score(fps=30)
        def bad(sc: eks.Score) -> None:
            sc.take(platform="web", app="web-app", timezone="Istanbul")

        eks.plan(bad)


def test_take_lead_in_of_zero_is_allowed(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", now="2026-01-15T09:00+03:00", lead_in=t("0s"),
                       timezone="Europe/Istanbul", locale="tr-TR")
        with take.flow() as s:
            s.ui.nav("/a")

    plan = eks.plan(ad)
    assert by_op(plan, "score.take")[0].args["lead_in"] == "0s"  # type: ignore[index]
    assert_valid(plan)


@pytest.mark.parametrize("field", ["lead_in", "tolerance"])
@pytest.mark.parametrize("value", ["bar:1", "-1s", "downbeat:2+1s"])
def test_take_model_rejects_anchored_and_negative_lengths(field: str, value: str) -> None:
    # JSON plans reach the model without the builder, so the model checks lengths too
    session = {"key": "take0", "app": "web-app", "platform": "web"}
    with pytest.raises(ValidationError, match="the length of time"):
        Take.model_validate({"session": session, field: value})


def test_take_model_accepts_lengths() -> None:
    session = {"key": "take0", "app": "web-app", "platform": "web"}
    take = Take.model_validate({"session": session, "lead_in": "0s", "tolerance": "2f"})
    assert take.lead_in == t("0s")
    assert take.tolerance == t("2f")


@pytest.mark.parametrize("size", [(0, 10), (720, -5)])
def test_take_size_must_be_positive(eks_registry: Registry, size: tuple[int, int]) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        sc.take(platform="web", app="web-app", size=size)

    with pytest.raises(ValidationError, match="must be two positive integers"):
        eks.plan(ad)


def test_a_take_session_cannot_record_after_its_flow(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#inside")
        s.ui.tap("#after")

    with pytest.raises(TraceError, match=r"take0's session is used outside `with take0.flow\(\)`"):
        eks.plan(ad)


def test_a_take_session_cannot_record_inside_another_take(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def ad(sc: eks.Score) -> None:
        a = sc.take(platform="web", app="web-app")
        b = sc.take(platform="ios", app="mobile-app")
        with a.flow() as s:
            s.ui.tap("#a")
        with b.flow() as s2:
            s2.ui.tap("#b")
            s.ui.tap("#x", at=t("bar:1"))

    with pytest.raises(TraceError, match="take0's session is used outside"):
        eks.plan(ad)


def test_a_take_session_cannot_open_containers_after_its_flow(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def ad(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#inside")
        with s.record("late"):
            pass

    with pytest.raises(TraceError, match="take0's session is used outside"):
        eks.plan(ad)


def test_open_score_requires_a_score_entry(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(720, 1280))
    def cut(e: eks.Edit) -> None:
        return None

    with pytest.raises(TraceError, match="not a score"):
        trace(eks_registry.entry_for(cut), root=open_score)


def test_score_plans_are_deterministic(eks_registry: Registry) -> None:
    ad, _flags = two_take_score()
    first = eks.plan(ad)
    second = eks.plan(ad)
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash


SCORE_SOURCE = '''
import eks
from eks import t


@eks.score(fps=30, bpm=120)
def ad(sc: eks.Score):
    take = sc.take(platform="ios", app="mobile-app", span=t.span(t("bar:1"), t("bar:5")))
    with take.flow() as s:
        s.ui.tap("#new-invoice", at=t("bar:1"))
        s.ui.fill("#amount", "5", at=t("beat:6"))
    sc.edit.track("main").clip(take)
    sc.audio.master(loudness=-14)
'''


def test_score_ids_survive_edits_elsewhere(tmp_path: Path, eks_registry: Registry) -> None:
    path = tmp_path / ".harness" / "scores" / "ad.py"
    path.parent.mkdir(parents=True)
    path.write_text(SCORE_SOURCE, encoding="utf-8")
    (entry,) = load_file(path)
    before = trace(entry)
    (take,) = by_op(before, "score.take")
    assert take.args["span"] == {"start": "bar:1", "end": "bar:5"}

    edited = SCORE_SOURCE.replace(
        '    with take.flow() as s:\n',
        '\n    # the take drives the invoice screen\n    unused = {"note": 1}\n\n    with take.flow() as s:\n')
    path.write_text("# header comment\n\n" + edited, encoding="utf-8")
    (entry,) = load_file(path)
    after = trace(entry)
    assert [node.id for node, _a in after.walk()] == [node.id for node, _a in before.walk()]
    assert [node.hash for node, _a in after.walk()] == [node.hash for node, _a in before.walk()]
    assert after.hash == before.hash
    assert [node.src for node, _a in after.walk()] != [node.src for node, _a in before.walk()]
