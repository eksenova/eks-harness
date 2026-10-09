"""Brightness effect plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Brightness
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor
from eks_harness.video.render.ffmpeg_builder import FilterChain, eq

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class _BrightnessProcessor(FrameProcessor):
    def __init__(self, per_frame: np.ndarray | float) -> None:
        self._per_frame = per_frame

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        amount = self._sample(frame_idx)
        if amount == 0.0:
            return frame
        scaled = frame.astype(np.int16) + round(amount * 255.0)
        np.clip(scaled, 0, 255, out=scaled)
        return scaled.astype(np.uint8)

    def _sample(self, frame_idx: int) -> float:
        if isinstance(self._per_frame, np.ndarray):
            idx = max(0, min(frame_idx, len(self._per_frame) - 1))
            return float(self._per_frame[idx])
        return float(self._per_frame)


class BrightnessPlugin(Effect):
    name: ClassVar[str] = "brightness"
    model: ClassVar[type] = Brightness
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset(
        {"ffmpeg_graph", "frame_pipeline"}
    )

    def compile_graph(self, ir: Brightness, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        expr = animated_to_ffmpeg_expr(ir.amount, ctx.project, ctx.markers)
        if expr is None:
            raise ValueError("brightness: amount could not be lowered to ffmpeg expression")
        chain = FilterChain()
        chain.add(eq(brightness=expr))
        return chain

    def open(self, ir: Brightness, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        cached = ctx.extra.get(_resolved_key(ir))
        if isinstance(cached, np.ndarray):
            return _BrightnessProcessor(cached)
        if isinstance(cached, (int, float)):
            return _BrightnessProcessor(float(cached))
        resolved = resolve_animated(ir.amount, ctx.project, ctx.markers)
        if isinstance(resolved, np.ndarray):
            return _BrightnessProcessor(resolved)
        return _BrightnessProcessor(float(resolved))


def _resolved_key(ir: Brightness) -> str:
    return f"brightness:{id(ir)}"
