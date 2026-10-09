"""Brightness effect: IR round-trip, graph chain, frame processor."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, Brightness, EffectAdapter
from eks_harness.video.plugins.builtin.effects.brightness import BrightnessPlugin
from eks_harness.video.render.context import RenderContext


def test_brightness_round_trip() -> None:
    effect = Brightness(amount=Animated[float](root=0.2))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Brightness)
    assert rebuilt.amount.root == 0.2


def test_brightness_graph_chain(render_ctx: RenderContext) -> None:
    effect = Brightness(amount=Animated[float](root=0.2))
    chain = BrightnessPlugin().compile_graph(effect, render_ctx)
    assert chain.serialize() == "eq=brightness=0.200000"


def test_brightness_frame_processor_increases_value(render_ctx: RenderContext) -> None:
    effect = Brightness(amount=Animated[float](root=0.2))
    processor = BrightnessPlugin().open(effect, render_ctx)
    frame = np.full((64, 64, 3), 128, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    expected = min(255, 128 + int(round(0.2 * 255)))
    assert int(out[0, 0, 0]) == expected
