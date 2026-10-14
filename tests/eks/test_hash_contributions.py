from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel, Field

import eks
from eks import by, t
from eks_harness.engine import validator
from eks_harness.ir import contributions as contributions_module
from eks_harness.ir import hashing
from eks_harness.ir.catalog.curves import CurveBase
from eks_harness.ir.catalog.easing import EasingBase
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.contributions import (
    Contribution,
    SessionScope,
    contributions,
    node_contributions,
)
from eks_harness.ir.hashing import (
    cacheable,
    hash_plan,
    kinds_payload,
    node_hash,
    plan_hash,
    resource_hash,
    stored_clock_payload,
)
from eks_harness.ir.ops.overlay import Caption, OverlayBase
from eks_harness.ir.plan import Binding, Node, Plan, SessionSpec
from eks_harness.ir.presets import NetProfile, OverlayStyle, Pace, PersonaPreset
from eks_harness.ir.registry import Registry, contributing

CLIP = "artifact:CLIP1@sha256:abababababababababababababababababababababababababababababababab"


@contextmanager
def isolated(registry: Registry) -> Iterator[None]:
    state = registry.snapshot()
    try:
        yield
    finally:
        registry.restore(state)


def only(plan: Plan, op: str) -> Node:
    found = [node for node, _ancestors in plan.walk() if node.op == op]
    assert len(found) == 1, [node.op for node, _ancestors in plan.walk()]
    return found[0]


def issue_codes(plan: Plan) -> list[str]:
    return [issue.code for issue in eks.validate(plan).issues]


def sparkle_plan(registry: Registry, version: str, plugin_version: str) -> Plan:
    with isolated(registry):
        with contributing("sparkle-fx", plugin_version):
            @eks.effect(description="Sparkles over the clip", version=version)
            class Sparkle(EffectBase, frozen=True):
                kind: Literal["sparkle"] = "sparkle"
                density: float = 1.0

        @eks.edit(fps=30, size=(1080, 1920))
        def sparkly(e: eks.Edit) -> None:
            e.render_settings(encoder="libx264")
            e.track("main").clip(e.media(CLIP)).effect(eks.fx.custom("sparkle", density=3.0))

        plan = eks.plan(sparkly)
        assert issue_codes(plan).count("plan.hash") == 0
        del Sparkle
        return plan


def test_bumping_a_plugin_effect_changes_the_render_hash(eks_registry: Registry) -> None:
    first = sparkle_plan(eks_registry, "1", "1.0")
    again = sparkle_plan(eks_registry, "1", "1.0")
    bumped = sparkle_plan(eks_registry, "2", "2.0")
    kind_only = sparkle_plan(eks_registry, "2", "1.0")
    plugin_only = sparkle_plan(eks_registry, "1", "2.0")
    render = only(first, "edit.render")
    assert cacheable(first, render)
    assert only(again, "edit.render").hash == render.hash
    assert only(bumped, "edit.render").hash != render.hash
    assert only(kind_only, "edit.render").hash != render.hash
    assert only(plugin_only, "edit.render").hash != render.hash
    assert len({only(plan, "edit.render").hash for plan in (first, bumped, kind_only, plugin_only)}) == 4
    assert [(item.plugin, item.version) for item in bumped.requires] == [("sparkle-fx", "2.0")]


def media_check_plan(registry: Registry, version: str, plugin_version: str) -> Plan:
    with isolated(registry):
        with contributing("sharpness-check", plugin_version):
            @eks.check("sharpness", family="media", description="Frames are in focus", version=version)
            class Sharpness(eks.Args):
                min_score: float = 0.5

        @eks.edit(fps=30, size=(1080, 1920))
        def sharp(e: eks.Edit) -> None:
            e.track("main").clip(CLIP)
            e.expect.use("sharpness", at=t("1s"), min_score=0.8)

        plan = eks.plan(sharp)
        assert issue_codes(plan).count("plan.hash") == 0
        del Sharpness
        return plan


