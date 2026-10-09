"""ChromaKey effect plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import ChromaKey
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class _ChromaKeyProcessor(FrameProcessor):
    def __init__(self, color_bgr: tuple[int, int, int], similarity: float, blend: float) -> None:
        self._color = np.array(color_bgr, dtype=np.float32)
        self._similarity = max(1e-3, similarity)
        self._blend = blend

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        diff = frame.astype(np.float32) - self._color
        distance = np.sqrt(np.sum(diff * diff, axis=-1)) / 441.6729559300637
        mask = (distance < self._similarity).astype(np.float32)
        if self._blend > 0:
            soft = np.clip(
                (distance - self._similarity) / max(1e-3, self._blend) + 1.0, 0.0, 1.0
            )
            mask = 1.0 - np.maximum(mask, 1.0 - soft)
        else:
            mask = 1.0 - mask
        out = (frame.astype(np.float32) * mask[..., None]).astype(np.uint8)
        return out


class ChromaKeyPlugin(Effect):
    name: ClassVar[str] = "chroma_key"
    model: ClassVar[type] = ChromaKey
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset(
        {"ffmpeg_graph", "frame_pipeline"}
    )

    def compile_graph(self, ir: ChromaKey, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        r, g, b = ir.color
        color_hex = f"0x{r:02x}{g:02x}{b:02x}"
        chain = FilterChain()
        chain.add(
            FilterNode(
                name="chromakey",
                params={
                    "color": color_hex,
                    "similarity": f"{ir.similarity:.6f}",
                    "blend": f"{ir.blend:.6f}",
                },
            )
        )
        return chain

    def open(self, ir: ChromaKey, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        r, g, b = ir.color
        return _ChromaKeyProcessor((b, g, r), ir.similarity, ir.blend)
