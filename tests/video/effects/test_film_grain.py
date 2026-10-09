"""Film grain effect tests."""

from __future__ import annotations

import numpy as np
from eks_harness.video.ir import Animated, EffectAdapter
from eks_harness.video.ir.effects import FilmGrain
from eks_harness.video.plugins.builtin.effects.film_grain import FilmGrainPlugin
from eks_harness.video.render.context import RenderContext


def test_film_grain_round_trip() -> None:
    effect = FilmGrain(intensity=Animated[float](root=8.0), seed=42)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, FilmGrain)
    assert rebuilt.intensity.root == 8.0
    assert rebuilt.seed == 42


def test_film_grain_zero_intensity_passes_through(render_ctx: RenderContext) -> None:
    effect = FilmGrain(intensity=Animated[float](root=0.0), seed=1)
    processor = FilmGrainPlugin().open(effect, render_ctx)
    frame = np.full((8, 8, 3), 128, dtype=np.uint8)
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert np.array_equal(out, frame)


def test_film_grain_is_deterministic_with_seed(render_ctx: RenderContext) -> None:
    effect = FilmGrain(intensity=Animated[float](root=10.0), seed=7)
    processor_a = FilmGrainPlugin().open(effect, render_ctx)
    processor_b = FilmGrainPlugin().open(effect, render_ctx)
    frame = np.full((8, 8, 3), 128, dtype=np.uint8)
    out_a = processor_a.process(frame, t=0.0, frame_idx=3)
    out_b = processor_b.process(frame, t=0.0, frame_idx=3)
    assert np.array_equal(out_a, out_b)
    assert not np.array_equal(out_a, frame)
