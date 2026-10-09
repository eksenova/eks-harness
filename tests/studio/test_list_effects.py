"""Tests for ``list_effects`` tool."""

from __future__ import annotations

from eks_harness.studio.toolimpl.list_effects import _run as run_list_effects


def test_list_effects_includes_builtins() -> None:
    result = run_list_effects()
    effects = result["effects"]
    names = {entry["name"] for entry in effects}
    kinds = {entry["kind"] for entry in effects}
    assert {"Blur", "Sharpen", "Vignette", "FilmGrain", "LUT", "Invert", "Posterize"}.issubset(names)
    assert {"blur", "film_grain", "lut", "invert", "posterize"}.issubset(kinds)


def test_list_effects_reports_compile_targets() -> None:
    result = run_list_effects()
    by_kind = {entry["kind"]: entry for entry in result["effects"]}
    assert by_kind["film_grain"]["compile_targets"] == ["frame_pipeline"]
    assert "ffmpeg_graph" in by_kind["blur"]["compile_targets"]


def test_list_effects_reports_fields() -> None:
    result = run_list_effects()
    by_kind = {entry["kind"]: entry for entry in result["effects"]}
    blur_fields = {f["name"] for f in by_kind["blur"]["fields"]}
    assert "radius" in blur_fields
    radius = next(f for f in by_kind["blur"]["fields"] if f["name"] == "radius")
    assert radius["required"] is True
