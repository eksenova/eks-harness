"""Tests for ``inspect_project`` tool."""

from __future__ import annotations

from pathlib import Path

from eks_harness.video.ir import (
    BeatTracker,
    Blur,
    FilmGrain,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.video.ir.animated import Animated
from eks_harness.studio.toolimpl.inspect_project import _run as run_inspect


def _write_project(directory: Path) -> Path:
    project = Project(
        fps=30,
        resolution=(1080, 1920),
        duration=4.0,
        random_seed=42,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="assets/hero.png"),
                        out=Seconds(t=4.0),
                        effects=[
                            Blur(radius=Animated[float](root=2.0)),
                            FilmGrain(intensity=Animated[float](root=0.5)),
                        ],
                    )
                ],
            )
        ],
        markers=[BeatTracker(name="beats", source="audio_tracks[0]")],
    )
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "project.json"
    json_path.write_text(
        project.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    return json_path


def test_inspect_project_returns_timeline_summary(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_inspect(json_path, roots=roots)
    summary = result["timeline_summary"]
    assert summary["fps"] == 30.0
    assert summary["resolution"] == [1080, 1920]
    assert summary["track_count"] == 1
    assert summary["total_effect_count"] == 2
    assert summary["tracks"][0]["segment_count"] == 1


def test_inspect_project_reports_compile_split(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_inspect(json_path, roots=roots)
    split = result["compile_split"]
    by_kind = {entry["kind"]: entry for entry in split}
    assert by_kind["blur"]["selected_target"] == "ffmpeg_graph"
    assert by_kind["film_grain"]["selected_target"] == "frame_pipeline"


def test_inspect_project_exposes_marker_sources_and_seed(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_inspect(json_path, roots=roots)
    assert result["random_seed"] == 42
    marker_kinds = {entry["kind"] for entry in result["marker_sources"]}
    assert "beat_tracker" in marker_kinds
    beat_entry = next(e for e in result["marker_sources"] if e["kind"] == "beat_tracker")
    assert set(beat_entry["streams"]) >= {"beat"}
