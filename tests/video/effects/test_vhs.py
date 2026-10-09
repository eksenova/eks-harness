"""VHS effect tests."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import VHS
from eks_harness.video.plugins.builtin.effects.vhs import VHSPlugin
from eks_harness.video.render.context import RenderContext


def test_vhs_round_trip() -> None:
    effect = VHS(scanlines=True, chroma_blur=2.0, noise=0.1)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, VHS)
    assert rebuilt.chroma_blur == 2.0


def test_vhs_scanlines_dim_alternate_rows(render_ctx: RenderContext) -> None:
    processor = VHSPlugin().open(
        VHS(scanlines=True, chroma_blur=0.0, noise=0.0), render_ctx
    )
    frame = np.full((8, 8, 3), 200, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert int(out[0, 0, 0]) == 170  # 200 * 0.85
    assert int(out[1, 0, 0]) == 200


def test_vhs_noise_changes_frame(render_ctx: RenderContext) -> None:
    processor = VHSPlugin().open(
        VHS(scanlines=False, chroma_blur=0.0, noise=0.2), render_ctx
    )
    frame = np.full((8, 8, 3), 128, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=5)
    assert not np.array_equal(out, frame)
