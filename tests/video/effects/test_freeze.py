"""Freeze effect tests."""

from __future__ import annotations

from eks_harness.video.ir import EffectAdapter, Seconds
from eks_harness.video.ir.effects import Freeze
from eks_harness.video.plugins.builtin.effects.freeze import FreezePlugin
from eks_harness.video.render.context import RenderContext


def test_freeze_round_trip() -> None:
    effect = Freeze(at=Seconds(t=1.0), hold=0.5)
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Freeze)
    assert rebuilt.hold == 0.5


def test_freeze_graph_chain_emits_tpad(render_ctx: RenderContext) -> None:
    effect = Freeze(at=Seconds(t=1.0), hold=0.25)
    chain = FreezePlugin().compile_graph(effect, render_ctx)
    serialised = chain.serialize()
    assert "tpad" in serialised
    assert "stop_mode=clone" in serialised
    assert "stop_duration=0.250000" in serialised
