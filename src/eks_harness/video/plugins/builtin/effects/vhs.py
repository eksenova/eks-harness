"""VHS-style degradation effect.

Adds three artifacts in order: scanline darkening (every other row dimmed),
chroma blur in YCrCb space (chroma planes only -> classic NTSC bleed), and
luminance noise. ``cv2`` is used when available for a fast Gaussian blur,
otherwise a pure-numpy box blur stands in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import VHS
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["VHSPlugin"]


class _VHSProcessor(FrameProcessor):
    def __init__(self, scanlines: bool, chroma_blur: float, noise: float, seed: int) -> None:
        self._scanlines = scanlines
        self._chroma_blur = max(0.0, chroma_blur)
        self._noise = max(0.0, noise)
        self._seed = seed
        self._cv2 = _try_import_cv2()

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        out = frame.astype(np.float32)
        if self._chroma_blur > 0.0:
            out = self._apply_chroma_blur(out)
        if self._scanlines:
            out[::2] *= 0.85
        if self._noise > 0.0:
            rng = np.random.default_rng(np.uint64(self._seed + frame_idx))
            grain = rng.standard_normal(out.shape[:2]).astype(np.float32) * (255.0 * self._noise)
            out += grain[..., None]
        np.clip(out, 0.0, 255.0, out=out)
        return out.astype(np.uint8)

    def _apply_chroma_blur(self, frame_f32: np.ndarray) -> np.ndarray:
        if self._cv2 is not None:
            ycrcb = self._cv2.cvtColor(frame_f32.astype(np.uint8), self._cv2.COLOR_BGR2YCrCb)
            ksize = max(1, int(self._chroma_blur) * 2 + 1)
            ycrcb[..., 1] = self._cv2.GaussianBlur(ycrcb[..., 1], (ksize, 1), 0)
            ycrcb[..., 2] = self._cv2.GaussianBlur(ycrcb[..., 2], (ksize, 1), 0)
            blurred = self._cv2.cvtColor(ycrcb, self._cv2.COLOR_YCrCb2BGR)
            blurred_f32: np.ndarray = blurred.astype(np.float32)
            return blurred_f32
        # Pure-numpy fallback: horizontal box blur on R and B channels.
        kernel_size = max(1, int(self._chroma_blur) * 2 + 1)
        kernel = np.ones(kernel_size, dtype=np.float32) / kernel_size
        out = frame_f32.copy()
        for ch in (0, 2):
            out[..., ch] = np.apply_along_axis(
                lambda row: np.convolve(row, kernel, mode="same"), 1, frame_f32[..., ch]
            )
        return out


class VHSPlugin(Effect):
    name: ClassVar[str] = "vhs"
    model: ClassVar[type] = VHS
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: VHS, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        return _VHSProcessor(ir.scanlines, ir.chroma_blur, ir.noise, seed=0)


def _try_import_cv2() -> Any | None:
    try:
        import cv2  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:
        return None
    return cv2
