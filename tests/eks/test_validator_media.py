from __future__ import annotations

from typing import Any

import eks
from eks import t
from eks.planning import trace
from eks.tracing import TraceContext
from eks_harness.engine.validator import Issue, Report, validate
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def render_node(plan: Plan, op: str) -> Node:
    return next(node for node, _ancestors in plan.walk() if node.op == op)


def blender_scene(**changes: Any) -> dict[str, Any]:
    scene: dict[str, Any] = {
        "nodes": [
            {"kind": "load", "id": "phone", "path": None, "obj": "Phone"},
            {"kind": "camera", "id": "camera", "lens": 50.0, "look_at": "phone"},
            {"kind": "light", "id": "key", "parent": "camera"},
        ],
        "textures": [{"node": "phone", "material": "Screen", "slot": "screen", "media": None}],
        "animations": [{"node": "camera", "prop": "location",
                        "keys": [{"t": "bar:0", "v": [0, -4, 1]}, {"t": "bar:4", "v": [0, -2, 1]}]}],
        "triggers": [{"at": t.beats("downbeat")[0:8], "node": "phone",
                      "action": {"kind": "pulse", "amount": 1.04, "duration": "0.25s"}}],
        "camera": "camera",
        "render": {"engine": "CYCLES", "samples": 32, "device": "GPU", "seed": 7, "size": [1080, 1920], "fps": 30},
    }
    scene.update(changes)
    return scene


def blender_plan(registry: Registry, scene: dict[str, Any]) -> Plan:
    @eks.scene(kind="blender", engine="cycles", samples=32, seed=7, bpm=120)
    def phone(sc: TraceContext) -> None:
        screen = sc.record("media.source", {"uri": "artifact:S1", "kind": "image"})
        blend = sc.record("media.source", {"uri": "file:assets/phone.blend", "kind": "file"})
        textures = [{**texture, "media": screen} for texture in scene["textures"]]
        nodes = [{**node, "path": blend} if node["kind"] == "load" else node for node in scene["nodes"]]
        sc.record("blender.render", {"scene": {**scene, "nodes": nodes, "textures": textures}})

    return trace(registry.entry("scene", "phone"), root=lambda ctx, _entry: ctx, registry=registry)


def test_valid_blender_scene(eks_registry: Registry) -> None:
    report = validate(blender_plan(eks_registry, blender_scene()))
    assert report.ok, report.text()
    assert report.issues == ()


def test_blender_render_settings_must_be_explicit(eks_registry: Registry) -> None:
    plan = blender_plan(eks_registry, blender_scene())
    node = render_node(plan, "blender.render")
    scene = node.args["scene"]
    assert isinstance(scene, dict)
    render = scene["render"]
    assert isinstance(render, dict)
    render["engine"] = "cycles"
    del render["samples"]
    render["device"] = "gpu"
    report = validate(plan)
    found = issues_of(report, "blender.render")
    assert [issue.field for issue in found] == ["scene.render.engine", "scene.render.samples",
                                                "scene.render.device"]
    assert "CYCLES" in found[0].message
    assert all(issue.node == node.id for issue in found)
    assert issues_of(report, "args.invalid")


def test_blender_look_at_a_missing_node(eks_registry: Registry) -> None:
    scene = blender_scene()
    scene["nodes"][1]["look_at"] = "tablet"
    [issue] = issues_of(validate(blender_plan(eks_registry, scene)), "scene.ref")
    assert issue.field == "scene.nodes.1.look_at"
    assert "'tablet'" in issue.message
    assert "'phone'" in issue.message


def test_blender_references_of_every_kind(eks_registry: Registry) -> None:
    scene = blender_scene(camera="cam2")
    scene["nodes"][2]["parent"] = "rig"
    scene["textures"][0]["node"] = "screen"
    scene["animations"][0]["node"] = "dolly"
    scene["triggers"][0]["node"] = "logo"
    found = issues_of(validate(blender_plan(eks_registry, scene)), "scene.ref")
    assert [issue.field for issue in found] == [
        "scene.nodes.2.parent",
        "scene.textures.0.node",
        "scene.animations.0.node",
        "scene.triggers.0.node",
        "scene.camera",
    ]


def gfx_plan(registry: Registry, scene: dict[str, Any]) -> Plan:
    @eks.scene(kind="gfx", fps=30, size=(1080, 1920))
    def titles(sc: TraceContext) -> None:
        sc.record("gfx.render", {"scene": scene})

    return trace(registry.entry("scene", "titles"), root=lambda ctx, _entry: ctx, registry=registry)


def gfx_scene() -> dict[str, Any]:
    return {
        "size": [1080, 1920],
        "layers": [
            {"kind": "rect", "id": "plate", "width": 900.0, "height": 200.0, "fill": "#101820"},
            {"kind": "group", "id": "copy", "mask": "plate", "children": [
                {"kind": "text", "id": "headline", "text": "EksenGOLD", "start": "0s", "end": "end-12f",
                 "opacity": {"keys": [{"t": "start", "v": 0.0}, {"t": "8f", "v": 1.0}]}},
                {"kind": "text", "id": "tagline", "text": "Anlık kur", "mask": "headline"},
            ]},
        ],
        "triggers": [
            {"at": t.beats("downbeat")[0:8], "layer": "headline",
             "action": {"kind": "pulse", "amount": 1.04, "duration": "0.25s"}},
            {"at": "beat:4", "layer": "tagline", "action": {"kind": "set", "prop": "opacity", "value": 1.0}},
        ],
    }


def test_valid_gfx_scene(eks_registry: Registry) -> None:
    report = validate(gfx_plan(eks_registry, gfx_scene()))
    assert report.ok, report.text()
    assert report.issues == ()


def test_gfx_trigger_to_a_missing_layer(eks_registry: Registry) -> None:
    scene = gfx_scene()
    scene["triggers"][1]["layer"] = "subtitle"
    [issue] = issues_of(validate(gfx_plan(eks_registry, scene)), "scene.ref")
    assert issue.field == "scene.triggers.1.layer"
    assert "'subtitle'" in issue.message
    assert "'headline'" in issue.message


def test_gfx_mask_to_a_missing_layer(eks_registry: Registry) -> None:
    scene = gfx_scene()
    scene["layers"][1]["children"][1]["mask"] = "ghost"
    [issue] = issues_of(validate(gfx_plan(eks_registry, scene)), "scene.ref")
    assert issue.field == "scene.layers.1.children.1.mask"


def test_scene_fps_comes_from_the_scene(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def clockless(e: TraceContext) -> None:
        e.record("gfx.render", {"scene": {**gfx_scene(), "fps": 25}})

    plan = trace(eks_registry.entry("edit", "clockless"), root=lambda ctx, _entry: ctx, registry=eks_registry)
    plan.clock.fps = None
    plan.hash = ""
    for node, _ancestors in plan.walk():
        node.hash = ""
    found = issues_of(validate(plan), "time.unresolvable")
    # beat times still need a tempo; frames are fine because the scene declares its own fps
    assert found
    assert all("fps" not in issue.message for issue in found)
    scene = render_node(plan, "gfx.render").args["scene"]
    assert isinstance(scene, dict)
    del scene["fps"]
    assert any("fps" in issue.message for issue in issues_of(validate(plan), "time.unresolvable"))
