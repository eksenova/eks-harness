"""PunchIn tests (IR round-trip + open without ML deps falls back gracefully)."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, EffectAdapter, Seconds
from eks_harness.video.ir.effects import PunchIn
from eks_harness.video.plugins.builtin.effects.punch_in import PunchInPlugin
from eks_harness.video.render.context import RenderContext


def test_punch_in_round_trip() -> None:
    effect = PunchIn(track_speaker=False, zoom=Animated[float](root=1.5), trigger=Seconds(t=0.5))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, PunchIn)
    assert rebuilt.track_speaker is False


def test_punch_in_zoom_one_is_passthrough(render_ctx: RenderContext) -> None:
    processor = PunchInPlugin().open(
        PunchIn(track_speaker=False, zoom=Animated[float](root=1.0)), render_ctx
    )
    frame = np.full((16, 16, 3), 100, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert np.array_equal(out, frame)