def test_bumping_a_plugin_media_check_changes_the_check_hash(eks_registry: Registry) -> None:
    first = media_check_plan(eks_registry, "1", "1.0")
    bumped = media_check_plan(eks_registry, "2", "2.0")
    assert only(first, "check.media").args == only(bumped, "check.media").args
    assert only(first, "check.media").hash != only(bumped, "check.media").hash
    assert only(first, "edit.render").hash == only(bumped, "edit.render").hash
    assert media_check_plan(eks_registry, "1", "1.0").hash == first.hash


def overlay_plan(registry: Registry, version: str, plugin_version: str) -> Plan:
    with isolated(registry):
        with contributing("acme.marks", plugin_version):
            @eks.overlay_kind(live="acme.mark", screenshot="acme.mark", video="acme.mark", version=version,
                              description="Brand mark")
            class Mark(OverlayBase, frozen=True):
                kind: Literal["mark"] = "mark"
                label: str = "x"

        @eks.edit(fps=30, size=(1080, 1920))
        def marked(e: eks.Edit) -> None:
            e.track("main").clip(CLIP)
            e.track("overlays").overlay(Mark(label="Eksen", window=t.span(t("1s"), t("2s"))))

        plan = eks.plan(marked)
        assert issue_codes(plan).count("plan.hash") == 0
        return plan


def test_bumping_a_plugin_overlay_in_an_edit_track_changes_the_render_hash(eks_registry: Registry) -> None:
    first = overlay_plan(eks_registry, "1", "1.0")
    bumped = overlay_plan(eks_registry, "2", "2.0")
    assert only(first, "edit.render").args == only(bumped, "edit.render").args
    assert only(first, "edit.render").hash != only(bumped, "edit.render").hash
    assert [(item.plugin, item.version) for item in first.requires] == [("acme.marks", "1.0")]


def builtin_style(registry: Registry) -> OverlayStyle:
    spec = next(spec for spec in registry.presets("style") if spec.provenance.plugin == "eks")
    assert isinstance(spec.value, OverlayStyle)
    return spec.value


def style_plans(registry: Registry, stroke: int, plugin_version: str) -> tuple[Plan, Plan]:
    base = builtin_style(registry)
    with isolated(registry):
        with contributing("acme.brand", plugin_version):
            @eks.preset("brand", description="Brand overlay style")
            def brand() -> OverlayStyle:
                return base.model_copy(update={"stroke": stroke})

        @eks.edit(fps=30, size=(1080, 1920))
        def branded(e: eks.Edit) -> None:
            e.render_settings(encoder="libx264")
            e.track("main").clip(CLIP)
            e.track("overlays").overlay(Caption(text="Merhaba", window=t.span(t("1s"), t("2s")), style="brand"))

        @eks.flow(app="web-app")
        def annotated(s: eks.Session) -> None:
            eks.annotate(s.shot("liste"), [eks.overlay.caption("Merhaba")], style="brand")

        edit = eks.plan(branded)
        flow = eks.plan(annotated, platform="web")
        for plan in (edit, flow):
            assert issue_codes(plan).count("plan.hash") == 0
        del brand
        return edit, flow


def test_changing_a_plugin_style_preset_changes_render_and_annotate_hashes(eks_registry: Registry) -> None:
    edit, flow = style_plans(eks_registry, 2, "1.0")
    same_edit, same_flow = style_plans(eks_registry, 2, "1.0")
    restroked_edit, restroked_flow = style_plans(eks_registry, 9, "1.0")
    bumped_edit, bumped_flow = style_plans(eks_registry, 9, "2.0")
    render = only(edit, "edit.render")
    annotate = only(flow, "overlay.annotate")
    assert only(same_edit, "edit.render").hash == render.hash
    assert only(same_flow, "overlay.annotate").hash == annotate.hash
    assert only(restroked_edit, "edit.render").args == render.args
    assert only(restroked_edit, "edit.render").hash != render.hash
    assert only(restroked_flow, "overlay.annotate").args == annotate.args
    assert only(restroked_flow, "overlay.annotate").hash != annotate.hash
    assert only(bumped_edit, "edit.render").hash != only(restroked_edit, "edit.render").hash
    assert only(bumped_flow, "overlay.annotate").hash != only(restroked_flow, "overlay.annotate").hash
    assert cacheable(edit, render)
    for plan in (edit, flow):
        assert [(item.plugin, item.version) for item in plan.requires] == [("acme.brand", "1.0")]
    assert [(item.plugin, item.version) for item in bumped_flow.requires] == [("acme.brand", "2.0")]


