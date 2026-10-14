import json
import textwrap
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.blender import HALF_BEAT, BlenderBuilder, node_slug, resolve_engine
from eks.discovery import load_file
from eks.ref import RefUsageError
from eks.scenes import scene_event
from eks.tracing import TraceError
from eks_harness.ir.hashing import canonical_json
from eks_harness.ir.ops.blender import DEFAULT_SAMPLES
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import Time, TimeError


def define_phone() -> Callable[..., Any]:
    # design.md 10.4, with the bpy hook body spelled out
    @eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
    def phone(sc: eks.BlenderScene, screen: eks.Media):
        p = sc.load("assets/phone.blend", obj="Phone")
        p.material("Screen").texture("screen", screen)
        cam = sc.camera(lens=50).look_at(p)
        cam.animate("location", {t("bar:0"): (0, -4, 1), t("bar:4"): (0, -2, 1)}, ease="out_cubic")
        sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

        @sc.bpy
        def rig(bpy, ctx):
            return None

    return phone


def render_node(plan: Plan) -> Node:
    renders = [node for node in plan.nodes if node.op == "blender.render"]
    assert len(renders) == 1
    return renders[0]


def scene_args(plan: Plan) -> dict[str, Any]:
    return render_node(plan).args["scene"]  # type: ignore[return-value]


def source_of(plan: Plan, ref: Any) -> dict[str, Any]:
    # file inputs are media.source nodes; return the args of the one a $ref names
    assert isinstance(ref, dict) and ref.get("path") == []
    (node,) = [node for node in plan.nodes if node.id == ref["$ref"]]
    assert node.op == "media.source"
    return node.args  # type: ignore[return-value]


def plan_scene(body: Callable[[BlenderBuilder], object], **options: Any) -> Plan:
    @eks.scene(kind="blender", **options)
    def custom(sc: eks.BlenderScene) -> None:
        body(sc)

    return eks.plan(custom)


def test_design_phone_scene_plans_one_source_and_one_render(eks_registry: Registry) -> None:
    plan = eks.plan(define_phone(), screen="artifact:SCREEN1")
    # the screen parameter and the .blend file are both media.source nodes the render depends on
    assert [node.op for node in plan.nodes] == ["media.source", "media.source", "blender.render"]
    source, blend = plan.nodes[:2]
    assert source.args == {"uri": "artifact:SCREEN1"}
    assert blend.args == {"uri": "file:assets/phone.blend", "kind": "file"}
    assert plan.params == {"screen": "artifact:SCREEN1"}
    render = render_node(plan)
    assert sorted(render.after) == sorted([source.id, blend.id])
    assert render.out == "media"
    assert plan.clock.tempo is not None
    assert plan.clock.tempo.external is True
    assert plan.clock.seed == 7


def test_design_phone_render_settings_are_explicit(eks_registry: Registry) -> None:
    scene = scene_args(eks.plan(define_phone(), screen="artifact:SCREEN1"))
    render = scene["render"]
    assert render["engine"] == "CYCLES"
    assert render["samples"] == 32
    assert render["device"] == "GPU"
    assert render["seed"] == 7
    assert render["size"] == [1080, 1920]
    assert render["transparent"] is True
    assert "fps" not in render


