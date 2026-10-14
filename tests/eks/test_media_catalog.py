from typing import Any, Literal

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import eks
from eks import t
from eks.effects import animated, curves, driven, fx, require_catalogs, tx
from eks.tracing import TraceContext, tracing
from eks_harness.ir.catalog.animated import Driven
from eks_harness.ir.catalog.curves import (
    BeatPulse,
    BoxEnv,
    Curve,
    CurveBase,
    EasingEnv,
    ExpDecayEnv,
    LinearEnv,
    RandomChoice,
    Sine,
)
from eks_harness.ir.catalog.easing import (
    LEGACY_NAMES,
    CubicBezier,
    Easing,
    EasingBase,
    OutCubic,
    OutElastic,
)
from eks_harness.ir.catalog.effects import Effect, EffectBase, Flash, TextOverlay, Zoom
from eks_harness.ir.catalog.keys import Keys
from eks_harness.ir.catalog.markers import (
    BeatTracker,
    EnergyMarkers,
    FaceMarkers,
    MarkerBase,
    MarkerSource,
    MomentumMarkers,
    MotionMarkers,
    OnsetMarkers,
    SceneMarkers,
    SilenceMarkers,
    STTMarkers,
)
from eks_harness.ir.catalog.transitions import Crossfade, Transition, TransitionBase
from eks_harness.ir.registry import (
    Category,
    Registry,
    RegistryError,
    contributing,
    default_registry,
)
from eks_harness.ir.time import Anchor, Time, TimeError, TimeSet


def effect_samples() -> list[EffectBase]:
    # file inputs (LUTs, watermark and sticker images) are media.source nodes, so they need an active trace
    with tracing(TraceContext(kind="edit", name="samples", registry=default_registry())):
        return effect_list()


def effect_list() -> list[EffectBase]:
    return [
        fx.brightness(0.1),
        fx.contrast(1.2),
        fx.saturation(1.1),
        fx.crop(0, 0, 540, 960),
        fx.mirror("vertical"),
        fx.zoom(1.0, 1.08, ease="out_cubic"),
        fx.pan(10, -4),
        fx.speed(eks.keys({t("0s"): 1.0, t("1s"): 2.0})),
        fx.freeze(t("1s"), t("500ms")),
        fx.chroma_key("#00FF00", similarity=0.2),
        fx.color_grade(lut="grade.cube", exposure=0.5),
        fx.fade("in", t("12f")),
        fx.blur(curves.sine(2.0)),
        fx.sharpen(0.8),
        fx.vignette(angle=0.5),
        fx.film_grain(0.3, seed=4),
        fx.lut("looks/warm.cube"),
        fx.invert(),
        fx.posterize(4),
        fx.text("Merhaba", start=t("1s"), duration=t("2s")),
        fx.lower_third("EksenGOLD", "Kuyumcular için"),
        fx.watermark("logo.png", opacity=0.5),
        fx.sticker("star.png", rotation=curves.lfo("tri", 1.0)),
        fx.flash(t.beats("beat")[0:4], t("100ms")),
        fx.rgb_split(4, 2),
        fx.glitch(0.4, seed=9),
        fx.shake(0.5, decay=1.0),
        fx.vhs(),
        fx.datamosh(),
        fx.captions("words", style="kinetic"),
        fx.matanyone_remove(),
        fx.birefnet_remove(background="#ffffff"),
        fx.sam3_track("phone", output="matte"),
        fx.auto_reframe(tracker="salience"),
        fx.punch_in(trigger=t.beats("downbeat")[0:2]),
    ]


