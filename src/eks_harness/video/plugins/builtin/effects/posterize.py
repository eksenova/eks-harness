"""Posterize effect - quantize each channel to a fixed number of levels."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import Posterize
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["PosterizePlugin"]


class _PosterizeProcessor(FrameProcessor):
    def __init__(self, levels: np.ndarray | int) -> None:
        self._levels = levels

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        levels = max(2, round(_sample(self._levels, frame_idx)))
        step = 256 // levels
        if step <= 1:
            return frame
        out = (frame.astype(np.int16) // step) * step
        np.clip(out, 0, 255, out=out)
        return out.astype(np.uint8)


class PosterizePlugin(Effect):
    name: ClassVar[str] = "posterize"
    model: ClassVar[type] = Posterize
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: Posterize, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        levels = resolve_animated(ir.levels, ctx.project, ctx.markers)
        return _PosterizeProcessor(
            levels if isinstance(levels, np.ndarray) else int(levels),
        )


def _sample(value: np.ndarray | int, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
