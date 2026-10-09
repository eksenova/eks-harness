"""Sticker overlay tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import StickerOverlay
from eks_harness.video.plugins.builtin.effects.sticker import StickerOverlayPlugin
from eks_harness.video.render.context import RenderContext

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


@pytest.fixture
def sticker_image(tmp_path: Path) -> Path:
    image = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
    path = tmp_path / "sticker.png"
    image.save(path)
    return path


def test_sticker_round_trip(sticker_image: Path) -> None:
    effect = StickerOverlay(path=sticker_image)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, StickerOverlay)
    assert rebuilt.scale.root == 1.0
    assert rebuilt.rotation.root == 0.0


def test_sticker_pastes_onto_frame(sticker_image: Path, render_ctx: RenderContext) -> None:
    effect = StickerOverlay(
        path=sticker_image,
        x=Animated[int](root=1),
        y=Animated[int](root=1),
    )
    processor = StickerOverlayPlugin().open(effect, render_ctx)
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    # Sticker is green (0,255,0). In BGR frame, green lives at channel 1.
    assert int(out[2, 2, 1]) == 255
    assert int(out[0, 0, 1]) == 0
