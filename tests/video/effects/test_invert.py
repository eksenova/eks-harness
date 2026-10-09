"""Invert effect tests."""

from __future__ import annotations

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import Invert
from eks_harness.video.plugins.builtin.effects.invert import InvertPlugin
from eks_harness.video.render.context import RenderContext


def test_invert_round_trip() -> None:
    effect = Invert()
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Invert)


def test_invert_graph_chain(render_ctx: RenderContext) -> None:
    chain = InvertPlugin().compile_graph(Invert(), render_ctx)
    assert chain.serialize() == "negate"
