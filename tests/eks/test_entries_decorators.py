from __future__ import annotations

import inspect
from fractions import Fraction
from typing import Any

import pytest

import eks
from eks_harness.ir.entries import (
    ENTRY_ATTR,
    EditOptions,
    FlowOptions,
    SceneOptions,
    ScoreOptions,
    SeedOptions,
)
from eks_harness.ir.registry import (
    PROJECT_PLUGIN,
    Provenance,
    Registry,
    RegistryError,
    contributing,
)
from eks_harness.ir.time import Span, t


def test_flow_registers_an_entry_spec(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", seed="standard", as_="harman-admin", tags=["subeler"], platform="web",
              viewport="phone", pace="demo", timeout=t("30s"), now="2026-01-15T09:00+03:00", root_seed=7,
              noise=["console_errors"], backend="eksenapi", description="Branch list")
    def subeler(s: Any, limit: int = 50) -> dict[str, Any]:
        del s
        return {"limit": limit}

    spec = eks_registry.entry("flow", "subeler")
    assert spec.fn is subeler
    assert spec.kind == "flow"
    assert spec.module == __name__
    assert spec.qualname == subeler.__qualname__
    assert spec.file == __file__
    assert spec.line == inspect.getsourcelines(subeler)[1]
    assert spec.provenance == Provenance(PROJECT_PLUGIN, "")
    assert vars(subeler)[ENTRY_ATTR] == ("flow", "subeler")
    assert eks_registry.entry_for(subeler) is spec
    options = spec.options
    assert isinstance(options, FlowOptions)
    assert options == FlowOptions(
        app="web-app", platforms=("web",), seed="standard", persona="harman-admin", backend="eksenapi",
        viewport="phone", pace="demo", timeout=t("30s"), noise=("console_errors",), tags=("subeler",),
        now="2026-01-15T09:00:00+03:00", root_seed=7, description="Branch list")


def test_flow_returns_the_function_unchanged(eks_registry: Registry) -> None:
    def plain(s: Any) -> None:
        del s

    assert eks.flow(app="web-app")(plain) is plain
    assert eks_registry.entry_for(plain).options == FlowOptions(app="web-app")


def test_flow_platform_matrix(eks_registry: Registry) -> None:
    @eks.flow("matrix", app="mobile-app", platforms=["ios", "android"])
    def anything(s: Any) -> None:
        del s

    options = eks_registry.entry("flow", "matrix").options
    assert isinstance(options, FlowOptions)
    assert options.platforms == ("ios", "android")


def test_platform_and_platforms_together_raise() -> None:
    with pytest.raises(RegistryError, match="platform= or platforms=, not both"):
        eks.flow(app="web-app", platform="web", platforms=["ios"])


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"platforms": []}, "at least one platform"),
     ({"platforms": "web"}, "takes a list"),
     ({"platforms": ["web", "web"]}, "repeat"),
     ({"platform": "windows"}, "platforms"),
     ({"app": " "}, "must not be empty"),
     ({"seed": "bad seed"}, "seed"),
     ({"viewport": "my phone"}, "preset name"),
     ({"timeout": t("2f")}, "wall-clock"),
     ({"timeout": t("0s")}, "wall-clock"),
     ({"now": "2026-01-15T09:00"}, "UTC offset"),
     ({"now": "yesterday"}, "ISO 8601"),
     ({"tags": ["two words"]}, "tag"),
     ({"tags": ["a", "a"]}, "repeat"),
     ({"tags": "single"}, "list of names"),
     ({"backend": "Eksen API"}, "backend")],
)
def test_flow_option_errors(kwargs: dict[str, Any], match: str) -> None:
    values: dict[str, Any] = {"app": "web-app", **kwargs}
    with pytest.raises(RegistryError, match=match) as error:
        eks.flow(**values)
    assert str(error.value).startswith("@eks.flow")


def test_reserved_platform_parameter_raises(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match=r"\['platform'\] are reserved"):
        @eks.flow(app="web-app")
        def picky(s: Any, platform: str = "web") -> None:
            del s, platform

    assert all(spec.name != "picky" for spec in eks_registry.entries("flow"))


