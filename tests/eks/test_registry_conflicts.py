from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

import eks
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.catalog.transitions import TransitionBase
from eks_harness.ir.presets import Viewport
from eks_harness.ir.registry import (
    BUILTIN_FAMILIES,
    OpSpec,
    Provenance,
    Registry,
    RegistryError,
    contributing,
    definition_of,
)
from eks_harness.plugins.sdk import BackendSpec


@pytest.fixture
def module_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A throwaway import root so tests can define, import and reload real modules. Bytecode caching is off:
    # a rewrite within the same second and of the same size would otherwise reload stale bytecode.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    root = tmp_path / "mods"
    root.mkdir()
    sys.path.insert(0, str(root))
    before = set(sys.modules)
    try:
        yield root
    finally:
        sys.path.remove(str(root))
        for name in set(sys.modules) - before:
            sys.modules.pop(name, None)


def write_module(root: Path, name: str, body: str) -> None:
    (root / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    importlib.invalidate_caches()


OP_MODULE = """
    import eks

    @eks.op("demo", out="{out}")
    class OpenUrl(eks.Args):
        url: str
"""


def test_registering_the_same_class_again_is_fine(eks_registry: Registry) -> None:
    class Ping(eks.Args):
        pass

    eks.op("demo")(Ping)
    eks.op("demo")(Ping)
    assert eks_registry.op("demo.ping").args is Ping


def test_another_class_with_the_same_op_name_raises_naming_both(eks_registry: Registry) -> None:
    @eks.op("demo", name="open_url")
    class First(eks.Args):
        pass

    with pytest.raises(RegistryError) as error:
        @eks.op("demo", name="open_url")
        class Second(eks.Args):
            pass

    message = str(error.value)
    assert "op 'demo.open_url'" in message
    assert f"{__name__}:{First.__qualname__}" in message
    assert "Second" in message
    assert "project" in message
    assert eks_registry.op("demo.open_url").args is First


def test_reloading_a_module_replaces_its_registrations(module_dir: Path, eks_registry: Registry) -> None:
    write_module(module_dir, "p5_reload_ops", OP_MODULE.format(out="none"))
    module = importlib.import_module("p5_reload_ops")
    first = eks_registry.op("demo.open_url")
    assert first.args is module.OpenUrl
    write_module(module_dir, "p5_reload_ops", OP_MODULE.format(out="json"))
    module = importlib.reload(module)
    second = eks_registry.op("demo.open_url")
    assert second.args is module.OpenUrl
    assert second.args is not first.args
    assert second.out == "json"


def test_same_qualname_in_another_module_conflicts(module_dir: Path, eks_registry: Registry) -> None:
    write_module(module_dir, "p5_first_ops", OP_MODULE.format(out="none"))
    write_module(module_dir, "p5_second_ops", OP_MODULE.format(out="none"))
    importlib.import_module("p5_first_ops")
    with pytest.raises(RegistryError, match="p5_first_ops:OpenUrl.*p5_second_ops:OpenUrl"):
        importlib.import_module("p5_second_ops")
    assert eks_registry.op("demo.open_url").args.__module__ == "p5_first_ops"


def test_scoped_plugin_modules_of_a_new_generation_replace(eks_registry: Registry) -> None:
    # A changed plugin is imported under a new scope prefix; its definitions count as a reload.
    old = type("Probe", (eks.Args,), {"__module__": "_eks_harness_plugin_acme_0123456789ab.acme_ops"})
    new = type("Probe", (eks.Args,), {"__module__": "_eks_harness_plugin_acme_ba9876543210.acme_ops"})
    other = type("Probe", (eks.Args,), {"__module__": "_eks_harness_plugin_other_ba9876543210.acme_ops"})
    with contributing("acme", "1"):
        eks.op("acme", name="probe")(old)
        eks.op("acme", name="probe")(new)
    assert eks_registry.op("acme.probe").args is new
    with contributing("other", "1"), pytest.raises(RegistryError):
        eks.op("acme", name="probe")(other)


def test_plugin_op_in_a_builtin_family_raises(eks_registry: Registry) -> None:
    assert "ui" in BUILTIN_FAMILIES
    with contributing("acme.tools", "1"), pytest.raises(RegistryError) as error:
        @eks.op("ui")
        class AcmeSwipe(eks.Args):
            pass

    message = str(error.value)
    assert "built-in family 'ui'" in message
    assert "acme_tools.acme_swipe" in message
    assert not eks_registry.has_op("ui.acme_swipe")


@pytest.mark.parametrize("plugin", ["eks", "project"])
def test_reserved_plugin_ids_cannot_contribute(plugin: str, eks_registry: Registry) -> None:
    # Otherwise a plugin with id 'eks' would pass the built-in family rule.
    with pytest.raises(RegistryError, match=f"plugin id '{plugin}' is reserved"), contributing(plugin, "9"):
        @eks.op("ui")
        class Hijack(eks.Args):
            pass

    assert not eks_registry.has_op("ui.hijack")
    with contributing("eks.drivers", "1"), pytest.raises(RegistryError, match="built-in family 'ui'"):
        @eks.op("ui")
        class Sneak(eks.Args):
            pass


def test_drop_module_removes_every_table_and_matches_scoped_generations(eks_registry: Registry) -> None:
    old = type("Probe", (eks.Args,), {"__module__": "_eks_harness_plugin_acme_0123456789ab.acme_ops"})
    keep = type("Keep", (eks.Args,), {"__module__": "_eks_harness_plugin_acme_0123456789ab.acme_other"})
    with contributing("acme", "1"):
        eks.op("acme", name="probe")(old)
        eks.op("acme", name="keep")(keep)
    dropped = eks_registry.drop_module("_eks_harness_plugin_acme_ba9876543210.acme_ops", "acme")
    assert [spec.name for spec in dropped if isinstance(spec, OpSpec)] == ["acme.probe"]
    assert not eks_registry.has_op("acme.probe")
    assert eks_registry.has_op("acme.keep")
    # Without a plugin id only the exact module name matches.
    assert eks_registry.drop_module("_eks_harness_plugin_acme_ba9876543210.acme_other") == []
    assert eks_registry.has_op("acme.keep")


def test_rollback_reverts_one_module_and_keeps_the_rest(module_dir: Path, eks_registry: Registry) -> None:
    write_module(module_dir, "p5_helper_ops", '''
        import eks

        @eks.op("acme")
        class Helper(eks.Args):
            pass
    ''')
    state = eks_registry.snapshot()

    class Broken(eks.Args):
        pass

    eks.op("acme")(Broken)
    importlib.import_module("p5_helper_ops")
    eks_registry.rollback(state, __name__)
    assert not eks_registry.has_op("acme.broken")
    assert eks_registry.has_op("acme.helper")


def test_project_op_in_a_builtin_family_raises(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="built-in family 'capture'"):
        @eks.op("capture")
        class Snap(eks.Args):
            pass

    assert not eks_registry.has_op("capture.snap")


def test_builtin_provenance_may_use_builtin_families(eks_registry: Registry) -> None:
    class Extra(eks.Args):
        pass

    spec = OpSpec(name="ui.extra_probe", family="ui", args=Extra, provenance=Provenance("eks", "0"))
    eks_registry.register_op(spec)
    assert eks_registry.op("ui.extra_probe") is spec


@pytest.mark.parametrize("family", ["Demo", "1demo", "de-mo", "", "demo.sub"])
def test_bad_op_families_raise(family: str) -> None:
    with pytest.raises(RegistryError, match="family.name"):
        @eks.op(family, name="thing")
        class Thing(eks.Args):
            pass


@pytest.mark.parametrize("name", ["Thing", "thing-x", "2go", "a.b", "thing\n"])
def test_bad_op_names_raise(name: str) -> None:
    with pytest.raises(RegistryError, match="family.name"):
        @eks.op("demo", name=name)
        class Thing(eks.Args):
            pass


def test_op_family_mismatch_raises(eks_registry: Registry) -> None:
    class Thing(eks.Args):
        pass

    with pytest.raises(RegistryError, match="declares family"):
        eks_registry.register_op(OpSpec(name="demo.thing", family="other", args=Thing))


def test_op_out_must_be_a_ref_type() -> None:
    with pytest.raises(RegistryError, match="not a ref type"):
        @eks.op("demo", out="dict")
        class Thing(eks.Args):
            pass


def test_op_branches_need_a_container() -> None:
    with pytest.raises(RegistryError, match="container=True"):
        @eks.op("demo", branches=["body"])
        class Loop(eks.Args):
            pass


def test_op_platforms_must_be_known() -> None:
    with pytest.raises(RegistryError, match="platforms"):
        @eks.op("demo", platforms=["windows"])
        class Thing(eks.Args):
            pass


def test_op_decorates_models_only() -> None:
    with pytest.raises(RegistryError, match="pydantic model class"):
        eks.op("demo")(len)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("kind", ["Bad", "bad-kind", "9lives", ""])
def test_bad_check_kinds_raise(kind: str) -> None:
    with pytest.raises(RegistryError, match="check kind"):
        @eks.check(kind)
        class Params(eks.Args):
            pass


@pytest.mark.parametrize("kind", ["Bad", "bad-kind", "two words"])
def test_bad_catalog_kinds_raise(kind: str) -> None:
    with pytest.raises(RegistryError, match="kind"):
        @eks.target_kind(kind, surfaces=["web"])
        class Widget(BaseModel, frozen=True, extra="forbid"):
            name: str


def test_catalog_model_kind_must_match(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="needs a 'kind' field"):
        @eks.effect()
        class NoDefault(EffectBase, frozen=True):
            pass

    class Glow(EffectBase, frozen=True):
        kind: Literal["acme_glow"] = "acme_glow"

    from eks_harness.ir.registry import CatalogSpec

    with pytest.raises(RegistryError, match="declares kind 'acme_glow'"):
        eks_registry.register_catalog(CatalogSpec(category="effect", kind="acme_other", model=Glow))


def test_target_shorthands_must_be_unique(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="shorthand 'testid' is already used by target kind 'test_id'"):
        @eks.target_kind("acme_id", surfaces=["web"], shorthand="testid")
        class AcmeId(BaseModel, frozen=True, extra="forbid"):
            value: str

    with pytest.raises(RegistryError, match="surfaces"):
        @eks.target_kind("acme_far", surfaces=["moon"])
        class Far(BaseModel, frozen=True, extra="forbid"):
            value: str


def test_macro_names_are_validated(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="public Python identifier"):
        @eks.macro(name="open-menu")
        def open_menu(s: object) -> None:
            del s

    with pytest.raises(RegistryError, match="would shadow the built-in s.ui"):
        @eks.macro(namespace="ui")
        def wiggle(s: object) -> None:
            del s

    with pytest.raises(RegistryError, match="macro namespace"):
        @eks.macro(namespace="Acme")
        def shake(s: object) -> None:
            del s

    @eks.macro()
    def login(s: object) -> None:
        del s

    with pytest.raises(RegistryError, match="macro 'project.login' is already registered"):
        @eks.macro(name="login")
        def other_login(s: object) -> None:
            del s

    assert eks_registry.macro("project.login").fn is login


def test_resource_validation(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="positional names"):
        @eks.resource("acme_item", positional=("missing",))
        class Item(eks.Resource):
            name: str

    with pytest.raises(RegistryError, match="persona kind must be keyed"):
        @eks.resource("acme_person", persona=True, keyed=False)
        class Person(eks.Resource):
            pass

    with pytest.raises(RegistryError, match="resource kind"):
        @eks.resource("Acme-Thing")
        class Thing(eks.Resource):
            pass

    assert all(spec.kind not in {"acme_item", "acme_person"} for spec in eks_registry.resources())


def test_preset_conflicts_and_names(eks_registry: Registry) -> None:
    @eks.preset("acme-wall")
    def wall() -> Viewport:
        return Viewport(width=3840, height=2160)

    with pytest.raises(RegistryError, match="viewport preset 'acme-wall' is already registered"):
        @eks.preset("acme-wall")
        def other_wall() -> Viewport:
            return Viewport(width=1, height=1)

    with pytest.raises(RegistryError, match="viewport preset 'phone' is already registered"):
        @eks.preset("phone")
        def my_phone() -> Viewport:
            return Viewport(width=1, height=1)

    with pytest.raises(RegistryError, match="preset name"):
        @eks.preset("bad name")
        def spaced() -> Viewport:
            return Viewport(width=1, height=1)

    with pytest.raises(RegistryError, match="belongs to category 'viewport'"):
        @eks.preset("wrong", category="pace")
        def wrong() -> Viewport:
            return Viewport(width=1, height=1)

    assert eks_registry.preset("viewport", "acme-wall").value.model_dump()["width"] == 3840
    eks.preset("acme-wall")(wall)


class FirstApi:
    def describe(self) -> dict[str, Any]:
        return {}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return "first"

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec()

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


class SecondApi(FirstApi):
    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return "second"


def test_renderer_backend_and_entry_conflicts(eks_registry: Registry) -> None:
    eks.backend("acme_api")(FirstApi)
    eks.backend("acme_api")(FirstApi)
    with pytest.raises(RegistryError, match="backend 'acme_api' is already registered"):
        eks.backend("acme_api")(SecondApi)

    @eks.flow("acme-flow", app="web-app", platform="web")
    def first(s: object) -> None:
        del s

    with pytest.raises(RegistryError, match="flow 'acme-flow' is already registered"):
        @eks.flow("acme-flow", app="web-app", platform="web")
        def second(s: object) -> None:
            del s

    assert eks_registry.entry("flow", "acme-flow").fn is first
    with pytest.raises(RegistryError, match="backend name"):
        eks.backend("Acme API")(FirstApi)


def test_lookup_errors_suggest_close_names(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match=r"unknown op 'ui\.tapp'; did you mean 'ui\.tap'"):
        eks_registry.op("ui.tapp")
    with pytest.raises(RegistryError, match="did you mean 'zoom'"):
        eks_registry.catalog("effect", "zom")
    with pytest.raises(RegistryError, match="did you mean 'phone'"):
        eks_registry.preset("viewport", "phon")
    with pytest.raises(RegistryError, match="did you mean 'layout_spill'"):
        eks_registry.check("layout_spil")
    with pytest.raises(RegistryError, match="known: "):
        eks_registry.preset("pace", "zzzzzz")
    with pytest.raises(RegistryError, match="none are registered"):
        eks_registry.macro("nothing.here")


def test_lookup_errors_are_value_errors(eks_registry: Registry) -> None:
    with pytest.raises(ValueError, match="unknown resource kind"):
        eks_registry.resource("nope")
    with pytest.raises(ValueError, match="unknown backend"):
        eks_registry.backend("nope")
    with pytest.raises(ValueError, match="unknown flow"):
        eks_registry.entry("flow", "nope")
    with pytest.raises(ValueError, match="unknown renderer media kind"):
        eks_registry.renderer("nope")
    with pytest.raises(ValueError, match="unknown backend policy"):
        eks_registry.backend_policy("nope")


def test_definition_describe_names_module_and_plugin() -> None:
    class Local(eks.Args):
        pass

    spec = OpSpec(name="acme.local", family="acme", args=Local, provenance=Provenance("acme", "2.0"))
    described = definition_of(spec).describe()
    assert described == f"{__name__}:{Local.__qualname__} (from acme 2.0)"


def test_op_and_check_models_must_be_strict_args(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="op 'acme.loose': Loose must subclass eks.Args"):
        @eks.op("acme")
        class Loose(BaseModel):
            url: str

    with pytest.raises(RegistryError, match="must forbid unknown fields"):
        @eks.op("acme")
        class Lenient(eks.Args, extra="ignore"):
            url: str

    with pytest.raises(RegistryError, match="check 'acme_lax': Lax must subclass eks.Args"):
        @eks.check("acme_lax")
        class Lax(BaseModel):
            limit: int = 1

    assert not eks_registry.has_op("acme.loose")
    assert not eks_registry.has_op("acme.lenient")
    assert all(spec.kind != "acme_lax" for spec in eks_registry.checks())


def test_resource_and_catalog_models_must_be_strict(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="resource 'acme_loose': Loose must subclass eks.Resource"):
        @eks.resource("acme_loose")
        class Loose(BaseModel):
            name: str

    with pytest.raises(RegistryError, match="must forbid unknown fields"):
        @eks.resource("acme_open")
        class Open(eks.Resource, extra="allow"):
            name: str

    with pytest.raises(RegistryError, match="effect kind 'acme_soft'.*must forbid unknown fields"):
        @eks.effect()
        class Soft(EffectBase, frozen=True, extra="ignore"):
            kind: Literal["acme_soft"] = "acme_soft"

    @eks.effect()
    class Firm(EffectBase, frozen=True):
        kind: Literal["acme_firm"] = "acme_firm"

    assert eks_registry.catalog("effect", "acme_firm").model is Firm
    assert all(spec.kind != "acme_soft" for spec in eks_registry.catalogs("effect"))


def test_catalog_models_must_subclass_their_category_base(eks_registry: Registry) -> None:
    # A plain strict model would register but could never validate into an Effect field.
    with pytest.raises(RegistryError, match=r"effect kind 'acme_plain': Plain must subclass "
                                            r"eks_harness\.ir\.catalog\.effects\.EffectBase"):
        @eks.effect()
        class Plain(BaseModel, frozen=True, extra="forbid"):
            kind: Literal["acme_plain"] = "acme_plain"

    with pytest.raises(RegistryError, match="must subclass eks_harness.ir.catalog.transitions.TransitionBase"):
        @eks.transition()
        class Misfiled(EffectBase, frozen=True):
            kind: Literal["acme_misfiled"] = "acme_misfiled"

    with pytest.raises(RegistryError, match="must subclass eks_harness.ir.ops.overlay.OverlayBase"):
        @eks.overlay_kind(live="acme/live.js")
        class Sticker(TransitionBase, frozen=True):
            kind: Literal["acme_sticker"] = "acme_sticker"

    assert all(spec.kind != "acme_plain" for spec in eks_registry.catalogs("effect"))
    assert all(spec.kind != "acme_misfiled" for spec in eks_registry.catalogs("transition"))
    assert all(spec.kind != "acme_sticker" for spec in eks_registry.catalogs("overlay"))
    assert [spec.kind for spec in eks_registry.resources() if spec.kind.startswith("acme_")] == []


def test_strict_models_reject_misspelled_args(eks_registry: Registry) -> None:
    @eks.op("acme")
    class Visit(eks.Args):
        url: str

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="tagret"):
        eks_registry.op("acme.visit").args.model_validate({"url": "x", "tagret": 1})


