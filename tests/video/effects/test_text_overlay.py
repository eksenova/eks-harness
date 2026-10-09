"""Text overlay effect tests."""

from __future__ import annotations

from eks_harness.video.ir import Animated, EffectAdapter, Seconds
from eks_harness.video.ir.effects import TextOverlay
from eks_harness.video.plugins.builtin.effects.text_overlay import TextOverlayPlugin
from eks_harness.video.render.context import RenderContext


def test_text_overlay_round_trip() -> None:
    effect = TextOverlay(
        text="hello",
        x=Animated[int](root=50),
        y=Animated[int](root=80),
    )
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, TextOverlay)
    assert rebuilt.text == "hello"
    assert rebuilt.x.root == 50


def test_text_overlay_graph_chain_emits_drawtext(render_ctx: RenderContext) -> None:
    effect = TextOverlay(text="hello")
    serialized = TextOverlayPlugin().compile_graph(effect, render_ctx).serialize()
    assert serialized.startswith("drawtext=")
    assert "fontsize=48" in serialized
    assert "x=100" in serialized
    assert "y=100" in serialized


def test_text_overlay_emits_enable_when_timing_set(render_ctx: RenderContext) -> None:
    effect = TextOverlay(text="hi", start=Seconds(t=1.0), duration=0.5)
    serialized = TextOverlayPlugin().compile_graph(effect, render_ctx).serialize()
    assert "enable=" in serialized
    assert "between(t,1.000000,1.500000)" in serialized
