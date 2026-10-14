from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks.gfx import GfxBuilder, LayerAction, LayerHandle
from eks.scenes import embed_scene, scene_event
from eks.tracing import TraceError, current
from eks_harness.ir.hashing import canonical_json, derive_id
from eks_harness.ir.ops.gfx import Animate, Pulse, SetProp
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, RegistryError
from eks_harness.ir.time import TimeError


def define_intro() -> Callable[..., Any]:
    # a Blender scene whose render emits the event the titles listen to
    @eks.scene(kind="blender")
    def intro(sc: eks.BlenderScene) -> None:
        sc.empty(id="logo")
        sc.emits("logo.done")

    return intro


def define_titles() -> Callable[..., Any]:
    intro = define_intro()

    @eks.scene(kind="gfx", size=(1080, 1920), fps=30, seed=3)
    def titles(sc: eks.GfxScene, logo: eks.Media) -> None:
        intro_render = embed_scene(current(), intro, {})
        title = sc.text("Anlık kur", id="title", y=-600.0, size=96.0, color="#f5c542")
        card = sc.rect(900.0, 300.0, radius=24.0, fill="#101828")
        with sc.group(id="badge", x=120.0, blend="screen") as badge:
            sc.ellipse(80.0, 80.0, fill="#ffffff")
            mark = sc.image(logo, width=64.0, height=64.0)
        sc.mask(card, title)
        title.animate("opacity", {t("0s"): 0.0, t("0.5s"): 1.0}, ease="out_cubic")
        badge.show(t("beat:4"), t("beat:12"))
        sc.on(t.beats("downbeat")[0:4], title.pulse(amount=1.1, duration=t("200ms")))
        sc.on(scene_event(intro_render, "logo.done"), mark.assign("opacity", 0.5))
        sc.emits("titles.done")

    return titles


def render_node(plan: Plan) -> Node:
    (node,) = [node for node in plan.nodes if node.op == "gfx.render"]
    return node


def scene_args(plan: Plan) -> dict[str, Any]:
    return render_node(plan).args["scene"]  # type: ignore[return-value]


def plan_gfx(body: Callable[[GfxBuilder], object], **options: Any) -> Plan:
    @eks.scene(kind="gfx", **options)
    def custom(sc: eks.GfxScene) -> None:
        body(sc)

    return eks.plan(custom)


def test_gfx_render_args_json(eks_registry: Registry) -> None:
    plan = eks.plan(define_titles(), logo="artifact:LOGO1")
    assert [node.op for node in plan.nodes] == ["media.source", "blender.render", "gfx.render"]
    logo, intro, render = plan.nodes
    assert render.id == derive_id("n", "scene:titles/final:gfx.render#0")
    # the event trigger is a node ref, so the render waits for the intro and its hash covers it
    assert render.after == [logo.id, intro.id]
    assert render.args == {
        "cpu": False,
        "scene": {
            "size": [1080, 1920],
            "fps": 30,
            "seed": 3,
            "layers": [
                {
                    "kind": "text", "id": "title", "x": 0.0, "y": -600.0, "anchor": [0.5, 0.5], "scale": 1.0,
                    "rotation": 0.0, "blend": "normal", "text": "Anlık kur", "font": "IBM Plex Sans", "weight": 600,
                    "color": "#f5c542", "align": "center", "tracking": 0.0, "size": 96.0,
                    "opacity": {"keys": [
                        {"t": "0s", "v": 0.0, "interp": "linear"},
                        {"t": "0.5s", "v": 1.0, "ease": {"kind": "out_cubic"}, "interp": "linear"},
                    ]},
                },
                {
                    "kind": "rect", "id": "rect1", "x": 0.0, "y": 0.0, "anchor": [0.5, 0.5], "scale": 1.0,
                    "rotation": 0.0, "opacity": 1.0, "blend": "normal", "mask": "title", "width": 900.0,
                    "height": 300.0, "radius": 24.0, "fill": "#101828", "stroke_width": 0.0,
                },
                {
                    "kind": "group", "id": "badge", "start": "beat:4", "end": "beat:12", "x": 120.0, "y": 0.0,
                    "anchor": [0.5, 0.5], "scale": 1.0, "rotation": 0.0, "opacity": 1.0, "blend": "screen",
                    "children": [
                        {
                            "kind": "ellipse", "id": "ellipse1", "x": 0.0, "y": 0.0, "anchor": [0.5, 0.5],
                            "scale": 1.0, "rotation": 0.0, "opacity": 1.0, "blend": "normal", "width": 80.0,
                            "height": 80.0, "fill": "#ffffff", "stroke_width": 0.0,
                        },
                        {
                            "kind": "image", "id": "image1", "x": 0.0, "y": 0.0, "anchor": [0.5, 0.5],
                            "scale": 1.0, "rotation": 0.0, "opacity": 1.0, "blend": "normal",
                            "media": {"$ref": logo.id, "path": []}, "width": 64.0, "height": 64.0,
                            "fit": "contain",
                        },
                    ],
                },
            ],
            "triggers": [
                {
                    "at": {"stream": "downbeat", "start": 0, "stop": 4, "step": 1, "times": [], "offset": "0s"},
                    "layer": "title",
                    "action": {"kind": "pulse", "prop": "scale", "amount": 1.1, "duration": "0.2s"},
                },
                {
                    "at": {"source": {"$ref": intro.id, "path": []}, "name": "logo.done", "where": {}},
                    "layer": "image1",
                    "action": {"kind": "set", "prop": "opacity", "value": 0.5},
                },
            ],
            "events": ["titles.done"],
        },
    }


