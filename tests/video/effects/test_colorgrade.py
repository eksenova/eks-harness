"""ColorGrade effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import ColorGrade
from eks_harness.video.plugins.builtin.effects.colorgrade import ColorGradePlugin
from eks_harness.video.render.context import RenderContext


def test_colorgrade_round_trip() -> None:
    effect = ColorGrade(
        exposure=Animated[float](root=0.1),
        temperature=Animated[float](root=-0.05),
        tint=Animated[float](root=0.02),
    )
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, ColorGrade)
    assert rebuilt.exposure.root == 0.1


def test_colorgrade_graph_chain(render_ctx: RenderContext) -> None:
    effect = ColorGrade(
        exposure=Animated[float](root=0.1),
        temperature=Animated[float](root=-0.05),
        tint=Animated[float](root=0.02),
    )
    serialized = ColorGradePlugin().compile_graph(effect, render_ctx).serialize()
    assert "eq=brightness=0.100000" in serialized
    assert "colorbalance=" in serialized
