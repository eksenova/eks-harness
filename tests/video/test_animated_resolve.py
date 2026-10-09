from __future__ import annotations

import math

import numpy as np
import pytest

from eks_harness.video import (
    Animated,
    BeatPulse,
    BeatRef,
    BeatTracker,
    CubicBezier,
    EaseInOut,
    ExpDecayEnv,
    ImageFile,
    Keyframe,
    LinearEasing,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.compile.markers import extract_markers


def _stub_project(duration: float = 4.0, fps: float = 60, bpm: float = 120.0) -> Project:
    return Project(
        fps=fps,
        resolution=(64, 64),
        duration=duration,
        markers=[BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"], bpm=bpm)],
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="poster.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=duration),
                    )
                ],
            )
        ],
    )


def test_resolve_constant_returns_scalar() -> None:
    project = _stub_project()
    markers = extract_markers(project)
    out = resolve_animated(Animated[float](root=0.5), project, markers)
    assert out == 0.5


def test_resolve_linear_keyframes() -> None:
    project = _stub_project(duration=2.0, fps=60)
    markers = extract_markers(project)
    anim = Animated[float](
        root=[
            Keyframe[float](t=Seconds(t=0.0), v=0.0, easing=LinearEasing()),
            Keyframe[float](t=Seconds(t=2.0), v=1.0),
        ]
    )
    out = resolve_animated(anim, project, markers)
    assert isinstance(out, np.ndarray)
    assert out.shape == (120,)
    assert out[0] == pytest.approx(0.0)
    assert out[-1] == pytest.approx(1.0, abs=1.0 / project.fps + 1e-6)
    assert out[60] == pytest.approx(0.5, abs=1e-2)


def test_resolve_cubic_bezier_keyframes_monotone() -> None:
    project = _stub_project(duration=1.0, fps=60)
    markers = extract_markers(project)
    bezier = CubicBezier(p1x=0.42, p1y=0.0, p2x=0.58, p2y=1.0)
    anim = Animated[float](
        root=[
            Keyframe[float](t=Seconds(t=0.0), v=0.0, easing=bezier),
            Keyframe[float](t=Seconds(t=1.0), v=1.0),
        ]
    )
    out = resolve_animated(anim, project, markers)
    assert isinstance(out, np.ndarray)
    assert np.all(np.diff(out) >= -1e-6)
    assert out[0] == pytest.approx(0.0)
    assert out[-1] == pytest.approx(1.0, abs=2.0 / project.fps)


def test_beat_pulse_canary_120_bpm() -> None:
    project = _stub_project(duration=4.0, fps=60, bpm=120.0)
    markers = extract_markers(project)
    pulse = BeatPulse(
        trigger=BeatRef(stream="kick", range=(Seconds(t=0.0), Seconds(t=4.0))),
        envelope=ExpDecayEnv(tau=0.05),
        intensity_ramp=[
            Keyframe[float](t=Seconds(t=0.0), v=0.1, easing=EaseInOut()),
            Keyframe[float](t=Seconds(t=4.0), v=0.9),
        ],
        baseline=0.0,
    )
    anim = Animated[float](root=pulse)
    out = resolve_animated(anim, project, markers)

    assert isinstance(out, np.ndarray)
    n_frames = int(round(project.duration * project.fps))
    assert out.shape == (n_frames,)

    fps = project.fps
    period_s = 60.0 / 120.0
    expected_trigger_frames = [int(round(i * period_s * fps)) for i in range(int(project.duration / period_s) + 1)]

    for tf in expected_trigger_frames:
        if tf >= n_frames:
            continue
        window = out[max(0, tf - 1) : min(n_frames, tf + 3)]
        assert window.max() > 0.05, f"expected pulse near frame {tf}, max={window.max()}"

    valid_triggers = [tf for tf in expected_trigger_frames if tf < n_frames]
    intensity_at_first_trigger = float(out[valid_triggers[0]])
    intensity_at_last_trigger = float(out[valid_triggers[-1]])
    assert intensity_at_last_trigger > intensity_at_first_trigger, (
        f"ramp expected to rise: first={intensity_at_first_trigger}, "
        f"last={intensity_at_last_trigger}"
    )

    decay_window = out[valid_triggers[1] : valid_triggers[1] + 6]
    assert decay_window[-1] < decay_window[0]


def test_lambda_curve() -> None:
    from eks_harness.video import Lambda

    project = _stub_project(duration=1.0, fps=10)
    markers = extract_markers(project)
    out = resolve_animated(Animated[float](root=Lambda(expr="t * 2")), project, markers)
    assert isinstance(out, np.ndarray)
    assert out[0] == pytest.approx(0.0)
    assert out[5] == pytest.approx(1.0, abs=1e-6)


def test_sine_curve() -> None:
    from eks_harness.video import Sine

    project = _stub_project(duration=1.0, fps=100)
    markers = extract_markers(project)
    out = resolve_animated(Animated[float](root=Sine(freq_hz=1.0)), project, markers)
    assert isinstance(out, np.ndarray)
    assert out[0] == pytest.approx(0.0, abs=1e-6)
    assert out[25] == pytest.approx(1.0, abs=1e-2)
    assert out[50] == pytest.approx(0.0, abs=1e-2)
    assert out[75] == pytest.approx(-1.0, abs=1e-2)
    assert math.isfinite(float(out.sum()))
