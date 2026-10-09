"""Captions effect: IR round-trip + frame-pipeline render smoke test.

Avoids invoking any STT backend by feeding hand-built ``WordToken`` lists
straight into ``CaptionsProcessor``. The smoke test renders onto a black
1080x1920 frame and verifies that text actually landed in the configured
band of the output.
"""

from __future__ import annotations

import numpy as np
import pytest

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import Captions
from eks_harness.video.plugins.builtin.captioners import WordToken
from eks_harness.video.plugins.builtin.effects.captions import CaptionsProcessor


def _frame(height: int = 1920, width: int = 1080) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def _words() -> list[WordToken]:
    return [
        WordToken(text="hello", start=0.0, end=0.4),
        WordToken(text="world", start=0.4, end=0.8),
        WordToken(text="from", start=0.8, end=1.2),
        WordToken(text="captions", start=1.2, end=1.8),
    ]


def test_captions_round_trip() -> None:
    effect = Captions(source="vo", style="tiktok", margin=120, max_words_per_card=3)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Captions)
    assert rebuilt.source == "vo"
    assert rebuilt.max_words_per_card == 3
    assert rebuilt.margin == 120


def test_captions_processor_writes_pixels_in_bottom_band_tiktok() -> None:
    pytest.importorskip("PIL")
    ir = Captions(source="vo", style="tiktok", margin=120, max_words_per_card=2, size=72)
    processor = CaptionsProcessor(ir=ir, words=_words(), fps=30.0)

    out = processor.process(_frame(), t=0.5, frame_idx=15)

    bottom_band = out[-240:]
    nonzero = int(np.count_nonzero(bottom_band))
    assert nonzero >= 1000, f"expected at least 1000 nonzero pixels in bottom band, got {nonzero}"


def test_captions_processor_writes_pixels_in_bottom_band_subtitle() -> None:
    pytest.importorskip("PIL")
    ir = Captions(source="vo", style="subtitle", margin=120, max_words_per_card=2, size=72)
    processor = CaptionsProcessor(ir=ir, words=_words(), fps=30.0)

    out = processor.process(_frame(), t=0.5, frame_idx=15)

    bottom_band = out[-240:]
    nonzero = int(np.count_nonzero(bottom_band))
    assert nonzero >= 1000, f"expected at least 1000 nonzero pixels in bottom band, got {nonzero}"


def test_captions_processor_passes_through_when_no_active_card() -> None:
    pytest.importorskip("PIL")
    ir = Captions(source="vo", max_words_per_card=4)
    processor = CaptionsProcessor(ir=ir, words=_words(), fps=30.0)

    frame = _frame()
    out = processor.process(frame, t=10.0, frame_idx=300)

    assert np.array_equal(out, frame)
