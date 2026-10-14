from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, SerializeAsAny

import eks
from eks_harness import __version__
from eks_harness.ir.catalog.curves import CurveBase
from eks_harness.ir.catalog.easing import EasingBase
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.catalog.markers import MarkerBase
from eks_harness.ir.catalog.transitions import TransitionBase
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    Provenance,
    Registry,
    RegistryError,
    catalog,
    contributing,
    provenance_for,
    snake_case,
)
from eks_harness.ir.target import BUILTIN_KINDS
from eks_harness.plugins.sdk import BackendChoice, BackendInfo, BackendSpec


def open_url_executor(args: object) -> None:
    del args


class AcmeBackend:
    def describe(self) -> dict[str, Any]:
        return {"name": "acme_api"}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return "acme"

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec()

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


class AcmeManaged:
    def describe(self) -> dict[str, Any]:
        return {"name": "acme_managed"}

    def start(self, context: Mapping[str, Any]) -> BackendInfo:
        return BackendInfo(url="http://127.0.0.1:9000")

    def health(self, context: Mapping[str, Any]) -> bool:
        return True

    def stop(self, context: Mapping[str, Any]) -> None:
        return None

    def cleanup(self, context: Mapping[str, Any]) -> None:
        return None


class AcmePolicy:
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice:
        return BackendChoice(backend=requested or "acme_api", reason="acme")


def acme_renderer() -> None:
    return None


def test_op_defaults_snake_case_name_and_project_provenance(eks_registry: Registry) -> None:
    @eks.op("demo", out="json", executor=open_url_executor, description="Open a URL")
    class OpenUrl(eks.Args):
        url: str

    spec = eks_registry.op("demo.open_url")
    assert spec.args is OpenUrl
    assert spec.family == "demo"
    assert spec.out == "json"
    assert spec.description == "Open a URL"
    assert spec.executor == f"{__name__}:open_url_executor"
    assert spec.ordering == "program"
    assert spec.location == "hub"
    assert spec.provenance == Provenance(PROJECT_PLUGIN, "")


def test_op_executor_string_is_kept_and_pure_ops_order_by_graph(eks_registry: Registry) -> None:
    @eks.op("demo", name="hash_text", pure=True, executor="acme.exec:HashText", platforms=["web"])
    class Anything(eks.Args):
        text: str

    spec = eks_registry.op("demo.hash_text")
    assert spec.executor == "acme.exec:HashText"
    assert spec.pure
    assert spec.ordering == "graph"
    assert spec.platforms == frozenset({"web"})


def test_explicit_ordering_wins_over_pure(eks_registry: Registry) -> None:
    @eks.op("demo", pure=True, ordering="program")
    class Sequenced(eks.Args):
        pass

    assert eks_registry.op("demo.sequenced").ordering == "program"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("OpenUrl", "open_url"), ("OpenURL", "open_url"), ("Tap", "tap"), ("HTTPGetJson", "http_get_json"),
     ("Shot2Png", "shot2_png")],
)
def test_snake_case(name: str, expected: str) -> None:
    assert snake_case(name) == expected


def test_builtin_ops_have_eks_provenance(eks_registry: Registry) -> None:
    tap = eks_registry.op("ui.tap")
    assert tap.provenance == Provenance(BUILTIN_PLUGIN, __version__)
    assert eks_registry.has_op("ui.tap")
    assert not eks_registry.has_op("ui.nothing")


def test_provenance_for_modules() -> None:
    assert provenance_for("eks").plugin == BUILTIN_PLUGIN
    assert provenance_for("eks.session").plugin == BUILTIN_PLUGIN
    assert provenance_for("eks_harness.ir.ops.ui").plugin == BUILTIN_PLUGIN
    assert provenance_for("eksample").plugin == PROJECT_PLUGIN
    with contributing("acme.tools", "1.2"):
        assert provenance_for("acme_ops") == Provenance("acme.tools", "1.2")
        assert provenance_for("eks_harness.ir.ops.ui").plugin == BUILTIN_PLUGIN
    assert provenance_for("acme_ops") == Provenance(PROJECT_PLUGIN, "")


def test_macro_default_namespace_is_project(eks_registry: Registry) -> None:
    @eks.macro(description="Log in")
    def login(s: object, persona: str) -> None:
        del s, persona

    spec = eks_registry.macro("project.login")
    assert spec.namespace == "project"
    assert spec.short == "login"
    assert spec.fn is login
    assert spec.description == "Log in"
    assert eks_registry.macros("project") == [spec]


def test_macro_namespace_derives_from_plugin_id(eks_registry: Registry) -> None:
    with contributing("acme.tools", "1"):
        @eks.macro(platforms=["web"])
        def open_menu(s: object) -> None:
            del s

    spec = eks_registry.macro("acme_tools.open_menu")
    assert spec.namespace == "acme_tools"
    assert spec.provenance == Provenance("acme.tools", "1")
    assert spec.platforms == frozenset({"web"})


