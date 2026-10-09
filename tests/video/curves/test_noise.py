from __future__ import annotations

import numpy as np
from pydantic import TypeAdapter
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.curves import Curve, Noise

from .conftest import empty_markers, make_project


def test_noise_roundtrip() -> None:
    adapter = TypeAdapter(Curve)
    parsed = adapter.validate_python(
        {"kind": "noise", "seed": 42, "min_value": -1.0, "max_value": 1.0, "hold": 0.25}
    )
    assert isinstance(parsed, Noise)
    assert parsed.seed == 42
    assert parsed.hold == 0.25


def test_noise_no_hold_in_range() -> None:
    project = make_project(duration=1.0, fps=30.0)
    out = resolve_animated(
        Animated[float](root=Noise(seed=7, min_value=0.0, max_value=1.0)),
        project,
        empty_markers(),
    )
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == (30,)
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


def test_noise_hold_keeps_samples_constant_for_hold_window() -> None:
    project = make_project(duration=1.0, fps=30.0)
    out = resolve_animated(
        Animated[float](root=Noise(seed=11, hold=0.25)),
        project,
        empty_markers(),
    )
    hold_frames = round(0.25 * 30.0)
    assert np.all(out[:hold_frames] == out[0])
    assert np.all(out[hold_frames : 2 * hold_frames] == out[hold_frames])


def test_noise_is_deterministic_for_same_seed() -> None:
    project = make_project(duration=1.0, fps=30.0)
    curve = Noise(seed=99)
    out_a = resolve_animated(Animated[float](root=curve), project, empty_markers())
    out_b = resolve_animated(Animated[float](root=curve), project, empty_markers())
    assert np.array_equal(out_a, out_b)


def test_noise_project_seed_changes_output() -> None:
    curve = Noise(seed=1)
    project_a = make_project(random_seed=None)
    project_b = make_project(random_seed=12345)
    out_a = resolve_animated(Animated[float](root=curve), project_a, empty_markers())
    out_b = resolve_animated(Animated[float](root=curve), project_b, empty_markers())
    assert not np.array_equal(out_a, out_b)
