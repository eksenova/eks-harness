"""RGB channel-split effect (frame-pipeline only).

Shifts the red channel left and blue channel right by ``offset_x`` pixels
(and analogously by ``offset_y`` vertically), producing the chromatic
aberration look used by glitch / VHS aesthetics. The green channel stays
put so luminance reads cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import RGBSplit
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["RGBSplitPlugin"]


class _RGBSplitProcessor(FrameProcessor):
    def __init__(self, offset_x: np.ndarray | int, offset_y: np.ndarray | int) -> None:
        self._offset_x = offset_x
        self._offset_y = offset_y

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        ox = round(_sample(self._offset_x, frame_idx))
        oy = round(_sample(self._offset_y, frame_idx))
        if ox == 0 and oy == 0:
            return frame
        out = frame.copy()
        # Frames are BGR (channels 0=B, 1=G, 2=R); shift R left and B right.
        out[..., 2] = np.roll(frame[..., 2], shift=(-oy, -ox), axis=(0, 1))
        out[..., 0] = np.roll(frame[..., 0], shift=(oy, ox), axis=(0, 1))
        return out


class RGBSplitPlugin(Effect):
    name: ClassVar[str] = "rgb_split"
    model: ClassVar[type] = RGBSplit
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: RGBSplit, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        ox = resolve_animated(ir.offset_x, ctx.project, ctx.markers)
        oy = resolve_animated(ir.offset_y, ctx.project, ctx.markers)
        return _RGBSplitProcessor(ox if isinstance(ox, np.ndarray) else int(ox),
                                  oy if isinstance(oy, np.ndarray) else int(oy))


def _sample(value: np.ndarray | int, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
