"""Flash effect tests."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import Animated, EffectAdapter, Flash, Seconds
from eks_harness.video.plugins.builtin.effects.flash import FlashPlugin
from eks_harness.video.render.context import RenderContext


def test_flash_round_trip() -> None:
    effect = Flash(at=Seconds(t=1.0), duration=Animated[float](root=0.1))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Flash)
    assert rebuilt.color == (255, 255, 255)


def test_flash_graph_chain(render_ctx: RenderContext) -> None:
    effect = Flash(at=Seconds(t=0.5), duration=Animated[float](root=0.1))
    serialized = FlashPlugin().compile_graph(effect, render_ctx).serialize()
    assert serialized == "fade=t=in:st=0.500000:d=0.100000:color=0xffffff"


def test_flash_frame_processor_brightens_at_trigger(render_ctx: RenderContext) -> None:
    render_ctx.extra["segment_duration_seconds"] = 1.0
    effect = Flash(at=Seconds(t=0.0), duration=Animated[float](root=0.1))
    processor = FlashPlugin().open(effect, render_ctx)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert int(out.mean()) > 200
