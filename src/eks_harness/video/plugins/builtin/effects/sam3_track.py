"""SAM 3 open-vocabulary segmentation via per-frame image inference.

Wraps Meta's SAM 3 image model (released 2025-11-19,
``facebookresearch/sam3``) behind the ``FrameProcessor`` contract.
Each frame is segmented independently with the user's concept ``prompt``;
SAM 3's separate temporal-tracking API requires file-based session
start (not compatible with one-frame-at-a-time pipelines) and is a
distinct integration on the roadmap.

Install:

    pip install --no-deps git+https://github.com/facebookresearch/sam3
    pip install timm einops iopath ftfy==6.1.1 hydra-core triton-windows pycocotools

First inference downloads model weights from HuggingFace
(``facebook/sam3`` - gated, needs ``hf auth login`` with approved access).

Output modes per :class:`eks_harness.video.ir.effects.SAM3Track`:

* ``mask`` - white where the concept is, black elsewhere.
* ``matte`` - source pixels inside the mask, zeroed outside.
* ``highlight`` - source pixels inside the mask, dimmed to 30% outside.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import SAM3Track
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["SAM3TrackPlugin"]

_LOG = logging.getLogger(__name__)
_HIGHLIGHT_DIM = 0.30


def _to_numpy(value: Any) -> Any:
    """Convert a torch tensor (possibly on GPU / bfloat16) to a numpy array.

    SAM 3 returns masks/scores as ``cuda:0`` tensors; numpy can't read GPU
    memory directly, and bfloat16 has no numpy dtype - so detach, move to
    CPU, and upcast to float32 before converting. Pass-through for values
    that are already numpy / lists / None.
    """

    if value is None:
        return None
    if hasattr(value, "detach") and hasattr(value, "cpu"):
        return value.detach().to("cpu", dtype=__import__("torch").float32).numpy()
    return value


def _import_sam3_or_raise() -> tuple[Any, Any]:
    try:
        from sam3.model_builder import build_sam3_image_model  # type: ignore[import-not-found]
        from sam3.model.sam3_image_processor import Sam3Processor  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "SAM3Track requires the SAM 3 install. Run: "
            "`pip install --no-deps git+https://github.com/facebookresearch/sam3` "
            "and its runtime deps (timm, iopath, ftfy==6.1.1, hydra-core, "
            "triton-windows, pycocotools). Weights download from HuggingFace; "
            "you need `hf auth login` with approved access to facebook/sam3."
        ) from exc
    return build_sam3_image_model, Sam3Processor


class _SAM3TrackProcessor(FrameProcessor):
    parallel_safe: ClassVar[bool] = False

    def __init__(
        self,
        processor: Any,
        pil_module: Any,
        prompt: str,
        output: str,
        confidence_threshold: float,
        torch_module: Any,
        device: Any,
    ) -> None:
        self._processor = processor
        self._Image = pil_module
        self._prompt = prompt
        self._output = output
        self._confidence_threshold = float(confidence_threshold)
        self._torch = torch_module
        self._device = device
        # SAM 3 weights load in bfloat16; the image transform produces a
        # float32 tensor. Inference must run under autocast so the dtypes
        # reconcile (upstream's decoder selectively disables autocast,
        # confirming it's enabled during inference).
        self._use_autocast = str(device).startswith("cuda")

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        # BGR -> RGB -> PIL.
        rgb = np.ascontiguousarray(frame[..., [2, 1, 0]])
        pil = self._Image.fromarray(rgb)
        if self._use_autocast:
            ctx = self._torch.autocast(device_type="cuda", dtype=self._torch.bfloat16)
        else:
            import contextlib

            ctx = contextlib.nullcontext()
        with ctx:
            state = self._processor.set_image(pil)
            output = self._processor.set_text_prompt(state=state, prompt=self._prompt)
        masks = _to_numpy(output.get("masks"))
        scores = _to_numpy(output.get("scores"))
        if masks is None or len(masks) == 0:
            return frame
        # Filter detections by confidence threshold.
        if scores is not None:
            try:
                keep = [
                    i for i, s in enumerate(np.asarray(scores).flatten().tolist())
                    if float(s) >= self._confidence_threshold
                ]
            except Exception:
                keep = list(range(len(masks)))
        else:
            keep = list(range(len(masks)))
        if not keep:
            return frame
        # Union of kept masks → single binary mask of frame size.
        masks_np = np.asarray(masks)
        if masks_np.ndim == 4:  # (N, 1, H, W)
            masks_np = masks_np.squeeze(1)
        if masks_np.ndim == 2:
            masks_np = masks_np[None, ...]
        union = np.zeros(masks_np.shape[-2:], dtype=bool)
        for i in keep:
            union |= masks_np[i] > 0
        return _apply_output_mode(frame, union.astype(np.uint8), self._output)


def _apply_output_mode(frame: Any, mask: np.ndarray, output: str) -> Any:
    h, w = frame.shape[:2]
    if mask.shape[:2] != (h, w):
        ys = np.linspace(0, mask.shape[0] - 1, h).astype(np.int64)
        xs = np.linspace(0, mask.shape[1] - 1, w).astype(np.int64)
        mask = mask[ys[:, None], xs[None, :]]
    binary = (mask > 0).astype(np.uint8)

    if output == "mask":
        out = np.zeros_like(frame)
        out[binary == 1] = 255
        return out
    if output == "matte":
        out = frame.copy()
        out[binary == 0] = 0
        return out
    if output == "highlight":
        out = frame.astype(np.float32)
        dimmed = out * _HIGHLIGHT_DIM
        out[binary == 0] = dimmed[binary == 0]
        return out.astype(frame.dtype)
    return frame


class SAM3TrackPlugin(Effect):
    name: ClassVar[str] = "sam3_track"
    model: ClassVar[type] = SAM3Track
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: SAM3Track, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        build_sam3_image_model, Sam3Processor = _import_sam3_or_raise()
        import torch
        from PIL import Image  # PIL is a hard dep elsewhere; deferred only for symmetry

        _LOG.info(
            "SAM3Track loading image model (first run downloads weights from "
            "HuggingFace facebook/sam3 - gated, needs hf auth login)"
        )
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = build_sam3_image_model(device=device)
        processor = Sam3Processor(model)
        return _SAM3TrackProcessor(
            processor=processor,
            pil_module=Image,
            prompt=ir.prompt,
            output=ir.output,
            confidence_threshold=ir.confidence_threshold,
            torch_module=torch,
            device=device,
        )