def test_macro_plugin_id_dashes_become_underscores(eks_registry: Registry) -> None:
    with contributing("acme-co.web-tools", "2"):
        @eks.macro(name="go")
        def navigate(s: object) -> None:
            del s

    assert eks_registry.macro("acme_co_web_tools.go").fn is navigate


def test_macro_explicit_namespace(eks_registry: Registry) -> None:
    @eks.macro(namespace="harman")
    def pick_branch(s: object) -> None:
        del s

    assert eks_registry.macro("harman.pick_branch").namespace == "harman"


def test_resource_and_check_registrations(eks_registry: Registry) -> None:
    @eks.resource("acme_tenant", positional=("name",), description="A tenant")
    class AcmeTenant(eks.Resource):
        name: str
        currency: str = "TRY"

    @eks.resource("acme_user", parent="acme_tenant", persona=True, phase="finalize")
    class AcmeUser(eks.Resource):
        role: str = "admin"

    @eks.check("acme_budget", family="perf", platforms=["web"], default=True)
    class AcmeBudget(eks.Args):
        max_ms: int = 100

    tenant = eks_registry.resource("acme_tenant")
    assert tenant.model is AcmeTenant
    assert tenant.positional == ("name",)
    assert tenant.parent is None
    assert tenant.provenance.plugin == PROJECT_PLUGIN
    user = eks_registry.resource("acme_user")
    assert (user.parent, user.persona, user.phase, user.model) == ("acme_tenant", True, "finalize", AcmeUser)
    budget = eks_registry.check("acme_budget")
    assert (budget.family, budget.default, budget.args) == ("perf", True, AcmeBudget)
    assert budget in eks_registry.checks("perf")
    assert budget not in eks_registry.checks("media")


def test_catalog_registrations_land_in_their_categories(eks_registry: Registry) -> None:
    # Every category model subclasses the Poly base of its category.
    @eks.effect(description="Sepia")
    class Sepia(EffectBase, frozen=True):
        kind: Literal["acme_sepia"] = "acme_sepia"

    @eks.transition()
    class Swirl(TransitionBase, frozen=True):
        kind: Literal["acme_swirl"] = "acme_swirl"

    @eks.easing()
    class Steps(EasingBase, frozen=True):
        kind: Literal["acme_steps"] = "acme_steps"

    @eks.curve()
    class Saw(CurveBase, frozen=True):
        kind: Literal["acme_saw"] = "acme_saw"

    @catalog("marker")
    class Claps(MarkerBase, frozen=True):
        kind: Literal["acme_claps"] = "acme_claps"

    for category, kind, model in [("effect", "acme_sepia", Sepia), ("transition", "acme_swirl", Swirl),
                                  ("easing", "acme_steps", Steps), ("curve", "acme_saw", Saw),
                                  ("marker", "acme_claps", Claps)]:
        spec = eks_registry.catalog(category, kind)
        assert spec.model is model
        assert spec.category == category
        assert spec.provenance.plugin == PROJECT_PLUGIN
    assert eks_registry.catalog("effect", "acme_sepia").description == "Sepia"
    with pytest.raises(RegistryError):
        eks_registry.catalog("transition", "acme_sepia")


def test_overlay_kind_stores_renderers(eks_registry: Registry) -> None:
    from eks_harness.ir.ops.overlay import OverlayBase

    def draw(frame: object) -> None:
        del frame

    @eks.overlay_kind(live="acme/live.js", screenshot=draw, video="acme.video:Draw", platforms=["web"])
    class Stamp(OverlayBase, frozen=True):
        kind: Literal["acme_stamp"] = "acme_stamp"

    spec = eks_registry.catalog("overlay", "acme_stamp")
    assert spec.model is Stamp
    assert dict(spec.renderers) == {
        "live": "acme/live.js", "screenshot": f"{__name__}:{draw.__qualname__}", "video": "acme.video:Draw"}
    assert spec.platforms == frozenset({"web"})


def test_target_kinds_include_builtins_and_plugins(eks_registry: Registry) -> None:
    assert set(BUILTIN_KINDS) <= set(eks_registry.target_kinds())
    with contributing("acme", "3"):
        @eks.target_kind("acme_widget", surfaces=["web", "ios"], shorthand="widget", description="A widget")
        class AcmeWidget(BaseModel, frozen=True, extra="forbid"):
            name: str

    kinds = eks_registry.target_kinds()
    assert kinds["acme_widget"].args is AcmeWidget
    assert kinds["acme_widget"].surfaces == frozenset({"web", "ios"})
    assert kinds["acme_widget"].shorthand == "widget"
    assert kinds["test_id"].shorthand == BUILTIN_KINDS["test_id"].shorthand
    spec = eks_registry.catalog("target", "acme_widget")
    assert spec.provenance == Provenance("acme", "3")
    assert eks_registry.catalog("target", "test_id").provenance.plugin == BUILTIN_PLUGIN


