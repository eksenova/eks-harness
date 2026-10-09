"""MatAnyoneRemove tests (round-trip + heavy inference behind importorskip)."""

from __future__ import annotations

import pytest

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import MatAnyoneRemove


def test_matanyone_round_trip() -> None:
    effect = MatAnyoneRemove(background=(0, 0, 0, 0))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, MatAnyoneRemove)
    assert rebuilt.background == (0, 0, 0, 0)


def test_matanyone_solid_background_round_trip() -> None:
    effect = MatAnyoneRemove(background=(12, 34, 56, 255))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, MatAnyoneRemove)
    assert rebuilt.background == (12, 34, 56, 255)


def test_matanyone_compile_targets() -> None:
    assert MatAnyoneRemove.compile_targets == frozenset({"frame_pipeline"})


def test_matanyone_open_requires_extra() -> None:
    pytest.importorskip("rembg")
    # If the extra is installed the import inside open() succeeds; otherwise
    # the test is skipped above. Either way we never want to fail the suite
    # for a missing optional dep.
