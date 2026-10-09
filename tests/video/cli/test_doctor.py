from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from eks_harness.video.cli import doctor

_FFMPEG_VERSION_STDOUT = "ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers\n"
_FFMPEG_ENCODERS_STDOUT = """\
Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V..... h264_nvenc           NVIDIA NVENC H.264 encoder
 V..... h264_qsv             H.264 / AVC (Intel Quick Sync Video)
 V..... libsvtav1            SVT-AV1 (Scalable Video Technology for AV1)
 A..... aac                  AAC (Advanced Audio Coding)
"""


def _make_fake_run(
    version_text: str = _FFMPEG_VERSION_STDOUT,
    encoders_text: str = _FFMPEG_ENCODERS_STDOUT,
):
    def _fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        args = list(cmd)
        if "-encoders" in args:
            return subprocess.CompletedProcess(cmd, 0, encoders_text, "")
        return subprocess.CompletedProcess(cmd, 0, version_text, "")

    return _fake_run


def test_doctor_exits_zero_when_ffmpeg_and_libx264_present(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.subprocess, "run", _make_fake_run())

    exit_code = doctor.run_doctor("ffmpeg")
    captured = capsys.readouterr()

    assert exit_code == 0, captured.out
    assert "ffmpeg" in captured.out
    assert "libx264" in captured.out
    assert "MISSING" in captured.out or "OK" in captured.out


def test_doctor_exits_one_when_ffmpeg_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.subprocess, "run", _make_fake_run())

    exit_code = doctor.run_doctor("ffmpeg")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ffmpeg" in captured.out
    assert "MISSING" in captured.out


def test_doctor_exits_one_when_libx264_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    encoders_without_libx264 = _FFMPEG_ENCODERS_STDOUT.replace(
        " V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10\n",
        "",
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _make_fake_run(encoders_text=encoders_without_libx264),
    )

    exit_code = doctor.run_doctor("ffmpeg")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "libx264" in captured.out


def test_dep_row_marks_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "eks_doctor_nonexistent_module_xyz"
    if sentinel in sys.modules:
        del sys.modules[sentinel]

    row = doctor._dep_row(sentinel)
    assert row.status == "MISSING"
    assert "not installed" in row.detail


def test_dep_row_reports_version_for_installed_module() -> None:
    row = doctor._dep_row("pydantic")
    assert row.status == "OK"
    assert row.detail != "(version unknown)"


def test_list_ffmpeg_encoders_parses_known_names() -> None:
    def fake_run(cmd, **_):
        return subprocess.CompletedProcess(cmd, 0, _FFMPEG_ENCODERS_STDOUT, "")

    import eks_harness.video.cli.doctor as doctor_mod

    original = doctor_mod.subprocess.run
    doctor_mod.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        encoders = doctor_mod._list_ffmpeg_encoders("/usr/bin/ffmpeg")
    finally:
        doctor_mod.subprocess.run = original  # type: ignore[assignment]

    assert "libx264" in encoders
    assert "h264_nvenc" in encoders
    assert "libsvtav1" in encoders
