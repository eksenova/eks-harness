"""Block-scramble glitch effect.

Splits the frame into ``block_size`` rows and randomly shifts each row by a
small horizontal offset proportional to ``intensity``. Roughly every fourth
block additionally swaps two channels, producing the colored tear common in
glitch art. Determinism: a fresh ``numpy.random.default_rng`` seeded with
``(seed, frame_idx)`` is used per frame so playback is reproducible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import Glitch
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["GlitchPlugin"]


class _GlitchProcessor(FrameProcessor):
    def __init__(self, intensity: np.ndarray | float, block_size: int, seed: int) -> None:
        self._intensity = intensity
        self._block_size = max(1, block_size)
        self._seed = seed

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        intensity = _sample(self._intensity, frame_idx)
        if intensity <= 1e-4:
            return frame
        rng = np.random.default_rng(np.uint64(self._seed + frame_idx))
        height, width = frame.shape[:2]
        max_shift = max(1, int(width * 0.1 * intensity))
        out = frame.copy()
        for top in range(0, height, self._block_size):
            bottom = min(height, top + self._block_size)
            shift = int(rng.integers(-max_shift, max_shift + 1))
            if shift != 0:
                out[top:bottom] = np.roll(out[top:bottom], shift, axis=1)
            if rng.random() < 0.25 * intensity:
                channels = list(rng.permutation(3))
                out[top:bottom] = out[top:bottom][..., channels]
        return out


class GlitchPlugin(Effect):
    name: ClassVar[str] = "glitch"
    model: ClassVar[type] = Glitch
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: Glitch, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        intensity = resolve_animated(ir.intensity, ctx.project, ctx.markers)
        seed = ir.seed if ir.seed is not None else 0
        return _GlitchProcessor(
            intensity if isinstance(intensity, np.ndarray) else float(intensity),
            ir.block_size,
            seed,
        )


def _sample(value: np.ndarray | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
