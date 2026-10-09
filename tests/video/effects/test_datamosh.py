"""Datamosh effect tests."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import Datamosh
from eks_harness.video.plugins.builtin.effects.datamosh import DatamoshPlugin
from eks_harness.video.render.context import RenderContext


def test_datamosh_round_trip() -> None:
    effect = Datamosh(intensity=Animated[float](root=0.7))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Datamosh)
    assert rebuilt.intensity.root == 0.7


def test_datamosh_first_frame_passthrough(render_ctx: RenderContext) -> None:
    processor = DatamoshPlugin().open(Datamosh(intensity=Animated[float](root=0.5)), render_ctx)
    frame = np.full((8, 8, 3), 100, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert np.array_equal(out, frame)


def test_datamosh_blends_with_previous(render_ctx: RenderContext) -> None:
    processor = DatamoshPlugin().open(Datamosh(intensity=Animated[float](root=0.5)), render_ctx)
    frame_a = np.full((8, 8, 3), 100, dtype=np.uint8)
    frame_b = np.full((8, 8, 3), 200, dtype=np.uint8)
    processor.process(frame_a, t=0.0, frame_idx=0)
    out = processor.process(frame_b, t=1 / 30, frame_idx=1)
    # Blended: 200*0.5 + 100*0.5 = 150
    assert int(out[0, 0, 0]) == 150
