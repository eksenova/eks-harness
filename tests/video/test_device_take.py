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


def test_beacon_frames_find_each_cue_even_when_two_share_a_frame(tmp_path: Path) -> None:
    # cues 0..7; cues 2 and 3 land in the same frame (only 3's hue is ever drawn); a modal dims cue 6's beacon
    schedule = {10: 0, 25: 1, 40: 3, 55: 4, 70: 5, 85: 6, 100: 7}
    video = _beacon_recording(tmp_path / "b.mp4", schedule, 120, {i: 0.55 for i in range(85, 100)})
    frames, box = beacon_frames(video, 8)
    assert frames == [10, 25, 40, 40, 55, 70, 85, 100]
    assert box is not None and box[0] <= 4 and box[1] <= 4 and 28 <= box[2] <= 32 and 28 <= box[3] <= 32


def test_synced_marks_fall_back_to_the_wall_clock_offset_of_the_nearest_beacon() -> None:
    order = ["a", "b", "c"]
    wall = {"a": 1.0, "b": 2.0, "c": 5.0}
    marks = synced_marks(order, wall, [30, None, 120], 30.0)
    assert marks == {"a": 1.0, "b": 2.0, "c": 4.0}
