from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

import pytest

import eks
from eks import fx, t
from eks_harness.ir.catalog.effects import (
    AutoReframe,
    BiRefNetRemove,
    EffectBase,
    MatAnyoneRemove,
    PunchIn,
    SAM3Track,
)
from eks_harness.ir.catalog.markers import STTMarkers
from eks_harness.ir.hashing import (
    _node_needs,
    cache_inputs,
    hash_plan,
    machine_dependent,
    node_needs,
    pure_closure,
)
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import (
    Registry,
    RegistryError,
    catalog,
    contributing,
    default_registry,
)

FOOTAGE = "artifact:VID1@sha256:" + "a" * 64
VOICE = "artifact:VO1@sha256:" + "b" * 64
REF = {"$ref": "n0000000001", "path": []}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def render_node(effects: list[dict[str, Any]], encoder: str | None = "libx264") -> Node:
    timeline: dict[str, Any] = {
        "fps": 30,
        "size": [1080, 1920],
        "tracks": [{"name": "main", "clips": [{"id": "main/0", "media": REF, "effects": effects}]}],
    }
    if encoder is not None:
        timeline["render"] = {"encoder": encoder}
    return Node(id="n0000000002", op="edit.render", args={"timeline": timeline}, out="media")


def register_glow(device_dependent: bool) -> None:
    @eks.effect(description="Neural glow", device_dependent=device_dependent)
    class NeuralGlow(EffectBase, frozen=True):
        kind: Literal["neural_glow"] = "neural_glow"
        strength: float = 1.0

    del NeuralGlow


# CatalogSpec.device_dependent


def test_effect_keyword_sets_device_dependent(registry: Registry) -> None:
    register_glow(True)
    assert registry.catalog("effect", "neural_glow").device_dependent


def test_bare_effect_decorator_is_not_device_dependent(registry: Registry) -> None:
    @eks.effect
    class Plain(EffectBase, frozen=True):
        kind: Literal["plain_fx"] = "plain_fx"

    assert not registry.catalog("effect", "plain_fx").device_dependent
    assert Plain().kind == "plain_fx"


def test_bare_effect_call_takes_the_keyword(registry: Registry) -> None:
    class Bare(EffectBase, frozen=True):
        kind: Literal["bare_fx"] = "bare_fx"

    assert eks.effect(Bare, device_dependent=True) is Bare
    assert registry.catalog("effect", "bare_fx").device_dependent


def test_device_dependent_is_refused_on_other_categories(registry: Registry) -> None:
    from eks_harness.ir.catalog.curves import CurveBase

    class Wobble(CurveBase, frozen=True):
        kind: Literal["wobble"] = "wobble"

    with pytest.raises(RegistryError, match="applies to effects only"):
        catalog("curve", device_dependent=True)(Wobble)
    assert all(spec.kind != "wobble" for spec in registry.catalogs("curve"))


def test_the_five_ml_effects_are_flagged(registry: Registry) -> None:
    flagged = {spec.kind for spec in registry.catalogs("effect") if spec.device_dependent}
    assert flagged == {
        MatAnyoneRemove().kind, BiRefNetRemove().kind, SAM3Track(prompt="cat").kind, AutoReframe().kind,
        PunchIn().kind,
    }


# machine_dependent(edit.render)


def test_a_software_render_with_ordinary_effects_is_not_machine_dependent(registry: Registry) -> None:
    node = render_node([{"kind": "zoom", "scale": 1.1}, {"kind": "blur", "radius": 2.0}])
    assert not machine_dependent(node, registry)


@pytest.mark.parametrize("effect", [
    {"kind": "matanyone_remove"},
    {"kind": "birefnet_remove"},
    {"kind": "sam3_track", "prompt": "person"},
    {"kind": "auto_reframe"},
    {"kind": "punch_in"},
])
def test_each_ml_effect_makes_the_render_machine_dependent(registry: Registry, effect: dict[str, Any]) -> None:
    assert machine_dependent(render_node([{"kind": "zoom", "scale": 1.1}, effect]), registry)


def test_a_device_dependent_plugin_effect_makes_the_render_machine_dependent(registry: Registry) -> None:
    with contributing("acme.fx", "1.0"):
        register_glow(True)
    assert machine_dependent(render_node([{"kind": "neural_glow"}]), registry)


def test_a_plugin_effect_without_the_flag_keeps_the_render_cacheable(registry: Registry) -> None:
    with contributing("acme.fx", "1.0"):
        register_glow(False)
    assert not machine_dependent(render_node([{"kind": "neural_glow"}]), registry)


def test_render_args_that_do_not_validate_are_machine_dependent(registry: Registry) -> None:
    invalid = render_node([{"kind": "zoom", "scale": "huge"}])
    assert machine_dependent(invalid, registry)
    broken = Node(id="n0000000002", op="edit.render",
                  args={"timeline": {"tracks": "none", "render": {"encoder": "libx264"}}}, out="media")
    assert machine_dependent(broken, registry)


def test_an_unregistered_effect_alone_does_not_make_the_render_machine_dependent(registry: Registry) -> None:
    assert not machine_dependent(render_node([{"kind": "gone_fx", "x": 1}]), registry)
    assert machine_dependent(render_node([{"kind": "gone_fx"}, {"kind": "punch_in"}]), registry)


