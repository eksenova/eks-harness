"""``render_driver._apply_quality_profile`` tests.

The driver receives custom encoding/resolution overrides as a JSON blob and
applies them to the loaded project before rendering. Most overrides map onto
``project.render_settings``; ``resolution`` is special - it lives on the
``Project`` itself, so the driver must route it there instead of silently
dropping it onto ``render_settings``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from eks_harness.studio.render_driver import _apply_quality_profile
from eks_harness.studio.toolimpl import render_project as render_tool


def _write_fake_project(workspace: Path, name: str) -> Path:
    project_dir = workspace / name
    (project_dir / "renders").mkdir(parents=True)
    (project_dir / "project.py").write_text("project = None\n", encoding="utf-8")
    return project_dir


def test_resolve_render_plan_threads_resolution(workspace_root: Path, roots) -> None:
    _write_fake_project(workspace_root, "demo")

    plan = render_tool._resolve_render_plan(
        project_path=Path("demo"),
        mode="final",
        encoder=None,
        bitrate_kbps=None,
        crf=None,
        width=720,
        height=1280,
        roots=roots,
    )

    assert plan.settings_overrides["resolution"] == (720, 1280)


def test_resolve_render_plan_ignores_partial_resolution(
    workspace_root: Path, roots
) -> None:
    _write_fake_project(workspace_root, "demo")

    plan = render_tool._resolve_render_plan(
        project_path=Path("demo"),
        mode="final",
        encoder=None,
        bitrate_kbps=None,
        crf=None,
        width=720,
        height=None,
        roots=roots,
    )

    # A lone width (or height) is ambiguous - both are required to override.
    assert "resolution" not in plan.settings_overrides


def _fake_project(resolution: tuple[int, int]) -> SimpleNamespace:
    settings = SimpleNamespace(quality_profile="final")
    return SimpleNamespace(resolution=resolution, render_settings=settings)


def test_resolution_override_applied_to_project_in_final_mode() -> None:
    project = _fake_project((1080, 1920))

    _apply_quality_profile(
        project, mode="final", overrides={"resolution": (720, 1280)}
    )

    assert project.resolution == (720, 1280)
    # The override must NOT leak onto render_settings (RenderSettings forbids
    # extras, so a leaked key would be silently dropped at render time).
    assert not hasattr(project.render_settings, "resolution")


def test_resolution_override_rounded_to_even() -> None:
    project = _fake_project((1080, 1920))

    _apply_quality_profile(
        project, mode="final", overrides={"resolution": (101, 203)}
    )

    # yuv420p mux requires even dimensions - round down to the nearest even.
    assert project.resolution == (100, 202)


def test_preview_cap_still_applies_to_custom_resolution() -> None:
    project = _fake_project((1080, 1920))

    _apply_quality_profile(
        project, mode="preview", overrides={"resolution": (1920, 1080)}
    )

    # Custom resolution sets the base; the preview long-axis cap (960) still
    # applies on top so preview renders stay fast. 1920 -> 960 (scale 0.5).
    assert project.resolution == (960, 540)


def test_other_overrides_still_reach_render_settings() -> None:
    project = _fake_project((1080, 1920))

    _apply_quality_profile(
        project,
        mode="final",
        overrides={"resolution": (720, 1280), "crf": 21, "encoder": "libx265"},
    )

    assert project.resolution == (720, 1280)
    assert project.render_settings.crf == 21
    assert project.render_settings.encoder == "libx265"
