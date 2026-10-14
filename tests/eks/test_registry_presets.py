from __future__ import annotations

import re
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

import eks
from eks_harness import __version__
from eks_harness.annotate.spec import BUILTIN_STYLES
from eks_harness.ir.presets import (
    NetProfile,
    OverlayStyle,
    Pace,
    PersonaPreset,
    Viewport,
    register_builtin_presets,
)
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    Registry,
    RegistryError,
)
from eks_harness.ir.time import TICKS_PER_MS, Time, t


def ms(value: int) -> Time:
    return Time(ticks=value * TICKS_PER_MS)


def camel_to_snake(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name).lower()


def annotate_style_as_preset(raw: dict[str, Any]) -> dict[str, Any]:
    # Map the old annotate style document onto the flat snake_case fields of OverlayStyle.
    return {
        "ref_width": raw["refWidth"],
        "fonts": raw["fonts"],
        "colors": {camel_to_snake(key): value for key, value in raw["colors"].items()},
        "stroke": raw["stroke"],
        "halo": raw["halo"],
        "badge_shape": raw["badge"]["shape"],
        "badge_size": raw["badge"]["size"],
        "badge_font_size": raw["badge"]["fontSize"],
        "callout_font_size": raw["callout"]["fontSize"],
        "callout_padding": raw["callout"]["padding"],
        "callout_leader_width": raw["callout"]["leaderWidth"],
        "callout_max_width": raw["callout"]["maxWidth"],
        "arrow_width": raw["arrow"]["width"],
        "arrow_head": raw["arrow"]["head"],
        "spotlight_dim": raw["spotlight"]["dim"],
        "caption_font_size": raw["caption"]["fontSize"],
        "caption_bar_height": raw["caption"]["barHeight"],
        "title_font_size": raw["title"]["fontSize"],
        "title_sub_font_size": raw["title"]["subFontSize"],
    }


def test_builtin_viewports(eks_registry: Registry) -> None:
    assert eks_registry.preset("viewport", "desktop").value == Viewport(width=1920, height=1080)
    assert eks_registry.preset("viewport", "phone").value == Viewport(
        width=390, height=844, scale=3.0, mobile=True, touch=True)
    assert eks_registry.preset("viewport", "tablet").value == Viewport(
        width=820, height=1180, scale=2.0, mobile=True, touch=True)


def test_builtin_demo_pace_is_the_only_pace(eks_registry: Registry) -> None:
    demo = eks_registry.preset("pace", "demo").value
    assert demo == Pace(move=ms(300), dwell=ms(400), type_per_char=ms(40), hold=ms(700), screen_hold=ms(1500),
                        press=ms(250))
    assert isinstance(demo, Pace)
    assert demo.move == t("300ms")
    assert demo.screen_hold == t("1.5s")
    assert [spec.name for spec in eks_registry.presets("pace")] == ["demo"]


def test_demo_pace_matches_the_capture_pace_settings(eks_registry: Registry) -> None:
    from eks_harness.config import SETTINGS

    defaults = {key: setting.default for key, setting in SETTINGS.items()}
    demo = Pace(move=ms(defaults["capture.pace.moveMs"]), dwell=ms(defaults["capture.pace.dwellMs"]),
                type_per_char=ms(defaults["capture.pace.typeMsPerChar"]), hold=ms(defaults["capture.pace.holdMs"]),
                screen_hold=ms(defaults["capture.pace.screenHoldMs"]),
                press=ms(defaults["capture.pace.mobilePressMs"]))
    assert eks_registry.preset("pace", "demo").value == demo


@pytest.mark.parametrize(
    ("name", "latency", "down", "up", "offline"),
    [("wifi", 2, None, None, False), ("4g", 60, 9000, 9000, False), ("3g", 563, 1440, 675, False),
     ("slow-3g", 2000, 400, 400, False), ("offline", 0, None, None, True)],
)
def test_builtin_networks(
    eks_registry: Registry, name: str, latency: int, down: int | None, up: int | None, offline: bool
) -> None:
    value = eks_registry.preset("network", name).value
    assert value == NetProfile(latency=ms(latency), down_kbps=down, up_kbps=up, loss=0.0, offline=offline)