def transition_samples() -> list[TransitionBase]:
    d = t("8f")
    return [
        tx.cut(), tx.crossfade(d), tx.dip_to_black(d), tx.dip_to_white(d), tx.wipe(d, direction="up"),
        tx.slide(d), tx.push(d), tx.glitch(d, intensity=0.3), tx.zoom(d), tx.dissolve(d), tx.blur_through(d),
        tx.iris(d, mode="close"), tx.radial(d), tx.pixelize(d), tx.fade_grays(d), tx.luma_wipe(d),
        tx.rgb_shift_wipe(d), tx.whip_pan(d, direction="left"), tx.light_leak(d), tx.film_burn(d),
        tx.beat_flash(d), tx.beat_glitch(d, stream="downbeat"),
    ]


def curve_samples() -> list[CurveBase]:
    return [
        curves.beat_pulse(t.beats("downbeat")[0:8], curves.easing_env(t("250ms"), "out_elastic")),
        curves.exp_decay(t("300ms"), baseline=0.1),
        curves.sine(2.0, phase=0.5),
        curves.lfo("square", 4.0),
        curves.expr("sin(t) * 0.5"),
        curves.spring(1.0, stiffness=200.0),
        curves.noise(seed=3, hold=t("100ms")),
        curves.random_choice([0.0, 0.5, 1.0], t("200ms"), seed=5),
        curves.step([1.0, 2.0], t("1b")),
        curves.bezier(0.0, 0.2, 0.8, 1.0, t("1s")),
        curves.bounce(1.0, t("400ms"), decay=2.0),
    ]


def easing_samples() -> list[EasingBase]:
    names = ["linear", "in_quad", "out_quad", "in_out_quad", "in_cubic", "out_cubic", "in_out_cubic",
             "in_quart", "out_quart", "in_out_quart", "in_sine", "out_sine", "in_out_sine", "in_expo", "out_expo",
             "in_out_expo", "out_bounce", "out_elastic", "out_back"]
    samples = [eks.ease(name) for name in names]
    return [sample for sample in samples if sample is not None] + [CubicBezier(p1x=0.2, p1y=0.0, p2x=0.0, p2y=1.0)]


def marker_samples() -> list[MarkerBase]:
    return [
        BeatTracker(name="song"),
        STTMarkers(name="words", language="tr"),
        SceneMarkers(name="cuts"),
        SilenceMarkers(name="gaps", min_duration=t("300ms")),
        OnsetMarkers(name="hits"),
        EnergyMarkers(name="loud", band="low"),
        MomentumMarkers(name="song", smoothing=t("1s"), min_build=t("2s"), min_section=t("4s"),
                        snap_window=t("1b")),
        FaceMarkers(name="faces"),
        MotionMarkers(name="moves"),
    ]


SAMPLES: dict[Category, tuple[Any, Any]] = {
    "effect": (Effect, effect_samples),
    "transition": (Transition, transition_samples),
    "curve": (Curve, curve_samples),
    "easing": (Easing, easing_samples),
    "marker": (MarkerSource, marker_samples),
}


def adapter_of(category: Category) -> TypeAdapter[Any]:
    # the Poly annotated aliases are typing forms pyright does not accept as TypeAdapter arguments
    annotated: Any = SAMPLES[category][0]
    return TypeAdapter(annotated)


@pytest.mark.parametrize("category", list(SAMPLES))
def test_every_builtin_kind_validates_through_its_poly_type_and_round_trips(
    category: Category, eks_registry: Registry
) -> None:
    _annotated, build = SAMPLES[category]
    adapter = adapter_of(category)
    samples: list[BaseModel] = build()
    builtin = {spec.kind for spec in eks_registry.catalogs(category) if spec.provenance.plugin == "eks"}
    assert {str(getattr(sample, "kind", "")) for sample in samples} == builtin
    for sample in samples:
        data = sample.model_dump(mode="json", exclude_none=True)
        restored = adapter.validate_python(data)
        assert type(restored) is type(sample)
        assert restored == sample
        assert adapter.validate_json(adapter.dump_json(sample)) == sample
        assert adapter.dump_python(restored, mode="json", exclude_none=True) == data


