from __future__ import annotations

from pathlib import Path

import pytest
from eks_harness.video.cli import init as init_mod
from eks_harness.video.cli.init import TEMPLATES, run_init


def test_list_mode_prints_all_templates(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = run_init(template_name=None, dest_dir=None, list_only=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    for template in TEMPLATES:
        assert template in captured.out
    assert len(TEMPLATES) == 8


def test_init_copies_template_to_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "edit"
    exit_code = run_init(template_name="beat_flash_montage", dest_dir=dest)
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    project_py = dest / "project.py"
    assert project_py.exists()
    assert project_py.read_text(encoding="utf-8").strip() != ""
    assert (dest / "assets" / ".gitkeep").exists()
    assert str(project_py) in captured.out


def test_init_refuses_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "edit"
    first = run_init(template_name="podcast_clip", dest_dir=dest)
    assert first == 0

    capsys.readouterr()
    second = run_init(template_name="podcast_clip", dest_dir=dest)
    captured = capsys.readouterr()

    assert second == 1
    assert "already exists" in captured.err


def test_init_overwrites_with_force(tmp_path: Path) -> None:
    dest = tmp_path / "edit"
    assert run_init(template_name="podcast_clip", dest_dir=dest) == 0

    project_py = dest / "project.py"
    project_py.write_text("# mutated\n", encoding="utf-8")

    assert (
        run_init(template_name="beat_flash_montage", dest_dir=dest, force=True) == 0
    )
    assert project_py.read_text(encoding="utf-8") != "# mutated\n"


def test_init_unknown_template_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run_init(template_name="not_a_real_template", dest_dir=tmp_path / "x")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unknown template" in captured.err
    assert "--list" in captured.err


def test_init_missing_args_without_list_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run_init(template_name=None, dest_dir=None)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "required" in captured.err


def test_init_reports_missing_cookbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(init_mod, "_resolve_cookbook_dir", lambda: None)
    exit_code = run_init(
        template_name="beat_flash_montage", dest_dir=tmp_path / "out"
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cookbook" in captured.err
