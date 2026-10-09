from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eks_harness.api.routes_captures import step_times
from eks_harness.capture.trim import TrimConfig, expected_duration, plan
from eks_harness.store.encode import encode_recording

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg and ffprobe are needed")


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def typing_clip(path: Path, seconds: float = 12.0) -> Path:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x808080:d={seconds}:s=320x240:r=30",
        "-vf", "drawbox=x='mod(t*60,300)':y=100:w=4:h=8:color=black:t=fill",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_step_times_keeps_only_real_offsets() -> None:
    steps = [{"t": 1.5}, {"t": 2}, {"t": True}, {"t": -1}, {"t": "3"}, {"x": 4}, "5", {"t": 6.25}]
    assert step_times(steps) == [1.5, 2.0, 6.25]
    assert step_times(None) == []
    assert step_times({"t": 1}) == []


@needs_ffmpeg
def test_device_trim_keeps_real_time_around_steps(tmp_path: Path) -> None:
    source = typing_clip(tmp_path / "raw.mp4")
    result = encode_recording(source, tmp_path / "out.mp4", trim=True, events=[6.0])
    assert result.trimmed is not None
    full = probe_duration(result.full)
    want = expected_duration(plan(full, [6.0], TrimConfig()))
    assert abs(probe_duration(result.trimmed) - want) < 0.3


@needs_ffmpeg
def test_device_trim_without_steps_falls_back_to_the_pixel_trim(tmp_path: Path) -> None:
    source = typing_clip(tmp_path / "raw.mp4")
    with_steps = encode_recording(source, tmp_path / "steps.mp4", trim=True, events=[6.0])
    pixel = encode_recording(source, tmp_path / "pixel.mp4", trim=True, events=[99.0])
    assert pixel.trimmed is not None and with_steps.trimmed is not None
    assert probe_duration(pixel.trimmed) != pytest.approx(probe_duration(with_steps.trimmed), abs=0.3)
