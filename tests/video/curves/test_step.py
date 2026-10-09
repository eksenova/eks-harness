from __future__ import annotations

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.curves import Curve, Step

from .conftest import empty_markers, make_project


def test_step_roundtrip() -> None:
    adapter = TypeAdapter(Curve)
    parsed = adapter.validate_python(
        {"kind": "step", "values": [0.0, 1.0, 2.0], "hold": 0.5}
    )
    assert isinstance(parsed, Step)
    assert parsed.hold == 0.5


def test_step_round_robins_values() -> None:
    project = make_project(duration=3.0, fps=10.0)
    out = resolve_animated(
        Animated[float](root=Step(values=[10.0, 20.0, 30.0], hold=1.0)),
        project,
        empty_markers(),
    )
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == (30,)
    assert np.all(out[0:10] == 10.0)
    assert np.all(out[10:20] == 20.0)
    assert np.all(out[20:30] == 30.0)


def test_step_wraps_around_after_full_cycle() -> None:
    project = make_project(duration=2.0, fps=10.0)
    out = resolve_animated(
        Animated[float](root=Step(values=[1.0, 2.0], hold=0.5)),
        project,
        empty_markers(),
    )
    expected = np.array([1.0] * 5 + [2.0] * 5 + [1.0] * 5 + [2.0] * 5)
    assert np.array_equal(out, expected)


def test_step_hold_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Step(values=[1.0], hold=0.0)
