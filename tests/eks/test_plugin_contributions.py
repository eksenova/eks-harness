from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.tracing import TraceError
from eks_harness.engine.validator import validate
from eks_harness.ir.hashing import resource_hash
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, contributing
from eks_harness.plugins.sdk import BackendSpec


def rasterize(source: Path, out: Path, *, size: tuple[int, int]) -> Path:
    del source, size
    return out


class AcmeApi:
    def describe(self) -> dict[str, Any]:
        return {}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return "acme"

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec()

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


@contextmanager
def isolated(registry: Registry) -> Iterator[None]:
    state = registry.snapshot()
    try:
        yield
    finally:
        registry.restore(state)


def one(plan: Plan, op: str) -> Node:
    (node,) = [node for node, _ancestors in plan.walk() if node.op == op]
    return node


def logo_plan(registry: Registry, plugin_version: str | None) -> Plan:
    with isolated(registry):
        if plugin_version is not None:
            with contributing("acme.svg", plugin_version):
                eks.renderer("svg", description="Rasterizes SVG stills")(rasterize)

        @eks.scene(name="svg_card", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=True)
        def card(sc: eks.GfxScene) -> None:
            sc.image("assets/logo.svg", id="logo")

        return eks.plan(card)


def test_a_source_a_plugin_renders_names_the_renderer(eks_registry: Registry) -> None:
    plan = logo_plan(eks_registry, "1.0")
    source = one(plan, "media.source")
    assert source.args == {"uri": "file:assets/logo.svg", "renderer": "svg"}
    assert [(item.plugin, item.version) for item in plan.requires] == [("acme.svg", "1.0")]
    bumped = logo_plan(eks_registry, "2.0")
    plain = logo_plan(eks_registry, None)
    assert one(bumped, "media.source").hash != source.hash
    assert one(bumped, "gfx.render").hash != one(plan, "gfx.render").hash
    assert one(plain, "media.source").args == {"uri": "file:assets/logo.svg"}
    assert plain.requires == []
    assert len({plan.hash, bumped.hash, plain.hash}) == 3


def test_a_missing_renderer_plugin_is_reported(eks_registry: Registry) -> None:
    plan = logo_plan(eks_registry, "1.0")
    report = validate(plan, versions={})
    assert sorted(issue.code for issue in report.issues if issue.code != "plan.hash") == [
        "plugin.missing", "renderer.unknown"]


def test_an_unregistered_renderer_cannot_be_named(eks_registry: Registry) -> None:
    @eks.edit(name="named_renderer", fps=30, size=(1080, 1920))
    def named(e: eks.Edit) -> None:
        e.track("main").clip(eks.media.source("assets/logo.svg", renderer="svg"))

    with pytest.raises(TraceError, match="names renderer 'svg', which no loaded plugin registers"):
        eks.plan(named)


def backend_plan(registry: Registry, plugin_version: str) -> Plan:
    with isolated(registry):
        with contributing("acme.backends", plugin_version):
            eks.backend("acme-api", description="The acme API")(AcmeApi)

        @eks.flow(name="on_acme", app="web-app", platform="web", backend="acme-api", now="2026-01-15T09:00+03:00",
                  timezone="Europe/Istanbul", locale="tr-TR")
        def on_acme(s: eks.Session) -> None:
            s.api.get("/api/items")

        return eks.plan(on_acme)


def test_the_backend_plugin_is_required_and_hashed(eks_registry: Registry) -> None:
    first, again, bumped = (backend_plan(eks_registry, "1.0"), backend_plan(eks_registry, "1.0"),
                            backend_plan(eks_registry, "2.0"))
    assert [(item.plugin, item.version) for item in first.requires] == [("acme.backends", "1.0")]
    (backend,) = [resource for resource in first.resources if resource.kind == "backend"]
    assert backend.definition == "acme-api"
    assert again.hash == first.hash
    assert bumped.hash != first.hash
    assert one(bumped, "api.get").hash != one(first, "api.get").hash
    with isolated(eks_registry):
        with contributing("acme.backends", "1.0"):
            eks.backend("acme-api", description="The acme API")(AcmeApi)
        registered = resource_hash(backend, eks_registry)
    assert registered != resource_hash(backend, eks_registry)
    report = validate(first, versions={})
    assert "plugin.missing" in [issue.code for issue in report.issues]
