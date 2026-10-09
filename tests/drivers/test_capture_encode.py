from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eks_harness.capture.encode import main, read_timed_frames, resample

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")


def make_frames(folder: Path, stamps: list[float]) -> Path:
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, stamp in enumerate(stamps):
        file = folder / f"f{index:03d}.jpg"
        Image.new("RGB", (322, 240), (index * 40 % 255, 80, 160)).save(file, quality=90)
        rows.append(f"{stamp:.4f}\t{file}")
    listing = folder / "frames.tsv"
    listing.write_text("\n".join(reversed(rows)) + "\n")
    return listing


def test_resample_holds_the_latest_frame_on_a_30fps_grid(tmp_path: Path) -> None:
    listing = make_frames(tmp_path / "frames", [10.0, 10.05, 10.5, 11.0])
    rows = read_timed_frames(listing)
    assert [r[0] for r in rows] == [10.0, 10.05, 10.5, 11.0]
    out = tmp_path / "seq"
    out.mkdir()
    count, extension = resample(rows, out)
    assert count == 31 and extension == ".jpg"
    assert (out / "0000002.jpg").stat().st_ino == rows[1][1].stat().st_ino
    assert (out / "0000015.jpg").stat().st_ino == rows[2][1].stat().st_ino


@needs_ffmpeg
def test_timed_frames_encode_into_a_verified_trimmed_mp4(tmp_path: Path, capsys) -> None:
    listing = make_frames(tmp_path / "frames", [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    steps = tmp_path / "steps.jsonl"
    steps.write_text(json.dumps({"t": 1.0, "action": "click"}) + "\n" + json.dumps({"t": 5.5, "action": "fill"}) + "\n")
    out = tmp_path / "clip.mp4"
    assert main(["--timed-frames", str(listing), "--out", str(out), "--trim", "--step-log", str(steps),
                 "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["full"]).is_file() and Path(result["trimmed"]).is_file()
    assert any(line.startswith("full: 6.0s 322x240") for line in result["report"])
    assert main(["--timed-frames", str(tmp_path / "missing.tsv"), "--out", str(out)]) == 1
