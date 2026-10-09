"""Posterize effect tests."""

from __future__ import annotations

import numpy as np
from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Posterize
from eks_harness.video.plugins.builtin.effects.posterize import PosterizePlugin
from eks_harness.video.render.context import RenderContext


def test_posterize_round_trip() -> None:
    effect = Posterize(levels=Animated[int](root=4))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Posterize)
    assert rebuilt.levels.root == 4


def test_posterize_quantizes_values(render_ctx: RenderContext) -> None:
    effect = Posterize(levels=Animated[int](root=4))
    processor = PosterizePlugin().open(effect, render_ctx)
    # Make a frame with 256 distinct intensity columns.
    row = np.arange(256, dtype=np.uint8)
    frame = np.stack([row] * 3, axis=-1)
    frame = np.broadcast_to(frame, (4, 256, 3)).astype(np.uint8).copy()
    out = processor.process(frame, t=0.0, frame_idx=0)
    unique_levels = np.unique(out[0, :, 0])
    assert len(unique_levels) == 4
    # Quantization step is 64, so 64 contiguous source values collapse to the same level.
    assert int(out[0, 0, 0]) == 0
    assert int(out[0, 63, 0]) == 0
    assert int(out[0, 64, 0]) == 64
