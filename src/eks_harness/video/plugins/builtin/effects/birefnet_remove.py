"""Per-frame BiRefNet matting via ``rembg``.

Uses the ``birefnet-portrait`` ONNX model from ``rembg``. Per-frame with no
temporal model - on video segments the alpha mask can flicker across frames.
The plugin emits a one-shot warning when it sees a non-first-frame call to
make the trade-off explicit.

Install with ``pip install eks-harness[matting]``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import BiRefNetRemove
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["BiRefNetRemovePlugin"]

_LOG = logging.getLogger(__name__)


def _import_rembg_or_raise() -> Any:
    try:
        import rembg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "BiRefNetRemove requires `pip install eks-harness[matting]` "
            "(rembg + onnxruntime)."
        ) from exc
    return rembg


class _BiRefNetProcessor(FrameProcessor):
    parallel_safe: ClassVar[bool] = False

    def __init__(self, background: tuple[int, int, int, int]) -> None:
        self._background = background
        self._rembg: Any = None
        self._session: Any = None
        self._warned_video = False

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        if frame_idx > 0 and not self._warned_video:
            _LOG.warning(
                "BiRefNetRemove is per-frame; on a video segment the alpha "
                "mask may flicker across frames. Use MatAnyoneRemove for "
                "temporally-consistent video matting."
            )
            self._warned_video = True
        if self._rembg is None or self._session is None:
            return frame

        rgb = frame[..., [2, 1, 0]]  # BGR -> RGB
        rgba = self._rembg.remove(rgb, session=self._session)
        return _composite_bgr(rgba, frame, self._background)


class BiRefNetRemovePlugin(Effect):
    name: ClassVar[str] = "birefnet_remove"
    model: ClassVar[type] = BiRefNetRemove
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: BiRefNetRemove, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        rembg = _import_rembg_or_raise()
        from eks_harness.video.render.winml_ep import install_autoep_session_hook

        proc = _BiRefNetProcessor(ir.background)
        proc._rembg = rembg
        # rembg builds its own ONNX session; the hook routes it onto the AMD
        # GPU (MIGraphX) when available, with clean CPU fallback otherwise.
        install_autoep_session_hook()
        proc._session = rembg.new_session("birefnet-portrait")
        return proc


def _composite_bgr(
    rgba: np.ndarray,
    src_bgr: np.ndarray,
    background: tuple[int, int, int, int],
) -> np.ndarray:
    """Alpha-blend the rembg RGBA output over ``background`` and return BGR."""

    h, w = src_bgr.shape[:2]
    alpha = rgba[..., 3].astype(np.float32) / 255.0
    fg_rgb = rgba[..., :3].astype(np.float32)
    bg_rgb = np.array(background[:3], dtype=np.float32)
    out_rgb = fg_rgb * alpha[..., None] + bg_rgb * (1.0 - alpha[..., None])
    out_rgb_u8 = np.clip(out_rgb, 0, 255).astype(np.uint8)
    out_bgr = out_rgb_u8[..., [2, 1, 0]]
    return out_bgr.reshape(h, w, 3)
