from __future__ import annotations

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.curves import Bezier, Curve

from .conftest import empty_markers, make_project


def test_bezier_roundtrip() -> None:
    adapter = TypeAdapter(Curve)
    parsed = adapter.validate_python(
        {"kind": "bezier", "p0": 0.0, "p1": 0.2, "p2": 0.8, "p3": 1.0, "duration": 1.0}
    )
    assert isinstance(parsed, Bezier)
    assert parsed.duration == 1.0


def test_bezier_hits_endpoints_and_holds_after_duration() -> None:
    project = make_project(duration=2.0, fps=60.0)
    out = resolve_animated(
        Animated[float](root=Bezier(p0=0.0, p1=0.0, p2=1.0, p3=1.0, duration=1.0)),
        project,
        empty_markers(),
    )
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == (120,)
    assert out[0] == pytest.approx(0.0)
    assert out[60] == pytest.approx(1.0, abs=1e-6)
    assert out[-1] == pytest.approx(1.0)


def test_bezier_linear_control_points_are_monotonic() -> None:
    project = make_project(duration=1.0, fps=60.0)
    out = resolve_animated(
        Animated[float](
            root=Bezier(p0=0.0, p1=1.0 / 3.0, p2=2.0 / 3.0, p3=1.0, duration=1.0)
        ),
        project,
        empty_markers(),
    )
    assert np.all(np.diff(out) >= -1e-9)
    assert out[0] == pytest.approx(0.0)


def test_bezier_duration_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Bezier(p0=0.0, p1=0.0, p2=1.0, p3=1.0, duration=0.0)
