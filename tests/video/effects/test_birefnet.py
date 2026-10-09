"""BiRefNetRemove tests (round-trip + heavy inference behind importorskip)."""

from __future__ import annotations

import pytest

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import BiRefNetRemove


def test_birefnet_round_trip() -> None:
    effect = BiRefNetRemove(background=(0, 0, 0, 0))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, BiRefNetRemove)
    assert rebuilt.background == (0, 0, 0, 0)


def test_birefnet_solid_background_round_trip() -> None:
    effect = BiRefNetRemove(background=(255, 0, 128, 255))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, BiRefNetRemove)
    assert rebuilt.background == (255, 0, 128, 255)


def test_birefnet_compile_targets() -> None:
    assert BiRefNetRemove.compile_targets == frozenset({"frame_pipeline"})


def test_birefnet_open_requires_extra() -> None:
    pytest.importorskip("rembg")
    # Heavy inference is skipped when the extra is not installed.
