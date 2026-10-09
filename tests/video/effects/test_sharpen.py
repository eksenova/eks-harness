"""Sharpen effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Sharpen
from eks_harness.video.plugins.builtin.effects.sharpen import SharpenPlugin
from eks_harness.video.render.context import RenderContext


def test_sharpen_round_trip() -> None:
    effect = Sharpen(amount=Animated[float](root=0.8))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Sharpen)
    assert rebuilt.amount.root == 0.8


def test_sharpen_graph_chain(render_ctx: RenderContext) -> None:
    effect = Sharpen(amount=Animated[float](root=1.2))
    serialized = SharpenPlugin().compile_graph(effect, render_ctx).serialize()
    assert serialized == "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=1.200000"