def test_flow_without_a_session_parameter_raises() -> None:
    with pytest.raises(RegistryError, match="must take the session"):
        @eks.flow(app="web-app")
        def lonely() -> None:
            return None


def test_entry_first_parameter_must_be_positional() -> None:
    with pytest.raises(RegistryError, match="must take the data builder"):
        @eks.seed("only-keywords", backend="eksenapi")
        def only_keywords(*, d: Any) -> None:
            del d


def test_variadic_parameters_raise() -> None:
    with pytest.raises(RegistryError, match=r"\*args and \*\*kwargs"):
        @eks.flow(app="web-app")
        def loose(s: Any, **params: Any) -> None:
            del s, params

    with pytest.raises(RegistryError, match=r"\*args and \*\*kwargs"):
        @eks.edit(fps=30, size=(1080, 1920))
        def loose_edit(e: Any, *clips: Any) -> None:
            del e, clips


def test_positional_only_parameters_raise() -> None:
    with pytest.raises(RegistryError, match="positional-only"):
        @eks.flow(app="web-app")
        def positional(s: Any, limit: int, /) -> None:
            del s, limit


def test_async_and_generator_entries_raise() -> None:
    with pytest.raises(RegistryError, match="plain function"):
        @eks.flow(app="web-app")
        async def later(s: Any) -> None:
            del s

    with pytest.raises(RegistryError, match="plain function"):
        @eks.score(fps=30)
        def steps(sc: Any) -> Any:
            yield sc


def test_entries_decorate_functions_only() -> None:
    class NotAFunction:
        def __call__(self, s: Any) -> None:
            del s

    with pytest.raises(RegistryError, match="decorates a function"):
        eks.flow(app="web-app")(NotAFunction())


@pytest.mark.parametrize("name", ["checkout", "checkout-flow", "_private", "Flow_2", "a-b-c"])
def test_valid_entry_names(eks_registry: Registry, name: str) -> None:
    def body(s: Any) -> None:
        del s

    eks.flow(name, app="web-app")(body)
    assert eks_registry.entry("flow", name).fn is body


@pytest.mark.parametrize("name", ["2fast", "has space", "dot.name", "", "-dash", "x\n"])
def test_invalid_entry_names(name: str) -> None:
    def body(s: Any) -> None:
        del s

    with pytest.raises(RegistryError, match="must match"):
        eks.flow(name, app="web-app")(body)


def test_lambda_names_need_an_explicit_name(eks_registry: Registry) -> None:
    with pytest.raises(RegistryError, match="pass name="):
        eks.flow(app="web-app")(lambda s: s)
    eks.flow("named-lambda", app="web-app")(lambda s: s)
    assert eks_registry.entry("flow", "named-lambda").qualname.endswith("<lambda>")


def test_seed_options(eks_registry: Registry) -> None:
    @eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026, base="empty",
              tags=["base"], description="Standard scenario")
    def standard(d: Any) -> None:
        del d

    spec = eks_registry.entry("seed", "standard")
    assert spec.options == SeedOptions(backend="eksenapi", now="2026-01-15T09:00:00+03:00", seed=2026, base="empty",
                                       tags=("base",), description="Standard scenario")
    assert eks_registry.entry_for(standard) is spec


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"backend": "Bad Backend"}, "backend"), ({"base": "two words"}, "base seed"),
     ({"seed": "x"}, "seed"), ({"now": "noon"}, "ISO 8601")],
)
def test_seed_option_errors(kwargs: dict[str, Any], match: str) -> None:
    values: dict[str, Any] = {"backend": "eksenapi", **kwargs}
    with pytest.raises(RegistryError, match=match):
        eks.seed("bad", **values)