def test_untouched_plans_validate_without_hash_issues(eks_registry: Registry) -> None:
    with contributing("sparkle-fx", "1.0"):
        @eks.effect(description="Sparkles over the clip")
        class Sparkle(EffectBase, frozen=True):
            kind: Literal["sparkle"] = "sparkle"
            density: float = 1.0

    @eks.edit(fps=30, size=(1080, 1920))
    def sparkly(e: eks.Edit) -> None:
        e.track("main").clip(CLIP).effect(eks.fx.custom("sparkle"))
        e.track("overlays").overlay(Caption(text="Merhaba", window=t.span(t("1s"), t("2s")), style="kb"))

    @eks.flow(app="web-app")
    def looked(s: eks.Session) -> None:
        s.ui.tap("#kaydet")
        s.checkpoint("liste", viewport="phone")
        eks.annotate(s.shot("liste"), [eks.overlay.highlight("#total")], style="kb")

    for plan in (eks.plan(sparkly), eks.plan(looked, platform="web")):
        assert "plan.hash" not in issue_codes(plan)
        stored = {node.id: node.hash for node, _ancestors in plan.walk()}
        resources = {resource.id: resource_hash(resource) for resource in plan.resources}
        for node, _ancestors in plan.walk():
            local = node_hash(node, refs=stored, resources=resources, executor_version=eks_registry.op(node.op).executor_version,
                              clock=stored_clock_payload(plan, node, eks_registry))
            assert local == node.hash, node.op
        assert plan_hash(plan) == plan.hash
        reloaded = hash_plan(Plan.from_json(plan.to_json()))
        assert reloaded.hash == plan.hash
    del Sparkle


def test_kinds_payload_reports_kinds_and_presets_and_stays_out_of_kindless_nodes(eks_registry: Registry) -> None:
    @eks.flow(app="web-app")
    def flow(s: eks.Session) -> None:
        s.ui.tap("#kaydet")
        s.op("ui.nav", route="/liste")

    plan = eks.plan(flow, platform="web")
    tap, nav = plan.nodes
    payload = kinds_payload(tap, eks_registry)
    assert payload == {"kinds": [["target", "test_id", "1", "eks", ""]], "presets": []}
    assert kinds_payload(nav, eks_registry) is None
    broken = tap.model_copy(update={"args": {"target": 3}})
    assert kinds_payload(broken, eks_registry) == {"kinds": None}
    assert kinds_payload(Node(id="n0000000001", op="acme.unknown"), eks_registry) == {"kinds": None}
    assert node_contributions(broken, eks_registry) is None


def test_viewports_of_the_app_profile_are_not_presets(eks_registry: Registry) -> None:
    @eks.flow(app="web-app")
    def flow(s: eks.Session) -> None:
        s.checkpoint("liste", viewport="phone")

    plan = eks.plan(flow, platform="web")
    checkpoint = only(plan, "check.checkpoint")
    found = node_contributions(checkpoint, eks_registry)
    assert found is not None
    assert [(item.category, item.kind) for item in found if item.preset] == [("viewport", "phone")]
    own = node_contributions(checkpoint, eks_registry, scope=SessionScope(viewports=frozenset({"phone"})))
    assert own is not None
    assert [item for item in own if item.preset] == []


