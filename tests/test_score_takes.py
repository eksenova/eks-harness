from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from eks_harness.score import Score, beats, plan
from eks_harness.score.takes import TimeWarp, drive, observed_events, retime


def test_time_warp_maps_both_ways_and_extrapolates() -> None:
    warp = TimeWarp.from_anchors([(0.0, 1.0), (2.0, 3.5), (4.0, 5.0), (3.0, 2.0)])
    assert warp.anchors == ((0.0, 1.0), (2.0, 3.5), (4.0, 5.0))
    assert warp.video_at(1.0) == pytest.approx(2.25)
    assert warp.video_at(-1.0) == pytest.approx(0.0)
    assert warp.video_at(5.0) == pytest.approx(6.0)
    for s in (0.0, 0.7, 2.0, 3.3, 6.0):
        assert warp.score_at(warp.video_at(s)) == pytest.approx(s)


class FakeSession:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.calls: list[tuple[float, str, dict]] = []

    def act(self, verb: str, **args) -> dict:
        self.calls.append((self.clock[0], verb, args))
        self.clock[0] += 0.05
        if verb == "fill":
            raise RuntimeError("no such field")
        return {"ok": True}


def test_drive_fires_actions_on_the_score_clock_and_collects_events() -> None:
    s = Score(fps=30, bpm=120, duration=4.0)
    app = s.device("ios", id="app")
    s.on(beats("downbeat")[1], app.press("#start"), id="start")
    s.on(3.0, app.fill("#name", "Ada"), id="fill")
    current = plan(s.build())
    clock = [100.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    session = FakeSession(clock)
    events = [{"name": "result-shown", "data": {"route": "/done"}}]
    take = drive(session, current.for_track("app"), span_start=0.0, clock_zero=101.0, recording_started=100.5,
                 events=None, sleep=sleep, now=lambda: clock[0])
    assert [(round(t, 2), v) for t, v, _ in session.calls] == [(103.0, "press"), (104.0, "fill")]
    assert [a.ok for a in take.fired] == [True, False] and "no such field" in take.fired[1].error
    anchors = take.anchors(0.0)
    assert anchors == [(2.0, pytest.approx(2.55))]
    warp = TimeWarp.from_anchors([(0.0, 0.5), *anchors])
    take.observed.append((104.2, events[0]))
    found = observed_events(take, warp, track_id="app", span_start=0.0)
    assert found[0].name == "result-shown" and found[0].source == "app"
    assert found[0].time == pytest.approx(warp.score_at(3.7))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_retime_resamples_frames_through_the_warp(tmp_path: Path) -> None:
    source = tmp_path / "raw.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:r=30:d=4",
                    "-vf", "drawbox=x=0:y=0:w=32:h=32:color=white:t=fill:enable='gte(t,2)'",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    warp = TimeWarp.from_anchors([(0.0, 0.5), (1.0, 2.0)])
    out = retime(source, tmp_path / "take.mp4", fps=30, duration=2.0, warp=warp)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 32, 32)
    assert frames.shape[0] == 60
    lum = frames[:, 16, 16]
    first_white = int(np.argmax(lum > 128))
    assert abs(first_white - 30) <= 1
