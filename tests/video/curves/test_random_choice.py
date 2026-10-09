from __future__ import annotations

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.curves import Curve, RandomChoice

from .conftest import empty_markers, make_project


def test_random_choice_roundtrip() -> None:
    adapter = TypeAdapter(Curve)
    parsed = adapter.validate_python(
        {"kind": "random_choice", "seed": 3, "values": [0.0, 0.5, 1.0], "hold": 0.5}
    )
    assert isinstance(parsed, RandomChoice)
    assert parsed.values == [0.0, 0.5, 1.0]


def test_random_choice_only_yields_allowed_values() -> None:
    project = make_project(duration=2.0, fps=30.0)
    values = [-1.0, 0.25, 0.75]
    out = resolve_animated(
        Animated[float](root=RandomChoice(seed=4, values=values, hold=0.1)),
        project,
        empty_markers(),
    )
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == (60,)
    assert set(np.unique(out).tolist()).issubset(set(values))


def test_random_choice_holds_for_hold_window() -> None:
    project = make_project(duration=2.0, fps=30.0)
    out = resolve_animated(
        Animated[float](root=RandomChoice(seed=5, values=[0.0, 1.0], hold=0.5)),
        project,
        empty_markers(),
    )
    hold_frames = round(0.5 * 30.0)
    assert np.all(out[:hold_frames] == out[0])


def test_random_choice_is_deterministic_for_same_seed() -> None:
    project = make_project(duration=2.0, fps=30.0)
    curve = RandomChoice(seed=17, values=[1.0, 2.0, 3.0], hold=0.2)
    out_a = resolve_animated(Animated[float](root=curve), project, empty_markers())
    out_b = resolve_animated(Animated[float](root=curve), project, empty_markers())
    assert np.array_equal(out_a, out_b)


def test_random_choice_requires_non_empty_values() -> None:
    with pytest.raises(ValidationError):
        RandomChoice(seed=1, values=[], hold=0.1)
