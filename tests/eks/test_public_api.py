from __future__ import annotations

import inspect
import json
import re
import sys
import types
from pathlib import Path

import eks
import eks.checks
import eks.overlay
from eks.discovery import load_file, module_name
from eks_harness.ir import registry as ir_registry
from eks_harness.ir.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs" / "dsl" / "contracts-m0.md"
TYPING_EXAMPLES = Path(__file__).parent / "typing_public_api.py"

PUBLIC_API = [
    "Args", "Artifact", "BlenderScene", "CurveBase", "Data", "EasingBase", "Edit", "EffectBase", "GfxScene", "Json",
    "JsonLike", "MarkerBase", "Media", "NetProfile", "OverlayBase", "OverlayStyle", "Pace", "Persona", "PersonaPreset", "Plan", "PresetModel",
    "Ref", "Resource", "ResourceRef", "Score", "Session", "Span", "Target", "Time", "TimeSet", "TransitionBase",
    "Viewport", "WebScene", "annotate", "app", "backend", "backend_policy", "by", "check", "checks", "curve", "curves",
    "dynamic", "each", "ease", "easing", "edit", "effect", "expect", "flow", "fx", "keys", "macro", "marker", "media", "op",
    "otherwise", "overlay", "overlay_kind", "parallel", "plan", "preset", "refers_to", "renderer", "requires", "resource",
    "scene", "score",
    "seed", "t", "target_kind", "transition", "tx", "validate", "when",
]


def contract_names() -> list[str]:
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("Public API of `import eks`", 1)[1].split("\n## ", 1)[0]
    listing = section.split("):", 1)[1]
    return re.findall(r"`([A-Za-z_]+)`", listing)


def test_all_equals_the_contract_list() -> None:
    assert sorted(eks.__all__) == sorted(PUBLIC_API)
    assert sorted(contract_names()) == sorted(PUBLIC_API)
    assert list(eks.__all__) == sorted(eks.__all__, key=lambda name: (not name[0].isupper(), name))
    assert len(set(eks.__all__)) == len(eks.__all__)


def test_every_public_name_resolves() -> None:
    for name in eks.__all__:
        assert getattr(eks, name) is not None, name
    namespace: dict[str, object] = {}
    exec("from eks import *", namespace)  # noqa: S102
    assert set(eks.__all__) <= set(namespace)


def test_decorators_and_modules_are_the_right_objects() -> None:
    assert eks.edit is ir_registry_entry("edit")
    assert eks.score is ir_registry_entry("score")
    assert eks.flow is ir_registry_entry("flow")
    assert eks.seed is ir_registry_entry("seed")
    assert eks.scene is ir_registry_entry("scene")
    assert callable(eks.edit)
    assert callable(eks.score)
    assert isinstance(eks.overlay, types.ModuleType)
    assert isinstance(eks.checks, types.ModuleType)
    assert eks.overlay is sys.modules["eks.overlay"]
    assert eks.checks is sys.modules["eks.checks"]
    assert eks.media is sys.modules["eks.media"]
    assert eks.annotate is eks.overlay.annotate
    assert eks.op is ir_registry.op
    assert eks.resource is ir_registry.resource
    assert eks.Plan is sys.modules["eks_harness.ir.plan"].Plan
    assert inspect.isclass(eks.Session)
    assert inspect.isclass(eks.Score)
    assert inspect.isclass(eks.Edit)


def ir_registry_entry(name: str) -> object:
    entries = sys.modules["eks_harness.ir.entries"]
    return getattr(entries, name)


def test_no_submodule_shadows_an_exported_name() -> None:
    package = Path(eks.__file__).parent
    modules = {path.stem for path in package.glob("*.py") if path.stem != "__init__"}
    assert modules & set(eks.__all__) == {"checks", "media", "overlay"}


def test_py_typed_ships_with_the_package() -> None:
    marker = Path(eks.__file__).parent / "py.typed"
    assert marker.is_file()
    assert marker.read_bytes() == b""


def test_typing_examples_plan_and_validate(eks_registry: Registry) -> None:
    entries = load_file(TYPING_EXAMPLES)
    assert {(entry.kind, entry.name) for entry in entries} == {
        ("seed", "typing_standard"), ("flow", "typing_subeler"), ("flow", "typing_control"),
        ("edit", "typing_demo"), ("scene", "typing_phone"), ("scene", "typing_titles"), ("score", "typing_ad"),
    }
    module = sys.modules[module_name(TYPING_EXAMPLES)]
    plans = module.typing_plans()
    assert [plan.kind for plan in plans] == ["seed", "flow", "flow", "edit", "scene", "scene", "score"]
    for plan in plans:
        report = eks.validate(plan)
        assert report.ok, report.text()
        assert report.warnings == (), report.text()
    seed = plans[0]
    assert seed.personas == ["host", "harman-admin"]
    again = module.typing_plans()
    assert [json.loads(plan.to_json()) for plan in again] == [json.loads(plan.to_json()) for plan in plans]
    start, window, beats, target = module.typing_time_and_targets()
    assert isinstance(start, eks.Time)
    assert isinstance(window, eks.Span)
    assert isinstance(beats, eks.TimeSet)
    assert isinstance(target, eks.Target)
