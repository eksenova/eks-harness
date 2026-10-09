"""Invert (negate) effect plugin (ffmpeg ``negate``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.ir.effects import Invert
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["InvertPlugin"]


class InvertPlugin(Effect):
    name: ClassVar[str] = "invert"
    model: ClassVar[type] = Invert
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Invert, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        chain = FilterChain()
        chain.add(FilterNode(name="negate"))
        return chain
