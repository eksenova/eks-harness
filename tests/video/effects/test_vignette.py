"""Vignette effect tests."""

from __future__ import annotations

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import Vignette
from eks_harness.video.plugins.builtin.effects.vignette import VignettePlugin
from eks_harness.video.render.context import RenderContext


def test_vignette_round_trip() -> None:
    effect = Vignette(angle=0.5, x0=0.4, y0=0.6)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Vignette)
    assert rebuilt.angle == 0.5
    assert rebuilt.x0 == 0.4
    assert rebuilt.y0 == 0.6


def test_vignette_graph_chain(render_ctx: RenderContext) -> None:
    effect = Vignette()
    serialized = VignettePlugin().compile_graph(effect, render_ctx).serialize()
    assert "vignette=" in serialized
    assert "angle=0.628000" in serialized
    # ffmpeg's vignette filter parses ``x0``/``y0`` against lowercase
    # ``w``/``h`` constants; uppercase was rejected as undefined.
    assert "x0=(w*0.500000)" in serialized
    assert "y0=(h*0.500000)" in serialized