def test_design_phone_nodes_texture_animation_trigger_and_hook(eks_registry: Registry) -> None:
    plan = eks.plan(define_phone(), screen="artifact:SCREEN1")
    scene = scene_args(plan)
    load, camera = scene["nodes"]
    assert load["kind"] == "load"
    assert load["obj"] == "Phone"
    assert source_of(plan, load["path"]) == {"uri": "file:assets/phone.blend", "kind": "file"}
    assert load["id"] == "phone"
    assert camera["kind"] == "camera"
    assert camera["lens"] == 50
    assert camera["look_at"] == load["id"]
    assert scene["camera"] == camera["id"] == "camera"
    assert scene["textures"] == [{
        "node": load["id"], "material": "Screen", "slot": "screen",
        "media": {"$ref": plan.nodes[0].id, "path": []},
    }]
    (animation,) = scene["animations"]
    assert animation["node"] == camera["id"]
    assert animation["prop"] == "location"
    assert [(key["t"], key["v"]) for key in animation["keys"]] == [("bar:0", [0, -4, 1]), ("bar:4", [0, -2, 1])]
    assert animation["ease"] == {"kind": "out_cubic"}
    (trigger,) = scene["triggers"]
    assert trigger["at"]["stream"] == "downbeat"
    assert (trigger["at"]["start"], trigger["at"]["stop"], trigger["at"]["step"]) == (0, 8, 1)
    assert trigger["node"] == load["id"]
    assert trigger["action"] == {"kind": "pulse", "prop": "scale", "amount": 1.04, "duration": "0.5b"}
    (hook,) = scene["hooks"]
    assert hook["phase"] == "build"
    assert hook["fn"]["name"] == "rig"
    assert hook["fn"]["source"] == "def rig(bpy, ctx):\n    return None\n"
    assert hook["fn"]["hash"].startswith("sha256:")
    assert hook["fn"]["params"] == ["bpy", "ctx"]
    assert scene["events"] == []


def test_render_node_id_comes_from_the_final_key(eks_registry: Registry) -> None:
    plan = eks.plan(define_phone(), screen="artifact:SCREEN1")
    from eks_harness.ir.hashing import derive_id

    assert render_node(plan).id == derive_id("n", "scene:phone/final:blender.render#0")
    assert render_node(plan).src is None


def test_engine_alias_eevee_is_the_default(eks_registry: Registry) -> None:
    plan = plan_scene(lambda sc: None)
    render = scene_args(plan)["render"]
    assert render["engine"] == "BLENDER_EEVEE_NEXT"
    assert render["samples"] == DEFAULT_SAMPLES["BLENDER_EEVEE_NEXT"]
    assert render["device"] == "GPU"


@pytest.mark.parametrize(("alias", "engine"), [
    ("eevee", "BLENDER_EEVEE_NEXT"), ("eevee_next", "BLENDER_EEVEE_NEXT"), ("cycles", "CYCLES"),
    ("Cycles", "CYCLES"), ("workbench", "BLENDER_WORKBENCH"), ("CYCLES", "CYCLES"),
])
def test_engine_aliases_resolve_with_default_samples(eks_registry: Registry, alias: str, engine: str) -> None:
    render = scene_args(plan_scene(lambda sc: None, engine=alias))["render"]
    assert render["engine"] == engine
    assert render["samples"] == DEFAULT_SAMPLES[engine]  # type: ignore[index]


def test_explicit_samples_device_fps_and_duration(eks_registry: Registry) -> None:
    plan = plan_scene(lambda sc: None, engine="cycles", samples=128, device="CPU", fps=30, duration=t("4s"),
                      size=(1920, 1080), transparent=False)
    scene = scene_args(plan)
    assert scene["render"]["samples"] == 128
    assert scene["render"]["device"] == "CPU"
    assert scene["render"]["fps"] == 30
    assert scene["render"]["size"] == [1920, 1080]
    assert scene["render"]["transparent"] is False
    assert scene["duration"] == "4s"


def test_unknown_engine_raises(eks_registry: Registry) -> None:
    with pytest.raises(TraceError, match="unknown Blender engine 'octane'"):
        plan_scene(lambda sc: None, engine="octane")
    with pytest.raises(TraceError, match="cycles"):
        resolve_engine("redshift")


