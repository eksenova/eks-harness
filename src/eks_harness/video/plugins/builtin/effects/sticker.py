"""Sticker overlay - PIL paste with alpha onto a BGR frame.

Supports animated ``x``, ``y``, ``scale`` and ``rotation``. The sticker
image is loaded once and re-transformed per frame; for animations whose
parameters never change between frames the orchestrator can amortize this
by resolving constants at open time (no transform call when scale=1,
rotation=0).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.ir.effects import StickerOverlay
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["StickerOverlayPlugin"]


class _StickerProcessor(FrameProcessor):
    def __init__(
        self,
        path: str,
        x: np.ndarray | int,
        y: np.ndarray | int,
        scale: np.ndarray | float,
        rotation: np.ndarray | float,
    ) -> None:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - explicit user guidance
            raise ImportError(
                "sticker requires Pillow; install it with `pip install Pillow`"
            ) from exc

        self._Image = Image
        self._source = Image.open(path).convert("RGBA")
        self._x = x
        self._y = y
        self._scale = scale
        self._rotation = rotation

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        scale = max(1e-3, _sample_float(self._scale, frame_idx))
        rotation = _sample_float(self._rotation, frame_idx)
        x = round(_sample_float(self._x, frame_idx))
        y = round(_sample_float(self._y, frame_idx))

        sticker = self._source
        if abs(scale - 1.0) > 1e-3:
            new_size = (max(1, int(sticker.width * scale)), max(1, int(sticker.height * scale)))
            sticker = sticker.resize(new_size, self._Image.LANCZOS)
        if abs(rotation) > 1e-3:
            sticker = sticker.rotate(rotation, resample=self._Image.BICUBIC, expand=True)

        rgb = frame[..., ::-1]
        pil_frame = self._Image.fromarray(np.ascontiguousarray(rgb), mode="RGB").convert("RGBA")
        pil_frame.alpha_composite(sticker, dest=(x, y))
        rgb_out = np.asarray(pil_frame.convert("RGB"))
        return np.ascontiguousarray(rgb_out[..., ::-1])


class StickerOverlayPlugin(Effect):
    name: ClassVar[str] = "sticker"
    model: ClassVar[type] = StickerOverlay
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: StickerOverlay, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        x = resolve_animated(ir.x, ctx.project, ctx.markers)
        y = resolve_animated(ir.y, ctx.project, ctx.markers)
        scale = resolve_animated(ir.scale, ctx.project, ctx.markers)
        rotation = resolve_animated(ir.rotation, ctx.project, ctx.markers)
        return _StickerProcessor(
            str(ir.path),
            x if isinstance(x, np.ndarray) else int(x),
            y if isinstance(y, np.ndarray) else int(y),
            scale if isinstance(scale, np.ndarray) else float(scale),
            rotation if isinstance(rotation, np.ndarray) else float(rotation),
        )


def _sample_float(value: np.ndarray | int | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)
