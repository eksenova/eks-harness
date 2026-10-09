"""Watermark overlay tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.animated import Keyframe
from eks_harness.video.ir.effects import Watermark
from eks_harness.video.ir.time import Seconds
from eks_harness.video.plugins.builtin.effects.watermark import WatermarkPlugin
from eks_harness.video.render.context import RenderContext

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


@pytest.fixture
def watermark_image(tmp_path: Path) -> Path:
    image = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    path = tmp_path / "wm.png"
    image.save(path)
    return path


def test_watermark_round_trip(watermark_image: Path) -> None:
    effect = Watermark(path=watermark_image)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Watermark)
    assert rebuilt.x.root == 20
    assert rebuilt.opacity.root == 1.0


def test_watermark_constant_path_emits_overlay(
    watermark_image: Path, render_ctx: RenderContext
) -> None:
    effect = Watermark(path=watermark_image)
    serialized = WatermarkPlugin().compile_graph(effect, render_ctx).serialize()
    assert "movie=" in serialized
    assert "overlay=" in serialized
    assert "scale=iw*1.000000:ih*1.000000" in serialized


def test_watermark_animated_prefers_frame_pipeline(
    watermark_image: Path, render_ctx: RenderContext
) -> None:
    animated_x = Animated[int](
        root=[
            Keyframe[int](t=Seconds(t=0.0), v=0),
            Keyframe[int](t=Seconds(t=1.0), v=200),
        ]
    )
    effect = Watermark(path=watermark_image, x=animated_x)
    plugin = WatermarkPlugin()
    assert plugin.prefers_frame_pipeline(effect, render_ctx) is True


def test_watermark_frame_pipeline_paints_pixels(
    watermark_image: Path, render_ctx: RenderContext
) -> None:
    effect = Watermark(path=watermark_image, x=Animated[int](root=2), y=Animated[int](root=3))
    processor = WatermarkPlugin().open(effect, render_ctx)
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    # Watermark is red RGBA (255,0,0,255). In BGR frame, red lives at channel 2.
    assert int(out[5, 5, 2]) == 255
    # Outside the watermark region (placed at x=2,y=3, 8x8) the frame is still black.
    assert int(out[0, 0, 2]) == 0