def test_edit_options_accept_integer_and_ratio_fps(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920), duration=t("12s"), window=Span(start=t("0s"), end=t("10s")),
              background="#000000", seed=3)
    def promo(e: Any) -> None:
        del e

    @eks.edit("ntsc", fps="30000/1001", size=(1920, 1080))
    def ntsc_edit(e: Any) -> None:
        del e

    options = eks_registry.entry("edit", "promo").options
    assert isinstance(options, EditOptions)
    assert options.fps == Fraction(30)
    assert options.duration == t("12s")
    assert options.background == "#000000"
    assert options.model_dump(mode="json")["fps"] == 30
    ntsc = eks_registry.entry("edit", "ntsc").options
    assert isinstance(ntsc, EditOptions)
    assert ntsc.fps == Fraction(30000, 1001)
    assert ntsc.model_dump(mode="json")["fps"] == "30000/1001"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"fps": 0}, "fps"), ({"fps": "fast"}, "fps"), ({"fps": True}, "fps"), ({"size": (0, 10)}, "size"),
     ({"duration": t("0s")}, "positive duration"), ({"duration": t("beat:4")}, "positive duration"),
     ({"background": "black"}, "background")],
)
def test_edit_option_errors(kwargs: dict[str, Any], match: str) -> None:
    values: dict[str, Any] = {"fps": 30, "size": (1080, 1920), **kwargs}
    with pytest.raises(RegistryError, match=match):
        eks.edit(**values)


def test_scene_options(eks_registry: Registry) -> None:
    @eks.scene(kind="blender", engine="cycles", samples=64, device="GPU", fps=30, size=(1080, 1920),
               duration=t("4bar"), bpm=120, transparent=False)
    def phone(sc: Any) -> None:
        del sc

    options = eks_registry.entry("scene", "phone").options
    assert isinstance(options, SceneOptions)
    assert (options.kind, options.engine, options.samples, options.device) == ("blender", "cycles", 64, "GPU")
    assert options.bpm == 120
    assert options.duration == t("4bar")
    assert not options.transparent


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"kind": "unity"}, "kind"), ({"samples": 0}, "samples"), ({"device": "TPU"}, "device"),
     ({"bpm": 0}, "bpm"), ({"size": (10, -1)}, "size"), ({"fps": "30000/0"}, "fps")],
)
def test_scene_option_errors(kwargs: dict[str, Any], match: str) -> None:
    values: dict[str, Any] = {"kind": "gfx", **kwargs}
    with pytest.raises(RegistryError, match=match):
        eks.scene(**values)


def test_score_options(eks_registry: Registry) -> None:
    @eks.score(fps="30000/1001", song="media:song.mp3", bpm=128, meter=(3, 4), duration=t("16bar"))
    def promo_score(sc: Any) -> None:
        del sc

    options = eks_registry.entry("score", "promo_score").options
    assert isinstance(options, ScoreOptions)
    assert options.fps == Fraction(30000, 1001)
    assert options.size == (1080, 1920)
    assert options.meter == (3, 4)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"meter": (4, 3)}, "power-of-two"), ({"meter": (0, 4)}, "meter"), ({"size": (1080, 0)}, "size"),
     ({"fps": -1}, "fps")],
)
def test_score_option_errors(kwargs: dict[str, Any], match: str) -> None:
    values: dict[str, Any] = {"fps": 30, **kwargs}
    with pytest.raises(RegistryError, match=match):
        eks.score(**values)


def test_entry_for_errors(eks_registry: Registry) -> None:
    def unregistered(s: Any) -> None:
        del s

    with pytest.raises(RegistryError, match="is not a registered flow"):
        eks_registry.entry_for(unregistered)

    state = eks_registry.snapshot()

    @eks.flow(app="web-app")
    def transient(s: Any) -> None:
        del s

    eks_registry.restore(state)
    with pytest.raises(RegistryError, match="no longer holds it"):
        eks_registry.entry_for(transient)


def test_entries_inside_contributing_carry_the_plugin(eks_registry: Registry) -> None:
    with contributing("acme", "1.0"):
        @eks.flow(app="web-app")
        def plugin_flow(s: Any) -> None:
            del s

    assert eks_registry.entry_for(plugin_flow).provenance == Provenance("acme", "1.0")


def test_entries_listing_is_sorted(eks_registry: Registry) -> None:
    @eks.seed("zeta", backend="eksenapi")
    def zeta(d: Any) -> None:
        del d

    @eks.flow("alpha", app="web-app")
    def alpha(s: Any) -> None:
        del s

    keys = [(spec.kind, spec.name) for spec in eks_registry.entries()]
    assert keys == sorted(keys)
    assert [spec.name for spec in eks_registry.entries("seed") if spec.name == "zeta"] == ["zeta"]