def test_poly_rejects_unknown_kinds() -> None:
    with pytest.raises(ValidationError, match="unknown effect kind 'zooom'"):
        adapter_of("effect").validate_python({"kind": "zooom", "scale": 1.0})


def test_ease_accepts_registered_and_legacy_names() -> None:
    assert eks.ease("out_cubic") == OutCubic()
    assert eks.ease("ease_out_cubic") == OutCubic()
    assert eks.ease("elastic_out") == OutElastic()
    for legacy, current in LEGACY_NAMES.items():
        eased = eks.ease(legacy)
        assert eased is not None
        assert eased.kind == current
    assert eks.ease(None) is None
    model = CubicBezier(p1x=0.1, p1y=0.2, p2x=0.3, p2y=0.4)
    assert eks.ease(model) is model


def test_ease_rejects_unknown_names_with_suggestions() -> None:
    with pytest.raises(RegistryError, match="unknown easing 'out_cubik'; did you mean 'out_cubic'"):
        eks.ease("out_cubik")
    with pytest.raises(RegistryError, match="takes parameters"):
        eks.ease("cubic_bezier")
    with pytest.raises(TypeError, match="easing name"):
        eks.ease(3)  # type: ignore[arg-type]


def test_keys_in_insertion_order_with_ease_after_the_first() -> None:
    animation = eks.keys({t("0s"): 1.0, t("1s"): 2.0}, ease="out_cubic")
    assert [frame.t for frame in animation.keys] == [t("0s"), t("1s")]
    assert [frame.v for frame in animation.keys] == [1.0, 2.0]
    assert [frame.ease for frame in animation.keys] == [None, OutCubic()]
    reversed_order = eks.keys({t("2s"): 0.0, t("1s"): 5.0})
    assert [frame.t for frame in reversed_order.keys] == [t("2s"), t("1s")]
    assert all(frame.ease is None for frame in reversed_order.keys)


def test_keys_rejects_bad_input() -> None:
    with pytest.raises(TypeError, match="bare numbers are not times"):
        eks.keys({1.0: 2.0})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="pass t"):
        eks.keys({"1s": 2.0})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="at least one keyframe"):
        eks.keys({})
    with pytest.raises(TypeError, match="mapping of times"):
        eks.keys([(t("0s"), 1.0)])  # type: ignore[arg-type]


def test_zoom_constant_or_keys_over_the_clip() -> None:
    animated_zoom = fx.zoom(1.0, 1.08, ease="out_cubic")
    assert isinstance(animated_zoom, Zoom)
    assert isinstance(animated_zoom.scale, Keys)
    frames = animated_zoom.scale.keys
    assert [frame.t for frame in frames] == [t.start, t.end]
    assert frames[0].t.anchor == Anchor(kind="start")
    assert [frame.v for frame in frames] == [1.0, 1.08]
    assert frames[0].ease is None
    assert frames[1].ease == OutCubic()
    assert fx.zoom(1.2).scale == 1.2
    with pytest.raises(TypeError, match="constant zoom"):
        fx.zoom(1.2, ease="out_cubic")
    with pytest.raises(TypeError, match="expects a number"):
        fx.zoom("1.2")  # type: ignore[arg-type]


def test_crossfade_duration_and_strict_times() -> None:
    fade = tx.crossfade(t("8f"))
    assert isinstance(fade, Crossfade)
    assert fade.duration == t("8f")
    assert fade.model_dump(mode="json") == {"kind": "crossfade", "duration": "8f"}
    with pytest.raises(TypeError, match="pass t"):
        tx.crossfade("8f")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bare numbers"):
        tx.dissolve(0.5)  # type: ignore[arg-type]