@pytest.mark.parametrize("encoder", ["nvenc", "videotoolbox", None])
def test_a_hardware_or_unset_encoder_is_machine_dependent(registry: Registry, encoder: str | None) -> None:
    assert machine_dependent(render_node([{"kind": "zoom", "scale": 1.1}], encoder=encoder), registry)


def test_a_planned_ml_render_joins_its_own_cache_inputs(registry: Registry) -> None:
    @eks.edit(name="matte", fps=30, size=(1080, 1920), duration=t("3s"))
    def edit(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media(FOOTAGE)).effect(fx.matanyone_remove())

    plan = eks.plan(edit)
    render = one(plan, "edit.render")
    assert machine_dependent(render)
    assert cache_inputs(plan, render) == (render.id,)


# pure_closure and node_needs


def stt_plan() -> Plan:
    @eks.edit(name="captioned", fps=30, size=(1080, 1920), seed=7, duration=t("10s"))
    def edit(e: eks.Edit) -> None:
        footage = e.media(FOOTAGE)
        voice = e.media(VOICE)
        eks.media.markers(voice, STTMarkers(name="vo", model="large-v3", device="cpu", compute_type="float32"))
        e.track("main").clip(footage, start=t("0s")).effect(fx.captions("vo"))
        e.render_settings(encoder="libx264")

    return eks.plan(edit)


def walk_order(plan: Plan) -> list[str]:
    return [node.id for node, _ancestors in plan.walk()]


def test_pure_closure_follows_refs_and_clock_providers_in_program_order(registry: Registry) -> None:
    plan = stt_plan()
    render = one(plan, "edit.render")
    markers = one(plan, "media.markers")
    sources = nodes_of(plan, "media.source")
    closure = pure_closure(plan, render, registry)
    assert set(closure) == {render.id, markers.id, *(source.id for source in sources)}
    order = walk_order(plan)
    assert list(closure) == sorted(closure, key=order.index)
    assert pure_closure(plan, markers, registry)[-1] == markers.id
    assert cache_inputs(plan, render, registry) == ()


def item_plan() -> Plan:
    @eks.op("acme", name="items", out="list[str]")
    class Items(eks.Args):
        pass

    @eks.op("acme", name="shape", out="json", pure=True)
    class Shape(eks.Args):
        value: Any = None

    @eks.op("acme", name="probe", out="json")
    class Probe(eks.Args):
        pass

    @eks.flow(app="web-app", platform="web")
    def items(s: eks.Session) -> None:
        listed = s.record_op("acme.items", {})
        probe = s.record_op("acme.probe", {})
        with eks.each(listed) as item:
            shaped = s.record_op("acme.shape", {"value": item})
            s.record_op("acme.shape", {"value": {"inner": shaped, "probe": probe}})

    return eks.plan(items)


def test_pure_closure_includes_item_consumers_and_stops_at_impure_producers(registry: Registry) -> None:
    plan = item_plan()
    each = one(plan, "ctl.each")
    probe = one(plan, "acme.probe")
    first, second = nodes_of(plan, "acme.shape")
    assert pure_closure(plan, second, registry) == (first.id, second.id)
    assert pure_closure(plan, first, registry) == (first.id,)
    assert set(cache_inputs(plan, second, registry) or ()) == {each.id, probe.id}
    assert cache_inputs(plan, first, registry) == (each.id,)
    assert pure_closure(plan, probe, registry) == (probe.id,)


def test_node_needs_matches_the_private_walk(registry: Registry) -> None:
    plan = stt_plan()
    for node, _ancestors in plan.walk():
        assert node_needs(node, registry) == _node_needs(node, registry)
    render = one(plan, "edit.render")
    needs = node_needs(render, registry)
    assert needs is not None and "vo" in needs.words


def test_node_needs_is_none_for_unknown_ops_and_invalid_args(registry: Registry) -> None:
    unknown = Node(id="n0000000002", op="acme.nothing", args={"a": 1})
    invalid = Node(id="n0000000003", op="media.markers", args={"media": REF, "sources": "x"}, out="json")
    assert node_needs(unknown, registry) is None
    assert node_needs(invalid, registry) is None
    assert node_needs(Node(id="n0000000004", op="acme.nothing"), registry) is not None


# hashes never see device_dependent


def glow_plan(device_dependent: bool) -> Plan:
    registry = default_registry()
    state = registry.snapshot()
    try:
        with contributing("acme.fx", "1.0"):
            register_glow(device_dependent)

        @eks.edit(name="glowing", fps=30, size=(1080, 1920), duration=t("3s"))
        def edit(e: eks.Edit) -> None:
            e.render_settings(encoder="libx264")
            e.track("main").clip(e.media(FOOTAGE)).effect(fx.custom("neural_glow", strength=2.0))

        plan = eks.plan(edit)
        rehashed = hash_plan(Plan.model_validate(plan.model_dump(mode="json", by_alias=True)))
        assert rehashed.hash == plan.hash
        return plan
    finally:
        registry.restore(state)


def test_device_dependent_never_enters_hashes(registry: Registry) -> None:
    flagged, plain = glow_plan(True), glow_plan(False)
    assert flagged.hash == plain.hash
    assert [node.hash for node, _ancestors in flagged.walk()] == [node.hash for node, _ancestors in plain.walk()]
