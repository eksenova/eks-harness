"""Tests for ``validate_project`` tool."""

from __future__ import annotations

from pathlib import Path

from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.studio.toolimpl.validate_project import _run as run_validate


def _write_project_json(directory: Path) -> Path:
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


def test_validate_project_accepts_valid_json(workspace_root: Path, roots) -> None:
    json_path = _write_project_json(workspace_root / "proj")
    result = run_validate(json_path, roots=roots)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["source_kind"] == "json"


def test_validate_project_reports_pydantic_errors(workspace_root: Path, roots) -> None:
    project_dir = workspace_root / "broken"
    project_dir.mkdir()
    bad = project_dir / "project.json"
    bad.write_text(
        '{"schema_version":"0.1","fps":-1,"resolution":[1080,1920],"duration":4.0}',
        encoding="utf-8",
    )
    result = run_validate(bad, roots=roots)
    assert result["ok"] is False
    assert any("fps" in err["loc"] for err in result["errors"])


def test_validate_project_handles_missing_file(workspace_root: Path, roots) -> None:
    result = run_validate(workspace_root / "missing", roots=roots)
    assert result["ok"] is False
    assert result["errors"]
    assert result["errors"][0]["type"] == "file_not_found"


def test_validate_project_resolves_relative_to_workspace(workspace_root: Path, roots) -> None:
    _write_project_json(workspace_root / "proj")
    result = run_validate(Path("proj"), roots=roots)
    assert result["ok"] is True