def test_renderer_backend_and_policy_registrations(eks_registry: Registry) -> None:
    with contributing("acme", "0.4"):
        eks.renderer("acme_scene", description="Renders acme scenes")(acme_renderer)
        eks.backend("acme_api")(AcmeBackend)
        eks.backend_policy("acme_policy")(AcmePolicy)

    renderer = eks_registry.renderer("acme_scene")
    assert renderer.impl == f"{__name__}:acme_renderer"
    assert renderer.provenance == Provenance("acme", "0.4")
    assert eks_registry.backend("acme_api").impl == f"{__name__}:AcmeBackend"
    assert eks_registry.backend_policy("acme_policy").impl == f"{__name__}:AcmePolicy"
    assert renderer in eks_registry.renderers()
    assert [b.name for b in eks_registry.backends() if b.name == "acme_api"] == ["acme_api"]
    assert [p.name for p in eks_registry.backend_policies() if p.name == "acme_policy"] == ["acme_policy"]


def test_backends_and_policies_must_implement_the_sdk_protocols(eks_registry: Registry) -> None:
    # contract 8.2: @eks.backend takes a class implementing Backend (or ManagedBackend), @eks.backend_policy a
    # class implementing BackendPolicy; anything else is rejected at registration
    eks.backend("acme_managed")(AcmeManaged)
    assert eks_registry.backend("acme_managed").impl == f"{__name__}:AcmeManaged"

    def not_a_backend() -> int:
        return 1

    with pytest.raises(RegistryError, match="decorates a class implementing"):
        eks.backend("weird")(not_a_backend)  # pyright: ignore[reportArgumentType]

    class Half:
        def describe(self) -> dict[str, Any]:
            return {}

        def prepare(self, context: Mapping[str, Any]) -> None:
            return None

    with pytest.raises(RegistryError, match=r"does not implement eks_harness\.plugins\.sdk\.Backend; it lacks "
                                            r"fingerprint, spec, teardown"):
        eks.backend("half")(Half)  # pyright: ignore[reportArgumentType]
    with pytest.raises(RegistryError, match="lacks choose"):
        eks.backend_policy("half_policy")(AcmeBackend)  # pyright: ignore[reportArgumentType]
    with pytest.raises(RegistryError, match="unknown backend"):
        eks_registry.backend("weird")


def test_preset_registration(eks_registry: Registry) -> None:
    from eks_harness.ir.presets import Viewport

    @eks.preset("kiosk", description="Portrait kiosk")
    def kiosk() -> Viewport:
        return Viewport(width=1080, height=1920)

    spec = eks_registry.preset("viewport", "kiosk")
    assert spec.value == Viewport(width=1080, height=1920)
    assert spec.provenance.plugin == PROJECT_PLUGIN
    assert spec.description == "Portrait kiosk"
    assert spec.source == f"{__name__}:{kiosk.__qualname__}"


def test_listings_are_sorted(eks_registry: Registry) -> None:
    @eks.op("zzz")
    class Beta(eks.Args):
        pass

    @eks.op("aaa")
    class Alpha(eks.Args):
        pass

    names = [spec.name for spec in eks_registry.ops()]
    assert names == sorted(names)
    assert eks_registry.families() == sorted(eks_registry.families())
    assert [s.kind for s in eks_registry.checks()] == sorted(s.kind for s in eks_registry.checks())
    assert [s.kind for s in eks_registry.catalogs("effect")] == sorted(s.kind for s in eks_registry.catalogs("effect"))
    presets = [(s.category, s.name) for s in eks_registry.presets()]
    assert presets == sorted(presets)
    assert [s.name for s in eks_registry.ops("aaa")] == ["aaa.alpha"]


def test_versions_maps_plugins_to_versions(eks_registry: Registry) -> None:
    with contributing("acme", "9.1"):
        @eks.op("acme")
        class Ping(eks.Args):
            pass

    versions = eks_registry.versions()
    assert versions[BUILTIN_PLUGIN] == __version__
    assert versions["acme"] == "9.1"
    assert list(versions) == sorted(versions)


def test_snapshot_and_restore_copy_every_table(eks_registry: Registry) -> None:
    state = eks_registry.snapshot()

    @eks.op("tmp")
    class Gone(eks.Args):
        pass

    @eks.flow(app="web-app", platform="web")
    def gone_flow(s: object) -> None:
        del s

    assert eks_registry.has_op("tmp.gone")
    eks_registry.restore(state)
    assert not eks_registry.has_op("tmp.gone")
    with pytest.raises(RegistryError):
        eks_registry.entry("flow", "gone_flow")


def test_poly_alias_types_keep_working(eks_registry: Registry) -> None:
    from typing import Annotated

    from eks_harness.ir.registry import Poly

    class Holder(BaseModel):
        effect: Annotated[SerializeAsAny[EffectBase], Poly("effect")]

    assert Holder.model_validate({"effect": {"kind": "zoom", "scale": 2}}).effect.kind == "zoom"
    del eks_registry