def test_beat_pulse_with_easing_envelope() -> None:
    pulse = curves.beat_pulse(t.beats("downbeat")[0:8], curves.easing_env(t("250ms"), "out_elastic"))
    assert isinstance(pulse, BeatPulse)
    assert pulse.trigger == TimeSet(stream="downbeat", start=0, stop=8)
    assert isinstance(pulse.envelope, EasingEnv)
    assert pulse.envelope.easing == OutElastic()
    assert pulse.envelope.duration == t("250ms")
    assert pulse.model_dump(mode="json", exclude_none=True)["envelope"] == {
        "kind": "easing_env", "duration": "0.25s", "easing": {"kind": "out_elastic"}, "direction": "out"}
    single = curves.beat_pulse(t("beat:4"), curves.box_env(t("100ms")), intensity=0.5)
    assert single.trigger == TimeSet(times=(t("beat:4"),))
    assert isinstance(single.envelope, BoxEnv)
    assert isinstance(curves.exp_decay_env(t("1s")), ExpDecayEnv)
    assert isinstance(curves.linear_env(t("10ms"), t("90ms")), LinearEnv)
    with pytest.raises(TypeError, match="envelope"):
        curves.beat_pulse(t.beats()[0:2], curves.sine(1.0))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="needs an easing"):
        curves.easing_env(t("1s"), None)


def test_flash_on_beats() -> None:
    flash = fx.flash(t.beats("beat")[0:4], t("100ms"))
    assert isinstance(flash, Flash)
    assert flash.at == TimeSet(stream="beat", start=0, stop=4)
    assert flash.duration == t("100ms")
    assert flash.color == "#ffffff"
    assert fx.flash(t("bar:2"), t("2f"), color="#FFCC00").color == "#ffcc00"
    with pytest.raises(TypeError, match="fx.flash at"):
        fx.flash("beat:2", t("2f"))  # type: ignore[arg-type]


def test_animated_and_driven() -> None:
    wave = curves.sine(2.0)
    assert animated(wave) == Driven(curve=wave)
    assert driven(wave, scale=2.0, offset=1.0) == Driven(curve=wave, scale=2.0, offset=1.0)
    animation = eks.keys({t("0s"): 0.0})
    assert animated(animation) is animation
    assert animated(0.5) == 0.5
    with pytest.raises(TypeError, match="expects a number"):
        animated(True)
    with pytest.raises(TypeError, match="curve"):
        driven(0.5)  # type: ignore[arg-type]


def test_text_effects_carry_refs_as_fmt_inside_a_trace() -> None:
    plain = fx.text("Merhaba")
    assert isinstance(plain, TextOverlay)
    assert plain.text == "Merhaba"
    with pytest.raises(TypeError, match="expects text"):
        fx.text(5)  # type: ignore[arg-type]


def test_custom_builds_registered_kinds() -> None:
    zoom = fx.custom("zoom", scale=1.5)
    assert zoom == Zoom(scale=1.5)
    fade = tx.custom("crossfade", duration=t("4f"))
    assert fade == Crossfade(duration=t("4f"))
    wave = curves.custom("sine", freq_hz=1.0)
    assert wave == Sine(freq_hz=1.0)
    with pytest.raises(RegistryError, match="did you mean 'zoom'"):
        fx.custom("zoon", scale=1.0)
    with pytest.raises(TypeError, match="pass t"):
        tx.custom("crossfade", duration="4f")
    with pytest.raises(TypeError, match="multiple values for argument 'kind'"):
        fx.custom("zoom", kind="zoom", scale=1.0)  # type: ignore[misc]
    with pytest.raises(TypeError, match="sets kind itself"):
        fx.captions("words", kind="captions")  # pyright: ignore[reportCallIssue]


