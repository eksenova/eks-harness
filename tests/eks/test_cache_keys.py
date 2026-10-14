from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import fx, t
from eks_harness.engine.validator import validate
from eks_harness.ir.hashing import (
    cache_inputs,
    cacheable,
    content_dependent,
    machine_dependent,
)
from eks_harness.ir.ops.edit import Encoder
from eks_harness.ir.ops.media import Source
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.presets import StyleFonts
from eks_harness.ir.registry import Registry

PIN = "@sha256:" + "12" * 32


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = by_op(plan, op)
    return node


def layer_font(node: Node) -> Any:
    args: Any = node.args
    return args["scene"]["layers"][0]["font"]


# artifacts are versioned in place, so only a content pin makes them immutable


def still(uri: str, encoder: Encoder | None = "libx264") -> Plan:
    @eks.edit(name=f"still_{abs(hash((uri, encoder)))}", fps=30, size=(1080, 1920), duration=t("3s"))
    def edit(e: eks.Edit) -> None:
        if encoder is not None:
            e.render_settings(encoder=encoder)
        e.track("main").clip(e.media(uri), start=t("0s"))

    return eks.plan(edit)


def test_an_unpinned_artifact_joins_the_cache_key_by_content(eks_registry: Registry) -> None:
    plan = still("artifact:SHOT1")
    source = one(plan, "media.source")
    assert content_dependent(source)
    assert cache_inputs(plan, one(plan, "edit.render")) == (source.id,)


def test_a_content_pinned_artifact_is_cacheable_by_node_hash(eks_registry: Registry) -> None:
    plan = still(f"artifact:SHOT1{PIN}")
    source = one(plan, "media.source")
    assert not content_dependent(source)
    assert cacheable(plan, one(plan, "edit.render"))
    other = still("artifact:SHOT1@sha256:" + "34" * 32)
    assert one(other, "edit.render").hash != one(plan, "edit.render").hash


def test_a_malformed_content_pin_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Source.model_validate({"uri": "artifact:SHOT1@sha256:abc"})


# render choices made per machine join the cache key


def test_an_edit_without_an_encoder_is_machine_dependent(eks_registry: Registry) -> None:
    plan = still(f"artifact:SHOT1{PIN}", encoder=None)
    render = one(plan, "edit.render")
    assert machine_dependent(render)
    assert cache_inputs(plan, render) == (render.id,)
    explicit = still(f"artifact:SHOT1{PIN}")
    assert not machine_dependent(one(explicit, "edit.render"))


def gfx_plan(cpu: bool) -> Plan:
    @eks.scene(name=f"card_{cpu}", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=cpu)
    def card(sc: eks.GfxScene) -> None:
        sc.rect(width=200.0, height=200.0, fill="#f5c542")

    return eks.plan(card)


def test_a_gpu_gfx_render_is_machine_dependent(eks_registry: Registry) -> None:
    gpu, cpu = gfx_plan(False), gfx_plan(True)
    render = one(gpu, "gfx.render")
    assert machine_dependent(render)
    assert cache_inputs(gpu, render) == (render.id,)
    assert not machine_dependent(one(cpu, "gfx.render"))
    assert cacheable(cpu, one(cpu, "gfx.render"))


def test_a_gpu_blender_render_is_machine_dependent(eks_registry: Registry) -> None:
    def build(device: Any, platform: str | None = None) -> Plan:
        @eks.scene(name=f"cube_{device}_{platform or 'any'}".replace("-", "_"), kind="blender", engine="cycles",
                   samples=8, fps=30, duration=t("2s"), device=device, platform=platform)
        def cube(sc: eks.BlenderScene) -> None:
            sc.camera(lens=50)

        return eks.plan(cube)

    gpu, cpu, pinned = build("GPU"), build("CPU"), build("CPU", "linux-x86_64")
    assert machine_dependent(one(gpu, "blender.render"))
    # a CPU Cycles render differs per worker platform, so only a platform pin makes the node hash enough
    assert machine_dependent(one(cpu, "blender.render"))
    assert not machine_dependent(one(pinned, "blender.render"))


# fonts are media or families the renderer ships


def test_font_files_are_media_sources_of_text_renders(eks_registry: Registry) -> None:
    @eks.scene(name="branded_card", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=True)
    def card(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", id="title", font="fonts/Brand.ttf")

    plan = eks.plan(card)
    font = one(plan, "media.source")
    assert font.args == {"uri": "file:fonts/Brand.ttf", "kind": "file"}
    render = one(plan, "gfx.render")
    assert layer_font(render) == {"$ref": font.id, "path": []}
    assert font.id in render.after
    assert cache_inputs(plan, render) == (font.id,)
    assert validate(plan).issues == ()


def test_text_effects_take_font_files(eks_registry: Registry) -> None:
    @eks.edit(name="burned", fps=30, size=(1080, 1920), duration=t("4s"))
    def burned(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media(f"artifact:VID1{PIN}"), start=t("0s"), src=t.span(t("0s"), t("4s"))).effect(
            fx.text("Kontör", font="fonts/Brand.ttf")).effect(fx.lower_third("Kontör", font="IBM Plex Mono"))

    plan = eks.plan(burned)
    fonts = [node for node in by_op(plan, "media.source") if node.args.get("kind") == "file"]
    assert [node.args["uri"] for node in fonts] == ["file:fonts/Brand.ttf"]
    assert cache_inputs(plan, one(plan, "edit.render")) == (fonts[0].id,)


def test_a_family_the_renderer_does_not_ship_is_rejected(eks_registry: Registry) -> None:
    @eks.scene(name="unknown_family", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30)
    def card(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", font="Brand Sans")

    with pytest.raises(ValueError, match="neither a font family the renderer ships"):
        eks.plan(card)


def test_shipped_families_stay_plain_names(eks_registry: Registry) -> None:
    plan = gfx_plan(True)
    assert by_op(plan, "media.source") == []

    @eks.scene(name="plex_card", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=True)
    def card(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", font="IBM Plex Mono")

    plex = eks.plan(card)
    assert layer_font(one(plex, "gfx.render")) == "IBM Plex Mono"
    assert cacheable(plex, one(plex, "gfx.render"))


def test_overlay_styles_name_shipped_fonts_only() -> None:
    StyleFonts(family="IBM Plex Sans", regular="IBMPlexSans-Regular.ttf", semibold="IBMPlexSans-SemiBold.ttf")
    with pytest.raises(ValidationError):
        StyleFonts.model_validate({"family": "Brand Sans", "regular": "Brand-Regular.ttf",
                                   "semibold": "Brand-SemiBold.ttf"})
