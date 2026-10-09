"""Camera shake effect via deterministic pseudo-random translation.

Each frame is offset by ``(dx, dy)`` sampled from a seeded RNG and scaled by
``intensity``. ``freq_hz`` controls how often a fresh random target is drawn
(samples in between are interpolated via the per-frame seeded draw, which
produces a coherent low-frequency wobble). ``decay`` linearly attenuates the
intensity from 1 -> 0 across the segment when > 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import Shake
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["ShakePlugin"]


class _ShakeProcessor(FrameProcessor):
    def __init__(
        self,
        intensity: np.ndarray | float,
        freq_hz: float,
        decay: float,
        seed: int,
        fps: float,
    ) -> None:
        self._intensity = intensity
        self._freq_hz = max(0.1, freq_hz)
        self._decay = max(0.0, decay)
        self._seed = seed
        self._fps = max(1.0, fps)

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        intensity = _sample(self._intensity, frame_idx)
        if self._decay > 0.0:
            intensity *= max(0.0, 1.0 - self._decay * t)
        if intensity <= 1e-4:
            return frame
        height, width = frame.shape[:2]
        max_px = max(1, int(0.05 * min(height, width) * intensity))
        bucket = int(t * self._freq_hz)
        mixed = (int(self._seed) ^ (bucket * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFFFFFF
        rng = np.random.default_rng(np.uint64(mixed))
        dx = int(rng.integers(-max_px, max_px + 1))
        dy = int(rng.integers(-max_px, max_px + 1))
        if dx == 0 and dy == 0:
            return frame
        return np.roll(frame, shift=(dy, dx), axis=(0, 1))


class ShakePlugin(Effect):
    name: ClassVar[str] = "shake"
    model: ClassVar[type] = Shake
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: Shake, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        intensity = resolve_animated(ir.intensity, ctx.project, ctx.markers)
        return _ShakeProcessor(
            intensity if isinstance(intensity, np.ndarray) else float(intensity),
            ir.freq_hz,
            ir.decay,
            seed=0,
            fps=float(ctx.project.fps),
        )


def _sample(value: np.ndarray | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