def test_contributions_walk_nested_targets_and_check_params(eks_registry: Registry) -> None:
    @eks.target_kind("acme_chip", surfaces=["web"], shorthand="chip")
    class AcmeChip(BaseModel, frozen=True, extra="forbid"):
        label: str

    @eks.flow(app="web-app")
    def flow(s: eks.Session) -> None:
        s.ui.tap(eks.by.text("Kaydet").within("chip=Form"))
        s.checkpoint("liste", checks=[eks.checks.use("layout_spill", ignore=["chip=Banner"])])

    plan = eks.plan(flow, platform="web")
    tap = only(plan, "ui.tap")
    checkpoint = only(plan, "check.checkpoint")
    tap_found = node_contributions(tap, eks_registry) or []
    check_found = node_contributions(checkpoint, eks_registry) or []
    assert {(item.category, item.kind) for item in tap_found} == {("target", "text"), ("target", "acme_chip")}
    assert ("target", "acme_chip") in {(item.category, item.kind) for item in check_found}
    assert ("check", "layout_spill") in {(item.category, item.kind) for item in check_found}
    del AcmeChip


def test_contributions_are_sorted_and_unique(eks_registry: Registry) -> None:
    model = eks_registry.op("ui.tap").args.model_validate({"target": {"by": "test_id", "args": {"id": "a"}}})
    found = contributions((model, [model], {"x": model}), eks_registry)
    assert found == [Contribution("target", "test_id", "1", "eks", eks_registry.catalog("target", "test_id")
                                  .provenance.version)]
    assert found == sorted(found, key=Contribution.sort_key)


@pytest.mark.parametrize("name", ["PRESET_FIELDS", "SESSION_PRESETS"])
def test_preset_tables_match_the_validator(name: str) -> None:
    ours: object = getattr(contributions_module, name)
    theirs: object = getattr(validator, name)
    assert ours == theirs


def test_session_presets_join_the_plan_hash(eks_registry: Registry) -> None:
    def plan_for(width: int) -> Plan:
        with isolated(eks_registry):
            with contributing("acme.screens", "1.0"):
                @eks.preset("acme-wide", description="Wide screen")
                def wide() -> eks.Viewport:
                    return eks.Viewport(width=width, height=900)

            @eks.flow(app="web-app", viewport="acme-wide")
            def flow(s: eks.Session) -> None:
                s.ui.tap("#kaydet")

            plan = eks.plan(flow, platform="web")
            del wide
            return plan

    narrow, wide = plan_for(1200), plan_for(2400)
    assert narrow.nodes[0].hash == wide.nodes[0].hash
    assert narrow.hash != wide.hash
    assert plan_for(1200).hash == narrow.hash


def slugify_plan(registry: Registry, plugin_version: str, version: str = "1") -> tuple[Plan, Node, bool, list[str]]:
    with isolated(registry):
        with contributing("acme-text", plugin_version):
            @eks.op("acme", name="slugify", out="json", pure=True, executor="acme.exec:Slugify", version=version,
                    description="Slugify a text", examples=[{"text": "a"}])
            class Slugify(eks.Args):
                text: str = Field(description="Text")

        @eks.flow(app="web-app")
        def slugged(s: eks.Session) -> None:
            s.op("acme.slugify", text="Merhaba Dünya")

        plan = eks.plan(slugged, platform="web")
        node = only(plan, "acme.slugify")
        result = plan, node, cacheable(plan, node), issue_codes(plan)
        del Slugify
        return result


def test_bumping_a_plugin_pure_op_changes_its_node_hash(eks_registry: Registry) -> None:
    first, node, first_cacheable, first_codes = slugify_plan(eks_registry, "1.0")
    again = slugify_plan(eks_registry, "1.0")[1]
    bumped_plan, bumped, bumped_cacheable, _codes = slugify_plan(eks_registry, "2.0")
    assert first_cacheable and bumped_cacheable
    assert "plan.hash" not in first_codes
    assert node.v == bumped.v and node.args == bumped.args
    assert again.hash == node.hash
    assert bumped.hash != node.hash
    assert first.hash != bumped_plan.hash
    assert [(item.plugin, item.version) for item in first.requires] == [("acme-text", "1.0")]


