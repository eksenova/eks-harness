"""Fade effect plugin (in/out)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import Fade
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class _FadeProcessor(FrameProcessor):
    def __init__(self, direction: str, duration: float, fps: float, total_frames: int) -> None:
        self._direction = direction
        self._duration = duration
        self._fps = fps
        self._total = total_frames

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        alpha = self._alpha(frame_idx)
        if alpha >= 0.999:
            return frame
        if alpha <= 0.001:
            return np.zeros_like(frame)
        scaled = (frame.astype(np.float32) * alpha).astype(np.uint8)
        return scaled

    def _alpha(self, frame_idx: int) -> float:
        fade_frames = max(1, round(self._duration * self._fps))
        if self._direction == "in":
            return min(1.0, frame_idx / fade_frames)
        end_window_start = max(0, self._total - fade_frames)
        if frame_idx < end_window_start:
            return 1.0
        remaining = self._total - frame_idx
        return max(0.0, remaining / fade_frames)


class FadePlugin(Effect):
    name: ClassVar[str] = "fade"
    model: ClassVar[type] = Fade
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset(
        {"ffmpeg_graph", "frame_pipeline"}
    )

    def compile_graph(self, ir: Fade, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        chain = FilterChain()
        start = 0.0 if ir.direction == "in" else max(0.0, _segment_duration(ctx, ir) - ir.duration)
        chain.add(
            FilterNode(
                name="fade",
                params={"t": ir.direction, "st": f"{start:.6f}", "d": f"{ir.duration:.6f}"},
            )
        )
        return chain

    def open(self, ir: Fade, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        duration = _segment_duration(ctx, ir)
        total_frames = max(1, round(duration * ctx.project.fps))
        return _FadeProcessor(ir.direction, ir.duration, ctx.project.fps, total_frames)


def _segment_duration(ctx: RenderContext, ir: Fade) -> float:
    duration = ctx.extra.get("segment_duration_seconds")
    if isinstance(duration, (int, float)):
        return float(duration)
    return float(ir.duration)
