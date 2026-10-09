"""ChromaKey effect tests."""

from __future__ import annotations

import numpy as np

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import ChromaKey
from eks_harness.video.plugins.builtin.effects.chromakey import ChromaKeyPlugin
from eks_harness.video.render.context import RenderContext


def test_chromakey_round_trip() -> None:
    effect = ChromaKey(color=(0, 255, 0), similarity=0.2, blend=0.05)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, ChromaKey)
    assert rebuilt.similarity == 0.2


def test_chromakey_graph_chain(render_ctx: RenderContext) -> None:
    chain = ChromaKeyPlugin().compile_graph(
        ChromaKey(color=(0, 255, 0), similarity=0.1, blend=0.0), render_ctx
    )
    assert chain.serialize() == "chromakey=color=0x00ff00:similarity=0.100000:blend=0.000000"


def test_chromakey_frame_processor_zeros_matched_pixels(render_ctx: RenderContext) -> None:
    processor = ChromaKeyPlugin().open(
        ChromaKey(color=(0, 255, 0), similarity=0.3, blend=0.0), render_ctx
    )
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[..., 1] = 255
    out = processor.process(frame, t=0.0, frame_idx=0)
    assert int(out.sum()) == 0