def test_a_plugin_pure_op_planned_under_an_older_plugin_fails_the_hash_check(eks_registry: Registry) -> None:
    first, node, _cacheable, _codes = slugify_plan(eks_registry, "1.0")
    with isolated(eks_registry):
        with contributing("acme-text", "2.0"):
            @eks.op("acme", name="slugify", out="json", pure=True, executor="acme.exec:Slugify",
                    description="Slugify a text", examples=[{"text": "a"}])
            class Slugify(eks.Args):
                text: str = Field(description="Text")

        codes = issue_codes(first)
        payload = kinds_payload(node, eks_registry)
        del Slugify
    assert "plugin.version" in codes
    assert "plan.hash" in codes
    assert payload == {"kinds": [["op", "acme.slugify", "1", "acme-text", "2.0"]], "presets": []}


def test_builtin_and_project_ops_do_not_report_their_own_provenance(eks_registry: Registry) -> None:
    @eks.op("acme", name="local", out="json", pure=True, description="A project op", examples=[{"text": "a"}])
    class Local(eks.Args):
        text: str = Field(description="Text")

    @eks.flow(app="web-app")
    def local(s: eks.Session) -> None:
        s.op("acme.local", text="a")
        s.op("ui.nav", route="/liste")

    plan = eks.plan(local, platform="web")
    assert eks_registry.op("acme.local").provenance.plugin == "project"
    assert [kinds_payload(node, eks_registry) for node in plan.nodes] == [None, None]
    del Local


def persona_plans(registry: Registry, plugin_version: str, username: str) -> tuple[Plan, Plan]:
    with isolated(registry):
        with contributing("acme.people", plugin_version):
            @eks.preset("acme-admin", description="Admin persona")
            def admin() -> PersonaPreset:
                return PersonaPreset(label="Admin", username=username, tenant="harman", roles=("admin",))

        @eks.flow(app="web-app", as_="acme-admin")
        def logged(s: eks.Session) -> None:
            s.ui.tap("#kaydet")
            s.api.get("/api/tenants", as_="acme-admin")

        @eks.flow(app="web-app")
        def switched(s: eks.Session) -> None:
            s.as_("acme-admin")
            s.ui.tap("#kaydet")

        plans = eks.plan(logged, platform="web"), eks.plan(switched, platform="web")
        for plan in plans:
            assert "plan.hash" not in issue_codes(plan)
        del admin
        return plans


def test_changing_a_plugin_persona_preset_changes_node_and_plan_hashes(eks_registry: Registry) -> None:
    logged, switched = persona_plans(eks_registry, "1.0", "a@acme.test")
    same_logged, same_switched = persona_plans(eks_registry, "1.0", "a@acme.test")
    renamed_logged, renamed_switched = persona_plans(eks_registry, "1.0", "b@acme.test")
    bumped_logged, bumped_switched = persona_plans(eks_registry, "2.0", "a@acme.test")
    assert same_logged.hash == logged.hash and same_switched.hash == switched.hash
    for changed in (renamed_logged, bumped_logged):
        assert only(changed, "api.get").args == only(logged, "api.get").args
        assert only(changed, "api.get").hash != only(logged, "api.get").hash
        assert changed.hash != logged.hash
    for changed in (renamed_switched, bumped_switched):
        assert only(changed, "session.as").hash != only(switched, "session.as").hash
        assert changed.hash != switched.hash
    assert [(item.plugin, item.version) for item in logged.requires] == [("acme.people", "1.0")]
    assert [(item.plugin, item.version) for item in bumped_switched.requires] == [("acme.people", "2.0")]


