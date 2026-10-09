"""Fade effect tests."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import EffectAdapter, Fade
from eks_harness.video.plugins.builtin.effects.fade import FadePlugin
from eks_harness.video.render.context import RenderContext


def test_fade_round_trip() -> None:
    effect = Fade(direction="in", duration=0.5)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Fade)
    assert rebuilt.direction == "in"


def test_fade_graph_chain(render_ctx: RenderContext) -> None:
    render_ctx.extra["segment_duration_seconds"] = 1.0
    effect = Fade(direction="in", duration=0.4)
    chain = FadePlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "fade=t=in:st=0.000000:d=0.400000"


def test_fade_frame_processor_in(render_ctx: RenderContext) -> None:
    render_ctx.extra["segment_duration_seconds"] = 1.0
    processor = FadePlugin().open(Fade(direction="in", duration=0.5), render_ctx)
    frame = np.full((8, 8, 3), 200, dtype=np.uint8)
    early = processor.process(frame, t=0.0, frame_idx=0)
    late = processor.process(frame, t=0.5, frame_idx=29)
    assert int(early.mean()) == 0 or int(early.mean()) < int(late.mean())
