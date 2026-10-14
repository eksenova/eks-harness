from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

import eks
import eks.registry
from eks_harness.ir import registry as ir_registry
from eks_harness.ir.catalog.markers import MarkerBase
from eks_harness.ir.registry import (
    PROJECT_PLUGIN,
    Provenance,
    Registry,
    RegistryError,
    contributing,
)


def test_marker_decorator_is_public_and_needs_no_ir_import() -> None:
    assert "marker" in eks.__all__
    assert "marker" in eks.registry.__all__
    assert "MarkerBase" in eks.__all__
    assert eks.marker is eks.registry.marker is ir_registry.marker
    assert eks.MarkerBase is eks.media.MarkerBase is MarkerBase


def test_bare_and_called_marker_register_with_provenance(eks_registry: Registry) -> None:
    with contributing("acme-beats", "0.3.0"):
        @eks.marker
        class Claps(eks.MarkerBase, frozen=True):
            kind: Literal["acme_claps"] = Field(default="acme_claps", description="Hand claps of a song")

        @eks.marker(description="Cowbell hits", version="2")
        class Cowbell(eks.MarkerBase, frozen=True):
            kind: Literal["acme_cowbell"] = Field(default="acme_cowbell", description="Cowbell hits")
            sensitivity: float = Field(default=0.5, ge=0.0, le=1.0, description="Detector sensitivity")

    @eks.marker()
    class Snaps(eks.MarkerBase, frozen=True):
        kind: Literal["acme_snaps"] = Field(default="acme_snaps", description="Finger snaps")

    kinds = {spec.kind: spec for spec in eks_registry.catalogs("marker")}
    assert {"acme_claps", "acme_cowbell", "acme_snaps", "beat_tracker"} <= set(kinds)
    claps, cowbell, snaps = kinds["acme_claps"], kinds["acme_cowbell"], kinds["acme_snaps"]
    assert (claps.model, claps.category, claps.description, claps.version) == (Claps, "marker", "", "1")
    assert (cowbell.model, cowbell.description, cowbell.version) == (Cowbell, "Cowbell hits", "2")
    assert claps.provenance == Provenance("acme-beats", "0.3.0")
    assert cowbell.provenance == Provenance("acme-beats", "0.3.0")
    assert snaps.model is Snaps
    assert snaps.provenance == Provenance(PROJECT_PLUGIN, "")
    assert eks_registry.catalog("marker", "acme_cowbell") is cowbell
    with pytest.raises(RegistryError):
        eks_registry.catalog("effect", "acme_claps")


def test_plugin_marker_plans_through_media_markers_and_requires_the_plugin(eks_registry: Registry) -> None:
    with contributing("acme-beats", "0.3.0"):
        @eks.marker(description="Hand claps of a song", version="2")
        class Claps(eks.MarkerBase, frozen=True):
            kind: Literal["acme_claps"] = Field(default="acme_claps", description="Hand claps of a song")
            sensitivity: float = Field(default=0.5, ge=0.0, le=1.0, description="Detector sensitivity")

    @eks.edit(fps=30, size=(1080, 1920))
    def clapping(e: eks.Edit) -> None:
        song = e.media("music/song.ogg")
        eks.media.markers(song, Claps(name="claps", sensitivity=0.8))
        e.track("main").clip(e.media("clips/a.mp4"))

    plan = eks.plan(clapping)
    assert [(item.plugin, item.version) for item in plan.requires] == [("acme-beats", "0.3.0")]
    found = [node for node, _ in plan.walk() if node.op == "media.markers"]
    assert len(found) == 1
    sources: Any = found[0].args["sources"]
    assert sources == [{"kind": "acme_claps", "name": "claps", "sensitivity": 0.8}]
    report = eks.validate(plan)
    assert report.ok, report.text()


def test_builtin_markers_do_not_add_requirements(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def beats(e: eks.Edit) -> None:
        eks.media.markers(e.media("music/song.ogg"), eks.media.BeatTracker(name="song"))
        e.track("main").clip(e.media("clips/a.mp4"))

    assert eks.plan(beats).requires == []


def test_a_model_that_is_not_a_marker_base_is_rejected(eks_registry: Registry) -> None:
    class Loose(BaseModel, frozen=True):
        model_config = ConfigDict(extra="forbid")
        kind: Literal["acme_loose"] = "acme_loose"
        name: str = "loose"

    with pytest.raises(RegistryError, match=r"marker kind 'acme_loose': Loose must subclass .*MarkerBase"):
        eks.marker(Loose)
    with pytest.raises(RegistryError, match="must subclass .*MarkerBase"):
        eks.marker(description="Loose", version="2")(Loose)

    class Effecty(eks.EffectBase, frozen=True):
        kind: Literal["acme_effecty"] = "acme_effecty"

    with pytest.raises(RegistryError, match="must subclass .*MarkerBase"):
        eks.marker(Effecty)
    with pytest.raises(RegistryError, match="decorates a pydantic model class"):
        eks.marker(object)  # pyright: ignore[reportArgumentType]
    assert all(spec.kind not in {"acme_loose", "acme_effecty"} for spec in eks_registry.catalogs("marker"))
