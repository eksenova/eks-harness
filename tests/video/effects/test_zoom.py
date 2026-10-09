"""Zoom effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Zoom
from eks_harness.video.plugins.builtin.effects.zoom import ZoomPlugin
from eks_harness.video.render.context import RenderContext


def test_zoom_round_trip() -> None:
    effect = Zoom(
        scale=Animated[float](root=1.5),
        cx=Animated[float](root=0.5),
        cy=Animated[float](root=0.5),
    )
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Zoom)
    assert rebuilt.scale.root == 1.5


def test_zoom_graph_chain_emits_crop_and_scale(render_ctx: RenderContext) -> None:
    effect = Zoom(
        scale=Animated[float](root=2.0),
        cx=Animated[float](root=0.5),
        cy=Animated[float](root=0.5),
    )
    chain = ZoomPlugin().compile_graph(effect, render_ctx)
    serialised = chain.serialize()
    assert "crop=" in serialised
    assert "scale=" in serialised
