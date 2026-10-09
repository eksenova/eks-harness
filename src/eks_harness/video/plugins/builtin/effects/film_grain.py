"""Film grain effect via deterministic per-frame Gaussian noise.

A fresh seeded RNG is built per frame from ``seed XOR frame_idx`` so the
grain pattern is stable between renders but evolves across the segment.
``intensity`` is the standard deviation of the noise in 0..255 units.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import FilmGrain
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["FilmGrainPlugin"]


_SEED_MIX = 0x9E3779B97F4A7C15


class _FilmGrainProcessor(FrameProcessor):
    def __init__(self, intensity: np.ndarray | float, seed: int) -> None:
        self._intensity = intensity
        self._seed = seed

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        intensity = _sample(self._intensity, frame_idx)
        if intensity <= 1e-4:
            return frame
        mixed = (int(self._seed) ^ (frame_idx * _SEED_MIX)) & 0xFFFFFFFFFFFFFFFF
        rng = np.random.default_rng(np.uint64(mixed))
        noise = rng.standard_normal(frame.shape).astype(np.float32) * float(intensity)
        scaled = frame.astype(np.float32) + noise
        np.clip(scaled, 0.0, 255.0, out=scaled)
        return scaled.astype(np.uint8)


class FilmGrainPlugin(Effect):
    name: ClassVar[str] = "film_grain"
    model: ClassVar[type] = FilmGrain
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: FilmGrain, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        intensity = resolve_animated(ir.intensity, ctx.project, ctx.markers)
        seed = ir.seed if ir.seed is not None else 0
        return _FilmGrainProcessor(
            intensity if isinstance(intensity, np.ndarray) else float(intensity),
            seed,
        )


def _sample(value: np.ndarray | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