def test_seeded_sessions_take_personas_from_the_seed_not_from_presets(eks_registry: Registry) -> None:
    with contributing("acme.people", "1.0"):
        @eks.preset("acme-admin", description="Admin persona")
        def admin() -> PersonaPreset:
            return PersonaPreset(label="Admin", username="a@acme.test")

    node = Node(id="n0000000001", op="session.as", args={"persona": "acme-admin"})
    unseeded = node_contributions(node, eks_registry) or []
    seeded = node_contributions(node, eks_registry, scope=SessionScope(seeded=True))
    assert [(item.category, item.kind) for item in unseeded] == [("persona", "acme-admin")]
    assert seeded == []
    free = SessionSpec(key="main", app="web-app", platform="web", persona="acme-admin")
    with_seed = free.model_copy(update={"seed": "standard"})
    assert [(item.category, item.kind) for item in contributions(free, eks_registry)] == [("persona", "acme-admin")]
    assert contributions(with_seed, eks_registry) == []
    del admin


def dynamic_plan(registry: Registry, plugin_version: str, case: str) -> Plan:
    with isolated(registry):
        with contributing("acme.kit", plugin_version):
            @eks.target_kind("aria", surfaces=["web"], shorthand="aria", description="Element by its aria-label")
            class Aria(BaseModel, frozen=True, extra="forbid"):
                label: str

        @eks.flow(app="web-app")
        def closure(s: eks.Session) -> None:
            save = by.custom("aria", label="Kaydet")

            @eks.dynamic
            def run() -> None:
                s.ui.tap(save)

        @eks.flow(app="web-app")
        def default(s: eks.Session) -> None:
            chosen = by.custom("aria", label="Kaydet")

            @eks.dynamic
            def run(target: eks.Target = chosen) -> None:
                s.ui.tap(target)

        plan = eks.plan(closure if case == "closure" else default, platform="web")
        assert "plan.hash" not in issue_codes(plan)
        del Aria
        return plan


@pytest.mark.parametrize("case", ["closure", "default"])
def test_bumping_a_target_kind_bound_into_a_dynamic_function_changes_its_hash(eks_registry: Registry,
                                                                              case: str) -> None:
    first = dynamic_plan(eks_registry, "1.0", case)
    again = dynamic_plan(eks_registry, "1.0", case)
    bumped = dynamic_plan(eks_registry, "2.0", case)
    node = only(first, "ctl.dynamic")
    assert only(again, "ctl.dynamic").hash == node.hash
    assert only(bumped, "ctl.dynamic").args == node.args
    assert only(bumped, "ctl.dynamic").hash != node.hash
    assert bumped.hash != first.hash


def test_hash_helpers_are_exported() -> None:
    assert {"kinds_payload", "session_presets"} <= set(hashing.__all__)


def register_motion(registry: Registry, version: str, plugin_version: str) -> None:
    with contributing("acme-curve", plugin_version):
        @eks.curve(description="Wobble", version=version)
        class Wobble(CurveBase, frozen=True):
            kind: Literal["wobble"] = "wobble"
            amp: float = 1.0

        @eks.easing(description="Snap", version=version)
        class Snap(EasingBase, frozen=True):
            kind: Literal["snap"] = "snap"

    del Wobble, Snap


def gfx_plan(mode: str) -> Plan:
    @eks.scene(kind="gfx", size=(1080, 1920), duration=t("2s"), cpu=True)
    def card(sc: eks.GfxScene) -> None:
        box = sc.rect(1080.0, 1920.0, fill="#0b1628")
        if mode == "assign":
            sc.on(t("1s"), box.assign("opacity", eks.curves.custom("wobble", amp=0.2)))
        elif mode == "set":
            box.set(opacity=eks.curves.custom("wobble", amp=0.2))
        else:
            sc.on(t("1s"), box.tween("opacity", {t("0s"): 0.0, t("1s"): 1.0}, ease="snap"))

    @eks.edit(fps=30, size=(1080, 1920))
    def promo(e: eks.Edit) -> None:
        e.track("main").clip(e.gfx(card), start=t("0s"))

    return eks.plan(promo)


def motion_plan(registry: Registry, mode: str, version: str, plugin_version: str) -> Plan:
    with isolated(registry):
        register_motion(registry, version, plugin_version)
        plan = gfx_plan(mode)
        assert "plan.hash" not in issue_codes(plan)
        return plan


