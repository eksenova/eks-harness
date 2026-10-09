"""Tests for ``add_segment`` tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.studio.toolimpl.add_segment import _run as run_add_segment


def _write_project(directory: Path, *, with_track: bool = True) -> Path:
    tracks: list[Track] = []
    if with_track:
        tracks.append(
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="assets/hero.png"),
                        out=Seconds(t=2.0),
                    )
                ],
            )
        )
    project = Project(
        fps=30, resolution=(1080, 1920), duration=10.0, tracks=tracks
    )
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "project.json"
    json_path.write_text(
        project.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    return json_path


def _new_segment_dict(seg_id: str, start_s: float, out_s: float) -> dict:
    return {
        "id": seg_id,
        "start": {"kind": "seconds", "t": start_s},
        "in": {"kind": "seconds", "t": 0.0},
        "out": {"kind": "seconds", "t": out_s},
        "media": {"kind": "image_file", "path": "assets/next.png"},
    }


def test_add_segment_appends_to_existing_track(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_add_segment(
        project_path=json_path,
        track_name="main",
        segment_json=_new_segment_dict("next", 2.0, 4.0),
        create_track=False,
        roots=roots,
    )
    assert result["ok"] is True
    assert result["segment_id"] == "next"
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    seg_ids = [s["id"] for s in raw["tracks"][0]["segments"]]
    assert seg_ids == ["hero", "next"]


def test_add_segment_creates_track_when_requested(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_add_segment(
        project_path=json_path,
        track_name="overlay",
        segment_json=_new_segment_dict("badge", 0.0, 2.0),
        create_track=True,
        roots=roots,
    )
    assert result["ok"] is True
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    track_names = [t["name"] for t in raw["tracks"]]
    assert "overlay" in track_names


def test_add_segment_refuses_missing_track_without_flag(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    with pytest.raises(ValueError, match="not found"):
        run_add_segment(
            project_path=json_path,
            track_name="overlay",
            segment_json=_new_segment_dict("badge", 0.0, 2.0),
            create_track=False,
            roots=roots,
        )


def test_add_segment_rejects_invalid_segment(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        run_add_segment(
            project_path=json_path,
            track_name="main",
            segment_json={"id": "bad"},  # missing required fields
            create_track=False,
            roots=roots,
        )


def test_add_segment_refuses_project_py(workspace_root: Path, roots) -> None:
    project_dir = workspace_root / "proj"
    project_dir.mkdir()
    (project_dir / "project.py").write_text("project = None\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"project\.json"):
        run_add_segment(
            project_path=project_dir / "project.py",
            track_name="main",
            segment_json=_new_segment_dict("x", 0.0, 1.0),
            create_track=True,
            roots=roots,
        )
