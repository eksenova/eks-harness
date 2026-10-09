"""Blur effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Blur
from eks_harness.video.plugins.builtin.effects.blur import BlurPlugin
from eks_harness.video.render.context import RenderContext


def test_blur_round_trip() -> None:
    effect = Blur(radius=Animated[float](root=3.0))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Blur)
    assert rebuilt.radius.root == 3.0


def test_blur_graph_chain(render_ctx: RenderContext) -> None:
    effect = Blur(radius=Animated[float](root=2.5))
    chain = BlurPlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "gblur=sigma=2.500000"
