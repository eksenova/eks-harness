"""Cheap datamosh approximation.

True datamosh requires rewriting AVI containers to drop I-frames so motion
vectors from the previous GOP smear over new frames; that is deferred. This
plugin instead blends each frame with the previous frame weighted by
``intensity`` and, every ~30 frames, replicates the previous frame outright
to mimic the "skip" artifact. Deterministic given ``seed``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import Datamosh
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["DatamoshPlugin"]


class _DatamoshProcessor(FrameProcessor):
    parallel_safe: ClassVar[bool] = False

    def __init__(self, intensity: np.ndarray | float, seed: int) -> None:
        self._intensity = intensity
        self._seed = seed
        self._previous: np.ndarray | None = None

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        intensity = _clamp01(_sample(self._intensity, frame_idx))
        if self._previous is None or intensity <= 1e-4:
            self._previous = frame.copy()
            return frame

        rng = np.random.default_rng(np.uint64(self._seed + frame_idx))
        if frame_idx % 30 == 0 and rng.random() < intensity:
            replicate = self._previous.copy()
            self._previous = frame.copy()
            return replicate

        blended = (
            frame.astype(np.float32) * (1.0 - intensity)
            + self._previous.astype(np.float32) * intensity
        )
        np.clip(blended, 0.0, 255.0, out=blended)
        out = blended.astype(np.uint8)
        self._previous = out.copy()
        return out


class DatamoshPlugin(Effect):
    name: ClassVar[str] = "datamosh"
    model: ClassVar[type] = Datamosh
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: Datamosh, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        intensity = resolve_animated(ir.intensity, ctx.project, ctx.markers)
        return _DatamoshProcessor(
            intensity if isinstance(intensity, np.ndarray) else float(intensity),
            seed=0,
        )


def _sample(value: np.ndarray | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))