def test_name_lists_reject_a_single_string(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match=r"branches= takes a list such as \['body'\], not a single string"):
        @eks.op("acme", container=True, branches="body")
        class Loop(eks.Args):
            pass

    with pytest.raises(RegistryError, match=r"platforms= takes a list such as \['web'\]"):
        @eks.op("acme", platforms="web")
        class Visit(eks.Args):
            pass

    with pytest.raises(RegistryError, match=r"resources= takes a list"):
        @eks.op("acme", resources="lease")  # pyright: ignore[reportArgumentType]
        class Hold(eks.Args):
            pass

    with pytest.raises(RegistryError, match=r"platforms= takes a list"):
        @eks.macro(platforms="ios")
        def shake(s: object) -> None:
            del s

    with pytest.raises(RegistryError, match=r"platforms= takes a list"):
        @eks.check("acme_one", platforms="web")
        class One(eks.Args):
            pass

    with pytest.raises(RegistryError, match=r"positional= takes a list such as \['name'\]"):
        @eks.resource("acme_item", positional="name")
        class Item(eks.Resource):
            name: str

    with pytest.raises(RegistryError, match=r"surfaces= takes a list such as \['web'\]"):
        @eks.target_kind("acme_chip", surfaces="web")
        class Chip(BaseModel, frozen=True, extra="forbid"):
            label: str

    with pytest.raises(RegistryError, match=r"platforms= takes a list"):
        @eks.overlay_kind(live="acme/live.js", platforms="web")
        class Badge(BaseModel, frozen=True, extra="forbid"):
            kind: Literal["acme_badge"] = "acme_badge"

    assert [spec.name for spec in eks_registry.ops("acme")] == []
    assert "acme_chip" not in eks_registry.target_kinds()