@pytest.mark.parametrize(("mode", "kind"), [("assign", "wobble"), ("set", "wobble"), ("tween", "snap")])
def test_bumping_a_plugin_curve_or_easing_in_a_gfx_scene_changes_the_render_hashes(
        eks_registry: Registry, mode: str, kind: str) -> None:
    first = motion_plan(eks_registry, mode, "1", "1.0")
    again = motion_plan(eks_registry, mode, "1", "1.0")
    bumped = motion_plan(eks_registry, mode, "2", "2.0")
    gfx = only(first, "gfx.render")
    assert cacheable(first, gfx)
    assert only(again, "gfx.render").hash == gfx.hash
    assert only(bumped, "gfx.render").args == gfx.args
    assert only(bumped, "gfx.render").hash != gfx.hash
    assert only(bumped, "edit.render").hash != only(first, "edit.render").hash
    with isolated(eks_registry):
        register_motion(eks_registry, "1", "1.0")
        payload = kinds_payload(gfx, eks_registry)
    assert isinstance(payload, dict)
    assert [entry[1] for entry in cast("list[list[str]]", payload["kinds"])] == [kind]
    assert [(item.plugin, item.version) for item in bumped.requires] == [("acme-curve", "2.0")]


def test_a_gfx_assign_of_a_plugin_curve_is_stored_as_plain_json(eks_registry: Registry) -> None:
    plan = motion_plan(eks_registry, "assign", "1", "1.0")
    scene = cast("dict[str, Any]", only(plan, "gfx.render").args["scene"])
    action = scene["triggers"][0]["action"]
    assert action["kind"] == "set"
    assert action["value"]["curve"] == {"kind": "wobble", "amp": 0.2}


def test_a_gfx_plan_made_under_an_older_curve_plugin_fails_the_hash_check(eks_registry: Registry) -> None:
    stored = motion_plan(eks_registry, "assign", "1", "1.0").to_json()
    with isolated(eks_registry):
        register_motion(eks_registry, "2", "2.0")
        reloaded = Plan.from_json(stored)
        gfx = only(reloaded, "gfx.render")
        old_hash = gfx.hash
        flagged = [issue.node for issue in eks.validate(reloaded).issues if issue.code == "plan.hash"]
        assert gfx.id in flagged
        assert only(hash_plan(Plan.from_json(stored)), "gfx.render").hash != old_hash


def dynamic_preset_plan(registry: Registry, stroke: int, plugin_version: str, case: str) -> Plan:
    base = builtin_style(registry)
    demo = registry.preset("pace", "demo").value
    assert isinstance(demo, Pace)
    with isolated(registry):
        with contributing("acme.kit", plugin_version):
            @eks.preset("brand", description="Brand overlay style")
            def brand() -> OverlayStyle:
                return base.model_copy(update={"stroke": stroke})

            @eks.preset("acme-slow", description="Slow pace")
            def slow() -> Pace:
                return demo.model_copy(update={"hold": t(f"{stroke}s")})

            @eks.preset("acme-edge", description="EDGE network")
            def edge() -> NetProfile:
                return NetProfile(latency=t(f"{stroke}00ms"), down_kbps=240)

        @eks.flow(app="web-app")
        def overlay_default(s: eks.Session) -> None:
            cap = eks.overlay.caption("Merhaba", style="brand")

            @eks.dynamic
            def go(item: Any = cap) -> None:
                eks.annotate(s.shot("liste"), [item])

        @eks.flow(app="web-app")
        def name_default(s: eks.Session) -> None:
            @eks.dynamic
            def go(profile: str = "acme-edge") -> None:
                s.net.shape(profile)

        @eks.flow(app="web-app")
        def closure_name(s: eks.Session) -> None:
            pace = "acme-slow"

            @eks.dynamic
            def go() -> None:
                with s.record("demo"):
                    s.ui.type("#ara", "Kadikoy", pace=pace)

        flows = {"overlay": overlay_default, "name": name_default, "closure": closure_name}
        plan = eks.plan(flows[case], platform="web")
        assert "plan.hash" not in issue_codes(plan)
        del brand, slow, edge
        return plan


