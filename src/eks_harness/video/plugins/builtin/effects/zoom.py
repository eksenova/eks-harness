"""Zoom effect plugin.

Emits ``scale,crop`` to keep the output dimensions identical while
magnifying around an animated focal point ``(cx, cy)`` in normalised
[0, 1] coordinates. Mirrors the ``pan.py`` convention of expressing
everything as ffmpeg expressions over ``iw`` / ``ih`` so the orchestrator
never bakes in a hardcoded resolution.

If any of ``scale`` / ``cx`` / ``cy`` doesn't fold to an ffmpeg
expression, the orchestrator's compile-target machinery spills the
effect to the frame pipeline. A minimal frame-pipeline implementation is
provided here too so the spill path doesn't crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Zoom
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["ZoomPlugin"]


class _ZoomProcessor(FrameProcessor):
    def __init__(
        self,
        scale: np.ndarray | float,
        cx: np.ndarray | float,
        cy: np.ndarray | float,
    ) -> None:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - explicit guidance
            raise ImportError("zoom requires Pillow; install it with `pip install Pillow`") from exc
        self._Image = Image
        self._scale = scale
        self._cx = cx
        self._cy = cy

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        s = max(1.0, _sample(self._scale, frame_idx))
        cx = min(1.0, max(0.0, _sample(self._cx, frame_idx)))
        cy = min(1.0, max(0.0, _sample(self._cy, frame_idx)))
        h, w = frame.shape[:2]
        crop_w = max(1, int(round(w / s)))
        crop_h = max(1, int(round(h / s)))
        x0 = int(round((w - crop_w) * cx))
        y0 = int(round((h - crop_h) * cy))
        cropped = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
        rgb = cropped[..., ::-1]
        pil = self._Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")
        resized = pil.resize((w, h), self._Image.LANCZOS)
        return np.ascontiguousarray(np.asarray(resized)[..., ::-1])


class ZoomPlugin(Effect):
    name: ClassVar[str] = "zoom"
    model: ClassVar[type] = Zoom
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset(
        {"ffmpeg_graph", "frame_pipeline"}
    )

    def compile_graph(self, ir: Zoom, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        scale_expr = animated_to_ffmpeg_expr(ir.scale, ctx.project, ctx.markers)
        cx_expr = animated_to_ffmpeg_expr(ir.cx, ctx.project, ctx.markers)
        cy_expr = animated_to_ffmpeg_expr(ir.cy, ctx.project, ctx.markers)
        if scale_expr is None or cx_expr is None or cy_expr is None:
            raise ValueError("zoom: scale/cx/cy could not be lowered to ffmpeg expressions")
        chain = FilterChain()
        # crop a (iw/s)x(ih/s) window centred at (cx, cy) in [0,1] -> [0, iw-iw/s]
        chain.add(
            FilterNode(
                name="crop",
                positional=[
                    f"iw/({scale_expr})",
                    f"ih/({scale_expr})",
                    f"(iw-iw/({scale_expr}))*({cx_expr})",
                    f"(ih-ih/({scale_expr}))*({cy_expr})",
                ],
            )
        )
        # rescale back to the original frame size so the segment doesn't shrink
        chain.add(FilterNode(name="scale", positional=["iw*0+ih*0+in_w", "in_h"]))
        return chain

    def open(self, ir: Zoom, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        scale = resolve_animated(ir.scale, ctx.project, ctx.markers)
        cx = resolve_animated(ir.cx, ctx.project, ctx.markers)
        cy = resolve_animated(ir.cy, ctx.project, ctx.markers)
        return _ZoomProcessor(
            scale if isinstance(scale, np.ndarray) else float(scale),
            cx if isinstance(cx, np.ndarray) else float(cx),
            cy if isinstance(cy, np.ndarray) else float(cy),
        )


def _sample(value: np.ndarray | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
