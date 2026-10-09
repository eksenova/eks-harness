"""SAM3Track tests.

Round-trips the IR and confirms the entry-point registers the plugin. The
heavy ML inference path is not exercised - it requires the SAM 3 model
checkpoint and is far too slow for unit tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "sam3", reason="SAM3Track requires `pip install eks-harness[sam3]`"
)

from eks_harness.video.ir import EffectAdapter  # noqa: E402
from eks_harness.video.ir.effects import SAM3Track  # noqa: E402
from eks_harness.video.plugins.builtin.effects.sam3_track import SAM3TrackPlugin  # noqa: E402
from eks_harness.video.plugins.registry import _EFFECTS, load_builtins  # noqa: E402


def test_sam3_track_round_trip() -> None:
    effect = SAM3Track(prompt="person", output="matte", confidence_threshold=0.7)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, SAM3Track)
    assert rebuilt.prompt == "person"
    assert rebuilt.output == "matte"
    assert rebuilt.confidence_threshold == pytest.approx(0.7)


def test_sam3_track_defaults() -> None:
    effect = SAM3Track(prompt="yellow school bus")
    assert effect.output == "mask"
    assert effect.confidence_threshold == pytest.approx(0.5)


def test_sam3_track_plugin_is_registered() -> None:
    load_builtins()
    matches = [p for p in _EFFECTS.values() if p.model is SAM3Track]
    assert matches, "SAM3TrackPlugin not registered for SAM3Track IR"
    assert isinstance(matches[0], SAM3TrackPlugin)
