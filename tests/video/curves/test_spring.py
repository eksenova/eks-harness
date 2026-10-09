from __future__ import annotations

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.curves import Curve, Spring

from .conftest import empty_markers, make_project


def test_spring_roundtrip() -> None:
    adapter = TypeAdapter(Curve)
    payload = {"kind": "spring", "target": 1.0, "stiffness": 80.0, "damping": 12.0, "initial": 0.0}
    parsed = adapter.validate_python(payload)
    assert isinstance(parsed, Spring)
    assert parsed.target == 1.0
    assert parsed.stiffness == 80.0


def test_spring_settles_toward_target() -> None:
    project = make_project(duration=3.0, fps=120.0)
    spring = Spring(target=1.0, stiffness=80.0, damping=12.0, initial=0.0)
    out = resolve_animated(Animated[float](root=spring), project, empty_markers())

    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == (360,)
    assert out[0] == pytest.approx(0.0)
    assert out[-1] == pytest.approx(1.0, abs=5e-2)


def test_spring_stiffness_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Spring(target=1.0, stiffness=0.0)