def test_plugin_effect_through_custom_requires_the_plugin(eks_registry: Registry) -> None:
    with contributing("sparkle-fx", "1.2.0"):
        @eks.effect(description="Sparkles over the clip")
        class Sparkle(EffectBase, frozen=True):
            kind: Literal["sparkle"] = "sparkle"
            density: float = 1.0
            at: Time | None = None

    assert eks_registry.catalog("effect", "sparkle").provenance.plugin == "sparkle-fx"
    sparkle = fx.custom("sparkle", density=2.0)
    assert isinstance(sparkle, Sparkle)
    assert adapter_of("effect").validate_python({"kind": "sparkle", "density": 2.0}) == sparkle
    with pytest.raises(TypeError, match="bare numbers"):
        fx.custom("sparkle", at=1.0)

    @eks.edit(fps=30, size=(1080, 1920))
    def sparkly(e: eks.Edit) -> None:
        e.track("main").clip(e.media("clips/a.mp4")).effect(eks.fx.custom("sparkle", density=3.0))

    plan = eks.plan(sparkly)
    assert [(item.plugin, item.version) for item in plan.requires] == [("sparkle-fx", "1.2.0")]
    render = next(node for node, _ in plan.walk() if node.op == "edit.render")
    timeline: Any = render.args["timeline"]
    assert timeline["tracks"][0]["clips"][0]["effects"] == [{"kind": "sparkle", "density": 3.0}]


def test_unregistered_catalog_models_are_rejected() -> None:
    class Rogue(EffectBase, frozen=True):
        kind: Literal["rogue"] = "rogue"

    with pytest.raises(RegistryError, match="unknown effect kind 'rogue'"):
        require_catalogs(Rogue())

    class FakeZoom(EffectBase, frozen=True):
        kind: Literal["zoom"] = "zoom"

    with pytest.raises(RegistryError, match="registered by"):
        require_catalogs(FakeZoom())


def test_stream_names_of_every_marker_kind() -> None:
    assert BeatTracker(name="song").stream_names() == ("song.beat", "song.downbeat")
    assert BeatTracker(name="song", streams=("kick", "snare", "beat")).stream_names() == (
        "song.kick", "song.snare", "song.beat")
    momentum = MomentumMarkers(name="m", smoothing=t("1s"), min_build=t("2s"), min_section=t("4s"),
                               snap_window=t("1b"))
    assert momentum.stream_names() == ("m.drop", "m.build", "m.break", "m.section", "m.peak")
    assert STTMarkers(name="words").stream_names() == ("words",)
    assert SceneMarkers(name="cuts").stream_names() == ("cuts",)
    assert SilenceMarkers(name="gaps", min_duration=t("1s")).stream_names() == ("gaps",)
    assert OnsetMarkers(name="hits").stream_names() == ("hits",)
    assert EnergyMarkers(name="loud").stream_names() == ("loud",)
    assert FaceMarkers(name="faces").stream_names() == ("faces",)
    assert MotionMarkers(name="moves").stream_names() == ("moves",)
    covered = {type(sample).__name__ for sample in marker_samples()}
    assert len(covered) == 9


def test_catalog_defaults_survive_the_port_to_time() -> None:
    momentum = MomentumMarkers(name="song")
    assert momentum.smoothing == t("750ms")
    assert momentum.min_build == t("1.5s")
    assert momentum.min_section == t("4s")
    assert momentum.snap_window == t("120ms")
    assert momentum.drop_threshold == 0.22
    assert momentum.break_threshold == 0.2
    assert SilenceMarkers(name="gaps").min_duration == t("300ms")
    assert RandomChoice(values=(1.0, 2.0)).hold == t("500ms")
    assert curves.random_choice([1.0, 2.0]).hold == t("500ms")
    assert curves.random_choice([1.0, 2.0], t("1b")).hold == t("1b")
    # None turns onset snapping off; a zero window is not a length
    unsnapped = MomentumMarkers(name="song", snap_window=None)
    assert unsnapped.snap_window is None
    dumped = unsnapped.model_dump(mode="json")
    assert dumped["snap_window"] is None
    assert MomentumMarkers.model_validate(dumped) == unsnapped
    assert MomentumMarkers.model_validate_json(momentum.model_dump_json()) == momentum
    with pytest.raises(ValidationError, match="positive length"):
        MomentumMarkers(name="song", snap_window=t("0s"))


