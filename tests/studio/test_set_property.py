"""Tests for ``set_property`` tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eks_harness.video.ir import Blur, ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.ir.animated import Animated
from eks_harness.studio.toolimpl.set_property import _parse_path
from eks_harness.studio.toolimpl.set_property import _run as run_set_property


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
                        effects=[Blur(radius=Animated[float](root=2.0))],
                    )
                ],
            )
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "project.json"
    json_path.write_text(
        project.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    return json_path


def test_parse_path_handles_dotted_and_indexed() -> None:
    assert _parse_path("tracks[0].segments[1].effects[0].amount") == [
        "tracks",
        0,
        "segments",
        1,
        "effects",
        0,
        "amount",
    ]
    assert _parse_path("metadata.author") == ["metadata", "author"]
    assert _parse_path("a[0][1]") == ["a", 0, 1]


def test_parse_path_rejects_whitespace_and_garbage() -> None:
    with pytest.raises(ValueError):
        _parse_path("a. b")
    with pytest.raises(ValueError):
        _parse_path(".a")
    with pytest.raises(ValueError):
        _parse_path("a..b")
    with pytest.raises(ValueError):
        _parse_path("a.")


def test_set_property_updates_scalar(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_set_property(
        project_path=json_path,
        path="tracks[0].segments[0].effects[0].radius",
        value=5.0,
        roots=roots,
    )
    assert result["ok"] is True
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    assert raw["tracks"][0]["segments"][0]["effects"][0]["radius"] == 5.0


def test_set_property_can_change_render_settings(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_set_property(
        project_path=json_path,
        path="render_settings.preset",
        value="reels-1080-h264",
        roots=roots,
    )
    assert result["ok"] is True
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    assert raw["render_settings"]["preset"] == "reels-1080-h264"


def test_set_property_rejects_invalid_value(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        run_set_property(
            project_path=json_path,
            path="fps",
            value=-5,
            roots=roots,
        )


def test_set_property_reports_unknown_path(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    with pytest.raises(KeyError):
        run_set_property(
            project_path=json_path,
            path="tracks[0].nonexistent.field",
            value=1,
            roots=roots,
        )


def test_set_property_appends_to_list_at_length_index(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    new_effect = {"kind": "invert"}
    result = run_set_property(
        project_path=json_path,
        path="tracks[0].segments[0].effects[1]",
        value=new_effect,
        roots=roots,
    )
    assert result["ok"] is True
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    effects = raw["tracks"][0]["segments"][0]["effects"]
    assert len(effects) == 2
    assert effects[1]["kind"] == "invert"
