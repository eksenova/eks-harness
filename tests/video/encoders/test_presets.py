"""Encoding-preset table tests."""

from __future__ import annotations

import pytest

from eks_harness.video.plugins.builtin.encoders.presets import PRESETS

EXPECTED_PRESETS = {
    "tiktok-1080-h264",
    "reels-1080-h264",
    "shorts-1080-h264",
    "archival-h265",
    "av1-experimental",
}


def test_every_expected_preset_present() -> None:
    assert EXPECTED_PRESETS.issubset(PRESETS.keys())


@pytest.mark.parametrize("name", sorted(EXPECTED_PRESETS))
def test_preset_has_expected_color_and_movflags(name: str) -> None:
    preset = PRESETS[name]
    assert preset.color in {"bt709", "bt2020"}
    assert "+faststart" in preset.movflags


def test_archival_preset_is_hevc_with_hvc1_tag() -> None:
    preset = PRESETS["archival-h265"]
    assert preset.codec == "hevc"
    assert preset.tag == "hvc1"
