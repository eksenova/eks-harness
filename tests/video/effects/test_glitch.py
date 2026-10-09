"""Glitch effect tests (deterministic via seed)."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Glitch
from eks_harness.video.plugins.builtin.effects.glitch import GlitchPlugin
from eks_harness.video.render.context import RenderContext


def test_glitch_round_trip() -> None:
    effect = Glitch(intensity=Animated[float](root=0.4), block_size=8, seed=7)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Glitch)
    assert rebuilt.seed == 7


def test_glitch_zero_intensity_is_passthrough(render_ctx: RenderContext) -> None:
    processor = GlitchPlugin().open(Glitch(intensity=Animated[float](root=0.0)), render_ctx)
    frame = np.full((16, 16, 3), 50, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert np.array_equal(out, frame)


def test_glitch_is_deterministic_with_same_seed(render_ctx: RenderContext) -> None:
    rng_frame = np.random.default_rng(42).integers(0, 256, size=(32, 32, 3), dtype=np.uint8)
    a = GlitchPlugin().open(Glitch(intensity=Animated[float](root=0.5), seed=11), render_ctx)
    b = GlitchPlugin().open(Glitch(intensity=Animated[float](root=0.5), seed=11), render_ctx)
    out_a = a.process(rng_frame.copy(), t=0.0, frame_idx=3)
    out_b = b.process(rng_frame.copy(), t=0.0, frame_idx=3)
    assert np.array_equal(out_a, out_b)
