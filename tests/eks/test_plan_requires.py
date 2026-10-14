from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

import eks
from eks import by, t
from eks.planning import REQUIRES_FINALIZE_ORDER, trace
from eks.scenes import SCENE_FINALIZE_ORDER
from eks_harness.ir.catalog.curves import CurveBase
from eks_harness.ir.plan import Plan, Requirement
from eks_harness.ir.presets import (
    NetProfile,
    OverlayStyle,
    Pace,
    PersonaPreset,
    Viewport,
)
from eks_harness.ir.registry import Registry, contributing

PLUGIN = "acme.kit"
KIT = [Requirement(plugin=PLUGIN, version="1.0")]
TARGET_LOOKALIKE = {"by": "aria", "label": "Kaydet"}

PROFILE = """
[app]
name = "web-app"
platform = "web"

[web]
url = "http://localhost:3000"
locale = "tr-TR"
timezone = "Europe/Istanbul"

[defaults]
checks = ["acme_contrast"]
"""


def register_kit(registry: Registry, version: str = "1.0") -> None:
    demo_pace = registry.preset("pace", "demo").value
    assert isinstance(demo_pace, Pace)
    with contributing(PLUGIN, version):
        @eks.target_kind("aria", surfaces=["web"], shorthand="aria", description="Element by its aria-label")
        class Aria(BaseModel, frozen=True, extra="forbid"):
            label: str

        @eks.check("acme_contrast", platforms=["web"], description="Text contrast ratio")
        class AcmeContrast(eks.Args):
            ratio: float = 4.5

        @eks.check("acme_landmark", platforms=["web"], description="A landmark exists")
        class AcmeLandmark(eks.Args):
            landmark: eks.Target

        @eks.preset("acme-wide", description="Wide screen")
        def wide() -> Viewport:
            return Viewport(width=2560, height=1440)

        @eks.preset("acme-slow", description="Slow pace")
        def slow() -> Pace:
            return demo_pace.model_copy(update={"hold": t("2s")})

        @eks.preset("acme-edge", description="EDGE network")
        def edge() -> NetProfile:
            return NetProfile(latency=t("400ms"), down_kbps=240)

        @eks.preset("acme-admin", description="Admin persona")
        def admin() -> PersonaPreset:
            return PersonaPreset(label="Admin", username="admin@acme.test", tenant="harman", roles=("admin",))

    del Aria, AcmeContrast, AcmeLandmark, wide, slow, edge, admin


def plan_of(fn: Callable[..., Any]) -> Plan:
    return eks.plan(fn, platform="web")


def test_the_requirement_finalizer_runs_after_scene_finalizers() -> None:
    assert REQUIRES_FINALIZE_ORDER > SCENE_FINALIZE_ORDER


