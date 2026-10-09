"""Flash effect plugin.

The graph path emits a single ``fade=t=in`` envelope at the trigger time
(or stitches many for a list of triggers); the frame-pipeline path samples
the resolved alpha curve per frame and composites ``color`` onto the frame.
The latter is the canary path for Phase 2 - :class:`BeatPulse` curves drive
the alpha array end-to-end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.effects import Flash
from eks_harness.video.ir.time import BeatRef, Frames, MarkerRef, Seconds, WordRef
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class _FlashProcessor(FrameProcessor):
    def __init__(self, alpha_per_frame: np.ndarray, color_bgr: tuple[int, int, int]) -> None:
        self._alpha = alpha_per_frame
        self._color = np.array(color_bgr, dtype=np.float32)

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        idx = max(0, min(frame_idx, len(self._alpha) - 1))
        alpha = float(self._alpha[idx])
        if alpha <= 1e-4:
            return frame
        alpha = min(1.0, max(0.0, alpha))
        blended = frame.astype(np.float32) * (1.0 - alpha) + self._color * alpha
        np.clip(blended, 0.0, 255.0, out=blended)
        return blended.astype(np.uint8)


class FlashPlugin(Effect):
    name: ClassVar[str] = "flash"
    model: ClassVar[type] = Flash
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset(
        {"ffmpeg_graph", "frame_pipeline"}
    )

    def prefers_frame_pipeline(self, ir: Flash, ctx: RenderContext) -> bool:  # type: ignore[override]
        return ir.curve is not None

    def compile_graph(self, ir: Flash, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        if ir.curve is not None:
            raise ValueError(
                "flash with a curve must run on the frame pipeline; the orchestrator should "
                "have routed it there"
            )
        triggers = _flash_triggers(ir, ctx)
        duration_expr = _duration_to_expr(ir.duration)
        chain = FilterChain()
        for trigger_t in triggers:
            chain.add(
                FilterNode(
                    name="fade",
                    params={
                        "t": "in",
                        "st": f"{trigger_t:.6f}",
                        "d": duration_expr,
                        "color": _color_hex(ir.color),
                    },
                )
            )
        return chain

    def open(self, ir: Flash, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        seg_duration = _segment_duration(ctx)
        alpha = _resolve_flash_alpha(ir, ctx, seg_duration)
        r, g, b = ir.color
        return _FlashProcessor(alpha, color_bgr=(b, g, r))


def _resolve_flash_alpha(ir: Flash, ctx: RenderContext, seg_duration: float) -> np.ndarray:
    fps = ctx.project.fps
    n_frames = max(1, round(seg_duration * fps))

    if ir.curve is not None:
        wrapped = Animated[float](root=ir.curve)
        resolved = resolve_animated(wrapped, ctx.project, ctx.markers, duration_seconds=seg_duration)
        if isinstance(resolved, np.ndarray):
            return resolved.astype(np.float32)
        return np.full(n_frames, float(resolved), dtype=np.float32)

    triggers = _flash_triggers(ir, ctx)
    duration = _scalar_duration(ir.duration)
    pulse_frames = max(1, round(duration * fps))
    out = np.zeros(n_frames, dtype=np.float32)
    for trigger_t in triggers:
        start_frame = max(0, round(trigger_t * fps))
        end_frame = min(n_frames, start_frame + pulse_frames)
        if end_frame <= start_frame:
            continue
        ramp = np.linspace(1.0, 0.0, end_frame - start_frame, dtype=np.float32, endpoint=False)
        out[start_frame:end_frame] = np.maximum(out[start_frame:end_frame], ramp)
    return out


def _flash_triggers(ir: Flash, ctx: RenderContext) -> list[float]:
    if isinstance(ir.at, Animated):
        resolved = ir.at.root
        if isinstance(resolved, list):
            return [float(resolve_time(kf.t, ctx.project, ctx.markers)) for kf in resolved]
        return [float(resolved)]
    if isinstance(ir.at, (Seconds, Frames, BeatRef, WordRef, MarkerRef)):
        return [float(resolve_time(ir.at, ctx.project, ctx.markers))]
    return [float(ir.at)]


def _scalar_duration(duration: Animated[float] | float) -> float:
    if isinstance(duration, Animated):
        root = duration.root
        if isinstance(root, (int, float)):
            return float(root)
        return 0.1
    return float(duration)


def _duration_to_expr(duration: Animated[float] | float) -> str:
    return f"{_scalar_duration(duration):.6f}"


def _color_hex(color: tuple[int, int, int]) -> str:
    r, g, b = color
    return f"0x{r:02x}{g:02x}{b:02x}"


def _segment_duration(ctx: RenderContext) -> float:
    duration = ctx.extra.get("segment_duration_seconds")
    if isinstance(duration, (int, float)):
        return float(duration)
    return float(ctx.project.duration)
