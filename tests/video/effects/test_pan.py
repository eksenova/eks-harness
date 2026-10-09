"""Pan effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Pan
from eks_harness.video.plugins.builtin.effects.pan import PanPlugin
from eks_harness.video.render.context import RenderContext


def test_pan_round_trip() -> None:
    effect = Pan(dx=Animated[int](root=10), dy=Animated[int](root=-5))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Pan)
    assert rebuilt.dx.root == 10


def test_pan_graph_chain(render_ctx: RenderContext) -> None:
    effect = Pan(dx=Animated[int](root=10), dy=Animated[int](root=-5))
    chain = PanPlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "crop=iw:ih:(10.000000):(-5.000000)"