def test_duplicate_ids_raise(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.empty(id="rig")
        sc.mesh("cube", id="rig")

    with pytest.raises(TraceError, match="'rig' is used twice"):
        plan_scene(body)


def test_explicit_id_that_collides_with_a_derived_one_raises(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.load("assets/phone.blend")
        sc.empty(id="phone")

    with pytest.raises(TraceError, match="'phone' is used twice"):
        plan_scene(body)


def test_invalid_explicit_id_raises(eks_registry: Registry) -> None:
    with pytest.raises(ValueError, match="not valid"):
        plan_scene(lambda sc: sc.empty(id="9 lives"))


def test_derived_ids(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.load("assets/phone.blend")
        sc.load("assets/phone.blend", obj="Phone")
        sc.load("assets/Ekran Çerçevesi.blend")
        sc.load("assets/3d-logo.blend")
        sc.camera()
        sc.camera(active=False)
        sc.light()
        sc.light("sun")
        sc.mesh("cube")
        sc.mesh("cube")
        sc.text("EksenGOLD")
        sc.empty()

    nodes = scene_args(plan_scene(body))["nodes"]
    assert [node["id"] for node in nodes] == [
        "phone", "phone_2", "ekran_cercevesi", "_3d_logo", "camera", "camera_2", "light", "light_2",
        "cube", "cube_2", "text", "empty",
    ]


def test_node_slug() -> None:
    assert node_slug("Phone", "load") == "phone"
    assert node_slug("Şık Kutu", "load") == "sik_kutu"
    assert node_slug("---", "load") == "load"
    assert node_slug("x" * 100, "load") == "x" * 56


def test_second_active_camera_raises(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.camera()
        sc.camera()

    with pytest.raises(TraceError, match="already the active camera"):
        plan_scene(body)


def test_inactive_first_camera_leaves_the_active_one_to_later(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.camera(id="wide", active=False)
        sc.camera(id="close", lens=85, location=(0, -1, 1), rotation=(1.2, 0, 0))

    scene = scene_args(plan_scene(body))
    assert scene["camera"] == "close"
    close = scene["nodes"][1]
    assert close["lens"] == 85
    assert close["location"] == [0, -1, 1]
    assert close["rotation"] == [1.2, 0, 0]


def test_scene_without_camera_has_no_active_camera(eks_registry: Registry) -> None:
    assert "camera" not in scene_args(plan_scene(lambda sc: sc.empty()))


def test_node_fields(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.light("spot", energy=500.0, color="#ffeedd", size=2.0, location=(1, 2, 3))
        sc.mesh("plane", size=4.0, material="Floor", location=(0, 0, -1))
        sc.text("Kontör", font="fonts/Inter.ttf", size=0.5, extrude=0.1, id="label")
        sc.load("assets/set.blend", collection="Props", link=True, id="props")

    plan = plan_scene(body)
    light, plane, label, props = scene_args(plan)["nodes"]
    assert (light["light"], light["energy"], light["color"], light["size"]) == ("spot", 500.0, "#ffeedd", 2.0)
    assert light["location"] == [1, 2, 3]
    assert (plane["primitive"], plane["size"], plane["material"]) == ("plane", 4.0, "Floor")
    assert (label["body"], label["extrude"]) == ("Kontör", 0.1)
    assert source_of(plan, label["font"]) == {"uri": "file:fonts/Inter.ttf", "kind": "file"}
    assert source_of(plan, props["path"]) == {"uri": "file:assets/set.blend", "kind": "file"}
    assert (props["collection"], props["link"], "obj" in props) == ("Props", True, False)


def test_text_body_from_a_ref_is_encoded(eks_registry: Registry) -> None:
    @eks.scene(kind="blender")
    def titled(sc: eks.BlenderScene, screen: eks.Media) -> None:
        sc.text(f"take {screen.uri}")

    plan = eks.plan(titled, screen="artifact:SCREEN1")
    body = scene_args(plan)["nodes"][0]["body"]
    assert body == {"$fmt": ["take ", {"$ref": plan.nodes[0].id, "path": ["uri"]}]}


def test_bad_vectors_raise(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="three numbers"):
        plan_scene(lambda sc: sc.empty(location=(1, 2)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="finite numbers"):
        plan_scene(lambda sc: sc.empty(location=(1, True, 2)))
    with pytest.raises(TypeError, match="finite numbers"):
        plan_scene(lambda sc: sc.empty(location=(1, float("nan"), 2)))


def test_set_updates_fields_and_resolves_handles(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        rig = sc.empty(id="rig")
        sc.load("assets/phone.blend").set(parent=rig, scale=(2, 2, 2), visible=False, name="Telefon")

    rig, phone = scene_args(plan_scene(body))["nodes"]
    assert rig["id"] == "rig"
    assert phone["parent"] == "rig"
    assert phone["scale"] == [2, 2, 2]
    assert phone["visible"] is False
    assert phone["name"] == "Telefon"


def test_set_rejects_unknown_and_fixed_properties(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="has no property 'lens'"):
        plan_scene(lambda sc: sc.empty().set(lens=50))
    with pytest.raises(TypeError, match="has no property 'id'"):
        plan_scene(lambda sc: sc.empty().set(id="other"))


def test_set_validates_values(eks_registry: Registry) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        plan_scene(lambda sc: sc.camera().set(clip_start=-1))


def test_parent_must_be_a_node_of_the_scene(eks_registry: Registry) -> None:
    with pytest.raises(TraceError, match="not a node of this scene"):
        plan_scene(lambda sc: sc.empty().set(parent="ghost"))
    with pytest.raises(TraceError, match="cannot name the node itself"):
        plan_scene(lambda sc: sc.empty(id="solo").set(parent="solo"))


def test_parent_cycles_raise(eks_registry: Registry) -> None:
    def two(sc: BlenderBuilder) -> None:
        a = sc.empty(id="a")
        b = sc.empty(id="b")
        a.set(parent=b)
        b.set(parent=a)

    def three(sc: BlenderBuilder) -> None:
        a = sc.empty(id="a")
        b = sc.empty(id="b")
        c = sc.empty(id="c")
        a.set(parent=b)
        b.set(parent=c)
        c.set(parent="a")

    with pytest.raises(TraceError, match="would form a cycle: b -> a -> b"):
        plan_scene(two)
    with pytest.raises(TraceError, match="would form a cycle: c -> a -> b -> c"):
        plan_scene(three)


def test_parent_chain_without_cycle_and_reparenting(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        a = sc.empty(id="a")
        b = sc.empty(id="b")
        c = sc.empty(id="c")
        a.set(parent=b)
        b.set(parent=c)
        a.set(parent=c)
        b.set(parent=None)
        c.set(parent=b)

    a, b, c = scene_args(plan_scene(body))["nodes"]
    assert (a["parent"], "parent" in b, c["parent"]) == ("c", False, "b")


def test_parent_cycle_through_assign_raises(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        a = sc.empty(id="a")
        b = sc.empty(id="b")
        a.set(parent=b)
        sc.on(t("bar:1"), b.assign("parent", a))

    with pytest.raises(TraceError, match="would form a cycle: b -> a -> b"):
        plan_scene(body)


def test_assign_none_clears_an_optional_property(eks_registry: Registry) -> None:
    from eks_harness.ir.ops.blender import BlenderTrigger

    def body(sc: BlenderBuilder) -> None:
        rig = sc.empty(id="rig")
        logo = sc.empty(id="logo").set(parent=rig)
        sc.on(t("bar:1"), logo.assign("parent", None), logo.assign("name", None))

    unparent, unname = scene_args(plan_scene(body))["triggers"]
    # None is the default of SetProp.value, so the canonical IR leaves it out
    assert unparent["action"] == {"kind": "set", "prop": "parent"}
    assert unname["action"] == {"kind": "set", "prop": "name"}
    assert BlenderTrigger.model_validate(unparent).action.model_dump() == {
        "kind": "set", "prop": "parent", "value": None}


def test_assign_validates_known_properties(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="'banana' is not a valid 'visible' for empty node 'empty'"):
        plan_scene(lambda sc: sc.on(t("bar:1"), sc.empty().assign("visible", "banana")))
    with pytest.raises(TypeError, match="not a valid 'location'"):
        plan_scene(lambda sc: sc.on(t("bar:1"), sc.empty().assign("location", (1, 2))))
    with pytest.raises(TypeError, match="parent takes an object of this scene, got int 5"):
        plan_scene(lambda sc: sc.on(t("bar:1"), sc.empty().assign("parent", 5)))
    with pytest.raises(TraceError, match="not a node of this scene"):
        plan_scene(lambda sc: sc.on(t("bar:1"), sc.empty().assign("parent", "ghost")))
    with pytest.raises(TypeError, match="the id of node 'empty' is fixed"):
        plan_scene(lambda sc: sc.on(t("bar:1"), sc.empty().assign("id", "other")))


def test_assign_passes_custom_properties_and_handles(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        rig = sc.empty(id="rig")
        cam = sc.camera()
        sc.on(t("bar:1"), cam.assign("look_at", rig), cam.assign("data.dof.aperture_fstop", 2.8),
              cam.assign("location", (0, -3, 1)))

    look, custom, location = scene_args(plan_scene(body))["triggers"]
    assert look["action"] == {"kind": "set", "prop": "look_at", "value": "rig"}
    assert custom["action"] == {"kind": "set", "prop": "data.dof.aperture_fstop", "value": 2.8}
    assert location["action"] == {"kind": "set", "prop": "location", "value": [0, -3, 1]}


def test_pulse_only_animatable_numbers_of_the_node(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="'visible' of empty node 'empty' is not an animatable number"):
        plan_scene(lambda sc: sc.empty().pulse(prop="visible", amount=1.1))
    with pytest.raises(TypeError, match="not an animatable number; pulse one of .*lens"):
        plan_scene(lambda sc: sc.camera().pulse(prop="clip_start", amount=1.1))

    def body(sc: BlenderBuilder) -> None:
        cam = sc.camera()
        sc.on(t("bar:1"), cam.pulse(prop="lens", amount=1.2), cam.pulse(prop="data.dof.focus_distance", amount=2.0))

    lens, custom = scene_args(plan_scene(body))["triggers"]
    assert (lens["action"]["prop"], custom["action"]["prop"]) == ("lens", "data.dof.focus_distance")


def test_look_at_and_focus(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        phone = sc.load("assets/phone.blend", obj="Phone")
        sc.camera().look_at(phone).focus(phone, f_stop=2.8)

    camera = scene_args(plan_scene(body))["nodes"][1]
    assert (camera["look_at"], camera["focus"], camera["f_stop"]) == ("phone", "phone", 2.8)


def test_look_at_rejects_objects_of_other_scenes_and_itself(eks_registry: Registry) -> None:
    other: list[Any] = []

    def first(sc: BlenderBuilder) -> None:
        other.append(sc.empty(id="elsewhere"))

    plan_scene(first)

    def second(sc: BlenderBuilder) -> None:
        sc.empty(id="elsewhere")
        sc.camera().look_at(other[0])

    with pytest.raises(TraceError, match="another scene"):
        plan_scene(second)

    def itself(sc: BlenderBuilder) -> None:
        camera = sc.camera()
        camera.look_at(camera)

    with pytest.raises(TraceError, match="itself"):
        plan_scene(itself)


def test_texture_rejects_an_empty_slot(eks_registry: Registry) -> None:
    @eks.scene(kind="blender")
    def textured(sc: eks.BlenderScene, screen: eks.Media) -> None:
        sc.load("assets/phone.blend").material("Screen").texture("", screen)

    with pytest.raises(TypeError, match="slot name"):
        eks.plan(textured, screen="artifact:SCREEN1")


def test_material_name_is_required(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="material name"):
        plan_scene(lambda sc: sc.empty().material(""))


def test_texture_chains_and_accepts_a_media_string(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        screen = sc.load("assets/phone.blend").material("Screen")
        screen.texture("screen", "artifact:SCREEN1").texture("glow", "artifact:SCREEN1")

    plan = plan_scene(body)
    (source,) = [node for node in plan.nodes if node.op == "media.source" and node.args["uri"] == "artifact:SCREEN1"]
    textures = scene_args(plan)["textures"]
    assert [binding["slot"] for binding in textures] == ["screen", "glow"]
    assert all(binding["media"] == {"$ref": source.id, "path": []} for binding in textures)


def test_animate_without_ease_and_rejects_bad_keys(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.empty(id="rig").animate("rotation", {t("start"): [0, 0, 0], t("end"): (0, 0, 6.283)})

    (animation,) = scene_args(plan_scene(body))["animations"]
    assert "ease" not in animation
    assert [key["t"] for key in animation["keys"]] == ["start", "end"]
    assert animation["keys"][1]["v"] == [0, 0, 6.283]
    with pytest.raises(TypeError, match="non-empty mapping"):
        plan_scene(lambda sc: sc.empty().animate("location", {}))
    with pytest.raises(TypeError, match="pass t\\('1s'\\)"):
        plan_scene(lambda sc: sc.empty().animate("location", {"1s": (0, 0, 0)}))  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="easing|ease"):
        plan_scene(lambda sc: sc.empty().animate("location", {t("0s"): (0, 0, 0)}, ease="wobbly"))


def test_animate_rejects_ref_values(eks_registry: Registry) -> None:
    @eks.scene(kind="blender")
    def animated(sc: eks.BlenderScene, screen: eks.Media) -> None:
        sc.empty().animate("location", {t("0s"): screen.width})

    with pytest.raises(RefUsageError, match="holds a Ref"):
        eks.plan(animated, screen="artifact:SCREEN1")


def test_animate_checks_props_and_keyframe_values(eks_registry: Registry) -> None:
    # wrong arity of a Vec3 keyframe
    with pytest.raises(TypeError, match="keyframe value \\(1, 2\\) is not a valid 'location'"):
        plan_scene(lambda sc: sc.empty().animate("location", {t("0s"): (1, 2)}))
    # a string on an animated float
    with pytest.raises(TypeError, match="keyframe value 'wide' is not a valid 'lens'"):
        plan_scene(lambda sc: sc.camera().animate("lens", {t("0s"): "wide"}))
    # a keyframe value may not itself be keys
    with pytest.raises(TypeError, match="plain value"):
        plan_scene(lambda sc: sc.camera().animate(
            "lens", {t("0s"): eks.keys({t("0s"): 1.0, t("1s"): 2.0})}))  # pyright: ignore[reportArgumentType]
    # model fields that are not animatable: fixed, references, plain settings
    for prop in ("id", "kind", "parent", "visible"):
        with pytest.raises(TypeError, match=f"{prop!r} of empty node 'empty' is not animatable; animate one of"):
            plan_scene(lambda sc, prop=prop: sc.empty().animate(prop, {t("0s"): 1.0}))
    for prop in ("clip_start", "look_at", "f_stop"):
        with pytest.raises(TypeError, match=f"{prop!r} of camera node 'camera' is not animatable"):
            plan_scene(lambda sc, prop=prop: sc.camera().animate(prop, {t("0s"): 1.0}))


def test_animate_normalizes_values_and_passes_custom_props(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        light = sc.light(id="key")
        light.animate("color", {t("0s"): "#ffffff", t("1s"): "#ff8800"})
        light.animate("energy", {t("0s"): 10, t("1s"): 500})
        sc.load("phone.blend", obj="Phone").animate("Screen.emission", {t("0s"): 0, t("1s"): {"any": [1, 2]}})

    color, energy, custom = scene_args(plan_scene(body))["animations"]
    assert color["prop"] == "color"
    assert [key["v"] for key in energy["keys"]] == [10.0, 500.0]
    assert all(isinstance(key["v"], float) for key in energy["keys"])
    assert custom["prop"] == "Screen.emission"
    assert custom["keys"][1]["v"] == {"any": [1, 2]}


def test_animate_twice_replaces_earlier_keys_in_place(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        rig = sc.empty(id="rig")
        rig.animate("location", {t("0s"): (0, 0, 0), t("1s"): (0, 0, 1)})
        rig.animate("scale", {t("0s"): (1, 1, 1), t("1s"): (2, 2, 2)})
        rig.animate("location", {t("0s"): (5, 5, 5)}, ease="out_cubic")

    location, scale = scene_args(plan_scene(body))["animations"]
    assert location["prop"] == "location"
    assert location["ease"] == {"kind": "out_cubic"}
    assert [key["v"] for key in location["keys"]] == [[5.0, 5.0, 5.0]]
    assert scale["prop"] == "scale"


def test_pulse_forms(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        logo = sc.empty(id="logo")
        sc.on(t("bar:1"), logo.pulse(scale=1.1), logo.pulse(prop="rotation", amount=0.2, duration=t("200ms"),
                                                            ease="out_back"))

    first, second = scene_args(plan_scene(body))["triggers"]
    assert first == {"at": "bar:1", "node": "logo",
                     "action": {"kind": "pulse", "prop": "scale", "amount": 1.1, "duration": "0.5b"}}
    assert second["action"] == {"kind": "pulse", "prop": "rotation", "amount": 0.2, "duration": "0.2s",
                                "ease": {"kind": "out_back"}}
    assert Time(beats=Fraction(1, 2)) == HALF_BEAT


def test_pulse_argument_errors(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="either scale= or amount="):
        plan_scene(lambda sc: sc.empty().pulse(scale=1.1, amount=1.2))
    with pytest.raises(TypeError, match="needs scale= or amount="):
        plan_scene(lambda sc: sc.empty().pulse())
    with pytest.raises(TypeError, match="pulses the scale"):
        plan_scene(lambda sc: sc.empty().pulse(scale=1.1, prop="rotation"))
    with pytest.raises(TypeError, match="pass t\\('1s'\\)"):
        plan_scene(lambda sc: sc.empty().pulse(scale=1.1, duration="1s"))  # type: ignore[arg-type]
    # pulse durations are unanchored and positive, like every other eks duration
    with pytest.raises(TimeError):
        plan_scene(lambda sc: sc.empty().pulse(scale=1.1, duration=t("bar:2")))
    with pytest.raises(TimeError):
        plan_scene(lambda sc: sc.empty().pulse(scale=1.1, duration=t("0s")))


def test_assign_emit_and_events(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        logo = sc.empty(id="logo")
        sc.emits("logo.done", "intro")
        sc.on(t("beat:4"), logo.assign("visible", False), logo.emit("logo.shown", data={"n": 1}))
        sc.on(t("beat:8"), logo.emit("intro"))
        sc.emits("intro")

    scene = scene_args(plan_scene(body))
    assign, shown, intro = scene["triggers"]
    assert assign["action"] == {"kind": "set", "prop": "visible", "value": False}
    assert shown["action"] == {"kind": "emit", "event": "logo.shown", "data": {"n": 1}}
    assert intro["action"]["event"] == "intro"
    assert scene["events"] == ["logo.done", "intro", "logo.shown"]


def test_bad_event_names_raise(eks_registry: Registry) -> None:
    with pytest.raises(ValueError, match="not an event name"):
        plan_scene(lambda sc: sc.emits("has space"))
    with pytest.raises(ValueError, match="not an event name"):
        plan_scene(lambda sc: sc.empty().emit("9lives"))


def test_trigger_errors(eks_registry: Registry) -> None:
    with pytest.raises(TypeError, match="pass t\\('bar:1'\\)"):
        plan_scene(lambda sc: sc.on("bar:1", sc.empty().pulse(scale=1.1)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="at least one action"):
        plan_scene(lambda sc: sc.on(t("bar:1")))
    with pytest.raises(TypeError, match="actions built by an object"):
        plan_scene(lambda sc: sc.on(t("bar:1"), "pulse"))  # type: ignore[arg-type]


def test_explicit_time_set_and_event_triggers(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        logo = sc.empty(id="logo")
        sc.on(t.set(t("1s"), t("beat:3")), logo.pulse(scale=1.2))
        sc.on(t.beats()[2:10:2] + t("2f"), logo.pulse(scale=1.3))

    first, second = scene_args(plan_scene(body))["triggers"]
    assert first["at"]["times"] == ["1s", "beat:3"]
    assert (second["at"]["stream"], second["at"]["step"], second["at"]["offset"]) == ("beat", 2, "2f")


def test_world(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        sc.world(hdri="studio.exr", strength=0.5)

    plan = plan_scene(body)
    world = scene_args(plan)["world"]
    assert world["strength"] == 0.5
    assert source_of(plan, world["hdri"]) == {"uri": "file:studio.exr", "kind": "image"}

    def twice(sc: BlenderBuilder) -> None:
        sc.world(color="#101010")
        sc.world(color="#202020")

    with pytest.raises(TraceError, match="already set"):
        plan_scene(twice)


def test_bpy_frame_hook_and_allowed_globals(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        amplitude = 0.25

        @sc.bpy_frame
        def wobble(bpy, ctx):
            import math

            bpy.data.objects["Phone"].rotation_euler.z = amplitude * math.sin(ctx.frame)

        assert callable(wobble)

    (hook,) = scene_args(plan_scene(body))["hooks"]
    assert hook["phase"] == "frame"
    assert hook["fn"]["freevars"] == {"amplitude": {"kind": "literal", "value": 0.25}}


def test_bpy_hook_rejects_unknown_globals(eks_registry: Registry) -> None:
    def body(sc: BlenderBuilder) -> None:
        @sc.bpy
        def leaky(bpy, ctx):
            return plan_scene

    with pytest.raises(TraceError, match="plan_scene"):
        plan_scene(body)


def test_bpy_returns_the_function(eks_registry: Registry) -> None:
    seen: list[Any] = []

    def body(sc: BlenderBuilder) -> None:
        def hook(bpy, ctx):
            return None

        seen.append(sc.bpy(hook) is hook)

    plan_scene(body)
    assert seen == [True]


def test_builder_is_closed_after_render(eks_registry: Registry) -> None:
    kept: list[BlenderBuilder] = []
    plan_scene(kept.append)
    with pytest.raises(TraceError, match="already rendered"):
        kept[0].empty()
    with pytest.raises(TraceError, match="already rendered"):
        kept[0].finalize()


def test_scene_event_on_a_blender_render(eks_registry: Registry) -> None:
    from eks.ref import Ref

    plan = eks.plan(define_phone(), screen="artifact:SCREEN1")
    render = render_node(plan)
    event = scene_event(Ref(render.id, "media"), "logo.done", index=2)
    # outside a trace there is nothing to check the event against
    assert (event.source.ref, event.source.path, event.name, event.where) == (
        render.id, (), "logo.done", {"index": 2})


def test_planning_twice_is_byte_identical(eks_registry: Registry) -> None:
    phone = define_phone()
    first = eks.plan(phone, screen="artifact:SCREEN1")
    second = eks.plan(phone, screen="artifact:SCREEN1")
    assert canonical_json(first.model_dump(mode="json")) == canonical_json(second.model_dump(mode="json"))
    assert first.hash == second.hash


SCENE_FILE = '''
import eks
from eks import t


@eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
def phone(sc: eks.BlenderScene, screen: eks.Media):
    p = sc.load("assets/phone.blend", obj="Phone")
{gap}    p.material("Screen").texture("screen", screen)
    sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

    @sc.bpy
    def rig(bpy, ctx):
        {body}
'''


def load_phone(path: Path, *, gap: str = "", body: str = "return None") -> Plan:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(SCENE_FILE).format(gap=gap, body=body), encoding="utf-8")
    (entry,) = load_file(path)
    return eks.plan(entry, screen="artifact:SCREEN1")


def hook_hash(plan: Plan) -> str:
    return scene_args(plan)["hooks"][0]["fn"]["hash"]


def test_blank_lines_outside_the_hook_keep_its_hash(tmp_path: Path, eks_registry: Registry) -> None:
    # the same file is edited and loaded again, as a developer would
    plain = load_phone(tmp_path / "phone.py")
    spaced = load_phone(tmp_path / "phone.py", gap="\n\n\n")
    assert hook_hash(plain) == hook_hash(spaced)
    assert render_node(plain).id == render_node(spaced).id
    assert json.dumps(render_node(plain).args, sort_keys=True) == json.dumps(render_node(spaced).args, sort_keys=True)
    assert render_node(plain).hash == render_node(spaced).hash


def test_a_change_inside_the_hook_changes_its_hash(tmp_path: Path, eks_registry: Registry) -> None:
    plain = load_phone(tmp_path / "phone.py")
    changed = load_phone(tmp_path / "phone.py", body="return 1")
    assert hook_hash(plain) != hook_hash(changed)
    assert render_node(plain).id == render_node(changed).id
    assert render_node(plain).hash != render_node(changed).hash