def test_builtin_contributions_are_not_required(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", viewport="phone")
    def plain(s: eks.Session) -> None:
        s.ui.tap("#kaydet")
        s.checkpoint("liste", viewport="desktop")
        s.net.shape("3g")

    assert plan_of(plain).requires == []


def test_plugin_target_kind_through_a_shorthand_is_required(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app")
    def tapped(s: eks.Session) -> None:
        s.ui.tap("aria=Kaydet")

    plan = plan_of(tapped)
    assert plan.nodes[0].args["target"] == {"by": "aria", "args": {"label": "Kaydet"}}
    assert plan.requires == KIT


def test_plugin_target_kind_through_by_custom_is_required(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app")
    def custom(s: eks.Session) -> None:
        s.ui.tap(by.custom("aria", label="Kaydet"))

    @eks.flow(app="web-app")
    def scoped(s: eks.Session) -> None:
        s.ui.tap(by.text("Kaydet").within("aria=Form"))

    assert plan_of(custom).requires == KIT
    assert plan_of(scoped).requires == KIT


def test_plugin_target_inside_check_params_is_required(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app")
    def spilled(s: eks.Session) -> None:
        s.checkpoint("liste", checks=[eks.checks.use("layout_spill", ignore=["aria=Banner"])])

    assert plan_of(spilled).requires == KIT


def test_flow_checks_are_required(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app", checks=["acme_contrast"])
    def checked(s: eks.Session) -> None:
        s.ui.tap("#kaydet")

    plan = plan_of(checked)
    assert plan.session is not None
    assert [use.kind for use in plan.session.checks] == ["acme_contrast"]
    assert plan.requires == KIT


def write_tree(root: Path, body: str) -> Path:
    (root / ".harness" / "flows").mkdir(parents=True)
    (root / ".harness" / "project.toml").write_text("[project]\n", encoding="utf-8")
    (root / "web-app" / ".harness").mkdir(parents=True)
    (root / "web-app" / ".harness" / "app.toml").write_text(body, encoding="utf-8")
    return root


def test_profile_default_checks_are_required(eks_registry: Registry, tmp_path: Path) -> None:
    register_kit(eks_registry)
    tree = write_tree(tmp_path, PROFILE)

    @eks.flow(app="web-app")
    def defaulted(s: eks.Session) -> None:
        s.ui.tap("#kaydet")

    plan = trace(eks_registry.entry_for(defaulted), platform="web", tree=tree)
    assert plan.session is not None
    assert [use.kind for use in plan.session.checks] == ["acme_contrast"]
    assert plan.requires == KIT


@pytest.mark.parametrize("case", ["flow_viewport", "checkpoint", "record", "type", "shape", "shot", "web_viewport"])
def test_plugin_presets_are_required(eks_registry: Registry, case: str) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app", viewport="acme-wide" if case == "flow_viewport" else None)
    def preset_flow(s: eks.Session) -> None:
        s.ui.tap("#kaydet")
        if case == "checkpoint":
            s.checkpoint("liste", viewport="acme-wide")
        elif case == "record":
            with s.record("demo", pace="acme-slow"):
                s.ui.tap("#kaydet")
        elif case == "type":
            with s.record("demo"):
                s.ui.type("#ara", "Kadıköy", pace="acme-slow")
        elif case == "shape":
            s.net.shape("acme-edge")
        elif case == "shot":
            s.shot("liste", viewport="acme-wide")
        elif case == "web_viewport":
            s.web.viewport("acme-wide")

    assert plan_of(preset_flow).requires == KIT


def test_take_session_contributions_are_required(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.score(fps=30)
    def scored(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", viewport="acme-wide")
        with take.flow() as s:
            s.ui.nav("/liste")

    assert eks.plan(scored).requires == KIT


def test_plugin_versions_pin_the_plan_hash(eks_registry: Registry) -> None:
    def planned(version: str) -> Plan:
        state = eks_registry.snapshot()
        try:
            register_kit(eks_registry, version)

            @eks.flow(app="web-app")
            def tapped(s: eks.Session) -> None:
                s.ui.tap("aria=Kaydet")

            return plan_of(tapped)
        finally:
            eks_registry.restore(state)

    first, second = planned("1.0"), planned("2.0")
    assert second.requires == [Requirement(plugin=PLUGIN, version="2.0")]
    assert first.nodes[0].hash != second.nodes[0].hash
    assert first.hash != second.hash


@pytest.mark.parametrize("case", ["flow_as", "session_as", "api_as"])
def test_plugin_persona_presets_are_required(eks_registry: Registry, case: str) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app", as_="acme-admin" if case == "flow_as" else None)
    def acted(s: eks.Session) -> None:
        if case == "session_as":
            s.as_("acme-admin")
        s.ui.tap("#kaydet")
        if case == "api_as":
            s.api.get("/api/tenants", as_="acme-admin")

    assert plan_of(acted).requires == KIT


def test_take_session_persona_preset_is_required(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.score(fps=30)
    def scored(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", as_="acme-admin")
        with take.flow() as s:
            s.ui.nav("/liste")

    assert eks.plan(scored).requires == KIT


@pytest.mark.parametrize("case", ["closure", "default"])
def test_plugin_targets_bound_into_a_dynamic_function_are_required(eks_registry: Registry, case: str) -> None:
    register_kit(eks_registry)

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

    plan = plan_of(closure if case == "closure" else default)
    assert [node.op for node in plan.nodes] == ["ctl.dynamic"]
    assert plan.requires == KIT


def test_plain_json_inputs_of_a_dynamic_function_are_not_targets(eks_registry: Registry) -> None:
    register_kit(eks_registry)

    @eks.flow(app="web-app")
    def plain(s: eks.Session) -> None:
        @eks.dynamic
        def run(rows: dict[str, str] = TARGET_LOOKALIKE, label: str = "aria=Kaydet") -> None:
            s.ui.tap("#kaydet")

    assert plan_of(plain).requires == []


@pytest.mark.parametrize("case", ["target", "flow_checks", "viewport", "shape", "persona", "dynamic"])
def test_unloaded_plugin_is_reported_as_missing(eks_registry: Registry, case: str) -> None:
    state = eks_registry.snapshot()
    register_kit(eks_registry)

    @eks.flow(app="web-app", checks=["acme_contrast"] if case == "flow_checks" else (),
              viewport="acme-wide" if case == "viewport" else None, as_="acme-admin" if case == "persona" else None)
    def used(s: eks.Session) -> None:
        s.ui.tap("aria=Kaydet" if case == "target" else "#kaydet")
        if case == "shape":
            s.net.shape("acme-edge")
        if case == "dynamic":
            save = by.custom("aria", label="Kaydet")

            @eks.dynamic
            def run() -> None:
                s.ui.tap(save)

    plan = plan_of(used)
    assert plan.requires == KIT
    assert "plugin.missing" not in [issue.code for issue in eks.validate(plan).issues]
    eks_registry.restore(state)
    codes = [issue.code for issue in eks.validate(plan).issues]
    assert "plugin.missing" in codes


def register_motion_kit(registry: Registry) -> None:
    style = next(spec.value for spec in registry.presets("style") if spec.provenance.plugin == "eks")
    assert isinstance(style, OverlayStyle)
    with contributing(PLUGIN, "1.0"):
        @eks.curve(description="Wobble")
        class Wobble(CurveBase, frozen=True):
            kind: Literal["wobble"] = "wobble"
            amp: float = 1.0

        @eks.preset("brand", description="Brand overlay style")
        def brand() -> OverlayStyle:
            return style.model_copy(update={"stroke": 7})

    del Wobble, brand


def gfx_assign_plan() -> Plan:
    @eks.scene(kind="gfx", size=(1080, 1920), duration=t("2s"))
    def card(sc: eks.GfxScene) -> None:
        box = sc.rect(1080.0, 1920.0, fill="#0b1628")
        sc.on(t("1s"), box.assign("opacity", eks.curves.custom("wobble", amp=0.2)))

    @eks.edit(fps=30, size=(1080, 1920))
    def promo(e: eks.Edit) -> None:
        e.track("main").clip(e.gfx(card), start=t("0s"))

    return eks.plan(promo)


def dynamic_overlay_plan() -> Plan:
    @eks.flow(app="web-app")
    def flow(s: eks.Session) -> None:
        cap = eks.overlay.caption("Merhaba", style="brand")

        @eks.dynamic
        def go(item: Any = cap) -> None:
            eks.annotate(s.shot("liste"), [item])

    return plan_of(flow)


def dynamic_name_plan(case: str) -> Plan:
    @eks.flow(app="web-app")
    def default(s: eks.Session) -> None:
        @eks.dynamic
        def go(profile: str = "acme-edge") -> None:
            s.net.shape(profile)

    @eks.flow(app="web-app")
    def closure(s: eks.Session) -> None:
        pace = "acme-slow"

        @eks.dynamic
        def go() -> None:
            with s.record("demo"):
                s.ui.type("#ara", "Kadikoy", pace=pace)

    return plan_of(default if case == "default" else closure)


def test_plugin_curve_assigned_in_a_gfx_scene_is_required(eks_registry: Registry) -> None:
    register_motion_kit(eks_registry)
    assert gfx_assign_plan().requires == KIT


def test_overlay_style_passed_into_a_dynamic_function_is_required(eks_registry: Registry) -> None:
    register_motion_kit(eks_registry)
    plan = dynamic_overlay_plan()
    assert [node.op for node in plan.nodes] == ["ctl.dynamic"]
    assert plan.requires == KIT


@pytest.mark.parametrize("case", ["default", "closure"])
def test_preset_names_passed_into_a_dynamic_function_are_required(eks_registry: Registry, case: str) -> None:
    register_kit(eks_registry)
    assert dynamic_name_plan(case).requires == KIT


@pytest.mark.parametrize("case", ["gfx", "dynamic_overlay", "dynamic_name"])
def test_unloaded_plugin_of_a_json_encoded_value_is_reported_as_missing(eks_registry: Registry, case: str) -> None:
    state = eks_registry.snapshot()
    register_kit(eks_registry)
    register_motion_kit(eks_registry)
    builders: dict[str, Callable[[], Plan]] = {
        "gfx": gfx_assign_plan,
        "dynamic_overlay": dynamic_overlay_plan,
        "dynamic_name": lambda: dynamic_name_plan("default"),
    }
    plan = builders[case]()
    assert plan.requires == KIT
    assert "plugin.missing" not in [issue.code for issue in eks.validate(plan).issues]
    eks_registry.restore(state)
    assert "plugin.missing" in [issue.code for issue in eks.validate(plan).issues]
