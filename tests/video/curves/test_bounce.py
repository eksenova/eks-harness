from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError
from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.curves import Bounce, Curve

from .conftest import empty_markers, make_project


def test_bounce_roundtrip() -> None:
    adapter = TypeAdapter(Curve)
    parsed = adapter.validate_python(
        {"kind": "bounce", "amplitude": 2.0, "period": 0.5, "decay": 1.5}
    )
    assert isinstance(parsed, Bounce)
    assert parsed.amplitude == 2.0


def test_bounce_is_non_negative_and_starts_at_zero() -> None:
    project = make_project(duration=2.0, fps=60.0)
    out = resolve_animated(
        Animated[float](root=Bounce(amplitude=1.0, period=0.4, decay=1.0)),
        project,
        empty_markers(),
    )
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == (120,)
    assert out[0] == pytest.approx(0.0)
    assert float(out.min()) >= 0.0


def test_bounce_envelope_decays() -> None:
    project = make_project(duration=4.0, fps=60.0)
    period = 0.4
    out = resolve_animated(
        Animated[float](root=Bounce(amplitude=1.0, period=period, decay=2.0)),
        project,
        empty_markers(),
    )
    # Peaks occur at quarter-period offsets; compare an early peak to a late one.
    fps = 60.0
    quarter = round(period / 4.0 * fps)
    first_peak = float(out[quarter])
    late_start = round(2.0 * fps)
    late_peak_window = out[late_start : late_start + quarter + 1]
    assert first_peak > float(late_peak_window.max())


def test_bounce_period_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Bounce(amplitude=1.0, period=0.0)


def test_bounce_zero_decay_keeps_amplitude() -> None:
    project = make_project(duration=1.0, fps=100.0)
    out = resolve_animated(
        Animated[float](root=Bounce(amplitude=1.0, period=0.5, decay=0.0)),
        project,
        empty_markers(),
    )
    # With decay=0 the envelope is just |sin(2*pi*t/period)|, max == 1.
    assert float(out.max()) == pytest.approx(1.0, abs=1e-2)
    # Sanity check the closest-frame sample to a quarter-period peak.
    fps = 100.0
    sample_frame = round(0.125 * fps)
    expected = abs(math.sin(2.0 * math.pi * (sample_frame / fps) / 0.5))
    assert out[sample_frame] == pytest.approx(expected, abs=1e-9)
