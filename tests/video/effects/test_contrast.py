"""Contrast effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Contrast
from eks_harness.video.plugins.builtin.effects.contrast import ContrastPlugin
from eks_harness.video.render.context import RenderContext


def test_contrast_round_trip() -> None:
    effect = Contrast(amount=Animated[float](root=1.4))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Contrast)
    assert rebuilt.amount.root == 1.4


def test_contrast_graph_chain(render_ctx: RenderContext) -> None:
    effect = Contrast(amount=Animated[float](root=1.4))
    chain = ContrastPlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "eq=contrast=1.400000"
