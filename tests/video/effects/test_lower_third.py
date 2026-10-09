"""Lower-third overlay tests."""

from __future__ import annotations

import numpy as np
import pytest
from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import LowerThird
from eks_harness.video.plugins.builtin.effects.lower_third import LowerThirdPlugin
from eks_harness.video.render.context import RenderContext

pytest.importorskip("PIL")


def test_lower_third_round_trip() -> None:
    effect = LowerThird(title="Name", subtitle="Title", position="center")
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, LowerThird)
    assert rebuilt.title == "Name"
    assert rebuilt.position == "center"


def test_lower_third_paints_panel_in_lower_third(render_ctx: RenderContext) -> None:
    # White input + dark semi-transparent panel = visible darkening in the lower third.
    effect = LowerThird(title="Hi", margin=4, bg_color=(0, 0, 0, 180))
    processor = LowerThirdPlugin().open(effect, render_ctx)
    frame = np.full((64, 64, 3), 255, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    panel_top = (64 * 2) // 3
    # Upper region remains white.
    assert int(out[0, 0].min()) == 255
    # Lower region is darkened by the panel composite.
    assert int(out[panel_top + 4, 32].max()) < 200
