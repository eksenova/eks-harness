"""LUT effect tests."""

from __future__ import annotations

from pathlib import Path

from eks_harness.video.ir import EffectAdapter
from eks_harness.video.ir.effects import LUT
from eks_harness.video.plugins.builtin.effects.lut import LUTPlugin
from eks_harness.video.render.context import RenderContext


def test_lut_round_trip() -> None:
    effect = LUT(path=Path("/tmp/teal_orange.cube"))
    rebuilt = EffectAdapter.validate_python(effect.model_dump())
    assert isinstance(rebuilt, LUT)
    assert str(rebuilt.path).endswith("teal_orange.cube")


def test_lut_expands_user_home() -> None:
    effect = LUT(path="~/luts/look.cube")
    assert "~" not in str(effect.path)


def test_lut_graph_chain(render_ctx: RenderContext) -> None:
    effect = LUT(path=Path("/tmp/look.cube"))
    serialized = LUTPlugin().compile_graph(effect, render_ctx).serialize()
    # The path contains characters ffmpeg-builder escapes; just check the
    # filter name and file token both appear.
    assert serialized.startswith("lut3d=")
    assert "look.cube" in serialized
