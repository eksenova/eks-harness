from __future__ import annotations

import json

from eks_harness.video import schema as schema_module


def test_export_returns_dict_with_defs() -> None:
    s = schema_module.export()
    assert isinstance(s, dict)
    assert "$defs" in s
    json.dumps(s)


def test_effect_oneof_contains_all_builtins() -> None:
    s = schema_module.export()
    effect_def = s["$defs"]["Effect"]
    assert "oneOf" in effect_def
    titles = {entry.get("title") for entry in effect_def["oneOf"]}
    assert {"Brightness", "Flash", "Crop", "Fade"}.issubset(titles)


def test_easing_curve_media_marker_defs_present() -> None:
    s = schema_module.export()
    defs = s["$defs"]
    expected_titles = {
        "BeatPulse",
        "ExpDecay",
        "Sine",
        "LFO",
        "Lambda",
        "VideoFile",
        "ImageFile",
        "Solid",
        "GeneratedCard",
        "AudioFile",
        "TTSGenerated",
        "BeatTracker",
        "STTMarkers",
        "SceneMarkers",
        "Crossfade",
        "Cut",
        "PluginTransition",
        "LinearEasing",
        "EaseInOut",
        "CubicBezier",
        "PluginEasing",
    }
    titles = {entry.get("title") for entry in defs.values()}
    missing = expected_titles - titles
    assert not missing, f"missing schema defs: {missing}"
