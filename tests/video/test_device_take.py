"""DeviceTake: cue resolution, beacons and frame-exact retiming (no live device needed)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from eks_harness.video.plugins.builtin.media.device_take import (
    BEACON_HUES,
    hue_color as _hue_color,
    beacon_frames,
    resolve_cue,
    retime,
    synced_marks,
    warp_points,
)

FPS_OUT = 24
SIZE = 32


def _recording(path: Path) -> Path:
    """30 fps, 4 s: white until 1.0 s, red until 3.0 s, then blue."""

    src = (f"color=white:s={SIZE}x{SIZE}:r=30:d=1[a];color=red:s={SIZE}x{SIZE}:r=30:d=2[b];"
           f"color=blue:s={SIZE}x{SIZE}:r=30:d=1[c];[a][b][c]concat=n=3:v=1:a=0[v]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-filter_complex", src, "-map", "[v]", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    return path


def _colors(path: Path) -> list[str]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, SIZE, SIZE, 3)
    names = {"white": (255, 255, 255), "red": (255, 0, 0), "blue": (0, 0, 255)}
    out = []
    for f in frames:
        px = f[SIZE // 2, SIZE // 2].astype(int)
        out.append(min(names, key=lambda n: sum(abs(px - np.array(names[n])))))
    return out


def test_cue_targets_resolve_from_markers() -> None:
    markers = {"streams": {"beat": [0.0, 0.44, 0.88]}, "named": {"drop": [5.0, 9.0]}}
    assert resolve_cue(1.25, markers) == 1.25
    assert resolve_cue("beat:2", markers) == 0.88
    assert resolve_cue("named:drop:1", markers) == 9.0
    with pytest.raises(ValueError):
        resolve_cue("beat:7", markers)


def test_cues_out_of_order_are_rejected() -> None:
    with pytest.raises(ValueError):
        warp_points({"a": 2.0, "b": 1.0}, {"a": 0.5, "b": 1.5})


def test_retime_lands_cues_on_exact_frames(tmp_path: Path) -> None:
    rec = _recording(tmp_path / "rec.mp4")
    points = warp_points({"red": 1.0, "blue": 3.0}, {"red": 0.5, "blue": 2.5})
    out = retime(rec, tmp_path / "out.mov", points, fps=FPS_OUT, frames=3 * FPS_OUT)
    colors = _colors(out)
    assert len(colors) == 3 * FPS_OUT
    assert colors.index("red") == round(0.5 * FPS_OUT)
    assert colors.index("blue") == round(2.5 * FPS_OUT)


def test_retime_stretches_between_cues_and_holds_the_ends(tmp_path: Path) -> None:
    rec = _recording(tmp_path / "rec.mp4")
    points = warp_points({"red": 1.0, "blue": 3.0}, {"red": 2.0, "blue": 3.0})
    out = retime(rec, tmp_path / "out.mov", points, fps=FPS_OUT, frames=5 * FPS_OUT)
    colors = _colors(out)
    assert colors.index("red") == 2 * FPS_OUT
    assert colors.index("blue") == 3 * FPS_OUT
    assert colors[-1] == "blue" and colors[0] == "white"


def _beacon_recording(path: Path, schedule: dict[int, int], frames: int, dim: dict[int, float]) -> Path:
    """A 200x300 grey "status bar" recording with a 30 px beacon at (3, 3) whose hue changes on the given frames."""

    w, h = 200, 300
    out = np.full((frames, h, w, 3), 236, np.uint8)
    color = None
    for i in range(frames):
        if i in schedule:
            hexa = _hue_color(BEACON_HUES[schedule[i] % len(BEACON_HUES)]).lstrip("#")
            color = np.array([int(hexa[k:k + 2], 16) for k in (0, 2, 4)], np.float32)
        if color is not None:
            out[i, 3:33, 3:33] = (color * dim.get(i, 1.0)).astype(np.uint8)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", "30",
                    "-i", "-", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", str(path)],
                   input=out.tobytes(), check=True)
    return path


def test_beacon_frames_leave_an_unseen_cue_to_the_wall_clock(tmp_path: Path) -> None:
    # cues 0..7; cue 2's hue is never drawn (cue 3 replaced it within one frame); a modal dims cue 6's beacon
    schedule = {10: 0, 25: 1, 40: 3, 55: 4, 70: 5, 85: 6, 100: 7}
    video = _beacon_recording(tmp_path / "b.mp4", schedule, 120, {i: 0.55 for i in range(85, 100)})
    frames, box = beacon_frames(video, 8)
    assert frames == [10, 25, None, 40, 55, 70, 85, 100]
    order = [f"c{i}" for i in range(8)]
    wall = {"c0": 0.3, "c1": 0.8, "c2": 1.31, "c3": 1.34, "c4": 1.8, "c5": 2.3, "c6": 2.8, "c7": 3.3}
    marks = synced_marks(order, wall, frames, 30.0)
    assert marks["c1"] < marks["c2"] < marks["c3"]
    targets = {name: 1.0 + i for i, name in enumerate(order)}
    assert [r for _, r in warp_points(marks, targets)] == sorted(marks.values())
    assert box is not None and box[0] <= 4 and box[1] <= 4 and 28 <= box[2] <= 32 and 28 <= box[3] <= 32


def test_synced_marks_fall_back_to_the_wall_clock_offset_of_the_nearest_beacon() -> None:
    order = ["a", "b", "c"]
    wall = {"a": 1.0, "b": 2.0, "c": 5.0}
    marks = synced_marks(order, wall, [30, None, 120], 30.0)
    assert marks == {"a": 1.0, "b": 2.0, "c": 4.0}


def test_retime_is_frame_exact_with_tiny_spans(tmp_path: Path) -> None:
    source = tmp_path / "ramp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "nullsrc=s=64x64:r=30:d=10,geq=lum='16+mod(N*2\\,200)':cb=128:cr=128", "-c:v", "libx264",
                    "-crf", "0", "-pix_fmt", "yuv420p", "-g", "30", str(source)], check=True)
    points = [(0.3, 0.6), (0.58, 1.4), (1.9, 4.2), (1.991, 4.9), (3.12, 6.1), (3.15, 6.12), (4.6, 8.0), (4.62, 9.9),
              (6.0, 9.95)]
    out = retime(source, tmp_path / "out.mov", points, fps=30.0, frames=210)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 64, 64)
    assert len(frames) == 210
    for target, recorded in points:
        level = float(frames[round(target * 30)].mean()) * 219 / 255 / 2
        assert abs(level - (round(recorded * 30) % 100)) <= 1.5, (target, recorded, level)
