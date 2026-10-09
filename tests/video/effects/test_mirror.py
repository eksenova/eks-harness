"""Mirror effect tests."""

from __future__ import annotations

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import Mirror
from eks_harness.video.plugins.builtin.effects.mirror import MirrorPlugin
from eks_harness.video.render.context import RenderContext


def test_mirror_round_trip() -> None:
    effect = Mirror(axis="horizontal")
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, Mirror)
    assert rebuilt.axis == "horizontal"


def test_mirror_horizontal_chain(render_ctx: RenderContext) -> None:
    chain = MirrorPlugin().compile_graph(Mirror(axis="horizontal"), render_ctx)
    assert chain.serialize() == "hflip"


def test_mirror_both_chain(render_ctx: RenderContext) -> None:
    chain = MirrorPlugin().compile_graph(Mirror(axis="both"), render_ctx)
    assert chain.serialize() == "hflip,vflip"