def test_builders_return_frozen_models() -> None:
    zoom = fx.zoom(1.0)
    with pytest.raises(ValidationError):
        zoom.scale = 2.0  # type: ignore[misc]
    assert isinstance(eks.ease("linear"), EasingBase)
    assert isinstance(Time(), Time)


def test_lengths_must_be_positive_unanchored_durations() -> None:
    # beat and frame counts are lengths too
    assert tx.crossfade(t("1b")).duration == t("1b")
    assert curves.step([1.0], t("8f")).hold == t("8f")
    bad_lengths = [t("0s"), t("0s") - t("1s"), t("0b"), t("0f") - t("2f")]
    builders = [
        lambda length: tx.crossfade(length),
        lambda length: tx.beat_flash(length),
        lambda length: curves.bounce(1.0, length),
        lambda length: curves.step([1.0], length),
        lambda length: curves.random_choice([1.0, 2.0], length),
        lambda length: curves.noise(hold=length),
        lambda length: curves.bezier(0.0, 0.2, 0.8, 1.0, length),
        lambda length: curves.exp_decay(length),
        lambda length: curves.exp_decay_env(length),
        lambda length: curves.linear_env(t("10ms"), length),
        lambda length: curves.box_env(length),
        lambda length: curves.easing_env(length, "out_cubic"),
        lambda length: fx.freeze(t("1s"), length),
        lambda length: fx.fade("in", length),
        lambda length: fx.flash(t("1s"), length),
        lambda length: fx.text("x", duration=length),
    ]
    for build in builders:
        for length in bad_lengths:
            with pytest.raises(TimeError, match="positive length"):
                build(length)
        # a point in time is not a length, even when it would resolve to one
        with pytest.raises(TimeError, match="point in time"):
            build(t("beat:8"))
    # the models enforce the same rule for IR that does not come from the builders
    with pytest.raises(ValidationError, match="positive length"):
        Crossfade.model_validate({"kind": "crossfade", "duration": "0s"})
    with pytest.raises(ValidationError, match="point in time"):
        Crossfade(duration=t("beat:8"))
    with pytest.raises(ValidationError, match="positive length"):
        SilenceMarkers(name="quiet", min_duration=t("0s"))
    with pytest.raises(ValidationError, match="point in time"):
        MomentumMarkers(name="song", smoothing=t("end"), min_build=t("2b"), min_section=t("8b"),
                        snap_window=t("100ms"))


def test_custom_checks_times_inside_container_fields(eks_registry: Registry) -> None:
    @eks.effect(description="Pulses at several times")
    class Multi(EffectBase, frozen=True):
        kind: Literal["multi"] = "multi"
        times: tuple[Time, ...] = ()
        pair: tuple[Time, float] | None = None
        named: dict[str, Time] | None = None
        listed: list[Time] | None = None
        label: str | Time = "x"

    multi = fx.custom(
        "multi", times=(t("1s"), t("2s")), pair=(t("500ms"), 0.5), named={"drop": t("beat:8")},
        listed=[t("1b")], label="plain text")
    assert isinstance(multi, Multi)
    assert multi.times == (t("1s"), t("2s"))
    assert multi.model_dump(mode="json") == {
        "kind": "multi", "times": ["1s", "2s"], "pair": ["0.5s", 0.5], "named": {"drop": "beat:8"},
        "listed": ["1b"], "label": "plain text"}
    with pytest.raises(TypeError, match=r"field 'times'\[1\].*pass t"):
        fx.custom("multi", times=(t("1s"), "2s"))
    with pytest.raises(TypeError, match=r"field 'pair'\[0\].*bare numbers"):
        fx.custom("multi", pair=(1.0, 0.5))
    with pytest.raises(TypeError, match=r"field 'named'\['drop'\]"):
        fx.custom("multi", named={"drop": "beat:8"})
    with pytest.raises(TypeError, match=r"field 'listed'\[0\]"):
        fx.custom("multi", listed=["1b"])
