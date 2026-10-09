"""Crop effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, Crop, EffectAdapter
from eks_harness.video.plugins.builtin.effects.crop import CropPlugin
from eks_harness.video.render.context import RenderContext


def test_crop_round_trip() -> None:
    effect = Crop(
        x=Animated[int](root=10),
        y=Animated[int](root=20),
        w=Animated[int](root=100),
        h=Animated[int](root=200),
    )
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Crop)
    assert rebuilt.w.root == 100


def test_crop_graph_chain(render_ctx: RenderContext) -> None:
    effect = Crop(
        x=Animated[int](root=10),
        y=Animated[int](root=20),
        w=Animated[int](root=100),
        h=Animated[int](root=200),
    )
    chain = CropPlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "crop=100.000000:200.000000:10.000000:20.000000"
