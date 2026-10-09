"""RGBSplit effect tests."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import RGBSplit
from eks_harness.video.plugins.builtin.effects.rgbsplit import RGBSplitPlugin
from eks_harness.video.render.context import RenderContext


def test_rgbsplit_round_trip() -> None:
    effect = RGBSplit(offset_x=Animated[int](root=4))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, RGBSplit)
    assert rebuilt.offset_x.root == 4


def test_rgbsplit_zero_offset_is_passthrough(render_ctx: RenderContext) -> None:
    processor = RGBSplitPlugin().open(RGBSplit(offset_x=Animated[int](root=0)), render_ctx)
    frame = np.full((8, 8, 3), 100, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert np.array_equal(out, frame)


def test_rgbsplit_shifts_red_and_blue(render_ctx: RenderContext) -> None:
    processor = RGBSplitPlugin().open(RGBSplit(offset_x=Animated[int](root=2)), render_ctx)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[:, 0, 2] = 255  # leftmost column has full red
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert int(out[0, 2, 2]) == 255  # red shifted left by 2 -> column 2
