"""Shake effect tests (deterministic via seed)."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Shake
from eks_harness.video.plugins.builtin.effects.shake import ShakePlugin
from eks_harness.video.render.context import RenderContext


def test_shake_round_trip() -> None:
    effect = Shake(intensity=Animated[float](root=0.3), freq_hz=5.0, decay=0.1)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Shake)
    assert rebuilt.freq_hz == 5.0


def test_shake_zero_intensity_is_passthrough(render_ctx: RenderContext) -> None:
    processor = ShakePlugin().open(Shake(intensity=Animated[float](root=0.0)), render_ctx)
    frame = np.full((16, 16, 3), 100, dtype=np.uint8)
    out = processor.process(frame, t=0.5, frame_idx=15)
    assert np.array_equal(out, frame)


def test_shake_is_deterministic(render_ctx: RenderContext) -> None:
    a = ShakePlugin().open(Shake(intensity=Animated[float](root=0.5)), render_ctx)
    b = ShakePlugin().open(Shake(intensity=Animated[float](root=0.5)), render_ctx)
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 256, size=(32, 32, 3), dtype=np.uint8)
    out_a = a.process(frame.copy(), t=0.5, frame_idx=15)
    out_b = b.process(frame.copy(), t=0.5, frame_idx=15)
    assert np.array_equal(out_a, out_b)
