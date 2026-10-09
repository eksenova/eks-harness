"""Tests for ``add_effect`` tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.studio.toolimpl.add_effect import _run as run_add_effect


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
    )
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "project.json"
    json_path.write_text(
        project.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    return json_path


def test_add_effect_appends_to_segment(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    result = run_add_effect(
        project_path=json_path,
        segment_id="hero",
        effect_json={"kind": "blur", "radius": 2.5},
        roots=roots,
    )
    assert result["ok"] is True
    assert result["effect_kind"] == "blur"
    assert result["track_name"] == "main"
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    effects = raw["tracks"][0]["segments"][0]["effects"]
    assert len(effects) == 1
    assert effects[0]["kind"] == "blur"


def test_add_effect_rejects_unknown_segment(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    with pytest.raises(ValueError, match="not found"):
        run_add_effect(
            project_path=json_path,
            segment_id="missing",
            effect_json={"kind": "blur", "radius": 1.0},
            roots=roots,
        )


def test_add_effect_rejects_unknown_kind(workspace_root: Path, roots) -> None:
    json_path = _write_project(workspace_root / "proj")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        run_add_effect(
            project_path=json_path,
            segment_id="hero",
            effect_json={"kind": "not_a_real_effect", "x": 1},
            roots=roots,
        )
