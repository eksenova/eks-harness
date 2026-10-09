"""Watermark overlay plugin.

When ``x``, ``y``, ``opacity`` and ``scale`` are all constants the watermark
folds into ffmpeg's filter graph via ``movie`` + ``scale`` + ``overlay``.
Any animated parameter forces frame-pipeline execution with PIL for alpha
compositing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.effects import Watermark
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["WatermarkPlugin"]


class _WatermarkProcessor(FrameProcessor):
    def __init__(
        self,
        path: str,
        x: np.ndarray | int,
        y: np.ndarray | int,
        opacity: np.ndarray | float,
        scale: np.ndarray | float,
    ) -> None:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - explicit user guidance
            raise ImportError(
                "watermark requires Pillow; install it with `pip install Pillow`"
            ) from exc

        self._Image = Image
        self._source = Image.open(path).convert("RGBA")
        self._x = x
        self._y = y
        self._opacity = opacity
        self._scale = scale

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        scale = max(1e-3, _sample_float(self._scale, frame_idx))
        opacity = min(1.0, max(0.0, _sample_float(self._opacity, frame_idx)))
        x = round(_sample_float(self._x, frame_idx))
        y = round(_sample_float(self._y, frame_idx))
        if opacity <= 1e-4:
            return frame

        watermark = self._source
        if abs(scale - 1.0) > 1e-3:
            new_size = (max(1, int(watermark.width * scale)), max(1, int(watermark.height * scale)))
            watermark = watermark.resize(new_size, self._Image.LANCZOS)
        if opacity < 1.0:
            alpha = watermark.split()[-1].point(lambda value: int(value * opacity))
            watermark = watermark.copy()
            watermark.putalpha(alpha)

        rgb = frame[..., ::-1]
        pil_frame = self._Image.fromarray(np.ascontiguousarray(rgb), mode="RGB").convert("RGBA")
        pil_frame.alpha_composite(watermark, dest=(x, y))
        rgb_out = np.asarray(pil_frame.convert("RGB"))
        return np.ascontiguousarray(rgb_out[..., ::-1])


class WatermarkPlugin(Effect):
    name: ClassVar[str] = "watermark"
    model: ClassVar[type] = Watermark
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset(
        {"ffmpeg_graph", "frame_pipeline"}
    )

    def prefers_frame_pipeline(self, ir: Watermark, ctx: RenderContext) -> bool:  # type: ignore[override]
        return not _all_constant(ir)

    def compile_graph(self, ir: Watermark, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        if not _all_constant(ir):
            raise ValueError(
                "watermark with animated x/y/opacity/scale must run on the frame pipeline"
            )
        x_value = _constant_int(ir.x)
        y_value = _constant_int(ir.y)
        scale_value = _constant_float(ir.scale)
        opacity_value = _constant_float(ir.opacity)

        # See lut.py: forward-slash + colon-escape, then wrap in single quotes
        # and bypass _escape via raw_params so the backslash isn't doubled.
        movie_path = str(ir.path).replace("\\", "/").replace(":", r"\:")
        chain = FilterChain()
        chain.add(FilterNode(name="movie", raw_params={"filename": f"'{movie_path}'"}, outputs=["wm0"]))
        chain.add(
            FilterNode(
                name="scale",
                inputs=["wm0"],
                positional=[f"iw*{scale_value:.6f}", f"ih*{scale_value:.6f}"],
                outputs=["wm1"],
            )
        )
        chain.add(
            FilterNode(
                name="colorchannelmixer",
                inputs=["wm1"],
                params={"aa": f"{opacity_value:.6f}"},
                outputs=["wm"],
            )
        )
        chain.add(
            FilterNode(
                name="overlay",
                inputs=["main", "wm"],
                params={"x": x_value, "y": y_value},
            )
        )
        return chain

    def open(self, ir: Watermark, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        x = resolve_animated(ir.x, ctx.project, ctx.markers)
        y = resolve_animated(ir.y, ctx.project, ctx.markers)
        opacity = resolve_animated(ir.opacity, ctx.project, ctx.markers)
        scale = resolve_animated(ir.scale, ctx.project, ctx.markers)
        return _WatermarkProcessor(
            str(ir.path),
            x if isinstance(x, np.ndarray) else int(x),
            y if isinstance(y, np.ndarray) else int(y),
            opacity if isinstance(opacity, np.ndarray) else float(opacity),
            scale if isinstance(scale, np.ndarray) else float(scale),
        )


def _all_constant(ir: Watermark) -> bool:
    return all(_is_constant(value) for value in (ir.x, ir.y, ir.opacity, ir.scale))


def _is_constant(value: Animated[Any]) -> bool:
    root = value.root
    return isinstance(root, (int, float))


def _constant_int(value: Animated[int]) -> int:
    return int(value.root)


def _constant_float(value: Animated[float]) -> float:
    return float(value.root)


def _sample_float(value: np.ndarray | int | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