def test_gfx_scene_clock_and_determinism(eks_registry: Registry) -> None:
    titles = define_titles()
    first = eks.plan(titles, logo="artifact:LOGO1")
    second = eks.plan(titles, logo="artifact:LOGO1")
    assert first.clock.fps == 30
    assert first.clock.tempo is not None
    assert first.clock.tempo.external is True
    assert canonical_json(first.model_dump(mode="json")) == canonical_json(second.model_dump(mode="json"))


def test_default_size_duration_and_background(eks_registry: Registry) -> None:
    scene = scene_args(plan_gfx(lambda sc: None, duration=t("6s")))
    assert scene["size"] == [1080, 1920]
    assert scene["duration"] == "6s"
    assert "fps" not in scene
    assert scene["layers"] == []


def test_layer_ids_count_per_kind(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        sc.text("a")
        sc.text("b", id="text2")
        sc.text("c")
        sc.rect(10.0, 10.0)
        sc.path("M0 0 L10 10")
        with sc.group():
            sc.rect(5.0, 5.0)

    layers = scene_args(plan_gfx(body))["layers"]
    assert [layer["id"] for layer in layers] == ["text1", "text2", "text3", "rect1", "path1", "group1"]
    assert [child["id"] for child in layers[-1]["children"]] == ["rect2"]


def test_duplicate_and_invalid_layer_ids(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        sc.text("a", id="title")
        sc.rect(1.0, 1.0, id="title")

    with pytest.raises(TraceError, match="'title' is used twice"):
        plan_gfx(body)
    with pytest.raises(ValueError, match="not valid"):
        plan_gfx(lambda sc: sc.text("a", id="has space"))


def test_nested_groups(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        with sc.group(id="outer"):
            sc.text("in outer")
            with sc.group(id="inner", x=10.0):
                sc.rect(1.0, 1.0)
            sc.text("after inner")
        sc.text("root")

    layers = scene_args(plan_gfx(body))["layers"]
    assert [layer["id"] for layer in layers] == ["outer", "text3"]
    outer = layers[0]
    assert [child["id"] for child in outer["children"]] == ["text1", "inner", "text2"]
    assert [child["id"] for child in outer["children"][1]["children"]] == ["rect1"]


def test_empty_group_has_no_children(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        with sc.group(id="empty"):
            pass

    assert scene_args(plan_gfx(body))["layers"][0]["children"] == []


def test_group_closes_on_errors(eks_registry: Registry) -> None:
    kept: list[GfxBuilder] = []

    def body(sc: GfxBuilder) -> None:
        kept.append(sc)
        try:
            with sc.group(id="broken"):
                raise KeyError("boom")
        except KeyError:
            pass
        sc.text("root")

    layers = scene_args(plan_gfx(body))["layers"]
    assert [layer["id"] for layer in layers] == ["broken", "text1"]


def test_other_layer_kinds(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def kinds(sc: eks.GfxScene, clip: eks.Media) -> None:
        sc.path("M0 0 L1 1", stroke="#ffffff", stroke_width=2.0, trim_end=0.5, start=t("1s"))
        sc.video(clip, src=t.span(t("2s"), t("6s")), speed=2.0, fit="contain", end=t("end"))
        sc.ellipse(10.0, 20.0, stroke="#000000")

    plan = eks.plan(kinds, clip="artifact:CLIP1")
    path, video, ellipse = scene_args(plan)["layers"]
    assert (path["d"], path["stroke"], path["stroke_width"], path["trim_end"], path["start"]) == (
        "M0 0 L1 1", "#ffffff", 2.0, 0.5, "1s")
    assert video["src"] == {"start": "2s", "end": "6s"}
    assert (video["speed"], video["fit"], video["end"], video["media"]["$ref"]) == (2.0, "contain", "end",
                                                                                     plan.nodes[0].id)
    assert ellipse["stroke"] == "#000000"


def test_video_src_must_be_a_span(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def bad(sc: eks.GfxScene, clip: eks.Media) -> None:
        sc.video(clip, src=(t("1s"), t("2s")))  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Span"):
        eks.plan(bad, clip="artifact:CLIP1")


def test_animated_values_take_keys_and_curves(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        sc.text("a", x=eks.keys({t("0s"): 0.0, t("1s"): 100.0}), y=eks.curves.sine(2.0))

    layer = scene_args(plan_gfx(body))["layers"][0]
    assert layer["x"]["keys"][1] == {"t": "1s", "v": 100.0, "interp": "linear"}
    assert layer["y"]["curve"]["kind"] == "sine"


def test_set_and_show(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        sc.text("a", id="a").set(opacity=0.5, weight=800, rotation=eks.curves.sine(1.0)).show(end=t("bar:2"))

    layer = scene_args(plan_gfx(body))["layers"][0]
    assert (layer["opacity"], layer["weight"], layer["end"], "start" in layer) == (0.5, 800, "bar:2", False)
    assert layer["rotation"]["curve"]["kind"] == "sine"


def test_set_errors(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="has no property 'radius'"):
        plan_gfx(lambda sc: sc.text("a").set(radius=2.0))
    with pytest.raises(TypeError, match="has no property 'children'"):
        plan_gfx(lambda sc: sc.text("a").set(children=[]))
    with pytest.raises(ValidationError):
        plan_gfx(lambda sc: sc.text("a").set(weight=1000))
    with pytest.raises(TypeError, match="needs start=, end= or both"):
        plan_gfx(lambda sc: sc.text("a").show())
    with pytest.raises(TypeError, match="pass t\\('1s'\\)"):
        plan_gfx(lambda sc: sc.text("a").show("1s"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pass t\\('1s'\\)"):
        plan_gfx(lambda sc: sc.text("a", start="1s"))  # type: ignore[arg-type]


def test_animate_errors(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="non-empty mapping"):
        plan_gfx(lambda sc: sc.text("a").animate("opacity", {}))
    with pytest.raises(TypeError, match="has no property 'zoom'"):
        plan_gfx(lambda sc: sc.text("a").animate("zoom", {t("0s"): 1.0}))
    with pytest.raises(TypeError, match="pass t\\('0s'\\)"):
        plan_gfx(lambda sc: sc.text("a").animate("opacity", {"0s": 1.0}))  # type: ignore[dict-item]
    # non-animatable fields get the same clear message as tween, not a raw pydantic error
    with pytest.raises(TypeError, match="'weight' of text layer .* is not animatable; animate one of"):
        plan_gfx(lambda sc: sc.text("a").animate("weight", {t("0s"): 400}))


def test_pulse_duration_must_be_positive_unanchored(eks_registry: Registry) -> None:
    with pytest.raises(TimeError):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.text("a").pulse(amount=1.1, duration=t("bar:2"))))
    with pytest.raises(TimeError):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.text("a").pulse(amount=1.1, duration=t("0s"))))
    with pytest.raises(TimeError):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.text("a").pulse(amount=1.1, duration=t("-1s"))))


def test_mask_rules(eks_registry: Registry) -> None:
    def itself(sc: GfxBuilder) -> None:
        layer = sc.text("a")
        sc.mask(layer, layer)

    with pytest.raises(TraceError, match="cannot mask itself"):
        plan_gfx(itself)
    with pytest.raises(TypeError, match="takes a layer"):
        plan_gfx(lambda sc: sc.mask(sc.text("a"), 3))  # type: ignore[arg-type]
    with pytest.raises(TraceError, match="not a layer of this gfx scene"):
        plan_gfx(lambda sc: sc.mask(sc.text("a"), "ghost"))  # type: ignore[arg-type]

    other: list[LayerHandle] = []
    plan_gfx(lambda sc: other.append(sc.text("elsewhere")))
    with pytest.raises(TraceError, match="another gfx scene"):
        plan_gfx(lambda sc: sc.mask(sc.text("a"), other[0]))


def test_set_mask_by_handle(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        shape = sc.ellipse(10.0, 10.0, id="shape")
        sc.text("a", id="a").set(mask=shape)

    layers = scene_args(plan_gfx(body))["layers"]
    assert layers[1]["mask"] == "shape"


def test_set_mask_to_itself_raises(eks_registry: Registry) -> None:
    def by_handle(sc: GfxBuilder) -> None:
        layer = sc.text("a")
        layer.set(mask=layer)

    with pytest.raises(TraceError, match="layer 'text1' cannot mask itself"):
        plan_gfx(by_handle)
    with pytest.raises(TraceError, match="layer 'r' cannot mask itself"):
        plan_gfx(lambda sc: sc.rect(1.0, 1.0, id="r").set(mask="r"))


def test_mask_cycles_raise(eks_registry: Registry) -> None:
    def two_layers(sc: GfxBuilder) -> None:
        a = sc.text("a", id="a")
        b = sc.text("b", id="b")
        sc.mask(a, b)
        sc.mask(b, a)

    with pytest.raises(TraceError, match="mask of layer 'b' would form a cycle: b -> a -> b"):
        plan_gfx(two_layers)

    def three_layers(sc: GfxBuilder) -> None:
        a, b, c = (sc.rect(1.0, 1.0, id=name) for name in "abc")
        a.set(mask=b)
        b.set(mask=c)
        c.set(mask=a)

    with pytest.raises(TraceError, match="mask of layer 'c' would form a cycle: c -> a -> b -> c"):
        plan_gfx(three_layers)

    def assigned(sc: GfxBuilder) -> None:
        a = sc.text("a", id="a")
        b = sc.text("b", id="b")
        sc.mask(a, b)
        sc.on(t("1s"), b.assign("mask", a))

    with pytest.raises(TraceError, match="would form a cycle: b -> a -> b"):
        plan_gfx(assigned)


def test_mask_by_ancestor_group_raises(eks_registry: Registry) -> None:
    def child_masked_by_parent(sc: GfxBuilder) -> None:
        with sc.group(id="outer") as outer, sc.group(id="inner"):
            title = sc.text("a", id="title")
            sc.mask(title, outer)

    with pytest.raises(TraceError, match="would form a cycle: title -> outer -> inner -> title"):
        plan_gfx(child_masked_by_parent)

    def child_masked_by_layer_masked_by_parent(sc: GfxBuilder) -> None:
        shape = sc.ellipse(10.0, 10.0, id="shape")
        with sc.group(id="g") as g:
            title = sc.text("a", id="title")
            sc.mask(title, shape)
        sc.mask(shape, g)

    with pytest.raises(TraceError, match="mask of layer 'shape' would form a cycle: shape -> g -> title -> shape"):
        plan_gfx(child_masked_by_layer_masked_by_parent)


def test_valid_masks_are_kept(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        a, b, c = (sc.rect(1.0, 1.0, id=name) for name in "abc")
        sc.mask(a, b)
        sc.mask(b, c)
        sc.mask(a, c)
        with sc.group(id="g") as g:
            inner = sc.text("x", id="inner")
        sc.mask(g, inner)

    layers = {layer["id"]: layer for layer in scene_args(plan_gfx(body))["layers"]}
    assert layers["a"]["mask"] == "c"
    assert layers["b"]["mask"] == "c"
    assert layers["g"]["mask"] == "inner"


def test_actions(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        title = sc.text("a", id="title")
        pulse = title.pulse(amount=1.2)
        tween = title.tween("y", {t("0s"): 0.0, t("1b"): -40.0}, ease="out_back")
        assign = title.assign("color", "#000000")
        assert isinstance(pulse, LayerAction)
        assert isinstance(pulse.action, Pulse)
        assert isinstance(tween.action, Animate)
        assert isinstance(assign.action, SetProp)
        sc.on(t("beat:1"), pulse, tween, assign)

    triggers = scene_args(plan_gfx(body))["triggers"]
    assert [trigger["at"] for trigger in triggers] == ["beat:1"] * 3
    assert triggers[0]["action"] == {"kind": "pulse", "prop": "scale", "amount": 1.2, "duration": "0.5b"}
    assert triggers[1]["action"] == {"kind": "animate", "prop": "y", "keys": [
        {"t": "0s", "v": 0.0, "interp": "linear"},
        {"t": "1b", "v": -40.0, "ease": {"kind": "out_back"}, "interp": "linear"},
    ]}
    assert triggers[2]["action"] == {"kind": "set", "prop": "color", "value": "#000000"}


def test_action_errors(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="has no property 'glow'"):
        plan_gfx(lambda sc: sc.text("a").pulse(prop="glow", amount=1.0))
    with pytest.raises(TypeError, match="at least one action"):
        plan_gfx(lambda sc: sc.on(t("1s")))
    with pytest.raises(TypeError, match="actions built by a layer"):
        plan_gfx(lambda sc: sc.on(t("1s"), "pulse"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pass t\\('1s'\\)"):
        plan_gfx(lambda sc: sc.on("1s", sc.text("a").pulse(amount=1.1)))  # type: ignore[arg-type]
    with pytest.raises(TraceError, match="not a layer"):
        plan_gfx(lambda sc: sc.on(t("1s"), LayerAction("ghost", SetProp(prop="x", value=1))))


def test_assign_none_clears_an_optional_property(eks_registry: Registry) -> None:
    from eks_harness.ir.ops.gfx import GfxTrigger

    def body(sc: GfxBuilder) -> None:
        rect = sc.rect(100.0, 100.0, fill="#ff0000")
        sc.on(t("bar:1"), rect.assign("fill", None), rect.assign("blur", None))

    fill, blur = scene_args(plan_gfx(body))["triggers"]
    # None is the default of SetProp.value, so the canonical IR leaves it out
    assert fill["action"] == {"kind": "set", "prop": "fill"}
    assert blur["action"] == {"kind": "set", "prop": "blur"}
    assert GfxTrigger.model_validate(fill).action.model_dump() == {"kind": "set", "prop": "fill", "value": None}


def test_assign_validates_the_value_type(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="'banana' is not a valid 'opacity' for rect layer 'rect1'"):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.rect(1.0, 1.0).assign("opacity", "banana")))
    with pytest.raises(TypeError, match="5 is not a valid 'fill' for rect layer 'rect1'"):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.rect(1.0, 1.0).assign("fill", 5)))
    with pytest.raises(TypeError, match="not a valid 'opacity'"):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.rect(1.0, 1.0).assign("opacity", None)))
    with pytest.raises(TraceError, match="not a layer of this gfx scene"):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.rect(1.0, 1.0).assign("mask", "ghost")))
    with pytest.raises(TraceError, match="cannot mask itself"):
        plan_gfx(lambda sc: sc.on(t("1s"), sc.rect(1.0, 1.0, id="r").assign("mask", "r")))


def test_assign_mask_by_handle_and_animated_values(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        card = sc.rect(10.0, 10.0, id="card")
        shape = sc.ellipse(5.0, 5.0, id="shape")
        sc.on(t("1s"), card.assign("mask", shape), card.assign("opacity", eks.keys({t("0s"): 0.0, t("1s"): 1.0})))

    mask, opacity = scene_args(plan_gfx(body))["triggers"]
    assert mask["action"] == {"kind": "set", "prop": "mask", "value": "shape"}
    assert opacity["action"]["value"]["keys"][1] == {"t": "1s", "v": 1.0, "interp": "linear"}


def test_pulse_and_tween_only_animatable_properties(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="'text' of text layer 'text1' is not an animatable number"):
        plan_gfx(lambda sc: sc.text("a").pulse(prop="text", amount=1.1))
    with pytest.raises(TypeError, match="'color' of text layer 'text1' is not an animatable number"):
        plan_gfx(lambda sc: sc.text("a").pulse(prop="color", amount=1.1))
    with pytest.raises(TypeError, match="'font' of text layer 'text1' is not animatable; tween one of"):
        plan_gfx(lambda sc: sc.text("a").tween("font", {t("0s"): "Inter", t("1s"): "Lora"}))
    with pytest.raises(TypeError, match="is not a valid 'opacity'"):
        plan_gfx(lambda sc: sc.text("a").tween("opacity", {t("0s"): "dim", t("1s"): 1.0}))

    def body(sc: GfxBuilder) -> None:
        title = sc.text("a", id="title")
        sc.on(t("1s"), title.pulse(prop="blur", amount=2.0), title.tween("color", {t("0s"): "#000000", t("1s"): "#FFFFFF"}))

    blur, color = scene_args(plan_gfx(body))["triggers"]
    assert blur["action"]["prop"] == "blur"
    assert color["action"]["keys"][1]["v"] == "#ffffff"


def test_text_from_a_ref(eks_registry: Registry) -> None:
    @eks.scene(kind="gfx")
    def captioned(sc: eks.GfxScene, logo: eks.Media) -> None:
        sc.text(logo.uri)

    plan = eks.plan(captioned, logo="artifact:LOGO1")
    assert scene_args(plan)["layers"][0]["text"] == {"$ref": plan.nodes[0].id, "path": ["uri"]}


def test_emits_dedupes_and_validates(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        sc.emits("a", "b")
        sc.emits("a", "c")

    assert scene_args(plan_gfx(body))["events"] == ["a", "b", "c"]
    with pytest.raises(ValueError, match="not an event name"):
        plan_gfx(lambda sc: sc.emits(""))


def test_builder_is_closed_after_render(eks_registry: Registry) -> None:
    kept: list[GfxBuilder] = []
    plan_gfx(kept.append)
    with pytest.raises(TraceError, match="already rendered"):
        kept[0].text("late")


def test_finalize_with_an_open_group_raises(eks_registry: Registry) -> None:
    def body(sc: GfxBuilder) -> None:
        group = sc.group()
        group.__enter__()
        sc.finalize()

    with pytest.raises(TraceError, match="still open"):
        plan_gfx(body)


def test_wrong_scene_kind_for_the_gfx_builder(eks_registry: Registry) -> None:
    from eks.gfx import open_gfx
    from eks.planning import trace

    @eks.scene(kind="web", entry="scenes/x/index.html")
    def web(sc: eks.WebScene) -> None:
        return None

    with pytest.raises(TraceError, match="is a web scene, not a gfx scene"):
        trace(eks_registry.entry_for(web), root=open_gfx)


def test_scene_event_rejects_non_render_refs(eks_registry: Registry) -> None:
    from eks.scenes import scene_event

    seen: list[str] = []

    @eks.scene(kind="gfx")
    def probe(sc: eks.GfxScene, logo: eks.Media) -> None:
        with pytest.raises(TraceError, match="which is media.source; events come from"):
            scene_event(logo, "done")
        with pytest.raises(TypeError, match="not a path"):
            scene_event(logo.uri, "done")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="takes the render"):
            scene_event("logo", "done")  # type: ignore[arg-type]
        seen.append("checked")

    eks.plan(probe, logo="artifact:LOGO1")
    assert seen == ["checked"]


def test_cpu_reference_path(eks_registry: Registry) -> None:
    gpu = plan_gfx(lambda sc: sc.rect(10.0, 10.0))
    assert render_node(gpu).args["cpu"] is False
    cpu = plan_gfx(lambda sc: sc.rect(10.0, 10.0), cpu=True)
    assert render_node(cpu).args["cpu"] is True
    # the renderer choice is part of the node hash, so goldens never share a cache entry with GPU renders
    assert render_node(cpu).hash != render_node(gpu).hash
    assert eks.validate(cpu).issues == ()

    @eks.scene(kind="gfx", cpu=True)
    def badge(sc: eks.GfxScene) -> None:
        sc.rect(4.0, 4.0)

    @eks.edit(fps=30, size=(1080, 1920))
    def cut(e: eks.Edit) -> None:
        e.track("main").clip(embed_scene(current(), badge, {}, kind="gfx"), start=t("0s"))

    (embedded,) = [node for node, _ in eks.plan(cut).walk() if node.op == "gfx.render"]
    assert embedded.args["cpu"] is True
    with pytest.raises(RegistryError, match="CPU reference renderer of gfx scenes"):
        eks.scene(kind="blender", cpu=True)
