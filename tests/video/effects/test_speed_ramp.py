"""SpeedRamp effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import SpeedRamp
from eks_harness.video.plugins.builtin.effects.speed_ramp import SpeedRampPlugin
from eks_harness.video.render.context import RenderContext


def test_speed_ramp_round_trip() -> None:
    effect = SpeedRamp(factor=Animated[float](root=2.0))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, SpeedRamp)
    assert rebuilt.factor.root == 2.0


def test_speed_ramp_graph_chain_emits_setpts(render_ctx: RenderContext) -> None:
    effect = SpeedRamp(factor=Animated[float](root=0.5))
    chain = SpeedRampPlugin().compile_graph(effect, render_ctx)
    serialised = chain.serialize()
    assert "setpts=" in serialised
    assert "PTS" in serialised