def test_exactly_five_networks(eks_registry: Registry) -> None:
    assert [spec.name for spec in eks_registry.presets("network")] == ["3g", "4g", "offline", "slow-3g", "wifi"]


@pytest.mark.parametrize("name", ["kb", "review"])
def test_builtin_styles_equal_the_annotate_styles(eks_registry: Registry, name: str) -> None:
    value = eks_registry.preset("style", name).value
    assert isinstance(value, OverlayStyle)
    assert value == OverlayStyle.model_validate(annotate_style_as_preset(BUILTIN_STYLES[name]))
    assert value.model_dump(mode="json") == annotate_style_as_preset(BUILTIN_STYLES[name])


def test_every_annotate_style_is_a_preset(eks_registry: Registry) -> None:
    assert sorted(BUILTIN_STYLES) == [spec.name for spec in eks_registry.presets("style")]


def test_builtin_presets_have_eks_provenance_and_descriptions(eks_registry: Registry) -> None:
    for spec in eks_registry.presets():
        if spec.category in {"viewport", "pace", "network", "style"} and spec.provenance.plugin == BUILTIN_PLUGIN:
            assert spec.provenance.version == __version__
            assert spec.description
    assert {spec.category for spec in eks_registry.presets() if spec.provenance.plugin == BUILTIN_PLUGIN} >= {
        "viewport", "pace", "network", "style"}


def test_registering_builtins_again_is_idempotent(eks_registry: Registry) -> None:
    before = {(s.category, s.name): s.value for s in eks_registry.presets()}
    register_builtin_presets()
    after = {(s.category, s.name): s.value for s in eks_registry.presets()}
    assert before == after


def test_project_preset_infers_its_category(eks_registry: Registry) -> None:
    @eks.preset("acme-admin")
    def admin() -> PersonaPreset:
        return PersonaPreset(label="Acme admin", username="admin@acme.test", roles=("admin",))

    @eks.preset("acme-edge")
    def edge() -> NetProfile:
        return NetProfile(latency=ms(300), down_kbps=200, up_kbps=100)

    persona = eks_registry.preset("persona", "acme-admin")
    assert persona.category == "persona"
    assert persona.provenance.plugin == PROJECT_PLUGIN
    assert eks_registry.preset("network", "acme-edge").value.model_dump()["down_kbps"] == 200


def test_custom_preset_model_category(eks_registry: Registry) -> None:
    class Palette(eks.PresetModel):
        preset_category: ClassVar[str] = "palette"
        primary: str

    @eks.preset("acme")
    def acme() -> Palette:
        return Palette(primary="#112233")

    assert eks_registry.preset("palette", "acme").value == Palette(primary="#112233")


def test_preset_without_category_on_a_plain_model_raises() -> None:
    class Plain(BaseModel):
        value: int

    with pytest.raises(RegistryError, match="pass category="):
        @eks.preset("plain")
        def plain() -> Plain:
            return Plain(value=1)


def test_plain_model_with_explicit_category(eks_registry: Registry) -> None:
    class Plain(BaseModel):
        value: int

    @eks.preset("plain", category="numbers")
    def plain() -> Plain:
        return Plain(value=1)

    assert eks_registry.preset("numbers", "plain").value == Plain(value=1)


def test_preset_must_return_a_model() -> None:
    with pytest.raises(RegistryError, match="must return a pydantic model"):
        @eks.preset("nothing", category="viewport")
        def nothing() -> int:
            return 1


def test_preset_function_is_called_once_and_returned() -> None:
    calls: list[int] = []

    def phone_wide() -> Viewport:
        calls.append(1)
        return Viewport(width=844, height=390, scale=3.0, mobile=True, touch=True)

    decorated = eks.preset("phone-wide")(phone_wide)
    assert decorated is phone_wide
    assert calls == [1]
