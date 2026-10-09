from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VALID_PROJECT = """\
from eks_harness.video import (
    Animated,
    Brightness,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Track,
)

project = Project(
    fps=60,
    resolution=(64, 64),
    duration=2.0,
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="hero",
                    start=Seconds(t=0.0),
                    media=ImageFile(path="poster.png"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=2.0),
                    effects=[Brightness(amount=Animated[float](root=0.4))],
                )
            ],
        )
    ],
)
"""

BROKEN_PROJECT = """\
from eks_harness.video import Project

project = Project(fps=-1, resolution=(64, 64), duration=2.0)
"""

NO_PROJECT_VAR = """\
print('hi')
"""


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "eks_harness.video.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_validate_ok(tmp_path: Path) -> None:
    project_py = tmp_path / "project.py"
    project_py.write_text(VALID_PROJECT, encoding="utf-8")
    result = _run_cli(["validate", str(project_py)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_validate_broken(tmp_path: Path) -> None:
    project_py = tmp_path / "broken.py"
    project_py.write_text(BROKEN_PROJECT, encoding="utf-8")
    result = _run_cli(["validate", str(project_py)])
    assert result.returncode == 1
    assert result.stderr.strip() != ""


def test_validate_missing_project_var(tmp_path: Path) -> None:
    project_py = tmp_path / "noproj.py"
    project_py.write_text(NO_PROJECT_VAR, encoding="utf-8")
    result = _run_cli(["validate", str(project_py)])
    assert result.returncode == 1


def test_schema_export_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "schema.json"
    result = _run_cli(["schema", "--out", str(out)])
    assert result.returncode == 0, result.stderr
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "$defs" in payload


def test_sync_writes_project_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "edit"
    project_dir.mkdir()
    (project_dir / "project.py").write_text(VALID_PROJECT, encoding="utf-8")
    result = _run_cli(["sync", str(project_dir)])
    assert result.returncode == 0, result.stderr
    snapshot = project_dir / "project.json"
    assert snapshot.exists()
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["fps"] == 60


def test_unknown_subcommand_fails() -> None:
    result = _run_cli(["bogus"])
    assert result.returncode != 0


@pytest.fixture
def fake_module() -> None:
    return None
