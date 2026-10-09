"""Tests for ``extract_markers`` tool.

We rely on the stub beat extractor in ``eks_harness.video.compile.markers`` so the
test does not need madmom or any other ML backend installed.
"""

from __future__ import annotations

from pathlib import Path

from eks_harness.video.ir import (
    BeatTracker,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.studio.toolimpl.extract_markers import _run as run_extract


def _write_project(directory: Path) -> Path:
    project = Project(
        fps=30,
        resolution=(1080, 1920),
        duration=4.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="assets/hero.png"),
                        out=Seconds(t=4.0),
                    )
                ],
            )
        ],
        markers=[
            BeatTracker(name="beats", source="audio_tracks[0]", bpm=120.0)
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "project.json"
    json_path.write_text(
        project.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    return json_path


def test_extract_markers_returns_stub_beat_grid(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_extract(json_path, roots=roots)
    assert "beat" in result["streams"]
    beat_times = result["streams"]["beat"]
    assert len(beat_times) > 0
    # 120 BPM over 4s = beats every 0.5s, so we expect about 9 markers including t=0 and t=4
    assert beat_times[0] == 0.0
    assert beat_times[-1] <= 4.0 + 1e-6
    assert "beats" in result["named"]


def test_extract_markers_returns_empty_when_no_sources(workspace_root: Path, roots) -> None:
    project = Project(fps=30, resolution=(1080, 1920), duration=4.0)
    project_dir = workspace_root / "no_markers"
    project_dir.mkdir()
    json_path = project_dir / "project.json"
    json_path.write_text(
        project.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )

    result = run_extract(json_path, roots=roots)
    assert result["streams"] == {}
    assert result["named"] == {}
    assert result["warnings"] == []
