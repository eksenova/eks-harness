"""Saturation effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Saturation
from eks_harness.video.plugins.builtin.effects.saturation import SaturationPlugin
from eks_harness.video.render.context import RenderContext


def test_saturation_round_trip() -> None:
    effect = Saturation(amount=Animated[float](root=0.8))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Saturation)
    assert rebuilt.amount.root == 0.8


def test_saturation_graph_chain(render_ctx: RenderContext) -> None:
    effect = Saturation(amount=Animated[float](root=0.8))
    chain = SaturationPlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "eq=saturation=0.800000"