@pytest.mark.parametrize("case", ["overlay", "name", "closure"])
def test_presets_passed_into_a_dynamic_function_join_its_hash(eks_registry: Registry, case: str) -> None:
    first = dynamic_preset_plan(eks_registry, 2, "1.0", case)
    again = dynamic_preset_plan(eks_registry, 2, "1.0", case)
    changed = dynamic_preset_plan(eks_registry, 9, "1.0", case)
    bumped = dynamic_preset_plan(eks_registry, 9, "2.0", case)
    node = only(first, "ctl.dynamic")
    assert only(again, "ctl.dynamic").hash == node.hash
    assert only(changed, "ctl.dynamic").args == node.args
    assert only(changed, "ctl.dynamic").hash != node.hash
    assert only(bumped, "ctl.dynamic").hash != only(changed, "ctl.dynamic").hash
    assert len({first.hash, changed.hash, bumped.hash}) == 3
    assert [(item.plugin, item.version) for item in first.requires] == [("acme.kit", "1.0")]


def test_an_overlay_passed_into_a_dynamic_function_is_decoded(eks_registry: Registry) -> None:
    plan = dynamic_preset_plan(eks_registry, 2, "1.0", "overlay")
    inputs = cast("dict[str, Any]", only(plan, "ctl.dynamic").args["inputs"])
    assert inputs["item"]["kind"] == "caption"
    assert inputs["item"]["style"] == "brand"
    with isolated(eks_registry):
        with contributing("acme.kit", "1.0"):
            @eks.preset("brand", description="Brand overlay style")
            def brand() -> OverlayStyle:
                return builtin_style(eks_registry)

        found = node_contributions(only(plan, "ctl.dynamic"), eks_registry) or []
        del brand
    assert {(item.category, item.kind) for item in found} >= {("overlay", "caption"), ("style", "brand")}


def test_plain_json_is_decoded_only_when_a_registered_model_accepts_it(eks_registry: Registry) -> None:
    curve = {"kind": "sine", "freq_hz": 2.0}
    check = {"kind": "layout_spill", "params": {}}
    target = {"by": "test_id", "args": {"id": "kaydet"}}
    found = contributions({"a": [curve], "b": {"nested": check}, "c": target}, eks_registry)
    assert {(item.category, item.kind) for item in found} == {
        ("curve", "sine"), ("check", "layout_spill"), ("target", "test_id")}
    rejected = contributions([{"kind": "sine", "unknown": 1}, {"kind": "nothing"}, {"by": "test_id", "id": "x"},
                              {"kind": 3}], eks_registry)
    assert rejected == []


def test_a_kind_name_shared_by_two_categories_counts_in_each_it_validates_in(eks_registry: Registry) -> None:
    found = contributions({"kind": "zoom"}, eks_registry)
    categories = {item.category for item in found if item.kind == "zoom"}
    for category in ("effect", "transition"):
        try:
            eks_registry.catalog(category, "zoom").model.model_validate({"kind": "zoom"})
        except ValueError:
            assert category not in categories
        else:
            assert category in categories


def test_preset_names_count_only_in_dynamic_inputs_and_literal_bindings(eks_registry: Registry) -> None:
    assert contributions({"profile": "3g"}, eks_registry) == []
    literal = Binding(kind="literal", value={"pace": "demo"})
    session = Binding(kind="session", value="demo")
    assert [(item.category, item.kind) for item in contributions(literal, eks_registry)] == [("pace", "demo")]
    assert contributions(session, eks_registry) == []
    phone = Binding(kind="literal", value="phone")
    assert [(item.category, item.kind) for item in contributions(phone, eks_registry)] == [("viewport", "phone")]
    own = contributions(phone, eks_registry, scope=SessionScope(viewports=frozenset({"phone"})))
    assert own == []
